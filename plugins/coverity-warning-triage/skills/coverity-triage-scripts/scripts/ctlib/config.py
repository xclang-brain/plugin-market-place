"""Project settings (``.coverity-triage/``): config.yaml, filters, knowledge, CIDs not to group."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .common import CtError, resolve_path

CONFIG_DIR = ".coverity-triage"
CONFIG_FILE = "config.yaml"
FILTERS_DIR = "filters"
KNOWLEDGE_FILE = "knowledge.md"
NO_GROUPING_FILE = "no-grouping.yaml"
DEFAULT_OUTPUT_DIR = "../coverity-triage-out"
DEFAULT_FILTER = "untriaged.yaml"

KNOWLEDGE_TEMPLATE = """# プロジェクトの知識（Coverity トリアージ）

警告を調べる前に AI が読みます。チームで共有するため、コミットしてください。
1 項目 1 行で、コードで確かめられる事実と、判断の方針を書きます。

## 調査で使う事実

<!-- 例：fatal_error()（src/common/error.c）は戻らない（内部で abort する） -->

## 推奨の方針（修正か逸脱か）

<!-- 例：ハードウェアレジスタへのアクセスのためのポインタ変換（MISRA Rule 11.x）は逸脱にする -->

## 逸脱コメントの書き方

<!-- 例：誤検知の根拠には、呼び出し元の関数名と行番号を必ず書く -->
"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CoverityConfig(_Strict):
    """Same fields as the MCP server's ``settings.CoverityConfig`` (a test keeps them equal)."""

    url: str
    api: Literal["auto", "fake"] = "auto"
    user_env: str = "COV_USER"
    key_env: str = "COV_AUTH_KEY"
    triage_store: str = "Default Triage Store"
    revision_field: str | None = "sourceVersion"
    path_strip_prefixes: list[str] = Field(default_factory=list)
    ca_file: str | None = None
    fake_data: str | None = None


class VcsConfig(_Strict):
    type: Literal["git", "svn"]
    base_branch: str = "main"
    branch_prefix: str = "coverity-fix/"


class Options(_Strict):
    annotation: bool = False
    per_run_branch: bool = False
    knowledge_suggestions: bool = False
    metrics: bool = False
    markdown: bool = False  # also write summary.md and cid/<ID>.md next to the HTML reports


VerifyMode = Literal["none", "build", "build+analyze"]


class VerifyConfig(_Strict):
    default: VerifyMode = "none"
    # Initial analysis: at the start of /coverity-run, build and analyze the open folder and commit the
    # result to Coverity Connect (what the person did by hand before), then search the warnings.
    initial: bool = False
    # Run before the build in the same shell; "{root}" becomes the folder being built.
    setup_command: str = ""
    # Folder (relative to the repository) to move into after setup_command.
    build_dir: str = ""
    build_command: str = ""
    cov_build_args: str = "--dir idir"
    cov_analyze_args: str = "--dir idir --all"
    # Added to cov-commit-defects; --dir, --url, --stream, --user and --version are added by the script.
    cov_commit_args: str = ""
    # The initial analysis is stopped when it takes longer (a build that hangs while nobody watches).
    timeout_minutes: int = Field(default=180, ge=1)


class AgentsConfig(_Strict):
    """Optional steps after the worker agents (both off by default, decision 16)."""

    integration: bool = False  # after the work: merge items whose warnings have one cause
    review: bool = False  # after the integration: an independent check of every result
    max_rework: int = Field(default=1, ge=0, le=2)  # times the review may send an item back


DEFAULT_PARALLEL = 3  # independent items are investigated at the same time, each in its own work copy


class ProjectConfig(_Strict):
    coverity: CoverityConfig
    vcs: VcsConfig
    output_dir: str = DEFAULT_OUTPUT_DIR
    max_items: int = Field(default=100, ge=1)
    parallel: int = Field(default=DEFAULT_PARALLEL, ge=1, le=8)
    options: Options = Field(default_factory=Options)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)


class TriageFilter(_Strict):
    status: list[str] = Field(default_factory=list)
    classification: list[str] = Field(default_factory=list)
    action: list[str] = Field(default_factory=list)


class FilterSpec(_Strict):
    """Issue selection. Values inside one key are OR-ed, keys are AND-ed."""

    name: str = ""
    project: str | None = None
    streams: list[str] = Field(default_factory=list)
    checkers: list[str] = Field(default_factory=list)
    impacts: list[str] = Field(default_factory=list)
    triage: TriageFilter = Field(default_factory=TriageFilter)
    limit: int | None = Field(default=None, ge=1)


def config_dir(repo: Path) -> Path:
    return repo / CONFIG_DIR


NOT_UTF8 = "文字コードが UTF-8 ではないため読めません（メモ帳などで、文字コードを UTF-8 にして保存し直してください）"

