"""Unit tests of the deterministic pieces, imported from the skill's scripts (the shared contract cases run in
one separate process, started like run.py)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import SKILL, import_runlib, script_runner

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
    """The schemas this skill owns (run.json, approvals.json) are checked by runlib/schema.py."""

    def test_every_keyword_is_known(self) -> None:
        for name in ("run.schema.json", "approvals.schema.json"):
            self.assertEqual(schema.unsupported(schema.load(name)), set(), name)

    def test_approvals_schema(self) -> None:
        good = {"version": 1, "updated_at": "2026-11-08T10:00:00",
                "items": {"20001": {"decision": "fix", "edited": False, "updated_at": "2026-11-08T10:00:00"},
                          "G1": {"decision": None, "edited": True,
                                 "deviation": {"classification": "Intentional", "action": "Ignore",
                                               "severity": "Unspecified", "comment": "意図的。人が直した。"}}}}
        self.assertEqual(schema.validate(good, "approvals.schema.json"), [])
        self.assertTrue(schema.validate({"version": 1, "items": []}, "approvals.schema.json"))
        self.assertTrue(schema.validate({"version": 1, "items": {"1": {"decision": "maybe"}}}, "approvals.schema.json"))


COPIES = {"check_comment.py": "comment", "check_result.py": "result", "check_review.py": "review",
          "lint_result.py": "lint", "check_plan.py": "plan"}


class CopiedRulesTest(unittest.TestCase):
    """The rules of other parts are their scripts copied unchanged; runlib holds no rule of its own."""

    def test_copies_are_there_and_self_contained(self) -> None:
        for name in COPIES:
            path = SKILL / "scripts" / name
            self.assertTrue(path.is_file(), name)
            text = path.read_text(encoding="utf-8")
            self.assertIn("# /// script", text, name)
            self.assertNotIn("import runlib", text, name)
            self.assertNotIn("from runlib", text, name)

    def test_runlib_has_no_rule_of_its_own(self) -> None:
        source = (SKILL / "scripts" / "runlib" / "rules.py").read_text(encoding="utf-8")
        for word in ("CONDITION_WORDS", "REQUEST_WORDS", "COMMENT_MAX", "def check_comment", "evidence_missing_file"):
            self.assertNotIn(word, source)

    def test_runlib_loads_the_copies(self) -> None:
        for name in ("check_comment", "check_result", "check_review", "lint_result", "check_plan", "confidence"):
            module = rules.script(name)
            self.assertEqual(Path(module.__file__).resolve(), (SKILL / "scripts" / f"{name}.py").resolve())

    def test_applicability_follows_the_review_rules(self) -> None:
        standard = rules.applicability("NULL_RETURNS", False)
        self.assertEqual((standard["C-20"], standard["C-21"], standard["C-22"], standard["C-23"]), (False, True, False, False))
        misra = rules.applicability("MISRA C-2012 Rule 10.4", True)
        self.assertEqual((misra["C-20"], misra["C-21"], misra["C-22"]), (True, False, True))
        self.assertIsNone(rules.applicability(None, False)["C-21"])
        self.assertEqual(len(rules.item_criteria()), 20)


class ClientDocsTest(unittest.TestCase):
    """SKILL.md tells the agent to pass --client claude-code in Claude Code, with the read tools' names as it sees them."""

    def test_step_four_names_both_clients(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        step = text[text.index("\n4. "):text.index("\n5. ")]
        self.assertIn("Claude Code で動いているときは、`--client claude-code` を付け", step)
        self.assertIn("見えている名前のまま", step)
        self.assertIn("`mcp__<サーバー>__<ツール>`", step)
        self.assertEqual(step.count("書き戻すツールは渡しません"), 2)
        self.assertIn("`--client` と `--mcp-tool`", text)
        actions = (SKILL / "references" / "actions.md").read_text(encoding="utf-8")
        self.assertIn("--client copilot\\|claude-code", actions)
        resume = text[text.index("再開するなら"):text.index("\n3. ")]
        self.assertIn("`--client claude-code` を必ず付け", resume)

    def test_claude_code_waits_before_the_start_only(self) -> None:
        """In Claude Code the permissions are given before the agents start: the turn ends after the steps, unless
        the request says not to ask on the way or that the permissions were given at launch. Copilot CLI goes on."""
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        step = text[text.index("\n6. "):text.index("## 2. ")]
        self.assertIn("Claude Code で動いているときは、案内を示したら、そのターンを終えて待ちます", step)
        self.assertIn("「途中で質問せず」", step)
        self.assertIn("許可を起動のときに済ませた", step)
        self.assertIn("始めた後は質問しません", step)
        self.assertEqual(step.count("ターンを終えて"), 1, "only under the Claude Code condition")

    def test_cli_accepts_the_client(self) -> None:
        text = (SKILL / "scripts" / "run.py").read_text(encoding="utf-8")
        self.assertEqual(text.count('cmd.add_argument("--client", **client)'), 2)  # new-run and resume


class ContractCasesTest(unittest.TestCase):
    """Every case of the shared test data (design 4.17) through the code the run uses: scripts/runlib/rules.py with
    the copied scripts. They run in a separate process started like run.py (uv reads the libraries it needs)."""

    answers: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        names = [n for n in ("comment", "result", "lint", "plan", "confidence") if (CONTRACTS / n / "cases.json").is_file()]
        if not names:
            return
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        env.pop("VIRTUAL_ENV", None)
        proc = subprocess.run([*script_runner(SKILL / "tests" / "contract_runner.py"), *names], capture_output=True,
                              env=env, timeout=300, cwd=tempfile.gettempdir())
        if proc.returncode != 0:
            raise AssertionError(f"contract_runner failed: {proc.stderr.decode('utf-8', 'replace')}")
        cls.answers = json.loads(proc.stdout.decode("utf-8"))

    def answers_of(self, name: str) -> list[dict]:
        path = CONTRACTS / name / "cases.json"
        if not path.is_file():
            self.skipTest(f"{path.relative_to(SKILL)} がまだありません（持ち主の部品のものが写されたら動きます）")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data.get("version"), data.get("contract")), (1, name))
        self.assertGreaterEqual(len(data["cases"]), 8)
        self.assertEqual(len({c["name"] for c in data["cases"]}), len(data["cases"]))
        self.assertEqual(len(self.answers[name]), len(data["cases"]))
        return self.answers[name]

    def test_comment(self) -> None:
        for case in self.answers_of("comment"):
            with self.subTest(case["name"]):
                self.assertEqual(case["got"]["ok"], case["expect"]["ok"])
                self.assertEqual(case["got"]["inside_result"], case["expect"]["ok"])
                self.assertEqual(case["got"]["inside_plan"], case["expect"]["ok"])

    def test_result(self) -> None:
        for case in self.answers_of("result"):
            with self.subTest(case["name"]):
                self.assertEqual(case["got"]["ok"], case["expect"]["ok"])

    def test_lint(self) -> None:
        for case in self.answers_of("lint"):
            with self.subTest(case["name"]):
                self.assertEqual(case["got"]["points"], sorted(case["expect"]["points"]))

    def test_plan(self) -> None:
        for case in self.answers_of("plan"):
            with self.subTest(case["name"]):
                self.assertEqual(case["got"]["ok"], case["expect"]["ok"])

    def test_confidence(self) -> None:
        for case in self.answers_of("confidence"):
            with self.subTest(case["name"]):
                self.assertEqual(case["got"]["confidence"], case["expect"]["confidence"])


