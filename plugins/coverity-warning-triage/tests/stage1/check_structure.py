#!/usr/bin/env python3
"""1段目 構造の検査。

使い方:
    python3 -I check_structure.py プラグインのフォルダ [--kind generated|plugin] [--json]

  --kind generated  生成プラグイン（既定）。公式仕様の検査に、作業の定義・EVAL の課題・ナレッジの番号・
                    エージェントの tools・要件の追跡の表の検査を足す。
  --kind plugin     公式仕様の検査だけ（クリエイター自身の1段目が、この関数を読み込んで使う）。
  --json            結果を JSON で標準出力に出す。

終了コード: 0 全部合格、1 不合格あり、2 道具が足りない・使い方の誤り。
モデルも外のネットワークも使わない。公式スキーマは schemas/ の写しを使う。
EVAL の課題の regex の pattern が正規表現として読めるかと、課題の入力のままで合ってしまわないかだけは、node があれば node で確かめる
（採点は JavaScript の正規表現で行うため）。
"""
from __future__ import annotations

import argparse
import ast
import glob
import ipaddress
import json
import math
import os
import posixpath
import re
import subprocess
import sys
import unicodedata
import warnings
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

if sys.version_info < (3, 9):
    sys.stderr.write("Python 3.9 以上が要ります（今は %s）。\n" % sys.version.split()[0])
    raise SystemExit(2)

try:
    import jsonschema
    import yaml
except ImportError as _e:  # 道具が足りないときは、足りないものを示して止まる
    sys.stderr.write(
        "1段目の検査に要る Python のパッケージが足りません（%s）。\n"
        "仮想環境の中で次を実行してください: python3 -m pip install PyYAML jsonschema\n" % _e
    )
    raise SystemExit(2)

if not hasattr(jsonschema, "Draft202012Validator"):   # 公式スキーマは JSON Schema 2020-12。jsonschema 4.0 から読める
    sys.stderr.write("jsonschema が古い（4.0 以上が要る）。仮想環境の中で次を実行してください: python3 -m pip install -U jsonschema\n")
    raise SystemExit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(HERE, "schemas", "agent-plugins-1.0.0")

PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

# 試験の一覧。(番号, 確かめる要件, 名前)。名前には要件の番号を入れる（追跡の表の突き合わせにも使う）。
CATALOG: Dict[str, Tuple[List[str], str]] = {
    "manifest": (["REQ-004"], "plugin.json が公式スキーマ（Agent Plugins 1.0.0）に合う"),
    "mcp": (["REQ-004"], "mcp.json が公式スキーマと決まりに合う"),
    "skills": (["REQ-004"], "スキルのフロントマターが Agent Skills の決まりに合う"),
    "references": (["REQ-004"], "スキルの references の各ファイルが 20KB 未満で、本文で名指しされている"),
    "agents": (["REQ-004", "REQ-010"], "カスタムエージェントの定義が決まりに合う（フロントマター、tools に skill や * や知らない名前がない）"),
    "layout": (["REQ-004"], "ファイルの置き場所が Agent Plugins の形式に合う"),
    "symlinks": (["REQ-004"], "根の外に出るシンボリックリンクがない"),
    "task": (["REQ-011"], "作業の定義（task.json）が task.schema.json に合う"),
    "eval": (["REQ-011"], "EVAL の課題が採点のしかたの決まりに合い、基準の番号と文が作業の定義の写しと同じ"),
    # ナレッジの番号の突き合わせは、生成プラグインの要件ではなく設計書 7章の試験なので、要件の番号の代わりに節を付ける
    "knowledge": ([], "ナレッジの番号がそれぞれの置き場所にあり、外した番号を指したままの箇所がない（設計書 7章）"),
    "standalone": (["REQ-062"], "入口のほかのスキルが、そのスキルのフォルダだけで使え、スキルの決まりの文がほかの部品に写されていない"),
    "agent-tools": (["REQ-010"], "統括・作業・検証の定義がそろい、作業と検証に tools があり、統括に tools がなく、検証エージェントに書き込みとコマンドがない"),
    "tool-names": (["REQ-010"], "記録のツールの名前の頭がプラグインの名前"),
    "marketplace": (["REQ-004"], "marketplace.json に名前・持ち主・プラグインの一覧があり、一覧のプラグインの名前が plugin.json と同じ"),
    "trace": (["REQ-003"], "要件の追跡の表がそろっている"),
}


def check_title(check_id: str) -> str:
    reqs, text = CATALOG[check_id]
    return " ".join(reqs + [text]) if reqs else text


def stage1_requirement_numbers() -> Set[str]:
    """1段目の試験の名前に入っている要件の番号（追跡の表の「自動のテスト」の突き合わせに使う）。"""
    return {r for reqs, _ in CATALOG.values() for r in reqs}


# ---------------------------------------------------------------- 報告

class Check:
    """1つの試験。errors があれば不合格。"""

    def __init__(self, check_id: str, title: str) -> None:
        self.id = check_id
        self.title = title
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.skipped: Optional[str] = None

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def skip(self, reason: str) -> None:
        self.skipped = reason

    @property
    def status(self) -> str:
        if self.errors:
            return "failed"
        return "skipped" if self.skipped else "passed"

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "problems": list(self.errors),
                "warnings": list(self.warnings), "skipped": self.skipped}


class Report:
    def __init__(self, root: str, kind: str) -> None:
        self.root = root
        self.kind = kind
        self.checks: List[Check] = []

    def new(self, check_id: str, title: Optional[str] = None) -> Check:
        check = Check(check_id, title or check_title(check_id))
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return not any(c.errors for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin": self.root,
            "kind": self.kind,
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "failures": [{"test": c.title, "reason": m} for c in self.checks for m in c.errors],
            "warnings": [{"test": c.title, "message": m} for c in self.checks for m in c.warnings],
        }


class Context:
    """検査の間で分け合う、読み込み済みの値。"""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.real_root = os.path.realpath(self.root)
        self.manifest: Optional[Dict[str, Any]] = None
        self.plugin_name: Optional[str] = None
        self.mcp_servers: Set[str] = set()                          # mcp.json にあるサーバーの名前
        self.agents: Dict[str, Tuple[Dict[str, Any], str]] = {}   # ファイル名（.agent.md を除く）-> (フロントマター, 本文)
        self.task: Optional[Dict[str, Any]] = None
        self.task_path: Optional[str] = None
        self.knowledge: List[Dict[str, Any]] = []                 # tests/knowledge.json の読めた項目


# ---------------------------------------------------------------- 読み込みの部品

def rel(ctx: Context, path: str) -> str:
    return os.path.relpath(path, ctx.root).replace(os.sep, "/")


def is_within(path: str, base: str) -> bool:
    """path が base の中（base 自身を含む）か。どちらも本当の場所（realpath か normpath 済み）で渡す。"""
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(base)]) == os.path.abspath(base)
    except ValueError:  # ドライブが違う
        return False


def short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_json_file(path: str, label: str, check: Check) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        check.error("%s: 読めない（%s）" % (label, e.strerror or e))
    except UnicodeDecodeError:
        check.error("%s: UTF-8 の文字として読めない" % label)
    except ValueError as e:
        check.error("%s: JSON として読めない（%s）" % (label, e))
    return None


def load_schema(directory: str, name: str) -> Dict[str, Any]:
    with open(os.path.join(directory, name), encoding="utf-8") as f:
        return json.load(f)


def describe_schema_error(error: "jsonschema.ValidationError", prefix: Tuple[Any, ...] = ()) -> str:
    """jsonschema の英語の文を、平易な日本語にする。prefix は、部分だけ検査したときの上の階層の名前。"""
    # oneOf / anyOf は、いちばん近い選択肢の誤りを示す
    if error.validator in ("oneOf", "anyOf") and error.context:
        best = jsonschema.exceptions.best_match(error.context)
        if best is not None:
            error = best
    where = "/".join(str(p) for p in list(prefix) + list(error.absolute_path)) or "(全体)"
    v, want, inst = error.validator, error.validator_value, error.instance
    if v == "required" and isinstance(inst, dict):
        message = "必須の項目がない: " + "、".join(k for k in want if k not in inst)
    elif v == "additionalProperties" and isinstance(inst, dict):
        known = error.schema.get("properties", {})
        message = "決まりにない項目がある: " + "、".join(k for k in inst if k not in known)
    elif v == "const":
        message = "%s でなければならない（今は %s）" % (short(want, 90), short(inst, 90))
    elif v == "enum":
        message = "次のどれかでなければならない: %s（今は %s）" % ("、".join(map(str, want)), short(inst))
    elif v == "type":
        message = "型が %s でない（今は %s）" % (want if isinstance(want, str) else "または".join(want), short(inst))
    elif v == "pattern":
        message = "%s が形の決まり %s に合わない" % (short(inst), want)
    elif v == "minLength":
        message = "%d 文字以上でなければならない" % want
    elif v == "maxLength":
        message = "%d 文字以内でなければならない" % want
    elif v == "minItems":
        message = "%d 個以上の要素が要る" % want
    elif v == "minimum":
        message = "%s 以上でなければならない（今は %s）" % (want, short(inst))
    elif v == "uniqueItems":
        message = "同じ要素が重なっている"
    elif v == "not":
        message = "%s は使えない（%s）" % (short(inst), want.get("description", "決まりで除かれている値") if isinstance(want, dict) else "決まりで除かれている値")
    else:
        message = error.message
    return "%s: %s" % (where, message)


def schema_problems(instance: Any, schema: Dict[str, Any], label: str, hints: Optional[Dict[str, str]] = None) -> List[str]:
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: [str(p) for p in e.absolute_path])
    out = []
    for e in errors:
        text = describe_schema_error(e)
        where = "/".join(str(p) for p in e.absolute_path)
        if hints and where in hints:
            text += hints[where]
        out.append("%s %s" % (label, text))
    return out


FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.S)


