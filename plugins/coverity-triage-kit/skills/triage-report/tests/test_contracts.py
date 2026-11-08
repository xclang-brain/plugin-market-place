"""Shared test data: contract "result" (this skill owns it) and a copy of contract "comment"."""

from __future__ import annotations

import json
import unittest

from _support import SKILL, UV, clean_bytecode, load_script, run_json

RESULT_CASES = SKILL / "assets" / "contract" / "result" / "cases.json"
COMMENT_CASES = SKILL / "assets" / "contract" / "comment" / "cases.json"


def tearDownModule() -> None:
    clean_bytecode()


def load_cases(path, contract: str) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["contract"] == contract
    return data["cases"]


@unittest.skipUnless(RESULT_CASES.is_file(), "assets/contract/result/cases.json がない")
@unittest.skipUnless(UV, "uv がないので、形の確かめ（jsonschema を使う）を飛ばす")
class ResultContractTest(unittest.TestCase):
    def test_case_file_shape(self) -> None:
        cases = load_cases(RESULT_CASES, "result")
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len({c["name"] for c in cases}), len(cases))
        for case in cases:
            self.assertEqual(set(case), {"name", "input", "expect"})
            self.assertLessEqual(set(case["input"]), {"result", "item", "cids"})
            self.assertIsInstance(case["input"]["item"], str)
            self.assertIsInstance(case["expect"]["ok"], bool)

    def test_command(self) -> None:
        for case in load_cases(RESULT_CASES, "result"):
            with self.subTest(case["name"]):
                code, out = run_json("check_result", "check", "--stdin",
                                     stdin=json.dumps(case["input"], ensure_ascii=False))
                self.assertEqual(out["ok"], case["expect"]["ok"], out)
                self.assertEqual(code, 0 if case["expect"]["ok"] else 1)


@unittest.skipUnless(COMMENT_CASES.is_file(), "assets/contract/comment/cases.json がない（写しは後で置かれる）")
class CommentContractCopyTest(unittest.TestCase):
    def test_comment_rules(self) -> None:
        m = load_script("check_result")
        for case in load_cases(COMMENT_CASES, "comment"):
            with self.subTest(case["name"]):
                self.assertEqual(m.check_comment(case["input"]["comment"])["ok"], case["expect"]["ok"])

    @unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
    def test_comment_rules_inside_result_check(self) -> None:
        """A result that is fine except for its comment is accepted exactly when the comment is."""
        base = json.loads((SKILL / "assets" / "contract" / "result" / "cases.json").read_text(encoding="utf-8"))
        result = next(c["input"]["result"] for c in base["cases"] if c["name"] == "valid_true_bug_fix")
        for case in load_cases(COMMENT_CASES, "comment")[:12]:
            with self.subTest(case["name"]):
                value = json.loads(json.dumps(result))
                value["deviation"]["comment"] = case["input"]["comment"]
                code, out = run_json("check_result", "check", "--stdin",
                                     stdin=json.dumps({"result": value, "item": "20001"}, ensure_ascii=False))
                self.assertEqual(out["ok"], case["expect"]["ok"], out)


if __name__ == "__main__":
    unittest.main()
