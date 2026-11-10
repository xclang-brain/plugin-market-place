"""Tests for claude_adapter.py (no model is called).

Run from the plugin folder:  python3 -m unittest discover -s tests/eval -p "test_*.py"
The fake plugin below uses made-up part names; the real agents are read from the
plugin folder, and their names come from their tables only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE
REAL_AGENTS = PLUGIN_ROOT / "com.github.copilot" / "agents"
REAL_CLAUDE_AGENTS = PLUGIN_ROOT / "agents"
SCRIPT = HERE / "claude_adapter.py"
HAS_CLAUDE = shutil.which("claude") is not None

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


def make_fake_claude_plugin(root: Path) -> Path:
    """The Claude Code form of the fake plugin (agents/<name>.md, manifest, .mcp.json) next to the Copilot one."""
    make_fake_plugin(root)
    agents = root / "agents"
    write(agents / "lead.md", """
        ---
        name: lead
        description: "Use this agent when 試しのまとめ役が要るとき。"
        model: sonnet
        color: blue
        disallowedTools: Edit, Write, mcp__plugin_fake-kit_fake-server__fake_write
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
    write(agents / "helper-a.md", """
        ---
        name: helper-a
        description: 'Use this agent when 試しの作業役が要るとき（''引用''）。'
        model: haiku
        color: green
        tools: Read, Grep, Glob, Edit, Write, Bash, Skill, AskUserQuestion, mcp__plugin_fake-kit_fake-server__fake_read, mcp__fake-server__fake_read
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
    write(root / "skills" / "unused-skill" / "SKILL.md", """
        ---
        name: unused-skill
        description: どの表にもないスキル。
        ---

        # unused-skill
        """)
    write(root / ".claude-plugin" / "plugin.json", json.dumps({
        "name": "fake-kit", "version": "9.8.7", "description": "試しのプラグイン", "license": "MIT",
        "skills": "./more-skills/"}, ensure_ascii=False))
    write(root / ".mcp.json", json.dumps({"mcpServers": {"fake-server": {
        "type": "stdio", "command": "uv",
        "args": ["run", "--native-tls", "--frozen", "--quiet", "--directory",
                 "${CLAUDE_PLUGIN_ROOT}/mcp-servers/fake-server", "fake-server-mcp"],
        "env": {"COV_USER": "${COV_USER:-}", "HTTPS_PROXY": "${HTTPS_PROXY:-}"}}}}))
    return agents


class Args:
    """A stand-in for argparse.Namespace with the build defaults."""

    def __init__(self, **kw):
        defaults = dict(workspace=None, parts_from_table=None, agent=None, skill=None, mcp=None,
                        plugin_root=None, main=None, output_dir=None, fake_data=None, mcp_env=None,
                        model=None, agent_model=None, prompt=None, extra_arg=None, claude_bin=None,
                        allow_missing=False, run=False, inspect=False, timeout=30.0,
                        tools_from_definitions=False, main_model=None)
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
        self.base = Path(self.tmp.name).resolve()  # a long path on Windows, like the code returns
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


class RealAgentsTest(unittest.TestCase):
    """The real definitions can be read and converted; names come from their tables."""

    def setUp(self):
        self.assertTrue(any(REAL_AGENTS.glob("*.agent.md")), "このプラグインにエージェントの定義がない")

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


class ClaudePluginFormatTest(unittest.TestCase):
    """--format claude-plugin: the Claude Code parts are copied as they are into a temporary plugin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "plugin"
        self.agents = make_fake_claude_plugin(self.root)
        self.ws = self.base / "ws"
        self.ws.mkdir()
        self.dest = self.base / "made-plugin"

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **kw) -> dict:
        kw.setdefault("workspace", str(self.ws))
        kw.setdefault("plugin_dir", str(self.dest))
        return ca.build(Args(format=ca.FORMAT_PLUGIN, **kw))

    def test_layout_reads_the_claude_form(self):
        layout = ca.Layout(self.root, flavor="claude")
        self.assertEqual(layout.agent_file("helper-a"), self.agents / "helper-a.md")
        self.assertIsNone(layout.agent_file("lead.agent"))
        self.assertEqual(layout.mcp_folder("fake-server"), (self.root / "mcp-servers" / "fake-server").resolve(),
                         "${CLAUDE_PLUGIN_ROOT} in .mcp.json points at the plugin folder")
        got = ca.collect_parts(self.agents / "lead.md", layout)
        self.assertEqual([a["name"] for a in got["agents"]], ["lead", "helper-a"])
        self.assertEqual(sorted(got["skills"]), ["notes-skill", "probe-skill", "run-steps"])
        self.assertEqual(got["missing"], [])
        self.assertEqual(ca.load_agent(self.agents / "helper-a.md")["name"], "helper-a")
        self.assertEqual(ca.guess_plugin_root(self.agents / "lead.md"), self.root.resolve())
        with self.assertRaises(ca.AdapterError):
            ca.Layout(self.root, flavor="other")

    def test_build_copies_the_parts_as_they_are(self):
        fake = write(self.base / "fake-issues.yaml", "issues: []\n")
        out = self.base / "out"
        res = self.build(parts_from_table=str(self.agents / "lead.md"), fake_data=str(fake), output_dir=str(out),
                         mcp_env=["UV_PROJECT_ENVIRONMENT=/tmp/venv"], prompt="List the tools")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["format"], ca.FORMAT_PLUGIN)
        self.assertEqual(Path(res["plugin_dir"]), self.dest.resolve())
        self.assertEqual(res["plugin_name"], "fake-kit")
        self.assertEqual(res["agent_ref"], "fake-kit:lead")

        manifest = json.loads((self.dest / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["version"]), ("fake-kit", "9.8.7"))
        self.assertNotIn("skills", manifest, "path keys are dropped: the copy uses the default places")
        for name in ("lead", "helper-a"):
            self.assertEqual((self.dest / "agents" / f"{name}.md").read_bytes(), (self.agents / f"{name}.md").read_bytes(),
                             "the Claude Code definition is copied byte for byte")
        self.assertEqual(sorted(p.name for p in (self.dest / "agents").iterdir()), ["helper-a.md", "lead.md"])
        self.assertEqual(sorted(p.name for p in (self.dest / "skills").iterdir()),
                         ["notes-skill", "probe-skill", "run-steps"], "only the skills of the tables")
        self.assertFalse((self.dest / "skills" / "run-steps" / "__pycache__").exists())
        self.assertTrue((self.dest / "mcp-servers" / "fake-server" / "uv.lock").is_file())
        self.assertFalse((self.dest / "mcp.json").exists(), "Claude Code reads .mcp.json only")
        self.assertFalse((self.dest / "com.github.copilot").exists())

        server = json.loads((self.dest / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["fake-server"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/mcp-servers/fake-server", server["args"])
        self.assertEqual(server["args"][-1], "fake-server-mcp")
        self.assertEqual(server["env"]["COVERITY_API"], "fake")
        self.assertEqual(server["env"]["COVERITY_FAKE_DATA"], str(fake.resolve()))
        self.assertEqual(server["env"]["COV_USER"], "eval-user")
        self.assertEqual(server["env"]["HTTPS_PROXY"], "${HTTPS_PROXY:-}", "the other variables stay as written")
        self.assertEqual(server["env"]["UV_PROJECT_ENVIRONMENT"], "/tmp/venv")
        self.assertEqual(res["mcp_servers"], {"fake-server": str(self.dest / "mcp-servers" / "fake-server")})
        self.assertEqual(res["skills"]["probe-skill"], str(self.dest / "skills" / "probe-skill"))

        argv = res["command"]
        self.assertEqual(argv[:3], ["claude", "-p", "List the tools"])
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(self.dest.resolve()))
        self.assertEqual(argv[argv.index("--agent") + 1], "fake-kit:lead")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        for flag in ("--agents", "--mcp-config", "--strict-mcp-config", "--model", "--permission-mode"):
            self.assertNotIn(flag, argv, "no acceptEdits: the writes are allowed in the output folder only, as for a user")
        allowed = argv[argv.index("--allowedTools") + 1: argv.index("--add-dir")]
        self.assertEqual(allowed, ["Bash(uv run *)", "Skill(fake-kit:*)", ca.plugin_read_rule(self.dest),
                                   ca.output_edit_rule(out), "mcp__plugin_fake-kit_fake-server__*"],
                         "the rules a user is asked for: the skills ask before use, their references are outside the "
                         "folder, and only the output folder is written")
        for bare in ("Write", "Edit"):
            self.assertNotIn(bare, allowed)
        if os.name == "nt":
            for rule in allowed[2:4]:
                self.assertRegex(rule, r"^(Read|Edit)\(//[a-z]/", "the drive form //c/... in the full argv")
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(out.resolve()))
        self.assertNotIn("--tools", argv, "by default the plugin runs with Claude Code's own tools, like a user's session")
        self.assertIsNone(res["tools"])
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()),
                         sorted([".claude-eval-plugin", ".claude-plugin", ".mcp.json", "agents", "mcp-servers", "skills"]))
        self.assertEqual(json.loads((self.dest / ca.PLUGIN_MARKER).read_text(encoding="utf-8")), {"made_by": "claude_adapter"})

        self.assertEqual(sorted(p.name for p in (self.ws / ".claude").iterdir()), ["eval"],
                         "no agents or skills in the workspace: they come from the plugin")
        saved = json.loads((self.ws / ".claude" / "eval" / "command.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["format"], ca.FORMAT_PLUGIN)
        self.assertEqual(saved["argv"][saved["prompt_index"]], ca.PROMPT_PLACEHOLDER)
        self.assertEqual(saved["expect"]["main"], "fake-kit:lead")
        self.assertEqual(saved["expect"]["tools"], [],
                         "a main agent without a tools line takes Claude Code's default set: nothing is expected")
        self.assertEqual(saved["expect"]["mcp_servers"], ["plugin:fake-kit:fake-server"])
        listed = self.build(parts_from_table=str(self.agents / "lead.md"), tools_from_definitions=True)
        argv = listed["command"]
        self.assertEqual(argv[argv.index("--tools") + 1].split(","), ca.BASE_TOOLS,
                         "--tools-from-definitions: the lead has no tools line, the base set (the union keeps the helper's)")
        saved = json.loads((self.ws / ".claude" / "eval" / "command.json").read_text(encoding="utf-8"))
        self.assertNotIn("Edit", saved["expect"]["tools"], "disallowedTools of the main agent are not expected")
        self.assertIn("Agent", saved["expect"]["tools"])
        self.assertEqual(saved["expect"]["skills"], ["notes-skill", "probe-skill", "run-steps"])
        rows = {r["name"]: r for r in res["agents"]}
        self.assertEqual(rows["helper-a"]["skills"], ["notes-skill", "probe-skill"])
        self.assertEqual(rows["helper-a"]["model"], "haiku")
        self.assertIsNone(rows["lead"]["tools"])
        self.assertIn("Edit", rows["lead"]["disallowed_tools"])
        self.assertIn("'Bash(uv run *)'", res["command_line_bash"])

    def test_plugin_read_rule(self):
        if os.name == "nt":
            self.assertEqual(ca.plugin_read_rule(Path("C:/a/b")), "Read(//c/a/b/**)")
            self.assertEqual(ca.output_edit_rule(Path("C:/a/out")), "Edit(//c/a/out/**)")
        else:
            here = Path(__file__).resolve().parent
            self.assertEqual(ca.plugin_read_rule(here), f"Read(/{here.as_posix()}/**)")
            self.assertTrue(ca.plugin_read_rule(here).startswith("Read(//"))
            self.assertEqual(ca.output_edit_rule(here), f"Edit(/{here.as_posix()}/**)")

    def test_plugin_argv_without_output_dir_writes_nowhere(self):
        argv = ca.plugin_argv(["claude"], "p", self.dest, "fake-kit:lead", "fake-kit", ["fake-server"], None, None, [])
        self.assertNotIn("--add-dir", argv)
        self.assertNotIn("--permission-mode", argv)
        self.assertFalse([a for a in argv if a.startswith(("Edit", "Write"))], argv)

    def test_build_stage2_shape_and_options(self):
        res = self.build(parts_from_table=str(self.agents / "helper-a.md"), model="claude-haiku-5-5")
        self.assertTrue(res["ok"], res)
        self.assertEqual([a["name"] for a in res["agents"]], ["helper-a"])
        self.assertEqual(sorted(res["skills"]), ["notes-skill", "probe-skill"])
        argv = res["command"]
        self.assertEqual(argv[argv.index("--agent") + 1], "fake-kit:helper-a")
        self.assertNotIn("--tools", argv, "the default leaves Claude Code's own set")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-5-5")
        self.assertEqual((self.dest / "agents" / "helper-a.md").read_bytes(), (self.agents / "helper-a.md").read_bytes(),
                         "--model is for the session; the definition stays as it is")
        saved = json.loads((self.ws / ".claude" / "eval" / "command.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["expect"]["tools"], ["Read", "Grep", "Glob", "Edit", "Write", "Bash", "Skill",
                                                    "AskUserQuestion", "mcp__plugin_fake-kit_fake-server__fake_read",
                                                    "mcp__fake-server__fake_read"],
                         "a tools line is still expected without --tools: a tool missing from the default set shows")
        with_tools = self.build(parts_from_table=str(self.agents / "helper-a.md"), tools_from_definitions=True)
        tools = with_tools["command"][with_tools["command"].index("--tools") + 1].split(",")
        self.assertEqual(tools, ["Read", "Grep", "Glob", "Edit", "Write", "Bash", "Skill", "AskUserQuestion"],
                         "the built-in tools of the helper's tools line, no MCP names")

    def test_agent_model_changes_only_the_model_line(self):
        res = self.build(parts_from_table=str(self.agents / "lead.md"), agent_model="inherit")
        self.assertTrue(res["ok"], res)
        for name in ("lead", "helper-a"):
            old = (self.agents / f"{name}.md").read_text(encoding="utf-8").split("\n")
            new = (self.dest / "agents" / f"{name}.md").read_text(encoding="utf-8").split("\n")
            self.assertEqual(len(old), len(new))
            changed = [(a, b) for a, b in zip(old, new) if a != b]
            self.assertEqual(len(changed), 1, changed)
            self.assertTrue(changed[0][0].startswith("model: "))
            self.assertEqual(changed[0][1], "model: inherit")
        self.assertEqual({r["model"] for r in res["agents"]}, {"inherit"})

    def test_main_model_changes_only_the_main_agent(self):
        res = self.build(parts_from_table=str(self.agents / "lead.md"), model="claude-opus-5-5", main_model="claude-opus-5-5")
        self.assertTrue(res["ok"], res)
        self.assertIn("model: claude-opus-5-5", (self.dest / "agents" / "lead.md").read_text(encoding="utf-8").split("\n"))
        self.assertEqual((self.dest / "agents" / "helper-a.md").read_bytes(), (self.agents / "helper-a.md").read_bytes(),
                         "the other agents keep their own model line")
        rows = {r["name"]: r for r in res["agents"]}
        self.assertEqual((rows["lead"]["model"], rows["helper-a"]["model"]), ("claude-opus-5-5", "haiku"))
        both = self.build(parts_from_table=str(self.agents / "lead.md"), main_model="opus", agent_model="inherit")
        rows = {r["name"]: r for r in both["agents"]}
        self.assertEqual((rows["lead"]["model"], rows["helper-a"]["model"]), ("opus", "inherit"),
                         "--main-model wins over --agent-model for the main agent")
        proc = subprocess.run([sys.executable, str(SCRIPT), "build", "--format", "claude-plugin", "--workspace", str(self.ws),
                               "--plugin-dir", str(self.dest), "--parts-from-table", str(self.agents / "lead.md"),
                               "--main-model", "sonnet", "--tools-from-definitions"], capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual({r["name"]: r["model"] for r in data["agents"]}, {"lead": "sonnet", "helper-a": "haiku"})
        self.assertIn("--tools", data["command"])

    def test_with_model(self):
        self.assertEqual(ca.with_model("---\nname: a\nmodel: haiku\n---\nbody model: x\n", "sonnet"),
                         "---\nname: a\nmodel: sonnet\n---\nbody model: x\n")
        self.assertEqual(ca.with_model("---\nname: a\n---\nbody\n", "opus"), "---\nname: a\nmodel: opus\n---\nbody\n")
        self.assertEqual(ca.with_model("---\r\nmodel: haiku\r\n---\r\nb\r\n", "x"), "---\r\nmodel: x\r\n---\r\nb\r\n")
        for bad in ("no frontmatter\n", "---\nmodel: a\n"):
            with self.subTest(bad=bad), self.assertRaises(ca.AdapterError):
                ca.with_model(bad, "x")

    def test_tool_list(self):
        self.assertEqual(ca.tool_list("Read, Grep ,mcp__a__b"), ["Read", "Grep", "mcp__a__b"])
        self.assertEqual(ca.tool_list(["Read", " Edit "]), ["Read", "Edit"])
        self.assertEqual(ca.tool_list(None), [])

    def test_plugin_folder_rules(self):
        table = str(self.agents / "helper-a.md")
        for bad in (self.root / "inside", self.ws / "inside", self.base):
            with self.subTest(dest=bad), self.assertRaises(ca.AdapterError):
                self.build(parts_from_table=table, plugin_dir=str(bad))
        self.assertFalse((self.root / "inside").exists())
        busy = self.base / "busy"
        write(busy / "keep.txt", "人のファイル\n")
        with self.assertRaises(ca.AdapterError):
            self.build(parts_from_table=table, plugin_dir=str(busy))
        self.assertTrue((busy / "keep.txt").is_file(), "a folder that is not a made plugin is never emptied")
        real = self.base / "real-plugin"
        write(real / ".claude-plugin" / "plugin.json", json.dumps({"name": "someone-else", "version": "1.0.0"}))
        write(real / "src" / "code.py", "print('人のコード')\n")
        with self.assertRaises(ca.AdapterError) as err:
            self.build(parts_from_table=table, plugin_dir=str(real))
        self.assertIn(ca.PLUGIN_MARKER, str(err.exception))
        self.assertTrue((real / "src" / "code.py").is_file(), "a real plugin (manifest, no marker) is never emptied")
        self.assertTrue((real / ".claude-plugin" / "plugin.json").is_file())
        write(real / ca.PLUGIN_MARKER, json.dumps({"made_by": "someone"}))
        with self.assertRaises(ca.AdapterError):
            self.build(parts_from_table=table, plugin_dir=str(real))
        self.assertTrue((real / "src" / "code.py").is_file(), "a marker of someone else does not count")
        proc = subprocess.run([sys.executable, str(SCRIPT), "build", "--format", "claude-plugin", "--workspace", str(self.ws),
                               "--plugin-dir", str(real), "--parts-from-table", table], capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(json.loads(proc.stdout.decode("utf-8"))["ok"])
        self.assertTrue((real / "src" / "code.py").is_file())
        empty = self.base / "empty"
        empty.mkdir()
        self.assertTrue(self.build(parts_from_table=table, plugin_dir=str(empty))["ok"], "an empty folder is used")
        self.assertTrue((empty / ca.PLUGIN_MARKER).is_file())
        self.assertTrue(self.build(parts_from_table=table)["ok"])
        write(self.dest / "skills" / "stale" / "SKILL.md", "---\nname: stale\ndescription: x\n---\n")
        self.assertTrue(self.build(parts_from_table=table)["ok"])
        self.assertFalse((self.dest / "skills" / "stale").exists(), "a new build starts from an empty folder")
        default = ca.build(Args(format=ca.FORMAT_PLUGIN, workspace=str(self.ws), parts_from_table=table))
        self.assertEqual(Path(default["plugin_dir"]), (self.base / "claude-plugin").resolve(),
                         "default: next to the workspace")

    def test_needs_the_claude_definitions(self):
        with self.assertRaises(ca.AdapterError) as err:
            self.build(parts_from_table=str(self.root / "com.github.copilot" / "agents" / "helper-a.agent.md"))
        self.assertIn("agents/<名前>.md", str(err.exception))
        (self.root / ".claude-plugin" / "plugin.json").unlink()
        with self.assertRaises(ca.AdapterError):
            self.build(parts_from_table=str(self.agents / "helper-a.md"))
        with self.assertRaises(ca.AdapterError):
            ca.build(Args(format="other", workspace=str(self.ws), parts_from_table=str(self.agents / "helper-a.md")))

    def test_leftover_workspace_parts_are_reported(self):
        (self.ws / ".claude" / "agents").mkdir(parents=True)
        res = self.build(parts_from_table=str(self.agents / "helper-a.md"))
        self.assertTrue(any(".claude/agents/" in p for p in res["problems"]), res["problems"])

    def test_cli_build_and_collect(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "collect", "--format", "claude-plugin",
                               str(self.agents / "lead.md")], capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual((data["format"], [a["name"] for a in data["agents"]]), ("claude-plugin", ["lead", "helper-a"]))
        proc = subprocess.run([sys.executable, str(SCRIPT), "build", "--format", "claude-plugin", "--workspace", str(self.ws),
                               "--plugin-dir", str(self.dest), "--parts-from-table", str(self.agents / "helper-a.md")],
                              capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["agent_ref"], "fake-kit:helper-a")
        proc = subprocess.run([sys.executable, str(SCRIPT), "build", "--format", "nope", "--workspace", str(self.ws),
                               "--parts-from-table", str(self.agents / "helper-a.md")],
                              capture_output=True, env=ENV, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(json.loads(proc.stdout.decode("utf-8"))["ok"])

    def test_run_and_inspect_with_a_stand_in(self):
        fake_claude = write(self.base / "fake_plugin_stream.py", """
            import json, sys
            argv = sys.argv[1:]
            plugin = argv[argv.index("--plugin-dir") + 1]
            agent = argv[argv.index("--agent") + 1]
            init = {"type": "system", "subtype": "init", "model": "m", "permissionMode": "acceptEdits",
                    "tools": ["Read", "Grep", "Glob", "Edit", "Write", "Bash", "Skill",
                              "mcp__plugin_fake-kit_fake-server__fake_read"],
                    "skills": ["fake-kit:notes-skill", "fake-kit:probe-skill"], "agents": [agent],
                    "mcp_servers": [{"name": "plugin:fake-kit:fake-server", "status": "connected"}]}
            print(json.dumps(init))
            print(json.dumps({"type": "result", "result": plugin, "total_cost_usd": 0.0, "num_turns": 1}))
            """)
        res = self.build(parts_from_table=str(self.agents / "helper-a.md"), claude_bin=[sys.executable, str(fake_claude)],
                         prompt="x", run=True, inspect=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["run"]["checks"], [], "plugin names with the plugin name in front count")
        self.assertEqual(res["run"]["result"], str(self.dest.resolve()))
        lacking = ca.inspect_stream(json.dumps({"type": "system", "subtype": "init", "tools": ["Read"], "skills": [],
                                                "agents": [], "mcp_servers": []}),
                                    self.ws, "fake-kit:helper-a", {"tools": ["Read", "Grep"], "skills": ["probe-skill"]})
        joined = "\n".join(lacking["checks"])
        for word in ("Grep", "probe-skill", "fake-kit:helper-a"):
            self.assertIn(word, joined)

    def test_inspect_needs_the_plugin_server(self):
        expect = {"tools": [], "skills": [], "mcp_servers": ["plugin:fake-kit:fake-server"]}

        def init(servers):
            return json.dumps({"type": "system", "subtype": "init", "tools": ["Read"], "skills": [],
                               "agents": ["fake-kit:helper-a"], "mcp_servers": servers})

        none = ca.inspect_stream(init([]), self.ws, "fake-kit:helper-a", expect)
        self.assertEqual(len(none["checks"]), 1, none["checks"])
        self.assertIn("plugin:fake-kit:fake-server", none["checks"][0])
        failed = ca.inspect_stream(init([{"name": "plugin:fake-kit:fake-server", "status": "failed"}]),
                                   self.ws, "fake-kit:helper-a", expect)
        self.assertEqual(len(failed["checks"]), 1, failed["checks"])
        self.assertIn("failed", failed["checks"][0])
        other = ca.inspect_stream(init([{"name": "plugin:fake-kit:fake-server", "status": "connected"},
                                        {"name": "users-broken", "status": "failed"}]), self.ws, "fake-kit:helper-a", expect)
        self.assertEqual(other["checks"], [], "a server that is not the plugin's is not checked")
        self.assertEqual(other["init"]["extra_mcp_servers"], [{"name": "users-broken", "status": "failed"}])
        hand = ca.inspect_stream(init([{"name": "fake-server", "status": "connected"}]), self.ws, "fake-kit:helper-a", expect)
        self.assertEqual(len(hand["checks"]), 1, "a hand-registered server of the same name does not stand in for the plugin's")
        self.assertEqual(hand["init"]["extra_mcp_servers"], [{"name": "fake-server", "status": "connected"}])
        other = ca.inspect_stream(init([{"name": "plugin:fake-kit-copy:fake-server", "status": "connected"}]),
                                  self.ws, "fake-kit:helper-a", expect)
        self.assertEqual(other["checks"], [], "a plugin-provided server of the same last ':' part counts")
        no_expect = ca.inspect_stream(init([]), self.ws, "fake-kit:helper-a", {"tools": [], "skills": []})
        self.assertEqual(no_expect["checks"], [], "no server expected (no MCP server in the tables): nothing to check")


class RealClaudePluginTest(unittest.TestCase):
    """The real Claude Code definitions (agents/<name>.md) build a temporary plugin of their parts only."""

    def setUp(self):
        self.assertTrue((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").is_file() and REAL_CLAUDE_AGENTS.is_dir(),
                        "Claude Code の形（.claude-plugin/plugin.json と agents/）がない")
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.ws = self.base / "ws"
        self.ws.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_claude_definition_collects(self):
        layout = ca.Layout(PLUGIN_ROOT, flavor="claude")
        for path in sorted(REAL_CLAUDE_AGENTS.glob("*.md")):
            with self.subTest(agent=path.name):
                got = ca.collect_parts(path, layout)
                self.assertEqual(got["agents"][0]["name"], path.stem)
                self.assertEqual(got["missing"], [], "every part of the table is in the plugin")
                copilot = REAL_AGENTS / f"{path.stem}{ca.AGENT_SUFFIX}"
                if copilot.is_file():
                    self.assertEqual(ca.load_agent(path)["table"], ca.load_agent(copilot)["table"],
                                     "both definitions have the same table")

    def build(self, agent: Path, dest: Path) -> dict:
        return ca.build(Args(format=ca.FORMAT_PLUGIN, workspace=str(self.ws), parts_from_table=str(agent),
                             plugin_dir=str(dest), fake_data=None))

    def check_built(self, agent: Path, dest: Path) -> dict:
        res = self.build(agent, dest)
        self.assertTrue(res["ok"], res)
        collected = ca.collect_parts(agent, ca.Layout(PLUGIN_ROOT, flavor="claude"))
        self.assertEqual(sorted(p.stem for p in (dest / "agents").iterdir()), sorted(a["name"] for a in collected["agents"]))
        self.assertEqual(sorted(p.name for p in (dest / "skills").iterdir()), sorted(collected["skills"]),
                         "only the skills of the tables (Claude Code loads every skill in skills/)")
        for a in collected["agents"]:
            self.assertEqual((dest / "agents" / f"{a['name']}.md").read_bytes(), a["path"].read_bytes())
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        copied = json.loads((dest / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((copied["name"], copied["version"]), (manifest["name"], manifest["version"]))
        self.assertEqual(res["agent_ref"], f"{manifest['name']}:{agent.stem}")
        if not collected["mcp"]:
            self.assertFalse((dest / ".mcp.json").exists(), "no MCP server in the tables: no .mcp.json")
        servers = json.loads((dest / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"] if collected["mcp"] else {}
        self.assertEqual(sorted(servers), sorted(collected["mcp"]))
        for name, server in servers.items():
            self.assertTrue(any(a.startswith("${CLAUDE_PLUGIN_ROOT}/") for a in server["args"]), server)
            self.assertTrue((dest / "mcp-servers" / name / "pyproject.toml").is_file())
            self.assertFalse((dest / "mcp-servers" / name / ".venv").exists())
        allowed = res["command"][res["command"].index("--allowedTools") + 1:]
        for name in servers:
            self.assertIn(f"mcp__plugin_{manifest['name']}_{name}__*", allowed)
        return res

    def validate(self, dest: Path) -> None:
        if not HAS_CLAUDE:
            return
        proc = subprocess.run(["claude", "plugin", "validate", str(dest)], capture_output=True, env=ENV, timeout=120,
                              cwd=str(self.base), stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace"))

    def test_stage2_and_stage3_plugins(self):
        tables = {}
        for path in sorted(REAL_CLAUDE_AGENTS.glob("*.md")):
            tables[path.stem] = {r["part"] for r in ca.load_agent(path)["table"]}
        agent_names = set(tables)
        # stage 3 = the agent whose table names other agents; stage 2 = an agent it names whose table names none
        leads = [n for n, parts in tables.items() if parts & agent_names]
        self.assertEqual(len(leads), 1, leads)
        workers = [n for n in tables[leads[0]] & agent_names if not tables[n] & agent_names]
        self.assertTrue(workers)
        for name in [leads[0]] + workers:
            with self.subTest(agent=name):
                dest = self.base / f"plugin-{name}"
                res = self.check_built(REAL_CLAUDE_AGENTS / f"{name}.md", dest)
                self.assertEqual(len(res["agents"]), 1 + len(tables[name] & agent_names))
                self.validate(dest)

    @unittest.skipUnless(HAS_CLAUDE, "claude（Claude Code）がない")
    def test_claude_validates_the_copied_plugin(self):
        dest = self.base / "plugin-all"
        lead = next(p for p in sorted(REAL_CLAUDE_AGENTS.glob("*.md"))
                    if {r["part"] for r in ca.load_agent(p)["table"]} & {q.stem for q in REAL_CLAUDE_AGENTS.glob("*.md")})
        self.check_built(lead, dest)
        self.validate(dest)


if __name__ == "__main__":
    unittest.main()
