"""Data exchanged with Coverity Connect."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Event(BaseModel):
    """One step of the warning path."""

    file: str
    line: int | None = None
    tag: str = ""
    description: str = ""
    main: bool = False


class Issue(BaseModel):
    """Basic information of one CID."""

    cid: int
    checker: str
    file: str
    function: str | None = None
    line: int | None = None
    impact: str | None = None
    category: str | None = None
    cwe: int | None = None
    merge_key: str | None = None
    macro: str | None = None
    stream: str | None = None
    classification: str | None = None
    action: str | None = None
    severity: str | None = None
    status: str | None = None


class IssueDetail(BaseModel):
    """Issue plus warning path and checker documentation."""

    issue: Issue
    events: list[Event] = Field(default_factory=list)
    checker_description: str = ""


class TriageAttributes(BaseModel):
    classification: str
    action: str
    severity: str = ""


class TriageEntry(BaseModel):
    """One write-back: an item of the ``triage`` list of a plan file, or of the ``entries`` argument."""

    id: str
    cids: list[int] = Field(min_length=1)
    classification: str = Field(min_length=1)
    action: str = Field(min_length=1)
    severity: str = ""
    comment: str = ""
