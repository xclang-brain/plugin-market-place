"""Detached jobs: the initial analysis (fake cov-* tools), the build verification, and stop."""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path

from support import RunCase, import_runlib, issue, wait_for

FAKE_TOOL = r'''
import json, os, sys, time
name = os.path.basename(sys.argv[0]).split(".")[0]
with open(os.environ["FAKE_COV_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"tool": name, "argv": sys.argv[1:], "passphrase": os.environ.get("COVERITY_PASSPHRASE"),
                        "key": os.environ.get("COVTEST_KEY")}) + "\n")
if os.environ.get("FAKE_COV_SLEEP_" + name.replace("-", "_").upper()):
    time.sleep(float(os.environ["FAKE_COV_SLEEP_" + name.replace("-", "_").upper()]))
sys.exit(int(os.environ.get("FAKE_COV_FAIL_" + name.replace("-", "_").upper(), "0")))
'''

ISSUES = [issue(20001, line=6)]


def fake_tools(folder: Path) -> Path:
    bin_dir = folder / "fakebin"
    bin_dir.mkdir()
    for name in ("cov-build", "cov-analyze", "cov-commit-defects", "cov-format-errors"):
        script = bin_dir / f"{name}.py"
        script.write_text(FAKE_TOOL, encoding="utf-8")
        if sys.platform == "win32":
            (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n', encoding="utf-8")
        else:
            launcher = bin_dir / name
            launcher.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


class InitialAnalysisTest(RunCase):
    settings = {"initial": "true", "build_command": "make"}

    def setUp(self) -> None:
        super().setUp()
        self.env["PATH"] = str(fake_tools(self.tmp)) + os.pathsep + self.env.get("PATH", "")
        self.env["FAKE_COV_LOG"] = str(self.tmp / "cov.log")

    def calls(self) -> list[dict]:
        path = self.tmp / "cov.log"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def test_initial_analysis_then_fetch(self) -> None:
        created = self.new_run()
        self.assertTrue(created["initial"])
        out = self.tick(created["run_dir"], wait=20)
        while out["stage"] == "initial" and not out["stop"]:
            out = self.tick(created["run_dir"], wait=20)
        self.assertEqual(out["stage"], "fetch", out)
        self.assertEqual([c["tool"] for c in self.calls()], ["cov-build", "cov-analyze"])  # fake api: no commit
        status = json.loads((Path(created["run_dir"]) / "analysis" / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "done")
        log = (Path(created["run_dir"]) / "analysis" / "initial.log").read_text(encoding="utf-8")
        self.assertIn("commit skipped because coverity.api is fake", log)
        view = self.run_json("initial-status", "--run", created["run_dir"])
        self.assertEqual((view["used"], view["value"], view["status"]), (True, 100, "done"))

    def test_commit_gets_the_key_only_in_the_environment(self) -> None:
        config = self.repo / ".coverity-triage" / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8").replace("api: fake", "api: auto"), encoding="utf-8")
        self.env.update(COVTEST_USER="alice", COVTEST_KEY="s3cret-key-value")
        run_dir = self.new_run("--initial")["run_dir"]
        out = self.tick(run_dir, wait=20)
        while out["stage"] == "initial" and not out["stop"]:
            out = self.tick(run_dir, wait=20)
        calls = {c["tool"]: c for c in self.calls()}
        commit = calls["cov-commit-defects"]
        self.assertEqual(commit["passphrase"], "s3cret-key-value")
        self.assertNotIn("s3cret-key-value", " ".join(commit["argv"]))
        self.assertIsNone(calls["cov-build"]["key"])
        self.assertIsNone(calls["cov-build"]["passphrase"])
        log = (Path(run_dir) / "analysis" / "initial.log").read_text(encoding="utf-8")
        self.assertNotIn("alice", log)
        self.assertIn("****", log)

    def test_failed_analysis_stops_and_resume_tries_again(self) -> None:
        self.env["FAKE_COV_FAIL_COV_ANALYZE"] = "3"
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir, wait=20)
        while out["stage"] == "initial" and not out["stop"]:
            out = self.tick(run_dir, wait=20)
        self.assertTrue(out["stop"], out)
        self.assertIn("解析（cov-analyze）", out["error"])
        status = self.run_json("status", "--run", run_dir)
        self.assertTrue(status["halted"])
        self.assertIn("cov-analyze", status["halt_reason"])
        self.assertTrue(any(e["kind"] == "初回解析" and e["level"] == "critical" for e in status["errors"]))
        self.assertIn("失敗", status["initial"]["label"])
        del self.env["FAKE_COV_FAIL_COV_ANALYZE"]
        self.run_json("resume", "--run", run_dir)
        out = self.tick(run_dir, wait=20)
        while out["stage"] == "initial" and not out["stop"]:
            out = self.tick(run_dir, wait=20)
        self.assertEqual(out["stage"], "fetch", out)

    def test_stop_ends_the_running_job(self) -> None:
        self.env["FAKE_COV_SLEEP_COV_BUILD"] = "120"
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir, wait=1)
        self.assertEqual(out["stage"], "initial")
        self.assertTrue(wait_for(lambda: any(c["tool"] == "cov-build" for c in self.calls()), 30))
        job = json.loads((Path(run_dir) / "jobs" / "initial.json").read_text(encoding="utf-8"))
        pids = [job["pid"], *job["children"]]
        stopped = self.run_json("stop", "--run", run_dir)
        self.assertEqual(stopped["stopped_jobs"], ["initial"])
        self.assertEqual(stopped["unstopped"], [])
        import_runlib()
        from runlib.common import pid_alive

        self.assertTrue(wait_for(lambda: not any(pid_alive(p) for p in pids), 10), pids)
        self.assertTrue(self.state(run_dir)["stopped"])
        out = self.tick(run_dir)
        self.assertTrue(out["stop"])
        status = self.run_json("status", "--run", run_dir)
        self.assertTrue(status["stopped"])
        self.assertFalse(status["halted"])  # a person stopped it: not a run that cannot go on
        self.assertEqual(status["stage"], "stopped")


