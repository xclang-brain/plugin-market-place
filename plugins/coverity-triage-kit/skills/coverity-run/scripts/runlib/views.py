"""The commands other than tick (design 4.12): settings, runs, new-run, resume, status, summary, stop,
note-issue, initial-status."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET  # noqa: N817 - the usual name
from pathlib import Path
from typing import Any

from . import approvals, briefs, checks, initial, issues, jobs, metrics, oplog, report, schema
from .envvars import KEY_ENVS
from .common import (RunError, append_line, file_lock, inside, mtime, now_iso, read_json, remove_file, remove_tree,
                     resolve_path, run_cmd, try_json, write_json)
from .config import (DEFAULT_FILTER, PREPARE_HINT, config_path, filter_names, load_config, load_filter, output_dir,
                     parse_filter, read_yaml)
from .flow import STAGE_JA, STALE_SECONDS
from .state import FORMAT, Run, counts, create_run, list_runs, tool_version, unfinished
from .vcs import make_vcs
from .work import VCS_LOCK_STALE

CLEANUP_LOCK_WAIT = 60  # summary waits this long for the work copy lock, else it leaves the copies (the screen ends it at 120 s)

STALL_SECONDS = 600
INITIAL_STALL_SECONDS = 1800
SCRIPT_STAGES = ("copy", "save", "merge")  # an item's stage while a script (not an agent) works on it: not "stalled"
LONG_PATH = 240
AGENT_ROLES = ("worker", "reviewer", "integrator")


def _repo(path: str) -> Path:
    repo = resolve_path(path)
    if not repo.is_dir():
        raise RunError(f"フォルダがありません: {repo}")
    return repo


MCP_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\([A-Za-z0-9_]{1,64}\)$")
# Claude Code's names of MCP tools: mcp__<server>__<tool>, or mcp__plugin_<plugin>_<server>__<tool> for a server
# that a plugin registers. A name part is letters, digits, "-" and single "_" (never "__", which separates the parts);
# a wildcard or the server name alone (it would allow the tool that writes back too) does not match.
_CLAUDE_PART = r"[A-Za-z0-9](?:[A-Za-z0-9-]|_(?!_)){0,63}"
CLAUDE_MCP_TOOL = re.compile(r"^mcp__(?:plugin_" + _CLAUDE_PART + r"_)?" + _CLAUDE_PART + r"__" + _CLAUDE_PART + r"$")
# Words of a tool name that changes something: such a tool is never pre-approved, whatever the caller passes.
WRITE_WORDS = {"update", "write", "set", "put", "post", "patch", "delete", "remove", "create", "apply", "commit",
               "push", "edit", "modify", "insert", "upload"}
CLIENTS = ("copilot", "claude-code")


def _tool_words(tool: str, client: str) -> set[str]:
    name = tool.rsplit("__", 1)[-1] if client == "claude-code" else tool[tool.index("(") + 1:-1]
    return {w.lower() for w in re.split(r"[_-]", name) if w}


def _read_tools(mcp_tools: list[str] | None, client: str) -> list[str]:
    pattern = CLAUDE_MCP_TOOL if client == "claude-code" else MCP_TOOL
    tools = [t for t in dict.fromkeys(mcp_tools or []) if pattern.match(t)]
    if client == "claude-code":  # the Copilot CLI form is left as it was (the caller passes only the tools that read)
        tools = [t for t in tools if not _tool_words(t, client) & WRITE_WORDS]
    return tools


def permission_steps(output: Path, mcp_tools: list[str] | None = None, client: str = "copilot") -> list[str]:
    """The two ways to pre-approve (REQ-306). The Coverity tools to allow come from the caller (``--mcp-tool``):
    only the tools that read, as ``<registration name>(<tool>)`` for Copilot CLI or as Claude Code's tool names
    (``mcp__<server>__<tool>``) for Claude Code. The tool that writes triage back to Coverity is never pre-approved,
    so writing back always asks (REQ-107)."""
    if client not in CLIENTS:
        raise RunError(f"--client は {' か '.join(CLIENTS)} です: {client}")
    tools = _read_tools(mcp_tools, client)
    if client == "claude-code":
        return _claude_steps(output, tools)
    first = (f"チャットで /add-dir \"{output}\" と入力し、最初の確認で「このセッションでは許可」を選びます"
             "（uv のコマンド、ファイルの書き込み、Coverity から読むツール）。Coverity に書き戻すツールの確認には、許可を与えません。")
    restart = f"または、Copilot CLI を copilot --add-dir \"{output}\" --allow-tool 'shell(uv:*)' --allow-tool write"
    if tools:
        flags = " ".join(f"--allow-tool '{t}'" for t in tools)
        return [first, f"{restart} {flags} で起動し直します（Coverity に書き戻すツールは許可しません。反映するときに確認が出ます）。"]
    return [first, f"{restart} で起動し直します（Coverity から読むツールは、最初の確認で「このセッションでは許可」を選びます。"
                   "書き戻すツールは前もって許可しません）。"]


SKILLS_DIR = Path(__file__).resolve().parents[3]  # <skills>/coverity-run/scripts/runlib/views.py
OWN_SKILL = Path(__file__).resolve().parents[2].name  # this skill's folder (a renamed copy keeps working)
_SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
# A path that can go into a permission rule and into the double-quoted command to copy as it is.
_RULE_PATH = re.compile(r'^[^"`$\\()*?\[\]!\n\r]+$')


def _rule_path(path: Path) -> str | None:
    """An absolute path in Claude Code's rule form: //<path> (on Windows C:\\a becomes //c/a). None when the
    path has a character that the rule or the double-quoted command would read in another way."""
    text = path.as_posix()
    drive = re.match(r"^([A-Za-z]):/(.*)$", text)
    if not drive and not text.startswith("/"):
        return None
    rule = f"//{drive.group(1).lower()}/{drive.group(2)}" if drive else "/" + text
    return rule if _RULE_PATH.match(rule) else None


def _read_rule(folder: Path) -> str | None:
    """The rule that only reads ``folder``: Read(//<path>/**), or Read(~/<path>/**) when only the home folder's
    name has a character that cannot go into a rule. None when neither can be written."""
    path = _rule_path(folder)
    if path:
        return f"Read({path}/**)"
    try:
        rel = folder.relative_to(Path.home().resolve()).as_posix()
    except (ValueError, RuntimeError, OSError):
        return None
    return f"Read(~/{rel}/**)" if rel != "." and _RULE_PATH.match(rel) else None


def _allowed_line(skill_md: Path) -> str | None:
    """The allowed-tools line of a SKILL.md's frontmatter (Claude Code asks before a skill that has one), or None."""
    try:
        text = skill_md.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    head = text.split("\n---", 1)[0] if text.startswith("---") else ""
    return next((line.rstrip() for line in head.splitlines() if line.startswith("allowed-tools")), None)


def claude_skill_rules(skills_dir: Path = SKILLS_DIR, own: str = OWN_SKILL) -> list[str]:
    """The Claude Code rules that let the kit's skills run unasked (their frontmatter has allowed-tools, so Claude
    Code asks before each one) and let their reference files be read (the skill folder of an installed plugin is
    outside the repository, so each read asks). In a plugin, Skill(<plugin>:*); with the skills taken out on their
    own (.claude/skills/), Skill(<skill>) for this skill and for the skills next to it whose allowed-tools line is
    exactly this skill's own (never a skill that allows other tools). The Read rule only reads: nothing in that
    folder is written."""
    rules: list[str] = []
    try:
        manifest = read_json(skills_dir.parent / ".claude-plugin" / "plugin.json", {})
    except RunError:  # a broken manifest: the skills are named one by one below
        manifest = {}
    name = manifest.get("name") if isinstance(manifest, dict) else None
    if isinstance(name, str) and _SKILL_NAME.match(name):
        rules.append(f"Skill({name}:*)")
    else:
        line = _allowed_line(skills_dir / own / "SKILL.md")
        found: list[str] = []
        if line is None:  # this skill's own line is unknown: only this skill
            found = [own] if (skills_dir / own).is_dir() else []
        else:
            try:
                found = [d.name for d in sorted(skills_dir.iterdir(), key=lambda p: p.name)
                         if (d / "SKILL.md").is_file() and _allowed_line(d / "SKILL.md") == line]
            except OSError:
                found = [own]
        rules += [f"Skill({n})" for n in found if _SKILL_NAME.match(n)]
    read = _read_rule(skills_dir)
    if read:
        rules.append(read)
    return rules


def _claude_steps(output: Path, tools: list[str], skill_rules: list[str] | None = None) -> list[str]:
    """Claude Code's two ways. /add-dir only adds the folder: it allows neither the writes nor the Coverity tools,
    and an agent that runs in the background cannot show a confirmation, so the in-session way also allows them
    through /permissions. Starting Claude Code again with --allowedTools is the reliable way for a night run.
    The writes are allowed in the run folder only (Edit(//<folder>/**) covers Write too); when the folder's name
    cannot go into a rule, Write and Edit are allowed as they are, and the steps say so."""
    rules = claude_skill_rules() if skill_rules is None else skill_rules
    has_skill = any(r.startswith("Skill(") for r in rules)
    has_read = any(r.startswith("Read(") for r in rules)
    read = (f"Coverity から読むツール（{'、'.join(tools)}）" if tools else
            "Coverity から読むツール（接続の確かめ・警告の検索・警告の詳細・反映の確かめ。/mcp で見える名前）")
    what = "と".join(w for w, on in (("スキルを使うこと", has_skill), ("スキルの参照資料を読むこと", has_read)) if on)
    skills = f"{what}（{'、'.join(rules)}）、" if rules else ""
    kinds = " と ".join(k for k, on in (("Skill", has_skill), ("Read", has_read)) if on)
    times = "と".join(t for t, on in (("スキルを使うたび", has_skill), ("参照資料を読むたび", has_read)) if on)
    why = f"{kinds} を許可しないと、{times}に確認が出ます" if kinds else ""
    why += "（Read は読むだけで、そのフォルダには書きません）。" if has_read else ("。" if why else "")
    if has_skill and not has_read:
        why += ("スキルのフォルダの名前に、許可の指定で使えない文字があるので、そのフォルダを読む許可（Read）を示していません。"
                "スキルのフォルダが調べているリポジトリの外にあるときは、/permissions で、そのフォルダを読む許可を手で足します。"
                "足さないと、参照資料を読むたびに確認が出て、裏で動くエージェントは止まります。")
    folder = _rule_path(output)
    edit = f"Edit({folder}/**)" if folder else None
    if edit:
        writes = f"実行フォルダに書くこと（{edit}。Write も含みます）"
        scope = (f"{edit} は、実行フォルダの中に書くこと（新しいファイルも）だけの許可です。ほかの場所（調べているリポジトリなど）に書くときは、"
                 "確認が出ます。")
    else:
        writes = "ファイルの書き込み（Write・Edit）"
        scope = ("実行フォルダの名前に、許可の指定で使えない文字があるので、書く場所を絞れません。"
                 "Write と Edit の許可は、このセッションのどのファイルにも効きます（実行フォルダにだけ書くのは、エージェントの決まりです）。")
    first = (f"チャットで /add-dir \"{output}\" と入力し、確認で、このセッションのあいだの許可を選びます。"
             "/add-dir だけでは足りません（フォルダが加わるだけで、ファイルの書き込みもツールも許可されません）。"
             f"続けて /permissions で Bash(uv run *)、{skills}{writes}、{read}を許可します。"
             f"{why}{scope}"
             "/permissions で足した許可は、設定ファイルに保存され、後のセッションにも残ります。保存先は、自分だけの、このプロジェクトの設定"
             "（.claude/settings.local.json）にし、実行が終わったら /permissions で消します。"
             "Coverity に書き戻すツールは許可しません。")
    allowed = " ".join(f'"{r}"' for r in ["Bash(uv run *)", *rules, *([edit] if edit else ["Write", "Edit"])])
    restart = f"または、Claude Code を claude --add-dir \"{output}\" --allowedTools {allowed}"
    agent = "エージェントを --agent で起動して始めたときは、同じ --agent も後ろに足します。"
    sure = "夜通しの実行には、こちらが確実です。起動のときの許可は、そのセッションだけに効きます。"
    apply = "反映するときに確認が出ます。反映は、--agent を付けずに起動したセッションでします"
    if tools:
        names = " ".join(f'"{t}"' for t in tools)
        return [first, f"{restart} {names} で起動し直します（{sure}{agent}Coverity に書き戻すツールは許可しません。{apply}）。"]
    return [first, f"{restart} で起動し直します（{sure}{agent}Coverity から読むツール（接続の確かめ・警告の検索・"
                   f"警告の詳細・反映の確かめ）も、/mcp で見える名前を後ろに 1 つずつ足します。書き戻すツールは前もって許可しません。{apply}）。"]


# ---- settings ---------------------------------------------------------------------------------


def settings(repo_root: str) -> dict[str, Any]:
    repo = _repo(repo_root)
    if not config_path(repo).is_file():
        raise RunError(f"設定ファイルがありません: {config_path(repo)}", how_to_fix=PREPARE_HINT)
    values, problems = load_config(repo)
    names = filter_names(repo)
    for name in names:
        path = config_path(repo).parent / "filters" / name
        try:
            spec, found = parse_filter(read_yaml(path), name)
        except RunError as exc:
            problems.append(str(exc))
            continue
        problems += found
        if not found and len(spec["streams"]) != 1:
            problems.append(f"{name} の streams: ストリームを 1 つだけ書いてください")
    if not names:
        problems.append("条件ファイル（.coverity-triage/filters/*.yaml）がありません")
    out_dir = output_dir(repo, values)
    if inside(out_dir, repo):
        problems.append(f"output_dir: 実行フォルダの置き場所がリポジトリの中です（{out_dir}）。リポジトリの外にしてください")
    verify = values["verify"]
    if (verify["default"] != "none" or verify["initial"]) and not verify["build_command"]:
        problems.append("verify.build_command: ビルドでの検証か初回解析を使うときは、ビルドのコマンドを書いてください")
    return {"ok": True, "repo_root": str(repo), "config_file": str(config_path(repo)),
            "max_rework": values["agents"]["max_rework"], "review": values["agents"]["review"],
            "integration": values["agents"]["integration"], "initial": verify["initial"], "verify": verify["default"],
            "max_items": values["max_items"], "parallel": values["parallel"], "output_dir": str(out_dir),
            "filters": names, "default_filter": DEFAULT_FILTER if DEFAULT_FILTER in names else (names[0] if names else None),
            "vcs": values["vcs"]["type"], "problems": problems, "ready": not problems,
            # names (never values) of the variables that hold credentials: callers mask their values in text they show
            "secret_envs": [n for n in (values["coverity"]["user_env"], values["coverity"]["key_env"], *KEY_ENVS) if n]}


# ---- runs -------------------------------------------------------------------------------------


def runs(repo_root: str, limit: int = 20) -> dict[str, Any]:
    repo = _repo(repo_root)
    values, _ = load_config(repo, need_vcs=False)
    output = output_dir(repo, values)
    found = []
    for folder in list_runs(output)[:max(1, limit)]:
        data = try_json(folder / "run.json")
        has_summary = (folder / "summary.html").is_file() or (folder / "summary.md").is_file()
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            found.append({"run_id": folder.name, "run_dir": str(folder),
                          "created_at": (data or {}).get("created_at") if isinstance(data, dict) else None,
                          "filter_name": (data or {}).get("filter_name") if isinstance(data, dict) else None,
                          "counts": {}, "unfinished": False, "has_summary": has_summary, "foreign": True})
            continue
        found.append({"run_id": data.get("run_id", folder.name), "run_dir": str(folder), "created_at": data.get("created_at"),
                      "filter_name": data.get("filter_name"), "counts": counts(data),
                      "unfinished": unfinished(data, folder), "has_summary": has_summary, "foreign": False,
                      "same_tool": data.get("tool_version") == tool_version(), "initial": bool(data.get("initial")),
                      "stopped": bool(data.get("stopped"))})
    return {"ok": True, "output_dir": str(output), "runs": found}


# ---- new-run ----------------------------------------------------------------------------------


def _versioned(repo: Path, vcs_type: str) -> list[str] | None:
    """The files a work copy will hold (git ls-files, or svn's versioned items), relative to ``repo``.
    None when the command fails (then every file is measured)."""
    try:
        if vcs_type == "git":
            proc = run_cmd(["git", "-c", "core.longpaths=true", "ls-files", "-z"], cwd=repo, check=False, timeout=60)
            if proc.returncode != 0:
                return None
            return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
        if vcs_type == "svn":
            proc = run_cmd(["svn", "status", "-v", "--xml", "--non-interactive"], cwd=repo, check=False, timeout=120)
            if proc.returncode != 0:
                return None
            root = ET.fromstring(proc.stdout)  # svn writes XML in UTF-8 whatever the console code page is
            out = []
            for entry in root.iter("entry"):
                status = entry.find("wc-status")
                if status is not None and status.get("item") not in ("unversioned", "ignored", "external"):
                    out.append(os.path.relpath(entry.get("path", ""), repo) if os.path.isabs(entry.get("path", ""))
                               else entry.get("path", ""))
            return out
    except (RunError, OSError, ET.ParseError, subprocess.SubprocessError):
        return None
    return None


def _longest_path(repo: Path, vcs_type: str = "") -> int:
    files = _versioned(repo, vcs_type) if vcs_type else None
    if files is not None:
        return max((len(os.path.normpath(f)) for f in files), default=0)
    longest = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".svn")]
        for name in filenames:
            longest = max(longest, len(os.path.relpath(os.path.join(dirpath, name), repo)))
    return longest


