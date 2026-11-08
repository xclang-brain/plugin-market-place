"""Shared test data: contract "plan" (this skill owns it), a copy of contract "comment",
and the result definitions inside plan.schema.json (compared with the copy of result.schema.json in this folder)."""

from __future__ import annotations

import json
import unittest

from _support import SKILL, UV, clean_bytecode, load_script, run_json

ASSETS = SKILL / "assets"
PLAN_CASES = ASSETS / "contract" / "plan" / "cases.json"
COMMENT_CASES = ASSETS / "contract" / "comment" / "cases.json"
PLAN_SCHEMA = ASSETS / "plan.schema.json"
RESULT_SCHEMA = ASSETS / "result.schema.json"


def tearDownModule() -> None:
    clean_bytecode()


def load_cases(path, contract: str) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["contract"] == contract
    return data["cases"]


@unittest.skipUnless(PLAN_CASES.is_file(), "assets/contract/plan/cases.json がない")
class PlanContractTest(unittest.TestCase):
    def test_case_file_shape(self) -> None:
        cases = load_cases(PLAN_CASES, "plan")
        self.assertGreaterEqual(len(cases), 8)
        self.assertEqual(len({c["name"] for c in cases}), len(cases))
        for case in cases:
            self.assertEqual(set(case["input"]), {"plan", "items"})
            for info in case["input"]["items"].values():
                self.assertEqual(set(info), {"cids", "status", "confidence", "no_grouping"})
            self.assertIsInstance(case["expect"]["ok"], bool)

    @unittest.skipUnless(UV, "uv がないので、形の確かめ（jsonschema を使う）を飛ばす")
    def test_command(self) -> None:
        for case in load_cases(PLAN_CASES, "plan"):
            with self.subTest(case["name"]):
                code, out = run_json("check_plan", "--stdin", stdin=json.dumps(case["input"], ensure_ascii=False))
                self.assertEqual(out["ok"], case["expect"]["ok"], out)
                self.assertEqual(code, 0 if case["expect"]["ok"] else 1)


@unittest.skipUnless(COMMENT_CASES.is_file(), "assets/contract/comment/cases.json がない（写しは後で置かれる）")
class CommentContractCopyTest(unittest.TestCase):
    def test_comment_rules(self) -> None:
        m = load_script("check_plan")
        for case in load_cases(COMMENT_CASES, "comment"):
            with self.subTest(case["name"]):
                self.assertEqual(m.check_comment(case["input"]["comment"])["ok"], case["expect"]["ok"])


@unittest.skipUnless(PLAN_SCHEMA.is_file() and RESULT_SCHEMA.is_file(),
                     "plan.schema.json と result.schema.json の両方がこのフォルダにない")
class ResultDefinitionsTest(unittest.TestCase):
    """plan.schema.json carries the result definitions; they must equal result.schema.json's."""

    def setUp(self) -> None:
        self.plan = json.loads(PLAN_SCHEMA.read_text(encoding="utf-8"))
        self.result = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))

    def test_defs_are_the_same(self) -> None:
        for name, definition in self.result["$defs"].items():
            with self.subTest(name):
                self.assertEqual(self.plan["$defs"].get(name), definition)

    def test_merged_result_is_the_result_without_item(self) -> None:
        merged = self.plan["$defs"]["merged_result"]
        self.assertEqual(merged["properties"], self.result["properties"])
        self.assertEqual(merged["required"], [k for k in self.result["required"] if k != "item"])
        self.assertEqual(merged["additionalProperties"], self.result["additionalProperties"])
        self.assertEqual(merged["type"], self.result["type"])


if __name__ == "__main__":
    unittest.main()
