"""What the MCP server reads: the Coverity connection and a search filter.

The settings file is ``<repo_root>/.coverity-triage/config.yaml``. The server reads only its
``coverity`` section and ignores the other sections. A search filter is a JSON file or an object
passed as a tool argument; both have the shape of :class:`FilterSpec`. The file may hold other keys
(they are skipped); the argument may not (:class:`FilterArg`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_PATH = Path(".coverity-triage") / "config.yaml"


class SettingsError(Exception):
    """A settings or filter file is missing or invalid."""


class CoverityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    # auto: Coverity Connect. fake: issues from a local file (self-test and automated tests).
    api: Literal["auto", "fake"] = "auto"
    user_env: str = "COV_USER"
    key_env: str = "COV_AUTH_KEY"
    triage_store: str = "Default Triage Store"
    # Element of the SOAP snapshot information that holds the analyzed revision.
    revision_field: str | None = "sourceVersion"
    path_strip_prefixes: list[str] = Field(default_factory=list)
    # CA certificate file; empty means the OS certificate store.
    ca_file: str | None = None
    fake_data: str | None = None


class TriageFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: list[str] = Field(default_factory=list)
    classification: list[str] = Field(default_factory=list)
    action: list[str] = Field(default_factory=list)


class FilterSpec(BaseModel):
    """Issue selection. Values inside one key are OR-ed, keys are AND-ed. ``project`` is not used to
    search Coverity Connect (the stream decides which issues are searched)."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    project: str | None = None
    streams: list[str] = Field(default_factory=list)
    checkers: list[str] = Field(default_factory=list)
    impacts: list[str] = Field(default_factory=list)
    triage: TriageFilter = Field(default_factory=TriageFilter)
    limit: int | None = Field(default=None, ge=1)


class FilterArg(FilterSpec):
    """Issue selection. Values inside one key are OR-ed, keys are AND-ed. Other keys are refused.
    ``project`` is not used to search Coverity Connect (the stream decides which issues are searched)."""

    # A misspelt key (for example ``status`` outside ``triage``) would otherwise be dropped silently
    # and the search not narrowed.
    model_config = ConfigDict(extra="forbid")


def load_coverity_config(repo_root: str | Path) -> CoverityConfig:
    path = Path(repo_root) / CONFIG_PATH
    if not path.is_file():
        raise SettingsError(f"設定ファイルがありません: {path} を作り、coverity.url（Coverity Connect の URL）を書いてください")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SettingsError(f"設定ファイルの書式が正しくありません: {path}: {exc}") from exc
    section = data.get("coverity") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise SettingsError(f"設定ファイルに coverity の項目がありません: {path} に coverity.url を書いてください")
    try:
        config = CoverityConfig.model_validate(section)
    except ValidationError as exc:
        raise SettingsError(f"設定ファイルの coverity の値が正しくありません: {path}\n{exc}") from exc
    if config.fake_data and not Path(config.fake_data).is_absolute():
        config.fake_data = str((path.parent / config.fake_data).resolve())
    return config


def load_filter(path: str | Path) -> FilterSpec:
    """A filter from a JSON file."""
    file = Path(path)
    if not file.is_file():
        raise SettingsError(f"条件のファイルがありません: {file}")
    try:
        spec = FilterSpec.model_validate(json.loads(file.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as exc:
        raise SettingsError(f"条件のファイルが正しくありません: {file}\n{exc}") from exc
    return _one_stream(spec)


def parse_filter(data: FilterArg | dict) -> FilterSpec:
    """A filter passed as a tool argument (the same shape as the file, without other keys)."""
    try:
        spec = data if isinstance(data, FilterSpec) else FilterArg.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(f"条件（filter）が正しくありません\n{exc}") from exc
    return _one_stream(spec)


def _one_stream(spec: FilterSpec) -> FilterSpec:
    if len(spec.streams) != 1:
        raise SettingsError("条件にはストリームを 1 つだけ指定してください")
    return spec
