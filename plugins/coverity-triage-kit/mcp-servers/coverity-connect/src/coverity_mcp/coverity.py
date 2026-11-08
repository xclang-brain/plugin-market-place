"""Access to Coverity: the client interface, the filter checks, and the fake client.

:class:`CoverityClient` is what the tools use. :mod:`.connect` talks to Coverity Connect;
:class:`FakeCoverityClient` reads issues from a local YAML file (``api: fake``: trials and tests).
"""

from __future__ import annotations

import fnmatch
import json
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import Event, Issue, IssueDetail, TriageAttributes
from .settings import STATUSES, CoverityConfig, FilterSpec


class CoverityError(Exception):
    """Coverity cannot be reached or returned an error.

    ``fatal`` is True when the whole call has to stop (authentication, 5xx, a redirect, an answer
    that is not JSON or SOAP, a connection failure): the next request would fail the same way.
    It is False when only this request failed (for example one CID is not in the stream, or a value
    the server refused with a 4xx). ``status`` is the HTTP status of a REST answer, if any.
    """

    def __init__(self, message: str, fatal: bool = True, status: int | None = None):
        super().__init__(message)
        self.fatal = fatal
        self.status = status


def filter_mismatches(issue: Issue, spec: FilterSpec, project: str | None = None,
                      skip: frozenset[str] = frozenset()) -> list[str]:
    """Filter keys the issue does not match. Values inside one key are OR-ed, keys are AND-ed.
    Keys in ``skip`` (already applied by the server) are not checked."""
    def any_of(value: str | None, allowed: list[str]) -> bool:
        return not allowed or (value is not None and value in allowed)

    checks = {
        "project": not spec.project or project is None or project == spec.project,
        "checkers": not spec.checkers or any(fnmatch.fnmatchcase(issue.checker, p) for p in spec.checkers),
        "streams": any_of(issue.stream, spec.streams),
        "impacts": any_of(issue.impact, spec.impacts),
        "status": any_of(issue.status, spec.triage.status),
        "classification": any_of(issue.classification, spec.triage.classification),
        "action": any_of(issue.action, spec.triage.action),
    }
    return [key for key, ok in checks.items() if not ok and key not in skip]


def issue_value(issue: Issue, key: str) -> Any:
    field = {"checkers": "checker", "impacts": "impact", "streams": "stream"}.get(key, key)
    return getattr(issue, field, None)


class SearchPage:
    """Issues found so far, the total the server reported, and where to continue.

    ``excluded`` lists the rows the server returned that the checks after the search left out:
    ``{"cid", "mismatched": {filter key: the issue's value}}``. ``ends[i]`` is the offset just after
    the row of ``issues[i]`` and ``excluded_ends[i]`` the one after the row of ``excluded[i]``: a caller
    that keeps only the first issues continues from there. ``ignored`` lists the filter values the
    server did not know and that were left out: ``{"filter", "value"}``.
    """

    def __init__(self, issues: list[Issue], total: int | None, next_offset: int | None,
                 excluded: list[dict[str, Any]] | None = None, ends: list[int] | None = None,
                 excluded_ends: list[int] | None = None, ignored: list[dict[str, str]] | None = None):
        self.issues = issues
        self.total = total
        self.next_offset = next_offset  # None when the search is complete
        self.excluded = excluded or []
        self.ends = ends if ends is not None else []
        self.excluded_ends = excluded_ends if excluded_ends is not None else []
        self.ignored = ignored or []


# The status of an issue an earlier snapshot has and the latest one does not.
ABSENT_STATUSES = frozenset({"Fixed", "Absent Dismissed"})


def unknown_statuses(spec: FilterSpec) -> list[dict[str, str]]:
    """Status values no issue can have. The status is checked after the search, so the server
    does not report them."""
    return [{"filter": "triage.status", "value": v} for v in spec.triage.status if v not in STATUSES]


class CoverityClient(ABC):
    # Seconds one request is expected to take before any has been measured.
    FIRST_REQUEST_SECONDS = 0.0

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request_seconds(self) -> float:
        """Expected seconds for the next request: the slowest one so far."""
        return max((r["seconds"] for r in self.requests if isinstance(r.get("seconds"), (int, float))),
                   default=self.FIRST_REQUEST_SECONDS)

    def close(self) -> None:
        pass

    @abstractmethod
    def search(self, spec: FilterSpec, limit: int, offset: int, seconds: float) -> SearchPage:
        """Issues matching the filter from ``offset``. Stops at ``limit`` issues, or before a request
        that would end later than ``seconds``."""

    @abstractmethod
    def issue_detail(self, cid: int, stream: str) -> IssueDetail:
        """Warning path (events) and checker documentation."""

    @abstractmethod
    def snapshot_revision(self, stream: str, field: str) -> str | None:
        """git commit / svn revision recorded in the latest snapshot, if any."""

    @abstractmethod
    def write_triage(self, cids: list[int], attributes: TriageAttributes, comment: str,
                     stream: str | None = None) -> str | None:
        """Register classification, action, severity and comment in the triage store ``stream`` uses
        (without a stream, the one of the settings). Returns the store written, if known."""


