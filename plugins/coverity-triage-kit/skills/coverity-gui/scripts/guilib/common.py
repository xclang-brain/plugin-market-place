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


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """An exclusive ``<name>.lock`` next to ``path`` (the same convention as the other writers of a run)."""
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):  # left by a process that died
                if time.time() - lock.stat().st_mtime > timeout:
                    lock.unlink()
                    continue
        except PermissionError:
            # Windows: while another process deletes the lock (delete pending), creating it is refused with
            # "access denied" instead of "exists". The lock is still taken: wait like for an existing one.
            if sys.platform != "win32":
                raise
        if time.monotonic() > deadline:
            raise GuiError(f"ほかの処理がファイルを使っています。少し待ってから、もう一度保存してください: {lock}") from None
        time.sleep(0.05)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


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
