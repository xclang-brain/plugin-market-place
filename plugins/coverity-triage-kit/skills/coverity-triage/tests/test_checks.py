"""The field checks of result.json and of a review, and the rules of a deviation comment."""

import unittest

import support  # noqa: F401 - puts scripts/ on the path
import checks

GOOD = support.FIX_COMMENT


class CommentTest(unittest.TestCase):

    def problems(self, comment) -> str:
        return "\n".join(checks.check_comment(comment))

    def test_a_good_comment_has_no_problems(self):
        for comment in (GOOD, support.KEEP_COMMENT, "意図的。src/a.c:8 のフォールスルーは、設計書 3.2 の状態遷移のとおりである。"):
            self.assertEqual(checks.check_comment(comment), [], comment)

    def test_length_is_200_characters_at_most(self):
        exact = "誤検知。" + "あ" * 196
        self.assertEqual(checks.check_comment(exact), [])
        self.assertIn("200 文字以内", self.problems(exact + "あ"))
        self.assertIn("現在 201 文字", self.problems(exact + "あ"))

    def test_empty_and_untidy_comments(self):
        for empty in ("", "   ", None, 5):
            self.assertIn("空です", self.problems(empty))
        self.assertIn("前後の空白", self.problems(GOOD + "\n"))
        self.assertIn("1行で", self.problems(GOOD.replace("、", "、\n", 1)))

    def test_starts_with_the_category_and_gives_a_reason(self):
        self.assertIn("のいずれか）で始めてください", self.problems("src/a.c:3 は問題ない。"))
        self.assertIn("問題にならない理由", self.problems("誤検知。"))

    def test_one_or_two_plain_sentences_after_the_verdict(self):
        two = "誤検知。index は parse_header()（src/reader.c:55〜60）で 0〜7 に制限している。配列 buf は 8 要素で、外を読むことはない。"
        quoted = "意図的。レジスタを書く順は決まっており、src/hal/reg.c:40 のコメント「REG_A のあとに REG_B を書くこと。」のとおりに書いている。"
        for good in (two, quoted, "意図的。タイムアウトは推奨値の 30 秒（src/net.c:44）に固定しており、0 になることはない。",
                     "誤検知。引数 char **argv は main()（src/main.c:20）から必ず NULL でない値で渡される。",
                     "誤検知。index は parse_header()（src/reader.c:55〜60）で 0〜7 に制限しており、配列の外を読むことがない"):
            self.assertEqual(checks.check_comment(good), [], good)
        refused = {  # the comments that the reviewers saw pass
            "誤検知。index は 0〜7 に制限している（src/reader.c:55）。配列 buf は 8 要素である。外を読むことはない。": "1〜2文にしてください（現在 3 文）",
            "本物のバグ。src/a.c:42 で戻り値を確認していないため、修正を推奨。": "「を推奨」",
            "誤検知。`get_buf()` の戻り値は呼び出し元 read_all()（src/io/reader.c:120）で確認している。": "書式を使わずに",
            "誤検知。**呼び出し元で** NULL を除外している（src/a.c:10）。": "書式を使わずに",
            "本物のバグ。legacy_export は無効（src/config.c:12）で、src/export.c:18 の strcpy は実行されない。有効にする前に直すこと。": "「〜こと。」",
            "誤検知。呼び出し元 init()（src/a.c:10）で NULL を除外している。呼び出し元を追加するときは len の確認をこの関数に入れること": "「〜こと。」"}
        for comment, problem in refused.items():
            self.assertIn(problem, self.problems(comment), comment)

    def test_conditions_guesses_process_and_requests_are_refused(self):
        for word, kind in (("ただし", "条件や推測"), ("場合は", "条件や推測"), ("おそらく", "条件や推測"),
                           ("と思われ", "条件や推測"), ("確認できなかった", "調査の過程"), ("ください", "依頼や指示"),
                           ("見直", "依頼や指示"), ("推奨する", "依頼や指示")):
            found = self.problems(f"誤検知。src/a.c:3 は{word}範囲内である。")
            self.assertIn(kind, found, word)
            self.assertIn(f"「{word}」", found)


