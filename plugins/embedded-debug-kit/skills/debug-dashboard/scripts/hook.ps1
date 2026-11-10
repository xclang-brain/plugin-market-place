# Windows の Copilot CLI のフックの設定（hooks.json）から呼ばれ、hook.py を動かす。
# hooks.json は、このファイルを読み、スクリプトブロックにして動かす。
# .ps1 のファイルとして動かさないのは、PowerShell の実行ポリシーが禁じていても動くようにするため。
#
# 記録中フラグ（recording）がなければ、Python を起動せずに終える（REQ-408）。
# Python が見つからないときも、hook.py が失敗したときも、0 で終える。
# $input は使わない。使うと、PowerShell がフックの入力を先に読み、文字コードを変えてしまう。
param($hookEvent)
$project = $env:COPILOT_PROJECT_DIR
if (-not $project) { $project = '.' }
if (-not (Test-Path -LiteralPath (Join-Path $project '.debug-dashboard/recording') -PathType Leaf)) { exit 0 }
$script = Join-Path $env:PLUGIN_ROOT 'skills/debug-dashboard/scripts/hook.py'
if (Get-Command py -CommandType Application -ErrorAction SilentlyContinue) {
    & py -3 $script $hookEvent 2>$null
} elseif (Get-Command python -CommandType Application -ErrorAction SilentlyContinue) {
    & python $script $hookEvent 2>$null
} else {
    & python3 $script $hookEvent 2>$null
}
exit 0
