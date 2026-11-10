#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""EVAL stage 4 (entry routing): which skill and tool Claude picks for each way of asking.

The workspace is the self-test's sample repository with three skills copied into ``.claude/skills/``
(the screen entry, the batch run and the help skill; names read from docs/parts.md by role) and a
stand-in for the extension's open tool (fake_open_server.py: a stdio MCP server that logs each call
and returns a fake URL and the notices). The tool's name, description and notices are read from the
extension's core/messages.mjs with node when it is there.

For each case of cases.json, ``claude -p "<words>"`` runs with Read, Glob, Grep, Skill and Bash; only
Skill and the open tool are allowed, so commands are refused (an attempt is recorded, nothing runs).
Checks: the open tool called once with requestText equal to the words (expect open), not called
(expect not_open), absent with the three Copilot CLI causes explained and no work started (expect
no_tool: the request says the user is on Copilot CLI), or absent with the Claude Code ways shown
(expect no_tool_claude: the screen is a Copilot CLI feature, ``claude --agent`` and ``/<batch run>``;
none of the three causes; no work started); the skills loaded; no agents, commands or edits.

Claude Code shows the tool as mcp__<server>__<tool>, Copilot CLI by its own name: the report says so.

  python3 tests/eval/stage4/run_stage4.py [--only NAME|N]... [--model M] [--timeout S] [--max-turns N]
        [--out DIR] [--build-only] [--claude-bin EXE ...]

Writes <out>/<case>/ (transcript, calls.jsonl, case.json), <out>/results.json, <out>/report.md.
Prints one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import evalkit as ek  # noqa: E402

TAG = "stage4"
CASES_FILE = HERE / "cases.json"
SERVER_SCRIPT = HERE / "fake_open_server.py"
SERVER_NAME = "extension"
DEFAULT_MODEL = "claude-sonnet-5-5"
ROLE_SCREEN, ROLE_RUN, ROLE_HELP, ROLE_SELFTEST, ROLE_EXTENSION = "画面の入口", "一括実行", "困ったとき", "自己診断", "エクステンション"
TOOLS = ["Read", "Glob", "Grep", "Skill", "Bash"]
FAKE_URL = "http://127.0.0.1:8731/?k=eval-7f3a9c"
WORK_TOOLS = ("Task", "Agent", "Edit", "Write", "MultiEdit", "NotebookEdit")
# The three causes of a missing tool, each found by any of its words (skill reference when-tool-missing).
CAUSES = {
    "試験的な機能": ["--experimental", "experimental", "試験的"],
    "読み込みの確認を断った": ["断った", "断る", "断ると", "elevated", "読み込みの確認", "確認を許可", "declin", "拒否"],
    "/extensions mode が Disabled": ["/extensions mode", "extensions mode", "Disabled"],
}
# What the Claude Code answer for a missing tool must say (skill: "Claude Code で動いているとき").
CLAUDE_WAY_WORDS = {"Copilot CLI の機能": ["Copilot CLI"], "claude --agent": ["claude --agent"]}
NO_TOOL_EXPECTS = ("no_tool", "no_tool_claude")
NOTICE_WORDS = {"開始": ["開始"], "/clear": ["/clear"], "チャットで答える": ["チャットで答", "このチャットで"],
                "前もって許可（README）": ["README"]}
JAPANESE = re.compile(r"[ぁ-んァ-ン一-龥]")