class ConfidenceTest(unittest.TestCase):
    """scripts/confidence.py: this skill owns the contract "confidence" (other parts copy the file)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = rules.script("confidence")

    def test_cases_file(self) -> None:
        data = json.loads((CONTRACTS / "confidence" / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual((data["version"], data["contract"]), (1, "confidence"))
        self.assertGreaterEqual(len(data["cases"]), 10)
        for case in data["cases"]:
            self.assertEqual(set(case["input"]) - {"run_item"}, {"result", "review", "verify"})
            self.assertIn(case["expect"]["confidence"], ("high", "medium", "low"))
            with self.subTest(case["name"]):
                got = self.m.effective_confidence(**case["input"])
                self.assertEqual(got["confidence"], case["expect"]["confidence"])

    def test_which_review_counts_is_the_rule_s_own(self) -> None:
        """Callers pass review.json as read and the item of run.json; they filter nothing themselves."""
        result = {"confidence": "high", "recommendation": "fix", "confidence_reason": "経路をすべて読んだ"}
        capped = {"outcome": "flag", "confidence_cap": "low", "note": "根拠が弱い"}
        for review in (None, "flag", ["flag"], {"outcome": "unverified", "confidence_cap": "low"},
                       {"outcome": ["flag"], "confidence_cap": "low"}, {"confidence_cap": "low"}):
            with self.subTest(review=review):
                self.assertEqual(self.m.effective_confidence(result, review)["confidence"], "high")
                self.assertIsNone(self.m.counted_review(review))
        for state in (None, "unverified", "", ["flag"], {"outcome": "flag"}):
            with self.subTest(state=state):
                self.assertEqual(self.m.effective_confidence(result, capped, None, {"id": "1", "review": state})["confidence"], "high")
        for run_item in ("flag", ["flag"], 1):  # not an item of run.json: the review was not taken in
            with self.subTest(run_item=run_item):
                self.assertIsNone(self.m.counted_review(capped, run_item))
        for state in ("pass", "flag", "rework"):
            with self.subTest(state=state):
                self.assertEqual(self.m.effective_confidence(result, capped, None, {"id": "1", "review": state})["confidence"], "low")
        self.assertEqual(self.m.effective_confidence(result, capped)["confidence"], "low")  # no run folder: by its outcome

    def test_unusual_values_never_raise(self) -> None:
        for result in (None, [], "x", {"confidence": ["high"]}, {"confidence": {"a": 1}, "recommendation": ["fix"]}):
            for review in (None, {"outcome": "flag", "confidence_cap": ["low"]}, {"outcome": "pass", "confidence_cap": {}},
                           {"outcome": "flag", "confidence_cap": "low", "note": {"x": 1}}):
                for verify in (None, [], {"build_ok": False, "problems": "x"}, {"problems": [None, 1, {"a": 1}]}):
                    with self.subTest(result=result, review=review, verify=verify):
                        got = self.m.effective_confidence(result, review, verify, {"review": ["pass"]})
                        self.assertEqual(got["confidence"], "low")
                        got = self.m.effective_confidence(result, review, verify)
                        self.assertIn(got["confidence"], ("high", "medium", "low"))
                        self.assertIsInstance(got["reason"], str)

    def test_stdlib_only_and_reads_no_file(self) -> None:
        text = (SKILL / "scripts" / "confidence.py").read_text(encoding="utf-8")
        self.assertIn("# dependencies = []", text)
        for word in ("open(", "read_text", "Path(", "import runlib", "from runlib"):
            self.assertNotIn(word, text)

    def test_reason_says_why_it_was_lowered(self) -> None:
        result = {"confidence": "high", "recommendation": "fix", "confidence_reason": "経路をすべて読んだ"}
        same = self.m.effective_confidence(result)
        self.assertEqual((same["confidence"], same["reason"], same["lowered_by"]), ("high", "経路をすべて読んだ", []))
        capped = self.m.effective_confidence(result, {"outcome": "flag", "confidence_cap": "medium", "note": "経路が一部だけ"})
        self.assertEqual(capped["confidence"], "medium")
        self.assertIn("経路が一部だけ", capped["reason"])
        built = self.m.effective_confidence(result, None, {"build_ok": False, "problems": ["ビルドに失敗しました"]})
        self.assertEqual(built["confidence"], "low")
        self.assertIn("ビルドでの検証で問題", built["reason"])
        self.assertTrue(built["reason"].startswith("経路をすべて読んだ（"))

    def test_command(self) -> None:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        script = SKILL / "scripts" / "confidence.py"
        command = [sys.executable, str(script)]  # stdlib only: any Python 3.10+ runs it
        given = {"result": {"confidence": "high", "recommendation": "fix"}, "review": {"outcome": "rework"}, "verify": None}
        proc = subprocess.run([*command, "--stdin"], input=json.dumps(given).encode("utf-8"), capture_output=True,
                              env=env, timeout=60, cwd=tempfile.gettempdir())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["confidence"], "low")
        given["run_item"] = {"id": "20001", "review": "unverified"}  # the run did not take the review in
        proc = subprocess.run([*command, "--stdin"], input=json.dumps(given).encode("utf-8"), capture_output=True,
                              env=env, timeout=60, cwd=tempfile.gettempdir())
        self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["confidence"], "high")
        proc = subprocess.run([*command, "--stdin"], input=b"[1]", capture_output=True, env=env, timeout=60,
                              cwd=tempfile.gettempdir())
        self.assertEqual(proc.returncode, 1)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(out["ok"])
        self.assertIn("result", out["error"])


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

    def test_ignored_build_trees_do_not_count(self) -> None:
        import shutil
        import subprocess

        from runlib import views

        if shutil.which("git") is None:
            self.skipTest("git がありません")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        (tmp / ".gitignore").write_text("build/\n", encoding="utf-8")
        (tmp / "main.c").write_text("int x;\n", encoding="utf-8")
        deep = tmp / "build" / ("d" * 120) / ("e" * 100)
        deep.mkdir(parents=True)
        (deep / "obj.o").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
        self.assertIsNone(views.long_path_problem(Path("C:/out/run/work"), tmp, vcs_type="git"))
        self.assertIsNotNone(views.long_path_problem(Path("C:/out/run/work"), tmp), "without the VCS, every file counts")


class ClaudeSkillRulesTest(unittest.TestCase):
    """The Skill and Read rules that Claude Code needs for the kit's skills (they ask before each use, and the
    skill folder of an installed plugin is outside the repository)."""

    KIT_LINE = 'allowed-tools: ["shell(uv run:*)"]\n'

    def make_skills(self, plugin: str | None) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        root = tmp / ("kit" if plugin else ".claude")
        skills = root / "skills"
        # "release" is a skill of the repository that allows other tools: it is never pre-approved
        for name, front in (("run-x", self.KIT_LINE), ("plain", ""), ("Bad_Name", self.KIT_LINE),
                            ("other-x", self.KIT_LINE),
                            ("release", "allowed-tools: Bash(uv run x.py:*), Bash(git push:*)\n"),
                            ("wider", 'allowed-tools: ["shell(uv run:*)", "shell(git:*)"]\n')):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(f"---\nname: {name}\n{front}---\nallowed-tools uv run\n",
                                                    encoding="utf-8")
        if plugin:
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin}), encoding="utf-8")
        return skills

    def test_plugin_allows_its_skills_and_reads_its_folder(self) -> None:
        from runlib import views

        skills = self.make_skills("kit-x")
        self.assertEqual(views.claude_skill_rules(skills, own="run-x"),
                         ["Skill(kit-x:*)", f"Read({views._rule_path(skills)}/**)"])

    def test_taken_out_skills_are_named_one_by_one(self) -> None:
        from runlib import views

        skills = self.make_skills(None)
        rules = views.claude_skill_rules(skills, own="run-x")
        self.assertEqual(rules, ["Skill(other-x)", "Skill(run-x)", f"Read({views._rule_path(skills)}/**)"],
                         "only the skills whose allowed-tools line is this skill's own, with a valid name")

    def test_a_skill_that_allows_other_tools_is_never_listed(self) -> None:
        from runlib import views

        skills = self.make_skills(None)
        (skills / "release" / "SKILL.md").write_text(
            "---\nname: release\nallowed-tools: Bash(uv run *), Bash(git push *), mcp__x__update_y\n---\n",
            encoding="utf-8")
        rules = views.claude_skill_rules(skills, own="run-x")
        for name in ("release", "wider", "plain", "Bad_Name"):
            self.assertNotIn(f"Skill({name})", rules)

    def test_own_line_unknown_names_only_this_skill(self) -> None:
        from runlib import views

        skills = self.make_skills(None)
        self.assertEqual(views.claude_skill_rules(skills, own="plain")[:-1], ["Skill(plain)"])
        self.assertEqual(views.claude_skill_rules(skills, own="gone-x")[:-1], [], "no folder: no rule")

    def test_odd_manifest_name_falls_back(self) -> None:
        from runlib import views

        skills = self.make_skills("Kit X")
        self.assertEqual(views.claude_skill_rules(skills, own="run-x")[:2], ["Skill(other-x)", "Skill(run-x)"])

    def test_rule_path(self) -> None:
        from runlib import views

        self.assertEqual(views._rule_path(Path("C:/Users/a b/kit/skills")), "//c/Users/a b/kit/skills")
        self.assertEqual(views._rule_path(Path("/home/u/kit/skills")), "//home/u/kit/skills")
        for odd in ("/home/u/$x/skills", '/home/u/"q"/skills', "/home/u/(x)/skills", "/home/u/*/skills",
                    "relative/skills"):
            self.assertIsNone(views._rule_path(Path(odd)), odd)

    def test_read_rule_falls_back_to_the_home_folder(self) -> None:
        from unittest import mock

        from runlib import views

        self.assertEqual(views._read_rule(Path("/home/u/kit/skills")), "Read(//home/u/kit/skills/**)")
        home = Path(tempfile.gettempdir()).resolve() / "a (x)"
        with mock.patch.object(Path, "home", return_value=home):
            self.assertEqual(views._read_rule(home / ".claude" / "plugins" / "cache" / "m" / "k" / "1" / "skills"),
                             "Read(~/.claude/plugins/cache/m/k/1/skills/**)")
            self.assertIsNone(views._read_rule(home), "never the whole home folder")
            self.assertIsNone(views._read_rule(home.parent / "(y)" / "skills"))

    def test_steps_without_rules_say_nothing_of_them(self) -> None:
        from runlib import views

        first, restart = views._claude_steps(Path("/o/out"), ["mcp__s__read_a"], skill_rules=[])
        self.assertNotIn("Skill(", first + restart)
        self.assertNotIn("Read", first + restart)
        self.assertIn('--allowedTools "Bash(uv run *)" "Edit(//o/out/**)" "mcp__s__read_a"', restart)

    def test_writes_only_in_the_run_folder(self) -> None:
        from runlib import views

        first, restart = views._claude_steps(Path("/o/out"), [], skill_rules=["Skill(kit-x:*)", "Read(//x/**)"])
        self.assertIn('"Read(//x/**)" "Edit(//o/out/**)" で起動し直します', restart)
        self.assertNotIn('"Write"', restart)
        self.assertIn("実行フォルダに書くこと（Edit(//o/out/**)。Write も含みます）", first)
        self.assertIn("ほかの場所（調べているリポジトリなど）に書くときは、確認が出ます", first)
        windows = views._claude_steps(Path("C:/Users/me/out"), [], skill_rules=[])[1]
        self.assertIn('"Edit(//c/Users/me/out/**)"', windows)
        # a run folder whose name cannot go into a rule: Write and Edit as they are, and the steps say so
        odd_first, odd = views._claude_steps(Path("/o/(x)"), [], skill_rules=[])
        self.assertIn('"Bash(uv run *)" "Write" "Edit" で起動し直します', odd)
        self.assertNotIn("Edit(", odd_first + odd)
        self.assertIn("書く場所を絞れません", odd_first)
        self.assertIn("このセッションのどのファイルにも効きます", odd_first)

    def test_wording_follows_the_rules_listed(self) -> None:
        from runlib import views

        only_skill = views._claude_steps(Path("/o/out"), [], skill_rules=["Skill(kit-x:*)"])[0]
        self.assertIn("スキルを使うこと（Skill(kit-x:*)）、", only_skill)
        self.assertIn("Skill を許可しないと、スキルを使うたびに確認が出ます。", only_skill)
        self.assertNotIn("参照資料を読むこと", only_skill)
        self.assertNotIn("Read は読むだけ", only_skill)
        self.assertIn("そのフォルダを読む許可（Read）を示していません", only_skill)
        self.assertIn("裏で動くエージェントは止まります", only_skill)
        only_read = views._claude_steps(Path("/o/out"), [], skill_rules=["Read(//x/**)"])[0]
        self.assertIn("スキルの参照資料を読むこと（Read(//x/**)）、", only_read)
        self.assertIn("Read を許可しないと、参照資料を読むたびに確認が出ます（Read は読むだけ", only_read)
        self.assertNotIn("Skill", only_read)
        both = views._claude_steps(Path("/o/out"), [], skill_rules=["Skill(kit-x:*)", "Read(//x/**)"])[0]
        self.assertIn("スキルを使うこととスキルの参照資料を読むこと（Skill(kit-x:*)、Read(//x/**)）", both)
        self.assertIn("Skill と Read を許可しないと", both)
        self.assertNotIn("示していません", both)


class LockTest(unittest.TestCase):
    """file_lock on Windows: creating a lock that another process is deleting is refused as access denied."""

    def flaky_open(self, failures: int):
        real_open = os.open
        calls = []

        def fake(path, flags, *args):
            calls.append(path)
            if len(calls) <= failures:
                raise PermissionError(13, "Access is denied", str(path))
            return real_open(path, flags, *args)
        return fake, calls

    def test_access_denied_on_windows_means_wait(self) -> None:
        from unittest import mock

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        fake, calls = self.flaky_open(2)
        with mock.patch.object(common.os, "open", fake), mock.patch.object(common.sys, "platform", "win32"):
            with common.file_lock(tmp / "vcs", timeout=5):
                self.assertTrue((tmp / "vcs.lock").exists())
        self.assertEqual(len(calls), 3)
        self.assertFalse((tmp / "vcs.lock").exists())

    def test_access_denied_elsewhere_is_an_error(self) -> None:
        from unittest import mock

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        fake, _ = self.flaky_open(1)
        with mock.patch.object(common.os, "open", fake), mock.patch.object(common.sys, "platform", "linux"):
            with self.assertRaises(PermissionError):
                with common.file_lock(tmp / "vcs", timeout=1):
                    pass


class LockHolderTest(unittest.TestCase):
    """A lock whose process has ended is taken over; a lock of a living process, or a fresh one, is not."""

    HOLDER = ("import sys, time\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
              "from runlib.common import file_lock\n"
              "with file_lock(Path(sys.argv[2]), timeout=5, stale=7200):\n    print('held', flush=True)\n    time.sleep(60)\n")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.lock = self.tmp / "vcs.lock"

    def age(self, path: Path, seconds: float) -> None:
        t = time.time() - seconds
        os.utime(path, (t, t))

    @staticmethod
    def dead_pid() -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def live(self) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def take(self, timeout: float = 10.0, stale: float = 7200) -> float:
        started = time.monotonic()
        with common.file_lock(self.tmp / "vcs", timeout=timeout, stale=stale):
            self.assertEqual(self.lock.read_text(), str(os.getpid()))
        self.assertFalse(self.lock.exists())
        return time.monotonic() - started

    def test_lock_of_an_ended_process_is_taken_over(self) -> None:
        self.lock.write_text(str(self.dead_pid()))
        self.age(self.lock, 10)
        self.assertLess(self.take(), 2)

    def test_fresh_lock_waits_for_the_grace(self) -> None:
        self.lock.write_text(str(self.dead_pid()))
        self.age(self.lock, common.LOCK_GRACE - 2)
        self.assertGreater(self.take(), 1)

    def test_empty_lock_after_the_grace_is_taken_over(self) -> None:
        self.lock.write_bytes(b"")
        self.age(self.lock, 10)
        self.assertLess(self.take(), 2)

    def test_lock_of_a_living_process_is_kept(self) -> None:
        proc = self.live()
        self.lock.write_text(str(proc.pid))
        self.age(self.lock, 10)
        with self.assertRaises(common.RunError):
            self.take(timeout=1)
        self.assertTrue(self.lock.exists())
        self.age(self.lock, 7300)  # older than the stale limit: taken all the same
        self.take()

    def test_own_lock_left_by_a_failed_release_is_free_but_a_held_one_is_not(self) -> None:
        self.lock.write_text(str(os.getpid()))
        self.age(self.lock, 10)
        self.take()
        with common.file_lock(self.tmp / "vcs", timeout=5):
            self.age(self.lock, 10)
            with self.assertRaises(common.RunError):  # not re-entrant
                with common.file_lock(self.tmp / "vcs", timeout=0.5, stale=7200):
                    pass
            self.assertTrue(self.lock.exists())
        self.assertFalse(self.lock.exists())

    def test_a_lock_taken_again_in_between_is_not_removed(self) -> None:
        self.lock.write_text(str(self.dead_pid()))
        seen = common._lock_seen(self.lock)
        self.lock.unlink()
        self.lock.write_text(str(os.getpid()))  # another waiter removed it and took it
        self.assertFalse(common._remove_if_same(self.lock, *seen))
        self.assertTrue(self.lock.exists())

    def test_holder_killed_by_stop(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", self.HOLDER, str(SKILL / "scripts"), str(self.tmp / "vcs")],
                                stdout=subprocess.PIPE, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), b"held")
        self.assertTrue(common.kill_tree(proc.pid, group=False))
        proc.wait()
        self.assertEqual(self.lock.read_text(), str(proc.pid))  # a killed process never runs its finally
        self.assertTrue(common.clear_dead_lock(self.tmp / "vcs"))
        self.assertFalse(self.lock.exists())

    def test_clear_dead_lock_keeps_a_living_holder(self) -> None:
        proc = self.live()
        self.lock.write_text(str(proc.pid))
        self.assertFalse(common.clear_dead_lock(self.tmp / "vcs"))
        self.assertTrue(self.lock.exists())


class WindowsLockTest(unittest.TestCase):
    """"Access denied" on Windows: wait only while a lock is there (being deleted); else it is a real error."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.lock = self.tmp / "vcs.lock"

    def test_access_denied_with_no_lock_is_raised_soon(self) -> None:
        from unittest import mock

        def denied(path, flags, *args):
            raise PermissionError(13, "Access is denied", str(path))
        started = time.monotonic()
        with mock.patch.object(common.os, "open", denied), mock.patch.object(common.sys, "platform", "win32"):
            with self.assertRaises(PermissionError):
                with common.file_lock(self.tmp / "vcs", timeout=30):
                    pass
        self.assertLess(time.monotonic() - started, common.DENIED_GRACE + 2)

    def test_access_denied_while_the_lock_is_there_waits(self) -> None:
        import threading
        from unittest import mock

        real_open = os.open
        self.lock.write_text("1")

        def pending(path, flags, *args):  # a lock being deleted: refused while its name is there
            if os.path.lexists(path):
                raise PermissionError(13, "Access is denied", str(path))
            return real_open(path, flags, *args)
        timer = threading.Timer(common.DENIED_GRACE + 1, self.lock.unlink)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with mock.patch.object(common.os, "open", pending), mock.patch.object(common.sys, "platform", "win32"):
            with common.file_lock(self.tmp / "vcs", timeout=30):
                pass
        self.assertGreater(time.monotonic() - started, common.DENIED_GRACE)

    def test_release_is_tried_again(self) -> None:
        from unittest import mock

        real_unlink = Path.unlink
        calls = []

        def busy(path, *args, **kwargs):  # a waiter reads the pid in the lock
            if path.name == "vcs.lock":
                calls.append(path)
                if len(calls) <= 2:
                    raise PermissionError(32, "The process cannot access the file", str(path))
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", busy), mock.patch.object(common.sys, "platform", "win32"):
            with common.file_lock(self.tmp / "vcs", timeout=5):
                pass
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.lock.exists())


class RemoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_read_only_files_are_removed(self) -> None:
        import stat
        from unittest import mock

        from support import windows_unlink

        tree = self.tmp / "w1"
        pristine = tree / ".svn" / "pristine" / "55"
        pristine.mkdir(parents=True)
        (tree / ".svn" / "wc.db").write_text("db")
        base = pristine / "5512.svn-base"
        base.write_text("x")
        base.chmod(stat.S_IREAD)
        with mock.patch.object(os, "unlink", windows_unlink()):
            __import__("shutil").rmtree(tree, ignore_errors=True)
            self.assertTrue(base.exists(), "the plain rmtree leaves the read-only file (as on Windows)")
            self.assertTrue(common.remove_tree(tree))
        self.assertFalse(tree.exists())
        self.assertTrue(common.remove_tree(tree))  # nothing there: fine

    def test_a_folder_that_stays_is_an_error(self) -> None:
        from unittest import mock

        tree = self.tmp / "held"
        tree.mkdir()
        (tree / "busy.obj").write_text("x")

        def held(path, *args, **kwargs):
            raise PermissionError(32, "The process cannot access the file", str(path))
        with mock.patch.object(os, "unlink", held):
            with self.assertRaises(common.RunError):
                common.remove_tree(tree)
            self.assertFalse(common.remove_tree(tree, check=False))

    @unittest.skipIf(sys.platform == "win32", "chmod changes only the read-only mark on Windows")
    def test_a_link_in_the_tree_leaves_its_target_as_it_is(self) -> None:
        import stat
        from unittest import mock

        outside = self.tmp / "repo" / "build.sh"
        outside.parent.mkdir()
        outside.write_text("#!/bin/sh\n")
        outside.chmod(0o755)
        tree = self.tmp / "w1"
        (tree / "tools").mkdir(parents=True)
        (tree / "tools" / "build.sh").symlink_to(outside)
        real = os.unlink
        calls = []

        def once(path, *args, **kwargs):  # the first delete of the link is refused (its folder is read-only)
            calls.append(path)
            if os.path.basename(str(path)) == "build.sh" and len(calls) == 1:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *args, **kwargs)
        with mock.patch.object(os, "unlink", once):
            self.assertTrue(common.remove_tree(tree))
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

    @unittest.skipIf(sys.platform == "win32", "chmod changes only the read-only mark on Windows")
    def test_only_the_tree_is_made_writable_and_only_the_write_bit_is_added(self) -> None:
        import stat
        from unittest import mock

        above = self.tmp / "work"
        tree = above / "w1"
        tree.mkdir(parents=True)
        above.chmod(0o755)
        busy = tree / "busy.obj"
        busy.write_text("x")
        busy.chmod(0o555)
        real = os.unlink

        def held(path, *args, **kwargs):
            if os.path.basename(str(path)) == "busy.obj":
                raise PermissionError(32, "The process cannot access the file", str(path))
            return real(path, *args, **kwargs)
        with mock.patch.object(os, "unlink", held):
            self.assertFalse(common.remove_tree(tree, check=False))  # rmdir of the tree fails: the folder above stays as it was
        self.assertEqual(stat.S_IMODE(above.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(busy.stat().st_mode), 0o755)

    def test_remove_file_is_tried_again_on_windows(self) -> None:
        from unittest import mock

        target = self.tmp / "progress.json"
        target.write_text("{}")
        real = os.unlink
        calls = []

        def busy(path, *args, **kwargs):
            calls.append(path)
            if len(calls) <= 2:
                raise PermissionError(32, "The process cannot access the file", str(path))
            return real(path, *args, **kwargs)
        with mock.patch.object(common.os, "unlink", busy), mock.patch.object(common.sys, "platform", "win32"):
            common.remove_file(target)
            common.remove_file(target)  # missing: nothing to do
        self.assertFalse(target.exists())
        target.write_text("{}")
        calls.clear()
        with mock.patch.object(common.os, "unlink", busy), mock.patch.object(common.sys, "platform", "linux"):
            with self.assertRaises(PermissionError):
                common.remove_file(target)


@unittest.skipUnless(__import__("shutil").which("git"), "git がないため飛ばします")
class GitCopyTest(unittest.TestCase):
    def test_broken_work_copy_is_made_again(self) -> None:
        from support import base_env, git

        from runlib.vcs import GitVcs

        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        env = base_env(tmp)
        repo = tmp / "repo"
        repo.mkdir()
        (repo / "a.c").write_text("int a;\n", encoding="utf-8")
        git(repo, "init", "-q", "-b", "main", env=env)
        git(repo, "add", "-A", env=env)
        git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init", env=env)
        from unittest import mock

        with mock.patch.dict(os.environ, env):
            vcs = GitVcs(repo, {"type": "git", "base_branch": "main"})
            rev = vcs.current_revision()
            dest = tmp / "work" / "w1"
            vcs.reset_copy(dest, rev)
            self.assertTrue(vcs.usable(dest))
            # git on Windows marks .git hidden, and a hidden file cannot be overwritten: replace it
            (dest / ".git").unlink()
            (dest / ".git").write_text("gitdir: " + str(tmp / "gone") + "\n", encoding="utf-8")
            self.assertFalse(vcs.usable(dest))
            vcs.reset_copy(dest, rev)
            self.assertTrue(vcs.usable(dest))
            self.assertTrue((dest / "a.c").is_file())


if __name__ == "__main__":
    unittest.main()
