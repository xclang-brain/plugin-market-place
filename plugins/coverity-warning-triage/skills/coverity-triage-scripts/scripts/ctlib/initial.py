"""Initial analysis (option verify.initial): what the person did by hand before running /coverity-run.

The open folder is built and analyzed with Coverity, and the result is committed to Coverity
Connect, to the stream of the run's filter, with the revision of the folder as the snapshot's
version. The search that follows then finds the warnings of the current code. Nothing else is done
with the local result.

It takes tens of minutes, so it runs as a detached process (``ct.py initial-run``) that writes
``analysis/status.json``; ``ct.py tick`` waits for it. When a step fails, the run stops before the
triage (the warnings would be the old ones). Only the commands written in the settings are run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import oplog
from .common import CtError, build_env, display_text, pid_alive, read_json, resolve_path, spawn_detached, write_json
from .envvars import get_env
from .run import Run
from .verify import LOG_TAIL, intermediate_dir, no_ticker, safe_relpath

STATUS_FILE = "status.json"
LOG_FILE = "initial.log"
MARK = "__CT_STEP__"
STEPS = {"setup": "環境の設定", "build": "ビルド（cov-build）", "analyze": "解析（cov-analyze）",
         "commit": "サーバへの登録（cov-commit-defects）"}


def folder(run: Run) -> Path:
    return run.dir / "analysis"


def _status_file(run: Run) -> Path:
    return folder(run) / STATUS_FILE


def _tail(log: Path) -> str:
    """The end of the log (the build tools of a Japanese Windows write cp932)."""
    return display_text(log.read_bytes()[-LOG_TAIL:]) if log.is_file() else ""


def status(run: Run) -> dict[str, Any]:
    """none / running / done / failed, with the step, the seconds and (on failure) the end of the log."""
    data = read_json(_status_file(run), {}) if _status_file(run).is_file() else {}
    if not data:
        return {"status": "none"}
    if data.get("status") == "running":
        elapsed = time.time() - data.get("started", 0)
        if not pid_alive(data.get("pid")) and elapsed > 30:  # the process is gone without writing its end
            data = {**data, "status": "failed", "error": "初回解析のプロセスが途中で止まりました"}
        elif elapsed > run.config.verify.timeout_minutes * 60 + 600:  # its PID may be another program's now
            data = {**data, "status": "failed", "error": "初回解析が時間の上限を過ぎても終わっていません"}
    data["elapsed_seconds"] = round((data.get("finished") or time.time()) - data.get("started", time.time()))
    data["step_label"] = STEPS.get(data.get("step", ""), data.get("step", ""))
    if data["status"] == "failed":
        data["log_tail"] = _tail(folder(run) / LOG_FILE)
    return data


def start(run: Run) -> dict[str, Any]:
    """Start the initial analysis in the background (it keeps running when the command returns)."""
    if status(run)["status"] == "running":
        return status(run)
    config = run.config.verify
    if not config.build_command:
        raise CtError("初回解析の設定（ビルドのコマンド）がありません（/coverity-setup の「ビルドでの検証」で設定します）")
    folder(run).mkdir(parents=True, exist_ok=True)
    write_json(_status_file(run), {"status": "running", "step": "setup", "started": time.time(), "pid": None})
    script = resolve_path(__file__).parents[1] / "ct.py"
    with (folder(run) / "process.log").open("wb") as log:
        proc = spawn_detached([sys.executable, str(script), "initial-run", "--run", str(run.dir)], dict(os.environ),
                              run.dir, log)
    data = read_json(_status_file(run), {})
    if data.get("status") == "running" and not data.get("pid"):
        write_json(_status_file(run), {**data, "pid": proc.pid})
    from .triage import _log  # late import: triage is the larger module

    _log(run, "initial-start", pid=proc.pid)
    return status(run)


def wait(run: Run, seconds: int) -> dict[str, Any]:
    """Wait while the initial analysis runs, at most ``seconds`` (one tick of the orchestrating agent)."""
    deadline = time.monotonic() + max(0, seconds)
    current = status(run)
    while current["status"] == "running" and time.monotonic() < deadline:
        time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        current = status(run)
    return current


def command_lines(run: Run, root: Path) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """Two shell lines and their environments: setup → build → analyze, then setup → commit.

    The authentication key is given only to the commit, in an environment variable
    (``COVERITY_PASSPHRASE``, which cov-commit-defects reads when --password is not given): never on
    a command line, and never to the build, whose tools may print their environment into the log."""
    config, coverity = run.config.verify, run.config.coverity
    prefix = []
    if config.setup_command:
        setup = config.setup_command.replace("{root}", str(root))
        prefix.append(("call " if sys.platform == "win32" else "") + setup)
    if config.build_dir:
        target = root / safe_relpath(config.build_dir)
        prefix.append(f'cd /d "{target}"' if sys.platform == "win32" else f'cd "{target}"')
    idir = intermediate_dir(config.cov_analyze_args)
    revision = run.vcs.current_revision()
    build = " && ".join([*prefix, f"echo {MARK}build", f"cov-build {config.cov_build_args} {config.build_command}",
                         f"echo {MARK}analyze", f"cov-analyze {no_ticker(config.cov_analyze_args)}"])
    keys = (coverity.key_env, "COVERITY_PASSPHRASE")
    build_env_ = build_env(drop=keys)
    if coverity.api == "fake":  # the self-test and the tests: nothing is sent anywhere
        commit = " && ".join([*prefix, f"echo {MARK}commit",
                              f"echo commit skipped because coverity.api is fake: stream {run.state.stream} version {revision}"])
        return build, commit, build_env_, build_env_
    user, key = get_env(coverity.user_env), get_env(coverity.key_env)
    if not user or not key:
        raise CtError(f"サーバへの登録に使う認証情報がありません（環境変数 {coverity.user_env} / {coverity.key_env}。"
                      "/coverity-setup で入れます）")
    line = (f'cov-commit-defects --dir "{idir}" --url "{coverity.url}" --stream "{run.state.stream}" '
            f'--user "{user}" --version "{revision}"')
    line += " " + no_ticker(config.cov_commit_args)
    commit = " && ".join([*prefix, f"echo {MARK}commit", line])
    return build, commit, build_env_, build_env(extra={"COVERITY_PASSPHRASE": key}, drop=keys)


def _stop_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


class _Steps:
    """The last step mark the shell lines echoed into the log, read as the log grows."""

    def __init__(self, log: Path):
        self.log, self.offset, self.step, self.rest = log, 0, "setup", b""

    def update(self) -> str:
        try:
            with open(self.log, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return self.step
        self.offset += len(data)
        lines = (self.rest + data).split(b"\n")
        self.rest = lines.pop()
        for line in lines:
            if line.startswith(MARK.encode()):
                self.step = line[len(MARK):].decode("ascii", "replace").strip() or self.step
        return self.step


def _shell(line: str, env: dict[str, str], cwd: Path, log: Path, deadline: float, steps: _Steps,
           on_step: Any) -> tuple[int | None, bool]:
    """Run one shell line into the log. Returns the exit code (None: stopped at the deadline)."""
    extra: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                             else {"start_new_session": True})
    with open(log, "ab") as f:
        proc = subprocess.Popen(line, cwd=cwd, shell=True, stdout=f, stderr=subprocess.STDOUT, env=env, **extra)
        try:
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    _stop_tree(proc)
                    proc.wait()
                    return None, True
                on_step(steps.update())
                time.sleep(2)
        except BaseException:  # the watcher itself fails: never leave the build running unseen
            _stop_tree(proc)
            raise
    on_step(steps.update())
    return proc.returncode, False


def execute(run_dir: str) -> dict[str, Any]:
    """The body of the detached process (ct.py initial-run)."""
    run = Run(run_dir)
    log = folder(run) / LOG_FILE
    started = read_json(_status_file(run), {}).get("started") or time.time()
    dirty = run.vcs.has_local_changes()  # uncommitted changes are analyzed, but the snapshot says HEAD
    base = {"started": started, "pid": os.getpid(), "dirty": dirty}
    state = {"step": ""}

    def on_step(step: str) -> None:
        if step != state["step"]:
            state["step"] = step
            write_json(_status_file(run), {**base, "status": "running", "step": step})

    on_step("setup")
    minutes = run.config.verify.timeout_minutes
    deadline = time.monotonic() + minutes * 60
    steps = _Steps(log)
    secrets = oplog.secrets_of(run.config)
    out: dict[str, Any]
    try:
        build, commit, build_env_, commit_env = command_lines(run, run.repo)
        log.write_bytes(b"")
        code, timed_out = None, False
        for line, env in ((build, build_env_), (commit, commit_env)):
            with open(log, "ab") as f:
                f.write(f"$ {oplog.mask(line, secrets)}\n".encode("utf-8"))
            code, timed_out = _shell(line, env, run.repo, log, deadline, steps, on_step)
            if timed_out or code != 0:
                break
        step = steps.update()
        if timed_out:
            out = {**base, "status": "failed", "step": step, "finished": time.time(),
                   "error": f"{minutes} 分たっても終わらなかったため止めました（設定 verify.timeout_minutes）"}
        elif code != 0:
            out = {**base, "status": "failed", "step": step, "finished": time.time(),
                   "error": f"{STEPS.get(step, step)}で失敗しました（終了コード {code}）"}
        else:
            out = {**base, "status": "done", "step": "commit", "finished": time.time(),
                   "revision": run.vcs.current_revision(), "stream": run.state.stream}
    except CtError as exc:
        out = {**base, "status": "failed", "step": state["step"] or "setup", "error": str(exc), "finished": time.time()}
    except Exception as exc:  # noqa: BLE001 - the status must say it ended
        out = {**base, "status": "failed", "step": state["step"] or "setup", "finished": time.time(),
               "error": f"初回解析を続けられませんでした: {type(exc).__name__}: {exc}"}
    write_json(_status_file(run), out)
    from .triage import _log

    _log(run, "initial", level="critical" if out["status"] == "failed" else "info", status=out["status"],
         step=out["step"], error=out.get("error", ""), seconds=round(out["finished"] - started))
    return out
