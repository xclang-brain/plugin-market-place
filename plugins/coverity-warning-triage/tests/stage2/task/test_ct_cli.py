"""スクリプト ct.py（skills/coverity-triage-scripts/scripts/ct.py）の試験（REQ-062、REQ-102、REQ-104、REQ-106）。

ct.py を、本物と同じく別のプロセスで動かす。Coverity とは通信しない。MCP サーバーが書くファイル
（issues.json、issue-<CID>.json、apply-plan.result.json）は、server/src/coverity_triage/server.py の
search_issues・get_issues・update_triage が書く形のまま、試験の中で用意する。
git と svn のリポジトリ、push 先の bare のリモート、偽の cov-build・cov-analyze・cov-format-errors・
cov-commit-defects は、どれも一時のフォルダに作る。プラグインの中には何も作らない（server/.venv も作らない）。

動かし方:
  - tests/run.py が動かす。ct.py の依存（Python 3.12 以上、pyyaml、pydantic>=2.7）がある Python を探し、
    なければ `uv run --native-tls ct.py` で動かす。どちらもなければ、理由を示して skip する。
  - 単独では: python3 -m unittest discover -s <プラグインのフォルダ>/tests/stage2/task -p test_ct_cli.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[2]
SCRIPTS = PLUGIN / "skills" / "coverity-triage-scripts" / "scripts"
CT = SCRIPTS / "ct.py"
ASSETS = PLUGIN / "skills" / "coverity-selftest" / "assets"
STREAM = "Proj-main"
TIMEOUT = 300

_RUNNER: tuple[list[str] | None, str] | None = None


def _has_deps(python: str) -> bool:
    code = ("import sys, yaml, pydantic\n"
            "v = tuple(int(x) for x in pydantic.VERSION.split('.')[:2])\n"
            "sys.exit(0 if sys.version_info >= (3, 12) and v >= (2, 7) else 1)\n")
    try:
        return subprocess.run([python, "-c", code], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def runner() -> tuple[list[str] | None, str]:
    """ct.py の動かし方（コマンドの前半）と、動かせないときの理由。1回だけ探す。"""
    global _RUNNER
    if _RUNNER is None:
        found: list[str] | None = None
        for python in dict.fromkeys(p for p in (sys.executable, shutil.which("python3"), shutil.which("python")) if p):
            if _has_deps(python):
                found = [python, str(CT)]
                break
        uv = shutil.which("uv")
        if found is None and uv:
            command = [uv, "run", "--native-tls", "--quiet", str(CT)]
            try:
                if subprocess.run([*command, "--help"], capture_output=True, timeout=600).returncode == 0:
                    found = command
            except (OSError, subprocess.SubprocessError):
                pass
        _RUNNER = (found, "" if found else "ct.py の依存（Python 3.12 以上、pyyaml、pydantic>=2.7）がある Python も、"
                                           "それを用意できる uv もありません")
    return _RUNNER


def result_json(item: str, recommendation: str = "fix", comment: str | None = None,
                already_fixed: bool = False) -> dict[str, Any]:
    """作業エージェントが書く result.json（スキル triage-report の形）。"""
    return {
        "item": item,
        "verdict": {"judgement": "true_bug" if recommendation == "fix" else "false_positive",
                    "summary": "戻り値を確かめずに使っている", "rationale": "呼び出し元でも確かめていない",
                    "evidence": [{"file": "src/a.c", "line": 4, "note": "ここで使う"}]},
        "recommendation": recommendation, "confidence": "high", "confidence_reason": "経路をすべて読んだ",
        "deviation": {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                      "comment": comment if comment is not None else "呼び出し元 src/a.c:3 で NULL を確かめているため問題ない"},
        "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Major", "summary": "NULL を確かめる",
                "impact": "戻り値が NULL のときだけ動きが変わる", "exceeded_constraints": [],
                "already_fixed_on_latest": already_fixed},
        "revision_drift": {"status": "none", "detail": ""},
        "group_excluded_cids": [],
    }


def issue(cid: int, checker: str, file: str, function: str, line: int, **extra: Any) -> dict[str, Any]:
    """issues.json の1件（server の models.Issue を model_dump(exclude_none=True) した形）。"""
    return {"cid": cid, "checker": checker, "file": file, "function": function, "line": line, "impact": "High",
            "stream": STREAM, "classification": "Unclassified", "action": "Undecided", "status": "New", **extra}


FAKE_COV_TOOL = r'''
import json, os, sys
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["FAKE_COV_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"tool": name, "argv": args, "cwd": os.getcwd(),
                        "passphrase": os.environ.get("COVERITY_PASSPHRASE"),
                        "key_env": os.environ.get("COVTEST_KEY")}) + "\n")
if name == "cov-format-errors":
    out = args[args.index("--json-output-v7") + 1]
    idir = args[args.index("--dir") + 1]
    after = idir.endswith("-after")
    issues = [] if after else [{"mergeKey": "mk301", "checkerName": "NULL_RETURNS",
                                "mainEventFilePathname": "/build/ws/src/a.c", "functionDisplayName": "read_a",
                                "mainEventLineNumber": 4}]
    at = os.environ.get("FAKE_FORMAT_ERROR_AT", "after")
    error = os.environ.get("FAKE_FORMAT_ERROR") if at == ("after" if after else "before") else None
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"type": "Coverity issues", "formatVersion": 7, "suppressedIssueCount": 0, "issues": issues,
                   "desktopAnalysisSettings": None, "error": error, "warnings": []}, f)
sys.exit(0)
'''


class CtCase(unittest.TestCase):
    """一時のフォルダ、ct.py の呼び方、git と svn の道具。"""

    cmd: list[str] = []

    @classmethod
    def setUpClass(cls) -> None:
        found, reason = runner()
        if not found:
            raise unittest.SkipTest(reason)
        if not shutil.which("git"):
            raise unittest.SkipTest("git がありません")
        cls.cmd = found

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ct-cli-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if k not in ("COV_USER", "COV_AUTH_KEY", "COVTEST_USER",
                                                                      "COVTEST_KEY", "COVERITY_PASSPHRASE")}
        self.env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", COPILOT_HOME=str(self.tmp / "copilot"),
                        GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                        GIT_AUTHOR_NAME="tester", GIT_AUTHOR_EMAIL="tester@localhost",
                        GIT_COMMITTER_NAME="tester", GIT_COMMITTER_EMAIL="tester@localhost")

    # ---- 動かす ----------------------------------------------------------------------------

    def run_ct(self, *args: str, cmd: list[str] | None = None, env: dict[str, str] | None = None
               ) -> tuple[dict[str, Any], int, str]:
        """ct.py を動かし、標準出力が JSON 1つだけであることを確かめて返す。"""
        proc = subprocess.run([*(cmd or self.cmd), *args], capture_output=True, timeout=TIMEOUT,
                              env={**self.env, **(env or {})})
        out = proc.stdout.decode("utf-8")
        err = proc.stderr.decode("utf-8", "replace")
        try:
            data = json.loads(out)
        except ValueError:
            self.fail(f"ct.py {' '.join(args)} の標準出力が JSON 1つではありません:\n{out[:2000]}\n{err[:2000]}")
        self.assertIsInstance(data, dict)
        self.assertNotIn("Traceback", out + err)
        return data, proc.returncode, err

    def ok(self, *args: str, **kwargs: Any) -> dict[str, Any]:
        data, code, err = self.run_ct(*args, **kwargs)
        self.assertTrue(data.get("ok"), f"ct.py {' '.join(args)}: {json.dumps(data, ensure_ascii=False)[:3000]}\n{err}")
        self.assertEqual(code, 0)
        return data

    def ng(self, *args: str, **kwargs: Any) -> dict[str, Any]:
        data, code, _ = self.run_ct(*args, **kwargs)
        self.assertIs(data.get("ok"), False, f"ct.py {' '.join(args)}: {json.dumps(data, ensure_ascii=False)[:3000]}")
        self.assertEqual(code, 1)
        self.assertTrue(data.get("error"))
        return data

    def sh(self, args: list[str], cwd: Path | None = None) -> str:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, env=self.env, timeout=TIMEOUT)
        self.assertEqual(proc.returncode, 0, f"{args}: {proc.stderr.decode('utf-8', 'replace')}")
        return proc.stdout.decode("utf-8", "replace").strip()

    def git(self, cwd: Path, *args: str) -> str:
        return self.sh(["git", *args], cwd)

    # ---- リポジトリと設定 -------------------------------------------------------------------

    def write_tree(self, root: Path, files: dict[str, str]) -> None:
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def config_text(self, vcs: str, out: Path, **extra: Any) -> str:
        coverity = {"url": "https://coverity.example.co.jp:8443", "api": "fake", "fake_data": "fake-issues.yaml",
                    "path_strip_prefixes": ["/build/ws/"], **extra.pop("coverity", {})}
        config: dict[str, Any] = {"coverity": coverity,
                                  "vcs": {"type": vcs, **({"base_branch": "main"} if vcs == "git" else {})},
                                  "output_dir": str(out), "max_items": 50, "parallel": 2, **extra}
        return json.dumps(config, ensure_ascii=False, indent=1)  # JSON は YAML としても読める

    SOURCES = {
        "src/a.c": "#include <stdlib.h>\nint read_a(void) {\n  int *p = malloc(4);\n  *p = 1;\n  return *p;\n}\n"
                   "int read_a2(void) {\n  int *q = malloc(4);\n  *q = 2;\n  return *q;\n}\n",
        "src/b.c": "#include <stdio.h>\nint read_b(const char *path) {\n  FILE *fp = fopen(path, \"r\");\n"
                   "  if (!fp) return -1;\n  return fgetc(fp);\n}\n",
    }
    FILTERS = {
        ".coverity-triage/filters/all.yaml": json.dumps({"name": "すべて", "project": "Proj", "streams": [STREAM],
                                                          "triage": {"status": ["New", "Triaged"]}, "limit": 20}),
        ".coverity-triage/filters/untriaged.yaml": json.dumps({"name": "未トリアージ", "streams": [STREAM],
                                                                "triage": {"classification": ["Unclassified"]},
                                                                "limit": 10}),
        ".coverity-triage/filters/big.yaml": json.dumps({"name": "多い", "streams": [STREAM], "limit": 80}),
    }

    def git_repo(self, name: str = "repo", config: dict[str, Any] | None = None, with_config: bool = True) -> Path:
        """main ブランチと、push 先の bare のリモート origin を持つ git のリポジトリ。"""
        repo = self.tmp / name
        origin = self.tmp / f"{name}-origin.git"
        self.git(self.tmp, "init", "--quiet", "--bare", str(origin))
        self.git(self.tmp, "init", "--quiet", "-b", "main", str(repo))
        files = dict(self.SOURCES)
        if with_config:
            files[".coverity-triage/config.yaml"] = self.config_text("git", self.tmp / f"{name}-out", **(config or {}))
            files.update(self.FILTERS)
        self.write_tree(repo, files)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "--quiet", "-m", "init")
        self.git(repo, "remote", "add", "origin", str(origin))
        self.git(repo, "push", "--quiet", "origin", "main")
        return repo

    def svn_repo(self, name: str = "wc", with_config: bool = True) -> Path:
        """svnadmin create で作ったリポジトリと、その作業中のコピー。"""
        if not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn か svnadmin がありません")
        server = self.tmp / f"{name}-svnrepo"
        self.sh(["svnadmin", "create", str(server)])
        wc = self.tmp / name
        self.sh(["svn", "checkout", "--quiet", server.as_uri(), str(wc)])
        files = dict(self.SOURCES)
        if with_config:
            files[".coverity-triage/config.yaml"] = self.config_text("svn", self.tmp / f"{name}-out")
            files.update(self.FILTERS)
        self.write_tree(wc, files)
        self.sh(["svn", "add", "--quiet", "--force", "."], wc)
        self.sh(["svn", "commit", "--quiet", "-m", "init"], wc)
        self.sh(["svn", "update", "--quiet"], wc)
        return wc

    # ---- MCP サーバーが書くファイルの代わり ----------------------------------------------------

    def write_issues(self, run_dir: Path, issues: list[dict[str, Any]], analyzed: str | None = None) -> None:
        """search_issues（output_file あり）が done で書く issues.json の形。"""
        data = {"filter_sha": "0" * 12, "stream": STREAM, "limit": 20, "started_at": "2026-11-03T10:00:00",
                "finished_at": "2026-11-03T10:00:05", "coverity_total": len(issues), "next_offset": None,
                "limited": False, "revision_field": "sourceVersion", "revision_checked": True,
                "analyzed_revision": analyzed, "revision_error": None, "issues": issues, "excluded_count": 0,
                "excluded": [], "requests": []}
        (run_dir / "issues.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_issue_files(self, item_dir: str | Path, issues: list[dict[str, Any]]) -> None:
        """get_issues（output_dir あり）が書く issue-<CID>.json の形。"""
        for record in issues:
            detail = {"fetched_at": "2026-11-03T10:00:06", "issue": record,
                      "events": [{"file": record["file"], "line": record["line"] - 1, "tag": "returned_null",
                                  "description": "malloc が NULL を返しうる", "main": False},
                                 {"file": record["file"], "line": record["line"], "tag": "dereference",
                                  "description": "NULL かもしれないポインタを使う", "main": True}],
                      "checker_description": "NULL を返しうる関数の戻り値を確かめずに使っている"}
            Path(item_dir, f"issue-{record['cid']}.json").write_text(json.dumps(detail, ensure_ascii=False),
                                                                     encoding="utf-8")

    def new_run(self, repo: Path, *args: str) -> Path:
        return Path(self.ok("new-run", "--repo", str(repo), "--filter", "all.yaml", *args)["run_dir"])

    def work_item(self, run_dir: Path, issues: dict[int, dict[str, Any]], edit: dict[str, str],
                  result: dict[str, Any]) -> dict[str, Any]:
        """next → 警告経路のファイル → brief → 修正用のコピーを直す → result.json → finish。"""
        got = self.ok("next", "--run", str(run_dir))
        self.assertEqual(got["item"], result["item"])
        self.write_issue_files(got["item_dir"], [issues[c] for c in got["cids"]])
        self.ok("brief", "--run", str(run_dir), "--item", got["item"])
        fix_dir = run_dir / "work" / f"fix-{self.slot_of(run_dir, got['item'])}"
        self.write_tree(fix_dir, edit)
        Path(got["item_dir"], "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return self.ok("finish", "--run", str(run_dir), "--item", got["item"])

    def slot_of(self, run_dir: Path, item_id: str) -> int:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        return next(i["slot"] for i in state["items"] if i["id"] == item_id)


ISSUES = {
    101: issue(101, "NULL_RETURNS", "/build/ws/src/a.c", "read_a", 4, merge_key="mk101"),
    102: issue(102, "NULL_RETURNS", "/build/ws/src/a.c", "read_a", 5, merge_key="mk102"),
    103: issue(103, "RESOURCE_LEAK", "/other/root/src/b.c", "read_b", 5, merge_key="mk103"),
    104: issue(104, "CHECKED_RETURN", "src/zzz.c", "zzz", 3),
}
FIXED_A = "#include <stdlib.h>\nint read_a(void) {\n  int *p = malloc(4);\n  if (!p) return -1;\n  *p = 1;\n  return *p;\n}\n" \
          "int read_a2(void) {\n  int *q = malloc(4);\n  *q = 2;\n  return *q;\n}\n"
FIXED_B = "#include <stdio.h>\nint read_b(const char *path) {\n  FILE *fp = fopen(path, \"r\");\n" \
          "  if (!fp) return -1;\n  int c = fgetc(fp);\n  fclose(fp);\n  return c;\n}\n"


# ==== どのコマンドも JSON を1つ出す（REQ-062） ================================================


class OutputTest(CtCase):

    def test_req062_help_lists_commands(self) -> None:
        proc = subprocess.run([*self.cmd, "--help"], capture_output=True, timeout=TIMEOUT, env=self.env)
        self.assertEqual(proc.returncode, 0)
        text = proc.stdout.decode("utf-8")
        for name in ("new-run", "plan", "next", "brief", "finish", "summary", "preview", "apply-code", "selftest"):
            self.assertIn(name, text)

    def test_req062_any_error_prints_json(self) -> None:
        repo = self.git_repo()
        run_dir = self.new_run(repo)
        broken = {"run.json の形が違う": json.dumps({"run_id": 1}), "run.json が JSON でない": "{",
                  "run.json が空の配列": "[]"}
        commands = [["status"], ["plan"], ["next"], ["resume"], ["summary"], ["preview"], ["verify"],
                    ["initial-status"], ["initial-run"], ["knowledge-candidates"], ["tick", "--wait", "0"],
                    ["brief", "--item", "101"], ["finish", "--item", "101"], ["fail", "--item", "101", "--reason", "r"],
                    ["apply-code", "--token", "x"], ["add-knowledge", "--entry", "e"]]
        for label, text in broken.items():
            (run_dir / "run.json").write_text(text, encoding="utf-8")
            for command in commands:
                with self.subTest(label=label, command=command[0]):
                    self.ng(command[0], "--run", str(run_dir), *command[1:])
        missing = self.tmp / "nothing"
        for command in (["runs"], ["new-run"], ["stats"], ["init-config", "--url", "u", "--project", "p", "--stream", "s"],
                        ["trial-build", "--build-command", "true"], ["set-verify", "--build-command", "true"],
                        ["gui", "--no-browser"]):
            with self.subTest(command=command[0]):
                self.ng(command[0], "--repo", str(missing), *command[1:])
        with self.subTest(command="status（実行フォルダがない）"):
            self.ng("status", "--run", str(missing))

    def test_req062_argument_errors_print_json(self) -> None:
        # 引数の誤りも JSON を1つ出し、終了コード 1。使い方は標準エラーではなく error に入る
        cases = {"型が違う": (["new-run", "--repo", str(self.tmp), "--limit", "abc"], "ct.py new-run", "--limit"),
                 "必須の引数がない": (["selftest", "sample"], "ct.py selftest sample", "--dir"),
                 "コマンドがない": ([], "ct.py", "apply-code"),
                 "知らないコマンド": (["nosuch"], "ct.py", "nosuch"),
                 "知らない引数": (["runs", "--repo", str(self.tmp), "--bogus"], "ct.py", "--bogus")}
        for label, (args, usage, part) in cases.items():
            with self.subTest(label=label):
                data, code, err = self.run_ct(*args)
                self.assertIs(data.get("ok"), False, data)
                self.assertEqual(code, 1)
                self.assertEqual(set(data), {"ok", "error"})
                self.assertIn("引数が正しくありません", data["error"])
                self.assertIn(f"使い方: {usage} [-h]", data["error"])
                self.assertIn(part, data["error"])
                self.assertNotIn("usage:", err)
        # --help は今のまま（使い方を出して終了コード 0）
        proc = subprocess.run([*self.cmd, "new-run", "--help"], capture_output=True, timeout=TIMEOUT, env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--limit", proc.stdout.decode("utf-8"))

    def test_req062_unexpected_error_is_json_and_logged(self) -> None:
        repo = self.git_repo()
        run_dir = self.new_run(repo)
        (run_dir / "run.json").write_text(json.dumps({"run_id": 1}), encoding="utf-8")
        self.assertIn("ValidationError", self.ng("status", "--run", str(run_dir))["error"])
        # 実行フォルダが読めるときは、予期しない例外も operations.log に残る（画面の「重大なエラー」）
        run_dir = self.new_run(repo)
        self.write_issues(run_dir, [ISSUES[103]])
        self.ok("plan", "--run", str(run_dir))
        got = self.ok("next", "--run", str(run_dir))
        self.write_issue_files(got["item_dir"], [ISSUES[103]])
        (run_dir / "issues.mapped.json").write_text("[1]", encoding="utf-8")
        self.assertIn("TypeError", self.ng("brief", "--run", str(run_dir), "--item", "103")["error"])
        log = [json.loads(line) for line in (run_dir / "operations.log").read_text(encoding="utf-8").splitlines()]
        errors = [r for r in log if r.get("event") == "error" and r.get("command") == "brief"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["level"], "critical")
        self.assertIn("TypeError", errors[0]["error"])

    def test_req062_imports_in_a_shallow_folder(self) -> None:
        # 階層が4つより浅い場所に置いても、import で止まらない（IndexError を出さない）
        probe = ("import json, sys\n"
                 "src = open(sys.argv[1], encoding='utf-8').read()\n"
                 "g = {'__name__': 'ctlib_common_probe', '__file__': '/ctlib/common.py'}\n"
                 "exec(compile(src, '/ctlib/common.py', 'exec'), g)\n"
                 "try:\n    g['plugin_path']('server')\n    message = ''\n"
                 "except g['CtError'] as exc:\n    message = str(exc)\n"
                 "print(json.dumps({'root': g['PLUGIN_ROOT'], 'skills': g['SKILLS_DIR'], 'error': message},"
                 " ensure_ascii=False))\n")
        proc = subprocess.run([sys.executable, "-c", probe, str(SCRIPTS / "ctlib" / "common.py")], capture_output=True,
                              timeout=TIMEOUT, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        got = json.loads(proc.stdout.decode("utf-8"))
        self.assertIsNone(got["root"])
        self.assertIsNone(got["skills"])
        self.assertIn("プラグインのファイルが見つかりません", got["error"])

    def test_req062_outside_the_plugin_says_what_is_missing(self) -> None:
        # スクリプトのフォルダだけを、プラグインの外に写して動かす
        copy = self.tmp / "a" / "b" / "scripts"
        shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__"))
        cmd = [*self.cmd[:-1], str(copy / "ct.py")]
        repo = self.git_repo()
        self.assertEqual(self.ok("detect", "--repo", str(repo), cmd=cmd)["vcs"], "git")
        for args in (["install-agent"], ["prewarm"], ["selftest", "start", "--sections", "2", "--base", str(self.tmp / "st")]):
            with self.subTest(command=args[0]):
                self.assertIn("プラグインのファイルが見つかりません", self.ng(*args, cmd=cmd)["error"])
        checks = {c["check"]: c for c in self.ok("doctor", "--repo", str(repo), cmd=cmd)["checks"]}
        self.assertEqual(checks["MCP サーバの Python 環境"]["status"], "ng")
        self.assertIn("プラグインのファイルが見つかりません", checks["MCP サーバの Python 環境"]["detail"])
        self.assertEqual(checks["サブエージェント"]["status"], "ng")
        self.assertEqual(checks["設定ファイル"]["status"], "ok")
        # 実行フォルダと plan はプラグインがなくても動く。brief は、手順書のフォルダがないと言う
        run_dir = Path(self.ok("new-run", "--repo", str(repo), "--filter", "all", cmd=cmd)["run_dir"])
        self.write_issues(run_dir, [ISSUES[103]])
        self.ok("plan", "--run", str(run_dir), cmd=cmd)
        got = self.ok("next", "--run", str(run_dir), cmd=cmd)
        self.write_issue_files(got["item_dir"], [ISSUES[103]])
        error = self.ng("brief", "--run", str(run_dir), "--item", "103", cmd=cmd)["error"]
        self.assertIn("プラグインのファイルが見つかりません", error)
        self.assertIn("skills", error)


# ==== new-run の条件（REQ-102） =============================================================


class NewRunTest(CtCase):

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.git_repo()
        self.out = self.tmp / "repo-out"

    def filter_of(self, data: dict[str, Any]) -> dict[str, Any]:
        return json.loads(Path(data["filter_file"]).read_text(encoding="utf-8"))

    def runs(self) -> list[str]:
        return sorted(p.name for p in self.out.iterdir()) if self.out.is_dir() else []

    def test_req102_conditions_go_into_filter_json(self) -> None:
        data = self.ok("new-run", "--repo", str(self.repo), "--filter", "all", "--impact", "High", "--impact", "Medium",
                       "--checker", "MISRA C-2012*", "--checker", "NULL_RETURNS", "--classification", "Unclassified",
                       "--classification", "Pending", "--action", "Undecided", "--limit", "5")
        self.assertEqual(self.filter_of(data), {
            "name": "すべて", "project": "Proj", "streams": [STREAM], "checkers": ["MISRA C-2012*", "NULL_RETURNS"],
            "impacts": ["High", "Medium"],
            "triage": {"status": ["New", "Triaged"], "classification": ["Unclassified", "Pending"], "action": ["Undecided"]},
            "limit": 5})
        self.assertEqual(data["limit"], 5)
        self.assertEqual(data["stream"], STREAM)
        self.assertTrue(Path(data["run_dir"]).is_dir())
        self.assertEqual(data["issues_file"], str(Path(data["run_dir"]) / "issues.json"))

    def test_req102_status_replaces_only_status(self) -> None:
        got = self.filter_of(self.ok("new-run", "--repo", str(self.repo), "--filter", "all.yaml", "--status", "Fixed"))
        self.assertEqual(got["triage"], {"status": ["Fixed"], "classification": [], "action": []})
        self.assertEqual(got["impacts"], [])
        self.assertEqual(got["limit"], 20)  # 条件ファイルの limit

    def test_req102_filter_names_and_default(self) -> None:
        absolute = str(self.repo / ".coverity-triage" / "filters" / "all.yaml")
        for name in ("all", "all.yaml", ".coverity-triage/filters/all.yaml", absolute):
            with self.subTest(name=name):
                self.assertEqual(self.filter_of(self.ok("new-run", "--repo", str(self.repo), "--filter", name))["name"], "すべて")
        default = self.ok("new-run", "--repo", str(self.repo))
        self.assertEqual(self.filter_of(default)["name"], "未トリアージ")
        self.assertEqual(default["limit"], 10)
        missing = self.ng("new-run", "--repo", str(self.repo), "--filter", "misra.yaml")
        self.assertIn("misra", missing["error"])

    def test_req102_limit_must_be_1_to_max_items(self) -> None:
        before = self.runs()
        for value in ("0", "-3", "51", "1000"):
            with self.subTest(limit=value):
                data = self.ng("new-run", "--repo", str(self.repo), "--filter", "all", "--limit", value)
                self.assertIn("1 から 50", data["error"])
                self.assertEqual(data["max_items"], 50)
        self.assertEqual(self.runs(), before)  # 断ったときは実行フォルダを作らない
        for value in ("1", "50"):
            with self.subTest(limit=value):
                data = self.ok("new-run", "--repo", str(self.repo), "--filter", "all", "--limit", value)
                self.assertEqual(self.filter_of(data)["limit"], int(value))

    def test_req102_filter_limit_over_max_items_is_cut(self) -> None:
        data = self.ok("new-run", "--repo", str(self.repo), "--filter", "big")
        self.assertEqual(data["limit"], 50)
        self.assertEqual(self.filter_of(data)["limit"], 50)
        self.assertIn("max_items", data["note"])

    def test_req102_verify_and_initial_need_build_command(self) -> None:
        self.assertIn("ビルド", self.ng("new-run", "--repo", str(self.repo), "--filter", "all", "--verify", "build")["error"])
        self.assertIn("初回解析", self.ng("new-run", "--repo", str(self.repo), "--filter", "all", "--initial")["error"])
        self.assertFalse(self.ok("new-run", "--repo", str(self.repo), "--filter", "all", "--no-initial")["initial"])


# ==== 準備：detect・init-config・install-agent・doctor（REQ-062、REQ-106） ======================


class SetupTest(CtCase):

    def test_req062_detect_init_config_doctor_git(self) -> None:
        repo = self.git_repo("fresh", with_config=False)
        found = self.ok("detect", "--repo", str(repo))
        self.assertEqual((found["vcs"], found["base_branch"], found["has_config"]), ("git", "main", False))
        out = self.tmp / "fresh-results"
        written = self.ok("init-config", "--repo", str(repo), "--url", "https://cov.example.co.jp:8443/",
                          "--project", "Proj", "--stream", STREAM, "--output-dir", str(out),
                          "--strip-prefix", "/build/ws/")["written"]
        config_dir = repo / ".coverity-triage"
        self.assertEqual(sorted(Path(p).relative_to(config_dir).as_posix() for p in written),
                         ["config.yaml", "filters/untriaged.yaml", "knowledge.md"])
        text = (config_dir / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("url: https://cov.example.co.jp:8443\n", text)
        self.assertIn("/build/ws/", text)
        self.assertIn(STREAM, (config_dir / "filters" / "untriaged.yaml").read_text(encoding="utf-8"))
        self.assertIn("設定ファイルがもうあります", self.ng("init-config", "--repo", str(repo), "--url", "u",
                                                           "--project", "p", "--stream", "s")["error"])
        self.assertTrue(self.ok("detect", "--repo", str(repo))["has_config"])

        installed = self.ok("install-agent")
        self.assertEqual(Path(installed["folder"]), self.tmp / "copilot" / "agents")
        self.assertEqual(sorted(installed["agents"]), ["coverity-triage-collector", "coverity-triage-integrator",
                                                       "coverity-triage-reviewer", "coverity-triage-worker"])
        self.assertTrue(all(a["status"] == "installed" for a in installed["agents"].values()))
        self.assertTrue(all(a["status"] == "unchanged" for a in self.ok("install-agent")["agents"].values()))

        checks = {c["check"]: c["status"] for c in self.ok("doctor", "--repo", str(repo))["checks"]}
        for name in ("リポジトリ", "git コマンド", "設定ファイル", "条件ファイル", "出力先", "取り込み先のブランチ",
                     "git のリモート origin", "サブエージェント coverity-triage-worker"):
            self.assertEqual(checks.get(name), "ok", name)
        self.assertEqual(checks["Coverity のユーザ名"], "ng")  # 認証情報の環境変数がない
        self.assertIn("MCP サーバの Python 環境", checks)

    def test_req062_detect_init_config_doctor_svn(self) -> None:
        wc = self.svn_repo("fresh", with_config=False)
        found = self.ok("detect", "--repo", str(wc))
        self.assertEqual((found["vcs"], found["base_branch"]), ("svn", None))
        self.ok("init-config", "--repo", str(wc), "--url", "https://cov.example.co.jp:8443", "--project", "Proj",
                "--stream", STREAM, "--output-dir", str(self.tmp / "svn-results"))
        text = (wc / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("type: svn", text)
        self.assertNotIn("base_branch", text)
        checks = {c["check"]: c["status"] for c in self.ok("doctor", "--repo", str(wc))["checks"]}
        for name in ("リポジトリ", "svn コマンド", "設定ファイル", "条件ファイル", "出力先"):
            self.assertEqual(checks.get(name), "ok", name)

    def test_req062_doctor_reports_a_folder_that_is_not_a_repository(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        report = self.ok("doctor", "--repo", str(plain))
        self.assertFalse(report["ready"])
        self.assertEqual(report["checks"][0]["status"], "ng")
        self.assertIsNone(self.ok("detect", "--repo", str(plain))["vcs"])

    def test_req106_output_dir_outside_repo(self) -> None:
        repo = self.git_repo("inside", config={"output_dir": "results"})
        self.assertIn("リポジトリの中", self.ng("new-run", "--repo", str(repo), "--filter", "all")["error"])
        self.assertFalse((repo / "results").exists())
        checks = {c["check"]: c["status"] for c in self.ok("doctor", "--repo", str(repo))["checks"]}
        self.assertEqual(checks["出力先"], "ng")
        fresh = self.git_repo("fresh", with_config=False)
        self.assertIn("リポジトリの外", self.ng("init-config", "--repo", str(fresh), "--url", "u", "--project", "p",
                                                  "--stream", "s", "--output-dir", "out")["error"])
        self.assertFalse((fresh / ".coverity-triage" / "config.yaml").exists())


    def test_req102_set_verify_keeps_the_comments_of_config_yaml(self) -> None:
        """set-verify は変えた値の行だけを書き換え、人が書いた注釈は元の場所に残る。--check は何も書かない"""
        repo = self.git_repo("notes", with_config=False)
        self.ok("init-config", "--repo", str(repo), "--url", "https://cov.example.co.jp:8443", "--project", "Proj",
                "--stream", STREAM, "--output-dir", str(self.tmp / "notes-results"), "--strip-prefix", "/build/ws/")
        path = repo / ".coverity-triage" / "config.yaml"
        text = path.read_text(encoding="utf-8")
        text = (text.replace("  url: https://cov.example.co.jp:8443\n", "  url: https://cov.example.co.jp:8443   # 社内の Coverity\n")
                .replace("vcs:\n", "# --- ここからバージョン管理（チームで決めた） ---\nvcs:\n")
                .replace("  build_dir: ''\n", "  # makefile のあるフォルダ\n  build_dir: ''\n")
                + "agents:\n  integration: true   # 統合を使う（/coverity-setup のエージェントが書く形）\n")
        self.assertEqual(text.count("#"), 6)
        path.write_text(text, encoding="utf-8")
        args = ("set-verify", "--repo", str(repo), "--build-command", "make all", "--build-dir", "src")
        checked = self.ok(*args, "--check")
        self.assertEqual((checked["check"], checked["comments"]), (True, "kept"))
        self.assertNotIn("notice", checked)
        self.assertEqual(path.read_text(encoding="utf-8"), text, "--check が書いた")
        written = self.ok(*args)
        self.assertEqual(written["comments"], "kept")
        saved = path.read_text(encoding="utf-8")
        self.assertEqual(saved, text.replace("  default: none\n", "  default: build\n")
                         .replace("  build_dir: ''\n", "  build_dir: src\n")
                         .replace("  build_command: ''\n", "  build_command: make all\n"),
                         "変えた値の行のほかが変わった（注釈が動いたか消えた）")
        self.ok("doctor", "--repo", str(repo))
        # もう一度：ある verify の値だけが変わる
        self.ok("set-verify", "--repo", str(repo), "--build-command", "make -j4 all", "--build-dir", "src")
        self.assertEqual(path.read_text(encoding="utf-8"), saved.replace("build_command: make all", "build_command: make -j4 all"))

    def test_req102_set_verify_adds_only_values_that_differ_from_the_default(self) -> None:
        """ファイルにない項目は、既定の値から変えたときだけ足す（空の環境設定のコマンドなどは足さない）"""
        repo = self.git_repo("small", with_config=False)
        self.ok("init-config", "--repo", str(repo), "--url", "https://cov.example.co.jp:8443", "--project", "Proj",
                "--stream", STREAM, "--output-dir", str(self.tmp / "small-results"))
        path = repo / ".coverity-triage" / "config.yaml"
        small = f"# チームの設定\ncoverity:\n  url: https://cov.example.co.jp:8443   # 社内\nvcs:\n  type: git\noutput_dir: {self.tmp / 'small-results'}\n"
        path.write_text(small, encoding="utf-8")
        self.ok("set-verify", "--repo", str(repo), "--build-command", "make all", "--default", "build", "--no-initial")
        self.assertEqual(path.read_text(encoding="utf-8"), small + "verify:\n  default: build\n  build_command: make all\n")
        self.ok("doctor", "--repo", str(repo))

    def test_req102_set_verify_tells_when_the_comments_cannot_be_kept(self) -> None:
        """注釈を残せない書き方（「|」の値）なら、--check が lost と知らせの文を返し、書くときは全体を書き直す（値は正しい）"""
        repo = self.git_repo("block", with_config=False)
        self.ok("init-config", "--repo", str(repo), "--url", "https://cov.example.co.jp:8443", "--project", "Proj",
                "--stream", STREAM, "--output-dir", str(self.tmp / "block-results"))
        path = repo / ".coverity-triage" / "config.yaml"
        text = (path.read_text(encoding="utf-8").replace("vcs:\n", "# バージョン管理\nvcs:\n")
                .replace("  setup_command: ''\n", "  setup_command: |-\n    call env.bat\n")
                .replace("  build_command: ''\n", "  build_command: make   # 夜間\n"))
        path.write_text(text, encoding="utf-8")
        args = ("set-verify", "--repo", str(repo), "--build-command", "make all", "--setup-command", "call env.bat")
        checked = self.ok(*args, "--check")
        self.assertEqual(checked["comments"], "lost")
        self.assertIn("このファイルでは、注釈は動いたり消えたりします", checked["notice"])
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(self.ok(*args)["comments"], "lost")
        saved = path.read_text(encoding="utf-8")
        self.assertNotIn("# 夜間", saved)
        self.assertIn("build_command: make all", saved)
        self.assertIn("setup_command: call env.bat", saved)
        self.ok("doctor", "--repo", str(repo))


# ==== plan・next・brief・finish・summary・preview・apply-code（REQ-104、REQ-106） ===============


class GitFlowTest(CtCase):

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.git_repo()
        self.head = self.git(self.repo, "rev-parse", "HEAD")

    def planned(self, cids: tuple[int, ...] = (101, 102, 103)) -> tuple[Path, dict[str, Any]]:
        run_dir = self.new_run(self.repo)
        self.write_issues(run_dir, [ISSUES[c] for c in cids], analyzed=self.head)
        return run_dir, self.ok("plan", "--run", str(run_dir))

    def test_req104_plan_groups_and_maps_paths(self) -> None:
        run_dir, plan = self.planned((101, 102, 103, 104))
        self.assertEqual(plan["groups"], [{"id": "G1", "cids": [101, 102]}])
        self.assertEqual(plan["items"], 3)  # G1、103、104
        self.assertEqual(plan["analyzed_revision"], self.head)
        self.assertEqual(plan["latest_revision"], self.head)
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual([(i["id"], i["cids"]) for i in state["items"]], [("G1", [101, 102]), ("103", [103]), ("104", [104])])
        mapped = {m["cid"]: m for m in json.loads((run_dir / "issues.mapped.json").read_text(encoding="utf-8"))}
        self.assertEqual(mapped[101]["file"], "src/a.c")  # path_strip_prefixes
        self.assertEqual(mapped[101]["path_notes"], [])
        self.assertEqual(mapped[103]["file"], "src/b.c")  # 末尾の一致
        self.assertIn("自動で対応づけました", mapped[103]["path_notes"][0])
        self.assertEqual(mapped[104]["file"], "src/zzz.c")
        self.assertIn("対応づけられませんでした", mapped[104]["path_notes"][0])

    def test_req104_plan_keeps_no_grouping_cids_apart(self) -> None:
        (self.repo / ".coverity-triage" / "no-grouping.yaml").write_text("cids: [102]\n", encoding="utf-8")
        _, plan = self.planned()
        self.assertEqual(plan["groups"], [])
        self.assertEqual(plan["items"], 3)

    def test_req104_plan_needs_finished_search(self) -> None:
        run_dir = self.new_run(self.repo)
        self.assertIn("issues.json", self.ng("plan", "--run", str(run_dir))["error"])
        (run_dir / "issues.json").write_text(json.dumps({"issues": []}), encoding="utf-8")
        self.assertIn("search_issues", self.ng("plan", "--run", str(run_dir))["error"])

    def test_req104_next_makes_git_worktrees(self) -> None:
        run_dir, _ = self.planned((101, 102, 103, 104))
        first = self.ok("next", "--run", str(run_dir))
        self.assertEqual((first["item"], first["cids"], first["missing_issue_files"]), ("G1", [101, 102], [101, 102]))
        self.assertEqual(first["stream"], STREAM)
        self.assertEqual(first["can_start_more"], 1)
        fix = run_dir / "work" / "fix-1"
        self.assertTrue((fix / ".git").is_file())  # git worktree
        self.assertEqual(self.git(fix, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.git(run_dir / "work" / "analyzed", "rev-parse", "HEAD"), self.head)
        self.assertIn(str(fix), self.git(self.repo, "worktree", "list"))
        second = self.ok("next", "--run", str(run_dir))
        self.assertEqual((second["item"], self.slot_of(run_dir, "103")), ("103", 2))
        third = self.ok("next", "--run", str(run_dir))
        self.assertIsNone(third["item"])  # parallel 2 に達した
        self.assertIn("parallel", third["next"])
        self.assertIn("始まっています", self.ng("plan", "--run", str(run_dir))["error"])
        self.assertIn("警告経路のファイルがありません", self.ng("brief", "--run", str(run_dir), "--item", "G1")["error"])
        self.write_issue_files(first["item_dir"], [ISSUES[101], ISSUES[102]])
        brief = self.ok("brief", "--run", str(run_dir), "--item", "G1")
        text = Path(brief["brief"]).read_text(encoding="utf-8")
        for part in ("CID 101", "CID 102", "## グループ", str(fix), str(PLUGIN / "skills"), brief["result_file"],
                     "`src/a.c`:4", "triage-investigation"):
            self.assertIn(part, text)
        self.assertIn(brief["brief"], brief["prompt"])
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")  # 利用者のファイルは変わらない

    def test_req104_finish_checks_result_and_makes_outputs(self) -> None:
        run_dir, _ = self.planned((101, 102))
        got = self.ok("next", "--run", str(run_dir))
        self.write_issue_files(got["item_dir"], [ISSUES[101], ISSUES[102]])
        item_dir = Path(got["item_dir"])
        fix = run_dir / "work" / "fix-1"

        self.assertIn("結果ファイルがありません", self.ng("finish", "--run", str(run_dir), "--item", "G1")["error"])
        bad = {"形が違う": {"item": "G1", "verdict": {"judgement": "maybe"}},
               "item が違う": result_json("101"),
               "逸脱コメントが長い": result_json("G1", comment="あ" * 201),
               "全部を外す": {**result_json("G1"), "group_excluded_cids": [101, 102]}}
        for label, data in bad.items():
            with self.subTest(label=label):
                (item_dir / "result.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                refused = self.ng("finish", "--run", str(run_dir), "--item", "G1")
                self.assertTrue(refused["problems"], refused)
        (item_dir / "result.json").write_text(json.dumps(result_json("G1"), ensure_ascii=False), encoding="utf-8")
        self.assertIn("変更がありません", self.ng("finish", "--run", str(run_dir), "--item", "G1")["error"])
        log = [json.loads(line) for line in (run_dir / "operations.log").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(r.get("event") == "error" and r.get("command") == "finish" for r in log))

        self.write_tree(fix, {"src/a.c": FIXED_A})
        done = self.ok("finish", "--run", str(run_dir), "--item", "G1")
        self.assertEqual((done["recommendation"], done["confidence"]), ("fix", "high"))
        run_id = run_dir.name
        patch = run_dir / "patches" / "G1-fix.patch"
        self.assertIn("+  if (!p) return -1;", patch.read_text(encoding="utf-8"))
        self.assertEqual((run_dir / "fixed" / "G1" / "fix" / "src" / "a.c").read_text(encoding="utf-8"), FIXED_A)
        self.assertEqual(self.git(self.repo, "branch", "--list", f"coverity-fix/{run_id}-G1"), f"coverity-fix/{run_id}-G1")
        report = (run_dir / "cid" / "G1.html").read_text(encoding="utf-8")
        for part in ("案A: 逸脱", "案B: 修正", 'class="diff"', "101", "102"):
            self.assertIn(part, report)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        self.assertEqual((self.repo / "src" / "a.c").read_text(encoding="utf-8"), self.SOURCES["src/a.c"])

    def finished_run(self) -> Path:
        run_dir, _ = self.planned()
        self.work_item(run_dir, ISSUES, {"src/a.c": FIXED_A}, result_json("G1"))
        self.work_item(run_dir, ISSUES, {"src/b.c": FIXED_B}, result_json("103", recommendation="deviation"))
        return run_dir

    def test_req104_summary_writes_html_and_approvals(self) -> None:
        run_dir = self.finished_run()
        self.assertIn("一覧がありません", self.ng("preview", "--run", str(run_dir))["error"])
        summary = self.ok("summary", "--run", str(run_dir))
        self.assertEqual(Path(summary["summary"]), run_dir / "summary.html")
        self.assertEqual(summary["recommended"], {"fix": 1, "deviation": 1})
        self.assertEqual(sorted(summary["removed_work_copies"]), ["analyzed", "fix-1"])  # 空いた fix-1 を使い回す
        self.assertFalse((run_dir / "work" / "fix-1").exists())
        html = (run_dir / "summary.html").read_text(encoding="utf-8")
        self.assertIn("G1", html)
        self.assertIn("103", html)
        approvals = json.loads((run_dir / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual({k: v["decision"] for k, v in approvals["items"].items()}, {"G1": "fix", "103": "deviation"})
        self.assertNotIn(str(run_dir / "work"), self.git(self.repo, "worktree", "list"))
        status = self.ok("status", "--run", str(run_dir))
        self.assertEqual(status["counts"]["done"], 2)
        self.assertEqual(status["summary"], str(run_dir / "summary.html"))
        listed = self.ok("runs", "--repo", str(self.repo))["runs"]
        self.assertEqual(listed[0]["run_dir"], str(run_dir))
        self.assertTrue(listed[0]["summary"])

    def test_req103_status_gives_recent_steps_newest_first(self) -> None:
        """status の recent は、画面の「監視」と同じく新しい順の「HH:MM:SS 文」で、10 行まで。"""
        run_dir = self.new_run(self.repo)
        self.assertRegex(self.ok("status", "--run", str(run_dir))["recent"][0], r"^\d{2}:\d{2}:\d{2} 実行を始めた")
        records = [{"time": f"2026-11-03T10:00:{i:02d}", "event": "fail", "level": "critical", "source": "ct",
                    "item": str(200 + i), "reason": f"理由 {i}"} for i in range(11)]
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        lines.append("{書きかけの行")  # 読めない行は飛ばす
        lines.append(json.dumps({"time": "時刻なし", "event": "brief", "level": "info", "source": "ct", "item": "G9"},
                                ensure_ascii=False))
        (run_dir / "operations.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        recent = self.ok("status", "--run", str(run_dir))["recent"]
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0], "--:--:-- G9 の指示を作った（サブエージェントが調べる）")  # 時刻が読めない
        self.assertEqual(recent[1], "10:00:10 210 を処理できなかった: 理由 10")  # 新しいものが先
        self.assertEqual(recent[-1], "10:00:02 202 を処理できなかった: 理由 2")
        self.assertTrue(all(isinstance(line, str) for line in recent))
        (run_dir / "operations.log").unlink()
        self.assertEqual(self.ok("status", "--run", str(run_dir))["recent"], [])  # 記録がなくても止まらない

    def test_req106_preview_and_apply_code_push_once(self) -> None:
        run_dir = self.finished_run()
        self.ok("summary", "--run", str(run_dir))
        origin = self.tmp / "repo-origin.git"
        branch = f"coverity-fix/{run_dir.name}-G1"
        preview = self.ok("preview", "--run", str(run_dir))
        self.assertEqual(preview["counts"], {"修正": 1, "逸脱": 1, "却下": 0})
        token = preview["confirmation_token"]
        plan = json.loads(Path(preview["plan_file"]).read_text(encoding="utf-8"))
        self.assertEqual([(e["id"], e["cids"], e["classification"]) for e in plan["triage"]],
                         [("103", [103], "False Positive")])
        self.assertEqual([(c["id"], c["kind"], c["branch"]) for c in plan["code"]], [("G1", "fix", branch)])
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")  # 承認して反映するまで変えない
        self.assertEqual(self.git(origin, "branch", "--list"), "main")

        self.assertIn("確認用の文字列", self.ng("apply-code", "--run", str(run_dir), "--token", "000000000000")["error"])
        self.assertEqual(self.git(origin, "branch", "--list"), "main")

        first = self.ok("apply-code", "--run", str(run_dir), "--token", token)
        self.assertEqual([p["branch"] for p in first["pull_requests"]], [branch])
        self.assertEqual(first["coverity"], {"registered": [], "not_registered": ["103"]})
        self.assertIn("まだ Coverity に書き戻していません", first["next"])
        self.assertIn("update_triage", first["next"])
        self.assertIn(branch, self.git(origin, "branch", "--list"))

        # 同じ確認用の文字列でもう一度：push し直さず、前の結果を返す
        self.git(origin, "branch", "-D", branch)
        again = self.ok("apply-code", "--run", str(run_dir), "--token", token)
        self.assertTrue(again["repeated"])
        self.assertEqual(again["pull_requests"], first["pull_requests"])
        self.assertNotIn(branch, self.git(origin, "branch", "--list"))
        self.assertIn("作り直さない", again["next"])
        self.assertIn("まだ Coverity に書き戻していません", again["next"])
        self.assertNotIn("errors があれば", again["next"])  # 前の結果に errors がない

        # update_triage が書き戻した後なら、登録済みになる（push はしない）
        (run_dir / "apply-plan.result.json").write_text(json.dumps({
            "written": {"0123456789abcdef": {"id": "103", "cids": [103], "at": "2026-11-03T11:00:00"}},
            "failed": {}, "requests": []}), encoding="utf-8")
        third = self.ok("apply-code", "--run", str(run_dir), "--token", token)
        self.assertEqual(third["coverity"], {"registered": ["103"], "not_registered": []})
        self.assertNotIn("書き戻していません", third["next"])
        self.assertNotIn(branch, self.git(origin, "branch", "--list"))
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        applied = {i["id"]: i["applied"] for i in state["items"]}
        self.assertEqual(applied["103"]["coverity"], "登録済み")
        self.assertTrue(applied["G1"]["fix"]["pushed"])

        # もう一度 preview すると、反映したものは入らず、確認用の文字列も変わる
        later = self.ok("preview", "--run", str(run_dir))
        self.assertEqual(later["counts"], {"修正": 0, "逸脱": 0, "却下": 0})
        self.assertEqual(sorted(later["already_applied"]), ["103", "G1"])

    def write_triage_result(self, run_dir: Path, token: str, failed: dict[str, str],
                            written: tuple[str, ...] = (), failed_token: str | None = None) -> None:
        """update_triage（plan_file あり）が書く apply-plan.result.json の形。failed は {ID: error}。"""
        cids = {"G1": [101, 102], "103": [103]}
        (run_dir / "apply-plan.result.json").write_text(json.dumps({
            "written": {f"w{i:015d}": {"id": item, "cids": cids[item], "at": "2026-11-03T11:00:00"}
                        for i, item in enumerate(written)},
            "failed": {f"f{i:015d}": {"id": item, "cids": cids[item], "error": error, "token": failed_token or token,
                                      "at": "2026-11-03T11:00:00"} for i, (item, error) in enumerate(failed.items())},
            "requests": []}, ensure_ascii=False), encoding="utf-8")

    def test_req106_apply_code_says_why_coverity_did_not_take_items(self) -> None:
        run_dir, _ = self.planned()
        self.work_item(run_dir, ISSUES, {"src/a.c": FIXED_A}, result_json("G1", recommendation="deviation"))
        self.work_item(run_dir, ISSUES, {"src/b.c": FIXED_B}, result_json("103", recommendation="deviation"))
        self.ok("summary", "--run", str(run_dir))
        preview = self.ok("preview", "--run", str(run_dir))
        self.assertEqual(preview["counts"], {"修正": 0, "逸脱": 2, "却下": 0})
        token = preview["confirmation_token"]
        refused = "Coverity REST PUT /api/v2/issues/triage が失敗しました（400）: Invalid attribute value"
        broken = "Coverity REST PUT /api/v2/issues/triage が失敗しました（500）: Internal Server Error"

        # Coverity が断った項目（4xx）は値を直して preview から。5xx は少し待って同じ文字列で呼び直す
        self.write_triage_result(run_dir, token, {"G1": refused, "103": broken})
        first = self.ok("apply-code", "--run", str(run_dir), "--token", token)
        self.assertEqual(first["coverity"], {"registered": [], "not_registered": ["G1", "103"]})
        note = first["next"]
        self.assertIn(f"逸脱 1 件は、Coverity が書き戻しを断りました（G1: {refused}）", note)
        self.assertIn("値を直して ct.py preview からやり直す", note)
        self.assertIn(f"逸脱 1 件は、Coverity の不具合か一時的な失敗で書き戻せませんでした（103: {broken}）", note)
        self.assertIn("少し待って、MCP の update_triage を同じ確認用の文字列で呼び直し", note)
        self.assertNotIn("まだ Coverity に書き戻していません", note)  # 送っていない項目はない

        # HTTP の状態が 5xx の書き方は、どれも一時的な失敗として案内する（同じ文字列での呼び直しは _repeat が返す）
        for error in ("Coverity REST PUT /api/v2/issues/triage が失敗しました（503）: ", "HTTP 502 Bad Gateway",
                      "サーバーの 5xx エラー"):
            with self.subTest(error=error):
                self.write_triage_result(run_dir, token, {"103": error})
                again = self.ok("apply-code", "--run", str(run_dir), "--token", token)
                self.assertTrue(again["repeated"])
                self.assertIn(f"Coverity の不具合か一時的な失敗で書き戻せませんでした（103: {error}）", again["next"])
                self.assertNotIn("断りました", again["next"])
                self.assertIn("逸脱 1 件（G1）は、まだ Coverity に書き戻していません", again["next"])

        # 別の確認用の文字列（前の preview）で失敗した記録は、この反映ではまだ送っていない項目として扱う
        self.write_triage_result(run_dir, token, {"G1": refused}, written=("103",), failed_token="0" * 12)
        later = self.ok("apply-code", "--run", str(run_dir), "--token", token)
        self.assertEqual(later["coverity"], {"registered": ["103"], "not_registered": ["G1"]})
        self.assertIn("逸脱 1 件（G1）は、まだ Coverity に書き戻していません", later["next"])
        self.assertIn("update_triage", later["next"])
        self.assertNotIn("断りました", later["next"])
        self.assertNotIn("一時的な失敗", later["next"])

    def test_req106_apply_code_again_keeps_the_errors_note(self) -> None:
        run_dir = self.finished_run()
        self.ok("summary", "--run", str(run_dir))
        token = self.ok("preview", "--run", str(run_dir))["confirmation_token"]
        self.git(self.repo, "remote", "set-url", "origin", str(self.tmp / "missing-origin.git"))  # push できない
        first = self.ok("apply-code", "--run", str(run_dir), "--token", token)
        self.assertEqual(first["pull_requests"], [])
        self.assertEqual(len(first["errors"]), 1)
        self.assertIn("G1:fix", first["errors"][0])
        self.assertIn("errors があれば人に伝える", first["next"])
        # 同じ確認用の文字列でもう一度：前の errors は残り、その案内も next に付く
        again = self.ok("apply-code", "--run", str(run_dir), "--token", token)
        self.assertTrue(again["repeated"])
        self.assertEqual(again["errors"], first["errors"])
        self.assertIn("前の結果を返しました", again["next"])
        self.assertIn("errors があれば人に伝える（反映されなかった項目は、もう一度 /coverity-apply で反映できる）",
                      again["next"])
        self.assertIn("まだ Coverity に書き戻していません", again["next"])


# ==== svn の作業中のコピー（REQ-104、REQ-106） ================================================


class SvnFlowTest(CtCase):

    def test_req106_svn_checkout_finish_and_patch_working_copy(self) -> None:
        wc = self.svn_repo()
        run_dir = self.new_run(wc)
        record = issue(201, "NULL_RETURNS", "src/a.c", "read_a", 4)
        self.write_issues(run_dir, [record])
        plan = self.ok("plan", "--run", str(run_dir))
        self.assertEqual(plan["items"], 1)
        self.assertEqual(plan["latest_revision"], "1")
        got = self.ok("next", "--run", str(run_dir))
        fix = run_dir / "work" / "fix-1"
        self.assertTrue((fix / ".svn").is_dir())  # svn checkout
        self.write_issue_files(got["item_dir"], [record])
        self.ok("brief", "--run", str(run_dir), "--item", "201")
        self.write_tree(fix, {"src/a.c": FIXED_A, "src/new.h": "#define NEW 1\n"})
        Path(got["item_dir"], "result.json").write_text(json.dumps(result_json("201")), encoding="utf-8")
        self.ok("finish", "--run", str(run_dir), "--item", "201")
        patch = (run_dir / "patches" / "201-fix.patch").read_text(encoding="utf-8")
        self.assertIn("Index: src/a.c", patch)
        self.assertIn("Index: src/new.h", patch)
        self.assertTrue((run_dir / "cid" / "201.html").is_file())
        self.ok("summary", "--run", str(run_dir))
        self.assertEqual(self.sh(["svn", "status"], wc), "")  # 承認して反映するまで変えない
        self.assertEqual((wc / "src" / "a.c").read_text(encoding="utf-8"), self.SOURCES["src/a.c"])

        token = self.ok("preview", "--run", str(run_dir))["confirmation_token"]
        applied = self.ok("apply-code", "--run", str(run_dir), "--token", token)
        self.assertEqual(applied["svn_applied"], ["201:fix"])
        self.assertEqual(applied["pull_requests"], [])
        self.assertEqual((wc / "src" / "a.c").read_text(encoding="utf-8"), FIXED_A)
        status = self.sh(["svn", "status"], wc)
        self.assertIn("M       src/a.c", status)
        self.assertIn("A       src/new.h", status)
        again = self.ok("apply-code", "--run", str(run_dir), "--token", token)  # 二度は当てない
        self.assertTrue(again["repeated"])
        self.assertEqual((wc / "src" / "a.c").read_text(encoding="utf-8"), FIXED_A)
        self.assertEqual(self.sh(["svn", "status"], wc), status)


# ==== ビルドでの検証と初回解析のコマンド（REQ-104、REQ-106） ======================================


@unittest.skipIf(sys.platform == "win32", "偽の cov-* を shebang で動かすため、Windows では動かさない")
class CoverityCommandsTest(CtCase):

    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name in ("cov-build", "cov-analyze", "cov-format-errors", "cov-commit-defects"):
            tool = bin_dir / name
            tool.write_text(f"#!{sys.executable}\n{FAKE_COV_TOOL}", encoding="utf-8")
            tool.chmod(0o755)
        self.log = self.tmp / "cov.log"
        self.idir = self.tmp / "id dir"  # 空白を含む中間ディレクトリ
        self.env.update(PATH=f"{bin_dir}{os.pathsep}{self.env.get('PATH', '')}", FAKE_COV_LOG=str(self.log))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_req104_verify_build_analyze_commands_and_format_errors_error(self) -> None:
        repo = self.git_repo(config={"verify": {"build_command": "echo build", "cov_build_args": f'--dir "{self.idir}"',
                                                "cov_analyze_args": f'--dir "{self.idir}" --all'}})
        run_dir = self.new_run(repo)
        record = issue(301, "NULL_RETURNS", "/build/ws/src/a.c", "read_a", 4, merge_key="mk301")
        self.write_issues(run_dir, [record])
        self.ok("plan", "--run", str(run_dir))
        self.work_item(run_dir, {301: record}, {"src/a.c": FIXED_A}, result_json("301"))

        checked = self.ok("verify", "--run", str(run_dir), "--mode", "build+analyze")
        self.assertTrue(checked["build_ok"], checked)
        result = json.loads((run_dir / "verify.json").read_text(encoding="utf-8"))["301"]
        self.assertEqual(result["resolved_cids"], [301])
        analyze = [c for c in self.calls() if c["tool"] == "cov-analyze"]
        self.assertEqual(len(analyze), 2)  # 修正前と修正後
        for call in analyze:
            self.assertEqual(call["argv"][:2], ["--ticker-mode", "none"])
        # cov-format-errors の --dir は引用符でくくる（空白を含む中間ディレクトリが1つの引数のまま届く）
        formats = [c["argv"] for c in self.calls() if c["tool"] == "cov-format-errors"]
        self.assertEqual([a[0] for a in formats], ["--dir", "--dir"])
        self.assertEqual([a[1] for a in formats], [a["argv"][a["argv"].index("--dir") + 1] for a in analyze])
        self.assertEqual([a[2] for a in formats], ["--json-output-v7", "--json-output-v7"])
        self.assertTrue(all(a[1].startswith(str(self.idir)) for a in formats))

        # 修正後の結果の外枠に error があれば、解析の失敗として扱う（「直った」と数えない）
        failed = self.ok("verify", "--run", str(run_dir), "--mode", "build+analyze",
                         env={"FAKE_FORMAT_ERROR": "license expired"})
        self.assertFalse(failed["build_ok"])
        result = json.loads((run_dir / "verify.json").read_text(encoding="utf-8"))["301"]
        self.assertFalse(result["build_ok"])
        self.assertNotIn("resolved_cids", result)
        self.assertIn("license expired", (run_dir / "verify" / "after.log").read_text(encoding="utf-8"))

        # 修正前の結果に error があれば、修正前の解析の失敗として扱う
        before = self.ok("verify", "--run", str(run_dir), "--mode", "build+analyze",
                         env={"FAKE_FORMAT_ERROR": "no license", "FAKE_FORMAT_ERROR_AT": "before"})
        self.assertIn("修正前のコード", before["error"])
        result = json.loads((run_dir / "verify.json").read_text(encoding="utf-8"))["301"]
        self.assertIsNone(result["build_ok"])
        self.assertIn("no license", (run_dir / "verify" / "before.log").read_text(encoding="utf-8"))

    def test_req106_initial_analysis_commands_keep_the_key_off_the_command_line(self) -> None:
        repo = self.git_repo(config={
            "coverity": {"api": "auto", "fake_data": None, "user_env": "COVTEST_USER", "key_env": "COVTEST_KEY"},
            "verify": {"initial": True, "build_command": "echo build", "cov_build_args": f'--dir "{self.idir}"',
                       "cov_analyze_args": f'--dir "{self.idir}" --all', "cov_commit_args": "--ticker-mode no-spin"}})
        secret = "s3cret-key-value"
        env = {"COVTEST_USER": "alice", "COVTEST_KEY": secret}
        run_dir = Path(self.ok("new-run", "--repo", str(repo), "--filter", "all", "--initial", env=env)["run_dir"])
        done = self.ok("initial-run", "--run", str(run_dir), env=env)
        self.assertEqual(done["status"], "done", done)
        calls = {c["tool"]: c for c in self.calls()}
        self.assertEqual(calls["cov-analyze"]["argv"][:2], ["--ticker-mode", "none"])
        commit = calls["cov-commit-defects"]["argv"]
        self.assertEqual(commit.count("--ticker-mode"), 1)  # 設定にあれば、足さない
        self.assertEqual(commit[commit.index("--ticker-mode") + 1], "no-spin")
        self.assertEqual(commit[commit.index("--dir") + 1], str(self.idir))
        self.assertEqual(commit[commit.index("--stream") + 1], STREAM)
        self.assertEqual(commit[commit.index("--version") + 1], self.git(repo, "rev-parse", "HEAD"))
        self.assertNotIn(secret, " ".join(commit))
        self.assertEqual(calls["cov-commit-defects"]["passphrase"], secret)  # 登録だけに環境変数で渡す
        for tool in ("cov-build", "cov-analyze"):
            self.assertIsNone(calls[tool]["passphrase"])
            self.assertIsNone(calls[tool]["key_env"])
        log = (run_dir / "analysis" / "initial.log").read_text(encoding="utf-8")
        self.assertIn("cov-commit-defects", log)
        self.assertNotIn(secret, log)
        status = self.ok("initial-status", "--run", str(run_dir))
        self.assertEqual(status["status"], "done")


# ==== 動作確認の見本（REQ-062） ===============================================================


class SelftestTest(CtCase):

    def test_req062_selftest_sample_takes_assets(self) -> None:
        started = self.ok("selftest", "start", "--sections", "2", "--base", str(self.tmp / "st"))
        folder = started["dir"]
        self.assertEqual([c["id"] for c in started["checks"]][:2], ["2-1", "2-2"])
        self.assertIn("--assets に見本のフォルダ", self.ng("selftest", "sample", "--dir", folder)["error"])
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIn("sample-target", self.ng("selftest", "sample", "--dir", folder, "--assets", str(empty))["error"])
        made = self.ok("selftest", "sample", "--dir", folder, "--assets", str(ASSETS))
        repo = Path(made["repo_root"])
        self.assertEqual(repo, Path(folder) / "sample-target")
        self.assertTrue((repo / "src" / "reader.c").is_file())
        config = (repo / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn(str(Path(folder) / "out"), config)
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        self.assertIn("main", self.git(Path(folder) / "origin.git", "branch", "--list"))
        state = json.loads((Path(folder) / "selftest.json").read_text(encoding="utf-8"))
        self.assertEqual(state["checks"]["2-1"]["status"], "pass")
        self.assertEqual(Path(state["assets"]), ASSETS)
        self.assertTrue(self.ok("detect", "--repo", str(repo))["has_config"])
        report = self.ok("selftest", "report", "--dir", folder)
        self.assertTrue(Path(report["report"]).is_file())

    def test_req062_selftest_check_run_reads_expected_from_assets(self) -> None:
        folder = self.ok("selftest", "start", "--sections", "2", "--base", str(self.tmp / "st"))["dir"]
        repo = Path(self.ok("selftest", "sample", "--dir", folder, "--assets", str(ASSETS))["repo_root"])
        run_dir = self.new_run(repo, "--no-initial", "--verify", "none")
        record = {"cid": 20001, "checker": "RESOURCE_LEAK", "file": "src/reader.c", "function": "open_and_read",
                  "line": 20, "stream": "Sample-main"}
        self.write_issues(run_dir, [record])
        self.ok("plan", "--run", str(run_dir))
        source = (repo / "src" / "reader.c").read_text(encoding="utf-8")
        fixed = source.replace("    if (fgets(out, BUF_SIZE, fp) == NULL) {\n        return -1;",
                               "    if (fgets(out, BUF_SIZE, fp) == NULL) {\n        fclose(fp);\n        return -1;")
        self.assertNotEqual(fixed, source)
        self.work_item(run_dir, {20001: record}, {"src/reader.c": fixed}, result_json("20001"))
        self.ok("summary", "--run", str(run_dir))
        checks = self.ok("selftest", "check-run", "--dir", folder, "--run", str(run_dir))["checks"]
        self.assertEqual(checks["2-2"]["status"], "pass")
        self.assertEqual(checks["2-6"]["status"], "pass")
        self.assertEqual(checks["2-5"]["status"], "review")  # 20002・20003 の結果がない（expected.yaml を読んだ）
        state_file = Path(folder) / "selftest.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertIn("20002: 結果がありません", state["checks"]["2-5"]["detail"])
        del state["assets"]
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.assertIn("見本のフォルダが記録されていません",
                      self.ng("selftest", "check-run", "--dir", folder, "--run", str(run_dir))["error"])

    def test_req004_selftest_static_looks_where_doctor_looks(self) -> None:
        """UV_PROJECT_ENVIRONMENT があるとき、1-7 の「MCP サーバの Python 環境」は doctor と同じ場所を見る。"""
        folder = self.ok("selftest", "start", "--sections", "1", "--base", str(self.tmp / "st"))["dir"]
        repo = self.git_repo()
        made = self.tmp / "uv-env"
        made.mkdir()
        for env, shown, status in ((made, "あり", "ok"), (self.tmp / "no-env", "なし", "ng")):
            with self.subTest(environment=str(env)):
                given = {"UV_PROJECT_ENVIRONMENT": str(env)}
                check = self.ok("selftest", "static", "--dir", folder, env=given)["1-7"]
                self.assertIn(f"MCP サーバの Python 環境: {shown}", check["actual"])
                doctor = {c["check"]: c for c in self.ok("doctor", "--repo", str(repo), env=given)["checks"]}
                self.assertEqual(doctor["MCP サーバの Python 環境"]["status"], status)


if __name__ == "__main__":
    unittest.main()