class FakeCoverityClient(CoverityClient):
    """Issues from a YAML file (see the README for its format). Write-backs are appended to
    ``<file>.writes.jsonl``."""

    _lock = threading.Lock()

    def __init__(self, path: str | Path, triage_store: str = "Default Triage Store"):
        super().__init__()
        self.path = Path(path)
        self.triage_store = triage_store
        if not self.path.is_file():
            raise CoverityError(f"偽の警告のファイルがありません: {self.path}（coverity.fake_data か環境変数 "
                                "COVERITY_FAKE_DATA を確かめてください）")
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8-sig")) or {}
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise CoverityError(f"偽の警告のファイルの書式が正しくありません: {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("issues") or [], list):
            raise CoverityError(f"偽の警告のファイルに issues の並びがありません: {self.path}")
        self.data = data
        self.records: list[dict[str, Any]] = []
        self.issues: list[Issue] = []
        for index, record in enumerate(data.get("issues") or []):
            try:
                issue = Issue.model_validate({k: v for k, v in record.items() if k in Issue.model_fields})
            except (AttributeError, ValidationError) as exc:
                raise CoverityError(f"偽の警告のファイルの issues の {index + 1} 番目が正しくありません"
                                    f"（cid・checker・file は必須）: {self.path}: {exc}") from exc
            self.records.append(record)
            self.issues.append(issue)

    def streams(self) -> dict[str, dict[str, Any]]:
        known: dict[str, dict[str, Any]] = {i.stream: {} for i in self.issues if i.stream}
        extra = self.data.get("streams") or {}
        if isinstance(extra, dict):
            for name, value in extra.items():
                known[str(name)] = value if isinstance(value, dict) else {}
        return known

    def search(self, spec: FilterSpec, limit: int, offset: int, seconds: float) -> SearchPage:
        # Like the real server: the latest snapshot holds no Fixed or Absent Dismissed issue, so they
        # are found only when the status filter asks for them.
        every_snapshot = bool(ABSENT_STATUSES & set(spec.triage.status))
        found = [issue for issue, record in zip(self.issues, self.records, strict=True)
                 if not filter_mismatches(issue, spec, record.get("project"))
                 and (every_snapshot or issue.status not in ABSENT_STATUSES)]
        end = offset + limit
        page = found[offset:end]
        return SearchPage(page, len(found), end if end < len(found) else None,
                          ends=[offset + i + 1 for i in range(len(page))], ignored=unknown_statuses(spec))

    def issue_detail(self, cid: int, stream: str) -> IssueDetail:
        for issue, record in zip(self.issues, self.records, strict=True):
            if issue.cid != cid:
                continue
            if issue.stream and issue.stream != stream:
                continue
            events = []
            for event in record.get("events") or []:
                try:
                    events.append(Event.model_validate(event))
                except ValidationError as exc:
                    raise CoverityError(f"偽の警告のファイルの CID {cid} の events が正しくありません: {exc}",
                                        fatal=False) from exc
            return IssueDetail(issue=issue, events=events,
                               checker_description=str(record.get("checker_description") or ""))
        raise CoverityError(f"CID {cid} の警告経路がストリーム {stream} にありません", fatal=False)

    def snapshot_revision(self, stream: str, field: str) -> str | None:
        snapshot = self.data.get("snapshot") or {}
        value = snapshot.get(field) if isinstance(snapshot, dict) else None
        return str(value) if value not in (None, "") else None

    def write_triage(self, cids: list[int], attributes: TriageAttributes, comment: str,
                     stream: str | None = None) -> str | None:
        store = self.triage_store
        if stream:
            streams = self.streams()
            if stream not in streams:
                raise CoverityError(f"ストリーム {stream} が Coverity にありません。書き戻しの項目の stream に、"
                                    "警告のあるストリームの名前を書いてください", fatal=False, status=404)
            store = str(streams[stream].get("triage_store") or self.triage_store)
        record: dict[str, Any] = {"cids": list(cids), **attributes.model_dump(), "comment": comment}
        if stream:
            record["stream"] = stream
        record.update({"triage_store": store, "at": datetime.now().isoformat(timespec="seconds")})
        with self._lock, open(str(self.path) + ".writes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return store


def make_client(config: CoverityConfig) -> CoverityClient:
    if config.api == "fake":
        return FakeCoverityClient(config.fake_data or "", config.triage_store)
    from .connect import ConnectClient  # noqa: PLC0415 - only needed with a real server
    return ConnectClient(config)