def parse_frontmatter(path: str, label: str, check: Check) -> Tuple[Optional[Dict[str, Any]], str]:
    """(フロントマターの辞書, 本文)。読めなければ error を足して (None, 本文) を返す。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        check.error("%s: 読めない（%s）" % (label, e))
        return None, ""
    match = FRONTMATTER.match(text)
    if not match:
        check.error("%s: 先頭にフロントマター（--- で囲んだ YAML）がない" % label)
        return None, text
    body = text[match.end():]
    try:
        meta = yaml.safe_load(match.group(1) or "")
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = " %d 行目:" % (mark.line + 2) if mark else ""
        check.error("%s: フロントマターが YAML として読めない（%s %s）" % (label, line, getattr(e, "problem", e)))
        return None, body
    if not isinstance(meta, dict):
        check.error("%s: フロントマターが項目の集まり（キーと値）になっていない" % label)
        return None, body
    return meta, body


# ---------------------------------------------------------------- 公式仕様の検査

def check_manifest(ctx: Context, c: Check) -> None:
    path = os.path.join(ctx.root, "plugin.json")
    if not os.path.isfile(path):
        c.error("plugin.json がない（プラグインの根に置く）")
        return
    data = read_json_file(path, "plugin.json", c)
    if data is None:
        return
    hints = {"$schema": "。書き間違えると、Copilot CLI は何も言わずに以前の形式として読み、com.github.copilot/ と mcp.json を読まない"}
    for problem in schema_problems(data, load_schema(SCHEMA_DIR, "plugin.schema.json"), "plugin.json:", hints):
        c.error(problem)
    if isinstance(data, dict):
        ctx.manifest = data
        if isinstance(data.get("name"), str):
            ctx.plugin_name = data["name"]


def schema_version(url: Any) -> Optional[str]:
    match = re.search(r"/schemas/([^/]+)/", url) if isinstance(url, str) else None
    return match.group(1) if match else None


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


MCP_SERVER_DEFS = {"stdio": "stdioServer", "streamable-http": "streamableHttpServer", "sse": "sseServer"}


def mcp_server_problems(schema: Dict[str, Any], name: str, server: Any) -> List[str]:
    """サーバー1つを、その type に合う定義だけで調べる（oneOf のままだと、関係ない選択肢の誤りが出てしまう）。"""
    where = "mcpServers/%s" % name
    if not isinstance(server, dict):
        return ["%s: サーバーの定義が項目の集まりになっていない" % where]
    kind = MCP_SERVER_DEFS.get(server.get("type"))
    if kind is None:
        return ["%s/type: stdio、streamable-http、sse のどれかでなければならない（今は %s）" % (where, short(server.get("type")))]
    sub = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/" + kind}
    errors = sorted(jsonschema.Draft202012Validator(sub).iter_errors(server), key=lambda e: [str(p) for p in e.absolute_path])
    return [describe_schema_error(e, ("mcpServers", name)) for e in errors]


def check_mcp_url(name: str, url: str, c: Check) -> None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as e:
        c.error("mcp.json: %s の url が読めない（%s）" % (name, e))
        return
    if parts.scheme not in ("http", "https") or not host:
        c.error("mcp.json: %s の url %s は絶対の http か https の URL でなければならない" % (name, short(url)))
        return
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        c.error("mcp.json: %s の url にユーザー情報（名前:パスワード@）を含めない" % name)
    if parts.fragment or "#" in url:
        c.error("mcp.json: %s の url に断片（# 以降）を含めない" % name)
    if parts.scheme == "http" and not is_loopback(host):
        c.error("mcp.json: %s の url はループバック（localhost、127.0.0.1、::1）以外では https にする" % name)


def check_mcp_stdio(ctx: Context, name: str, server: Dict[str, Any], c: Check) -> None:
    command = server.get("command")
    if isinstance(command, str):
        if re.search(r"\s", command):
            c.error("mcp.json: %s の command %s は実行ファイル1語だけにする（引数は args に分ける）" % (name, short(command)))
        elif "${" in command:
            c.error("mcp.json: %s の command では ${...} が展開されない" % name)
        elif command.startswith("./"):
            check_inside_root(ctx, name, "command", command, c, must_exist=True)
        elif "/" in command or "\\" in command:
            c.error("mcp.json: %s の command %s は素の名前か ./ で始まるプラグイン相対パスにする" % (name, short(command)))
    for arg in server.get("args") or []:
        if isinstance(arg, str) and "${PLUGIN_ROOT}/" in arg:
            check_inside_root(ctx, name, "args", arg.split("${PLUGIN_ROOT}/", 1)[1], c, must_exist=True, shown=arg)
    cwd = server.get("cwd")
    if isinstance(cwd, str):
        for prefix, base in (("${PLUGIN_DATA}", None), ("${PLUGIN_ROOT}", ctx.root), ("./", ctx.root)):
            if cwd.startswith(prefix):
                rest = cwd[len(prefix):] if prefix != "./" else cwd
                if base is None:   # PLUGIN_DATA は実際の場所が分からないので、書き方だけで閉じ込めを見る
                    resolved = os.path.normpath(os.path.join("/data", rest.lstrip("/")))
                    if not is_within(resolved, "/data"):
                        c.error("mcp.json: %s の cwd %s が PLUGIN_DATA の外に出る" % (name, short(cwd)))
                else:
                    check_inside_root(ctx, name, "cwd", rest.lstrip("/") or ".", c, must_exist=False, shown=cwd)
                break


def check_inside_root(ctx: Context, server: str, field: str, relative: str, c: Check, must_exist: bool,
                      shown: Optional[str] = None) -> None:
    """根からの相対パスが、本当の場所でも根の中に収まるか、あるかを見る。"""
    shown = shown or relative
    target = os.path.normpath(os.path.join(ctx.root, relative))
    if not is_within(target, ctx.root) or not is_within(os.path.realpath(target), ctx.real_root):
        c.error("mcp.json: %s の %s %s が根の外に出る" % (server, field, short(shown)))
    elif must_exist and not os.path.exists(target):
        c.error("mcp.json: %s の %s %s が見つからない" % (server, field, short(shown)))


def check_mcp(ctx: Context, c: Check) -> None:
    path = os.path.join(ctx.root, "mcp.json")
    if not os.path.exists(path):
        c.skip("mcp.json がない")
        return
    data = read_json_file(path, "mcp.json", c)
    if data is None:
        return
    schema = load_schema(SCHEMA_DIR, "mcp.schema.json")
    for e in sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path]):
        path = list(e.absolute_path)
        if e.validator == "oneOf" and len(path) == 2 and path[0] == "mcpServers":
            for problem in mcp_server_problems(schema, path[1], e.instance):   # 種類（type）に合う選択肢だけで調べる
                c.error("mcp.json: " + problem)
        else:
            c.error("mcp.json: " + describe_schema_error(e))
    if not isinstance(data, dict):
        return
    plugin_version = schema_version(ctx.manifest.get("$schema")) if ctx.manifest else None
    mcp_version = schema_version(data.get("$schema"))
    if plugin_version and mcp_version and plugin_version != mcp_version:
        c.error("mcp.json: $schema の版 %s が plugin.json の版 %s と違う（食い違うと MCP だけが無効になる）" % (mcp_version, plugin_version))
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        ctx.mcp_servers = set(servers)
    for name, server in (servers.items() if isinstance(servers, dict) else []):
        if not isinstance(server, dict):
            continue
        if server.get("type") == "stdio":
            check_mcp_stdio(ctx, name, server, c)
        elif server.get("type") in ("streamable-http", "sse"):
            if isinstance(server.get("url"), str):
                check_mcp_url(name, server["url"], c)
            headers = server.get("headers")
            if isinstance(headers, dict):
                seen: Dict[str, str] = {}
                for header in headers:
                    if header.lower() in seen:
                        c.error("mcp.json: %s の headers の名前 %s と %s が（大文字小文字を除くと）重なっている" % (name, seen[header.lower()], header))
                    seen[header.lower()] = header


SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Agent Skills の項目。これ以外（user-invocable、disable-model-invocation、argument-hint など）は書かない（R1、仕様 7.1）
SKILL_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
SKILL_BODY_LINES = 500
REFERENCE_MAX_BYTES = 20000   # 「20KB 未満」。view が読まない上限を超えないよう、1KB を 1000 バイトとして数える


def check_skill_file(path: str, folder: str, c: Check) -> None:
    label = "skills/%s/SKILL.md:" % folder
    meta, body = parse_frontmatter(path, label[:-1], c)
    if meta is None:
        return
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not name:
        c.error("%s name がない" % label)
    elif not SKILL_NAME.match(name) or len(name) > 64:
        c.error("%s name %s は小文字の英数字とハイフンだけ（先頭と末尾はハイフン不可、-- 不可）で64文字以内にする" % (label, short(name)))
    elif name != folder:
        c.error("%s name %s がフォルダの名前 %s と違う" % (label, short(name), folder))
    if not isinstance(description, str) or not description.strip():
        c.error("%s description がない（何ができるかと、いつ使うかを書く）" % label)
    else:
        if len(description) > 1024:
            c.error("%s description が %d 文字ある（1024 文字以内）" % (label, len(description)))
        if "<" in description or ">" in description:
            c.error("%s description に < や > を入れない（XML のタグと見なされる）" % label)
        if re.match(r"\s*(I|You)\s+can\b", description, re.I):
            c.error("%s description を「I can …」「You can …」の形にしない（スキルの内容を三人称で書く）" % label)
    if "license" in meta and not isinstance(meta["license"], str):
        c.error("%s license は文字にする" % label)
    comp = meta.get("compatibility")
    if "compatibility" in meta and (not isinstance(comp, str) or not 1 <= len(comp) <= 500):
        c.error("%s compatibility は1〜500文字の文字にする" % label)
    md = meta.get("metadata")
    if "metadata" in meta and (not isinstance(md, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in md.items())):
        c.error("%s metadata は文字から文字への表にする" % label)
    tools = meta.get("allowed-tools")
    if "allowed-tools" in meta:
        if not (isinstance(tools, str) or (isinstance(tools, list) and all(isinstance(t, str) for t in tools))):
            c.error("%s allowed-tools は文字（空白区切り）にする" % label)
        c.warn("%s allowed-tools は書かない決まり（スキルが呼ばれると、そのツールが確認なしで許可される）。要るときは書く前にユーザーに確かめる" % label)
    for key in meta:
        if key not in SKILL_FIELDS:
            c.error("%s %s は Agent Skills の項目にない（Agent Plugins は Agent Skills に従わないスキルを読み飛ばす。書くのは name と description だけにする）" % (label, key))
    lines = len(body.splitlines())
    if lines > SKILL_BODY_LINES:
        c.warn("%s 本文が %d 行ある（%d 行以内が勧め）" % (label, lines, SKILL_BODY_LINES))


def skill_folders(ctx: Context) -> List[Tuple[str, str]]:
    """(フォルダの名前, SKILL.md の場所)。skills/ の直下の子フォルダだけ。"""
    base = os.path.join(ctx.root, "skills")
    found = []
    for folder in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        path = os.path.join(base, folder, "SKILL.md")
        if os.path.isfile(path):
            found.append((folder, path))
    return found


def check_skills(ctx: Context, c: Check, kind: str) -> None:
    base = os.path.join(ctx.root, "skills")
    if not os.path.isdir(base):
        if kind == "generated":
            c.error("skills/ がない（入口のスキルが要る）")
        else:
            c.skip("skills/ がない")
        return
    for folder in sorted(os.listdir(base)):
        if os.path.isdir(os.path.join(base, folder)) and not os.path.isfile(os.path.join(base, folder, "SKILL.md")):
            c.warn("skills/%s/: SKILL.md がない（スキルとして読まれない）" % folder)
    found = skill_folders(ctx)
    for folder, path in found:
        check_skill_file(path, folder, c)
    if kind == "generated" and not found:
        c.error("skills/ に入口のスキル（skills/<名前>/SKILL.md）がない")


def check_references(ctx: Context, c: Check) -> None:
    """references の各ファイルが view で読める大きさで、SKILL.md の本文が名指ししている。"""
    checked = 0
    for folder, path in skill_folders(ctx):
        refs = os.path.join(os.path.dirname(path), "references")
        if not os.path.isdir(refs):
            continue
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
        for directory, dirs, files in os.walk(refs):
            if directory != refs:
                c.warn("skills/%s/references/: 入れ子のフォルダ %s がある（SKILL.md から1段で参照できる場所に置く）" % (folder, rel(ctx, directory)))
            for file in sorted(files):
                checked += 1
                full = os.path.join(directory, file)
                size = os.path.getsize(full)
                if size >= REFERENCE_MAX_BYTES:
                    c.error("%s が %d バイトある（20KB 未満にする。view は大きいファイルを読まない）" % (rel(ctx, full), size))
                if file not in text:
                    c.error("%s を SKILL.md の本文で名指ししていない（いつ読むかを本文に書く）" % rel(ctx, full))
    if not checked:
        c.skip("references のファイルがない")


AGENT_FIELDS = {"description", "name", "model", "models", "model-policy", "modelPolicy", "tools", "mcp-servers",
                "include-custom-instructions", "infer", "reasoningEffort", "reasoning-effort", "target",
                "disable-model-invocation", "user-invocable", "metadata"}
# 書いてよいが、効くかが確かめられていない、または既定のままでよい項目（R1）
AGENT_DISCOURAGED = {"models", "modelPolicy", "model-policy", "reasoningEffort", "reasoning-effort",
                     "include-custom-instructions", "infer", "user-invocable"}
AGENT_BODY_MAX = 30000
AGENT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOOL_ALIASES = {"execute", "read", "edit", "search", "agent", "web", "todo"}
# Claude Code 互換の名前（公式のコマンドリファレンスが受け付けるとしているもの）
TOOL_COMPAT = {"shell", "bash", "powershell", "notebookread", "multiedit", "write", "notebookedit", "grep", "glob",
               "task", "custom-agent"}
BUILTIN_MCP_SERVERS = {"github-mcp-server"}


def string_or_list(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip())) or (
        isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value))


def agent_tools(meta: Dict[str, Any]) -> Optional[List[str]]:
    """tools を文字のリストにする。書かれていなければ None。"""
    value = meta.get("tools")
    if value is None:
        return None
    if isinstance(value, str):
        return [t for t in re.split(r"[,\s]+", value) if t]
    if isinstance(value, list):
        return [t.strip() for t in value if isinstance(t, str) and t.strip()]
    return []


def check_agent_tool_names(ctx: Context, label: str, meta: Dict[str, Any], tools: List[str], c: Check) -> None:
    """tools の各名前が、別名・このプラグインのエクステンションのツール・MCP のツールのどれかか（知らない名前は黙って無視される）。"""
    own_servers = set(meta["mcp-servers"]) if isinstance(meta.get("mcp-servers"), dict) else set()
    servers = ctx.mcp_servers | own_servers | BUILTIN_MCP_SERVERS
    for tool in tools:
        lowered = tool.lower()
        if tool == "*":
            c.error("%s tools に * がある（すべてのツールとスキルが使える）" % label)
        elif lowered == "skill":
            c.error("%s tools に skill がある（サブエージェントがスキルを呼び、メインのエージェント向けの手順を始めてしまう）" % label)
        elif lowered in TOOL_ALIASES or lowered in TOOL_COMPAT:
            continue
        elif "/" in tool:
            if tool.split("/", 1)[0] not in servers:
                c.error("%s tools の %s は、mcp.json か mcp-servers にないサーバーのツール（黙って無視される）" % (label, tool))
        elif ctx.plugin_name and tool.startswith(ctx.plugin_name + "_"):
            continue
        elif RECORD_TOOL.match(tool):
            continue   # 記録のツールの名前の頭は、試験「記録のツールの名前の頭がプラグインの名前」が見る
        else:
            c.error("%s tools の %s は、別名（%s）でも、このプラグインのエクステンションのツール（%s_…）でも、"
                    "MCP のツール（サーバー名/ツール名）でもない（知らない名前は黙って無視される）"
                    % (label, tool, "、".join(sorted(TOOL_ALIASES)), ctx.plugin_name or "<プラグインの名前>"))


def check_agents(ctx: Context, c: Check) -> None:
    base = os.path.join(ctx.root, "com.github.copilot", "agents")
    if not os.path.isdir(base):
        c.skip("com.github.copilot/agents/ がない")
        return
    for file in sorted(os.listdir(base)):
        path = os.path.join(base, file)
        if os.path.isdir(path):
            continue
        label = "com.github.copilot/agents/%s:" % file
        if not file.endswith(".agent.md"):
            c.warn("%s ファイル名が *.agent.md ではない（エージェントとして読まれないことがある）" % label)
            continue
        meta, body = parse_frontmatter(path, label[:-1], c)
        if meta is None:
            continue
        agent_id = file[: -len(".agent.md")]
        ctx.agents[agent_id] = (meta, body)
        if not AGENT_ID.match(agent_id):
            c.error("%s ファイル名（エージェントの ID）は小文字の英数字とハイフンにする" % label)
        if not isinstance(meta.get("description"), str) or not meta["description"].strip():
            c.error("%s description がない（必須）" % label)
        if "name" in meta and meta["name"] != agent_id:
            c.error("%s name %s がファイル名の ID %s と違う（name は書かないか、ID と同じにする）" % (label, short(meta["name"]), agent_id))
        for key in ("model", "models"):   # 1つでも、優先する順のリストでもよい
            if key in meta and not string_or_list(meta[key]):
                c.error("%s %s はモデルの名前（1つ）か、優先する順の名前のリストにする" % (label, key))
        if "tools" in meta and not (isinstance(meta["tools"], str) or (
                isinstance(meta["tools"], list) and all(isinstance(t, str) for t in meta["tools"]))):
            c.error("%s tools は文字か、文字のリストにする" % label)
        tools = agent_tools(meta)
        if agent_id == "orchestrator":
            pass   # 統括は tools を書かない（検査 agent-tools が見る）
        elif tools is None:
            c.warn("%s tools がない（書かないと、すべてのツールとスキルが使える）" % label)
        else:
            check_agent_tool_names(ctx, label, meta, tools, c)
        if meta.get("disable-model-invocation") is True:
            c.error("%s disable-model-invocation: true がある（task で起動するエージェントに書くと、起動が断られる）" % label)
        for key in meta:
            if key not in AGENT_FIELDS:
                c.warn("%s %s は公式ドキュメントにない項目" % (label, key))
            elif key in AGENT_DISCOURAGED:
                c.warn("%s %s は書かない決まり（効くかが確かめられていない、または既定のままでよい）" % (label, key))
        if len(body) > AGENT_BODY_MAX:
            c.error("%s 本文が %d 文字ある（%d 文字以内）" % (label, len(body), AGENT_BODY_MAX))


LEGACY_PATHS = [
    ("agents", "com.github.copilot/agents/ に移す"), ("commands", "com.github.copilot/commands/ に移す"),
    ("rules", "com.github.copilot/rules/ に移す"), ("hooks", "com.github.copilot/hooks/ に移す"),
    ("hooks.json", "com.github.copilot/hooks/hooks.json に移す"), ("lsp.json", "com.github.copilot/lsp.json に移す"),
    (".mcp.json", "mcp.json に直す"), ("extensions", "com.github.copilot/extensions/ に移す"),
    (".claude-plugin", "plugin.json は根に置く"), (".github/plugin/plugin.json", "plugin.json は根に置く"),
]
COPILOT_ENTRIES = {"agents", "commands", "rules", "hooks", "extensions", "lsp.json"}
EXTENSION_FILES = ("extension.mjs", "extension.cjs", "extension.js")


def check_layout(ctx: Context, c: Check) -> None:
    for name, fix in LEGACY_PATHS:
        if os.path.lexists(os.path.join(ctx.root, *name.split("/"))):
            c.error("%s: 以前の形式の置き場所で、Agent Plugins の形式では読まれない（%s）" % (name, fix))
    base = os.path.join(ctx.root, "com.github.copilot")
    if not os.path.isdir(base):
        return
    for entry in sorted(os.listdir(base)):
        if entry not in COPILOT_ENTRIES:
            c.error("com.github.copilot/%s: この場所は決まりにない（使えるのは %s）" % (entry, "、".join(sorted(COPILOT_ENTRIES))))
    ext_base = os.path.join(base, "extensions")
    if os.path.isdir(ext_base):
        for entry in sorted(os.listdir(ext_base)):
            path = os.path.join(ext_base, entry)
            if not os.path.isdir(path):
                c.error("com.github.copilot/extensions/%s: エクステンションはフォルダにして、中に extension.mjs を置く" % entry)
            elif not any(os.path.isfile(os.path.join(path, f)) for f in EXTENSION_FILES):
                c.error("com.github.copilot/extensions/%s/: 入口のファイル（extension.mjs）がない" % entry)


# plugin-creator が改善・設計変更のときに作る、作業用の一時フォルダ（下書きと下見。終わったら消える。ひな形の .gitignore）。
# プラグインのフォルダが Copilot CLI を起動したフォルダと同じときは、プラグインのフォルダの中にできるので、フォルダ全体を歩く検査では飛ばす。
# 下見の中の古い文（外した番号など）を拾わないためである。
WORK_DIR = ".plugin-creator-work"

# Copilot CLI に読み込まれない、試験の道具の置き場所（git にも入れない。ひな形の .gitignore）。仮想環境の bin/python は
# 外の Python へのリンクなので、中を調べると、仮想環境を作っただけで必ず不合格になる。調べないのは、この2つのフォルダと、
# 作業用の一時フォルダ（WORK_DIR）だけ。
TOOL_FOLDERS = (("tests", ".venv"), ("tests", "node_modules"))


def check_symlinks(ctx: Context, c: Check) -> None:
    tool_folders = {os.path.join(ctx.root, *parts) for parts in TOOL_FOLDERS}
    for directory, dirs, files in os.walk(ctx.root):
        dirs[:] = [d for d in dirs if d not in (".git", WORK_DIR) and os.path.join(directory, d) not in tool_folders]
        for entry in dirs + files:
            path = os.path.join(directory, entry)
            if os.path.islink(path) and not is_within(os.path.realpath(path), ctx.real_root):
                c.error("%s: 根の外を指すシンボリックリンク（読み込みで拒まれる）" % rel(ctx, path))


# ---------------------------------------------------------------- 生成プラグインの検査

def extension_dir(ctx: Context) -> Optional[str]:
    if not ctx.plugin_name:
        return None
    return os.path.join(ctx.root, "com.github.copilot", "extensions", ctx.plugin_name)


def task_criteria(task: Any) -> Dict[str, Dict[str, Any]]:
    """作業の定義の合否の基準を、番号 -> 基準 の表にする（形が崩れていても読める分だけ）。"""
    items = task.get("criteria") if isinstance(task, dict) else None
    return {i["id"]: i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)} if isinstance(items, list) else {}


def check_task(ctx: Context, c: Check) -> None:
    folder = extension_dir(ctx)
    if folder is None:
        c.error("plugin.json の name が読めないので、作業の定義の場所（com.github.copilot/extensions/<名前>/）が分からない")
        return
    shown = rel(ctx, folder)
    task_path = os.path.join(folder, "task.json")
    schema_path = os.path.join(folder, "core", "task.schema.json")
    for path in (task_path, schema_path):
        if not os.path.isfile(path):
            c.error("%s がない" % rel(ctx, path))
    if not (os.path.isfile(task_path) and os.path.isfile(schema_path)):
        return
    schema = read_json_file(schema_path, rel(ctx, schema_path), c)
    task = read_json_file(task_path, rel(ctx, task_path), c)
    if schema is None or task is None:
        return
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        c.error("%s: スキーマ自体が誤っている（%s）" % (rel(ctx, schema_path), e.message))
        return
    ctx.task, ctx.task_path = task, task_path
    label = "%s/task.json:" % shown
    for problem in schema_problems(task, schema, label):
        c.error(problem)
    if not isinstance(task, dict):
        return
    # JSON Schema では書けない決まり：番号が重ならない、作業エージェントのファイルがある、選択肢に初めの値がある
    for field, items_key in (("criteria", "id"), ("workers", "agent"), ("settings", "key"), ("displays", "key")):
        seen: Set[str] = set()
        for item in task.get(field) if isinstance(task.get(field), list) else []:
            value = item.get(items_key) if isinstance(item, dict) else None
            if isinstance(value, str):
                if value in seen:
                    c.error("%s %s の %s %s が重なっている" % (label, field, items_key, value))
                seen.add(value)
    for worker in task.get("workers") if isinstance(task.get("workers"), list) else []:
        agent = worker.get("agent") if isinstance(worker, dict) else None
        if isinstance(agent, str) and not os.path.isfile(os.path.join(ctx.root, "com.github.copilot", "agents", agent + ".agent.md")):
            c.error("%s workers の %s に対応する com.github.copilot/agents/%s.agent.md がない" % (label, agent, agent))
    for setting in task.get("settings") if isinstance(task.get("settings"), list) else []:
        if isinstance(setting, dict) and setting.get("type") == "select" and isinstance(setting.get("options"), list):
            if isinstance(setting.get("default"), str) and setting["default"] and setting["default"] not in setting["options"]:
                c.error("%s settings の %s の初めの値 %s が選択肢にない" % (label, setting.get("key"), short(setting["default"])))


# ---- EVAL の課題（eval.json）。形と採点のしかたの決まりは skills/test-plugin/scripts/grade.mjs の先頭の表と同じにする。
# 2つがずれていないことは、tests/creator/stage1/ の試験（同じ誤りの例を両方に通す）が見張る。

GRADINGS = ("program", "model")
CHECK_TYPES: Dict[str, Tuple[List[str], List[str]]] = {   # 種類 -> (必須の項目, 任意の項目)
    "file_exists": (["file"], []),
    "contains": (["file", "text"], []),
    "not_contains": (["file", "text"], []),
    "regex": (["file", "pattern"], ["flags"]),
    "unchanged": (["file"], []),   # file に当てはまる課題の入力のファイルが、もとのまま残っている
    "agent_ran": (["agent"], []),  # その実行で、agent のエージェント（com.github.copilot/agents/<agent>.agent.md）が動いた。file を持たない
}
REGEX_FLAGS = "imsu"
# 課題の bin に書ける、ただ1つの場所。擬似の機器の道具（差し替えのスクリプト）を置く、課題の入力のフォルダ。
# test-plugin は、作業フォルダに写したこのフォルダを、その課題の実行の PATH の先頭に足す。
BIN_DIR = "inputs/bin"
Regexes = List[Tuple[str, str, str]]   # (場所, pattern, flags)


def is_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_integer(value: Any) -> bool:
    return is_number(value) and value == int(value)


def normalize_rel(path: Any) -> str:
    return unicodedata.normalize("NFC", str(path).replace("\\", "/"))


def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """作業フォルダの根からの相対のグロブを正規表現にする（grade.mjs の globToRegExp と同じ書き方）。"""
    p = normalize_rel(pattern)
    out, i = "", 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i + 1:i + 2] == "*":
                i += 1
                if p[i + 1:i + 2] == "/":
                    i += 1
                    out += "(?:.*/)?"
                else:
                    out += ".*"
            else:
                out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            end = p.find("]", i + 2)
            if end == -1:
                out += "\\["
            else:
                body = p[i + 1:end]
                if body[:1] == "!":
                    body = "^" + body[1:]
                out += "[" + body.replace("\\", "\\\\") + "]"
                i = end
        elif c == "{":
            end = p.find("}", i)
            if end == -1:
                out += "\\{"
            else:
                out += "(?:" + "|".join(re.escape(x) for x in p[i + 1:end].split(",")) + ")"
                i = end
        else:
            out += re.escape(c)
        i += 1
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return re.compile("^" + out + "$")


def glob_problems(value: Any, label: str) -> List[str]:
    if not is_text(value):
        return ["%s は、作業フォルダの根からの場所（グロブ）を文字で書く" % label]
    out = []
    if "\\" in value:
        out.append("%s は、場所の区切りを / で書く（\\ は使えない）" % label)
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        out.append("%s は、作業フォルダの根からの相対の場所で書く（絶対の場所は使えない）" % label)
    if ".." in value.split("/"):
        out.append("%s に .. は使えない" % label)
    try:
        glob_to_regex(value)
    except re.error as e:
        out.append("%s がグロブとして読めない（%s）" % (label, e))
    return out


def check_problems(check: Any, label: str, regexes: Regexes) -> List[str]:
    """program の基準の check を確かめる。regex の pattern は regexes に集める（あとで node で読めるかを見る）。"""
    if not isinstance(check, dict):
        return ["%s がない（program の基準には check が要る）" % label]
    spec = CHECK_TYPES.get(check.get("type")) if isinstance(check.get("type"), str) else None
    if spec is None:
        shown = check.get("type") if isinstance(check.get("type"), str) else ""
        return ["%s.type「%s」は使えない（使えるのは %s）" % (label, shown, "・".join(CHECK_TYPES))]
    out = []
    allowed = {"type", *spec[0], *spec[1]}
    out += ["%s に、%s では使わない項目「%s」がある" % (label, check["type"], k) for k in check if k not in allowed]
    out += ["%s に %s がない（%s には要る）" % (label, k, check["type"]) for k in spec[0] if k not in check]
    if "file" in check:
        out += glob_problems(check["file"], label + ".file")
    if check["type"] == "agent_ran" and "agent" in check and not (isinstance(check["agent"], str) and AGENT_ID.match(check["agent"])):
        out.append("%s.agent は、エージェントの名前（com.github.copilot/agents/<名前>.agent.md の <名前>。プラグインの名前は付けない）で書く" % label)
    if check["type"] in ("contains", "not_contains") and "text" in check and not is_text(check["text"]):
        out.append("%s.text は、探す文字を空でない文字で書く" % label)
    if check["type"] == "regex" and "pattern" in check:
        flags = check.get("flags")
        flags = "" if flags is None else flags   # 採点（check.flags ?? ''）と同じく、null は書いていないものとする
        if not is_text(check["pattern"]):
            out.append("%s.pattern は、正規表現を空でない文字で書く" % label)
        elif not isinstance(flags, str) or any(f not in REGEX_FLAGS for f in flags) or len(set(flags)) != len(flags):
            out.append("%s.flags は、i・m・s・u のうち使うものを並べて書く" % label)
        else:
            regexes.append((label + ".pattern", check["pattern"], flags))
    return out


def model_check_problems(check: Any, label: str) -> List[str]:
    """model の基準の check（省いてもよい）を確かめる。"""
    if not isinstance(check, dict):
        return ["%s は、{ \"files\": [グロブ] } の形で書く（省いてもよい）" % label]
    out = ["%s に、model の基準では使わない項目「%s」がある" % (label, k) for k in check if k != "files"]
    if "files" in check:
        files = check["files"]
        if not isinstance(files, list) or not files:
            out.append("%s.files は、グロブの並びを書く" % label)
        else:
            for i, g in enumerate(files):
                out += glob_problems(g, "%s.files[%d]" % (label, i))
    return out


def bin_problems(task: Dict[str, Any], label: str) -> List[str]:
    """課題の bin（擬似の機器の道具のフォルダ）の形。inputs/bin だけを書け、inputs に inputs/bin かその下のファイルがある。"""
    if task["bin"] != BIN_DIR:
        return ["%s.bin は \"%s\" だけを書ける（擬似の機器の道具を置く、課題の入力のフォルダ）" % (label, BIN_DIR)]
    items = task.get("inputs") if isinstance(task.get("inputs"), list) else []
    under = [i for i in items if is_text(i) and (posixpath.normpath(normalize_rel(i)) + "/").startswith(BIN_DIR + "/")]
    if not under:
        return ["%s.inputs に、%s か、その下のファイルがない（bin のフォルダも、課題の入力として作業フォルダに写す）" % (label, BIN_DIR)]
    return []


def eval_task_problems(task: Any, label: str, regexes: Regexes) -> List[str]:
    """eval.json の1課題の形を確かめる。"""
    if not isinstance(task, dict):
        return ["%s が JSON のオブジェクトではない" % label]
    out = []
    if not is_text(task.get("id")):
        out.append("%s に id がない" % label)
    if not is_text(task.get("prompt")):
        out.append("%s に prompt（頼むときの言葉）がない" % label)
    if "inputs" in task:
        if not isinstance(task["inputs"], list):
            out.append("%s.inputs は、入力のファイルの場所の並びで書く" % label)
        else:
            for i, item in enumerate(task["inputs"]):
                if not is_text(item) or item.startswith("/") or re.match(r"^[A-Za-z]:", item) or ".." in normalize_rel(item).split("/"):
                    out.append("%s.inputs[%d] は、課題のフォルダの中の相対の場所で書く" % (label, i))
    if "bin" in task:
        out += bin_problems(task, label)
    if "settings" in task:
        settings = task["settings"]
        if not isinstance(settings, dict):
            out.append("%s.settings は、{ \"maxIterations\": 数, \"task\": {...} } の形で書く" % label)
        else:
            if "maxIterations" in settings and not (is_integer(settings["maxIterations"]) and settings["maxIterations"] >= 1):
                out.append("%s.settings.maxIterations は、1以上の整数で書く" % label)
            if "task" in settings:
                if not isinstance(settings["task"], dict):
                    out.append("%s.settings.task は、設定の名前と値の組で書く" % label)
                else:
                    for k, v in settings["task"].items():
                        if not (isinstance(v, (str, bool)) or is_number(v)):
                            out.append("%s.settings.task.%s の値は、文字・数・真偽のどれかで書く" % (label, k))
    criteria = task.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        out.append("%s に criteria（合否の基準）がない" % label)
        return out
    seen: Set[str] = set()
    for i, item in enumerate(criteria):
        where = "%s.criteria[%d]" % (label, i)
        if not isinstance(item, dict):
            out.append("%s がオブジェクトではない" % where)
            continue
        if not is_text(item.get("id")):
            out.append("%s に id がない" % where)
        elif item["id"] in seen:
            out.append("%s の id「%s」が重なっている" % (where, item["id"]))
        else:
            seen.add(item["id"])
        if not is_text(item.get("text")):
            out.append("%s に text（基準の文）がない" % where)
        if item.get("grading") not in GRADINGS:
            out.append("%s.grading は program か model で書く" % where)
        elif item["grading"] == "program":
            out += check_problems(item.get("check"), where + ".check", regexes)
        elif "check" in item:
            out += model_check_problems(item["check"], where + ".check")
    return out


def js_regex_errors(regexes: Regexes) -> Dict[str, str]:
    """pattern が JavaScript の正規表現として読めるかを node で確かめる（採点が JavaScript で行われるため）。場所 -> 誤りの文。
    node がない・動かないときは None の代わりに空の辞書を返し、確かめなかったことは呼び出し側が注意にする。"""
    script = ("const items = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "console.log(JSON.stringify(items.map(([p, f]) => { try { new RegExp(p, f); return null; } catch (e) { return e.message; } })));")
    try:
        done = subprocess.run(["node", "-e", script], input=json.dumps([[p, f] for _, p, f in regexes]).encode("utf-8"),
                              capture_output=True, timeout=30)
        results = json.loads(done.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise RuntimeError("node を動かせなかった")
    return {where: message for (where, _, _), message in zip(regexes, results) if message}


def settings_problems(task_def: Dict[str, Any], given: Dict[str, Any], label: str) -> List[str]:
    """課題の settings.task の値が、作業の定義の settings（画面の欄）に合うか。合わないと、EVAL が画面を操作できない。"""
    settings = task_def.get("settings")
    defs = {d["key"]: d for d in settings if isinstance(d, dict) and isinstance(d.get("key"), str)} if isinstance(settings, list) else {}
    out = []
    for key, value in given.items():
        d = defs.get(key)
        if d is None:
            out.append("%s.settings.task の「%s」が、作業の定義の settings にない（画面に欄がないので、EVAL が値を入れられない）" % (label, key))
            continue
        kind = d.get("type")
        options = [o.get("value") if isinstance(o, dict) else o for o in d.get("options", [])] if isinstance(d.get("options"), list) else []
        if kind in ("text", "path") and not isinstance(value, str):
            out.append("%s.settings.task.%s は、%s の設定なので文字で書く" % (label, key, kind))
        elif kind == "number" and not is_number(value):
            out.append("%s.settings.task.%s は、number の設定なので数で書く" % (label, key))
        elif kind == "checkbox" and not isinstance(value, bool):
            out.append("%s.settings.task.%s は、checkbox の設定なので true か false で書く" % (label, key))
        elif kind == "select" and value not in options:
            out.append("%s.settings.task.%s の値 %s が、作業の定義の選択肢（%s）にない" % (label, key, short(value), "、".join(map(str, options))))
    for key, d in defs.items():   # 必須で、初めの値がない設定は、課題が値を入れないと画面で開始できない
        default = d.get("default")
        empty_default = default is None or (isinstance(default, str) and default.strip() == "")
        given_value = given.get(key)
        missing = given_value is None or (isinstance(given_value, str) and given_value.strip() == "")
        if d.get("required") and d.get("type") != "checkbox" and empty_default and missing:
            out.append("%s.settings.task に、必須の設定「%s」（%s）の値がない（初めの値もないので、画面で開始できない）" % (label, d.get("label", key), key))
    return out


def input_problems(task_dir: str, items: List[Any], label: str) -> List[str]:
    out = []
    for item in items:
        if not is_text(item):
            continue
        source = os.path.join(task_dir, *normalize_rel(item).split("/"))
        if not os.path.lexists(source):
            out.append("%s: 入力のファイル %s がない" % (label, item))
        elif os.path.islink(source):
            out.append("%s: 入力「%s」はシンボリックリンクなので使えない" % (label, item))
        elif os.path.isdir(source):
            for directory, dirs, files in os.walk(source):
                for entry in dirs + files:
                    if os.path.islink(os.path.join(directory, entry)):
                        out.append("%s: 入力「%s」の中に、シンボリックリンクがある（%s）" % (label, item, entry))
    return out


# ---- 課題の入力と突き合わせる確かめ（grade.mjs の inputCheckProblems と同じ決まり）。
# 採点は、中身が変わっていない入力を成果物に数えない。そのため、unchanged のほかの4種類で、課題の入力のファイル（もとの中身）だけを
# 成果物とみなして合格してしまうものは誤りにする（入力を変えないと合格せず、変えれば中身にかかわらず合格に近づくため）。
# unchanged の file は、課題の入力のファイルのどれかに当てはまらなければならない。
# 文字の読み方は採点（grade.mjs の readPlainText）と同じにする。ただし docx・xlsx・pptx の入力は、文字を取り出さず「中身が分からない」とする
# （ここでは採点と同じには取り出せないため）。文字にできない入力（0 のバイトがある、大きすぎる、文字コードが読めない）も同じ。
# 分からない入力では、合格してしまうとは決めない（contains・regex は文字にできた入力だけで決め、not_contains は分からない入力があれば誤りにしない）。
# file_exists は中身を読まず、大きさだけで決める。regex は JavaScript の正規表現なので、node で確かめる（node がなければ注意にする）。

OFFICE_EXTENSIONS = (".docx", ".xlsx", ".pptx")
CHECK_MAX_BYTES = 20 * 1024 * 1024   # 採点が読む1ファイルの大きさの上限
CHECK_MAX_CHARS = 2000000            # 採点が読む1ファイルの文字数の上限
GRADER_SKIP_DIRS = {"node_modules", ".git"}   # 採点が見ないフォルダ
INPUT_CHECK_HINT = ("採点は、中身が変わっていない入力を成果物に数えないので、作業をする前の入力で合格してしまう確かめ方は使えない。"
                    "file_exists・contains・not_contains・regex は、もとの入力のままでは合格せず、作業をして初めて合格するものにだけ使う"
                    "（グロブを作業が作るファイルに絞る、作業が書き足す文字を確かめるなど）。"
                    "入力のファイルを変えていないことは、確かめ方の種類 unchanged（file）で確かめる。"
                    "作業が変える入力に、もとからある文字が残ることなどは、model で判定する")   # grade.mjs の INPUT_CHECK_HINT と同じ文
InputFile = Tuple[str, str, int]   # (作業フォルダの根からの相対の場所, 実物の場所, 大きさ)
RegexProbes = List[Tuple[str, str, str, List[Tuple[str, str]]]]   # (場所, pattern, flags, [(相対の場所, 文字)])


def input_files(task_dir: str, items: List[Any]) -> List[InputFile]:
    """課題の入力を作業フォルダに写したときのファイルの一覧（tasks.mjs の listInputFiles と同じ）。フォルダは中のファイルをすべて数える。"""
    found: Dict[str, InputFile] = {}

    def walk(path: str, relative: str) -> None:
        if os.path.islink(path):
            return
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                walk(os.path.join(path, name), relative + "/" + name if relative else name)
        elif os.path.isfile(path):
            key = normalize_rel(relative)
            found[key] = (key, path, os.path.getsize(path))

    for item in items:
        if not is_text(item):
            continue
        relative = posixpath.normpath(normalize_rel(item))
        relative = "" if relative == "." else relative
        walk(os.path.join(task_dir, *relative.split("/")) if relative else task_dir, relative)
    return [found[k] for k in sorted(found)]


def input_text(path: str) -> Optional[str]:
    """入力のファイルの中の文字（採点と同じ読み方。改行と Unicode の正規化もそろえる）。中身が分からなければ None。"""
    if os.path.splitext(path)[1].lower() in OFFICE_EXTENSIONS:
        return None
    try:
        if os.path.getsize(path) > CHECK_MAX_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data:
        return None
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = sjis_text(data)
    if text is None:
        return None
    return unify_text(text[:CHECK_MAX_CHARS])


# 採点の TextDecoder("shift_jis")（Node の ICU）と cp932 の違い。ほかの1・2バイトの並びは、すべて同じに読む。
#   cp932 は 1バイトの 0x80・0xA0・0xFD・0xFE・0xFF を U+0080・U+F8F0〜U+F8F3 に読むが、採点は読めない（文字にできない）とする。
#   制御文字の 0x1A・0x1C・0x7F は、採点では U+001C・U+007F・U+001A に入れ替わる。
SJIS_UNREADABLE = re.compile("[\u0080\uf8f0-\uf8f3]")
SJIS_SWAP = {0x1A: 0x1C, 0x1C: 0x7F, 0x7F: 0x1A}


def sjis_text(data: bytes) -> Optional[str]:
    """Shift_JIS のバイト列を、採点（TextDecoder の shift_jis）と同じに読む。読めなければ None。"""
    try:
        text = data.decode("cp932")
    except UnicodeDecodeError:
        return None
    if SJIS_UNREADABLE.search(text):
        return None
    return text.translate(SJIS_SWAP)


def unify_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")


def names_of(paths: List[str], limit: int = 3) -> str:
    shown = "、".join(paths[:limit])
    return shown + ("、ほか%d件" % (len(paths) - limit) if len(paths) > limit else "")


def input_check_problems(task: Dict[str, Any], files: List[InputFile], label: str, probes: RegexProbes) -> List[str]:
    """課題の入力のファイルと突き合わせて、基準の check を確かめる。regex は probes に集める（あとで node で確かめる）。"""
    out = []
    candidates = [f for f in files if not any(part in GRADER_SKIP_DIRS for part in f[0].split("/"))]
    for i, item in enumerate(task.get("criteria") or []):
        if not isinstance(item, dict) or item.get("grading") != "program" or not isinstance(item.get("check"), dict):
            continue
        where = "%s.criteria[%d].check" % (label, i)
        check = item["check"]
        if check["type"] == "agent_ran":   # 作業フォルダのファイルを見ない種類
            continue
        matcher = glob_to_regex(check["file"])
        if check["type"] == "unchanged":
            if not any(matcher.match(f[0]) for f in files):
                out.append("%s.file「%s」が、課題の入力のファイルのどれにも当てはまらない（unchanged は、課題の入力を変えていないことを確かめる種類）"
                           % (where, check["file"]))
            continue
        matched = [f for f in candidates if matcher.match(f[0])]
        if not matched:
            continue
        if check["type"] == "file_exists":
            filled = [f[0] for f in matched if f[2] > 0]
            if filled:
                out.append("%s は、課題の入力のファイルがもとのままでも合格してしまう（「%s」に当てはまるファイルがある（%s））。%s"
                           % (where, check["file"], names_of(filled), INPUT_CHECK_HINT))
            continue
        texts = [(f[0], input_text(f[1])) for f in matched]
        readable = [(p, t) for p, t in texts if t is not None]
        if check["type"] == "regex":
            if readable:
                probes.append((where, check["pattern"], check.get("flags") or "", readable))
            continue
        needle = unify_text(check["text"])
        hit = next((p for p, t in readable if needle in t), None)
        if check["type"] == "contains" and hit is not None:
            out.append("%s は、課題の入力のファイルがもとのままでも合格してしまう（「%s」に「%s」がある）。%s"
                       % (where, hit, short_text(check["text"]), INPUT_CHECK_HINT))
        elif check["type"] == "not_contains" and hit is None and len(readable) == len(matched):
            out.append("%s は、課題の入力のファイルがもとのままでも合格してしまう（「%s」はどのファイルにもない（調べたファイル：%s））。%s"
                       % (where, short_text(check["text"]), names_of([f[0] for f in matched]), INPUT_CHECK_HINT))
    return out


def short_text(text: str, limit: int = 40) -> str:
    """grade.mjs の clip と同じく、長い文字を切って … を付ける。"""
    return text if len(text) <= limit else text[:limit] + "…"


def js_regex_hits(probes: RegexProbes) -> Dict[str, str]:
    """regex の pattern が、入力の文字のどれかに合うかを node で確かめる。場所 -> 合った入力の相対の場所。node がなければ RuntimeError。"""
    script = ("const items = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "console.log(JSON.stringify(items.map(([p, f, texts]) => { let re; try { re = new RegExp(p, f); } catch (e) { return null; }"
              " const hit = texts.find(([, t]) => re.test(t)); return hit ? hit[0] : null; })));")
    try:
        done = subprocess.run(["node", "-e", script],
                              input=json.dumps([[p, f, [list(t) for t in texts]] for _, p, f, texts in probes]).encode("utf-8"),
                              capture_output=True, timeout=60)
        results = json.loads(done.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise RuntimeError("node を動かせなかった")
    return {where: hit for (where, _, _, _), hit in zip(probes, results) if hit}


def agent_ran_problems(ctx: Context, task: Dict[str, Any], label: str) -> List[str]:
    """agent_ran の基準のエージェントの定義が、このプラグインにあるか（grade.mjs と tasks.mjs の loadTasks と同じ決まり）。"""
    out = []
    for i, item in enumerate(task.get("criteria") or []):
        check = item.get("check") if isinstance(item, dict) and item.get("grading") == "program" else None
        if isinstance(check, dict) and check.get("type") == "agent_ran":
            agent = check["agent"]
            if not os.path.isfile(os.path.join(ctx.root, "com.github.copilot", "agents", agent + ".agent.md")):
                out.append("%s.criteria[%d].check.agent「%s」のエージェントの定義（com.github.copilot/agents/%s.agent.md）がない"
                           % (label, i, agent, agent))
    return out


def check_eval(ctx: Context, c: Check) -> None:
    base = os.path.join(ctx.root, "tests", "eval")
    names = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))) if os.path.isdir(base) else []
    if not names:
        c.error("tests/eval/<課題>/eval.json がない（EVAL の課題が1つも入っていない）")
        return
    criteria = task_criteria(ctx.task)
    if ctx.task is None:
        c.error("作業の定義が読めないので、基準の写しと設定を比べられない")
    used: Set[str] = set()
    regexes: Regexes = []
    probes: RegexProbes = []
    probe_patterns: Dict[str, str] = {}
    ids: Dict[str, str] = {}
    for name in names:
        path = os.path.join(base, name, "eval.json")
        label = rel(ctx, path)
        if not os.path.isfile(path):
            c.error("%s がない" % label)
            continue
        data = read_json_file(path, label, c)
        if data is None:
            continue
        problems = eval_task_problems(data, label, regexes)
        for problem in problems:
            c.error(problem)
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("id"), str) and data["id"].strip():
            if data["id"] in ids:
                c.error("%s の id「%s」が、%s と重なっている" % (label, data["id"], ids[data["id"]]))
            ids[data["id"]] = label
        found_inputs: List[str] = []
        if isinstance(data.get("inputs"), list):
            found_inputs = input_problems(os.path.dirname(path), data["inputs"], label)
            for problem in found_inputs:
                c.error(problem)
        if not problems:
            for problem in agent_ran_problems(ctx, data, label):
                c.error(problem)
        if not problems and not found_inputs:   # 入力のままで当てはまってしまう採点や、入力に当てはまらない unchanged は、形が正しく入力がそろうときに確かめる
            before = len(probes)
            for problem in input_check_problems(data, input_files(os.path.dirname(path), data.get("inputs", [])), label, probes):
                c.error(problem)
            for where, pattern, _, _ in probes[before:]:
                probe_patterns[where] = pattern
        # settings.task を省いた課題も、空の値として比べる（必須で初めの値のない設定があれば、画面で開始できない）。
        # settings や settings.task が辞書でないときは、形の誤りとして eval_task_problems が知らせているので、ここでは比べない
        eval_settings = data.get("settings", {})
        given_task = eval_settings.get("task", {}) if isinstance(eval_settings, dict) else None
        if isinstance(ctx.task, dict) and isinstance(given_task, dict):
            for problem in settings_problems(ctx.task, given_task, label):
                c.error(problem)
        for item in data.get("criteria") if isinstance(data.get("criteria"), list) else []:
            if ctx.task is None or not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            original = criteria.get(item["id"])
            if original is None:
                c.error("%s: 基準 %s が作業の定義にない" % (label, item["id"]))
            elif item.get("text") != original.get("text"):
                c.error("%s: 基準 %s の文が作業の定義と違う（EVAL: %s／作業の定義: %s）"
                        % (label, item["id"], short(item.get("text"), 80), short(original.get("text"), 80)))
            else:
                used.add(item["id"])
    if regexes:
        try:
            for where, message in js_regex_errors(regexes).items():
                c.error("%s が正規表現として読めない（%s）" % (where, message))
        except RuntimeError as e:
            c.warn("regex の pattern が正規表現として読めるかは確かめていない（%s。Node.js があれば確かめる）" % e)
    if probes:
        try:
            for where, hit in js_regex_hits(probes).items():
                c.error("%s は、課題の入力のファイルがもとのままでも合格してしまう（「%s」に、パターン「%s」に合う箇所がある）。%s"
                        % (where, hit, short_text(probe_patterns[where]), INPUT_CHECK_HINT))
        except RuntimeError as e:
            c.warn("regex の基準が課題の入力のままで当てはまってしまわないかは確かめていない（%s。Node.js があれば確かめる）" % e)
    for cid, original in criteria.items():
        if ctx.task is not None and cid not in used:
            c.error("作業の定義の基準 %s（%s）が、どの EVAL の課題にも写されていない" % (cid, short(original.get("text"), 80)))


KNOWLEDGE_ID = re.compile(r"^K-[0-9]+$")
KNOWLEDGE_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])K-[0-9]+(?![0-9])")
SKIP_DIRS = {".git", "node_modules", "__pycache__", WORK_DIR}


def read_text_file(path: str) -> Optional[str]:
    """文字のファイルの中身（先頭の 5MB）。0 のバイトがあるファイル（フォントや画像など）、読めないファイル、シンボリックリンクは None。
    0 のバイトがあるファイルを見ないのは、中のバイトの並びが、たまたま K-数字 に見えることがあるため。"""
    if os.path.islink(path):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(5 * 1024 * 1024)
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="ignore")


def walk_files(base: str) -> List[str]:
    """base の下のファイル（SKIP_DIRS のフォルダの中は見ない）。"""
    found = []
    for directory, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        found += [os.path.join(directory, f) for f in sorted(files)]
    return found


def files_outside_tests(ctx: Context) -> List[str]:
    """作業の定義と、根の tests/ のフォルダを除くファイル。"""
    found = []
    for directory, dirs, files in os.walk(ctx.root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (directory == ctx.root and d == "tests"))
        found += [os.path.join(directory, f) for f in sorted(files) if os.path.join(directory, f) != ctx.task_path]
    return found


def knowledge_tokens_outside_tests(ctx: Context) -> Set[str]:
    """作業の定義とテストのフォルダを除く、文字のファイルに書かれたナレッジの番号。"""
    found: Set[str] = set()
    for path in files_outside_tests(ctx):
        text = read_text_file(path)
        if text is not None:
            found.update(KNOWLEDGE_TOKEN.findall(text))
    return found


REMOVED_FILE = "tests/knowledge-removed.json"


def knowledge_entry_problem(item: Any, where: str) -> Optional[str]:
    """ナレッジの1件（tests/knowledge.json と tests/knowledge-removed.json で同じ形）の誤り。なければ None。"""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not KNOWLEDGE_ID.match(item["id"]):
        return "%s: id は K-数字 の形にする（例：K-01）" % where
    if item.get("kind") not in ("criterion", "other"):
        return "%s（%s）: kind は criterion か other にする" % (where, item["id"])
    if not isinstance(item.get("text"), str) or not item["text"].strip():
        return "%s（%s）: text（ナレッジの文）がない" % (where, item["id"])
    return None


def read_removed_knowledge(ctx: Context, c: Check, current: Set[str]) -> Dict[str, Dict[str, Any]]:
    """外した決まりの一覧（tests/knowledge-removed.json）。番号 -> 項目。ないときは空（1件も外していない）。"""
    path = os.path.join(ctx.root, *REMOVED_FILE.split("/"))
    if not os.path.isfile(path):
        return {}
    data = read_json_file(path, REMOVED_FILE, c)
    if data is None:
        return {}
    if not isinstance(data, list):
        c.error("%s: 外した決まりの並び（リスト）になっていない" % REMOVED_FILE)
        return {}
    removed: Dict[str, Dict[str, Any]] = {}
    for i, item in enumerate(data):
        where = "%s の %d 番目" % (REMOVED_FILE, i + 1)
        problem = knowledge_entry_problem(item, where)
        if problem:
            c.error(problem)
            continue
        if item["id"] in current:
            c.error("%s: 番号 %s が tests/knowledge.json にもある（外した番号は、ほかの決まりに使わない）" % (where, item["id"]))
        elif item["id"] in removed:
            c.error("%s: 番号 %s が重なっている" % (where, item["id"]))
        removed[item["id"]] = item
    for item in removed.values():
        to = item.get("replacedBy")
        if "replacedBy" in item and not (isinstance(to, str) and KNOWLEDGE_ID.match(to) and to != item["id"] and (to in current or to in removed)):
            c.error("%s の %s: replacedBy %s が、tests/knowledge.json にも %s にもない番号になっている（中身を直した後の番号を書く）"
                    % (REMOVED_FILE, item["id"], short(to), REMOVED_FILE))
    return removed


def latest_number(removed: Dict[str, Dict[str, Any]], kid: str) -> Optional[str]:
    """外した番号 kid の、今の番号（replacedBy をたどった先）。中身を直さずに外したものは None。"""
    seen = {kid}
    to = removed[kid].get("replacedBy")
    while isinstance(to, str) and to in removed and to not in seen:
        seen.add(to)
        to = removed[to].get("replacedBy")
    return to if isinstance(to, str) and to not in removed else None


def removed_hint(removed: Dict[str, Dict[str, Any]], kid: str) -> str:
    now = latest_number(removed, kid)
    return "今の番号は %s。%s に直す" % (now, now) if now else "外した決まりなので、指している箇所を消す"


def files_for_removed_numbers(ctx: Context) -> List[str]:
    """外した番号を探すファイル：作業の定義と tests/ を除くファイル、EVAL の課題（tests/eval/*/eval.json）、作業ごとの試験（tests/stage2/task/）。
    課題の入力（tests/eval/*/inputs/）はユーザーのデータなので見ない。"""
    found = files_outside_tests(ctx)
    found += sorted(glob.glob(os.path.join(ctx.root, "tests", "eval", "*", "eval.json")))
    task_tests = os.path.join(ctx.root, "tests", "stage2", "task")
    if os.path.isdir(task_tests):
        found += walk_files(task_tests)
    return found


def check_knowledge(ctx: Context, c: Check) -> None:
    path = os.path.join(ctx.root, "tests", "knowledge.json")
    if not os.path.isfile(path):
        c.error("tests/knowledge.json がない（番号と種類つきのナレッジの一覧）")
        return
    data = read_json_file(path, "tests/knowledge.json", c)
    if data is None:
        return
    if not isinstance(data, list):
        c.error("tests/knowledge.json: ナレッジの並び（リスト）になっていない")
        return
    entries = []
    seen: Set[str] = set()
    for i, item in enumerate(data):
        where = "tests/knowledge.json の %d 番目" % (i + 1)
        problem = knowledge_entry_problem(item, where)
        if problem:
            c.error(problem)
            continue
        if item["id"] in seen:
            c.error("%s: 番号 %s が重なっている" % (where, item["id"]))
        seen.add(item["id"])
        entries.append(item)
    ctx.knowledge = entries
    removed = read_removed_knowledge(ctx, c, seen)
    if removed:   # 外した番号や、中身を直す前の番号を指したままの箇所（改善・設計変更の後に残りやすい）
        for path in files_for_removed_numbers(ctx):
            text = read_text_file(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for kid in sorted(set(KNOWLEDGE_TOKEN.findall(line)) & set(removed)):
                    c.error("%s:%d が、外した決まり %s を指している（%s）" % (rel(ctx, path), number, kid, removed_hint(removed, kid)))
    if ctx.task is None:
        c.error("作業の定義が読めないので、番号を突き合わせられない")
        return
    in_criteria = {i.get("knowledge") for i in task_criteria(ctx.task).values()}
    by_id = {e["id"]: e for e in entries}
    outside = knowledge_tokens_outside_tests(ctx)
    for e in entries:
        if e["kind"] == "criterion" and e["id"] not in in_criteria:
            c.error("%s（合否の決め方）が作業の定義の合否の基準（criteria の knowledge）にない: 「%s」" % (e["id"], e["text"]))
        elif e["kind"] == "other" and e["id"] not in outside:
            c.error("%s がエージェントの指示などのファイル（作業の定義とテストのフォルダを除く）にない: 「%s」" % (e["id"], e["text"]))
    for cid, criterion in task_criteria(ctx.task).items():
        k = criterion.get("knowledge")
        if isinstance(k, str) and k in removed:
            c.error("基準 %s の knowledge %s は、外した決まりの番号（%s）" % (cid, k, removed_hint(removed, k)))
        elif k is not None and (k not in by_id or by_id[k]["kind"] != "criterion"):
            c.error("基準 %s の knowledge %s が、ナレッジの一覧にある「合否の決め方」の番号でない" % (cid, k))


# ---- 部品の独立性（REQ-062）。入口のほかのスキル（ルールのスキル）は、そのスキルのフォルダだけで使える。
# 入口のスキル（本文に <名前>_open がある）は、統括エージェントを始めることが仕事なので見ない。

RULE_TEXT_MIN = 15   # これより短い文は、ほかの文にたまたま含まれやすいので、写しを探さない（決まりの文の1文ごとの字数）
SKILL_FOLDERS = ("scripts", "references", "assets")
# コードの中の scripts/・references/・assets/ のパス（例：`python3 <このスキルのフォルダ>/scripts/build.py`）。
# 置き場所の書き方（<名前> や *）が続くものは、決まったファイルを指していないので見ない。どのフォルダの下かは path_base で決める
SKILL_PATH = re.compile(r"(?<![A-Za-z0-9_.-])((?:%s)/[^\s`\"'()<>\[\]{}|;,、。（）「」*?]+)" % "|".join(SKILL_FOLDERS))
MD_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)")
URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
OTHER_SKILL = re.compile(r"(?<![A-Za-z0-9_.-])skills/([A-Za-z0-9][A-Za-z0-9_.-]*)/")
# エージェントの定義の中の、プラグインのファイルのパス（skills/<名前>/… と scripts/…）
AGENT_PLUGIN_PATH = re.compile(r"(?<![A-Za-z0-9_.-])((?:skills/([A-Za-z0-9][A-Za-z0-9_.-]*)/|scripts/)[A-Za-z0-9_.~/+@%-]*)")
DATA_FILES = re.compile(r"(?<![A-Za-z0-9_-])(?:task|knowledge)\.json")
PLACEHOLDER_DIR = re.compile(r"<([^<>\n]*)>/$")       # 前にある置き場所の書き方（例：<このスキルのフォルダ>/）
PATH_TAIL = re.compile(r"[A-Za-z0-9_.~$/\\{}-]*$")   # パスの前に続く、フォルダの部分
SKILL_WORD = re.compile(r"スキル|skill", re.I)
PLUGIN_WORD = re.compile(r"プラグイン|plugin", re.I)
KNOWLEDGE_MARK = re.compile(r"[（(]K-[0-9]+(?:[、,・]K-[0-9]+)*[）)]")   # 文に添えた番号（例：（K-03））。空白を除いた後の形
SENTENCE_END = re.compile(r"[。．！？!?]|\.(?=\s|$)")


def path_base(text: str, at: int) -> Optional[str]:
    """text の at から始まるパスが、どのフォルダからのパスか。前に何もない（か ./）なら ""（相対パス）、
    前に置き場所の書き方 <…>/ があればその中の言葉、そのほかのフォルダの下（例：src/、../、${PLUGIN_ROOT}/）なら None。"""
    line = text[text.rfind("\n", 0, at) + 1:at]
    holder = PLACEHOLDER_DIR.search(line)
    if holder:
        return holder.group(1)
    tail = PATH_TAIL.search(line).group(0)
    return "" if tail in ("", "./") else None


def plugin_relative(text: str, at: int) -> bool:
    """at から始まるパスが、プラグインのフォルダの中を指すか（相対パスか、<プラグインのフォルダ>/ のように書いたもの）。"""
    base = path_base(text, at)
    return base == "" or (base is not None and bool(PLUGIN_WORD.search(base)))


def split_code(text: str) -> Tuple[str, str]:
    """Markdown を、(コードの外の文, コードの中の文) に分ける。コードは ``` か ~~~ の囲みと、`…` の中。"""
    prose, code, fence = [], [], None
    for line in text.splitlines():
        mark = re.match(r"\s*(`{3,}|~{3,})", line)
        if fence is None and mark:
            fence = mark.group(1)[0] * 3
        elif fence is not None and line.strip().startswith(fence):
            fence = None
        elif fence is not None:
            code.append(line)
        else:
            prose.append(line)
    outside = "\n".join(prose)
    code += re.findall(r"`([^`\n]+)`", outside)
    return re.sub(r"`[^`\n]+`", " ", outside), "\n".join(code)


