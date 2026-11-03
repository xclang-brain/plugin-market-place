"""What the agents of a run are doing, for the monitor of the screen (``ct.py gui``).

Read only from the files of the run folder; nothing here changes a file:

- ``run.json`` (state of every item, and the collector / integrator / reviewer agents at work in
  ``roles``) and ``operations.log`` (steps of ct.py and calls of the MCP server)
- ``items/<ID>/progress.json``: milestones the worker subagent writes (best effort; it has no clock,
  so the time is the file's mtime). Without it a worker is shown from the log alone.
  ``agents/integrator/progress.json`` and ``agents/reviewer/progress.json`` are the same for the
  integrator and the reviewer; the collector writes none (its MCP calls are in the log).
- ``analysis/status.json`` and ``analysis/initial.log``: the initial analysis (option verify.initial)
- problems kept in other files: ``items/*/issue-*.json`` (warning paths Coverity did not return),
  ``verify.json`` (build verification) and ``apply-plan.result.json`` (write-back to Coverity).

``board()`` is what the screen draws; ``alerts()`` are its serious errors and warnings.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import initial, oplog
from .config import DEFAULT_PARALLEL
from .run import ItemState, Run

STALL_SECONDS = 600  # an item (or an agent) with no sign of work for this long may have stopped
INITIAL_STALL_SECONDS = 1800  # the build may print nothing for 10 minutes: the log of the initial analysis gets 30
RECENT = 40
STATUS_RECENT = 10  # lines of the recent steps that ct.py status gives
CLOCK = re.compile(r"\d{2}:\d{2}:\d{2}")
MAX_ALERTS = 100
TEXT_CHARS = 160  # one line of the screen
DETAIL_CHARS = 4000
PROGRESS_FILE = "progress.json"
LEVELS = ("info", "warning", "critical")

STAGES = {
    "initial": "初回解析（ビルド・解析・サーバへの登録）",
    "search": "Coverity で警告を検索中",
    "plan": "作業の一覧を作っている",
    "paths": "警告経路を取っている",
    "items": "警告を調べている",
    "integrate": "同じ原因の警告をまとめている",
    "review": "結果を検証している",
    "verify": "ビルドで確かめている",
    "summary": "一覧を作っている",
    "apply": "反映の途中",
    "done": "終わりました",
}
INITIAL_FAILED = "初回解析に失敗しました（止まっています）"
INITIAL_JA = {"none": "待ち", "running": "実行中", "done": "登録まで終わった", "failed": "失敗した"}
ROLES = {"collector": "取得", "integrator": "統合", "reviewer": "検証"}  # the agents besides the workers, in order
ROLE_PHASES = {
    "integrator": {"read": "結果と差分を読む", "compare": "同じ原因かを比べる", "write": "計画を書く"},
    "reviewer": {"read": "警告経路と結果を読む", "check": "コードで確かめる", "write": "検証の結果を書く"},
}
ROLE_STATES = {"waiting": "待ち", "running": "動いている", "fallback": "メインの AI が代わりにしている",
               "done": "終わった", "failed": "うまくいかなかった"}
REVIEW_JA = {"pass": "合格", "flag": "要確認", "rework": "差し戻し", "unverified": "検証できなかった"}
WORKER_STAGES = {
    "idle": "空き",
    "copy": "修正用のコピーを用意中",
    "fetch": "警告経路を取得中",
    "brief": "指示を作っている",
    "investigate": "調査中",
    "retry": "結果を直している",
    "finishing": "結果を確かめている",
}
PHASES = {
    "read": "コードを読む",
    "judge": "真偽を判断",
    "fix": "修正を作る",
    "deviation": "逸脱を書く",
    "result": "結果を書く",
}
PLAN_JA = {"fix": "修正", "deviation": "逸脱"}
CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}


# ---- small helpers ---------------------------------------------------------------------------


def _epoch(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(epoch: float | None) -> str:
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _short(text: Any, limit: int = TEXT_CHARS) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _detail(*parts: Any) -> str:
    lines: list[str] = []
    for part in parts:
        if isinstance(part, (list, tuple)):
            lines += [f"- {p}" for p in part if p]
        elif part:
            lines.append(str(part))
    text = "\n".join(lines)
    return text if len(text) <= DETAIL_CHARS else text[:DETAIL_CHARS - 1] + "…"


def _item(record: dict[str, Any]) -> str | None:
    value = record.get("item")
    return None if value in (None, "") else str(value)


def _level(record: dict[str, Any]) -> str:
    # A failed item stops its work; older logs have no level at all.
    if record.get("event") == "fail":
        return "critical"
    level = record.get("level")
    return level if level in LEVELS else "info"


def _source(record: dict[str, Any]) -> str:
    return "mcp" if record.get("source") == "mcp" else "ct"


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


def _num(value: Any) -> float | None:
    """A time or a number kept in a JSON file (anything else is None)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _duration(seconds: Any) -> str:
    value = _num(seconds)
    if value is None:
        return ""
    return f"{round(value)} 秒" if value < 60 else f"{round(value / 60)} 分"


def _task_label(role: Any, task: Any, item: str | None) -> str:
    """What one start of the collector, integrator or reviewer is for."""
    if role == "collector":
        return {"search": "警告の検索", "paths": "警告経路の取得"}.get(str(task), str(task or "取得"))
    if role == "integrator":
        return "同じ原因の警告をまとめる"
    if role == "reviewer":
        return f"{item} の検証" if item else "結果の検証"
    return str(task or "")


def _role_name(role: Any) -> str:
    return f"{ROLES[role]}エージェント" if role in ROLES else f"{role or ''} エージェント"


def _last(records: list[dict[str, Any]], *events: str, command: str | None = None) -> int:
    """Index of the last record of one of ``events`` (or of an error of ``command``); -1 if none."""
    for i in range(len(records) - 1, -1, -1):
        record = records[i]
        if record["event"] in events or (command and record["event"] == "error"
                                         and record.get("command") == command):
            return i
    return -1


