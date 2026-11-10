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
        self.assertIn("のいずれかで始めてください", self.problems("src/a.c:3 は問題ない。"))
        self.assertIn("問題にならない理由", self.problems("誤検知。"))

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

    def test_not_an_object(self):
        self.assertIn("オブジェクト", "\n".join(checks.check_result([1], 20001)))

    def test_every_field_is_checked(self):
        cases = {"cid": (dict(cid=20002), {}), "verdict": ({}, dict(verdict="bug")), "summary": ({}, dict(summary=" ")),
                 "evidence": ({}, dict(evidence=[])), "evidence[0]": ({}, dict(evidence=[{"file": "a.c", "line": 0, "note": "x"}])),
                 "evidence[1]": ({}, dict(evidence=[{"file": "a.c", "line": 1, "note": "x"}, "a.c:3"])),
                 "recommendation": ({}, dict(recommendation="none")), "confidence": ({}, dict(confidence="certain")),
                 "fix:": ({}, dict(fix={"summary": ""})), "deviation:": ({}, dict(deviation=None))}
        for name, (args, over) in cases.items():
            self.assertIn(name, self.problems(*args.values(), **over), name)

    def test_fix_or_the_reason_why_there_is_none(self):
        self.assertIn("どちらか一方", self.problems(no_fix_reason="不要"))  # both
        self.assertIn("どちらか一方", self.problems(with_fix=False, no_fix_reason=""))  # neither
        self.assertIn("fix.summary", self.problems(with_fix=False, recommendation="fix"))

    def test_deviation_matches_the_verdict_and_coverity_values(self):
        deviation = support.result(1)["deviation"]
        self.assertIn("「誤検知。」で始めてください", self.problems(verdict="false_positive"))
        self.assertIn("deviation.classification: 「本物の不具合。」", self.problems(deviation={**deviation, "classification": "Intentional"}))
        self.assertIn("deviation.action", self.problems(deviation={**deviation, "action": "Later"}))
        self.assertIn("deviation.severity", self.problems(deviation={**deviation, "severity": "High"}))
        self.assertIn("deviation.comment: 200 文字以内", self.problems(deviation={**deviation, "comment": GOOD + "あ" * 200}))


class ReviewTest(unittest.TestCase):

    def test_review_fields(self):
        self.assertEqual(checks.check_review("pass", [], None), [])
        self.assertEqual(checks.check_review("rework", ["src/a.c:3 NULL のときの戻り値が仕様と違う"], "medium"), [])
        self.assertIn("outcome", checks.check_review("ok", [], None)[0])
        self.assertIn("1件以上", checks.check_review("flag", [], None)[0])
        self.assertIn("ファイル:行", checks.check_review("flag", ["戻り値が違う"], None)[0])
        self.assertIn("cap", checks.check_review("pass", [], "certain")[0])

    def test_shown_confidence_is_the_lower_one(self):
        self.assertEqual(checks.lower_confidence("high", "medium"), "medium")
        self.assertEqual(checks.lower_confidence("low", "high"), "low")
        self.assertEqual(checks.lower_confidence("medium", None), "medium")


if __name__ == "__main__":
    unittest.main()
