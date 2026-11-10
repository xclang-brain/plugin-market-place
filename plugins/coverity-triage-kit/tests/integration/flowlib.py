"""Shared harness of the plugin-level flow tests (no model is used).

- a tiny C repository with five Coverity-like warnings (one group of two, one pair with one cause, one
  deviation) and what is true about each of them;
- the batch run's commands (the scripts named in tests/structure/plugin_parts.py);
- scripted fake agents that do what the instruction files (briefs) ask: the worker makes a real, minimal
  fix in its work copy and writes result.json, the reviewer writes review.json, the integrator writes
  integration/plan.json;
- a small stdio client of the MCP server started with the plugin's mcp.json command;
- the tick loop of the orchestrator (fetch actions to the MCP server or to offline files, launch actions
  to the fake agents).

Used by tests/integration/test_flow.py, tests/independence (the briefs name no part) and tests/ui.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
PLUGIN = TESTS.parent
sys.dont_write_bytecode = True
if str(TESTS / "structure") not in sys.path:
    sys.path.insert(0, str(TESTS / "structure"))
import plugin_parts as pp  # noqa: E402

UV = shutil.which("uv")
GIT = shutil.which("git")
STREAM = "app-main"
PROJECT = "app"
BUILD_PREFIX = "/build/app/"   # the path the analysis saw; config.yaml strips it

# ---------------------------------------------------------------------------
# the dataset
# ---------------------------------------------------------------------------

SOURCES = {
    "src/config.c": """#include <stdio.h>
#include <stdlib.h>
#include <string.h>

char *read_config(const char *path)
{
    FILE *fp = fopen(path, "r");
    char *buf = malloc(128);
    fgets(buf, 128, fp);
    strcat(buf, "\\n");
    fclose(fp);
    return buf;
}
""",
    "src/uart.c": """int hal_write(int port, int value);

int uart_init(int port)
{
    hal_write(port, 1);
    return 0;
}
""",
    "src/log.c": """#include <stdio.h>

