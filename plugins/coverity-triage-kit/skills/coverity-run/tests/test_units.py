"""Unit tests of the deterministic pieces, imported from the skill's scripts (no subprocess)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from support import SKILL, import_runlib, result_json, review_json

import_runlib()

from runlib import common, config, grouping, pathmap, rules, schema  # noqa: E402

CONTRACTS = SKILL / "assets" / "contract"


class GroupingTest(unittest.TestCase):
    def issue(self, cid, checker="C", file="a.c", function=None, line=None, macro=None):
        return {"cid": cid, "checker": checker, "file": file, "function": function, "line": line, "macro": macro}

    def test_keys(self) -> None:
        issues = [self.issue(1, function="f", line=1), self.issue(2, function="f", line=9),  # same function
                  self.issue(3, function="g", line=5), self.issue(4, function="h", line=5),  # same line
                  self.issue(5, macro="M", function="x", file="b.c"), self.issue(6, macro="M", function="y", file="c.c"),
                  self.issue(7, checker="OTHER", function="f", line=1), self.issue(8, function="z", line=99)]
        items = grouping.build_work_items(issues)
        self.assertEqual([(i["id"], i["cids"]) for i in items],
                         [("G1", [1, 2]), ("G2", [3, 4]), ("G3", [5, 6]), ("7", [7]), ("8", [8])])

    def test_no_grouping_and_size_limit(self) -> None:
        issues = [self.issue(c, function="f") for c in range(1, 24)]
        items = grouping.build_work_items(issues, no_grouping={23})
        self.assertEqual([len(i["cids"]) for i in items], [10, 10, 2, 1])
        self.assertEqual([i["id"] for i in items], ["G1", "G2", "G3", "23"])
        self.assertEqual(len({i["cluster"] for i in items[:3]}), 1)


class PathMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        for rel in ("src/io/reader.c", "lib/util.c", "a/x.c", "b/x.c"):
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / rel).write_text("x", encoding="utf-8")

    def test_methods(self) -> None:
        mapper = pathmap.PathMapper(self.tmp, ["/build/ws", "C:\\jenkins\\job"])
        self.assertEqual(mapper.map("src/io/reader.c"), ("src/io/reader.c", "as_is"))
        self.assertEqual(mapper.map("/build/ws/src/io/reader.c"), ("src/io/reader.c", "prefix"))
        self.assertEqual(mapper.map("C:\\Jenkins\\job\\lib\\util.c"), ("lib/util.c", "prefix"))
        self.assertEqual(mapper.map("/other/root/io/reader.c"), ("src/io/reader.c", "suffix"))
        self.assertEqual(mapper.map("/other/x.c")[1], "unmapped")  # two files share the longest suffix
        notes: list[str] = []
        mapper.mapped("/other/x.c", notes)
        mapper.mapped("/zzz/io/reader.c", notes)
        self.assertEqual(len(notes), 2)


class EncodingTest(unittest.TestCase):
    class Pipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    def test_console_code_page_on_windows(self) -> None:
        saved = os.environ.pop("PYTHONIOENCODING", None)
        try:
            self.assertEqual(common.output_encoding(self.Pipe(), "win32", 932), "cp932")
            self.assertEqual(common.output_encoding(self.Pipe(), "win32", 65001), "cp65001")
            self.assertEqual(common.output_encoding(self.Pipe(), "linux", 932), "utf-8")
            os.environ["PYTHONIOENCODING"] = "cp1252:backslashreplace"
            self.assertEqual(common.output_encoding(self.Pipe(), "win32", 932), "cp1252")
        finally:
            os.environ.pop("PYTHONIOENCODING", None)
            if saved is not None:
                os.environ["PYTHONIOENCODING"] = saved

    def test_only_unencodable_characters_are_escaped(self) -> None:
        text = json.dumps({"a": "日本語 ①", "b": "😀"}, ensure_ascii=False)
        out = common.escape_unencodable(text, "cp932")
        self.assertIn("日本語", out)
        self.assertIn("\\ud83d\\ude00", out)
        self.assertEqual(json.loads(out), {"a": "日本語 ①", "b": "😀"})
        latin = common.escape_unencodable(text, "cp1252")
        self.assertEqual(json.loads(latin), {"a": "日本語 ①", "b": "😀"})
        latin.encode("cp1252")

    def test_emit_json_writes_bytes_in_the_encoding(self) -> None:
        class Stream:
            def __init__(self):
                self.buffer = io.BytesIO()

            def isatty(self):
                return False

            def flush(self):
                pass

        stream = Stream()
        saved = os.environ.get("PYTHONIOENCODING")
        os.environ["PYTHONIOENCODING"] = "cp932"
        try:
            common.emit_json({"error": "設定がありません 😀"}, stream)
        finally:
            if saved is None:
                os.environ.pop("PYTHONIOENCODING", None)
            else:
                os.environ["PYTHONIOENCODING"] = saved
        self.assertEqual(json.loads(stream.buffer.getvalue().decode("cp932")), {"error": "設定がありません 😀"})


class SchemaTest(unittest.TestCase):
    def test_every_keyword_is_known(self) -> None:
        for name in ("result.schema.json", "review.schema.json", "plan.schema.json", "run.schema.json",
                     "approvals.schema.json"):
            self.assertEqual(schema.unsupported(schema.load(name)), set(), name)

    def test_result_schema(self) -> None:
        good = result_json("20001")
        self.assertEqual(schema.validate(good, "result.schema.json"), [])
        bad = dict(good, extra=1, confidence="sure")
        bad["deviation"] = dict(good["deviation"], comment=" 誤検知。前に空白。")
        errors = schema.validate(bad, "result.schema.json")
        self.assertTrue(any("extra は使えない項目" in e for e in errors), errors)
        self.assertTrue(any(e.startswith("confidence は次のどれか") for e in errors), errors)
        self.assertTrue(any(e.startswith("deviation.comment の形") for e in errors), errors)
        trailing = dict(good, deviation=dict(good["deviation"], comment="誤検知。末尾に改行。\n"))
        self.assertTrue(schema.validate(trailing, "result.schema.json"))
        long = dict(good, deviation=dict(good["deviation"], comment="誤" * 201))
        self.assertTrue(any("200 文字以内" in e for e in schema.validate(long, "result.schema.json")))

    def test_review_schema_rework_needs_items(self) -> None:
        review = review_json("1", "rework")
        self.assertEqual(schema.validate(review, "review.schema.json"), [])
        review["rework"] = []
        self.assertTrue(schema.validate(review, "review.schema.json"))


class RuleTest(unittest.TestCase):
    def test_comment_rules(self) -> None:
        ok = "誤検知。get_buf() の戻り値は、呼び出し元 read_all()（src/io/reader.c:120）で NULL を除いてから渡している。"
        self.assertEqual(rules.comment_problems(ok), [])
        self.assertEqual(rules.comment_problems("本物の不具合。この機能は今の製品では無効（config.h:12）で、実行されない。"), [])
        cases = {"x" * 201: "too_long", "これは誤検知です。": "no_category", " 誤検知。前に空白。": "surrounding_space",
                 "誤検知。入力が正の場合に限り問題ない。": "condition", "誤検知。修正を推奨します。": "request",
                 "誤検知。一。二。三。": "too_many_sentences", "誤検知。\n- 箇条書き": "markdown", "": "empty"}
        for text, code in cases.items():
            self.assertIn(code, [p["code"] for p in rules.comment_problems(text)], text)
        self.assertEqual(rules.comment_problems("誤検知。推奨範囲の値だけが渡る（a.c:1）。"), [])

    def test_result_rules(self) -> None:
        self.assertTrue(rules.result_check(result_json("20001"), "20001", [20001])["ok"])
        self.assertFalse(rules.result_check(result_json("20001"), "20002", [20002])["ok"])
        group = result_json("G1", excluded=[2])
        self.assertTrue(rules.result_check(group, "G1", [1, 2])["ok"])
        self.assertFalse(rules.result_check(result_json("G1", excluded=[1, 2]), "G1", [1, 2])["ok"])
        self.assertFalse(rules.result_check(result_json("G1", excluded=[3]), "G1", [1, 2])["ok"])
        self.assertFalse(rules.result_check(result_json("5", excluded=[5]), "5", [5])["ok"])
        odd = result_json("1", "deviation")
        odd["deviation"]["classification"] = "Bug"
        checked = rules.result_check(odd, "1", [1])
        self.assertTrue(checked["ok"])
        self.assertTrue(checked["warnings"])

    def test_lint_points(self) -> None:
        files = {"src/a.c": "1\n2\n3\n"}
        result = result_json("1", "deviation")
        result["verdict"]["judgement"] = "true_bug"
        result["verdict"]["evidence"] = [{"file": "src/a.c", "line": 9, "note": ""}, {"file": "src/none.c", "line": 1}]
        result["revision_drift"] = {"status": "detected", "detail": ""}
        codes = [p["code"] for p in rules.lint_points(result, files.get)]
        self.assertEqual(codes, ["drift_high", "evidence_line_out_of_range", "evidence_missing_file", "true_bug_deviation"])
        empty = result_json("1")
        empty["verdict"]["evidence"] = []
        empty["verdict"]["judgement"] = "undetermined"
        self.assertEqual([p["code"] for p in rules.lint_points(empty, files.get)], ["no_evidence", "undetermined_high"])
        fp_fix = result_json("1")
        fp_fix["verdict"]["judgement"] = "intentional"
        fp_fix["verdict"]["evidence"] = [{"file": "src/a.c", "line": 3}]
        self.assertEqual([p["code"] for p in rules.lint_points(fp_fix, files.get)], ["not_bug_fix"])

    def test_plan_rules(self) -> None:
        merged = result_json("x")
        merged.pop("item")
        items = {"1": {"cids": [1], "status": "done", "confidence": "medium", "no_grouping": False},
                 "2": {"cids": [2], "status": "done", "confidence": "high", "no_grouping": False},
                 "3": {"cids": [3], "status": "pending", "confidence": None, "no_grouping": False},
                 "4": {"cids": [4], "status": "done", "confidence": "high", "no_grouping": True}}
        plan = {"merges": [{"members": ["1", "2"], "reason": "同じ原因", "fix_source": "item:1",
                            "result": dict(merged, confidence="medium")}], "conflicts": []}
        self.assertTrue(rules.plan_check(plan, items)["ok"], rules.plan_check(plan, items))
        for change in ({"confidence": "high"},):
            bad = json.loads(json.dumps(plan))
            bad["merges"][0]["result"].update(change)
            self.assertFalse(rules.plan_check(bad, items)["ok"])
        for members in (["1"], ["1", "3"], ["1", "4"]):
            bad = json.loads(json.dumps(plan))
            bad["merges"][0]["members"] = members
            bad["merges"][0]["fix_source"] = "combine"
            self.assertFalse(rules.plan_check(bad, items)["ok"], members)
        twice = json.loads(json.dumps(plan))
        twice["merges"].append(dict(twice["merges"][0], fix_source="rework"))
        self.assertFalse(rules.plan_check(twice, items)["ok"])
        wrong = json.loads(json.dumps(plan))
        wrong["merges"][0]["fix_source"] = "item:9"
        self.assertFalse(rules.plan_check(wrong, items)["ok"])
        big = {str(n): {"cids": list(range(n * 10, n * 10 + 6)), "status": "done", "confidence": "low", "no_grouping": False}
               for n in (5, 6)}
        large = {"merges": [{"members": ["5", "6"], "reason": "r", "fix_source": "combine", "result": dict(merged, confidence="low")}]}
        self.assertFalse(rules.plan_check(large, big)["ok"])

    def test_review_rules(self) -> None:
        self.assertTrue(rules.review_check(review_json("1"), "1")["ok"])
        missing = review_json("1")
        missing["findings"] = [f for f in missing["findings"] if f["check"] != "C-01"]
        self.assertIn("C-01", " ".join(rules.review_check(missing, "1")["problems"]))
        optional = review_json("1")
        optional["findings"] = [f for f in optional["findings"] if f["check"] != "C-20"]
        self.assertTrue(rules.review_check(optional, "1")["ok"])  # a criterion with applies_when may be left out
        flag = review_json("1", "flag", confidence_cap=None)
        self.assertFalse(rules.review_check(flag, "1")["ok"])
        twice = review_json("1")
        twice["findings"].append(twice["findings"][0])
        self.assertFalse(rules.review_check(twice, "1")["ok"])


class ContractCasesTest(unittest.TestCase):
    """The shared test data of the contracts this skill holds copies of (design 4.17)."""

    def cases(self, name: str) -> list[dict]:
        path = CONTRACTS / name / "cases.json"
        if not path.is_file():
            self.skipTest(f"{path.relative_to(SKILL)} がまだありません（持ち主の部品のものが写されたら動きます）")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("contract"), name)
        self.assertGreater(len(data["cases"]), 0)
        return data["cases"]

    def test_comment(self) -> None:
        for case in self.cases("comment"):
            with self.subTest(case["name"]):
                self.assertEqual(not rules.comment_problems(case["input"]["comment"]), case["expect"]["ok"])

    def test_result(self) -> None:
        for case in self.cases("result"):
            with self.subTest(case["name"]):
                given = case["input"]
                checked = rules.result_check(given["result"], given.get("item"), given.get("cids"))
                self.assertEqual(checked["ok"], case["expect"]["ok"], checked["problems"])

    def test_lint(self) -> None:
        for case in self.cases("lint"):
            with self.subTest(case["name"]):
                files = case["input"].get("files") or {}
                points = rules.lint_points(case["input"]["result"], files.get)
                self.assertEqual(sorted(p["code"] for p in points), sorted(case["expect"]["points"]))

    def test_plan(self) -> None:
        for case in self.cases("plan"):
            with self.subTest(case["name"]):
                checked = rules.plan_check(case["input"]["plan"], case["input"]["items"])
                self.assertEqual(checked["ok"], case["expect"]["ok"], checked["problems"])


class ConfigTest(unittest.TestCase):
    def test_defaults_unknown_keys_and_empty_values(self) -> None:
        values, problems = config.parse_config({"coverity": {"url": "https://c.example/", "new_key": 1},
                                                "vcs": {"base_branch": None}, "output_dir": "", "future": {"x": 1}})
        self.assertEqual(problems, [])
        self.assertEqual(values["coverity"]["url"], "https://c.example")
        self.assertEqual(values["output_dir"], config.DEFAULT_OUTPUT_DIR)
        self.assertEqual(values["parallel"], 3)
        self.assertEqual(values["agents"]["max_rework"], 1)
        self.assertEqual(values["verify"]["cov_analyze_args"], "--dir idir --all")

    def test_problems(self) -> None:
        _, problems = config.parse_config({"coverity": {"user_env": "A", "key_env": "A"}, "verify": {"default": "all"},
                                           "options": {"metrics": "yes"}})
        self.assertIn("config.yaml の coverity.url: 入力してください", problems)
        self.assertIn("config.yaml の verify.default: 次のどれかにしてください: none / build / build+analyze", problems)
        self.assertIn("config.yaml の options.metrics: true か false にしてください", problems)
        self.assertTrue(any("違う名前" in p for p in problems))

    def test_filter_overrides(self) -> None:
        spec, problems = config.parse_filter({"streams": ["s"], "impacts": ["High"], "triage": {"status": ["new"]}},
                                             "f.yaml", {"impacts": ["medium"], "classification": ["Bug"]})
        self.assertEqual(problems, [])
        self.assertEqual(spec["impacts"], ["Medium"])
        self.assertEqual(spec["triage"], {"classification": ["Bug"], "action": [], "status": ["New"]})


class LongPathTest(unittest.TestCase):
    def test_long_path_problem(self) -> None:
        from runlib import views

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        (tmp / ("d" * 50)).mkdir()
        (tmp / ("d" * 50) / "f.c").write_text("", encoding="utf-8")
        self.assertIsNone(views.long_path_problem(Path("C:/out/run/work"), tmp))
        self.assertIn("output_dir", views.long_path_problem(Path("C:/" + "o" * 200), tmp))


if __name__ == "__main__":
    unittest.main()
