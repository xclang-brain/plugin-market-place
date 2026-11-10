#!/usr/bin/env python3
"""coverity-triage-kit の、モデルを使わない試験をすべて動かす（仕様 REQ-601、設計書 9.1）。

  python3 tests/run.py                  すべて動かし、まとめを出す（Windows は py -3 tests/run.py）
  python3 tests/run.py --list           動かせる試験の名前を出す
  python3 tests/run.py --only docs      名前を選んで動かす（何回でも付けられる。parts は部品の試験すべて）
  python3 tests/run.py --skip-slow      時間のかかる試験（結合の試験、画面の試験）を飛ばす
  python3 tests/run.py -v               すべての記録を出す

動かすもの（この順）：
  1. 部品ごとの試験：skills/<名前>/tests/ と mcp-servers/<名前>/tests/。部品のフォルダで動かす。
     決まり：フォルダに pyproject.toml があれば uv のプロジェクトとして
       uv run --native-tls --frozen --quiet --directory <フォルダ> python -m unittest discover -s tests -v
     なければ
       python -m unittest discover -s tests -v
  2. tests/structure、tests/contracts、tests/independence、tests/integration、tests/ui、tests/docs、tests/eval（Python の unittest。
     ui は結果と設定の画面を Playwright のブラウザーで確かめる。Node.js か Playwright がなければ、理由を書いて飛ばす）

結果は tests/results/latest.json にも書く（git に入れない）。終了コード：0 すべて合格（飛ばしたものはあってよい）、
1 不合格がある、2 要る道具がない（何も動かさない）。
試験は、リポジトリの中に __pycache__・.venv・結果のファイルを残さない（PYTHONDONTWRITEBYTECODE=1、
UV_PROJECT_ENVIRONMENT を一時フォルダにする）。動かした後に残っていたら、不合格にする。
標準ライブラリだけを使う。古い Python でも、版が足りないことを伝えられる書き方にしてある。
"""

import argparse
import codecs
import datetime
import json
import locale
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
RESULTS = TESTS / "results" / "latest.json"
MIN_PYTHON = (3, 10)
TAIL_LINES = 80
TIMEOUTS = {"part": 900}
DEFAULT_TIMEOUT = 900
PLUGIN_SUITES = ["structure", "contracts", "independence", "integration", "ui", "docs", "eval"]
SLOW_SUITES = {"integration", "ui"}
# 試験が作るが、リポジトリに残してよいもの（.gitignore にある結果の置き場所）
ALLOWED_LEFTOVERS = ("tests/results/",)


# ---------------------------------------------------------------------------
# 部品の試験の動かし方
# ---------------------------------------------------------------------------

def part_dirs(root=ROOT):
    """部品のフォルダ（skills/ と mcp-servers/ の下。点や _ で始まるものを除く）"""
    out = []
    for group in ("skills", "mcp-servers"):
        folder = Path(root) / group
        if folder.is_dir():
            out += sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
    return out


def has_tests(part):
    tests = Path(part) / "tests"
    return tests.is_dir() and any(tests.rglob("test*.py"))


def is_uv_project(part):
    return (Path(part) / "pyproject.toml").is_file()


def part_test_command(part, python=None):
    """部品の試験を動かすコマンド（部品のフォルダで動かす）"""
    part = Path(part)
    if is_uv_project(part):
        command = ["uv", "run", "--native-tls"]
        if (part / "uv.lock").is_file():
            command.append("--frozen")
        return command + ["--quiet", "--directory", str(part), "python", "-m", "unittest", "discover", "-s", "tests", "-v"]
    return [python or sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]


