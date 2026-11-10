"""プラグインの形を確かめる試験（設計書 9.1「構造」、仕様 REQ-101）。

- plugin.json・mcp.json を、Agent Plugins 1.0.0 の公式の JSON Schema（schemas/ の無加工の写し）で確かめる。
  schema では表せない決まり（mcp.json の版、起動のコマンド、フォルダ、認証情報の環境変数の渡し方）も確かめる。
- すべての SKILL.md の frontmatter（Agent Skills の決まりと設計書 3.1）、本文の長さ、SKILL.md が指すファイル、
  references/ の目次、README・試験・試験の課題（evals/、skill-creator の形）がフォルダの中にあること。
- エージェントの定義（com.github.copilot/agents/ の .md は .agent.md だけ、frontmatter の項目、統括エージェントに tools がない）。
- Claude Code の形（同じフォルダ）：plugin.json の $schema は Agent Plugins 1.0.0 のまま（Copilot CLI が agents/ と .mcp.json を
  読まないように）。.claude-plugin/plugin.json の name と version、.mcp.json と mcp.json の対応、agents/ の .md はエージェントだけで
  plugin-dev の決まりの frontmatter。リポジトリの2つのマーケットプレイスの名前と版、根の marketplace.json と .plugin/ がないこと。
  claude コマンドがあれば claude plugin validate。
名前は、フォルダ・mcp.json と、plugin_parts.py の定数（ORCHESTRATOR など）から読みます。

  python3 -m unittest discover -s tests/structure -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_parts as pp  # noqa: E402

SLASH_COMMAND = re.compile(r"(?<![\w./])/[a-z][a-z0-9-]*")
SKILL_KEYS = {"name", "description", "allowed-tools"}
ALLOWED_TOOLS = ["shell(uv run:*)"]
RESERVED_WORDS = ("anthropic", "claude")
AGENT_KEYS = {"description", "model", "tools"}
# Copilot CLI のツールの別名（https://docs.github.com/copilot … custom agents の tools）
AGENT_TOOL_ALIASES = {
    "read", "edit", "search", "execute", "agent", "web", "todo", "skill", "ask_user",
    "shell", "bash", "powershell", "Read", "Edit", "Write", "MultiEdit", "NotebookRead", "NotebookEdit",
    "Grep", "Glob", "Task", "custom-agent", "WebSearch", "WebFetch", "TodoWrite",
}
# Claude Code のエージェントの frontmatter（plugin-dev の agent-development の決まり）
CLAUDE_AGENT_KEYS = {"name", "description", "model", "color", "tools", "disallowedTools"}
CLAUDE_MODELS = {"inherit", "sonnet", "opus", "haiku"}
CLAUDE_COLORS = {"blue", "cyan", "green", "yellow", "magenta", "red"}
CLAUDE_TOOLS = {
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead", "Grep", "Glob", "LS", "Bash", "BashOutput",
    "KillShell", "Skill", "AskUserQuestion", "Agent", "Task", "TodoWrite", "WebFetch", "WebSearch",
}
# mcp.json で MCP サーバーに渡す環境変数（設計書 3.2。Copilot CLI は PATH のほかを渡さないため）
MCP_ENV = ["COV_USER", "COV_AUTH_KEY", "HTTPS_PROXY", "NO_PROXY"]


def skill_md(name):
    return pp.SKILLS_DIR / name / "SKILL.md"


class ManifestTest(unittest.TestCase):
    """plugin.json と mcp.json（Agent Plugins 1.0.0）"""

    def schema(self, name):
        return json.loads(pp.read(pp.SCHEMAS_DIR / name))

    def test_plugin_json_matches_the_official_schema(self):
        path = pp.ROOT / "plugin.json"
        self.assertTrue(path.is_file(), "plugin.json がありません")
        data = json.loads(pp.read(path))
        self.assertEqual(pp.schema_errors(data, self.schema("plugin.schema.json")), [], "plugin.json が公式の schema に合いません")
        self.assertEqual(data["name"], pp.PLUGIN_NAME, "plugin.json の name が、プラグインのフォルダの名前と違います")
        self.assertRegex(data.get("version", ""), r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$", "plugin.json の version が x.y.z ではありません")
        self.assertTrue(data.get("description"), "plugin.json に description がありません")

    def test_mcp_json_matches_the_official_schema(self):
        path = pp.ROOT / "mcp.json"
        if not path.is_file():
            self.skipTest("mcp.json がありません")
        data = json.loads(pp.read(path))
        self.assertEqual(pp.schema_errors(data, self.schema("mcp.schema.json")), [], "mcp.json が公式の schema に合いません")
        plugin = json.loads(pp.read(pp.ROOT / "plugin.json"))

        def version(url):
            m = re.search(r"/schemas/([^/]+)/", url or "")
            return m.group(1) if m else None

        self.assertIsNotNone(version(data.get("$schema")), "mcp.json の $schema に版がありません")
        self.assertEqual(version(data.get("$schema")), version(plugin.get("$schema")), "mcp.json と plugin.json の $schema の版が違います")

    def test_mcp_servers_start_from_their_folder(self):
        """stdio の command は1語。--directory（か cwd）は ${PLUGIN_ROOT}/mcp-servers/<フォルダ> を指し、そのフォルダがある"""
        servers = pp.mcp_config().get("mcpServers") or {}
        if not servers:
            self.skipTest("mcp.json に MCP サーバーがありません")
        for name, server in servers.items():
            with self.subTest(server=name):
                if server.get("type") != "stdio":
                    continue
                self.assertRegex(server.get("command", ""), r"^[A-Za-z0-9_.\-]+$|^\./\S+$", "command は1語にします")
                for arg in server.get("args") or []:
                    for ref in re.findall(r"\$\{PLUGIN_ROOT\}(/[^\s\"']*)?", arg):
                        target = pp.ROOT / ref.lstrip("/")
                        self.assertTrue(target.exists(), f"mcp.json の {arg} が指すものがありません")
                folder = pp.mcp_registrations().get(name)
                self.assertIsNotNone(folder, f"{name} の --directory がありません")
                self.assertEqual(folder.resolve().parent, pp.MCP_DIR.resolve(), f"{name} が mcp-servers/ の下を指していません")

    def test_mcp_servers_receive_credentials(self):
        """Copilot CLI は MCP サーバーに PATH のほかの環境変数を渡さないので、mcp.json の env で渡す（設計書 3.2）"""
        servers = pp.mcp_config().get("mcpServers") or {}
        if not servers:
            self.skipTest("mcp.json に MCP サーバーがありません")
        for name, server in servers.items():
            with self.subTest(server=name):
                env = server.get("env") or {}
                missing = [k for k in MCP_ENV if env.get(k) != "${" + k + "}"]
                self.assertEqual(missing, [], f"mcp.json の {name} の env に、次の受け渡しがありません（\"名前\": \"${{名前}}\"）")

    def test_every_mcp_folder_is_registered(self):
        registered = {p.resolve() for p in pp.mcp_registrations().values() if p}
        for folder in pp.mcp_dirs():
            with self.subTest(folder=folder.name):
                self.assertIn(folder.resolve(), registered, f"mcp-servers/{folder.name} が mcp.json に登録されていません")

    def test_plugin_marketplace(self):
        path = pp.ROOT / ".github" / "plugin" / "marketplace.json"
        self.assertTrue(path.is_file(), ".github/plugin/marketplace.json がありません")
        data = json.loads(pp.read(path))
        self.assertTrue((data.get("owner") or {}).get("name"), "marketplace.json に owner.name がありません")
        names = [p.get("name") for p in data.get("plugins") or []]
        self.assertIn(pp.PLUGIN_NAME, names, "marketplace.json の plugins にこのプラグインがありません")

    def test_plugin_json_keeps_the_agent_plugins_schema(self):
        """plugin.json の $schema は、Agent Plugins 1.0.0 のものそのまま。これがないと、Copilot CLI は古い読み方になり、
        Claude Code のための agents/ と .mcp.json も読んでしまう"""
        official = self.schema("plugin.schema.json")["properties"]["$schema"]["const"]
        self.assertIn("/schemas/1.0.0/", official)
        data = json.loads(pp.read(pp.ROOT / "plugin.json"))
        self.assertEqual(data.get("$schema"), official, "plugin.json の $schema が Agent Plugins 1.0.0 のものではありません")

    def test_layout(self):
        """Copilot CLI の決まった置き場所と、Claude Code の置き場所（.claude-plugin/plugin.json、agents/、.mcp.json）だけを使う"""
        for legacy in ["commands", "rules", "hooks", "hooks.json", "lsp.json", "extensions", "marketplace.json", ".plugin",
                       ".github/plugin/plugin.json"]:
            self.assertFalse((pp.ROOT / legacy).exists(), f"{legacy} はプラグインの根に置きません（com.github.copilot/ の下に置く）")
        if pp.CLAUDE_MANIFEST.parent.is_dir():
            extra = sorted(p.name for p in pp.CLAUDE_MANIFEST.parent.iterdir() if p.name != pp.CLAUDE_MANIFEST.name)
            self.assertEqual(extra, [], ".claude-plugin/ には plugin.json だけを置きます")
        if pp.COPILOT_DIR.is_dir():
            allowed = {"agents", "commands", "rules", "hooks", "lsp.json"}
            extra = sorted(p.name for p in pp.COPILOT_DIR.iterdir() if p.name not in allowed)
            self.assertEqual(extra, [], "com.github.copilot/ の下に、決まりにないものがあります")


class SkillTest(unittest.TestCase):
    """SKILL.md（Agent Skills の決まりと設計書 3.1）"""

    def setUp(self):
        self.names = pp.skill_names()

    def test_every_skill_folder_has_skill_md(self):
        for folder in pp.skill_dirs():
            with self.subTest(skill=folder.name):
                self.assertTrue((folder / "SKILL.md").is_file(), f"skills/{folder.name}/ に SKILL.md がありません")
                extra = [p for p in folder.rglob("SKILL.md") if p != folder / "SKILL.md"
                         and not pp.skipped(p, folder) and p.relative_to(folder).parts[0] != "evals"]
                self.assertEqual(extra, [], "スキルのフォルダに SKILL.md が2つ以上あります")

    def test_frontmatter(self):
        for name in self.names:
            with self.subTest(skill=name):
                data, _, problem = pp.frontmatter(skill_md(name))
                self.assertIsNone(problem, f"{name}: {problem}")
                self.assertEqual(sorted(set(data) - SKILL_KEYS), [], f"{name}: frontmatter は name・description・allowed-tools だけにします")
                self.assertEqual(data.get("name"), name, f"{name}: name がフォルダの名前と違います")
                self.assertLessEqual(len(name), 64, f"{name}: name が64文字を超えています")
                self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$", f"{name}: name は小文字・数字・ハイフンだけにします")
                for word in RESERVED_WORDS:
                    self.assertNotIn(word, name, f"{name}: name に予約された言葉「{word}」があります")
                description = data.get("description")
                self.assertIsInstance(description, str, f"{name}: description がありません")
                self.assertTrue(description.strip(), f"{name}: description が空です")
                self.assertLessEqual(len(description), 1024, f"{name}: description が1024文字を超えています")
                self.assertNotRegex(description, r"[<>]", f"{name}: description に < や >（XML のタグ）があります")
                self.assertEqual(SLASH_COMMAND.findall(description), [],
                                 f"{name}: description にスラッシュコマンドの名前があります（名前を変えてコピーすると食い違う）")

    def test_allowed_tools_only_for_scripts(self):
        """スクリプトを動かすスキル（本文に uv run がある）だけが allowed-tools: ["shell(uv run:*)"] を持つ"""
        for name in self.names:
            with self.subTest(skill=name):
                data, body, problem = pp.frontmatter(skill_md(name))
                if problem:
                    continue
                runs = "uv run" in body
                if "allowed-tools" in data:
                    self.assertEqual(data["allowed-tools"], ALLOWED_TOOLS, f"{name}: allowed-tools は {ALLOWED_TOOLS} だけにします")
                    self.assertTrue(runs, f"{name}: スクリプトを動かさないのに allowed-tools があります")
                else:
                    self.assertFalse(runs, f"{name}: スクリプトを動かす（uv run がある）のに allowed-tools がありません")

    def test_body_is_short(self):
        for name in self.names:
            with self.subTest(skill=name):
                _, body, _ = pp.frontmatter(skill_md(name))
                self.assertLess(len(body.splitlines()), 500, f"{name}: SKILL.md の本文が500行以上あります（詳しいことは references/ へ）")

    def test_referenced_files_exist_inside_the_folder(self):
        """SKILL.md のリンクと、`scripts/…`・`references/…`・`assets/…` のパスが、フォルダの中にある"""
        for name in self.names:
            folder = pp.SKILLS_DIR / name
            text = pp.read(folder / "SKILL.md")
            problems = []
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                path = target.split("#", 1)[0]
                dest = (folder / path).resolve()
                if not dest.exists() or folder.resolve() not in [dest, *dest.parents]:
                    problems.append(f"リンク {target}")
            for span in re.findall(r"`([^`\n]+)`", text):
                for path in re.findall(r"(?:^|[\s\"'/>])((?:scripts|references|assets)/[A-Za-z0-9_.\-/]+)", span):
                    path = path.rstrip(".,:;)/")
                    if "<" in path or "*" in path:
                        continue
                    if not (folder / path).exists():
                        problems.append(f"パス {path}")
            with self.subTest(skill=name):
                self.assertEqual(problems, [], f"{name}: SKILL.md が指すものが、フォルダの中にありません")

    def test_references_are_one_level_and_have_a_toc(self):
        """references/ は1段（SKILL.md から直接たどる）。100行を超えるものは、先頭に目次がある"""
        for name in self.names:
            folder = pp.SKILLS_DIR / name / "references"
            if not folder.is_dir():
                continue
            skill_text = pp.read(pp.SKILLS_DIR / name / "SKILL.md")
            for path in sorted(folder.rglob("*")):
                if path.is_dir() or pp.skipped(path, folder):
                    continue
                with self.subTest(skill=name, file=path.name):
                    self.assertEqual(path.parent, folder, f"{pp.rel(path)}: references/ の中にフォルダを作らないでください")
                    self.assertIn(path.name, skill_text, f"{pp.rel(path)}: SKILL.md から案内されていません")
                    if path.suffix == ".md":
                        lines = pp.read(path).splitlines()
                        if len(lines) > 100:
                            head = "\n".join(lines[:40])
                            has_toc = re.search(r"目次|Contents", head) or len(re.findall(r"\]\(#", head)) >= 2
                            self.assertTrue(has_toc, f"{pp.rel(path)}: 100行を超えるのに、先頭に目次がありません")

    def test_readme_tests_and_evals_inside(self):
        """スキルは README.md・tests/・evals/ を持つ（仕様 REQ-101）"""
        for name in self.names:
            folder = pp.SKILLS_DIR / name
            with self.subTest(skill=name):
                self.assertTrue((folder / "README.md").is_file(), f"{name}: README.md がありません")
                self.assertTrue(list((folder / "tests").rglob("test*.py")), f"{name}: tests/ に試験がありません")
                self.assertTrue((folder / "evals").is_dir(), f"{name}: evals/ がありません")

    def test_eval_cases_follow_the_skill_creator_layout(self):
        """evals/<課題>/ は prompt.md（設定の欄つき）・case.yaml（schema_version "1.1"、name＝フォルダ）・graders/NN-*.md"""
        reserved = {"results", "mocks", "node_modules", "files", "true", "false", "null"}
        for name in self.names:
            evals = pp.SKILLS_DIR / name / "evals"
            if not evals.is_dir():
                continue
            cases = sorted(p for p in evals.iterdir() if p.is_dir() and p.name not in ("results",))
            with self.subTest(skill=name):
                self.assertTrue(cases or (evals / "evals.json").is_file(), f"{name}: evals/ に課題がありません")
            for case in cases:
                with self.subTest(skill=name, case=case.name):
                    self.assertRegex(case.name, r"^[a-z][a-z0-9-]*$", "課題の名前は小文字で始め、小文字・数字・ハイフンにします")
                    self.assertNotIn(case.name, reserved, "課題に使えない名前です")
                    prompt = case / "prompt.md"
                    self.assertTrue(prompt.is_file(), "prompt.md がありません")
                    raw, body = pp.split_frontmatter(pp.read(prompt))
                    self.assertIsNotNone(raw, "prompt.md の先頭に設定の欄（---）がありません")
                    self.assertTrue(body.strip(), "prompt.md に頼みの文がありません")
                    case_yaml = case / "case.yaml"
                    self.assertTrue(case_yaml.is_file(), "case.yaml がありません")
                    text = pp.read(case_yaml)
                    self.assertRegex(text, r"(?m)^schema_version:\s*[\"']1\.1[\"']\s*$", "case.yaml の schema_version は \"1.1\"（引用符つき）")
                    self.assertRegex(text, r"(?m)^name:\s*[\"']?" + re.escape(case.name) + r"[\"']?\s*$", "case.yaml の name がフォルダの名前と違います")
                    graders = sorted((case / "graders").glob("*.md")) if (case / "graders").is_dir() else []
                    self.assertTrue(graders, "graders/ に採点の文がありません")
                    for grader in graders:
                        self.assertRegex(grader.name, r"^\d{2}-[a-z0-9-]+\.md$", f"{grader.name}: NN-few-words.md の形にします")
                        self.assertIsNotNone(pp.split_frontmatter(pp.read(grader))[0], f"{grader.name}: 先頭の欄（type など）がありません")
                    if (case / "files").is_dir():
                        self.assertTrue((case / "place-files.sh").is_file(), "files/ があるのに place-files.sh がありません")
                        self.assertIn("place-files.sh", text, "case.yaml に scaffold_script: place-files.sh がありません")

    def test_mini_yaml_reads_like_pyyaml(self):
        """PyYAML がない PC でも同じに読めるよう、小さな読み手が PyYAML と同じ答えを出す（PyYAML があるときだけ）"""
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML がないため、比べられません")
        files = [skill_md(n) for n in self.names] + pp.agent_files() + pp.claude_agent_files()
        for path in files:
            raw, _ = pp.split_frontmatter(pp.read(path))
            if raw is None:
                continue
            with self.subTest(file=pp.rel(path)):
                try:
                    expected = yaml.safe_load(raw)
                except yaml.YAMLError as error:
                    self.fail(f"{pp.rel(path)}: frontmatter が YAML として読めません：{error}")
                self.assertEqual(pp.parse_simple_yaml(raw), expected)


class AgentTest(unittest.TestCase):
    """エージェントの定義（設計書 2.2、3.3、7）"""

    def test_agents_folder_holds_only_definitions_and_readmes(self):
        """agents/ の .md は、Copilot CLI がすべてエージェントの定義として読む。使い方は .README.markdown にする"""
        if not pp.AGENTS_DIR.is_dir():
            self.skipTest("com.github.copilot/agents/ がまだありません")
        for path in pp.AGENTS_DIR.iterdir():
            with self.subTest(file=path.name):
                self.assertTrue(path.is_file(), f"agents/ の中にフォルダ {path.name} があります")
                self.assertTrue(path.name.endswith(pp.AGENT_SUFFIX) or path.suffix == ".markdown",
                                f"agents/{path.name}：置けるのは <名前>.agent.md と <名前>.README.markdown だけです")

    def test_frontmatter(self):
        files = pp.agent_files()
        if not files:
            self.skipTest("エージェントの定義がまだありません")
        servers = set((pp.mcp_config().get("mcpServers") or {}))
        for path in files:
            with self.subTest(agent=path.name):
                data, body, problem = pp.frontmatter(path)
                self.assertIsNone(problem, f"{path.name}: {problem}")
                self.assertEqual(sorted(set(data) - AGENT_KEYS), [], f"{path.name}: frontmatter は description・model・tools だけにします")
                self.assertTrue(isinstance(data.get("description"), str) and data["description"].strip(),
                                f"{path.name}: description がありません")
                if "model" in data:
                    self.assertTrue(isinstance(data["model"], list) and data["model"] and all(isinstance(m, str) and m for m in data["model"]),
                                    f"{path.name}: model は候補の並び（[a, b]）にします")
                for tool in data.get("tools") or []:
                    if not isinstance(tool, str):
                        self.fail(f"{path.name}: tools の値が文字列ではありません：{tool!r}")
                    if "/" in tool:
                        server = tool.split("/", 1)[0]
                        self.assertIn(server, servers, f"{path.name}: tools の {tool} の MCP サーバーが mcp.json にありません")
                    else:
                        self.assertIn(tool, AGENT_TOOL_ALIASES, f"{path.name}: tools の {tool} は Copilot CLI のツールの名前ではありません")
                self.assertTrue(body.strip(), f"{path.name}: 本文がありません")
                self.assertLessEqual(len(body), 30000, f"{path.name}: 本文が長すぎます")

    def test_tools_line_except_orchestrator(self):
        """統括エージェントは tools を書かない。ほかのエージェントは tools を書く（設計書 3.3）"""
        for path in pp.agent_files():
            with self.subTest(agent=path.name):
                fm, _, problem = pp.frontmatter(path)
                if problem:
                    continue
                if pp.agent_stem(path) == pp.ORCHESTRATOR:
                    self.assertNotIn("tools", fm, f"{path.name}: 統括エージェントに tools があります（呼んだエージェントのツールが絞られる）")
                else:
                    self.assertIn("tools", fm, f"{path.name}: tools がありません")

    def test_every_agent_has_a_readme(self):
        """エージェントの使い方は <名前>.README.markdown（設計書 7）"""
        for path in pp.agent_files():
            with self.subTest(agent=path.name):
                readme = pp.AGENTS_DIR / (pp.agent_stem(path) + pp.AGENT_README_SUFFIX)
                self.assertTrue(readme.is_file(), f"{readme.name} がありません")


class ClaudeCodeTest(unittest.TestCase):
    """Claude Code の形（.claude-plugin/plugin.json、agents/<名前>.md、.mcp.json）。Copilot CLI の形と同じフォルダに置く"""

    def manifest(self):
        if not pp.CLAUDE_MANIFEST.is_file():
            self.fail(".claude-plugin/plugin.json がありません")
        data = pp.load_json(pp.CLAUDE_MANIFEST)
        self.assertIsInstance(data, dict, ".claude-plugin/plugin.json が JSON の object ではありません")
        return data

    def test_manifest_has_the_same_name_and_version(self):
        data = self.manifest()
        copilot = json.loads(pp.read(pp.ROOT / "plugin.json"))
        self.assertEqual(data.get("name"), pp.PLUGIN_NAME, ".claude-plugin/plugin.json の name が、フォルダの名前と違います")
        self.assertEqual(data.get("name"), copilot.get("name"), ".claude-plugin/plugin.json と plugin.json の name が違います")
        self.assertEqual(data.get("version"), copilot.get("version"), ".claude-plugin/plugin.json と plugin.json の version が違います")
        self.assertTrue(isinstance(data.get("description"), str) and data["description"].strip(),
                        ".claude-plugin/plugin.json に description がありません")

    def test_mcp_json_matches_the_copilot_one(self):
        """.mcp.json は mcp.json と同じサーバー・コマンド・引数（${PLUGIN_ROOT} → ${CLAUDE_PLUGIN_ROOT}）・env の鍵。
        env の値は ${名前:-}（Claude Code は、ない環境変数の ${名前} をそのままの文字で渡す）"""
        copilot = pp.mcp_config().get("mcpServers") or {}
        if not copilot:
            self.skipTest("mcp.json に MCP サーバーがありません")
        self.assertTrue(pp.CLAUDE_MCP.is_file(), ".mcp.json がありません")
        data = pp.load_json(pp.CLAUDE_MCP)
        self.assertIsInstance(data, dict, ".mcp.json が JSON の object ではありません")
        claude = data.get("mcpServers") or {}
        self.assertEqual(sorted(claude), sorted(copilot), ".mcp.json と mcp.json のサーバーの登録名が違います")
        self.assertNotIn("${PLUGIN_ROOT}", pp.read(pp.CLAUDE_MCP), ".mcp.json に ${PLUGIN_ROOT} があります（${CLAUDE_PLUGIN_ROOT} にする）")
        for name, server in copilot.items():
            other = claude.get(name) or {}
            with self.subTest(server=name):
                self.assertEqual(other.get("type"), server.get("type"), "type が違います")
                self.assertEqual(other.get("command"), server.get("command"), "command が違います")
                want = [a.replace("${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}") if isinstance(a, str) else a
                        for a in server.get("args") or []]
                self.assertEqual(other.get("args"), want, "args が、mcp.json の ${PLUGIN_ROOT} を ${CLAUDE_PLUGIN_ROOT} にしたものと違います")
                env = other.get("env") or {}
                self.assertEqual(sorted(env), sorted(server.get("env") or {}), "env の鍵が違います")
                wrong = [k for k, v in env.items() if v != "${" + k + ":-}"]
                self.assertEqual(wrong, [], "env の値は \"${名前:-}\" にします")

    def test_agents_folder_holds_only_the_agents(self):
        """Claude Code は agents/ の .md をすべてエージェントとして読む。README などを置かない。名前は Copilot CLI のものと同じ"""
        self.assertTrue(pp.CLAUDE_AGENTS_DIR.is_dir(), "agents/ がありません")
        for path in sorted(pp.CLAUDE_AGENTS_DIR.iterdir()):
            with self.subTest(file=path.name):
                self.assertTrue(path.is_file(), f"agents/ の中にフォルダ {path.name} があります")
                self.assertEqual(path.suffix, pp.CLAUDE_AGENT_SUFFIX, f"agents/{path.name}：置けるのはエージェントの <名前>.md だけです")
                self.assertNotEqual(path.stem.lower(), "readme", "agents/ に README を置きません（エージェントとして読まれる）")
        self.assertEqual(sorted(pp.claude_agent_names()), sorted(pp.agent_names()),
                         "agents/ のエージェントと com.github.copilot/agents/ のエージェントが違います")

    def test_agent_frontmatter(self):
        """plugin-dev の決まり：name はファイルの名前（3〜50 文字、小文字・数字・ハイフン）、description は
        「Use this agent when」で始める、model と color は決まった値、tools と disallowedTools は「A, B」の文字列"""
        files = pp.claude_agent_files()
        if not files:
            self.skipTest("agents/ にエージェントがありません")
        for path in files:
            with self.subTest(agent=path.name):
                data, body, problem = pp.frontmatter(path)
                self.assertIsNone(problem, f"{path.name}: {problem}")
                self.assertEqual(sorted(set(data) - CLAUDE_AGENT_KEYS), [],
                                 f"{path.name}: frontmatter は {sorted(CLAUDE_AGENT_KEYS)} だけにします")
                name = data.get("name")
                self.assertEqual(name, path.stem, f"{path.name}: name がファイルの名前と違います")
                self.assertTrue(3 <= len(str(name)) <= 50, f"{path.name}: name は 3〜50 文字にします")
                self.assertRegex(str(name), r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", f"{path.name}: name は小文字・数字・ハイフンにします")
                description = data.get("description")
                self.assertIsInstance(description, str, f"{path.name}: description がありません")
                self.assertTrue(description.startswith("Use this agent when"), f"{path.name}: description は「Use this agent when」で始めます")
                self.assertLessEqual(len(description), 5000, f"{path.name}: description が長すぎます")
                self.assertIn(data.get("model"), CLAUDE_MODELS, f"{path.name}: model は {sorted(CLAUDE_MODELS)} のどれかにします")
                self.assertIn(data.get("color"), CLAUDE_COLORS, f"{path.name}: color は {sorted(CLAUDE_COLORS)} のどれかにします")
                for key in ("tools", "disallowedTools"):
                    if key in data:
                        self.assertIsInstance(data[key], str, f"{path.name}: {key} は「A, B」の文字列にします")
                        self.assertTrue(pp.tool_list(data[key]), f"{path.name}: {key} が空です")
                        self.assertNotRegex(data[key], r"[\[\]\"']", f"{path.name}: {key} に [ ] や引用符を書きません")
                self.assertGreaterEqual(len(body.strip()), 20, f"{path.name}: 本文が短すぎます")
                self.assertLessEqual(len(body), 30000, f"{path.name}: 本文が長すぎます")

    def test_agent_tools_are_known(self):
        """tools・disallowedTools は、Claude Code のツールか、.mcp.json のサーバーの MCP のツール（2つの名前の形）"""
        servers = set((pp.load_json(pp.CLAUDE_MCP) or {}).get("mcpServers") or {}) if pp.CLAUDE_MCP.is_file() else set()
        known = pp.all_mcp_tool_names()
        for path in pp.claude_agent_files():
            data, _, problem = pp.frontmatter(path)
            if problem:
                continue
            for key in ("tools", "disallowedTools"):
                for tool in pp.tool_list(data.get(key)):
                    with self.subTest(agent=path.name, tool=tool):
                        parsed = pp.parse_claude_mcp_tool(tool)
                        if parsed is None:
                            self.assertIn(tool, CLAUDE_TOOLS, f"{path.name}: {key} の {tool} は Claude Code のツールの名前ではありません")
                            continue
                        server, name, _ = parsed
                        self.assertIn(server, servers, f"{path.name}: {key} の {tool} のサーバーが .mcp.json にありません")
                        if known:
                            self.assertIn(name, known, f"{path.name}: {key} の {tool} は、MCP サーバーにないツールです")

    def test_tools_line_except_orchestrator(self):
        """統括エージェントは tools を書かない（呼んだエージェントのツールが絞られないように）。ほかのエージェントは書く"""
        for path in pp.claude_agent_files():
            with self.subTest(agent=path.name):
                fm, _, problem = pp.frontmatter(path)
                if problem:
                    continue
                if path.stem == pp.ORCHESTRATOR:
                    self.assertNotIn("tools", fm, f"{path.name}: 統括エージェントに tools があります")
                else:
                    self.assertIn("tools", fm, f"{path.name}: tools がありません")


class RepositoryTest(unittest.TestCase):
    """リポジトリのマーケットプレイス。Copilot CLI は marketplace.json（根）、.plugin/、.github/plugin/、.claude-plugin/ の順に
    最初のものを使うので、根の marketplace.json と .plugin/ を置かない。.claude-plugin/ のものは Claude Code だけが読む"""

    def setUp(self):
        self.copilot = pp.REPO_ROOT / ".github" / "plugin" / "marketplace.json"
        self.claude = pp.REPO_ROOT / ".claude-plugin" / "marketplace.json"
        if not self.copilot.is_file():
            self.skipTest("リポジトリの .github/plugin/marketplace.json がありません（プラグインのフォルダだけをコピーしたとき）")

    def test_no_root_marketplace_and_no_plugin_folder(self):
        self.assertFalse((pp.REPO_ROOT / "marketplace.json").exists(),
                         "リポジトリの根に marketplace.json があります（Copilot CLI が .github/plugin/ のものより先に読む）")
        found = []
        for current, dirs, _ in os.walk(pp.REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__")]
            found += [pp.rel(Path(current) / d) for d in dirs if d == ".plugin"]
        self.assertEqual(found, [], "リポジトリに .plugin/ のフォルダがあります（Copilot CLI がマーケットプレイスとして先に読む）")

    def test_marketplaces_agree(self):
        """2つのマーケットプレイスは同じ名前。このプラグインの行は、同じ版（2つのマニフェストと同じ）と source"""
        self.assertTrue(self.claude.is_file(), "リポジトリの .claude-plugin/marketplace.json がありません")
        copilot, claude = json.loads(pp.read(self.copilot)), json.loads(pp.read(self.claude))
        self.assertTrue(claude.get("name"), ".claude-plugin/marketplace.json に name がありません")
        self.assertEqual(claude.get("name"), copilot.get("name"), "2つのマーケットプレイスの name が違います")
        self.assertTrue((claude.get("owner") or {}).get("name"), ".claude-plugin/marketplace.json に owner.name がありません")
        version = json.loads(pp.read(pp.ROOT / "plugin.json")).get("version")
        claude_version = (pp.load_json(pp.CLAUDE_MANIFEST) or {}).get("version")
        self.assertEqual(claude_version, version, ".claude-plugin/plugin.json と plugin.json の version が違います")
        for path, data in ((self.copilot, copilot), (self.claude, claude)):
            rows = [p for p in data.get("plugins") or [] if p.get("name") == pp.PLUGIN_NAME]
            with self.subTest(marketplace=pp.rel(path)):
                self.assertEqual(len(rows), 1, "このプラグインの行が1つではありません")
                self.assertEqual(rows[0].get("version"), version, "このプラグインの版が、マニフェストと違います")
                self.assertEqual(rows[0].get("source"), f"./plugins/{pp.PLUGIN_NAME}", "このプラグインの source が違います")

    def test_claude_plugin_validate(self):
        """claude コマンドがあれば、claude plugin validate でプラグインとマーケットプレイスを確かめる"""
        claude = shutil.which("claude")
        if claude is None:
            self.skipTest("claude コマンドがないため飛ばしました")
        command = ["cmd", "/c", claude] if claude.lower().endswith((".cmd", ".bat")) else [claude]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        with tempfile.TemporaryDirectory(prefix="validate-") as tmp:
            # プラグインのフォルダで Claude Code を起動しない（.mcp.json をプロジェクトのものとして読む）
            for target in (pp.ROOT, pp.REPO_ROOT):
                with self.subTest(target=pp.rel(target) if target != pp.REPO_ROOT else "リポジトリ"):
                    try:
                        done = subprocess.run(command + ["plugin", "validate", str(target)], cwd=tmp, env=env,
                                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                                              timeout=180)
                    except subprocess.TimeoutExpired:
                        self.fail("claude plugin validate が 180 秒で終わりません")
                    out = (done.stdout or "") + (done.stderr or "")
                    self.assertEqual(done.returncode, 0, f"claude plugin validate が通りません：\n{out[-3000:]}")


class NamedPartsTest(unittest.TestCase):
    """テストが名前で指定するコンポーネント（plugin_parts.py の定数）が、すべてあること"""

    def test_named_parts_exist(self):
        for name in [pp.ORCHESTRATOR, *pp.ROLES.values()]:
            for path in (pp.agent_path(name), pp.claude_agent_path(name)):
                self.assertTrue(path.is_file(), f"{pp.rel(path)} がありません")
        for path in [pp.RUN_SCRIPT, pp.GUI_SCRIPT, pp.APPLY_SCRIPT, *(skill_md(n) for n in pp.PLUGIN_SKILLS)]:
            self.assertTrue(path.is_file(), f"{pp.rel(path)} がありません")


class SchemaToolTest(unittest.TestCase):
    """この試験の道具（小さな JSON Schema の確かめ）が、間違いを見つけられること"""

    def test_mini_validator_rejects_wrong_documents(self):
        plugin = json.loads(pp.read(pp.SCHEMAS_DIR / "plugin.schema.json"))
        mcp = json.loads(pp.read(pp.SCHEMAS_DIR / "mcp.schema.json"))
        ok_plugin = {"$schema": plugin["properties"]["$schema"]["const"], "name": "a-b"}
        ok_mcp = {"$schema": mcp["properties"]["$schema"]["const"],
                  "mcpServers": {"x": {"type": "stdio", "command": "uv", "args": ["run"], "env": {"A": "b"}}}}
        for validate in (pp.MiniValidator(plugin).errors, lambda d: pp.schema_errors(d, plugin)):
            self.assertEqual(validate(ok_plugin), [])
            self.assertTrue(validate(dict(ok_plugin, name="A--b")))
            self.assertTrue(validate(dict(ok_plugin, extra=1)))
            self.assertTrue(validate({"name": "a"}))
        for validate in (pp.MiniValidator(mcp).errors, lambda d: pp.schema_errors(d, mcp)):
            self.assertEqual(validate(ok_mcp), [])
            bad = json.loads(json.dumps(ok_mcp))
            bad["mcpServers"]["x"]["env"] = {"PLUGIN_ROOT": "x"}
            self.assertTrue(validate(bad))
            bad = json.loads(json.dumps(ok_mcp))
            bad["mcpServers"]["x"]["type"] = "http"
            self.assertTrue(validate(bad))

    def test_mini_yaml(self):
        text = ('name: a-b\ndescription: "x: y"\nmodel: [m-1, "m 2"]\ntools:\n  - read\n  - "coverity/x"\n'
                'long: >-\n  one\n  two\nplain: a\n  b\n')
        self.assertEqual(pp.parse_simple_yaml(text), {"name": "a-b", "description": "x: y", "model": ["m-1", "m 2"],
                                                      "tools": ["read", "coverity/x"], "long": "one two", "plain": "a b"})
        with self.assertRaises(ValueError):
            pp.parse_simple_yaml("description: 例: これは読めない\n")

    def test_claude_code_helpers(self):
        """Claude Code の形を読む道具：tools の並び、MCP のツールの2つの名前の形、frontmatter の名前の行を空にする、書き戻しのツール"""
        self.assertEqual(pp.tool_list("Read, Grep ,Skill"), ["Read", "Grep", "Skill"])
        self.assertEqual(pp.tool_list(["read", "x/y"]), ["read", "x/y"])
        self.assertEqual(pp.tool_list(None), [])
        plugin_form, hand_form = pp.claude_mcp_tool_names("srv-a", "do_it")
        self.assertEqual(pp.parse_claude_mcp_tool(plugin_form), ("srv-a", "do_it", "plugin"))
        self.assertEqual(pp.parse_claude_mcp_tool(hand_form), ("srv-a", "do_it", "hand"))
        self.assertIsNone(pp.parse_claude_mcp_tool("Read"))
        text = "---\nname: a-b\ndescription: x\ntools: A, B\ndisallowedTools:\n  - C\nmodel: haiku\n---\nbody a-b\n"
        blank = pp.without_frontmatter_keys(text)
        self.assertEqual(blank.splitlines(), ["---", "", "description: x", "", "", "", "model: haiku", "---", "body a-b"])
        self.assertEqual(pp.without_frontmatter_keys("no frontmatter\n"), "no frontmatter\n")
        import ast
        tree = ast.parse('@reg("w", "t", readOnlyHint=False, destructiveHint=True)\nasync def w(): pass\n'
                         '@reg("r", "t", readOnlyHint=True, destructiveHint=False)\nasync def r(): pass\n'
                         '@mcp.tool(annotations=Ann(destructiveHint=True))\ndef d(): pass\n')
        self.assertEqual(pp._destructive_tools(tree), {"w", "d"})

    def test_write_back_detection_forms(self):
        """書き戻しのツールの見つけ方：名前付きの引数、辞書（annotations={"destructiveHint": True}）、
        変数に作ってから渡すもの（ほかのファイルで作ったものも）。False のものは拾わない"""
        import ast
        tree = ast.parse('WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)\n'
                         'READ = {"readOnlyHint": True, "destructiveHint": False}\n'
                         '@mcp.tool(annotations={"destructiveHint": True})\ndef a(): pass\n'
                         '@mcp.tool(annotations=WRITE)\ndef b(): pass\n'
                         '@mcp.tool(annotations=READ)\ndef c(): pass\n'
                         '@mcp.tool(annotations={"destructiveHint": False})\ndef d(): pass\n'
                         '@mcp.tool(name="e_tool", annotations=SHARED)\ndef e(): pass\n'
                         'LOOP = LOOP\n@mcp.tool(annotations=LOOP)\ndef f(): pass\n')
        self.assertEqual(pp._destructive_tools(tree), {"a", "b"})
        shared = pp._assignments(ast.parse('SHARED = {"destructiveHint": True}\n'))
        self.assertEqual(pp._destructive_tools(tree, dict(pp._assignments(tree), **shared)), {"a", "b", "e_tool"})

    def test_write_back_tool_is_found(self):
        """この MCP サーバーの書き戻しのツールが、今の見つけ方で見つかる（見つからないと、書き戻しの守りの試験が黙って外れる）"""
        if not pp.mcp_registrations():
            self.skipTest("MCP サーバーがありません")
        self.assertTrue(any(pp.write_back_tools().values()),
                        "書き戻しのツール（destructiveHint=True）が MCP サーバーに見つかりません")


if __name__ == "__main__":
    unittest.main()
