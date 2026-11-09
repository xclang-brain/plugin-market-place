"""Small helpers shared by the modules of the screen (stdlib only)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SKILL_DIR = Path(__file__).resolve().parents[2]  # <this skill>/scripts/guilib/common.py
ASSETS = SKILL_DIR / "assets"


class GuiError(Exception):
    """A problem the person can act on; printed as ``{"ok": false, "error": ..., ...}``."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


class NotFound(GuiError):
    """A run, an item or a file the screen does not show (HTTP 404)."""


def resolve_path(path: str | Path) -> Path:
    """An absolute path. Some Windows drives refuse ``resolve()``; then it is made absolute without links."""
    path = Path(path).expanduser()
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def epoch(value: Any) -> float | None:
    """A time kept in a file: a number (epoch seconds) or an ISO text; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        try:
            return moment.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def read_text(path: Path) -> str:
    """UTF-8 text (a BOM is allowed). UnicodeDecodeError and OSError reach the caller."""
    return path.read_bytes().decode("utf-8-sig")


def read_json(path: Path) -> Any:
    """The JSON of a file; GuiError when it is missing or broken (the message names the file)."""
    try:
        return json.loads(read_text(path))
    except FileNotFoundError:
        raise GuiError(f"ファイルがありません: {path}") from None
    except UnicodeDecodeError:
        raise GuiError(f"文字コードが UTF-8 ではないため読めません: {path}") from None
    except ValueError as exc:
        raise GuiError(f"JSON として読めません（壊れています）: {path}: {exc}") from None
    except OSError as exc:
        raise GuiError(f"ファイルを読めません: {path}: {exc.strerror or exc}") from None


def try_json(path: Path) -> Any:
    """The JSON of a file, or None when it is missing or cannot be read."""
    try:
        return read_json(path)
    except GuiError:
        return None


def replace(tmp: Path, path: Path) -> None:
    """os.replace; on Windows a file being read elsewhere cannot be replaced at once, so it is tried again."""
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                raise
            time.sleep(0.05)


def write_text(path: Path, text: str, *, bom: bool = False) -> None:
    """Write through a temporary file, so that nobody ever reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    data = ("﻿" if bom else "").encode("utf-8") + text.encode("utf-8")
    try:
        tmp.write_bytes(data)
        replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


LOCK_GRACE = 5.0  # a younger lock may be just created (its pid not written yet): never taken as left behind
DENIED_GRACE = 2.0  # Windows: access denied with no lock there is waited out this long, then reported as it is


def pid_alive(pid: Any) -> bool:
    """Whether a process with this pid exists. When it cannot be told, True (the caller then waits)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # it exists but belongs to someone else
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists; anything else: it does not
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return True


def _remove_stale_lock(lock: Path, stale: float) -> bool:
    """Remove a lock left by a process that is gone. True when the lock is gone now (try again at once)."""
    try:
        first = lock.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    age = time.time() - first.st_mtime
    if age <= stale and age <= LOCK_GRACE:
        return False  # not read: on Windows a reader keeps the holder from removing it for a moment
    try:
        content = lock.read_bytes()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if age <= stale:
        text = content.strip()
        if text:
            try:
                pid = int(text)
            except ValueError:
                return False  # not a pid: only the age decides
            if pid_alive(pid):
                return False
    try:  # the same lock still? another waiter may have taken a new one meanwhile
        again = lock.stat()
        if (again.st_ino, again.st_mtime_ns) != (first.st_ino, first.st_mtime_ns) or lock.read_bytes() != content:
            return False
        lock.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _release_lock(lock: Path) -> None:
    """Remove the lock; on Windows a waiter reading the pid can hold it open for a moment, so try again."""
    for attempt in range(40):
        try:
            os.unlink(lock)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                return
            time.sleep(0.05)
        except OSError:
            return


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0, stale: float | None = None) -> Iterator[None]:
    """An exclusive ``<name>.lock`` next to ``path`` (the same convention as the other writers of a run).

    The holder writes its pid. A lock is taken as left behind when it is older than ``stale`` (default:
    ``timeout``), or when it is older than a few seconds and empty or its pid is no longer running.
    """
    lock = path.with_name(path.name + ".lock")
    stale = timeout if stale is None else stale
    deadline = time.monotonic() + timeout
    denied_since: float | None = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            denied_since = None
            if _remove_stale_lock(lock, stale):
                continue
        except PermissionError:
            # Windows: while another process deletes the lock (delete pending), creating it is refused with
            # "access denied" instead of "exists"; the name is still there then. With no lock there, it is a
            # real access problem (a read-only folder, for example): report it after a short grace.
            if sys.platform != "win32":
                raise
            if os.path.lexists(lock):
                denied_since = None
            elif denied_since is None:
                denied_since = time.monotonic()
            elif time.monotonic() - denied_since > DENIED_GRACE:
                raise
        if time.monotonic() > deadline:
            raise GuiError(f"ほかの処理がファイルを使っています。少し待ってから、もう一度保存してください: {lock}") from None
        time.sleep(0.05)
    try:
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        yield
    finally:
        _release_lock(lock)


def display_text(data: bytes) -> str:
    """Bytes of a patch or a source file, for showing only: UTF-8, else cp932 (Japanese Windows)."""
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def spawn_detached(args: list[str], env: dict[str, str], cwd: Path, log: Any) -> subprocess.Popen:
    """Start a process that keeps running after this command (and its terminal) has returned."""
    common: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                              "cwd": str(cwd), "env": env, "close_fds": True}
    if sys.platform != "win32":
        return subprocess.Popen(args, start_new_session=True, **common)
    # No console window; a process group of its own; out of the terminal's job when Windows allows it.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        return subprocess.Popen(args, creationflags=flags, **common)


# ---- the one JSON object on stdout -------------------------------------------------------------


def output_encoding() -> str:
    """PYTHONIOENCODING if set; on Windows with a piped stdout the console code page; else UTF-8."""
    configured = (os.environ.get("PYTHONIOENCODING") or "").split(":")[0].strip()
    if configured:
        return configured
    if sys.platform == "win32":
        try:
            piped = not sys.stdout.isatty()
        except (AttributeError, ValueError):
            piped = True
        if piped:
            try:
                import ctypes

                page = ctypes.windll.kernel32.GetConsoleOutputCP()
            except (AttributeError, OSError):
                page = 0
            if page:
                return f"cp{page}"
    return "utf-8"


def encode_json(data: Any, encoding: str) -> bytes:
    """JSON with ensure_ascii=False in ``encoding``; only the characters it cannot hold become \\uXXXX."""
    try:
        "".encode(encoding)
    except LookupError:
        encoding = "utf-8"
    text = json.dumps(data, ensure_ascii=False, indent=1)
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        pass
    out = []
    for char in text:
        try:
            char.encode(encoding)
            out.append(char)
        except UnicodeEncodeError:
            code = ord(char)
            if code > 0xFFFF:
                code -= 0x10000
                out.append("\\u%04x\\u%04x" % (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)))
            else:
                out.append("\\u%04x" % code)
    return "".join(out).encode(encoding)


def emit(data: Any) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    payload = encode_json(data, output_encoding()) + b"\n"
    if stream is None:
        sys.stdout.write(payload.decode("utf-8", "replace"))
    else:
        stream.write(payload)
        stream.flush()
