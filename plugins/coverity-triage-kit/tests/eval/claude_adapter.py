#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build a Claude Code test workspace from the parts of this plugin (design 9.3).

For model-based tests (EVAL stage 2 and 3) the parts run with Claude Code. Two formats:

``--format copilot-converted`` (the default) reads the GitHub Copilot CLI definitions and
converts them (below). ``--format claude-plugin`` reads the Claude Code definitions
(``agents/<name>.md``, ``.claude-plugin/plugin.json``, ``.mcp.json``) and copies the agent,
the parts of its table (followed through the agents of the table) and the MCP server, as
they are, into a temporary plugin folder outside the workspace, with its own
``.claude-plugin/plugin.json`` and ``.mcp.json``. It is run as a real plugin:
``claude -p ... --plugin-dir <folder> --agent <plugin>:<agent>``, without ``--tools`` (the way a
user runs it; ``--tools-from-definitions`` adds the union of the definitions' built-in tools).
Only ``--agent-model`` (every agent) and ``--main-model`` (the main agent) change a copy (the
``model`` line). The folder gets a marker file (``.claude-eval-plugin``); a later build empties
only a folder with that marker and refuses any other non-empty folder.

The copilot-converted format:

- converts Copilot agent definitions (``<name>.agent.md``) into Claude Code agents:
  tool names are mapped, ``skills:`` lists the skills of the agent's
  ``## 使う部品`` table, and a one-paragraph glossary is put in front of the
  unchanged body;
- copies skill folders into ``<ws>/.claude/skills/<name>/``;
- copies MCP server folders into ``<ws>/.claude/mcp/<name>/`` and writes an MCP
  config for them (optionally pointing the server at a fake Coverity);
- writes the ``--agents`` JSON and prints the exact ``claude -p`` command line.

``--parts-from-table <agent file>`` collects the agent and every part named in its
table, recursively through the agents of the table (stage-2/3 extraction). Names are
read from the tables only; nothing in this file names a part.

Commands (one JSON object on stdout, progress on stderr):

  collect AGENT_FILE [--plugin-root DIR] [--format F]
  build --workspace WS [--format copilot-converted|claude-plugin] [--plugin-dir DIR]
        [--tools-from-definitions] [--parts-from-table FILE] [--agent FILE]... [--skill DIR]...
        [--mcp [NAME=]DIR]... [--main NAME] [--output-dir DIR] [--fake-data FILE]
        [--mcp-env KEY=VALUE]... [--model M] [--agent-model M] [--main-model M] [--prompt TEXT]
        [--extra-arg ARG]... [--allow-missing] [--run] [--inspect] [--timeout S]
  run --workspace WS --prompt TEXT [--timeout S] [--inspect] [--claude-bin EXE [ARG...]]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PLUGIN_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE

AGENTS_REL = Path("com.github.copilot") / "agents"
AGENT_SUFFIX = ".agent.md"
# The Claude Code form of the same plugin (design 2.3).
CLAUDE_AGENTS_REL = Path("agents")
CLAUDE_AGENT_SUFFIX = ".md"
CLAUDE_MANIFEST_REL = Path(".claude-plugin") / "plugin.json"
CLAUDE_MCP_JSON = ".mcp.json"
COPILOT_MCP_JSON = "mcp.json"
FORMAT_CONVERTED = "copilot-converted"
FORMAT_PLUGIN = "claude-plugin"
FORMATS = (FORMAT_CONVERTED, FORMAT_PLUGIN)
PLUGIN_ROOT_VARS = ("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}")
# Manifest keys that point at files; the copy keeps the default places instead.
MANIFEST_PATH_KEYS = {"skills", "agents", "commands", "hooks", "mcpServers", "outputStyles", "lspServers"}
# The Claude Code form of the uv permission (design 9.3); the converted format keeps its old form.
PLUGIN_BASH_ALLOW = "Bash(uv run *)"
# The file a claude-plugin build writes into the folder it makes (only such a folder is emptied).
PLUGIN_MARKER = ".claude-eval-plugin"
PLUGIN_MARKER_DATA = {"made_by": "claude_adapter"}
TABLE_HEADING = "使う部品"
TABLE_HEADER = ["役割", "部品", "使うとき"]

# Copilot CLI tool aliases -> Claude Code tools (design 9.3).
TOOL_MAP: dict[str, list[str]] = {
    "read": ["Read", "Glob", "Grep"],
    "search": ["Grep", "Glob"],
    "edit": ["Edit", "Write"],
    "execute": ["Bash"],
    "agent": ["Agent"],
    "skill": ["Skill"],
    "ask_user": ["AskUserQuestion"],
}

# Built-in tools for an agent without a tools line (Copilot CLI gives it every tool).
BASE_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "Skill", "Agent", "AskUserQuestion"]
# Claude Code reports some tools under another name in its init event.
TOOL_ALIASES = {"Agent": "Task"}
# Tools that `claude -p` never offers (design 9.3): not counted as missing.
NOT_IN_PRINT_MODE = {"AskUserQuestion"}

GLOSSARY = (
    "（Claude Code で試すための読み替え）この定義は GitHub Copilot CLI 向けに書かれています。"
    "Claude Code では、次のように読み替えてください。ask_user は AskUserQuestion ツールです"
    "（このツールがないときが「ask_user が使えないとき」です）。skill ツールは Skill ツールです。"
    "エージェントを呼ぶ `task` は Agent ツールで、`subagent_type` にエージェントの名前を渡します"
    "（`task` の background は Agent の run_in_background に当たります）。"
    "`<登録名>/<ツール>` の形の MCP のツールは `mcp__<登録名>__<ツール>` です。"
    "ファイルを読む・探す・直すツールは Read・Glob・Grep・Edit・Write、シェルは Bash です。"
    "「/agent で選ぶ」は、この会話でこのエージェントとして動いていることに当たります。"
)

COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".venv", ".pytest_cache", ".ruff_cache", "node_modules", ".mypy_cache"
)

PROMPT_PLACEHOLDER = "<頼みの文>"


