# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0", "pydantic>=2.7"]
# ///
"""Scripts of the Coverity triage plugin: the fixed steps that need no judgement.

Run with ``uv run <this folder>/ct.py <command> ...``. Every command prints one JSON object.
``"ok": false`` (exit code 1) means the command could not do its work; ``error`` says why and
``problems`` lists what to fix. Usage of each command: skill coverity-triage-scripts (SKILL.md).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, NoReturn

from ctlib import apply, flow, gui, initial, knowledge, metrics, prepare, selftest, triage
from ctlib.common import CtError, resolve_path
from ctlib.config import load_config, output_dir


class UsageError(Exception):
    """A wrong command line. It is printed as one JSON object, like every other failure."""


class Parser(argparse.ArgumentParser):
    """argparse that raises UsageError instead of printing the usage to stderr and exiting with 2.
    The subcommands are made with the same class. ``--help`` still prints the help and exits with 0."""

    def error(self, message: str) -> NoReturn:
        usage = " ".join(self.format_usage().split())
        usage = usage.split(":", 1)[1].strip() if usage.lower().startswith("usage:") else usage
        raise UsageError(f"引数が正しくありません（{message}）。使い方: {usage}（くわしくは {self.prog} --help）")


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("checkers", "impacts"):
        if getattr(args, key):
            out[key] = getattr(args, key)
    triage_filter = {k: getattr(args, k) for k in ("status", "classification", "action") if getattr(args, k)}
    if triage_filter:
        out["triage"] = triage_filter
    return out


def _stats(repo_root: str) -> dict[str, Any]:
    repo = resolve_path(repo_root)
    return {"ok": True, **metrics.aggregate(output_dir(repo, load_config(repo)))}


def parser() -> argparse.ArgumentParser:
    p = Parser(prog="ct.py", description="Coverity トリアージのスクリプト")
    sub = p.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, *options: tuple[str, dict[str, Any]]) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, help=help_text)
        for flag, kwargs in options:
            cmd.add_argument(flag, **kwargs)
        return cmd

    repo = ("--repo", {"required": True, "help": "対象リポジトリの一番上のフォルダ"})
    run = ("--run", {"required": True, "help": "実行フォルダ（new-run が返す run_dir）"})
    item = ("--item", {"required": True, "help": "作業の ID（CID か G1 など）"})

    # setup
    command("prewarm", "MCP サーバの Python 環境を作る")
    command("doctor", "準備の状況を調べる", repo)
    command("detect", "リポジトリから決められる値を調べる", repo)
    command("init-config", "設定・条件・知識のファイルを作る", repo,
            ("--url", {"required": True}), ("--project", {"required": True}), ("--stream", {"required": True}),
            ("--base-branch", {}), ("--output-dir", {}),
            ("--strip-prefix", {"action": "append", "default": [], "dest": "strip_prefixes"}),
            ("--overwrite", {"action": "store_true"}))
    command("install-agent", "サブエージェント（取得・作業・統合・検証）の定義を ~/.copilot/agents"
                             "（環境変数 COPILOT_HOME があればその agents）に置き、無くなった古い定義を消す")
    command("trial-build", "（オプション）修正前の最新のコードを試しにビルドする", repo,
            ("--build-command", {"required": True}), ("--setup-command", {"default": ""}),
            ("--build-dir", {"default": ""}))
    command("set-verify", "（オプション）ビルドでの検証と初回解析の設定を書く", repo,
            ("--build-command", {"required": True}), ("--setup-command", {"default": ""}),
            ("--build-dir", {"default": ""}),
            ("--default", {"default": "build", "choices": ["none", "build", "build+analyze"]}),
            ("--cov-build-args", {}), ("--cov-analyze-args", {}),
            ("--initial", {"action": argparse.BooleanOptionalAction, "default": None,
                           "help": "/coverity-run の最初に解析してサーバに登録する（--no-initial でしない。省略時は今の設定のまま）"}),
            ("--cov-commit-args", {"help": "初回解析の cov-commit-defects に足す引数（省略時は今の設定のまま）"}),
            ("--timeout-minutes", {"type": int, "help": "初回解析を止めるまでの分（省略時は今の設定のまま）"}),
            ("--check", {"action": "store_true",
                         "help": "書かずに、config.yaml の注釈を元の場所に残せるか（comments: kept / lost）だけを返す"}))
    command("gui", "設定・結果・監視の画面をブラウザで開く（裏で動かし、すぐ戻る。動いていれば再利用する）", repo,
            ("--run", {"help": "開く実行フォルダ（new-run が返す run_dir）"}),
            ("--view", {"choices": list(gui.VIEWS), "help": "最初に出す画面（既定: --run があれば monitor）"}),
            ("--foreground", {"action": "store_true",
                              "help": "「サーバを止める」を押すまで、このコマンドで動かす（動いている画面は止めて置き換える）"}),
            ("--no-browser", {"action": "store_true", "help": "ブラウザを開かずにアドレスだけ返す"}),
            ("--serve", {"action": "store_true", "help": argparse.SUPPRESS}),
            ("--ready-file", {"help": argparse.SUPPRESS}))

    # run
    command("runs", "実行の一覧（新しい順）", repo, ("--limit", {"type": int, "default": 10}))
    command("new-run", "実行フォルダを作り、条件を決める", repo,
            ("--filter", {"help": "条件ファイル（filters/ の名前かパス）"}),
            ("--checker", {"action": "append", "dest": "checkers"}),
            ("--impact", {"action": "append", "dest": "impacts"}),
            ("--status", {"action": "append"}), ("--classification", {"action": "append"}),
            ("--action", {"action": "append"}), ("--limit", {"type": int}),
            ("--verify", {"choices": ["none", "build", "build+analyze"]}),
            ("--initial", {"action": argparse.BooleanOptionalAction, "default": None,
                           "help": "最初に解析してサーバに登録する（--no-initial でしない。省略時は設定 verify.initial）"}))
    command("tick", "次にすることを返す（エージェントが終わったら --finished で知らせる）", run,
            ("--finished", {"help": "終わったエージェントの task（worker:<ID>、collector:search など）"}),
            ("--failed", {"help": "途中で止まった・起動できなかったエージェントの task"}),
            ("--reason", {"default": "", "help": "--failed の理由"}),
            ("--wait", {"type": int, "default": flow.WAIT_SECONDS, "help": "初回解析を待つ最長の秒数"}))
    command("initial-status", "初回解析の状況", run)
    command("initial-run", "（内部用）初回解析の本体。ct.py tick が裏で動かす", run)
    command("plan", "検索結果から作業の一覧を作る", run)
    command("next", "次の作業を取り出し、修正用のコピーを用意する", run)
    command("brief", "サブエージェントへの指示ファイルを作る", run, item)
    command("finish", "結果を確かめ、差分・ブランチ・詳細レポートを作る", run, item)
    command("fail", "作業を処理できなかったと記録する", run, item, ("--reason", {"required": True}))
    command("resume", "止まった実行を再開できるようにする", run)
    command("status", "実行の進み具合", run)
    command("verify", "（オプション）修正案をまとめてビルドで確かめる", run,
            ("--mode", {"choices": ["none", "build", "build+analyze"]}))
    command("summary", "一覧 summary.html を作る", run)

    # apply
    command("preview", "反映の内容と確認用の文字列を作る", run)
    command("apply-code", "修正を反映する（git: push、svn: 差分の適用）", run, ("--token", {"required": True}))
    command("knowledge-candidates", "（オプション）知識の追記の候補", run)
    command("add-knowledge", "（オプション）知識を追記する", run,
            ("--entry", {"action": "append", "required": True, "dest": "entries"}))
    command("stats", "（オプション）効果測定の集計", repo)

    # self-test
    selftest.add_commands(sub)
    return p


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    c = args.command
    if c == "prewarm":
        return prepare.prewarm()
    if c == "doctor":
        return prepare.doctor(args.repo)
    if c == "detect":
        return prepare.detect(args.repo)
    if c == "init-config":
        return prepare.init_config(args.repo, args.url, args.project, args.stream, args.base_branch,
                                   args.output_dir, args.strip_prefixes, args.overwrite)
    if c == "install-agent":
        return prepare.install_agent()
    if c == "trial-build":
        return prepare.trial_build(args.repo, args.setup_command, args.build_command, args.build_dir)
    if c == "set-verify":
        return prepare.set_verify(args.repo, args.default, args.setup_command, args.build_command, args.build_dir,
                                  args.cov_build_args, args.cov_analyze_args, args.initial, args.cov_commit_args,
                                  args.timeout_minutes, args.check)
    if c == "gui":
        if args.serve:  # the token is not passed on to the commands the screen runs (trial build)
            return gui.serve(args.repo, os.environ.pop(gui.TOKEN_ENV, None), args.ready_file)
        if args.foreground:
            return gui.serve(args.repo, open_browser=not args.no_browser, run_dir=args.run, view=args.view)
        return gui.launch(args.repo, open_browser=not args.no_browser, run_dir=args.run, view=args.view)
    if c == "runs":
        return triage.runs(args.repo, args.limit)
    if c == "new-run":
        return triage.new_run(args.repo, args.filter, _overrides(args), args.limit, args.verify, args.initial)
    if c == "tick":
        return flow.tick(args.run, args.finished, args.failed, args.reason, args.wait)
    if c == "initial-status":
        from ctlib.run import Run

        return {"ok": True, **initial.status(Run(args.run))}
    if c == "initial-run":
        return {"ok": True, **initial.execute(args.run)}
    if c == "plan":
        return triage.plan(args.run)
    if c == "next":
        return triage.next_item(args.run)
    if c == "brief":
        return triage.brief(args.run, args.item)
    if c == "finish":
        return triage.finish(args.run, args.item)
    if c == "fail":
        return triage.fail(args.run, args.item, args.reason)
    if c == "resume":
        return triage.resume(args.run)
    if c == "status":
        return triage.status(args.run)
    if c == "verify":
        return triage.verify(args.run, args.mode)
    if c == "summary":
        return triage.summary(args.run)
    if c == "preview":
        return apply.preview(args.run)
    if c == "apply-code":
        return apply.apply_code(args.run, args.token)
    if c == "knowledge-candidates":
        return knowledge.candidates(args.run)
    if c == "add-knowledge":
        return knowledge.add(args.run, args.entries)
    if c == "stats":
        return _stats(args.repo)
    return selftest.dispatch(args)


def main(argv: list[str] | None = None) -> int:
    # Never stop on a character the console cannot show.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        args = parser().parse_args(argv)
    except UsageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=1))
        return 1

    def log_failure(failed: dict[str, Any]) -> None:
        """Record a failed step of a run in its operations.log, for the monitor of ``ct.py gui``.

        Problems in the result file are for the subagent to fix (warning); anything else stops the
        work (critical). Recording never changes what the command prints or returns."""
        if not getattr(args, "run", None) or args.command in ("gui", "selftest"):
            return
        try:
            from ctlib import oplog
            from ctlib.run import Run

            run = Run(args.run)
            try:
                secrets = oplog.secrets_of(run.config)
            except Exception:  # noqa: BLE001 - the settings may be what failed
                secrets = []
            level = "warning" if args.command == "finish" and failed.get("problems") else "critical"
            fields = {"command": args.command, "error": failed.get("error", ""),
                      "problems": failed.get("problems") or []}
            if getattr(args, "item", None):
                fields["item"] = args.item
            oplog.log(run.dir, "error", secrets, level=level, **fields)
        except Exception:  # noqa: BLE001 - the log is best effort
            pass

    try:
        result = dispatch(args)
        code = 0 if result.get("ok", True) else 1
    except CtError as exc:
        result, code = {"ok": False, "error": str(exc), **exc.details}, 1
    except OSError as exc:
        result, code = {"ok": False, "error": f"ファイルやコマンドの操作に失敗しました: {exc}"}, 1
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback on the output
        result, code = {"ok": False, "error": f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}"}, 1
    if code:
        log_failure(result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