def long_path_problem(work_dir: Path, repo: Path, limit: int = LONG_PATH, vcs_type: str = "") -> str | None:
    """A note when a work copy path, or a fixed file's copy, would pass ``limit`` characters (Windows tools fail
    on long paths). Only the files a work copy holds count (not ignored build trees)."""
    prefix = max(len(str(work_dir / "w8")), len(str(work_dir.parent / "fixed" / "0000000" / "fix")))
    worst = prefix + 1 + _longest_path(repo, vcs_type)
    if worst > limit:
        return (f"作業用のコピーのパスが {worst} 文字ほどになり、{limit} 文字を超えます。"
                "設定 output_dir を短いパス（例 C:/cov-out）にしてください")
    return None


def new_run(repo_root: str, filter_name: str | None, overrides: dict[str, Any], limit: int | None, verify: str | None,
            first: bool | None, review: bool | None, integration: bool | None, max_rework: int | None,
            issues_file: str | None, mcp_tools: list[str] | None = None, client: str = "copilot") -> dict[str, Any]:
    if client not in CLIENTS:
        raise RunError(f"--client は {' か '.join(CLIENTS)} です: {client}")
    repo = _repo(repo_root)
    values, problems = load_config(repo)
    if problems:
        raise RunError("設定に直すところがあります", problems=problems)
    if values["vcs"]["type"] not in ("git", "svn"):
        raise RunError("git / svn のリポジトリの一番上のフォルダを --repo に渡してください")
    max_items = values["max_items"]
    if limit is not None and not 1 <= limit <= max_items:
        raise RunError(f"件数の上限（--limit）は 1 から {max_items}（設定 max_items）までにしてください")
    if filter_name is None:
        names = filter_names(repo)
        if not names:
            raise RunError("条件ファイル（.coverity-triage/filters/*.yaml）がありません", how_to_fix=PREPARE_HINT)
        filter_name = DEFAULT_FILTER if DEFAULT_FILTER in names else names[0]
    spec, filter_file = load_filter(repo, filter_name, overrides)
    if len(spec["streams"]) != 1:
        raise RunError("条件ファイルには、ストリームを 1 つだけ書いてください", streams=spec["streams"], filter_file=str(filter_file))
    requested = limit or spec["limit"] or max_items
    run_limit = min(requested, max_items)
    mode = verify or values["verify"]["default"]
    if mode not in ("none", "build", "build+analyze"):
        raise RunError("ビルドでの検証は none / build / build+analyze のどれかです")
    if mode != "none" and not values["verify"]["build_command"]:
        raise RunError("ビルドでの検証の設定（verify.build_command）がありません")
    if issues_file and first:
        raise RunError("取ってある警告で始めるとき（--issues-file）は、初回解析はできません")
    first = (values["verify"]["initial"] and not issues_file) if first is None else first
    if first and not values["verify"]["build_command"]:
        raise RunError("初回解析の設定（verify.build_command）がありません")
    review = values["agents"]["review"] if review is None else review
    integration = values["agents"]["integration"] if integration is None else integration
    max_rework = values["agents"]["max_rework"] if max_rework is None else max_rework
    if not 0 <= max_rework <= 2:
        raise RunError("作り直しの上限（--max-rework）は 0 から 2 までにしてください")
    output = output_dir(repo, values)
    if inside(output, repo):
        raise RunError(f"実行フォルダの置き場所がリポジトリの中です: {output}（設定 output_dir をリポジトリの外にしてください）")
    given = None
    if issues_file:
        source = resolve_path(issues_file)
        given = read_json(source)
        found = issues.check_issues_file(given)
        if found:
            raise RunError("警告の一覧のファイルに直すところがあります", problems=found, issues_file=str(source))
    if sys.platform == "win32":
        problem = long_path_problem(output / "00000000-000000-00" / "work", repo, vcs_type=values["vcs"]["type"])
        if problem:
            raise RunError(problem)
    vcs = make_vcs(repo, values)
    fingerprint = vcs.fingerprint()
    run = create_run(output, {
        "repo_root": str(repo), "filter_name": spec["name"], "filter_file": str(filter_file), "stream": spec["streams"][0],
        "limit": run_limit, "verify_mode": mode, "initial": bool(first), "review": bool(review),
        "integration": bool(integration), "max_rework": int(max_rework), "parallel": values["parallel"],
        "offline": bool(issues_file), "settings": values, "planned": False, "items": [], "tasks": {}, "fetch": {},
        "alerts": [], "notes": [], "fingerprint_start": fingerprint, "stage": "initial" if first else "fetch"})
    write_json(run.dir / "filter.json", {**spec, "limit": run_limit})
    shutil.copyfile(config_path(repo), run.dir / "config.snapshot.yaml")
    briefs.copy_schemas(run.dir)
    if given is not None:
        write_json(run.dir / "issues.json", given)
        wanted = {i["cid"] for i in given["issues"]}
        for path in Path(issues_file).resolve().parent.glob("issue-*.json"):
            data = try_json(path)
            cid = (data or {}).get("cid") or ((data or {}).get("issue") or {}).get("cid")
            if cid in wanted:
                shutil.copyfile(path, run.dir / "details" / f"issue-{cid}.json")
        with run.update() as st:
            st.setdefault("fetch", {})["issues_done"] = True
    run.log("new-run", f"実行を始めた（条件 {spec['name']}、上限 {run_limit} 件）", filter=spec, limit=run_limit,
            verify=mode, initial=bool(first), review=bool(review), integration=bool(integration))
    out = {"ok": True, "run_dir": str(run.dir), "run_id": run.state["run_id"], "repo_root": str(repo),
           "work_dir": str(run.dir / "work"), "output_dir": str(output), "filter_name": spec["name"],
           "stream": spec["streams"][0], "limit": run_limit, "verify": mode, "initial": bool(first), "review": bool(review),
           "integration": bool(integration), "max_rework": int(max_rework), "parallel": values["parallel"],
           "permission_steps": permission_steps(output, mcp_tools, client), "next": "tick で次にすることを聞く"}
    if requested > run_limit:
        out["note"] = f"1 回の上限（設定 max_items）が {max_items} 件のため、{run_limit} 件にしました"
    return out


