# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0", "jsonschema>=4.18,<5"]
# ///
"""The batch triage of Coverity warnings: the run folder and the order of the work.

Run with ``uv run --native-tls --quiet <this folder>/run.py <command> ...``. Every command prints one
JSON object on stdout. ``"ok": false`` (exit code 1) means the command could not do its work:
``error`` says why and ``problems`` lists what to fix. Progress goes to stderr.

Commands: settings, runs, new-run, resume, tick, status, summary, stop, note-issue, initial-status
(and job-run, which the scripts start themselves).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

# The compiled modules go to a cache in the temporary folder, never into the skill's folder (which may be
# read-only, and is often inside a repository); each command then starts without compiling the scripts.
sys.pycache_prefix = str(Path(tempfile.gettempdir()) / f"{Path(__file__).resolve().parents[1].name}-pycache")
sys.dont_write_bytecode = False

from runlib.common import RunError, emit_json, read_stdin_text  # noqa: E402


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        usage = " ".join(self.format_usage().split())
        usage = usage.split(":", 1)[1].strip() if usage.lower().startswith("usage:") else usage
        raise UsageError(f"引数が正しくありません（{message}）。使い方: {usage}")


def _bool_option(cmd: argparse.ArgumentParser, name: str, help_text: str) -> None:
    group = cmd.add_mutually_exclusive_group()
    group.add_argument(f"--{name}", dest=name.replace("-", "_"), action="store_true", default=None, help=help_text)
    group.add_argument(f"--no-{name}", dest=name.replace("-", "_"), action="store_false", help=f"{help_text}（しない）")


def parser() -> argparse.ArgumentParser:
    p = Parser(prog="run.py", description="Coverity の警告の一括のトリアージ（実行フォルダと、次にすること）")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    repo = {"required": True, "help": "調べるリポジトリの一番上のフォルダ"}
    run = {"required": True, "help": "実行フォルダ（new-run が返す run_dir）"}

    sub.add_parser("settings", help="設定の今の値と条件ファイルの一覧（読むだけ）").add_argument("--repo", **repo)
    cmd = sub.add_parser("runs", help="実行の一覧（新しい順）")
    cmd.add_argument("--repo", **repo)
    cmd.add_argument("--limit", type=int, default=20)

    cmd = sub.add_parser("new-run", help="実行フォルダを作る")
    cmd.add_argument("--repo", **repo)
    cmd.add_argument("--filter", help="条件ファイル（filters/ の名前かパス）")
    for flag, dest in (("--impact", "impacts"), ("--checker", "checkers"), ("--status", "status"),
                       ("--classification", "classification"), ("--action", "action")):
        cmd.add_argument(flag, action="append", dest=dest)
    cmd.add_argument("--limit", type=int)
    cmd.add_argument("--verify", choices=["none", "build", "build+analyze"])
    _bool_option(cmd, "initial", "最初に解析してサーバに登録する")
    _bool_option(cmd, "review", "結果の確かめ（検証役）を使う")
    _bool_option(cmd, "integration", "同じ原因の統合（統合役）を使う")
    cmd.add_argument("--max-rework", type=int, dest="max_rework")
    cmd.add_argument("--issues-file", dest="issues_file", help="取ってある警告の一覧（issues.json の形）で始める")
    mcp = {"dest": "mcp_tools", "action": "append",
           "help": "許可の案内に書く、Coverity から読むツール（Copilot CLI は <登録名>(<ツール>)、Claude Code は "
                   "mcp__<登録名>__<ツール> か mcp__plugin_<プラグイン>_<登録名>__<ツール>。書き戻すツールは渡さない）"}
    client = {"choices": ["copilot", "claude-code"], "default": "copilot",
              "help": "許可の案内の形（動かしている道具。既定 copilot）"}
    cmd.add_argument("--mcp-tool", **mcp)
    cmd.add_argument("--client", **client)

    cmd = sub.add_parser("resume", help="途中の実行を続きからにする")
    cmd.add_argument("--run", **run)
    cmd.add_argument("--mcp-tool", **mcp)
    cmd.add_argument("--client", **client)
    cmd = sub.add_parser("tick", help="次にすることを返す")
    cmd.add_argument("--run", **run)
    cmd.add_argument("--finished", help="終わった task")
    cmd.add_argument("--failed", help="失敗した task")
    cmd.add_argument("--reason", default="", help="--failed の理由")
    cmd.add_argument("--wait", type=int, default=20, help="待つ最長の秒数（25 まで）")
    sub.add_parser("status", help="実行の様子（読むだけ）").add_argument("--run", **run)
    sub.add_parser("summary", help="一覧と承認の案を作る").add_argument("--run", **run)
    sub.add_parser("stop", help="切り離した処理を止め、実行を止めた印をつける").add_argument("--run", **run)
    cmd = sub.add_parser("note-issue", help="振り返りの課題を書き残す")
    cmd.add_argument("--run", **run)
    cmd.add_argument("--title")
    cmd.add_argument("--body")
    cmd.add_argument("--stdin", action="store_true", help='標準入力から {"title": ..., "body": ...} を読む')
    sub.add_parser("initial-status", help="初回解析の段と進み").add_argument("--run", **run)
    cmd = sub.add_parser("job-run", help=argparse.SUPPRESS)
    cmd.add_argument("--run", **run)
    cmd.add_argument("--name", required=True)
    return p


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("impacts", "checkers", "status", "classification", "action"):
        if getattr(args, key):
            out[key] = getattr(args, key)
    return out


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    from runlib import flow, jobs, views

    c = args.command
    if c == "settings":
        return views.settings(args.repo)
    if c == "runs":
        return views.runs(args.repo, args.limit)
    if c == "new-run":
        return views.new_run(args.repo, args.filter, _overrides(args), args.limit, args.verify, args.initial,
                             args.review, args.integration, args.max_rework, args.issues_file, args.mcp_tools, args.client)
    if c == "resume":
        return views.resume(args.run, args.mcp_tools, args.client)
    if c == "tick":
        if args.finished and args.failed:
            raise RunError("--finished と --failed は一緒に使えません")
        return flow.tick(args.run, args.finished, args.failed, args.reason, args.wait)
    if c == "status":
        return views.status(args.run)
    if c == "summary":
        return views.summary(args.run)
    if c == "stop":
        return views.stop(args.run)
    if c == "note-issue":
        title, body = args.title, args.body
        if args.stdin:
            try:
                data = json.loads(read_stdin_text() or "{}")
            except ValueError as exc:
                raise RunError(f"標準入力の JSON を読めません: {exc}") from None
            if not isinstance(data, dict):
                raise RunError('標準入力には {"title": "…", "body": "…"} を渡してください')
            title, body = data.get("title", title), data.get("body", body)
        return views.note_issue(args.run, str(title or ""), str(body or ""))
    if c == "initial-status":
        return views.initial_status(args.run)
    if c == "job-run":
        return {"ok": True, **jobs.execute(args.run, args.name)}
    raise UsageError(f"知らないコマンドです: {c}")


def _log_failure(args: argparse.Namespace, result: dict[str, Any]) -> None:
    """A failed command of a run is written to its operations.log (for the screens)."""
    if not getattr(args, "run", None) or args.command in ("status", "initial-status", "job-run"):
        return
    if result.get("foreign") or result.get("run_tool_version"):
        return
    try:
        from runlib.state import Run

        Run(args.run).log("error", f"{args.command} がうまくいかなかった: {result.get('error', '')}",
                          level="critical", command=args.command, problems=result.get("problems") or [])
    except Exception:  # noqa: BLE001 - the log is best effort
        pass


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        args = parser().parse_args(argv)
    except UsageError as exc:
        emit_json({"ok": False, "error": str(exc), "problems": []})
        return 1
    try:
        result = dispatch(args)
        code = 0 if result.get("ok", True) else 1
    except UsageError as exc:
        result, code = {"ok": False, "error": str(exc), "problems": []}, 1
    except RunError as exc:
        result, code = {"ok": False, "error": str(exc), "problems": [], **exc.details}, 1
    except OSError as exc:
        result, code = {"ok": False, "error": f"ファイルかコマンドの操作に失敗しました: {exc}", "problems": []}, 1
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback on stdout
        result, code = {"ok": False, "error": f"思いがけないエラーで止まりました: {type(exc).__name__}: {exc}",
                        "problems": []}, 1
    if code:
        _log_failure(args, result)
    emit_json(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
