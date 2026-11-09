"""Comment-keeping YAML rewrite (REQ-107), through ``gui.py yaml-edit --stdin``.

The cases of ``tests/data/yaml-edit-cases.json`` are this skill's own. The shared test data of the
contract (``assets/contract/yaml-edit/cases.json``, owned by the part that prepares the settings) is run
too when its copy is here: both copies must give the same answers."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import helpers

OWN = helpers.SKILL / "tests" / "data" / "yaml-edit-cases.json"
CONTRACT = helpers.SKILL / "assets" / "contract" / "yaml-edit" / "cases.json"


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("contract") == "yaml-edit", path
    return data["cases"]


class YamlEditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = helpers.temp_dir(self)
        self.env = helpers.env(self.tmp)

    def check(self, cases: list[dict]) -> None:
        for case in cases:
            with self.subTest(case["name"]):
                code, out = helpers.run_gui(["yaml-edit", "--stdin"], self.env, stdin=case["input"])
                self.assertEqual(code, 0, out)
                self.assertEqual(out["text"], case["expect"]["text"])
                self.assertEqual(out["notice"], case["expect"]["notice"])

    def test_own_cases(self) -> None:
        cases = load(OWN)
        self.assertGreaterEqual(len(cases), 12)
        self.check(cases)

    def test_contract_cases(self) -> None:
        if not CONTRACT.is_file():
            self.skipTest("assets/contract/yaml-edit/cases.json is not here yet (the copy of the contract arrives later)")
        self.check(load(CONTRACT))

    def test_wrong_input_is_told(self) -> None:
        code, out = helpers.run_gui(["yaml-edit", "--stdin"], self.env, stdin={"original": 1})
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        code, out = helpers.run_gui(["yaml-edit", "--stdin"], self.env, stdin=b"not json")
        self.assertEqual(code, 1)
        self.assertIn("JSON", out["error"])


if __name__ == "__main__":
    unittest.main()
