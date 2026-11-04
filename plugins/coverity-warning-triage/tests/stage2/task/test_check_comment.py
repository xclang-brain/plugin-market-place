"""逸脱コメントを確かめるスクリプト（skills/deviation-comment/scripts/check_comment.py）の試験（REQ-109）。

スクリプトを、コマンドラインから単体で動かして確かめる（引数か標準入力で文を渡し、JSON と終了コードを見る）。
依存はない。tests/run.py が tests/.venv の Python で動かす。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[2]
SKILL = PLUGIN / "skills" / "deviation-comment"
SCRIPT = SKILL / "scripts" / "check_comment.py"

# スキル deviation-comment の例の文（どれも合格する）
EXAMPLES = [
    "誤検知。get_buf() の戻り値は NULL になりうるが、呼び出し元 read_all()（src/io/reader.c:120）で NULL を除いてから"
    "渡しているため、NULL を参照することはない。",
    "意図的。case 2 から case 3 へのフォールスルーは、状態 2 の処理のあと状態 3 の処理を続けて行う設計"
    "（src/fsm.c:88 のコメントに記載）であり、break の書き忘れではない。",
    "本物の不具合。ただし、この機能は今の製品では無効（config.h:12）で、この処理は実行されない。",
]


def run(*args: str, stdin: str | None = None, script: Path = SCRIPT) -> tuple[dict, int]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(script), *args], input=(stdin or "").encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
    out = proc.stdout.decode("utf-8")
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError("JSON を1行だけ出すはずが、%d 行出た: %r（stderr: %r）" % (len(lines), out, proc.stderr))
    return json.loads(lines[0]), proc.returncode


class CheckCommentTest(unittest.TestCase):
    def test_req109_examples_of_the_skill_pass(self):
        """REQ-109 スキルの例の文（本物の不具合の例を含む）は合格し、終了コードは 0。"""
        for text in EXAMPLES:
            with self.subTest(text=text[:10]):
                result, code = run("--comment", text)
                self.assertEqual(code, 0, result)
                self.assertEqual(result, {"ok": True, "length": len(text), "problems": []})

    def test_req109_result_has_only_ok_length_problems(self):
        """REQ-109 結果の JSON は ok・length・problems の3つだけ。"""
        result, _ = run("--comment", "誤検知。理由。")
        self.assertEqual(sorted(result), ["length", "ok", "problems"])

    def test_req109_length_limit_is_200(self):
        """REQ-109 200 字はよく、201 字は不合格。前後の空白は数えない。"""
        head = "誤検知。"
        ok_text = head + "あ" * (200 - len(head))
        result, code = run("--comment", "  " + ok_text + "\n")
        self.assertEqual((result["ok"], result["length"], code), (True, 200, 0))
        result, code = run("--comment", ok_text + "あ")
        self.assertEqual((result["ok"], result["length"], code), (False, 201, 1))
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("200 字以内", result["problems"][0])
        self.assertIn("201 字", result["problems"][0])

    def test_req109_must_start_with_category(self):
        """REQ-109 判定区分（誤検知。・意図的。・本物の不具合。）で始まらなければ不合格。"""
        for text in ("get_buf() の戻り値は呼び出し元で確かめている。", "# 誤検知。見出しを付けた。", "誤検知 呼び出し元で確かめている。"):
            with self.subTest(text=text):
                result, code = run("--comment", text)
                self.assertEqual(code, 1)
                self.assertFalse(result["ok"])
                self.assertTrue(any("判定区分" in p for p in result["problems"]), result)

    def test_req109_conditions_are_not_written(self):
        """REQ-109 条件や但し書きの言い方（場合に限る、確認した場合は、に対して など）があれば不合格。"""
        for word, text in (("場合に限", "誤検知。ただし、引数が NULL でない場合に限る。"),
                           ("た場合は", "誤検知。呼び出し元を確認した場合は問題にならない。"),
                           ("に対して", "意図的。この入力に対しては問題にならない。"),
                           ("に限り", "意図的。初期化の後に限り呼ばれる。")):
            with self.subTest(word=word):
                result, code = run("--comment", text)
                self.assertEqual(code, 1)
                self.assertTrue(any("条件や但し書き" in p and word in p for p in result["problems"]), result)

    def test_req109_recommendations_and_requests_are_not_written(self):
        """REQ-109 推奨や人への依頼の言い方（修正を推奨、確認してください など）があれば不合格。"""
        for word, text in (("推奨", "本物の不具合。修正を推奨する。"),
                           ("ください", "誤検知。念のため確認してください。"),
                           ("要確認", "誤検知。呼び出し元は要確認。")):
            with self.subTest(word=word):
                result, code = run("--comment", text)
                self.assertEqual(code, 1)
                self.assertTrue(any("推奨や人への依頼" in p and word in p for p in result["problems"]), result)

    def test_req109_recommend_phrases_fail_but_nouns_pass(self):
        """REQ-109 「を推奨」「推奨します」「推奨する」は不合格。「推奨範囲」のような名詞の一部は合格。"""
        for word, text in (("を推奨", "誤検知。修正を推奨。"),
                           ("推奨します", "誤検知。このままの使い方を推奨します。"),
                           ("推奨する", "意図的。この書き方は、そのまま残すよう推奨する。")):
            with self.subTest(word=word):
                result, code = run("--comment", text)
                self.assertEqual(code, 1)
                self.assertTrue(any("推奨や人への依頼" in p and "「%s」" % word in p for p in result["problems"]),
                                result)
        for text in ("誤検知。値は呼び出し元 clamp()（src/a.c:10）で推奨範囲の 0〜100 に丸めてから渡すため、範囲を外れない。",
                     "誤検知。呼び出し元 set_level()（src/a.c:20）で値を推奨範囲に丸めてから渡すため、範囲を外れない。",
                     "意図的。推奨値の 3 を既定にする設計（src/conf.c:5 のコメントに記載）である。"):
            with self.subTest(text=text[:20]):
                result, code = run("--comment", text)
                self.assertEqual((result["ok"], code), (True, 0), result)

    def test_req109_all_problems_are_listed(self):
        """REQ-109 直すところが複数あれば、すべてを problems に並べる。"""
        result, code = run("--comment", "修正を推奨。この条件に対しては問題ない。" + "あ" * 200)
        self.assertEqual(code, 1)
        self.assertEqual(len(result["problems"]), 4, result)

    def test_req109_comment_from_stdin(self):
        """REQ-109 --comment を付けなければ、標準入力の文を確かめる。"""
        result, code = run(stdin=EXAMPLES[0] + "\n")
        self.assertEqual((result["ok"], result["length"], code), (True, len(EXAMPLES[0]), 0))
        result, code = run(stdin="確認してください。")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])

    def test_req109_empty_or_missing_comment_fails(self):
        """REQ-109 空の文や、文を渡さないときは、不合格（終了コード 1）で理由を示す。"""
        result, code = run("--comment", "   ")
        self.assertEqual((result["ok"], result["length"], code), (False, 0, 1))
        self.assertTrue(result["problems"])
        result, code = run(stdin="")
        self.assertEqual((result["ok"], code), (False, 1))
        self.assertTrue(result["problems"])

    def test_req109_wrong_argument_gives_json(self):
        """REQ-109 引数の誤りでも、JSON を1つ出して終了コード 1 にする（使い方を標準出力に出さない）。"""
        result, code = run("--no-such-option", "x")
        self.assertEqual((result["ok"], code), (False, 1))
        self.assertIn("引数", result["problems"][0])

    def test_req109_works_from_the_skill_folder_alone(self):
        """REQ-109 スキルのフォルダだけを別の場所に写しても動く（ほかの部品に頼らない）。"""
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "deviation-comment"
            shutil.copytree(SKILL, copy, ignore=shutil.ignore_patterns("__pycache__"))
            result, code = run("--comment", EXAMPLES[1], script=copy / "scripts" / "check_comment.py")
            self.assertEqual((result["ok"], code), (True, 0))


if __name__ == "__main__":
    unittest.main()