int open_log(const char *name)
{
    FILE *fp = fopen(name, "a");
    if (fp == NULL) {
        return -1;
    }
    fputs("start\\n", fp);
    return 0;
}
""",
    "Makefile": "all:\n\tcc -c src/config.c src/uart.c src/log.c\n",
}

# One fix per cause (applied in the worker's work copy; the same text change for every warning of the cause).
EDITS = {
    "config": ("src/config.c",
               '    FILE *fp = fopen(path, "r");\n    char *buf = malloc(128);\n',
               '    FILE *fp = fopen(path, "r");\n    if (fp == NULL) {\n        return NULL;\n    }\n'
               '    char *buf = malloc(128);\n    if (buf == NULL) {\n        fclose(fp);\n        return NULL;\n    }\n'),
    "uart": ("src/uart.c", "    hal_write(port, 1);\n", "    if (hal_write(port, 1) < 0) {\n        return -1;\n    }\n"),
    "log": ("src/log.c", '    fputs("start\\n", fp);\n    return 0;\n',
            '    int failed = fputs("start\\n", fp) == EOF;\n    fclose(fp);\n    return failed ? -1 : 0;\n'),
}

# The warnings (key -> what the analysis found) and the truth about each (what a good worker concludes).
WARNINGS = [
    {"key": "A", "checker": "NULL_RETURNS", "function": "read_config", "file": "src/config.c", "line": 9, "impact": "High",
     "cwe": "476", "category": "Null pointer dereferences",
     "events": [(7, "returned_null", "fopen は NULL を返すことがある", False),
                (9, "dereference", "fp を NULL かを確かめずに fgets に渡す", True)]},
    {"key": "B", "checker": "NULL_RETURNS", "function": "read_config", "file": "src/config.c", "line": 10, "impact": "High",
     "cwe": "476", "category": "Null pointer dereferences",
     "events": [(8, "returned_null", "malloc は NULL を返すことがある", False),
                (10, "dereference", "buf を NULL かを確かめずに strcat に渡す", True)]},
    {"key": "C", "checker": "CHECKED_RETURN", "function": "uart_init", "file": "src/uart.c", "line": 5, "impact": "Medium",
     "cwe": "252", "category": "Error handling issues",
     "events": [(5, "check_return", "hal_write の戻り値を確かめていない", True)]},
    {"key": "D", "checker": "RESOURCE_LEAK", "function": "open_log", "file": "src/log.c", "line": 10, "impact": "High",
     "cwe": "404", "category": "Resource leaks",
     "events": [(5, "alloc_fn", "fopen がファイルを開く", False), (10, "leaked_storage", "fp を閉じずに戻る", True)]},
    {"key": "E", "checker": "CHECKED_RETURN", "function": "open_log", "file": "src/log.c", "line": 9, "impact": "Medium",
     "cwe": "252", "category": "Error handling issues",
     "events": [(9, "check_return", "fputs の戻り値を確かめていない", True)]},
]
TRUTH = {
    "A": {"cause": "config", "judgement": "true_bug", "summary": "fopen と malloc の戻り値を確かめずに使っている",
          "rationale": "read_config は fopen と malloc の戻り値を確かめずに fgets と strcat に渡す。呼び出し元も確かめていない。",
          "evidence": [("src/config.c", 7, "fopen の戻り値"), ("src/config.c", 9, "fp をそのまま使う")]},
    "B": {"cause": "config", "judgement": "true_bug", "summary": "fopen と malloc の戻り値を確かめずに使っている",
          "rationale": "read_config は malloc の戻り値を確かめずに strcat に渡す。",
          "evidence": [("src/config.c", 8, "malloc の戻り値"), ("src/config.c", 10, "buf をそのまま使う")]},
    "C": {"cause": "uart", "judgement": "intentional", "summary": "hal_write は失敗を返さない",
          "rationale": "hal_write の戻り値は書いた数で、失敗の値を返さない。宣言と使い方を読んだ。",
          "evidence": [("src/uart.c", 1, "hal_write の宣言"), ("src/uart.c", 5, "戻り値を使わない")],
          "comment": "意図的。hal_write の戻り値は書いた数で失敗の値を返さないため、使わずに進めて問題はない（src/uart.c:5）。"},
    "D": {"cause": "log", "judgement": "true_bug", "summary": "open_log が fp を閉じずに戻り、書き込みの失敗も見ない",
          "rationale": "open_log は fopen で開いた fp を閉じずに 0 を返す。fputs の失敗も見ていない。",
          "evidence": [("src/log.c", 5, "fopen で開く"), ("src/log.c", 10, "閉じずに戻る")]},
    "E": {"cause": "log", "judgement": "true_bug", "summary": "open_log が fp を閉じずに戻り、書き込みの失敗も見ない",
          "rationale": "open_log は fputs の戻り値を見ずに成功を返し、fp も閉じない。",
          "evidence": [("src/log.c", 9, "fputs の戻り値"), ("src/log.c", 10, "閉じずに戻る")]},
}
SEVERITY_OF_IMPACT = {"High": "Major", "Medium": "Moderate", "Low": "Minor"}
ORDER = {"high": 3, "medium": 2, "low": 1}


def write_repo(repo: Path, env: dict[str, str]) -> str:
    """The person's repository (git, branch main) with the sources; returns the commit."""
    for rel, text in SOURCES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "init", "-q", "-b", "main", env=env)
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", "init", env=env)
    return git(repo, "rev-parse", "HEAD", env=env)


