#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""EVAL stage 1 (MCP server): answer the qa_pairs of an mcp-builder evaluation.xml with ``claude -p``.

The model gets ONLY the MCP server (no built-in tools, no other MCP servers, no settings), and
mcp-builder's EVALUATION_PROMPT appended to the system prompt. For each task the answer in
<response> is compared with <answer> exactly (after strip), as mcp-builder's evaluation.py does.
We drive ``claude -p`` because the Anthropic SDK is not used here.

The server is started from the plugin's mcp.json command, with ``--directory`` pointing at a temp
copy of the server folder and the environment pointing at the fake Coverity data of the evals
(``COVERITY_API=fake``, ``COVERITY_FAKE_DATA=<copy>/evals/data/.coverity-triage/fake-issues.yaml``).
Tools whose annotations say destructiveHint are left out (the write-back), so the data stays clean.

  python3 tests/eval/run_mcp_eval.py [--eval-file F] [--server NAME] [--model M] [--only 1,3]
          [--timeout S] [--max-turns N] [--out DIR] [--allow-write] [--no-preflight] [--claude-bin EXE ...]

Writes <out>/report.md (the mcp-builder report, in Japanese), <out>/results.json and
<out>/transcripts/task-NN.jsonl. Prints one JSON object (summary) on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evalkit as ek  # noqa: E402

TAG = "mcp-eval"
DEFAULT_MODEL = "claude-sonnet-5-5"
DATA_REL = Path("evals") / "data" / ".coverity-triage" / "fake-issues.yaml"
EVAL_REL = Path("evals") / "evaluation.xml"

# Copied word for word from mcp-builder's scripts/evaluation.py (EVALUATION_PROMPT).
EVALUATION_PROMPT = """You are an AI assistant with access to tools.

When given a task, you MUST:
1. Use the available tools to complete the task
2. Provide summary of each step in your approach, wrapped in <summary> tags
3. Provide feedback on the tools provided, wrapped in <feedback> tags
4. Provide your final response, wrapped in <response> tags

Summary Requirements:
- In your <summary> tags, you must explain:
  - The steps you took to complete the task
  - Which tools you used, in what order, and why
  - The inputs you provided to each tool
  - The outputs you received from each tool
  - A summary for how you arrived at the response

Feedback Requirements:
- In your <feedback> tags, provide constructive feedback on the tools:
  - Comment on tool names: Are they clear and descriptive?
  - Comment on input parameters: Are they well-documented? Are required vs optional parameters clear?
  - Comment on descriptions: Do they accurately describe what the tool does?
  - Comment on any errors encountered during tool usage: Did the tool fail to execute? Did the tool return too many tokens?
  - Identify specific areas for improvement and explain WHY they would help
  - Be specific and actionable in your suggestions

Response Requirements:
- Your response should be concise and directly address what was asked
- Always wrap your final response in <response> tags
- If you cannot solve the task return <response>NOT_FOUND</response>
- For numeric responses, provide just the number
- For IDs, provide just the ID
- For names or text, provide the exact text requested
- Your response should go last"""