class AdapterError(Exception):
    """A problem the caller can fix (bad file, missing part, bad argument)."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


# --------------------------------------------------------------------------- output


def _stdout_encoding() -> str:
    env = os.environ.get("PYTHONIOENCODING")
    if env:
        name = env.split(":", 1)[0].strip()
        if name:
            return name
    if os.name == "nt" and not sys.stdout.isatty():
        try:
            import ctypes

            cp = ctypes.windll.kernel32.GetConsoleOutputCP()  # type: ignore[attr-defined]
            if cp:
                return f"cp{cp}"
        except Exception:  # noqa: BLE001 - fall back to UTF-8
            pass
    return "utf-8"


def _escape_unencodable(text: str, encoding: str) -> str:
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        pass
    out = []
    for ch in text:
        try:
            ch.encode(encoding)
            out.append(ch)
        except UnicodeEncodeError:
            code = ord(ch)
            if code > 0xFFFF:
                code -= 0x10000
                out.append("\\u%04x\\u%04x" % (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)))
            else:
                out.append("\\u%04x" % code)
    return "".join(out)


def emit(obj: dict) -> None:
    encoding = _stdout_encoding()
    try:
        "".encode(encoding)
    except LookupError:
        encoding = "utf-8"
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    data = (_escape_unencodable(text, encoding) + "\n").encode(encoding)
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


VERBOSE = True  # tests switch this off


def progress(message: str) -> None:
    if not VERBOSE:
        return
    try:
        print(f"[claude_adapter] {message}", file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        print(f"[claude_adapter] {message}".encode("ascii", "backslashreplace").decode("ascii"),
              file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- parsing


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _split_flow(inner: str) -> list[str]:
    items, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    last = "".join(buf).strip()
    if last:
        items.append(last)
    return [i for i in items if i != ""]


def _scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(v) for v in _split_flow(value[1:-1])]
    return value


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (fields, body) of a Markdown file with a simple YAML frontmatter."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise AdapterError("frontmatter（先頭の --- の行）がありません")
    end = text.find("\n---", 4)
    if end < 0:
        raise AdapterError("frontmatter の終わり（--- の行）がありません")
    block = text[4:end]
    rest = text[end + 4:]
    body = rest[1:] if rest.startswith("\n") else rest
    fields: dict = {}
    key = None
    for line in block.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):(.*)$", line)
        if m and not line.startswith(" "):
            key = m.group(1)
            raw = m.group(2).strip()
            fields[key] = _scalar(raw) if raw else []
            continue
        m = re.match(r"^\s+-\s*(.*)$", line)
        if m and key is not None:
            if not isinstance(fields.get(key), list):
                fields[key] = []
            fields[key].append(_scalar(m.group(1)))
            continue
        raise AdapterError(f"frontmatter の行を読めません: {line!r}")
    return fields, body


def _row_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_parts_table(body: str) -> list[dict]:
    """Rows of the ``## 使う部品`` table: [{role, part, when}]."""
    lines = body.replace("\r\n", "\n").split("\n")
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^##\s+" + re.escape(TABLE_HEADING) + r"\s*$", line):
            start = i + 1
            break
    if start is None:
        raise AdapterError(f"「## {TABLE_HEADING}」の見出しがありません")
    table: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        if line.strip().startswith("|"):
            table.append(line)
        elif table:
            break
    if len(table) < 2:
        raise AdapterError(f"「## {TABLE_HEADING}」の下に表がありません")
    header = _row_cells(table[0])
    if header != TABLE_HEADER:
        raise AdapterError(f"表の見出しが {TABLE_HEADER} ではありません: {header}")
    rows = []
    for line in table[2:]:
        cells = _row_cells(line)
        if len(cells) != 3:
            raise AdapterError(f"表の行の列が3つではありません: {line!r}")
        m = re.fullmatch(r"`([^`]+)`", cells[1])
        if not m:
            raise AdapterError(f"部品の名前は `名前` の形で書きます: {cells[1]!r}")
        rows.append({"role": cells[0], "part": m.group(1), "when": cells[2]})
    if not rows:
        raise AdapterError(f"「## {TABLE_HEADING}」の表に行がありません")
    return rows


def agent_name(path: Path, suffix: str = AGENT_SUFFIX) -> str:
    name = path.name
    if not name.endswith(suffix) or len(name) == len(suffix):
        raise AdapterError(f"エージェントの定義のファイル名は <名前>{suffix} です: {path}")
    return name[: -len(suffix)]


def load_agent(path: Path, suffix: str | None = None) -> dict:
    """Read an agent definition. Without ``suffix``: <name>.agent.md (Copilot CLI), else <name>.md."""
    path = Path(path).resolve()
    if not path.is_file():
        raise AdapterError(f"エージェントの定義がありません: {path}")
    if suffix is None:
        suffix = AGENT_SUFFIX if path.name.endswith(AGENT_SUFFIX) else CLAUDE_AGENT_SUFFIX
    fields, body = parse_frontmatter(read_text(path))
    return {
        "name": agent_name(path, suffix),
        "path": path,
        "fields": fields,
        "body": body,
        "table": parse_parts_table(body),
    }


def skill_name(folder: Path) -> str:
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        raise AdapterError(f"SKILL.md がありません: {folder}")
    try:
        fields, _ = parse_frontmatter(read_text(skill_md))
    except AdapterError:
        fields = {}
    name = fields.get("name")
    return name if isinstance(name, str) and name else folder.name


# --------------------------------------------------------------------------- mapping


def map_tools(tools) -> tuple[list[str], list[str]]:
    """Map Copilot tool names to Claude Code tool names. Returns (mapped, unknown)."""
    mapped: list[str] = []
    unknown: list[str] = []
    for tool in tools or []:
        tool = str(tool).strip()
        if tool in TOOL_MAP:
            targets = TOOL_MAP[tool]
        elif "/" in tool:
            server, _, name = tool.partition("/")
            targets = [f"mcp__{server}__{name}" if name and name != "*" else f"mcp__{server}"]
        else:
            unknown.append(tool)
            continue
        for t in targets:
            if t not in mapped:
                mapped.append(t)
    return mapped, unknown


def claude_model(models, override: str | None = None) -> str:
    """Pick the first claude-* model of a Copilot model list, as a Claude Code model id."""
    if override:
        return override
    if isinstance(models, str):
        models = [models]
    for m in models or []:
        m = str(m).strip()
        if m.lower().startswith("claude-"):
            return m.lower().replace(".", "-")
    return "inherit"


# --------------------------------------------------------------------------- plugin layout


