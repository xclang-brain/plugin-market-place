"""確認の文字列（取り決め token）。このスキルが持ち主で、assets/contract/token/cases.json の全部の組を確かめる。

確認の文字列は、反映の計画のファイルのバイト列（または、ファイルなしの項目を決まった形の JSON にしたもの）の
SHA-256 の先頭12文字。Coverity に書き戻す道具も、同じデータで同じ答えを出す。
"""
from __future__ import annotations

import importlib.util
import json
import unittest

from apply_testlib import SKILL

CASES = SKILL / "assets" / "contract" / "token" / "cases.json"


def load_token_module():
    spec = importlib.util.spec_from_file_location("apply_plantoken_under_test", SKILL / "scripts" / "plantoken.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TokenContract(unittest.TestCase):
    def setUp(self) -> None:
        self.token = load_token_module()
        self.data = json.loads(CASES.read_text(encoding="utf-8"))

    def test_cases_file_has_the_contract_form(self) -> None:
        self.assertEqual(self.data["version"], 1)
        self.assertEqual(self.data["contract"], "token")
        cases = self.data["cases"]
        self.assertGreaterEqual(len(cases), 6)
        self.assertTrue(any("plan_text" in c["input"] for c in cases))
        self.assertTrue(any("entries" in c["input"] for c in cases))
        for case in cases:
            self.assertRegex(case["expect"]["token"], r"^[0-9a-f]{12}$", case["name"])

    def test_every_case_gives_the_expected_token(self) -> None:
        for case in self.data["cases"]:
            with self.subTest(case=case["name"]):
                given = case["input"]
                if "plan_text" in given:
                    got = self.token.token_for_bytes(given["plan_text"].encode("utf-8"))
                else:
                    got = self.token.token_for_entries(given["entries"])
                self.assertEqual(got, case["expect"]["token"])

    def test_plan_files_are_written_with_lf(self) -> None:
        data = self.token.plan_bytes({"triage": [{"comment": "誤検知。"}]})
        self.assertNotIn(b"\r\n", data)
        self.assertTrue(data.endswith(b"}\n"))
        self.assertIn("誤検知。".encode("utf-8"), data)


if __name__ == "__main__":
    unittest.main()
