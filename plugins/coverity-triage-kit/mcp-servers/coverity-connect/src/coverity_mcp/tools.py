"""What the tools do, as plain functions (the MCP layer in :mod:`.server` runs them in a thread).

Each call returns within about 20 seconds, because MCP clients such as the Copilot runtime stop a
tool call after 30 seconds. Work that does not fit is reported (``done: false``, ``has_more``,
``remaining``) and continued by calling again.

Results are passed in one of two ways:

- Files: with ``output_file`` / ``output_dir`` / ``plan_file`` the result is saved in that file, so
  that large data does not pass through the conversation. When work remains the tool returns
  ``done: false`` and is called again with the same arguments; work already saved is not repeated.
- Inline: without them the result is in the tool result, kept small (search: one page of at most
  100 issues; warning paths: at most 10 CIDs; about 25,000 characters).

Writing triage takes two calls: ``coverity_preview_triage`` returns what will be written and a
confirmation token, and ``coverity_update_triage`` writes only with that token.
"""

from __future__ import annotations

import contextlib
import functools
import getpass
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from pydantic import ValidationError

from . import connect, plantoken
from .coverity import CoverityClient, CoverityError, make_client
from .envvars import Masker
from .models import Issue, IssueDetail, PlanEntry, StreamInfo, TriageAttributes, TriageEntry
from .settings import CoverityConfig, FilterArg, FilterSpec, SettingsError, load_config, load_filter, parse_filter

log = logging.getLogger("coverity_mcp")

BUDGET = 20.0  # seconds one tool call may use
DEFAULT_FILE_LIMIT = 100  # issues saved to a file when the filter has no limit
DEFAULT_PAGE, MAX_PAGE = 20, 100  # issues returned in one page without a file
MAX_CIDS = 50  # warning paths saved by one call to files
MAX_INLINE_CIDS = 10  # warning paths returned by one call without files
MAX_EXCLUDED_SAMPLES = 50  # rows left out after the search, kept in issues.json to find the cause
MAX_RECORDS = 500  # request records kept in a file
CHARACTER_LIMIT = 25_000  # inline results stay below this size (clients cap tool output near 25k tokens)
CONCISE_EVENTS = 40  # events shown per warning in the concise form (first and last halves)
MAX_LISTED_STREAMS = 100  # streams in one check result (names: about 3,000 characters)
MAX_LOOKUP_STREAMS = 100  # streams one warning-path request looks in when no stream is given (see below)
MAX_HINT_STREAMS = 20  # stream names in a hint of an error
CALL_AGAIN = "続きがあります。同じ引数でもう一度呼んでください"
RESULT_DIR = "coverity-connect-mcp"  # <RESULT_DIR>-<user> in the temporary folder: write-backs passed as entries
DEFAULT_SECRET_NAMES = ("COV_USER", "COV_AUTH_KEY")

ResponseFormat = Literal["concise", "detailed"]
CONCISE_FIELDS = ("cid", "checker", "file", "line", "function", "impact", "cwe", "classification", "action",
                  "status", "stream")
DETAILED_FIELDS = tuple(Issue.model_fields)


class ToolFailure(Exception):
    """A failure the agent can act on; the message says what to fix."""


class FileBusy(ToolFailure):
    """Another process held a shared file for longer than a call waits."""


# ---- common helpers ---------------------------------------------------------------------------

class Deadline:
    def __init__(self, budget: float = BUDGET):
        self.started = time.monotonic()
        self.budget = budget

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def left(self) -> float:
        return self.budget - self.elapsed()

    def allows(self, client: CoverityClient, requests: int = 1) -> bool:
        """Whether ``requests`` more requests are expected to end within the budget."""
        return self.elapsed() + requests * client.request_seconds() <= self.budget


_secret_names: set[str] = set(DEFAULT_SECRET_NAMES)  # also the names of every settings read so far


def masker_for(config: CoverityConfig | None) -> Masker:
    names = set(_secret_names)
    if config is not None:
        names |= {config.user_env, config.key_env}
    return Masker(sorted(names))


def guarded(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Expected errors become a :class:`ToolFailure` with a message that says what to fix, with the
    credential values masked; unexpected ones are logged and reported without a traceback."""
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return func(*args, **kwargs)
        except ToolFailure as exc:
            raise ToolFailure(_masked(str(exc), kwargs.get("repo_root"))) from None
        except (SettingsError, CoverityError, ValueError, OSError) as exc:
            raise ToolFailure(_masked(str(exc), kwargs.get("repo_root"))) from None
        except Exception as exc:  # noqa: BLE001 - never a traceback to the agent
            log.error("%s", _masked(f"{func.__name__}: {type(exc).__name__}: {exc}", kwargs.get("repo_root")),
                      exc_info=False)
            raise ToolFailure(_masked(f"思わぬエラーで止まりました（{type(exc).__name__}: {exc}）。同じ引数でもう一度呼んでも"
                                      "直らないときは、サーバーのログ（標準エラー）を確かめてください",
                                      kwargs.get("repo_root"))) from None
    return wrapper


def _masked(text: str, repo_root: Any) -> str:
    config = None
    try:
        config = load_config(Path(repo_root)) if repo_root and Path(str(repo_root)).is_absolute() else None
    except Exception:  # noqa: BLE001 - the settings may be what failed
        config = None
    return masker_for(config).text(text)


def absolute(value: str, name: str) -> Path:
    # The server runs in its own folder, so a relative path would point there.
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ToolFailure(f"{name} は絶対パスで渡してください（例: C:/work/app や /home/me/app）: {value}")
    return path


def folder(value: str, name: str) -> Path:
    path = absolute(value, name)
    if not path.is_dir():
        raise ToolFailure(f"{name} のフォルダがありません: {path}")
    return path


def config_of(repo_root: str | None) -> CoverityConfig:
    config = load_config(folder(repo_root, "repo_root") if repo_root else None)
    _secret_names.update((config.user_env, config.key_env))
    return config


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compact(data: dict[str, Any]) -> dict[str, Any]:
    """Without the items that have no value (None, empty text, empty list)."""
    return {k: v for k, v in data.items() if v is not None and v != "" and v != []}


def size(data: Any) -> int:
    return len(json.dumps(data, ensure_ascii=False))


def read_json(path: Path) -> Any:
    for attempt in range(10):
        try:
            text = path.read_text(encoding="utf-8-sig")
            break
        except PermissionError:  # Windows: another process is replacing the file just now
            if sys.platform != "win32" or attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ToolFailure(f"ファイルを JSON として読めません: {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    """Write via a temporary file so an interrupted call never leaves half a file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: another process (a virus scanner, an editor) holds the file
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.1 * (attempt + 1))


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(path: Path) -> threading.Lock:
    """One lock per file, so that two calls at the same time on one file take turns."""
    key = os.path.normcase(str(path.resolve()))
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


