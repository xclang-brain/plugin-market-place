"""Claude Code での使い方の文書を確かめる試験（仕様 REQ-009、設計書 2.3・9.1「文書」）。

- 名前：Claude Code のマニフェスト（.claude-plugin/plugin.json）が plugin.json と同じ名前、リポジトリの2つの一覧の
  マーケットプレイスの名前が同じ（版は test_docs.py が確かめる）。
- プラグインの README に「Claude Code で使う」があり、入れ方・段階ごとの呼び方・4段階目は Copilot CLI だけ・
  前もって許可するしかた・認証情報の環境変数がある。前もって許可する指定に、書き戻すツールがない。
- エージェントの README に「Claude Code で使う」があり、Claude Code の定義（agents/<名前>.md）と、
  .claude/agents/ への取り出し方を書いている。書いた `--agent <プラグイン>:<名前>` と定義のファイルが、あるものを指す。
- 仕様書に Claude Code の要求があり、設計書に「2つの形の同居」の節がある。リポジトリの README の行が Claude Code に触れる。
部品の名前は書かない。フォルダから読む。

  python3 -m unittest discover -s tests/docs -v
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS / "structure"))
import plugin_parts as pp  # noqa: E402

README = pp.ROOT / "README.md"
CLAUDE_MANIFEST = pp.ROOT / ".claude-plugin" / "plugin.json"
CLAUDE_AGENTS_DIR = pp.ROOT / "agents"
MARKET_ROOT = pp.ROOT.parents[1]
CLAUDE_MARKET = MARKET_ROOT / ".claude-plugin" / "marketplace.json"
COPILOT_MARKET = MARKET_ROOT / ".github" / "plugin" / "marketplace.json"
MARKET_README = MARKET_ROOT / "README.md"
HEADING = "Claude Code で使う"
WRITE_BACK_TOOL = "coverity_update_triage"  # MCP サーバーのツールの名前（取り決めの名前。設計書 5章）


def section(text, heading, level=2):
    """見出しの節の本文（同じか上の段の次の見出しまで）。なければ None"""
    marks = "#" * level
    m = re.search(rf"(?ms)^{marks} {re.escape(heading)}\s*\n(.*?)(?=^#{{1,{level}}} |\Z)", text)
    return m.group(1) if m else None


def claude_agent_names():
    if not CLAUDE_AGENTS_DIR.is_dir():
        return []
    return sorted(p.stem for p in CLAUDE_AGENTS_DIR.glob("*.md"))


class ClaudeNamesTest(unittest.TestCase):
    """版の一致は test_docs.VersionTest が確かめる。ここは、README の入れ方の名前が食い違わないこと"""

    def setUp(self):
        if not CLAUDE_MANIFEST.is_file():
            self.skipTest("Claude Code のマニフェストがまだありません")

    def test_manifest_name_matches(self):
        data = json.loads(pp.read(CLAUDE_MANIFEST))
        self.assertEqual(data.get("name"), json.loads(pp.read(pp.ROOT / "plugin.json")).get("name"),
                         ".claude-plugin/plugin.json の name が plugin.json と違います")

    def test_marketplace_names_match(self):
        if not (CLAUDE_MARKET.is_file() and COPILOT_MARKET.is_file()):
            self.skipTest("リポジトリに2つの一覧がありません")
        self.assertEqual(json.loads(pp.read(CLAUDE_MARKET)).get("name"), json.loads(pp.read(COPILOT_MARKET)).get("name"),
                         "2つの一覧のマーケットプレイスの名前が違います（README の入れ方の @<名前> が食い違う）")


class ReadmeClaudeSectionTest(unittest.TestCase):
    def setUp(self):
        self.text = pp.read(README)
        self.body = section(self.text, HEADING)
        self.assertIsNotNone(self.body, f"README に「## {HEADING}」がありません（REQ-009）")

    def test_after_usage_and_before_tests(self):
        headings = re.findall(r"(?m)^## (.+?)\s*$", pp.strip_code_blocks(self.text))
        i = headings.index(HEADING)
        self.assertGreater(i, headings.index("使い方"), "「Claude Code で使う」は「使い方」の後に置く")
        tests = [k for k, h in enumerate(headings) if "試験" in h]
        self.assertTrue(tests and i < tests[0], "「Claude Code で使う」は「試験」の前に置く")

    def test_mentions_the_essentials(self):
        market = json.loads(pp.read(CLAUDE_MARKET)).get("name") if CLAUDE_MARKET.is_file() else None
        install = f"claude plugin install {pp.PLUGIN_NAME}" + (f"@{market}" if market else "")
        for word in [install, "claude plugin marketplace add", "--agent", "--allowedTools", "Bash(uv run *)",
                     "/add-dir", "--add-dir", "COV_USER", "COV_AUTH_KEY", "Copilot CLI だけ"]:
            self.assertIn(word, self.body, f"README の「{HEADING}」に {word} がありません（REQ-009）")
        for stage in ["1", "2", "3", "4"]:
            self.assertRegex(self.body, rf"{stage}\s*段階", f"README の「{HEADING}」に {stage}段階目の呼び方がありません")

    def test_stage4_is_copilot_only_in_the_stage_table(self):
        rows = [line for line in self.text.splitlines() if pp.is_table_line(line) and line.strip().startswith("| 4段階目")]
        self.assertTrue(rows, "README に4段階目の行がありません")
        self.assertTrue(any("Copilot CLI だけ" in r for r in rows), "README の段階の表で、4段階目が Copilot CLI だけと分かりません")

    def test_agent_commands_point_to_agents(self):
        names = claude_agent_names()
        for name in re.findall(rf"--agent {re.escape(pp.PLUGIN_NAME)}:([a-z0-9-]+)", self.body):
            self.assertIn(name, names, f"README の --agent {pp.PLUGIN_NAME}:{name} の定義が agents/ にありません")

    def test_write_back_is_not_pre_approved(self):
        for line in self.body.splitlines():
            if "--allowedTools" in line:
                self.assertNotIn(WRITE_BACK_TOOL, line, "前もって許可する指定に、書き戻すツールがあります")
                self.assertNotRegex(line, r'"mcp__[^"]*coverity-connect"', "サーバーの名前だけの許可は、書き戻すツールまで許します")


class AgentReadmeClaudeSectionTest(unittest.TestCase):
    def setUp(self):
        self.names = claude_agent_names()
        if not self.names:
            self.skipTest("Claude Code のエージェントの定義がまだありません")

    def test_every_agent_readme_has_the_section(self):
        for name in self.names:
            readme = pp.AGENTS_DIR / (name + pp.AGENT_README_SUFFIX)
            with self.subTest(agent=name):
                self.assertTrue(readme.is_file(), f"{readme.name} がありません（2つの形の使い方はここに書く）")
                body = section(pp.read(readme), HEADING)
                self.assertIsNotNone(body, f"{readme.name} に「## {HEADING}」がありません")
                self.assertIn(f"agents/{name}.md", body, f"{readme.name}：Claude Code の定義の場所がありません")
                self.assertIn(".claude/agents/", body, f"{readme.name}：取り出し方（.claude/agents/）がありません")
                self.assertIn(".claude/skills/", body, f"{readme.name}：取り出し方（.claude/skills/）がありません")

    def test_named_files_and_agents_exist(self):
        for readme in sorted(pp.AGENTS_DIR.glob("*" + pp.AGENT_README_SUFFIX)):
            body = section(pp.read(readme), HEADING) or ""
            with self.subTest(readme=readme.name):
                for name in re.findall(rf"--agent {re.escape(pp.PLUGIN_NAME)}:([a-z0-9-]+)", body):
                    self.assertIn(name, self.names, f"{readme.name}：--agent の {name} の定義が agents/ にありません")
                for name in re.findall(r"`agents/([a-z0-9-]+)\.md`", body):
                    self.assertIn(name, self.names, f"{readme.name}：agents/{name}.md がありません")


class SpecAndDesignTest(unittest.TestCase):
    def test_spec_has_the_claude_code_requirement(self):
        reqs = re.findall(r"(?m)^- (REQ-\d{3})：([^\n]*)", pp.read(pp.SPEC))
        hits = [r for r, line in reqs if "Claude Code" in line and "Copilot CLI だけ" in line]
        self.assertTrue(hits, "仕様書に、1〜3段階目は Claude Code でも使え、4段階目は Copilot CLI だけ、の要求がありません")

    def test_design_has_the_coexistence_section(self):
        text = pp.read(pp.DESIGN)
        self.assertRegex(text, r"(?m)^### \d+\.\d+ 2つの形の同居", "設計書に「2つの形の同居」の節がありません")
        self.assertIn("$schema", text, "設計書に、plugin.json の $schema の守りがありません")

    def test_repository_readme_mentions_claude_code(self):
        if not MARKET_README.is_file():
            self.skipTest("リポジトリの README がありません")
        rows = [line for line in pp.read(MARKET_README).splitlines() if line.startswith(f"| [{pp.PLUGIN_NAME}]")]
        self.assertEqual(len(rows), 1, "リポジトリの README の一覧に、このプラグインの行がありません")
        self.assertIn("Claude Code", rows[0], "リポジトリの README の行に、Claude Code でも使えることがありません")


if __name__ == "__main__":
    unittest.main()
