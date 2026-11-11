"""Long work as one detached background process per kind, with a status file and a log.

  prepare  fetch the base revision and create the work copies of the slots
  build    option build.check_fixes: apply every fix to one clean copy and run the build command once
  analyze  option build.initial_analysis: cov-build, cov-analyze and cov-commit-defects in the person's tree

A tool call is cut after about 30 seconds, so the caller starts a job with ``ensure`` and asks again until
the status is no longer ``running``. The job process is the only writer of its status file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True  # the skill folder may be read-only, and it stays clean
import vcs  # noqa: E402

# kind -> (status file, log), below the run folder. work/ is removed when the run is finished.
FILES = {"prepare": ("work/prepare.json", "work/prepare.log"), "analyze": ("work/analyze.json", "analyze.log"),
         "build": ("build.json", "build.log")}
SECRET_ENVS = ("COV_AUTH_KEY", "COVERITY_PASSPHRASE")


class JobError(Exception):
    """A problem the person can fix. The message is Japanese."""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- files: every write is atomic, every read survives a writer on Windows ------------------


LOCKED_TRIES = 200  # 10 seconds for a file that another process replaces or reads at this moment

def write_bytes(path: Path, data: bytes) -> None:
    """Write through a temporary file and a rename. Windows refuses the rename while another process
    reads the file: tried again for up to 10 seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    for attempt in range(LOCKED_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == LOCKED_TRIES - 1:
                raise
            time.sleep(0.05)


def write_json(path: Path, data) -> None:
    write_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_json(path: Path, default=None):
    """A JSON file (UTF-8, a BOM is allowed). ``default`` when it is missing or is not JSON. A file that
    stays locked is an error, never ``default``: a locked state.json must not read as a CID nobody works on."""
    for attempt in range(LOCKED_TRIES):
        try:
            return json.loads(path.read_bytes().decode("utf-8-sig"))
        except PermissionError:  # Windows: the file is being replaced at this moment
            if attempt == LOCKED_TRIES - 1:
                raise
            time.sleep(0.05)
        except (OSError, ValueError):
            return default


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        alive = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        kernel32.CloseHandle(handle)
        return alive
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---- the caller's side ----------------------------------------------------------------------


def read(run_dir: Path, kind: str) -> dict | None:
    """The job's status record, or None when it never ran. A job whose process is gone reads as failed."""
    path = run_dir / FILES[kind][0]
    status = read_json(path)
    if isinstance(status, dict) and status.get("status") == "running" and not pid_alive(status.get("pid")):
        status = read_json(path) or status  # it may have ended while we looked
        if status.get("status") == "running":
            status = {**status, "status": "failed", "error": "バックグラウンドの処理が途中で終了しました"}
    return status if isinstance(status, dict) else None


def _spawn(run_dir: Path, kind: str, args: dict) -> subprocess.Popen:
    """Start the job as a process that keeps running after this command has answered."""
    log = run_dir / FILES[kind][1]
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        log.unlink(missing_ok=True)
    except PermissionError:  # Windows: a process an earlier build left behind still holds the log
        log.write_bytes(b"")
    command = [sys.executable, str(Path(__file__).resolve()), str(run_dir), kind, json.dumps(args)]
    with open(log, "ab") as out:
        common = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT, "cwd": str(run_dir),
                  "close_fds": True}
        if os.name != "nt":
            return subprocess.Popen(command, start_new_session=True, **common)
        # No window, and outside the terminal's job object, so that closing the terminal does not end the job.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
        except OSError:
            return subprocess.Popen(command, creationflags=flags, **common)


def ensure(run_dir: Path, kind: str, args: dict, deadline: float) -> dict:
    """The status of the job for these arguments. It is started when it has not run with them (after a
    job of this kind that still runs with other arguments has ended); then this waits until it has ended
    or ``deadline`` (time.monotonic) has passed. A failure is reported once: the next call starts the job
    again."""
    path, log = run_dir / FILES[kind][0], (run_dir / FILES[kind][1]).as_posix()
    while True:
        status = read(run_dir, kind)
        if status is None or (status["status"] != "running" and status.get("args") != args):
            path.unlink(missing_ok=True)
            proc = _spawn(run_dir, kind, args)
            while read(run_dir, kind) is None:  # until the job has written its first record
                if proc.poll() is not None and read(run_dir, kind) is None:
                    return {"status": "failed", "error": "バックグラウンドの処理を開始できませんでした", "log": log}
                time.sleep(0.05)
        elif status["status"] != "running" or time.monotonic() >= deadline:
            break
        else:
            time.sleep(0.2)
    if status["status"] == "failed":
        path.unlink(missing_ok=True)
    return {**status, "log": log}


# ---- the job's side -------------------------------------------------------------------------