LOCK_WAIT = 10.0  # seconds a call waits for another process to finish with a file
LOCK_STALE = 60.0  # a lock older than this was left by a process that died (a hold lasts well under a second)
LOCK_GRACE = 5.0  # a younger lock may be just created (its pid not written yet): never taken as left behind
MIN_LOCK_WAIT = 0.5  # a wait bounded by the time left of a call is never shorter than this
SAVE_GRACE = 3.0  # the last save of a write-back may run this long past the budget (calls are cut at 30 s)
DENIED_GRACE = 2.0  # Windows: access denied with no lock there is waited out this long, then reported as it is
_held_files: set[str] = set()  # the lock files this process holds now


def pid_alive(pid: Any) -> bool:
    """Whether a process with this pid exists. When it cannot be told, True (the caller then waits)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)  # os.kill(pid, 0) would end the process on Windows
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

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # type: ignore[attr-defined]  # ERROR_ACCESS_DENIED: it exists
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return True


def _lock_seen(lock: Path) -> tuple[os.stat_result, bytes] | None:
    try:
        info = lock.stat()
        with open(lock, "rb") as f:
            return info, f.read(64)
    except OSError:
        return None


def _lock_is_stale(lock: Path, info: os.stat_result, data: bytes) -> bool:
    """A lock left by a process that is gone: older than LOCK_STALE; or older than LOCK_GRACE and empty or
    with the pid of a process that has ended (or of this process, which does not hold it)."""
    age = time.time() - info.st_mtime
    if age > LOCK_STALE:
        return True
    if age <= LOCK_GRACE:
        return False
    try:
        pid = int(data.decode("ascii").split()[0])
    except (UnicodeDecodeError, ValueError, IndexError):
        return True
    if pid == os.getpid():
        return str(lock) not in _held_files
    return not pid_alive(pid)


def _remove_if_same(lock: Path, info: os.stat_result, data: bytes) -> bool:
    """Delete a stale lock only when it is still the one looked at (never a fresh lock another waiter took)."""
    again = _lock_seen(lock)
    if again is None:
        return False
    now_info, now_data = again
    if (now_info.st_ino, now_info.st_mtime_ns, now_info.st_size) != (info.st_ino, info.st_mtime_ns, info.st_size) \
            or now_data != data:
        return False
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


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
def file_lock(path: Path, wait: float | None = None) -> Iterator[None]:
    """An exclusive lock between processes on ``path`` (``<path>.lock``, holding the pid): every session
    starts its own server process, and some files are shared by them. Hold it only for a short read and
    write. Not re-entrant."""
    lock = path.with_name(path.name + ".lock")
    wait = LOCK_WAIT if wait is None else wait
    give_up = time.monotonic() + wait
    denied_since: float | None = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            denied_since = None
            seen = _lock_seen(lock)
            if seen is not None and _lock_is_stale(lock, *seen) and _remove_if_same(lock, *seen):
                continue
        except PermissionError:
            # Windows: while another process deletes the lock (delete pending), creating it is refused with
            # "access denied" instead of "exists". With no lock to see (after a short grace), it is a real
            # "access denied" (a folder we may not write to): raise it.
            if sys.platform != "win32":
                raise
            if os.path.lexists(lock):
                denied_since = None
            elif denied_since is None:
                denied_since = time.monotonic()
            elif time.monotonic() - denied_since > DENIED_GRACE:
                raise
        if time.monotonic() > give_up:
            raise FileBusy(f"ほかのセッションがこのファイルを使っています。少し待ってから、同じ引数でもう一度呼んで"
                           f"ください: {path}（{lock.name} は、このファイルを使うセッションがなければ消してかまいません。"
                           f"{LOCK_STALE:.0f} 秒より古いものは、次の呼び出しが片づけます）")
        time.sleep(0.05)
    _held_files.add(str(lock))
    try:
        try:
            os.write(fd, f"{os.getpid()} {now()}".encode("ascii"))
        finally:
            os.close(fd)
        yield
    finally:
        _held_files.discard(str(lock))
        _release(lock)


def one_of(name_a: str, a: Any, name_b: str, b: Any) -> None:
    if (a is None) == (b is None):
        raise ToolFailure(f"{name_a} か {name_b} の、どちらか一方を渡してください")


# ---- check ------------------------------------------------------------------------------------

def _names(names: list[str], most: int = MAX_HINT_STREAMS) -> str:
    """Stream names for a message: at most ``most``, then how many more."""
    shown = ", ".join(names[:most])
    return shown + (f" ほか {len(names) - most} 個" if len(names) > most else "")


def _streams_out(streams: list[StreamInfo], response_format: ResponseFormat) -> dict[str, Any]:
    shown = streams[:MAX_LISTED_STREAMS]
    out: dict[str, Any] = {
        "streams_total": len(streams),
        "streams": [s.name for s in shown] if response_format == "concise"
        else [compact(s.model_dump()) for s in shown]}
    if len(streams) > len(shown):
        out["streams_note"] = (f"ストリームが {len(streams)} 個あるので、名前の順に先頭の {len(shown)} 個だけを"
                               "返しました")
    return out


@guarded
def check_connection(repo_root: str | None = None, response_format: ResponseFormat = "concise") -> dict[str, Any]:
    try:
        config = config_of(repo_root)
    except SettingsError as exc:
        return {"ok": False, "result": str(exc)}
    out: dict[str, Any] = {"config": config.source, "api": config.api, "url": config.url or None,
                           "triage_store": config.triage_store}
    if config.unknown_keys:
        out["unknown_keys"] = config.unknown_keys
        out["unknown_keys_note"] = ("coverity の中のこの項目は、このサーバーでは使いません"
                                    "（書き間違いでなければ、ほかの道具の項目です）")
    if config.api == "fake":
        try:
            client = make_client(config)
        except CoverityError as exc:
            return {**out, "ok": False, "result": str(exc)}
        try:
            count, streams = len(getattr(client, "issues", [])), client.list_streams()
        finally:
            client.close()
        return {**out, "ok": True, "fake_data": config.fake_data,
                "result": f"偽の Coverity（api: fake）を使う設定です。偽の警告は {count} 件です",
                **_streams_out(streams, response_format)}
    out.update(connect.check_auth(config))
    streams = out.pop("streams", None)
    if config.url.lower().startswith("http://"):
        out["warning"] = ("URL が http です。認証キーが暗号化されずに送られます。"
                          "https が使えるか Coverity の管理者に確認してください")
    if streams is not None:
        out.update(_streams_out(streams, response_format))
    return out


def visible_streams(config: CoverityConfig, client: CoverityClient, deadline: Deadline) -> list[StreamInfo] | None:
    """The streams the user can see, or None when they cannot be read now. For hints only: a failure
    here never fails the call whose result is already there."""
    if not deadline.allows(client):
        return None
    try:
        return client.list_streams()
    except CoverityError as exc:
        log.warning("%s", masker_for(config).text(f"ストリームの一覧を取れませんでした: {exc}"))
        return None


def unknown_stream(config: CoverityConfig, stream: str, deadline: Deadline) -> str | None:
    """A message when ``stream`` is not among the streams the user can see; None when it is, or when
    the list cannot be read now."""
    try:
        client = make_client(config)
    except CoverityError:
        return None
    try:
        streams = visible_streams(config, client, deadline)
    finally:
        client.close()
    names = [s.name for s in streams or []]
    if streams is None or stream in names:
        return None
    return (f"ストリーム {stream} は、見られるストリームにありません（名前は大文字と小文字も区別します）。"
            + (f"見られるストリーム（{len(names)} 個）: {_names(names)}。" if names else
               "見られるストリームが1つもありません。ユーザーにストリームを見る権限があるかを、Coverity の管理者に確かめてください。")
            + "ストリームの名前は coverity_check_connection でも分かります")


# ---- search ------------------------------------------------------------------------------------

def _filter(filter_arg: FilterArg | dict | None, filter_file: str | None) -> tuple[FilterSpec, str, list[str]]:
    one_of("filter_file", filter_file, "filter", filter_arg)
    if filter_file is not None:
        spec, raw, skipped = load_filter(absolute(filter_file, "filter_file"))
        return spec, sha(raw)[:12], skipped
    spec = parse_filter(filter_arg)
    return spec, sha(spec.model_dump_json().encode("utf-8"))[:12], []


def _fields(issue: Issue, response_format: ResponseFormat) -> dict[str, Any]:
    names = DETAILED_FIELDS if response_format == "detailed" else CONCISE_FIELDS
    return compact({k: getattr(issue, k) for k in names})


def _ignored(values: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


IGNORED_NOTE = ("条件の値のうち ignored_values のものは、Coverity にない値なので外して探しました"
                "（書き間違いなら直して呼び直してください）")


@guarded
def search_issues(repo_root: str | None = None, filter: FilterArg | dict | None = None,  # noqa: A002
                  filter_file: str | None = None, offset: int = 0, limit: int = DEFAULT_PAGE,
                  response_format: ResponseFormat = "concise", output_file: str | None = None) -> dict[str, Any]:
    deadline = Deadline()
    config = config_of(repo_root)
    spec, filter_sha, skipped = _filter(filter, filter_file)
    if offset < 0:
        raise ToolFailure("offset は 0 以上で渡してください")
    if output_file is None:
        if not 1 <= limit <= MAX_PAGE:
            raise ToolFailure(f"limit は 1〜{MAX_PAGE} で渡してください（{limit}）。もっと多く取るときは、"
                              "ページを続けて取るか、output_file を渡してファイルに書いてください")
    elif offset:
        raise ToolFailure("offset は output_file を渡さないときだけ使います（output_file を渡したときは、"
                          "同じ引数で呼び直すと続きから検索します）")
    try:
        if output_file is None:
            out = _search_inline(config, spec, offset, limit, response_format, deadline)
            empty = offset == 0 and out["count"] == 0 and not out["has_more"]
        else:
            out = _search_file(config, spec, filter_sha, absolute(output_file, "output_file"), deadline)
            empty = out["done"] and out["found"] == 0
    except CoverityError as exc:
        # Some servers answer a stream they do not have with 404, others with no issue.
        if exc.fatal or exc.status != 404:
            raise
        raise ToolFailure(unknown_stream(config, spec.streams[0], deadline) or str(exc)) from None
    if empty:
        # No issue: say so when the stream itself is wrong, rather than "0 issues" (one request, reused).
        message = unknown_stream(config, spec.streams[0], deadline)
        if message:
            raise ToolFailure(message)
    if skipped:
        out["skipped_keys"] = skipped
    return out


def _search_inline(config: CoverityConfig, spec: FilterSpec, offset: int, limit: int,
                   response_format: ResponseFormat, deadline: Deadline) -> dict[str, Any]:
    """One page of issues in the tool result. ``offset`` counts the rows Coverity returned."""
    client = make_client(config)
    try:
        page = client.search(spec, limit, offset, deadline.left())
    finally:
        client.close()
    issues, ends, next_offset = page.issues, page.ends, page.next_offset
    if len(issues) > limit:
        next_offset = ends[limit - 1]  # continue just after the last issue returned, so none is skipped
        issues = issues[:limit]
    rows = [_fields(i, response_format) for i in issues]
    truncated = None
    budget, used = CHARACTER_LIMIT - 2_000, 0
    for count, row in enumerate(rows):
        used += size(row) + 2
        if used > budget and count > 0:
            rows, next_offset = rows[:count], ends[count - 1]
            truncated = (f"結果が大きいので {count} 件で区切りました。続きは offset に next_offset を渡して取るか、"
                         "response_format を concise にしてください")
            break
    if next_offset is not None and page.total is not None and next_offset >= page.total:
        next_offset = None
    excluded = len(page.excluded) if next_offset is None else sum(1 for e in page.excluded_ends if e <= next_offset)
    out: dict[str, Any] = {
        "stream": spec.streams[0], "count": len(rows), "offset": offset, "total": page.total,
        "has_more": next_offset is not None, "next_offset": next_offset, "excluded": excluded,
        "seconds": round(deadline.elapsed(), 1),
        "next": None if next_offset is None else f"続きがあります。offset に {next_offset} を渡して呼び直してください"}
    ignored = _ignored(page.ignored)
    if ignored:
        out["ignored_values"], out["ignored_note"] = ignored, IGNORED_NOTE
    if truncated:
        out["truncated"] = truncated
    out["issues"] = rows
    return out


def _new_state(config: CoverityConfig, spec: FilterSpec, filter_sha: str) -> dict[str, Any]:
    return {"filter_sha": filter_sha, "stream": spec.streams[0], "limit": spec.limit or DEFAULT_FILE_LIMIT,
            "started_at": now(), "finished_at": None, "coverity_total": None, "next_offset": 0,
            "limited": False, "revision_field": config.revision_field or None,
            "revision_checked": not config.revision_field, "analyzed_revision": None,
            "revision_error": None, "issues": [], "excluded_count": 0, "excluded": [],
            "excluded_cids": [], "requests": []}


def _search_file(config: CoverityConfig, spec: FilterSpec, filter_sha: str, out: Path,
                 deadline: Deadline) -> dict[str, Any]:
    if not out.parent.is_dir():
        raise ToolFailure(f"output_file を書くフォルダがありません: {out.parent}")
    masker = masker_for(config)
    with lock_for(out):
        state = read_json(out) if out.is_file() else None
        if not isinstance(state, dict) or state.get("filter_sha") != filter_sha or not isinstance(state.get("issues"), list):
            state = _new_state(config, spec, filter_sha)
        limit = int(state.get("limit") or DEFAULT_FILE_LIMIT)
        ignored: list[dict[str, str]] = []
        client = make_client(config)
        try:
            if state["next_offset"] is not None and deadline.allows(client):
                page = client.search(spec, limit - len(state["issues"]), state["next_offset"], deadline.left())
                ignored += page.ignored
                # A new snapshot between calls, or pages that overlap, can return a row again: never keep
                # a CID twice, neither from an earlier call nor within this one.
                known = {i["cid"] for i in state["issues"]}
                for issue in page.issues:
                    if issue.cid not in known:
                        known.add(issue.cid)
                        state["issues"].append(issue.model_dump(exclude_none=True))
                samples = state.setdefault("excluded", [])
                left_out = state.setdefault("excluded_cids", [e["cid"] for e in samples])
                seen = set(left_out)
                new = []
                for e in page.excluded:
                    if e["cid"] not in seen and e["cid"] not in known:
                        seen.add(e["cid"])
                        new.append(e)
                left_out += [e["cid"] for e in new]
                state["excluded_count"] = int(state.get("excluded_count") or 0) + len(new)
                samples += new[:max(0, MAX_EXCLUDED_SAMPLES - len(samples))]
                state["coverity_total"] = page.total
                if len(state["issues"]) >= limit:
                    state["limited"] = len(state["issues"]) > limit or page.next_offset is not None
                    state["issues"] = state["issues"][:limit]
                    state["next_offset"] = None
                else:
                    state["next_offset"] = page.next_offset
            if state["next_offset"] is None and not state["revision_checked"] and deadline.allows(client, 2):
                try:
                    state["analyzed_revision"] = client.snapshot_revision(state["stream"], state["revision_field"])
                except CoverityError as exc:
                    if exc.fatal:
                        raise
                    state["revision_error"] = masker.text(str(exc))  # the issues are usable without the revision
                state["revision_checked"] = True
        finally:
            state["requests"] = (list(state.get("requests") or []) + masker.data(client.requests))[-MAX_RECORDS:]
            client.close()
            done = state["next_offset"] is None and bool(state["revision_checked"])
            if done and not state.get("finished_at"):
                state["finished_at"] = now()
            write_json(out, state)
    ignored += [{"filter": str(r.get("filter") or ""), "value": str(r.get("value"))} for r in state["requests"]
                if r.get("status") == "ignored"]
    excluded = int(state.get("excluded_count") or 0)
    result: dict[str, Any] = {
        "done": done, "found": len(state["issues"]), "limit": limit, "limited": state["limited"],
        "coverity_total": state["coverity_total"], "excluded": excluded,
        "excluded_note": (f"Coverity が返した警告のうち {excluded} 件が、検索のあとの条件（Status やワイルドカードの"
                          f"チェッカー）に合わず外れました。合わなかった項目と値は {out} の excluded にあります")
        if excluded else None,
        "analyzed_revision": state["analyzed_revision"], "revision_error": state["revision_error"],
        "seconds": round(deadline.elapsed(), 1), "output_file": str(out), "next": None if done else CALL_AGAIN}
    ignored = _ignored(ignored)
    if ignored:
        result["ignored_values"], result["ignored_note"] = ignored, IGNORED_NOTE
    return result


# ---- warning paths -----------------------------------------------------------------------------
#
# Without a stream, the CID is looked for in every stream the user can see, in one request per CID
# (the same number of requests as with a stream; see connect.py). The path comes from the first of
# those streams, by name, that holds the CID, and the others are listed. More than
# MAX_LOOKUP_STREAMS streams are not searched: the request and the server's work grow with the
# number of streams, and 100 is the limit the SOAP API documents for its lists of CIDs (no limit is
# documented for streamIdList; the same bound keeps one request of a known size).


def _events(detail: IssueDetail, response_format: ResponseFormat, cap: bool) -> tuple[list[dict[str, Any]], int]:
    events = []
    for e in detail.events:
        item = e.model_dump()
        if response_format == "concise" and not item["main"]:
            item.pop("main")
        events.append(compact(item) if response_format == "concise" else {k: v for k, v in item.items() if v is not None})
    if cap and len(events) > CONCISE_EVENTS:
        half = CONCISE_EVENTS // 2
        return events[:half] + events[-half:], len(events) - CONCISE_EVENTS
    return events, 0


def _detail_inline(detail: IssueDetail, response_format: ResponseFormat, cap: bool) -> dict[str, Any]:
    issue = detail.issue
    events, omitted = _events(detail, response_format, cap)
    head = {"cid": issue.cid, "stream": issue.stream, "checker": issue.checker, "file": issue.file,
            "line": issue.line, "function": issue.function}
    if response_format == "detailed":
        head.update({"impact": issue.impact, "category": issue.category, "cwe": issue.cwe})
    others = [s for s in detail.found_in if s != issue.stream]
    item = compact({**head, "other_streams": others, "events": events,
                    "checker_description": detail.checker_description})
    item.setdefault("events", [])
    if omitted:
        item["omitted_events"] = omitted
        item["omitted_note"] = (f"イベントが多いので、間の {omitted} 件を省きました。すべては output_dir に"
                                "書いて読んでください")
    return item


def _lookup_streams(config: CoverityConfig, client: CoverityClient) -> list[str]:
    """Every stream the user can see, to look for CIDs whose stream is not given."""
    try:
        streams = [s.name for s in client.list_streams()]
    except CoverityError as exc:
        if exc.fatal:
            raise
        raise ToolFailure(f"ストリームの一覧を取れないので、CID だけでは探せません（{exc}）。stream を渡してください") from None
    if not streams:
        raise ToolFailure("見られるストリームが1つもありません。ユーザーにストリームを見る権限があるかを、"
                          "Coverity の管理者に確かめてください")
    if len(streams) > MAX_LOOKUP_STREAMS:
        raise ToolFailure(f"見られるストリームが {len(streams)} 個あり、{MAX_LOOKUP_STREAMS} 個を超えるので、CID だけでは"
                          "探しません。stream を渡してください（ストリームの名前は coverity_check_connection の streams に"
                          "あります。警告の検索の結果の各警告にも stream があります）")
    return streams


def _failed_note(config: CoverityConfig, client: CoverityClient, stream: str | None, failed: dict[str, str],
                 deadline: Deadline) -> str | None:
    """With a stream given, where else to look for the CIDs in ``failed`` (without one, the message of
    each CID already says which streams were searched)."""
    if not failed or stream is None:
        return None
    text = f"failed の CID は、ストリーム {stream} で取れませんでした。"
    streams = visible_streams(config, client, deadline)
    if streams is not None:
        names = [s.name for s in streams]
        others = [n for n in names if n != stream]
        if stream not in names:
            text += f"ストリーム {stream} は、見られるストリームにありません。"
        text += (f"ほかの見られるストリーム（{len(others)} 個）: {_names(others)}。" if others
                 else "ほかに見られるストリームはありません。")
    return text + "stream を省いて呼ぶと、見られるすべてのストリームから CID を探します"


@guarded
def get_issue_details(repo_root: str | None = None, stream: str | None = None, cids: list[int] | None = None,
                      response_format: ResponseFormat = "concise", output_dir: str | None = None) -> dict[str, Any]:
    deadline = Deadline()
    stream = str(stream).strip() or None if stream is not None else None
    config = config_of(repo_root)
    wanted = list(dict.fromkeys(cids or []))
    most = MAX_CIDS if output_dir is not None else MAX_INLINE_CIDS
    if not wanted or len(wanted) > most:
        raise ToolFailure(f"cids は 1〜{most} 件で渡してください（{len(wanted)} 件）"
                          + ("。分けて呼んでください" if output_dir is not None else "。もっと多いときは output_dir を渡してください"))
    target = None
    if output_dir is not None:
        target = absolute(output_dir, "output_dir")
        if not target.is_dir():
            if not target.parent.is_dir():
                raise ToolFailure(f"output_dir を作るフォルダがありません: {target.parent}")
            target.mkdir(exist_ok=True)
    client = make_client(config)
    try:
        if target is None:
            out = _details_inline(config, client, stream, wanted, response_format, deadline)
        else:
            out = _details_files(config, client, stream, wanted, target, deadline)
        note = _failed_note(config, client, stream, out["failed"], deadline)
    finally:
        client.close()
    if note:
        out["failed_note"] = note
    return out


def _where(stream: str | None, streams: list[str] | None) -> dict[str, Any]:
    """The stream given, or how many streams were searched for CIDs without one (none: no lookup)."""
    if stream is not None:
        return {"stream": stream}
    return {"streams_searched": len(streams)} if streams else {}


def _fetch(client: CoverityClient, cid: int, streams: list[str], masker: Masker) -> IssueDetail | str:
    """The warning path, or why this CID failed (a failure of the whole call is raised)."""
    try:
        return client.issue_detail(cid, streams)
    except CoverityError as exc:
        if exc.fatal:
            raise  # nothing is written, so the CID can be fetched later
        return masker.text(str(exc))


def _details_inline(config: CoverityConfig, client: CoverityClient, stream: str | None, cids: list[int],
                    response_format: ResponseFormat, deadline: Deadline) -> dict[str, Any]:
    masker = masker_for(config)
    streams = [stream] if stream is not None else _lookup_streams(config, client)
    issues: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    remaining: list[int] = []
    too_big = False
    used = 0
    for cid in cids:
        if too_big or not deadline.allows(client):
            remaining.append(cid)
            continue
        detail = _fetch(client, cid, streams, masker)
        if isinstance(detail, str):
            failed[str(cid)] = detail
            continue
        item = _detail_inline(detail, response_format, cap=response_format == "concise")
        if size(item) > CHARACTER_LIMIT - 2_000:
            item = _detail_inline(detail, "concise", cap=True)
        if issues and used + size(item) > CHARACTER_LIMIT - 2_000:
            too_big = True
            remaining.append(cid)
            continue
        used += size(item)
        issues.append(item)
    if too_big:
        note = "結果が大きいので区切りました。remaining の CID だけを cids に渡して呼び直してください"
    elif remaining:
        note = "時間が足りず、remaining の CID は取っていません。その CID だけを cids に渡して呼び直してください"
    else:
        note = None
    return {**_where(stream, streams), "issues": issues, "failed": failed, "remaining": remaining,
            "seconds": round(deadline.elapsed(), 1), "next": note}


def _details_files(config: CoverityConfig, client: CoverityClient, stream: str | None, cids: list[int],
                   target: Path, deadline: Deadline) -> dict[str, Any]:
    masker = masker_for(config)
    streams: list[str] | None = [stream] if stream is not None else None
    saved: list[int] = []
    already: list[int] = []
    failed: dict[str, str] = {}
    remaining: list[int] = []
    for cid in cids:
        file = target / f"issue-{cid}.json"
        with lock_for(file):
            if file.is_file():
                already.append(cid)
                continue
            if streams is None:
                streams = _lookup_streams(config, client)  # only when a CID is still to fetch
            if not deadline.allows(client):
                remaining.append(cid)
                continue
            detail = _fetch(client, cid, streams, masker)
            if isinstance(detail, str):
                failed[str(cid)] = detail
                write_json(file, {"cid": cid, "stream": stream, "fetched_at": now(), "error": detail})
                continue
            write_json(file, {"fetched_at": now(), "issue": detail.issue.model_dump(exclude_none=True),
                              "events": [e.model_dump(exclude_none=True) for e in detail.events],
                              "checker_description": detail.checker_description})
            saved.append(cid)
    done = not remaining
    return {"done": done, **_where(stream, streams), "saved": saved, "already_saved": already,
            "failed": failed, "remaining": remaining, "output_dir": str(target),
            "seconds": round(deadline.elapsed(), 1), "next": None if done else CALL_AGAIN}


# ---- write-back --------------------------------------------------------------------------------

def plan_token(raw: bytes) -> str:
    """Confirmation token of a plan file (the shared rule of plantoken.py: SHA-256 of its bytes, 12 hex digits)."""
    return plantoken.token_for_bytes(raw)


def entry_dict(entry: TriageEntry) -> dict[str, Any]:
    """An entry as it is shown and confirmed: without a stream when none is given."""
    return entry.model_dump(exclude_none=True)


def entry_key(entry: TriageEntry) -> str:
    """Key of an entry in the result file, by content."""
    return sha(entry.model_dump_json(exclude_none=True).encode("utf-8"))[:16]


def entries_token(entries: list[TriageEntry]) -> str:
    """Confirmation token of entries already checked at the tool boundary (the shared rule of plantoken.py)."""
    return plantoken.token_for_entries([entry_dict(e) for e in entries])


def _entries(entries: list[TriageEntry] | list[dict[str, Any]]) -> list[TriageEntry]:
    try:
        items = [e if isinstance(e, TriageEntry) else TriageEntry.model_validate(e) for e in entries]
    except ValidationError as exc:
        raise ToolFailure(f"entries が正しくありません: {exc}") from exc
    if not items:
        raise ToolFailure("entries に、書き戻す項目を1つ以上入れてください")
    return items


def _plan(plan_file: str) -> tuple[Path, bytes, list[TriageEntry]]:
    path = absolute(plan_file, "plan_file")
    if not path.is_file():
        raise ToolFailure(f"plan_file がありません: {path}")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        items = [PlanEntry.model_validate(e) for e in (data.get("triage") or [])]
    except (ValueError, AttributeError, TypeError, ValidationError) as exc:
        raise ToolFailure(f"書き戻しの内容を読めません（{{\"triage\": [...]}} の JSON にしてください）: {path}: {exc}") from exc
    return path, raw, items


@guarded
def preview_triage(plan_file: str | None = None,
                   entries: list[TriageEntry] | list[dict[str, Any]] | None = None) -> dict[str, Any]:
    one_of("plan_file", plan_file, "entries", entries)
    if plan_file is not None:
        _, raw, items = _plan(plan_file)
        token = plan_token(raw)
    else:
        items = _entries(entries or [])
        token = entries_token(items)
    no_stream = sum(1 for e in items if not e.stream)
    out = {"counts": {"entries": len(items), "cids": len({c for e in items for c in e.cids})},
           "items": [entry_dict(e) for e in items], "confirmation_token": token,
           "next": ("件数と内容を人に見せ、同意を得てから coverity_update_triage に confirmation_token と、"
                    "同じ plan_file か entries を渡してください。confirmation_token は自分で作らないでください")}
    if no_stream:
        out["note"] = (f"stream のない {no_stream} 項目は、設定のトリアージストア（coverity.triage_store、既定 "
                       "Default Triage Store）に書きます")
    return out


def _user_name() -> str:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no name in the environment nor in the user database
        name = str(os.getuid()) if hasattr(os, "getuid") else ""
    return re.sub(r"[^\w.-]", "_", name) or "user"


def entries_result_path(config: CoverityConfig) -> tuple[Path, dict[str, str]]:
    """The result file of write-backs passed as entries, and the target it is for.

    One folder per user in the temporary folder (only that user can read it), one file per
    Coverity and triage store: an entry written there is known by its content, whatever the order
    of the entries or the confirmation token."""
    base = Path(tempfile.gettempdir()) / f"{RESULT_DIR}-{_user_name()}"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if hasattr(os, "getuid"):
        if base.stat().st_uid != os.getuid():
            raise ToolFailure(f"書き戻しの記録のフォルダが、ほかのユーザーのものです: {base}。環境変数 TMPDIR に、"
                              "自分だけが使う一時のフォルダを設定してください")
        os.chmod(base, 0o700)
    url = config.url.rstrip("/") if config.api != "fake" else f"fake:{config.fake_data}"
    target = {"url": url, "triage_store": config.triage_store}
    name = sha(json.dumps([url, config.triage_store], ensure_ascii=False).encode("utf-8"))[:12]
    return base / f"triage-{name}.result.json", target


def _load_result(path: Path, target: dict[str, str] | None) -> dict[str, Any]:
    """The record of write-backs as saved now, in its full form (empty when there is none yet)."""
    try:
        result = read_json(path)
    except FileNotFoundError:
        result = {}
    if not isinstance(result, dict) or (target is not None and {k: result.get(k) for k in target} != target):
        result = dict(target or {})
    for key in ("written", "failed"):
        if not isinstance(result.get(key), dict):
            result[key] = {}
    if not isinstance(result.get("requests"), list):
        result["requests"] = []
    return result


def _merge_result(saved: dict[str, Any], written: dict[str, Any], failed: dict[str, Any],
                  requests: list[Any]) -> dict[str, Any]:
    """The saved record with what this call wrote, what Coverity refused in it, and its requests added."""
    out = {**saved, "written": dict(saved["written"]), "failed": dict(saved["failed"])}
    for key, value in written.items():
        out["written"][key] = value
        out["failed"].pop(key, None)
    for key, value in failed.items():
        if key not in out["written"]:
            out["failed"][key] = value
    out["requests"] = (saved["requests"] + list(requests))[-MAX_RECORDS:]
    return out


_unsaved: dict[str, dict[str, Any]] = {}  # per record: entries written whose save failed (and refusals, requests)


def _done_before(result: dict[str, Any], key: str, token: str) -> bool:
    """Written already, or refused by Coverity with this confirmation token."""
    return key in result["written"] or (result["failed"].get(key) or {}).get("token") == token


@guarded
def update_triage(confirmation_token: str, repo_root: str | None = None, plan_file: str | None = None,
                  entries: list[TriageEntry] | list[dict[str, Any]] | None = None) -> dict[str, Any]:
    deadline = Deadline()
    one_of("plan_file", plan_file, "entries", entries)
    if plan_file is not None:
        plan_path, raw, items = _plan(plan_file)
        token = plan_token(raw)
    else:
        items = _entries(entries or [])
        token = entries_token(items)
    if (confirmation_token or "").strip() != token:
        raise ToolFailure("確認の文字列（confirmation_token）が書き戻しの内容と合いません。何も書いていません。"
                          "coverity_preview_triage をやり直し、件数と内容を人に見せて同意を得てから呼んでください")
    config = config_of(repo_root)
    masker = masker_for(config)
    if plan_file is not None:
        result_path, target = plan_path.with_name(plan_path.stem + ".result.json"), None
    else:
        result_path, target = entries_result_path(config)
    # The record of entries is shared by the server processes of every session: each save is a short
    # merge under a lock between processes, and an entry is looked up again just before it is sent.
    # Entries written to Coverity whose save failed are kept in this process (see _unsaved) and saved
    # by the next call: they are never sent again.
    unsaved_key = os.path.normcase(str(result_path.resolve()))
    written_mine: dict[str, Any] = {}
    failed_mine: dict[str, Any] = {}

    def lock_wait(extra: float = 0.0) -> float:
        """A lock wait that ends within the time left of this call (plus ``extra``)."""
        return max(MIN_LOCK_WAIT, min(LOCK_WAIT, deadline.left() + extra))

    def fresh(requests: list[Any] | None = None) -> dict[str, Any]:
        return _merge_result(_load_result(result_path, target), written_mine, failed_mine, requests or [])

    def fresh_locked() -> dict[str, Any]:
        with file_lock(result_path, wait=lock_wait()):
            return fresh()

    def save(wait: float, requests: list[Any] | None = None) -> dict[str, Any]:
        with file_lock(result_path, wait=wait):
            merged = fresh(requests)
            write_json(result_path, merged)
        return merged

    with lock_for(result_path):
        earlier = _unsaved.get(unsaved_key) or {}
        written_mine.update(earlier.get("written") or {})
        failed_mine.update(earlier.get("failed") or {})
        unsaved = list(written_mine)  # written to Coverity, not in the record yet
        result = fresh()
        written_now: list[str] = []
        remaining: list[str] = []
        client = make_client(config)
        finished = busy = False
        save_error: BaseException | None = None
        stopped_by_save = False  # sending stopped because the save after a write failed (not a busy record)
        try:
            for entry in items:
                key = entry_key(entry)  # by content: an entry changed after writing is written again
                if _done_before(result, key, token):
                    continue
                # An entry with a stream also asks which triage store the stream uses: two requests.
                requests_needed = 2 if entry.stream else 1
                if busy or not deadline.allows(client, requests_needed):
                    remaining.append(entry.id)
                    continue
                try:
                    result = fresh_locked()  # another session may have written it since
                except FileBusy:
                    busy = True  # nothing is sent while the record cannot be saved: calling again sends it
                    remaining.append(entry.id)
                    continue
                if _done_before(result, key, token):
                    continue
                if not deadline.allows(client, requests_needed):  # the wait for the lock took the time
                    remaining.append(entry.id)
                    continue
                attributes = TriageAttributes(classification=entry.classification, action=entry.action,
                                              severity=entry.severity)
                try:
                    store = client.write_triage(entry.cids, attributes, entry.comment, entry.stream)
                except CoverityError as exc:
                    if exc.fatal:
                        raise  # nothing is recorded for this entry: calling again writes it
                    # A value Coverity refused is not sent again with this token: correct and confirm again.
                    failed_mine[key] = {"id": entry.id, "cids": entry.cids, "error": masker.text(str(exc)),
                                        "token": token, "at": now()}
                    result = _merge_result(result, {}, {key: failed_mine[key]}, [])
                    continue
                written_mine[key] = compact({"id": entry.id, "cids": entry.cids, "stream": entry.stream,
                                             "triage_store": store, "at": now()})
                written_now.append(entry.id)
                unsaved.append(key)
                try:
                    result = save(lock_wait())
                    unsaved.clear()
                except (ToolFailure, OSError) as exc:
                    # Written to Coverity: never an error now (calling again would send it twice). Nothing
                    # more is sent; the save is tried again below.
                    busy, save_error, stopped_by_save = True, exc, True
                    result = _merge_result(result, {key: written_mine[key]}, {}, [])
            finished = True
        finally:
            requests = list(earlier.get("requests") or []) + masker.data(client.requests)
            client.close()
            # Already busy with nothing unsaved: a short wait (the outcome is known). Entries written but
            # not saved: a last chance a little past the budget.
            wait = lock_wait(SAVE_GRACE) if unsaved or not busy else min(1.0, lock_wait())
            try:
                result = save(wait, requests)
                _unsaved.pop(unsaved_key, None)
                unsaved.clear()
                save_error = None  # the earlier failed save is made good: everything written is recorded
            except (ToolFailure, OSError) as exc:
                # Kept in this process: the next call saves them and does not send them again.
                if written_mine or failed_mine:
                    _unsaved[unsaved_key] = {"written": dict(written_mine), "failed": dict(failed_mine),
                                             "requests": requests[-MAX_RECORDS:]}
                else:  # nothing written or refused in this process: nothing to keep
                    _unsaved.pop(unsaved_key, None)
                result = _merge_result(result, written_mine, failed_mine, [])
                log.warning("書き戻しの記録を保存できませんでした: %s: %s", result_path, exc)
                if finished and not unsaved and not isinstance(exc, FileBusy):
                    raise
                if unsaved:
                    busy, save_error = True, exc
    keys = {entry_key(e) for e in items}
    failed = [{"id": v["id"], "cids": v["cids"], "error": v["error"]} for k, v in result["failed"].items() if k in keys]
    unrecorded = [written_mine[k]["id"] for k in unsaved if k in keys]
    done = not remaining and not unrecorded
    out: dict[str, Any] = {
        "done": done, "written": written_now, "written_total": sum(1 for k in result["written"] if k in keys),
        "entries": len(items), "failed": failed, "remaining": remaining, "result_file": str(result_path),
        "seconds": round(deadline.elapsed(), 1), "next": None if done else CALL_AGAIN}
    if busy and not done:
        if unrecorded and save_error is not None and not isinstance(save_error, FileBusy):
            note = f"記録のファイルに保存できませんでした（{masker.text(str(save_error))}）。"
        elif stopped_by_save and not unrecorded:
            note = "書いた項目を記録のファイルにすぐ保存できなかったので、残りの項目はまだ送っていません。"
        else:
            note = "ほかのセッションが同じ Coverity に書き戻していて、記録のファイルが空きませんでした。"
        if unrecorded:
            note += (f"{', '.join(unrecorded)} は Coverity に書きましたが、まだ記録していません。このセッションで"
                     "もう一度呼ぶと、送り直さずに記録します。")
        out["busy_note"] = note + "少し待ってから、同じ引数でもう一度呼んでください"
    if failed:
        out["failed_note"] = ("failed の項目は Coverity に断られました。同じ確認の文字列では送り直しません。値を直して"
                              "coverity_preview_triage からやり直してください")
    return out
