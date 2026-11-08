"""The confidence shown to the person (contract "confidence"): scripts/confidence.py is a byte copy of the
batch run's file, and the screen uses it as it is.

Every case of ``assets/contract/confidence/cases.json`` (the shared test data, copied from the owner) is run
through the copied function, and through the screen's own path (a run folder read by guilib.runs)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import unittest

import helpers

RULE = helpers.SKILL / "scripts" / "confidence.py"
CASES = helpers.SKILL / "assets" / "contract" / "confidence" / "cases.json"
RUNNER = helpers.SKILL / "tests" / "confidence_runner.py"


def cases() -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    assert (data.get("version"), data.get("contract")) == (1, "confidence"), CASES
    return data["cases"]


def load_rule():
    spec = importlib.util.spec_from_file_location("gui_test_confidence", RULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfidenceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        if not CASES.is_file():
            self.skipTest("assets/contract/confidence/cases.json is not here (the copy of the contract arrives later)")

    def test_every_case_through_the_copied_rule(self) -> None:
        rule = load_rule()
        found = cases()
        self.assertGreaterEqual(len(found), 8)
        for case in found:
            with self.subTest(case["name"]):
                given = case["input"]
                got = rule.effective_confidence(given["result"], given.get("review"), given.get("verify"),
                                                given.get("run_item"))
                for key, value in case["expect"].items():
                    self.assertEqual(got[key], value)

    def test_every_case_through_the_screen(self) -> None:
        if helpers.UV is None:
            self.skipTest("uv is not on PATH")
        proc = subprocess.run([helpers.UV, "run", "--native-tls", "--quiet", str(RUNNER)], capture_output=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=180, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        answer = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(answer["rule_file"], str(RULE.resolve()))  # the copy, not a rule of its own
        self.assertEqual(len(answer["cases"]), len(cases()))
        for case in answer["cases"]:
            with self.subTest(case["name"]):
                self.assertEqual(case["got"]["confidence"], case["expect"]["confidence"])
                self.assertEqual(case["got"]["reason"], case["direct"]["reason"])

    def test_the_screen_has_no_review_filter_of_its_own(self) -> None:
        source = (helpers.SKILL / "scripts" / "guilib" / "runs.py").read_text(encoding="utf-8")
        self.assertNotIn("TAKEN_REVIEWS", source)
        body = source.split("def effective_confidence(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn('item.get("review")', body)  # the rule decides which review counts

    def test_the_copy_is_self_contained(self) -> None:
        text = RULE.read_text(encoding="utf-8")
        self.assertIn("def effective_confidence(", text)
        self.assertNotIn("open(", text.split("# --- end of confidence rule")[0])  # the rule reads no file


if __name__ == "__main__":
    unittest.main()
