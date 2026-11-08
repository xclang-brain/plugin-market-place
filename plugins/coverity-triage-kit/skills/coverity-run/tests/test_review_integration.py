"""The review (rework, flag, unverified) and the integration (merges, conflicts) in the tick state machine."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from support import RunCase, issue, review_json

ISSUES = [issue(20001, line=6), issue(20002, checker="RESOURCE_LEAK", function="open_file", line=12)]


class ReviewTest(RunCase):
    settings = {"review": "true", "max_rework": 1}

    def test_rework_then_pass_and_flag(self) -> None:
        run_dir = self.new_run()["run_dir"]
        reviews: list[str] = []

        def reviewer(action):
            reviews.append(action["task"])
            brief = self.brief_path(action).read_text(encoding="utf-8")
            self.assertIn("## 評価基準（警告ごと）", brief)
            self.assertIn("C-01", brief)
            self.assertIn("## スクリプトが決まった方法で見つけた点", brief)
            self.assertNotIn("C-25", brief)  # run-scope criteria are not the reviewer's
            if action["item"] == "20001" and len([t for t in reviews if "20001" in t]) == 1:
                return self.review_as_reviewer(action, "rework")
            if action["item"] == "20002":
                return self.review_as_reviewer(action, "flag")
            return self.review_as_reviewer(action, "pass")

        workers: list[str] = []

        def worker(action):
            workers.append(action["task"])
            if action["task"] == "worker:20001@2":
                brief = self.brief_path(action).read_text(encoding="utf-8")
                self.assertIn("## 確かめの指摘（直すこと）", brief)
                self.assertIn("根拠の行を読み直して直す", brief)
                self.assertTrue((Path(run_dir) / "items" / "20001" / "result.previous.json").is_file())
                self.assertEqual(action["description"], "20001 の作り直し（1 回目）")
            return self.work_as_worker(action)

        out = self.drive(run_dir, ISSUES, worker=worker, reviewer=reviewer)
        self.assertTrue(out["done"], out)
        self.assertIn("worker:20001@2", workers)
        self.assertIn("reviewer:20001@2", reviews)
        items = {i["id"]: i for i in self.state(run_dir)["items"]}
        self.assertEqual(items["20001"]["review"], "pass")
        self.assertEqual(items["20001"]["review_rounds"], 1)
        self.assertEqual(items["20002"]["review"], "flag")
        self.assertFalse((Path(run_dir) / "items" / "20001" / "result.previous.json").exists())

        summary = self.run_json("summary", "--run", run_dir)
        results = {r["id"]: r for r in summary["results"]}
        self.assertEqual(results["20002"]["confidence"], "medium")  # high, capped by the flag (C-19)
        self.assertEqual(json.loads((Path(run_dir) / "items" / "20002" / "result.json").read_text(encoding="utf-8"))
                         ["confidence"], "high")  # result.json is never changed
        self.assertEqual(summary["results"][0]["id"], "20002")  # lowest confidence first
        checks = {c["id"]: c for c in summary["run_checks"]}
        for cid in ("C-16", "C-17", "C-18", "C-19"):
            self.assertTrue(checks[cid]["ok"], checks[cid])
        by_id = {c["id"]: c for c in summary["criteria_results"]}
        self.assertEqual(by_id["C-01"]["passed"], 2)
        self.assertEqual(by_id["C-21"]["passed"], 2)  # standard checkers: C-21 applies
        self.assertEqual(by_id["C-22"]["not_applicable"], 2)  # the MISRA criterion does not
        self.assertEqual(by_id["C-25"]["passed"], 1)
        status = self.run_json("status", "--run", run_dir)
        review = next(i for i in status["items"] if i["id"] == "20002")["review"]
        self.assertEqual(review["outcome"], "flag")
        self.assertTrue(review["findings"][0]["check"].startswith("C-"))

    def test_rework_limit_turns_into_flag(self) -> None:
        self.write_config(review="true", max_rework=0)
        run_dir = self.new_run()["run_dir"]
        out = self.drive(run_dir, ISSUES, reviewer=lambda a: self.review_as_reviewer(a, "rework"))
        self.assertTrue(out["done"])
        items = {i["id"]: i for i in self.state(run_dir)["items"]}
        self.assertEqual(items["20001"]["review"], "flag")
        summary = self.run_json("summary", "--run", run_dir)
        self.assertTrue(all(r["confidence"] == "low" for r in summary["results"]))  # a flag without a cap is low

    def test_bad_review_is_sent_back_then_unverified(self) -> None:
        run_dir = self.new_run()["run_dir"]
        tries: list[str] = []

        def reviewer(action):
            tries.append(action["task"])
            if action["item"] == "20001":
                review = review_json("20001")
                review["findings"] = review["findings"][:3]  # criteria left out
                Path(self.brief_value(self.brief_path(action), "確かめの結果（書く）")).write_text(
                    json.dumps(review, ensure_ascii=False), encoding="utf-8")
                return review
            return self.review_as_reviewer(action)

        self.assertTrue(self.drive(run_dir, ISSUES, reviewer=reviewer)["done"])
        self.assertIn("reviewer:20001@1#2", tries)
        self.assertIn("reviewer:20001@1#3", tries)
        brief = (Path(run_dir) / "agents" / "reviewer" / "20001.md").read_text(encoding="utf-8")
        self.assertIn("## 確かめの結果の直すこと", brief)
        item = next(i for i in self.state(run_dir)["items"] if i["id"] == "20001")
        self.assertEqual(item["review"], "unverified")
        summary = self.run_json("summary", "--run", run_dir)
        self.assertFalse({c["id"]: c for c in summary["run_checks"]}["C-16"]["ok"])

    def test_old_review_file_is_not_accepted(self) -> None:
        run_dir = self.new_run()["run_dir"]

        def reviewer(action):
            review = self.review_as_reviewer(action)
            path = Path(self.brief_value(self.brief_path(action), "確かめの結果（書く）"))
            old = time.time() - 3600
            os.utime(path, (old, old))  # as if written before this review was asked for
            return review

        self.assertTrue(self.drive(run_dir, ISSUES[:1], reviewer=reviewer)["done"])
        self.assertEqual(self.state(run_dir)["items"][0]["review"], "unverified")

    def test_changed_result_during_review_is_unverified(self) -> None:
        run_dir = self.new_run()["run_dir"]

        def reviewer(action):
            result = Path(run_dir) / "items" / action["item"] / "result.json"
            data = json.loads(result.read_text(encoding="utf-8"))
            data["confidence_reason"] = "検証役が書き換えた"
            result.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return self.review_as_reviewer(action)

        self.assertTrue(self.drive(run_dir, ISSUES[:1], reviewer=reviewer)["done"])
        state = self.state(run_dir)
        self.assertEqual(state["items"][0]["review"], "unverified")
        self.assertTrue(any("変わりました" in a["detail"] for a in state["alerts"]))


class IntegrationTest(RunCase):
    settings = {"integration": "true", "review": "true"}

    def plan_writer(self, run_dir: str, source: str = "combine", conflicts: bool = False):
        def integrator(action):
            brief = self.brief_path(action).read_text(encoding="utf-8")
            self.assertIn("## 候補（決まった方法で見つけた組）", brief)
            plan_file = Path(self.brief_value(self.brief_path(action), "計画（書く）"))
            result = json.loads((Path(run_dir) / "items" / "20001" / "result.json").read_text(encoding="utf-8"))
            result.pop("item")
            result.pop("group_excluded_cids", None)
            plan = {"merges": [{"members": ["20001", "20002"], "reason": "同じ fgets の戻り値の扱い",
                                "fix_source": source, "result": result}],
                    "conflicts": [{"items": ["20001", "20002"], "note": "同じ行"}] if conflicts else []}
            if conflicts:
                plan["merges"] = []
            plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            return plan
        return integrator

    def test_merge_with_item_fix_source(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.drive(run_dir, [issue(20001, line=6), issue(20002, checker="RESOURCE_LEAK", function="open_file", line=12)],
                         integrator=self.plan_writer(run_dir, "item:20001"))
        self.assertTrue(out["done"], out)
        items = {i["id"]: i for i in self.state(run_dir)["items"]}
        self.assertEqual(items["20001"]["status"], "merged")
        self.assertEqual(items["20001"]["merged_into"], "G1")
        self.assertEqual(items["G1"]["status"], "done")
        self.assertEqual(items["G1"]["merged_from"], ["20001", "20002"])
        self.assertEqual(items["G1"]["review"], "pass")
        saved = json.loads((Path(run_dir) / "items" / "G1" / "saved.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["fix"]["branch"].endswith("-G1"))
        result = json.loads((Path(run_dir) / "items" / "G1" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["item"], "G1")
        summary = self.run_json("summary", "--run", run_dir)
        self.assertEqual([r["id"] for r in summary["results"]], ["G1"])
        self.assertEqual(summary["results"][0]["title"], "G1（2 件）")
        status = self.run_json("status", "--run", run_dir)
        self.assertEqual(status["counts"]["merged"], 2)
        self.assertEqual(status["counts"]["total"], 1)

    def test_merge_with_rework_goes_to_a_worker(self) -> None:
        run_dir = self.new_run()["run_dir"]
        seen: list[str] = []

        def worker(action):
            seen.append(action["task"])
            if action["item"] == "G1":
                self.assertIn("## 統合の指示", self.brief_path(action).read_text(encoding="utf-8"))
            return self.work_as_worker(action)

        out = self.drive(run_dir, ISSUES, worker=worker, integrator=self.plan_writer(run_dir, "rework"))
        self.assertTrue(out["done"])
        self.assertIn("worker:G1@1", seen)

    def test_bad_plan_twice_then_no_merge(self) -> None:
        run_dir = self.new_run()["run_dir"]

        def integrator(action):
            plan_file = Path(self.brief_value(self.brief_path(action), "計画（書く）"))
            plan_file.write_text(json.dumps({"merges": [{"members": ["20001"], "reason": "x", "fix_source": "combine",
                                                        "result": {}}]}), encoding="utf-8")
        out = self.drive(run_dir, ISSUES, integrator=integrator)
        self.assertTrue(out["done"])
        state = self.state(run_dir)
        self.assertTrue(state["integrated"])
        self.assertTrue(all(i["status"] == "done" for i in state["items"]))
        self.assertTrue(any(a["kind"] == "統合" for a in state["alerts"]))

    def test_conflict_is_noted(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.drive(run_dir, ISSUES, integrator=self.plan_writer(run_dir, conflicts=True))
        self.assertTrue(out["done"])
        items = {i["id"]: i for i in self.state(run_dir)["items"]}
        self.assertTrue(any("同じ箇所" in w for w in items["20001"]["warnings"]))



class AllOptionsTest(RunCase):
    """Integration, review, build verification and markdown together: every stage in its order."""

    def test_every_stage(self) -> None:
        import sys

        script = self.tmp / "build.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        self.write_config(integration="true", review="true", verify="build", markdown="true", parallel=3,
                          build_command=json.dumps(f'"{sys.executable}" "{script}"'))
        run_dir = self.new_run()["run_dir"]
        stages: list[str] = []
        original = self.tick

        def tick(run_dir_, *extra, wait=10):
            out = original(run_dir_, *extra, wait=wait)
            if not stages or stages[-1] != out["stage"]:
                stages.append(out["stage"])
            return out

        self.tick = tick
        out = self.drive(run_dir, ISSUES, integrator=IntegrationTest.plan_writer(self, run_dir, "combine"))
        self.assertTrue(out["done"], out)
        self.assertEqual(stages[:3], ["fetch", "details", "items"])
        self.assertLess(stages.index("integrate"), stages.index("review"))  # the reviews wait for the integration
        self.assertEqual(stages[-1], "done")
        state = self.state(run_dir)
        self.assertTrue(state["verified"])  # the build verification ran after the reviews
        self.assertTrue(json.loads((Path(run_dir) / "verify.json").read_text(encoding="utf-8"))["G1"]["build_ok"])
        g1 = next(i for i in state["items"] if i["id"] == "G1")
        self.assertEqual((g1["status"], g1["review"]), ("done", "pass"))
        summary = self.run_json("summary", "--run", run_dir)
        self.assertTrue(all(c["ok"] is not False for c in summary["run_checks"]), summary["run_checks"])
        self.assertTrue((Path(run_dir) / "cid" / "G1.md").is_file())


if __name__ == "__main__":
    unittest.main()