FALLBACK_TEXTS = {  # used when node or the extension is not there (same words as core/messages.mjs)
    "description": "Coverity の警告を一括でトリアージする Web管理画面（設定・監視・結果）を開き、URL と、ユーザーに伝える注意を返します。"
                   "ユーザーの頼みの文を requestText にそのまま渡してください。画面の最初の条件は、頼みの文から拾います。"
                   "作業は、ユーザーが画面で「開始する」を押すと始まります。このツールを呼んだ後に、自分で作業をしたり、"
                   "エージェントを呼んだりしないでください。実行中に呼ぶと、新しい実行は作らず、今の画面の URL を返します。",
    "param_description": "ユーザーの頼みの文（書き換えずに、そのまま）",
    "notices": ["URL をブラウザーで開き、条件を確かめて「開始する」を押してください。",
                "実行中は /clear を使わないでください。実行が途中で切れます。",
                "実行中にエージェントが確認（ツールの許可、AI クレジットの上限、質問）を求めたときは、このチャットで答えてください。画面には、確認を待っていることが出ます。",
                "任せたいツール（コマンドの実行、ファイルの書き込み、Coverity との通信）を前もって許可するしかたは、プラグインの README にあります。"],
}
NODE_SNIPPET = ("const m = await import(process.argv[1]);"
                "console.log(JSON.stringify({description: m.OPEN_TOOL_DESCRIPTION, param_description: m.OPEN_PARAM_DESCRIPTION,"
                "notices: [m.OPEN_START_NOTICE, m.CLEAR_NOTICE, m.CHAT_CONFIRM_NOTICE, m.PREALLOW_NOTICE]}));")


def extension_texts(root: Path) -> tuple[dict, str]:
    """The open tool's description, parameter description and notices, from the extension when possible."""
    try:
        messages = ek.part(ROLE_EXTENSION, root)["path"] / "core" / "messages.mjs"
    except ek.EvalError:
        messages = None
    node = shutil.which("node")
    if messages is not None and messages.is_file() and node:
        res = ek.run_command([node, "--input-type=module", "-e", NODE_SNIPPET, messages.resolve().as_uri()], timeout=60)
        try:
            data = json.loads(res["stdout"])
            if all(isinstance(data.get(k), str) and data[k] for k in ("description", "param_description")) \
                    and all(isinstance(n, str) for n in data.get("notices") or []):
                return data, str(messages)
        except ValueError:
            pass
    return dict(FALLBACK_TEXTS), "（このファイルの写し）"


def fill(text: str, names: dict[str, str]) -> str:
    for key, value in names.items():
        text = text.replace("{" + key + "}", value)
    return text


def build_workspace(out: Path, root: Path, skills: list[Path]) -> Path:
    selftest = ek.part(ROLE_SELFTEST, root)["path"]
    ws = out / "ws"
    sample = selftest / "assets" / "sample-target"
    if sample.is_dir():
        ek.make_repo(sample, ws)
    else:
        ws.mkdir(parents=True, exist_ok=True)
    for folder in skills:
        ek.copy_tree(folder, ws / ".claude" / "skills" / folder.name)
    if (ws / ".git").exists():
        ek.exclude_in_git(ws, ".claude/")
    return ws


def build_argv(claude_bin: list[str], prompt: str, mcp_file: Path, model: str, max_turns: int) -> list[str]:
    return list(claude_bin) + ["-p", prompt, "--mcp-config", str(mcp_file), "--strict-mcp-config",
                               "--setting-sources", "project", "--tools", ",".join(TOOLS),
                               "--allowedTools", "Skill", ek.mcp_tool_name(SERVER_NAME, "*"),
                               "--model", model, "--max-turns", str(max_turns), "--no-session-persistence",
                               "--output-format", "stream-json", "--verbose"]


