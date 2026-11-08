"""check_comment.py: rules and command-line behaviour."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from _support import UV, clean_bytecode, load_script, run_json, run_script

GOOD = "誤検知。get_buf() の戻り値は NULL になりうるが、呼び出し元 read_all()（src/io/reader.c:120）で NULL を除いてから渡しているため、NULL を参照することはない。"


def tearDownModule() -> None:
    clean_bytecode()


class RuleTest(unittest.TestCase):
    """The rules themselves (in-process)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = load_script("check_comment")

    def codes(self, comment: str) -> list[str]:
        return [p["code"] for p in self.m.check_comment(comment)["problems"]]

    def test_good_comment(self) -> None:
        result = self.m.check_comment(GOOD)
        self.assertEqual(result, {"ok": True, "length": len(GOOD), "problems": []})

    def test_each_problem_has_code_and_japanese_message(self) -> None:
        result = self.m.check_comment(" ただし、確認してください\n- `x`")
        self.assertFalse(result["ok"])
        for problem in result["problems"]:
            self.assertEqual(set(problem), {"code", "message"})
            self.assertRegex(problem["message"], r"[ぁ-んァ-ン一-龥]")

    def test_length_is_counted_after_strip(self) -> None:
        result = self.m.check_comment("  " + GOOD + "  ")
        self.assertEqual(result["length"], len(GOOD))
        self.assertEqual(self.codes("  " + GOOD + "  "), ["surrounding_whitespace"])

    def test_limit_is_200(self) -> None:
        self.assertEqual(self.m.COMMENT_MAX, 200)
        ok = "誤検知。" + "あ" * 195 + "。"
        self.assertEqual(len(ok), 200)
        self.assertEqual(self.codes(ok), [])
        self.assertEqual(self.codes(ok[:-1] + "あ。"), ["too_long"])

    def test_tadashi_is_a_condition(self) -> None:
        self.assertIn("condition_words", self.codes("本物の不具合。ただし、この機能は無効（config.h:12）である。"))

    def test_k98_fixed_wording_passes(self) -> None:
        self.assertEqual(self.codes("本物の不具合。この機能は今の製品では無効（config.h:12）で、この処理は実行されない。"), [])

    def test_recommend_noun_is_not_a_recommendation(self) -> None:
        self.assertEqual(self.codes("意図的。推奨範囲の 0〜10（src/a.c:3）に収めている。"), [])
        self.assertIn("request_words", self.codes("本物の不具合。src/a.c:3 の修正を推奨します。"))

    def test_sentences_ignore_marks_in_brackets(self) -> None:
        self.assertEqual(self.m._count_sentences("A（B。C）。D。"), 2)
        self.assertEqual(self.m._count_sentences("A。。B"), 2)
        self.assertEqual(self.m._count_sentences(""), 0)

    def test_unencodable_characters_become_json_escapes(self) -> None:
        text = json.dumps({"a": "誤検知😀"}, ensure_ascii=False)
        data = text.encode("cp932", errors="json_u_escape")
        self.assertIn(b"\\ud83d\\ude00", data)
        self.assertEqual(json.loads(data.decode("cp932")), {"a": "誤検知😀"})

    def test_found_words_are_named_in_message(self) -> None:
        problems = self.m.check_comment("誤検知。仮に NULL でも src/a.c:3 で戻る。")["problems"]
        self.assertIn("「仮に」", problems[0]["message"])

    def test_guess_words(self) -> None:
        """The writer's guesses (「〜と思う」「〜と考える」「〜かもしれない」「〜だろう」 …) are not facts."""
        base = "誤検知。index は parse()（src/a.c:10）で 0〜7 に制限している"
        for ending, word in (("と思う。", "と思う"), ("と思います。", "と思い"), ("と思われる。", "と思われ"),
                             ("と考える。", "と考える"), ("と考えます。", "と考えます"), ("と考えられる。", "と考えられ"),
                             ("かもしれない。", "かもしれ"), ("かも知れない。", "かも知れ"), ("だろう。", "だろう"),
                             ("でしょう。", "でしょう"), ("と推測される。", "と推測"), ("と推定される。", "と推定"),
                             ("ように思われる。", "ように思"), ("とみられる。", "とみられ")):
            with self.subTest(ending):
                problems = self.m.check_comment(base + ending)["problems"]
                self.assertEqual([p["code"] for p in problems], ["condition_words"])
                self.assertIn(f"「{word}」", problems[0]["message"])
        for word in ("おそらく", "たぶん"):
            with self.subTest(word):
                self.assertEqual(self.codes(f"誤検知。{word} NULL は渡されない（src/a.c:10）。"), ["condition_words"])

    def test_what_the_tool_does_is_not_a_guess(self) -> None:
        for text in ("誤検知。fatal_error()（src/fault.c:42）は戻らないが、noreturn がないため解析ツールは戻ると考えて警告する。",
                     "誤検知。Coverity は関数をまたいで範囲を推測しないが、index は parse()（src/a.c:10）で 0〜7 に制限している。",
                     "誤検知。Coverity は fatal_error() が戻るとみなすが、platform_reset()（src/timer.c:227）で止まり戻らない。",
                     "意図的。値 0 を未設定として扱う設計の考え方（docs/design.md:12）どおりで、取り違えではない。"):
            with self.subTest(text):
                self.assertEqual(self.codes(text), [])