class Layout:
    """Where the parts of a plugin live: agents, skills, MCP servers.

    ``flavor`` "copilot" reads com.github.copilot/agents/<name>.agent.md and mcp.json;
    "claude" reads agents/<name>.md and .mcp.json. Skills and MCP servers are the same folders.
    """

    def __init__(self, root: Path, extra_skills: dict[str, Path] | None = None,
                 extra_mcp: dict[str, Path] | None = None, flavor: str = "copilot"):
        if flavor not in ("copilot", "claude"):
            raise AdapterError(f"知らない形です: {flavor}")
        self.root = Path(root).resolve()
        self.flavor = flavor
        claude = flavor == "claude"
        self.agents_dir = self.root / (CLAUDE_AGENTS_REL if claude else AGENTS_REL)
        self.suffix = CLAUDE_AGENT_SUFFIX if claude else AGENT_SUFFIX
        self.mcp_json = self.root / (CLAUDE_MCP_JSON if claude else COPILOT_MCP_JSON)
        self.skills_dir = self.root / "skills"
        self.mcp_dir = self.root / "mcp-servers"
        self.extra_skills = dict(extra_skills or {})
        self.extra_mcp = dict(extra_mcp or {})
        self.mcp_entries: dict[str, dict] = {}
        if self.mcp_json.is_file():
            try:
                data = json.loads(read_text(self.mcp_json))
                servers = data.get("mcpServers") or data.get("servers") or {}
                if isinstance(servers, dict):
                    self.mcp_entries = {k: v for k, v in servers.items() if isinstance(v, dict)}
            except json.JSONDecodeError as exc:
                raise AdapterError(f"{self.mcp_json.name} を読めません: {exc}") from exc

    def agent_file(self, name: str) -> Path | None:
        p = self.agents_dir / f"{name}{self.suffix}"
        return p if p.is_file() else None

    def skill_dir(self, name: str) -> Path | None:
        if name in self.extra_skills:
            return self.extra_skills[name]
        p = self.skills_dir / name
        return p if (p / "SKILL.md").is_file() else None

    def mcp_folder(self, name: str) -> Path | None:
        if name in self.extra_mcp:
            return self.extra_mcp[name]
        entry = self.mcp_entries.get(name)
        if entry:
            directory = _directory_arg(entry.get("args") or [])
            if directory:
                for var in PLUGIN_ROOT_VARS:
                    directory = directory.replace(var, str(self.root))
                p = Path(directory)
                if not p.is_absolute():
                    p = self.root / p
                if p.is_dir():
                    return p.resolve()
        p = self.mcp_dir / name
        return p if p.is_dir() else None

    def is_mcp(self, name: str) -> bool:
        return name in self.extra_mcp or name in self.mcp_entries or (self.mcp_dir / name).is_dir()


def _directory_arg(args: list) -> str | None:
    args = [str(a) for a in args]
    for i, a in enumerate(args):
        if a == "--directory" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--directory="):
            return a.split("=", 1)[1]
    return None


def guess_plugin_root(agent_file: Path) -> Path:
    agent_file = Path(agent_file).resolve()
    parts = agent_file.parent
    if parts.name == "agents" and parts.parent.name == "com.github.copilot":
        return parts.parent.parent
    if parts.name == "agents" and (parts.parent / CLAUDE_MANIFEST_REL).is_file():
        return parts.parent
    return DEFAULT_PLUGIN_ROOT


def claude_agent_file(root: Path, name: str) -> Path:
    """The Claude Code definition of an agent: <root>/agents/<name>.md."""
    path = Path(root).resolve() / CLAUDE_AGENTS_REL / f"{name}{CLAUDE_AGENT_SUFFIX}"
    if not path.is_file():
        raise AdapterError(f"Claude Code のエージェントの定義がありません: {path}")
    return path


def collect_parts(agent_file: Path, layout: Layout, recursive: bool = True) -> dict:
    """The agent and every part of its table, following the agents of the table."""
    agents: list[dict] = []
    skills: dict[str, Path | None] = {}
    mcp: dict[str, Path | None] = {}
    missing: list[dict] = []
    seen: set[str] = set()
    queue = [Path(agent_file).resolve()]
    while queue:
        path = queue.pop(0)
        agent = load_agent(path, layout.suffix)
        if agent["name"] in seen:
            continue
        seen.add(agent["name"])
        agent["skills"] = []
        agents.append(agent)
        for row in agent["table"]:
            name = row["part"]
            sub = layout.agent_file(name)
            if sub is not None:
                if recursive and name not in seen:
                    queue.append(sub)
                continue
            if layout.is_mcp(name):
                mcp.setdefault(name, layout.mcp_folder(name))
                if mcp[name] is None:
                    missing.append({"name": name, "kind": "mcp", "from": agent["name"]})
                continue
            folder = layout.skill_dir(name)
            if folder is not None:
                skills.setdefault(name, folder)
                agent["skills"].append(name)
                continue
            missing.append({"name": name, "kind": "unknown", "from": agent["name"]})
    return {"agents": agents, "skills": skills, "mcp": mcp, "missing": missing}


# --------------------------------------------------------------------------- conversion


def convert_agent(agent: dict, skills: list[str], model_override: str | None = None) -> tuple[dict, list[str]]:
    """Return (claude agent definition, problems)."""
    fields = agent["fields"]
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip():
        raise AdapterError(f"description がありません: {agent['path']}")
    problems: list[str] = []
    definition: dict = {"description": description}
    if "tools" in fields:
        mapped, unknown = map_tools(fields.get("tools"))
        for u in unknown:
            problems.append(f"{agent['name']}: Claude Code に読み替えられないツールを外しました: {u}")
        definition["tools"] = mapped
    definition["model"] = claude_model(fields.get("model"), model_override)
    definition["skills"] = list(skills)
    definition["prompt"] = GLOSSARY + "\n\n" + agent["body"].lstrip("\n")
    return definition, problems


def render_agent_md(name: str, definition: dict) -> str:
    lines = ["---", f"name: {name}", f"description: {json.dumps(definition['description'], ensure_ascii=False)}"]
    if "tools" in definition:
        lines.append(f"tools: {', '.join(definition['tools'])}")
    lines.append(f"model: {definition['model']}")
    if definition.get("skills"):
        lines.append("skills:")
        lines.extend(f"  - {s}" for s in definition["skills"])
    lines.append("---")
    return "\n".join(lines) + "\n\n" + definition["prompt"]