def skill_markdown(skill_dir: str) -> List[str]:
    """スキルの本文と参考の文書（SKILL.md と references/ の .md）。"""
    files = [os.path.join(skill_dir, "SKILL.md")]
    refs = os.path.join(skill_dir, "references")
    if os.path.isdir(refs):
        files += [p for p in walk_files(refs) if p.lower().endswith(".md")]
    return files


def inside(path: str, base: str) -> bool:
    return is_within(os.path.realpath(path), os.path.realpath(base))


def link_problems(ctx: Context, skill_dir: str, path: str, prose: str, code: str) -> List[str]:
    """本文が指すファイル（Markdown のリンクと、コードの中の scripts/・references/・assets/ のパス）が、スキルのフォルダの中にあるか。"""
    out = []
    label = rel(ctx, path)
    folder = rel(ctx, skill_dir)
    for target in MD_LINK.findall(prose):
        if URL_SCHEME.match(target) or target.startswith("#"):
            continue
        target = target.split("#", 1)[0]
        candidates = [] if target.startswith("/") else [os.path.normpath(os.path.join(os.path.dirname(path), target)),
                                                         os.path.normpath(os.path.join(skill_dir, target))]
        if not any(inside(p, skill_dir) and os.path.exists(p) for p in candidates):
            out.append("%s: リンク %s が指すファイルが、スキルのフォルダ（%s/）の中にない（スキルが使うファイルはスキルのフォルダに置き、"
                       "そのフォルダからの相対パスで指す）" % (label, target, folder))
    targets = set()
    for m in SKILL_PATH.finditer(code):
        # スキルのフォルダからの相対パスか、<このスキルのフォルダ>/ のように書いたものだけを見る。
        # ほかのフォルダの下のもの（例：<ソースのフォルダ>/scripts/、src/assets/、URL の中）は、スキルのファイルを指していない
        base = path_base(code, m.start())
        # 「scripts/build-*.sh」のような、いくつものファイルを表す書き方は、1つのファイルを指していない
        if code[m.end():m.end() + 1] in ("*", "?"):
            continue
        if base == "" or (base is not None and SKILL_WORD.search(base)):
            targets.add(m.group(1).rstrip(".:"))
    for target in sorted(targets):
        full = os.path.normpath(os.path.join(skill_dir, target))
        if not (inside(full, skill_dir) and os.path.exists(full)):
            out.append("%s: %s が、スキルのフォルダ（%s/）の中にない（スキルが使うスクリプトや参考の文書は、プラグインの根でなく、"
                       "スキルのフォルダの scripts/・references/・assets/ に置く）" % (label, target, folder))
    return out


