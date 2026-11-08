"""What the server reads: the Coverity connection and a search filter.

Settings come from ``<repo_root>/.coverity-triage/config.yaml`` (its ``coverity`` section only;
other sections and unknown keys are ignored, and the unknown keys of ``coverity`` are reported by
the connection check), or, without a repo_root, from environment variables (``COVERITY_URL``,
``COVERITY_TRIAGE_STORE``, ``COVERITY_CA_FILE``, ``COVERITY_API``, ``COVERITY_FAKE_DATA``).

A filter is a file (``filters/*.yaml`` or ``filter.json``; unknown keys are skipped) or an object
passed as a tool argument (unknown keys are refused). Values inside one key are OR-ed, keys are
AND-ed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from .envvars import get_env

CONFIG_DIR = ".coverity-triage"
CONFIG_PATH = Path(CONFIG_DIR) / "config.yaml"
ENV_SOURCE = "環境変数"

# Known values, matched without regard to case ("high" -> "High"). Other values are kept as given:
# a server can have more (for example the Test Advisor classifications).
IMPACTS = ("High", "Medium", "Low", "Unspecified", "Audit")
CLASSIFICATIONS = ("Unclassified", "Pending", "False Positive", "Intentional", "Bug")
ACTIONS = ("Undecided", "Fix Required", "Fix Submitted", "Modeling Required", "Ignore")
STATUSES = ("New", "Triaged", "Dismissed", "Absent Dismissed", "Fixed")


class SettingsError(Exception):
    """A settings or filter file is missing or invalid."""


def _canonical(values: list[str], known: tuple[str, ...]) -> list[str]:
    by_lower = {k.lower(): k for k in known}
    out: list[str] = []
    for value in values:
        value = by_lower.get(value.strip().lower(), value.strip())
        if value and value not in out:
            out.append(value)
    return out


class CoverityConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = ""
    api: Literal["auto", "fake"] = "auto"
    user_env: str = Field(default="COV_USER", min_length=1)
    key_env: str = Field(default="COV_AUTH_KEY", min_length=1)
    triage_store: str = Field(default="Default Triage Store", min_length=1)
    # Element of the SOAP snapshot information that holds the analyzed revision; empty: not read.
    revision_field: str | None = "sourceVersion"
    path_strip_prefixes: list[str] = Field(default_factory=list)  # not used here; read by other tools
    ca_file: str | None = None  # CA certificate file; empty means the OS certificate store
    fake_data: str | None = None  # issues YAML for api: fake
    # Where the values came from, and the keys of the section this server does not know.
    _source: str = PrivateAttr(default=ENV_SOURCE)
    _unknown_keys: list[str] = PrivateAttr(default_factory=list)

    @property
    def source(self) -> str:
        return self._source

    @property
    def unknown_keys(self) -> list[str]:
        return list(self._unknown_keys)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = (value or "").strip().rstrip("/")
        if value and not (value.lower().startswith("https://") or value.lower().startswith("http://")):
            raise ValueError("url は https:// か http:// で始めてください（例: https://coverity.example.com:8443）")
        return value

    @field_validator("revision_field", "ca_file", "fake_data", mode="before")
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        return None if value is None or (isinstance(value, str) and not value.strip()) else value

    @model_validator(mode="after")
    def _check(self) -> "CoverityConfig":
        if self.user_env == self.key_env:
            raise ValueError("user_env と key_env に同じ名前は使えません（ユーザ名と認証キーを別の環境変数に入れます）")
        if self.api == "fake" and not self.fake_data:
            raise ValueError("api が fake のときは fake_data（偽の警告のファイル）を書いてください")
        if self.api != "fake" and not self.url:
            raise ValueError("url（Coverity Connect の URL。例: https://coverity.example.com:8443）を書いてください")
        return self


KNOWN_KEYS = frozenset(CoverityConfig.model_fields)


def _errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        message = str(err.get("msg", "")).removeprefix("Value error, ")
        lines.append(f"- {where}: {message}" if where else f"- {message}")
    return "\n".join(lines)


def _resolve(value: str | None, base: Path) -> str | None:
    if not value:
        return value
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


def load_from_repo(repo_root: Path) -> CoverityConfig:
    path = repo_root / CONFIG_PATH
    if not path.is_file():
        raise SettingsError(f"設定ファイルがありません: {path}。このファイルを作って coverity.url（Coverity Connect の URL）を"
                            "書いてください。リポジトリなしで使うときは、repo_root を渡さずに、環境変数 COVERITY_URL に"
                            "URL を設定してください")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise SettingsError(f"設定ファイルの書式が正しくありません: {path}: {exc}") from exc
    section = data.get("coverity") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise SettingsError(f"設定ファイルに coverity の項目がありません: {path} に coverity.url を書いてください")
    try:
        config = CoverityConfig.model_validate(section)
    except ValidationError as exc:
        raise SettingsError(f"設定ファイルの coverity の値が正しくありません: {path}\n{_errors(exc)}") from exc
    config._source = str(path)
    config._unknown_keys = sorted(str(k) for k in section if k not in KNOWN_KEYS)
    config.fake_data = _resolve(config.fake_data, path.parent)
    config.ca_file = _resolve(config.ca_file, path.parent)
    return config


ENV_KEYS = {"url": "COVERITY_URL", "triage_store": "COVERITY_TRIAGE_STORE", "ca_file": "COVERITY_CA_FILE",
            "api": "COVERITY_API", "fake_data": "COVERITY_FAKE_DATA"}


def load_from_env() -> CoverityConfig:
    values: dict[str, Any] = {}
    for key, name in ENV_KEYS.items():
        value = get_env(name)
        if value is not None:
            values[key] = value.strip()
    if values.get("api") not in (None, "auto", "fake"):
        raise SettingsError(f"環境変数 COVERITY_API は auto か fake にしてください（今の値: {values['api']}）")
    for key in ("fake_data", "ca_file"):
        if values.get(key) and not Path(values[key]).expanduser().is_absolute():
            raise SettingsError(f"環境変数 {ENV_KEYS[key]} は絶対パスで書いてください（今の値: {values[key]}）")
    if not values.get("url") and values.get("api") != "fake":
        raise SettingsError("Coverity の URL がわかりません。repo_root（.coverity-triage/config.yaml のあるリポジトリの"
                            "絶対パス）を渡すか、環境変数 COVERITY_URL に Coverity Connect の URL を設定してください")
    try:
        config = CoverityConfig.model_validate(values)
    except ValidationError as exc:
        raise SettingsError(f"環境変数の Coverity の設定が正しくありません\n{_errors(exc)}") from exc
    return config


def load_config(repo_root: Path | None) -> CoverityConfig:
    return load_from_repo(repo_root) if repo_root is not None else load_from_env()


# ---- filters ------------------------------------------------------------------------------------

TRIAGE_FIELDS = {"classification": CLASSIFICATIONS, "action": ACTIONS, "status": STATUSES}


class TriageFilter(BaseModel):
    """トリアージの値の条件。"""

    model_config = ConfigDict(extra="forbid")

    classification: list[str] = Field(default_factory=list,
                                      description="Classification のどれか（例: [\"Unclassified\"]、[\"Bug\", \"Pending\"]）")
    action: list[str] = Field(default_factory=list,
                              description="Action のどれか（例: [\"Undecided\"]、[\"Fix Required\"]）")
    status: list[str] = Field(default_factory=list,
                              description="Status のどれか（例: [\"New\", \"Triaged\"]）。Fixed か Absent Dismissed を入れると、"
                                          "前のスナップショットの警告も探す")

    @model_validator(mode="after")
    def _canonical_values(self) -> "TriageFilter":
        for name, known in TRIAGE_FIELDS.items():
            setattr(self, name, _canonical(getattr(self, name), known))
        return self


class FilterSpec(BaseModel):
    """Issue selection read from a file: other keys are skipped."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    project: str | None = None
    streams: list[str] = Field(default_factory=list)
    checkers: list[str] = Field(default_factory=list)
    impacts: list[str] = Field(default_factory=list)
    triage: TriageFilter = Field(default_factory=TriageFilter)
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _canonical_values(self) -> "FilterSpec":
        self.impacts = _canonical(self.impacts, IMPACTS)
        self.checkers = [c.strip() for c in self.checkers if c and c.strip()]
        self.streams = [s.strip() for s in self.streams if s and s.strip()]
        return self