class VerifyTest(RunCase):
    def build_script(self, code: int, text: str = "") -> str:
        script = self.tmp / "build.py"
        script.write_text(f"import sys\nprint({text!r})\nsys.exit({code})\n", encoding="utf-8")
        return json.dumps(f'"{sys.executable}" "{script}"')

    def test_build_ok(self) -> None:
        self.write_config(verify="build", build_command=self.build_script(0, "built"))
        run_dir = self.new_run()["run_dir"]
        out = self.drive(run_dir, ISSUES)
        self.assertTrue(out["done"], out)
        verify = json.loads((Path(run_dir) / "verify.json").read_text(encoding="utf-8"))
        self.assertTrue(verify["20001"]["build_ok"])
        summary = self.run_json("summary", "--run", run_dir)
        self.assertEqual(summary["results"][0]["confidence"], "high")

    def test_failed_build_lowers_a_fix_to_low(self) -> None:
        self.write_config(verify="build", build_command=self.build_script(1, "src/reader.c:7: error: x"))
        run_dir = self.new_run()["run_dir"]
        self.assertTrue(self.drive(run_dir, ISSUES)["done"])
        verify = json.loads((Path(run_dir) / "verify.json").read_text(encoding="utf-8"))
        self.assertFalse(verify["20001"]["build_ok"])
        summary = self.run_json("summary", "--run", run_dir)
        self.assertEqual(summary["results"][0]["confidence"], "low")
        report = (Path(run_dir) / "cid" / "20001.html").read_text(encoding="utf-8")
        self.assertIn("ビルドでの検証", report)
        self.assertTrue({c["id"]: c for c in summary["run_checks"]}["C-19"]["ok"])