def forbidden_problems(ctx: Context, folder: str, path: str, text: str) -> List[str]:
    """ルールのスキルが、ほかの部品やスキルのフォルダの外に頼っていないか。単体で使うと、どれも意味をなさないため。
    ほかのスキルのフォルダは、プラグインの中を指すもの（skills/<名前>/ か <プラグインのフォルダ>/skills/<名前>/）だけを数える。"""
    label = rel(ctx, path)
    found = []
    if "../" in text:
        found.append("../（スキルのフォルダの外）")
    for word in ("${PLUGIN_ROOT}", "${CLAUDE_SKILL_DIR}"):
        if word in text:
            found.append("%s（スキルの本文では展開されない）" % word)
    others = sorted({m.group(1) for m in OTHER_SKILL.finditer(text) if m.group(1) != folder and plugin_relative(text, m.start())})
    found += ["skills/%s/（ほかのスキルのフォルダ）" % m for m in others]
    if "com.github.copilot/" in text:
        found.append("com.github.copilot/（エージェントやエクステンションの場所）")
    if ctx.plugin_name:
        name = re.escape(ctx.plugin_name)
        tools = sorted(set(m.group(0) for m in re.finditer(r"(?<![A-Za-z0-9_.-])%s_(?:%s)(?![A-Za-z0-9_])" % (name, RECORD_SUFFIXES), text)))
        found += ["%s（記録のツール）" % t for t in tools]
        agents = sorted(set(re.findall(r"(?<![A-Za-z0-9_.-])%s:[a-z0-9][a-z0-9-]*" % name, text)))
        found += ["%s（エージェント）" % a for a in agents]
    files = sorted(set(DATA_FILES.findall(text)))
    found += ["%s（作業の定義かナレッジの一覧）" % f for f in files]
    if not found:
        return []
    return ["%s: %s を書いている（入口のほかのスキルは、そのスキルのフォルダの中のファイルだけで仕事を終えられるようにする。"
            "プラグインの流れの中での使い方は、エージェントの定義に書く）" % (label, "、".join(found))]


