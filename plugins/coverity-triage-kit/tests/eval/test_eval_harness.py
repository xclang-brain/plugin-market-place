"""Tests for the EVAL harnesses (no model is called): evalkit, run_mcp_eval, stage2, stage3, stage4.

Run from the plugin folder:  python3 -m unittest discover -s tests/eval -p "test_*.py"
Stand-ins for claude are small Python scripts. Tests that need git, uv or the plugin's parts skip
with a reason when those are not there (e.g. when only tests/eval is copied somewhere else).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evalkit as ek  # noqa: E402

ek.VERBOSE = False
ek.ADAPTER.VERBOSE = False
mcp_eval = ek.load_module("run_mcp_eval_under_test", HERE / "run_mcp_eval.py")
stage2 = ek.load_module("run_stage2_under_test", HERE / "stage2" / "run_stage2.py")
stage3 = ek.load_module("run_stage3_under_test", HERE / "stage3" / "run_stage3.py")
stage4 = ek.load_module("run_stage4_under_test", HERE / "stage4" / "run_stage4.py")

ROOT = ek.PLUGIN_ROOT
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
ENV.pop("CLAUDECODE", None)
HAS_GIT = shutil.which("git") is not None
HAS_UV = shutil.which("uv") is not None
HAS_PARTS = bool(ek.current_parts(ROOT)) and (ROOT / "mcp.json").is_file()
HAS_CLAUDE_FORM = (ROOT / ".claude-plugin" / "plugin.json").is_file() and (ROOT / ".mcp.json").is_file()
PLUGIN = ek.ADAPTER.FORMAT_PLUGIN


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def event_init(tools=None, skills=None):
    return {"type": "system", "subtype": "init", "tools": tools or ["Read"], "skills": skills or [], "agents": [],
            "mcp_servers": [{"name": "srv", "status": "connected"}], "model": "m", "permissionMode": "default"}


def event_tool(tid, name, data, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": data}]}}


def event_result_of(tid, text, is_error=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text,
                                                     "is_error": is_error}]}}


def event_text(text):
    return {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text": text}]}}


def event_final(text, cost=0.01):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": cost,
            "num_turns": 3, "duration_ms": 1000, "usage": {}, "permission_denials": []}


def fake_run(events, seconds=1.0, timed_out=False, code=0):
    return {"exit_code": code, "timed_out": timed_out, "seconds": seconds, "events": events, "transcript": "t.jsonl"}


def fake_claude_script(base: Path, body: str) -> list[str]:
    """A stand-in for claude: prints stream-json lines (body is Python using argv, prompt, emit)."""
    header = textwrap.dedent("""
        import json, sys
        sys.stdout.reconfigure(encoding="utf-8")
        argv = sys.argv[1:]
        prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
        def emit(obj):
            print(json.dumps(obj, ensure_ascii=False), flush=True)
        emit({"type": "system", "subtype": "init", "tools": [], "skills": [], "agents": [], "mcp_servers": [],
              "model": "fake", "permissionMode": "default"})
        """)
    script = base / "fake_claude.py"
    script.write_text(header + textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


class EvalkitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()  # a long path on Windows, like the code returns

    def tearDown(self):
        self.tmp.cleanup()

    def make_doc(self) -> Path:
        root = self.base / "plugin"
        write(root / "docs" / "replace.md", """
            # 部品を変える

            ## 今の部品

            | 役割 | 段階 | 部品 | 置き場所 |
            | --- | --- | --- | --- |
            | 調べ | 1 | `probe` | `skills/probe/` |
            | ない | 1 | `ghost` | `skills/ghost/` |

            ## ほか
            """)
        (root / "skills" / "probe").mkdir(parents=True)
        return root

    def test_parts_by_role(self):
        root = self.make_doc()
        rows = ek.current_parts(root)
        self.assertEqual([r["name"] for r in rows], ["probe", "ghost"])
        got = ek.part("調べ", root)
        self.assertEqual(got["name"], "probe")
        self.assertEqual(got["path"], (root / "skills" / "probe").resolve())
        with self.assertRaises(ek.EvalError):
            ek.part("ない", root)  # listed, but the folder is missing
        with self.assertRaises(ek.EvalError) as cm:
            ek.part("どこにもない", root)
        self.assertTrue(cm.exception.problems)
        with self.assertRaises(ek.EvalError):
            ek.part("調べ", self.base / "nothing")

    @unittest.skipUnless(HAS_PARTS, "プラグインの部品の表がない（tests/eval だけをコピーしたとき）")
    def test_real_parts_table_has_the_roles_the_harnesses_use(self):
        roles = {r["role"] for r in ek.current_parts(ROOT)}
        for role in ("作業役", "統括", "検証役", "統合役", "自己診断", "一括実行", "画面の入口", "困ったとき",
                     "逸脱コメント", "結果の記録"):
            self.assertIn(role, roles)

    def test_out_dir_outside_the_plugin(self):
        with self.assertRaises(ek.EvalError):
            ek.prepare_out(str(ROOT / "tests" / "eval" / "inside-out"), "x", ROOT)
        self.assertFalse((ROOT / "tests" / "eval" / "inside-out").exists())
        out = ek.prepare_out(str(self.base / "out"), "x", ROOT)
        self.assertTrue(out.is_dir())
        self.assertIn(f"{ROOT.name}-evals", str(ek.default_out("mcp")))

    def test_summarize_a_stream(self):
        events = [
            event_init(["Read", "Skill"]),
            event_text("考えています"),
            event_tool("t1", "Skill", {"skill": "probe-skill", "args": "x"}),
            event_result_of("t1", "Launching skill: probe-skill"),
            event_tool("t2", "mcp__srv__do_it", {"a": 1}),
            event_result_of("t2", "bad input", is_error=True),
            event_tool("t2", "mcp__srv__do_it", {"a": 1}),  # the same block repeated in another event
            event_tool("t3", "Agent", {"subagent_type": "helper-a", "prompt": "p"}),
            event_tool("t4", "Read", {"file_path": "/x"}, parent="t3"),
            {"type": "user", "message": {"content": "<command-name>/run-steps</command-name> High"}},
            {"type": "user", "message": {"content": [{"type": "text", "text": "Base directory for this skill: /w/.claude/skills/notes-skill"}]}},
            event_final("<summary>s</summary><response>42</response>"),
        ]
        s = ek.summarize(events)
        self.assertEqual(s["init"]["tools"], ["Read", "Skill"])
        self.assertEqual(s["main_tool_counts"], {"Skill": 1, "mcp__srv__do_it": 1, "Agent": 1})
        self.assertEqual(s["main_tool_calls"], 3)
        self.assertEqual([e["name"] for e in s["tool_errors"]], ["mcp__srv__do_it"])
        self.assertEqual(s["agents_called"], ["helper-a"])
        self.assertEqual(s["skills_loaded"], ["run-steps", "notes-skill", "probe-skill"])
        self.assertEqual(ek.tag_content(ek.final_text(s), "response"), "42")
        self.assertEqual(s["cost_usd"], 0.01)
        self.assertTrue(s["has_result"])
        self.assertEqual(ek.final_text(ek.summarize([event_text("a"), event_text("b")])), "a\nb")

    def test_skills_from_slash_commands_and_paths(self):
        init = dict(event_init(), slash_commands=["batch", "entry"])
        bash = event_tool("b", "Bash", {"command": 'uv run "/w/.claude/skills/batch/scripts/run.py" runs'})
        self.assertEqual(ek.summarize([init, event_final("x")], "/batch High だけ")["skills_loaded"], ["batch"])
        self.assertEqual(ek.summarize([init, event_final("x")], "/unknown High")["skills_loaded"], [])
        self.assertEqual(ek.summarize([init, event_final("x")], "batch High")["skills_loaded"], [])
        self.assertEqual(ek.summarize([init, bash, event_final("x")])["skills_loaded"], ["batch"])
        win = event_tool("w", "Read", {"file_path": "C:\\w\\.claude\\skills\\entry\\SKILL.md"})
        self.assertEqual(ek.summarize([init, win])["skills_loaded"], ["entry"])

    def test_skills_from_a_copied_plugin(self):
        root = self.base / "made" / "skills"
        uses = [event_tool("r", "Read", {"file_path": str(root / "probe-skill" / "SKILL.md")}),
                event_tool("b", "Bash", {"command": f'uv run "{root}/notes-skill/scripts/x.py" check'}),
                event_tool("s", "Skill", {"skill": "fake-kit:run-steps"}),
                event_tool("o", "Read", {"file_path": str(self.base / "repo" / "skills" / "other" / "a.c")})]
        got = ek.summarize([event_init()] + uses, None, [root])["skills_loaded"]
        self.assertEqual(sorted(got), ["notes-skill", "probe-skill", "run-steps"],
                         "paths under the copied skills folder count; other skills/ folders do not")
        self.assertEqual(ek.summarize([event_init()] + uses)["skills_loaded"], ["run-steps"], "no roots: only the Skill tool")
        win = event_tool("w", "Read", {"file_path": "C:\\made\\skills\\entry\\SKILL.md"})
        self.assertEqual(ek.summarize([win], None, [Path("C:/made/skills")])["skills_loaded"], ["entry"])

    def test_tag_content_takes_the_last(self):
        self.assertEqual(ek.tag_content("<response>1</response> x <response> 2 </response>", "response"), "2")
        self.assertIsNone(ek.tag_content("none", "response"))

    def test_run_claude_saves_the_transcript_and_kills_on_timeout(self):
        claude = fake_claude_script(self.base, """
            emit({"type": "result", "result": "done " + prompt, "is_error": False})
            if "slow" in prompt:
                import time
                time.sleep(60)
            """)
        res = ek.run_claude(claude + ["-p", "早い"], self.base, 60, self.base / "t" / "fast.jsonl")
        self.assertEqual(res["exit_code"], 0)
        self.assertFalse(res["timed_out"])
        self.assertEqual(ek.summarize(res["events"])["result_text"], "done 早い")
        self.assertEqual(len(ek.read_events(self.base / "t" / "fast.jsonl")), 2)
        res = ek.run_claude(claude + ["-p", "slow"], self.base, 3, self.base / "t" / "slow.jsonl")
        self.assertTrue(res["timed_out"])
        self.assertLess(res["seconds"], 30)
        self.assertEqual(len(res["events"]), 2, "lines written before the timeout are kept")

    def test_claude_env_drops_the_nesting_marker(self):
        os.environ["CLAUDECODE"] = "1"
        try:
            self.assertNotIn("CLAUDECODE", ek.claude_env())
        finally:
            os.environ.pop("CLAUDECODE", None)

    def test_markdown_and_checks(self):
        self.assertEqual(ek.md_cell("a|b\nc"), "a\\|b c")
        self.assertTrue(ek.md_cell("x" * 300, 10).endswith("…"))
        self.assertEqual(ek.md_table(["a"], [[1]])[2], "| 1 |")
        ok = [ek.check("a", True), ek.check("b", False, level="should"), ek.check("c", None)]
        self.assertTrue(ek.verdict_of(ok))
        self.assertFalse(ek.verdict_of(ok + [ek.check("d", False)]))
        self.assertEqual(ek.parse_json_object('前置き ```json\n{"a": 1}\n``` 後'), {"a": 1})
        self.assertEqual(ek.parse_json_object('答え: {"b": 2}'), {"b": 2})
        self.assertIsNone(ek.parse_json_object("なし"))

    def test_llm_judge_reads_one_json_object(self):
        claude = fake_claude_script(self.base, """
            assert argv[argv.index("--tools") + 1] == ""
            text = '採点です ```json\\n{"verdict_correct": true, "overall": 4}\\n```' if "採点" in prompt else "JSON なし"
            emit({"type": "result", "result": text, "total_cost_usd": 0.002, "is_error": False})
            """)
        got = ek.llm_judge("採点してください", "m", claude, self.base, self.base / "j.jsonl", timeout=60)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["grade"], {"verdict_correct": True, "overall": 4})
        bad = ek.llm_judge("ほか", "m", claude, self.base, self.base / "k.jsonl", timeout=60)
        self.assertFalse(bad["ok"])
        self.assertIn("error", bad)

    def test_judge_prompt_shows_the_code_and_the_answer(self):
        repo = self.base / "repo"
        write(repo / "src" / "r.c", "int a;\nint b;\n")
        ctx = {"repo": repo, "issues": {1: {"cid": 1, "checker": "X", "file": "src/r.c", "line": 2}},
               "expected": {"recommendation": "fix", "reason": "本物"}}
        text = stage2.judge_prompt({"mode": "brief", "cids": [1]}, ctx, {"result": {"item": "1"}, "diff": "@@ -1 +1 @@"}, "")
        for needle in ("src/r.c", "   2  int b;", "本物", '"item": "1"', "@@ -1 +1 @@", '"verdict_correct"'):
            self.assertIn(needle, text)

    @unittest.skipUnless(HAS_GIT, "git がない")
    def test_git_helpers(self):
        src = write(self.base / "src" / "a.c", "int a;\n").parent
        repo = self.base / "repo"
        sha = ek.make_repo(src, repo)
        self.assertEqual(ek.head(repo), sha)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "x").write_text("x", encoding="utf-8")
        self.assertTrue(ek.working_changes(repo))
        ek.exclude_in_git(repo, ".claude/")
        ek.exclude_in_git(repo, ".claude/")
        self.assertEqual(ek.working_changes(repo), [])

    def test_mcp_list_tools_with_the_stand_in_server(self):
        config_file = ek.write_json(self.base / "c.json", {"tool": "kit_open", "log": str(self.base / "calls.jsonl")})
        listed = ek.mcp_list_tools({"command": sys.executable,
                                    "args": [str(stage4.SERVER_SCRIPT), "--config", str(config_file)]}, timeout=60)
        self.assertTrue(listed["ok"], listed)
        self.assertEqual([t["name"] for t in listed["tools"]], ["kit_open"])
        bad = ek.mcp_list_tools({"command": sys.executable, "args": ["-c", "import sys; sys.exit(0)"]}, timeout=20)
        self.assertFalse(bad["ok"])


class McpEvalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()  # a long path on Windows, like the code returns

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_evaluation_file_and_only(self):
        xml = write(self.base / "e.xml", """
            <evaluation>
              <qa_pair><question>Q1 &amp; more</question><answer> 7 </answer></qa_pair>
              <qa_pair><question>Q2</question><answer>x</answer></qa_pair>
            </evaluation>
            """)
        pairs = mcp_eval.parse_evaluation_file(xml)
        self.assertEqual(pairs, [{"question": "Q1 & more", "answer": "7"}, {"question": "Q2", "answer": "x"}])
        self.assertEqual(mcp_eval.parse_only(None, 3), [1, 2, 3])
        self.assertEqual(mcp_eval.parse_only(["3,1", "1"], 3), [3, 1])
        for bad in (["0"], ["4"], ["a"]):
            with self.subTest(bad=bad), self.assertRaises(ek.EvalError):
                mcp_eval.parse_only(bad, 3)
        with self.assertRaises(ek.EvalError):
            mcp_eval.parse_evaluation_file(write(self.base / "empty.xml", "<evaluation/>"))
        with self.assertRaises(ek.EvalError):
            mcp_eval.parse_evaluation_file(write(self.base / "broken.xml", "<evaluation>"))

    def test_command_line(self):
        argv = mcp_eval.build_argv(["claude"], "問い", Path("/m.json"), "srv", ["write_it"], "claude-x", 9)
        self.assertEqual(argv[:3], ["claude", "-p", "問い"])
        self.assertEqual(argv[argv.index("--tools") + 1], "", "no built-in tools: only the MCP server")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__srv__*")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "mcp__srv__write_it")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], mcp_eval.EVALUATION_PROMPT)
        for flag in ("--strict-mcp-config", "--setting-sources", "--output-format", "--verbose", "--max-turns"):
            self.assertIn(flag, argv)
        self.assertNotIn("--disallowedTools", mcp_eval.build_argv(["c"], "q", Path("m"), "s", [], "m", 1))

    def test_evaluation_prompt_is_mcp_builders(self):
        # where the official skills are installed for this user (MCP_BUILDER_DIR, or ~/.claude/skills)
        source = Path(os.environ.get("MCP_BUILDER_DIR") or Path.home() / ".claude" / "skills")
        try:
            found = sorted(source.glob("**/mcp-builder/scripts/evaluation.py")) if source.is_dir() else []
        except OSError:  # a folder this user cannot read
            found = []
        if not found:
            self.skipTest("mcp-builder の evaluation.py がない")
        text = found[0].read_text(encoding="utf-8")
        self.assertIn(mcp_eval.EVALUATION_PROMPT, text)

    def test_score_and_report(self):
        events = [event_init(), event_tool("a", "mcp__srv__search", {}), event_result_of("a", "{}"),
                  event_tool("b", "mcp__srv__search", {}), event_result_of("b", "error", True),
                  event_tool("c", "mcp__srv__details", {}), event_result_of("c", "{}"),
                  event_final("<summary>手順</summary><feedback>よい</feedback><response> 31021 </response>", 0.02)]
        scored = mcp_eval.score_task(1, {"question": "q", "answer": "31021"}, fake_run(events, 4.0), "srv")
        self.assertEqual(scored["score"], 1)
        self.assertEqual(scored["tool_calls"], {"search": {"count": 2, "errors": 1}, "details": {"count": 1, "errors": 0}})
        self.assertEqual((scored["num_tool_calls"], scored["tool_errors"]), (3, 1))
        self.assertEqual((scored["summary"], scored["feedback"]), ("手順", "よい"))
        wrong = mcp_eval.score_task(2, {"question": "q", "answer": "ABC"}, fake_run([event_final("<response>abc</response>")]), "srv")
        self.assertEqual(wrong["score"], 0)
        self.assertTrue(wrong["near_match"])
        none = mcp_eval.score_task(3, {"question": "q", "answer": "1"}, fake_run([], timed_out=True), "srv")
        self.assertIsNone(none["actual"])
        self.assertTrue(none["problems"])
        meta = {"started_at": "now", "model": "m", "eval_file": "e.xml", "tasks_total": 3, "server": "srv",
                "server_copy": "c", "fake_data": "f", "disallowed": ["w"]}
        report = mcp_eval.render_report(meta, [scored, wrong, none])
        self.assertIn("1/3（33.3%）", report)
        self.assertIn("### 課題 2", report)
        self.assertIn("N/A", report)

    @unittest.skipUnless(HAS_PARTS, "プラグインの mcp.json と MCP サーバーがない")
    def test_end_to_end_with_a_stand_in(self):
        xml = write(self.base / "e.xml", """
            <evaluation>
              <qa_pair><question>RIGHT</question><answer>7</answer></qa_pair>
              <qa_pair><question>WRONG</question><answer>7</answer></qa_pair>
            </evaluation>
            """)
        claude = fake_claude_script(self.base, """
            assert argv[argv.index("--tools") + 1] == ""
            emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_use", "id": "x", "name": "mcp__" + "SERVER" + "__tool", "input": {}}]}})
            answer = "7" if prompt == "RIGHT" else "8"
            emit({"type": "result", "result": f"<response>{answer}</response>", "total_cost_usd": 0.5, "is_error": False})
            """.replace("SERVER", "coverity-connect"))
        out = self.base / "out"
        proc = subprocess.run([sys.executable, str(HERE / "run_mcp_eval.py"), "--eval-file", str(xml), "--no-preflight",
                               "--out", str(out), "--claude-bin", *claude], capture_output=True, env=ENV, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")[-2000:])
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual((data["correct"], data["total"], data["cost_usd"]), (1, 2, 1.0))
        self.assertTrue((out / "report.md").is_file())
        self.assertTrue((out / "transcripts" / "task-02.jsonl").is_file())
        saved = json.loads((out / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["results"][0]["tool_calls"], {"tool": {"count": 1, "errors": 0}})
        mcp = json.loads((out / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        server = next(iter(mcp.values()))
        self.assertIn(str(out / "server"), " ".join(server["args"]), "--directory points at the temp copy")
        self.assertEqual(server["env"]["COVERITY_API"], "fake")
        self.assertTrue(server["env"]["COVERITY_FAKE_DATA"].startswith(str(out)))
        proc = subprocess.run([sys.executable, str(HERE / "run_mcp_eval.py"), "--only", "9", "--eval-file", str(xml),
                               "--out", str(self.base / "o2")], capture_output=True, env=ENV, timeout=120)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(json.loads(proc.stdout.decode("utf-8"))["ok"])


REPORT = """\
結論：fgets が失敗したとき fp を閉じていない本物のバグ。
**判定**：true_bug
確信度：高（経路をたどって確かめた）
推奨：修正
根拠：src/reader.c:19 — fgets が NULL を返す分岐
src/reader.c:20-21 — fclose せずに return
修正案：
```diff
@@ -19,3 +19,4 @@
     if (fgets(out, BUF_SIZE, fp) == NULL) {
+        fclose(fp);
         return -1;
```
逸脱コメント案：「本物の不具合。fgets 失敗時に fp を閉じずに戻る（src/reader.c:20）。」（Classification Bug／Action Ignore／Severity Moderate）
"""


class Stage2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()  # a long path on Windows, like the code returns

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_the_chat_report(self):
        sections = stage2.parse_report(REPORT)
        self.assertEqual(sorted(sections), sorted(stage2.LABELS))
        self.assertEqual(stage2.judgement_of(sections["判定"]), "true_bug")
        self.assertEqual(stage2.recommendation_of(sections["推奨"]), "fix")
        self.assertTrue(stage2.has_fix_proposal(sections["修正案"]))
        self.assertEqual(stage2.comment_of(sections["逸脱コメント案"]), "本物の不具合。fgets 失敗時に fp を閉じずに戻る（src/reader.c:20）。")
        self.assertEqual(stage2.evidence_in(sections["根拠"]),
                         [{"file": "src/reader.c", "line": 19}, {"file": "src/reader.c", "line": 20, "end": 21}])
        table = "| 項目 | 内容 |\n|---|---|\n| 判定 | false_positive |\n| 推奨 | 逸脱 |\n"
        sections = stage2.parse_report(table)
        self.assertEqual(stage2.judgement_of(sections["判定"]), "false_positive")
        self.assertEqual(stage2.recommendation_of(sections["推奨"]), "deviation")
        self.assertEqual(stage2.judgement_of("誤検知です"), "false_positive")
        self.assertFalse(stage2.has_fix_proposal("なし"))
        self.assertTrue(stage2.has_fix_proposal("最新のリビジョンで直っているので作りません"))
        self.assertEqual(stage2.comment_of("\n> 誤検知。呼び出し元で確かめている（src/a.c:3）。\n"),
                         "誤検知。呼び出し元で確かめている（src/a.c:3）。")

    def test_one_line_reply(self):
        for text in ("20003: 修正（確信度 高）— malloc の戻り値を見ていない",
                     "`20003: 修正を推奨（確信度 低）— 要約`",
                     "G1: 逸脱(確信度 中) - 意図的な切り捨て"):
            with self.subTest(text=text):
                got = stage2.one_line_reply(text)
                self.assertTrue(got["match"], got)
                self.assertEqual(got["lines"], 1)
        got = stage2.one_line_reply("20003: 修正（確信度 高）— 要約\n2 行目")
        self.assertEqual(got["lines"], 2)
        self.assertEqual(got["recommendation"], "fix")
        self.assertFalse(stage2.one_line_reply("続けられません")["match"])

    def test_evidence_check(self):
        repo = self.base / "repo"
        write(repo / "src" / "a.c", "1\n2\n3\n")
        ok = stage2.check_evidence(repo, [{"file": "src/a.c", "line": 3}, {"file": "./src/a.c", "line": 1, "end": 2}])
        self.assertTrue(ok["ok"], ok)
        bad = stage2.check_evidence(repo, [{"file": "src/a.c", "line": 4}, {"file": "src/b.c", "line": 1}])
        self.assertFalse(bad["ok"])
        self.assertIn("src/b.c", bad["detail"])
        self.assertFalse(stage2.check_evidence(repo, [])["ok"])
        self.assertFalse(stage2.check_evidence(repo, [{"file": str(self.base / "x.c"), "line": 1}])["ok"])

    def test_touched_lines(self):
        diff = textwrap.dedent("""\
            diff --git a/src/r.c b/src/r.c
            --- a/src/r.c
            +++ b/src/r.c
            @@ -56,4 +56,4 @@ unsigned char to_u8(int value)
             {
            -    unsigned char a = value;
            -    unsigned char b = value + 1;
            +    unsigned char a = (unsigned char)value;
            +    unsigned char b = (unsigned char)(value + 1);
                 unsigned char c = value + 2;
            """)
        self.assertEqual(stage2.touched_lines(diff), {"src/r.c": {57, 58, 59}})

    def test_brief_has_what_the_worker_needs(self):
        repo = self.base / "repo"
        write(repo / "src" / "reader.c", "".join(f"line {n}\n" for n in range(1, 70)))
        issues = {20004: {"cid": 20004, "checker": "MISRA C-2012 Rule 10.3", "file": "src/reader.c", "function": "to_u8",
                          "line": 57, "impact": "Low", "events": [{"file": "src/reader.c", "line": 57, "tag": "t",
                                                                    "description": "d", "main": True}]},
                  20005: {"cid": 20005, "checker": "MISRA C-2012 Rule 10.3", "file": "src/reader.c", "line": 58}}
        case = {"name": "g", "mode": "brief", "item": "G1", "cids": [20004, 20005]}
        work, item_dir, schema = self.base / "w1", self.base / "items" / "G1", self.base / "schemas" / "result.schema.json"
        text = stage2.render_brief(case, issues, repo, work, item_dir, schema, "abc123")
        for needle in (str(repo), str(work), str(item_dir / "result.json"), str(schema), str(item_dir / "progress.json"),
                       "abc123", "## グループ", "group_excluded_cids", "`G1: <推奨（修正 か 逸脱）>", "★  57",
                       "コマンド（ターミナル）は使いません"):
            self.assertIn(needle, text)
        single = stage2.render_brief({**case, "item": "20004", "cids": [20004]}, issues, repo, work, item_dir, schema, "abc")
        self.assertNotIn("## グループ", single)

    def test_user_checks_on_a_stand_in_answer(self):
        repo = self.base / "repo"
        write(repo / "src" / "reader.c", "".join(f"line {n}\n" for n in range(1, 30)))
        ctx = {"repo": repo, "head": "", "expected": {"recommendation": "fix", "reason": "r"}, "check_comment": None,
               "every_time": ["probe-skill"]}
        orig_changes, orig_head = ek.working_changes, ek.head
        ek.working_changes, ek.head = (lambda r: []), (lambda r: "")
        try:
            summary = ek.summarize([event_init(), event_tool("s", "Skill", {"skill": "probe-skill"}), event_final(REPORT)])
            checks, found = stage2.user_checks({"cids": [20001]}, ctx, fake_run([]), summary)
        finally:
            ek.working_changes, ek.head = orig_changes, orig_head
        by_id = {c["id"]: c for c in checks}
        self.assertTrue(by_id["verdict"]["ok"])
        self.assertTrue(by_id["both_proposals"]["ok"])
        self.assertTrue(by_id["evidence"]["ok"], by_id["evidence"])
        self.assertTrue(by_id["table_skills_used"]["ok"])
        self.assertTrue(by_id["files_unchanged"]["ok"])
        self.assertIsNone(by_id["comment_rules"]["ok"], "no script: skipped, not failed")
        self.assertEqual(found["recommendation"], "fix")

    def test_skill_script_and_definitions(self):
        skills = {"a-skill": str(self.base / "a"), "b-skill": str(self.base / "b")}
        write(self.base / "b" / "scripts" / "check.py", "x\n")
        self.assertEqual(stage2.skill_script(skills, "b-skill", "check.py"), self.base / "b" / "scripts" / "check.py")
        self.assertEqual(stage2.skill_script(skills, "a-skill", "check.py"), self.base / "b" / "scripts" / "check.py",
                         "falls back to any copied skill that has the script")
        self.assertIsNone(stage2.skill_script(skills, None, "none.py"))
        self.assertEqual(stage2.skill_roots({"plugin_dir": str(self.base / "p")}), [self.base / "p" / "skills"])
        self.assertEqual(stage2.skill_roots({}), [])
        part = {"name": "w-agent", "path": self.base / "w-agent.agent.md"}
        self.assertEqual(stage2.worker_definition(ek.ADAPTER.FORMAT_CONVERTED, part, self.base), part["path"])
        with self.assertRaises(ek.ADAPTER.AdapterError):
            stage2.worker_definition(PLUGIN, part, self.base)
        write(self.base / "agents" / "w-agent.md", "---\nname: w-agent\n---\n")
        self.assertEqual(stage2.worker_definition(PLUGIN, part, self.base), (self.base / "agents" / "w-agent.md").resolve())
        self.assertEqual(stage2.make_parser().parse_args([]).format, ek.ADAPTER.FORMAT_CONVERTED)
        self.assertEqual(stage2.make_parser().parse_args(["--format", PLUGIN]).format, PLUGIN)

    @unittest.skipUnless(HAS_PARTS and HAS_CLAUDE_FORM and HAS_GIT and HAS_UV, "プラグインの部品・git・uv のどれかがない")
    def test_build_only_with_the_claude_plugin(self):
        out = self.base / "out"
        proc = subprocess.run(["uv", "run", "--native-tls", "--quiet", str(HERE / "stage2" / "run_stage2.py"),
                               "--format", PLUGIN, "--only", "brief-20003", "--build-only", "--no-sync", "--out", str(out)],
                              capture_output=True, env=ENV, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")[-2000:])
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(data["meta"]["format"], PLUGIN)
        case = data["cases"][0]
        argv = case["command"]
        worker = ek.part("作業役", ROOT)["name"]
        plugin_name = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
        plugin_dir = Path(case["plugin_dir"])
        self.assertEqual(plugin_dir, (out / "brief-20003" / "claude-plugin").resolve())
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(plugin_dir))
        self.assertEqual(argv[argv.index("--agent") + 1], f"{plugin_name}:{worker}", "the worker is the main agent")
        self.assertNotIn("--agents", argv)
        self.assertNotIn("--model", argv, "the definition's model line is used")
        self.assertIn("指示ファイル", argv[2])
        self.assertEqual(sorted(p.name for p in (plugin_dir / "agents").iterdir()), [f"{worker}.md"],
                         "stage 2: the worker alone")
        self.assertEqual((plugin_dir / "agents" / f"{worker}.md").read_bytes(), (ROOT / "agents" / f"{worker}.md").read_bytes())
        table = {r["part"] for r in ek.ADAPTER.load_agent(ROOT / "agents" / f"{worker}.md")["table"]}
        self.assertTrue({p.name for p in (plugin_dir / "skills").iterdir()} <= table)
        repo = Path(case["cwd"])
        self.assertFalse(ek.inside(plugin_dir, repo), "the plugin is outside the repository")
        self.assertFalse((repo / ".claude" / "agents").exists())
        self.assertEqual(ek.working_changes(repo), [])
        brief = (out / "brief-20003" / "run" / "items" / "20003" / "brief.md").read_text(encoding="utf-8")
        self.assertIn("result.schema.json", brief)
        self.assertTrue((out / "brief-20003" / "run" / "schemas" / "result.schema.json").is_file(),
                        "the schema is read from the copied skill")

    def test_select_cases(self):
        cases = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        self.assertEqual(stage2.select_cases(cases, ["c,1"]), [{"name": "c"}, {"name": "a"}])
        with self.assertRaises(ek.EvalError):
            stage2.select_cases(cases, ["z"])

    def test_cases_file(self):
        data = json.loads((HERE / "stage2" / "cases.json").read_text(encoding="utf-8"))
        names = [c["name"] for c in data["cases"]]
        self.assertEqual(len(names), len(set(names)))
        modes = {c["mode"] for c in data["cases"]}
        self.assertEqual(modes, {"user", "brief"})
        for c in data["cases"]:
            self.assertIn("{brief}" if c["mode"] == "brief" else "{repo}", c["prompt"])
            if c["mode"] == "user":
                self.assertIn("ファイルは変えないで", c["prompt"])
        self.assertTrue(any(len(c["cids"]) > 1 for c in data["cases"]), "a group case")

    @unittest.skipUnless(HAS_PARTS and HAS_GIT and HAS_UV, "プラグインの部品・git・uv のどれかがない")
    def test_brief_case_end_to_end_with_a_stand_in(self):
        claude = fake_claude_script(self.base, """
            import re
            from pathlib import Path
            brief = Path(re.search(r"指示ファイル (\\S+) を読み", prompt).group(1)).read_text(encoding="utf-8")
            paths = dict(re.findall(r"^\\| (.+?) \\| `(.+?)` \\|$", brief, re.M))
            work = Path(next(v for k, v in paths.items() if k.startswith("直すフォルダ")))
            result_file = Path(next(v for k, v in paths.items() if k.startswith("結果ファイル（書く）")))
            src = work / "src" / "reader.c"
            src.write_text(src.read_text(encoding="utf-8").replace("    buf[0] = 'a';",
                           "    if (buf == NULL) {\\n        return -1;\\n    }\\n    buf[0] = 'a';"), encoding="utf-8")
            result = {"item": "20003",
                      "verdict": {"judgement": "true_bug", "summary": "malloc の戻り値を確かめずに参照している",
                                  "rationale": "get_buf は malloc の戻り値をそのまま返し、first_char は NULL を確かめずに buf[0] に書く。",
                                  "evidence": [{"file": "src/reader.c", "line": 49, "note": "NULL かもしれない buf に書く"}]},
                      "recommendation": "fix", "confidence": "high", "confidence_reason": "経路をコードで確かめた",
                      "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Major",
                                    "comment": "本物の不具合。malloc が失敗すると NULL の buf に書き込む（src/reader.c:49）。"},
                      "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Major",
                              "summary": "buf が NULL なら -1 を返す", "impact": "malloc の失敗のときだけ振る舞いが変わる",
                              "exceeded_constraints": [], "already_fixed_on_latest": False},
                      "revision_drift": {"status": "none", "detail": ""}, "group_excluded_cids": []}
            result_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            (result_file.parent / "progress.json").write_text(json.dumps({"item": "20003", "phase": "result", "note": "書いた"}), encoding="utf-8")
            emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": str(result_file)}},
                {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": str(src)}}]}})
            emit({"type": "result", "result": "20003: 修正（確信度 高）— malloc の戻り値を確かめずに参照", "is_error": False,
                  "total_cost_usd": 0.0, "num_turns": 2})
            """)
        out = self.base / "out"
        proc = subprocess.run(["uv", "run", "--native-tls", "--quiet", str(HERE / "stage2" / "run_stage2.py"),
                               "--only", "brief-20003", "--no-sync", "--out", str(out), "--claude-bin", *claude],
                              capture_output=True, env=ENV, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")[-2000:])
        data = json.loads(proc.stdout.decode("utf-8"))
        case = json.loads((out / "brief-20003" / "case.json").read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in case["checks"]}
        for must in ("finished", "one_line_reply", "result_valid", "comment_rules", "evidence", "verdict",
                     "fix_in_work_copy", "repo_unchanged", "writes_only_allowed"):
            self.assertTrue(by_id[must]["ok"], (must, by_id[must]))
        self.assertTrue(data["cases"][0]["passed"])
        self.assertFalse(by_id["table_skills_used"]["ok"], "the stand-in loaded no skill")
        self.assertTrue((out / "report.md").is_file())
        self.assertEqual(ek.working_changes(out / "brief-20003" / "repo"), [], ".claude/ is hidden from git status")

    @unittest.skipUnless(HAS_PARTS and HAS_CLAUDE_FORM and HAS_GIT and HAS_UV, "プラグインの部品・git・uv のどれかがない")
    def test_brief_case_with_the_claude_plugin_and_a_stand_in(self):
        claude = fake_claude_script(self.base, """
            import re
            from pathlib import Path
            plugin = Path(argv[argv.index("--plugin-dir") + 1])
            assert (plugin / ".claude-plugin" / "plugin.json").is_file(), plugin
            assert "--agents" not in argv and "--mcp-config" not in argv, argv
            for skill in sorted((plugin / "skills").iterdir()):
                emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                    {"type": "tool_use", "id": "r-" + skill.name, "name": "Read",
                     "input": {"file_path": str(skill / "SKILL.md")}}]}})
            brief = Path(re.search(r"指示ファイル (\\S+) を読み", prompt).group(1)).read_text(encoding="utf-8")
            paths = dict(re.findall(r"^\\| (.+?) \\| `(.+?)` \\|$", brief, re.M))
            work = Path(next(v for k, v in paths.items() if k.startswith("直すフォルダ")))
            result_file = Path(next(v for k, v in paths.items() if k.startswith("結果ファイル（書く）")))
            src = work / "src" / "reader.c"
            src.write_text(src.read_text(encoding="utf-8").replace("    buf[0] = 'a';",
                           "    if (buf == NULL) {\\n        return -1;\\n    }\\n    buf[0] = 'a';"), encoding="utf-8")
            result = {"item": "20003",
                      "verdict": {"judgement": "true_bug", "summary": "malloc の戻り値を確かめずに参照している",
                                  "rationale": "get_buf は malloc の戻り値をそのまま返し、first_char は NULL を確かめずに buf[0] に書く。",
                                  "evidence": [{"file": "src/reader.c", "line": 49, "note": "NULL かもしれない buf に書く"}]},
                      "recommendation": "fix", "confidence": "high", "confidence_reason": "経路をコードで確かめた",
                      "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Major",
                                    "comment": "本物の不具合。malloc が失敗すると NULL の buf に書き込む（src/reader.c:49）。"},
                      "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Major",
                              "summary": "buf が NULL なら -1 を返す", "impact": "malloc の失敗のときだけ振る舞いが変わる",
                              "exceeded_constraints": [], "already_fixed_on_latest": False},
                      "revision_drift": {"status": "none", "detail": ""}, "group_excluded_cids": []}
            result_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            (result_file.parent / "progress.json").write_text(json.dumps({"item": "20003", "phase": "result", "note": "書いた"}), encoding="utf-8")
            emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": str(result_file)}},
                {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": str(src)}}]}})
            emit({"type": "result", "result": "20003: 修正（確信度 高）— malloc の戻り値を確かめずに参照", "is_error": False,
                  "total_cost_usd": 0.0, "num_turns": 2})
            """)
        out = self.base / "out"
        proc = subprocess.run(["uv", "run", "--native-tls", "--quiet", str(HERE / "stage2" / "run_stage2.py"),
                               "--format", PLUGIN, "--only", "brief-20003", "--no-sync", "--out", str(out),
                               "--claude-bin", *claude], capture_output=True, env=ENV, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")[-2000:])
        data = json.loads(proc.stdout.decode("utf-8"))
        case = json.loads((out / "brief-20003" / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(case["format"], PLUGIN)
        by_id = {c["id"]: c for c in case["checks"]}
        for must in ("finished", "one_line_reply", "result_valid", "comment_rules", "evidence", "verdict",
                     "fix_in_work_copy", "repo_unchanged", "writes_only_allowed"):
            self.assertTrue(by_id[must]["ok"], (must, by_id[must]))
        self.assertTrue(by_id["table_skills_used"]["ok"], by_id["table_skills_used"])
        self.assertTrue(data["cases"][0]["passed"])
        self.assertIn("claude-plugin", (out / "report.md").read_text(encoding="utf-8"))
        self.assertEqual(ek.working_changes(out / "brief-20003" / "repo"), [])


class Stage3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()  # a long path on Windows, like the code returns

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_agent_options_keeps_comments(self):
        text = "a: 1\nagents:\n  # 検証役も動かす\n  review: true   # 確かめ\n  max_rework: 1\nb: 2\n"
        got = stage3.set_agent_options(text, {"review": False, "integration": True})
        self.assertIn("  review: false   # 確かめ", got)
        self.assertIn("  integration: true", got)
        self.assertIn("  # 検証役も動かす", got)
        self.assertTrue(got.endswith("b: 2\n"))
        self.assertEqual(got.count("agents:"), 1)
        added = stage3.set_agent_options("a: 1\n", {"review": True, "integration": False})
        self.assertEqual(added, "a: 1\nagents:\n  review: true\n  integration: false\n")

    def test_prompts(self):
        self.assertIn("すべて", stage3.prompt_for("all.yaml", None))
        limited = stage3.prompt_for("all.yaml", 1)
        self.assertIn("1 件だけ", limited)
        self.assertNotIn("すべて", limited, "「すべて」 would contradict the limit")
        self.assertIn("all.yaml", limited)

    def make_run(self) -> Path:
        out = self.base / "out"
        run = out / "20261108-101500"
        (out / "20261108-101400").mkdir(parents=True)
        (out / "not-a-run").mkdir()
        ek.write_json(run / "items" / "20001" / "result.json", {"recommendation": "fix"})
        ek.write_json(run / "items" / "20002" / "result.json", {"recommendation": "fix"})
        ek.write_json(run / "items" / "20001" / "review.json", {"outcome": "pass"})
        return run

    def test_run_folder_items_and_accuracy(self):
        run = self.make_run()
        self.assertEqual(stage3.find_run_dir(run.parent), run)
        self.assertEqual(len(stage3.run_dirs(run.parent)), 2)
        self.assertIsNone(stage3.find_run_dir(self.base / "none"))
        acc = stage3.accuracy(run, {"items": {"20001": {"recommendation": "fix"}, "20002": {"recommendation": "deviation"},
                                              "20003": {"recommendation": "fix"}}})
        self.assertEqual((acc["matched"], acc["total"]), (1, 3))
        run_json = {"items": [{"id": "20001", "status": "done"}, {"id": "20002", "status": "done"},
                              {"id": "20009", "status": "merged"}]}
        checks = {c["id"]: c for c in stage3.item_checks(run, run_json, review_on=True, integration_on=True)}
        self.assertTrue(checks["results_every_item"]["ok"])
        self.assertFalse(checks["reviews_when_on"]["ok"])
        self.assertIn("20002", checks["reviews_when_on"]["detail"])
        self.assertTrue(checks["integration_when_on"]["ok"], "no candidates: nothing to integrate")

    def test_counts_text(self):
        self.assertEqual(stage3.counts_text({"done": 3, "total": 4, "error": 1, "pending": 0}), "終わった 3 / 4（error 1）")
        self.assertEqual(stage3.counts_text(None), "－")

    def test_agent_checks(self):
        names = {"worker": "w-agent", "reviewer": "r-agent", "integrator": "i-agent"}
        good = ek.summarize([event_tool("a", "Agent", {"subagent_type": "w-agent"}),
                             event_tool("b", "Agent", {"subagent_type": "kit:r-agent"}),
                             event_tool("c", "Write", {"file_path": "/x"}, parent="a")])
        self.assertTrue(ek.verdict_of(stage3.agent_checks(good, names)))
        bad = ek.summarize([event_tool("a", "Agent", {"subagent_type": "general-purpose"}),
                            event_tool("c", "Edit", {"file_path": "/repo/x.c"})])
        failed = {c["id"] for c in stage3.agent_checks(bad, names) if c["ok"] is False}
        self.assertEqual(failed, {"role_agents_only", "worker_called", "orchestrator_no_edits"})

    def test_selftest_checks_mapping(self):
        answer = {"ok": True, "checks": [{"id": "2-2", "status": "review", "actual": "a"},
                                         {"id": "2-3", "status": "fail", "actual": "b", "detail": "d"},
                                         {"id": "2-4", "status": "review", "actual": "c"},
                                         {"id": "2-5", "status": "pass", "actual": "e"},
                                         {"id": "2-6", "status": "pass", "actual": "f"}]}
        original = ek.run_json
        ek.run_json = lambda argv, **kw: (answer, {"stderr": "", "stdout": ""})
        try:
            full = {c["id"]: c for c in stage3.selftest_checks(Path("s.py"), Path("d"), Path("r"))[0]}
            limited = {c["id"]: c for c in stage3.selftest_checks(Path("s.py"), Path("d"), Path("r"), limited=True)[0]}
        finally:
            ek.run_json = original
        self.assertEqual([full[k]["ok"] for k in ("selftest-2-2", "selftest-2-3", "selftest-2-4", "selftest-2-5", "selftest-2-6")],
                         [True, False, False, True, True], "must: review passes; should: only pass passes")
        self.assertEqual(full["selftest-2-3"]["level"], "must")
        self.assertIsNone(limited["selftest-2-4"]["ok"], "a limited run skips the conclusions check")
        self.assertIsNone(limited["selftest-2-5"]["ok"], "and the group check")
        self.assertFalse(limited["selftest-2-3"]["ok"])

    @unittest.skipUnless(HAS_PARTS and HAS_GIT and HAS_UV, "プラグインの部品・git・uv のどれかがない")
    def test_build_only(self):
        out = self.base / "out"
        proc = subprocess.run(["uv", "run", "--native-tls", "--quiet", str(HERE / "stage3" / "run_stage3.py"),
                               "--build-only", "--no-sync", "--variant", "full", "--limit", "2", "--out", str(out)],
                              capture_output=True, env=ENV, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")[-2000:])
        data = json.loads(proc.stdout.decode("utf-8"))
        variant = data["variants"][0]
        argv = variant["command"]
        orchestrator = ek.part("統括", ROOT)["name"]
        self.assertEqual(argv[argv.index("--agent") + 1], orchestrator)
        self.assertIn("2 件だけ", argv[2])
        self.assertIn("Skill", argv[argv.index("--allowedTools"):])
        repo = Path(variant["cwd"])
        output_dir = Path(argv[argv.index("--add-dir") + 1])
        self.assertFalse(ek.inside(output_dir, repo), "the run folder is outside the repository")
        self.assertEqual(output_dir, (repo.parent / "coverity-triage-out").resolve())
        self.assertEqual(argv[argv.index("--max-turns") + 1], "500")
        config = (repo / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("review: true", config)
        self.assertIn("integration: true", config)
        self.assertEqual(ek.working_changes(repo), [], "the variant's config is committed, .claude/ is hidden")
        agents = json.loads((repo / ".claude" / "eval" / "agents.json").read_text(encoding="utf-8"))
        self.assertIn(ek.part("作業役", ROOT)["name"], agents)

    def test_orchestrator_definition(self):
        part = {"name": "o-agent", "path": self.base / "o-agent.agent.md"}
        self.assertEqual(stage3.orchestrator_definition(ek.ADAPTER.FORMAT_CONVERTED, part, self.base), part["path"])
        write(self.base / "agents" / "o-agent.md", "---\nname: o-agent\n---\n")
        self.assertEqual(stage3.orchestrator_definition(PLUGIN, part, self.base), (self.base / "agents" / "o-agent.md").resolve())
        self.assertEqual(stage3.make_parser().parse_args(["--format", PLUGIN]).format, PLUGIN)

    @unittest.skipUnless(HAS_PARTS and HAS_CLAUDE_FORM and HAS_GIT and HAS_UV, "プラグインの部品・git・uv のどれかがない")
    def test_build_only_with_the_claude_plugin(self):
        out = self.base / "out"
        proc = subprocess.run(["uv", "run", "--native-tls", "--quiet", str(HERE / "stage3" / "run_stage3.py"),
                               "--format", PLUGIN, "--build-only", "--no-sync", "--variant", "default", "--limit", "1",
                               "--agent-model", "inherit", "--out", str(out)],
                              capture_output=True, env=ENV, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")[-2000:])
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(data["meta"]["format"], PLUGIN)
        variant = data["variants"][0]
        argv = variant["command"]
        plugin_name = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
        orchestrator = ek.part("統括", ROOT)["name"]
        plugin_dir = Path(variant["plugin_dir"])
        self.assertEqual(plugin_dir, (out / "default" / "claude-plugin").resolve())
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(plugin_dir))
        self.assertEqual(argv[argv.index("--agent") + 1], f"{plugin_name}:{orchestrator}")
        self.assertIn("1 件だけ", argv[2])
        self.assertIn("Bash(uv run *)", argv)
        self.assertIn(f"mcp__plugin_{plugin_name}_{ek.part('Coverity との通信', ROOT)['name']}__*", argv)
        roles = {ek.part(word, ROOT)["name"] for word in ("統括", "作業役", "検証役", "統合役")}
        self.assertEqual({p.stem for p in (plugin_dir / "agents").iterdir()}, roles)
        for path in (plugin_dir / "agents").iterdir():
            old = (ROOT / "agents" / path.name).read_text(encoding="utf-8").split("\n")
            new = path.read_text(encoding="utf-8").split("\n")
            self.assertEqual([b for a, b in zip(old, new) if a != b], ["model: inherit"], "--agent-model: only the model line")
        self.assertIn(ek.part("一括実行", ROOT)["name"], {p.name for p in (plugin_dir / "skills").iterdir()})
        self.assertNotIn(ek.part("画面の入口", ROOT)["name"], {p.name for p in (plugin_dir / "skills").iterdir()},
                         "a skill named in no table is not copied")
        repo = Path(variant["cwd"])
        self.assertFalse(ek.inside(plugin_dir, repo))
        output_dir = Path(argv[argv.index("--add-dir") + 1])
        self.assertFalse(ek.inside(plugin_dir, output_dir))
        self.assertFalse((repo / ".claude" / "agents").exists())
        self.assertEqual(ek.working_changes(repo), [])
        record = json.loads((out / "default" / "variant.json").read_text(encoding="utf-8"))
        self.assertEqual(record["format"], PLUGIN)
        self.assertEqual({a["model"] for a in record["agents"]}, {"inherit"})


class Stage4Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()  # a long path on Windows, like the code returns

    def tearDown(self):
        self.tmp.cleanup()

    def talk(self, config: Path, messages: list[dict]) -> list[dict]:
        data = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8") + b"not json\n"
        proc = subprocess.run([sys.executable, str(stage4.SERVER_SCRIPT), "--config", str(config)], input=data,
                              capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]

    def test_stand_in_server(self):
        log = self.base / "calls.jsonl"
        config = ek.write_json(self.base / "c.json", {"tool": "kit_open", "description": "画面を開く", "url": "http://u/",
                                                      "notices": ["注意その1"], "log": str(log)})
        replies = self.talk(config, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kit_open", "arguments": {"requestText": "頼み"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "other", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 6, "method": "ping"},
        ])
        by_id = {r.get("id"): r for r in replies}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-03-26")
        tool = by_id[2]["result"]["tools"][0]
        self.assertEqual((tool["name"], tool["inputSchema"]["required"]), ("kit_open", ["requestText"]))
        text = by_id[3]["result"]["content"][0]["text"]
        self.assertIn("http://u/", text)
        self.assertIn("- 注意その1", text)
        self.assertTrue(by_id[4]["result"]["isError"])
        self.assertEqual(by_id[5]["error"]["code"], -32601)
        self.assertEqual(by_id[6]["result"], {})
        self.assertEqual(by_id[None]["error"]["code"], -32700)
        calls = stage4.read_calls(log)
        self.assertEqual([c["arguments"] for c in calls], [{"requestText": "頼み"}])

    def test_extension_texts(self):
        texts, source = stage4.extension_texts(self.base / "no-plugin")
        self.assertEqual(texts, stage4.FALLBACK_TEXTS)
        if not (HAS_PARTS and shutil.which("node")):
            self.skipTest("node かエクステンションがない")
        texts, source = stage4.extension_texts(ROOT)
        self.assertTrue(source.endswith("messages.mjs"), source)
        self.assertIn("requestText", texts["description"])
        self.assertEqual(len(texts["notices"]), 4)

    def summary_for(self, events):
        return ek.summarize([event_init()] + events)

    def test_analyze_routes(self):
        names = {"screen": "entry", "run": "batch", "help": "helper"}
        tool = "kit_open"
        full = ek.mcp_tool_name(stage4.SERVER_NAME, tool)
        good_text = f"開きました {stage4.FAKE_URL}\n- 開始を押す\n- /clear しない\n- このチャットで答える\n- README にある"
        summary = self.summary_for([event_tool("s", "Skill", {"skill": "entry"}), event_tool("o", full, {"requestText": "頼み"}),
                                    event_final(good_text)])
        calls = [{"arguments": {"requestText": "頼み"}}]
        checks = stage4.analyze({"prompt": "頼み", "expect": "open"}, fake_run([]), summary, calls, tool, names)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        reworded = stage4.analyze({"prompt": "頼み", "expect": "open"}, fake_run([]), summary,
                                  [{"arguments": {"requestText": "頼みです"}}], tool, names)
        self.assertFalse({c["id"]: c for c in reworded}["request_text_exact"]["ok"])
        worked = self.summary_for([event_tool("o", full, {"requestText": "頼み"}), event_tool("b", "Bash", {"command": "uv run x"}),
                                   event_final(good_text)])
        checks = {c["id"]: c for c in stage4.analyze({"prompt": "頼み", "expect": "open"}, fake_run([]), worked, calls, tool, names)}
        self.assertFalse(checks["no_work"]["ok"])
        self.assertFalse(checks["entry_skill_used"]["ok"])
        not_open = stage4.analyze({"prompt": "/batch High だけ", "expect": "not_open", "expect_skill": "{run}"}, fake_run([]),
                                  self.summary_for([event_tool("s", "Skill", {"skill": "batch"}), event_final("始めます")]),
                                  [], tool, names)
        self.assertTrue(ek.verdict_of(not_open))
        self.assertTrue({c["id"]: c for c in not_open}["expected_skill"]["ok"])
        opened = stage4.analyze({"prompt": "CID 1", "expect": "not_open"}, fake_run([]), summary, calls, tool, names)
        self.assertFalse(ek.verdict_of(opened))
        causes = ("画面のツールがありません。1. copilot --experimental で起動。2. 読み込みの確認を断った。"
                  "3. /extensions mode が Disabled。")
        missing = stage4.analyze({"prompt": "頼み", "expect": "no_tool", "expect_skill": "{screen}"}, fake_run([]),
                                 self.summary_for([event_tool("s", "Skill", {"skill": "entry"}), event_final(causes)]),
                                 [], tool, names)
        self.assertTrue(all(c["ok"] for c in missing), missing)
        two = stage4.analyze({"prompt": "頼み", "expect": "no_tool"}, fake_run([]),
                             self.summary_for([event_final("--experimental で起動してください")]), [], tool, names)
        self.assertFalse({c["id"]: c for c in two}["three_causes"]["ok"])

    def test_cases_file(self):
        data = json.loads((HERE / "stage4" / "cases.json").read_text(encoding="utf-8"))
        expects = [c["expect"] for c in data["cases"]]
        self.assertGreaterEqual(len(expects), 6)
        self.assertEqual(set(expects), {"open", "not_open", "no_tool"})
        self.assertTrue(any(c["prompt"].startswith("/{run}") for c in data["cases"]))
        self.assertTrue(all(c.get("mcp", True) is False for c in data["cases"] if c["expect"] == "no_tool"))

    @unittest.skipUnless(HAS_PARTS and HAS_GIT, "プラグインの部品か git がない")
    def test_build_only(self):
        out = self.base / "out"
        proc = subprocess.run([sys.executable, str(HERE / "stage4" / "run_stage4.py"), "--build-only", "--out", str(out)],
                              capture_output=True, env=ENV, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")[-2000:])
        data = json.loads(proc.stdout.decode("utf-8"))
        run_name = ek.part("一括実行", ROOT)["name"]
        commands = {c["name"]: c["command"] for c in data["cases"]}
        self.assertEqual(commands["run-command"][2], f"/{run_name} High だけ")
        argv = commands["triage"]
        self.assertEqual(argv[argv.index("--allowedTools") + 1: argv.index("--allowedTools") + 3],
                         ["Skill", ek.mcp_tool_name(stage4.SERVER_NAME, "*")])
        self.assertNotIn("Bash(", " ".join(argv), "commands are not allowed")
        servers = json.loads((out / "tool-missing" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(servers, {})
        servers = json.loads((out / "triage" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(list(servers), [stage4.SERVER_NAME])
        skills = sorted(p.name for p in (out / "ws" / ".claude" / "skills").iterdir())
        self.assertEqual(skills, sorted({ek.part(r, ROOT)["name"] for r in ("画面の入口", "一括実行", "困ったとき")}))


if __name__ == "__main__":
    unittest.main()