class JobRecordTest(unittest.TestCase):
    """How a job record reads when its process is gone, or still alive after stop."""

    def setUp(self) -> None:
        import shutil
        import tempfile

        import_runlib()
        from runlib import jobs

        self.jobs = jobs
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "jobs").mkdir()
        self.run_state({"stopped": False})

    def run_state(self, data: dict) -> None:
        (self.dir / "run.json").write_text(json.dumps(data), encoding="utf-8")

    def record(self, name: str, **fields) -> Path:
        import time

        data = {"name": name, "kind": "copy", "status": "running", "pid": None, "started": "x",
                "started_ts": time.time() - 60, "finished": None, "args": {}, "result": None, "error": None,
                "children": [], **fields}
        path = self.dir / "jobs" / f"{name}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def on_disk(self, name: str) -> dict:
        return json.loads((self.dir / "jobs" / f"{name}.json").read_text(encoding="utf-8"))

    @staticmethod
    def dead_pid() -> int:
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def live(self):
        import subprocess

        # a session of its own, as spawn_detached starts jobs (stop ends the whole group)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                **({} if sys.platform == "win32" else {"start_new_session": True}))
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_dead_job_is_written_as_failed(self) -> None:
        self.record("copy-w1", pid=self.dead_pid())
        job = self.jobs.read(self.dir, "copy-w1")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.on_disk("copy-w1")["status"], "running")  # read writes nothing
        self.jobs.settle(self.dir, job)
        self.assertEqual(self.on_disk("copy-w1")["status"], "failed")
        self.assertEqual(self.on_disk("copy-w1")["error"], self.jobs.DEAD)

    def test_stopped_job_whose_process_lives_counts_as_running(self) -> None:
        proc = self.live()
        self.record("copy-w1", pid=proc.pid, status="stopped", stopping=True)
        self.assertEqual(self.jobs.read(self.dir, "copy-w1")["status"], "running")
        self.assertEqual([j["name"] for j in self.jobs.running(self.dir)], ["copy-w1"])
        self.record("copy-w1", pid=proc.pid, status="stopped", stopping=True, gone=True)
        self.assertEqual(self.jobs.read(self.dir, "copy-w1")["status"], "stopped")

    def test_stopped_record_without_stopping_is_final(self) -> None:
        from unittest import mock

        # written by an earlier version (no "stopping"): its pid may belong to another process now
        proc = self.live()
        self.record("copy-w1", pid=proc.pid, status="stopped")
        self.assertEqual(self.jobs.read(self.dir, "copy-w1")["status"], "stopped")
        self.assertEqual(self.jobs.running(self.dir), [])
        with mock.patch.object(self.jobs, "kill_tree", return_value=True) as kill:
            self.assertEqual(self.jobs.stop_all(self.dir), ([], []))
        kill.assert_not_called()
        self.assertIsNone(proc.poll())

    def test_a_job_being_stopped_is_written_gone_once_its_process_ended(self) -> None:
        from unittest import mock

        # stop was ended between "stopping" and "gone": the process ends later
        proc = self.live()
        self.record("copy-w1", pid=proc.pid, status="stopped", stopping=True)
        self.assertEqual(self.jobs.read(self.dir, "copy-w1")["status"], "running")
        proc.kill()
        proc.wait()
        job = self.jobs.read(self.dir, "copy-w1")
        self.assertEqual(job["status"], "stopped")
        self.jobs.settle(self.dir, job)
        self.assertTrue(self.on_disk("copy-w1")["gone"])
        with mock.patch.object(self.jobs, "pid_alive", return_value=True):  # the number is given to another process
            self.assertEqual(self.jobs.read(self.dir, "copy-w1")["status"], "stopped")
        # stop also writes it down for such a job
        self.record("copy-w2", pid=self.dead_pid(), status="stopped", stopping=True)
        self.jobs.stop_all(self.dir)
        self.assertTrue(self.on_disk("copy-w2")["gone"])
        # settle leaves a job whose process still lives as it is
        live = self.live()
        self.record("copy-w3", pid=live.pid, status="stopped", stopping=True)
        self.jobs.settle(self.dir, {**self.on_disk("copy-w3")})
        self.assertNotIn("gone", self.on_disk("copy-w3"))

    def test_stop_writes_stopping_before_the_kill(self) -> None:
        from unittest import mock

        proc = self.live()
        self.record("copy-w1", pid=proc.pid)
        seen = []

        def kill(pid):
            seen.append(self.on_disk("copy-w1"))
            return True
        with mock.patch.object(self.jobs, "kill_tree", kill):
            self.assertEqual(self.jobs.stop_all(self.dir), (["copy-w1"], []))
        self.assertEqual((seen[0]["status"], seen[0].get("stopping"), seen[0].get("gone")), ("stopped", True, None))
        self.assertTrue(self.on_disk("copy-w1")["gone"])

    def test_a_job_whose_stop_failed_sees_that_it_is_stopped(self) -> None:
        proc = self.live()
        self.record("copy-w1", pid=proc.pid, status="stopped", stopping=True)
        self.assertTrue(self.jobs.Handle(self.dir, "copy-w1").stopped())
        self.record("copy-w2", pid=proc.pid)
        self.assertFalse(self.jobs.Handle(self.dir, "copy-w2").stopped())

    def test_start_does_not_fail_once_the_job_was_started(self) -> None:
        from unittest import mock

        def busy(path, *args, **kwargs):
            raise PermissionError(32, "The process cannot access the file", str(path))
        with mock.patch.object(self.jobs, "spawn_detached", return_value=4242), \
                mock.patch.object(self.jobs, "file_lock", busy):
            job = self.jobs.start(self.dir, "copy-w1", "copy", {"item": "20001", "slot": 1})
        self.assertEqual((job["status"], job["pid"], job["args"]["item"]), ("running", 4242, "20001"))

    def test_a_failed_kill_leaves_the_job_busy(self) -> None:
        from unittest import mock

        proc = self.live()
        self.record("copy-w1", pid=proc.pid)
        with mock.patch.object(self.jobs, "kill_tree", return_value=False):
            stopped, unstopped = self.jobs.stop_all(self.dir)
        self.assertEqual((stopped, [u["name"] for u in unstopped]), ([], ["copy-w1"]))
        self.assertEqual(self.on_disk("copy-w1")["status"], "stopped")
        self.assertEqual([j["name"] for j in self.jobs.running(self.dir)], ["copy-w1"])  # its process still works
        stopped, unstopped = self.jobs.stop_all(self.dir)  # stop again: killed now
        self.assertEqual((stopped, unstopped), (["copy-w1"], []))
        self.assertTrue(self.on_disk("copy-w1")["gone"])
        self.assertEqual(self.jobs.running(self.dir), [])

    def test_job_started_after_stop_does_nothing(self) -> None:
        called = []
        self.jobs.KINDS["probe"] = lambda run_dir, args, handle: called.append(1) or {"x": 1}
        self.addCleanup(self.jobs.KINDS.pop, "probe", None)
        self.record("probe", kind="probe")
        self.run_state({"stopped": True})
        self.assertEqual(self.jobs.execute(str(self.dir), "probe")["status"], "stopped")
        self.record("probe", kind="probe", status="stopped")
        self.run_state({"stopped": False})
        self.assertEqual(self.jobs.execute(str(self.dir), "probe")["status"], "stopped")
        self.assertEqual(called, [])
        self.assertTrue(self.on_disk("probe")["gone"])
        self.record("probe", kind="probe")
        self.assertEqual(self.jobs.execute(str(self.dir), "probe")["status"], "done")
        self.assertEqual(called, [1])

    def test_stop_clears_the_lock_of_a_killed_holder(self) -> None:
        lock = self.dir / "work" / "vcs.lock"
        lock.parent.mkdir()
        lock.write_text(str(self.dead_pid()))
        job_lock = self.dir / "jobs" / "save-20001.json.lock"
        job_lock.write_text(str(self.dead_pid()))
        self.jobs.stop_all(self.dir)
        self.assertFalse(lock.exists())
        self.assertFalse(job_lock.exists())
        lock.write_text(str(self.live().pid))
        self.jobs.stop_all(self.dir)
        self.assertTrue(lock.exists())

    def test_start_goes_on_when_the_old_log_is_held_on_windows(self) -> None:
        from unittest import mock

        log = self.dir / "jobs" / "copy-w1.log"
        log.write_text("the earlier job", encoding="utf-8")

        def held(path):
            raise PermissionError(32, "The process cannot access the file", str(path))
        with mock.patch.object(self.jobs, "remove_file", held), mock.patch.object(self.jobs, "spawn_detached", return_value=4242), \
                mock.patch.object(self.jobs, "pid_alive", return_value=True), mock.patch.object(self.jobs.sys, "platform", "win32"):
            job = self.jobs.start(self.dir, "copy-w1", "copy", {"item": "20001", "slot": 1})
        self.assertEqual((job["status"], job["pid"]), ("running", 4242))
        self.assertEqual(log.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
