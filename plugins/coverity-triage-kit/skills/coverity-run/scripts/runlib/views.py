"""The commands other than tick (design 4.12): settings, runs, new-run, resume, status, summary, stop,
note-issue, initial-status."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import approvals, briefs, checks, initial, issues, jobs, metrics, oplog, report, schema
from .common import (RunError, append_line, file_lock, inside, mtime, now_iso, read_json, resolve_path, try_json,
                     write_json)
from .config import (DEFAULT_FILTER, PREPARE_HINT, config_path, filter_names, load_config, load_filter, output_dir,
                     parse_filter, read_yaml)
from .flow import STAGE_JA, STALE_SECONDS
from .state import FORMAT, Run, counts, create_run, list_runs, tool_version, unfinished
from .vcs import make_vcs
from .work import VCS_LOCK_STALE, VCS_LOCK_WAIT

STALL_SECONDS = 600
INITIAL_STALL_SECONDS = 1800
LONG_PATH = 240
AGENT_ROLES = ("worker", "reviewer", "integrator")


def _repo(path: str) -> Path:
    repo = resolve_path(path)
    if not repo.is_dir():
        raise RunError(f"フォルダがありません: {repo}")
    return repo


def permission_steps(output: Path) -> list[str]:
    return [f"チャットで /add-dir \"{output}\" と入力し、最初の確認で「このセッションでは許可」を選びます"
            "（uv のコマンド、ファイルの書き込み、Coverity との通信のツール）。",
            f"または、Copilot CLI を copilot --add-dir \"{output}\" --allow-tool 'shell(uv:*)' --allow-tool write "
            "--allow-tool '<Coverity との通信の MCP サーバーの登録名>' で起動し直します。"]


# ---- settings ---------------------------------------------------------------------------------


def settings(repo_root: str) -> dict[str, Any]:
    repo = _repo(repo_root)
    if not config_path(repo).is_file():
        raise RunError(f"設定ファイルがありません: {config_path(repo)}", how_to_fix=PREPARE_HINT)
    values, problems = load_config(repo)
    names = filter_names(repo)
    for name in names:
        path = config_path(repo).parent / "filters" / name
        try:
            spec, found = parse_filter(read_yaml(path), name)
        except RunError as exc:
            problems.append(str(exc))
            continue
        problems += found
        if not found and len(spec["streams"]) != 1:
            problems.append(f"{name} の streams: ストリームを 1 つだけ書いてください")
    if not names:
        problems.append("条件ファイル（.coverity-triage/filters/*.yaml）がありません")
    out_dir = output_dir(repo, values)
    if inside(out_dir, repo):
        problems.append(f"output_dir: 実行フォルダの置き場所がリポジトリの中です（{out_dir}）。リポジトリの外にしてください")
    verify = values["verify"]
    if (verify["default"] != "none" or verify["initial"]) and not verify["build_command"]:
        problems.append("verify.build_command: ビルドでの検証か初回解析を使うときは、ビルドのコマンドを書いてください")
    return {"ok": True, "repo_root": str(repo), "config_file": str(config_path(repo)),
            "max_rework": values["agents"]["max_rework"], "review": values["agents"]["review"],
            "integration": values["agents"]["integration"], "initial": verify["initial"], "verify": verify["default"],
            "max_items": values["max_items"], "parallel": values["parallel"], "output_dir": str(out_dir),
            "filters": names, "default_filter": DEFAULT_FILTER if DEFAULT_FILTER in names else (names[0] if names else None),
            "vcs": values["vcs"]["type"], "problems": problems, "ready": not problems}


# ---- runs -------------------------------------------------------------------------------------


def runs(repo_root: str, limit: int = 20) -> dict[str, Any]:
    repo = _repo(repo_root)
    values, _ = load_config(repo, need_vcs=False)
    output = output_dir(repo, values)
    found = []
    for folder in list_runs(output)[:max(1, limit)]:
        data = try_json(folder / "run.json")
        has_summary = (folder / "summary.html").is_file() or (folder / "summary.md").is_file()
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            found.append({"run_id": folder.name, "run_dir": str(folder),
                          "created_at": (data or {}).get("created_at") if isinstance(data, dict) else None,
                          "filter_name": (data or {}).get("filter_name") if isinstance(data, dict) else None,
                          "counts": {}, "unfinished": False, "has_summary": has_summary, "foreign": True})
            continue
        found.append({"run_id": data.get("run_id", folder.name), "run_dir": str(folder), "created_at": data.get("created_at"),
                      "filter_name": data.get("filter_name"), "counts": counts(data),
                      "unfinished": unfinished(data, folder), "has_summary": has_summary, "foreign": False,
                      "same_tool": data.get("tool_version") == tool_version(), "initial": bool(data.get("initial")),
                      "stopped": bool(data.get("stopped"))})
    return {"ok": True, "output_dir": str(output), "runs": found}


# ---- new-run ----------------------------------------------------------------------------------


def _longest_path(repo: Path) -> int:
    longest = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".svn")]
        for name in filenames:
            longest = max(longest, len(os.path.relpath(os.path.join(dirpath, name), repo)))
    return longest


def long_path_problem(work_dir: Path, repo: Path, limit: int = LONG_PATH) -> str | None:
    """A note when a work copy path would pass ``limit`` characters (Windows tools fail on long paths)."""
    worst = len(str(work_dir / "w8")) + 1 + _longest_path(repo)
    if worst > limit:
        return (f"作業用のコピーのパスが {worst} 文字ほどになり、{limit} 文字を超えます。"
                "設定 output_dir を短いパス（例 C:\\cov-out）にしてください")
    return None


def new_run(repo_root: str, filter_name: str | None, overrides: dict[str, Any], limit: int | None, verify: str | None,
            first: bool | None, review: bool | None, integration: bool | None, max_rework: int | None,
            issues_file: str | None) -> dict[str, Any]:
    repo = _repo(repo_root)
    values, problems = load_config(repo)
    if problems:
        raise RunError("設定に直すところがあります", problems=problems)
    if values["vcs"]["type"] not in ("git", "svn"):
        raise RunError("git / svn のリポジトリの一番上のフォルダを --repo に渡してください")
    max_items = values["max_items"]
    if limit is not None and not 1 <= limit <= max_items:
        raise RunError(f"件数の上限（--limit）は 1 から {max_items}（設定 max_items）までにしてください")
    if filter_name is None:
        names = filter_names(repo)
        if not names:
            raise RunError("条件ファイル（.coverity-triage/filters/*.yaml）がありません", how_to_fix=PREPARE_HINT)
        filter_name = DEFAULT_FILTER if DEFAULT_FILTER in names else names[0]
    spec, filter_file = load_filter(repo, filter_name, overrides)
    if len(spec["streams"]) != 1:
        raise RunError("条件ファイルには、ストリームを 1 つだけ書いてください", streams=spec["streams"], filter_file=str(filter_file))
    requested = limit or spec["limit"] or max_items
    run_limit = min(requested, max_items)
    mode = verify or values["verify"]["default"]
    if mode not in ("none", "build", "build+analyze"):
        raise RunError("ビルドでの検証は none / build / build+analyze のどれかです")
    if mode != "none" and not values["verify"]["build_command"]:
        raise RunError("ビルドでの検証の設定（verify.build_command）がありません")
    if issues_file and first:
        raise RunError("取ってある警告で始めるとき（--issues-file）は、初回解析はできません")
    first = (values["verify"]["initial"] and not issues_file) if first is None else first
    if first and not values["verify"]["build_command"]:
        raise RunError("初回解析の設定（verify.build_command）がありません")
    review = values["agents"]["review"] if review is None else review
    integration = values["agents"]["integration"] if integration is None else integration
    max_rework = values["agents"]["max_rework"] if max_rework is None else max_rework
    if not 0 <= max_rework <= 2:
        raise RunError("作り直しの上限（--max-rework）は 0 から 2 までにしてください")
    output = output_dir(repo, values)
    if inside(output, repo):
        raise RunError(f"実行フォルダの置き場所がリポジトリの中です: {output}（設定 output_dir をリポジトリの外にしてください）")
    given = None
    if issues_file:
        source = resolve_path(issues_file)
        given = read_json(source)
        found = issues.check_issues_file(given)
        if found:
            raise RunError("警告の一覧のファイルに直すところがあります", problems=found, issues_file=str(source))
    if sys.platform == "win32":
        problem = long_path_problem(output / "00000000-000000-00" / "work", repo)
        if problem:
            raise RunError(problem)
    vcs = make_vcs(repo, values)
    fingerprint = vcs.fingerprint()
    run = create_run(output, {
        "repo_root": str(repo), "filter_name": spec["name"], "filter_file": str(filter_file), "stream": spec["streams"][0],
        "limit": run_limit, "verify_mode": mode, "initial": bool(first), "review": bool(review),
        "integration": bool(integration), "max_rework": int(max_rework), "parallel": values["parallel"],
        "offline": bool(issues_file), "settings": values, "planned": False, "items": [], "tasks": {}, "fetch": {},
        "alerts": [], "notes": [], "fingerprint_start": fingerprint, "stage": "initial" if first else "fetch"})
    write_json(run.dir / "filter.json", {**spec, "limit": run_limit})
    shutil.copyfile(config_path(repo), run.dir / "config.snapshot.yaml")
    briefs.copy_schemas(run.dir)
    if given is not None:
        write_json(run.dir / "issues.json", given)
        wanted = {i["cid"] for i in given["issues"]}
        for path in Path(issues_file).resolve().parent.glob("issue-*.json"):
            data = try_json(path)
            cid = (data or {}).get("cid") or ((data or {}).get("issue") or {}).get("cid")
            if cid in wanted:
                shutil.copyfile(path, run.dir / "details" / f"issue-{cid}.json")
        with run.update() as st:
            st.setdefault("fetch", {})["issues_done"] = True
    run.log("new-run", f"実行を始めた（条件 {spec['name']}、上限 {run_limit} 件）", filter=spec, limit=run_limit,
            verify=mode, initial=bool(first), review=bool(review), integration=bool(integration))
    out = {"ok": True, "run_dir": str(run.dir), "run_id": run.state["run_id"], "repo_root": str(repo),
           "work_dir": str(run.dir / "work"), "output_dir": str(output), "filter_name": spec["name"],
           "stream": spec["streams"][0], "limit": run_limit, "verify": mode, "initial": bool(first), "review": bool(review),
           "integration": bool(integration), "max_rework": int(max_rework), "parallel": values["parallel"],
           "permission_steps": permission_steps(output), "next": "tick で次にすることを聞く"}
    if requested > run_limit:
        out["note"] = f"1 回の上限（設定 max_items）が {max_items} 件のため、{run_limit} 件にしました"
    return out


# ---- resume / stop / note-issue -----------------------------------------------------------------


def resume(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir, write=True)
    try:
        values, problems = load_config(run.repo)
    except RunError as exc:
        raise RunError(str(exc), how_to_fix=PREPARE_HINT) from None
    if problems:
        raise RunError("設定に直すところがあります", problems=problems)
    running = {j["name"] for j in jobs.running(run.dir)}
    back = {"in_progress": 0, "error": 0}
    with run.update() as st:
        st["settings"] = values
        for item in st["items"]:
            job = {"copy": f"copy-w{item.get('slot')}", "save": f"save-{item['id']}", "merge": "merge"}.get(item.get("stage") or "")
            if item["status"] == "in_progress" and not (job and job in running):
                back["in_progress"] += 1
                item.update(status="pending", stage=None, slot=None)
            elif item["status"] == "error":
                back["error"] += 1
                item.update(status="pending", stage=None, slot=None, error="", stale_retries=0)
        st["tasks"] = {}
        st["stopped"] = False
        st["stopped_at"] = None
        st["finished"] = False
        st["finished_at"] = None
        st.pop("copy_failed", None)
        if not st.get("planned"):
            st.setdefault("fetch", {})["issues_failures"] = 0
        if st.get("verify_started") and not st.get("verified") and "verify" not in running:
            st["verify_started"] = False
        if st.get("initial") and initial.status(run).get("status") == "failed":
            (initial.folder(run.dir) / initial.STATUS_FILE).unlink(missing_ok=True)
            st["initial_failed_logged"] = False
    run.log("resume", f"実行を再開した（戻した作業 {back['in_progress'] + back['error']} 件）", reset=back)
    output = run.dir.parent
    return {"ok": True, "run_dir": str(run.dir), "run_id": run.state["run_id"], "repo_root": str(run.repo),
            "work_dir": str(run.dir / "work"), "counts": counts(run.state), "back_to_pending": back,
            "initial": bool(run.state.get("initial")), "review": bool(run.state.get("review")),
            "integration": bool(run.state.get("integration")), "max_rework": run.state.get("max_rework"),
            "permission_steps": permission_steps(output), "next": "tick で次にすることを聞く"}


def stop(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir, write=True)
    with run.update() as st:
        st["stopped"] = True
        st["stopped_at"] = now_iso()
        st["tasks"] = {}
    stopped, unstopped = jobs.stop_all(run.dir)
    run.log("stop", "実行を止めた" + (f"（止めきれなかった処理 {len(unstopped)} 件）" if unstopped else ""),
            level="warning", stopped_jobs=stopped, unstopped=unstopped)
    return {"ok": True, "run_dir": str(run.dir), "stopped": True, "stopped_jobs": stopped, "unstopped": unstopped}


def note_issue(run_dir: str, title: str, body: str) -> dict[str, Any]:
    run = Run(run_dir, write=True)
    title, body = (title or "").strip(), (body or "").strip()
    if not title or not body:
        raise RunError("課題の題（title）と本文（body）の両方を渡してください")
    import json

    path = run.dir / "issues-found.jsonl"
    record = oplog.mask({"title": title[:200], "body": body[:8000], "at": now_iso()}, run.secrets())
    with file_lock(path):
        append_line(path, json.dumps(record, ensure_ascii=False))
    run.log("note-issue", f"課題を書き残した: {title[:60]}")
    return {"ok": True, "issues_found": len(issues_found(run))}


def issues_found(run: Run) -> list[dict[str, Any]]:
    path = run.dir / "issues-found.jsonl"
    out = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    import json

    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("title"):
            out.append({"title": record.get("title"), "body": record.get("body", ""), "at": record.get("at")})
    return out


# ---- status ------------------------------------------------------------------------------------


def criteria_list() -> list[dict[str, Any]]:
    out = []
    for c in schema.load("criteria.json")["criteria"]:
        entry = {"id": c["id"], "scope": c["scope"], "text": c["text"]}
        if c.get("applies_when"):
            entry["applies_when"] = c["applies_when"]
        out.append(entry)
    return out


def criteria_results(run: Run, run_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviews = []
    for item in run.state["items"]:
        if item["status"] == "done" and item.get("review") in ("pass", "flag", "rework"):
            data = try_json(run.review_file(item["id"]))
            if isinstance(data, dict):
                reviews.append((item["id"], {f.get("check"): f for f in data.get("findings") or [] if isinstance(f, dict)}))
    checked = {c["id"]: c for c in run_checks}
    out = []
    for c in criteria_list():
        entry = {"id": c["id"], "scope": c["scope"], "passed": 0, "failed": 0, "not_applicable": 0, "items_failed": []}
        if c["scope"] == "item":
            for item_id, findings in reviews:
                f = findings.get(c["id"])
                if f is None or (f.get("ok") and "対象外" in str(f.get("detail", ""))):
                    entry["not_applicable"] += 1
                elif f.get("ok"):
                    entry["passed"] += 1
                else:
                    entry["failed"] += 1
                    entry["items_failed"].append(item_id)
        elif c["id"] in checked:
            ok = checked[c["id"]].get("ok")
            entry["passed" if ok else ("not_applicable" if ok is None else "failed")] += 1
        out.append(entry)
    return out


def _progress_of(path: Path, since: float) -> dict[str, Any]:
    t = mtime(path)
    if t is None or t < since - 2:
        return {}
    data = try_json(path)
    return data if isinstance(data, dict) else {}


def _item_status(run: Run, item: dict[str, Any], tasks: dict[str, Any], mapped: dict[int, Any], verify: dict[str, Any],
                 now: float) -> dict[str, Any]:
    item_id = item["id"]
    stage = item.get("stage")
    phase, note, idle = None, "", None
    task = tasks.get(f"worker:{item_id}") or tasks.get(f"reviewer:{item_id}")
    if item["status"] == "in_progress":
        if stage == "copy":
            phase, note = "copy", "作業用のコピーを用意している（初めての枠は時間がかかります）"
        elif stage in ("save", "merge"):
            phase, note = stage, "修正を保存している" if stage == "save" else "まとめた修正を作っている"
        elif task:
            progress = _progress_of(run.item_dir(item_id) / "progress.json", float(task.get("launched_at") or 0))
            phase, note = progress.get("phase") or "read", str(progress.get("note") or "")[:200]
    elif task and task.get("role") == "reviewer":
        progress = _progress_of(run.dir / "agents" / "reviewer" / "progress.json", float(task.get("launched_at") or 0))
        phase, note = progress.get("phase") or "read", str(progress.get("note") or "確かめている")[:200]
    if task:
        folder = run.item_dir(item_id)
        seen = [float(task.get("launched_at") or 0), *(mtime(p) or 0 for p in folder.glob("*"))]
        if task.get("role") == "reviewer":
            seen.append(mtime(run.dir / "agents" / "reviewer" / "progress.json") or 0)
        idle = now - max(seen)
    review = None
    if item.get("review") == "unverified":
        review = {"outcome": "unverified", "findings": []}
    elif item.get("review"):
        data = try_json(run.review_file(item_id)) or {}
        outcome = "flag" if item["review"] == "flag" else item["review"]
        review = {"outcome": outcome, "findings": [{"check": f.get("check"), "ok": f.get("ok"), "detail": f.get("detail")}
                                                   for f in data.get("findings") or [] if isinstance(f, dict)]}
    recommendation = confidence = None
    found = mapped.get(item["cids"][0]) if item["cids"] else None
    checker = (found or {}).get("checker")
    if item["status"] == "done":
        shown = report.shown_result(run, item, verify)
        if shown:
            recommendation, confidence = shown.get("recommendation"), shown.get("confidence")
    started = item.get("started")
    elapsed = (int(now - started) if item["status"] == "in_progress" and started else
               (int(item["seconds"]) if item.get("seconds") is not None else None))
    return {"id": item_id, "cids": item["cids"], "checker": checker, "status": item["status"], "phase": phase,
            "note": note, "elapsed_seconds": elapsed,
            "stalled": bool(idle is not None and idle >= STALL_SECONDS and stage != "copy"),
            "preparing_copy": stage == "copy", "attempts": item.get("attempts", 0),
            "rework_rounds": item.get("review_rounds", 0), "review": review, "recommendation": recommendation,
            "confidence": confidence, "error": item.get("error") or None}


def status(run_dir: str) -> dict[str, Any]:
    """The state for a screen. Reads only; takes no lock."""
    run = Run(run_dir)
    st = run.state
    now = time.time()
    tasks = st.get("tasks") or {}
    mapped = issues.mapped(run)
    verify = try_json(run.dir / "verify.json") or {}
    c = counts(st)
    stage = st.get("stage") or "fetch"
    if st.get("stopped"):
        stage = "stopped"
    initial_view = initial.progress_view(run)
    if initial_view["used"] and initial_view["step"] and initial.status(run).get("status") == "running":
        log = initial.folder(run.dir) / initial.LOG_FILE
        initial_view["stalled"] = now - (mtime(log) or now) >= INITIAL_STALL_SECONDS
    run_checks = st.get("run_checks") or []
    processed = c["done"] + c["error"]
    return {"ok": True, "run_id": st["run_id"], "run_dir": str(run.dir), "repo_root": st.get("repo_root"),
            "stage": stage, "stage_label": STAGE_JA.get(stage, stage), "finished": bool(st.get("finished")),
            "stopped": bool(st.get("stopped")), "has_summary": run.summary_file().is_file(),
            "review": bool(st.get("review")), "integration": bool(st.get("integration")),
            "max_rework": st.get("max_rework"), "parallel": st.get("parallel"),
            "counts": {k: c[k] for k in ("pending", "in_progress", "done", "error", "merged", "total")},
            "items": [_item_status(run, i, tasks, mapped, verify if isinstance(verify, dict) else {}, now)
                      for i in st["items"] if i["status"] != "merged"],
            "running_tasks": [{"task": t["task"], "role": t["role"], "item": t.get("item"), "description": t.get("description")}
                              for t in tasks.values() if t.get("role") in AGENT_ROLES],
            "errors": [{"item": a.get("item"), "kind": a.get("kind"), "detail": a.get("detail"), "level": a.get("level")}
                       for a in (st.get("alerts") or [])[-50:]],
            "recent": oplog.recent(run.dir, 10), "initial": initial_view,
            "progress": {"value": processed, "max": c["total"], "label": f"{processed} / {c['total']}"},
            "run_checks": run_checks, "criteria": criteria_list(), "criteria_results": criteria_results(run, run_checks),
            "issues_found": issues_found(run), "stale_seconds": STALE_SECONDS}


def initial_status(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    data = initial.status(run)
    view = initial.progress_view(run)
    return {"ok": True, **view, "status": data.get("status"), "step_label": data.get("step_label"),
            "elapsed_seconds": data.get("elapsed_seconds"), "error": data.get("error"),
            "log_tail": data.get("log_tail"), "dirty": data.get("dirty")}


# ---- summary -----------------------------------------------------------------------------------


def _cleanup(run: Run) -> list[str]:
    work = run.dir / "work"
    removed = []
    if not work.is_dir():
        return removed
    vcs = make_vcs(run.repo, run.settings)
    with file_lock(work / "vcs", timeout=VCS_LOCK_WAIT, stale=VCS_LOCK_STALE):
        for folder in sorted(work.iterdir()):
            if folder.is_dir():
                try:
                    vcs.remove_copy(folder)
                except RunError:
                    shutil.rmtree(folder, ignore_errors=True)
                removed.append(folder.name)
            elif folder.suffix in (".rev", ".json"):
                folder.unlink(missing_ok=True)
    return removed


def summary(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir, write=True)
    results = {}
    for item in run.state["items"]:
        if item["status"] == "done" and run.result_file(item["id"]).is_file():
            results[item["id"]] = read_json(run.result_file(item["id"]))
    approvals.ensure_defaults(run, results)
    made = report.write_summary(run)
    rows = made["rows"]
    run_checks = checks.run_checks(run, rows)
    with run.update() as st:
        st["run_checks"] = run_checks
        st["summary_at"] = now_iso()
    removed = []
    busy = [j for j in jobs.running(run.dir)]
    c = counts(run.state)
    if not c["pending"] and not c["in_progress"] and not busy:
        removed = _cleanup(run)
    if run.settings["options"]["metrics"]:
        metrics.write(run, rows)
    numbers = made["counts"]
    failed_checks = [x["id"] for x in run_checks if x["ok"] is False]
    say = [f"一覧を作りました: {run.summary_file()}",
           f"作業 {numbers['items']} 件（修正の推奨 {numbers['fix']}、逸脱の推奨 {numbers['deviation']}、確信度 低 {numbers['low']}）、"
           f"処理できなかった作業 {numbers['errors']} 件、未処理 {numbers['unprocessed']} 件です。"]
    if not run.state["items"]:
        say.append("条件に合う警告はありませんでした。")
    if failed_checks:
        say.append("実行ごとの評価基準で合わなかったもの: " + "、".join(failed_checks) + "（run_checks の detail を見てください）")
    say.append("承認（修正・逸脱・却下・反映しない）は、結果を見る画面で確かめて選びます。終わったら、反映の手順で反映します。")
    run.log("summary", f"一覧を作った（{numbers['items']} 件、処理できなかった作業 {numbers['errors']} 件）",
            items=numbers["items"], errors=numbers["errors"])
    return {"ok": True, "summary_html": str(run.summary_file()), "run_dir": str(run.dir), "counts": numbers,
            "approvals_counts": made["approvals_counts"],
            "results": [{**r, "report": str(run.dir / r["report"])} for r in rows], "unprocessed": made["unprocessed"],
            "run_checks": run_checks, "criteria": criteria_list(), "criteria_results": criteria_results(run, run_checks),
            "removed_work_copies": removed, "say": say}
