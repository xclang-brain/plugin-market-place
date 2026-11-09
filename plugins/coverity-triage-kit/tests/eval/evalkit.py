#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Shared helpers of the EVAL harnesses (design 9.2, 9.3). Standard library only.

The harnesses (run_mcp_eval.py, stage2/, stage3/, stage4/) use this module for:

- part names by role, read from the "## 今の部品" table of docs/replace.md (no part name is
  written in the harnesses; contract names such as the extension tool name are fine);
- the result folder (outside the plugin folder; default $TMPDIR/<plugin>-evals/<stage>/<time>);
- running ``claude -p`` with ``--output-format stream-json --verbose``: the transcript is saved
  line by line, the process tree is killed on timeout, and ``CLAUDECODE`` is dropped so it also
  starts inside another Claude Code session;
- reading the transcript: tools, skills and agents used, tool errors, the final text, cost;
- small git, uv and MCP (stdio JSON-RPC) helpers, an optional LLM judge, and Markdown tables.

Nothing here prints to stdout: the harnesses print one JSON object at the end.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE
REPLACE_DOC = Path("docs") / "replace.md"
CURRENT_PARTS_HEADING = "今の部品"
CURRENT_PARTS_HEADER = ["役割", "段階", "部品", "置き場所"]
AGENT_SUFFIX = ".agent.md"


def load_module(name: str, path: Path):
    """Import a file by path without writing bytecode into the repository."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = load_module("claude_adapter_for_eval", HERE / "claude_adapter.py")
COPY_IGNORE = ADAPTER.COPY_IGNORE
emit = ADAPTER.emit


class EvalError(Exception):
    """A problem the caller can fix (missing part, bad option, tool not found)."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


VERBOSE = True  # tests switch this off


