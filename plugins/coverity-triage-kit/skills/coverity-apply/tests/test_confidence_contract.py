"""人に見せる確信度（取り決め confidence）。scripts/confidence.py は一括実行のスクリプトと同じバイト列の写しで、
このスキルはそれをそのまま使う（プルリクエストの本文の確信度と、推奨が採られた割合の記録）。

assets/contract/confidence/cases.json（持ち主から写した共通の試験のデータ）の全部の組を、写した関数と、
このスキルの読み方（実行フォルダの run.json・review.json・verify.json から読む）の両方で確かめる。
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from apply_testlib import SKILL, run_item, run_state, write_json

RULE = SKILL / "scripts" / "confidence.py"
CASES = SKILL / "assets" / "contract" / "confidence" / "cases.json"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cases() -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    assert (data.get("version"), data.get("contract")) == (1, "confidence"), CASES
    return data["cases"]


class ConfidenceContract(unittest.TestCase):
    def setUp(self) -> None:
        if not CASES.is_file():
            self.skipTest("assets/contract/confidence/cases.json の写しがまだないので飛ばします")

    def test_every_case_through_the_copied_rule(self) -> None:
        rule = load("apply_confidence_under_test", RULE)
        found = cases()
        self.assertGreaterEqual(len(found), 8)
        for case in found:
            with self.subTest(case=case["name"]):
                given = case["input"]
                got = rule.effective_confidence(given["result"], given.get("review"), given.get("verify"))
                for key, value in case["expect"].items():
                    self.assertEqual(got[key], value)

    def test_every_case_through_the_run_folder(self) -> None:
        apply = load("apply_script_under_test", SKILL / "scripts" / "apply.py")
        self.assertEqual(Path(apply.confidence.__file__).resolve(), RULE.resolve())  # the copy, not a rule of its own
        with tempfile.TemporaryDirectory(prefix="apply-confidence-") as tmp:
            for n, case in enumerate(cases()):
                with self.subTest(case=case["name"]):
                    given = case["input"]
                    review, verify = given.get("review"), given.get("verify")
                    folder = Path(tmp) / f"20261108-1000{n:02d}"
                    outcome = review.get("outcome") if isinstance(review, dict) else None
                    write_json(folder / "run.json", run_state(folder.name, Path(tmp), [
                        run_item("20001", [20001], review=outcome)], review=review is not None))
                    if review is not None:
                        write_json(folder / "items" / "20001" / "review.json", review)
                    if verify is not None:
                        write_json(folder / "verify.json", {"20001": verify})
                    got = apply.shown_confidence(apply.RunFolder(folder), "20001", given["result"])
                    self.assertEqual(got["confidence"], case["expect"]["confidence"])
                    self.assertEqual(got, apply.confidence.effective_confidence(given["result"], review, verify))

    def test_a_review_counts_only_once_the_run_took_it_in(self) -> None:
        apply = load("apply_script_under_test2", SKILL / "scripts" / "apply.py")
        result = {"item": "20001", "recommendation": "fix", "confidence": "high", "confidence_reason": "経路を読んだ"}
        review = {"item": "20001", "outcome": "flag", "findings": [], "confidence_cap": "low", "rework": [], "note": "弱い"}
        with tempfile.TemporaryDirectory(prefix="apply-confidence-") as tmp:
            folder = Path(tmp) / "20261108-100000"
            write_json(folder / "items" / "20001" / "review.json", review)
            for taken, expect in ((None, "high"), ("unverified", "high"), ("flag", "low")):
                with self.subTest(review=taken):
                    write_json(folder / "run.json", run_state(folder.name, Path(tmp), [
                        run_item("20001", [20001], review=taken)], review=True))
                    got = apply.shown_confidence(apply.RunFolder(folder), "20001", result)
                    self.assertEqual(got["confidence"], expect)


if __name__ == "__main__":
    unittest.main()
