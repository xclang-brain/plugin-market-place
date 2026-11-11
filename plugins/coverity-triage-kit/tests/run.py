#!/usr/bin/env python3
"""Run the model-free tests of coverity-triage-kit.

    python3 tests/run.py [--list] [--only <suite>]... [-v]        (Windows: py -3 tests/run.py)

The suites: every folder below skills/ and mcp-servers/ that has tests (a folder with pyproject.toml runs
through uv, the others with this Python), every tests/test_*.py, and tests/flow. One line per suite. The
exit code is 0 when every suite passed, else 1. A suite also fails when it leaves a file in the repository.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent


def suites() -> dict[str, tuple[list[str], Path]]:
    """name -> (the command, the folder it runs in)."""
    found = {}
    for folder in sorted(PLUGIN.glob("skills/*")) + sorted(PLUGIN.glob("mcp-servers/*")):
        if any(folder.glob("tests/test*.py")):
            python = [sys.executable]
            if (folder / "pyproject.toml").is_file():
                python = ["uv", "run", "--native-tls", "--frozen", "--quiet", "--directory", str(folder), "python"]
            found[folder.relative_to(PLUGIN).as_posix()] = (python + ["-m", "unittest", "discover", "-s", "tests", "-v"], folder)
    for path in sorted(TESTS.glob("test_*.py")):
        found[path.stem[5:]] = ([sys.executable, "-m", "unittest", "-v", path.stem], TESTS)
    found["flow"] = ([sys.executable, "-m", "unittest", "discover", "-s", "flow", "-v"], TESTS)
    return found


def tree() -> set[str]:
    """Every file and folder of the plugin."""
    return {os.path.join(top, name) for top, folders, names in os.walk(PLUGIN) for name in folders + names}


def run(name: str, command: list[str], folder: Path, env: dict, verbose: bool) -> tuple[bool, int]:
    """Run one suite and print its line. Returns (passed, the number of tests)."""
    before, started = tree(), time.monotonic()
    # Into a file: a pipe would stay open as long as a process that a test left behind.
    with tempfile.TemporaryFile("w+", errors="replace") as out:
        code = subprocess.run(command, cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=out,
                              stderr=subprocess.STDOUT).returncode
        out.seek(0)
        text = out.read()
    left = sorted(Path(path).relative_to(PLUGIN).as_posix() for path in tree() - before)
    ran = re.findall(r"^Ran (\d+) tests? in", text, re.M)
    verdict = re.findall(r"^((?:OK|FAILED)(?: \(.*\))?)$", text, re.M)
    count = int(ran[-1]) if ran else 0
    passed = code == 0 and count > 0 and not left
    line = f"{count} 件 {verdict[-1]}" if ran and verdict else f"テストを実行できませんでした（終了コード {code}）"
    if left:
        line += f"、リポジトリにファイルが残りました: {', '.join(left[:10])}"
    print(f"{'○' if passed else '×'} {name}: {line}（{time.monotonic() - started:.0f} 秒）", flush=True)
    skipped = re.findall(r" \.\.\. skipped (.+)$", text, re.M)
    for reason in sorted(set(skipped)):
        print(f"    スキップ {skipped.count(reason)} 件: {reason}", flush=True)
    if verbose or not passed:
        print(text, flush=True)
    return passed, count


def main() -> int:
    sys.stdout.reconfigure(errors="backslashreplace", **({"encoding": "utf-8"} if os.environ.get("GITHUB_ACTIONS") else {}))
    parser = argparse.ArgumentParser(description="coverity-triage-kit の、モデルを使用しないテストを実行します。")
    parser.add_argument("--list", action="store_true", help="スイートの名前を表示して終了します")
    parser.add_argument("--only", action="append", default=[], metavar="<名前>",
                        help="指定したスイートだけを実行します（複数回指定できます）")
    parser.add_argument("-v", "--verbose", action="store_true", help="合格したスイートの出力も表示します")
    args = parser.parse_args()
    found = suites()
    if args.list:
        print("\n".join(found))
        return 0
    unknown = [name for name in args.only if name not in found]
    if unknown:
        print(f"スイートがありません: {', '.join(unknown)}。名前は --list で確認できます")
        return 1
    if not shutil.which("uv"):
        print("uv がありません。インストールしてから、もう一度実行してください: https://docs.astral.sh/uv/")
        return 1
    failed, total = [], 0
    # The environment that uv makes for a suite goes outside the repository.
    with tempfile.TemporaryDirectory(prefix="coverity-triage-kit-tests-", ignore_cleanup_errors=True) as scratch:
        for name, (command, folder) in found.items():
            if args.only and name not in args.only:
                continue
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                   "UV_PROJECT_ENVIRONMENT": str(Path(scratch) / name.replace("/", "-"))}
            passed, count = run(name, command, folder, env, args.verbose)
            total += count
            failed += [] if passed else [name]
    print(f"合計 {total} 件。" + (f"不合格のスイート: {', '.join(failed)}" if failed else "すべてのスイートが合格しました"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