class ResultTest(unittest.TestCase):

    def problems(self, cid=20001, with_fix=True, **over) -> str:
        return "\n".join(checks.check_result(support.result(20001, with_fix, **over), cid))

    def test_good_results(self):
        self.assertEqual(self.problems(), "")
        self.assertEqual(self.problems(with_fix=False), "")
        self.assertEqual(self.problems(verdict="undetermined", confidence="low"), "")
        self.assertIn("undetermined のときは low", self.problems(verdict="undetermined"))

    def test_not_an_object(self):
        self.assertIn("オブジェクト", "\n".join(checks.check_result([1], 20001)))

    def test_every_field_is_checked(self):
        self.assertIn("verdict:", self.problems(verdict=["true_bug"]))  # a list: a problem to correct, no crash
        cases = {"cid": (dict(cid=20002), {}), "verdict": ({}, dict(verdict="bug")), "summary": ({}, dict(summary=" ")),
                 "evidence": ({}, dict(evidence=[])),
                 "evidence[0]": ({}, dict(evidence=[{"file": "a.c", "line": 0, "note": "x"}])),
                 "evidence[1]": ({}, dict(evidence=[{"file": "a.c", "line": 1, "note": "x"}, "a.c:3"])),
                 "recommendation": ({}, dict(recommendation="none")), "confidence": ({}, dict(confidence="certain")),
                 "fix:": ({}, dict(fix={"summary": ""})), "deviation:": ({}, dict(deviation=None))}
        for name, (args, over) in cases.items():
            self.assertIn(name, self.problems(*args.values(), **over), name)

    def test_fix_or_the_reason_why_there_is_none(self):
        self.assertIn("どちらか一方", self.problems(no_fix_reason="不要"))  # both
        self.assertIn("どちらか一方", self.problems(with_fix=False, no_fix_reason=""))  # neither
        self.assertIn("fix.summary", self.problems(with_fix=False, recommendation="fix"))

    def test_the_comment_matches_the_verdict(self):
        self.assertIn("「誤検知。」で始めてください", self.problems(verdict="false_positive"))
        self.assertIn("deviation.comment: 200 文字以内", self.problems(deviation={"comment": GOOD + "あ" * 200}))

    def test_values_for_coverity_follow_from_the_comment_and_the_impact(self):
        fix, keep = support.result(1), support.result(1, with_fix=False)
        fixed = {**keep, "verdict": "true_bug", "deviation": {"comment": GOOD}}  # a real bug without fix code
        for result, impact, expected in ((fix, "Medium", ("Bug", "Ignore", "Moderate")),
                                         (fix, None, ("Bug", "Ignore", "Unspecified")),
                                         (keep, "High", ("False Positive", "Ignore", "Unspecified")),
                                         (fixed, "Low", ("Bug", "Fix Submitted", "Minor"))):
            self.assertEqual(tuple(checks.coverity_fields(result, impact).values()), expected)

    def test_a_fix_keeps_line_ends_and_encoding(self):
        sjis, utf8 = "// 日本語\r\nint a;\r\n".encode("cp932"), "// 日本語\nint a;\n".encode("utf-8")
        self.assertEqual(checks.check_form("a.c", sjis, sjis + b"int b;\r\n"), [])
        self.assertEqual(checks.check_form("a.c", sjis, b"int a;\r\n"), [], "ASCII is left: the encoding is not told")
        self.assertEqual(checks.check_form("a.c", b"int a;\r\nint b;\n", b"int a;\nint b;\n"), [], "mixed before")
        both = "\n".join(checks.check_form("a.c", sjis, utf8))
        self.assertIn("a.c: 改行コードが変わっています（修正前: CRLF、修正後: LF）", both)
        self.assertIn("a.c: 文字コードが変わっています", both)
        self.assertEqual(len(checks.check_form("a.c", utf8, sjis.replace(b"\r\n", b"\n"))), 1)
        bom = b"\xef\xbb\xbf" + utf8
        self.assertEqual(checks.check_form("a.c", bom, utf8),
                         ["a.c: 先頭の BOM が削除されています。修正前と同じく、BOM を付けて保存してください"])
        self.assertEqual(checks.check_form("a.c", b"int a;\n", b"\xef\xbb\xbfint a;\n"),
                         ["a.c: 先頭の BOM が追加されています。修正前と同じく、BOM を付けずに保存してください"])
        self.assertEqual(checks.check_form("a.c", bom, bom + b"int b;\n"), [])


class ReviewTest(unittest.TestCase):

    def test_review_fields(self):
        self.assertEqual(checks.check_review("pass", [], None), [])
        self.assertEqual(checks.check_review("rework", ["src/a.c:3 NULL のときの戻り値が仕様と違う"], "medium"), [])
        self.assertIn("outcome", checks.check_review("ok", [], None)[0])
        self.assertIn("1件以上", checks.check_review("flag", [], None)[0])
        self.assertIn("ファイル:行", checks.check_review("flag", ["戻り値が違う"], None)[0])
        self.assertEqual(checks.check_review("flag", ["src/my dir/sp ace.c:3 NULL の比較を明示する"], None), [])
        self.assertIn("cap", checks.check_review("pass", [], "certain")[0])

    def test_shown_confidence_is_the_lower_one(self):
        self.assertEqual(checks.lower_confidence("high", "medium"), "medium")
        self.assertEqual(checks.lower_confidence("low", "high"), "low")
        self.assertEqual(checks.lower_confidence("medium", None), "medium")


if __name__ == "__main__":
    unittest.main()