def write_settings(repo: Path, out: Path, url: str, *, extra_coverity: str = "", review: bool = True,
                   integration: bool = True, parallel: int = 3, max_rework: int = 1, env: dict[str, str]) -> None:
    """.coverity-triage/ (config.yaml, the filter, knowledge.md), committed like a team would."""
    folder = repo / ".coverity-triage"
    (folder / "filters").mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(
        "# チームの設定\n"
        "coverity:\n"
        f"  url: {url}\n"
        f"{extra_coverity}"
        "  path_strip_prefixes:\n"
        f"    - {BUILD_PREFIX}\n"
        "vcs:\n  type: git\n  base_branch: main\n"
        f"output_dir: {json.dumps(out.as_posix())}\n"
        f"parallel: {parallel}\n"
        "verify:\n  default: none\n  initial: false\n"
        f"agents:\n  review: {str(review).lower()}\n  integration: {str(integration).lower()}\n  max_rework: {max_rework}\n",
        encoding="utf-8", newline="\n")
    (folder / "filters" / "untriaged.yaml").write_text(
        f"name: 分類がまだの警告\nproject: {PROJECT}\nstreams: [{STREAM}]\ntriage:\n  classification: [Unclassified]\nlimit: 20\n",
        encoding="utf-8", newline="\n")
    (folder / "knowledge.md").write_text(
        "# プロジェクトの知識\n\n## 調査で使う事実\n\n- hal_write は書いた数を返し、失敗を返さない。\n\n"
        "## 推奨の方針（修正か逸脱か）\n\n## 逸脱コメントの書き方\n", encoding="utf-8", newline="\n")
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", "settings", env=env)


def emulator_tree(warning: dict[str, Any]) -> dict[str, Any]:
    """One warning as the analysis commits it to the emulator (the emulator gives it a CID)."""
    path = BUILD_PREFIX + warning["file"]
    events = [{"eventDescription": desc, "eventTag": tag, "eventNumber": n, "eventTreePosition": str(n), "main": main,
               "filePathname": path, "lineNumber": line, "events": None}
              for n, (line, tag, desc, main) in enumerate(warning["events"], 1)]
    checker = warning["checker"]
    return {"checkerName": checker, "subcategory": "none", "type": checker.lower(), "subtype": "none",
            "extra": warning["key"], "functionDisplayName": warning["function"], "functionMangledName": warning["function"],
            "mainEventFilePathname": path, "strippedMainEventFilePathname": path, "mainEventLineNumber": warning["line"],
            "domain": "STATIC_C", "language": "C", "events": events,
            "checkerProperties": {"category": warning["category"], "categoryDescription": warning["category"],
                                  "cweCategory": warning["cwe"], "issueKinds": ["QUALITY"], "eventSetCaptions": [],
                                  "impact": warning["impact"], "impactDescription": warning["impact"],
                                  "subcategoryLocalEffect": "The program may crash.",
                                  "subcategoryShortDescription": f"{checker} short",
                                  "subcategoryLongDescription": f"{checker} の説明"}}


def offline_files(folder: Path, cids: dict[str, int], revision: str) -> Path:
    """issues.json and issue-<CID>.json in the shape the MCP server writes (design 4.3, 4.4), without a server."""
    folder.mkdir(parents=True, exist_ok=True)
    now = "2026-11-08T10:00:00+09:00"
    issues = []
    for w in WARNINGS:
        issue = {"cid": cids[w["key"]], "checker": w["checker"], "file": BUILD_PREFIX + w["file"], "function": w["function"],
                 "line": w["line"], "impact": w["impact"], "category": w["category"], "cwe": int(w["cwe"]),
                 "merge_key": f"mk-{w['key']}", "stream": STREAM, "classification": "Unclassified", "action": "Undecided",
                 "severity": "Unspecified", "status": "New"}
        issues.append(issue)
        events = [{"file": BUILD_PREFIX + w["file"], "line": line, "tag": tag, "description": desc, "main": main}
                  for line, tag, desc, main in w["events"]]
        (folder / f"issue-{issue['cid']}.json").write_text(json.dumps(
            {"fetched_at": now, "issue": issue, "events": events, "checker_description": f"{w['checker']} の説明"},
            ensure_ascii=False), encoding="utf-8")
    data = {"filter_sha": "offline", "stream": STREAM, "limit": 20, "started_at": now, "finished_at": now,
            "coverity_total": len(issues), "next_offset": 0, "limited": False, "revision_field": "sourceVersion",
            "revision_checked": True, "analyzed_revision": revision, "revision_error": None, "issues": issues,
            "excluded_count": 0, "excluded": [], "excluded_cids": [], "requests": []}
    path = folder / "issues.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def base_env(tmp: Path) -> dict[str, str]:
    """The environment of the commands: no bytecode, a git of its own (no global settings), UTF-8 output."""
    gitconfig = tmp / "gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[init]\n\tdefaultBranch = main\n[user]\n\tname = flow-test\n\temail = flow@example.com\n",
                             encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("COV_USER", "COV_AUTH_KEY", "VIRTUAL_ENV", "GUI_TOKEN", "GUI_IDLE_SECONDS")
           and not k.startswith("COVERITY_")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", GIT_CONFIG_GLOBAL=str(gitconfig),
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


