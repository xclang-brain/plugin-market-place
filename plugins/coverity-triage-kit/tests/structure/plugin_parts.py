"""プラグイン全体の試験が使う道具（標準ライブラリだけ。Python 3.10 以上）。

名前は、次のところから読みます。
- スキル：skills/ の下の、SKILL.md があるフォルダ（フォルダの名前がスキルの名前）
- MCP サーバー：mcp-servers/ の下のフォルダと、mcp.json の mcpServers の鍵（登録名）。
  ツールの名前は、MCP サーバーのソースの @….tool の付いた関数から読む
- エージェント：com.github.copilot/agents/<名前>.agent.md（Copilot CLI）と、agents/<名前>.md（Claude Code。同じ名前の組）
- テストが名前で指定するコンポーネント（統括エージェント、役割ごとのエージェント、一括実行・画面・反映のスクリプト、
  役割ごとのスキルと MCP サーバー）：このファイルの定数（ORCHESTRATOR、PARTS など）。ここに書いたものがなければ、
  テストは不合格になります
- エージェントが使うコンポーネント：エージェントの「## 使う部品」の表
- 設定の項目：設計書 4.1 の config.yaml の表（取り決め）

structure・independence・contracts・docs の試験が、このファイルを読み込む。
"""

from __future__ import annotations

import ast
import functools
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
PLUGIN_NAME = ROOT.name
SKILLS_DIR = ROOT / "skills"
MCP_DIR = ROOT / "mcp-servers"
COPILOT_DIR = ROOT / "com.github.copilot"
AGENTS_DIR = COPILOT_DIR / "agents"
DOCS_DIR = ROOT / "docs"
SPEC = DOCS_DIR / "specs" / "requirements.md"
DESIGN = DOCS_DIR / "design" / "design.md"
SCHEMAS_DIR = TESTS / "structure" / "schemas" / "agent-plugins-1.0.0"
# Claude Code の形（同じフォルダに置く。Copilot CLI は、plugin.json の $schema が Agent Plugins 1.0.0 なら読まない）
CLAUDE_MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
CLAUDE_MCP = ROOT / ".mcp.json"
CLAUDE_AGENTS_DIR = ROOT / "agents"
CLAUDE_AGENT_SUFFIX = ".md"
REPO_ROOT = ROOT.parents[1]

AGENT_SUFFIX = ".agent.md"
PARTS_HEADING = "使う部品"
PARTS_HEADER = ["役割", "部品", "使うとき"]
# テストが名前で指定するコンポーネント
ORCHESTRATOR = "triage-orchestrator"
ROLES = {"worker": "triage-worker", "reviewer": "triage-reviewer", "integrator": "triage-integrator"}
RUN_SCRIPT = SKILLS_DIR / "coverity-run" / "scripts" / "run.py"
GUI_SCRIPT = SKILLS_DIR / "coverity-gui" / "scripts" / "gui.py"
APPLY_SCRIPT = SKILLS_DIR / "coverity-apply" / "scripts" / "apply.py"
# 役割 → フォルダ（取り決めのテストが役割で指定するスキルと MCP サーバー）
PARTS = {
    "真偽の調査": "skills/triage-investigation",
    "逸脱コメント": "skills/deviation-comment",
    "結果の記録": "skills/triage-report",
    "結果の確かめ": "skills/triage-review",
    "同じ原因の統合": "skills/triage-integration",
    "準備": "skills/coverity-setup",
    "一括実行": "skills/coverity-run",
    "反映": "skills/coverity-apply",
    "結果と設定の画面": "skills/coverity-gui",
    "Coverity との通信": "mcp-servers/coverity-connect",
}

TEXT_SUFFIXES = {
    ".md", ".py", ".yaml", ".yml", ".json", ".jsonl", ".toml", ".mjs", ".js", ".cjs",
    ".html", ".css", ".txt", ".sh", ".ps1", ".xml", ".ini", ".cfg", ".patch", ".c", ".h", ".cpp", ".hpp",
}
# 作られたもの・他者のもの（言葉の確かめから外す）
SKIP_DIR_NAMES = {"__pycache__", ".venv", "node_modules", ".pytest_cache", ".ruff_cache", ".mypy_cache", "vendor"}
SKIP_FILE_NAMES = {"uv.lock", "package-lock.json"}


