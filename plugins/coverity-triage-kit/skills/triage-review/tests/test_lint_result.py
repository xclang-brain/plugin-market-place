"""lint_result.py: the fixed-rule points and the command; plus the lint contract cases."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from _support import SKILL, UV, clean_bytecode, load_script, run_json

CASES = SKILL / "assets" / "contract" / "lint" / "cases.json"
RESULT = {
    "item": "20001",
    "verdict": {"judgement": "true_bug", "summary": "s", "rationale": "r",
                "evidence": [{"file": "src/reader.c", "line": 3, "note": ""}]},
    "recommendation": "fix", "confidence": "high", "confidence_reason": "c",
    "deviation": {"classification": "Bug", "action": "Ignore", "comment": "本物の不具合。x（src/reader.c:3）。"},
    "fix": {"classification": "Bug", "action": "Fix Required", "summary": "s", "impact": "i"},
    "revision_drift": {"status": "none"},
}


def tearDownModule() -> None:
    clean_bytecode()


def make_repo(root: Path, files: dict[str, str]) -> Path:
    repo = root / "repo"
    repo.mkdir()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    return repo


class RuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_script("lint_result")

    def test_codes_are_the_contract_codes(self) -> None:
        self.assertEqual(set(self.m.LINT_CODES), {"evidence_missing_file", "evidence_line_out_of_range",
                                                  "undetermined_high", "drift_high", "true_bug_deviation",
                                                  "not_bug_fix", "no_evidence"})

    def test_resolve_in_repo(self) -> None:
        repo = Path("repo")
        self.assertEqual(self.m.resolve_in_repo(repo, "src\\a.c"), repo / "src" / "a.c")
        self.assertEqual(self.m.resolve_in_repo(repo, " ./src/./a.c "), repo / "src" / "a.c")
        for bad in ("", "/abs/a.c", "C:\\a.c", "c:/a.c", "..", ".", "..\\a.c", "src\\..\\..\\a.c"):
            self.assertIsNone(self.m.resolve_in_repo(repo, bad), bad)

    def test_count_lines(self) -> None:
        for data, n in ((b"", 0), (b"a", 1), (b"a\n", 1), (b"a\nb", 2), (b"a\r\nb\r\n", 2), (b"a\rb\r", 2), (b"\n\n", 2)):
            self.assertEqual(self.m.count_lines(data), n, data)

    def test_findings_name_the_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp), {"src/reader.c": "a\nb\n"})
            findings = self.m.lint(RESULT, repo)
        self.assertEqual(findings[0]["code"], "evidence_line_out_of_range")
        self.assertEqual((findings[0]["file"], findings[0]["line"]), ("src/reader.c", 3))
        self.assertIn("2 行まで", findings[0]["message"])

    def test_broken_shapes_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp), {})
            for value, expected in (({}, ["no_evidence"]), ({"verdict": "x"}, ["no_evidence"]),
                                    ({"verdict": {"evidence": ["x", 1]}}, ["no_evidence"]),
                                    ({"verdict": {"evidence": [{"line": True}]}}, ["evidence_missing_file"])):
                self.assertEqual(self.m.points(self.m.lint(value, repo)), expected, value)


@unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = make_repo(self.root, {"src/reader.c": "a\nb\nc\n"})
        self.result_file = self.root / "result.json"
        self.result_file.write_text(json.dumps(RESULT, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_file_input(self) -> None:
        code, out = run_json("lint_result", "--result", str(self.result_file), "--repo", str(self.repo), cwd=self.root)
        self.assertEqual((code, out), (0, {"ok": True, "points": [], "findings": []}))
        self.assertEqual(sorted(os.listdir(self.root)), ["repo", "result.json"])

    def test_stdin_input_plain_and_wrapped(self) -> None:
        value = dict(RESULT, recommendation="deviation")
        for payload in (value, {"result": value}):
            code, out = run_json("lint_result", "--stdin", "--repo", str(self.repo),
                                 stdin=json.dumps(payload, ensure_ascii=False))
            self.assertEqual((code, out["points"]), (0, ["true_bug_deviation"]))

    def test_input_errors(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("[1,", encoding="utf-8")
        for args in (["--result", str(self.result_file)], ["--repo", str(self.repo)],
                     ["--result", str(bad), "--repo", str(self.repo)],
                     ["--result", str(self.root / "none.json"), "--repo", str(self.repo)],
                     ["--result", str(self.result_file), "--repo", str(self.root / "none")]):
            code, out = run_json("lint_result", *args)
            self.assertEqual(code, 1, args)
            self.assertFalse(out["ok"])
            self.assertEqual(out["problems"][0]["code"], "bad_input")


class LintContractTest(unittest.TestCase):
    """This skill owns the lint contract."""

    def cases(self) -> list[dict]:
        data = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertEqual((data["version"], data["contract"]), (1, "lint"))
        return data["cases"]

    def test_case_file_shape(self) -> None:
        cases = self.cases()
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len({c["name"] for c in cases}), len(cases))
        codes = set(load_script("lint_result").LINT_CODES)
        seen = set()
        for case in cases:
            self.assertEqual(set(case["input"]), {"result", "files"})
            self.assertEqual(case["expect"]["points"], sorted(set(case["expect"]["points"])))
            seen |= set(case["expect"]["points"])
        self.assertEqual(seen, codes)  # every code is covered

    def test_in_process(self) -> None:
        m = load_script("lint_result")
        for case in self.cases():
            with self.subTest(case["name"]), tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(Path(tmp), case["input"]["files"])
                self.assertEqual(m.points(m.lint(case["input"]["result"], repo)), case["expect"]["points"])

    @unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
    def test_command(self) -> None:
        for case in self.cases():
            with self.subTest(case["name"]), tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(Path(tmp), case["input"]["files"])
                code, out = run_json("lint_result", "--stdin", "--repo", str(repo),
                                     stdin=json.dumps({"result": case["input"]["result"]}, ensure_ascii=False))
                self.assertEqual((code, out["points"]), (0, case["expect"]["points"]))


if __name__ == "__main__":
    unittest.main()