def progress(tag: str, message: str) -> None:
    if not VERBOSE:
        return
    line = f"[{tag}] {message}"
    try:
        print(line, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- parts by role


def current_parts(root: Path = PLUGIN_ROOT) -> list[dict]:
    """Rows of the "## 今の部品" table of docs/replace.md: [{role, stage, name, place}]."""
    doc = Path(root) / REPLACE_DOC
    if not doc.is_file():
        return []
    lines = doc.read_text(encoding="utf-8-sig").replace("\r\n", "\n").split("\n")
    start = next((i + 1 for i, line in enumerate(lines)
                  if re.match(r"^##\s+" + re.escape(CURRENT_PARTS_HEADING) + r"\s*$", line)), None)
    if start is None:
        return []
    table: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        if line.strip().startswith("|"):
            table.append(line)
        elif table:
            break
    if len(table) < 3 or ADAPTER._row_cells(table[0]) != CURRENT_PARTS_HEADER:
        return []
    rows = []
    for line in table[2:]:
        cells = ADAPTER._row_cells(line)
        if len(cells) != 4:
            continue
        rows.append({"role": cells[0], "stage": cells[1], "name": cells[2].strip("`"),
                     "place": cells[3].strip("`").rstrip("/")})
    return rows


def part(role: str, root: Path = PLUGIN_ROOT) -> dict:
    """The current part of a role: {role, name, path}. Raises EvalError when it is not there."""
    rows = current_parts(root)
    if not rows:
        raise EvalError(f"部品の表がありません: {Path(root) / REPLACE_DOC} の「## {CURRENT_PARTS_HEADING}」",
                        ["プラグインのフォルダで動かすか、--plugin-root で渡します"])
    for row in rows:
        if row["role"] == role:
            path = (Path(root) / row["place"]).resolve()
            if not path.exists():
                raise EvalError(f"役割「{role}」の部品 {row['name']} がありません: {path}")
            return {"role": role, "name": row["name"], "path": path}
    raise EvalError(f"役割「{role}」の部品が、部品の表にありません",
                    [f"表にある役割: {'、'.join(r['role'] for r in rows)}"])


# --------------------------------------------------------------------------- files and folders


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def default_out(stage: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{PLUGIN_ROOT.name}-evals" / stage / stamp()


def inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def prepare_out(arg: str | None, stage: str, root: Path = PLUGIN_ROOT) -> Path:
    out = Path(arg).expanduser().resolve() if arg else default_out(stage)
    if inside(out, root):
        raise EvalError(f"結果のフォルダがプラグインのフォルダの中です。外の場所にしてください: {out}")
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError:  # Windows: the target may be open for a moment
            if attempt == 9:
                raise
            time.sleep(0.2)
    return path


def write_json(path: Path, data: Any) -> Path:
    return write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def load_yaml(path: Path) -> Any:
    """YAML through PyYAML (the harnesses that need it are run with ``uv run``)."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise EvalError("PyYAML がありません。このスクリプトは uv run --native-tls --quiet で動かします") from exc
    return yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))


def copy_tree(src: Path, dst: Path) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=COPY_IGNORE)
    return dst


def normalize(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


# --------------------------------------------------------------------------- processes


def claude_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # lets claude start inside another Claude Code session
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def resolve_exe(argv: list[str]) -> list[str]:
    argv = list(argv)
    if argv:
        argv[0] = shutil.which(argv[0]) or argv[0]
    return argv


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def run_command(argv: list[str], cwd: Path | None = None, env: dict | None = None, timeout: float = 300,
                input_text: str | None = None) -> dict:
    """Run a short command; never raises for a failing command."""
    started = time.monotonic()
    try:
        proc = subprocess.run(resolve_exe(argv), cwd=str(cwd) if cwd else None, env=env, capture_output=True,
                              timeout=timeout, input=(input_text.encode("utf-8") if input_text is not None else None),
                              stdin=None if input_text is not None else subprocess.DEVNULL)
        code, out, err, timed_out = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        code, out, err, timed_out = None, exc.stdout or b"", exc.stderr or b"", True
    except OSError as exc:
        return {"code": None, "stdout": "", "stderr": str(exc), "timed_out": False, "seconds": 0.0, "missing": True}
    dec = (lambda b: b.decode("utf-8", errors="replace") if isinstance(b, bytes) else (b or ""))
    return {"code": code, "stdout": dec(out), "stderr": dec(err), "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 1)}


def run_json(argv: list[str], **kw) -> tuple[dict | None, dict]:
    """Run a command that prints one JSON object (the scripts of the parts).

    The scripts write in the console code page on Windows when stdout is a pipe, unless
    PYTHONIOENCODING is set (design 3.1), so they get PYTHONIOENCODING=utf-8 here.
    """
    if kw.get("env") is None:
        kw["env"] = claude_env({"PYTHONIOENCODING": "utf-8"})
    res = run_command(argv, **kw)
    data = None
    text = res["stdout"].strip()
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            start = text.find("{")
            if start >= 0:
                try:
                    data = json.loads(text[start:])
                except ValueError:
                    data = None
    return (data if isinstance(data, dict) else None), res


def uv_script(script: Path, *args: str) -> list[str]:
    return ["uv", "run", "--native-tls", "--quiet", str(script), *args]


def run_claude(argv: list[str], cwd: Path, timeout: float, transcript: Path, env: dict | None = None) -> dict:
    """Run claude -p (stream-json). Saves every stdout line to ``transcript`` as it comes."""
    transcript = Path(transcript)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    stderr_file = transcript.with_suffix(".stderr.txt")
    argv = resolve_exe(argv)
    progress("claude", f"cwd={cwd} timeout={timeout:g}s: {ADAPTER.bash_line(argv)[:400]}")
    started = time.monotonic()
    events: list[dict] = []
    with open(transcript, "w", encoding="utf-8", newline="\n") as tout, open(stderr_file, "wb") as terr:
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env if env is not None else claude_env(),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=terr, **_popen_kwargs())
        except OSError as exc:
            raise EvalError(f"claude を起動できません: {exc}", ["claude（Claude Code）を入れて PATH を通します"]) from exc

        def pump() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                tout.write(line + "\n")
                tout.flush()
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()
    seconds = round(time.monotonic() - started, 1)
    stderr_tail = stderr_file.read_text(encoding="utf-8", errors="replace")[-3000:] if stderr_file.is_file() else ""
    return {"exit_code": proc.returncode, "timed_out": timed_out, "seconds": seconds, "events": events,
            "transcript": str(transcript), "stderr_tail": stderr_tail, "argv": argv}


# --------------------------------------------------------------------------- stream-json


def read_events(path: Path) -> list[dict]:
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                out.append(event)
    return out


def _blocks(event: dict) -> list:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_block_text(c.get("text") if isinstance(c, dict) else c) for c in content)
    return "" if content is None else str(content)


SKILL_DIR_RE = re.compile(r"Base directory for this skill:\s*(\S+)")
COMMAND_NAME_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
SKILL_PATH_RE = re.compile(r"[/\\]\.claude[/\\]skills[/\\]([A-Za-z0-9_.-]+)[/\\]")


def skill_of_tool_input(data: dict) -> str | None:
    for key in ("skill", "command", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lstrip("/").split()[0]
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def skill_root_patterns(skill_roots: list[Path] | None) -> list[re.Pattern]:
    """Patterns for "<root>/<name>/" in a tool input, for each skills folder (either slash)."""
    out = []
    for root in skill_roots or []:
        text = str(root).replace("\\", "/").rstrip("/")
        if text:
            out.append(re.compile(re.escape(text).replace("/", r"[/\\]") + r"[/\\]([A-Za-z0-9_.-]+)[/\\]"))
    return out


def summarize(events: list[dict], prompt: str | None = None, skill_roots: list[Path] | None = None) -> dict:
    """What a stream-json run did: init, tool calls (main and in subagents), skills, agents, result.

    A skill counts as loaded when the Skill tool was called with it, when a tool input points into
    its folder (.claude/skills/<name>/, or <root>/<name>/ for each of ``skill_roots``, such as the
    skills folder of a copied plugin), or when the prompt starts with /<name> and <name> is one of
    the init event's slash commands (claude -p does not show the expansion of a slash command).
    Names with the plugin name and ":" in front count without it.
    """
    root_patterns = skill_root_patterns(skill_roots)
    init: dict = {}
    final: dict = {}
    tool_uses: list[dict] = []
    seen: set[str] = set()
    results: dict[str, dict] = {}
    texts: list[str] = []
    skills_loaded: list[str] = []
    for event in events:
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init" and not init:
            init = event
        elif etype == "result":
            final = event
        elif etype == "assistant":
            parent = event.get("parent_tool_use_id")
            for block in _blocks(event):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tid = str(block.get("id") or len(tool_uses))
                    if tid in seen:
                        continue
                    seen.add(tid)
                    tool_uses.append({"id": tid, "name": str(block.get("name")),
                                      "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                                      "parent": parent})
                elif block.get("type") == "text" and parent is None and block.get("text"):
                    texts.append(str(block["text"]))
        elif etype == "user":
            content = _blocks(event)
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                content = [{"type": "text", "text": message["content"]}]
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    results[str(block.get("tool_use_id"))] = {"is_error": bool(block.get("is_error")),
                                                              "text": _block_text(block.get("content"))[:2000]}
                text = _block_text(block.get("text")) if block.get("type") == "text" else ""
                for m in COMMAND_NAME_RE.finditer(text):
                    skills_loaded.append(m.group(1).split(":")[-1])
                for m in SKILL_DIR_RE.finditer(text):
                    skills_loaded.append(Path(m.group(1).rstrip("/\\")).name)
    if prompt and prompt.strip().startswith("/") and init:
        word = prompt.strip()[1:].split()[0] if prompt.strip()[1:] else ""
        if word and word in (init.get("slash_commands") or []):
            skills_loaded.insert(0, word.split(":")[-1])
    for use in tool_uses:
        res = results.get(use["id"])
        use["is_error"] = bool(res and res["is_error"])
        use["result"] = res["text"] if res else None
        if use["name"] == "Skill":
            name = skill_of_tool_input(use["input"])
            if name:
                skills_loaded.append(name.split(":")[-1])
        for value in _strings(use["input"]):
            skills_loaded += [m.group(1) for m in SKILL_PATH_RE.finditer(value)]
            for pattern in root_patterns:
                skills_loaded += [m.group(1) for m in pattern.finditer(value)]
    main = [u for u in tool_uses if u["parent"] is None]
    counts: dict[str, int] = {}
    for u in main:
        counts[u["name"]] = counts.get(u["name"], 0) + 1
    agents = [str(u["input"].get("subagent_type") or u["input"].get("agent") or "")
              for u in tool_uses if u["name"] in ("Task", "Agent")]
    unique_skills: list[str] = []
    for s in skills_loaded:
        if s and s not in unique_skills:
            unique_skills.append(s)
    return {
        "init": {"tools": init.get("tools") or [], "skills": init.get("skills") or [],
                 "agents": init.get("agents") or [], "mcp_servers": init.get("mcp_servers") or [],
                 "model": init.get("model"), "permission_mode": init.get("permissionMode"),
                 "slash_commands": init.get("slash_commands") or []} if init else {},
        "has_result": bool(final),
        "result_text": final.get("result") if isinstance(final.get("result"), str) else None,
        "is_error": bool(final.get("is_error")) if final else None,
        "subtype": final.get("subtype"),
        "cost_usd": final.get("total_cost_usd"),
        "num_turns": final.get("num_turns"),
        "duration_ms": final.get("duration_ms"),
        "usage": final.get("usage") or {},
        "permission_denials": final.get("permission_denials") or [],
        "tool_uses": tool_uses,
        "main_tool_counts": counts,
        "main_tool_calls": len(main),
        "tool_errors": [{"name": u["name"], "text": (u["result"] or "")[:500]} for u in tool_uses if u["is_error"]],
        "skills_loaded": unique_skills,
        "agents_called": agents,
        "texts": texts,
    }


def final_text(summary: dict) -> str:
    if summary.get("result_text"):
        return summary["result_text"]
    return "\n".join(summary.get("texts") or [])


def tag_content(text: str | None, tag: str) -> str | None:
    """The last <tag>...</tag> of a text (same rule as mcp-builder's evaluation.py)."""
    matches = re.findall(rf"<{tag}>(.*?)</{tag}>", text or "", re.DOTALL)
    return matches[-1].strip() if matches else None


def mcp_tool_name(server: str, tool: str) -> str:
    """How Claude Code names an MCP tool (Copilot CLI shows extension tools without a prefix)."""
    return f"mcp__{server}__{tool}"


# --------------------------------------------------------------------------- git


def git(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    res = run_command(["git", "-c", "core.longpaths=true", "-C", str(repo), *args], timeout=timeout)
    if check and res["code"] != 0:
        raise EvalError(f"git {' '.join(args)} が失敗しました: {res['stderr'].strip()[:500]}")
    return res["stdout"]


def make_repo(src: Path, dst: Path, message: str = "見本") -> str:
    """Copy a folder and make it a git repository on branch main with one commit. Returns HEAD."""
    copy_tree(src, dst)
    git(dst, "init", "-q")
    git(dst, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, value in (("user.name", "eval"), ("user.email", "eval@localhost"), ("core.autocrlf", "false"),
                       ("core.longpaths", "true"), ("commit.gpgsign", "false")):
        git(dst, "config", key, value)
    git(dst, "add", "-A")
    git(dst, "commit", "-q", "-m", message)
    return git(dst, "rev-parse", "HEAD").strip()


def exclude_in_git(repo: Path, pattern: str) -> None:
    """Hide a path from git status without touching tracked files (.git/info/exclude)."""
    info = Path(git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    if not info.is_absolute():
        info = Path(repo) / info
    info.parent.mkdir(parents=True, exist_ok=True)
    old = info.read_text(encoding="utf-8") if info.is_file() else ""
    if pattern not in old.splitlines():
        info.write_text(old + ("" if old.endswith("\n") or not old else "\n") + pattern + "\n", encoding="utf-8")


def working_changes(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain", "--untracked-files=all", check=False)
    return [line for line in out.splitlines() if line.strip()]


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD", check=False).strip()


# --------------------------------------------------------------------------- uv and MCP


def uv_sync(folder: Path, env: dict | None = None, timeout: float = 600) -> dict:
    res = run_command(["uv", "sync", "--native-tls", "--frozen", "--quiet", "--directory", str(folder)],
                      env=env, timeout=timeout)
    if res.get("missing"):
        raise EvalError("uv がありません", ["uv を入れて PATH を通します"])
    return res


def mcp_list_tools(config: dict, cwd: Path | None = None, timeout: float = 120) -> dict:
    """Start a stdio MCP server, initialize it and list its tools (JSON-RPC, one message per line)."""
    argv = resolve_exe([str(config.get("command"))] + [str(a) for a in config.get("args") or []])
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (config.get("env") or {}).items()})
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_popen_kwargs())
    except OSError as exc:
        return {"ok": False, "error": f"MCP サーバーを起動できません: {exc}", "tools": []}
    lines: queue.Queue = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def wait_for(msg_id: int) -> dict | None:
        while time.monotonic() < deadline:
            try:
                raw = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if raw is None:
                return None
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == msg_id:
                return msg
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "eval-preflight", "version": "1"}}})
        init = wait_for(1)
        if not init or "result" not in init:
            err = proc.stderr.read(2000).decode("utf-8", "replace") if proc.poll() is not None and proc.stderr else ""
            return {"ok": False, "error": f"initialize に答えがありません {err}".strip(), "tools": []}
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = wait_for(2)
        if not listed or "result" not in listed:
            return {"ok": False, "error": "tools/list に答えがありません", "tools": []}
        tools = [{"name": t.get("name"), "annotations": t.get("annotations") or {}}
                 for t in listed["result"].get("tools") or [] if isinstance(t, dict)]
        return {"ok": True, "server_info": init["result"].get("serverInfo"), "tools": tools}
    except OSError as exc:
        return {"ok": False, "error": f"MCP サーバーとやりとりできません: {exc}", "tools": []}
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- LLM judge