def test_env(extra=None):
    """試験に渡す環境変数。バイトコードを書かない。文字コードの環境変数は足さない（利用者の PC と同じ既定で動かす）"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(quiet_git(env))
    env.update(extra or {})
    return env


def quiet_git(env):
    """git が commit などの後に裏で始める保守（.git の中のファイルを変える）を止める設定を、環境変数で足す。
    試験が「ファイルを変えていない」を確かめるときに、git の保守と取り違えないため"""
    try:
        count = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        count = 0
    out = {}
    for key, value in (("maintenance.auto", "false"), ("gc.auto", "0")):
        out[f"GIT_CONFIG_KEY_{count}"] = key
        out[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    out["GIT_CONFIG_COUNT"] = str(count)
    return out


def output_encoding():
    """子の Python が、パイプに書くときの文字コード（PYTHONIOENCODING があればそれ、なければ既定）"""
    for name in (os.environ.get("PYTHONIOENCODING", "").split(":")[0].strip(), locale.getpreferredencoding(False)):
        try:
            if name:
                return codecs.lookup(name).name
        except LookupError:
            pass
    return "utf-8"


def kill_tree(proc):
    """プロセスと、その子・孫を止める"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            subprocess.run([str(taskkill), "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(command, cwd, env, timeout):
    """コマンドを動かし、(終了コード（時間切れは None）, 出力, 秒) を返す。
    出力は一時ファイルで受ける（切り離して動く孫のプロセスが出力をつかんでいても、待たずに済む）"""
    started = time.monotonic()
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **kwargs)
        except OSError as error:
            return 127, "コマンドを動かせません：{}（{}）".format(command[0], error), 0.0
        try:
            code = proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            code, timed_out = None, True
        out.seek(0)
        text = out.read().decode(output_encoding(), errors="replace")
    if timed_out:
        text += "\n{} 秒たっても終わらないため、止めました。".format(timeout)
    return code, text, time.monotonic() - started


# ---------------------------------------------------------------------------
# 出力を読む
# ---------------------------------------------------------------------------

