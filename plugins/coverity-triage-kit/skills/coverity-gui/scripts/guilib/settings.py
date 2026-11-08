"""The settings of the repository (``.coverity-triage/``), as the screen shows and saves them.

- ``config.yaml``: the keys of the contract (their defaults and checks are in ``CONFIG_FIELDS``).
  Keys this screen does not know are kept as they are. Saving rewrites only the values that change
  and keeps the comments (``yamledit``); a key that is not in the file is added only when its value
  differs from the default.
- ``filters/*.yaml``: the conditions. ``knowledge.md``. ``no-grouping.yaml`` (``cids: [...]``).
- the credentials: environment variables named in ``coverity.user_env`` / ``coverity.key_env``.
  The user name may be shown; of a key, only whether it is set. Saving works only on Windows.

A file that cannot be read is shown with the defaults and a Japanese hint of what to fix.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import envvars, yamledit
from .common import GuiError, read_text, resolve_path, write_text

CONFIG_DIR = ".coverity-triage"
CONFIG_FILE = "config.yaml"
FILTERS_DIR = "filters"
KNOWLEDGE_FILE = "knowledge.md"
NO_GROUPING_FILE = "no-grouping.yaml"
# The default of output_dir: a folder next to the repository (one level up, then coverity-triage-out).
DEFAULT_OUTPUT_DIR = os.pardir + "/coverity-triage-out"
NOT_UTF8 = "文字コードが UTF-8 ではないため読めません（メモ帳などで、文字コードを UTF-8 にして保存し直してください）"
COMMENTS_LOST = ("このファイルでは、注釈は動いたり消えたりします。注釈を元の場所に残せない書き方（「|」「>」で何行かに"
                 "わたる値、「&」「*」で値を使い回す書き方など）があるため、ファイル全体を書き直します。行の頭の「#」の行は"
                 "ファイルの先頭にまとめ、字下げした「#」の行と行の終わりの注釈は消えます（値は正しく保存されます）。"
                 "注釈を残すときは、保存しないで、メモ帳などでファイルを直してください。")
CONFIG_HEADER = ("# Coverity トリアージの設定。チームで共有するためコミットします。\n"
                 "# 認証情報はここに書きません（環境変数に置きます）。\n")
NO_GROUPING_HEADER = "# グループにしない CID（却下したグループの CID が、ここに足されます）\n"
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
FILTER_NAME = re.compile(r"^[^\\/:*?\"<>|.][^\\/:*?\"<>|]{0,80}\.yaml$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
URL = re.compile(r"^https?://[^/\s]+")
KEY_ENVS = ("COV_AUTH_KEY", "COVERITY_PASSPHRASE")  # always treated as names of a key


@dataclass(frozen=True)
class Field:
    kind: str  # str, nstr (text or empty), int, nint (number or empty), bool, list, choice
    default: Any
    choices: tuple[str, ...] = ()
    low: int | None = None
    high: int | None = None
    label: str = ""


def _f(kind: str, default: Any, label: str, *choices: str, low: int | None = None, high: int | None = None) -> Field:
    return Field(kind, default, tuple(choices), low, high, label)


CONFIG_FIELDS: dict[tuple[str, ...], Field] = {
    ("coverity", "url"): _f("str", "", "Coverity の URL"),
    ("coverity", "api"): _f("choice", "auto", "接続先", "auto", "fake"),
    ("coverity", "user_env"): _f("str", "COV_USER", "ユーザ名の環境変数の名前"),
    ("coverity", "key_env"): _f("str", "COV_AUTH_KEY", "認証キーの環境変数の名前"),
    ("coverity", "triage_store"): _f("str", "Default Triage Store", "トリアージストア"),
    ("coverity", "revision_field"): _f("nstr", "sourceVersion", "解析リビジョンの項目"),
    ("coverity", "path_strip_prefixes"): _f("list", [], "パスから取り除く先頭部分"),
    ("coverity", "ca_file"): _f("nstr", None, "CA 証明書のファイル"),
    ("coverity", "fake_data"): _f("nstr", None, "偽データのファイル"),
    ("vcs", "type"): _f("choice", "git", "バージョン管理", "git", "svn"),
    ("vcs", "base_branch"): _f("str", "main", "取り込み先のブランチ"),
    ("vcs", "branch_prefix"): _f("str", "coverity-fix/", "ブランチ名の先頭"),
    ("output_dir",): _f("str", DEFAULT_OUTPUT_DIR, "結果を置く場所"),
    ("max_items",): _f("int", 100, "1 回で調べる上限", low=1),
    ("parallel",): _f("int", 3, "同時に調べる数", low=1, high=8),
    ("options", "annotation"): _f("bool", False, "アノテーション"),
    ("options", "per_run_branch"): _f("bool", False, "1 つのブランチにまとめる"),
    ("options", "knowledge_suggestions"): _f("bool", False, "知識の追記の提案"),
    ("options", "metrics"): _f("bool", False, "効果測定の記録"),
    ("options", "markdown"): _f("bool", False, "Markdown も出す"),
    ("verify", "default"): _f("choice", "none", "ビルドでの検証", "none", "build", "build+analyze"),
    ("verify", "initial"): _f("bool", False, "初回解析"),
    ("verify", "setup_command"): _f("str", "", "環境設定のコマンド"),
    ("verify", "build_dir"): _f("str", "", "ビルドするフォルダ"),
    ("verify", "build_command"): _f("str", "", "ビルドのコマンド"),
    ("verify", "cov_build_args"): _f("str", "--dir idir", "cov-build の引数"),
    ("verify", "cov_analyze_args"): _f("str", "--dir idir --all", "cov-analyze の引数"),
    ("verify", "cov_commit_args"): _f("str", "", "cov-commit-defects の引数"),
    ("verify", "timeout_minutes"): _f("int", 180, "初回解析の時間の上限（分）", low=1),
    ("agents", "integration"): _f("bool", False, "統合"),
    ("agents", "review"): _f("bool", False, "検証"),
    ("agents", "max_rework"): _f("int", 1, "差し戻しの上限", low=0, high=2),
}
FILTER_FIELDS: dict[tuple[str, ...], Field] = {
    ("name",): _f("str", "", "表示名"),
    ("project",): _f("nstr", None, "プロジェクト"),
    ("streams",): _f("list", [], "ストリーム"),
    ("checkers",): _f("list", [], "チェッカー名"),
    ("impacts",): _f("list", [], "Impact"),
    ("triage", "status"): _f("list", [], "Status"),
    ("triage", "classification"): _f("list", [], "Classification"),
    ("triage", "action"): _f("list", [], "Action"),
    ("limit",): _f("nint", None, "調べる警告の数（上限）", low=1),
}
REQUIRED_TEXT = {("coverity", "user_env"), ("coverity", "key_env"), ("output_dir",)}
KIND_TEXT = {
    "str": "文字列にしてください", "nstr": "文字列にしてください（空でもかまいません）", "int": "整数にしてください",
    "nint": "整数にしてください（空でもかまいません）", "bool": "true（オン）か false（オフ）にしてください",
    "list": "値を並べる形（1 行に 1 つ「- 値」）にしてください",
}


# ---- the repository ------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-c", "core.longpaths=true", "-C", str(repo), *args], capture_output=True,
                              timeout=10, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    text = proc.stdout.decode("utf-8", "replace").strip()
    return text if proc.returncode == 0 and text else None


def detect(repo: Path) -> dict[str, Any]:
    """The kind of version control (from .git / .svn) and the branch fixes go to (git)."""
    if not repo.is_dir():
        raise GuiError(f"フォルダがありません: {repo}")
    vcs = "git" if (repo / ".git").exists() else "svn" if (repo / ".svn").is_dir() else None
    base = None
    if vcs == "git":
        head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        base = head.split("/", 1)[1] if head and "/" in head else (_git(repo, "symbolic-ref", "--short", "HEAD") or "main")
    note = None if vcs else "git / svn のリポジトリの一番上のフォルダ（.git か .svn があるフォルダ）を開いてください"
    return {"vcs": vcs, "vcs_note": note, "base_branch": base}


def config_dir(repo: Path) -> Path:
    return repo / CONFIG_DIR


def read_yaml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The mapping of a YAML file, or None and the reason in Japanese."""
    try:
        text = read_text(path)
    except UnicodeDecodeError:
        return None, f"{path.name} の{NOT_UTF8}"
    except OSError as exc:
        return None, f"{path.name} を読めません（{exc.strerror or exc}）"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"{path.name} の YAML の書式が正しくないため読めません: {' '.join(str(exc).split())}"
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, f"{path.name} の一番外側が「キー: 値」の形ではありません"
    return data, None


