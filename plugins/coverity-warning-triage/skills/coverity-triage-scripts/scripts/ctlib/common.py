"""Helpers shared by the commands of ct.py."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class CtError(Exception):
    """A problem the agent (and the person) can act on. ct.py prints it as ``{"ok": false, ...}``."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


def resolve_path(path: str | Path) -> Path:
    """An absolute path for a folder or file the person named.

    On Windows ``Path.resolve()`` asks the drive for the final path and some drives (RAM disks,
    virtual or network drives) refuse with WinError 433 or Errno 22 although the folder is usable.
    Such a path is then made absolute without following links."""
    path = Path(path)
    try:
        return path.resolve()
    except OSError as exc:
        try:
            return Path(os.path.abspath(path))
        except (OSError, ValueError) as inner:
            raise CtError(f"パスを絶対パスにできません: {path}: {inner}", resolve_error=str(exc)) from inner


def _plugin_root() -> Path | None:
    """The plugin's folder: four folders above this file (``<plugin>/skills/coverity-triage-scripts/scripts/ctlib``).
    None when the scripts sit in a shallower folder, so that importing never fails; ``plugin_path`` says
    what is missing where a command needs the plugin."""
    parents = resolve_path(__file__).parents
    return parents[4] if len(parents) > 4 else None


PLUGIN_ROOT = _plugin_root()
SKILLS_DIR = PLUGIN_ROOT / "skills" if PLUGIN_ROOT else None   # the procedure documents (手順書) the agents read


def plugin_path(*parts: str) -> Path:
    """A file or folder of the plugin around the scripts, for the commands that need it (MCP server,
    agent definitions, procedure documents, plugin.json). A CtError says what is missing otherwise."""
    path = PLUGIN_ROOT.joinpath(*parts) if PLUGIN_ROOT else None
    if path is None or not path.exists():
        raise CtError("プラグインのファイルが見つかりません: " + ("/".join(parts) if path is None else str(path))
                      + "（このコマンドは、ct.py を <プラグインのフォルダ>/skills/coverity-triage-scripts/scripts/"
                        " に置いたまま使います）")
    return path


def skills_dir() -> Path:
    """The folder of the procedure documents (手順書) the agents read, written into their instruction files."""
    return plugin_path("skills")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        if default is not None:
            return default
        raise CtError(f"ファイルがありません: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CtError(f"ファイルを読めません: {path}: {exc}") from exc


def _replace(tmp: Path, path: Path) -> None:
    """os.replace, waited for a little on Windows: a file another process is reading cannot be replaced
    there (Python opens files without FILE_SHARE_DELETE), and the screen reads the run's files often."""
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                raise
            time.sleep(0.05)


def write_json(path: Path, data: Any) -> None:
    """Write via a temporary file so an interrupted command never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # two processes may write the same file
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _replace(tmp, path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    _replace(tmp, path)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Exclusive lock between ct.py processes (several items can be worked on at once)."""
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # A lock older than the timeout was left by a process that died.
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > timeout:
                    lock.unlink()
                    continue
            if time.monotonic() > deadline:
                raise CtError(f"ほかの処理が終わるのを待ちましたが、終わりませんでした: {lock}") from None
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def run_cmd(args: list[str], cwd: str | Path | None = None, *, env: dict[str, str] | None = None,
            input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run a command without a shell. Output stays bytes (no decoding of file contents)."""
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, input=input_bytes, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise CtError(f"コマンドが見つかりません: {args[0]}（インストールして PATH を通してください）") from exc
    if check and proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode("utf-8", "replace").strip()
        raise CtError(f"{' '.join(args[:3])} が失敗しました: {message[:1000]}")
    return proc


def out_text(proc: subprocess.CompletedProcess[bytes]) -> str:
    """Command output that is not file content (revisions, URLs, status lines)."""
    return proc.stdout.decode("utf-8", "replace").strip()


def display_text(data: bytes) -> str:
    """Bytes of a patch shown inside a Markdown report. Only for display; files keep their bytes."""
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def spawn_detached(args: list[str], env: dict[str, str], cwd: Path, log: Any) -> subprocess.Popen[bytes]:
    """Start a process that keeps running after this command (and its terminal) returns."""
    common: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                              "cwd": str(cwd), "env": env, "close_fds": True}
    if sys.platform != "win32":
        return subprocess.Popen(args, start_new_session=True, **common)
    # CREATE_NO_WINDOW, not DETACHED_PROCESS: uv runs the script through a launcher that starts the real
    # python.exe as a console program; a detached launcher would make Windows open a new, visible console
    # for it. With a console of its own that has no window, nothing it starts (cmd, the build) shows one.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:  # leave the terminal's job, so closing the terminal does not stop the process
        return subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        return subprocess.Popen(args, creationflags=flags, **common)


def pid_alive(pid: int | None) -> bool:
    """Whether a process of this PC is still running (False for None)."""
    if not pid:
        return False
    if sys.platform == "win32":
        proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, check=False,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        return str(pid).encode() in proc.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def build_env(extra: dict[str, str] | None = None, drop: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment for the person's build commands: without the Python environment uv made for
    the scripts (a ``python`` in the build would otherwise be that one), and without ``drop``."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV", "PYTHONHOME", *drop)}
    scripts = str(Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin"))
    if sys.prefix != sys.base_prefix and env.get("PATH"):
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                      if os.path.normcase(p.rstrip("\\/")) != os.path.normcase(scripts))
    return {**env, **(extra or {})}