# ---------------------------------------------------------------------------
# ファイル
# ---------------------------------------------------------------------------

def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def skipped(path: Path, base: Path) -> bool:
    parts = path.relative_to(base).parts
    if any(p in SKIP_DIR_NAMES for p in parts[:-1]):
        return True
    # evals/results/ などは、試験の道具が作る記録
    return len(parts) >= 2 and parts[0] == "evals" and parts[1] == "results"


def text_files(folder: Path):
    """フォルダの中の文のファイル（作られたもの・同梱した他者のものを除く）"""
    if not folder.exists():
        return []
    if folder.is_file():
        return [folder]
    files = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or skipped(path, folder) or path.name in SKIP_FILE_NAMES:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


# ---------------------------------------------------------------------------
# 部品を読む
# ---------------------------------------------------------------------------

def skill_dirs():
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def skill_names():
    return [p.name for p in skill_dirs() if (p / "SKILL.md").is_file()]


def mcp_dirs():
    if not MCP_DIR.is_dir():
        return []
    return sorted(p for p in MCP_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def load_json(path: Path):
    try:
        return json.loads(read(path))
    except (OSError, ValueError):
        return None


def mcp_config():
    data = load_json(ROOT / "mcp.json")
    return data if isinstance(data, dict) else {}


def expand_plugin_root(value: str) -> str:
    return value.replace("${PLUGIN_ROOT}", str(ROOT))


def mcp_registrations():
    """mcp.json の登録名 → そのサーバーのフォルダ（--directory か cwd。分からなければ None）"""
    out = {}
    for name, server in (mcp_config().get("mcpServers") or {}).items():
        folder = None
        if isinstance(server, dict):
            args = [a for a in server.get("args") or [] if isinstance(a, str)]
            for i, arg in enumerate(args):
                if arg == "--directory" and i + 1 < len(args):
                    folder = Path(expand_plugin_root(args[i + 1]))
                elif arg.startswith("--directory="):
                    folder = Path(expand_plugin_root(arg.split("=", 1)[1]))
            if folder is None and isinstance(server.get("cwd"), str):
                folder = Path(expand_plugin_root(server["cwd"]))
        out[name] = folder
    return out


def _decorated_tools(tree):
    """ツールとして登録する関数の名前。@<何か>.tool / @tool の付いた関数（name= があればそれ）と、
    自前の登録の飾り（@register("名前", …)）で、関数と同じ名前の文字列を渡しているもの"""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            attr = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
            given = None
            if isinstance(deco, ast.Call):
                for kw in deco.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        given = kw.value.value
                if deco.args and isinstance(deco.args[0], ast.Constant) and deco.args[0].value == node.name:
                    given = node.name
            if attr == "tool":
                names.add(given or node.name)
            elif given == node.name:
                names.add(given)
    return names


@functools.lru_cache(maxsize=None)
def all_mcp_tool_names():
    """MCP サーバーのツールの名前（mcp-servers/ の下のソースの、ツールとして登録した関数）"""
    names = set()
    for folder in mcp_dirs():
        for path in sorted(folder.rglob("*.py")):
            if skipped(path, folder) or "tests" in path.relative_to(folder).parts:
                continue
            try:
                names |= _decorated_tools(ast.parse(read(path)))
            except SyntaxError:
                continue
    return names


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _flags_destructive(node, assigned, seen=()) -> bool:
    """node（とその中）に destructiveHint=True があるか。形は3つ：
    - 名前付きの引数：ToolAnnotations(destructiveHint=True)
    - 辞書：annotations={"destructiveHint": True}
    - 変数：ANN = ToolAnnotations(...) / ANN = {...} を、あとで annotations=ANN と渡す（assigned で辿る）"""
    for n in ast.walk(node):
        if isinstance(n, ast.keyword) and n.arg == "destructiveHint" and _is_true(n.value):
            return True
        if isinstance(n, ast.Dict):
            for key, value in zip(n.keys, n.values):
                if isinstance(key, ast.Constant) and key.value == "destructiveHint" and _is_true(value):
                    return True
        if isinstance(n, ast.Name) and n.id in assigned and n.id not in seen:
            if any(_flags_destructive(v, assigned, seen + (n.id,)) for v in assigned[n.id]):
                return True
    return False


def _assignments(tree):
    """変数の名前 → 代入した値（ToolAnnotations を変数に作ってから渡す書き方のため）"""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            out.setdefault(node.target.id, []).append(node.value)
    return out


def _destructive_tools(tree, assigned=None):
    """destructiveHint=True を付けて登録したツールの名前（Coverity に書き込むツール）。
    assigned は変数の名前 → 値（ほかのファイルで作った ToolAnnotations も辿れるように、渡せる）"""
    assigned = _assignments(tree) if assigned is None else assigned
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            if _flags_destructive(deco, assigned):
                names |= _decorated_tools(ast.Module(body=[node], type_ignores=[]))
    return names


@functools.lru_cache(maxsize=None)
def write_back_tools():
    """登録名 → 書き戻しのツール（destructiveHint=True のツール）の名前の集まり"""
    out = {}
    for name, folder in mcp_registrations().items():
        trees = []
        if folder and folder.is_dir():
            for path in sorted(folder.rglob("*.py")):
                if skipped(path, folder) or "tests" in path.relative_to(folder).parts:
                    continue
                try:
                    trees.append(ast.parse(read(path)))
                except SyntaxError:
                    continue
        assigned = {}
        for tree in trees:
            for key, values in _assignments(tree).items():
                assigned.setdefault(key, []).extend(values)
        tools = set()
        for tree in trees:
            tools |= _destructive_tools(tree, assigned)
        out[name] = tools
    return out


def claude_mcp_tool_names(server: str, tool: str):
    """Claude Code での MCP のツールの名前：プラグインのサーバー（plugin_<プラグイン>_<登録名>）と、手で登録したサーバー"""
    return [f"mcp__plugin_{PLUGIN_NAME}_{server}__{tool}", f"mcp__{server}__{tool}"]


def parse_claude_mcp_tool(name: str):
    """Claude Code の MCP のツールの名前を (登録名, ツール, 形) に。形は "plugin" か "hand"。MCP のツールでなければ None"""
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    m = re.fullmatch(r"mcp__plugin_" + re.escape(PLUGIN_NAME) + r"_(.+?)__(.+)", name)
    if m:
        return m.group(1), m.group(2), "plugin"
    m = re.fullmatch(r"mcp__(?!plugin_)(.+?)__(.+)", name)
    if m:
        return m.group(1), m.group(2), "hand"
    return None


def agent_files():
    if not AGENTS_DIR.is_dir():
        return []
    return sorted(p for p in AGENTS_DIR.iterdir() if p.is_file() and p.name.endswith(AGENT_SUFFIX))


def agent_stem(path_or_name) -> str:
    name = path_or_name.name if isinstance(path_or_name, Path) else str(path_or_name)
    return name[: -len(AGENT_SUFFIX)] if name.endswith(AGENT_SUFFIX) else name


def agent_names():
    return [agent_stem(p) for p in agent_files()]


def agent_path(name: str) -> Path:
    return AGENTS_DIR / (name + AGENT_SUFFIX)


def claude_agent_files():
    """Claude Code のエージェント（agents/ の .md。Claude Code は、ここの .md をすべてエージェントとして読む）"""
    if not CLAUDE_AGENTS_DIR.is_dir():
        return []
    return sorted(p for p in CLAUDE_AGENTS_DIR.iterdir() if p.is_file() and p.suffix == CLAUDE_AGENT_SUFFIX)


def claude_agent_names():
    return [p.stem for p in claude_agent_files()]


def claude_agent_path(name: str) -> Path:
    return CLAUDE_AGENTS_DIR / (name + CLAUDE_AGENT_SUFFIX)


def tool_list(value):
    """エージェントの tools・disallowedTools の並び。Claude Code は「A, B」の文字列、Copilot CLI は [a, b]"""
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [t for t in value]
    return [value]


# Claude Code の frontmatter のうち、部品の名前を書いてよい行（名前と、ツールの名前）
CLAUDE_NAME_KEYS = ("name", "tools", "disallowedTools")


def without_frontmatter_keys(text: str, keys=CLAUDE_NAME_KEYS) -> str:
    """frontmatter の中の、keys の行（とその続きの行）を空にした文（行の番号は変えない）"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    out, blank = [lines[0]], False
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            out += lines[i:]
            break
        m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*:", line)
        if m:
            blank = m.group(1) in keys
        out.append("" if blank else line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def entry_scripts(skill_dir: Path):
    """スキルのスクリプト（scripts/ の直下の .py。中の部品の名前）"""
    folder = skill_dir / "scripts"
    if not folder.is_dir():
        return set()
    return {p.name for p in folder.glob("*.py") if p.is_file()}


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def strip_code_blocks(text: str) -> str:
    return re.sub(r"(?ms)^\s*```.*?^\s*```[^\n]*$", "", text)


def section(text: str, heading: str):
    """「## 見出し」から、次の「## 」の手前まで。なければ None"""
    m = re.search(r"(?ms)^## " + re.escape(heading) + r"[ \t]*\n(.*?)(?=^## |\Z)", text)
    return m.group(1) if m else None


def is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def split_cells(line: str):
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    # バッククォートの中の | は区切りではない
    cells, current, in_code = [], "", False
    for ch in body:
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(current.strip())
            current = ""
        else:
            current += ch
    cells.append(current.strip())
    return cells


def table_in(text: str):
    """最初の表を、(見出しのセル, 行のセルの並び) で返す。表がなければ (None, [])"""
    header, rows = None, []
    started = False
    for line in text.splitlines():
        if is_table_line(line):
            started = True
            cells = split_cells(line)
            if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c) and header is not None:
                continue
            if header is None:
                header = cells
            else:
                rows.append(cells)
        elif started and line.strip():
            break
    return header, rows