def read_calls(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def words_found(text: str, groups: dict[str, list[str]]) -> dict[str, bool]:
    low = (text or "").lower()
    return {name: any(w.lower() in low for w in words) for name, words in groups.items()}


def analyze(case: dict, run: dict, summary: dict, calls: list[dict], tool: str, names: dict[str, str]) -> list[dict]:
    """The checks of one case (see cases.json for the meaning of expect)."""
    text = ek.final_text(summary)
    expect = case["expect"]
    full = ek.mcp_tool_name(SERVER_NAME, tool)
    stream_calls = [u for u in summary["tool_uses"] if u["name"] == full]
    work = [u["name"] for u in summary["tool_uses"] if u["name"] in WORK_TOOLS]
    commands = [str(u["input"].get("command", ""))[:120] for u in summary["tool_uses"] if u["name"] == "Bash"]
    checks = [ek.check("finished", not run["timed_out"] and summary["has_result"] and not summary.get("is_error"),
                       ("時間切れ" if run["timed_out"] else "") + ("" if summary["has_result"] else " result の知らせなし")
                       or f"{summary.get('num_turns')} ターン")]
    want_skill = fill(case.get("expect_skill", ""), names)
    if expect == "open":
        n = max(len(calls), len(stream_calls))
        checks.append(ek.check("open_called_once", n == 1, f"{n} 回呼んだ"))
        got = (calls[0].get("arguments") or {}).get("requestText") if calls else None
        checks.append(ek.check("request_text_exact", got is not None and got.strip() == case["prompt"].strip(),
                               f"requestText: {got!r}" + ("（大文字・全角を除けば同じ）"
                                                          if got is not None and got.strip() != case["prompt"].strip()
                                                          and ek.normalize(got) == ek.normalize(case["prompt"]) else "")))
        checks.append(ek.check("entry_skill_used", names["screen"] in summary["skills_loaded"],
                               f"読んだスキル: {summary['skills_loaded'] or 'なし'}", "should"))
        checks.append(ek.check("url_shown", FAKE_URL in text, "URL を示した" if FAKE_URL in text else "URL を示していない", "should"))
        notices = words_found(text, NOTICE_WORDS)
        checks.append(ek.check("notices_shown", all(notices.values()),
                               "示していない注意: " + "、".join(k for k, v in notices.items() if not v) if not all(notices.values())
                               else "4つの注意を示した", "should"))
    elif expect == "not_open":
        n = max(len(calls), len(stream_calls))
        checks.append(ek.check("open_not_called", n == 0, f"{n} 回呼んだ"))
        if want_skill:
            checks.append(ek.check("expected_skill", want_skill in summary["skills_loaded"],
                                   f"読んだスキル: {summary['skills_loaded'] or 'なし'}（想定 {want_skill}）", "should"))
    elif expect == "no_tool":
        causes = words_found(text, CAUSES)
        checks.append(ek.check("three_causes", all(causes.values()),
                               "示していない原因: " + "、".join(k for k, v in causes.items() if not v) if not all(causes.values())
                               else "3つの原因を示した"))
        if want_skill:
            checks.append(ek.check("expected_skill", want_skill in summary["skills_loaded"],
                                   f"読んだスキル: {summary['skills_loaded'] or 'なし'}（想定 {want_skill}）", "should"))
    elif expect == "no_tool_claude":
        ways = words_found(text, CLAUDE_WAY_WORDS)
        run_name = names.get("run", "")
        ways[f"/{run_name}"] = bool(run_name) and (f"/{run_name}" in text or f":{run_name}" in text)
        checks.append(ek.check("claude_code_ways", all(ways.values()),
                               "示していないこと: " + "、".join(k for k, v in ways.items() if not v) if not all(ways.values())
                               else "Copilot CLI の機能であることと、Claude Code での2つのしかたを示した"))
        causes = words_found(text, CAUSES)
        checks.append(ek.check("no_copilot_causes", not any(causes.values()),
                               "示した Copilot CLI の原因: " + "、".join(k for k, v in causes.items() if v) if any(causes.values())
                               else "Copilot CLI の3つの原因を示していない"))
        if want_skill:
            checks.append(ek.check("expected_skill", want_skill in summary["skills_loaded"],
                                   f"読んだスキル: {summary['skills_loaded'] or 'なし'}（想定 {want_skill}）", "should"))
    if expect in ("open",) + NO_TOOL_EXPECTS:
        checks.append(ek.check("no_work", not work and not commands,
                               ("動かしたもの: " + "、".join(work + commands)) if work or commands
                               else "作業を始めていない（エージェント・コマンド・書き込みなし）"))
    checks.append(ek.check("same_language", bool(JAPANESE.search(text)) if JAPANESE.search(case["prompt"]) else True,
                           "日本語で答えた" if JAPANESE.search(text) else "日本語で答えていない", "should"))
    return checks


def select_cases(cases: list[dict], only: list[str] | None) -> list[dict]:
    if not only:
        return cases
    picked = []
    for word in only:
        for token in [w.strip() for w in word.split(",") if w.strip()]:
            hit = [c for i, c in enumerate(cases, 1) if c["name"] == token or str(i) == token]
            if not hit:
                raise ek.EvalError(f"課題 {token} がありません", [f"{i}. {c['name']}" for i, c in enumerate(cases, 1)])
            picked += [c for c in hit if c not in picked]
    return picked


def render_report(meta: dict, records: list[dict]) -> str:
    done = [r for r in records if r.get("passed") is not None]
    lines = ["# 4段階目（画面の入口の選ばれ方）の EVAL の結果", "",
             f"- 日時: {meta['started_at']}", f"- モデル: `{meta['model']}`",
             f"- スキル: {', '.join(meta['skills'])}（作業フォルダの .claude/skills/）",
             f"- 画面を開くツールの代わり: `{meta['tool']}`（stdio の MCP サーバー `{SERVER_NAME}`。説明と注意は {meta['texts_from']}）",
             f"- 読み替えの注意: Claude Code ではこのツールは `{ek.mcp_tool_name(SERVER_NAME, meta['tool'])}` に見える。"
             "Copilot CLI では、エクステンションのツールは接頭辞なしの名前で見える。呼ばれた回数と requestText は、"
             "サーバーの記録（calls.jsonl）で数える",
             "- コマンドは許可していない（試みは記録だけ）。AskUserQuestion はない（claude -p）", "",
             "## まとめ", "", f"- **合格**: {sum(1 for r in done if r['passed'])}/{len(done)}",
             f"- **費用の合計**: {ek.money(sum(r.get('cost_usd') or 0 for r in records))}", ""]
    lines += ek.md_table(["課題", "頼みの文", "想定", "開いた回数", "読んだスキル", "合否", "落ちた確かめ", "費用"],
                         [[r["name"], r["prompt"], r["expect"], r.get("open_calls", "－"), "、".join(r.get("skills_loaded") or []) or "－",
                           ek.mark(r.get("passed")), "、".join(c["id"] for c in r.get("checks", []) if c["ok"] is False) or "－",
                           ek.money(r.get("cost_usd"))] for r in records])
    for r in records:
        lines += ["", f"## {r['name']}", "", f"頼みの文: {r['prompt']}（想定 {r['expect']}）", ""]
        lines += ek.md_table(["確かめ", "重さ", "結果", "詳しく"],
                             [[c["id"], c["level"], ek.mark(c["ok"]), c["detail"]] for c in r.get("checks", [])])
        if r.get("reply"):
            lines += ["", "<details><summary>返事</summary>", "", "```text", r["reply"][:3000], "```", "", "</details>"]
    return "\n".join(lines) + "\n"


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="4段階目（画面の入口の選ばれ方）の EVAL を claude -p で動かす")
    p.add_argument("--plugin-root")
    p.add_argument("--cases", default=str(CASES_FILE))
    p.add_argument("--only", action="append", help="動かす課題の名前か番号（何度でも。カンマ区切りも可）")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"モデル（既定 {DEFAULT_MODEL}）")
    p.add_argument("--tool-name", help="画面を開くツールの名前（既定は <プラグインのフォルダの名前>_open）")
    p.add_argument("--timeout", type=float, default=240.0, help="1課題の時間の上限（秒）")
    p.add_argument("--max-turns", type=int, default=8)
    p.add_argument("--out", help="結果のフォルダ（既定 $TMPDIR/<プラグイン>-evals/stage4/<日時>）")
    p.add_argument("--build-only", action="store_true", help="作業フォルダとコマンドを作るだけ")
    p.add_argument("--claude-bin", nargs="+", help="claude の代わりに動かすコマンド（試験用）")
    return p


