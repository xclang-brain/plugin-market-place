"""check_result.py: check and render."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from _support import SKILL, UV, clean_bytecode, load_script, run_json

RESULT = {
    "item": "20001",
    "verdict": {"judgement": "true_bug", "summary": "エラーのとき fp を閉じずに戻っている",
                "rationale": "18 行の return で fclose を通らずに戻る。",
                "evidence": [{"file": "src/reader.c", "line": 18, "note": "fclose の前の return"}]},
    "recommendation": "fix", "confidence": "high", "confidence_reason": "経路をコードで最後まで追えた",
    "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Moderate",
                  "comment": "本物の不具合。read_config()（src/reader.c:18）はエラーのとき fp を閉じずに戻っており、直さない理由は説明できない。"},
    "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Moderate",
            "summary": "18 行の return の前で fclose する", "impact": "エラーのときだけ fp を閉じる",
            "exceeded_constraints": [], "already_fixed_on_latest": False},
    "revision_drift": {"status": "none", "detail": ""},
    "group_excluded_cids": [],
}


def tearDownModule() -> None:
    clean_bytecode()


class RulesInProcessTest(unittest.TestCase):
    """Rules that need no schema library (combos, rendering)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_script("check_result")

    def test_combo_warnings(self) -> None:
        result = copy.deepcopy(RESULT)
        self.assertEqual(self.m.combo_warnings(result, "Medium"), [])
        self.assertEqual([w["code"] for w in self.m.combo_warnings(result, "High")], ["fix_severity"])
        result["deviation"].update(classification="False Positive")
        self.assertEqual([w["code"] for w in self.m.combo_warnings(result)], ["deviation_combo"])
        result["verdict"]["judgement"] = "false_positive"
        self.assertEqual([w["code"] for w in self.m.combo_warnings(result)], ["comment_judgement", "deviation_combo"])

    def test_render_order_and_missing_items(self) -> None:
        line, text = self.m.render(RESULT)
        self.assertEqual(line, "20001: 修正を推奨（確信度 高）— エラーのとき fp を閉じずに戻っている")
        heads = ["1. 見立て", "判定：本物のバグ（true_bug）", "2. 推奨：修正（fix）", "確信度：高（high）",
                 "3. 調べた経過", "src/reader.c:18 — fclose の前の return", "4. 逸脱案", "逸脱コメント：本物の不具合。",
                 "5. 修正案", "影響：", "決まりを超えたこと：なし", "6. リビジョンのずれ：なし（none）", "外した CID：なし"]
        positions = [text.index(h) for h in heads]
        self.assertEqual(positions, sorted(positions))

    def test_render_partial_result(self) -> None:
        line, text = self.m.render({"item": "G2", "verdict": "壊れた値", "group_excluded_cids": [30003]})
        self.assertTrue(line.startswith("G2: なしを推奨"))
        self.assertIn("1. 見立て：なし", text)
        self.assertIn("根拠の場所：なし", text)
        self.assertIn("外した CID：30003", text)
        self.assertNotIn("最新のリビジョン", text)  # no fix proposal given


@unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, value, name: str = "result.json") -> str:
        path = self.dir / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_check_ok(self) -> None:
        code, out = run_json("check_result", "check", "--result", self.write(RESULT), "--item", "20001", cwd=self.dir)
        self.assertEqual(code, 0)
        self.assertEqual(out, {"ok": True, "item": "20001", "problems": [], "warnings": []})
        self.assertEqual(sorted(os.listdir(self.dir)), ["result.json"])  # nothing written

    def test_check_reports_problems_with_paths(self) -> None:
        result = copy.deepcopy(RESULT)
        result["verdict"]["summary"] = ""
        result["extra"] = True
        code, out = run_json("check_result", "check", "--result", self.write(result), "--item", "20001")
        self.assertEqual(code, 1)
        self.assertTrue(out["error"])
        paths = sorted(p["path"] for p in out["problems"])
        self.assertEqual(paths, ["verdict.summary", "（全体）"])
        self.assertTrue(all(p["code"] == "schema" for p in out["problems"]))

    def test_cids_option_forms(self) -> None:
        result = dict(copy.deepcopy(RESULT), item="G1", group_excluded_cids=[30003])
        path = self.write(result)
        for args in (["--cids", "30001,30002,30003"], ["--cids", "30001", "--cids", "30002, 30003"]):
            code, out = run_json("check_result", "check", "--result", path, "--item", "G1", *args)
            self.assertEqual(code, 0, out)
        code, out = run_json("check_result", "check", "--result", path, "--item", "G1", "--cids", "30003")
        self.assertEqual([p["code"] for p in out["problems"]], ["group_excluded_not_group"])
        code, out = run_json("check_result", "check", "--result", path, "--cids", "abc")
        self.assertEqual((code, out["problems"][0]["code"]), (1, "bad_input"))

    def test_stdin_form(self) -> None:
        payload = {"result": RESULT, "item": "20001", "cids": [20001], "impact": "Medium"}
        code, out = run_json("check_result", "check", "--stdin", stdin=json.dumps(payload, ensure_ascii=False))
        self.assertEqual((code, out["warnings"]), (0, []))
        code, out = run_json("check_result", "check", "--stdin", stdin='{"item": "1"}')
        self.assertEqual((code, out["problems"][0]["code"]), (1, "bad_input"))

    def test_input_errors(self) -> None:
        bad = self.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        for args in (["check", "--result", str(bad)], ["check", "--result", str(self.dir / "none.json")], ["check"], [],
                     ["render", "--result", self.write([1, 2], "list.json")]):
            code, out = run_json("check_result", *args)
            self.assertEqual(code, 1, args)
            self.assertFalse(out["ok"])
            self.assertTrue(out["error"])

    def test_bom_file_is_read(self) -> None:
        path = self.dir / "bom.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(RESULT, ensure_ascii=False).encode("utf-8"))
        code, out = run_json("check_result", "check", "--result", str(path))
        self.assertEqual(code, 0, out)

    def test_render(self) -> None:
        code, out = run_json("check_result", "render", "--result", self.write(RESULT))
        self.assertEqual(code, 0)
        self.assertEqual(set(out), {"ok", "line", "text", "valid", "problems"})
        self.assertTrue(out["valid"])
        self.assertTrue(out["text"].startswith(out["line"]))
        broken = dict(copy.deepcopy(RESULT), confidence="sure")
        code, out = run_json("check_result", "render", "--result", self.write(broken))
        self.assertEqual(code, 0)
        self.assertFalse(out["valid"])
        self.assertIn("確信度：sure", out["text"])

    def test_examples_in_skill_files_pass(self) -> None:
        import re

        for path in (SKILL / "SKILL.md", SKILL / "references" / "examples.md"):
            for block in re.findall(r"```json\n(.*?)```", path.read_text(encoding="utf-8"), re.S):
                value = json.loads(block)
                code, out = run_json("check_result", "check", "--stdin",
                                     stdin=json.dumps({"result": value}, ensure_ascii=False))
                self.assertEqual(code, 0, (path.name, out))
                self.assertEqual([w for w in out["warnings"] if w["code"] != "group_unchecked"], [], path.name)


if __name__ == "__main__":
    unittest.main()
