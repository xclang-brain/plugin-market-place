#!/bin/bash
# CLI のフックの設定（hooks.json）から呼ばれ、hook.py を動かす。
# Claude Code と、Windows 以外の Copilot CLI が使う。Windows の Copilot CLI は hook.ps1 を使う。
#
#   bash hook.sh <イベント> < フックの入力（JSON）
#
# 記録中フラグ（recording）がなければ、Python を起動せずに終える（REQ-408）。
# Python が見つからないときも、hook.py が失敗したときも、0 で終える。
[ -f "${CLAUDE_PROJECT_DIR:-${COPILOT_PROJECT_DIR:-.}}/.debug-dashboard/recording" ] || exit 0
script="${BASH_SOURCE[0]%/*}/hook.py"
if command -v py >/dev/null 2>&1; then py -3 "$script" "$1"; else python3 "$script" "$1"; fi 2>/dev/null
exit 0
