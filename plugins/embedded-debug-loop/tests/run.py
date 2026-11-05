#!/usr/bin/env python3
"""このプラグインのテストを、すべて動かす。

  python3 tests/run.py        結果のまとめだけを出す（不合格のときは、その記録も出す）
  python3 tests/run.py -v     すべての記録を出す
  （Windows では python3 の代わりに py -3 か python）

動かすもの：
- 各スキルのフォルダの中のテスト（skills/<スキル>/tests/。スキルのフォルダで動かす。そのフォルダだけで動く形）
- tests/independence/ の独立性のテスト（unittest）
- tests/extension/*.test.mjs（node --test）

すべて合格なら終了コード 0、1つでも不合格や動かせないテストがあれば 1 を返す。
Python 3.8 以上の標準の部品だけを使う。
結果は、Python の既定の文字コードで書く（書けない文字は \\uXXXX の形にする）。
テストに、文字コードの環境変数（PYTHONIOENCODING）を足さない。スキルのスクリプトを、
利用者の PC と同じ既定の文字コードで動かすため。
"""

import codecs
import locale
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
    """（名前, 動かすフォルダ, テストのフォルダ）の並び。スキルごとと、独立性"""
    groups = []
    skills = ROOT / "skills"
    if skills.is_dir():
        for skill in sorted(p for p in skills.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            if any((skill / "tests").glob("test_*.py")):
                # スキルの README に書いた動かし方と同じ（スキルのフォルダで discover -s tests）
                groups.append(("スキル {} のテスト".format(skill.name), skill, "tests"))
    independence = TESTS / "independence"
    if independence.is_dir() and any(independence.rglob("test_*.py")):
        groups.append(("独立性のテスト", ROOT, str(independence)))
    return groups


def python_output_encoding():
    """子の Python が、パイプに書くときの文字コード（PYTHONIOENCODING があればそれ、なければ既定）"""
    for name in (os.environ.get("PYTHONIOENCODING", "").split(":")[0].strip(), locale.getpreferredencoding(False)):
        try:
            if name:
                return codecs.lookup(name).name
        except LookupError:
            pass
    return "utf-8"


def run(command, cwd, encoding):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        done = subprocess.run(command, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=TIMEOUT_SECONDS)
        return done.returncode, done.stdout.decode(encoding, errors="replace")
    except subprocess.TimeoutExpired as error:
        out = (error.stdout or b"").decode(encoding, errors="replace")
        return None, out + "\n{} 秒たっても終わらないため、止めました。".format(TIMEOUT_SECONDS)


def run_python(name, cwd, start):
    code, out = run([sys.executable, "-m", "unittest", "discover", "-s", start, "-p", "test_*.py", "-v"], cwd,
                    python_output_encoding())
    m = re.search(r"^Ran (\d+) tests?", out, re.M)
    total = int(m.group(1)) if m else 0
    bad = 0
    m = re.search(r"^FAILED \((.*)\)", out, re.M)
    if m:
        for key, value in re.findall(r"(failures|errors|unexpected successes)=(\d+)", m.group(1)):
            bad += int(value)
    if code != 0 and bad == 0:
        bad = 1  # 数えられない失敗（読み込みの失敗、時間切れなど）
    m = re.search(r"^(?:OK|FAILED) \(.*?skipped=(\d+)", out, re.M)
    skipped = int(m.group(1)) if m else 0
    return {"name": name, "total": total, "bad": bad, "skipped": skipped, "output": out}


def run_node():
    name = "Web管理画面のテスト"
    files = sorted((TESTS / "extension").glob("*.test.mjs")) if (TESTS / "extension").is_dir() else []
    if not files:
        return None
    node = shutil.which("node")
    if node is None:
        return {"name": name, "total": 0, "bad": 1,
                "output": "node（Node.js 20 以上）が見つからないため、Web管理画面のテストを動かせませんでした。"}
    code, out = run([node, "--test", "--test-reporter=tap"] + [str(f) for f in files], ROOT, "utf-8")

    def count(label):
        m = re.search(r"^# {} (\d+)".format(label), out, re.M)
        return int(m.group(1)) if m else 0

    total = count("tests")
    bad = count("fail") + count("cancelled")
    if code != 0 and bad == 0:
        bad = 1
    return {"name": name, "total": total, "bad": bad, "output": out}


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    verbose = "-v" in argv or "--verbose" in argv
    results = [run_python(name, cwd, start) for name, cwd, start in python_groups()]
    node = run_node()
    if node is not None:
        results.append(node)

    if not results:
        print("テストが見つかりません。skills/<スキル>/tests/ と tests/ の下を確かめてください。")
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

    print("テストの結果")
    for r in results:
        skipped = r.get("skipped", 0)
        note = "（うち {} 件は、要る道具がないため飛ばしました。記録は -v で見られます）".format(skipped) if skipped else ""
        if r["bad"]:
            print("  × {}：{} 件中 {} 件が不合格{}".format(r["name"], r["total"], r["bad"], note))
        elif skipped:
            print("  △ {}：{} 件中 {} 件が合格{}".format(r["name"], r["total"], r["total"] - skipped, note))
        else:
            print("  ○ {}：{} 件すべて合格".format(r["name"], r["total"]))
    total = sum(r["total"] for r in results)
    bad = sum(r["bad"] for r in results)
    skipped = sum(r.get("skipped", 0) for r in results)
    if bad:
        print("不合格があります（合計 {} 件中 {} 件）。上の記録を見てください。".format(total, bad))
        return 1
    if skipped:
        print("不合格はありません（合計 {} 件。うち {} 件は飛ばしました）。".format(total, skipped))
        return 0
    print("すべて合格しました（合計 {} 件）。".format(total))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
