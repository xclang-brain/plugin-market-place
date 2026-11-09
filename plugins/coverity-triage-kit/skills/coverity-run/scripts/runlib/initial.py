"""Option verify.initial: build and analyze the open folder, and commit the result to Coverity Connect
before the warnings are searched (what the person did by hand before).

It takes tens of minutes, so it runs as a job that writes ``analysis/status.json`` and
``analysis/initial.log``. The authentication key goes only to the commit, in the environment variable
COVERITY_PASSPHRASE: never on a command line, and never to the build.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from . import envvars, jobs, oplog
from .common import RunError, build_env, read_json, write_json
from .state import Run
from .vcs import make_vcs
from .verify import intermediate_dir, no_ticker, prefix_lines, run_shell, tail

STATUS_FILE = "status.json"
LOG_FILE = "initial.log"
MARK = "__RUN_STEP__"
STEPS = {"setup": "環境の設定", "build": "ビルド（cov-build）", "analyze": "解析（cov-analyze）",
         "commit": "サーバへの登録（cov-commit-defects）"}
PERCENT = {"setup": 5, "build": 25, "analyze": 60, "commit": 90}
JOB = "initial"


def folder(run_dir: Path) -> Path:
    return Path(run_dir) / "analysis"


def status(run: Run) -> dict[str, Any]:
    """none / running / done / failed, with the step, the seconds and, on a failure, the end of the log."""
    data = read_json(folder(run.dir) / STATUS_FILE, {}) if (folder(run.dir) / STATUS_FILE).is_file() else {}
    if not data:
        return {"status": "none"}
    data = dict(data)
    if data.get("status") == "running":
        job = jobs.read(run.dir, JOB)
        if job is None or job.get("status") in ("failed", "stopped"):
            data.update(status="failed", error=(job or {}).get("error") or "初回解析のプロセスが途中で止まりました")
        elif job.get("status") == "done":
            fresh = read_json(folder(run.dir) / STATUS_FILE, {})
            data = dict(fresh) if fresh.get("status") != "running" else {**data, "status": "failed",
                                                                         "error": "初回解析の終わりが記録されていません"}
    data["elapsed_seconds"] = round((data.get("finished") or time.time()) - data.get("started", time.time()))
    data["step_label"] = STEPS.get(data.get("step", ""), data.get("step", ""))
    if data["status"] == "failed":
        data["log_tail"] = tail(folder(run.dir) / LOG_FILE)
    return data


def start(run: Run) -> dict[str, Any]:
    folder(run.dir).mkdir(parents=True, exist_ok=True)
    write_json(folder(run.dir) / STATUS_FILE, {"status": "running", "step": "setup", "started": time.time()})
    jobs.start(run.dir, JOB, "initial", {})
    return status(run)


def command_lines(run: Run, root: Path) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """Two shell lines and their environments: setup → build → analyze, then setup → commit."""
    settings = run.settings
    verify, cov = settings["verify"], settings["coverity"]
    prefix = prefix_lines(verify, root)
    idir = intermediate_dir(verify["cov_analyze_args"])
    revision = make_vcs(run.repo, settings).current_revision()
    build = " && ".join([*prefix, f"echo {MARK}build", f"cov-build {verify['cov_build_args']} {verify['build_command']}",
                         f"echo {MARK}analyze", f"cov-analyze {no_ticker(verify['cov_analyze_args'])}"])
    keys = (cov["key_env"], "COVERITY_PASSPHRASE")
    plain = build_env(drop=keys)
    if cov["api"] == "fake":  # tests and trials: nothing is sent anywhere
        commit = " && ".join([*prefix, f"echo {MARK}commit",
                              f"echo commit skipped because coverity.api is fake: stream {run.state['stream']} "
                              f"version {revision}"])
        return build, commit, plain, plain
    user, key = envvars.get_env(cov["user_env"]), envvars.get_env(cov["key_env"])
    if not user or not key:
        raise RunError(f"サーバへの登録に使う認証情報がありません（環境変数 {cov['user_env']} と {cov['key_env']}）")
    line = (f'cov-commit-defects --dir "{idir}" --url "{cov["url"]}" --stream "{run.state["stream"]}" '
            f'--user "{user}" --version "{revision}" {no_ticker(verify["cov_commit_args"])}').rstrip()
    commit = " && ".join([*prefix, f"echo {MARK}commit", line])
    return build, commit, plain, build_env(extra={"COVERITY_PASSPHRASE": key}, drop=keys)


def _last_step(log: Path) -> str:
    step = "setup"
    try:
        for line in log.read_bytes().splitlines():
            if line.startswith(MARK.encode()):
                step = line[len(MARK):].decode("ascii", "replace").strip() or step
    except OSError:
        pass
    return step


@jobs.register("initial")
def run_initial(run_dir: Path, args: dict[str, Any], handle: jobs.Handle) -> dict[str, Any]:
    run = Run(run_dir)
    log = folder(run.dir) / LOG_FILE
    status_file = folder(run.dir) / STATUS_FILE
    started = read_json(status_file, {}).get("started") or time.time()
    vcs = make_vcs(run.repo, run.settings)
    dirty = vcs.has_local_changes()  # uncommitted changes are analyzed, but the snapshot names HEAD
    base = {"started": started, "pid": os.getpid(), "dirty": dirty}
    minutes = run.settings["verify"]["timeout_minutes"]
    deadline = time.monotonic() + minutes * 60
    secrets = run.secrets()
    write_json(status_file, {**base, "status": "running", "step": "setup"})
    out: dict[str, Any]
    try:
        build, commit, build_env_, commit_env = command_lines(run, run.repo)
        log.write_bytes(b"")
        code: int | None = 0
        for line, env in ((build, build_env_), (commit, commit_env)):
            with open(log, "ab") as f:
                f.write(f"$ {oplog.mask(line, secrets)}\n".encode("utf-8"))
            write_json(status_file, {**base, "status": "running", "step": _last_step(log)})
            code = _shell_with_steps(line, env, run.repo, log, deadline, handle, status_file, base)
            if code != 0:
                break
        step = _last_step(log)
        if code is None:
            out = {**base, "status": "failed", "step": step, "finished": time.time(),
                   "error": f"{minutes} 分たっても終わらなかったため止めました（設定 verify.timeout_minutes）"}
        elif code != 0:
            out = {**base, "status": "failed", "step": step, "finished": time.time(),
                   "error": f"{STEPS.get(step, step)}で失敗しました（終了コード {code}）"}
        else:
            out = {**base, "status": "done", "step": "commit", "finished": time.time(),
                   "revision": vcs.current_revision(), "stream": run.state["stream"]}
    except RunError as exc:
        out = {**base, "status": "failed", "step": _last_step(log), "finished": time.time(), "error": str(exc)}
    write_json(status_file, out)
    return {k: out.get(k) for k in ("status", "step", "error", "revision")}


def _shell_with_steps(line: str, env: dict[str, str], cwd: Path, log: Path, deadline: float, handle: jobs.Handle,
                      status_file: Path, base: dict[str, Any]) -> int | None:
    """run_shell, writing the step to status.json as the log grows."""
    import threading

    done = threading.Event()

    def watch() -> None:
        last = None
        while not done.wait(2):
            step = _last_step(log)
            if step != last:
                last = step
                write_json(status_file, {**base, "status": "running", "step": step})

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        return run_shell(line, cwd, log, env, handle, deadline)
    finally:
        done.set()
        watcher.join(timeout=5)


def progress_view(run: Run) -> dict[str, Any]:
    """``{used, step, value, max, label}`` for the screen."""
    if not run.state.get("initial"):
        return {"used": False, "step": None, "value": 0, "max": 100, "label": "初回解析はしない"}
    current = status(run)
    state = current.get("status")
    if state == "none":
        done = bool(run.state.get("planned")) or bool(run.state.get("fetch", {}).get("issues_done"))
        return {"used": True, "step": None, "value": 100 if done else 0, "max": 100,
                "label": "終わった" if done else "まだ始めていない"}
    step = current.get("step") or "setup"
    if state == "done":
        return {"used": True, "step": "commit", "value": 100, "max": 100, "label": "終わった（サーバに登録した）"}
    if state == "failed":
        return {"used": True, "step": step, "value": PERCENT.get(step, 0), "max": 100,
                "label": f"失敗した（{STEPS.get(step, step)}）"}
    return {"used": True, "step": step, "value": PERCENT.get(step, 0), "max": 100, "label": f"{STEPS.get(step, step)}中"}
