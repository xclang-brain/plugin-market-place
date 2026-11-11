"""What the four tools do, as plain functions (server.py runs them in a worker thread).

One call returns within about 20 seconds, because MCP clients stop a tool call after 30. Work that does
not fit is answered with ``done: false`` and continued by the same call: what is already saved in a file
is not fetched or written again. That can happen only with ``output_file``, ``output_dir`` and the write
tool; a reply without a file is sized to end in one call.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path

from .connect import INVALID_KEY, PAGE, Client, Refused, to_int, unknown_stream
from .settings import Failure, Settings, absolute, load

BUDGET = 20.0  # seconds one tool call may use
FIRST_REQUEST = 5.0  # seconds expected for a request before one is measured (a production server took 4.6)
INLINE_ISSUES = 100  # warnings in a search reply without a file
INLINE_CIDS = 10  # CIDs in a details reply without files
LISTED_STREAMS = 100  # stream names in a check reply
ENTRY_FIELDS = ("classification", "action", "severity", "comment")


class Deadline:
    def __init__(self) -> None:
        self.started = time.monotonic()

    def allows(self, client: Client) -> bool:
        """Whether one more request is expected to end within the budget of this call."""
        return time.monotonic() - self.started + (client.slowest or FIRST_REQUEST) <= BUDGET


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path):
    """The JSON of a file (a BOM is accepted); None when there is no file."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Failure(f"ファイルを読み取れません: {path.as_posix()}（{exc}）。ファイルの内容を確認してください") from None