def run(args: argparse.Namespace) -> dict:
    root = Path(args.plugin_root).resolve() if args.plugin_root else ek.PLUGIN_ROOT
    screen, run_skill, help_skill = (ek.part(r, root) for r in (ROLE_SCREEN, ROLE_RUN, ROLE_HELP))
    names = {"screen": screen["name"], "run": run_skill["name"], "help": help_skill["name"]}
    tool = args.tool_name or f"{root.name}_open"
    cases = select_cases((ek.read_json(Path(args.cases)) or {}).get("cases") or [], args.only)
    if not cases:
        raise ek.EvalError(f"課題がありません: {args.cases}")
    out = ek.prepare_out(args.out, "stage4", root)
    texts, texts_from = extension_texts(root)
    ws = build_workspace(out, root, [screen["path"], run_skill["path"], help_skill["path"]])
    claude_bin = args.claude_bin or ["claude"]
    meta = {"started_at": datetime.now().isoformat(timespec="seconds"), "model": args.model, "tool": tool,
            "texts_from": texts_from, "skills": list(names.values()), "workspace": str(ws), "out": str(out)}
    records = []
    for case in cases:
        prompt = fill(case["prompt"], names)
        cdir = out / case["name"]
        calls_file = cdir / "calls.jsonl"
        cdir.mkdir(parents=True, exist_ok=True)
        servers = {}
        if case.get("mcp", True):
            server_config = ek.write_json(cdir / "open-server.json", {
                "tool": tool, "description": texts["description"], "param_description": texts["param_description"],
                "url": FAKE_URL, "notices": texts.get("notices") or [], "log": str(calls_file)})
            servers[SERVER_NAME] = {"type": "stdio", "command": sys.executable,
                                    "args": [str(SERVER_SCRIPT), "--config", str(server_config)],
                                    "env": {"PYTHONDONTWRITEBYTECODE": "1"}}
        mcp_file = ek.write_json(cdir / "mcp.json", {"mcpServers": servers})
        argv = build_argv(claude_bin, prompt, mcp_file, args.model, args.max_turns)
        record = {"name": case["name"], "prompt": prompt, "expect": case["expect"], "mcp": bool(servers),
                  "command": argv, "cwd": str(ws)}
        if args.build_only:
            record.update({"checks": [], "passed": None})
            records.append(record)
            continue
        ek.progress(TAG, f"{case['name']}: {prompt}")
        res = ek.run_claude(argv, ws, args.timeout, cdir / "transcript.jsonl")
        summary = ek.summarize(res["events"], prompt)
        calls = read_calls(calls_file)
        checks = analyze({**case, "prompt": prompt}, res, summary, calls, tool, names)
        record.update({"checks": checks, "passed": ek.verdict_of(checks), "open_calls": len(calls),
                       "request_texts": [(c.get("arguments") or {}).get("requestText") for c in calls],
                       "skills_loaded": summary["skills_loaded"], "tools": summary["main_tool_counts"],
                       "agents_called": summary["agents_called"], "reply": ek.final_text(summary)[:4000],
                       "seconds": res["seconds"], "cost_usd": summary.get("cost_usd"), "num_turns": summary.get("num_turns"),
                       "permission_denials": summary["permission_denials"][:10], "init": summary.get("init"),
                       "transcript": res["transcript"]})
        ek.write_json(cdir / "case.json", record)
        ek.progress(TAG, f"{case['name']}: {'合格' if record['passed'] else '不合格'}")
        records.append(record)
    report = ek.write_text(out / "report.md", render_report(meta, records))
    done = [r for r in records if r.get("passed") is not None]
    result = {"ok": True, "kind": "stage4", "out": str(out), "report": str(report), "meta": meta,
              "passed": sum(1 for r in done if r["passed"]), "total": len(done),
              "cost_usd": round(sum(r.get("cost_usd") or 0 for r in records), 6), "cases": records}
    ek.write_json(out / "results.json", result)
    return {**{k: v for k, v in result.items() if k != "cases"},
            "cases": [{"name": r["name"], "expect": r["expect"], "passed": r.get("passed"), "open_calls": r.get("open_calls"),
                       "skills_loaded": r.get("skills_loaded"),
                       "failed": [c["id"] for c in r.get("checks", []) if c["ok"] is False], "cost_usd": r.get("cost_usd"),
                       **({"command": r["command"]} if args.build_only else {})} for r in records]}


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        ek.emit({"ok": False, "error": "引数が正しくありません（--help で使い方を見られます）", "problems": []})
        return 1
    try:
        result = run(args)
    except ek.EvalError as exc:
        ek.emit({"ok": False, "error": str(exc), "problems": exc.problems})
        return 1
    except Exception as exc:  # noqa: BLE001 - never print a traceback
        ek.emit({"ok": False, "error": f"思わぬエラーが起きました: {type(exc).__name__}: {exc}", "problems": []})
        return 1
    ek.emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
