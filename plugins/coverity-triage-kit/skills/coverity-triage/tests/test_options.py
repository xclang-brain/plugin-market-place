"""Background jobs, and the two options (build.check_fixes, build.initial_analysis): off by default,
working when they are on. The build and the cov-* commands are tiny stub commands on the PATH."""

import argparse
import json
import os
import subprocess
import sys
import time
import unittest
import warnings
from pathlib import Path

import support
from support import issue
import job

ONE, TWO, THREE = 20001, 20002, 20003
BUILD_STUB = """import pathlib, sys
if "BROKEN" in pathlib.Path("src/b.c").read_text():
    print("src/b.c:3: error: 'BROKEN' undeclared")
    sys.exit(1)
print("build ok")
"""
COV_STUB = """import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).stem
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"name": name, "args": sys.argv[1:], "cwd": pathlib.Path.cwd().as_posix(),
                          "passphrase": os.environ.get("COVERITY_PASSPHRASE"), "key": os.environ.get("COV_AUTH_KEY")}) + "\\n")
print(name, "ran")
sys.exit(1 if os.environ.get("STUB_FAIL") == name else 0)
"""


class OptionCase(support.RunCase):

    def setUp(self):
        super().setUp()
        support.stub(self.top / "bin", "build-stub", BUILD_STUB)
        for name in ("cov-build", "cov-analyze", "cov-commit-defects"):
            support.stub(self.top / "bin", name, COV_STUB)
        self.calls = self.top / "calls.log"
        self.env = {**os.environ, "PATH": f"{self.top / 'bin'}{os.pathsep}{os.environ['PATH']}",
                    "STUB_LOG": str(self.calls), "COV_USER": "tester", "COV_AUTH_KEY": "s3cret-key"}

    def finish(self, run) -> dict:
        while True:
            out = self.run_triage("finish", "--run", run)
            if not out.get("wait"):
                return out

    def one(self, run, cid, edits=None) -> dict:
        return self.work(self.run_triage("next", "--run", run, "--cid", cid), edits)


