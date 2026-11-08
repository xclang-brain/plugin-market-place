"""Tests for claude_adapter.py (no model is called).

Run from the plugin folder:  python3 -m unittest discover -s tests/eval -p "test_*.py"
The fake plugin below uses made-up part names; the real agents are read from the
plugin folder, and their names come from their tables only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE
REAL_AGENTS = PLUGIN_ROOT / "com.github.copilot" / "agents"
SCRIPT = HERE / "claude_adapter.py"

_spec = importlib.util.spec_from_file_location("claude_adapter_under_test", SCRIPT)
ca = importlib.util.module_from_spec(_spec)
sys.dont_write_bytecode = True
_spec.loader.exec_module(ca)  # type: ignore[union-attr]
ca.VERBOSE = False

ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def make_fake_plugin(root: Path) -> Path:
    agents = root / "com.github.copilot" / "agents"
    write(agents / "lead.agent.md", """
        ---
        description: "まとめ役。手順のスキルに従って呼ぶ。"
        model: [claude-sonnet-5.5, gpt-6.1-sol]
        ---

        あなたはまとめ役です。

        ## 使う部品

        | 役割 | 部品 | 使うとき |
        | --- | --- | --- |
        | 手順 | `run-steps` | 毎回 |
        | 通信 | `fake-server` | 取得のとき |
        | 作業役 | `helper-a` | 頼むとき |

        ## 守ること

        - ファイルを直しません。
        """)
    write(agents / "helper-a.agent.md", """
        ---
        description: "作業役。調べて返す。"
        model: [gpt-6-luna, claude-haiku-5.5]
        tools: [read, edit, search, execute, skill, ask_user, "fake-server/fake_read", "fake-server/fake_list"]
        ---

        あなたは作業役です。

        ## 使う部品

        | 役割 | 部品 | 使うとき |
        | --- | --- | --- |
        | 見方 | `notes-skill` | 調べ始めるとき |
        | 調べ | `probe-skill` | 毎回 |
        | 通信 | `fake-server` | 足りないとき |

        本文の終わり。
        """)
    for name in ("run-steps", "notes-skill", "probe-skill"):
        write(root / "skills" / name / "SKILL.md", f"""
            ---
            name: {name}
            description: 試しのスキル {name}。
            ---

            # {name}
            """)
        write(root / "skills" / name / "README.md", "試し\n")
        (root / "skills" / name / "__pycache__").mkdir()
        write(root / "skills" / name / "__pycache__" / "x.pyc", "junk\n")
    server = root / "mcp-servers" / "fake-server"
    write(server / "pyproject.toml", """
        [project]
        name = "fake-server"
        version = "0.0.1"

        [project.scripts]
        fake-server-mcp = "fake_server:main"
        """)
    write(server / "uv.lock", "version = 1\n")
    write(root / "mcp.json", json.dumps({"mcpServers": {"fake-server": {
        "type": "stdio", "command": "uv",
        "args": ["run", "--native-tls", "--frozen", "--quiet", "--directory",
                 "${PLUGIN_ROOT}/mcp-servers/fake-server", "fake-server-mcp"],
        "env": {"COV_USER": "${COV_USER}", "UNSET_FOR_TEST": "${CLAUDE_ADAPTER_SURELY_UNSET_VAR}"}}}}))
    return agents


class Args:
    """A stand-in for argparse.Namespace with the build defaults."""

    def __init__(self, **kw):
        defaults = dict(workspace=None, parts_from_table=None, agent=None, skill=None, mcp=None,
                        plugin_root=None, main=None, output_dir=None, fake_data=None, mcp_env=None,
                        model=None, agent_model=None, prompt=None, extra_arg=None, claude_bin=None,
                        allow_missing=False, run=False, inspect=False, timeout=30.0)
        defaults.update(kw)
        self.__dict__.update(defaults)


class ParsingTest(unittest.TestCase):
    def test_frontmatter_flow_lists_and_quotes(self):
        fields, body = ca.parse_frontmatter(
            '---\ndescription: "説明。/agent で選ぶ"\nmodel: [gpt-6-luna, claude-haiku-5.5]\n'
            'tools: [read, "srv/tool_a", \'srv/tool_b\']\n---\n\n本文\n')
        self.assertEqual(fields["description"], "説明。/agent で選ぶ")
        self.assertEqual(fields["model"], ["gpt-6-luna", "claude-haiku-5.5"])
        self.assertEqual(fields["tools"], ["read", "srv/tool_a", "srv/tool_b"])
        self.assertEqual(body, "\n本文\n")

    def test_frontmatter_missing(self):
        with self.assertRaises(ca.AdapterError):
            ca.parse_frontmatter("本文だけ\n")

    def test_parts_table(self):
        rows = ca.parse_parts_table("x\n\n## 使う部品\n\n| 役割 | 部品 | 使うとき |\n| --- | --- | --- |\n"
                                    "| 一 | `a-b` | 毎回 |\n| 二 | `c` | 必要なとき |\n\n- 注\n")
        self.assertEqual(rows, [{"role": "一", "part": "a-b", "when": "毎回"},
                                {"role": "二", "part": "c", "when": "必要なとき"}])

    def test_parts_table_rejects_bad_shapes(self):
        bad = [
            "## ほか\n| 役割 | 部品 | 使うとき |\n| --- | --- | --- |\n| 一 | `a` | 毎回 |\n",
            "## 使う部品\n| 役割 | スキル | 使うとき |\n| --- | --- | --- |\n| 一 | `a` | 毎回 |\n",
            "## 使う部品\n| 役割 | 部品 | 使うとき |\n| --- | --- | --- |\n| 一 | a | 毎回 |\n",
            "## 使う部品\n\n本文だけ\n",
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ca.AdapterError):
                ca.parse_parts_table(text)


class MappingTest(unittest.TestCase):
    def test_tool_mapping(self):
        mapped, unknown = ca.map_tools(["read", "edit", "search", "execute", "agent", "skill", "ask_user",
                                        "srv/tool_x", "srv/*", "web-thing"])
        self.assertEqual(mapped, ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "Agent", "Skill",
                                  "AskUserQuestion", "mcp__srv__tool_x", "mcp__srv"])
        self.assertEqual(unknown, ["web-thing"])

    def test_model(self):
        self.assertEqual(ca.claude_model(["gpt-6-luna", "claude-haiku-5.5"]), "claude-haiku-5-5")
        self.assertEqual(ca.claude_model("claude-sonnet-5.5"), "claude-sonnet-5-5")
        self.assertEqual(ca.claude_model(["gpt-6-luna"]), "inherit")
        self.assertEqual(ca.claude_model(None), "inherit")
        self.assertEqual(ca.claude_model(["claude-haiku-5.5"], "inherit"), "inherit")


class FakePluginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "plugin"
        self.agents = make_fake_plugin(self.root)
        self.ws = self.base / "ws"
        self.ws.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_follows_agents_in_the_table(self):
        layout = ca.Layout(self.root)
        got = ca.collect_parts(self.agents / "lead.agent.md", layout)
        self.assertEqual([a["name"] for a in got["agents"]], ["lead", "helper-a"])
        self.assertEqual(sorted(got["skills"]), ["notes-skill", "probe-skill", "run-steps"])
        self.assertEqual(list(got["mcp"]), ["fake-server"])
        self.assertEqual(got["mcp"]["fake-server"], (self.root / "mcp-servers" / "fake-server").resolve())
        self.assertEqual(got["missing"], [])
        self.assertEqual(got["agents"][1]["skills"], ["notes-skill", "probe-skill"])

    def test_collect_stage2_does_not_go_up(self):
        got = ca.collect_parts(self.agents / "helper-a.agent.md", ca.Layout(self.root))
        self.assertEqual([a["name"] for a in got["agents"]], ["helper-a"])
        self.assertNotIn("run-steps", got["skills"])

    def test_collect_reports_missing(self):
        import shutil
        shutil.rmtree(self.root / "skills" / "probe-skill")
        got = ca.collect_parts(self.agents / "helper-a.agent.md", ca.Layout(self.root))
        self.assertEqual(got["missing"], [{"name": "probe-skill", "kind": "unknown", "from": "helper-a"}])

    def test_build_writes_workspace_and_command(self):
        fake = write(self.base / "fake-issues.yaml", "issues: []\n")
        out = self.base / "out"
        res = ca.build(Args(workspace=str(self.ws), parts_from_table=str(self.agents / "lead.agent.md"),
                            fake_data=str(fake), output_dir=str(out), model="claude-haiku-5-5",
                            prompt="List the tools"))
        self.assertTrue(res["ok"], res)
        claude = self.ws / ".claude"
        self.assertEqual(sorted(p.name for p in (claude / "skills").iterdir()),
                         ["notes-skill", "probe-skill", "run-steps"])
        self.assertFalse((claude / "skills" / "run-steps" / "__pycache__").exists())
        self.assertEqual(sorted(p.name for p in (claude / "agents").iterdir()), ["helper-a.md", "lead.md"])

        agents = json.loads((claude / "eval" / "agents.json").read_text(encoding="utf-8"))
        helper = agents["helper-a"]
        self.assertEqual(helper["tools"], ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "Skill",
                                           "AskUserQuestion", "mcp__fake-server__fake_read",
                                           "mcp__fake-server__fake_list"])
        self.assertEqual(helper["model"], "claude-haiku-5-5")
        self.assertEqual(helper["skills"], ["notes-skill", "probe-skill"])
        original_body = ca.parse_frontmatter((self.agents / "helper-a.agent.md").read_text(encoding="utf-8"))[1]
        self.assertTrue(helper["prompt"].startswith(ca.GLOSSARY + "\n\n"))
        self.assertEqual(helper["prompt"][len(ca.GLOSSARY) + 2:], original_body.lstrip("\n"))
        self.assertNotIn("tools", agents["lead"], "an agent without tools inherits every tool")
        self.assertEqual(agents["lead"]["skills"], ["run-steps"])
        self.assertEqual(agents["lead"]["model"], "claude-sonnet-5-5")

        md_fields, md_body = ca.parse_frontmatter((claude / "agents" / "helper-a.md").read_text(encoding="utf-8"))
        self.assertEqual(md_fields["name"], "helper-a")
        self.assertEqual(md_fields["skills"], ["notes-skill", "probe-skill"])
        self.assertIn("mcp__fake-server__fake_read", md_fields["tools"])
        self.assertTrue(md_body.lstrip().startswith(ca.GLOSSARY))

        mcp = json.loads((claude / "eval" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["fake-server"]
        copied = claude / "mcp" / "fake-server"
        self.assertTrue((copied / "uv.lock").is_file())
        self.assertIn(str(copied), mcp["args"])
        self.assertNotIn("${PLUGIN_ROOT}", json.dumps(mcp))
        self.assertEqual(mcp["args"][-1], "fake-server-mcp")
        self.assertEqual(mcp["env"]["COVERITY_API"], "fake")
        self.assertEqual(mcp["env"]["COVERITY_FAKE_DATA"], str(fake.resolve()))
        self.assertNotIn("UNSET_FOR_TEST", mcp["env"])

        argv = res["command"]
        self.assertEqual(argv[:3], ["claude", "-p", "List the tools"])
        for flag in ("--agent", "--agents", "--mcp-config", "--strict-mcp-config", "--setting-sources",
                     "--permission-mode", "--allowedTools", "--add-dir", "--model"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "lead")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        allowed = argv[argv.index("--allowedTools") + 1: argv.index("--add-dir")]
        self.assertEqual(allowed, ["Bash(uv run:*)", "Skill", "mcp__fake-server__*"],
                         "Skill: claude -p denies skills with allowed-tools unless the Skill tool is allowed")
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(out.resolve()))
        self.assertEqual(argv[argv.index("--tools") + 1].split(","), ca.BASE_TOOLS,
                         "an agent without tools gets the base set; the union keeps it")
        self.assertIn("'Bash(uv run:*)'", res["command_line_bash"])
        saved = json.loads((claude / "eval" / "command.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["argv"][saved["prompt_index"]], ca.PROMPT_PLACEHOLDER)

    def test_build_with_agent_only_and_main(self):
        res = ca.build(Args(workspace=str(self.ws), agent=[str(self.agents / "helper-a.agent.md")],
                            plugin_root=str(self.root)))
        self.assertTrue(res["ok"], res)
        argv = res["command"]
        self.assertEqual(argv[argv.index("--tools") + 1],
                         "Read,Glob,Grep,Edit,Write,Bash,Skill,AskUserQuestion")
        self.assertEqual(res["main_agent"], "helper-a")
        self.assertEqual([a["name"] for a in res["agents"]], ["helper-a"])
        self.assertEqual(sorted(res["skills"]), ["notes-skill", "probe-skill"])

    def test_build_missing_needs_allow_missing(self):
        import shutil
        shutil.rmtree(self.root / "skills" / "probe-skill")
        with self.assertRaises(ca.AdapterError) as cm:
            ca.build(Args(workspace=str(self.ws), parts_from_table=str(self.agents / "helper-a.agent.md")))
        self.assertTrue(any("probe-skill" in p for p in cm.exception.problems))
        res = ca.build(Args(workspace=str(self.ws), parts_from_table=str(self.agents / "helper-a.agent.md"),
                            allow_missing=True))
        self.assertTrue(res["ok"])
        self.assertEqual(res["agents"][0]["skills"], ["notes-skill"])

    def test_extra_skill_fills_a_missing_row(self):
        import shutil
        shutil.rmtree(self.root / "skills" / "probe-skill")
        dummy = write(self.base / "dummies" / "probe-skill" / "SKILL.md",
                      "---\nname: probe-skill\ndescription: 代わり。\n---\n").parent
        res = ca.build(Args(workspace=str(self.ws), parts_from_table=str(self.agents / "helper-a.agent.md"),
                            skill=[str(dummy)]))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["agents"][0]["skills"], ["notes-skill", "probe-skill"])

    def test_workspace_inside_plugin_is_refused(self):
        with self.assertRaises(ca.AdapterError):
            ca.build(Args(workspace=str(self.root / "inside"), parts_from_table=str(self.agents / "lead.agent.md")))
        self.assertFalse((self.root / "inside").exists())

    def test_cli_collect_and_errors(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "collect", str(self.agents / "lead.agent.md")],
                              capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual([a["name"] for a in data["agents"]], ["lead", "helper-a"])
        proc = subprocess.run([sys.executable, str(SCRIPT), "collect", str(self.base / "nothing.agent.md")],
                              capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("error", data)
        self.assertNotIn(b"Traceback", proc.stderr)
        proc = subprocess.run([sys.executable, str(SCRIPT), "build"], capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(json.loads(proc.stdout.decode("utf-8"))["ok"])

    def test_run_with_a_stand_in_for_claude(self):
        fake_claude = write(self.base / "fake_claude.py", """
            import json, os, sys
            print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "nested": os.environ.get("CLAUDECODE")}))
            """)
        res = ca.build(Args(workspace=str(self.ws), parts_from_table=str(self.agents / "helper-a.agent.md"),
                            claude_bin=[sys.executable, str(fake_claude)]))
        self.assertTrue(res["ok"], res)
        proc = subprocess.run([sys.executable, str(SCRIPT), "run", "--workspace", str(self.ws),
                               "--prompt", "一行で答えて", "--timeout", "60"],
                              capture_output=True, env=dict(ENV, CLAUDECODE="1"), timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        result = json.loads(proc.stdout.decode("utf-8"))
        self.assertTrue(result["ok"])
        seen = json.loads(result["stdout"])
        self.assertEqual(seen["argv"][:2], ["-p", "一行で答えて"])
        self.assertEqual(Path(seen["cwd"]).resolve(), self.ws.resolve())
        self.assertIsNone(seen["nested"])


    def test_inspect_reads_the_init_event(self):
        fake_claude = write(self.base / "fake_stream.py", """
            import json, sys
            init = {"type": "system", "subtype": "init", "model": "m", "permissionMode": "acceptEdits",
                    "tools": ["Read", "Edit", "Write", "Bash", "Skill"],
                    "skills": ["notes-skill"], "agents": ["helper-a"],
                    "mcp_servers": [{"name": "fake-server", "status": "failed"}]}
            print(json.dumps(init))
            print(json.dumps({"type": "result", "result": "ok", "total_cost_usd": 0.001, "num_turns": 1}))
            """)
        res = ca.build(Args(workspace=str(self.ws), parts_from_table=str(self.agents / "helper-a.agent.md"),
                            claude_bin=[sys.executable, str(fake_claude)]))
        self.assertTrue(res["ok"], res)
        run = ca.run_saved(self.ws, "x", 60, inspect=True)
        self.assertTrue(run["ok"], run)
        self.assertIn("--output-format", run["argv"])
        self.assertEqual(run["result"], "ok")
        self.assertEqual(run["init"]["tools"], ["Read", "Edit", "Write", "Bash", "Skill"])
        joined = "\n".join(run["checks"])
        self.assertIn("Glob", joined)
        self.assertIn("Grep", joined)
        self.assertNotIn("AskUserQuestion", joined, "claude -p never offers it (design 9.3)")
        self.assertIn("fake-server", joined)
        self.assertIn("probe-skill", joined)

    def test_inspect_without_init(self):
        self.assertTrue(ca.inspect_stream("not json\n", self.ws, "helper-a")["checks"])


class EncodingTest(unittest.TestCase):
    def test_unencodable_chars_are_escaped(self):
        self.assertEqual(ca._escape_unencodable("aé😀", "ascii"), "a\\u00e9\\ud83d\\ude00")
        self.assertEqual(json.loads('"' + ca._escape_unencodable("日本😀", "cp932") + '"'), "日本😀")

    def test_pythonioencoding_is_honored(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "collect", "存在しない.agent.md"],
                              capture_output=True, env=dict(ENV, PYTHONIOENCODING="ascii"), timeout=60)
        self.assertEqual(proc.returncode, 1)
        text = proc.stdout.decode("ascii")
        self.assertIn("\\u", text)
        self.assertFalse(json.loads(text)["ok"])


@unittest.skipUnless(any(REAL_AGENTS.glob("*.agent.md")), "このプラグインにエージェントの定義がない")
class RealAgentsTest(unittest.TestCase):
    """The real definitions can be read and converted; names come from their tables."""

    def test_each_definition_converts(self):
        for path in sorted(REAL_AGENTS.glob("*.agent.md")):
            with self.subTest(agent=path.name):
                agent = ca.load_agent(path)
                self.assertTrue(agent["table"])
                definition, problems = ca.convert_agent(agent, [])
                self.assertEqual(problems, [], "every Copilot tool must have a Claude Code mapping")
                self.assertTrue(definition["prompt"].startswith(ca.GLOSSARY))
                self.assertNotEqual(definition["model"], "inherit", "a claude-* model should be in the list")

    def test_collect_from_each_definition(self):
        layout = ca.Layout(PLUGIN_ROOT)
        for path in sorted(REAL_AGENTS.glob("*.agent.md")):
            with self.subTest(agent=path.name):
                own = path.name[: -len(ca.AGENT_SUFFIX)]
                got = ca.collect_parts(path, layout)
                self.assertEqual(got["agents"][0]["name"], own)
                named = {r["part"] for a in got["agents"] for r in a["table"]}
                for agent in got["agents"][1:]:
                    self.assertIn(agent["name"], named, "only agents named in a table are collected")
                self.assertNotIn(own, named, "an agent does not name itself in its table")
                for name in named:
                    found = (name in {a["name"] for a in got["agents"]} or name in got["skills"]
                             or name in got["mcp"] or name in {m["name"] for m in got["missing"]})
                    self.assertTrue(found, name)


if __name__ == "__main__":
    unittest.main()
