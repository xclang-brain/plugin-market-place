"""``run.py tick``: what to do next (design 4.12). The agent that runs the batch never decides the order.

Stages, each skipped when its option is off:
initial analysis (job) → fetch the warnings (action) → plan the items → fetch the warning paths
(actions) → work on the items (copy job → worker → checks → save job), up to ``parallel`` at a time
→ integration (integrator → merge job) → review (one reviewer at a time) → build verification (job)
→ done. With the integration off, reviews start as soon as items are done.

A task ends when the agent reports it (``--finished`` / ``--failed``) or when its output file appears
and passes the checks. A task with no output and no progress for 30 minutes fails and is tried again.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import approvals, briefs, initial, integrate, issues, jobs, report, rules
from .common import (RunError, _replace, file_lock, file_sha, mtime, now_iso, now_ts, read_json, remove_file, try_json,
                     write_json)
from .config import load_no_grouping
from .grouping import build_work_items
from .pathmap import PathMapper
from .state import Run, counts, finish_item, new_item
from .vcs import make_vcs
from .work import PREVIOUS_RESULT, REWORK_FRESH

WAIT_DEFAULT = 20
WAIT_MAX = 25
FIX_ROUNDS = 2  # an agent is asked this many times to fix its output file
FETCH_TRIES = 2  # the warnings search may fail this many times before the run stops
DETAILS_ROUNDS = 2  # the warning paths are fetched at most twice (the second time, only the missing ones)
DETAILS_BATCH = 50  # CIDs per fetch of warning paths
STALE_SECONDS = 1800  # a task with no output and no progress for this long failed
SETTLE_SECONDS = 2  # an output file found without a report counts once it has not changed for this long
POLL_SECONDS = 0.5  # the longest pause between two looks at the jobs
REAP_TRIES = 3  # taking in how a job ended may fail this many times before its item fails
SCRIPT_JOBS = {"copy": "copy-w{slot}", "save": "save-{id}", "merge": "merge"}  # an item's stage → its job
STAGE_JA = {"initial": "初回解析", "fetch": "警告の取得", "details": "警告経路の取得", "items": "警告ごとの作業",
            "integrate": "同じ原因の統合", "review": "結果の確かめ", "verify": "ビルドでの検証", "done": "終わった",
            "stopped": "止めた"}
ROLE_JA = {"worker": "作業", "reviewer": "確かめ", "integrator": "統合"}
FATAL = re.compile(r"(?<!\d)(401|403|5\d\d)(?!\d)|認証|unauthori[sz]ed|forbidden|certificate|証明書", re.IGNORECASE)
TASK = re.compile(r"^(?P<base>[^@#]+?)(?:@(?P<gen>\d+))?(?:#(?P<round>\d+))?$")


@dataclass
class Step:
    stage: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    stop: bool = False
    done: bool = False
    error: str = ""
    waitable: bool = False


def task_name(base: str, gen: int | None = None, round_: int = 1) -> str:
    return base + (f"@{gen}" if gen else "") + (f"#{round_}" if round_ > 1 else "")


# ==== entry ======================================================================================


def tick(run_dir: str, finished: str | None = None, failed: str | None = None, reason: str = "",
         wait: int = WAIT_DEFAULT) -> dict[str, Any]:
    wait = max(0, min(int(wait), WAIT_MAX))
    run = Run(run_dir, write=True)
    with file_lock(run.dir / "tick", timeout=WAIT_MAX + 40, stale=WAIT_MAX + 120):
        run.reload()
        say: list[str] = []
        if run.state.get("stopped"):
            return _answer(run, Step("stopped", stop=True, error="この実行は止められています"),
                           ["この実行は止められています。続けるときは、再開してから tick を呼びます。"])
        carried: list[dict[str, Any]] = []
        reported = bool(finished or failed)
        if reported:
            _report(run, (finished or failed or "").strip(), failed is not None, reason or "", say, carried)
        deadline = time.monotonic() + wait
        poll = 0.03
        while True:
            run.reload()
            if run.state.get("stopped"):
                result = Step("stopped", stop=True, error="この実行は止められました")
                break
            result = _step(run, say, carried)
            carried = []
            if result.actions or result.stop or result.done or time.monotonic() >= deadline:
                break
            # Wait for the jobs; for the agents only when nothing was reported (their output files may appear).
            # After a report, the agent has other reports to give: answer at once. A job that ended while the
            # step ran is not taken in yet: one more step takes it in (a worker to launch may follow).
            unsettled = [j for j in jobs.all_jobs(run.dir) if j.get("status") == "running" or not j.get("reaped")]
            if not unsettled and (reported or not run.state.get("tasks")):
                break
            time.sleep(poll)
            poll = min(POLL_SECONDS, poll * 1.5)
        return _answer(run, result, say)


def _answer(run: Run, result: Step, say: list[str]) -> dict[str, Any]:
    agents = [t for t in (run.state.get("tasks") or {}).values() if t.get("role") in ROLE_JA]
    if result.done:
        nxt = "summary で一覧を作る"
    elif result.stop:
        nxt = "say と error を利用者に伝えて止める（ほかには何もしない）"
    elif result.actions:
        nxt = ("actions を順に行う。launch は裏で動かし、終わったら tick --finished <task>（失敗したら --failed <task> "
               "--reason \"<理由>\"）。fetch は呼び終わったら tick --finished <task>")
    elif agents:
        nxt = "裏で動いているエージェントが終わったら tick --finished <task>。それまでは tick をそのまま呼び直してよい"
    else:
        nxt = "時間のかかる処理が動いています。tick をそのまま呼び直す"
    shown = say[:2] if len(say) <= 2 else [say[0], f"ほかに {len(say) - 1} 件の知らせがあります（status の recent で見られます）"]
    out: dict[str, Any] = {"ok": True, "stage": result.stage, "stage_label": STAGE_JA[result.stage],
                           "actions": result.actions, "wait": bool(not result.actions and not result.done and not result.stop),
                           "done": result.done, "stop": result.stop, "say": shown, "next": nxt}
    if result.error:
        out["error"] = result.error
    current = counts(run.state)
    with run.update() as st:
        st["stage"] = result.stage
        if result.stop and result.stage != "stopped":  # the run cannot go on until it is fixed and resumed
            st["halted"] = True
            st["halt_reason"] = result.error or (say[0] if say else "続けられないため止めました")
        else:
            st["halted"] = False
            st["halt_reason"] = None
        st["last_tick"] = now_iso()
        if st.get("last_counts") != current:
            st["last_counts"] = current
            out["counts"] = current
    return out


# ==== reports from the agent ===================================================================


def _report(run: Run, task: str, failed: bool, reason: str, say: list[str], actions: list[dict[str, Any]]) -> None:
    match = TASK.match(task)
    if not match:
        raise RunError(f"task が分かりません: {task}（actions の task をそのまま渡します）")
    base = match.group("base")
    marked = bool(match.group("gen") or match.group("round"))
    if not re.match(r"^(worker:[^:]+|reviewer:[^:]+|integrator|fetch:issues|fetch:details:\d+)$", base):
        raise RunError(f"task が分かりません: {task}（actions の task をそのまま渡します）")
    current = (run.state.get("tasks") or {}).get(base)
    if current is None or (marked and current.get("task") != task):
        run.log("report-ignored", f"{task} の知らせは、もう終わった作業のものなので使いません")
        return
    role = current["role"]
    if role == "fetch_issues":
        _fetch_issues_ended(run, base, failed, reason, say)
    elif role == "fetch_details":
        _end_task(run, base)
        if failed:
            run.log("fetch-details-failed", f"警告経路の取得がうまくいきませんでした（{reason or '理由なし'}）",
                    level="warning")
            if FATAL.search(reason or ""):  # authentication, TLS or server errors do not go away by trying again
                with run.update() as st:
                    st["fatal"] = f"警告経路を取れませんでした（{reason}）"
                    _alert(st, None, "取得", st["fatal"], "critical")
    elif role == "worker":
        _worker_ended(run, current["item"], failed, reason, say, actions)
    elif role == "reviewer":
        _reviewer_ended(run, current["item"], failed, reason, say, actions)
    elif role == "integrator":
        _integrator_ended(run, failed, reason, say, actions)


def _add_task(run: Run, base: str, task: str, role: str, item: str | None, description: str, **extra: Any) -> None:
    with run.update() as st:
        st.setdefault("tasks", {})[base] = {"task": task, "role": role, "item": item, "description": description,
                                            "launched_at": now_ts(), **extra}


def _end_task(run: Run, base: str) -> dict[str, Any] | None:
    with run.update() as st:
        return (st.get("tasks") or {}).pop(base, None)


def _alert(st: dict[str, Any], item: str | None, kind: str, detail: str, level: str = "warning") -> None:
    st.setdefault("alerts", []).append({"item": item, "kind": kind, "detail": detail, "level": level, "at": now_iso()})


# ==== one step ===================================================================================


def _step(run: Run, say: list[str], carried: list[dict[str, Any]]) -> Step:
    actions: list[dict[str, Any]] = list(carried)
    _reap_jobs(run, say, actions)
    _heal_orphans(run, say)
    _collect_outputs(run, say, actions)
    _expire_stale(run, say, actions)
    st = run.state

    # 1. initial analysis
    if st.get("initial") and not st.get("planned") and not _issues_done(run):
        status = initial.status(run)
        if status["status"] == "none":
            initial.start(run)
            run.log("initial-start", "初回解析を始めた（ビルド → 解析 → サーバへの登録）")
            say.append("初回解析（ビルド → 解析 → サーバへの登録）を始めました。数十分かかることがあります。")
            return Step("initial", actions, waitable=True)
        if status["status"] == "running":
            return Step("initial", actions, waitable=True)
        if status["status"] == "failed":
            message = f"初回解析に失敗しました（{status.get('step_label', '')}）: {status.get('error', '')}"
            with run.update() as s:
                if not s.get("initial_failed_logged"):
                    s["initial_failed_logged"] = True
                    _alert(s, None, "初回解析", message, "critical")
            run.log("initial", message, level="critical")
            say += [message, "トリアージは始めずに止めます。設定を直してから再開してください。"]
            return Step("initial", [], stop=True, error=message)
        if not st.get("initial_done_logged"):
            with run.update() as s:
                s["initial_done_logged"] = True
                s["fingerprint_start"] = _fingerprint(run) or s.get("fingerprint_start")
            run.log("initial", "初回解析が終わった（サーバに登録した）")
            say.append("初回解析が終わりました。警告を取得します。")

    # 2. the warnings, then the items
    if not st.get("planned"):
        if not _issues_done(run):
            if "fetch:issues" in (st.get("tasks") or {}):
                return Step("fetch", actions, waitable=True)
            fetch = st.get("fetch") or {}
            if fetch.get("issues_failures", 0) >= FETCH_TRIES:
                message = ("警告を取得できませんでした（Coverity に届かない、認証キーの期限切れ、条件のストリームが違う など）。"
                           "トリアージは始めずに止めます。")
                say.append(message)
                return Step("fetch", [], stop=True, error="警告を取得できませんでした")
            attempt = fetch.get("issues_attempts", 0) + 1
            with run.update() as s:
                s.setdefault("fetch", {})["issues_attempts"] = attempt
            task = task_name("fetch:issues", attempt)
            _add_task(run, "fetch:issues", task, "fetch_issues", None, "警告の取得")
            run.log("fetch-issues", f"警告の取得を頼んだ（{attempt} 回目）")
            actions.append({"type": "fetch_issues", "task": task, "repo_root": str(run.repo),
                            "filter_file": str(run.dir / "filter.json"), "output_file": str(run.dir / "issues.json")})
            return Step("fetch", actions)
        stop = _plan(run, say)
        if stop:
            return stop
        if not run.state["items"]:
            _finish(run, say)
            return Step("done", [], done=True)

    if run.state.get("fatal"):
        say.append(run.state["fatal"] + "。トリアージを続けられないため止めます。直してから再開してください。")
        return Step("details", [], stop=True, error=run.state["fatal"])

    # 3. the warning paths
    if not run.state.get("details_done"):
        result = _details(run, say, actions)
        if result:
            return result

    # 4. the items
    if run.state.get("copy_failed"):
        say.append("作業用のコピーを用意できないため止めます（ほかの作業でも同じことが起きます）。直してから再開してください。")
        return Step("items", actions, stop=True, error=run.state["copy_failed"])
    actions += _fill_slots(run, say)
    actions += _launch_ready(run, say)
    st = run.state
    c = counts(st)
    tasks = st.get("tasks") or {}
    busy_jobs = [j for j in jobs.running(run.dir) if j["name"] != "initial"]
    integration_open = st.get("integration") and not st.get("integrated")

    # 5. reviews (pipelined with the work when there is no integration)
    if st.get("review") and not integration_open and not any(t["role"] == "reviewer" for t in tasks.values()):
        launch = _start_review(run, say)
        if launch:
            actions.append(launch)
    if c["pending"] or c["in_progress"]:
        return Step("items", actions, waitable=bool(tasks or busy_jobs))

    # 6. integration
    if integration_open:
        if any(t["role"] == "integrator" for t in tasks.values()) or busy_jobs:
            return Step("integrate", actions, waitable=True)
        launch = _start_integration(run, say)
        if launch:
            return Step("integrate", actions + [launch])
        if st.get("review") and not any(t["role"] == "reviewer" for t in (run.state.get("tasks") or {}).values()):
            launch = _start_review(run, say)  # nothing to merge: the reviews start at once
            if launch:
                actions.append(launch)
    st = run.state
    tasks = st.get("tasks") or {}
    if st.get("review"):
        waiting = [i for i in st["items"] if i["status"] == "done" and i.get("review") is None]
        if any(t["role"] == "reviewer" for t in tasks.values()) or waiting or actions:
            return Step("review", actions, waitable=bool(tasks or busy_jobs))

    # 7. build verification
    if st.get("verify_mode", "none") != "none" and not st.get("verified"):
        job = jobs.read(run.dir, "verify")
        if not st.get("verify_started"):
            with run.update() as s:
                s["verify_started"] = True
            jobs.start(run.dir, "verify", "verify", {})
            run.log("verify-start", "ビルドでの確かめを始めた")
            say.append("修正案をまとめてビルドで確かめます（時間がかかることがあります）。")
        elif job is None:
            with run.update() as s:
                s["verified"] = True
        return Step("verify", actions, waitable=True)

    _finish(run, say)
    return Step("done", actions, done=True)


def _finish(run: Run, say: list[str]) -> None:
    if run.state.get("finished"):
        return
    with run.update() as st:
        st["finished"] = True
        st["finished_at"] = now_iso()
    c = counts(run.state)
    run.log("done", f"すべての作業が終わった（終わった {c['done']} 件、処理できなかった {c['error']} 件）")
    say.append(f"作業が終わりました（終わった {c['done']} 件、処理できなかった {c['error']} 件）。一覧を作ります。")


def _fingerprint(run: Run) -> str | None:
    try:
        return make_vcs(run.repo, run.settings).fingerprint()
    except RunError:
        return None


# ==== warnings, plan, warning paths ===========================================================


def _issues_done(run: Run) -> bool:
    data = try_json(run.dir / "issues.json")
    return isinstance(data, dict) and bool(data.get("finished_at"))


def _fetch_issues_ended(run: Run, base: str, failed: bool, reason: str, say: list[str]) -> None:
    _end_task(run, base)
    if not failed and _issues_done(run):
        run.log("fetch-issues", "警告の取得が終わった")
        return
    why = reason or ("警告の一覧のファイルが最後まで書かれていません" if not failed else "理由なし")
    with run.update() as st:
        fetch = st.setdefault("fetch", {})
        fetch["issues_failures"] = FETCH_TRIES if FATAL.search(why) else fetch.get("issues_failures", 0) + 1
        _alert(st, None, "取得", f"警告の取得に失敗しました: {why}", "critical" if FATAL.search(why) else "warning")
    run.log("fetch-issues-failed", f"警告の取得に失敗した: {why}", level="warning")
    say.append(f"警告の取得に失敗しました（{why}）。")


def _plan(run: Run, say: list[str]) -> Step | None:
    found = read_json(run.dir / "issues.json")
    problems = issues.check_issues_file(found)
    if problems:
        message = "警告の一覧（issues.json）の形が正しくありません: " + "；".join(problems[:5])
        say.append(message)
        return Step("fetch", [], stop=True, error=message)
    settings = run.settings
    mapper = PathMapper(run.repo, settings["coverity"]["path_strip_prefixes"])
    mapped, cleaned = [], []
    for record in found["issues"]:
        notes: list[str] = []
        issue = issues.clean_issue(record)
        issue["file"] = mapper.mapped(issue["file"], notes)
        cleaned.append(issue)
        mapped.append({**issue, "path_notes": notes})
    limit = int(run.state.get("limit") or len(cleaned))
    limited = bool(found.get("limited")) or len(cleaned) > limit
    cleaned, mapped = cleaned[:limit], mapped[:limit]  # never more than the run's limit
    write_json(run.dir / "issues.mapped.json", mapped)
    items = build_work_items(cleaned, load_no_grouping(run.repo))
    notes = []
    vcs = make_vcs(run.repo, settings)
    analyzed = found.get("analyzed_revision")
    if found.get("revision_error"):
        notes.append(f"解析したリビジョンを Coverity から取れませんでした: {found['revision_error']}")
    if analyzed and not vcs.has_revision(str(analyzed)):
        notes.append(f"Coverity が解析したリビジョン {analyzed} がリポジトリにありません。")
        analyzed = None
    if analyzed and run.state.get("initial") and initial.status(run).get("dirty"):
        notes.append("初回解析は、コミットしていない変更を含むフォルダで行いました。")
        analyzed = None
    if not analyzed:
        notes.append("解析したリビジョンが分からないため、手元のコードで調べ、警告とのずれを確かめます。")
    try:
        latest, note = vcs.resolve_latest()
    except RunError as exc:
        say.append(str(exc))
        return Step("fetch", [], stop=True, error=str(exc))
    if note:
        notes.append(note)
    with run.update() as st:
        st["items"] = [new_item(i["id"], i["cids"], cluster=i["cluster"]) for i in items]
        st["planned"] = True
        st["found"] = len(cleaned)
        st["limited"] = limited
        st["latest_revision"] = latest
        st["analyzed_revision"] = str(analyzed) if analyzed else None
        st["revision_notes"] = notes
    groups = [i for i in items if i["id"].startswith("G")]
    message = (f"警告が {len(cleaned)} 件見つかった" + ("（上限の件数で止めた）" if limited else "")
               + f"。作業は {len(items)} 件（グループ {len(groups)} 件）")
    run.log("plan", message, issues=len(cleaned), items=len(items), groups=[g["id"] for g in groups],
            latest=latest, analyzed=analyzed)
    say.append(message + "です。" if items else "条件に合う警告はありませんでした。")
    return None


def _detail_ok(path: Path) -> bool:
    data = try_json(path)
    return isinstance(data, dict) and (isinstance(data.get("issue"), dict) or isinstance(data.get("events"), list))


def _missing_details(run: Run) -> list[int]:
    missing = []
    for item in run.state["items"]:
        if item["status"] != "pending":
            continue
        for cid in item["cids"]:
            if not (run.item_dir(item["id"]) / f"issue-{cid}.json").is_file() and \
                    not (run.dir / "details" / f"issue-{cid}.json").is_file():
                missing.append(cid)
    return missing


def _distribute(run: Run) -> None:
    for item in run.state["items"]:
        for cid in item["cids"]:
            target = run.item_dir(item["id"]) / f"issue-{cid}.json"
            src = run.dir / "details" / f"issue-{cid}.json"
            if not target.is_file() and src.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)


def _details(run: Run, say: list[str], actions: list[dict[str, Any]]) -> Step | None:
    st = run.state
    tasks = st.get("tasks") or {}
    if any(t["role"] == "fetch_details" for t in tasks.values()):
        return Step("details", actions, waitable=True)
    rounds = (st.get("fetch") or {}).get("details_rounds", 0)
    if not st.get("offline") and rounds < DETAILS_ROUNDS:
        if rounds == 1:  # the second round fetches the CIDs that only have an error once more
            for path in (run.dir / "details").glob("issue-*.json"):
                if not _detail_ok(path):
                    path.unlink(missing_ok=True)
        missing = _missing_details(run)
        if missing:
            with run.update() as s:
                s.setdefault("fetch", {})["details_rounds"] = rounds + 1
            for n in range(0, len(missing), DETAILS_BATCH):
                number = n // DETAILS_BATCH + 1
                base = f"fetch:details:{number}"
                task = task_name(base, rounds + 1)
                _add_task(run, base, task, "fetch_details", None, "警告経路の取得", cids=missing[n:n + DETAILS_BATCH])
                actions.append({"type": "fetch_details", "task": task, "repo_root": str(run.repo),
                                "stream": run.state["stream"], "cids": missing[n:n + DETAILS_BATCH],
                                "output_dir": str(run.dir / "details")})
            run.log("fetch-details", f"警告経路の取得を頼んだ（{len(missing)} 件、{rounds + 1} 回目）")
            return Step("details", actions)
    _distribute(run)
    unfetched = []
    for item in run.state["items"]:
        for cid in item["cids"]:
            path = run.item_dir(item["id"]) / f"issue-{cid}.json"
            if not path.is_file():
                write_json(path, {"cid": cid, "stream": run.state["stream"], "fetched_at": now_iso(),
                                  "error": "取ってある警告経路のファイルがありません" if st.get("offline")
                                  else "2 回試しても取れませんでした"})
            if not _detail_ok(path):
                unfetched.append(cid)
    with run.update() as s:
        s["details_done"] = True
        if unfetched:
            _alert(s, None, "取得", f"警告経路を取れなかった警告が {len(unfetched)} 件あります（経路なしで調べます）: "
                   + ", ".join(map(str, unfetched[:20])))
    if unfetched:
        run.log("details-missing", f"警告経路を取れなかった警告が {len(unfetched)} 件ある（経路なしで調べる）", level="warning")
        say.append(f"警告経路を取れなかった警告が {len(unfetched)} 件あります（経路なしで調べます）。")
    return None


# ==== jobs =====================================================================================


def _reap_jobs(run: Run, say: list[str], actions: list[dict[str, Any]]) -> None:
    """Take in the jobs that ended. A job counts as taken in only after its handler went through: a handler
    that failed (a file another process held, on Windows) is tried again on the next step, a few times."""
    for job in jobs.all_jobs(run.dir):
        if job.get("status") == "running" or job.get("reaped"):
            continue
        name = job["name"]
        try:
            jobs.settle(run.dir, job)
            if job.get("status") == "stopped":
                pass  # stopped by stop: its item is put back by resume, or by _heal_orphans
            elif job.get("kind") == "copy":
                _copy_reaped(run, job, say)
            elif job.get("kind") == "save":
                _save_reaped(run, job, say, actions)
            elif job.get("kind") == "merge":
                _merge_reaped(run, job, say)
            elif job.get("kind") == "verify":
                _verify_reaped(run, job, say)
        except Exception as exc:  # noqa: BLE001 - one job's trouble must not stop the others
            tries = int(job.get("reap_errors") or 0) + 1
            why = f"{type(exc).__name__}: {exc}" if not isinstance(exc, (RunError, OSError)) else str(exc)
            jobs.update(run.dir, name, reap_errors=tries, reap_error=why[:1000])
            run.log("reap-failed", f"{name} の終わりを取り込めなかった（{tries} 回目）: {why[:300]}", level="warning")
            if tries < REAP_TRIES:
                say.append(f"{name} の終わりを取り込めませんでした（{why[:200]}）。もう一度試します。")
                continue
            jobs.update(run.dir, name, reaped=True)
            _reap_gave_up(run, job, f"{name} の終わりを取り込めませんでした: {why[:500]}", say)
            continue
        jobs.update(run.dir, name, reaped=True)


def _job_items(job: dict[str, Any]) -> list[str]:
    args = job.get("args") or {}
    if job.get("kind") == "merge":
        return [m.get("item") for m in args.get("merges") or [] if m.get("item")]
    return [args["item"]] if args.get("item") else []


def _reap_gave_up(run: Run, job: dict[str, Any], reason: str, say: list[str]) -> None:
    if job.get("kind") == "verify":
        with run.update() as st:
            if not st.get("verified"):
                st["verified"] = True
                _alert(st, None, "ビルドでの検証", reason)
        say.append(reason)
        return
    for item_id in _job_items(job):
        item = run.find(item_id)
        stage = SCRIPT_JOBS.get(item.get("stage") or "") if item else None
        if item and item["status"] == "in_progress" and stage and stage.format(slot=item.get("slot"), id=item_id) == job["name"]:
            _fail_item(run, item_id, reason, say)


def _heal_orphans(run: Run, say: list[str]) -> None:
    """An item at a script stage (copy, save, merge) whose job is not there, has ended for another item, or
    has ended and was taken in without moving the item, would wait for ever: nothing else looks at it.
    (A tick ended between the stage change and the job's start, or a handler failed.) Put it back."""
    for item in list(run.state["items"]):
        stage = item.get("stage") or ""
        if item["status"] != "in_progress" or stage not in SCRIPT_JOBS:
            continue
        name = SCRIPT_JOBS[stage].format(slot=item.get("slot"), id=item["id"])
        job = jobs.read(run.dir, name)
        if job is not None and item["id"] in _job_items(job) and \
                (job.get("status") == "running" or not job.get("reaped")):
            continue  # still working, or ended and not taken in yet (the next step does)
        why = "作業用のコピーを用意する処理が見つかりません" if stage == "copy" else \
            ("修正を保存する処理が見つかりません" if stage == "save" else "修正を合わせる処理が見つかりません")
        _back_or_fail(run, item["id"], stage, why, say)


def _back_or_fail(run: Run, item_id: str, stage: str, why: str, say: list[str]) -> None:
    """An item whose job never ran (or whose end was lost): back to pending; a save lost twice fails."""
    if stage == "save" and (run.item(item_id).get("orphan_retries") or 0) >= 1:
        _fail_item(run, item_id, f"{why}（2 回目）", say)
        return
    with run.update() as st:
        current = run.item(item_id, st)
        if current["status"] != "in_progress" or current.get("stage") != stage:
            return
        if stage == "copy":
            current["attempts"] = max(0, (current.get("attempts") or 1) - 1)
        if stage == "save":
            current["orphan_retries"] = (current.get("orphan_retries") or 0) + 1
        current.update(status="pending", stage=None, slot=None)
        _alert(st, item_id, "作業", f"{why}。もう一度始めます")
    run.log("orphan", f"{item_id}: {why}（もう一度始める）", level="warning", item=item_id, stage=stage)
    say.append(f"{item_id}: {why}。もう一度始めます。")


def _copy_reaped(run: Run, job: dict[str, Any], say: list[str]) -> None:
    item_id = (job.get("args") or {}).get("item")
    item = run.find(item_id)
    if item is None or item["status"] != "in_progress" or item.get("stage") != "copy":
        return
    if job.get("status") == "done":
        with run.update() as st:
            run.item(item_id, st)["stage"] = "ready"
        if (job.get("result") or {}).get("created"):
            run.log("copy", f"{item_id} の作業用のコピーを作った", item=item_id)
        return
    error = f"作業用のコピーを用意できませんでした: {job.get('error') or '理由なし'}"
    with run.update() as st:
        finish_item(run.item(item_id, st), "error", error)
        _alert(st, item_id, "作業", error, "critical")
        st["copy_failed"] = error
    run.log("copy-failed", f"{item_id}: {error}", level="critical", item=item_id)
    say.append(f"{item_id}: {error}")


def _save_reaped(run: Run, job: dict[str, Any], say: list[str], actions: list[dict[str, Any]]) -> None:
    item_id = (job.get("args") or {}).get("item")
    item = run.find(item_id)
    if item is None or item["status"] != "in_progress" or item.get("stage") != "save":
        return
    result = job.get("result") or {}
    if job.get("status") != "done":
        _fail_item(run, item_id, f"修正を保存できませんでした: {job.get('error') or '理由なし'}", say)
        return
    if result.get("problems"):
        _fix_request(run, item_id, result["problems"], say, actions)
        return
    excluded = result.get("excluded") or []
    split = []
    with run.update() as st:
        current = run.item(item_id, st)
        if excluded:
            current["cids"] = [c for c in current["cids"] if c not in excluded]
            for cid in excluded:
                for member in st["items"]:  # a merged member that held the CID no longer does
                    if member["status"] == "merged" and member.get("merged_into") == item_id and cid in member["cids"]:
                        member["cids"] = [c for c in member["cids"] if c != cid]
                same = next((i for i in st["items"] if i["id"] == str(cid)), None)
                if same is None:
                    st["items"].append(new_item(str(cid), [cid], split_from=item_id))
                elif same["status"] == "merged":
                    same.update(status="pending", merged_into=None, cids=[cid], split_from=item_id, review=None,
                                rework=[], review_rounds=0)
                    current["merged_from"] = [m for m in current.get("merged_from") or [] if m != same["id"]]
                split.append(str(cid))
        current["warnings"] = list(dict.fromkeys([*(current.get("warnings") or []), *(result.get("warnings") or [])]))
        if result.get("analyzed_dirty"):
            st["analyzed_dirty"] = True
        reworked = bool(current.get("rework"))
        current["rework"] = []
        current["review"] = None
        current["result_sha"] = file_sha(run.result_file(item_id))
        finish_item(current, "done")
    if reworked:  # after the commit: a file another process holds must not undo it
        for leftover in (PREVIOUS_RESULT, REWORK_FRESH):
            try:
                remove_file(run.item_dir(item_id) / leftover)
            except OSError:
                pass
    data = read_json(run.result_file(item_id), {})
    run.log("finish", f"{item_id} を終えた（推奨 {report.PLAN_JA.get(data.get('recommendation'), '-')}、確信度 "
            f"{report.CONFIDENCE_JA.get(data.get('confidence'), '-')}）", item=item_id,
            recommendation=data.get("recommendation"), confidence=data.get("confidence"), split=split)
    if split:
        say.append(f"{item_id}: 原因の違う CID {', '.join(split)} を別の作業にしました。")
    for warning in result.get("warnings") or []:
        say.append(f"{item_id}: {warning}")
    if run.summary_file().is_file():
        try:
            approvals.ensure_defaults(run, {item_id: data})
        except RunError as exc:  # a broken approvals.json is never overwritten; summary says the same
            with run.update() as st:
                _alert(st, item_id, "承認", str(exc))
            say.append(str(exc))
    report.write_item(run, item_id)


def _merge_reaped(run: Run, job: dict[str, Any], say: list[str]) -> None:
    out = (job.get("result") or {}).get("items") or {}
    for item_id in [m["item"] for m in (job.get("args") or {}).get("merges") or []]:
        info = out.get(item_id) or {"built": False, "reason": job.get("error") or "修正を合わせられませんでした"}
        item = run.find(item_id)
        if item is None or item.get("stage") != "merge":
            continue
        with run.update() as st:
            current = run.item(item_id, st)
            if info.get("built"):
                current["started"] = time.time()
                finish_item(current, "done")
                current["seconds"] = 0.0
                current["result_sha"] = file_sha(run.result_file(item_id))
            else:
                current.update(status="pending", stage=None, slot=None)
        if info.get("built"):
            run.log("merge", f"{item_id} の修正を、元の作業の修正から作った", item=item_id)
            report.write_item(run, item_id)
        else:
            run.log("merge", f"{item_id} の修正は作業役が作り直す（{info.get('reason', '')}）", item=item_id, level="warning")


def _verify_reaped(run: Run, job: dict[str, Any], say: list[str]) -> None:
    if run.state.get("verified"):
        return  # taken in already (a later part of this handler failed once)
    with run.update() as st:
        st["verified"] = True
        if job.get("status") != "done":
            message = f"ビルドでの確かめはできませんでした（{job.get('error') or '理由なし'}）"
            st.setdefault("notes", []).append(message)
            _alert(st, None, "ビルドでの検証", message)
    result = job.get("result") or {}
    if job.get("status") != "done":
        run.log("verify-failed", "ビルドでの確かめができなかった（確かめずに一覧を作る）", level="warning")
        say.append("ビルドでの確かめはできませんでした。確かめずに一覧を作ります。")
        return
    if result.get("error"):
        with run.update() as st:
            _alert(st, None, "ビルドでの検証", result["error"])
    label = "成功" if result.get("build_ok") else ("確かめるものなし" if result.get("build_ok") is None else "失敗")
    run.log("verify", f"ビルドでの確かめが終わった（{label}）", build_ok=result.get("build_ok"))
    say.append(f"ビルドでの確かめが終わりました（{label}）。")
    for item in run.state["items"]:
        if item["status"] == "done":
            report.write_item(run, item["id"])


# ==== workers ======================================================================================


def _fill_slots(run: Run, say: list[str]) -> list[dict[str, Any]]:
    """Give free slots to pending items: each gets its work copy from a copy job."""
    st = run.state
    if st.get("copy_failed"):
        return []
    busy = {i["slot"] for i in st["items"] if i["status"] == "in_progress" and i.get("slot")}
    copying = {j["name"] for j in jobs.running(run.dir)}
    free = [s for s in range(1, int(st.get("parallel") or 1) + 1) if s not in busy and f"copy-w{s}" not in copying]
    pending = [i["id"] for i in st["items"] if i["status"] == "pending"]
    if not free or not pending:
        return []
    fingerprint = _fingerprint(run)
    for item_id, slot in zip(pending, free):
        # The earlier attempt's files go first (while the item is still pending: a failure here changes nothing).
        folder = run.item_dir(item_id)
        folder.mkdir(parents=True, exist_ok=True)
        result = run.result_file(item_id)
        if run.item(item_id).get("rework") and result.is_file() and not (folder / PREVIOUS_RESULT).is_file():
            _replace(result, folder / PREVIOUS_RESULT)
        remove_file(result)
        remove_file(folder / "progress.json")
        with run.update() as s:
            if s.get("stopped"):  # stop came while this step ran: start nothing
                return []
            item = run.item(item_id, s)
            if item["status"] != "pending":
                continue
            others = [i for i in s["items"] if i["status"] == "in_progress" and i["id"] != item_id]
            reset_analyzed = bool(s.get("analyzed_dirty")) and not others
            if reset_analyzed:
                s["analyzed_dirty"] = False
            item.update(status="in_progress", slot=slot, stage="copy", started=time.time(), fix_rounds=0,
                        error="", repo_fingerprint=fingerprint)
            item["attempts"] = item.get("attempts", 0) + 1
            if not item.get("rework"):
                item["warnings"] = []
        try:
            jobs.start(run.dir, f"copy-w{slot}", "copy", {"item": item_id, "slot": slot, "reset_analyzed": reset_analyzed})
        except BaseException:
            with run.update() as s:  # the job did not start: the item is pending again
                item = run.item(item_id, s)
                if item["status"] == "in_progress" and item.get("stage") == "copy":
                    item.update(status="pending", slot=None, stage=None)
                    item["attempts"] = max(0, (item.get("attempts") or 1) - 1)
                if reset_analyzed:
                    s["analyzed_dirty"] = True
            raise
        run.log("next", f"{item_id} の作業を始めた（枠 {slot}、作業用のコピーを用意する）", item=item_id, slot=slot)
    return []


def _launch_ready(run: Run, say: list[str]) -> list[dict[str, Any]]:
    actions = []
    for item in list(run.state["items"]):
        if item["status"] == "in_progress" and item.get("stage") == "ready":
            actions.append(_launch_worker(run, item["id"]))
    return actions


def _launch_worker(run: Run, item_id: str, problems: list[str] | None = None) -> dict[str, Any]:
    item = run.item(item_id)
    brief = briefs.worker(run, item, problems)
    task = task_name(f"worker:{item_id}", item.get("attempts") or 1, (item.get("fix_rounds") or 0) + 1)
    if problems:
        description = f"{item_id} の結果の直し"
    elif item.get("rework"):
        description = f"{item_id} の作り直し（{item.get('review_rounds', 1)} 回目）"
    else:
        description = f"{item_id} の作業"
    with run.update() as st:
        run.item(item_id, st)["stage"] = "work"
    _add_task(run, f"worker:{item_id}", task, "worker", item_id, description)
    run.log("launch", f"作業役に「{description}」を頼んだ", item=item_id, role="worker", task=task)
    return {"type": "launch", "task": task, "role": "worker", "item": item_id, "description": description,
            "prompt": briefs.prompt(brief), "background": True}


def _fix_request(run: Run, item_id: str, problems: list[str], say: list[str], actions: list[dict[str, Any]]) -> None:
    item = run.item(item_id)
    if (item.get("fix_rounds") or 0) >= FIX_ROUNDS:
        _fail_item(run, item_id, f"結果を {FIX_ROUNDS} 回直しても決まりに合いませんでした: {'；'.join(problems)[:500]}", say)
        return
    with run.update() as st:
        current = run.item(item_id, st)
        current["fix_rounds"] = (current.get("fix_rounds") or 0) + 1
        current["stage"] = "work"
    run.log("fix-request", f"{item_id} の結果に直すところがあった（作業役が直す）", level="warning", item=item_id,
            problems=problems)
    actions.append(_launch_worker(run, item_id, problems))


def _worker_ended(run: Run, item_id: str, failed: bool, reason: str, say: list[str],
                  actions: list[dict[str, Any]]) -> None:
    _end_task(run, f"worker:{item_id}")
    item = run.find(item_id)
    if item is None or item["status"] != "in_progress" or item.get("stage") != "work":
        return
    if failed:
        _fail_item(run, item_id, reason or "作業役が途中で止まりました", say)
        return
    data = try_json(run.result_file(item_id))
    if data is None:
        checked = {"ok": False, "problems": [f"結果ファイル {run.result_file(item_id)} がないか、JSON として読めません"],
                   "warnings": []}
    else:
        checked = rules.result_check(data, item_id, item["cids"])
    if not checked["ok"]:
        _fix_request(run, item_id, checked["problems"], say, actions)
        return
    with run.update() as st:
        if st.get("stopped"):  # stop came while this step ran: resume puts the item back
            return
        current = run.item(item_id, st)
        current["stage"] = "save"
        current["warnings"] = list(dict.fromkeys([*(current.get("warnings") or []), *checked["warnings"]]))
    try:
        jobs.start(run.dir, f"save-{item_id}", "save", {"item": item_id, "slot": item["slot"]})
    except BaseException as exc:
        _back_or_fail(run, item_id, "save", f"修正を保存する処理を始められませんでした（{exc}）", say)
        raise


def _fail_item(run: Run, item_id: str, reason: str, say: list[str]) -> None:
    members: list[str] = []
    with run.update() as st:
        item = run.item(item_id, st)
        finish_item(item, "error", reason)
        _alert(st, item_id, "作業", reason, "critical")
        if item.get("merged_from"):
            members = integrate.undo(st, item_id)
    run.log("fail", f"{item_id} を処理できなかった: {reason}", level="critical", item=item_id)
    if members:
        say.append(f"{item_id}: 処理できなかったため、統合をやめて元の作業 {', '.join(members)} に戻しました。")
    else:
        say.append(f"{item_id}: 処理できませんでした（{reason[:200]}）。")


# ==== reviews =======================================================================================


def _start_review(run: Run, say: list[str]) -> dict[str, Any] | None:
    target = next((i for i in run.state["items"] if i["status"] == "done" and i.get("review") is None), None)
    if target is None:
        return None
    item_id = target["id"]
    remove_file(run.review_file(item_id))
    remove_file(run.dir / "agents" / "reviewer" / "progress.json")
    with run.update() as st:
        current = run.item(item_id, st)
        current["review_gen"] = (current.get("review_gen") or 0) + 1
        current["review_fix_rounds"] = 0
        gen = current["review_gen"]
    return _launch_reviewer(run, item_id, gen, 1, None)


def _launch_reviewer(run: Run, item_id: str, gen: int, round_: int, problems: list[str] | None) -> dict[str, Any]:
    item = run.item(item_id)
    brief = briefs.reviewer(run, item, problems)
    saved = read_json(run.item_dir(item_id) / "saved.json", {})
    patch = (saved.get("fix") or {}).get("patch")
    task = task_name(f"reviewer:{item_id}", gen, round_)
    description = f"{item_id} の確かめ" + ("の直し" if problems else "")
    _add_task(run, f"reviewer:{item_id}", task, "reviewer", item_id, description)
    with run.update() as st:
        current = st["tasks"][f"reviewer:{item_id}"]
        current["result_sha"] = file_sha(run.result_file(item_id))
        current["patch_sha"] = file_sha(Path(patch)) if patch else None
        current["fingerprint"] = _fingerprint(run)
    run.log("launch", f"検証役に「{description}」を頼んだ", item=item_id, role="reviewer", task=task)
    return {"type": "launch", "task": task, "role": "reviewer", "item": item_id, "description": description,
            "prompt": briefs.prompt(brief), "background": True}


def _reviewer_ended(run: Run, item_id: str, failed: bool, reason: str, say: list[str],
                    actions: list[dict[str, Any]]) -> None:
    task = _end_task(run, f"reviewer:{item_id}") or {}
    item = run.find(item_id)
    if item is None or item["status"] != "done":
        return
    path = run.review_file(item_id)
    review = None
    problems: list[str] = []
    if not failed:
        fresh = (mtime(path) or 0) >= float(task.get("launched_at") or 0) - 1
        review = try_json(path) if fresh else None
        if review is None:
            problems = [f"確かめの結果 {path} がないか、JSON として読めません（この確かめを頼んだ後に書いたものだけを使います）"]
        else:
            problems = rules.review_check(review, item_id, *briefs.review_context(run, item))["problems"]
        if problems and (item.get("review_fix_rounds") or 0) < FIX_ROUNDS:
            with run.update() as st:
                current = run.item(item_id, st)
                current["review_fix_rounds"] = (current.get("review_fix_rounds") or 0) + 1
                round_ = current["review_fix_rounds"] + 1
                gen = current.get("review_gen") or 1
            run.log("fix-request", f"{item_id} の確かめの結果に直すところがあった（検証役が直す）", level="warning",
                    item=item_id, problems=problems)
            actions.append(_launch_reviewer(run, item_id, gen, round_, problems))
            return
        if not problems:
            saved = read_json(run.item_dir(item_id) / "saved.json", {})
            patch = (saved.get("fix") or {}).get("patch")
            if file_sha(run.result_file(item_id)) != task.get("result_sha") or \
                    (patch and file_sha(Path(patch)) != task.get("patch_sha")):
                failed, reason = True, "確かめの間に、結果ファイルか修正の差分が変わりました（この確かめは使いません）"
            elif task.get("fingerprint") and _fingerprint(run) != task["fingerprint"]:
                say.append(f"{item_id}: 確かめの間に、利用者のリポジトリのファイルが変わりました。誤って変えていないか確かめてください。")
    with run.update() as st:
        current = run.item(item_id, st)
        if failed or problems or review is None:
            why = reason or "；".join(problems) or "理由なし"
            current["review"] = "unverified"
            _alert(st, item_id, "確かめ", f"確かめられませんでした: {why}")
            outcome = "unverified"
        elif review["outcome"] == "rework" and (current.get("review_rounds") or 0) < int(st.get("max_rework") or 0):
            current.update(review="rework", status="pending", slot=None, stage=None, rework=list(review.get("rework") or []))
            current["review_rounds"] = (current.get("review_rounds") or 0) + 1
            outcome = "rework"
        else:
            current["review"] = "flag" if review["outcome"] == "rework" else review["outcome"]
            outcome = current["review"]
    label = {"pass": "合格", "flag": "要確認", "rework": "差し戻し", "unverified": "未検証"}[outcome]
    run.log("review", f"{item_id} の確かめが終わった（{label}）", item=item_id, outcome=outcome,
            level="info" if outcome == "pass" else "warning")
    if outcome == "rework":
        say.append(f"{item_id}: 確かめで差し戻しました（{(review or {}).get('note', '')}）。作り直します。")
    elif outcome in ("flag", "unverified"):
        say.append(f"{item_id}: 確かめの結果は「{label}」です。")
    if run.item(item_id)["status"] == "done":
        report.write_item(run, item_id)


# ==== integration =====================================================================================


def _start_integration(run: Run, say: list[str]) -> dict[str, Any] | None:
    found = integrate.candidates(run)
    write_json(run.dir / "integration" / "candidates.json", found)
    if not found:
        with run.update() as st:
            st["integrated"] = True
        run.log("integrate", "同じ原因でまとめる候補はなかった（統合しない）", candidates=0)
        say.append("同じ原因でまとめられる候補はありませんでした。")
        return None
    with run.update() as st:
        st["integration_gen"] = (st.get("integration_gen") or 0) + 1
        st["integration_fix_rounds"] = 0
    remove_file(run.dir / "integration" / "plan.json")
    remove_file(run.dir / "agents" / "integrator" / "progress.json")
    return _launch_integrator(run, found, None)


def _launch_integrator(run: Run, found: list[dict[str, Any]], problems: list[str] | None) -> dict[str, Any]:
    brief = briefs.integrator(run, found, problems)
    task = task_name("integrator", run.state.get("integration_gen") or 1, (run.state.get("integration_fix_rounds") or 0) + 1)
    description = "同じ原因の統合" + ("の直し" if problems else "")
    _add_task(run, "integrator", task, "integrator", None, description)
    run.log("launch", f"統合役に「{description}」を頼んだ", role="integrator", task=task)
    return {"type": "launch", "task": task, "role": "integrator", "item": None, "description": description,
            "prompt": briefs.prompt(brief), "background": True}


def _integrator_ended(run: Run, failed: bool, reason: str, say: list[str], actions: list[dict[str, Any]]) -> None:
    task = _end_task(run, "integrator") or {}
    if run.state.get("integrated"):
        return
    problems: list[str] = []
    plan = None
    if not failed:
        path = run.dir / "integration" / "plan.json"
        if (mtime(path) or 0) < float(task.get("launched_at") or 0) - 1:
            problems = [f"計画のファイル {path} がありません（この統合を頼んだ後に書いたものだけを使います）"]
        else:
            plan, problems = integrate.check(run)
        if problems and (run.state.get("integration_fix_rounds") or 0) < FIX_ROUNDS:
            with run.update() as st:
                st["integration_fix_rounds"] = (st.get("integration_fix_rounds") or 0) + 1
            run.log("fix-request", "統合の計画に直すところがあった（統合役が直す）", level="warning", problems=problems)
            found = read_json(run.dir / "integration" / "candidates.json", [])
            actions.append(_launch_integrator(run, found, problems))
            return
    if failed or problems or plan is None:
        why = reason or "；".join(problems) or "理由なし"
        with run.update() as st:
            st["integrated"] = True
            st.setdefault("notes", []).append(f"統合はできませんでした（{why}）。まとめずに進めました。")
            _alert(st, None, "統合", f"統合はできませんでした: {why}")
        run.log("integrate", f"統合はできなかった（{why[:200]}）", level="warning")
        say.append("統合はできませんでした。まとめずに進めます。")
        return
    with run.update() as st:
        made, warnings = integrate.apply(run, plan, st)
        st["integrated"] = True
        to_build = []
        for entry in made:
            current = run.item(entry["item"], st)
            if entry["source"] == "rework" or st["settings"]["options"]["annotation"] or st.get("stopped"):
                continue  # a worker writes the fix (and the annotation, which must carry the merged comment)
            current.update(status="in_progress", stage="merge", started=time.time())
            to_build.append(entry)
    if to_build:
        try:
            jobs.start(run.dir, "merge", "merge", {"merges": to_build})
        except BaseException:
            with run.update() as st:  # the job did not start: a worker makes these fixes
                for entry in to_build:
                    current = run.item(entry["item"], st)
                    if current["status"] == "in_progress" and current.get("stage") == "merge":
                        current.update(status="pending", stage=None, slot=None)
            raise
    names = ", ".join(e["item"] for e in made)
    run.log("integrate", f"同じ原因の作業を {len(made)} つにまとめた" + (f"（{names}）" if made else ""),
            merges=[e["item"] for e in made], conflicts=len(plan.get("conflicts") or []))
    say.append(f"同じ原因の作業を {len(made)} つにまとめました。" if made else "まとめる作業はありませんでした。")
    for w in warnings:
        run.log("conflict", w, level="warning")
    for item_id in {c for conflict in plan.get("conflicts") or [] for c in conflict.get("items") or []}:
        if (run.find(item_id) or {}).get("status") == "done":
            report.write_item(run, item_id)


# ==== outputs found without a report, and stale tasks =============================================


def _settled(path: Path, since: float) -> bool:
    t = mtime(path)
    return t is not None and t >= since - 1 and time.time() - t >= SETTLE_SECONDS


def _collect_outputs(run: Run, say: list[str], actions: list[dict[str, Any]]) -> None:
    for base, task in list((run.state.get("tasks") or {}).items()):
        role, item_id, since = task.get("role"), task.get("item"), float(task.get("launched_at") or 0)
        if role == "fetch_issues" and _issues_done(run) and (mtime(run.dir / "issues.json") or 0) >= since - 2:
            _fetch_issues_ended(run, base, False, "", say)
        elif role == "fetch_details":
            cids = task.get("cids") or []
            if cids and all((run.dir / "details" / f"issue-{c}.json").is_file() for c in cids):
                _end_task(run, base)
        elif role == "worker" and _settled(run.result_file(item_id), since):
            item = run.find(item_id) or {}
            data = try_json(run.result_file(item_id))
            if data is not None and rules.result_check(data, item_id, item.get("cids"))["ok"]:
                run.log("output-found", f"{item_id} の結果ファイルができていた（知らせの前に進める）", item=item_id)
                _worker_ended(run, item_id, False, "", say, actions)
        elif role == "reviewer" and _settled(run.review_file(item_id), since):
            data = try_json(run.review_file(item_id))
            item = run.find(item_id)
            if data is not None and item is not None and \
                    rules.review_check(data, item_id, *briefs.review_context(run, item))["ok"]:
                _reviewer_ended(run, item_id, False, "", say, actions)
        elif role == "integrator" and _settled(run.dir / "integration" / "plan.json", since):
            plan, problems = integrate.check(run)
            if plan is not None and not problems:
                _integrator_ended(run, False, "", say, actions)


def _activity(run: Run, task: dict[str, Any]) -> float:
    role, item_id = task.get("role"), task.get("item")
    times = [float(task.get("launched_at") or 0)]
    if role == "worker" and item_id:
        folder = run.item_dir(item_id)
        times += [mtime(p) or 0 for p in folder.glob("*")] if folder.is_dir() else []
    elif role == "reviewer":
        times += [mtime(run.review_file(item_id)) or 0, mtime(run.dir / "agents" / "reviewer" / "progress.json") or 0]
    elif role == "integrator":
        times += [mtime(run.dir / "integration" / "plan.json") or 0,
                  mtime(run.dir / "agents" / "integrator" / "progress.json") or 0]
    elif role == "fetch_issues":
        times.append(mtime(run.dir / "issues.json") or 0)
    elif role == "fetch_details":
        times += [mtime(p) or 0 for p in (run.dir / "details").glob("*")]
    return max(times)


def _expire_stale(run: Run, say: list[str], actions: list[dict[str, Any]]) -> None:
    now = time.time()
    for base, task in list((run.state.get("tasks") or {}).items()):
        if now - _activity(run, task) < STALE_SECONDS:
            continue
        role, item_id = task.get("role"), task.get("item")
        reason = "30 分のあいだ、出力も進み具合もありませんでした"
        run.log("stale", f"{task.get('task')}: {reason}（失敗としてやり直す）", level="warning", item=item_id)
        if role == "worker":
            _end_task(run, base)
            item = run.find(item_id)
            if item and item["status"] == "in_progress":
                if (item.get("stale_retries") or 0) < 1:
                    with run.update() as st:
                        current = run.item(item_id, st)
                        current["stale_retries"] = (current.get("stale_retries") or 0) + 1
                        current.update(status="pending", slot=None, stage=None)
                    say.append(f"{item_id}: {reason}。やり直します。")
                else:
                    _fail_item(run, item_id, reason, say)
        elif role == "reviewer":
            if (run.find(item_id) or {}).get("review_stale"):
                _reviewer_ended(run, item_id, True, reason, say, actions)
            else:
                _end_task(run, base)
                with run.update() as st:
                    current = run.item(item_id, st)
                    current["review_stale"] = True
                    current["review"] = None
                say.append(f"{item_id} の確かめ: {reason}。やり直します。")
        elif role == "integrator":
            if run.state.get("integration_stale"):
                _integrator_ended(run, True, reason, say, actions)
            else:
                _end_task(run, base)
                with run.update() as st:
                    st["integration_stale"] = True
                say.append(f"統合: {reason}。やり直します。")
        elif role == "fetch_issues":
            _fetch_issues_ended(run, base, True, reason, say)
        else:
            _end_task(run, base)
