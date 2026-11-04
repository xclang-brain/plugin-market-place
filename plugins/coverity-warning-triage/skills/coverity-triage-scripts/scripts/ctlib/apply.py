"""Commands of /coverity-apply: preview (what will be applied, and the confirmation string) and apply-code.

The write-back to Coverity is done by the MCP tool ``update_triage`` between the two commands,
with the same confirmation string; it reads the ``triage`` list of ``apply-plan.json``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import approvals, assess, metrics, oplog
from .common import CtError, now, read_json, sha, write_json
from .config import add_no_grouping
from .models import TriageResult
from .report import CONFIDENCE_JA
from .run import Run
from .vcs import GitVcs, SvnVcs

PLAN_FILE = "apply-plan.json"
CODE_RESULT_FILE = "apply-code.result.json"  # the result of apply-code for one confirmation string
LABELS = {"fix": "修正", "deviation": "逸脱", "reject": "却下"}


def _token(path: Path) -> str:
    return sha(path.read_bytes())[:12]


def preview(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    # approvals.json alone is not enough: items approved in the screen while the run went on have one,
    # the others get the AI's recommendation only from ct.py summary
    if not approvals.summary_made(run):
        raise CtError("一覧がありません（/coverity-run の最後に ct.py summary で作ります）")
    decided = approvals.decisions(run)
    triage, code, reject, skipped, problems, nothing = [], [], [], [], [], []
    for item in run.state.items:
        decision = decided.get(item.id)
        if decision is None or item.status != "done":
            continue
        saved = read_json(run.item_dir(item.id) / "saved.json", {})
        # Parts applied by an earlier /coverity-apply are not applied again.
        parts = {"fix": ["fix"], "deviation": ["coverity"] + (["annotation"] if "annotation" in saved else []),
                 "reject": ["reject"]}[decision]
        todo = [p for p in parts if p not in item.applied]
        if not todo:
            skipped.append(item.id)
            continue
        if decision == "fix":
            if "fix" not in saved and approvals.already_fixed(run, item.id):
                nothing.append(item.id)  # the latest revision is already fixed: nothing to take in
                continue
            if "fix" not in saved:
                problems.append(f"{item.id}: 修正の差分がないため「修正」は反映できません（「逸脱」か「却下」にしてください）")
                continue
            code.append({"id": item.id, "cids": item.cids, "kind": "fix", **saved["fix"]})
        elif decision == "deviation":
            if "coverity" in todo:
                try:
                    attrs, comment = approvals.deviation_for(run, item.id)
                except CtError as exc:
                    problems.append(f"{item.id}: {exc}")
                    continue
                # stream: the triage is written in the triage store that the stream of the warnings uses (MCP update_triage)
                triage.append({"id": item.id, "cids": item.cids, "stream": run.state.stream, **attrs.model_dump(),
                               "comment": comment})
            if "annotation" in todo:
                code.append({"id": item.id, "cids": item.cids, "kind": "annotation", **saved["annotation"]})
        else:
            reject.append({"id": item.id, "cids": item.cids, "group": item.is_group})
    if problems:
        raise CtError("反映の前に直すところがあります", problems=problems)
    plan = {"run_id": run.state.run_id, "created_at": now(), "triage": triage, "code": code, "reject": reject}
    path = run.dir / PLAN_FILE
    write_json(path, plan)
    token = _token(path)
    counts = {"修正": sum(1 for c in code if c["kind"] == "fix"), "逸脱": len(triage), "却下": len(reject)}
    oplog.log(run.dir, "preview", counts=counts, token=token)
    if triage or code or reject:
        next_step = ("件数を人に見せて同意を得る。同意を得たら、逸脱が 1 件以上あれば MCP の update_triage"
                     "（repo_root, plan_file, confirmation_token）を done まで呼び、そのあと ct.py apply-code --token")
    else:
        next_step = ("反映するものがありません（承認された作業が無いか、すべて反映済みです）。承認するときは、画面の「結果」で"
                     f"承認してから、もう一度 ct.py preview（画面: ct.py gui --repo {run.repo} --run {run.dir} --view summary）")
    out_nothing = {"already_fixed_on_latest": nothing} if nothing else {}
    return {"ok": True, "counts": counts, "already_applied": skipped, **out_nothing,
            "items": [{"id": e["id"], "approval": label, "cids": e["cids"]}
                      for label, entries in (("逸脱", triage), ("修正", [c for c in code if c["kind"] == "fix"]),
                                             ("却下", reject)) for e in entries],
            "plan_file": str(path), "confirmation_token": token, "next": next_step}


def _pr_text(run: Run, entry: dict[str, Any]) -> tuple[str, str]:
    from .triage import review_of, verify_results  # late import: triage is the larger module

    result = TriageResult.model_validate(read_json(run.result_file(entry["id"])))
    verify = verify_results(run).get(entry["id"])
    result = assess.effective(result, review_of(run, run.state.item(entry["id"])), verify)  # as the reports show it
    cids = entry["cids"]
    title = f"Coverity CID {', '.join(map(str, cids[:5]))}{' ほか' if len(cids) > 5 else ''}: {result.fix.summary}"
    if entry["kind"] == "annotation":
        title = f"Coverity CID {', '.join(map(str, cids[:5]))}: 逸脱の注釈"
    body = "\n".join([
        "## 見立て", "", result.verdict.summary, "", result.verdict.rationale, "",
        "## 変更の内容", "", result.fix.summary if entry["kind"] == "fix" else result.deviation.comment, "",
        "## 影響とリスク", "", result.fix.impact, "",
        f"確信度: {CONFIDENCE_JA[result.confidence]}（Coverity トリアージの AI が作成し、人が承認）", ""])
    return title[:200], body


ERRORS_NOTE = "errors があれば人に伝える（反映されなかった項目は、もう一度 /coverity-apply で反映できる）。"
# update_triage records "Coverity REST PUT ... が失敗しました（500）: ..." for a write the server failed.
SERVER_ERROR = re.compile(r"(?:HTTP\s*|[（(]\s*)5\d\d(?!\d)|(?<![0-9A-Za-z])5xx(?![0-9A-Za-z])", re.IGNORECASE)


def _coverity_failures(result: dict[str, Any], token: str, not_written: list[str]) -> dict[str, str]:
    """The reason of each item update_triage sent for this plan and Coverity did not take
    (``failed`` of apply-plan.result.json). A failure recorded for another plan is left out."""
    out: dict[str, str] = {}
    for record in (result.get("failed") or {}).values():
        if (isinstance(record, dict) and record.get("id") in not_written
                and record.get("token", token) == token):
            out[record["id"]] = str(record.get("error") or "理由は記録されていません")
    return out


def _not_written_note(not_written: list[str], failures: dict[str, str]) -> str:
    def reasons(ids: list[str]) -> str:
        return "、".join(f"{i}: {failures[i]}" for i in ids)

    server = [i for i in not_written if i in failures and SERVER_ERROR.search(failures[i])]
    refused = [i for i in not_written if i in failures and i not in server]
    unsent = [i for i in not_written if i not in failures]
    note = ""
    if unsent:
        note += (f"逸脱 {len(unsent)} 件（{', '.join(unsent)}）は、まだ Coverity に書き戻していません"
                 "（MCP の update_triage の結果 apply-plan.result.json にありません）。書き戻すときは、MCP の update_triage を"
                 "（repo_root, plan_file, confirmation_token）で done まで呼び、そのあと同じ確認用の文字列で ct.py apply-code"
                 " をもう一度実行する（push はし直しません）。")
    if refused:
        note += (f"逸脱 {len(refused)} 件は、Coverity が書き戻しを断りました（{reasons(refused)}）。"
                 "値を直して ct.py preview からやり直す。")
    if server:
        note += (f"逸脱 {len(server)} 件は、Coverity の不具合か一時的な失敗で書き戻せませんでした（{reasons(server)}）。"
                 "少し待って、MCP の update_triage を同じ確認用の文字列で呼び直し、そのあと ct.py apply-code を"
                 "もう一度実行する（push はし直しません）。")
    return note


def _repeat(run: Run, plan: dict[str, Any], previous: dict[str, Any], written_ids: set[str],
            failures: dict[str, str]) -> dict[str, Any]:
    """apply-code again with the same confirmation string: the code was applied already, so nothing is
    pushed (or patched) again and the earlier result is returned; only write-backs to Coverity that
    update_triage made since then are recorded."""
    out = previous["result"]
    registered = sorted(written_ids & {e["id"] for e in plan["triage"]})
    newly = [i for i in registered if i not in out["coverity"]["registered"]]
    if newly:
        with run.update() as state:
            for item_id in newly:
                state.item(item_id).applied.update({"coverity": "登録済み"}, at=now())
    not_written = [e["id"] for e in plan["triage"] if e["id"] not in written_ids]
    oplog.log(run.dir, "apply-code", outcome={i: {"coverity": "登録済み"} for i in newly}, errors=[], repeated=True)
    return {**out, "coverity": {"registered": registered, "not_registered": not_written}, "repeated": True,
            "next": (f"この確認用の文字列の反映は、前に済んでいます（{previous['at']}）。push も差分の適用もし直さず、"
                     "前の結果を返しました。プルリクエストは作り直さない。") + (ERRORS_NOTE if out.get("errors") else "")
                    + _not_written_note(not_written, failures)}


def apply_code(run_dir: str, token: str) -> dict[str, Any]:
    run = Run(run_dir)
    path = run.dir / PLAN_FILE
    if not path.is_file() or _token(path) != token.strip():
        raise CtError("確認用の文字列が反映の内容と一致しません。ct.py preview からやり直してください")
    plan = read_json(path)

    # Coverity: what update_triage reported.
    triage_result = read_json(path.with_name("apply-plan.result.json"), {})
    written = triage_result.get("written", {})
    written_ids = {v["id"] for v in written.values()}
    not_written = [e["id"] for e in plan["triage"] if e["id"] not in written_ids]
    failures = _coverity_failures(triage_result, token.strip(), not_written)
    previous = read_json(run.dir / CODE_RESULT_FILE, {})
    if previous.get("token") == token.strip():
        return _repeat(run, plan, previous, written_ids, failures)

    vcs = run.vcs
    outcome: dict[str, dict[str, Any]] = {}
    pull_requests, svn_applied, errors = [], [], []
    for entry in plan["triage"]:
        if entry["id"] in written_ids:
            outcome.setdefault(entry["id"], {})["coverity"] = "登録済み"

    # Code.
    if isinstance(vcs, GitVcs) and run.config.options.per_run_branch and plan["code"]:
        commits = [(f"{c['id']}:{c['kind']}", c["commit"]) for c in plan["code"]]
        branch, included, conflicted = vcs.combine(run.copy_dir("combine"), run.state.latest_revision or "",
                                                   commits, f"{run.config.vcs.branch_prefix}run-{run.state.run_id}")
        vcs.remove_copy(run.copy_dir("combine"))
        errors += [f"{key}: ほかの修正と同じ箇所を変えているため、まとめたブランチに入れられませんでした" for key in conflicted]
        if branch:
            try:
                vcs.push(branch)
                body = "\n".join(f"- {_pr_text(run, c)[0]}" for c in plan["code"] if f"{c['id']}:{c['kind']}" in included)
                title = f"Coverity トリアージ {run.state.run_id}（{len(included)} 件）"
                pull_requests.append({"ids": included, "branch": branch, "base": run.config.vcs.base_branch,
                                      "title": title, "body": body, "compare_url": vcs.compare_url(branch, title, body)})
                for key in included:
                    outcome.setdefault(key.split(":")[0], {})[key.split(":")[1]] = {"branch": branch, "pushed": True}
            except CtError as exc:
                errors.append(f"{branch}: push できませんでした: {exc}")
    else:
        for entry in plan["code"]:
            key = f"{entry['id']}:{entry['kind']}"
            try:
                if isinstance(vcs, SvnVcs):
                    vcs.apply_patch(Path(entry["patch"]))
                    svn_applied.append(key)
                    outcome.setdefault(entry["id"], {})[entry["kind"]] = {"svn_patch": "適用済み"}
                else:
                    vcs.push(entry["branch"])
                    title, body = _pr_text(run, entry)
                    pull_requests.append({"ids": [key], "branch": entry["branch"], "base": run.config.vcs.base_branch,
                                          "title": title, "body": body,
                                          "compare_url": vcs.compare_url(entry["branch"], title, body)})
                    outcome.setdefault(entry["id"], {})[entry["kind"]] = {"branch": entry["branch"], "pushed": True}
            except CtError as exc:
                errors.append(f"{key}: {exc}")

    # Rejected groups are worked on one by one next time.
    rejected_groups = [c for r in plan["reject"] if r["group"] for c in r["cids"]]
    if rejected_groups:
        add_no_grouping(run.repo, rejected_groups)
    for entry in plan["reject"]:
        outcome.setdefault(entry["id"], {})["reject"] = True

    with run.update() as state:
        for item_id, done in outcome.items():
            state.item(item_id).applied.update(done, at=now())
    if run.config.options.metrics:
        metrics.record(run, plan)
    oplog.log(run.dir, "apply-code", outcome=outcome, errors=errors)
    out = {"ok": True, "pull_requests": pull_requests, "svn_applied": svn_applied,
           "coverity": {"registered": sorted(written_ids & {e["id"] for e in plan["triage"]}),
                        "not_registered": not_written},
           "rejected": [r["id"] for r in plan["reject"]], "errors": errors,
           "next": (ERRORS_NOTE if errors else "") + _not_written_note(not_written, failures) + ("pull_requests の各項目について、GitHub のプルリクエストを作る（Copilot の GitHub の機能が"
                    "使えればそれで。使えなければ compare_url を人に示す）" if pull_requests else
                    "svn の変更はワーキングコピーに入りました。内容を確かめてコミットするよう人に伝える" if svn_applied
                    else "反映は終わりました")}
    # A second call with the same confirmation string returns this, and never pushes again (no second pull request).
    write_json(run.dir / CODE_RESULT_FILE, {"token": token.strip(), "at": now(), "result": out})
    return out
