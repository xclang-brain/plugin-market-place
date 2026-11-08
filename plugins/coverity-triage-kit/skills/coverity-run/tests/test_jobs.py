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


if __name__ == "__main__":
    unittest.main()