def split_section_table(text: str, heading: str):
    """本文を、節の表の行と、それ以外（表の外）に分ける。節がなければ (None, text)"""
    m = re.search(r"(?ms)^## " + re.escape(heading) + r"[ \t]*\n(.*?)(?=^## |\Z)", text)
    if m is None:
        return None, text
    lines = m.group(1).splitlines(keepends=True)
    table = "".join(line for line in lines if is_table_line(line))
    rest = "".join(line for line in lines if not is_table_line(line))
    return table, text[: m.start(1)] + rest + text[m.end(1):]


def parts_table(text: str, heading: str = PARTS_HEADING, header=None):
    """「## 使う部品」の表を [(役割, 名前, 使うとき)] で返す。形が違えば (None, 理由)"""
    header = header or PARTS_HEADER
    part = section(text, heading)
    if part is None:
        return None, f"「## {heading}」の節がありません"
    head, rows = table_in(part)
    if head is None:
        return None, f"「## {heading}」の節に表がありません"
    if head != header:
        return None, f"「## {heading}」の表の見出しが {header} ではありません：{head}"
    out = []
    name_col = header.index("部品")
    for cells in rows:
        if len(cells) != len(header):
            return None, f"表の行の列の数が違います：{cells}"
        m = re.fullmatch(r"`([^`]+)`", cells[name_col])
        if m is None:
            return None, f"表の部品の欄が `名前` の形ではありません：{cells[name_col]}"
        row = list(cells)
        row[name_col] = m.group(1)
        out.append(tuple(row))
    if not out:
        return None, f"「## {heading}」の表に行がありません"
    return out, None


