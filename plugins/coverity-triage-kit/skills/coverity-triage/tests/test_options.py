"""Background jobs, and the two options (build.check_fixes, build.initial_analysis): off by default,
working when they are on. The build and the cov-* commands are tiny stub commands on the PATH."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

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
call = {"name": name, "args": sys.argv[1:], "cwd": pathlib.Path.cwd().as_posix(),
        "passphrase": os.environ.get("COVERITY_PASSPHRASE"), "key": os.environ.get("COV_AUTH_KEY")}
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(call) + "\\n")
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
            if not (out["ok"] and "log" in out):  # the build still runs
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
        self.assertEqual(sorted(p.name for p in run.iterdir()),
                         ["cid", "issues.json", "log.txt", "run.json", "summary.md"])

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
        self.assertIn("| 確信度 | ビルド | 修正コード |", first["table"])
        self.assertIn(f"| {ONE} | NULL_RETURNS | src/a.c:3 | 本物のバグ | 修正を取り込む | 高 | 成功 |", first["table"])
        self.assertIn("build ok", (run / "build.log").read_text(encoding="utf-8"))

        self.one(run, TWO, {"src/b.c": (b"return 0;", b"return BROKEN;")})  # breaks the build
        self.one(run, THREE, {"src/a.c": (b"return *p;", b"return p ? *p : 9;")})  # does not fit on the fix of ONE
        second = self.finish(run)
        build = json.loads((run / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(build["cids"], {str(ONE): "unknown", str(TWO): "failed", str(THREE): "not_applied"})
        rows = {int(line.split("|")[1]): line.split("|")[7].strip() for line in second["table"].splitlines()[2:]}
        self.assertEqual(rows, {ONE: "不明", TWO: "失敗", THREE: "適用できず"})
        self.assertIn("- ビルド: 失敗", (run / "summary.md").read_text(encoding="utf-8"))
        self.assertFalse((run / "work").exists())
        self.assertTrue(self.finish(run)["ok"])  # asked again: the same answer, without another build
        self.assertEqual(json.loads((run / "build.json").read_text(encoding="utf-8")), build)

        # the fix of TWO is made again in the same file: it is built again
        self.run_triage("review", "--run", run, "--cid", TWO, "--outcome", "rework", "--point", "src/b.c:3 未定義の名前")
        self.start()
        self.one(run, TWO, {"src/b.c": (b"return BROKEN;", b"return 1;")})
        self.finish(run)
        again = json.loads((run / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(again["cids"], {str(ONE): "ok", str(TWO): "ok", str(THREE): "not_applied"})
        self.assertEqual(support.user_tree(self.kind, self.repo), before, "the person's files are not touched")


class GitBuild(Build, OptionCase):
    kind = "git"


class SvnBuild(Build, OptionCase):
    kind = "svn"


class AnalysisTest(OptionCase):

    def test_initial_analysis_and_registration(self):
        self.configure("build:\n  command: make all\n  initial_analysis: true\n  analyze_args: --all --strict\n")
        out = self.start("--slots", 1)
        self.assertEqual(out["fetch"][0]["tool"], "coverity_search_issues", out)
        run = Path(out["run_dir"])
        calls = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        folder = (run / "work" / "cov").as_posix()
        self.assertEqual([(c["name"], c["args"]) for c in calls], [
            ("cov-build", ["--dir", folder, "make", "all"]),
            ("cov-analyze", ["--dir", folder, "--all", "--strict"]),
            ("cov-commit-defects", ["--dir", folder, "--url", "https://coverity.example.co.jp:8443",
                                    "--stream", "app-main", "--user", "tester"])])
        self.assertEqual({c["cwd"] for c in calls}, {self.repo.as_posix()}, "in the person's tree")
        self.assertEqual([(c["passphrase"], c["key"]) for c in calls],
                         [(None, None), (None, None), ("s3cret-key", None)])
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

    def test_start_does_not_wait_for_the_copies_before_the_list_is_fetched(self):
        try:
            import triage
        except ImportError:
            self.skipTest("この Python に PyYAML がありません")
        args = argparse.Namespace(repo=str(self.repo), slots=None, review=False, new=False)
        self.addCleanup(setattr, triage, "WAIT", triage.WAIT)
        triage.WAIT = 0  # do not wait for a job at all
        first = triage.cmd_start(args)
        self.assertEqual(first["fetch"][0]["tool"], "coverity_search_issues", "asked for at once")
        run = Path(first["run_dir"])
        (run / "issues.json").write_text(json.dumps({"issues": [issue(ONE)]}), encoding="utf-8")
        while job.read(run, "prepare")["status"] == "running":
            time.sleep(0.1)
        made = job.read(run, "prepare")
        job.write_json(run / "work" / "prepare.json", {**made, "status": "running", "pid": os.getpid()})
        waiting = triage.cmd_start(args)  # with the list: the copies are needed now
        self.assertEqual((waiting["message"], waiting["log"]),
                         ("作業用コピーを準備しています", (run / "work" / "prepare.log").as_posix()))
        self.assertIn("start --repo", waiting["next"][0])
        job.write_json(run / "work" / "prepare.json", made)
        self.assertEqual(triage.cmd_start(args)["counts"]["total"], 1)

    def test_job_status(self):
        run = self.top / "run"
        (run / "work").mkdir(parents=True)
        args = {"repo": str(self.top / "nowhere"), "slots": 1, "base_branch": ""}
        failed = job.ensure(run, "prepare", args, time.monotonic() + 60)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("git または svn のリポジトリではありません", failed["error"])
        self.assertEqual(failed["log"], (run / "work" / "prepare.log").as_posix())
        self.assertIsNone(job.read(run, "prepare"), "reported once: the next call starts the job again")

        (run / "work" / "prepare.log").write_text("the log of an earlier job", encoding="utf-8")
        unlink = Path.unlink

        def held(path, **options):  # Windows: a program that the earlier job started still holds its log
            if path.suffix == ".log":
                raise PermissionError(13, "in use", str(path))
            unlink(path, **options)

        with mock.patch.object(Path, "unlink", held):
            ok = job.ensure(run, "prepare", {**args, "repo": str(self.repo)}, time.monotonic() + 60)
        self.assertEqual((ok["status"], ok["base_branch"]), ("done", "main"))
        self.assertNotIn("earlier job", (run / "work" / "prepare.log").read_text(encoding="utf-8"))
        again = job.ensure(run, "prepare", {**args, "repo": str(self.repo)}, time.monotonic())
        self.assertEqual((again["status"], again["started_at"], again["pid"]), ("done", ok["started_at"], ok["pid"]))

        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        job.write_json(run / "work" / "prepare.json", {"kind": "prepare", "status": "running", "pid": gone.pid})
        dead = job.read(run, "prepare")
        self.assertEqual(dead["status"], "failed")
        self.assertIn("途中で終了しました", dead["error"])

    def test_a_job_that_runs_with_other_arguments_is_not_the_answer(self):
        run = self.top / "run"
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])  # stands for a job for 1 slot
        threading.Thread(target=other.wait).start()  # when it has ended, it is gone (no zombie process)
        args = {"repo": str(self.repo), "slots": 2, "base_branch": ""}
        record = {"status": "running", "pid": other.pid, "args": {**args, "slots": 1}}
        job.write_json(run / "work" / "prepare.json", record)
        self.assertEqual(job.ensure(run, "prepare", args, time.monotonic())["status"], "running")
        self.assertFalse((run / "work" / "w1").exists(), "it is waited for, not doubled")
        made = job.ensure(run, "prepare", args, time.monotonic() + 60)
        self.assertEqual((made["status"], made["args"]["slots"]), ("done", 2))
        self.assertTrue((run / "work" / "w2" / "src" / "a.c").is_file())


if __name__ == "__main__":
    unittest.main()