def compact(text: str) -> str:
    """決まりの文の写しを探すための形。Unicode の正規化をそろえ、空白と改行と、文に添えた番号（（K-03）など）を除く
    （折り返しや、番号の添え方の違いを見ないため）。"""
    return KNOWLEDGE_MARK.sub("", re.sub(r"\s+", "", unicodedata.normalize("NFC", text)))


def rule_sentences(text: str) -> List[str]:
    """決まりの文を1文ずつに分けた、写しを探す形（RULE_TEXT_MIN 字以上の文だけ）。
    文の終わりの句点は除く。写すときに、番号を句点の前に添える（「…消す（K-03）。」）ためである。"""
    pieces = SENTENCE_END.split(unicodedata.normalize("NFC", text))
    return [p for p in (compact(piece) for piece in pieces) if len(p) >= RULE_TEXT_MIN]


def check_standalone(ctx: Context, c: Check) -> None:
    if not ctx.plugin_name:
        c.error("plugin.json の name が読めないので、入口のスキル（<名前>_open を呼ぶスキル）を見分けられない")
        return
    entry_word = ctx.plugin_name + "_open"
    rules: Dict[str, str] = {}      # ルールのスキルの名前 -> フォルダ
    corpus: Dict[str, str] = {}     # 場所（skills/<名前>/、エージェントの定義）-> 中の文字（compact）
    for folder, skill_path in skill_folders(ctx):
        skill_dir = os.path.dirname(skill_path)
        texts = [t for t in (read_text_file(p) for p in walk_files(skill_dir)) if t is not None]
        corpus["skills/%s/" % folder] = compact("\n".join(texts))
        body = read_text_file(skill_path) or ""
        if entry_word not in body:
            rules[folder] = skill_dir
    for folder, skill_dir in sorted(rules.items()):
        for path in skill_markdown(skill_dir):
            text = read_text_file(path)
            if text is None:
                continue
            prose, code = split_code(text)
            for problem in link_problems(ctx, skill_dir, path, prose, code) + forbidden_problems(ctx, folder, path, text):
                c.error(problem)
    agents_dir = os.path.join(ctx.root, "com.github.copilot", "agents")
    for file in sorted(os.listdir(agents_dir)) if os.path.isdir(agents_dir) else []:
        path = os.path.join(agents_dir, file)
        text = read_text_file(path) if file.endswith(".agent.md") else None
        if text is None:
            continue
        label = rel(ctx, path)
        corpus[label] = compact(text)
        if "${PLUGIN_ROOT}" in text:
            c.error("%s: ${PLUGIN_ROOT} を書いている（エージェントの本文では展開されない。<プラグインのフォルダ>/… と書き、"
                    "プラグインのフォルダは統括エージェントからの頼みで受け取る）" % label)
        relative, named = [], set()
        for m in AGENT_PLUGIN_PATH.finditer(text):
            found = m.group(1).rstrip(".")
            if path_base(text, m.start()) == "" and found not in relative:
                relative.append(found)
            if m.group(2) and plugin_relative(text, m.start()):
                named.add(m.group(2))
        for found in relative:
            c.error("%s: %s を相対パスで書いている（エージェントは利用者の作業フォルダで動くので、相対パスではプラグインのファイルに届かない。"
                    "<プラグインのフォルダ>/%s と書く。利用者のファイルなら、<作業フォルダ>/… のように、どのフォルダの中かを書く）" % (label, found, found))
        for name in sorted(named):
            if not os.path.isfile(os.path.join(ctx.root, "skills", name, "SKILL.md")):
                c.error("%s: skills/%s/ を指しているが、そのスキル（skills/%s/SKILL.md）がない" % (label, name, name))
    rule_places = {"skills/%s/" % f for f in rules}
    for entry in ctx.knowledge:
        copied: Set[str] = set()
        for sentence in rule_sentences(entry["text"]):   # 1文ずつ比べる。1文だけ写しても、決まりが2か所になるため
            places = {place for place, text in corpus.items() if sentence in text}
            if len(places) > 1 and places & rule_places:
                copied |= places
        if copied:
            c.error("%s の決まりの文「%s」が、%s に書かれている（決まりの文は、受け持つスキルの1か所にだけ書く。"
                    "エージェントの定義やほかのスキルには、スキルの場所と番号だけを書く）"
                    % (entry["id"], short_text(entry["text"]), "、".join(p for p in corpus if p in copied)))


