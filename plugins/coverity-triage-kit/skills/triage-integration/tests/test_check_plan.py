"""check_plan.py: the command forms and the rules that need no shared data."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from _support import SKILL, UV, clean_bytecode, load_script, run_json


def tearDownModule() -> None:
    clean_bytecode()


def skill_example() -> dict:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    return json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))


def example_ids() -> tuple[list[str], list[str], str]:
    """The merged members, the conflicting items and the merged confidence of the SKILL.md example."""
    plan = skill_example()
    merge = plan["merges"][0]
    return list(merge["members"]), list(plan["conflicts"][0]["items"]), merge["result"]["confidence"]


class RulesInProcessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_script("check_plan")

    def test_member_option_parsing(self) -> None:
        items = self.m._parse_members(["20011:20011:high", "G1:30001, 30002", " G2 :30003:low"], ["30002"])
        self.assertEqual(items["20011"], {"cids": [20011], "status": "done", "no_grouping": False, "confidence": "high"})
        self.assertEqual(items["G1"], {"cids": [30001, 30002], "status": "done", "no_grouping": True})
        self.assertEqual(items["G2"]["confidence"], "low")
        for bad in (["20011"], ["20011:abc"], ["20011:20011:sure"], [":20011"], ["a:1:high:x"]):
            with self.assertRaises(ValueError):
                self.m._parse_members(bad, [])
        with self.assertRaises(ValueError):
            self.m._parse_members(["a:1"], ["x"])

    def test_items_shape(self) -> None:
        self.assertEqual(self.m._valid_items({"items": {"a": {"cids": [1]}}}), {"a": {"cids": [1]}})
        for bad in ([], {"a": {"cids": ["1"]}}, {"a": "x"}, {"a": {"cids": [True]}}):
            with self.assertRaises(ValueError):
                self.m._valid_items(bad)

    def test_merged_combo_warnings(self) -> None:
        result = copy.deepcopy(skill_example()["merges"][0]["result"])
        self.assertEqual(self.m.combo_warnings(result, "merges[0].result."), [])
        result["deviation"]["classification"] = "False Positive"
        warnings = self.m.combo_warnings(result, "merges[0].result.")
        self.assertEqual([(w["code"], w["path"]) for w in warnings], [("deviation_combo", "merges[0].result.deviation")])


@unittest.skipUnless(UV, "uv がないので、形の確かめ（jsonschema を使う）を飛ばす")
class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.plan = self.dir / "plan.json"
        self.plan.write_text(json.dumps(skill_example(), ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def members(self) -> list[str]:
        """--member for every item of the example: the first member high, the others at the merged confidence."""
        merged, conflicting, confidence = example_ids()
        levels = ["high"] + [confidence] * (len(merged) - 1) + ["high", "low"][:len(conflicting)]
        args = []
        for item, level in zip(merged + conflicting, levels):
            args += ["--member", f"{item}:{item}:{level}"]
        return args

    def test_skill_example_passes_with_members(self) -> None:
        code, out = run_json("check_plan", "--plan", str(self.plan), *self.members(), cwd=self.dir)
        self.assertEqual(code, 0, out)
        self.assertEqual((out["merges"], out["conflicts"], out["warnings"]), (1, 1, []))
        self.assertEqual(sorted(os.listdir(self.dir)), ["plan.json"])  # nothing written

    def test_without_items_only_warns(self) -> None:
        code, out = run_json("check_plan", "--plan", str(self.plan))
        self.assertEqual(code, 0, out)
        self.assertEqual([w["code"] for w in out["warnings"]], ["items_unchecked"])

    def test_no_grouping_option(self) -> None:
        second = example_ids()[0][1]
        code, out = run_json("check_plan", "--plan", str(self.plan), *self.members(), "--no-grouping", second)
        self.assertEqual(code, 1)
        self.assertEqual([p["code"] for p in out["problems"]], ["member_no_grouping"])
        self.assertIn("merges[0].members", out["problems"][0]["message"])

    def test_items_file_and_stdin(self) -> None:
        merged, conflicting, confidence = example_ids()
        self.assertNotEqual(confidence, "low", "the example's merged confidence must leave room for a lower member")
        items = {m: {"cids": [int(m)], "confidence": confidence} for m in merged}
        items.update({c: {"cids": [int(c)]} for c in conflicting})
        path = self.dir / "items.json"
        path.write_text(json.dumps({"items": items}), encoding="utf-8")
        code, out = run_json("check_plan", "--plan", str(self.plan), "--items", str(path))
        self.assertEqual(code, 0, out)
        items[merged[0]]["confidence"] = "low"
        code, out = run_json("check_plan", "--stdin",
                             stdin=json.dumps({"plan": skill_example(), "items": items}, ensure_ascii=False))
        self.assertEqual([p["code"] for p in out["problems"]], ["confidence_too_high"])

    def test_input_errors(self) -> None:
        bad = self.dir / "bad.json"
        bad.write_text("{", encoding="utf-8")
        for args, stdin in ((["--plan", str(bad)], None), (["--plan", str(self.dir / "none.json")], None), ([], None),
                            (["--stdin"], '{"items": {}}'), (["--plan", str(self.plan), "--member", "x"], None),
                            (["--plan", str(self.plan), "--no-grouping", "1"], None)):
            code, out = run_json("check_plan", *args, stdin=stdin)
            self.assertEqual((code, out["problems"][0]["code"]), (1, "bad_input"), args)
            self.assertTrue(out["error"])


if __name__ == "__main__":
    unittest.main()
