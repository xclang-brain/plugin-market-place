"""Commands of /coverity-run: new-run, plan, next, brief, finish, fail, resume, status, runs, summary."""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import approvals, assess, oplog
from .common import CtError, file_lock, read_json, resolve_path, skills_dir, write_json, write_text
from .config import (CONFIG_DIR, CONFIG_FILE, FilterSpec, knowledge_path, load_config, load_filter,
                     load_no_grouping, output_dir)
from .grouping import build_work_items
from .models import Event, Issue, IssueDetail, TriageResult
from .pathmap import PathMapper
from .report import ItemReport, render_item, render_item_html, render_summary, render_summary_html
from .run import ItemState, Run, create_run, finish_item, list_runs, unfinished
from .vcs import detect

VCS_LOCK_SECONDS = 1800  # creating a work copy of a large repository can take long
DEVIATION_COMMENT_MAX = 200  # characters: a deviation comment says only why the warning is no problem
PROGRESS_FILE = "progress.json"  # written by the worker subagent at each step (best effort)
PREVIOUS_RESULT = "result.previous.json"  # the result before the review sent the item back
REWORK_FRESH = "rework-fresh"  # the earlier fix could not be applied to the work copy again


def _repo(path: str) -> Path:
    repo = resolve_path(path)
    if not repo.is_dir():
        raise CtError(f"フォルダがありません: {repo}")
    if detect(repo) is None:
        raise CtError(f"git / svn のリポジトリの一番上のフォルダを指定してください: {repo}")
    return repo


def _log(run: Run, event: str, **fields: Any) -> None:
    oplog.log(run.dir, event, oplog.secrets_of(run.config), **fields)


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


# ---- new-run ---------------------------------------------------------------------------------


def new_run(repo_root: str, filter_name: str | None, overrides: dict[str, Any], limit: int | None,
            verify: str | None, initial: bool | None = None) -> dict[str, Any]:
    repo = _repo(repo_root)
    config = load_config(repo)
    if limit is not None and not 1 <= limit <= config.max_items:
        raise CtError(f"件数の上限（--limit）は 1 から {config.max_items}（設定 max_items）までの数にしてください",
                      limit=limit, max_items=config.max_items)
    if filter_name is None:
        names = sorted(p.name for p in (repo / CONFIG_DIR / "filters").glob("*.yaml"))
        if not names:
            raise CtError("条件ファイル（.coverity-triage/filters/*.yaml）がありません（/coverity-setup で作ります）")
        filter_name = "untriaged.yaml" if "untriaged.yaml" in names else names[0]
    spec = load_filter(repo, filter_name, overrides)
    if len(spec.streams) != 1:
        raise CtError("条件にはストリームを 1 つだけ指定してください", streams=spec.streams)
    requested = limit or spec.limit or config.max_items
    run_limit = min(requested, config.max_items)
    mode = verify or config.verify.default
    if mode not in ("none", "build", "build+analyze"):
        raise CtError("検証の方法は none / build / build+analyze のどれかです")
    if mode != "none" and not config.verify.build_command:
        raise CtError("ビルドでの検証の設定がありません（/coverity-setup の「ビルドでの検証」で設定します）")
    first = config.verify.initial if initial is None else initial
    if first and not config.verify.build_command:
        raise CtError("初回解析の設定（ビルドのコマンド）がありません（/coverity-setup の「ビルドでの検証」で設定します）")
    output = output_dir(repo, config)
    if _inside(output, repo):
        raise CtError(f"出力先がリポジトリの中です: {output}（設定 output_dir をリポジトリの外にしてください）")

    agents = config.agents
    run = create_run(output, repo=repo, filter_name=spec.name, stream=spec.streams[0], limit=run_limit,
                     verify_mode=mode, initial=first, integration=agents.integration, review=agents.review,
                     max_rework=agents.max_rework)
    write_json(run.dir / "filter.json", spec.model_copy(update={"limit": run_limit}).model_dump())
    shutil.copyfile(repo / CONFIG_DIR / CONFIG_FILE, run.dir / "config.snapshot.yaml")
    _log(run, "new-run", filter=spec.model_dump(), limit=run_limit, verify=mode, initial=first,
         integration=agents.integration, review=agents.review)
    out = {"ok": True, "run_dir": str(run.dir), "repo_root": str(repo), "stream": spec.streams[0],
           "filter_file": str(run.dir / "filter.json"), "issues_file": str(run.dir / "issues.json"),
           "work_dir": str(run.dir / "work"), "limit": run_limit, "verify": mode, "filter": spec.model_dump(),
           "initial": first, "integration": agents.integration, "review": agents.review,
           "next": "ct.py tick で、次にすることを聞く"}
    if requested > run_limit:
        out["note"] = f"1 回の上限（設定 max_items）が {config.max_items} 件のため、{run_limit} 件にしました"
    return out


# ---- plan ------------------------------------------------------------------------------------