# ---- resume / stop / note-issue -----------------------------------------------------------------


def resume(run_dir: str, mcp_tools: list[str] | None = None, client: str = "copilot") -> dict[str, Any]:
    if client not in CLIENTS:
        raise RunError(f"--client は {' か '.join(CLIENTS)} です: {client}")
    run = Run(run_dir, write=True)
    try:
        values, problems = load_config(run.repo)
    except RunError as exc:
        raise RunError(str(exc), how_to_fix=PREPARE_HINT) from None
    if problems:
        raise RunError("設定に直すところがあります", problems=problems)
    running = {j["name"] for j in jobs.running(run.dir)}
    for job in jobs.all_jobs(run.dir):  # what ended in the earlier session is not this session's to finish
        if job["name"] not in running and not job.get("reaped"):
            jobs.settle(run.dir, job)
            jobs.update(run.dir, job["name"], reaped=True)
    if not running:  # a lock whose holder was killed (stop, a closed terminal) never goes away by itself
        jobs.clear_dead_locks(run.dir)
    back = {"in_progress": 0, "error": 0}
    with run.update() as st:
        st["settings"] = values
        for item in st["items"]:
            job = {"copy": f"copy-w{item.get('slot')}", "save": f"save-{item['id']}", "merge": "merge"}.get(item.get("stage") or "")
            if item["status"] == "in_progress" and not (job and job in running):
                back["in_progress"] += 1
                item.update(status="pending", stage=None, slot=None)
            elif item["status"] == "error":
                back["error"] += 1
                item.update(status="pending", stage=None, slot=None, error="", stale_retries=0, orphan_retries=0)
        st["tasks"] = {}
        st["stopped"] = False
        st["stopped_at"] = None
        st["finished"] = False
        st["finished_at"] = None
        st.pop("copy_failed", None)
        st.pop("fatal", None)
        st["halted"] = False
        st["halt_reason"] = None
        if not st.get("planned"):
            st.setdefault("fetch", {})["issues_failures"] = 0
        if st.get("verify_started") and not st.get("verified") and "verify" not in running:
            st["verify_started"] = False
        if st.get("initial") and initial.status(run).get("status") == "failed":
            remove_file(initial.folder(run.dir) / initial.STATUS_FILE)
            st["initial_failed_logged"] = False
    run.log("resume", f"実行を再開した（戻した作業 {back['in_progress'] + back['error']} 件）", reset=back)
    output = run.dir.parent
    return {"ok": True, "run_dir": str(run.dir), "run_id": run.state["run_id"], "repo_root": str(run.repo),
            "work_dir": str(run.dir / "work"), "counts": counts(run.state), "back_to_pending": back,
            "initial": bool(run.state.get("initial")), "review": bool(run.state.get("review")),
            "integration": bool(run.state.get("integration")), "max_rework": run.state.get("max_rework"),
            "permission_steps": permission_steps(output, mcp_tools, client), "next": "tick で次にすることを聞く"}


