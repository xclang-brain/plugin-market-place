"""Helpers shared by every command: errors, JSON files, locks, processes and the output encoding."""

from __future__ import annotations

import contextlib
import hashlib
import json
import locale
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SKILL_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = SKILL_DIR / "scripts"
ASSETS_DIR = SKILL_DIR / "assets"
RUN_SCRIPT = SCRIPTS_DIR / "run.py"


class RunError(Exception):
    """A problem the agent (and the person) can act on. Printed as ``{"ok": false, "error": ..., ...}``."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


# ---- time -----------------------------------------------------------------------------------


def now_ts() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def iso_of(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


# ---- paths and files ------------------------------------------------------------------------


def resolve_path(path: str | Path) -> Path:
    """An absolute path. Some Windows drives refuse ``resolve()``; then the path is made absolute only."""
    path = Path(path)
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


def inside(path: Path, folder: Path) -> bool:
    """Whether ``path`` is ``folder`` or below it (case-insensitive on Windows)."""
    a = os.path.normcase(str(resolve_path(path)))
    b = os.path.normcase(str(resolve_path(folder)))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes())
    except OSError:
        return None


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def read_json(path: Path, default: Any = None) -> Any:
    """A JSON file (UTF-8, a BOM is allowed). ``default`` when the file is missing (else a RunError)."""
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        if default is not None:
            return default
        raise RunError(f"ファイルがありません: {path}") from None
    except UnicodeDecodeError:
        raise RunError(f"ファイルの文字コードが UTF-8 ではありません: {path}") from None
    except OSError as exc:
        raise RunError(f"ファイルを読めません: {path}（{exc.strerror or exc}）") from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise RunError(f"JSON の形が正しくありません: {path}（{exc}）") from None


def try_json(path: Path) -> Any:
    """A JSON file, or None when it is missing or broken (for files that agents write)."""
    try:
        return read_json(path)
    except RunError:
        return None


def _replace(tmp: Path, path: Path) -> None:
    """os.replace, retried on Windows: a file another process is reading cannot be replaced there."""
    for attempt in range(60):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 59:
                raise
            time.sleep(0.05)


def remove_file(path: Path) -> None:
    """Delete a file (nothing when it is missing), retried on Windows like ``_replace``: a file another
    process is reading cannot be deleted there."""
    for attempt in range(60):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 59:
                raise
            time.sleep(0.05)


def _writable_again(func: Any, path: str, _exc: Any) -> None:
    """rmtree's error handler: clear the read-only mark (svn keeps its pristine files read-only; Windows
    cannot delete a read-only file) and try the delete once more. The caller checks what is left."""
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRWXU if os.path.isdir(path) else stat.S_IREAD | stat.S_IWRITE)
    with contextlib.suppress(OSError):
        os.chmod(os.path.dirname(path), stat.S_IRWXU)
    if func in (os.unlink, os.remove, os.rmdir):
        with contextlib.suppress(OSError):
            func(path)


def remove_tree(path: Path, check: bool = True) -> bool:
    """Delete a folder with everything in it, read-only files too. True when it is gone. With ``check``,
    a folder that is still there (a file another program holds open) is a RunError."""
    path = Path(path)
    for attempt in range(20 if sys.platform == "win32" else 2):
        if not os.path.lexists(path):
            return True
        if path.is_symlink() or not path.is_dir():
            if not path.is_symlink():
                with contextlib.suppress(OSError):
                    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            with contextlib.suppress(OSError):
                remove_file(path)
        elif sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_writable_again)
        else:
            shutil.rmtree(path, onerror=_writable_again)
        if not os.path.lexists(path):
            return True
        time.sleep(0.1)
    if check:
        raise RunError(f"フォルダを消せませんでした（ほかのプログラムが中のファイルを使っているかもしれません）: {path}")
    return False


def write_bytes(path: Path, data: bytes) -> None:
    """Write through a temporary file, so an interrupted command never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    _replace(tmp, path)


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def append_line(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip("\n") + "\n")


LOCK_GRACE = 5.0  # seconds a new lock may stay empty (between creating it and writing the pid)
DENIED_GRACE = 2.0  # seconds "access denied" is taken for a lock being deleted when there is no lock to see
_HELD: set[str] = set()  # the locks this process holds now


def _lock_seen(lock: Path) -> tuple[os.stat_result, bytes] | None:
    try:
        info = lock.stat()
        with open(lock, "rb") as f:
            return info, f.read(64)
    except OSError:
        return None


def _lock_pid(data: bytes) -> int | None:
    try:
        return int(data.decode("ascii").strip()) or None
    except (UnicodeDecodeError, ValueError):
        return None


