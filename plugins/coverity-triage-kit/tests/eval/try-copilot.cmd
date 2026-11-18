@echo off
rem Runs tests\eval\try_copilot.py: the checks that only the real GitHub Copilot CLI can answer.
rem Double-click it, or run it in a terminal.
chcp 65001 > nul
uv run --native-tls --quiet "%~dp0try_copilot.py" %*
echo.
pause