def heading_slugs(path: Path):
    """GitHub が見出しに付けるアンカー（小文字にし、記号を消し、空白を - にする）"""
    slugs = set()
    for m in re.finditer(r"(?m)^#{1,6}\s+(.*?)\s*#*\s*$", strip_code_blocks(read(path))):
        title = re.sub(r"`", "", m.group(1)).lower()
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
        slugs.add(re.sub(r"\s", "-", re.sub(r"[^\w\s\-]", "", title)))
    return slugs


# ---------------------------------------------------------------------------
# frontmatter（PyYAML があればそれ、なければ小さな読み手）
# ---------------------------------------------------------------------------

def split_frontmatter(text: str):
    """(frontmatter の文, 本文)。frontmatter がなければ (None, text)"""
    text = text.lstrip("﻿")
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.S)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def _unquote_double(text: str) -> str:
    inner = text[1:-1]
    inner = re.sub(r"[ \t]*\n[ \t]*", " ", inner)
    try:
        return json.loads('"' + inner.replace('\\\'', "'") + '"')
    except ValueError:
        return inner


def _scalar(text: str):
    text = text.strip()
    if not text:
        return None
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return _unquote_double(text)
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return re.sub(r"[ \t]*\n[ \t]*", " ", text[1:-1]).replace("''", "'")
    text = re.sub(r"\s+#.*$", "", text)
    # YAML では、引用符のない値に「: 」を入れたり、記号で始めたりできない（Copilot CLI も読めない）
    if ": " in text or text.endswith(":"):
        raise ValueError(f"引用符のない値に「: 」があります。値を \"…\" で囲んでください：{text[:60]}")
    if text[:1] in ("`", "@", "%", "&", "*", "!", "{", "|", ">"):
        raise ValueError(f"引用符のない値が、YAML の記号「{text[:1]}」で始まっています：{text[:60]}")
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text in ("null", "~", "Null"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _flow_list(text: str):
    inner = text.strip()[1:-1]
    items, current, quote = [], "", None
    for ch in inner:
        if quote:
            current += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current += ch
        elif ch == ",":
            items.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current)
    return [_scalar(i) for i in items if i.strip()]