def _lock_is_stale(lock: Path, info: os.stat_result, data: bytes, stale: float, grace: float = LOCK_GRACE) -> bool:
    """A lock left by a process that is gone: older than ``stale``; or older than ``grace`` and empty or with
    the pid of a process that has ended (or of this process, which does not hold it: a release that failed)."""
    age = time.time() - info.st_mtime
    if age > stale:
        return True
    if age <= grace:
        return False
    pid = _lock_pid(data)
    if pid is None:
        return True
    if pid == os.getpid():
        return str(lock) not in _HELD
    return not pid_alive(pid)


def _remove_if_same(lock: Path, info: os.stat_result, data: bytes) -> bool:
    """Delete a stale lock only when it is still the one looked at (never a fresh lock another waiter took)."""
    again = _lock_seen(lock)
    if again is None:
        return False
    now, now_data = again
    if (now.st_ino, now.st_mtime_ns, now.st_size) != (info.st_ino, info.st_mtime_ns, info.st_size) or now_data != data:
        return False
    try:
        lock.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def clear_dead_lock(path: Path) -> bool:
    """Delete ``<path>.lock`` when the process written in it has ended (after ``stop`` killed a holder, whose
    ``finally`` never ran). True when it was deleted."""
    lock = path.with_name(path.name + ".lock")
    seen = _lock_seen(lock)
    if seen is None:
        return False
    info, data = seen
    pid = _lock_pid(data)
    dead = (pid is not None and pid != os.getpid() and not pid_alive(pid)) or \
        (pid is None and time.time() - info.st_mtime > LOCK_GRACE)
    return dead and _remove_if_same(lock, info, data)


def _release(lock: Path) -> None:
    """Delete the lock; on Windows retried for a while (a waiter may be reading the pid in it)."""
    for attempt in range(100):
        try:
            lock.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 99:
                return  # left behind: the pid in it tells the next process that nobody holds it
            time.sleep(0.03)
        except OSError:
            return


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0, stale: float | None = None) -> Iterator[None]:
    """An exclusive lock between processes (``<path>.lock``, holding the pid). A lock older than ``stale``
    seconds (default: the timeout), or whose process has ended, was left by a process that died, and is removed.
    Not re-entrant: a process waits for a lock it holds itself."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    stale = timeout if stale is None else stale
    deadline = time.monotonic() + timeout
    denied_since: float | None = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            denied_since = None
            seen = _lock_seen(lock)
            if seen is not None and _lock_is_stale(lock, *seen, stale) and _remove_if_same(lock, *seen):
                continue
        except PermissionError:
            # Windows: while another process deletes the lock (delete pending), creating it is refused with
            # "access denied" instead of "exists"; the name is still in the folder then. With no lock to see
            # (after a short grace), it is a real "access denied" (a folder we may not write to): raise it.
            if sys.platform != "win32":
                raise
            if os.path.lexists(lock):
                denied_since = None
            elif denied_since is None:
                denied_since = time.monotonic()
            elif time.monotonic() - denied_since > DENIED_GRACE:
                raise
        if time.monotonic() > deadline:
            raise RunError(f"ほかの処理が終わるのを待ちましたが、終わりませんでした: {lock}") from None
        time.sleep(0.05)
    _HELD.add(str(lock))
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        _HELD.discard(str(lock))
        _release(lock)


def display_text(data: bytes) -> str:
    """Bytes of a log or patch shown in a report (the tools of a Japanese Windows write cp932)."""
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ---- commands and processes -----------------------------------------------------------------

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_cmd(args: list[str], cwd: str | Path | None = None, *, env: dict[str, str] | None = None,
            input_bytes: bytes | None = None, check: bool = True,
            timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run a command without a shell. The output stays bytes (file contents are never decoded)."""
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, input=input_bytes, capture_output=True, check=False,
                              timeout=timeout, creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise RunError(f"コマンドが見つかりません: {args[0]}（入れて、PATH を通してください）") from None
    except subprocess.TimeoutExpired:
        raise RunError(f"{' '.join(args[:3])} が {timeout:.0f} 秒で終わりませんでした") from None
    if check and proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode("utf-8", "replace").strip()
        raise RunError(f"{' '.join(args[:4])} が失敗しました: {message[:1000]}")
    return proc


