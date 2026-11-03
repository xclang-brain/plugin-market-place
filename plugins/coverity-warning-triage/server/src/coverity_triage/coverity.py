"""Access to Coverity Connect.

:class:`CoverityClient` is what the MCP tools use. :mod:`.connect` talks to Coverity Connect;
:class:`FakeCoverityClient` reads issues from a local YAML file (self-test and automated tests).
"""

from __future__ import annotations

import fnmatch
import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from .models import Event, Issue, IssueDetail, TriageAttributes
from .settings import CoverityConfig, FilterSpec


class CoverityError(Exception):
    """Coverity cannot be reached or returned an error.

    ``fatal`` is False when only this request failed (for example one CID is not in the stream)
    and the next request may still succeed. ``status`` is the HTTP status of a REST answer that
    failed, if any.
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


def filter_matches(issue: Issue, spec: FilterSpec, project: str | None = None) -> bool:
    return not filter_mismatches(issue, spec, project)


class SearchPage:
    """Issues found so far, the total the server reported, and where to continue.

    ``excluded`` lists the rows the server returned that the filter checked after the search left
    out: ``{"cid", "mismatched": {filter key: the issue's value}}``.
    ``ends[i]`` is the offset just after the row of ``issues[i]`` and ``excluded_ends[i]`` the one after
    the row of ``excluded[i]``: a caller that keeps only the first issues continues from there.
    """

    def __init__(self, issues: list[Issue], total: int | None, next_offset: int | None,
                 excluded: list[dict[str, Any]] | None = None, ends: list[int] | None = None,
                 excluded_ends: list[int] | None = None):
        self.issues = issues
        self.total = total
        self.next_offset = next_offset  # None when the search is complete
        self.excluded = excluded or []
        self.ends = ends if ends is not None else []
        self.excluded_ends = excluded_ends if excluded_ends is not None else []


class CoverityClient(ABC):
    # Seconds one request is expected to take before any has been measured.
    FIRST_REQUEST_SECONDS = 0.0

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request_seconds(self) -> float:
        """Expected seconds for the next request: the slowest one so far."""
        return max((r["seconds"] for r in self.requests), default=self.FIRST_REQUEST_SECONDS)

    def close(self) -> None:
        pass

    @abstractmethod
    def search(self, spec: FilterSpec, limit: int, offset: int, seconds: float) -> SearchPage:
        """Issues matching the filter from ``offset``.

        Stops at ``limit`` issues, or before a request that would end later than ``seconds``.
        """

    @abstractmethod
    def issue_detail(self, cid: int, stream: str) -> IssueDetail:
        """Warning path (events) and checker documentation."""

    @abstractmethod
    def snapshot_revision(self, stream: str, field: str) -> str | None:
        """git commit / svn revision recorded in the latest snapshot, if any."""

    @abstractmethod
    def write_triage(self, cids: list[int], attributes: TriageAttributes, comment: str) -> None:
        """Register classification, action, severity and comment."""


class FakeCoverityClient(CoverityClient):
    """Issues from a YAML file. Write-backs are appended to ``<file>.writes.jsonl``."""

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        if not self.path.is_file():
            raise CoverityError(f"偽データのファイルが見つかりません: {self.path}")
        self.data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self._lock = threading.Lock()

    def _records(self) -> list[dict]:
        return self.data.get("issues") or []

    @staticmethod
    def _issue(record: dict) -> Issue:
        return Issue.model_validate({k: v for k, v in record.items() if k in Issue.model_fields})

    def search(self, spec: FilterSpec, limit: int, offset: int, seconds: float) -> SearchPage:
        found = [self._issue(r) for r in self._records()
                 if filter_matches(self._issue(r), spec, r.get("project"))]
        end = offset + limit
        page = found[offset:end]
        return SearchPage(page, len(found), end if end < len(found) else None,
                          ends=[offset + i + 1 for i in range(len(page))])

    def issue_detail(self, cid: int, stream: str) -> IssueDetail:
        for record in self._records():
            if record.get("cid") == cid:
                return IssueDetail(issue=self._issue(record),
                                   events=[Event.model_validate(e) for e in record.get("events") or []],
                                   checker_description=record.get("checker_description", ""))
        raise CoverityError(f"CID {cid} が見つかりません", fatal=False)

    def snapshot_revision(self, stream: str, field: str) -> str | None:
        value = (self.data.get("snapshot") or {}).get(field)
        return str(value) if value else None

    def write_triage(self, cids: list[int], attributes: TriageAttributes, comment: str) -> None:
        record = {"cids": cids, **attributes.model_dump(), "comment": comment}
        with self._lock, open(str(self.path) + ".writes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def make_client(config: CoverityConfig) -> CoverityClient:
    if config.api == "fake":
        if not config.fake_data:
            raise CoverityError("coverity.api が fake のときは coverity.fake_data を指定してください")
        return FakeCoverityClient(config.fake_data)
    from .connect import ConnectClient
    return ConnectClient(config)