def _pyproject_script(folder: Path) -> str | None:
    pyproject = folder / "pyproject.toml"
    if not pyproject.is_file():
        return None
    text = read_text(pyproject)
    m = re.search(r"^\[project\.scripts\]\s*\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not m:
        return None
    for line in m.group(1).splitlines():
        mm = re.match(r"^\s*\"?([A-Za-z0-9_.-]+)\"?\s*=", line)
        if mm:
            return mm.group(1)
    return None


def _replace_directory(args: list[str], value: str) -> tuple[list[str], bool]:
    """Put ``value`` in place of the --directory argument. Returns (args, replaced)."""
    directory = _directory_arg(args)
    new_args = []
    replaced = False
    for i, a in enumerate(args):
        if directory is not None and a == directory and i > 0 and args[i - 1] == "--directory":
            new_args.append(value)
            replaced = True
        elif a.startswith("--directory="):
            new_args.append(f"--directory={value}")
            replaced = True
        else:
            new_args.append(a)
    return new_args, replaced


def fake_env(fake_data: Path | None, env_overrides: dict[str, str]) -> dict[str, str]:
    """The MCP server's variables for the fake Coverity (empty without fake data)."""
    if fake_data is None:
        return {}
    return {
        "COVERITY_API": "fake",
        "COVERITY_FAKE_DATA": str(fake_data),
        "COVERITY_URL": env_overrides.get("COVERITY_URL", "https://coverity.invalid"),
        "COV_USER": "eval-user",
        "COV_AUTH_KEY": "eval-key",
    }


def mcp_server_config(name: str, folder: Path, entry: dict | None, env_overrides: dict[str, str],
                      fake_data: Path | None) -> tuple[dict, list[str]]:
    problems: list[str] = []
    if entry and entry.get("command"):
        command = str(entry["command"])
        args, replaced = _replace_directory([str(a) for a in entry.get("args") or []], str(folder))
        if not replaced:
            problems.append(f"{name}: mcp.json の args に --directory がありません。そのまま使います")
        base_env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
    else:
        script = _pyproject_script(folder)
        if not script:
            raise AdapterError(f"MCP サーバー {name} の起動のしかたが分かりません（mcp.json の登録も pyproject.toml の [project.scripts] もありません）")
        command = "uv"
        args = ["run", "--native-tls", "--frozen", "--quiet", "--directory", str(folder), script]
        base_env = {}
    env: dict[str, str] = {}
    for k, v in base_env.items():
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", v)
        if m and m.group(1) not in os.environ:
            continue  # leave unset variables out instead of passing a literal ${...}
        env[k] = v
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(fake_env(fake_data, env_overrides))
    env.update(env_overrides)
    return {"type": "stdio", "command": command, "args": args, "env": env}, problems


def plugin_mcp_config(name: str, rel: str, folder: Path, entry: dict | None, env_overrides: dict[str, str],
                      fake_data: Path | None) -> tuple[dict, list[str]]:
    """The .mcp.json entry of a server copied to <plugin>/<rel> (paths through ${CLAUDE_PLUGIN_ROOT})."""
    problems: list[str] = []
    directory = "${CLAUDE_PLUGIN_ROOT}/" + rel
    if entry and entry.get("command"):
        command = str(entry["command"])
        args, replaced = _replace_directory([str(a) for a in entry.get("args") or []], directory)
        if not replaced:
            problems.append(f"{name}: {CLAUDE_MCP_JSON} の args に --directory がありません。そのまま使います")
        env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
        kind = str(entry.get("type") or "stdio")
    else:
        script = _pyproject_script(folder)
        if not script:
            raise AdapterError(f"MCP サーバー {name} の起動のしかたが分かりません（{CLAUDE_MCP_JSON} の登録も pyproject.toml の [project.scripts] もありません）")
        command = "uv"
        args = ["run", "--native-tls", "--frozen", "--quiet", "--directory", directory, script]
        env = {}
        kind = "stdio"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(fake_env(fake_data, env_overrides))
    env.update(env_overrides)
    return {"type": kind, "command": command, "args": args, "env": env}, problems


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=COPY_IGNORE)


def bash_line(argv: list[str]) -> str:
    return shlex.join(argv)


def powershell_line(argv: list[str]) -> str:
    def q(a: str) -> str:
        if a and re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", a):
            return a
        return "'" + a.replace("'", "''") + "'"
    return " ".join(q(a) for a in argv)


def builtin_tools(definitions: dict[str, dict]) -> list[str]:
    """Union of the built-in tools the converted agents use (for --tools).

    An agent's tools line only narrows what the session offers, and the default set of
    some Claude Code builds has no Glob or Grep, so the session gets exactly this set.
    """
    out: list[str] = []
    for d in definitions.values():
        for tool in d.get("tools", BASE_TOOLS):
            if not tool.startswith("mcp__") and tool not in out:
                out.append(tool)
    return out


def claude_argv(claude_bin: list[str], prompt: str, main: str, agents_json: Path, mcp_json: Path,
                mcp_names: list[str], output_dir: Path | None, model: str | None,
                extra: list[str], tools: list[str] | None = None) -> list[str]:
    # The prompt goes right after -p: --allowedTools, --mcp-config and --add-dir take
    # several values and would swallow a prompt written after them.
    argv = list(claude_bin) + ["-p", prompt, "--agent", main, "--agents", str(agents_json),
                               "--mcp-config", str(mcp_json), "--strict-mcp-config",
                               "--setting-sources", "project", "--permission-mode", "acceptEdits"]
    if tools:
        argv += ["--tools", ",".join(tools)]
    # Skill: Claude Code asks before it runs a skill whose frontmatter has allowed-tools (the
    # skills that run scripts have ["shell(uv run:*)"]), and claude -p denies what it would ask.
    argv += ["--allowedTools", "Bash(uv run:*)", "Skill"]
    argv += [f"mcp__{n}__*" for n in mcp_names]
    if output_dir is not None:
        argv += ["--add-dir", str(output_dir)]
    if model:
        argv += ["--model", model]
    argv += list(extra)
    return argv