ROLE_FILES = (("orchestrator", "統括"), ("worker", "作業"), ("verifier", "検証"))
WRITE_TOOLS = {"edit", "write", "multiedit", "notebookedit", "create"}
EXEC_TOOLS = {"execute", "shell", "bash", "powershell"}


def agents_of(ctx: Context, role: str) -> List[str]:
    if role == "worker":
        return sorted(n for n in ctx.agents if n == "worker" or n.startswith("worker-"))
    return [role] if role in ctx.agents else []


def check_agent_tools(ctx: Context, c: Check) -> None:
    for role, label in ROLE_FILES:
        names = agents_of(ctx, role)
        if not names:
            c.error("%sエージェントの com.github.copilot/agents/%s.agent.md がない" % (label, role))
        for name in names:
            where = "com.github.copilot/agents/%s.agent.md（%s）:" % (name, label)
            meta = ctx.agents[name][0]
            tools = agent_tools(meta)
            if role == "orchestrator":
                # 統括は全ツールを持ち、使ってよいツールはエクステンションがフックで決める。tools を書くと、Copilot CLI は
                # 子（作業・検証）のツールも統括の範囲に絞る（更新履歴 1.0.67・1.0.68。V1 の不具合 1）
                if "tools" in meta:
                    c.error("%s tools がある（統括の定義には tools を書かない。書くと、子の作業・検証が受け取れるツールも、統括の範囲に絞られる。"
                            "統括が使えるツールは、エクステンションが決める）" % where)
                continue
            if tools is None:
                c.error("%s tools が書かれていない（書かないと、すべてのツールとスキルが使える）" % where)
                continue
            if role == "verifier":
                for tool in tools:
                    if tool.lower() in WRITE_TOOLS:
                        c.error("%s tools に書き込みのツール %s がある（検証エージェントは成果物を直せない）" % (where, tool))
                    if tool.lower() in EXEC_TOOLS:
                        c.error("%s tools にコマンドのツール %s がある（検証エージェントはコマンドを動かせない）" % (where, tool))