def stop(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir, write=True)
    with run.update() as st:
        st["stopped"] = True
        st["stopped_at"] = now_iso()
        st["tasks"] = {}
    stopped, unstopped = jobs.stop_all(run.dir)
    run.log("stop", "実行を止めた" + (f"（止めきれなかった処理 {len(unstopped)} 件）" if unstopped else ""),
            level="warning", stopped_jobs=stopped, unstopped=unstopped)
    return {"ok": True, "run_dir": str(run.dir), "stopped": True, "stopped_jobs": stopped, "unstopped": unstopped}


def note_issue(run_dir: str, title: str, body: str) -> dict[str, Any]:
    run = Run(run_dir, write=True)
    title, body = (title or "").strip(), (body or "").strip()
    if not title or not body:
        raise RunError("課題の題（title）と本文（body）の両方を渡してください")
    import json

    path = run.dir / "issues-found.jsonl"
    record = oplog.mask({"title": title[:200], "body": body[:8000], "at": now_iso()}, run.secrets())
    with file_lock(path):
        append_line(path, json.dumps(record, ensure_ascii=False))
    run.log("note-issue", f"課題を書き残した: {title[:60]}")
    return {"ok": True, "issues_found": len(issues_found(run))}


def issues_found(run: Run) -> list[dict[str, Any]]:
    path = run.dir / "issues-found.jsonl"
    out = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    import json

    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("title"):
            out.append({"title": record.get("title"), "body": record.get("body", ""), "at": record.get("at")})
    return out


