"""A run that keeps going after a step went wrong: an item at a script stage with no job, a job that did not
start, a job end that could not be taken in, a job that ended while the tick looked, stop during a step.

Some tests call the tick in this process (with one function made to fail); the jobs it starts still run as
their own processes, as in real use.
"""

from __future__ import annotations

import json
import os
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from support import RunCase, import_runlib, issue

ISSUES = [issue(20001, line=6)]


def _in_process_ok() -> bool:
    try:
        import jsonschema  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


class RecoveryTest(RunCase):
    settings = {"parallel": 1}

    def setUp(self) -> None:
        super().setUp()
        import_runlib()
        from runlib import flow, jobs, views

        self.flow, self.jobs, self.views = flow, jobs, views

    def offline_run(self) -> str:
        given = self.tmp / "given"
        self.write_issues(given / "issues.json", ISSUES)
        self.write_details(given, ISSUES[0])
        return self.new_run("--issues-file", str(given / "issues.json"))["run_dir"]

    def in_process(self, fn, *args, **kwargs):
        """Call a runlib function here, with the test's environment (the jobs it starts inherit it)."""
        if not _in_process_ok():
            self.skipTest("この Python に pyyaml と jsonschema がないため、tick をこのプロセスで呼べません")
        with mock.patch.dict(os.environ, self.env, clear=True), warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)  # the jobs it starts go on after the call
            return fn(*args, **kwargs)

    def item(self, run_dir: str) -> dict:
        return self.state(run_dir)["items"][0]

    def first_launch(self, run_dir: str) -> dict:
        out = self.tick_until(run_dir)
        self.assertEqual(out["actions"][0]["type"], "launch", out)
        return out["actions"][0]

    # ---- an item at a script stage that no job will move -------------------------------------

    def test_item_at_copy_with_no_job_is_put_back(self) -> None:
        run_dir = self.offline_run()
        launch = self.first_launch(run_dir)
        self.assertEqual(launch["task"], "worker:20001@1")
        # as if a tick had ended between giving the slot and starting the copy job (copy-w1 is an old record)
        self.edit_state(run_dir, lambda s: (s["items"][0].update(stage="copy"), s.update(tasks={})))
        out = self.tick_until(run_dir)
        self.assertEqual(out["actions"][0]["task"], "worker:20001@1", out)  # the lost attempt does not count
        self.assertTrue(any("見つかりません" in a["detail"] for a in self.state(run_dir)["alerts"]))
        self.assertTrue(self.drive(run_dir, ISSUES, out=out)["done"])
        self.assertEqual(self.item(run_dir)["status"], "done")

    def test_item_at_save_with_no_job_goes_back_then_fails(self) -> None:
        run_dir = self.offline_run()
        self.first_launch(run_dir)
        self.edit_state(run_dir, lambda s: (s["items"][0].update(stage="save"), s.update(tasks={})))
        out = self.tick_until(run_dir)
        self.assertEqual(out["actions"][0]["role"], "worker", out)
        self.edit_state(run_dir, lambda s: (s["items"][0].update(stage="save"), s.update(tasks={})))
        out = self.tick_until(run_dir)
        self.assertTrue(out["done"], out)
        self.assertEqual(self.item(run_dir)["status"], "error")
        self.assertIn("2 回目", self.item(run_dir)["error"])

    # ---- a job that did not start -------------------------------------------------------------

    def test_copy_job_that_did_not_start_leaves_the_item_pending(self) -> None:
        run_dir = self.offline_run()
        refused = PermissionError(32, "The process cannot access the file", "copy-w1.log")
        with mock.patch.object(self.jobs, "start", side_effect=refused):
            with self.assertRaises(PermissionError):
                self.in_process(self.flow.tick, run_dir, wait=1)
        item = self.item(run_dir)
        self.assertEqual((item["status"], item["slot"], item["stage"], item["attempts"]), ("pending", None, None, 0))
        self.assertEqual(self.first_launch(run_dir)["task"], "worker:20001@1")

    def test_save_job_that_did_not_start_does_not_leave_the_item_at_save(self) -> None:
        run_dir = self.offline_run()
        launch = self.first_launch(run_dir)
        self.work_as_worker(launch)
        refused = PermissionError(32, "The process cannot access the file", "save-20001.log")
        with mock.patch.object(self.jobs, "start", side_effect=refused):
            with self.assertRaises(PermissionError):
                self.in_process(self.flow.tick, run_dir, finished=launch["task"], wait=1)
        self.assertEqual((self.item(run_dir)["status"], self.item(run_dir)["stage"]), ("pending", None))
        self.assertTrue(self.drive(run_dir, ISSUES)["done"])
        self.assertEqual(self.item(run_dir)["status"], "done")

    def test_copy_job_whose_process_could_not_start_leaves_the_item_pending(self) -> None:
        run_dir = self.offline_run()
        refused = OSError(5, "Access is denied")
        with mock.patch.object(self.jobs, "spawn_detached", side_effect=refused):
            with self.assertRaises(OSError):
                self.in_process(self.flow.tick, run_dir, wait=1)
        item = self.item(run_dir)
        self.assertEqual((item["status"], item["slot"], item["stage"], item["attempts"]), ("pending", None, None, 0))
        self.assertNotIn("copy_failed", self.state(run_dir))
        self.assertEqual(self.first_launch(run_dir)["task"], "worker:20001@1")

    def test_save_job_whose_process_could_not_start_goes_back_to_pending(self) -> None:
        run_dir = self.offline_run()
        launch = self.first_launch(run_dir)
        self.work_as_worker(launch)
        refused = OSError(5, "Access is denied")
        with mock.patch.object(self.jobs, "spawn_detached", side_effect=refused):
            with self.assertRaises(OSError):
                self.in_process(self.flow.tick, run_dir, finished=launch["task"], wait=1)
        self.assertEqual((self.item(run_dir)["status"], self.item(run_dir)["stage"]), ("pending", None))
        self.assertTrue(self.drive(run_dir, ISSUES)["done"])
        self.assertEqual(self.item(run_dir)["status"], "done")

    def started_then_failed(self):
        """jobs.start that starts the job and then raises (a record another process held, on Windows)."""
        real = self.jobs.start

        def start(*args, **kwargs):
            real(*args, **kwargs)
            raise PermissionError(32, "The process cannot access the file", "copy-w1.json")
        return start

    def test_copy_job_that_started_before_an_error_keeps_its_item(self) -> None:
        run_dir = self.offline_run()
        with mock.patch.object(self.jobs, "start", self.started_then_failed()):
            with self.assertRaises(PermissionError):
                self.in_process(self.flow.tick, run_dir, wait=1)
        item = self.item(run_dir)
        self.assertEqual((item["status"], item["slot"], item["stage"], item["attempts"]), ("in_progress", 1, "copy", 1))
        out = self.tick_until(run_dir)
        self.assertEqual([a["task"] for a in out["actions"]], ["worker:20001@1"], out)
        self.assertTrue(self.drive(run_dir, ISSUES, out=out)["done"])
        self.assertEqual(self.item(run_dir)["status"], "done")

    def test_save_job_that_started_before_an_error_keeps_its_item(self) -> None:
        run_dir = self.offline_run()
        launch = self.first_launch(run_dir)
        self.work_as_worker(launch)
        with mock.patch.object(self.jobs, "start", self.started_then_failed()):
            with self.assertRaises(PermissionError):
                self.in_process(self.flow.tick, run_dir, finished=launch["task"], wait=1)
        self.assertEqual((self.item(run_dir)["status"], self.item(run_dir)["stage"]), ("in_progress", "save"))
        self.assertTrue(self.drive(run_dir, ISSUES)["done"])
        self.assertEqual(self.item(run_dir)["status"], "done")
        self.assertNotIn("orphan_retries", self.item(run_dir))

    def test_copy_end_for_another_slot_does_not_move_the_item(self) -> None:
        from runlib.state import Run

        run_dir = self.offline_run()
        self.first_launch(run_dir)
        self.edit_state(run_dir, lambda s: s["items"][0].update(stage="copy", slot=1))
        old = {"name": "copy-w2", "kind": "copy", "status": "done", "args": {"item": "20001", "slot": 2},
               "result": {}}
        self.in_process(self.flow._copy_reaped, Run(run_dir, write=True), old, [])
        self.assertEqual(self.item(run_dir)["stage"], "copy")
        self.in_process(self.flow._copy_reaped, Run(run_dir, write=True), {**old, "name": "copy-w1",
                                                                         "args": {"item": "20001", "slot": 1}}, [])
        self.assertEqual(self.item(run_dir)["stage"], "ready")

    # ---- a job end that could not be taken in ------------------------------------------------

    def test_handler_that_failed_once_is_tried_again(self) -> None:
        run_dir = self.offline_run()
        launch = self.first_launch(run_dir)
        self.work_as_worker(launch)
        real = self.flow._save_reaped
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(32, "The process cannot access the file", "cid/20001.html")
            return real(*args, **kwargs)
        with mock.patch.object(self.flow, "_save_reaped", flaky):
            out = self.in_process(self.flow.tick, run_dir, finished=launch["task"], wait=25)
        self.assertTrue(out["done"], out)
        self.assertEqual(self.item(run_dir)["status"], "done")
        job = json.loads((Path(run_dir) / "jobs" / "save-20001.json").read_text(encoding="utf-8"))
        self.assertEqual((job["reaped"], job["reap_errors"]), (True, 1))

    def test_handler_that_always_fails_fails_the_item(self) -> None:
        run_dir = self.offline_run()
        launch = self.first_launch(run_dir)
        self.work_as_worker(launch)

        def broken(*args, **kwargs):
            raise OSError(5, "Input/output error", "run.json")
        with mock.patch.object(self.flow, "_save_reaped", broken):
            out = self.in_process(self.flow.tick, run_dir, finished=launch["task"], wait=25)
        self.assertTrue(out["done"], out)
        self.assertEqual(self.item(run_dir)["status"], "error")
        self.assertIn("取り込めませんでした", self.item(run_dir)["error"])

    # ---- a job that ends while the tick looks ------------------------------------------------

    def test_job_that_ends_during_the_step_is_taken_in_by_the_same_tick(self) -> None:
        run_dir = self.offline_run()
        real = self.flow._heal_orphans

        def slow(run, say):  # right after the jobs were looked at: let the copy job end now
            deadline = time.monotonic() + 60
            while self.jobs.running(run.dir) and time.monotonic() < deadline:
                time.sleep(0.05)
            return real(run, say)
        with mock.patch.object(self.flow, "_heal_orphans", slow):
            out = self.in_process(self.flow.tick, run_dir, wait=25)
        self.assertEqual([a["task"] for a in out["actions"]], ["worker:20001@1"], out)

    # ---- stop during a step ------------------------------------------------------------------

    def test_stop_during_the_step_starts_no_job(self) -> None:
        run_dir = self.offline_run()

        def stopped_meanwhile(run):
            self.views.stop(str(run.dir))
            return None
        with mock.patch.object(self.flow, "_fingerprint", stopped_meanwhile):
            self.in_process(self.flow.tick, run_dir, wait=1)
        self.assertEqual(list((Path(run_dir) / "jobs").glob("copy-*.json")), [])
        self.assertEqual(self.item(run_dir)["status"], "pending")
        self.assertTrue(self.tick(run_dir)["stop"])


if __name__ == "__main__":
    unittest.main()
