"""Shared test data: contract "plan" (this skill owns it), a copy of contract "comment",
and the result definitions inside plan.schema.json (compared with the copy of result.schema.json in this folder)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SKILL, UV, clean_bytecode, load_script, run_json

ASSETS = SKILL / "assets"
PLAN_CASES = ASSETS / "contract" / "plan" / "cases.json"
COMMENT_CASES = ASSETS / "contract" / "comment" / "cases.json"
COMMENT_SCRIPT = SKILL / "scripts" / "check_comment.py"
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


class CommentRulesAreImportedTest(unittest.TestCase):
    """check_plan.py uses scripts/check_comment.py (a byte copy of the shared comment rules) and holds none itself."""

    def test_the_copy_is_in_the_scripts_folder(self) -> None:
        self.assertTrue(COMMENT_SCRIPT.is_file(), "scripts/check_comment.py（逸脱コメントの決まりの写し）がない")

    def test_check_plan_imports_the_copy_of_its_own_folder(self) -> None:
        source = (SKILL / "scripts" / "check_plan.py").read_text(encoding="utf-8")
        self.assertIn("from check_comment import", source)
        for word in ("def check_comment", "def _count_sentences", "CONDITION_WORDS", "HEDGE_WORDS", "REQUEST_WORDS"):
            self.assertNotIn(word, source, "逸脱コメントの決まりを書き直さず、check_comment.py を読み込む")
        m = load_script("check_plan")
        self.assertEqual(Path(m.check_comment.__code__.co_filename).resolve(), COMMENT_SCRIPT.resolve())
        self.assertEqual(m.CATEGORIES, load_script("check_comment").CATEGORIES)

    @unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
    def test_check_leaves_no_files_in_the_skill_folder(self) -> None:
        """Run from a copy of the folder without PYTHONDONTWRITEBYTECODE: importing the copy writes no __pycache__."""
        payload = next(c["input"] for c in load_cases(PLAN_CASES, "plan") if c["name"] == "valid_merge_of_two_items")
        with tempfile.TemporaryDirectory(prefix="integration-copy-") as tmp:
            copy = Path(tmp) / "renamed-skill"
            for sub in ("scripts", "assets"):
                shutil.copytree(SKILL / sub, copy / sub, ignore=shutil.ignore_patterns("__pycache__"))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run([UV, "run", "--native-tls", "--quiet", str(copy / "scripts" / "check_plan.py"), "--stdin"],
                                  input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  capture_output=True, env=env, cwd=tmp, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(json.loads(proc.stdout.decode("utf-8"))["ok"])
            self.assertEqual([p.relative_to(copy).as_posix() for p in copy.rglob("__pycache__")], [])

    def test_missing_copy_is_reported_as_json(self) -> None:
        """A folder without check_comment.py gives one JSON object with the reason, not a traceback."""
        with tempfile.TemporaryDirectory(prefix="incomplete-") as tmp:
            scripts = Path(tmp) / "skill" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(SKILL / "scripts" / "check_plan.py", scripts / "check_plan.py")
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
            proc = subprocess.run([sys.executable, "-I", str(scripts / "check_plan.py"), "--stdin"], input=b"{}",
                                  capture_output=True, env=env, cwd=tmp, timeout=120)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn(b"Traceback", proc.stderr)
            lines = proc.stdout.decode("utf-8").splitlines()
            self.assertEqual(len(lines), 1, proc.stdout)
            out = json.loads(lines[0])
            self.assertFalse(out["ok"])
            self.assertIn("check_comment.py", out["error"])
            self.assertEqual(out["problems"][0]["code"], "missing_comment_rules")

@unittest.skipUnless(COMMENT_CASES.is_file(), "assets/contract/comment/cases.json がない（写しは後で置かれる）")
class CommentContractCopyTest(unittest.TestCase):
    def test_case_file_shape(self) -> None:
        cases = load_cases(COMMENT_CASES, "comment")
        self.assertGreaterEqual(len(cases), 8)
        self.assertEqual(len({c["name"] for c in cases}), len(cases))

    @unittest.skipUnless(COMMENT_SCRIPT.is_file(), "scripts/check_comment.py がない")
    def test_comment_rules(self) -> None:
        """Every case through the copied script and through check_plan.py (which imports it)."""
        copy, plan = load_script("check_comment"), load_script("check_plan")
        for case in load_cases(COMMENT_CASES, "comment"):
            with self.subTest(case["name"]):
                self.assertEqual(copy.check_comment(case["input"]["comment"])["ok"], case["expect"]["ok"])
                self.assertEqual(plan.check_comment(case["input"]["comment"])["ok"], case["expect"]["ok"])

    PICKED = ("false_positive_example_passes", "true_bug_with_tadashi_fails", "think_omou_fails", "darou_fails",
              "tool_assumes_kangaete_passes", "request_word_fails")

    @unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
    def test_comment_rules_inside_plan_check(self) -> None:
        """A plan that is fine except for its merged comment is accepted exactly when the comment is."""
        base = next(c["input"] for c in load_cases(PLAN_CASES, "plan") if c["name"] == "valid_merge_of_two_items")
        picked = [c for c in load_cases(COMMENT_CASES, "comment") if c["name"] in self.PICKED]
        self.assertEqual(len(picked), len(self.PICKED))
        for case in picked:
            with self.subTest(case["name"]):
                value = json.loads(json.dumps(base))
                value["plan"]["merges"][0]["result"]["deviation"]["comment"] = case["input"]["comment"]
                code, out = run_json("check_plan", "--stdin", stdin=json.dumps(value, ensure_ascii=False))
                self.assertEqual(out["ok"], case["expect"]["ok"], out)


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