RECORD_SUFFIXES = "open|plan|verdict|show|review|issue|finish|ask|answer"
RECORD_TOOL = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*?)_(%s)$" % RECORD_SUFFIXES)
BACKTICKED = re.compile(r"`([^`\s]+)`")


def check_tool_names(ctx: Context, c: Check) -> None:
    if not ctx.plugin_name:
        c.error("plugin.json の name が読めないので、ツール名の頭を比べられない")
        return
    for name in sorted(ctx.agents):
        meta, body = ctx.agents[name]
        where = "com.github.copilot/agents/%s.agent.md" % name
        found = [("tools", t) for t in agent_tools(meta) or []] + [("本文", t) for t in BACKTICKED.findall(body)]
        for place, token in found:
            m = RECORD_TOOL.match(token)
            if m and m.group(1) != ctx.plugin_name:
                c.error("%s の%s: 記録のツール %s の頭がプラグインの名前 %s ではない" % (where, place, token, ctx.plugin_name))


REQ_IN_NAME = re.compile(r"(?<![A-Za-z0-9])req[-_]?(\d+)(?!\d)", re.I)
NODE_TEST_CALL = re.compile(r"""\b(?:test|it|describe|suite)(?:\.\w+)*\s*\(\s*(['"`])((?:\\.|(?!\1).)*?)\1""", re.S)


