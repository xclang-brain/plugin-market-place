"""Work that takes long runs as detached processes (``run.py job-run``), recorded in ``jobs/<name>.json``.

A job record: ``name``, ``kind``, ``status`` (running / done / failed / stopped), ``pid``, ``started``
(ISO), ``started_ts``, ``finished``, ``args``, ``result``, ``error``, ``children`` (pids of process
groups the job started, for ``stop``). The tick reads the records and moves the run on; the job never
changes run.json itself.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from .common import (RUN_SCRIPT, RunError, file_lock, kill_how, kill_tree, now_iso, pid_alive, read_json,
                     spawn_detached, write_json)

JOBS_DIR = "jobs"
START_GRACE = 30  # seconds a job may take to write its pid
KINDS: dict[str, Callable[..., dict[str, Any]]] = {}


def register(kind: str) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    def wrap(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        KINDS[kind] = fn
        return fn
    return wrap


def _file(run_dir: Path, name: str) -> Path:
    return Path(run_dir) / JOBS_DIR / f"{name}.json"


def log_file(run_dir: Path, name: str) -> Path:
    return Path(run_dir) / JOBS_DIR / f"{name}.log"


def read(run_dir: Path, name: str) -> dict[str, Any] | None:
    """The job record, with a job whose process is gone shown as failed (nothing is written)."""
    path = _file(run_dir, name)
    try:
        data = read_json(path)
    except RunError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("status") == "running":
        age = time.time() - float(data.get("started_ts") or 0)
        pid = data.get("pid")
        if (pid and not pid_alive(pid) and age > 2) or (not pid and age > START_GRACE):
            fresh = read_json(path, {})  # it may have ended while we looked
            if isinstance(fresh, dict) and fresh.get("status") != "running":
                return fresh
            data = {**data, "status": "failed", "error": "処理のプロセスが途中で止まりました"}
    return data


def update(run_dir: Path, name: str, **fields: Any) -> dict[str, Any]:
    path = _file(run_dir, name)
    with file_lock(path):
        data = read_json(path, {}) if path.is_file() else {}
        data.update(fields)
        write_json(path, data)
    return data


def all_jobs(run_dir: Path) -> list[dict[str, Any]]:
    folder = Path(run_dir) / JOBS_DIR
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        job = read(run_dir, path.stem)
        if job:
            out.append(job)
    return out


def running(run_dir: Path) -> list[dict[str, Any]]:
    return [j for j in all_jobs(run_dir) if j.get("status") == "running"]


def start(run_dir: Path, name: str, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    """Start a job (a running job of the same name is kept)."""
    current = read(run_dir, name)
    if current and current.get("status") == "running":
        return current
    path = _file(run_dir, name)
    log = log_file(run_dir, name)
    log.unlink(missing_ok=True)
    write_json(path, {"name": name, "kind": kind, "status": "running", "pid": None, "started": now_iso(),
                      "started_ts": time.time(), "finished": None, "args": args, "result": None, "error": None,
                      "children": []})
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    pid = spawn_detached([sys.executable, str(RUN_SCRIPT), "job-run", "--run", str(run_dir), "--name", name],
                         Path(run_dir), log, env)
    with file_lock(path):
        data = read_json(path, {})
        if data.get("status") == "running" and not data.get("pid"):
            data["pid"] = pid
            write_json(path, data)
    return read(run_dir, name) or {}


def remove(run_dir: Path, name: str) -> None:
    _file(run_dir, name).unlink(missing_ok=True)


class Handle:
    """What a job body may do with its own record."""

    def __init__(self, run_dir: Path, name: str):
        self.run_dir, self.name = Path(run_dir), name

    def add_child(self, pid: int) -> None:
        data = read(self.run_dir, self.name) or {}
        update(self.run_dir, self.name, children=[*(data.get("children") or []), pid])

    def note(self, **fields: Any) -> None:
        update(self.run_dir, self.name, **fields)

    def stopped(self) -> bool:
        data = read(self.run_dir, self.name) or {}
        return data.get("status") == "stopped"


def execute(run_dir: str, name: str) -> dict[str, Any]:
    """The body of ``run.py job-run``: run the job and write how it ended."""
    from . import kinds  # noqa: F401 - registers the job bodies

    folder = Path(run_dir)
    job = read_json(_file(folder, name))
    update(folder, name, pid=os.getpid())
    fn = KINDS.get(job.get("kind"))
    try:
        if fn is None:
            raise RunError(f"知らない処理です: {job.get('kind')}")
        result = fn(folder, job.get("args") or {}, Handle(folder, name))
        status, error = "done", None
    except RunError as exc:
        result, status, error = {"details": exc.details}, "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - the record must say that it ended
        traceback.print_exc()
        result, status, error = {}, "failed", f"{type(exc).__name__}: {exc}"
    current = read_json(_file(folder, name), {})
    if current.get("status") == "stopped":  # stop came first: keep its word
        return current
    return update(folder, name, status=status, result=result, error=error, finished=now_iso())


def stop_all(run_dir: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """End every running job and the process groups it started. (stopped names, unstopped [{name, pid, how}])."""
    stopped, unstopped = [], []
    for job in all_jobs(run_dir):
        if job.get("status") != "running":
            continue
        update(run_dir, job["name"], status="stopped", finished=now_iso(), error="止めました")
        pids = [p for p in [job.get("pid"), *(job.get("children") or [])] if p]
        left = [p for p in pids if not kill_tree(p)]
        if left:
            unstopped += [{"name": job["name"], "pid": p, "how": kill_how(p)} for p in left]
        else:
            stopped.append(job["name"])
    return stopped, unstopped
