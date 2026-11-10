#!/bin/bash
# CLI のフックの設定（hooks.json）から呼ばれ、hook.py を動かす。
# Claude Code と、Windows 以外の Copilot CLI が使う。Windows の Copilot CLI は hook.ps1 を使う。
#
#   bash hook.sh <イベント> < フックの入力（JSON）
#
# 記録中フラグ（recording）がなければ、uv を起動せずに終える（REQ-408）。
# uv が見つからないときも、hook.py が失敗したときも、0 で終える。
[ -f "${CLAUDE_PROJECT_DIR:-${COPILOT_PROJECT_DIR:-.}}/.debug-dashboard/recording" ] || exit 0
script="${BASH_SOURCE[0]%/*}/hook.py"
uv run --project "${BASH_SOURCE[0]%/*}/.." "$script" "$1" 2>/dev/null
exit 0