def get_env(name: str) -> str:
    """An environment variable; on Windows also the value saved for the user (HKCU\\Environment), which a
    terminal opened before it was saved does not see."""
    value = os.environ.get(name, "").strip()
    if not value and os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, name)[0]).strip()
        except OSError:
            value = ""
    return value


def shell(line: str, cwd: Path, extra: dict[str, str] | None = None) -> int:
    """Run one shell line of the settings; its output goes to the job's log. The environment is the
    person's: without the Python environment uv made for this script, and without the credentials."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", *SECRET_ENVS)}
    if sys.prefix != sys.base_prefix:
        own = os.path.normcase(os.path.dirname(sys.executable))
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if os.path.normcase(p.rstrip("\\/")) != own)
    print(f"$ {line}", flush=True)
    return subprocess.run(line, shell=True, cwd=str(cwd), env={**env, **(extra or {})}, stdin=subprocess.DEVNULL,
                          stdout=sys.stdout, stderr=subprocess.STDOUT, creationflags=vcs.NO_WINDOW).returncode


def prepare(run_dir: Path, args: dict) -> dict:
    repo = vcs.open_repo(Path(args["repo"]))
    meta = read_json(run_dir / "run.json") or {}
    branch, revision, note = meta.get("base_branch", ""), meta.get("base_revision", ""), ""
    if not revision:  # resolved once: every copy and every patch of the run has the same base
        branch, revision, note = repo.base(args["base_branch"])
    for slot in range(1, args["slots"] + 1):
        repo.make_copy(run_dir / "work" / f"w{slot}", revision)
    return {"base_branch": branch, "base_revision": revision, "note": note}


def build(run_dir: Path, args: dict) -> dict:
    """Per CID: ok, failed (its files appear in the error lines, or no fix could be named), unknown
    (another fix broke the build) or not_applied."""
    repo = vcs.open_repo(Path(args["repo"]))
    copy = run_dir / "work" / "build"
    repo.make_copy(copy, args["base_revision"])
    repo.reset(copy, args["base_revision"])
    cids = {}
    for fix in args["fixes"]:
        problem = repo.apply(copy, run_dir / "cid" / str(fix["cid"]) / "fix.patch")
        cids[str(fix["cid"])] = "not_applied" if problem else "ok"
        if problem:
            print(f"CID {fix['cid']} の修正を適用できませんでした: {problem}", flush=True)
    log = run_dir / FILES["build"][1]
    start = log.stat().st_size
    code = shell(args["command"], copy)
    if code != 0:
        errors = [line for line in log.read_bytes()[start:].lower().splitlines()[1:] if b"error" in line]
        applied = [fix for fix in args["fixes"] if cids[str(fix["cid"])] == "ok"]
        blamed = {str(fix["cid"]) for fix in applied
                  if any(Path(name).name.lower().encode() in line for name in fix["files"] for line in errors)}
        for fix in applied:
            cids[str(fix["cid"])] = "failed" if not blamed or str(fix["cid"]) in blamed else "unknown"
    return {"exit_code": code, "cids": cids}


def analyze(run_dir: Path, args: dict) -> dict:
    user, key = get_env("COV_USER"), get_env("COV_AUTH_KEY")
    if not user or not key:
        raise JobError("環境変数 COV_USER と COV_AUTH_KEY を設定してください。"
                       "手順は /coverity-triage-kit:coverity-setup と入力すると表示されます")
    folder = (run_dir / "work" / "cov").as_posix()  # the intermediate directory
    steps = (("ビルド（cov-build）", f'cov-build --dir "{folder}" {args["command"]}', None),
             ("解析（cov-analyze）", f'cov-analyze --dir "{folder}" {args["analyze_args"]}', None),
             # the key goes to the registration only, in the environment: never on a command line or in the log
             ("登録（cov-commit-defects）", f'cov-commit-defects --dir "{folder}" --url "{args["url"]}" '
                                       f'--stream "{args["stream"]}" --user "{user}"', {"COVERITY_PASSPHRASE": key}))
    for step, line, extra in steps:
        code = shell(line, Path(args["repo"]), extra)
        if code != 0:
            raise JobError(f"{step}が失敗しました（終了コード {code}）")
    return {}


JOBS = {"prepare": prepare, "build": build, "analyze": analyze}


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    run_dir, kind, args = Path(argv[0]), argv[1], json.loads(argv[2])
    path = run_dir / FILES[kind][0]
    record = {"kind": kind, "status": "running", "pid": os.getpid(), "started_at": now(), "args": args}
    write_json(path, record)
    try:
        record.update(JOBS[kind](run_dir, args), status="done")
    except (JobError, vcs.VcsError) as exc:
        record.update(status="failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - the status file must say that the job has ended
        traceback.print_exc()
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    write_json(path, {**record, "finished_at": now()})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
