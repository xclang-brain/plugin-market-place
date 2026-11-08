"""Data exchanged with Coverity Connect, and the write-back entries."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Event(BaseModel):
    """One step of the warning path."""

    file: str
    line: int | None = None
    tag: str = ""
    description: str = ""
    main: bool = False


class Issue(BaseModel):
    """Basic information of one CID (the fields of issues.json)."""

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
    """Issue plus warning path and checker documentation.

    ``found_in`` lists the streams, among those searched, that hold the CID (``issue.stream`` is the
    one the warning path comes from)."""

    issue: Issue
    events: list[Event] = Field(default_factory=list)
    checker_description: str = ""
    found_in: list[str] = Field(default_factory=list)


class StreamInfo(BaseModel):
    """A stream the user can see, its project and the triage store it uses."""

    name: str
    project: str | None = None
    triage_store: str | None = None


class TriageAttributes(BaseModel):
    classification: str
    action: str
    severity: str = ""


class TriageEntry(BaseModel):
    """書き戻す1項目。ほかの鍵は受け付けない。"""

    # Other keys are refused, so that a misspelt key (for example ``comments``) is not dropped
    # silently. ``stream`` is left out when not given, so that the confirmation token and the key of
    # an entry without a stream do not change.
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="項目の ID（CID か、グループの名前。例: \"20001\"、\"G1\"）")
    cids: list[int] = Field(min_length=1, description="書き戻す CID（例: [20001]、[20005, 20006]）")
    stream: str | None = Field(default=None, min_length=1,
                               description="警告のあるストリームの名前（例: \"app-main\"）。そのストリームが使う"
                                           "トリアージストアに書く。省くと設定のトリアージストア")
    classification: str = Field(min_length=1, description="Classification（例: \"False Positive\"、\"Intentional\"、\"Bug\"）")
    action: str = Field(min_length=1, description="Action（例: \"Ignore\"、\"Fix Required\"）")
    severity: str = Field(default="", description="Severity（例: \"Unspecified\"、\"Major\"）。空なら書かない")
    comment: str = Field(default="", description="コメント（例: \"誤検知。…\"）。空なら書かない")


class PlanEntry(TriageEntry):
    """書き戻しの計画のファイルの1項目。知らない鍵は読み飛ばす。"""

    model_config = ConfigDict(extra="ignore")

    @field_validator("stream", mode="before")
    @classmethod
    def _empty_stream(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value