def parse_evaluation_file(path: Path) -> list[dict]:
    """The qa_pairs of an mcp-builder evaluation file: [{question, answer}]."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ek.EvalError(f"課題のファイルを読めません: {path}: {exc}") from exc
    pairs = []
    for qa in root.findall(".//qa_pair"):
        q, a = qa.find("question"), qa.find("answer")
        if q is not None and a is not None:
            pairs.append({"question": (q.text or "").strip(), "answer": (a.text or "").strip()})
    if not pairs:
        raise ek.EvalError(f"課題のファイルに qa_pair がありません: {path}")
    return pairs


def parse_only(values: list[str] | None, total: int) -> list[int]:
    """--only 1,3 --only 5 -> [1, 3, 5] (1-based task numbers)."""
    if not values:
        return list(range(1, total + 1))
    picked: list[int] = []
    for value in values:
        for piece in value.replace(" ", ",").split(","):
            if not piece:
                continue
            if not piece.isdigit() or not 1 <= int(piece) <= total:
                raise ek.EvalError(f"--only は 1〜{total} の課題の番号です（今は {piece}）")
            if int(piece) not in picked:
                picked.append(int(piece))
    return picked


def pick_server(root: Path, name: str | None) -> tuple[str, dict, Path]:
    layout = ek.ADAPTER.Layout(root)
    names = sorted(layout.mcp_entries)
    if not names:
        raise ek.EvalError(f"mcp.json に MCP サーバーの登録がありません: {root / 'mcp.json'}")
    if name is None:
        if len(names) != 1:
            raise ek.EvalError("MCP サーバーが複数あります。--server で選びます", names)
        name = names[0]
    if name not in layout.mcp_entries:
        raise ek.EvalError(f"mcp.json に {name} の登録がありません", names)
    folder = layout.mcp_folder(name)
    if folder is None:
        raise ek.EvalError(f"MCP サーバー {name} のフォルダがありません")
    return name, layout.mcp_entries[name], folder


def build_argv(claude_bin: list[str], question: str, mcp_config: Path, server: str, disallowed: list[str],
               model: str, max_turns: int) -> list[str]:
    # The prompt goes right after -p (--allowedTools and --disallowedTools take several values).
    argv = list(claude_bin) + ["-p", question, "--mcp-config", str(mcp_config), "--strict-mcp-config",
                               "--setting-sources", "project", "--tools", "",
                               "--allowedTools", ek.mcp_tool_name(server, "*")]
    if disallowed:
        argv += ["--disallowedTools", *[ek.mcp_tool_name(server, t) for t in disallowed]]
    argv += ["--append-system-prompt", EVALUATION_PROMPT, "--model", model, "--max-turns", str(max_turns),
             "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
    return argv


def score_task(number: int, pair: dict, run: dict, server: str) -> dict:
    summary = ek.summarize(run["events"])
    text = ek.final_text(summary)
    actual = ek.tag_content(text, "response")
    prefix = ek.mcp_tool_name(server, "")
    calls: dict[str, dict] = {}
    for use in summary["tool_uses"]:
        if use["parent"] is not None:
            continue
        name = use["name"][len(prefix):] if use["name"].startswith(prefix) else use["name"]
        entry = calls.setdefault(name, {"count": 0, "errors": 0})
        entry["count"] += 1
        entry["errors"] += int(use["is_error"])
    correct = actual is not None and actual.strip() == pair["answer"].strip()
    return {
        "task": number,
        "question": pair["question"],
        "expected": pair["answer"],
        "actual": actual,
        "score": int(correct),
        "near_match": actual is not None and not correct and ek.normalize(actual).casefold() == ek.normalize(pair["answer"]).casefold(),
        "total_duration": run["seconds"],
        "tool_calls": calls,
        "num_tool_calls": sum(c["count"] for c in calls.values()),
        "tool_errors": sum(c["errors"] for c in calls.values()),
        "summary": ek.tag_content(text, "summary"),
        "feedback": ek.tag_content(text, "feedback"),
        "cost_usd": summary.get("cost_usd"),
        "num_turns": summary.get("num_turns"),
        "exit_code": run["exit_code"],
        "timed_out": run["timed_out"],
        "is_error": summary.get("is_error"),
        "tools_offered": summary.get("init", {}).get("tools"),
        "mcp_servers": summary.get("init", {}).get("mcp_servers"),
        "transcript": run["transcript"],
        "problems": ([] if summary.get("has_result") else ["claude の result の知らせがありません（途中で終わった）"])
                    + (["時間切れ"] if run["timed_out"] else []),
    }


def render_report(meta: dict, results: list[dict]) -> str:
    total = len(results)
    correct = sum(r["score"] for r in results)
    accuracy = correct / total * 100 if total else 0.0
    avg_s = sum(r["total_duration"] for r in results) / total if total else 0.0
    calls = sum(r["num_tool_calls"] for r in results)
    errors = sum(r["tool_errors"] for r in results)
    cost = sum(r["cost_usd"] or 0 for r in results)
    lines = ["# MCP サーバーの EVAL の結果", "",
             f"- 日時: {meta['started_at']}",
             f"- モデル: `{meta['model']}`",
             f"- 課題: `{meta['eval_file']}`（{meta['tasks_total']} 問のうち {total} 問）",
             f"- MCP サーバー: `{meta['server']}`（フォルダのコピー `{meta['server_copy']}`、偽の Coverity `{meta['fake_data']}`）",
             f"- 渡したツール: MCP サーバーのツールだけ（外したツール: {', '.join(meta['disallowed']) or 'なし'}）",
             "", "## まとめ", "",
             f"- **正解**: {correct}/{total}（{accuracy:.1f}%）",
             f"- **1問の平均の時間**: {avg_s:.2f} 秒",
             f"- **1問の平均のツールの呼び出し**: {(calls / total if total else 0):.2f}",
             f"- **ツールの呼び出しの合計**: {calls}（エラー {errors}）",
             f"- **費用の合計**: {ek.money(cost)}", ""]
    lines += ek.md_table(["#", "正解", "期待", "答え", "時間（秒）", "呼び出し", "エラー", "費用"],
                         [[r["task"], ek.mark(bool(r["score"])), r["expected"], r["actual"] or "N/A",
                           f"{r['total_duration']:.1f}", r["num_tool_calls"], r["tool_errors"], ek.money(r["cost_usd"])]
                          for r in results])
    lines += ["", "---"]
    for r in results:
        lines += ["", f"### 課題 {r['task']}", "",
                  f"**問い**: {r['question']}", "",
                  f"**期待する答え**: `{r['expected']}`", "",
                  f"**答え**: `{r['actual'] or 'N/A'}`" + ("（大文字・全角を除けば同じ）" if r["near_match"] else ""), "",
                  f"**正解**: {ek.mark(bool(r['score']))}", "",
                  f"**時間**: {r['total_duration']:.2f} 秒、費用 {ek.money(r['cost_usd'])}", "",
                  "**ツールの呼び出し**:", "", "```json", json.dumps(r["tool_calls"], ensure_ascii=False, indent=2), "```", ""]
        if r["problems"]:
            lines += [f"**問題**: {'、'.join(r['problems'])}", ""]
        lines += ["**要約（summary）**", "", r["summary"] or "N/A", "",
                  "**ツールへの意見（feedback）**", "", r["feedback"] or "N/A", "", "---"]
    return "\n".join(lines) + "\n"


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MCP サーバーの EVAL（mcp-builder の evaluation.xml）を claude -p で動かす")
    p.add_argument("--plugin-root", help="プラグインのフォルダ（既定はこのファイルの2つ上）")
    p.add_argument("--server", help="mcp.json の登録名（1つだけなら省ける）")
    p.add_argument("--eval-file", help="課題のファイル（既定は <サーバーのフォルダ>/evals/evaluation.xml）")
    p.add_argument("--fake-data", help="偽の Coverity の警告のファイル（既定は evals/data の fake-issues.yaml）")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"モデル（既定 {DEFAULT_MODEL}）")
    p.add_argument("--only", action="append", help="動かす課題の番号（1 から。1,3 のようにも書ける）")
    p.add_argument("--timeout", type=float, default=300.0, help="1問の時間の上限（秒）")
    p.add_argument("--max-turns", type=int, default=30)
    p.add_argument("--out", help="結果のフォルダ（既定 $TMPDIR/<プラグイン>-evals/mcp/<日時>）")
    p.add_argument("--allow-write", action="store_true", help="書き戻しのツール（destructiveHint）も渡す")
    p.add_argument("--no-preflight", action="store_true", help="uv sync とツールの一覧の確かめをしない")
    p.add_argument("--claude-bin", nargs="+", help="claude の代わりに動かすコマンド（試験用）")
    return p


def run(args: argparse.Namespace) -> dict:
    root = Path(args.plugin_root).resolve() if args.plugin_root else ek.PLUGIN_ROOT
    server, entry, folder = pick_server(root, args.server)
    eval_file = Path(args.eval_file).resolve() if args.eval_file else folder / EVAL_REL
    pairs = parse_evaluation_file(eval_file)
    numbers = parse_only(args.only, len(pairs))
    out = ek.prepare_out(args.out, "mcp", root)
    started_at = datetime.now().isoformat(timespec="seconds")

    copy = ek.copy_tree(folder, out / "server" / folder.name)
    if args.fake_data:
        fake_src = Path(args.fake_data).resolve()
        data_dir = fake_src.parent.parent if fake_src.parent.name == ".coverity-triage" else fake_src.parent
    else:
        fake_src = copy / DATA_REL
        data_dir = copy / "evals" / "data"
    if not fake_src.is_file():
        raise ek.EvalError(f"偽の Coverity の警告のファイルがありません: {fake_src}")
    # The model works in a copy of the data folder (its .coverity-triage/config.yaml says api: fake),
    # so a repo_root of the working folder works too, and write logs stay out of the plugin.
    cwd = ek.copy_tree(data_dir, out / "data")
    fake = cwd / fake_src.relative_to(data_dir) if ek.inside(fake_src, data_dir) else fake_src
    config, problems = ek.ADAPTER.mcp_server_config(server, copy, entry, {}, fake)
    mcp_file = ek.write_json(out / "mcp.json", {"mcpServers": {server: config}})

    preflight: dict = {"skipped": True}
    disallowed: list[str] = []
    if not args.no_preflight:
        ek.progress(TAG, f"uv sync {copy}")
        sync = ek.uv_sync(copy)
        if sync["code"] != 0:
            raise ek.EvalError("MCP サーバーの用意（uv sync）が失敗しました", [sync["stderr"][-1500:]])
        preflight = ek.mcp_list_tools(config, cwd=cwd)
        if not preflight["ok"]:
            raise ek.EvalError(f"MCP サーバーが始まりません: {preflight.get('error')}")
        if not args.allow_write:
            disallowed = [t["name"] for t in preflight["tools"] if t["annotations"].get("destructiveHint")]
    claude_bin = args.claude_bin or ["claude"]

    results = []
    for n in numbers:
        pair = pairs[n - 1]
        ek.progress(TAG, f"課題 {n}/{len(pairs)}: {pair['question'][:60]}")
        argv = build_argv(claude_bin, pair["question"], mcp_file, server, disallowed, args.model, args.max_turns)
        res = ek.run_claude(argv, cwd, args.timeout, out / "transcripts" / f"task-{n:02d}.jsonl")
        result = score_task(n, pair, res, server)
        ek.progress(TAG, f"課題 {n}: {'正解' if result['score'] else '不正解'}（答え {result['actual']!r}、期待 {pair['answer']!r}）")
        results.append(result)

    meta = {"started_at": started_at, "model": args.model, "eval_file": str(eval_file), "tasks_total": len(pairs),
            "server": server, "server_copy": str(copy), "fake_data": str(fake), "disallowed": disallowed,
            "preflight": preflight, "config_problems": problems, "cwd": str(cwd)}
    report = ek.write_text(out / "report.md", render_report(meta, results))
    correct = sum(r["score"] for r in results)
    data = {"ok": True, "kind": "mcp", "out": str(out), "report": str(report), "meta": meta,
            "correct": correct, "total": len(results),
            "accuracy": round(correct / len(results) * 100, 1) if results else 0.0,
            "cost_usd": round(sum(r["cost_usd"] or 0 for r in results), 6),
            "seconds": round(sum(r["total_duration"] for r in results), 1), "results": results}
    ek.write_json(out / "results.json", data)
    return {k: v for k, v in data.items() if k != "results"} | {
        "tasks": [{"task": r["task"], "score": r["score"], "actual": r["actual"], "expected": r["expected"],
                   "num_tool_calls": r["num_tool_calls"], "seconds": r["total_duration"], "cost_usd": r["cost_usd"]}
                  for r in results]}


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