@unittest.skipUnless(UV, "uv がないので、コマンドの試験を飛ばす")
class CommandTest(unittest.TestCase):
    """The command as SKILL.md tells to run it."""

    def test_ok_exit_0(self) -> None:
        code, out = run_json("check_comment", "--comment", GOOD)
        self.assertEqual(code, 0)
        self.assertEqual(out, {"ok": True, "length": len(GOOD), "problems": []})

    def test_ng_exit_1_with_error(self) -> None:
        code, out = run_json("check_comment", "--comment", "NULL にならない。")
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"])
        self.assertEqual([p["code"] for p in out["problems"]], ["no_category"])

    def test_stdin_json(self) -> None:
        code, out = run_json("check_comment", "--stdin", stdin=json.dumps({"comment": GOOD}, ensure_ascii=False))
        self.assertEqual((code, out["ok"]), (0, True))

    def test_stdin_plain_text_drops_one_line_end(self) -> None:
        code, out = run_json("check_comment", "--stdin", stdin=GOOD + "\r\n")
        self.assertEqual((code, out["length"]), (0, len(GOOD)))

    def test_stdin_bom_is_read(self) -> None:
        code, out = run_json("check_comment", "--stdin", stdin=b"\xef\xbb\xbf" + GOOD.encode("utf-8"))
        self.assertEqual(code, 0, out)

    def test_stdin_json_without_comment_is_an_error(self) -> None:
        code, out = run_json("check_comment", "--stdin", stdin='{"text": "x"}')
        self.assertEqual(code, 1)
        self.assertEqual(out["problems"][0]["code"], "bad_input")

    def test_no_input_and_bad_arguments(self) -> None:
        for args in ([], ["--bogus"], ["--comment", "x", "--stdin"]):
            code, out = run_json("check_comment", *args)
            self.assertEqual(code, 1, args)
            self.assertFalse(out["ok"])
            self.assertIn("error", out)
            self.assertEqual(out["problems"][0]["code"], "bad_input")

    def test_output_in_requested_encoding(self) -> None:
        code, out, _ = run_script("check_comment", "--comment", "誤検知😀", env={"PYTHONIOENCODING": "cp932"})
        self.assertEqual(code, 1)
        data = json.loads(out.decode("cp932"))
        self.assertIn("誤検知", data["problems"][0]["message"])
        self.assertEqual(data["length"], 4)
        self.assertEqual(data["problems"][0]["code"], "no_category")

    def test_ascii_output_escapes_everything_else(self) -> None:
        code, out, _ = run_script("check_comment", "--comment", GOOD, env={"PYTHONIOENCODING": "ascii"})
        self.assertEqual(code, 0)
        out.decode("ascii")
        self.assertTrue(json.loads(out)["ok"])

    def test_creates_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_json("check_comment", "--comment", GOOD, cwd=tmp)
            self.assertEqual(os.listdir(tmp), [])
        self.assertFalse((Path(__file__).resolve().parents[1] / "scripts" / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
