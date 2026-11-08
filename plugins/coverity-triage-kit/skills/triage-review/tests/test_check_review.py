"""check_review.py: schema, criteria coverage and outcome rules; plus the contract files of this skill."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from _support import SKILL, UV, clean_bytecode, load_script, run_json

CRITERIA = json.loads((SKILL / "assets" / "criteria.json").read_text(encoding="utf-8"))
ITEM_IDS = [c["id"] for c in CRITERIA["criteria"] if c["scope"] == "item"]
NOT_FOR_STANDARD = {"C-20", "C-22", "C-23"}


def tearDownModule() -> None:
    clean_bytecode()


def review(outcome: str = "pass", **changes) -> dict:
    findings = [{"check": i, "ok": True, "detail": "対象外（標準チェッカーの警告）" if i in NOT_FOR_STANDARD else "確かめた",
                 "file": "src/reader.c", "line": 18} for i in ITEM_IDS]
    value = {"item": "20001", "outcome": outcome, "findings": findings, "confidence_cap": None, "rework": [],
             "note": "根拠と判定を確かめた。"}
    value.update(changes)
    return value


class ContractFilesTest(unittest.TestCase):
    def test_criteria(self) -> None:
        self.assertEqual(CRITERIA["version"], 1)
        ids = [c["id"] for c in CRITERIA["criteria"]]
        self.assertEqual(ids, [f"C-{n:02d}" for n in range(1, 29)])
        self.assertEqual(len(ITEM_IDS), 20)
        run_ids = {c["id"] for c in CRITERIA["criteria"] if c["scope"] == "run"}
        self.assertEqual(run_ids, {"C-16", "C-17", "C-18", "C-19", "C-24", "C-25", "C-26", "C-27"})
        self.assertEqual({c["id"] for c in CRITERIA["criteria"] if c.get("applies_when")}, {"C-20", "C-21", "C-22", "C-23"})

    def test_schema_is_valid_json_schema(self) -> None:
        schema = json.loads((SKILL / "assets" / "review.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(set(schema["properties"]["outcome"]["enum"]), {"pass", "flag", "rework"})


class ApplicabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_script("check_review")
        cls.items = [c for c in CRITERIA["criteria"] if c["scope"] == "item"]

    def test_checker_kinds(self) -> None:
        for name, kind in (("NULL_RETURNS", "standard"), ("MISRA C-2012 Rule 10.4", "misra"), ("misra c++-2008 5-0-3", "misra"),
                           ("CERT INT31-C", "cert"), ("cert-c ERR33-C", "cert"), ("RESOURCE_LEAK", "standard")):
            self.assertEqual(self.m.checker_kind(name), kind, name)

    def test_applicability(self) -> None:
        a = self.m.applicability(self.items, "MISRA C-2012 Rule 10.4", True)
        self.assertEqual((a["C-20"], a["C-21"], a["C-22"], a["C-23"], a["C-01"]), (True, False, True, False, True))
        a = self.m.applicability(self.items, None, None)
        self.assertEqual((a["C-20"], a["C-21"], a["C-22"], a["C-28"]), (None, None, None, True))


@unittest.skipUnless(UV, "uv がないので、形の確かめ（jsonschema を使う）を飛ばす")
class CommandTest(unittest.TestCase):
    def check(self, value, *args: str) -> tuple[int, dict]:
        payload = {"review": value}
        return run_json("check_review", "--stdin", *args, stdin=json.dumps(payload, ensure_ascii=False))

    def codes(self, value, *args: str) -> list[str]:
        return [p["code"] for p in self.check(value, *args)[1]["problems"]]

    def test_complete_pass(self) -> None:
        code, out = self.check(review(), "--item", "20001", "--checker", "NULL_RETURNS")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["outcome"], out["missing"], out["warnings"]), ("pass", [], []))

    def test_file_input_and_no_files_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            path.write_text(json.dumps(review(), ensure_ascii=False), encoding="utf-8")
            code, out = run_json("check_review", "--review", str(path), "--item", "20001", cwd=tmp)
            self.assertEqual(code, 0, out)
            self.assertEqual(os.listdir(tmp), ["review.json"])

    def test_item_mismatch(self) -> None:
        self.assertEqual(self.codes(review(), "--item", "20002"), ["item_mismatch"])

    def test_missing_unknown_and_duplicate_checks(self) -> None:
        value = review()
        value["findings"] = value["findings"][1:] + [dict(value["findings"][1]), {"check": "根拠の場所", "ok": True, "detail": "x"},
                                                     {"check": "C-16", "ok": True, "detail": "x"}]
        code, out = self.check(value)
        self.assertEqual(code, 1)
        self.assertEqual(sorted(p["code"] for p in out["problems"]),
                         ["duplicate_check", "missing_check", "unknown_check", "unknown_check"])
        self.assertEqual(out["missing"], ["C-01"])

    def test_flag_needs_cap(self) -> None:
        self.assertEqual(self.codes(review("flag")), ["flag_without_cap"])
        self.assertEqual(self.codes(review("flag", confidence_cap="low")), [])

    def test_rework_needs_items_and_a_failed_check(self) -> None:
        self.assertIn("schema", self.codes(review("rework")))
        value = review("rework", rework=["根拠の行を 18 行に直す"])
        self.assertEqual(self.codes(value), ["rework_without_failed_check"])
        value["findings"][11]["ok"] = False
        self.assertEqual(self.codes(value), [])

    def test_pass_with_failed_check_or_rework_items(self) -> None:
        value = review()
        value["findings"][0]["ok"] = False
        self.assertEqual(self.codes(value), ["pass_with_failed_check"])
        self.assertEqual(self.codes(review(rework=["x"])), ["rework_not_empty"])

    def test_not_applicable_rules_need_the_checker(self) -> None:
        value = review()
        value["findings"][ITEM_IDS.index("C-22")].update(ok=False, detail="MISRA の違反")
        self.assertEqual(self.codes(value, "--checker", "NULL_RETURNS"), ["not_applicable_failed", "pass_with_failed_check"])
        code, out = self.check(review(), "--checker", "MISRA C-2012 Rule 10.4")
        self.assertEqual([p["code"] for p in out["problems"]], ["applicable_marked_not_applicable"])
        self.assertEqual([w["code"] for w in out["warnings"]], ["not_applicable_detail"])  # C-21 lacks 対象外
        merged = self.codes(review(), "--merged")
        self.assertEqual(merged, ["applicable_marked_not_applicable"])  # C-20 applies to a merged item

    def test_schema_problems_are_reported(self) -> None:
        value = review(outcome="ok", extra=1, note=" ")
        del value["item"]
        code, out = self.check(value)
        self.assertEqual(code, 1)
        self.assertEqual(sorted(p["path"] for p in out["problems"]), ["note", "outcome", "（全体）", "（全体）"])

    def test_input_errors(self) -> None:
        for args, stdin in ((["--stdin"], "{}"), (["--stdin"], "x"), ([], None), (["--review", "/none/review.json"], None)):
            code, out = run_json("check_review", *args, stdin=stdin)
            self.assertEqual((code, out["problems"][0]["code"]), (1, "bad_input"), args)

    def test_stdin_options(self) -> None:
        payload = {"review": review(), "item": "20001", "checker": "MISRA C-2012 Rule 10.4", "merged": False}
        code, out = run_json("check_review", "--stdin", stdin=json.dumps(payload, ensure_ascii=False))
        self.assertEqual([p["code"] for p in out["problems"]], ["applicable_marked_not_applicable"])


if __name__ == "__main__":
    unittest.main()