class FilterArg(FilterSpec):
    """警告の条件。同じ項目の値はどれか、項目どうしはすべてに合う警告を探す。ほかの項目は受け付けない。"""

    # Other keys are refused, so that a misspelt key (for example ``status`` outside ``triage``, or
    # ``checker``) does not widen the search silently.

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", description="表示の名前（省略可）")
    project: str | None = Field(default=None, description="プロジェクト（省略可。検索の絞り込みには使わない）")
    streams: list[str] = Field(default_factory=list,
                               description="ストリームの名前をちょうど1つ（必須。例: [\"app-main\"]）")
    checkers: list[str] = Field(default_factory=list,
                                description="チェッカーのどれか。* ? [ ] のワイルドカード可"
                                            "（例: [\"NULL_RETURNS\", \"MISRA C-2012 Rule 10.*\"]）")
    impacts: list[str] = Field(default_factory=list,
                               description="Impact のどれか: High / Medium / Low / Unspecified（例: [\"High\"]）")
    triage: TriageFilter = Field(default_factory=TriageFilter,
                                 description="トリアージの値（classification / action / status）")
    limit: int | None = Field(default=None, ge=1,
                              description="output_file に書くときの件数の上限（既定 100）。会話に返すときは使わない"
                                          "（1ページの件数は引数の limit）")


def _one_stream(spec: FilterSpec, where: str) -> FilterSpec:
    if len(spec.streams) != 1:
        raise SettingsError(f"{where}の streams に、ストリームの名前をちょうど1つ書いてください"
                            f"（今は {len(spec.streams)} 個。例: {{\"streams\": [\"app-main\"]}}）")
    return spec


def load_filter(path: Path) -> tuple[FilterSpec, bytes, list[str]]:
    """A filter from a YAML or JSON file, its bytes, and the keys that were skipped."""
    if not path.is_file():
        raise SettingsError(f"条件のファイルがありません: {path}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise SettingsError(f"条件のファイルを読めません: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"条件のファイルの中身が、項目の並び（streams、checkers など）ではありません: {path}")
    try:
        spec = FilterSpec.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(f"条件のファイルの値が正しくありません: {path}\n{_errors(exc)}") from exc
    known = set(FilterSpec.model_fields)
    skipped = sorted(str(k) for k in data if k not in known)
    return _one_stream(spec, f"条件のファイル {path} "), raw, skipped


def parse_filter(data: FilterArg | dict) -> FilterSpec:
    """A filter passed as a tool argument."""
    try:
        spec = data if isinstance(data, FilterSpec) else FilterArg.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(f"条件（filter）が正しくありません\n{_errors(exc)}") from exc
    return _one_stream(spec, "条件（filter）")