# What is wrong with a value, by the kind of pydantic's error (its own text is English).
ERROR_TEXT = {
    "missing": "入力してください",
    "greater_than_equal": "{ge} 以上にしてください",
    "less_than_equal": "{le} 以下にしてください",
    "int_parsing": "整数にしてください",
    "int_type": "整数にしてください",
    "int_from_float": "整数にしてください",
    "string_type": "文字列にしてください",
    "bool_type": "オン / オフで指定してください",
    "bool_parsing": "オン / オフで指定してください",
    "list_type": "値を並べる形（1 行に 1 つ「- 値」）にしてください",
    "dict_type": "「キー: 値」の形にしてください",
    "model_type": "「キー: 値」の形にしてください",
    "model_attributes_type": "「キー: 値」の形にしてください",
    "literal_error": "次のどれかにしてください: {expected}",
    "extra_forbidden": "この項目は使えません",
}


def problems(exc: ValidationError, where: str) -> list[str]:
    """The errors of a settings file, one line each: ``<where> の <key>: <what to change>``."""
    out = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "（全体）"
        ctx = dict(err.get("ctx") or {})
        if "expected" in ctx:  # "'auto' or 'fake'"
            ctx["expected"] = " / ".join(re.findall(r"'([^']*)'", str(ctx["expected"]))) or ctx["expected"]
        template = ERROR_TEXT.get(err["type"])
        try:
            text = template.format(**ctx) if template else "値が正しくありません"
        except (KeyError, IndexError):
            text = "値が正しくありません"
        out.append(f"{where} の {field}: {text}")
    return out


def read_text(path: Path) -> str:
    """A settings file as text; a file not saved in UTF-8, or that cannot be read, is a CtError that says so."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise CtError(f"{path.name} の{NOT_UTF8}: {path}") from None
    except OSError as exc:  # no permission to read, a folder of that name, ...
        raise CtError(f"{path.name} を読めません（{exc.strerror or exc}）: {path}") from None


def _yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CtError(f"ファイルがありません: {path}")
    text = read_text(path)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise CtError(f"YAML の書式が正しくありません: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CtError(f"YAML の一番外側は「キー: 値」の形にしてください: {path}")
    return data


def _validate(model: type[BaseModel], data: dict[str, Any], source: Path) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise CtError(f"設定の値が正しくありません: {source}\n" + "\n".join(problems(exc, source.name))) from exc


def load_config(repo: Path) -> ProjectConfig:
    path = config_dir(repo) / CONFIG_FILE
    if not path.is_file():
        raise CtError(f"設定ファイルがありません: {path}（/coverity-setup で作ります）")
    return _validate(ProjectConfig, _yaml(path), path)


def output_dir(repo: Path, config: ProjectConfig) -> Path:
    path = Path(config.output_dir)
    return resolve_path(path if path.is_absolute() else repo / path)


def filter_path(repo: Path, name: str) -> Path:
    """An absolute path, a path relative to the repository, or a file name in filters/."""
    for candidate in (Path(name), repo / name, config_dir(repo) / FILTERS_DIR / name,
                      config_dir(repo) / FILTERS_DIR / f"{name}.yaml"):
        if candidate.is_file():
            return candidate
    names = sorted(p.name for p in (config_dir(repo) / FILTERS_DIR).glob("*.yaml"))
    raise CtError(f"条件ファイルが見つかりません: {name}", filters=names)


def load_filter(repo: Path, name: str, overrides: dict[str, Any] | None = None) -> FilterSpec:
    """Read a filter file and apply the conditions given in chat (they replace key by key)."""
    path = filter_path(repo, name)
    data = _yaml(path)
    for key, value in (overrides or {}).items():
        if key == "triage":
            data["triage"] = {**(data.get("triage") or {}), **value}
        else:
            data[key] = value
    spec: FilterSpec = _validate(FilterSpec, data, path)
    if not spec.name:
        spec.name = path.stem
    return spec


def load_no_grouping(repo: Path) -> set[int]:
    path = config_dir(repo) / NO_GROUPING_FILE
    return {int(c) for c in _yaml(path).get("cids") or []} if path.is_file() else set()


def add_no_grouping(repo: Path, cids: list[int]) -> Path:
    """CIDs of a rejected group; they are worked on one by one from the next run."""
    path = config_dir(repo) / NO_GROUPING_FILE
    merged = sorted(load_no_grouping(repo) | set(cids))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 却下されたグループの CID。次の実行からグループにしない（自動で追記）\n"
                    + yaml.safe_dump({"cids": merged}), encoding="utf-8")
    return path


def knowledge_path(repo: Path) -> Path:
    return config_dir(repo) / KNOWLEDGE_FILE