def git(repo: Path, *args: str, env: dict[str, str]) -> str:
    proc = subprocess.run([GIT or "git", "-c", "core.longpaths=true", *args], cwd=str(repo), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout.decode("utf-8", "replace").strip()


class CommandError(AssertionError):
    pass


def run_script(script: Path, *args: str, env: dict[str, str], cwd: Path | None = None, ok: bool | None = True,
               stdin: str | None = None, timeout: float = 180) -> dict[str, Any]:
    """``uv run --native-tls --quiet <script> <args>`` and its one JSON object."""
    proc = subprocess.run([UV or "uv", "run", "--native-tls", "--quiet", str(script), *args], cwd=str(cwd or script.parent),
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                          input=stdin.encode("utf-8") if stdin is not None else None)
    text = proc.stdout.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        raise CommandError(f"{script.name} {args[0] if args else ''}: not one JSON object: {text[-800:]!r} "
                           f"{proc.stderr.decode('utf-8', 'replace')[-800:]}") from None
    if ok is True and not data.get("ok"):
        raise CommandError(f"{script.name} {' '.join(args[:1])} failed: {json.dumps(data, ensure_ascii=False)[:1500]}")
    if ok is False and data.get("ok"):
        raise CommandError(f"{script.name} {' '.join(args[:1])} should have failed: {json.dumps(data, ensure_ascii=False)[:800]}")
    return data


# ---------------------------------------------------------------------------
# the MCP server over stdio
# ---------------------------------------------------------------------------

def mcp_command() -> tuple[list[str], dict[str, str]] | None:
    """The command and env of the first stdio server of mcp.json (${PLUGIN_ROOT} expanded), or None."""
    servers = pp.mcp_config().get("mcpServers") or {}
    if not servers:
        return None
    _, server = next(iter(servers.items()))
    if not isinstance(server, dict) or not server.get("command"):
        return None
    command = [shutil.which(server["command"]) or server["command"]] + [pp.expand_plugin_root(a) for a in server.get("args") or []]
    return command, dict(server.get("env") or {})


def mcp_folder() -> Path | None:
    regs = pp.mcp_registrations()
    folder = next(iter(regs.values()), None) if regs else None
    return folder if folder is not None and (folder / "pyproject.toml").is_file() else None


class ToolError(AssertionError):
    pass


class McpClient:
    """Newline-delimited JSON-RPC 2.0 over the pipes of the server process."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, timeout: float = 300):
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.log, **kwargs)
        self.lines: "queue.Queue[bytes | None]" = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "flow-tests", "version": "1"}}, timeout=timeout)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            self.lines.put(line)
        self.lines.put(None)

    def stderr(self) -> str:
        self.log.seek(0)
        return self.log.read().decode("utf-8", errors="replace")[-3000:]

    def send(self, message: dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        self.next_id += 1
        ident = self.next_id
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"the MCP server did not answer {method} in {timeout} s:\n{self.stderr()}")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(f"the MCP server ended:\n{self.stderr()}")
            message = json.loads(line)
            if message.get("id") == ident:
                if "error" in message:
                    raise AssertionError(f"{method} failed: {message['error']}")
                return message.get("result") or {}

    def call(self, tool: str, arguments: dict[str, Any], *, again: bool = True) -> dict[str, Any]:
        """The JSON result of a tool; called again with the same arguments while it says done: false."""
        for _ in range(30):
            result = self.request("tools/call", {"name": tool, "arguments": arguments})
            data = result.get("structuredContent")
            if not isinstance(data, dict):
                texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
                try:
                    data = json.loads(texts[0]) if texts else {}
                except ValueError:
                    data = {"text": "\n".join(texts)}
            if isinstance(data, dict) and isinstance(data.get("result"), dict) and len(data) == 1:
                data = data["result"]
            if result.get("isError"):
                raise ToolError(f"{tool}: {json.dumps(data, ensure_ascii=False)[:1500]}")
            if not (again and isinstance(data, dict) and data.get("done") is False):
                return data
        raise AssertionError(f"{tool} did not finish after 30 calls")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False)
                else:
                    os.killpg(self.proc.pid, 9)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for stream in (self.proc.stdout, self.log):
            try:
                stream.close()
            except OSError:
                pass


def find_token(data: Any) -> str | None:
    """The confirmation string (12 hex characters) in a tool result."""
    if isinstance(data, dict):
        for key, value in data.items():
            if "token" in key and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value):
                return value
        values = data.values()
    elif isinstance(data, list):
        values = data
    else:
        return None
    for value in values:
        found = find_token(value)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# the briefs and the fake agents
# ---------------------------------------------------------------------------

def brief_path(action: dict[str, Any]) -> Path:
    """The instruction file a launch prompt points to (design 4.14: 「指示ファイル <パス> を読み、…」)."""
    m = re.search(r"指示ファイル\s+`?(.+?)`?\s+を読み", action.get("prompt", ""))
    if not m:
        raise AssertionError(f"the launch prompt does not point to an instruction file: {action.get('prompt')!r}")
    return Path(m.group(1).strip().strip('"'))


class Brief:
    """What an instruction file asks: its title id, the files of its table, the CIDs of its warnings, its sections."""

    def __init__(self, path: Path):
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        m = re.search(r"(?m)^# .*?[:：]\s*(\S+)\s*$", self.text)
        self.item = m.group(1) if m else None
        self.rows: dict[str, str] = {}
        for line in self.text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.lstrip().startswith("|") and len(cells) >= 2:
                m = re.fullmatch(r"`([^`]+)`", cells[-1])
                if m:
                    self.rows.setdefault(cells[0], m.group(1))
        self.cids = [int(c) for c in re.findall(r"(?m)^### CID (\d+)", self.text)]

    def file(self, label: str) -> Path:
        for key, value in self.rows.items():
            if key.startswith(label):
                return Path(value)
        raise AssertionError(f"{self.path.name}: no row 「{label}」 in its table: {sorted(self.rows)}")

    def has(self, heading: str) -> bool:
        return re.search(r"(?m)^## " + re.escape(heading), self.text) is not None

    def section(self, heading: str) -> str:
        m = re.search(r"(?ms)^## " + re.escape(heading) + r"[^\n]*\n(.*?)(?=^## |\Z)", self.text)
        return m.group(1) if m else ""


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class FakeAgents:
    """Deterministic agents that do what the briefs ask, from the truth of the dataset.

    ``cids``: key -> CID. ``review_outcomes``: item id -> list of outcomes for its reviews in order (default pass);
    ``integrate``: whether the integrator merges the candidates of one cause."""

    def __init__(self, cids: dict[str, int], review_outcomes: dict[str, list[str]] | None = None, integrate: bool = True,
                 confidence: dict[str, str] | None = None):
        self.key_of = {cid: key for key, cid in cids.items()}
        self.warning = {w["key"]: w for w in WARNINGS}
        self.review_outcomes = {k: list(v) for k, v in (review_outcomes or {}).items()}
        self.integrate = integrate
        self.confidence = confidence or {}
        self.briefs: list[Path] = []
        self.calls: list[tuple[str, str]] = []  # (role, item)

    # ---- worker -------------------------------------------------------------------------------

    def result_for(self, item: str, keys: list[str]) -> dict[str, Any]:
        """The result.json a careful worker writes for the warnings ``keys`` (one cause)."""
        truth = TRUTH[keys[0]]
        judgement = truth["judgement"]
        impact = max((self.warning[k]["impact"] for k in keys), key=lambda i: ["Low", "Medium", "High"].index(i))
        evidence = [{"file": f, "line": line, "note": note} for k in keys for f, line, note in TRUTH[k]["evidence"]]
        evidence = [e for n, e in enumerate(evidence) if e not in evidence[:n]]
        where = f"{evidence[-1]['file']}:{evidence[-1]['line']}"
        if judgement == "true_bug":
            recommendation = "fix"
            deviation = {"classification": "Bug", "action": "Ignore", "severity": SEVERITY_OF_IMPACT[impact],
                         "comment": f"本物の不具合。{truth['summary']}が、呼び出し元はこの関数の結果を必ず使うため影響は小さい（{where}）。"}
        else:
            recommendation = "deviation"
            deviation = {"classification": "Intentional" if judgement == "intentional" else "False Positive",
                         "action": "Ignore", "severity": "Unspecified", "comment": truth["comment"]}
        confidence = min((self.confidence.get(k, "high") for k in keys), key=lambda c: ORDER[c])
        return {"item": item,
                "verdict": {"judgement": judgement, "summary": truth["summary"], "rationale": truth["rationale"],
                            "evidence": evidence},
                "recommendation": recommendation, "confidence": confidence,
                "confidence_reason": "警告経路と呼び出し元をすべて読んだ",
                "deviation": deviation,
                "fix": {"classification": "Bug", "action": "Fix Required", "severity": SEVERITY_OF_IMPACT[impact],
                        "summary": "戻り値を確かめ、失敗のときは後始末をして戻る",
                        "impact": "失敗のときだけ動きが変わる。成功のときの動きは同じ",
                        "exceeded_constraints": [], "already_fixed_on_latest": False},
                "revision_drift": {"status": "none", "detail": ""},
                "group_excluded_cids": []}

    def keys_of(self, cids: list[int]) -> list[str]:
        keys = [self.key_of[c] for c in cids if c in self.key_of]
        if not keys:
            raise AssertionError(f"the brief names no CID of the dataset: {cids}")
        return keys

    def worker(self, action: dict[str, Any]) -> str:
        brief = Brief(brief_path(action))
        self.briefs.append(brief.path)
        item = brief.item or action["item"]
        self.calls.append(("worker", item))
        keys = self.keys_of(brief.cids)
        fix_dir = brief.file("直すフォルダ")
        progress = brief.file("進み具合")
        write_json(progress, {"item": item, "phase": "fix", "note": "戻り値を確かめる修正を作っている"})
        for cause in dict.fromkeys(TRUTH[k]["cause"] for k in keys):
            rel, before, after = EDITS[cause]
            target = fix_dir / rel
            text = target.read_text(encoding="utf-8")
            if after not in text:
                if before not in text:
                    raise AssertionError(f"{item}: the work copy {target} does not hold the code of the warning")
                target.write_text(text.replace(before, after, 1), encoding="utf-8", newline="\n")
        result = self.result_for(item, keys)
        write_json(progress, {"item": item, "phase": "result", "note": "結果を書いている"})
        write_json(brief.file("結果ファイル"), result)
        label = "修正" if result["recommendation"] == "fix" else "逸脱"
        return f"{item}: {label}を推奨（確信度 高）— {result['verdict']['summary']}"

    # ---- reviewer -----------------------------------------------------------------------------

    def reviewer(self, action: dict[str, Any]) -> str:
        brief = Brief(brief_path(action))
        self.briefs.append(brief.path)
        item = brief.item or action["item"]
        self.calls.append(("reviewer", item))
        criteria = json.loads(brief.file("評価基準").read_text(encoding="utf-8"))["criteria"]
        keys = self.keys_of(brief.cids)
        checkers = {self.warning[k]["checker"].upper() for k in keys}
        merged = "まとめたもの" in brief.section("確かめる結果")
        outcomes = self.review_outcomes.get(item) or []
        outcome = outcomes.pop(0) if outcomes else "pass"

        def applies(when: str) -> bool:
            if "統合" in when:
                return merged
            if "MISRA" in when:
                return any(c.startswith("MISRA") for c in checkers)
            if "CERT" in when:
                return any(c.startswith("CERT") for c in checkers)
            return not any(c.startswith(("MISRA", "CERT")) for c in checkers)

        findings, failed_once = [], False
        first = TRUTH[keys[0]]["evidence"][-1]
        for c in criteria:
            if c.get("scope") != "item":
                continue
            when = c.get("applies_when") or ""
            if when and not applies(when):
                findings.append({"check": c["id"], "ok": True, "detail": "対象外"})
                continue
            ok = not (outcome == "rework" and not failed_once)
            failed_once = failed_once or not ok
            findings.append({"check": c["id"], "ok": ok,
                             "detail": "根拠の行と経路の条件を読み、結果と合っていた" if ok else "修正の影響の説明が足りない",
                             "file": first[0], "line": first[1]})
        review = {"item": item, "outcome": outcome, "findings": findings,
                  "confidence_cap": "medium" if outcome == "flag" else None,
                  "rework": ["修正の影響（失敗のときの戻り値）を説明に書き足す"] if outcome == "rework" else [],
                  "note": "警告経路と修正の差分を読み、結果と合っていることを確かめた"}
        write_json(brief.file("確かめの結果"), review)
        return f"{item}: {'合格' if outcome == 'pass' else '要確認' if outcome == 'flag' else '差し戻し'} — 根拠と合っていた"

    # ---- integrator ---------------------------------------------------------------------------

    def integrator(self, action: dict[str, Any]) -> str:
        brief = Brief(brief_path(action))
        self.briefs.append(brief.path)
        self.calls.append(("integrator", "integration"))
        plan_file = brief.file("計画")
        rows = {}
        for line in brief.section("調べ終わった作業").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 10 and re.fullmatch(r"\d+|G\d+", cells[0]):
                rows[cells[0]] = {"cids": [int(c) for c in re.findall(r"\d+", cells[1])],
                                  "result": Path(cells[8].strip("`")), "patch": cells[9]}
        merges = []
        if self.integrate:
            for line in brief.section("候補").splitlines():
                m = re.match(r"\s*\d+\.\s*作業\s+(.+)$", line)
                if not m:
                    continue
                members = [x.strip() for x in m.group(1).split(",") if x.strip() in rows]
                causes = {TRUTH[self.key_of[c]]["cause"] for x in members for c in rows[x]["cids"] if c in self.key_of}
                if len(members) < 2 or len(causes) != 1:
                    continue
                keys = [self.key_of[c] for x in members for c in rows[x]["cids"]]
                results = [json.loads(rows[x]["result"].read_text(encoding="utf-8")) for x in members]
                result = self.result_for(members[0], keys)
                result["confidence"] = min((r["confidence"] for r in results), key=lambda c: ORDER[c])
                result.pop("item", None)
                source = next((x for x in members if rows[x]["patch"] != "なし"), None)
                merges.append({"members": members, "reason": "同じ関数の同じ行を、同じ修正で直している",
                               "fix_source": f"item:{source}" if source else "rework", "result": result})
        write_json(plan_file, {"merges": merges, "conflicts": []})
        return f"統合: {len(merges)} つにまとめる" if merges else "統合: まとめるものはない"

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], str]]:
        return {"worker": self.worker, "reviewer": self.reviewer, "integrator": self.integrator}


# ---------------------------------------------------------------------------
# the orchestrator's loop
# ---------------------------------------------------------------------------

class Orchestrator:
    """Does what tick says, like the orchestrator agent: fetch actions through ``fetch`` (a function of the action),
    launch actions through the fake agents, then tells tick what finished. Every action and tick is kept."""

    def __init__(self, run_py: Path, run_dir: Path, env: dict[str, str], cwd: Path, agents: FakeAgents,
                 fetch: Callable[[dict[str, Any]], None] | None = None, wait: int = 3):
        self.run_py, self.run_dir, self.env, self.cwd = run_py, run_dir, env, cwd
        self.agents, self.fetch, self.wait = agents, fetch, wait
        self.actions: list[dict[str, Any]] = []
        self.ticks: list[dict[str, Any]] = []

    def tick(self, *extra: str) -> dict[str, Any]:
        out = run_script(self.run_py, "tick", "--run", str(self.run_dir), "--wait", str(self.wait), *extra,
                         env=self.env, cwd=self.cwd, timeout=120)
        self.ticks.append(out)
        return out

    def drive(self, until: Callable[[dict[str, Any]], bool] | None = None, limit_seconds: float = 150,
              max_ticks: int = 400) -> dict[str, Any]:
        """Until tick says done or stop (or ``until`` says so). Raises when the run does not end in time."""
        deadline = time.monotonic() + limit_seconds
        out = self.tick()
        todo = list(out.get("actions") or [])
        for _ in range(max_ticks):
            if out.get("stop"):
                raise AssertionError(f"tick stopped the run: {out.get('say')} {out.get('error')}")
            if out.get("done") and not todo:
                return out
            if until is not None and until(out):
                return out
            if time.monotonic() > deadline:
                break
            if not todo:
                out = self.tick()
                todo += out.get("actions") or []
                continue
            action = todo.pop(0)
            self.actions.append(action)
            kind = action.get("type")
            extra: list[str] = []
            if kind in ("fetch_issues", "fetch_details"):
                if self.fetch is None:
                    raise AssertionError(f"tick asked for {kind}, but this test fetches nothing: {action}")
                self.fetch(action)
            elif kind == "launch":
                handler = self.agents.handlers().get(action.get("role"))
                if handler is None:
                    extra = ["--reason", "その役のエージェントがない"]
                else:
                    handler(action)
            elif kind == "command":
                subprocess.run(action["command"], shell=True, cwd=str(self.cwd), env=self.env, timeout=60,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                raise AssertionError(f"tick returned an action of an unknown type: {action}")
            if action.get("task"):
                out = self.tick("--failed" if extra else "--finished", action["task"], *extra)
                todo += out.get("actions") or []
        raise AssertionError(f"the run did not end in {limit_seconds} s; last tick: {json.dumps(out, ensure_ascii=False)[:1500]}")

    def launches(self, role: str | None = None) -> list[dict[str, Any]]:
        return [a for a in self.actions if a.get("type") == "launch" and (role is None or a.get("role") == role)]


def missing_tool(reason: str | None) -> str | None:
    """A missing tool skips the tests on a developer's PC. In CI (CI=true) it fails them."""
    if reason and os.environ.get("CI") == "true":
        raise AssertionError(reason)
    return reason


def prerequisites() -> str | None:
    """The tool the flow needs and this PC does not have, or None."""
    return missing_tool("uv がありません" if UV is None else "git がありません" if GIT is None else None)