def write_json(path: Path, data) -> None:
    """Through a temporary file and a rename, so that a reader never sees half a file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: a virus scanner or an editor holds the file
                if attempt == 9:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise Failure(f"ファイルを保存できませんでした: {path.as_posix()}（{exc}）。"
                      "フォルダーがあること、書き込めることを確認してください") from None


# ---- check ----------------------------------------------------------------------------------


def check_connection(repo_root: str) -> dict:
    streams: list[str] = []
    try:
        settings = load(repo_root)
        with Client(settings.url) as client:
            streams = client.streams()
        if settings.stream not in streams:
            raise unknown_stream(settings.stream)
        ok, message = True, f"接続できました。ストリーム {settings.stream} を確認しました"
    except Failure as exc:
        ok, message = False, str(exc)
    return {"ok": ok, "message": message, "streams": streams[:LISTED_STREAMS]}


# ---- search ---------------------------------------------------------------------------------


@dataclass
class Search:
    """The pages read so far. A search into a file is kept here until it is complete."""

    checkers: list[str]  # the names the server filters by (one it does not know is taken out)
    issues: dict[int, dict] = field(default_factory=dict)  # CID -> warning, in the order of the server
    offset: int = 0
    total: int | None = None  # warnings that match (None: more than the limit, and the number is not known)
    complete: bool = False


_searches: dict[tuple, Search] = {}
_search_lock = threading.Lock()  # one search at a time: two calls for one file must not read the same pages


def _read_pages(client: Client, settings: Settings, state: Search, limit: int, deadline: Deadline) -> bool:
    """Read pages until the search is complete (True) or the time of this call is used up (False)."""
    # Wildcard names are matched here; the server filters everything else.
    wild = any(char in name for name in settings.checkers for char in "*?[")
    rows_wanted = PAGE if wild else min(PAGE, limit + 1)
    while not state.complete:
        if not deadline.allows(client):
            return False
        filters = {"checkers": [] if wild else state.checkers, "impacts": settings.impacts,
                   "classification": settings.classification}
        try:
            rows, total = client.search(settings.stream, filters, state.offset, rows_wanted)
        except Refused as exc:
            if exc.status == 404:  # some servers answer a stream they do not have with 404
                client.stream(settings.stream)
            unknown = INVALID_KEY.search(exc.text)
            if not unknown:
                raise
            if unknown.group(1) not in filters["checkers"]:
                raise Failure(f"設定ファイルの conditions に、Coverity にない値があります: {unknown.group(1)}。"
                              "conditions.impacts と conditions.classification を確認してください") from None
            # A checker without any warning on the server is not a mistake: no warning can have it.
            state.checkers.remove(unknown.group(1))
            if not state.checkers:
                state.total, state.complete = 0, True
            continue
        for row in rows:
            cid, checker = to_int(row.get("cid")), str(row.get("checker") or "")
            if cid is None or cid in state.issues:  # pages can overlap while the warnings change
                continue
            if wild and not any(fnmatchcase(checker, name) for name in settings.checkers):
                continue
            state.issues[cid] = {"cid": cid, "checker": checker, "file": str(row.get("displayFile") or ""),
                                 "line": to_int(row.get("lineNumber")),
                                 "function": str(row.get("displayFunction") or "").strip() or None,
                                 "impact": str(row.get("displayImpact") or "").strip() or None}
        state.offset += len(rows)
        found = len(state.issues)
        if not rows or (total is not None and state.offset >= total):  # every row was read
            state.total, state.complete = found, True
        elif found > limit or (found >= limit and not wild and total is not None):
            state.total, state.complete = (None if wild else total), True
    return True


def search_issues(repo_root: str, output_file: str | None = None) -> dict:
    deadline = Deadline()
    settings = load(repo_root)
    out = absolute(output_file, "output_file") if output_file else None
    limit = settings.limit if out is not None else min(settings.limit, INLINE_ISSUES)
    key = (str(out), settings.stream, json.dumps(settings.conditions, sort_keys=True))
    with _search_lock:
        # A complete list is never replaced. Looked at under the lock: a second call for the same file waits
        # for the first one and then answers from the file.
        if out is not None and out.exists():
            saved = read_json(out)
            if not isinstance(saved, dict) or not isinstance(saved.get("issues"), list):
                raise Failure(f"output_file は警告の一覧ではありません: {out.as_posix()}。別のパスを指定してください")
            return {"done": True, "count": len(saved["issues"]), "total_matched": saved.get("total_matched"),
                    "limited": bool(saved.get("limited")), "output_file": out.as_posix()}
        state = Search(list(settings.checkers))
        if out is not None:
            state = _searches.setdefault(key, state)
        with Client(settings.url) as client:
            done = _read_pages(client, settings, state, limit, deadline)
            if done and not state.issues:
                client.stream(settings.stream)  # a stream Coverity does not have is an error, not "no warnings"
        if not done:
            if out is None:
                raise Failure(f"警告の検索が {BUDGET:.0f} 秒以内に終わりませんでした。"
                              "output_file を指定して、もう一度呼び出してください")
            return {"done": False, "count": min(len(state.issues), limit), "total_matched": None, "limited": False,
                    "output_file": out.as_posix()}
        issues = sorted(list(state.issues.values())[:limit], key=lambda issue: issue["cid"])
        limited = state.total is None or state.total > limit
        counts = {"count": len(issues), "total_matched": state.total, "limited": limited}
        if out is None:
            return {"done": True, "stream": settings.stream, **counts, "issues": issues}
        write_json(out, {"stream": settings.stream, "conditions": settings.conditions, "fetched_at": now(),
                         "total_matched": state.total, "limited": limited, "issues": issues})
        del _searches[key]
        return {"done": True, **counts, "output_file": out.as_posix()}


# ---- events ---------------------------------------------------------------------------------


def get_issue_details(repo_root: str, cids: list[int], output_dir: str | None = None) -> dict:
    deadline = Deadline()
    settings = load(repo_root)
    folder = absolute(output_dir, "output_dir") if output_dir else None
    wanted = list(dict.fromkeys(cids))
    if folder is None and len(wanted) > INLINE_CIDS:
        raise Failure(f"output_dir を指定しないときは、cids は {INLINE_CIDS} 件までです。"
                      "output_dir を指定して、もう一度呼び出してください")
    saved: list[int] = []
    issues: list[dict] = []
    failed: dict[str, str] = {}
    remaining: list[int] = []
    with Client(settings.url) as client:
        for cid in wanted:
            file = folder / str(cid) / "issue.json" if folder is not None else None
            if file is not None and file.is_file():  # not fetched again
                saved.append(cid)
                continue
            if not deadline.allows(client):
                if file is not None:
                    remaining.append(cid)
                else:
                    failed[str(cid)] = "時間内に取得できませんでした。この CID を指定して、もう一度呼び出してください"
                continue
            try:
                detail = client.events(cid, settings.stream)
            except Refused as exc:
                failed[str(cid)] = exc.reason
                continue
            if detail is None:  # no file is written, so that the next call tries again
                failed[str(cid)] = f"ストリーム {settings.stream} に CID {cid} がありません"
            elif file is None:
                issues.append({"cid": cid, **detail})
            else:
                file.parent.mkdir(parents=True, exist_ok=True)
                write_json(file, {"cid": cid, "fetched_at": now(), **detail})
                saved.append(cid)
    if folder is None:
        return {"done": True, "issues": issues, "failed": failed}
    return {"done": not remaining, "saved": saved, "failed": failed, "remaining": remaining,
            "output_dir": folder.as_posix()}


# ---- triage ---------------------------------------------------------------------------------

_write_lock = threading.Lock()  # one write call at a time: written.json has one writer


def _entries(path: Path) -> tuple[str, list[dict]]:
    """Stream and entries of the file apply.py wrote, when it is still the file the person agreed to."""
    again = "もう一度、一覧の確認からやり直してください"
    if not path.is_file():
        raise Failure(f"登録する内容のファイルがありません: {path.as_posix()}。{again}")
    raw = path.read_bytes()
    try:
        recorded = Path(f"{path}.sha256").read_text(encoding="ascii").strip().lower()
    except (OSError, ValueError):
        recorded = ""
    if recorded != hashlib.sha256(raw).hexdigest():
        raise Failure(f"登録する内容のファイルが、作成された後に変更されています。{again}")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        stream, entries = data["stream"], data["entries"]
        seen: set[int] = set()
        for entry in entries:
            cids = entry["cids"]
            if (not cids or any(type(cid) is not int for cid in cids) or seen & set(cids)
                    or any(not isinstance(entry[name], str) or not entry[name].strip() for name in ENTRY_FIELDS)):
                raise ValueError
            seen |= set(cids)  # a CID may be in one entry only
        if not isinstance(stream, str) or not stream.strip():
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise Failure(f"登録する内容のファイルの形式が正しくありません: {path.as_posix()}。{again}") from None
    return stream, entries


def update_triage(repo_root: str, entries_file: str) -> dict:
    deadline = Deadline()
    settings = load(repo_root)
    stream, entries = _entries(absolute(entries_file, "entries_file"))
    written_file = Path(entries_file).with_name("written.json")
    written: list[int] = []
    skipped: list[int] = []
    failed: dict[str, str] = {}
    remaining: list[int] = []
    with _write_lock, Client(settings.url) as client:
        record = read_json(written_file) or {"written": {}}
        if not isinstance(record, dict) or not isinstance(record.get("written"), dict):
            raise Failure(f"ファイルを読み取れません: {written_file.as_posix()}（written がありません）。"
                          "ファイルの内容を確認してください")
        store = ""
        for entry in entries:
            todo = [cid for cid in entry["cids"] if str(cid) not in record["written"]]
            skipped += [cid for cid in entry["cids"] if cid not in todo]  # written before: never again
            if not todo:
                continue
            if not deadline.allows(client):
                remaining += todo
                continue
            if not store:  # the store the stream uses: another one would change the triage of other streams
                store = str(client.stream(stream).get("triageStoreName") or "").strip()
                if not store:
                    raise Failure(f"ストリーム {stream} のトリアージストアを取得できませんでした。"
                                  "Coverity の管理者に確認してください")
            try:
                client.write_triage(store, todo, entry)
            except Refused as exc:  # this entry only; the others are still written
                failed.update({str(cid): exc.reason for cid in todo})
                continue
            values = {**{name: entry[name] for name in ENTRY_FIELDS}, "at": now()}
            record["written"].update({str(cid): values for cid in todo})
            write_json(written_file, record)
            written += todo
    return {"done": not remaining, "written": written, "skipped": skipped, "failed": failed,
            "remaining": remaining, "written_file": written_file.as_posix()}