MARKETPLACE_FILE = ".github/plugin/marketplace.json"


def check_marketplace(ctx: Context, c: Check) -> None:
    """README のインストールの手順（marketplace add → install <プラグイン>@<マーケットプレイス>）が読むファイル。
    一覧のプラグインの名前が plugin.json と違うと、install が失敗する（設計書 10章）。"""
    path = os.path.join(ctx.root, *MARKETPLACE_FILE.split("/"))
    if not os.path.isfile(path):
        c.error("%s がない（README のインストールの手順が、このファイルを読む）" % MARKETPLACE_FILE)
        return
    data = read_json_file(path, MARKETPLACE_FILE, c)
    if data is None:
        return
    if not isinstance(data, dict):
        c.error("%s: JSON のオブジェクトにする" % MARKETPLACE_FILE)
        return
    if not is_text(data.get("name")):
        c.error("%s: マーケットプレイスの名前（name）がない" % MARKETPLACE_FILE)
    owner = data.get("owner")
    if not isinstance(owner, dict) or not is_text(owner.get("name")):
        c.error("%s: 持ち主の名前（owner.name）がない" % MARKETPLACE_FILE)
    plugins = data.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        c.error("%s: プラグインの一覧（plugins）がない" % MARKETPLACE_FILE)
        return
    names = [p.get("name") for p in plugins if isinstance(p, dict)]
    if len(names) != len(plugins) or not all(is_text(n) for n in names):
        c.error("%s: plugins のどれかに、プラグインの名前（name）がない" % MARKETPLACE_FILE)
    if ctx.plugin_name and ctx.plugin_name not in names:
        c.error("%s: plugins に plugin.json の名前「%s」がない（今は %s）。名前が違うと、copilot plugin install が失敗する"
                % (MARKETPLACE_FILE, ctx.plugin_name, "、".join("「%s」" % n for n in names if is_text(n)) or "なし"))


def req_number(digits: str) -> str:
    return "REQ-%03d" % int(digits)


def requirement_numbers_in(text: str) -> List[str]:
    return [req_number(d) for d in re.findall(r"REQ-(\d+)", text)]


def collect_test_requirements(ctx: Context) -> Set[str]:
    """テストの名前（node:test の test/it/describe の第1引数、Python の test 関数の名前と docstring）に入っている要件の番号。"""
    found: Set[str] = set()
    base = os.path.join(ctx.root, "tests")
    for directory, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for file in files:
            path = os.path.join(directory, file)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            if re.search(r"\.test\.(mjs|cjs|js)$", file):
                for m in NODE_TEST_CALL.finditer(text):
                    found.update(req_number(d) for d in REQ_IN_NAME.findall(m.group(2)))
            elif re.match(r"test_.*\.py$", file):
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                        names = node.name + "\n" + (ast.get_docstring(node) or "")
                        found.update(req_number(d) for d in REQ_IN_NAME.findall(names))
    return found


def markdown_tables(text: str) -> List[List[List[str]]]:
    """Markdown の表を、行のリスト（各行は、セルの文字のリスト）のリストにする。コードのかこみの中は読まない。"""
    tables: List[List[List[str]]] = []
    current: List[List[str]] = []
    fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
        if not fenced and stripped.startswith("|"):
            body = stripped[1:]
            if body.endswith("|") and not body.endswith("\\|"):
                body = body[:-1]
            current.append([cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", body)])
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


TRACE_COLUMNS = (("req", "要件"), ("section", "設計書"), ("files", "ファイル"), ("method", "確かめ方"), ("test", "テスト"))
EMPTY_CELL = {"", "-", "–", "—", "なし"}


def find_trace_table(text: str) -> Optional[Tuple[Dict[str, int], List[List[str]]]]:
    for table in markdown_tables(text):
        header = table[0]
        index = {}
        for key, word in TRACE_COLUMNS:
            for i, cell in enumerate(header):
                if cell.startswith(word):
                    index[key] = i
                    break
        if len(index) == len(TRACE_COLUMNS):
            return index, [r for r in table[2:] if len(r) > max(index.values())]
    return None


def cell_paths(cell: str) -> List[str]:
    ticked = re.findall(r"`([^`]+)`", cell)
    tokens = ticked or [t for t in re.split(r"(?:<br\s*/?>|[、,，;；\s])+", cell) if t]
    paths = []
    for token in tokens:
        token = re.sub(r":\d+(-\d+)?$", "", token.split("#", 1)[0].strip())
        if token not in EMPTY_CELL:
            paths.append(token[2:] if token.startswith("./") else token)
    return paths


def check_trace(ctx: Context, c: Check) -> None:
    spec_path = os.path.join(ctx.root, "docs", "specs", "requirements.md")
    design_path = os.path.join(ctx.root, "docs", "design", "design.md")
    for path in (spec_path, design_path):
        if not os.path.isfile(path):
            c.error("%s がない" % rel(ctx, path))
    if not (os.path.isfile(spec_path) and os.path.isfile(design_path)):
        return
    with open(spec_path, encoding="utf-8") as f:
        spec = f.read()
    with open(design_path, encoding="utf-8") as f:
        design = f.read()
    requirements = {}
    for m in re.finditer(r"^#{1,6}[ \t]+(REQ-(\d+))[ \t]*(.*)$", spec, re.M):
        requirements.setdefault(req_number(m.group(2)), m.group(3).strip())
    if not requirements:
        c.error("docs/specs/requirements.md に要件（見出し「### REQ-001 題」）が1つもない")
    found = find_trace_table(design)
    if found is None:
        c.error("docs/design/design.md に追跡の表がない（列：要件の番号｜設計書の節｜ファイル｜確かめ方｜テスト）")
        return
    index, rows = found
    tests = collect_test_requirements(ctx) | stage1_requirement_numbers()
    rows_of: Dict[str, List[List[str]]] = {}
    for row in rows:
        for number in requirement_numbers_in(row[index["req"]]):
            rows_of.setdefault(number, []).append(row)
    for number, title in requirements.items():
        if number not in rows_of:
            c.error("%s（%s）が追跡の表にない" % (number, title))
            continue
        for row in rows_of[number]:
            check_trace_row(ctx, c, number, row, index, tests)


def check_trace_row(ctx: Context, c: Check, number: str, row: List[str], index: Dict[str, int], tests: Set[str]) -> None:
    where = "追跡の表の %s:" % number
    if row[index["section"]].strip() in EMPTY_CELL:
        c.error("%s 設計書の節が書かれていない" % where)
    paths = cell_paths(row[index["files"]])
    if not paths:
        c.error("%s ファイルが書かれていない" % where)
    for path in paths:
        full = os.path.normpath(os.path.join(ctx.root, path))
        if not is_within(full, ctx.root) or not (glob.glob(full) if re.search(r"[*?\[]", path) else os.path.exists(full)):
            c.error("%s 書かれたファイル %s がない" % (where, path))
    method = row[index["method"]]
    automatic, evaluated, human = "自動のテスト" in method, "eval" in method.lower(), "人が確かめる" in method
    if not (automatic or evaluated or human):
        c.error("%s 確かめ方が「自動のテスト」「EVAL」「人が確かめる」のどれでもない: %s" % (where, short(method)))
    if automatic and number not in tests:
        c.error("%s 確かめ方が自動のテストだが、名前に %s を持つテストがない（node:test の test('%s …')、Python の test 関数の名前か docstring に書く）"
                % (where, number, number))


# ---------------------------------------------------------------- 入口

def run_checks(root: str, kind: str = "generated") -> Report:
    """公式仕様の検査（plugin）に、生成プラグインの検査（generated）を足して動かす。"""
    ctx = Context(root)
    report = Report(ctx.root, kind)
    check_manifest(ctx, report.new("manifest"))
    check_mcp(ctx, report.new("mcp"))
    check_skills(ctx, report.new("skills"), kind)
    check_references(ctx, report.new("references"))
    check_agents(ctx, report.new("agents"))
    check_layout(ctx, report.new("layout"))
    check_symlinks(ctx, report.new("symlinks"))
    if kind == "generated":
        check_task(ctx, report.new("task"))
        check_eval(ctx, report.new("eval"))
        check_knowledge(ctx, report.new("knowledge"))
        check_standalone(ctx, report.new("standalone"))
        check_agent_tools(ctx, report.new("agent-tools"))
        check_tool_names(ctx, report.new("tool-names"))
        check_marketplace(ctx, report.new("marketplace"))
        check_trace(ctx, report.new("trace"))
    return report


MARK = {"passed": "合格", "failed": "不合格", "skipped": "対象外"}


def format_report(report: Report) -> str:
    lines = ["構造の検査: %s" % report.root]
    for c in report.checks:
        lines.append("  [%s] %s%s" % (MARK[c.status], c.title, "（%s）" % c.skipped if c.skipped else ""))
        lines.extend("      - %s" % m for m in c.errors)
        lines.extend("      注意: %s" % m for m in c.warnings)
    count = sum(len(c.errors) for c in report.checks)
    lines.append("結果: 合格" if report.ok else "結果: 不合格（%d 件）" % count)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="1段目 構造の検査")
    parser.add_argument("plugin_dir", help="検査するプラグインのフォルダ")
    parser.add_argument("--kind", choices=["generated", "plugin"], default="generated")
    parser.add_argument("--json", action="store_true", help="結果を JSON で出す")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.plugin_dir):
        sys.stderr.write("フォルダがない: %s\n" % args.plugin_dir)
        return 2
    report = run_checks(args.plugin_dir, args.kind)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
