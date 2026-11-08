"""The 監視 tab's JSON: per-item state, 「⚠ 停止?」, the initial analysis, errors and recent activity."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import helpers

RUN = "20261108-130000"


def age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


class MonitorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="gui-mon-"))
        cls.repo, cls.out = helpers.make_repo(cls.tmp)
        cls.srv = helpers.Server(cls.repo, cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        shutil.rmtree(self.out / RUN, ignore_errors=True)

    def board(self) -> dict:
        status, data = self.srv.get(f"/api/runs/{RUN}/activity")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"], data)
        return data

    def items(self, data: dict) -> dict:
        return {i["id"]: i for i in data["items"]}

    def test_items_show_state_step_note_and_progress(self) -> None:
        items = [{"id": "20001", "cids": [20001], "status": "done", "review": "pass"},
                 {"id": "20002", "cids": [20002], "status": "in_progress", "attempts": 2, "rework_rounds": 1},
                 {"id": "20003", "cids": [20003], "status": "pending"}]
        run = helpers.make_run(self.out, RUN, items, review=True, summary=False, extra={"parallel": 2})
        helpers.write_json(run / "items" / "20002" / "progress.json", {"item": "20002", "phase": "fix", "note": "修正を作っている"})
        log = [{"time": "2026-11-08T13:00:00+09:00", "event": "new-run", "level": "info", "message": "実行を始めた"},
               {"time": "2026-11-08T13:05:00+09:00", "event": "launch", "level": "info", "item": "20002", "message": "20002 の作業を始めた"}]
        (run / "operations.log").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in log) + "not json\n",
                                            encoding="utf-8")
        data = self.board()
        self.assertEqual(data["stage"], "items")
        self.assertEqual(data["parallel"], 2)
        self.assertEqual(len(data["workers"]), 2)
        busy = [w for w in data["workers"] if w["item"]]
        self.assertEqual(busy[0]["item"], "20002")
        self.assertEqual((busy[0]["phase"], busy[0]["phase_index"], busy[0]["note"]), ("fix", 3, "修正を作っている"))
        state = self.items(data)
        self.assertEqual(state["20002"]["rework_rounds"], 1)
        self.assertEqual(state["20002"]["attempts"], 2)
        self.assertFalse(state["20002"]["stalled"])
        self.assertEqual(state["20001"]["review"], "pass")
        self.assertEqual(data["progress"], {"value": 1, "max": 3, "label": "1 / 3"})
        self.assertEqual([r["text"] for r in data["recent"]], ["20002 の作業を始めた", "実行を始めた"])  # newest first
        self.assertEqual(data["counts"]["in_progress"], 1)
        self.assertEqual([r["role"] for r in data["roles"]], ["reviewer"])

    def test_the_monitor_follows_the_files_without_a_reload(self) -> None:
        run = helpers.make_run(self.out, RUN, [{"id": "20002", "cids": [20002], "status": "in_progress"}], summary=False)
        first = self.board()
        self.assertIsNone(self.items(first)["20002"]["phase"])
        helpers.write_json(run / "items" / "20002" / "progress.json", {"item": "20002", "phase": "deviation", "note": "逸脱"})
        self.assertEqual(self.items(self.board())["20002"]["phase"], "deviation")
        data = json.loads((run / "run.json").read_text(encoding="utf-8"))
        data["items"][0]["status"] = "done"
        helpers.write_json(run / "run.json", data)
        helpers.write_json(run / "items" / "20002" / "result.json", helpers.result("20002"))
        after = self.board()
        self.assertEqual(after["counts"]["done"], 1)
        self.assertEqual(after["progress"]["value"], 1)

    def test_an_item_quiet_for_10_minutes_may_have_stopped_but_not_while_its_copy_is_prepared(self) -> None:
        items = [{"id": "20001", "cids": [20001], "status": "in_progress", "slot": 1},
                 {"id": "20002", "cids": [20002], "status": "in_progress", "slot": 2},
                 {"id": "20003", "cids": [20003], "status": "in_progress", "slot": 3},
                 {"id": "20004", "cids": [20004], "status": "in_progress", "slot": 4, "stage": "copy"}]
        run = helpers.make_run(self.out, RUN, items, summary=False, extra={"parallel": 4})
        for item, quiet in (("20001", 11 * 60), ("20002", 11 * 60), ("20003", 9 * 60), ("20004", 20 * 60)):
            for path in (run / "items" / item).iterdir():
                age(path, quiet)
        # a running copy job of the run tool: jobs/<name>.json with the item in its arguments
        helpers.write_json(run / "jobs" / "copy-w2.json", {"name": "copy-w2", "kind": "copy", "status": "running", "pid": 1,
                                                           "started": "2026-11-08T13:00:00+09:00",
                                                           "args": {"item": "20002", "slot": 2}})
        state = self.items(self.board())
        self.assertTrue(state["20001"]["stalled"])
        self.assertFalse(state["20002"]["stalled"])
        self.assertTrue(state["20002"]["preparing_copy"])
        self.assertFalse(state["20003"]["stalled"])
        self.assertFalse(state["20004"]["stalled"])
        self.assertTrue(state["20004"]["preparing_copy"])
        workers = {w["item"]: w for w in self.board()["workers"]}
        self.assertEqual((workers["20004"]["slot"], workers["20004"]["stage"]), (4, "copy"))
        alerts = self.board()["alerts"]
        self.assertTrue(any(a["source"] == "stall" and a["item"] == "20001" for a in alerts))
        self.assertFalse(any(a["source"] == "stall" and a["item"] == "20002" for a in alerts))

    def test_the_initial_analysis_gets_30_minutes(self) -> None:
        run = helpers.make_run(self.out, RUN, [], summary=False, extra={"initial": True})
        helpers.write_json(run / "analysis" / "status.json", {"status": "running", "step": "build", "started": time.time() - 3600, "pid": 1})
        (run / "analysis" / "initial.log").write_text("building\n", encoding="utf-8")
        for path in (run / "analysis").iterdir():
            age(path, 11 * 60)
        data = self.board()
        self.assertEqual(data["stage"], "initial")
        self.assertEqual((data["initial"]["step"], data["initial"]["step_index"]), ("build", 2))
        self.assertFalse(data["initial"]["stalled"])
        for path in (run / "analysis").iterdir():
            age(path, 31 * 60)
        self.assertTrue(self.board()["initial"]["stalled"])
        helpers.write_json(run / "analysis" / "status.json", {"status": "failed", "step": "analyze", "error": "cov-analyze が失敗"})
        data = self.board()
        self.assertEqual(data["initial"]["status"], "failed")
        self.assertEqual(data["alerts"][0]["level"], "critical")
        self.assertIn("cov-analyze が失敗", data["alerts"][0]["detail"])

    def test_failed_items_and_logged_errors_are_serious(self) -> None:
        items = [{"id": "20007", "cids": [20007], "status": "error", "error": "取れなかった <b>理由</b>"},
                 {"id": "20008", "cids": [20008], "status": "pending"}]
        run = helpers.make_run(self.out, RUN, items, summary=False)
        helpers.write_json(run / "items" / "20008" / "issue-20008.json", {"cid": 20008, "stream": "app-main", "error": "SOAP の失敗"})
        (run / "operations.log").write_text(json.dumps({"time": "2026-11-08T13:10:00+09:00", "event": "error", "level": "warning",
                                                        "message": "結果ファイルに直すところがあった"}, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
        alerts = self.board()["alerts"]
        self.assertEqual(alerts[0]["level"], "critical")
        self.assertEqual(alerts[0]["item"], "20007")
        self.assertIn("取れなかった <b>理由</b>", alerts[0]["detail"])  # the page shows it as text
        titles = [a["title"] for a in alerts]
        self.assertIn("結果ファイルに直すところがあった", titles)
        self.assertTrue(any("20008 の警告経路" in t for t in titles))

    def test_the_reviewer_box_follows_its_instruction_file(self) -> None:
        run = helpers.make_run(self.out, RUN, [{"id": "20001", "cids": [20001], "status": "done"}], review=True, summary=False)
        (run / "agents" / "reviewer").mkdir(parents=True)
        (run / "agents" / "reviewer" / "20001.md").write_text("確かめの指示", encoding="utf-8")
        helpers.write_json(run / "agents" / "reviewer" / "progress.json", {"item": "20001", "phase": "check", "note": "C-12"})
        data = self.board()
        reviewer = data["roles"][0]
        self.assertEqual((reviewer["state"], reviewer["target"], reviewer["phase"]), ("running", "20001", "check"))
        self.assertEqual(data["stage"], "review")
        time.sleep(0.05)
        helpers.write_json(run / "items" / "20001" / "review.json", {"item": "20001", "outcome": "pass", "findings": [],
                                                                     "confidence_cap": None, "rework": [], "note": "ok"})
        self.assertEqual(self.board()["roles"][0]["state"], "done")

    def test_a_run_with_no_items_does_not_break_the_monitor(self) -> None:
        run = helpers.make_run(self.out, RUN, [], summary=True)
        helpers.write_json(run / "issues.json", {"finished_at": "2026-11-08T13:00:00", "issues": []})
        data = self.board()
        self.assertEqual(data["stage"], "done")
        self.assertEqual(data["total"], 0)
        self.assertEqual([w["item"] for w in data["workers"]], [None, None])  # parallel 2 from config.yaml

    def test_a_stopped_run_is_shown_as_stopped(self) -> None:
        helpers.make_run(self.out, RUN, [{"id": "20001", "cids": [20001], "status": "pending"}], summary=False,
                         extra={"stopped": {"at": "2026-11-08T13:30:00"}})
        data = self.board()
        self.assertEqual(data["stage"], "stopped")
        runs = self.srv.get("/api/runs")[1]["runs"]
        self.assertFalse(next(r for r in runs if r["run_id"] == RUN)["unfinished"])

    def test_run_json_fixtures_match_the_contract_schema(self) -> None:
        schema = helpers.contract_schema("run.schema.json")
        if schema is None:
            self.skipTest("assets/run.schema.json is not here yet (the copy of the contract arrives later)")
        run = helpers.make_run(self.out, RUN, [{"id": "20001", "cids": [20001], "status": "done", "review": "pass"},
                                               {"id": "G1", "cids": [20004, 20005], "status": "in_progress"}])
        data = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(helpers.schema.errors(data, schema), [])


if __name__ == "__main__":
    unittest.main()