def _parse_kv(values: list[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values or []:
        if "=" not in v:
            raise AdapterError(f"{what} は KEY=VALUE の形で渡します: {v!r}")
        k, _, val = v.partition("=")
        out[k.strip()] = val
    return out


def _format_of(args: argparse.Namespace) -> str:
    fmt = getattr(args, "format", None) or FORMAT_CONVERTED
    if fmt not in FORMATS:
        raise AdapterError(f"--format は {' か '.join(FORMATS)} です: {fmt}")
    return fmt


def gather(args: argparse.Namespace, flavor: str = "copilot") -> dict:
    """Read the agents of --parts-from-table / --agent and the parts of their tables."""
    ws = Path(args.workspace).resolve()
    first_agent = Path(args.parts_from_table or (args.agent or [None])[0] or "")
    if not args.parts_from_table and not args.agent:
        raise AdapterError("--parts-from-table か --agent で、エージェントの定義を1つ以上渡してください")
    root = Path(args.plugin_root).resolve() if args.plugin_root else guess_plugin_root(first_agent)
    if _inside(ws, root):
        raise AdapterError(f"作業フォルダがプラグインのフォルダの中です。外の場所にしてください: {ws}")

    extra_skills: dict[str, Path] = {}
    for d in args.skill or []:
        folder = Path(d).resolve()
        extra_skills[skill_name(folder)] = folder
    extra_mcp: dict[str, Path] = {}
    for spec in args.mcp or []:
        name, sep, d = spec.partition("=")
        folder = Path(d if sep else name).resolve()
        if not folder.is_dir():
            raise AdapterError(f"MCP サーバーのフォルダがありません: {folder}")
        extra_mcp[name if sep else folder.name] = folder
    layout = Layout(root, extra_skills, extra_mcp, flavor)

    agents: list[dict] = []
    skills: dict[str, Path | None] = dict(extra_skills)
    mcp: dict[str, Path | None] = dict(extra_mcp)
    missing: list[dict] = []
    files = [Path(args.parts_from_table)] if args.parts_from_table else []
    files += [Path(a) for a in args.agent or []]
    if flavor == "claude":
        wrong = [str(f) for f in files if f.name.endswith(AGENT_SUFFIX)]
        if wrong:
            raise AdapterError(f"claude-plugin では、Claude Code の定義（{CLAUDE_AGENTS_REL.as_posix()}/<名前>{CLAUDE_AGENT_SUFFIX}）を渡します",
                               wrong)
    seen: set[str] = set()
    for i, f in enumerate(files):
        collected = collect_parts(f, layout, recursive=bool(args.parts_from_table) and i == 0)
        for a in collected["agents"]:
            if a["name"] not in seen:
                seen.add(a["name"])
                agents.append(a)
        for k, v in collected["skills"].items():
            skills.setdefault(k, v)
        for k, v in collected["mcp"].items():
            mcp.setdefault(k, v)
        missing += [m for m in collected["missing"] if m not in missing]

    if missing and not args.allow_missing:
        raise AdapterError("表の部品が見つかりません（--allow-missing で、ないまま作れます）",
                           [f"{m['name']}（{m['from']} の表）" for m in missing])

    main = args.main or agents[0]["name"]
    if main not in {a["name"] for a in agents}:
        raise AdapterError(f"--main のエージェントがありません: {main}")
    return {"ws": ws, "root": root, "layout": layout, "agents": agents, "skills": skills, "mcp": mcp,
            "missing": missing, "main": main}


def build(args: argparse.Namespace) -> dict:
    if _format_of(args) == FORMAT_PLUGIN:
        return build_plugin(args)
    g = gather(args, "copilot")
    ws, root, layout, agents, skills, mcp, missing, main = (
        g["ws"], g["root"], g["layout"], g["agents"], g["skills"], g["mcp"], g["missing"], g["main"])

    claude_dir = ws / ".claude"
    eval_dir = claude_dir / "eval"
    for d in (claude_dir / "agents", claude_dir / "skills", eval_dir):
        d.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    copied_skills = {}
    for name, folder in skills.items():
        if folder is None:
            continue
        dst = claude_dir / "skills" / name
        progress(f"skill {name} <- {folder}")
        _copy_tree(folder, dst)
        copied_skills[name] = str(dst)

    agents_json: dict[str, dict] = {}
    for agent in agents:
        own_skills = [s for s in agent["skills"] if s in copied_skills]
        definition, probs = convert_agent(agent, own_skills, args.agent_model)
        problems += probs
        agents_json[agent["name"]] = definition
        (claude_dir / "agents" / f"{agent['name']}.md").write_text(
            render_agent_md(agent["name"], definition), encoding="utf-8")

    fake_data = Path(args.fake_data).resolve() if args.fake_data else None
    if fake_data is not None and not fake_data.is_file():
        raise AdapterError(f"偽の警告のファイルがありません: {fake_data}")
    env_overrides = _parse_kv(args.mcp_env, "--mcp-env")
    servers: dict[str, dict] = {}
    for name, folder in mcp.items():
        if folder is None:
            continue
        dst = claude_dir / "mcp" / name
        progress(f"mcp {name} <- {folder}")
        _copy_tree(folder, dst)
        config, probs = mcp_server_config(name, dst, layout.mcp_entries.get(name), env_overrides, fake_data)
        problems += probs
        servers[name] = config

    agents_file = eval_dir / "agents.json"
    mcp_file = eval_dir / "mcp.json"
    agents_file.write_text(json.dumps(agents_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mcp_file.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    output_dir = Path(args.output_dir).resolve() if args.output_dir else (ws.parent / "coverity-triage-out")
    output_dir.mkdir(parents=True, exist_ok=True)
    claude_bin = args.claude_bin or ["claude"]
    argv = claude_argv(claude_bin, PROMPT_PLACEHOLDER, main, agents_file, mcp_file, sorted(servers),
                       output_dir, args.model, args.extra_arg or [], builtin_tools(agents_json))
    (eval_dir / "command.json").write_text(json.dumps(
        {"cwd": str(ws), "argv": argv, "prompt_index": argv.index(PROMPT_PLACEHOLDER)},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shown = [args.prompt if (a == PROMPT_PLACEHOLDER and args.prompt) else a for a in argv]
    result = {
        "ok": True,
        "format": FORMAT_CONVERTED,
        "workspace": str(ws),
        "plugin_root": str(root),
        "main_agent": main,
        "agents": [{"name": a["name"], "source": str(a["path"]), "skills": agents_json[a["name"]]["skills"],
                    "tools": agents_json[a["name"]].get("tools"), "model": agents_json[a["name"]]["model"]}
                   for a in agents],
        "skills": copied_skills,
        "mcp_servers": {k: str(claude_dir / "mcp" / k) for k in servers},
        "missing": missing,
        "problems": problems,
        "files": {"agents_json": str(agents_file), "mcp_config": str(mcp_file),
                  "command": str(eval_dir / "command.json")},
        "cwd": str(ws),
        "command": shown,
        "command_line_bash": bash_line(shown),
        "command_line_powershell": powershell_line(shown),
    }
    if args.run:
        if not args.prompt:
            raise AdapterError("--run には --prompt が要ります")
        result["run"] = run_saved(ws, args.prompt, args.timeout, inspect=args.inspect)
        result["ok"] = result["run"]["ok"]
    return result


# --------------------------------------------------------------------------- the claude-plugin format


def tool_list(value) -> list[str]:
    """A tools / disallowedTools value of a Claude Code agent (comma list or YAML list)."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(t).strip() for t in items if str(t).strip()]


def with_model(text: str, model: str) -> str:
    """The agent file with its frontmatter ``model`` line set to ``model`` (other lines unchanged)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    if not lines or lines[0].lstrip("\ufeff") != "---":
        raise AdapterError("frontmatter（先頭の --- の行）がありません")
    end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if end is None:
        raise AdapterError("frontmatter の終わり（--- の行）がありません")
    for i in range(1, end):
        if re.match(r"^model:", lines[i]):
            lines[i] = f"model: {model}"
            break
    else:
        lines.insert(end, f"model: {model}")
    return newline.join(lines)


def read_manifest(root: Path) -> dict:
    path = Path(root) / CLAUDE_MANIFEST_REL
    if not path.is_file():
        raise AdapterError(f"Claude Code のマニフェストがありません: {path}")
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Claude Code のマニフェストを読めません: {exc}") from exc
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise AdapterError(f"Claude Code のマニフェストに name がありません: {path}")
    return data


def _made_by_adapter(dest: Path) -> bool:
    """True when ``dest`` holds the marker a claude-plugin build writes (and nothing else claims it)."""
    marker = dest / PLUGIN_MARKER
    if not marker.is_file():
        return False
    try:
        data = json.loads(read_text(marker))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("made_by") == PLUGIN_MARKER_DATA["made_by"]


def _fresh_plugin_dir(dest: Path) -> None:
    """Empty ``dest`` for a new copy.

    Only a folder this tool made (it has the marker file) is emptied. Any other non-empty
    folder is refused and left as it is, also a real plugin with ``.claude-plugin/plugin.json``.
    """
    if dest.exists():
        if not dest.is_dir():
            raise AdapterError(f"プラグインを作る場所がファイルです: {dest}")
        if any(dest.iterdir()):
            if not _made_by_adapter(dest):
                raise AdapterError(f"プラグインを作るフォルダが空ではありません（この道具が作ったフォルダの印 {PLUGIN_MARKER} が"
                                   f"ありません）。別の場所にしてください: {dest}")
            shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / PLUGIN_MARKER).write_text(json.dumps(PLUGIN_MARKER_DATA) + "\n", encoding="utf-8")


def plugin_argv(claude_bin: list[str], prompt: str, plugin_dir: Path, agent_ref: str, plugin: str,
                mcp_names: list[str], output_dir: Path | None, model: str | None, extra: list[str],
                tools: list[str] | None = None) -> list[str]:
    """claude -p with the copied plugin (design 9.3). The prompt goes right after -p."""
    argv = list(claude_bin) + ["-p", prompt, "--plugin-dir", str(plugin_dir), "--agent", agent_ref,
                               "--setting-sources", "project", "--permission-mode", "acceptEdits"]
    if tools:
        argv += ["--tools", ",".join(tools)]
    argv += ["--allowedTools", PLUGIN_BASH_ALLOW, "Skill"]
    argv += [f"mcp__plugin_{plugin}_{n}__*" for n in mcp_names]
    if output_dir is not None:
        argv += ["--add-dir", str(output_dir)]
    if model:
        argv += ["--model", model]
    argv += list(extra)
    return argv


def build_plugin(args: argparse.Namespace) -> dict:
    """Copy the Claude Code agent(s), the table's skills and MCP servers into a temporary plugin."""
    g = gather(args, "claude")
    ws, root, layout, agents, skills, mcp, missing, main = (
        g["ws"], g["root"], g["layout"], g["agents"], g["skills"], g["mcp"], g["missing"], g["main"])
    manifest = read_manifest(root)
    plugin = manifest["name"]
    dest = Path(args.plugin_dir).resolve() if getattr(args, "plugin_dir", None) else (ws.parent / "claude-plugin").resolve()
    if _inside(dest, root):
        raise AdapterError(f"プラグインを作る場所が、元のプラグインのフォルダの中です。外の場所にしてください: {dest}")
    if _inside(dest, ws) or _inside(ws, dest):
        raise AdapterError(f"プラグインを作る場所と作業フォルダは、別のところにしてください（作業フォルダの中や上は不可）: {dest}")
    _fresh_plugin_dir(dest)

    problems: list[str] = []
    for leftover in ("agents", "skills"):
        if (ws / ".claude" / leftover).is_dir():
            problems.append(f"作業フォルダに .claude/{leftover}/ があります。プラグインの部品と一緒に読まれます")

    (dest / CLAUDE_MANIFEST_REL).parent.mkdir(parents=True)
    copied_manifest = {k: v for k, v in manifest.items() if k not in MANIFEST_PATH_KEYS}
    (dest / CLAUDE_MANIFEST_REL).write_text(json.dumps(copied_manifest, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")

    (dest / CLAUDE_AGENTS_REL).mkdir()
    main_model = getattr(args, "main_model", None)
    agent_rows = []
    defs: dict[str, dict] = {}
    for agent in agents:
        target = dest / CLAUDE_AGENTS_REL / f"{agent['name']}{CLAUDE_AGENT_SUFFIX}"
        progress(f"agent {agent['name']} <- {agent['path']}")
        new_model = (main_model if agent["name"] == main and main_model else None) or args.agent_model
        if new_model:
            raw = agent["path"].read_bytes().decode("utf-8")
            target.write_bytes(with_model(raw, new_model).encode("utf-8"))
        else:
            shutil.copyfile(agent["path"], target)
        fields = agent["fields"]
        definition = {"tools": tool_list(fields["tools"])} if "tools" in fields else {}
        defs[agent["name"]] = definition
        agent_rows.append({"name": agent["name"], "ref": f"{plugin}:{agent['name']}", "source": str(agent["path"]),
                           "file": str(target), "skills": list(agent["skills"]), "tools": definition.get("tools"),
                           "disallowed_tools": tool_list(fields.get("disallowedTools")) or None,
                           "model": new_model or (str(fields["model"]) if fields.get("model") else "inherit")})

    copied_skills: dict[str, str] = {}
    for name, folder in skills.items():
        if folder is None:
            continue
        target = dest / "skills" / name
        progress(f"skill {name} <- {folder}")
        _copy_tree(folder, target)
        copied_skills[name] = str(target)

    fake_data = Path(args.fake_data).resolve() if args.fake_data else None
    if fake_data is not None and not fake_data.is_file():
        raise AdapterError(f"偽の警告のファイルがありません: {fake_data}")
    env_overrides = _parse_kv(args.mcp_env, "--mcp-env")
    servers: dict[str, dict] = {}
    server_dirs: dict[str, str] = {}
    for name, folder in mcp.items():
        if folder is None:
            continue
        try:
            rel = Path(folder).resolve().relative_to(root).as_posix()
        except ValueError:
            rel = f"mcp-servers/{name}"
        target = dest / rel
        progress(f"mcp {name} <- {folder}")
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(folder, target)
        config, probs = plugin_mcp_config(name, rel, target, layout.mcp_entries.get(name), env_overrides, fake_data)
        problems += probs
        servers[name] = config
        server_dirs[name] = str(target)
    mcp_file = dest / CLAUDE_MCP_JSON
    if servers:
        mcp_file.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    eval_dir = ws / ".claude" / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (ws.parent / "coverity-triage-out")
    output_dir.mkdir(parents=True, exist_ok=True)
    claude_bin = args.claude_bin or ["claude"]
    agent_ref = f"{plugin}:{main}"
    # Default: no --tools, like a user's session. Opt-in: the union of the definitions' built-in tools.
    tools = builtin_tools(defs) if getattr(args, "tools_from_definitions", False) else None
    argv = plugin_argv(claude_bin, PROMPT_PLACEHOLDER, dest, agent_ref, plugin, sorted(servers), output_dir,
                       args.model, args.extra_arg or [], tools)
    main_row = next(r for r in agent_rows if r["name"] == main)
    denied = set(main_row["disallowed_tools"] or [])
    # A main agent without a tools line takes the session's tools: with --tools that is the base
    # set; without it, whatever Claude Code offers by default (nothing is expected then).
    wanted = defs[main].get("tools", BASE_TOOLS if tools else [])
    expect = {"main": agent_ref, "tools": [t for t in wanted if t not in denied],
              "skills": sorted(copied_skills), "agents": [r["ref"] for r in agent_rows],
              "mcp_servers": [f"plugin:{plugin}:{n}" for n in sorted(servers)]}
    command_file = eval_dir / "command.json"
    command_file.write_text(json.dumps(
        {"format": FORMAT_PLUGIN, "cwd": str(ws), "argv": argv, "prompt_index": argv.index(PROMPT_PLACEHOLDER),
         "expect": expect}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shown = [args.prompt if (a == PROMPT_PLACEHOLDER and args.prompt) else a for a in argv]
    result = {
        "ok": True,
        "format": FORMAT_PLUGIN,
        "workspace": str(ws),
        "plugin_root": str(root),
        "plugin_name": plugin,
        "plugin_dir": str(dest),
        "tools": tools,
        "main_agent": main,
        "agent_ref": agent_ref,
        "agents": agent_rows,
        "skills": copied_skills,
        "mcp_servers": server_dirs,
        "missing": missing,
        "problems": problems,
        "files": {"manifest": str(dest / CLAUDE_MANIFEST_REL), "mcp_config": str(mcp_file) if servers else None,
                  "command": str(command_file)},
        "cwd": str(ws),
        "command": shown,
        "command_line_bash": bash_line(shown),
        "command_line_powershell": powershell_line(shown),
    }
    if args.run:
        if not args.prompt:
            raise AdapterError("--run には --prompt が要ります")
        result["run"] = run_saved(ws, args.prompt, args.timeout, inspect=args.inspect)
        result["ok"] = result["run"]["ok"]
    return result


def run_saved(ws: Path, prompt: str, timeout: float, claude_bin: list[str] | None = None,
              inspect: bool = False) -> dict:
    command_file = Path(ws) / ".claude" / "eval" / "command.json"
    if not command_file.is_file():
        raise AdapterError(f"command.json がありません。先に build を動かしてください: {command_file}")
    saved = json.loads(read_text(command_file))
    argv = list(saved["argv"])
    idx = saved["prompt_index"]
    argv[idx] = prompt
    if claude_bin:
        old_len = idx - 1  # elements before "-p"
        argv = list(claude_bin) + argv[old_len:]
    if inspect and "--output-format" not in argv:
        argv += ["--output-format", "stream-json", "--verbose"]
    exe = shutil.which(argv[0]) or argv[0]
    argv[0] = exe
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # lets claude start inside another Claude Code session
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    progress(f"run (timeout {timeout:g} s): {bash_line(argv)}")
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=saved["cwd"], env=env, capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        timed_out = False
        code = proc.returncode
        out, err = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = None
        out, err = exc.stdout or b"", exc.stderr or b""
    except OSError as exc:
        raise AdapterError(f"claude を起動できません: {exc}") from exc
    seconds = round(time.monotonic() - started, 1)

    def dec(b) -> str:
        if isinstance(b, str):
            return b
        return b.decode("utf-8", errors="replace")

    result = {"ok": (not timed_out) and code == 0, "exit_code": code, "timed_out": timed_out,
              "seconds": seconds, "stdout": dec(out), "stderr": dec(err)[-4000:], "argv": argv}
    if inspect:
        result.update(inspect_stream(dec(out), ws, argv[argv.index("--agent") + 1], saved.get("expect")))
        result["stdout"] = result["stdout"][-4000:]
    return result


def _stem(name) -> str:
    return str(name).split(":")[-1]


def inspect_stream(stdout: str, ws: Path, main: str, expect: dict | None = None) -> dict:
    """Read the init and result events of a stream-json run and check the main agent's setup.

    ``expect`` (claude-plugin format: tools, skills and MCP servers of the copied plugin) replaces
    agents.json. Then each expected server must appear in init.mcp_servers as connected; other
    servers (not the plugin's) are listed as ``extra_mcp_servers`` and not checked.
    Plugin names may carry the plugin name and ":" in front (compared without it).
    """
    init, final = None, None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init" and init is None:
            init = event
        elif event.get("type") == "result":
            final = event
    checks: list[str] = []
    summary: dict = {}
    if init is None:
        checks.append("init の知らせがありません（claude が始まらなかった）")
    else:
        tools = list(init.get("tools") or [])
        summary = {"tools": tools, "skills": init.get("skills") or [], "agents": init.get("agents") or [],
                   "mcp_servers": init.get("mcp_servers") or [], "model": init.get("model"),
                   "permission_mode": init.get("permissionMode")}
        agents_file = Path(ws) / ".claude" / "eval" / "agents.json"
        definition = {}
        if expect is not None:
            definition = {"tools": list(expect["tools"] if expect.get("tools") is not None else BASE_TOOLS),
                          "skills": list(expect.get("skills") or [])}
        elif agents_file.is_file():
            definition = json.loads(read_text(agents_file)).get(main, {})
        for tool in definition.get("tools", BASE_TOOLS):
            if tool in NOT_IN_PRINT_MODE:
                continue
            if tool in tools or TOOL_ALIASES.get(tool) in tools:
                continue
            if tool.startswith("mcp__"):
                continue  # checked through the server status below
            checks.append(f"ツールがありません: {tool}")
        expected_servers = list(expect.get("mcp_servers") or []) if expect is not None else None
        if expected_servers is None:
            for server in summary["mcp_servers"]:
                if server.get("status") != "connected":
                    checks.append(f"MCP サーバーにつながっていません: {server.get('name')}（{server.get('status')}）")
        else:
            # Only the plugin's own servers count; others (e.g. the user's) are listed apart.
            seen = [s for s in summary["mcp_servers"] if isinstance(s, dict)]
            matched: set[int] = set()
            for want in expected_servers:
                # the exact plugin name first; else another plugin-provided server of the same stem
                # (never a hand-registered server: that would hide a plugin that did not load)
                hits = [i for i, s in enumerate(seen) if s.get("name") == want] or \
                       [i for i, s in enumerate(seen) if str(s.get("name") or "").startswith("plugin:")
                        and _stem(s.get("name")) == _stem(want)]
                matched.update(hits)
                if not hits:
                    checks.append(f"MCP サーバーが読み込まれていません: {want}")
                elif not any(seen[i].get("status") == "connected" for i in hits):
                    states = "、".join(str(seen[i].get("status")) for i in hits)
                    checks.append(f"MCP サーバーにつながっていません: {want}（{states}）")
            summary["extra_mcp_servers"] = [s for i, s in enumerate(seen) if i not in matched]
        loaded_skills = {_stem(s) for s in summary["skills"]}
        for skill in definition.get("skills", []):
            if skill not in summary["skills"] and _stem(skill) not in loaded_skills:
                checks.append(f"スキルが読み込まれていません: {skill}")
        if main not in summary["agents"] and _stem(main) not in {_stem(a) for a in summary["agents"]}:
            checks.append(f"エージェントが読み込まれていません: {main}")
    out = {"init": summary, "checks": checks}
    if final is not None:
        out["result"] = final.get("result")
        out["cost_usd"] = final.get("total_cost_usd")
        out["num_turns"] = final.get("num_turns")
    return out


def collect_command(args: argparse.Namespace) -> dict:
    agent_file = Path(args.agent_file)
    fmt = _format_of(args)
    root = Path(args.plugin_root).resolve() if args.plugin_root else guess_plugin_root(agent_file)
    layout = Layout(root, flavor="claude" if fmt == FORMAT_PLUGIN else "copilot")
    collected = collect_parts(agent_file, layout)
    return {
        "ok": True,
        "format": fmt,
        "plugin_root": str(root),
        "agents": [{"name": a["name"], "path": str(a["path"]), "table": a["table"]} for a in collected["agents"]],
        "skills": {k: (str(v) if v else None) for k, v in collected["skills"].items()},
        "mcp_servers": {k: (str(v) if v else None) for k, v in collected["mcp"].items()},
        "missing": collected["missing"],
    }


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="プラグインの部品から Claude Code の試験の作業フォルダを作る")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="エージェントと、その表の部品を（たどって）挙げる。何も書かない")
    c.add_argument("agent_file")
    c.add_argument("--plugin-root")
    c.add_argument("--format", choices=FORMATS, default=FORMAT_CONVERTED,
                   help="copilot-converted（Copilot CLI の定義）か claude-plugin（Claude Code の定義 agents/<名前>.md）")

    b = sub.add_parser("build", help="作業フォルダを作り、claude -p のコマンドを示す")
    b.add_argument("--workspace", required=True)
    b.add_argument("--format", choices=FORMATS, default=FORMAT_CONVERTED,
                   help="copilot-converted（Copilot CLI の定義を置き換える。既定）か claude-plugin（Claude Code の定義を"
                        "一時のプラグインに写し、--plugin-dir で動かす）")
    b.add_argument("--plugin-dir", help="claude-plugin で、プラグインを作るフォルダ（既定は作業フォルダの隣の claude-plugin）")
    b.add_argument("--tools-from-definitions", action="store_true",
                   help="claude-plugin で、写した定義の組み込みのツールを合わせた --tools を付ける"
                        "（既定は付けない。利用者と同じ Claude Code の既定のツール）")
    b.add_argument("--parts-from-table", help="このエージェントと、その表の部品をたどって集める")
    b.add_argument("--agent", action="append", help="置き換える（写す）エージェントの定義（何度でも）")
    b.add_argument("--skill", action="append", help="足すスキルのフォルダ（何度でも）")
    b.add_argument("--mcp", action="append", help="足す MCP サーバーのフォルダ（[名前=]フォルダ）")
    b.add_argument("--plugin-root")
    b.add_argument("--main", help="--agent で動かすエージェント（既定は最初のもの）")
    b.add_argument("--output-dir", help="--add-dir に渡すフォルダ（既定は作業フォルダの隣の coverity-triage-out）")
    b.add_argument("--fake-data", help="偽の Coverity の警告のファイル（MCP サーバーを偽の Coverity で動かす）")
    b.add_argument("--mcp-env", action="append", help="MCP サーバーの環境変数 KEY=VALUE（何度でも）")
    b.add_argument("--model", help="claude -p の --model")
    b.add_argument("--agent-model", help="エージェントの model を、これに替える（inherit など。claude-plugin では写しの model の行）")
    b.add_argument("--main-model", help="claude-plugin で、メインのエージェントの写しの model の行だけを、これに替える"
                                        "（--agent-model より先）")
    b.add_argument("--prompt", help="示すコマンドに入れる頼みの文")
    b.add_argument("--extra-arg", action="append", help="claude に足す引数（何度でも）")
    b.add_argument("--claude-bin", nargs="+", help="claude の代わりに動かすコマンド")
    b.add_argument("--allow-missing", action="store_true", help="表の部品がなくても作る")
    b.add_argument("--run", action="store_true", help="作った後に、そのコマンドを動かす")
    b.add_argument("--inspect", action="store_true",
                   help="動かすときに stream-json で始まりの知らせを読み、ツール・スキル・MCP を確かめる")
    b.add_argument("--timeout", type=float, default=120.0)

    r = sub.add_parser("run", help="build で作ったコマンドを、頼みの文を入れて動かす")
    r.add_argument("--workspace", required=True)
    r.add_argument("--prompt", required=True)
    r.add_argument("--timeout", type=float, default=120.0)
    r.add_argument("--claude-bin", nargs="+", help="claude の代わりに動かすコマンド")
    r.add_argument("--inspect", action="store_true",
                   help="stream-json で始まりの知らせを読み、ツール・スキル・MCP を確かめる")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        emit({"ok": False, "error": "引数が正しくありません（--help で使い方を見られます）", "problems": []})
        return 1
    try:
        if args.command == "collect":
            result = collect_command(args)
        elif args.command == "build":
            result = build(args)
        else:
            result = run_saved(Path(args.workspace).resolve(), args.prompt, args.timeout, args.claude_bin,
                               inspect=args.inspect)
    except AdapterError as exc:
        emit({"ok": False, "error": str(exc), "problems": exc.problems})
        return 1
    except Exception as exc:  # noqa: BLE001 - never print a traceback
        emit({"ok": False, "error": f"思わぬエラーが起きました: {type(exc).__name__}: {exc}", "problems": []})
        return 1
    emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
