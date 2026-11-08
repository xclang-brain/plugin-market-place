"""What the AI of a run is doing, for the 監視 tab. Read only, from the files of the run folder:

- ``run.json``: the items (``status``; ``started``, ``attempts``, ``rework_rounds``, ``slot``, ``error``
  when present) and whether the review / integration are used;
- ``items/<ID>/progress.json`` (``phase``, ``note``; the time is the file's), ``brief.md`` (when the
  current attempt started), ``result.previous.json`` (the item is being redone after the review);
- ``agents/reviewer/<ID>.md`` / ``agents/integrator/brief.md`` and their progress, ``review.json``,
  ``integration/plan.json``;
- ``operations.log`` (JSON Lines: ``time``, ``event``, ``level``, ``item``, ``message``);
- ``analysis/status.json`` and ``analysis/initial.log`` (initial analysis), ``jobs/*.json`` (detached
  jobs: a running job of an item that prepares its work copy), ``items/*/issue-*.json`` with ``error``,
  ``verify.json`` and ``apply-plan.result.json`` (problems kept in other files).

An item, or an agent, with no sign of work for 10 minutes may have stopped (「⚠ 停止?」); the initial
analysis gets 30 minutes (a build may print nothing for a long time). An item whose work copy is being
prepared is never shown as stopped.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .common import epoch, mtime, read_text, try_json
from .runs import Run, review_outcome, stage

STALL_SECONDS = 600
INITIAL_STALL_SECONDS = 1800
RECENT = 40
MAX_ALERTS = 100
TEXT_CHARS = 160
DETAIL_CHARS = 4000
LOG_BYTES = 4 * 1024 * 1024  # the end of a long log is enough for the screen
LEVELS = ("info", "warning", "critical")
PHASES = {"read": "コードを読む", "judge": "真偽を判断", "fix": "修正を作る", "deviation": "逸脱を書く", "result": "結果を書く"}
ROLE_PHASES = {
    "reviewer": {"read": "結果と差分を読む", "check": "評価基準で確かめる", "write": "確かめの結果を書く"},
    "integrator": {"read": "結果と差分を読む", "compare": "同じ原因かを比べる", "write": "まとめ方を書く"},
}
ROLE_JA = {"worker": "作業役", "reviewer": "検証役", "integrator": "統合役"}
WORKER_STAGES = {"idle": "空き", "copy": "修正用のコピーを用意中", "investigate": "調査中", "retry": "確かめの指摘を直している",
                 "finishing": "結果を保存している"}
SCRIPT_STAGES = ("copy", "save", "merge")  # an item's stage in run.json while a script (not an agent) works on it
STATUS_JA = {"pending": "待ち", "in_progress": "作業中", "done": "終わり", "error": "エラー", "merged": "統合でまとめた"}
REVIEW_JA = {"pass": "合格", "flag": "要確認", "rework": "差し戻し", "unverified": "未検証"}
INITIAL_STEPS = {"setup": "準備", "build": "ビルド", "analyze": "解析", "commit": "サーバへの登録"}
INITIAL_JA = {"none": "待ち", "running": "実行中", "done": "登録まで終わった", "failed": "失敗した"}
COPY_WORDS = ("copy", "prepare", "worktree", "checkout", "work")


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


def _iso(seconds: float | None) -> str:
    if seconds is None:
        return ""
    try:
        return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _since(now: float, then: float | None) -> int | None:
    return None if then is None else max(0, int(now - then))


# ---- operations.log --------------------------------------------------------------------------


def read_log(folder: Path) -> list[dict[str, Any]]:
    """The records of operations.log, oldest first (a line that is not a JSON object is skipped)."""
    path = folder / "operations.log"
    try:
        with path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - LOG_BYTES))
            data = f.read()
    except OSError:
        return []
    if len(data) == LOG_BYTES:
        data = data.split(b"\n", 1)[-1]  # the first line may be cut
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            record["event"] = str(record.get("event") or "")
            out.append(record)
    return out


def _level(record: dict[str, Any]) -> str:
    level = record.get("level")
    return level if level in LEVELS else "info"


def _item_of(record: dict[str, Any]) -> str | None:
    value = record.get("item")
    return None if value in (None, "") else str(value)


def _text(record: dict[str, Any]) -> str:
    message = record.get("message")
    if isinstance(message, str) and message.strip():
        return _short(message)
    return _short(" ".join(p for p in (record.get("event"), _item_of(record)) if p)) or "（記録）"


# ---- pieces of the run ---------------------------------------------------------------------


def _jobs(run: Run) -> list[dict[str, Any]]:
    out = []
    for path in sorted((run.dir / "jobs").glob("*.json")):
        data = try_json(path)
        if isinstance(data, dict):
            out.append({"name": path.stem, **data})
    return out


def _job_status(job: dict[str, Any]) -> str:
    return str(job.get("status") or job.get("state") or "")


def _job_item(job: dict[str, Any]) -> str:
    args = job.get("args") if isinstance(job.get("args"), dict) else {}
    return str(job.get("item") or args.get("item") or "")


def _preparing_copy(item: dict[str, Any], jobs: list[dict[str, Any]]) -> bool:
    """The run tool is preparing the item's work copy (its stage in run.json, or a running copy job)."""
    if item.get("preparing_copy") is True or item.get("stage") == "copy":
        return True
    for job in jobs:
        if _job_status(job) not in ("running", "started", "active"):
            continue
        words = f"{job.get('kind') or ''} {job.get('name') or ''}".lower()
        mine = _job_item(job) == item["id"] or item["id"].lower() in str(job.get("name") or "").lower()
        if mine and any(w in words for w in COPY_WORDS):
            return True
    return False