def parse_json_object(text: str | None) -> dict | None:
    """The first JSON object in a text (also inside a ```json fence)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:text.rfind("}") + 1])
    for c in candidates:
        try:
            value = json.loads(c)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def llm_judge(prompt: str, model: str, claude_bin: list[str], cwd: Path, transcript: Path,
              timeout: float = 300) -> dict:
    """Ask a model (no tools) to grade; it must answer with one JSON object."""
    argv = list(claude_bin) + ["-p", prompt, "--tools", "", "--strict-mcp-config", "--setting-sources", "project",
                               "--model", model, "--max-turns", "2", "--no-session-persistence",
                               "--output-format", "stream-json", "--verbose"]
    run = run_claude(argv, cwd, timeout, transcript)
    summary = summarize(run["events"])
    grade = parse_json_object(final_text(summary))
    return {"ok": grade is not None, "grade": grade, "model": model, "cost_usd": summary.get("cost_usd"),
            "seconds": run["seconds"], "transcript": run["transcript"],
            **({} if grade is not None else {"error": "採点の答えが JSON ではありません", "text": final_text(summary)[:1000]})}


# --------------------------------------------------------------------------- Markdown


def md_cell(value: Any, limit: int = 120) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", "").replace("\n", " ").replace("|", "\\|").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    out += ["| " + " | ".join(md_cell(c) for c in row) + " |" for row in rows]
    return out


def mark(ok: bool | None) -> str:
    return "○" if ok is True else ("×" if ok is False else "－")


def money(value: Any) -> str:
    return f"${value:.4f}" if isinstance(value, (int, float)) else "－"


def check(check_id: str, ok: bool | None, detail: str = "", level: str = "must") -> dict:
    """One programmatic check. ok None = could not be checked (skipped). level: must | should."""
    return {"id": check_id, "ok": ok, "level": level, "detail": detail}


def verdict_of(checks: list[dict]) -> bool:
    """Passed when no "must" check failed (skipped checks do not fail a case)."""
    return not any(c["ok"] is False and c["level"] == "must" for c in checks)