class Build:

    def test_off_by_default(self):
        self.configure("build:\n  command: build-stub\n")
        run = self.begin([issue(ONE)], "--slots", 1)
        self.one(run, ONE)
        self.assertTrue(self.finish(run)["ok"])
        self.assertFalse(self.calls.exists(), "no cov-* command ran")
        self.assertEqual(sorted(p.name for p in run.iterdir()), ["cid", "issues.json", "log.txt", "run.json", "summary.md"])

    def test_build_check_of_the_fixes(self):
        self.configure("build:\n  check_fixes: true\n")
        before = support.user_tree(self.kind, self.repo)
        run = self.begin([issue(ONE), issue(TWO, "src/b.c"), issue(THREE)])
        self.one(run, ONE)
        self.assertIn("build.command", self.finish(run)["error"])

        self.configure("build:\n  command: build-stub\n  check_fixes: true\n")
        first = self.finish(run)
        self.assertTrue(first["ok"], first)
        build = json.loads((run / "build.json").read_text(encoding="utf-8"))
        self.assertEqual((build["status"], build["exit_code"], build["cids"]), ("done", 0, {str(ONE): "ok"}))
        self.assertIn(f"| {ONE} | NULL_RETURNS | src/a.c:3 | 本物のバグ | 修正を取り込む | 高 | - | 成功 |", first["table"])
        self.assertIn("build ok", (run / "build.log").read_text(encoding="utf-8"))

        self.one(run, TWO, {"src/b.c": (b"return 0;", b"return BROKEN;")})  # breaks the build
        self.one(run, THREE, {"src/a.c": (b"return *p;", b"return p ? *p : 9;")})  # does not fit on the fix of ONE
        second = self.finish(run)
        build = json.loads((run / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(build["cids"], {str(ONE): "unknown", str(TWO): "failed", str(THREE): "not_applied"})
        rows = {int(line.split("|")[1]): line.split("|")[8].strip() for line in second["table"].splitlines()[2:]}
        self.assertEqual(rows, {ONE: "-", TWO: "失敗", THREE: "適用できず"})
        self.assertIn("- ビルド: 失敗", (run / "summary.md").read_text(encoding="utf-8"))
        self.assertFalse((run / "work").exists())
        self.assertTrue(self.finish(run)["ok"])  # asked again: the same answer, without another build
        self.assertEqual(json.loads((run / "build.json").read_text(encoding="utf-8")), build)
        self.assertEqual(support.user_tree(self.kind, self.repo), before, "the person's files are not touched")


class GitBuild(Build, OptionCase):
    kind = "git"


class SvnBuild(Build, OptionCase):
    kind = "svn"


class AnalysisTest(OptionCase):

    def test_initial_analysis_and_registration(self):
        self.configure("build:\n  command: make all\n  initial_analysis: true\n  analyze_args: --all --strict\n")
        out = self.start("--slots", 1)
        self.assertTrue(out["need_fetch"], out)
        run = Path(out["run_dir"])
        calls = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        folder = (run / "work" / "cov").as_posix()
        self.assertEqual([(c["name"], c["args"]) for c in calls], [
            ("cov-build", ["--dir", folder, "make", "all"]),
            ("cov-analyze", ["--dir", folder, "--all", "--strict"]),
            ("cov-commit-defects", ["--dir", folder, "--url", "https://coverity.example.co.jp:8443", "--stream", "app-main",
                                    "--user", "tester"])])
        self.assertEqual({c["cwd"] for c in calls}, {self.repo.as_posix()}, "in the person's tree")
        self.assertEqual([(c["passphrase"], c["key"]) for c in calls], [(None, None), (None, None), ("s3cret-key", None)])
        log = (run / "analyze.log").read_text(encoding="utf-8")
        self.assertIn("cov-commit-defects ran", log)
        self.assertNotIn("s3cret-key", log + (run / "work" / "analyze.json").read_text(encoding="utf-8"))
        self.start("--slots", 1)
        self.assertEqual(len(self.calls.read_text(encoding="utf-8").splitlines()), 3, "not analysed a second time")

        self.env["STUB_FAIL"] = "cov-analyze"
        failed = self.start("--new")
        self.assertFalse(failed["ok"])
        self.assertIn("最初の解析と登録が失敗しました: 解析（cov-analyze）が失敗しました（終了コード 1）", failed["error"])
        self.assertEqual(failed["log"], (Path(failed["run_dir"]) / "analyze.log").as_posix())
        self.assertIn("start --repo", failed["next"][0])
        del self.env["STUB_FAIL"], self.env["COV_AUTH_KEY"]
        self.assertIn("環境変数 COV_USER と COV_AUTH_KEY を設定してください", self.start()["error"])
        self.configure("build:\n  initial_analysis: true\n")
        self.assertIn("build.command", self.start()["error"])


class JobTest(support.RunCase):

    def setUp(self):
        super().setUp()
        warnings.simplefilter("ignore", ResourceWarning)  # a job outlives the call that started it, on purpose

    def test_a_long_job_is_polled(self):
        try:
            import triage
        except ImportError:
            self.skipTest("この Python に PyYAML がありません")
        args = argparse.Namespace(repo=str(self.repo), slots=None, review=False, new=False)
        self.addCleanup(setattr, triage, "WAIT", triage.WAIT)
        triage.WAIT = 0  # do not wait for the job at all
        first = triage.cmd_start(args)
        self.assertEqual((first["ok"], first["wait"]), (True, True))
        self.assertIn("作業用コピーの準備を実行しています", first["message"])
        self.assertIn("start --repo", first["next"][0])
        self.assertEqual(first["log"], (Path(first["run_dir"]) / "work" / "prepare.log").as_posix())
        triage.WAIT = 60
        self.assertEqual(triage.cmd_start(args)["need_fetch"], True)

    def test_job_status(self):
        run = self.top / "run"
        (run / "work").mkdir(parents=True)
        args = {"repo": str(self.top / "nowhere"), "slots": 1, "base_branch": ""}
        failed = job.ensure(run, "prepare", args, time.monotonic() + 60)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("git または svn のリポジトリではありません", failed["error"])
        self.assertEqual(failed["log"], (run / "work" / "prepare.log").as_posix())
        self.assertIsNone(job.read(run, "prepare"), "reported once: the next call starts the job again")

        ok = job.ensure(run, "prepare", {**args, "repo": str(self.repo)}, time.monotonic() + 60)
        self.assertEqual((ok["status"], ok["base_branch"]), ("done", "main"))
        again = job.ensure(run, "prepare", {**args, "repo": str(self.repo)}, time.monotonic())
        self.assertEqual((again["status"], again["started_at"], again["pid"]), ("done", ok["started_at"], ok["pid"]))

        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        job.write_json(run / "work" / "prepare.json", {"kind": "prepare", "status": "running", "pid": gone.pid})
        dead = job.read(run, "prepare")
        self.assertEqual(dead["status"], "failed")
        self.assertIn("途中で終了しました", dead["error"])


if __name__ == "__main__":
    unittest.main()