def _block_scalar(indicator: str, block):
    lines = [l for l in block]
    indents = [len(l) - len(l.lstrip(" ")) for l in lines if l.strip()]
    cut = min(indents) if indents else 0
    body = [l[cut:] if l.strip() else "" for l in lines]
    while body and not body[-1]:
        body.pop()
    if indicator.startswith("|"):
        value = "\n".join(body)
    else:
        value, para = "", []
        for line in body:
            if line:
                para.append(line)
            else:
                value += " ".join(para) + "\n"
                para = []
        value += " ".join(para)
    if "-" in indicator:
        return value
    return value + "\n" if value else value


def parse_simple_yaml(text: str) -> dict:
    """frontmatter の小さな YAML（鍵: 値、引用符、[…]、- の並び、| と > の複数行、続きの行）を読む"""
    lines = text.splitlines()
    data, i = {}, 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*:(?:[ \t]+(.*))?$", line)
        if not m:
            raise ValueError(f"読めない行です：{line}")
        key, rest = m.group(1), (m.group(2) or "").strip()
        i += 1
        block = []
        while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")
                                  or (not rest and lines[i].startswith("- "))):
            block.append(lines[i])
            i += 1
        if rest[:1] in ("|", ">"):
            data[key] = _block_scalar(rest, block)
        elif rest.startswith("["):
            data[key] = _flow_list(" ".join([rest] + [b.strip() for b in block]))
        elif not rest and any(b.strip().startswith("- ") or b.strip() == "-" for b in block):
            data[key] = [_scalar(b.strip()[1:]) for b in block if b.strip().startswith("-")]
        elif rest[:1] in ('"', "'"):
            data[key] = _scalar("\n".join([rest] + block))
        else:
            continuation = [b.strip() for b in block if b.strip()]
            data[key] = _scalar(" ".join([rest] + continuation)) if continuation else _scalar(rest)
    return data


def load_yaml(text: str):
    try:
        import yaml  # PyYAML（あれば）
    except ImportError:
        return parse_simple_yaml(text)
    return yaml.safe_load(text)


