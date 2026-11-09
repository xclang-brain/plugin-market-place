"""Shared test data: contract "result" (this skill owns it) and a copy of contract "comment"."""

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

RESULT_CASES = SKILL / "assets" / "contract" / "result" / "cases.json"
COMMENT_CASES = SKILL / "assets" / "contract" / "comment" / "cases.json"
COMMENT_SCRIPT = SKILL / "scripts" / "check_comment.py"


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


class CommentRulesAreImportedTest(unittest.TestCase):
    """check_result.py uses scripts/check_comment.py (a byte copy of the shared comment rules) and holds none itself."""

    def test_the_copy_is_in_the_scripts_folder(self) -> None:
        self.assertTrue(COMMENT_SCRIPT.is_file(), "scripts/check_comment.py（逸脱コメントの決まりの写し）がない")

    def test_check_result_imports_the_copy_of_its_own_folder(self) -> None:
        source = (SKILL / "scripts" / "check_result.py").read_text(encoding="utf-8")
        self.assertIn("from check_comment import", source)
        for word in ("def check_comment", "def _count_sentences", "CONDITION_WORDS", "HEDGE_WORDS", "REQUEST_WORDS"):
            self.assertNotIn(word, source, "逸脱コメントの決まりを書き直さず、check_comment.py を読み込む")
        m = load_script("check_result")
        self.assertEqual(Path(m.check_comment.__code__.co_filename).resolve(), COMMENT_SCRIPT.resolve())
        self.assertEqual(m.CATEGORIES, load_script("check_comment").CATEGORIES)

    @unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
    def test_check_leaves_no_files_in_the_skill_folder(self) -> None:
        """Run from a copy of the folder without PYTHONDONTWRITEBYTECODE: importing the copy writes no __pycache__."""
        payload = next(c["input"] for c in load_cases(RESULT_CASES, "result") if c["name"] == "valid_true_bug_fix")
        with tempfile.TemporaryDirectory(prefix="report-copy-") as tmp:
            copy = Path(tmp) / "renamed-skill"
            for sub in ("scripts", "assets"):
                shutil.copytree(SKILL / sub, copy / sub, ignore=shutil.ignore_patterns("__pycache__"))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run([UV, "run", "--native-tls", "--quiet", str(copy / "scripts" / "check_result.py"),
                                   "check", "--stdin"], input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  capture_output=True, env=env, cwd=tmp, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(json.loads(proc.stdout.decode("utf-8"))["ok"])
            self.assertEqual([p.relative_to(copy).as_posix() for p in copy.rglob("__pycache__")], [])

    def test_missing_copy_is_reported_as_json(self) -> None:
        """A folder without check_comment.py gives one JSON object with the reason, not a traceback."""
        with tempfile.TemporaryDirectory(prefix="incomplete-") as tmp:
            scripts = Path(tmp) / "skill" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(SKILL / "scripts" / "check_result.py", scripts / "check_result.py")
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
            proc = subprocess.run([sys.executable, "-I", str(scripts / "check_result.py"), "check", "--stdin"], input=b"{}",
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
        """Every case through the copied script and through check_result.py (which imports it)."""
        copy, report = load_script("check_comment"), load_script("check_result")
        for case in load_cases(COMMENT_CASES, "comment"):
            with self.subTest(case["name"]):
                self.assertEqual(copy.check_comment(case["input"]["comment"])["ok"], case["expect"]["ok"])
                self.assertEqual(report.check_comment(case["input"]["comment"])["ok"], case["expect"]["ok"])

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