def unittest_counts(out, code, modules=None):
    # A test that runs another suite and prints its output (independence) leaves earlier "Ran N tests"
    # and "OK"/"FAILED" lines: the suite's own summary is the last "Ran" line and the lines after it.
    runs = list(re.finditer(r"^Ran (\d+) tests?", out, re.M))
    total = int(runs[-1].group(1)) if runs else 0
    tail = out[runs[-1].end():] if runs else out
    counts = {"failures": 0, "errors": 0, "unexpected successes": 0}
    m = re.search(r"^FAILED \((.*)\)", tail, re.M)
    if m:
        for key, value in re.findall(r"(failures|errors|unexpected successes)=(\d+)", m.group(1)):
            counts[key] += int(value)
    failed = counts["failures"] + counts["errors"]
    if failed and modules:
        # "failures=" counts every failing subTest: count the failing tests of the suite's own modules instead
        # (not those of a suite a test ran and printed), so that the failures never outnumber the tests run.
        head = out[:runs[-1].start()] if runs else out
        failing = {found for found in re.findall(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", head, re.M)
                   if found[1].split(".")[0] in modules or found[1].startswith("unittest.")}
        if failing:
            failed = min(failed, len(failing))
    bad = failed + counts["unexpected successes"]
    if code != 0 and bad == 0:
        bad = 1  # 数えられない失敗（読み込みの失敗、時間切れなど）
    m = re.search(r"^(?:OK|FAILED) \(.*?skipped=(\d+)", tail, re.M)
    skipped = int(m.group(1)) if m else 0
    if total:
        skipped = min(skipped, total)  # skipped subTests are counted one by one too
    return total, bad, skipped


def suite_modules(root):
    """The names unittest gives the modules found under root (discover -s root): test_x, or the package of a sub-folder"""
    root = Path(root)
    if not root.is_dir():
        return set()
    return {p.relative_to(root).parts[0].rsplit(".", 1)[0] for p in root.rglob("test*.py") if "node_modules" not in p.parts}


def unittest_reasons(out, limit=12):
    """不合格の試験の名前と、理由の1行"""
    reasons = []
    blocks = re.split(r"^={50,}\n", out, flags=re.M)
    for block in blocks:
        m = re.match(r"(FAIL|ERROR): (\S+) \(([^)]*)\)", block)
        if not m:
            continue
        lines = [l for l in block.splitlines() if l.strip() and not re.match(r"^-{50,}$", l)]
        why = ""
        for i, line in enumerate(lines):
            if re.match(r"^\w*(Error|Exception|Failure)\b.*?:", line) or line.startswith(("AssertionError", "unittest.case.SkipTest")):
                why = " ".join(l.strip() for l in lines[i:i + 3])
                break
        if not why and len(lines) > 1:
            why = lines[-1].strip()
        reasons.append("{}: {} — {}".format(m.group(1), m.group(3) or m.group(2), why[:400]))
        if len(reasons) >= limit:
            break
    if not reasons and re.search(r"秒たっても終わらない", out):
        reasons.append("時間切れ")
    return reasons


# ---------------------------------------------------------------------------
# 試験の組
# ---------------------------------------------------------------------------

class Suite:
    def __init__(self, name, kind, cwd=None, command=None, folder=None, needs=()):
        self.name, self.kind, self.cwd, self.command, self.folder = name, kind, cwd, command, folder
        self.needs = set(needs)


def plugin_suite(name):
    folder = TESTS / name
    if not folder.is_dir() or not any(folder.rglob("test*.py")):
        return None
    needs = {"python"} if name in ("structure", "docs", "eval") else {"python", "uv", "git"}
    if name == "ui":
        needs |= {"browser"}  # Node.js and Playwright: optional (the suite skips without them; in CI it fails)
    return Suite(name, "plugin", ROOT, [sys.executable, "-m", "unittest", "discover", "-s", str(folder), "-t", str(folder), "-v"],
                 folder, needs=needs)


def all_suites():
    suites = []
    for part in part_dirs():
        name = part.relative_to(ROOT).as_posix()
        needs = {"python", "uv", "git"}
        suites.append(Suite(name, "part", part, part_test_command(part) if has_tests(part) else None, part, needs=needs))
    for name in PLUGIN_SUITES:
        suite = plugin_suite(name)
        suites.append(suite or Suite(name, "plugin"))
    return suites


def select(suites, only):
    if not only:
        return suites, []
    wanted, unknown = [], []
    for word in only:
        for token in [w.strip() for w in word.split(",") if w.strip()]:
            token = token.rstrip("/")
            hit = [s for s in suites if s.name == token or (token == "parts" and s.kind == "part")
                   or (s.kind == "part" and (s.name.split("/", 1)[1] == token or s.name.split("/", 1)[0] == token))]
            if not hit:
                unknown.append(token)
            wanted += [s for s in hit if s not in wanted]
    return [s for s in suites if s in wanted], unknown


# ---------------------------------------------------------------------------
# 要る道具
# ---------------------------------------------------------------------------

def version_of(command, pattern=r"(\d+)\.(\d+)(?:\.(\d+))?"):
    exe = shutil.which(command[0])
    if exe is None:
        return None, None
    try:
        done = subprocess.run([exe] + command[1:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return exe, None
    text = done.stdout.decode("utf-8", errors="replace")
    m = re.search(pattern, text)
    return exe, (tuple(int(g) for g in m.groups() if g is not None) if m else None)


def playwright_available():
    node = shutil.which("node")
    if not node:
        return False
    try:
        done = subprocess.run([node, "-e", "require.resolve('playwright')"], cwd=str(TESTS), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


INSTALL_HINTS = {
    "python": "Python {}.{} 以上を入れてください（https://www.python.org/downloads/ 。Windows は py -3 で動かします）".format(*MIN_PYTHON),
    "uv": "uv を入れてください。Windows（PowerShell）：powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"　"
          "Linux（bash）：curl -LsSf https://astral.sh/uv/install.sh | sh",
    "git": "git を入れてください（https://git-scm.com/downloads）",
}


def check_prerequisites(needs):
    required, optional, missing = [], [], []
    py = sys.version_info[:3]
    item = {"name": "python", "need": ">={}.{}".format(*MIN_PYTHON), "found": True, "version": ".".join(map(str, py)),
            "path": sys.executable}
    if py[:2] < MIN_PYTHON:
        item["found"] = False
        missing.append("python")
    required.append(item)
    for name in ("uv", "git"):
        if name not in needs:
            continue
        exe, version = version_of([name, "--version"])
        required.append({"name": name, "need": "あること", "found": exe is not None, "path": exe,
                         "version": ".".join(map(str, version)) if version else None})
        if exe is None:
            missing.append(name)
    for name, command in (("svn", ["svn", "--version", "--quiet"]),):
        exe, version = version_of(command)
        optional.append({"name": name, "found": exe is not None, "path": exe,
                         "version": ".".join(map(str, version)) if version else None,
                         "note": "なければ、それを使う試験は飛ばされます"})
    if "browser" in needs:
        optional.append({"name": "playwright", "found": playwright_available(),
                         "note": "なければ、画面をブラウザーで確かめる試験は飛ばされます"})
    return {"ok": not missing, "required": required, "optional": optional, "missing": missing}


# ---------------------------------------------------------------------------
# リポジトリに残したもの
# ---------------------------------------------------------------------------

def repo_snapshot():
    """git から見た、プラグインのフォルダの中の、変わったもの・入れていないもの・無視されたもの。git がなければ None"""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        done = subprocess.run([git, "-C", str(ROOT), "status", "--porcelain=v1", "--untracked-files=all", "--ignored",
                               "--", "."], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        top = subprocess.run([git, "-C", str(ROOT), "rev-parse", "--show-prefix"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or top.returncode != 0:
        return None
    prefix = top.stdout.decode("utf-8", errors="replace").strip()
    entries = set()
    for line in done.stdout.decode("utf-8", errors="replace").splitlines():
        path = line[3:].strip().strip('"')
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        entries.add((line[:2], path))
    return entries


def leftovers(before, after):
    """試験の間に増えたもの：(.gitignore で無視されるもの＝試験が作った __pycache__・.venv など, ほかに増えた・変わったもの)。
    ほかに増えたものは、同じ時に人が直したものかもしれないので、知らせるだけにする"""
    if before is None or after is None:
        return [], []
    new = sorted((code, path) for code, path in after - before if not path.startswith(ALLOWED_LEFTOVERS))
    made = ["{} {}".format(code, path) for code, path in new if code == "!!"]
    other = ["{} {}".format(code, path) for code, path in new if code != "!!"]
    return made, other


# ---------------------------------------------------------------------------
# 動かす
# ---------------------------------------------------------------------------

def run_suite(suite, args, uv_root):
    result = {"name": suite.name, "kind": suite.kind, "status": "skipped", "total": 0, "failed": 0, "skipped": 0,
              "seconds": 0.0, "reasons": [], "skip_reason": None, "command": suite.command,
              "cwd": str(suite.cwd) if suite.cwd else None, "output": ""}
    if suite.kind == "part" and suite.command is None:
        result.update(status="failed", failed=1,
                      reasons=["{}/tests/ に試験（test*.py）がありません（仕様 REQ-101）".format(suite.name)])
        return result
    if suite.command is None:
        result["skip_reason"] = "tests/{}/ に試験がまだありません".format(suite.name)
        return result
    if args.skip_slow and suite.name in SLOW_SUITES:
        result["skip_reason"] = "--skip-slow のため飛ばしました"
        return result
    extra = {"UV_PROJECT_ENVIRONMENT": str(Path(uv_root) / re.sub(r"[^A-Za-z0-9_.-]", "_", suite.name)),
             "TESTS_JOBS": str(args.jobs)}
    if args.skip_slow:
        extra["SKIP_SLOW_TESTS"] = "1"
    timeout = args.timeout or TIMEOUTS.get(suite.name if suite.kind == "plugin" else "part", DEFAULT_TIMEOUT)
    command = list(suite.command)
    if command[0] == "uv":
        command[0] = shutil.which("uv") or "uv"
    code, out, seconds = run_command(command, suite.cwd, test_env(extra), timeout)
    root = suite.folder / "tests" if suite.kind == "part" else suite.folder
    total, bad, skipped = unittest_counts(out, code, suite_modules(root) if root else None)
    reasons = unittest_reasons(out) if bad else []
    if bad == 0 and total == 0:
        bad, reasons = 1, ["試験が1つも動きませんでした"]
    if bad and not reasons:
        reasons = [line.strip() for line in out.strip().splitlines()[-3:]] or ["終了コード {}".format(code)]
    result.update(status="failed" if bad else "passed", total=total, failed=bad, skipped=skipped,
                  seconds=round(seconds, 1), reasons=reasons, output=out, exit_code=code)
    return result


def mark(result):
    if result["status"] == "failed":
        return "×"
    if result["status"] == "skipped" or result["skipped"]:
        return "△"
    return "○"


def line_for(result):
    name = result["name"]
    if result["status"] == "skipped":
        return "  △ {}：飛ばしました（{}）".format(name, result["skip_reason"])
    seconds = "、{} 秒".format(result["seconds"]) if result["seconds"] else ""
    if result["status"] == "failed":
        return "  × {}：{} 件中 {} 件が不合格{}".format(name, result["total"], result["failed"], seconds)
    if result["skipped"]:
        return "  △ {}：{} 件中 {} 件が合格（{} 件は飛ばしました{}）".format(
            name, result["total"], result["total"] - result["skipped"], result["skipped"], seconds)
    return "  ○ {}：{} 件すべて合格{}".format(name, result["total"], seconds)


def write_results(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(str(tmp), str(path))
                break
            except PermissionError:
                time.sleep(0.2 * (attempt + 1))
        return True
    except OSError as error:
        print("結果のファイルを書けませんでした：{}（{}）".format(path, error))
        return False


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="coverity-triage-kit の、モデルを使わない試験をすべて動かす")
    parser.add_argument("--only", action="append", default=[], metavar="名前",
                        help="動かす試験（parts、skills/<名前>、<部品の名前>、structure、contracts、independence、"
                             "integration、ui、docs、eval）。何回でも付けられる")
    parser.add_argument("--skip-slow", action="store_true", help="時間のかかる試験を飛ばす")
    parser.add_argument("--list", action="store_true", help="試験の名前を出して終わる")
    parser.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)),
                        help="部品の試験を同時にいくつ動かすか（既定：CPU の数、4 まで）")
    parser.add_argument("--timeout", type=int, default=0, help="1つの試験の組の時間の上限（秒）")
    parser.add_argument("--results", default=str(RESULTS), help="結果の JSON の置き場所")
    parser.add_argument("-v", "--verbose", action="store_true", help="すべての記録を出す")
    return parser.parse_args(argv)


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            if os.environ.get("GITHUB_ACTIONS") == "true":
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # readable job logs (run.py's own output only)
            else:
                stream.reconfigure(errors="backslashreplace")
    args = parse_args(argv)
    if sys.version_info[:2] < MIN_PYTHON:
        print("Python {}.{} 以上が要ります（いまは {}）。{}".format(MIN_PYTHON[0], MIN_PYTHON[1],
              sys.version.split()[0], INSTALL_HINTS["python"]))
        return 2
    suites = all_suites()
    if args.list:
        for suite in suites:
            state = "" if suite.command else "（試験がまだない）"
            print("{}{}".format(suite.name, state))
        return 0
    chosen, unknown = select(suites, args.only)
    if unknown:
        print("知らない試験の名前です：{}。--list で名前を見られます。".format("、".join(unknown)))
        return 2
    started, clock = now(), time.monotonic()
    needs = set()
    for suite in chosen:
        if suite.command is not None and not (args.skip_slow and suite.name in SLOW_SUITES):
            needs |= suite.needs
    prerequisites = check_prerequisites(needs)
    results_path = Path(args.results)
    if not prerequisites["ok"]:
        print("試験に要る道具が足りません。入れてから、もう一度動かしてください。")
        for name in prerequisites["missing"]:
            print("  × {}：{}".format(name, INSTALL_HINTS.get(name, "")))
        write_results(results_path, {"version": 1, "root": str(ROOT), "started_at": started, "finished_at": now(),
                                     "ok": False, "exit_code": 2, "prerequisites": prerequisites, "suites": []})
        return 2
    for item in prerequisites["optional"]:
        if not item["found"]:
            print("  （{} がありません。{}）".format(item["name"], item["note"]))

    before = repo_snapshot()
    uv_root = tempfile.mkdtemp(prefix="plugin-tests-uv-")
    results = []
    lock = threading.Lock()

    def one(suite):
        with lock:
            print("▶ {} …".format(suite.name), flush=True)
        result = run_suite(suite, args, uv_root)
        with lock:
            print(line_for(result), flush=True)
        return result

    try:
        parts = [s for s in chosen if s.kind == "part"]
        others = [s for s in chosen if s.kind != "part"]
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            results += list(pool.map(one, parts))
        for suite in others:
            results.append(one(suite))
    finally:
        shutil.rmtree(uv_root, ignore_errors=True)

    made, changed = leftovers(before, repo_snapshot())
    if made:
        results.append({"name": "後片付け（リポジトリに残したもの）", "kind": "check", "status": "failed",
                        "total": 1, "failed": 1, "skipped": 0, "seconds": 0.0, "skip_reason": None,
                        "reasons": ["試験のあとに、リポジトリに次のものが残りました（試験は一時フォルダを使ってください）："] + made[:50],
                        "output": "\n".join(made)})

    if not any(r["status"] != "skipped" for r in results):
        print("試験が見つかりません。skills/<名前>/tests/ と tests/ の下を確かめてください。")
        code = 1
    else:
        code = 1 if any(r["status"] == "failed" for r in results) else 0

    for r in results:
        if r.get("output") and (args.verbose or r["status"] == "failed"):
            print("=" * 20, r["name"], "=" * 20)
            lines = r["output"].rstrip().splitlines()
            if not args.verbose and len(lines) > TAIL_LINES:
                print("（前の {} 行は省きました。すべて見るときは -v を付けてください）".format(len(lines) - TAIL_LINES))
                lines = lines[-TAIL_LINES:]
            print("\n".join(lines))
            print()

    if changed:
        print("（試験の間に、次のファイルが増えたか変わりました。試験が作ったものなら、一時フォルダに書くように直してください）")
        for line in changed[:20]:
            print("    {}".format(line))
    print("試験の結果")
    for r in results:
        print(line_for(r) if r["kind"] != "check" else "  × {}".format(r["name"]))
        if r["status"] == "failed":
            for reason in r["reasons"][:8]:
                print("      - {}".format(reason))
    total = sum(r["total"] for r in results)
    bad = sum(r["failed"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    seconds = round(time.monotonic() - clock, 1)
    if code:
        print("不合格があります（合計 {} 件中 {} 件、{} 秒）。上の記録を見てください。".format(total, bad, seconds))
    elif skipped or any(r["status"] == "skipped" for r in results):
        print("不合格はありません（合計 {} 件。飛ばしたものがあります。{} 秒）。".format(total, seconds))
    else:
        print("すべて合格しました（合計 {} 件、{} 秒）。".format(total, seconds))

    data = {"version": 1, "root": str(ROOT), "started_at": started, "finished_at": now(), "duration_seconds": seconds,
            "ok": code == 0, "exit_code": code, "options": {"only": args.only, "skip_slow": args.skip_slow, "jobs": args.jobs},
            "prerequisites": prerequisites,
            "totals": {"tests": total, "failed": bad, "skipped": skipped},
            "changed_during_run": changed,
            "suites": [{k: v for k, v in r.items() if k != "output"} for r in results]}
    if write_results(results_path, data):
        print("結果：{}".format(results_path))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