# ---- one line per record ---------------------------------------------------------------------


def _mcp_text(tool: str, record: dict[str, Any]) -> str:
    phase, item = record.get("phase"), _item(record)
    error = _short(record.get("error"), 100)
    if tool == "search_issues":
        if phase == "start":
            return "Coverity で警告を検索し始めた"
        if phase == "error":
            return f"Coverity での検索が失敗した: {error}"
        notes = [f"{_count(record.get('found'))} 件"]
        if record.get("revision_error"):
            notes.append("解析リビジョンを取れなかった")
        if not record.get("done"):
            notes.append("続きがある")
        return f"Coverity で警告を検索した（{'、'.join(notes)}）"
    if tool == "get_issues":
        who = f"{item} の" if item else ""
        if phase == "start":
            return f"{who}警告経路を Coverity から取り始めた"
        if phase == "error":
            return f"{who}警告経路を取れなかった: {error}"
        notes = [f"新しく {_count(record.get('saved'))} 件"]
        if record.get("failed"):
            notes.append(f"取れなかった {_count(record.get('failed'))} 件")
        if not record.get("done"):
            notes.append("続きがある")
        return f"{who}警告経路を取った（{'、'.join(notes)}）"
    if tool == "update_triage":
        if phase == "start":
            return "Coverity にトリアージを書き込み始めた"
        if phase == "error":
            return f"Coverity への書き込みが失敗した: {error}"
        notes = [f"{_count(record.get('written_total'))}/{_count(record.get('entries'))} 件"]
        if record.get("failed"):
            notes.append(f"うまくいかなかった {_count(record.get('failed'))} 件")
        if not record.get("done"):
            notes.append("続きがある")
        return f"Coverity にトリアージを書き込んだ（{'、'.join(notes)}）"
    return f"MCP の {tool}（{phase or '-'}）"


def _text(record: dict[str, Any]) -> str:
    """One line of plain Japanese for a record of operations.log."""
    try:
        return _short(_describe(record))
    except (AttributeError, TypeError, ValueError):
        return _short(" ".join(p for p in (record["event"], _item(record)) if p))


def _describe(record: dict[str, Any]) -> str:
    event, item = record["event"], _item(record)
    if event.startswith("mcp:"):
        return _mcp_text(event[4:], record)
    if event == "new-run":
        flt = record.get("filter") if isinstance(record.get("filter"), dict) else {}
        return f"実行を始めた（条件 {flt.get('name') or '-'}、上限 {record.get('limit', '-')} 件）"
    if event == "plan":
        groups = _count(record.get("groups"))
        return (f"作業の一覧を作った（警告 {record.get('issues', '-')} 件、作業 {record.get('items', '-')} 件"
                + (f"、うちグループ {groups} 件" if groups else "") + "）")
    if event == "copy":
        return (f"{item} の修正用のコピーを" + ("作り始めた" if record.get("create") else "元に戻し始めた")
                + f"（スロット {record.get('slot', '-')}" + ("、初回は時間がかかる" if record.get("create") else "") + "）")
    if event == "next":
        attempt = record.get("attempt") or 1
        return (f"{item} の調査を始めた（スロット {record.get('slot', '-')}"
                + (f"、{attempt} 回目" if isinstance(attempt, int) and attempt > 1 else "") + "）")
    if event == "brief":
        return f"{item} の指示を作った（サブエージェントが調べる）"
    if event == "finish":
        notes = [f"推奨 {PLAN_JA.get(record.get('recommendation'), '-')}",
                 f"確信度 {CONFIDENCE_JA.get(record.get('confidence'), '-')}"]
        if record.get("split"):
            notes.append(f"{_count(record.get('split'))} 件を個別の作業に戻した")
        if record.get("warnings"):
            notes.append(f"注意 {_count(record.get('warnings'))} 件")
        return f"{item} を終えた（{'、'.join(notes)}）"
    if event == "fail":
        return f"{item} を処理できなかった: {record.get('reason', '')}"
    if event == "resume":
        reset = record.get("reset") if isinstance(record.get("reset"), dict) else {}
        back = sum(v for v in reset.values() if isinstance(v, int))
        return f"実行を再開した（戻した作業 {back} 件）"
    if event == "verify-start":
        return f"ビルドでの確かめを始めた（{_count(record.get('items'))} 件）"
    if event == "verify":
        notes = ["ビルド " + ("成功" if record.get("build_ok") else "失敗")]
        if record.get("downgraded"):
            notes.append(f"確信度を下げた {_count(record.get('downgraded'))} 件")
        return f"ビルドでの確かめが終わった（{'、'.join(notes)}）"
    if event == "summary":
        notes = [f"{record.get('items', '-')} 件", f"処理できなかった作業 {record.get('errors', 0)} 件"]
        if record.get("unprocessed"):
            notes.append(f"まだの作業 {_count(record.get('unprocessed'))} 件")
        return f"一覧を作った（{'、'.join(notes)}）"
    if event == "preview":
        counts = record.get("counts") if isinstance(record.get("counts"), dict) else {}
        return "反映の内容を作った（" + "、".join(f"{k} {v}" for k, v in counts.items()) + "）"
    if event == "apply-code":
        errors = _count(record.get("errors"))
        return "反映した" + (f"（うまくいかなかったもの {errors} 件）" if errors else "")
    if event == "add-knowledge":
        return f"知識を {_count(record.get('entries'))} 件書き足した"
    if event == "launch":
        role, rounds = record.get("role"), record.get("round")
        return (f"{_role_name(role)}を起動した（{_task_label(role, record.get('task'), item)}"
                + (f"、{rounds} 回目" if isinstance(rounds, int) and rounds > 1 else "") + "）")
    if event == "role-end":
        role, outcome = record.get("role"), record.get("outcome")
        task = _task_label(role, str(record.get("task") or "").removeprefix("reviewer:"), item)
        if outcome == "failed":
            reason = _short(record.get("reason"), 100)
            return f"{_role_name(role)}がうまくいかなかった（{task}）" + (f": {reason}" if reason else "")
        if role == "reviewer":
            return f"{_role_name(role)}が {item or '-'} を確かめた（{REVIEW_JA.get(str(outcome), outcome or '-')}）"
        if role == "integrator":
            merges = _count(record.get("merges"))
            return f"{_role_name(role)}が終わった（" + (f"{merges} つにまとめた" if merges else "まとめるものはなかった") + "）"
        return f"{_role_name(role)}が終わった（{task}）"
    if event == "initial-start":
        return "初回解析を始めた（ビルド → 解析 → サーバへの登録）"
    if event == "initial":
        if record.get("status") == "done":
            took = _duration(record.get("seconds"))
            return "初回解析が終わった（サーバへの登録まで" + (f"、{took}" if took else "") + "）"
        step = initial.STEPS.get(str(record.get("step")), record.get("step") or "-")
        return f"初回解析に失敗した（{step}）: {record.get('error', '')}"
    if event == "integrate":
        if not record.get("merges") and "candidates" in record:
            return "同じ原因でまとめる候補はなかった（統合しない）"
        notes = [f"{_count(record.get('merges'))} つにまとめた"]
        if record.get("conflicts"):
            notes.append(f"同じ箇所を変える別の原因 {_count(record.get('conflicts'))} 組")
        return f"同じ原因の作業をまとめた（{'、'.join(notes)}）"
    if event == "integrate-undo":
        members = record.get("members") if isinstance(record.get("members"), list) else []
        return (f"統合した {item or '-'} を処理できず、元の作業 {', '.join(map(str, members)) or '-'} に戻した"
                + (f": {_short(record.get('reason'), 100)}" if record.get("reason") else ""))
    if event == "integrate-build":
        return f"統合した {item or '-'} の修正を差分から作れなかった（作業エージェントが作り直す）"
    if event == "verify-failed":
        return "ビルドでの検証ができなかった（確かめずに一覧を作る）" + (
            f": {_short(record.get('reason'), 100)}" if record.get("reason") else "")
    if event == "paths-missing":
        items = record.get("items") if isinstance(record.get("items"), list) else []
        return f"警告経路を取れなかった作業がある（{', '.join(map(str, items)) or '-'}。経路なしで調べる）"
    if event == "error":
        command = record.get("command") or "-"
        if _level(record) == "warning" and command == "finish":
            return f"{item} の結果ファイルに直すところがあった（サブエージェントが直す）"
        return f"{item + ' の ' if item else ''}ct.py {command} が失敗した: {record.get('error', '')}"
    return " ".join(p for p in (event, item) if p)


