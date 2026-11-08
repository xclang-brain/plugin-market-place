"""The 結果 tab: runs, the summary, the detail, and the approvals saved in approvals.json."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import helpers

RUN = "20261108-101500"
REVIEWED = "20261108-120000"
FOREIGN = "20261101-090000"
BROKEN = "20261102-090000"


class ResultsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="gui-res-"))
        cls.repo, cls.out = helpers.make_repo(cls.tmp)
        items = [{"id": "20001", "cids": [20001], "status": "done"},
                 {"id": "20002", "cids": [20002], "status": "done"},
                 {"id": "G1", "cids": [20004, 20005, 20006], "status": "done"},
                 {"id": "20007", "cids": [20007], "status": "error", "error": "警告経路を取れませんでした"},
                 {"id": "20008", "cids": [20008], "status": "pending"},
                 {"id": "20009", "cids": [20009], "status": "merged", "merged_into": "G1"}]
        cls.run_dir = helpers.make_run(cls.out, RUN, items)
        helpers.make_run(cls.out, REVIEWED, [{"id": "30001", "cids": [30001], "status": "done"}], review=True, summary=False)
        foreign = cls.out / FOREIGN
        helpers.write_json(foreign / "run.json", {"run_id": FOREIGN, "created_at": "2026-11-01T09:00:00", "items": []})
        (foreign / "summary.html").write_text("<p>old</p>", encoding="utf-8")
        (cls.out / BROKEN).mkdir()
        (cls.out / BROKEN / "run.json").write_text("{broken", encoding="utf-8")
        cls.srv = helpers.Server(cls.repo, cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def approvals(self, run: str = RUN) -> dict:
        return json.loads((self.out / run / "approvals.json").read_text(encoding="utf-8"))

    def save(self, item: str, decision, deviation=None, run: str = RUN):
        return self.srv.post(f"/api/runs/{run}/approvals", {"item": item, "decision": decision, "deviation": deviation})[1]

    def test_runs_are_listed_newest_first_with_foreign_and_unreadable_ones_apart(self) -> None:
        data = self.srv.get("/api/runs")[1]
        self.assertEqual([r["run_id"] for r in data["runs"]], [REVIEWED, RUN, FOREIGN])
        foreign = data["runs"][2]
        self.assertTrue(foreign["foreign"])
        self.assertFalse(foreign["unfinished"])
        self.assertEqual([u["run_id"] for u in data["unreadable"]], [BROKEN])
        self.assertIn("run.json", data["unreadable"][0]["error"])
        main = data["runs"][1]
        self.assertEqual(main["counts"]["done"], 3)
        self.assertEqual(main["total"], 5)  # the merged item is shown inside G1
        self.assertTrue(main["unfinished"])  # 20008 is still pending

    def test_summary_lists_done_items_low_confidence_first_with_errors_and_unprocessed(self) -> None:
        data = self.srv.get(f"/api/runs/{RUN}/summary")[1]
        self.assertTrue(data["ok"])
        confidences = [r["confidence"] for r in data["items"]]
        self.assertEqual(confidences, sorted(confidences, key=["low", "medium", "high"].index))
        group = next(r for r in data["items"] if r["id"] == "G1")
        self.assertEqual(group["title"], "G1（3 件）")
        self.assertEqual(data["errors"], [{"id": "20007", "cids": [20007], "reason": "警告経路を取れませんでした"}])
        self.assertEqual(data["unprocessed"], ["20008"])
        self.assertTrue(data["summary_html"])
        row = next(r for r in data["items"] if r["id"] == "20001")
        self.assertEqual((row["checker"], row["file"], row["line"]), ("NULL_RETURNS", "src/reader.c", 40))
        self.assertEqual(row["report"], "cid/20001.html")

    def test_review_cap_and_build_problems_lower_the_confidence_shown(self) -> None:
        item = self.run_dir / "items" / "20001"
        helpers.write_json(item / "review.json", {"item": "20001", "outcome": "flag", "findings": [],
                                                  "confidence_cap": "medium", "rework": [], "note": "根拠が弱い"})
        self.addCleanup((item / "review.json").unlink)
        row = next(r for r in self.srv.get(f"/api/runs/{RUN}/summary")[1]["items"] if r["id"] == "20001")
        self.assertEqual((row["confidence"], row["review"]), ("medium", "flag"))
        self.assertIn("根拠が弱い", row["confidence_reason"])
        helpers.write_json(self.run_dir / "verify.json", {"20001": {"mode": "build", "build_ok": False, "problems": ["ビルドが失敗"]}})
        self.addCleanup((self.run_dir / "verify.json").unlink)
        row = next(r for r in self.srv.get(f"/api/runs/{RUN}/summary")[1]["items"] if r["id"] == "20001")
        self.assertEqual(row["confidence"], "low")
        result = json.loads((item / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["confidence"], "high")  # result.json is never changed

    def test_an_approval_is_saved_at_once_and_the_others_stay(self) -> None:
        before = self.approvals()["items"]
        report = (self.run_dir / "cid" / "20001.html").read_bytes()
        summary = (self.run_dir / "summary.html").read_bytes()
        for decision in ("reject", "deviation", None, "fix"):
            answer = self.save("20001", decision)
            self.assertTrue(answer["ok"], answer)
            saved = self.approvals()
            self.assertEqual(saved["version"], 1)
            self.assertEqual(saved["items"]["20001"]["decision"], decision)
            self.assertEqual(saved["items"]["20001"]["edited"], decision != "fix")
            self.assertEqual(saved["items"]["20002"], before["20002"])
        # the reports are not written again (the screen is where the current approvals are seen)
        self.assertEqual((self.run_dir / "cid" / "20001.html").read_bytes(), report)
        self.assertEqual((self.run_dir / "summary.html").read_bytes(), summary)
        schema = helpers.contract_schema("approvals.schema.json")
        if schema is not None:
            self.assertEqual(helpers.schema.errors(self.approvals(), schema), [])

    def test_wrong_approvals_are_refused_and_nothing_changes(self) -> None:
        before = self.approvals()
        self.assertFalse(self.save("20001", "maybe")["ok"])
        answer = self.save("20007", "fix")
        self.assertFalse(answer["ok"])
        self.assertIn("警告経路を取れませんでした", answer["error"])
        self.assertIn("まだ調べていません", self.save("20008", "fix")["error"])
        self.assertIn("G1", self.save("20009", "fix")["error"])
        self.assertEqual(self.srv.post(f"/api/runs/{RUN}/approvals", {"item": "99999", "decision": "fix"})[0], 404)
        self.assertEqual(self.srv.post("/api/runs/20991231-000000/approvals", {"item": "20001", "decision": "fix"})[0], 404)
        self.assertEqual(self.approvals(), before)

    def test_deviation_edits_are_checked_kept_and_dropped_when_back_to_the_ai(self) -> None:
        ai = {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
              "comment": "誤検知。呼び出し元で NULL を確かめている（src/main.c:10）。"}
        edit = {**ai, "comment": "誤検知。人が直した理由（src/main.c:12）。"}
        answer = self.save("20002", "deviation", edit)
        self.assertTrue(answer["ok"], answer)
        entry = self.approvals()["items"]["20002"]
        self.assertEqual(entry["deviation"], edit)
        self.assertTrue(entry["edited"])
        detail = self.srv.get(f"/api/runs/{RUN}/items/20002")[1]
        self.assertEqual(detail["deviation"], edit)
        self.assertEqual(detail["ai_deviation"], ai)
        self.assertTrue(detail["deviation_edited"])
        self.save("20002", "reject")  # deviation null keeps the edit
        self.assertEqual(self.approvals()["items"]["20002"]["deviation"], edit)
        refused = self.save("20002", "deviation", {**ai, "comment": "  "})
        self.assertFalse(refused["ok"])
        self.assertIn("逸脱コメント が空です", refused["problems"])
        self.assertFalse(self.save("20002", "deviation", {**ai, "classification": "Bug\nFix"})["ok"])
        self.assertEqual(self.approvals()["items"]["20002"]["deviation"], edit)
        self.assertTrue(self.save("20002", "deviation", ai)["ok"])  # the AI's proposal again: no edit kept
        entry = self.approvals()["items"]["20002"]
        self.assertNotIn("deviation", entry)
        self.assertFalse(entry["edited"])
        long = self.save("20002", "deviation", {**ai, "comment": "誤検知。" + "あ" * 210})
        self.assertTrue(long["ok"])
        self.assertIn("200 文字以内", long["warning"])
        self.save("20002", "deviation", ai)

    def test_approvals_wait_for_the_summary_when_the_review_is_used(self) -> None:
        answer = self.save("30001", "reject", run=REVIEWED)
        self.assertFalse(answer["ok"])
        self.assertIn("一覧", answer["error"])
        data = self.srv.get(f"/api/runs/{REVIEWED}/summary")[1]
        self.assertTrue(data["approval_wait"])
        self.assertEqual(data["items"][0]["decision"], "fix")  # the recommendation until the summary exists
        summary = self.out / REVIEWED / "summary.html"
        summary.write_text("<p>一覧</p>", encoding="utf-8")
        self.addCleanup(summary.unlink)
        self.assertTrue(self.save("30001", "reject", run=REVIEWED)["ok"])
        self.assertIsNone(self.srv.get(f"/api/runs/{REVIEWED}/summary")[1]["approval_wait"])
        (self.out / REVIEWED / "approvals.json").unlink()

    def test_foreign_runs_are_read_only(self) -> None:
        data = self.srv.get(f"/api/runs/{FOREIGN}/summary")[1]
        self.assertTrue(data["foreign"])
        self.assertEqual(data["items"], [])
        self.assertIn("見るだけ", data["notes"][0])
        answer = self.srv.post(f"/api/runs/{FOREIGN}/approvals", {"item": "1", "decision": "fix"})[1]
        self.assertFalse(answer["ok"])
        self.assertFalse((self.out / FOREIGN / "approvals.json").exists())
        self.assertEqual(self.srv.request("GET", f"/files/{FOREIGN}/summary.html")[0], 200)
        board = self.srv.get(f"/api/runs/{FOREIGN}/activity")[1]
        self.assertTrue(board["foreign"])

    def test_a_broken_approvals_file_is_named_and_not_overwritten(self) -> None:
        path = self.run_dir / "approvals.json"
        before = path.read_bytes()
        self.addCleanup(path.write_bytes, before)
        path.write_text("{broken", encoding="utf-8")
        data = self.srv.get(f"/api/runs/{RUN}/summary")[1]
        self.assertTrue(data["ok"])
        self.assertTrue(any("approvals.json" in n for n in data["notes"]))
        answer = self.save("20001", "reject")
        self.assertFalse(answer["ok"])
        self.assertIn("approvals.json", answer["error"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_detail_links_the_patch_the_fixed_files_and_the_report(self) -> None:
        detail = self.srv.get(f"/api/runs/{RUN}/items/20001")[1]
        self.assertTrue(detail["ok"])
        self.assertEqual([p["name"] for p in detail["patches"]], ["20001-fix.patch"])
        self.assertEqual([f["path"] for f in detail["fixed_files"]], ["src/読む ファイル.c"])
        for entry in detail["patches"] + detail["fixed_files"]:
            self.assertEqual(self.srv.request("GET", entry["url"])[0], 200)
            self.assertEqual(self.srv.request("GET", entry["raw_url"])[0], 200)
        self.assertEqual(self.srv.request("GET", detail["report_url"])[0], 200)
        missing = self.srv.get(f"/api/runs/{RUN}/items/20007")[1]
        self.assertFalse(missing["ok"])
        self.assertIn("処理できませんでした", missing["error"])

    def test_already_applied_items_warn_when_the_approval_changes(self) -> None:
        run_json = self.run_dir / "run.json"
        before = run_json.read_bytes()
        self.addCleanup(run_json.write_bytes, before)
        data = json.loads(before)
        data["items"][0]["applied"] = {"fix": {"branch": "coverity-fix/cid-20001"}, "at": "2026-11-08T12:00:00"}
        run_json.write_text(json.dumps(data), encoding="utf-8")
        answer = self.save("20001", "reject")
        self.assertTrue(answer["ok"])
        self.assertIn("もう反映しています", answer["warning"])
        self.save("20001", "fix")

    def test_a_result_file_of_the_wrong_shape_is_told_not_used(self) -> None:
        path = self.run_dir / "items" / "G1" / "result.json"
        before = path.read_bytes()
        self.addCleanup(path.write_bytes, before)
        path.write_text(json.dumps({"item": "G1", "recommendation": "maybe"}), encoding="utf-8")
        data = self.srv.get(f"/api/runs/{RUN}/summary")[1]
        self.assertTrue(any("G1" in n for n in data["notes"]))
        self.assertFalse(self.save("G1", "fix")["ok"])


if __name__ == "__main__":
    unittest.main()