def _span(folder: Path) -> tuple[float | None, float | None]:
    """The oldest and the newest change of the files in a folder."""
    times = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        times.append(entry.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        return None, None
    return (min(times), max(times)) if times else (None, None)


def _progress(path: Path, since: float | None, item_id: str | None = None) -> tuple[dict[str, Any], float | None]:
    """A progress.json of the current attempt (an older file is from an earlier attempt)."""
    when = mtime(path)
    if when is None or (since is not None and when < since - 2):
        return {}, None
    data = try_json(path)
    if not isinstance(data, dict) or (item_id and data.get("item") not in (None, "") and str(data.get("item")) != item_id):
        return {}, None
    return data, when


def _item_state(run: Run, item: dict[str, Any], records: list[dict[str, Any]], jobs: list[dict[str, Any]],
                now: float) -> dict[str, Any]:
    folder = run.item_dir(item["id"])
    mine = [r for r in records if _item_of(r) == item["id"]]
    oldest, newest = _span(folder)
    first_log = epoch(mine[0].get("time")) if mine else None
    started = epoch(item.get("started")) or mtime(folder / "brief.md") or first_log or oldest
    last_log = epoch(mine[-1].get("time")) if mine else None
    progress, _ = _progress(folder / "progress.json", started, item["id"])
    phase = progress.get("phase") if progress.get("phase") in PHASES else None
    seen = [t for t in (newest, last_log, started) if t]
    last = max(seen) if seen else None
    working = item["status"] == "in_progress"
    copying = working and _preparing_copy(item, jobs)
    scripted = working and (copying or item.get("stage") in SCRIPT_STAGES)
    idle = _since(now, last)
    rework = (folder / "result.previous.json").is_file()
    rounds = item.get("rework_rounds", item.get("review_rounds"))
    attempts = item.get("attempts")
    return {
        "id": item["id"], "cids": item["cids"], "is_group": item["id"].startswith("G"),
        "status": item["status"], "status_label": STATUS_JA[item["status"]],
        "phase": phase, "phase_label": PHASES.get(phase or "", ""),
        "phase_index": list(PHASES).index(phase) + 1 if phase else 0, "phase_total": len(PHASES),
        "note": _short(progress.get("note"), 120) if working else "",
        "elapsed_seconds": _since(now, started) if working else None, "idle_seconds": idle if working else None,
        "stalled": bool(working and not scripted and idle is not None and idle >= STALL_SECONDS),
        "preparing_copy": copying, "finishing": working and item.get("stage") in ("save", "merge"),
        "rework": working and rework,
        "attempts": attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 1,
        "rework_rounds": rounds if isinstance(rounds, int) and not isinstance(rounds, bool) else 0,
        "review": review_outcome(run, item) if item["status"] == "done" else None,
        "review_label": REVIEW_JA.get(review_outcome(run, item) or "", "") if item["status"] == "done" else "",
        "error": _short(item.get("error"), 300) if item["status"] == "error" else None,
        "slot": item.get("slot") if isinstance(item.get("slot"), int) else None,
        "started": started,
    }


def _workers(states: list[dict[str, Any]], parallel: int) -> list[dict[str, Any]]:
    busy = sorted((s for s in states if s["status"] == "in_progress"), key=lambda s: (s["started"] or 0, s["id"]))
    slots: dict[int, dict[str, Any]] = {}
    for s in busy:
        if s["slot"] and 1 <= s["slot"] <= max(parallel, len(busy)) and s["slot"] not in slots:
            slots[s["slot"]] = s
    free = [n for n in range(1, max(parallel, len(busy)) + 1) if n not in slots]
    for s in busy:
        if s not in slots.values():
            slots[free.pop(0)] = s
    out = []
    for slot in range(1, max(parallel, len(busy)) + 1):
        s = slots.get(slot)
        if s is None:
            out.append({"slot": slot, "item": None, "cids": [], "is_group": False, "attempts": 0, "rework": False,
                        "stage": "idle", "stage_label": WORKER_STAGES["idle"], "phase": None, "phase_label": "",
                        "phase_index": 0, "phase_total": len(PHASES), "note": "", "elapsed_seconds": None,
                        "idle_seconds": None, "stalled": False})
            continue
        key = "copy" if s["preparing_copy"] else "finishing" if s["finishing"] else "retry" if s["rework"] else "investigate"
        out.append({"slot": slot, "item": s["id"], "cids": s["cids"], "is_group": s["is_group"],
                    "attempts": s["attempts"], "rework": s["rework"], "stage": key, "stage_label": WORKER_STAGES[key],
                    "phase": s["phase"], "phase_label": s["phase_label"], "phase_index": s["phase_index"],
                    "phase_total": s["phase_total"], "note": s["note"], "elapsed_seconds": s["elapsed_seconds"],
                    "idle_seconds": s["idle_seconds"], "stalled": s["stalled"]})
    return out


def _good(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 8


def _parallel(run: Run, repo: Path | None) -> int:
    """Number of worker slots: run.json, else the copy of the settings made at the start, else the settings."""
    if _good(run.data.get("parallel")):
        return run.data["parallel"]
    files = [run.dir / "config.snapshot.yaml"] + ([repo / ".coverity-triage" / "config.yaml"] if repo else [])
    for path in files:
        try:
            data = yaml.safe_load(read_text(path))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and _good(data.get("parallel")):
            return data["parallel"]
    return 3


def _initial(run: Run, now: float) -> dict[str, Any] | None:
    status = try_json(run.dir / "analysis" / "status.json")
    if not isinstance(status, dict):
        if run.data.get("initial") and not run.items:
            return {"status": "none", "status_label": INITIAL_JA["none"], "step": None, "step_label": "",
                    "step_index": 0, "step_total": len(INITIAL_STEPS), "elapsed_seconds": None,
                    "idle_seconds": None, "stalled": False, "error": ""}
        return None
    state = status.get("status") if status.get("status") in ("running", "done", "failed") else "running"
    step = status.get("step") if status.get("step") in INITIAL_STEPS else None
    started = epoch(status.get("started"))
    finished = epoch(status.get("finished"))
    activity = max((t for t in (mtime(run.dir / "analysis" / "initial.log"), mtime(run.dir / "analysis" / "status.json")) if t),
                   default=None)
    idle = _since(now, activity)
    return {"status": state, "status_label": INITIAL_JA[state], "step": step, "step_label": INITIAL_STEPS.get(step or "", ""),
            "step_index": list(INITIAL_STEPS).index(step) + 1 if step else 0, "step_total": len(INITIAL_STEPS),
            "elapsed_seconds": _since(finished if finished and state != "running" else now, started),
            "idle_seconds": idle if state == "running" else None,
            "stalled": bool(state == "running" and idle is not None and idle >= INITIAL_STALL_SECONDS),
            "error": _short(status.get("error"), 500) if state == "failed" else ""}


def _role(run: Run, role: str, now: float, states: list[dict[str, Any]]) -> dict[str, Any]:
    """The reviewer or the integrator: one at a time, seen from its instruction file and its output."""
    folder = run.dir / "agents" / role
    out: dict[str, Any] = {"role": role, "label": ROLE_JA[role], "state": "waiting", "target": "", "task_label": "",
                           "phase": None, "phase_label": "", "phase_index": 0, "phase_total": len(ROLE_PHASES[role]),
                           "note": "", "detail": "", "rounds": 1, "elapsed_seconds": None, "idle_seconds": None,
                           "stalled": False, "bar": 0}
    briefs = []
    if role == "reviewer":
        for path in folder.glob("*.md"):
            item_id = path.stem
            review = run.item_dir(item_id) / "review.json"
            briefs.append((mtime(path) or 0, item_id, path, review))
    else:
        path = folder / "brief.md"
        if path.is_file():
            briefs.append((mtime(path) or 0, None, path, run.dir / "integration" / "plan.json"))
    if not briefs:
        return out
    briefs.sort(key=lambda b: b[0])
    settled = {item["id"] for item in run.items if (item.get("review").get("outcome") if isinstance(item.get("review"), dict)
                                                   else item.get("review")) in ("pass", "flag", "rework", "unverified")}
    merged = mtime(run.dir / "integration" / "merged.json") or 0

    def open_brief(brief: tuple) -> bool:
        written, item_id, _, output = brief
        if role == "reviewer":
            return (mtime(output) or 0) < written and item_id not in settled
        return (mtime(output) or 0) < written and merged < written and not run.data.get("integrated")

    pending = [b for b in briefs if open_brief(b)]
    finished = not any(s["status"] in ("pending", "in_progress") for s in states) and run.summary_made
    if not pending or finished or run.data.get("stopped"):
        out.update({"state": "done", "bar": 1,
                    "target": briefs[-1][1] or "", "task_label": "終わった"})
        return out
    started, item_id, path, _ = pending[-1]
    candidates = [folder / f"{item_id}.progress.json", folder / "progress.json"] if item_id else [folder / "progress.json"]
    progress, at = {}, None
    for candidate in candidates:
        progress, at = _progress(candidate, started, item_id)
        if progress:
            break
    phase = progress.get("phase") if progress.get("phase") in ROLE_PHASES[role] else None
    last = max((t for t in (at, started) if t), default=None)
    idle = _since(now, last)
    phases = list(ROLE_PHASES[role])
    out.update({"state": "running", "target": item_id or "まとめる候補",
                "task_label": f"{item_id} の確かめ" if item_id else "同じ原因の作業をまとめる",
                "phase": phase, "phase_label": ROLE_PHASES[role].get(phase or "", ""),
                "phase_index": phases.index(phase) + 1 if phase else 0,
                "note": _short(progress.get("note"), 120), "elapsed_seconds": _since(now, started), "idle_seconds": idle,
                "stalled": idle is not None and idle >= STALL_SECONDS,
                "bar": (phases.index(phase) + 1) / len(phases) if phase else 0,
                "detail": f"ほかに待っている確かめ {len(pending) - 1} 件" if len(pending) > 1 else ""})
    return out


def _alerts(run: Run, records: list[dict[str, Any]], states: list[dict[str, Any]], initial: dict[str, Any] | None,
            roles: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in records:
        level = _level(r)
        if level == "info":
            continue
        extra = {k: v for k, v in r.items() if k not in ("time", "level", "event", "item", "message", "source")}
        body = json.dumps(extra, ensure_ascii=False, indent=1) if extra else ""
        out.append({"time": str(r.get("time") or ""), "level": level, "source": "log", "item": _item_of(r),
                    "title": _text(r), "detail": _detail(r.get("event") and f"出来事: {r['event']}", body)})
    for s in states:
        if s["status"] == "error":
            reason = s["error"] or "理由の記録がありません"
            logged = [a for a in out if a["level"] == "critical" and a["item"] == s["id"]]
            if logged:  # the log already says it: the reason goes with it
                logged[-1]["detail"] = _detail(f"理由: {reason}", logged[-1]["detail"])
            else:
                out.append({"time": "", "level": "critical", "source": "item", "item": s["id"],
                            "title": f"{s['id']} を処理できませんでした", "detail": reason})
        if s["stalled"]:
            out.append({"time": _iso(time.time()), "level": "warning", "source": "stall", "item": s["id"],
                        "title": f"{s['id']} が {s['idle_seconds'] // 60} 分動いていません（止まっているかもしれません）",
                        "detail": "作業役のエージェントが止まっていないか、チャットで確認を待っていないか確かめてください。"})
        if s["status"] in ("pending", "in_progress", "done"):
            for path in sorted(run.item_dir(s["id"]).glob("issue-*.json")):
                data = try_json(path)
                if isinstance(data, dict) and data.get("error"):
                    out.append({"time": str(data.get("fetched_at") or ""), "level": "warning", "source": "fetch",
                                "item": s["id"], "title": f"{s['id']} の警告経路を Coverity から取れませんでした",
                                "detail": _detail(f"CID {data.get('cid', '-')}", str(data["error"]))})
    for r in roles:
        if r["stalled"]:
            out.append({"time": _iso(time.time()), "level": "warning", "source": "stall", "item": r["target"] or None,
                        "role": r["role"], "title": f"{r['label']}が {r['idle_seconds'] // 60} 分動いていません（止まっているかもしれません）",
                        "detail": "チャットで確認を待っていないか確かめてください。"})
    if initial and initial["status"] == "failed":
        out.append({"time": "", "level": "critical", "source": "initial", "item": None,
                    "title": "初回解析に失敗しました（警告を調べ始めずに止まっています）",
                    "detail": _detail(initial["step_label"] and f"段: {initial['step_label']}", initial["error"],
                                      "ログ: analysis/initial.log")})
    elif initial and initial["stalled"]:
        out.append({"time": _iso(time.time()), "level": "warning", "source": "stall", "item": None,
                    "title": f"初回解析のログが {initial['idle_seconds'] // 60} 分増えていません（止まっているかもしれません）",
                    "detail": "ビルドが入力を待っていないか、ログ analysis/initial.log を確かめてください。"})
    for job in jobs:
        if _job_status(job) in ("failed", "error"):
            out.append({"time": str(job.get("finished") or job.get("started") or ""), "level": "warning", "source": "job",
                        "item": _job_item(job) or None,
                        "title": f"切り離した処理 {job['name']} がうまくいきませんでした",
                        "detail": _short(job.get("error") or job.get("message"), DETAIL_CHARS)})
    verified = try_json(run.dir / "verify.json")
    if isinstance(verified, dict):
        for item_id, entry in verified.items():
            if isinstance(entry, dict) and (entry.get("build_ok") is False or entry.get("problems")):
                problems = entry.get("problems") if isinstance(entry.get("problems"), list) else []
                out.append({"time": "", "level": "warning", "source": "verify", "item": str(item_id),
                            "title": f"{item_id} の修正案で、ビルドでの検証に問題が出ました（確信度を「低」にしています）",
                            "detail": _detail([str(p) for p in problems], entry.get("log") and f"ログ: {entry['log']}")})
    applied = try_json(run.dir / "apply-plan.result.json")
    failed = applied.get("failed") if isinstance(applied, dict) else None
    if isinstance(failed, dict):
        for entry in failed.values():
            if isinstance(entry, dict):
                out.append({"time": str(entry.get("at") or ""), "level": "warning", "source": "apply",
                            "item": str(entry.get("id")) if entry.get("id") else None,
                            "title": f"{entry.get('id', '-')} の逸脱を Coverity に書き込めませんでした",
                            "detail": _short(entry.get("error"), DETAIL_CHARS)})
    critical = [a for a in out if a["level"] == "critical"]
    warning = [a for a in out if a["level"] != "critical"]
    critical.sort(key=lambda a: a["time"], reverse=True)
    warning.sort(key=lambda a: a["time"], reverse=True)
    return (critical + warning)[:MAX_ALERTS]


def _main(stage_key: str, counts: dict[str, int], free: int, roles: list[dict[str, Any]],
          initial: dict[str, Any] | None, records: list[dict[str, Any]], now: float) -> dict[str, Any]:
    reviewing = any(r["role"] == "reviewer" and r["state"] == "running" for r in roles)
    integrating = any(r["role"] == "integrator" and r["state"] == "running" for r in roles)
    if stage_key == "initial":
        label = "止まっている（初回解析に失敗）" if initial and initial["status"] == "failed" else "初回解析の終わりを待っている"
    elif stage_key == "items":
        if counts["pending"] and free:
            label = "作業を配っている"
        elif counts["pending"]:
            label = "作業役の終わりを待っている"
        else:
            label = "残りの作業の終わりを待っている"
    elif integrating:
        label = "統合役の終わりを待っている"
    elif reviewing:
        label = "検証役の終わりを待っている"
    else:
        label = {"fetch": "Coverity から警告を取っている", "details": "警告経路を取っている",
                 "integrate": "同じ原因の警告をまとめている", "review": "結果の確かめを進めている",
                 "verify": "ビルドでの検証の終わりを待っている", "summary": "まとめと一覧を作っている",
                 "stopped": "止めました", "done": "待機中（終わりました）"}.get(stage_key, "—")
    last = records[-1] if records else None
    when = epoch(last.get("time")) if last else None
    return {"label": label, "last_event": _text(last) if last else None, "last_time": last.get("time") if last else None,
            "idle_seconds": _since(now, when)}


def board(run: Run, repo: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """What the monitor draws for a run."""
    now = time.time() if now is None else now
    stage_key, stage_label = stage(run)
    info = {"run_id": run.run_id, "created_at": run.data.get("created_at") or "", "foreign": run.foreign,
            "filter_name": run.data.get("filter_name") or "", "stage": stage_key, "stage_label": stage_label}
    if run.foreign:
        return {**info, "main": {"label": "—", "last_event": None, "last_time": None, "idle_seconds": None},
                "counts": {}, "total": 0, "parallel": 0, "workers": [], "initial": None, "roles": [], "items": [],
                "recent": [], "alerts": [], "progress": {"value": 0, "max": 0, "label": ""}}
    records = read_log(run.dir)
    jobs = _jobs(run)
    states = [_item_state(run, item, records, jobs, now) for item in run.items]
    parallel = _parallel(run, repo)
    workers = _workers(states, parallel)
    initial = _initial(run, now)
    roles = []
    if run.integration_on:
        roles.append(_role(run, "integrator", now, states))
    if run.review_on:
        roles.append(_role(run, "reviewer", now, states))
    counts = run.counts()
    total = len(run.items) - counts["merged"]
    free = sum(1 for w in workers if not w["item"])
    recent = [{"time": str(r.get("time") or ""), "level": _level(r), "source": "log", "event": r["event"],
               "item": _item_of(r), "text": _text(r)} for r in reversed(records[-RECENT:])]
    items = [{k: v for k, v in s.items() if k not in ("started", "slot", "finishing")} for s in states]
    done = counts["done"] + counts["error"]
    return {**info, "main": _main(stage_key, counts, free, roles, initial, records, now),
            "counts": counts, "total": total, "parallel": parallel, "workers": workers, "initial": initial,
            "roles": roles, "items": items, "recent": recent,
            "alerts": _alerts(run, records, states, initial, roles, jobs),
            "progress": {"value": done, "max": total, "label": f"{done} / {total}"}}
