"""Project settings in ``<repo>/.coverity-triage/``: config.yaml, filters/*.yaml, knowledge.md, no-grouping.yaml.

Only the keys this skill uses are checked; unknown keys are ignored (other parts may add their own).
A key that is present but empty takes the default value.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .common import RunError, out_text, resolve_path, run_cmd

CONFIG_DIR = ".coverity-triage"
CONFIG_FILE = "config.yaml"
FILTERS_DIR = "filters"
KNOWLEDGE_FILE = "knowledge.md"
NO_GROUPING_FILE = "no-grouping.yaml"
DEFAULT_FILTER = "untriaged.yaml"
VERIFY_MODES = ("none", "build", "build+analyze")
IMPACTS = ("High", "Medium", "Low", "Unspecified")
TRIAGE_VALUES = {
    "classification": ("Unclassified", "Pending", "False Positive", "Intentional", "Bug"),
    "action": ("Undecided", "Fix Required", "Fix Submitted", "Modeling Required", "Ignore"),
    "status": ("New", "Triaged", "Dismissed", "Absent Dismissed", "Fixed"),
}

# (dotted key, kind, default, limits). Kinds: str (may be empty), text (must not be empty), opt (str or
# null), enum, bool, int, list.
SPEC: list[tuple[str, str, Any, Any]] = [
    ("coverity.url", "text", None, None),
    ("coverity.api", "enum", "auto", ("auto", "fake")),
    ("coverity.user_env", "text", "COV_USER", None),
    ("coverity.key_env", "text", "COV_AUTH_KEY", None),
    ("coverity.triage_store", "text", "Default Triage Store", None),
    ("coverity.revision_field", "opt", "sourceVersion", None),
    ("coverity.path_strip_prefixes", "list", [], None),
    ("coverity.ca_file", "opt", None, None),
    ("coverity.fake_data", "opt", None, None),
    ("vcs.type", "enum", "auto", ("auto", "git", "svn")),
    ("vcs.base_branch", "opt", None, None),
    ("vcs.branch_prefix", "text", "coverity-fix/", None),
    ("output_dir", "text", "../coverity-triage-out", None),
    ("max_items", "int", 100, (1, None)),
    ("parallel", "int", 3, (1, 8)),
    ("options.annotation", "bool", False, None),
    ("options.per_run_branch", "bool", False, None),
    ("options.knowledge_suggestions", "bool", False, None),
    ("options.metrics", "bool", False, None),
    ("options.markdown", "bool", False, None),
    ("verify.default", "enum", "none", VERIFY_MODES),
    ("verify.initial", "bool", False, None),
    ("verify.setup_command", "str", "", None),
    ("verify.build_dir", "str", "", None),
    ("verify.build_command", "str", "", None),
    ("verify.cov_build_args", "str", "--dir idir", None),
    ("verify.cov_analyze_args", "str", "--dir idir --all", None),
    ("verify.cov_commit_args", "str", "", None),
    ("verify.timeout_minutes", "int", 180, (1, None)),
    ("agents.integration", "bool", False, None),
    ("agents.review", "bool", False, None),
    ("agents.max_rework", "int", 1, (0, 2)),
]


def _yaml():
    try:
        import yaml
    except ImportError:  # pragma: no cover - uv installs it from the script header
        raise RunError("pyyaml がありません。uv run --native-tls でスクリプトを動かしてください") from None
    return yaml


def config_dir(repo: Path) -> Path:
    return repo / CONFIG_DIR


def config_path(repo: Path) -> Path:
    return config_dir(repo) / CONFIG_FILE


def read_yaml(path: Path) -> Any:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        raise RunError(f"ファイルがありません: {path}") from None
    except UnicodeDecodeError:
        raise RunError(f"{path.name} の文字コードが UTF-8 ではないため読めません（UTF-8 で保存し直してください）: {path}") \
            from None
    except OSError as exc:
        raise RunError(f"{path.name} を読めません（{exc.strerror or exc}）: {path}") from None
    yaml = _yaml()
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RunError(f"YAML の書き方が正しくありません: {path}（{str(exc).splitlines()[0]}）") from None


def _get(data: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _check(kind: str, value: Any, limits: Any) -> tuple[Any, str | None]:
    """The value to use and what is wrong with it (None: nothing)."""
    if kind in ("str", "text", "opt"):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            return None, "文字列にしてください"
        if kind == "text" and not value.strip():
            return None, "空にできません"
        return value, None
    if kind == "enum":
        if isinstance(value, str):
            for allowed in limits:
                if value.strip().lower() == allowed.lower():
                    return allowed, None
        return None, "次のどれかにしてください: " + " / ".join(limits)
    if kind == "bool":
        if isinstance(value, bool):
            return value, None
        return None, "true か false にしてください"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return None, "整数にしてください"
        low, high = limits
        if low is not None and value < low:
            return None, f"{low} 以上にしてください"
        if high is not None and value > high:
            return None, f"{high} 以下にしてください"
        return value, None
    if kind == "list":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, (str, int)) for v in value):
            return None, "値を並べる形（1 行に 1 つ「- 値」）にしてください"
        return [str(v) for v in value if str(v).strip()], None
    raise AssertionError(kind)


def parse_config(data: Any, where: str = CONFIG_FILE) -> tuple[dict[str, Any], list[str]]:
    """The settings with defaults filled in, and the problems (one line each: ``<file> の <key>: <fix>``)."""
    problems: list[str] = []
    out: dict[str, Any] = {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return _defaults(), [f"{where}: 一番外側は「キー: 値」の形にしてください"]
    for dotted, kind, default, limits in SPEC:
        present, value = _get(data, dotted)
        if not present or value is None or (value == "" and kind != "str"):
            if default is None and kind == "text":  # coverity.url: no default
                problems.append(f"{where} の {dotted}: 入力してください")
            _set(out, dotted, copy.deepcopy(default))
            continue
        checked, problem = _check(kind, value, limits)
        if problem:
            problems.append(f"{where} の {dotted}: {problem}")
            checked = copy.deepcopy(default)
        _set(out, dotted, checked)
    cov = out["coverity"]
    if cov["user_env"] and cov["user_env"] == cov["key_env"]:
        problems.append(f"{where} の coverity.user_env と coverity.key_env: 違う名前にしてください")
    if cov.get("url"):
        cov["url"] = cov["url"].rstrip("/")
    return out, problems


def _defaults() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dotted, _, default, _ in SPEC:
        _set(out, dotted, copy.deepcopy(default))
    return out


def detect_vcs(repo: Path) -> str | None:
    if (repo / ".git").exists():
        return "git"
    if (repo / ".svn").exists():
        return "svn"
    return None


def detect_base_branch(repo: Path) -> str:
    """origin's default branch, else the current branch, else main."""
    proc = run_cmd(["git", "-c", "core.longpaths=true", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                   cwd=repo, check=False, timeout=30)
    ref = out_text(proc)
    if proc.returncode == 0 and ref.startswith("refs/remotes/origin/"):
        return ref[len("refs/remotes/origin/"):]
    proc = run_cmd(["git", "-c", "core.longpaths=true", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                   check=False, timeout=30)
    name = out_text(proc)
    if proc.returncode == 0 and name and name != "HEAD":
        return name
    return "main"


def load_config(repo: Path, need_vcs: bool = True) -> tuple[dict[str, Any], list[str]]:
    """config.yaml of the repository: (settings, problems). A RunError when the file is missing or broken."""
    path = config_path(repo)
    if not path.is_file():
        raise RunError(f"設定ファイルがありません: {path}", how_to_fix=PREPARE_HINT)
    settings, problems = parse_config(read_yaml(path), CONFIG_FILE)
    vcs = settings["vcs"]
    if vcs["type"] == "auto":
        found = detect_vcs(repo)
        if found:
            vcs["type"] = found
        elif need_vcs:
            problems.append(f"{repo} は git / svn のリポジトリの一番上のフォルダではありません（vcs.type を決められません）")
    if vcs["type"] == "git" and not vcs.get("base_branch") and need_vcs and detect_vcs(repo) == "git":
        vcs["base_branch"] = detect_base_branch(repo)
    return settings, problems


PREPARE_HINT = ".coverity-triage/config.yaml を用意してください（準備の手順があれば、それで作れます）"


def output_dir(repo: Path, settings: dict[str, Any]) -> Path:
    path = Path(settings["output_dir"])
    return resolve_path(path if path.is_absolute() else repo / path)


# ---- filters ---------------------------------------------------------------------------------


def filter_names(repo: Path) -> list[str]:
    folder = config_dir(repo) / FILTERS_DIR
    return sorted(p.name for p in folder.glob("*.yaml")) if folder.is_dir() else []


def filter_path(repo: Path, name: str) -> Path:
    """An absolute path, a path relative to the repository, or a name in filters/ (with or without .yaml)."""
    folder = config_dir(repo) / FILTERS_DIR
    for candidate in (Path(name), repo / name, folder / name, folder / f"{name}.yaml"):
        if candidate.is_file():
            return resolve_path(candidate)
    raise RunError(f"条件ファイルが見つかりません: {name}", filters=filter_names(repo))


def _canonical(value: Any, allowed: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    for a in allowed:
        if value.strip().lower() == a.lower():
            return a
    return None


def parse_filter(data: Any, where: str, overrides: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    """A filter file with the conditions of the request applied (each replaces its key as a whole)."""
    problems: list[str] = []
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {}, [f"{where}: 一番外側は「キー: 値」の形にしてください"]
    data = copy.deepcopy(data)
    for key, value in (overrides or {}).items():
        if key in TRIAGE_VALUES:
            triage = data.get("triage") if isinstance(data.get("triage"), dict) else {}
            triage[key] = value
            data["triage"] = triage
        else:
            data[key] = value

    def strings(key: str, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, (str, int)) for v in value):
            problems.append(f"{where} の {key}: 値を並べる形（1 行に 1 つ「- 値」）にしてください")
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    spec: dict[str, Any] = {"name": str(data.get("name") or "").strip(), "project": data.get("project") or None,
                            "streams": strings("streams", data.get("streams")),
                            "checkers": strings("checkers", data.get("checkers")), "impacts": [],
                            "triage": {k: [] for k in TRIAGE_VALUES}, "limit": None}
    if spec["project"] is not None:
        spec["project"] = str(spec["project"])
    for raw in strings("impacts", data.get("impacts")):
        value = _canonical(raw, IMPACTS)
        if value is None:
            problems.append(f"{where} の impacts: 「{raw}」は使えません（{' / '.join(IMPACTS)}）")
        elif value not in spec["impacts"]:
            spec["impacts"].append(value)
    triage = data.get("triage")
    if triage is not None and not isinstance(triage, dict):
        problems.append(f"{where} の triage: 「キー: 値」の形にしてください")
        triage = {}
    for key, allowed in TRIAGE_VALUES.items():
        for raw in strings(f"triage.{key}", (triage or {}).get(key)):
            value = _canonical(raw, allowed)
            if value is None:
                problems.append(f"{where} の triage.{key}: 「{raw}」は使えません（{' / '.join(allowed)}）")
            elif value not in spec["triage"][key]:
                spec["triage"][key].append(value)
    limit = data.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            problems.append(f"{where} の limit: 1 以上の整数にしてください")
        else:
            spec["limit"] = limit
    return spec, problems


def load_filter(repo: Path, name: str, overrides: dict[str, Any] | None = None) -> tuple[dict[str, Any], Path]:
    path = filter_path(repo, name)
    spec, problems = parse_filter(read_yaml(path), path.name, overrides)
    if problems:
        raise RunError("条件ファイルに直すところがあります", problems=problems, filter_file=str(path))
    if not spec["name"]:
        spec["name"] = path.stem
    return spec, path


# ---- knowledge and CIDs not to group ---------------------------------------------------------


def knowledge_path(repo: Path) -> Path:
    return config_dir(repo) / KNOWLEDGE_FILE


def load_no_grouping(repo: Path) -> set[int]:
    path = config_dir(repo) / NO_GROUPING_FILE
    if not path.is_file():
        return set()
    data = read_yaml(path)
    cids = data.get("cids") if isinstance(data, dict) else None
    out = set()
    for value in cids or []:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out