def out_text(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def spawn_detached(args: list[str], cwd: Path, log_path: Path, env: dict[str, str] | None = None) -> int:
    """Start a process that keeps running after this command returns. Returns its pid.

    POSIX: a new session, so that ``kill_tree`` can end it and everything it started.
    Windows: no window and a new process group. CREATE_NO_WINDOW is used instead of DETACHED_PROCESS:
    uv starts the script through a launcher, and a detached launcher makes Windows open a visible
    console for the real python.exe it starts."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        common: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                                  "cwd": str(cwd), "env": env or dict(os.environ), "close_fds": True}
        if sys.platform != "win32":
            return subprocess.Popen(args, start_new_session=True, **common).pid
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:  # leave the terminal's job object, so that closing the terminal does not end the process
            return subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common).pid
        except OSError:
            return subprocess.Popen(args, creationflags=flags, **common).pid


def pid_alive(pid: int | None) -> bool:
    """Whether a process of this PC is still running (False for None)."""
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a child that ended but was not reaped yet is a zombie: not running
    with contextlib.suppress(OSError, ValueError):
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return False
    return True


def kill_how(pid: int) -> str:
    """How a person stops the process tree by hand."""
    if sys.platform == "win32":
        return f"taskkill /T /F /PID {pid}"
    return f"kill -KILL -- -{pid}"


def kill_tree(pid: int | None, group: bool = True) -> bool:
    """End a process and everything it started. True when it is gone afterwards."""
    if not pid or not pid_alive(pid):
        return True
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        with contextlib.suppress(OSError):
            subprocess.run([str(taskkill) if taskkill.is_file() else "taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, check=False, creationflags=NO_WINDOW, timeout=30)
    else:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                if group:
                    os.killpg(int(pid), sig)
                else:
                    os.kill(int(pid), sig)
            for _ in range(20):
                if not pid_alive(pid):
                    break
                time.sleep(0.05)
            if not pid_alive(pid):
                break
    for _ in range(40):
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def build_env(extra: dict[str, str] | None = None, drop: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment for the person's build commands: without the Python environment uv made for the
    scripts (a ``python`` in the build would be that one) and without the ``drop`` variables."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV", "PYTHONHOME", *drop)}
    scripts = str(Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin"))
    if sys.prefix != sys.base_prefix and env.get("PATH"):
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                      if os.path.normcase(p.rstrip("\\/")) != os.path.normcase(scripts))
    return {**env, **(extra or {})}


# ---- output encoding (design 3.1) -----------------------------------------------------------


def _console_code_page() -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except (AttributeError, OSError):
        return 0


def output_encoding(stream: Any = None, platform: str | None = None, code_page: int | None = None) -> str:
    """The encoding of the JSON on stdout.

    PYTHONIOENCODING when set; on Windows with a piped stdout, the console's code page (PowerShell reads
    the output with it); without a console there, the system's preferred encoding; else UTF-8."""
    stream = sys.stdout if stream is None else stream
    platform = sys.platform if platform is None else platform
    env = (os.environ.get("PYTHONIOENCODING") or "").split(":")[0].strip()
    if env:
        return env
    if platform == "win32":
        try:
            tty = bool(stream.isatty())
        except (AttributeError, ValueError):
            tty = False
        if not tty:
            cp = _console_code_page() if code_page is None else code_page
            return f"cp{cp}" if cp else (locale.getpreferredencoding(False) or "utf-8")
    return "utf-8"


def escape_unencodable(text: str, encoding: str) -> str:
    """``text`` (JSON written with ensure_ascii=False) with only the characters ``encoding`` cannot write
    turned into ``\\uXXXX`` escapes (surrogate pairs above U+FFFF). Valid JSON stays valid JSON."""
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        pass
    except LookupError:  # an unknown encoding name: the caller writes UTF-8
        return text
    out = []
    for ch in text:
        try:
            ch.encode(encoding)
            out.append(ch)
        except UnicodeEncodeError:
            code = ord(ch)
            if code > 0xFFFF:
                code -= 0x10000
                out.append(f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}")
            else:
                out.append(f"\\u{code:04x}")
    return "".join(out)


def emit_json(data: Any, stream: Any = None) -> None:
    """Write one JSON object to stdout in the encoding the reader expects."""
    stream = sys.stdout if stream is None else stream
    encoding = output_encoding(stream)
    try:
        "".encode(encoding)
    except LookupError:
        encoding = "utf-8"
    text = escape_unencodable(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding) + "\n"
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
    else:
        stream.flush()
        buffer.write(text.encode(encoding, "backslashreplace"))
        buffer.flush()


def read_stdin_text() -> str:
    """Standard input as text: UTF-8 (a BOM is removed), else the system's encoding; CRLF becomes LF."""
    data = sys.stdin.buffer.read() if getattr(sys.stdin, "buffer", None) else sys.stdin.read().encode()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode(locale.getpreferredencoding(False) or "utf-8", "replace")
    return text.replace("\r\n", "\n")


def progress(message: str) -> None:
    """A progress line on stderr (never mixed with the JSON on stdout)."""
    with contextlib.suppress(Exception):
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