def plan(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    if any(i.status != "pending" for i in run.state.items):
        raise CtError("この実行はもう始まっています（続きは ct.py next、やり直しは ct.py resume）")
    found = read_json(run.dir / "issues.json")
    if not found.get("finished_at"):
        raise CtError("警告の検索が終わっていません。MCP の search_issues を done になるまで呼んでください")
    config = run.config
    mapper = PathMapper(run.repo, config.coverity.path_strip_prefixes)
    mapped, issues = [], []
    for record in found.get("issues") or []:
        path_notes: list[str] = []
        issue = mapper.map_issue(Issue.model_validate(record), path_notes)
        issues.append(issue)
        mapped.append({**issue.model_dump(exclude_none=True), "path_notes": path_notes})
    write_json(run.dir / "issues.mapped.json", mapped)
    items = build_work_items(issues, load_no_grouping(run.repo))

    vcs = run.vcs
    notes = []
    analyzed = found.get("analyzed_revision")
    if found.get("revision_error"):
        notes.append(f"解析リビジョンを Coverity から取れませんでした: {found['revision_error']}")
    if analyzed and not vcs.has_revision(analyzed):
        notes.append(f"Coverity が解析したリビジョン {analyzed} がリポジトリにありません。")
        analyzed = None
    if analyzed and run.state.initial:
        from . import initial

        if initial.status(run).get("dirty"):
            notes.append("初回解析は、コミットしていない変更を含む手元のフォルダで行いました（登録したリビジョンは "
                         f"{analyzed[:10]}）。")
            analyzed = None
    if not analyzed:
        notes.append("解析したリビジョンが分からないため、手元のコードで調べ、警告とのずれを確かめます。")
    latest, latest_note = vcs.resolve_latest()
    if latest_note:
        notes.append(latest_note)

    with run.update() as state:
        state.items = [ItemState(id=i.id, cids=i.cids, cluster=i.cluster) for i in items]
        state.planned = True
        state.found = len(issues)
        state.limited = bool(found.get("limited"))
        state.latest_revision = latest
        state.analyzed_revision = analyzed
        state.revision_notes = notes
    groups = [{"id": i.id, "cids": i.cids} for i in items if i.id.startswith("G")]
    _log(run, "plan", issues=len(issues), items=len(items), groups=groups, latest=latest, analyzed=analyzed)
    excluded = found.get("excluded_count") or 0
    parallel = max(1, min(config.parallel, len(items)))
    return {"ok": True, "issues": len(issues), "limited": state.limited, "items": len(items),
            "groups": groups, "latest_revision": latest, "analyzed_revision": analyzed, "notes": notes,
            "excluded": excluded, "parallel": parallel, "parallel_setting": config.parallel,
            "next": (f"ct.py next を {parallel} 回続けて呼んで作業を取り出し、それぞれのサブエージェントを同時に起動する"
                     if parallel > 1 else "ct.py next で作業を取り出す") if items else
            "対象の警告がありません" + (f"（Coverity が返した {excluded} 件は条件に合わず外しました。"
                                         "合わなかった項目と値は issues.json の excluded にあります）"
                                         if excluded else "")}


# ---- next ------------------------------------------------------------------------------------


def next_item(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    if not run.state.planned:
        raise CtError("作業の一覧がありません。先に ct.py plan を実行してください")
    config = run.config
    vcs = run.vcs
    fingerprint = vcs.fingerprint()  # to notice if the person's files change while the item is worked on
    with run.update() as state:
        busy = {i.slot for i in state.items if i.status == "in_progress"}
        free = [s for s in range(1, config.parallel + 1) if s not in busy]
        item = next((i for i in state.items if i.status == "pending"), None)
        if item is not None and free:
            item.status, item.slot = "in_progress", free[0]
            item.attempts += 1
            item.started = time.time()
            item.error, item.warnings, item.repo_fingerprint = "", [], fingerprint
        # how many more items can be started now, without waiting for this one
        more = min(len(free) - 1, sum(1 for i in state.items if i.status == "pending")) if item and free else 0
    if item is None:
        counts = run.state.counts()
        waiting = counts["in_progress"]
        return {"ok": True, "item": None, "counts": counts,
                "next": (f"処理中の作業が {waiting} 件あります。終わるのを待ってから ct.py summary" if waiting
                         else "残りの作業はありません。ct.py summary で一覧を作る"
                         + ("（処理できなかった作業は ct.py resume で戻してやり直せます）" if counts["error"] else ""))}
    if not free:
        return {"ok": True, "item": None, "counts": run.state.counts(),
                "next": f"同時に進められる数（設定 parallel = {config.parallel}）に達しています。"
                        "処理中の作業を ct.py finish か ct.py fail で終えてから呼んでください"}

    latest = run.state.latest_revision or ""
    previous = run.dir / "patches" / f"{item.id}-fix.patch"
    start_from = previous if item.rework and previous.is_file() else None
    fix_dir = run.copy_dir("fix", item.slot)
    # Creating a new copy of a large repository can take minutes: show it on the monitor meanwhile.
    _log(run, "copy", item=item.id, slot=item.slot,
         create=not ((fix_dir / ".git").exists() or (fix_dir / ".svn").exists()))
    try:
        with file_lock(run.dir / "work" / "vcs", timeout=VCS_LOCK_SECONDS):
            vcs.reset_copy(fix_dir, latest)
            fresh = run.item_dir(item.id) / REWORK_FRESH
            fresh.unlink(missing_ok=True)
            if start_from is not None:  # sent back by the review: the worker changes its earlier fix
                if vcs.apply_to_copy(fix_dir, start_from):
                    vcs.reset_copy(fix_dir, latest)  # made anew from the latest revision
                    fresh.parent.mkdir(parents=True, exist_ok=True)
                    fresh.write_text("前の修正を当てられませんでした\n", encoding="utf-8")
            if config.options.annotation:
                vcs.reset_copy(run.copy_dir("annotation", item.slot), latest)
            # the analyzed copy is shared and read-only: reset it only while no other item is reading it
            if run.state.analyzed_revision and (not busy or not run.copy_dir("analyzed").is_dir()):
                vcs.reset_copy(run.copy_dir("analyzed"), run.state.analyzed_revision)
    except CtError as exc:
        with run.update() as state:
            finish_item(state.item(item.id), "error", f"修正用のコピーを用意できませんでした: {exc}")
        raise CtError(f"修正用のコピーを用意できませんでした: {exc}",
                      next="ほかの作業でも同じことが起きるため、止めて利用者に伝える（直したら ct.py resume で再開できる）") from exc
    folder = run.item_dir(item.id)
    folder.mkdir(parents=True, exist_ok=True)
    result_file = run.result_file(item.id)
    if item.rework and result_file.is_file() and not (folder / PREVIOUS_RESULT).is_file():
        result_file.replace(folder / PREVIOUS_RESULT)  # the worker reads what the review checked
    result_file.unlink(missing_ok=True)
    (folder / PROGRESS_FILE).unlink(missing_ok=True)  # the progress of an earlier attempt
    with run.update() as state:
        state.item(item.id).fix_rounds = 0
    missing = [c for c in item.cids if not (folder / f"issue-{c}.json").is_file()]
    _log(run, "next", item=item.id, slot=item.slot, attempt=item.attempts, rework=bool(item.rework))
    return {"ok": True, "item": item.id, "cids": item.cids, "is_group": item.is_group,
            "repo_root": str(run.repo), "stream": run.state.stream, "item_dir": str(folder),
            "counts": run.state.counts(), "can_start_more": more, "missing_issue_files": missing,
            "next": ("MCP の get_issues（repo_root, stream, cids, output_dir=item_dir）を done になるまで呼び、"
                     "そのあと ct.py brief" if missing else "ct.py brief（警告経路は取得済み）")
                    + (f"。この作業の終わりを待たずに、ct.py next であと {more} 件を取り出して同時に進める" if more else "")}


# ---- brief -----------------------------------------------------------------------------------


def _details(run: Run, item: ItemState) -> dict[int, IssueDetail]:
    folder = run.item_dir(item.id)
    missing = [c for c in item.cids if not (folder / f"issue-{c}.json").is_file()]
    if missing:
        raise CtError("警告経路のファイルがありません。MCP の get_issues を呼んでください", missing_cids=missing,
                      output_dir=str(folder))
    return {c: IssueDetail.model_validate(read_json(folder / f"issue-{c}.json")) for c in item.cids}


def _mapped(run: Run) -> tuple[dict[int, Issue], dict[int, list[str]]]:
    data = read_json(run.dir / "issues.mapped.json")
    return ({d["cid"]: Issue.model_validate(d) for d in data}, {d["cid"]: d.get("path_notes") or [] for d in data})


def _item_view(run: Run, item: ItemState) -> tuple[list[Issue], dict[int, list[Event]], str, list[str]]:
    """Issues and warning paths with repository-relative paths, and notes about the paths."""
    issues, path_notes = _mapped(run)
    details = _details(run, item)
    mapper = PathMapper(run.repo, run.config.coverity.path_strip_prefixes)
    notes: list[str] = []
    events: dict[int, list[Event]] = {}
    description = ""
    for cid in item.cids:
        notes += [n for n in path_notes.get(cid, []) if n not in notes]
        detail = details[cid]
        if detail.error:
            notes.append(f"CID {cid} の警告経路を Coverity から取れませんでした: {detail.error}")
        events[cid] = [mapper.map_event(e, notes) for e in detail.events]
        description = description or detail.checker_description
    return [issues[c] for c in item.cids], events, description, notes


def _drift_check(root: Path, issues: list[Issue], events: dict[int, list[Event]]) -> list[str]:
    """Signs that the local code differs from the analyzed code."""
    findings = []
    for issue in issues:
        files = {issue.file} | {e.file for e in events.get(issue.cid, [])}
        for rel in sorted(files):
            path = root / rel
            if not path.is_file():
                findings.append(f"CID {issue.cid}: ファイルがありません: {rel}")
                continue
            data = path.read_bytes()
            count = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
            lines = [e.line for e in events.get(issue.cid, []) if e.file == rel and e.line]
            if lines and max(lines) > count:
                findings.append(f"CID {issue.cid}: {rel} は {count} 行しかなく、警告の {max(lines)} 行目がありません")
            if rel == issue.file and issue.function and issue.function.split("::")[-1].encode() not in data:
                findings.append(f"CID {issue.cid}: {rel} に関数 {issue.function} が見つかりません")
    return findings


def brief(run_dir: str, item_id: str) -> dict[str, Any]:
    run = Run(run_dir)
    item = run.state.item(item_id)
    if item.status != "in_progress":
        raise CtError(f"{item_id} は処理中ではありません（状態: {item.status}）")
    issues, events, description, notes = _item_view(run, item)
    config = run.config
    analyzed = run.state.analyzed_revision
    investigate = run.copy_dir("analyzed") if analyzed else run.repo
    drift = [] if analyzed else _drift_check(run.repo, issues, events)
    knowledge = knowledge_path(run.repo)
    result = run.result_file(item_id)
    fix_dir = run.copy_dir("fix", item.slot)

    lines = [f"# 調査の指示: {item_id}", "",
             "## すること", "",
             f"Coverity の警告 {len(issues)} 件（CID {', '.join(map(str, item.cids))}）を調べ、"
             "**修正案と逸脱コメント案の両方**を作り、結果ファイルを書きます。",
             "最初に手順書 `triage-investigation` を読み、その手順に沿って進めます。"
             "チェッカー別の観点は `checker-knowledge`、修正の決まりは `code-fix`、逸脱コメントの書き方は "
             "`deviation-comment`、結果ファイルの書き方は `triage-report` にあります。"
             "手順書は、下の表の「手順書のフォルダ（読む）」の下の `<名前>/SKILL.md` と、そのフォルダの `references/` です。", "",
             "## フォルダとファイル", "",
             "| 用途 | 場所 |", "|---|---|",
             f"| 調べるコード（読むだけ。変更しない） | `{investigate}` |",
             f"| 修正するコード（ここだけを変更する） | `{fix_dir}` |",
             f"| 手順書のフォルダ（読む） | `{skills_dir()}` |"]
    if config.options.annotation:
        lines.append(f"| アノテーションを入れるコード（逸脱案の注釈だけを入れる） | `{run.copy_dir('annotation', item.slot)}` |")
    lines.append(f"| 結果ファイル（書く） | `{result}` |")
    progress = run.item_dir(item_id) / PROGRESS_FILE
    lines.append(f"| 進み具合（書く。節目ごとに上書き） | `{progress}` |")
    if knowledge.is_file():
        lines.append(f"| プロジェクトの知識（読む） | `{knowledge}` |")
    example = json.dumps({"item": item_id, "phase": "judge", "note": "呼び出し元を確かめている"}, ensure_ascii=False)
    lines += ["", f"- 進み具合のファイルには、節目ごとに次の形の JSON を上書きします（人が画面で見ます）: `{example}`",
              "  phase は read（警告経路とコードを読む）→ judge（真偽を判断する）→ fix（修正を作る）→ "
              "deviation（逸脱コメントを書く）→ result（結果ファイルを書く）の順です。note には今していることを短く書きます。"
              "書けなくても調査は続けます。",
              "- 下の警告のパスは、どのフォルダでもリポジトリの一番上からの相対パスです。",
              (f"- 調べるコードは、Coverity が解析したリビジョン {analyzed} です。" if analyzed else
               "- Coverity が解析したリビジョンが分からないため、調べるコードは利用者の手元のコードです。"
               "警告の行と内容が合っているか（ずれ）を確かめ、結果の revision_drift に書きます。"
               "このフォルダは利用者の作業中のファイルなので、絶対に変更しないでください。"),
              f"- 修正するコードは、取り込み先の最新のリビジョン {run.state.latest_revision} です。"
              "最新のリビジョンで既に直っていれば、修正はせずに fix.already_fixed_on_latest を true にします。",
              "- 上の表にないフォルダのファイルは変更しません。ターミナルは使いません。", ""]
    lines += ["## 警告", ""]
    for issue in issues:
        lines += [f"### CID {issue.cid} — {issue.checker}（Impact {issue.impact or '-'}）", "",
                  f"- 場所: `{issue.file}`:{issue.line or '-'}" + (f" {issue.function}()" if issue.function else ""),
                  f"- CWE: {issue.cwe or '-'}", "- 警告経路:", ""]
        evs = events.get(issue.cid) or []
        lines += [f"  {n}. {'★ ' if e.main else ''}`{e.file}`:{e.line or '-'} {e.tag} — {e.description}"
                  for n, e in enumerate(evs, 1)] or ["  （取れませんでした）"]
        lines.append("")
    if description:
        lines += ["チェッカーの説明（Coverity）:", "", description, ""]
    if drift:
        lines += ["## 機械的に見つけたずれ", "", *[f"- {d}" for d in drift], ""]
    if notes:
        lines += ["## パスなどの注意", "", *[f"- {n}" for n in notes], ""]
    if item.is_group:
        lines += ["## グループ", "",
                  "同じ原因と思われる警告をまとめた作業です。すべてが同じ原因かを確かめ、原因が違う CID は "
                  "group_excluded_cids に入れます（その CID は個別の作業に戻ります）。修正と逸脱コメントは、"
                  "グループに残す CID に対して作ります。", ""]
    lines += _merge_lines(run, item) + _rework_lines(run, item)
    lines += ["## 終わったら", "",
              f"結果ファイル `{result}` を書いたら、呼び出し元に 1 行だけ返します。"
              f"例: 「{item_id}: 修正を推奨（確信度 高）— fgets が失敗したとき fp を閉じていない」", ""]
    path = run.item_dir(item_id) / "brief.md"
    write_text(path, "\n".join(lines))
    _log(run, "brief", item=item_id, drift=len(drift))
    return {"ok": True, "brief": str(path), "result_file": str(result),
            "prompt": f"Coverity の警告を調べます。指示ファイル {path} を読み、書かれた手順に従ってください。",
            "next": "サブエージェント coverity-triage-worker を起動し、prompt をそのまま渡す。終わったら ct.py finish"}


def _merge_lines(run: Run, item: ItemState) -> list[str]:
    """An item the integration made, whose fix a worker writes anew from the members' work."""
    merged = read_json(run.dir / "integration" / "merged.json", {}).get(item.id)
    if not item.merged_from or not merged:
        return []
    what = ("検証からの指摘を直すときの参考に、元の作業の結果と差分を読みます。" if item.rework else
            "それぞれの結果と差分を読み、すべての CID に効く 1 つの修正を、修正するコードのフォルダで作り直します"
            "（修正するコードは最新のリビジョンのままで、まだ何も直していません）。")
    lines = ["## 統合の指示", "",
             f"統合エージェントが、作業 {', '.join(item.merged_from)} は同じ原因だと判断し、1 つの作業にまとめました。{what}",
             f"統合の理由：{merged['reason']}", "",
             "| 元の作業 | 結果ファイル | 修正の差分 |", "|---|---|---|"]
    for member in item.merged_from:
        patch = run.dir / "patches" / f"{member}-fix.patch"
        lines.append(f"| {member} | `{run.result_file(member)}` | " + (f"`{patch}`" if patch.is_file() else "なし") + " |")
    lines += ["", f"統合エージェントがまとめた見立てと逸脱コメントは `{run.dir / 'integration' / 'merged.json'}` の "
              f"`{item.id}` にあります。逸脱コメントは、内容がコードと合っていればそのまま使います。", ""]
    return lines


def _rework_lines(run: Run, item: ItemState) -> list[str]:
    """An item the review sent back: what to change, and where the earlier result is."""
    if not item.rework:
        return []
    previous = run.item_dir(item.id) / PREVIOUS_RESULT
    copy = ("修正するコードのフォルダは最新のリビジョンのままです（前の修正をもう当てられなかったため）。前の結果を参考に、"
            "修正を作り直します。" if (run.item_dir(item.id) / REWORK_FRESH).is_file() else
            "修正するコードのフォルダには、前の修正が当ててあります。")
    lines = ["## 検証からの指摘（直すこと）", "",
             "独立した検証エージェントが、前の結果を確かめて、次の点を指摘しました。",
             *[f"- {r}" for r in item.rework], "",
             f"前の結果ファイルは `{previous}` です。{copy}"
             "指摘の点を確かめ、正しければ直し、指摘が当たらないと分かったときは、その根拠を confidence_reason に書きます。"
             "結果ファイルは、前の結果を元に、全体を書き直します。",
             "- group_excluded_cids には、今の作業の CID の中から書きます（前に外した CID は、もう別の作業です）。"]
    if run.config.options.annotation:
        lines.append("- アノテーションを入れるコードのフォルダは最新のリビジョンに戻してあります。注釈も入れ直します。")
    return lines + [""]


# ---- finish ----------------------------------------------------------------------------------


def _read_result(run: Run, item: ItemState) -> TriageResult:
    path = run.result_file(item.id)
    if not path.is_file():
        raise CtError("結果ファイルがありません", problems=[f"{path} を書いてください（書き方はスキル triage-report）"])
    try:
        result = TriageResult.model_validate(read_json(path))
    except (CtError, ValidationError) as exc:
        problems = ([f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
                    if isinstance(exc, ValidationError) else [str(exc)])
        raise CtError("結果ファイルに直すところがあります", problems=problems) from exc
    problems = []
    if result.item != item.id:
        problems.append(f"item は {item.id} にしてください（今は {result.item}）")
    if len(result.deviation.comment.strip()) > DEVIATION_COMMENT_MAX:
        problems.append(f"deviation.comment は {DEVIATION_COMMENT_MAX} 字以内にしてください（今は "
                        f"{len(result.deviation.comment.strip())} 字）。問題にならない理由と根拠の場所だけを書き、"
                        "調べた過程や但し書きは verdict.rationale に移します（スキル deviation-comment）")
    excluded = set(result.group_excluded_cids)
    if excluded and not item.is_group:
        problems.append("group_excluded_cids はグループのときだけ書きます")
    elif excluded - set(item.cids):
        problems.append(f"group_excluded_cids にグループ外の CID があります: {sorted(excluded - set(item.cids))}")
    elif excluded and not set(item.cids) - excluded:
        problems.append("グループのすべての CID を外すことはできません（1 つは残します）")
    if problems:
        raise CtError("結果ファイルに直すところがあります", problems=problems)
    return result


def _copy_changed(src: Path, files: list[str], dest: Path) -> None:
    for rel in files:
        if (src / rel).is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dest / rel)


def finish(run_dir: str, item_id: str) -> dict[str, Any]:
    run = Run(run_dir)
    item = run.state.item(item_id)
    if item.status != "in_progress":
        raise CtError(f"{item_id} は処理中ではありません（状態: {item.status}）")
    result = _read_result(run, item)
    config, vcs = run.config, run.vcs
    fix_dir = run.copy_dir("fix", item.slot)
    changed = vcs.changed_files(fix_dir)
    warnings = []
    if not changed and not result.fix.already_fixed_on_latest:
        raise CtError("修正するコードに変更がありません", problems=[
            f"{fix_dir} のファイルを修正してください。最新のリビジョンで既に直っている場合は "
            "fix.already_fixed_on_latest を true にしてください"])
    if changed and result.fix.already_fixed_on_latest:
        warnings.append("「最新のリビジョンで直っている」とありますが、修正するコードに変更があります（変更を差分にしました）")
    annotation_dir = run.copy_dir("annotation", item.slot)
    annotated = vcs.changed_files(annotation_dir) if config.options.annotation else []
    if config.options.annotation and not annotated:
        raise CtError("アノテーションが入っていません", problems=[f"{annotation_dir} に注釈を入れてください"])
    if run.state.analyzed_revision and vcs.changed_files(run.copy_dir("analyzed")):
        warnings.append("調べるコード（解析リビジョン）が変更されていました。ほかに処理中の作業が無いときに、次の作業の前に元に戻します")
    if item.repo_fingerprint and vcs.fingerprint() != item.repo_fingerprint:
        warnings.append("調べている間に、利用者のリポジトリのファイルが変わりました。"
                        "サブエージェントが誤って変更していないか確かめてください")

    # The split is recorded only after the fix is saved, in the same update that marks the item done,
    # so a finish that failed half way can simply be run again.
    excluded = set(result.group_excluded_cids)
    kept = [c for c in item.cids if c not in excluded]
    for cid in result.group_excluded_cids:
        target = run.item_dir(str(cid))
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run.item_dir(item_id) / f"issue-{cid}.json", target / f"issue-{cid}.json")

    saved: dict[str, Any] = {}
    run_id = run.state.run_id
    earlier = read_json(run.item_dir(item_id) / "saved.json", {})
    for kind in ("fix", "annotation"):  # a reworked item: its new fix takes the branch of the earlier one
        branch = (earlier.get(kind) or {}).get("branch")
        if branch:
            vcs.delete_branch(branch)
    single = f"cid-{kept[0]}" if not item.is_group else f"{run_id}-{item_id}"
    for kind, folder, files in (("fix", fix_dir, changed), ("annotation", annotation_dir, annotated)):
        if not files:
            continue
        label = "修正" if kind == "fix" else "逸脱の注釈"
        summary = result.fix.summary if kind == "fix" else result.deviation.classification
        message = f"Coverity CID {', '.join(map(str, kept))}: {label}\n\n{summary}\n"
        branch = None if config.options.per_run_branch else (
            config.vcs.branch_prefix + single + ("-annotation" if kind == "annotation" else ""))
        with file_lock(run.dir / "work" / "vcs", timeout=VCS_LOCK_SECONDS):
            fix = vcs.save(folder, run.state.latest_revision or "", message, branch,
                           f"refs/coverity-triage/{run_id}/{item_id}-{kind}", run.dir / "patches" / f"{item_id}-{kind}.patch")
        _copy_changed(folder, files, run.dir / "fixed" / item_id / kind)
        saved[kind] = fix.__dict__
    write_json(run.item_dir(item_id) / "saved.json", saved)

    split = []
    with run.update() as state:
        current = state.item(item_id)
        if excluded:
            current.cids = kept
            for cid in result.group_excluded_cids:
                for member in state.items:  # a merged group that held the CID: it is no longer there
                    if member.status == "merged" and member.merged_into == item_id and member.id != str(cid) \
                            and cid in member.cids:
                        member.cids = [c for c in member.cids if c != cid]
                        if not member.cids:
                            current.merged_from = [m for m in current.merged_from if m != member.id]
                same = next((i for i in state.items if i.id == str(cid)), None)
                if same is None:
                    state.items.append(ItemState(id=str(cid), cids=[cid], split_from=item_id))
                elif same.status == "merged":  # merged into this item earlier: worked on by itself again
                    same.status, same.merged_into, same.cids, same.split_from = "pending", None, [cid], item_id
                    same.review, same.rework, same.review_rounds = None, [], 0
                    current.merged_from = [m for m in current.merged_from if m != same.id]
                split.append(str(cid))
        current.warnings = warnings
        if current.rework:
            (run.item_dir(item_id) / PREVIOUS_RESULT).unlink(missing_ok=True)
            (run.item_dir(item_id) / REWORK_FRESH).unlink(missing_ok=True)
        current.rework = []
        current.review = None  # a new result is reviewed again
        finish_item(current, "done")
    if approvals.summary_made(run):
        # finished after ct.py summary (a resumed run): the recommendation goes in as summary would put it,
        # so the screen and preview agree on what will be applied
        with contextlib.suppress(CtError):  # a broken approvals.json is reported by preview
            approvals.ensure_defaults(run, {item_id: result})
    write_item_files(run, item_id)
    _log(run, "finish", item=item_id, recommendation=result.recommendation, confidence=result.confidence,
         files=changed, split=split, warnings=warnings)
    return {"ok": True, "item": item_id, "recommendation": result.recommendation, "confidence": result.confidence,
            "report": str(run.report_file(item_id)), "split_into": split, "warnings": warnings,
            "counts": run.state.counts(), "next": "ct.py next"}


def review_of(run: Run, item: ItemState) -> dict[str, Any]:
    """The independent review of an item (option agents.review): review.json, or why there is none."""
    if not run.state.review:
        return {}
    if item.review == "unverified":
        return {"outcome": "unverified", "reason": "検証エージェントで確かめられませんでした"}
    data = read_json(run.item_dir(item.id) / "review.json", {}) if item.review else {}
    if not data:
        return {}
    if item.review == "flag" and data.get("outcome") == "rework":
        # sent back as often as allowed, and still not right: the person decides
        data = {**data, "outcome": "flag", "reason": "差し戻して直したあとも、指摘が残っています"}
    return data


def _report(run: Run, item_id: str, result: TriageResult, saved: dict[str, Any], seconds: float | None,
            verify: dict[str, Any] | None = None) -> ItemReport:
    item = run.state.item(item_id)
    issues, events, description, notes = _item_view(run, item)
    review = review_of(run, item)
    return ItemReport(item_id=item_id, issues=issues, events=events, checker_description=description,
                      result=assess.effective(result, review, verify), saved=saved, verify=verify or {},
                      analyzed_revision=run.state.analyzed_revision, latest_revision=run.state.latest_revision,
                      seconds=seconds, notes=notes + item.warnings, run_dir=run.dir, review=review,
                      merged_from=item.merged_from)


def _write_item(run: Run, report: ItemReport, decision: dict[str, Any] | None, markdown: bool = True) -> None:
    write_text(run.report_file(report.item_id), render_item_html(report, decision))
    if markdown and run.config.options.markdown:
        write_text(run.report_file(report.item_id, "md"), render_item(report, decision))


def _approvals(run: Run) -> dict[str, dict[str, Any]]:
    """Approvals for the pages; a broken approvals.json must not stop the run (preview reports it)."""
    try:
        return approvals.load(run)
    except CtError:
        return {}


def write_item_files(run: Run, item_id: str, verify: dict[str, Any] | None = None) -> None:
    """Write the detailed report cid/<ID>.html (and cid/<ID>.md with the option markdown).

    ``verify`` is the item's build verification; by default the one kept in verify.json."""
    result = TriageResult.model_validate(read_json(run.result_file(item_id)))
    saved = read_json(run.item_dir(item_id) / "saved.json", {})
    if verify is None:
        verify = read_json(run.dir / "verify.json", {}).get(item_id)
    report = _report(run, item_id, result, saved, run.state.item(item_id).seconds, verify)
    _write_item(run, report, _approvals(run).get(item_id))


def rerender(run: Run, item_id: str, verify: dict[str, Any] | None = None) -> None:
    """Write the detailed report again (after the build verification changed the result)."""
    write_item_files(run, item_id, verify)


# ---- fail / resume / status / runs -----------------------------------------------------------


def fail(run_dir: str, item_id: str, reason: str) -> dict[str, Any]:
    run = Run(run_dir)
    with run.update() as state:
        item = state.item(item_id)
        if item.status != "in_progress":
            raise CtError(f"{item_id} は処理中ではありません（状態: {item.status}）")
        finish_item(item, "error", reason)
    _log(run, "fail", level="critical", item=item_id, reason=reason)
    return {"ok": True, "item": item_id, "counts": run.state.counts(), "next": "ct.py next"}


def resume(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    reset = {"in_progress": 0, "error": 0}
    with run.update() as state:
        for item in state.items:
            if item.status in reset:
                reset[item.status] += 1
                item.status, item.slot = "pending", None
        for lease in state.roles.values():  # the agents of the stopped session are gone
            lease["active"] = False
    from . import initial

    if run.state.initial and initial.status(run)["status"] == "failed":  # tried again from the start
        (initial.folder(run) / initial.STATUS_FILE).unlink(missing_ok=True)
    _log(run, "resume", reset=reset)
    return {"ok": True, "run_dir": str(run.dir), "repo_root": str(run.repo), "stream": run.state.stream,
            "filter_file": str(run.dir / "filter.json"), "issues_file": str(run.dir / "issues.json"),
            "work_dir": str(run.dir / "work"), "verify": run.state.verify_mode,
            "planned": run.state.planned, "back_to_pending": reset, "counts": run.state.counts(),
            "next": "ct.py tick"}


def status(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    state = run.state
    return {"ok": True, "run_dir": str(run.dir), "counts": state.counts(),
            "in_progress": [i.id for i in state.items if i.status == "in_progress"],
            "errors": [{"item": i.id, "reason": i.error} for i in state.items if i.status == "error"],
            "summary": next((str(p) for p in (run.summary_file(), run.summary_file("md")) if p.is_file()), None)}


def runs(repo_root: str, limit: int) -> dict[str, Any]:
    repo = _repo(repo_root)
    output = output_dir(repo, load_config(repo))
    found = []
    for folder in list_runs(output)[:limit]:
        try:
            state = Run(folder).state
        except (CtError, ValidationError):
            continue
        counts = state.counts()
        found.append({"run_dir": str(folder), "created_at": state.created_at, "filter": state.filter_name,
                      "planned": state.planned, "counts": counts, "unfinished": unfinished(state, folder),
                      "summary": any((folder / f"summary.{ext}").is_file() for ext in ("html", "md")),
                      "applied": sum(1 for i in state.items if i.applied)})
    return {"ok": True, "output_dir": str(output), "runs": found}


# ---- summary ---------------------------------------------------------------------------------


def _collect(run: Run) -> tuple[list[ItemReport], list[tuple[str, str]], list[str]]:
    """Reports of the finished items, the items that failed (with the reason) and the rest."""
    reports, errors, unprocessed = [], [], []
    verify = read_json(run.dir / "verify.json", {})
    for item in run.state.items:
        if item.status == "done":
            result = TriageResult.model_validate(read_json(run.result_file(item.id)))
            saved = read_json(run.item_dir(item.id) / "saved.json", {})
            reports.append(_report(run, item.id, result, saved, item.seconds, verify.get(item.id)))
        elif item.status == "error":
            errors.append((item.id, item.error))
        elif item.status != "merged":  # merged items are shown inside the item that took them
            unprocessed.append(item.id)
    return reports, errors, unprocessed


def _write_summary(run: Run, reports: list[ItemReport], errors: list[tuple[str, str]], unprocessed: list[str],
                   decided: dict[str, dict[str, Any]]) -> Path:
    title = run.state.created_at.replace("T", " ")[:16]
    path = run.summary_file()
    write_text(path, render_summary_html(title, run.state.filter_name, reports, errors, unprocessed, decided))
    # An existing summary.md (a run made before 0.4, or with the option once on) is kept current too:
    # an old table would show approvals that are no longer the ones applied.
    if run.config.options.markdown or run.summary_file("md").is_file():
        write_text(run.summary_file("md"), render_summary(title, run.state.filter_name, reports, errors,
                                                          unprocessed, decided))
    return path


def write_summary_files(run: Run) -> Path:
    """Write summary.html (and summary.md with the option markdown) from the current approvals.

    The detailed reports it links to are written when missing (a run made before 0.4 has only
    cid/<ID>.md, which is left as it is)."""
    reports, errors, unprocessed = _collect(run)
    decided = _approvals(run)
    for report in reports:
        if not run.report_file(report.item_id).is_file():
            _write_item(run, report, decided.get(report.item_id), markdown=False)
    return _write_summary(run, reports, errors, unprocessed, decided)


def summary(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    # A run whose summary was made before 0.4: its approvals are taken over from summary.md below (or were,
    # by finish or the screen), and its cid/<ID>.md, which the person may have edited, are kept.
    legacy = not run.summary_file().is_file() and run.summary_file("md").is_file()
    reports, errors, unprocessed = _collect(run)
    decided = approvals.ensure_defaults(run, {r.item_id: r.result for r in reports})
    path = _write_summary(run, reports, errors, unprocessed, decided)
    for report in reports:  # the detailed reports show the approval too
        _write_item(run, report, decided.get(report.item_id), markdown=not legacy)
    removed = []
    if not any(i.status in ("pending", "in_progress") for i in run.state.items):
        removed = cleanup(run)
    counts: dict[str, int] = {"fix": 0, "deviation": 0}
    for r in reports:
        counts[r.result.recommendation] += 1
    _log(run, "summary", items=len(reports), errors=len(errors), unprocessed=unprocessed)
    return {"ok": True, "summary": str(path), "items": len(reports), "recommended": counts,
            "errors": len(errors), "unprocessed": unprocessed, "removed_work_copies": removed,
            "next": "summary の場所と件数を人に伝え、画面の「結果」で承認欄を確かめてから /coverity-apply を使うよう"
                    f"案内する（画面: ct.py gui --repo {run.repo} --run {run.dir} --view summary）"}


def cleanup(run: Run) -> list[str]:
    """Remove the work copies once every item is finished; branches and patches stay."""
    work = run.dir / "work"
    removed = []
    vcs = run.vcs
    with file_lock(work / "vcs", timeout=VCS_LOCK_SECONDS):
        for folder in sorted(p for p in work.iterdir() if p.is_dir()):
            vcs.remove_copy(folder)
            removed.append(folder.name)
    return removed


def filter_from(run: Run) -> FilterSpec:
    return FilterSpec.model_validate(read_json(run.dir / "filter.json"))


# ---- verify (option) -------------------------------------------------------------------------


def _mark_verified(run: Run) -> None:
    with run.update() as state:
        state.verified = True
        state.roles.pop("verify", None)


def verify(run_dir: str, mode: str | None) -> dict[str, Any]:
    """Build (and analyze) the latest code with every fix of the run applied, once."""
    from .verify import BatchItem, Verifier
    from .vcs import GitVcs

    run = Run(run_dir)
    mode = mode or run.state.verify_mode
    if mode == "none":
        _mark_verified(run)
        return {"ok": True, "mode": "none", "message": "ビルドでの検証は使わない設定です"}
    issues, _ = _mapped(run)
    strip = 1 if isinstance(run.vcs, GitVcs) else 0
    batch = []
    for item in run.state.items:
        saved = read_json(run.item_dir(item.id) / "saved.json", {}) if item.status == "done" else {}
        if "fix" in saved:
            batch.append(BatchItem(item.id, saved["fix"]["patch"], saved["fix"]["files"],
                                   [issues[c] for c in item.cids], strip))
    if not batch:
        _mark_verified(run)
        return {"ok": True, "mode": mode, "message": "確かめる修正案がありません"}
    latest = run.copy_dir("verify-latest")
    if latest.exists():
        shutil.rmtree(latest)
    run.vcs.export(run.state.latest_revision or "", latest)
    _log(run, "verify-start", mode=mode, items=[b.item_id for b in batch])
    outcome = Verifier(run.config.verify, run.dir, (run.config.coverity.key_env,)).run_batch(mode, latest, batch)
    shutil.rmtree(latest, ignore_errors=True)

    # result.json keeps what the worker wrote; the reports show the lower confidence (assess.effective)
    downgraded = []
    results = read_json(run.dir / "verify.json", {})
    for item_id, checked in outcome.items.items():
        result = TriageResult.model_validate(read_json(run.result_file(item_id)))
        shown = assess.effective(result, review_of(run, run.state.item(item_id)), checked)
        if checked.get("problems") and result.recommendation == "fix" and shown.confidence == "low" \
                and result.confidence != "low":
            downgraded.append(item_id)
        results[item_id] = checked
        rerender(run, item_id, checked)
    write_json(run.dir / "verify.json", results)
    _mark_verified(run)
    _log(run, "verify", mode=mode, build_ok=outcome.build_ok, downgraded=downgraded, error=outcome.error)
    return {"ok": True, "mode": mode, "build_ok": outcome.build_ok, "seconds": round(outcome.seconds),
            "items": len(outcome.items), "downgraded_to_low": sorted(downgraded),
            "unassigned_new_issues": len(outcome.unassigned_new_issues), "error": outcome.error,
            "log": outcome.log, "next": "ct.py summary"}