# ---- the run as a whole ----------------------------------------------------------------------


def _parallel(run: Run) -> int:
    """Number of slots: the settings that ``ct.py next`` uses, else the copy made at new-run."""
    try:
        return run.config.parallel
    except Exception:  # noqa: BLE001 - a broken or moved repository must not stop the monitor
        pass
    data = None
    try:
        data = yaml.safe_load((run.dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        pass
    value = data.get("parallel") if isinstance(data, dict) else None
    return value if isinstance(value, int) and 1 <= value <= 8 else DEFAULT_PARALLEL


def _summary_exists(run: Run) -> bool:
    return any((run.dir / name).is_file() for name in ("summary.html", "summary.md"))


def _applying(records: list[dict[str, Any]], now: float) -> bool:
    """/coverity-apply is under way: preview or update_triage after the last apply-code, lately.

    A preview the person did not agree to is not followed by anything: after STALL_SECONDS the
    run counts as done again (as in the list of runs, which reads the log only when it changed lately)."""
    last = _last(records, "preview", "mcp:update_triage")
    if last <= _last(records, "apply-code"):
        return False
    when = _epoch(records[last].get("time"))
    return when is not None and now - when < STALL_SECONDS


def done_label(run: Run) -> str:
    """Label of a finished run, from run.json alone (the list of runs uses it without reading the log)."""
    applied = any(i.applied for i in run.state.items)
    return "反映まで終わりました" if applied else "一覧ができました（承認を待っています）"


def _missing_paths(run: Run) -> list[str]:
    """Items still to be worked on whose warning path files are not all there yet (as ``ct.py tick`` sees)."""
    missing = []
    for item in run.state.items:
        if item.status != "pending":
            continue
        folder = run.item_dir(item.id)
        if any(not (folder / f"issue-{cid}.json").is_file() for cid in item.cids):
            missing.append(item.id)
    return missing


def _fetching_paths(run: Run) -> bool:
    """The collector agent (or the main agent in its place) fetches the warning paths of every item."""
    state = run.state
    lease = state.roles.get("collector") or {}
    if lease.get("active") and lease.get("task") == "paths":
        return True
    started = any(i.status != "pending" for i in state.items)
    return bool(state.fetch_rounds) and not started and bool(_missing_paths(run))


def _initial_stage(run: Run, first: dict[str, Any]) -> bool:
    """The run waits for the initial analysis (or stopped because it failed), as in ``ct.py tick``."""
    status = first["status"]
    return status in ("running", "failed") or (status == "none" and not (run.dir / "issues.json").is_file())


def _stage(run: Run, records: list[dict[str, Any]], now: float,
           first: dict[str, Any] | None = None) -> tuple[str, str]:
    state = run.state
    counts = state.counts()
    if state.initial and not state.planned:
        first = first or _initial(run, now)
        if first and _initial_stage(run, first):
            return "initial", INITIAL_FAILED if first["status"] == "failed" else STAGES["initial"]
    if not state.planned:
        found = _json(run.dir / "issues.json")
        stage = "plan" if isinstance(found, dict) and found.get("finished_at") else "search"
    elif _fetching_paths(run):
        stage = "paths"
    elif counts["pending"] or counts["in_progress"]:
        stage = "items"
    elif state.integration and not state.integrated:
        stage = "integrate"
    elif state.review and ((state.roles.get("reviewer") or {}).get("active")
                           or any(i.status == "done" and i.review is None for i in state.items)):
        stage = "review"
    elif _last(records, "verify-start") > _last(records, "verify", command="verify") or (
            state.verify_mode != "none" and not state.verified and not _summary_exists(run)
            and (state.integration or state.review or state.initial or bool(state.roles))):
        # the second rule is for runs that ct.py tick leads (they keep whether the verification is done)
        stage = "verify"
    elif not _summary_exists(run):
        stage = "summary"
    elif _applying(records, now):
        stage = "apply"
    else:
        stage = "done"
    return stage, done_label(run) if stage == "done" else STAGES[stage]


def run_stage(run: Run, now: float | None = None) -> tuple[str, str]:
    """The stage of the run and its label, as in ``board()`` (for the list of runs)."""
    return _stage(run, oplog.read(run.dir), time.time() if now is None else now)


def _main(stage: str, counts: dict[str, int], free: int, records: list[dict[str, Any]],
          now: float, roles: list[dict[str, Any]] | None = None,
          first: dict[str, Any] | None = None) -> dict[str, Any]:
    """The main agent (Copilot): what it is doing, and its last step."""
    working = {r["role"]: r for r in roles or [] if r["state"] in ("running", "fallback")}
    waiting = {"collector": "取得エージェントの終わりを待っている", "integrator": "統合エージェントの終わりを待っている",
               "reviewer": "検証エージェントの終わりを待っている"}
    by_stage = {"search": "collector", "paths": "collector", "integrate": "integrator", "review": "reviewer"}
    role = working.get(by_stage.get(stage, ""))
    if stage == "initial":
        label = "止まっている（初回解析に失敗）" if first and first["status"] == "failed" else "初回解析の終わりを待っている"
    elif role is not None and role["state"] == "running":
        label = waiting[role["role"]]
    elif stage == "items":
        if counts["pending"] and free:
            label = "作業を配っている"
        elif counts["pending"]:
            label = "サブエージェントの終わりを待っている"
        else:
            label = "残りの作業の終わりを待っている"
    elif stage == "apply":  # after preview the person is asked to agree; update_triage writes to Coverity
        writing = records[_last(records, "preview", "mcp:update_triage")]["event"] == "mcp:update_triage"
        label = "Coverity に書き込んでいる" if writing else "反映の同意を待っている"
    else:
        label = {"search": "Coverity で警告を検索している", "done": "待機中"}.get(stage, STAGES[stage])
    last = records[-1] if records else None
    if last is None:
        return {"label": label, "last_event": None, "last_time": None, "idle_seconds": None}
    parts = [last["event"], last.get("command") if last["event"] == "error" else last.get("phase"), _item(last)]
    when = _epoch(last.get("time"))
    return {"label": label, "last_event": " ".join(str(p) for p in parts if p),
            "last_time": last.get("time"), "idle_seconds": None if when is None else max(0, int(now - when))}


# ---- the workers -----------------------------------------------------------------------------


def _attempt(records: list[dict[str, Any]], item: ItemState) -> list[dict[str, Any]]:
    """Records of the item's current attempt: from its last ``copy`` or ``next`` on."""
    start = -1
    for i, record in enumerate(records):
        if record["event"] in ("copy", "next") and _item(record) == item.id:
            start = i
    if start >= 0:
        return [r for r in records[start:] if _item(r) == item.id]
    since = _iso(item.started)
    return [r for r in records if _item(r) == item.id and str(r.get("time", "")) >= since]


def _progress(run: Run, item: ItemState) -> tuple[dict[str, Any], float | None]:
    """The worker's milestone of this attempt (an older file is from an earlier attempt)."""
    path = run.item_dir(item.id) / PROGRESS_FILE
    mtime = _mtime(path)
    if mtime is None or (item.started and mtime < item.started - 2):
        return {}, None
    data = _json(path)
    if not isinstance(data, dict) or (data.get("item") not in (None, "") and str(data.get("item")) != item.id):
        return {}, None
    return data, mtime


def _newest_file(folder: Path) -> float | None:
    newest = None
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        mtime = entry.stat().st_mtime
                        newest = mtime if newest is None else max(newest, mtime)
                except OSError:
                    continue
    except OSError:
        return None
    return newest


def _idle_worker(slot: int) -> dict[str, Any]:
    return {"slot": slot, "item": None, "cids": [], "is_group": False, "attempts": 0, "rework": False, "stage": "idle",
            "stage_label": WORKER_STAGES["idle"], "phase": None, "phase_label": "", "phase_index": 0,
            "phase_total": len(PHASES), "note": "", "elapsed_seconds": None, "idle_seconds": None,
            "stalled": False}


def _worker(run: Run, item: ItemState, slot: int, records: list[dict[str, Any]], now: float) -> dict[str, Any]:
    mine = _attempt(records, item)
    folder = run.item_dir(item.id)
    stage, since = "fetch", item.started
    copying = bool(mine) and mine[0]["event"] == "copy" and not any(r["event"] == "next" for r in mine)
    if copying:  # ct.py next is still creating or resetting the work copy
        stage, since = "copy", _epoch(mine[0].get("time")) or since
    for record in mine:
        event = record["event"]
        if event == "mcp:get_issues":
            stage = "brief" if record.get("phase") == "end" and record.get("done") else "fetch"
        elif event == "brief":
            stage = "investigate"
        elif event == "error" and record.get("command") == "finish" and _level(record) == "warning":
            stage = "retry"
        else:
            continue
        since = _epoch(record.get("time")) or since
    if not copying and not any(r["event"] in ("mcp:get_issues", "brief") for r in mine):
        # No log of this attempt (an older run, or the log was lost): judge from the files.
        brief = _mtime(folder / "brief.md")
        if brief is not None and (not item.started or brief >= item.started - 2):
            stage, since = "investigate", brief
    result = _mtime(run.result_file(item.id))
    if stage in ("investigate", "retry") and result is not None and (since is None or result >= since + 1):
        stage = "finishing"  # the worker wrote its result; the main agent checks it with ct.py finish

    progress, progress_time = _progress(run, item)
    if copying:
        progress, progress_time = ({"note": "初めて使うスロットは、リポジトリの大きさにより数分から数十分かかります"}
                                   if mine[0].get("create") else {}), None
    phase = progress.get("phase") if progress.get("phase") in PHASES else None
    seen = [t for t in (item.started, progress_time, _newest_file(folder),
                        *(_epoch(r.get("time")) for r in mine)) if t is not None]
    idle = max(0, int(now - max(seen))) if seen else None
    return {"slot": slot, "item": item.id, "cids": list(item.cids), "is_group": item.is_group,
            "attempts": item.attempts, "rework": item.review == "rework",  # sent back by the review
            "stage": stage, "stage_label": WORKER_STAGES[stage], "phase": phase,
            "phase_label": PHASES.get(phase or "", ""), "phase_index": list(PHASES).index(phase) + 1 if phase else 0,
            "phase_total": len(PHASES), "note": _short(progress.get("note"), 200),
            "elapsed_seconds": max(0, int(now - item.started)) if item.started else None,
            # a copy of a large repository is made by svn / git without any sign of life on the way
            "idle_seconds": idle, "stalled": not copying and idle is not None and idle >= STALL_SECONDS,
            "last_seen": _iso(max(seen)) if seen else None}


def _workers(run: Run, parallel: int, records: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    busy = [i for i in run.state.items if i.status == "in_progress"]
    by_slot: dict[int, ItemState] = {}
    others = []
    for item in busy:
        if item.slot and item.slot >= 1 and item.slot not in by_slot:
            by_slot[item.slot] = item
        else:
            others.append(item)  # not expected; still shown
    slots = max([parallel, *by_slot])
    out = []
    for slot in range(1, slots + 1):
        item = by_slot.get(slot)
        out.append(_idle_worker(slot) if item is None else _worker(run, item, slot, records, now))
    for n, item in enumerate(others, slots + 1):
        out.append(_worker(run, item, n, records, now))
    return out


# ---- the initial analysis and the other agents ---------------------------------------------------


def _initial(run: Run, now: float) -> dict[str, Any] | None:
    """The initial analysis (option verify.initial): its step, its time and whether its log still grows.

    The build may print nothing for a long time, so a stop is suspected after 30 minutes, not 10."""
    try:
        status = initial.status(run)
    except Exception:  # noqa: BLE001 - a broken status file must not stop the monitor
        status = {"status": "none"}
    st = status.get("status") if status.get("status") in INITIAL_JA else "none"
    if not run.state.initial and st == "none":
        return None
    steps = list(initial.STEPS)
    step = str(status.get("step") or "setup")
    started, finished = _num(status.get("started")), _num(status.get("finished"))
    log = initial.folder(run) / initial.LOG_FILE
    log_time = _mtime(log)
    seen = [t for t in (started, log_time) if t is not None]
    idle = max(0, int(now - max(seen))) if seen and st == "running" else None
    # a process that is gone wrote no end: its time ends with the last line of its log
    end = (finished or log_time or started or now) if st == "failed" else (finished or now)
    out = {"status": st, "status_label": INITIAL_JA[st], "step": None, "step_label": "", "step_index": 0,
           "step_total": len(steps), "elapsed_seconds": None, "idle_seconds": idle,
           "stalled": idle is not None and idle >= INITIAL_STALL_SECONDS, "error": "", "log": None,
           "last_seen": _iso(max(seen)) if seen else None, "log_tail": "", "started_at": _iso(started),
           "ended_at": _iso(end) if st == "failed" else ""}
    if st == "none":
        return out
    out.update({"step": step, "step_label": initial.STEPS.get(step, step),
                "step_index": len(steps) if st == "done" else steps.index(step) + 1 if step in steps else 0,
                "elapsed_seconds": max(0, int(end - started)) if started is not None else None,
                "log": str(log) if log_time is not None else None})
    if st == "failed":
        out.update({"error": str(status.get("error") or ""), "log_tail": str(status.get("log_tail") or "")})
    return out


def _role_progress(run: Run, role: str, lease: dict[str, Any], item: str | None) -> tuple[dict[str, Any], float | None]:
    """The milestone the integrator or the reviewer wrote for its current task (not one of an earlier task)."""
    path = run.dir / "agents" / role / PROGRESS_FILE
    mtime = _mtime(path)
    started = _num(lease.get("started"))
    if role not in ROLE_PHASES or mtime is None or (started and mtime < started - 2):
        return {}, None
    data = _json(path)
    if not isinstance(data, dict):
        return {}, None
    if role == "reviewer" and item and data.get("item") not in (None, "") and str(data.get("item")) != item:
        return {}, None
    return data, mtime


def _role(run: Run, role: str, records: list[dict[str, Any]], now: float, stage: str) -> dict[str, Any]:
    """One of the agents that work one at a time: the collector, the integrator or the reviewer."""
    state = run.state
    lease = state.roles.get(role) or {}
    active = bool(lease.get("active"))
    task = str(lease.get("task") or "")
    item = str(lease["item"]) if role == "reviewer" and lease.get("item") else None
    started, ended = _num(lease.get("started")), _num(lease.get("ended"))
    since = _iso(started) if started else ""
    mine = [r for r in records if str(r.get("time", "")) >= since and (
        r.get("role") == role or (role == "collector" and r["event"] in ("mcp:search_issues", "mcp:get_issues")))]
    ends = [r for r in records if r["event"] == "role-end" and r.get("role") == role]
    outcome = ends[-1].get("outcome") if ends and not active else None

    if active:
        st = "running"
    elif role == "collector":  # when it could not be used, the main agent calls the MCP tools itself
        st = ("fallback" if lease.get("fallback") and stage in ("search", "paths")
              else "waiting" if stage in ("initial", "search", "plan", "paths") else "done")
    elif role == "integrator":
        st = ("failed" if outcome == "failed" else "done") if state.integrated else "waiting"
    else:
        st = "done" if state.planned and stage in ("verify", "summary", "apply", "done") else "waiting"

    done = total = None
    target, detail = "", ""
    if role == "collector" and task == "paths":
        listed = [i for i in state.items if i.status != "merged"]
        total = len(listed)
        done = total - len(_missing_paths(run))
        target, detail = f"経路 {done}/{total}", f"警告経路 {done}/{total} 件"
    elif role == "collector":
        found = next((r for r in reversed(mine) if r["event"] == "mcp:search_issues" and r.get("phase") == "end"), None)
        target = f"検索 {_count(found.get('found'))}件" if found else "警告の検索"
        detail = (f"見つけた警告 {_count(found.get('found'))} 件" + ("" if found.get("done") else "（続きあり）")
                  if found else "")
    elif role == "integrator" and st in ("done", "failed"):
        made = next((r for r in reversed(records) if r["event"] == "integrate"), None)
        merges = _count(made.get("merges")) if made else 0
        if st == "failed":
            target, detail = "統合できず", _short(ends[-1].get("reason"), 200) if ends else ""
        else:
            target = f"統合 {merges} 件" if merges else "統合なし"
            detail = f"{merges} つにまとめた" if merges else "まとめるものはなかった"
    elif role == "integrator":
        found = _json(run.dir / "integration" / "candidates.json")
        if isinstance(found, list) and active:
            target, detail = f"候補 {len(found)} 組", f"まとめる候補 {len(found)} 組"
    else:
        work = [i for i in state.items if i.status in ("pending", "in_progress", "done")]
        done, total = sum(1 for i in work if i.status == "done" and i.review in ("pass", "flag", "unverified")), len(work)
        current = next((i for i in state.items if i.id == item), None) if active else None
        target = (f"{current.id} ({len(current.cids)}件)" if current.is_group else f"CID {current.id}") if current \
            else f"済 {done}/{total}" if total else ""
        detail = f"検証 {done}/{total} 件" if total else ""

    progress, progress_time = _role_progress(run, role, lease, item) if active else ({}, None)
    phases = ROLE_PHASES.get(role, {})
    phase = progress.get("phase") if progress.get("phase") in phases else None
    index = list(phases).index(phase) + 1 if phase else 0
    if st == "done":
        bar = 1.0
    elif role == "integrator":
        bar = index / len(phases)
    else:
        bar = done / total if total else 0.0

    working = st in ("running", "fallback")
    signs = [t for t in (started, ended if st == "fallback" else None, progress_time) if t is not None]
    output = (_mtime(run.dir / "integration" / "plan.json") if role == "integrator"
              else _mtime(run.item_dir(item) / "review.json") if role == "reviewer" and item else None)
    if output is not None and started and output >= started - 2:
        signs.append(output)
    signs += [t for t in (_epoch(r.get("time")) for r in mine) if t is not None]
    idle = max(0, int(now - max(signs))) if working and signs else None
    begin = ended if st == "fallback" else started
    return {"role": role, "label": ROLES[role], "active": active, "state": st, "state_label": ROLE_STATES[st],
            "task": task or None, "task_label": _task_label(role, task, item) if task else "", "item": item,
            "target": target, "detail": detail, "phase": phase, "phase_label": phases.get(phase or "", ""),
            "phase_index": index, "phase_total": len(phases), "bar": round(min(1.0, max(0.0, bar)), 3),
            "done": done, "total": total, "note": _short(progress.get("note"), 200),
            "rounds": lease.get("rounds") if isinstance(lease.get("rounds"), int) else 0,
            "fallback": bool(lease.get("fallback")), "outcome": outcome,
            "elapsed_seconds": max(0, int(now - begin)) if working and begin else None,
            "idle_seconds": idle, "stalled": idle is not None and idle >= STALL_SECONDS,
            "last_seen": _iso(max(signs)) if signs else None}


def _roles(run: Run, records: list[dict[str, Any]], now: float, stage: str) -> list[dict[str, Any]]:
    """The collector (once it was started), and the integrator and the reviewer when their option is on."""
    state = run.state
    used = {"collector": "collector" in state.roles,
            "integrator": state.integration or "integrator" in state.roles,
            "reviewer": state.review or "reviewer" in state.roles}
    return [_role(run, role, records, now, stage) for role in ROLES if used[role]]


# ---- alerts ----------------------------------------------------------------------------------


def _alert(time_text: Any, level: str, source: str, item: str | None, title: str, detail: str,
           role: str | None = None) -> dict[str, Any]:
    """``role`` tells apart the stops of the agents and of the initial analysis (a stall has no fixed time)."""
    return {"time": str(time_text or ""), "level": level, "source": source, "item": item,
            "title": _short(title), "detail": detail, "role": role}


def _log_alert(record: dict[str, Any]) -> dict[str, Any]:
    event, item, level = record["event"], _item(record), _level(record)
    tool = event[4:] if event.startswith("mcp:") else None
    title = _text(record)
    detail = _detail(record.get("error") or record.get("reason"), record.get("problems"))
    if event == "fail":
        title = f"{item} を処理できませんでした"
    elif event == "error":
        command = record.get("command") or "-"
        title = (f"{item} の結果ファイルに直すところがありました（サブエージェントに直してもらいます）"
                 if level == "warning" and command == "finish" else
                 f"{item + ' の ' if item else ''}ct.py {command} が失敗しました")
    elif tool and record.get("phase") == "error":
        title = f"MCP の {tool} が失敗しました" + (f"（{item}）" if item else "")
    elif tool == "search_issues" and record.get("revision_error"):
        title = "解析したリビジョンを Coverity から取れませんでした（手元のコードで調べます）"
        detail = _detail(record.get("revision_error"))
    elif tool == "get_issues" and record.get("failed"):
        title = f"{item or ''} の警告経路のうち {_count(record.get('failed'))} 件を取れませんでした"
        detail = _detail(record.get("failed"))
    elif tool == "update_triage" and record.get("failed"):
        title = f"Coverity への書き込みが {_count(record.get('failed'))} 件うまくいきませんでした"
        detail = _detail(record.get("failed"))
    return _alert(record.get("time"), level, _source(record), item, title, detail)


def _issue_alerts(run: Run) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Warning paths that Coverity did not return (the item goes on with what it has), with their CIDs and items."""
    alerts, cids, items = [], set(), set()
    for path in sorted(run.dir.glob("items/*/issue-*.json")):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b'"error"' not in raw:  # most files hold a warning path; parse only the few that may not
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if not isinstance(data, dict) or not data.get("error"):
            continue
        cid = str(data.get("cid") or path.stem.removeprefix("issue-"))
        cids.add(cid)
        items.add(path.parent.name)
        alerts.append(_alert(data.get("fetched_at") or _iso(_mtime(path)), "warning", "mcp", path.parent.name,
                             f"CID {cid} の警告経路を Coverity から取れませんでした（{path.parent.name}）",
                             _detail(data.get("error"))))
    return alerts, cids, items


def _apply_alerts(run: Run) -> tuple[list[dict[str, Any]], set[str]]:
    """Deviations that Coverity refused to write (they stay until a new preview writes them)."""
    data = _json(run.dir / "apply-plan.result.json")
    failed = data.get("failed") if isinstance(data, dict) else None
    alerts, ids = [], set()
    for entry in (failed.values() if isinstance(failed, dict) else []):
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("id") or "")
        ids.add(item)
        cids = ", ".join(str(c) for c in entry.get("cids") or [])
        alerts.append(_alert(entry.get("at"), "critical", "apply", item or None,
                             f"{item} の逸脱を Coverity に書き込めませんでした",
                             _detail(entry.get("error"), f"CID {cids}" if cids else "")))
    return alerts, ids


def _verify_alerts(run: Run) -> list[dict[str, Any]]:
    path = run.dir / "verify.json"
    data = _json(path)
    if not isinstance(data, dict):
        return []
    when = _iso(_mtime(path))
    return [_alert(when, "warning", "verify", str(item_id), f"{item_id} の修正案は、ビルドでの確かめで問題がありました",
                   _detail(entry.get("problems"), entry.get("log_tail")))
            for item_id, entry in data.items() if isinstance(entry, dict) and entry.get("build_ok") is False]


def _initial_alerts(first: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The initial analysis failed: the run stopped before the triage (the log says the same once)."""
    if not first or first["status"] != "failed":
        return []
    tail = first["log_tail"].strip()
    return [_alert(first["ended_at"], "critical", "initial", None,
                   f"初回解析に失敗しました（{first['step_label'] or '-'}）。トリアージは始めずに止めています",
                   _detail(first["error"], f"ログ: {first['log']}" if first["log"] else "",
                           "設定を直してから /coverity-run で再開すると、初回解析からやり直します。",
                           f"ログの末尾:\n{tail}" if tail else ""))]


def _review_alerts(run: Run, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Results the reviewer could not check: they are shown with the worker's confidence."""
    ends = {}
    for record in records:
        if record["event"] == "role-end" and record.get("role") == "reviewer" and record.get("outcome") == "failed":
            ends[str(record.get("item") or str(record.get("task") or "").removeprefix("reviewer:"))] = record
    alerts = []
    for item in run.state.items:
        if item.review != "unverified" or item.status == "merged":
            continue
        record = ends.get(item.id) or {}
        alerts.append(_alert(record.get("time") or run.state.created_at, "warning", "review", item.id,
                             f"{item.id} は検証できませんでした（作業エージェントの結果のまま出しています）",
                             _detail(record.get("reason"), "確信度は検証の前のままです。人が確かめてください。")))
    return alerts


def _note_alerts(run: Run, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Notes of the run, such as an integration that could not be done (the items went on unmerged)."""
    ended = next((r for r in reversed(records) if r["event"] == "role-end" and r.get("role") == "integrator"
                  and r.get("outcome") == "failed"), {})
    alerts = []
    for note in run.state.notes:
        merged = "統合" in note
        alerts.append(_alert((ended.get("time") if merged else None) or run.state.created_at, "warning",
                             "integrate" if merged else "ct", None, note, note if len(note) > TEXT_CHARS else ""))
    return alerts


def _role_stall_alert(role: dict[str, Any], now: float) -> dict[str, Any]:
    minutes = (role["idle_seconds"] or 0) // 60
    who = (f"{role['label']}エージェント" if role["state"] == "running"
           else f"{role['label']}の代わりをしているメインの AI")
    where = "、".join(p for p in (role["task_label"], role["phase_label"]) if p)
    return _alert(_iso(now), "warning", "stall", role["item"],
                  f"{who}が {minutes} 分動いていません（止まっているかもしれません）",
                  _detail(where, role["detail"], role["note"], f"最後に動きがあった時刻: {role['last_seen'] or '-'}"),
                  role["role"])


def _initial_stall_alert(first: dict[str, Any], now: float) -> dict[str, Any]:
    minutes = (first["idle_seconds"] or 0) // 60
    return _alert(_iso(now), "warning", "stall", None,
                  f"初回解析のログが {minutes} 分増えていません（止まっているかもしれません）",
                  _detail(f"いまの段: {first['step_label'] or '-'}",
                          "ビルドは 10 分ほど何も出さないことがあります。30 分を超えて増えないときは、ログを確かめてください。",
                          f"ログ: {first['log']}" if first["log"] else "",
                          f"最後にログが増えた時刻: {first['last_seen'] or '-'}"), "initial")


def _stall_alert(worker: dict[str, Any], now: float) -> dict[str, Any]:
    minutes = (worker["idle_seconds"] or 0) // 60
    where = "、".join(p for p in (worker["stage_label"], worker["phase_label"]) if p)
    return _alert(_iso(now), "warning", "stall", worker["item"],
                  f"{worker['item']} が {minutes} 分動いていません（止まっているかもしれません）",
                  _detail(f"スロット {worker['slot']}: {where}", worker["note"],
                          f"最後に動きがあった時刻: {worker['last_seen'] or '-'}"))


def _alerts(run: Run, records: list[dict[str, Any]], workers: list[dict[str, Any]], now: float,
            roles: list[dict[str, Any]] | None = None, first: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    issue_alerts, issue_cids, issue_items = _issue_alerts(run)
    apply_alerts, apply_ids = _apply_alerts(run)
    initial_alerts = _initial_alerts(first)
    found: list[dict[str, Any]] = []
    messages: dict[str, tuple[int, dict[str, Any]]] = {}
    for record in records:
        if _level(record) == "info":
            continue
        failed = record.get("failed")
        if isinstance(failed, list) and failed and (
                (record["event"] == "mcp:get_issues" and {str(c) for c in failed} <= issue_cids)
                or (record["event"] == "mcp:update_triage" and {str(c) for c in failed} <= apply_ids)):
            continue  # the files below say the same per CID / item
        items = record.get("items")
        if record["event"] == "paths-missing" and isinstance(items, list) and {str(i) for i in items} <= issue_items:
            continue  # one alert per CID whose warning path could not be fetched
        if record["event"] == "initial" and initial_alerts and str(record.get("time", "")) >= first["started_at"]:
            continue  # the failure of the initial analysis now, with the end of its log
        message = record.get("reason") or record.get("error")
        if isinstance(message, str) and message:
            messages[message] = (len(found), record)
        found.append(_log_alert(record))
    for item in run.state.items:
        if item.status != "error":
            continue
        if item.error in messages:  # already in the log (ct.py fail, or the error of ct.py next)
            index, record = messages[item.error]
            if _item(record) is None:
                found[index] = _log_alert({**record, "item": item.id})
            continue
        ended = item.started + item.seconds if item.started and item.seconds is not None else None
        found.append(_alert(_iso(ended), "critical", "ct", item.id, f"{item.id} を処理できませんでした",
                            _detail(item.error)))
    found += issue_alerts + apply_alerts + _verify_alerts(run) + initial_alerts
    found += _review_alerts(run, records) + _note_alerts(run, records)
    found += [_stall_alert(w, now) for w in workers if w["stalled"]]
    found += [_role_stall_alert(r, now) for r in roles or [] if r["stalled"]]
    if first and first["stalled"]:
        found.append(_initial_stall_alert(first, now))
    found.reverse()  # among equal times, the later one first
    found.sort(key=lambda a: a["time"], reverse=True)
    return found[:MAX_ALERTS]


# ---- the board -------------------------------------------------------------------------------


def board(run: Run, now: float | None = None) -> dict[str, Any]:
    """State of the run for the monitor: the main agent, the initial analysis, one slot per worker, the
    collector / integrator / reviewer that the run uses, recent steps and alerts."""
    now = time.time() if now is None else now
    records = oplog.read(run.dir)
    state = run.state
    counts = state.counts()
    parallel = _parallel(run)
    first = _initial(run, now)
    stage, stage_label = _stage(run, records, now, first)
    workers = _workers(run, parallel, records, now)
    roles = _roles(run, records, now, stage)
    free = sum(1 for w in workers[:parallel] if w["item"] is None)
    recent = [{"time": r.get("time"), "level": _level(r), "source": _source(r), "event": r["event"],
               "item": _item(r), "text": _text(r)} for r in reversed(records[-RECENT:])]
    alerts = _alerts(run, records, workers, now, roles, first)
    for box in [*workers, *roles, *([first] if first else [])]:
        box.pop("last_seen", None)
    if first:
        for key in ("log_tail", "started_at", "ended_at"):
            first.pop(key, None)
    return {"run_id": state.run_id, "created_at": state.created_at, "filter_name": state.filter_name,
            "stage": stage, "stage_label": stage_label,
            "main": _main(stage, counts, free, records, now, roles, first),
            # an item the integration took into another one is not counted (it is shown inside that one)
            "counts": counts, "total": len(state.items) - counts.get("merged", 0), "parallel": parallel,
            "workers": workers, "initial": first, "roles": roles, "recent": recent, "alerts": alerts}


def recent_lines(run: Run, limit: int = STATUS_RECENT) -> list[str]:
    """The last steps of the run as lines "HH:MM:SS text", newest first (``ct.py status``), as the monitor's
    recent steps (``board()["recent"]``): the same log, the same text, the same order."""
    lines = []
    records = oplog.read(run.dir)[-limit:] if limit > 0 else []
    for record in reversed(records):
        clock = str(record.get("time") or "")[11:19]
        lines.append(f"{clock if CLOCK.fullmatch(clock) else '--:--:--'} {_text(record)}")
    return lines


def alerts(run: Run, now: float | None = None) -> list[dict[str, Any]]:
    """Serious errors and warnings of the run, newest first (the same as ``board()["alerts"]``)."""
    return board(run, now)["alerts"]
