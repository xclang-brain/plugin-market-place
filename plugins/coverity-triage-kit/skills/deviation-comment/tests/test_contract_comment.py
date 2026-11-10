"""Shared test data of the comment rules (assets/contract/comment/cases.json; this skill owns it)."""

from __future__ import annotations

import json
import unittest

from _support import SKILL, UV, clean_bytecode, load_script, run_json

CASES = SKILL / "assets" / "contract" / "comment" / "cases.json"


def tearDownModule() -> None:
    clean_bytecode()


def load_cases() -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["contract"] == "comment", data.keys()
    return data["cases"]


class CommentContractTest(unittest.TestCase):
    def test_case_file_shape(self) -> None:
        cases = load_cases()
        self.assertGreaterEqual(len(cases), 12)
        names = [c["name"] for c in cases]
        self.assertEqual(len(names), len(set(names)))
        for case in cases:
            self.assertEqual(set(case), {"name", "input", "expect"})
            self.assertIsInstance(case["input"]["comment"], str)
            self.assertIsInstance(case["expect"]["ok"], bool)
        self.assertTrue(any(c["expect"]["ok"] for c in cases))
        self.assertTrue(any(not c["expect"]["ok"] for c in cases))

    def test_rules_in_process(self) -> None:
        m = load_script("check_comment")
        for case in load_cases():
            with self.subTest(case["name"]):
                self.assertEqual(m.check_comment(case["input"]["comment"])["ok"], case["expect"]["ok"])

    @unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
    def test_command(self) -> None:
        for case in load_cases():
            with self.subTest(case["name"]):
                code, out = run_json("check_comment", "--stdin", stdin=json.dumps(case["input"], ensure_ascii=False))
                self.assertEqual(out["ok"], case["expect"]["ok"], out)
                self.assertEqual(code, 0 if case["expect"]["ok"] else 1)


if __name__ == "__main__":
    unittest.main()