# ---- status ------------------------------------------------------------------------------------


def criteria_list() -> list[dict[str, Any]]:
    out = []
    for c in schema.load("criteria.json")["criteria"]:
        entry = {"id": c["id"], "scope": c["scope"], "text": c["text"]}
        if c.get("applies_when"):
            entry["applies_when"] = c["applies_when"]
        out.append(entry)
    return out


def criteria_results(run: Run, run_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviews = []
    for item in run.state["items"]:
        if item["status"] == "done" and item.get("review") in ("pass", "flag", "rework"):
            data = try_json(run.review_file(item["id"]))
            if isinstance(data, dict):
                reviews.append((item["id"], {f.get("check"): f for f in data.get("findings") or [] if isinstance(f, dict)}))
    checked = {c["id"]: c for c in run_checks}
    out = []
    for c in criteria_list():
        entry = {"id": c["id"], "scope": c["scope"], "passed": 0, "failed": 0, "not_applicable": 0, "items_failed": []}
        if c["scope"] == "item":
            for item_id, findings in reviews:
                f = findings.get(c["id"])
                if f is None or (f.get("ok") and "対象外" in str(f.get("detail", ""))):
                    entry["not_applicable"] += 1
                elif f.get("ok"):
                    entry["passed"] += 1
                else:
                    entry["failed"] += 1
                    entry["items_failed"].append(item_id)
        elif c["id"] in checked:
            ok = checked[c["id"]].get("ok")
            entry["passed" if ok else ("not_applicable" if ok is None else "failed")] += 1
        out.append(entry)
    return out


def _progress_of(path: Path, since: float) -> dict[str, Any]:
    t = mtime(path)
    if t is None or t < since - 2:
        return {}
    data = try_json(path)
    return data if isinstance(data, dict) else {}


def _item_status(run: Run, item: dict[str, Any], tasks: dict[str, Any], mapped: dict[int, Any], verify: dict[str, Any],
                 now: float) -> dict[str, Any]:
    item_id = item["id"]
    stage = item.get("stage")
    phase, note, idle = None, "", None
    task = tasks.get(f"worker:{item_id}") or tasks.get(f"reviewer:{item_id}")
    if item["status"] == "in_progress":
        if stage == "copy":
            phase, note = "copy", "作業用のコピーを用意している（初めての枠は時間がかかります）"
        elif stage in ("save", "merge"):
            phase, note = stage, "修正を保存している" if stage == "save" else "まとめた修正を作っている"
        elif task:
            progress = _progress_of(run.item_dir(item_id) / "progress.json", float(task.get("launched_at") or 0))
            phase, note = progress.get("phase") or "read", str(progress.get("note") or "")[:200]
    elif task and task.get("role") == "reviewer":
        progress = _progress_of(run.dir / "agents" / "reviewer" / "progress.json", float(task.get("launched_at") or 0))
        phase, note = progress.get("phase") or "read", str(progress.get("note") or "確かめている")[:200]
    if task:
        folder = run.item_dir(item_id)
        seen = [float(task.get("launched_at") or 0), *(mtime(p) or 0 for p in folder.glob("*"))]
        if task.get("role") == "reviewer":
            seen.append(mtime(run.dir / "agents" / "reviewer" / "progress.json") or 0)
        idle = now - max(seen)
    review = None
    if item.get("review") == "unverified":
        review = {"outcome": "unverified", "findings": []}
    elif item.get("review"):
        data = try_json(run.review_file(item_id)) or {}
        outcome = "flag" if item["review"] == "flag" else item["review"]
        review = {"outcome": outcome, "findings": [{"check": f.get("check"), "ok": f.get("ok"), "detail": f.get("detail")}
                                                   for f in data.get("findings") or [] if isinstance(f, dict)]}
    recommendation = confidence = None
    found = mapped.get(item["cids"][0]) if item["cids"] else None
    checker = (found or {}).get("checker")
    if item["status"] == "done":
        shown = report.shown_result(run, item, verify)
        if shown:
            recommendation, confidence = shown.get("recommendation"), shown.get("confidence")
    started = item.get("started")
    elapsed = (int(now - started) if item["status"] == "in_progress" and started else
               (int(item["seconds"]) if item.get("seconds") is not None else None))
    return {"id": item_id, "cids": item["cids"], "checker": checker, "status": item["status"], "phase": phase,
            "note": note, "elapsed_seconds": elapsed,
            "stalled": bool(idle is not None and idle >= STALL_SECONDS and stage not in SCRIPT_STAGES),
            "preparing_copy": stage == "copy", "attempts": item.get("attempts", 0),
            "rework_rounds": item.get("review_rounds", 0), "review": review, "recommendation": recommendation,
            "confidence": confidence, "error": item.get("error") or None}


def status(run_dir: str) -> dict[str, Any]:
    """The state for a screen. Reads only; takes no lock."""
    run = Run(run_dir)
    st = run.state
    now = time.time()
    tasks = st.get("tasks") or {}
    mapped = issues.mapped(run)
    verify = try_json(run.dir / "verify.json") or {}
    c = counts(st)
    stage = st.get("stage") or "fetch"
    if st.get("stopped"):
        stage = "stopped"
    initial_view = initial.progress_view(run)
    if initial_view["used"] and initial_view["step"] and initial.status(run).get("status") == "running":
        log = initial.folder(run.dir) / initial.LOG_FILE
        initial_view["stalled"] = now - (mtime(log) or now) >= INITIAL_STALL_SECONDS
    run_checks = st.get("run_checks") or []
    processed = c["done"] + c["error"]
    return {"ok": True, "run_id": st["run_id"], "run_dir": str(run.dir), "repo_root": st.get("repo_root"),
            "stage": stage, "stage_label": STAGE_JA.get(stage, stage), "finished": bool(st.get("finished")),
            "stopped": bool(st.get("stopped")), "halted": bool(st.get("halted")),
            "halt_reason": (st.get("halt_reason") or None) if st.get("halted") else None,
            "has_summary": run.summary_file().is_file(),
            "review": bool(st.get("review")), "integration": bool(st.get("integration")),
            "max_rework": st.get("max_rework"), "parallel": st.get("parallel"),
            "counts": {k: c[k] for k in ("pending", "in_progress", "done", "error", "merged", "total")},
            "items": [_item_status(run, i, tasks, mapped, verify if isinstance(verify, dict) else {}, now)
                      for i in st["items"] if i["status"] != "merged"],
            "running_tasks": [{"task": t["task"], "role": t["role"], "item": t.get("item"), "description": t.get("description")}
                              for t in tasks.values() if t.get("role") in AGENT_ROLES],
            "errors": [{"item": a.get("item"), "kind": a.get("kind"), "detail": a.get("detail"), "level": a.get("level")}
                       for a in (st.get("alerts") or [])[-50:]],
            "recent": oplog.recent(run.dir, 10), "initial": initial_view,
            "progress": {"value": processed, "max": c["total"], "label": f"{processed} / {c['total']}"},
            "run_checks": run_checks, "criteria": criteria_list(), "criteria_results": criteria_results(run, run_checks),
            "issues_found": issues_found(run), "stale_seconds": STALE_SECONDS}


def initial_status(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    data = initial.status(run)
    view = initial.progress_view(run)
    return {"ok": True, **view, "status": data.get("status"), "step_label": data.get("step_label"),
            "elapsed_seconds": data.get("elapsed_seconds"), "error": data.get("error"),
            "log_tail": data.get("log_tail"), "dirty": data.get("dirty")}


# ---- summary -----------------------------------------------------------------------------------


def _cleanup(run: Run) -> list[str]:
    work = run.dir / "work"
    removed = []
    if not work.is_dir():
        return removed
    vcs = make_vcs(run.repo, run.settings)
    try:
        with file_lock(work / "vcs", timeout=CLEANUP_LOCK_WAIT, stale=VCS_LOCK_STALE):
            for folder in sorted(work.iterdir()):
                if folder.is_dir():
                    try:
                        vcs.remove_copy(folder)
                    except RunError:
                        remove_tree(folder, check=False)
                    if not folder.exists():  # only what is really gone (a file a program holds keeps its folder)
                        removed.append(folder.name)
                elif folder.suffix in (".rev", ".json"):
                    try:
                        remove_file(folder)
                    except OSError:
                        pass
    except RunError:  # another process still works on the copies (the lock wait ended): left for a later summary
        pass
    return removed


def summary(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir, write=True)
    results = {}
    for item in run.state["items"]:
        if item["status"] == "done" and run.result_file(item["id"]).is_file():
            results[item["id"]] = read_json(run.result_file(item["id"]))
    approvals.ensure_defaults(run, results)
    made = report.write_summary(run)
    rows = made["rows"]
    run_checks = checks.run_checks(run, rows)
    with run.update() as st:
        st["run_checks"] = run_checks
        st["summary_at"] = now_iso()
    removed = []
    busy = [j for j in jobs.running(run.dir)]
    c = counts(run.state)
    if not c["pending"] and not c["in_progress"] and not busy:
        removed = _cleanup(run)
    if run.settings["options"]["metrics"]:
        metrics.write(run, rows)
    numbers = made["counts"]
    failed_checks = [x["id"] for x in run_checks if x["ok"] is False]
    say = [f"一覧を作りました: {run.summary_file()}",
           f"作業 {numbers['items']} 件（修正の推奨 {numbers['fix']}、逸脱の推奨 {numbers['deviation']}、確信度 低 {numbers['low']}）、"
           f"処理できなかった作業 {numbers['errors']} 件、未処理 {numbers['unprocessed']} 件です。"]
    if not run.state["items"]:
        say.append("条件に合う警告はありませんでした。")
    if failed_checks:
        say.append("実行ごとの評価基準で合わなかったもの: " + "、".join(failed_checks) + "（run_checks の detail を見てください）")
    say.append("承認（修正・逸脱・却下・反映しない）は、結果を見る画面で確かめて選びます。終わったら、反映の手順で反映します。")
    run.log("summary", f"一覧を作った（{numbers['items']} 件、処理できなかった作業 {numbers['errors']} 件）",
            items=numbers["items"], errors=numbers["errors"])
    return {"ok": True, "summary_html": str(run.summary_file()), "run_dir": str(run.dir), "counts": numbers,
            "approvals_counts": made["approvals_counts"],
            "results": [{**r, "report": str(run.dir / r["report"])} for r in rows], "unprocessed": made["unprocessed"],
            "run_checks": run_checks, "criteria": criteria_list(), "criteria_results": criteria_results(run, run_checks),
            "removed_work_copies": removed, "say": say}
