# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""The results and settings screen: a local web page for one repository.

    gui.py open --repo R [--run D] [--view monitor|summary|settings] [--item ID] [--no-browser]
        -> {"ok", "url", "reused", "browser_opened", "pid", "note"?}
    gui.py stop --repo R                      -> {"ok", "stopped"}
    gui.py yaml-edit --stdin                  -> {"ok", "text", "notice"}  (input {"original", "values"})
    gui.py serve --repo R --ready-file F      (the detached server that ``open`` starts)

Every command prints one JSON object; a failure is {"ok": false, "error", "problems"?} with exit code 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from guilib.common import GuiError, emit  # noqa: E402


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        usage = " ".join(self.format_usage().split()).removeprefix("usage:").strip()
        raise UsageError(f"引数が正しくありません（{message}）。使い方: {usage}")


def parser() -> argparse.ArgumentParser:
    p = Parser(prog="gui.py", description="結果と設定の画面（ブラウザーで開くローカルの画面）")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    opened = sub.add_parser("open", help="画面を開く（動いていれば使い回す）")
    opened.add_argument("--repo", required=True, help="調べるリポジトリの一番上のフォルダ")
    opened.add_argument("--run", help="実行フォルダ（または実行の ID）")
    opened.add_argument("--view", choices=["monitor", "summary", "settings"], help="開くタブ")
    opened.add_argument("--item", help="結果のタブで開く作業の ID（CID か G1 など）")
    opened.add_argument("--no-browser", action="store_true", help="ブラウザーを開かず、URL だけを返す")
    stop = sub.add_parser("stop", help="このリポジトリの画面のサーバを止める")
    stop.add_argument("--repo", required=True)
    edit = sub.add_parser("yaml-edit", help="注釈を残して YAML を書き換えた結果を返す（ファイルは書かない）")
    edit.add_argument("--stdin", action="store_true", required=True, help='標準入力に {"original": ..., "values": ...}')
    serve = sub.add_parser("serve", help="（内部）画面のサーバそのもの")
    serve.add_argument("--repo", required=True)
    serve.add_argument("--ready-file")
    serve.add_argument("--port", type=int, default=0)
    return p


def _yaml_edit() -> dict[str, Any]:
    from guilib import yamledit

    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except ValueError as exc:
        raise GuiError(f"標準入力を JSON として読めません: {exc}") from None
    original, values = (data.get("original"), data.get("values")) if isinstance(data, dict) else (None, None)
    if not isinstance(original, str) or not isinstance(values, dict):
        raise GuiError('標準入力は {"original": "<YAML の文字>", "values": {<保存したい設定のすべて>}} の形にします')
    text, notice = yamledit.rewrite(original, values)
    return {"ok": True, "text": text, "notice": notice}


def run(argv: list[str]) -> dict[str, Any]:
    args = parser().parse_args(argv)
    from guilib import launch

    if args.command == "open":
        return launch.open_screen(args.repo, args.run, args.view, args.item, browser=not args.no_browser)
    if args.command == "stop":
        return launch.stop_screen(args.repo)
    if args.command == "yaml-edit":
        return _yaml_edit()
    return launch.serve(args.repo, args.ready_file, args.port)


def main(argv: list[str]) -> int:
    try:
        out = run(argv)
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except UsageError as exc:
        out = {"ok": False, "error": str(exc)}
    except GuiError as exc:
        out = {"ok": False, "error": str(exc), **exc.details}
    except OSError as exc:
        out = {"ok": False, "error": f"ファイルやコマンドの操作に失敗しました: {exc}"}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        out = {"ok": False, "error": f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}"}
    emit(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