def frontmatter(path: Path):
    """(frontmatter の辞書, 本文, 問題)。問題がなければ None"""
    raw, body = split_frontmatter(read(path))
    if raw is None:
        return None, body, "frontmatter（--- で囲んだ先頭の部分）がありません"
    try:
        data = load_yaml(raw)
    except Exception as error:  # noqa: BLE001 - YAML の読み違いをそのまま返す
        return None, body, f"frontmatter を読めません：{error}"
    if not isinstance(data, dict):
        return None, body, "frontmatter が「鍵: 値」の形ではありません"
    return data, body, None


# ---------------------------------------------------------------------------
# 言葉を探す
# ---------------------------------------------------------------------------

def token(word: str, ignore_case: bool = True):
    """英数字・_・- に挟まれていない、単語としての出現"""
    return re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(word) + r"(?![A-Za-z0-9_\-])",
                      re.IGNORECASE if ignore_case else 0)


def name_token(word: str):
    """部品の名前としての出現（大文字と小文字を分ける。unittest の setUp などを数えない）"""
    return token(word, ignore_case=False)


def hits_in_text(label: str, text: str, patterns):
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for word, pattern in patterns:
            if pattern.search(line):
                found.append(f"{label}:{lineno}: 「{word}」 … {line.strip()[:140]}")
    return found


# ---------------------------------------------------------------------------
# 設計書の取り決め
# ---------------------------------------------------------------------------

def _config_table_rows():
    if not DESIGN.is_file():
        return []
    text = read(DESIGN)
    m = re.search(r"(?ms)^### 4\.1 .*?(?=^### )", text)
    part = m.group(0) if m else ""
    rows = []
    for header, table_rows in _tables(part):
        if header and header[0] == "項目" and "既定" in header:
            rows += table_rows
    return rows


def _tables(text: str):
    tables, header, rows = [], None, []
    for line in text.splitlines() + [""]:
        if is_table_line(line):
            cells = split_cells(line)
            if header is None:
                header = cells
            elif all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
                continue
            else:
                rows.append(cells)
        elif header is not None:
            tables.append((header, rows))
            header, rows = None, []
    return tables


def output_dir_default():
    """設計書 4.1 の output_dir の既定の値（例：../<名前>）。なければ None"""
    for cells in _config_table_rows():
        if cells and cells[0].strip("` ") == "output_dir" and len(cells) > 1:
            m = re.search(r"`([^`]+)`", cells[1])
            if m:
                return m.group(1)
    return None