def output_dir(repo: Path) -> tuple[Path, str | None]:
    """The folder of the runs (from config.yaml, else the default), and why the default was used."""
    path = config_dir(repo) / CONFIG_FILE
    value, note = DEFAULT_OUTPUT_DIR, None
    if path.is_file():
        raw, error = read_yaml(path)
        if raw is None:
            note = f"{error}。結果を置く場所は既定（{DEFAULT_OUTPUT_DIR}）として探します"
        elif isinstance(raw.get("output_dir"), str) and raw["output_dir"].strip():
            value = raw["output_dir"].strip()
        elif "output_dir" in raw and raw["output_dir"] is not None:
            note = f"{CONFIG_FILE} の output_dir が文字列ではないため、既定（{DEFAULT_OUTPUT_DIR}）として探します"
    folder = Path(value)
    return resolve_path(folder if folder.is_absolute() else repo / folder), note


# ---- values as the screen shows them -------------------------------------------------------


def defaults(fields: dict[tuple[str, ...], Field], found: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, field in fields.items():
        value = copy.deepcopy(field.default)
        if found and path == ("vcs", "type") and found.get("vcs"):
            value = found["vcs"]
        if found and path == ("vcs", "base_branch") and found.get("base_branch"):
            value = found["base_branch"]
        _put(out, path, value)
    return out


def _put(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[path[-1]] = value


_MISSING = object()


def _get(data: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return _MISSING
        data = data[key]
    return data


def _fits(field: Field, value: Any) -> bool:
    """Whether a saved value can be shown in the field (a value that cannot is shown as the default)."""
    kind = field.kind
    if kind == "bool":
        return isinstance(value, bool)
    if kind in ("int", "nint"):
        return (value is None and kind == "nint") or (isinstance(value, int) and not isinstance(value, bool))
    if kind == "list":
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    if kind == "choice":
        return isinstance(value, str) and value in field.choices
    return value is None or isinstance(value, str)  # str, nstr: an empty value (null) shows as empty


def _text(value: Any) -> str:
    if value is None or value == "":
        return "（空）"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 80 else text[:79] + "…"


def unknown_keys(raw: Any, fields: dict[tuple[str, ...], Field], at: tuple[str, ...] = ()) -> list[str]:
    """The keys of a file that the screen does not know (they are kept when it saves)."""
    out: list[str] = []
    if not isinstance(raw, dict):
        return out
    sections = {p[: len(at) + 1] for p in fields if len(p) > len(at) + 1 and p[: len(at)] == at}
    leaves = {p for p in fields if len(p) == len(at) + 1 and p[: len(at)] == at}
    for key, value in raw.items():
        path = at + (str(key),)
        if path in leaves:
            continue
        if path in sections:
            out.extend(unknown_keys(value, fields, path))
        else:
            out.append(".".join(path))
    return out


def on_screen(raw: dict[str, Any], fields: dict[tuple[str, ...], Field],
              base: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The values to show (saved values over ``base``), and the saved values shown as the default.

    A value of the wrong kind (``streams: a`` for a list) is shown as the default; the table before
    saving lists it with what is in the file now, because saving writes over it."""
    shown = copy.deepcopy(base)
    reset: dict[str, str] = {}
    for path, field in fields.items():
        value = _get(raw, path)
        if value is _MISSING:
            parent = _get(raw, path[:-1]) if len(path) > 1 else raw
            if parent is not _MISSING and not isinstance(parent, dict):  # a section written as a value
                reset[".".join(path)] = _text(parent)
            continue
        if _fits(field, value):
            _put(shown, path, "" if value is None and field.kind == "str" else copy.deepcopy(value))
        else:
            reset[".".join(path)] = _text(value)
    return shown, reset


def problems(data: Any, fields: dict[tuple[str, ...], Field], where: str) -> list[str]:
    """What is wrong with the known values, one line each: ``<file> の <key>: <what to change>``."""
    out: list[str] = []
    if not isinstance(data, dict):
        return [f"{where}: 「キー: 値」の形にしてください"]
    for path, field in fields.items():
        value = _get(data, path)
        name = ".".join(path)
        if value is _MISSING:
            parent = _get(data, path[:-1]) if len(path) > 1 else data
            if parent is not _MISSING and not isinstance(parent, dict):
                out.append(f"{where} の {'.'.join(path[:-1])}: 「キー: 値」の形にしてください")
            continue
        if field.kind == "choice":
            if value not in field.choices:
                out.append(f"{where} の {name}: 次のどれかにしてください: {' / '.join(field.choices)}")
            continue
        if not _fits(field, value):
            out.append(f"{where} の {name}: {KIND_TEXT[field.kind]}")
            continue
        if field.kind in ("int", "nint") and value is not None:
            if field.low is not None and value < field.low or field.high is not None and value > field.high:
                span = f"{field.low}〜{field.high}" if field.high is not None else f"{field.low} 以上"
                out.append(f"{where} の {name}: {span}の整数にしてください")
    return list(dict.fromkeys(out))


# ---- reading everything the settings screen shows --------------------------------------------


def credentials(user_env: Any, key_env: Any, saved_key_env: Any = None) -> dict[str, Any]:
    """The user name may be shown; of the key, only whether it is set (the value never leaves this PC's
    environment). The variable named for the user name is not shown when it is one that holds a key."""
    user_env = user_env if isinstance(user_env, str) else ""
    key_env = key_env if isinstance(key_env, str) else ""
    names_ok = bool(ENV_NAME.match(user_env)) and bool(ENV_NAME.match(key_env))
    keys = {name.upper() for name in (key_env, saved_key_env if isinstance(saved_key_env, str) else "", *KEY_ENVS) if name}
    hidden = names_ok and user_env.upper() in keys
    user = (envvars.get_env(user_env) or "") if names_ok else ""
    return {"user_env": user_env, "key_env": key_env,
            "user": "" if hidden else user, "user_set": bool(user), "user_hidden": hidden,
            "key_set": bool(envvars.get_env(key_env)) if names_ok else False,
            "can_save": envvars.can_set_user_env()}


def _saved_names(repo: Path) -> dict[str, str]:
    raw, _ = read_yaml(config_dir(repo) / CONFIG_FILE) if (config_dir(repo) / CONFIG_FILE).is_file() else ({}, None)
    section = (raw or {}).get("coverity")
    section = section if isinstance(section, dict) else {}
    return {key: section[key] for key in ("user_env", "key_env") if isinstance(section.get(key), str)}


def saved_key_env(repo: Path) -> str | None:
    return _saved_names(repo).get("key_env")


def _knowledge(repo: Path) -> dict[str, Any]:
    path = config_dir(repo) / KNOWLEDGE_FILE
    if not path.is_file():
        return {"exists": False, "text": KNOWLEDGE_TEMPLATE, "error": None}
    try:
        return {"exists": True, "text": read_text(path), "error": None}
    except UnicodeDecodeError:
        error = f"{KNOWLEDGE_FILE} の{NOT_UTF8}"
    except OSError as exc:
        error = f"{KNOWLEDGE_FILE} を読めません（{exc.strerror or exc}）"
    # Not shown and not saved: saving must not replace what could not be read.
    return {"exists": True, "text": "", "error": error}


def _cids(raw: Any) -> list[int]:
    values = raw.get("cids") if isinstance(raw, dict) else None
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError
    out = set()
    for value in values:
        if isinstance(value, bool):
            raise ValueError
        out.add(int(value))
    return sorted(out)


def load_state(repo: Path) -> dict[str, Any]:
    """Everything the settings screen shows, read from the repository and this PC."""
    found = detect(repo)
    folder = config_dir(repo)
    base = defaults(CONFIG_FIELDS, found)
    path = folder / CONFIG_FILE
    config, error, unreadable, reset, unknown = copy.deepcopy(base), None, False, {}, []
    exists = path.is_file()
    if exists:
        raw, error = read_yaml(path)
        if raw is None:
            unreadable = True
        else:
            config, reset = on_screen(raw, CONFIG_FIELDS, base)
            unknown = unknown_keys(raw, CONFIG_FIELDS)
            lines = problems(raw, CONFIG_FIELDS, CONFIG_FILE)
            error = "\n".join(lines) or None
    filters = []
    for file in sorted((folder / FILTERS_DIR).glob("*.yaml")):
        raw, ferror = read_yaml(file)
        data, freset, funknown = defaults(FILTER_FIELDS), {}, []
        if raw is not None:
            data, freset = on_screen(raw, FILTER_FIELDS, defaults(FILTER_FIELDS))
            funknown = unknown_keys(raw, FILTER_FIELDS)
            ferror = "\n".join(problems(raw, FILTER_FIELDS, file.name)) or None
        filters.append({"file": file.name, "data": data, "error": ferror, "unreadable": raw is None,
                        "reset": freset, "unknown": funknown})
    ng_path = folder / NO_GROUPING_FILE
    no_grouping, ng_error = [], None
    if ng_path.is_file():
        raw, ng_error = read_yaml(ng_path)
        if raw is not None:
            try:
                no_grouping = _cids(raw)
            except (TypeError, ValueError):
                ng_error = f"{NO_GROUPING_FILE} の cids に数字でないものがあります"
    out_dir, out_note = output_dir(repo)
    return {
        "repo_root": str(repo), "vcs": found["vcs"], "vcs_note": found["vcs_note"],
        "config_exists": exists, "config": config, "config_error": error, "config_unreadable": unreadable,
        "config_reset": reset, "config_unknown": unknown, "output_dir": str(out_dir), "output_dir_note": out_note,
        "filters": filters, "knowledge": _knowledge(repo),
        "no_grouping": no_grouping, "no_grouping_error": ng_error,
        "credentials": credentials(config["coverity"]["user_env"], config["coverity"]["key_env"], saved_key_env(repo)),
    }


# ---- checking and writing ------------------------------------------------------------------


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _same_meaning(field: Field | None, before: Any, after: Any) -> bool:
    """Equal values; for text, an empty value (null) and "" mean the same (the file keeps what it has)."""
    if field is not None and field.kind in ("str", "nstr") and _blank(before) and _blank(after):
        return True
    return type(before) is type(after) and before == after


def _clean(data: dict[str, Any], fields: dict[tuple[str, ...], Field]) -> dict[str, Any]:
    """Only the known keys, in their order; texts trimmed where the screen trims them, empty → null."""
    out: dict[str, Any] = {}
    for path, field in fields.items():
        value = _get(data, path)
        if value is _MISSING:
            value = copy.deepcopy(field.default)
        if field.kind == "nstr" and _blank(value):
            value = None
        elif field.kind == "str" and value is None:
            value = ""
        _put(out, path, value)
    return out


def _target(full: dict[str, Any], raw: dict[str, Any], default: dict[str, Any],
            fields: dict[tuple[str, ...], Field], at: tuple[str, ...] = ()) -> dict[str, Any]:
    """The values to write: the keys already in the file (unchanged values exactly as written), the others
    only when they differ from the default, and the keys the screen does not know, kept as they are."""
    out: dict[str, Any] = {}
    for key, value in full.items():
        path = at + (key,)
        if isinstance(value, dict):
            below = raw.get(key)
            part = _target(value, below if isinstance(below, dict) else {}, default.get(key) or {}, fields, path)
            if key in raw:
                if isinstance(below, dict) or part:
                    out[key] = part
                else:  # 「options:」with nothing below means the defaults; a value of another kind is replaced
                    out[key] = below if below is None else value
            elif part:
                out[key] = part
            continue
        field = fields.get(path)
        if key in raw:
            out[key] = raw[key] if _same_meaning(field, raw[key], value) else value
        elif key not in default or not _same_meaning(field, default[key], value):
            out[key] = value
    for key, value in raw.items():
        if key not in full:
            out[key] = value
    return out


def _config_full(config: dict[str, Any]) -> dict[str, Any]:
    full = _clean(config, CONFIG_FIELDS)
    full["coverity"]["url"] = str(full["coverity"]["url"] or "").strip().rstrip("/")
    if full["vcs"]["type"] == "svn":  # svn has no branches to name
        full["vcs"].pop("base_branch", None)
        full["vcs"].pop("branch_prefix", None)
    return full


def config_problems(repo: Path, found: dict[str, Any], data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["config.yaml の内容がありません"]
    out: list[str] = []
    if not found["vcs"]:
        out.append(found["vcs_note"])
    out += problems(data, CONFIG_FIELDS, CONFIG_FILE)
    coverity = data.get("coverity") if isinstance(data.get("coverity"), dict) else {}
    url = str(coverity.get("url") or "").strip()
    if not URL.match(url):
        out.append("Coverity の URL: http:// か https:// で始まるアドレスを入れてください")
    for path in REQUIRED_TEXT:
        value = _get(data, path)
        if isinstance(value, str) and not value.strip() or value is None:
            out.append(f"{CONFIG_FIELDS[path].label}: 入力してください")
    for key in ("user_env", "key_env"):
        value = coverity.get(key)
        if isinstance(value, str) and value.strip() and not ENV_NAME.match(value):
            out.append(f"{CONFIG_FIELDS[('coverity', key)].label}: 英字・数字・_ だけにしてください（今は {value}）")
    user_env, key_env = coverity.get("user_env"), coverity.get("key_env")
    if isinstance(user_env, str) and isinstance(key_env, str) and user_env.strip() and user_env.upper() == key_env.upper():
        out.append(f"認証情報の環境変数の名前: ユーザ名と認証キーに、別の名前を入れてください（今はどちらも {user_env}）")
    vcs = data.get("vcs") if isinstance(data.get("vcs"), dict) else {}
    if found["vcs"] and vcs.get("type") != found["vcs"]:
        out.append(f"バージョン管理: このリポジトリは {found['vcs']} です（設定は {vcs.get('type')}）")
    if vcs.get("type") == "git" and _blank(vcs.get("base_branch")):
        out.append("取り込み先のブランチ: 入力してください")
    verify = data.get("verify") if isinstance(data.get("verify"), dict) else {}
    if _blank(verify.get("build_command")):
        if verify.get("default", "none") != "none":
            out.append("ビルドでの検証: ビルドのコマンドを入れてください")
        elif verify.get("initial") is True:
            out.append("初回解析: ビルドのコマンドを入れてください")
    folder = data.get("output_dir")
    if isinstance(folder, str) and folder.strip():
        path = Path(folder.strip())
        out_path = resolve_path(path if path.is_absolute() else repo / path)
        if out_path == repo or repo in out_path.parents:
            out.append(f"結果を置く場所: リポジトリの外にしてください（{out_path}）")
    return list(dict.fromkeys(out))


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _file_text(path: Path, full: dict[str, Any], fields: dict[tuple[str, ...], Field],
               default: dict[str, Any], new_text) -> tuple[str, bool]:
    """The new text of a settings file and whether its comments move or go."""
    try:
        text = path.read_bytes().decode("utf-8")  # a BOM and CRLF line breaks are kept as they are
        raw = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return new_text(), False  # a new file, or one the screen said it writes whole
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return new_text(), False
    target = _target(full, raw, default, fields)
    return yamledit.rewrite(text, target)


def config_text(repo: Path, config: dict[str, Any], found: dict[str, Any]) -> tuple[str, bool]:
    full = _config_full(config)
    default = _config_full(defaults(CONFIG_FIELDS, found))

    def whole() -> str:
        data = copy.deepcopy(full)
        for key in ("ca_file", "fake_data"):
            if data["coverity"].get(key) is None:
                del data["coverity"][key]
        return CONFIG_HEADER + _dump(data)

    return _file_text(config_dir(repo) / CONFIG_FILE, full, CONFIG_FIELDS, default, whole)


def _filter_new(full: dict[str, Any]) -> str:
    data = copy.deepcopy(full)
    data["triage"] = {k: v for k, v in data["triage"].items() if v}
    return _dump({k: v for k, v in data.items() if v not in (None, "", [], {})})


def filter_text(path: Path, data: dict[str, Any]) -> tuple[str, bool]:
    full = _clean(data, FILTER_FIELDS)
    return _file_text(path, full, FILTER_FIELDS, defaults(FILTER_FIELDS), lambda: _filter_new(full))


def no_grouping_text(path: Path, cids: list[int]) -> tuple[str, bool]:
    def new() -> str:
        return NO_GROUPING_HEADER + yaml.safe_dump({"cids": cids})

    try:
        text = path.read_bytes().decode("utf-8")
        raw = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return new(), False
    if not isinstance(raw, dict):
        return new(), False
    return yamledit.rewrite(text, {**raw, "cids": cids})


def auth_key_value(value: str) -> tuple[str, str | None]:
    """The key itself when the contents of a downloaded key file (JSON) were pasted."""
    text = value.strip()
    if not text.startswith("{"):
        return value, None
    try:
        data = json.loads(text)
    except ValueError:
        return value, "認証キーのファイルの中身（JSON）として読めません。キーの部分だけを貼ってください"
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip():
        return value, "認証キーのファイルの中身に key の値がありません。キーの部分だけを貼ってください"
    return key.strip(), None


def _credential_names(repo: Path, config: Any) -> tuple[str, set[str]]:
    saved = _saved_names(repo)
    coverity = config.get("coverity") if isinstance(config, dict) else None
    coverity = coverity if isinstance(coverity, dict) else {}
    user_env = coverity.get("user_env") if isinstance(coverity.get("user_env"), str) else saved.get("user_env", "")
    keys = {coverity.get("key_env") if isinstance(coverity.get("key_env"), str) else "", saved.get("key_env", ""), *KEY_ENVS}
    return (user_env or "").upper(), {name.upper() for name in keys if name}


def _plan(repo: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Everything a save would write, checked first: {problems, writes, deletes, env, lost}."""
    if not isinstance(payload, dict):
        return {"problems": ["保存する内容が「キー: 値」の形ではありません"], "writes": {}, "deletes": [], "env": [], "lost": []}
    found = detect(repo)
    folder = config_dir(repo)
    found_problems: list[str] = []
    writes: dict[Path, str] = {}
    deletes: list[Path] = []
    env: list[tuple[str, str]] = []
    lost: list[str] = []

    config = payload.get("config")
    if config is not None:
        issues = config_problems(repo, found, config)
        found_problems += issues
        if not issues:
            text, gone = config_text(repo, config, found)
            writes[folder / CONFIG_FILE] = text
            if gone:
                lost.append(CONFIG_FILE)
    elif not (folder / CONFIG_FILE).is_file():
        found_problems.append("設定ファイルがまだありません。基本設定を入れてください")

    filters = payload.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
        found_problems.append("条件の内容が正しくありません")
    existing = {p.name for p in (folder / FILTERS_DIR).glob("*.yaml")}
    for name in filters.get("delete") or []:
        if name not in existing:
            found_problems.append(f"削除する条件ファイルがありません: {name}")
        else:
            deletes.append(folder / FILTERS_DIR / name)
    saved_names = set()
    for entry in filters.get("save") or []:
        entry = entry if isinstance(entry, dict) else {}
        name = str(entry.get("file") or "")
        if not FILTER_NAME.match(name):
            found_problems.append(f"条件ファイルの名前: 「{name}」は使えません（.yaml で終わり、\\ / : * ? \" < > | を含まない名前）")
            continue
        if entry.get("new") and name in existing:
            found_problems.append(f"条件ファイル {name} はもうあります。別の名前にしてください")
            continue
        data = entry.get("data")
        issues = problems(data, FILTER_FIELDS, name)
        if not issues:
            streams = data.get("streams") or []
            if len(streams) != 1 or not str(streams[0]).strip():
                issues.append(f"{name} のストリーム: 1 つだけ入れてください")
        found_problems += issues
        if issues:
            continue
        path = folder / FILTERS_DIR / name
        text, gone = filter_text(path, data) if not entry.get("new") else (_filter_new(_clean(data, FILTER_FIELDS)), False)
        writes[path] = text
        saved_names.add(name)
        if gone:
            lost.append(f"{FILTERS_DIR}/{name}")
    remaining = (existing - {p.name for p in deletes}) | saved_names
    if (config is not None or filters.get("save") or filters.get("delete")) and not remaining:
        found_problems.append("条件ファイルが 1 つもありません。少なくとも 1 つ作ってください")

    if payload.get("knowledge") is not None:
        if _knowledge(repo)["error"]:
            found_problems.append(f"{KNOWLEDGE_FILE} を読めないため、画面からは保存しません。メモ帳などで直してください")
        elif not isinstance(payload["knowledge"], str):
            found_problems.append(f"{KNOWLEDGE_FILE} の内容が文字ではありません")
        else:
            writes[folder / KNOWLEDGE_FILE] = payload["knowledge"]

    if payload.get("no_grouping") is not None:
        try:
            values = payload["no_grouping"]
            if not isinstance(values, list) or any(isinstance(c, bool) for c in values):
                raise ValueError
            cids = sorted({int(c) for c in values})
            if any(c <= 0 for c in cids):
                raise ValueError
            text, gone = no_grouping_text(folder / NO_GROUPING_FILE, cids)
            writes[folder / NO_GROUPING_FILE] = text
            if gone:
                lost.append(NO_GROUPING_FILE)
        except (TypeError, ValueError):
            found_problems.append("グループにしない CID: 数字だけにしてください")

    user_env, key_envs = _credential_names(repo, config)
    for entry in payload.get("credentials") or []:
        entry = entry if isinstance(entry, dict) else {}
        name, value = str(entry.get("env") or ""), str(entry.get("value") or "")
        is_user = entry.get("kind") == "user" or ("kind" not in entry and name.upper() == user_env)
        value, key_problem = (value, None) if is_user else auth_key_value(value)
        if key_problem:
            found_problems.append(f"環境変数 {name}: {key_problem}")
        elif not ENV_NAME.match(name):
            found_problems.append(f"環境変数の名前「{name}」は使えません（英字・数字・_ だけ）")
        elif is_user and name.upper() in key_envs:
            found_problems.append(f"環境変数 {name}: 認証キーを入れる環境変数なので、ユーザ名は保存できません。"
                                  "ユーザ名の環境変数の名前を、別の名前にしてください")
        elif not value.strip() or "\n" in value or "\r" in value:
            found_problems.append(f"環境変数 {name}: 1 行の値を入れてください")
        elif not envvars.can_set_user_env():
            found_problems.append(f"環境変数 {name}: この PC では保存できません（Windows だけ）。自分で設定してください")
        elif any(name.upper() == done.upper() for done, _ in env):
            found_problems.append(f"環境変数 {name}: ユーザ名と認証キーを同じ環境変数には保存できません")
        else:
            env.append((name, value.strip()))
    return {"problems": found_problems, "writes": writes, "deletes": deletes, "env": env, "lost": lost}


def save_check(repo: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Before the save is confirmed: whether comments of the files to write move or go."""
    plan = _plan(repo, payload)
    if plan["problems"]:
        return {"ok": False, "error": "保存できません。次を直してください", "problems": plan["problems"]}
    out: dict[str, Any] = {"ok": True, "comments": "lost" if plan["lost"] else "kept", "files": plan["lost"]}
    if plan["lost"]:
        out["notice"] = COMMENTS_LOST
    return out


def save(repo: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Check everything first; write only when nothing is wrong."""
    plan = _plan(repo, payload)
    if plan["problems"]:
        return {"ok": False, "error": "保存できませんでした。次を直してください", "problems": plan["problems"]}
    for path, text in plan["writes"].items():
        write_text(path, text)
    for path in plan["deletes"]:
        path.unlink()
    saved_env = []
    for name, value in plan["env"]:
        try:
            envvars.set_user_env(name, value)
        except OSError as exc:
            return {"ok": False, "error": f"環境変数 {name} を保存できませんでした: {exc}",
                    "written": [str(p) for p in plan["writes"]], "deleted": [str(p) for p in plan["deletes"]]}
        saved_env.append(name)
    return {"ok": True, "written": [str(p) for p in plan["writes"]], "deleted": [str(p) for p in plan["deletes"]],
            "environment_variables": saved_env, "comments": "lost" if plan["lost"] else "kept", "lost": plan["lost"]}
