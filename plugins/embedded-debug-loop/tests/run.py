#!/usr/bin/env python3
"""このプラグインの試験を、すべて動かす。

  python3 tests/run.py        結果のまとめだけを出す（不合格のときは、その記録も出す）
  python3 tests/run.py -v     すべての記録を出す

動かすもの：
- tests/skills/<スキル>/ と tests/independence/ の Python の試験（unittest。フォルダごとに別に動かす）
- tests/extension/*.test.mjs（node --test）

すべて合格なら終了コード 0、1つでも不合格や動かせない試験があれば 1 を返す。
Python 3.8 以上の標準の部品だけを使う。
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
TIMEOUT_SECONDS = 900
TAIL_LINES = 80


def python_groups():
    """（名前, フォルダ）の並び。スキルのフォルダごとと、独立性のフォルダ"""
    groups = []
    skills = TESTS / "skills"
    if skills.is_dir():
        for folder in sorted(p for p in skills.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            if any(folder.rglob("test_*.py")):
                groups.append(("スキル {} の試験".format(folder.name), folder))
    independence = TESTS / "independence"
    if independence.is_dir() and any(independence.rglob("test_*.py")):
        groups.append(("独立性の試験", independence))
    return groups


def run(command, cwd):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    try:
        done = subprocess.run(command, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=TIMEOUT_SECONDS)
        return done.returncode, done.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as error:
        out = (error.stdout or b"").decode("utf-8", errors="replace")
        return None, out + "\n{} 秒たっても終わらないため、止めました。".format(TIMEOUT_SECONDS)


def run_python(name, folder):
    code, out = run([sys.executable, "-m", "unittest", "discover", "-s", str(folder), "-t", str(folder),
                     "-p", "test_*.py", "-v"], ROOT)
    m = re.search(r"^Ran (\d+) tests?", out, re.M)
    total = int(m.group(1)) if m else 0
    bad = 0
    m = re.search(r"^FAILED \((.*)\)", out, re.M)
    if m:
        for key, value in re.findall(r"(failures|errors|unexpected successes)=(\d+)", m.group(1)):
            bad += int(value)
    if code != 0 and bad == 0:
        bad = 1  # 数えられない失敗（読み込みの失敗、時間切れなど）
    return {"name": name, "total": total, "bad": bad, "output": out}


def run_node():
    name = "Web管理画面の試験"
    files = sorted((TESTS / "extension").glob("*.test.mjs")) if (TESTS / "extension").is_dir() else []
    if not files:
        return None
    node = shutil.which("node")
    if node is None:
        return {"name": name, "total": 0, "bad": 1,
                "output": "node（Node.js 20 以上）が見つからないため、Web管理画面の試験を動かせませんでした。"}
    code, out = run([node, "--test", "--test-reporter=tap"] + [str(f) for f in files], ROOT)

    def count(label):
        m = re.search(r"^# {} (\d+)".format(label), out, re.M)
        return int(m.group(1)) if m else 0

    total = count("tests")
    bad = count("fail") + count("cancelled")
    if code != 0 and bad == 0:
        bad = 1
    return {"name": name, "total": total, "bad": bad, "output": out}


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    results = [run_python(name, folder) for name, folder in python_groups()]
    node = run_node()
    if node is not None:
        results.append(node)

    if not results:
        print("試験が見つかりません。tests/ の下を確かめてください。")
        return 1

    for r in results:
        if verbose or r["bad"]:
            print("=" * 20, r["name"], "=" * 20)
            lines = r["output"].rstrip().splitlines()
            if not verbose and len(lines) > TAIL_LINES:
                print("（前の {} 行は省きました。すべて見るときは -v を付けてください）".format(len(lines) - TAIL_LINES))
                lines = lines[-TAIL_LINES:]
            print("\n".join(lines))
            print()

    print("試験の結果")
    for r in results:
        if r["bad"]:
            print("  × {}：{} 件中 {} 件が不合格".format(r["name"], r["total"], r["bad"]))
        else:
            print("  ○ {}：{} 件すべて合格".format(r["name"], r["total"]))
    total = sum(r["total"] for r in results)
    bad = sum(r["bad"] for r in results)
    if bad:
        print("不合格があります（合計 {} 件中 {} 件）。上の記録を見てください。".format(total, bad))
        return 1
    print("すべて合格しました（合計 {} 件）。".format(total))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