# ---------------------------------------------------------------------------
# JSON Schema（jsonschema があればそれ、なければ小さな確かめ）
# ---------------------------------------------------------------------------

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class MiniValidator:
    """JSON Schema 2020-12 のよく使う語だけを確かめる（type, enum, const, properties, required,
    additionalProperties, patternProperties, propertyNames, items, prefixItems, minItems, maxItems, uniqueItems,
    minLength, maxLength, pattern, minimum, maximum, exclusiveMinimum, exclusiveMaximum, minProperties,
    maxProperties, anyOf, oneOf, allOf, not, if/then/else, $ref（同じ文書の中）, $defs）"""

    def __init__(self, schema):
        self.root = schema

    def resolve(self, ref):
        if not ref.startswith("#"):
            raise ValueError(f"外の $ref は読めません：{ref}")
        node = self.root
        for part in [p for p in ref[1:].split("/") if p]:
            part = part.replace("~1", "/").replace("~0", "~")
            node = node[int(part)] if isinstance(node, list) else node[part]
        return node

    def errors(self, instance, schema=None, path="$"):
        schema = self.root if schema is None else schema
        if schema is True:
            return []
        if schema is False:
            return [f"{path}: 何も許されていない場所です"]
        out = []
        if "$ref" in schema:
            out += self.errors(instance, self.resolve(schema["$ref"]), path)
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_TYPES[t](instance) for t in types):
                return out + [f"{path}: 型が {types} ではありません"]
        if "const" in schema and instance != schema["const"]:
            out.append(f"{path}: 値が {schema['const']!r} ではありません")
        if "enum" in schema and instance not in schema["enum"]:
            out.append(f"{path}: 値 {instance!r} は {schema['enum']} のどれでもありません")
        if isinstance(instance, str):
            if "minLength" in schema and len(instance) < schema["minLength"]:
                out.append(f"{path}: 文字数が {schema['minLength']} より少ない")
            if "maxLength" in schema and len(instance) > schema["maxLength"]:
                out.append(f"{path}: 文字数が {schema['maxLength']} より多い")
            if "pattern" in schema and not re.search(schema["pattern"], instance):
                out.append(f"{path}: 形 {schema['pattern']} に合いません")
        if isinstance(instance, (int, float)) and not isinstance(instance, bool):
            for key, ok in (("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b),
                            ("exclusiveMinimum", lambda a, b: a > b), ("exclusiveMaximum", lambda a, b: a < b)):
                if key in schema and not ok(instance, schema[key]):
                    out.append(f"{path}: {key} {schema[key]} を満たしません")
        if isinstance(instance, dict):
            props = schema.get("properties", {})
            for key in schema.get("required", []):
                if key not in instance:
                    out.append(f"{path}: 要る項目 {key} がありません")
            if "minProperties" in schema and len(instance) < schema["minProperties"]:
                out.append(f"{path}: 項目が少ない")
            if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
                out.append(f"{path}: 項目が多い")
            for key, value in instance.items():
                matched = False
                if key in props:
                    matched = True
                    out += self.errors(value, props[key], f"{path}.{key}")
                for pattern, sub in schema.get("patternProperties", {}).items():
                    if re.search(pattern, key):
                        matched = True
                        out += self.errors(value, sub, f"{path}.{key}")
                if not matched and "additionalProperties" in schema:
                    extra = schema["additionalProperties"]
                    if extra is False:
                        out.append(f"{path}: 決まっていない項目 {key} があります")
                    else:
                        out += self.errors(value, extra, f"{path}.{key}")
                if "propertyNames" in schema:
                    out += self.errors(key, schema["propertyNames"], f"{path}（鍵 {key}）")
        if isinstance(instance, list):
            prefix = schema.get("prefixItems", [])
            for i, sub in enumerate(prefix[: len(instance)]):
                out += self.errors(instance[i], sub, f"{path}[{i}]")
            if "items" in schema:
                for i, item in enumerate(instance[len(prefix):], len(prefix)):
                    out += self.errors(item, schema["items"], f"{path}[{i}]")
            if "minItems" in schema and len(instance) < schema["minItems"]:
                out.append(f"{path}: 要素が {schema['minItems']} より少ない")
            if "maxItems" in schema and len(instance) > schema["maxItems"]:
                out.append(f"{path}: 要素が {schema['maxItems']} より多い")
            if schema.get("uniqueItems"):
                seen = [json.dumps(i, sort_keys=True) for i in instance]
                if len(seen) != len(set(seen)):
                    out.append(f"{path}: 同じ要素が2つあります")
        if "allOf" in schema:
            for sub in schema["allOf"]:
                out += self.errors(instance, sub, path)
        if "anyOf" in schema and not any(not self.errors(instance, sub, path) for sub in schema["anyOf"]):
            out.append(f"{path}: anyOf のどれにも合いません")
        if "oneOf" in schema:
            count = sum(1 for sub in schema["oneOf"] if not self.errors(instance, sub, path))
            if count != 1:
                out.append(f"{path}: oneOf のちょうど1つに合いません（{count} 個に合う）")
        if "not" in schema and not self.errors(instance, schema["not"], path):
            out.append(f"{path}: not の形に合ってしまいます")
        if "if" in schema:
            branch = "then" if not self.errors(instance, schema["if"], path) else "else"
            if branch in schema:
                out += self.errors(instance, schema[branch], path)
        return out


def schema_errors(instance, schema):
    """JSON Schema に合わない点の並び（合えば []）。jsonschema があれば、それで確かめる"""
    try:
        import jsonschema  # noqa: F401
        from jsonschema import Draft202012Validator
    except ImportError:
        return MiniValidator(schema).errors(instance)
    validator = Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}" for e in validator.iter_errors(instance)]
