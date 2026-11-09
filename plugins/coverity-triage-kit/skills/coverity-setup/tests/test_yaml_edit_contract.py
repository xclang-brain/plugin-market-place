"""REQ-107: config.yaml の書き換えで、人が書いた注釈を元の場所に残す（取り決め yaml-edit）。

このスキルが取り決めの持ち主。共通の試験のデータ assets/contract/yaml-edit/cases.json の全部の組で、
期待する文（1文字まで）と notice（注釈が残せないか）が一致することを確かめる。
"""
from __future__ import annotations

import json
import subprocess
import unittest

from setup_testlib import SKILL, UV, base_env, needs_uv

CASES = SKILL / "assets" / "contract" / "yaml-edit" / "cases.json"
RUNNER = SKILL / "tests" / "yaml_contract_runner.py"
REQUIRED_RULES = {
    "changed-value-keeps-its-end-comment", "other-comments-stay", "nothing-changed-keeps-the-text",
    "removed-key-takes-the-comment-line-above", "removed-key-takes-its-end-comment",
    "removed-list-value-takes-its-comments", "emptied-list-becomes-flow-and-keeps-key-comment",
    "reordered-list-values-move-with-comments", "absent-key-added-at-end-of-section",
    "block-scalar-is-written-whole-with-notice", "anchor-is-written-whole-with-notice",
    "unsupported-without-comments-has-no-notice", "crlf-is-kept", "bom-is-kept",
}


def run_runner(*args: str) -> dict:
    proc = subprocess.run([UV, "run", "--native-tls", "--quiet", str(RUNNER), *args], env=base_env(),
                          capture_output=True, check=False, timeout=600)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.decode("utf-8", "replace"))
    return json.loads(proc.stdout.decode("ascii"))


class ContractData(unittest.TestCase):
    def test_cases_file_has_the_contract_form(self) -> None:
        data = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["contract"], "yaml-edit")
        self.assertGreaterEqual(len(data["cases"]), 12)
        names = [c["name"] for c in data["cases"]]
        self.assertEqual(len(names), len(set(names)), "同じ名前の組がある")
        for case in data["cases"]:
            self.assertEqual(set(case["input"]), {"original", "values"}, case["name"])
            self.assertEqual(set(case["expect"]), {"text", "notice"}, case["name"])
            self.assertIsInstance(case["expect"]["notice"], bool)
        self.assertLessEqual(REQUIRED_RULES, set(names), "REQ-107 の決まりの組が足りない")


@needs_uv
class ContractCases(unittest.TestCase):
    def test_every_case_gives_the_expected_text_and_notice(self) -> None:
        data = json.loads(CASES.read_text(encoding="utf-8"))
        results = {r["name"]: r for r in run_runner("cases", str(CASES))["results"]}
        for case in data["cases"]:
            with self.subTest(case=case["name"]):
                got = results[case["name"]]
                self.assertEqual(got["text"], case["expect"]["text"])
                self.assertEqual(got["notice"], case["expect"]["notice"])

    def test_random_edits_keep_every_comment_with_its_line(self) -> None:
        """画面や set で変えそうな変え方を、でたらめに注釈を足したファイルに組み合わせる（いつも同じ種）"""
        result = run_runner("random", "300")
        self.assertEqual(result["failed"], 0, json.dumps(result["failures"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    unittest.main()
