"""プラグインの構成を確認するテスト。

- マニフェスト：plugin.json、.claude-plugin/plugin.json、mcp.json、.mcp.json が正しく、互いに一致する。
- バージョン：2つのマニフェスト、リポジトリの .github/plugin/marketplace.json、CHANGELOG.md で同じ。
- スキル：skills/<名前>/SKILL.md の frontmatter（name はフォルダー名と同じ、description がある、使えるキーは4つ）。
- エージェント：com.github.copilot/agents/<名前>.agent.md（Copilot CLI）と agents/<名前>.md（開発に使う Claude Code）の
  frontmatter が正しく、2つの本文はツールの呼び名を除いて同じ。

  python3 -m unittest discover -s tests/structure -v
"""

import json
import re
import sys
import unittest
from pathlib import Path

import yaml  # PyYAML が必要です：python -m pip install pyyaml

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parents[1]
PLUGIN = "coverity-triage-kit"
SERVER = "coverity-connect"
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/"
# Agent Plugins 1.0.0 の plugin.json に書けるキー
PLUGIN_KEYS = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license", "keywords",
               "extensions"}
SKILL_KEYS = {"name", "description", "allowed-tools", "disable-model-invocation"}
# 本文のツールの呼び名（Claude Code, Copilot CLI）
TOOL_WORDS = [("Skill ツール", "skill ツール"), ("Agent ツール", "`task`"), ("AskUserQuestion", "ask_user"), ("Bash", "シェル")]
# frontmatter の tools の対応（Copilot CLI → Claude Code）
COPILOT_TO_CLAUDE = {
    "read": {"Read", "Grep", "Glob"}, "search": {"Grep", "Glob"}, "edit": {"Edit", "Write"}, "execute": {"Bash"},
    "shell": {"Bash"}, "skill": {"Skill"}, "ask_user": {"AskUserQuestion"}, "agent": {"Agent"}, "task": {"Agent"},
}
FENCE = re.compile(r"(?ms)^[ \t]*```[^\n]*\n.*?^[ \t]*```[ \t]*$")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def frontmatter(path):
    """(frontmatter, 本文)。frontmatter は YAML として読む"""
    name = path.relative_to(ROOT).as_posix()
    if not path.is_file():
        raise AssertionError(f"{name} がありません")
    m = re.match(r"---\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)", path.read_text(encoding="utf-8-sig"), re.S)
    if m is None:
        raise AssertionError(f"{name} の先頭に frontmatter（--- で囲んだ部分）がありません")
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as error:
        raise AssertionError(f"{name} の frontmatter を YAML として読めません：{error}")
    if not isinstance(data, dict):
        raise AssertionError(f"{name} の frontmatter が「キー: 値」の形ではありません")
    return data, m.group(2)


def agents(folder, suffix):
    """{名前: (frontmatter, 本文)}。フォルダーの中のファイルは、すべてエージェントの定義として読まれる"""
    out = {}
    for path in sorted(folder.iterdir()):
        if not (path.is_file() and path.name.endswith(suffix)):
            raise AssertionError(f"{path.relative_to(ROOT).as_posix()}：このフォルダーに置けるのは <名前>{suffix} だけです")
        out[path.name[:-len(suffix)]] = frontmatter(path)
    if not out:
        raise AssertionError(f"{folder.relative_to(ROOT).as_posix()}/ にエージェントの定義がありません")
    return out


def tool_list(value):
    """frontmatter の tools。Claude Code は「A, B」の文字列、Copilot CLI は [a, b]"""
    return [t.strip() for t in value.split(",") if t.strip()] if isinstance(value, str) else list(value or [])


def count(word, text):
    """本文に出てくる回数。英数字だけの呼び名は、単語として数える"""
    if re.fullmatch(r"\w+", word):
        return len(re.findall(r"(?<![A-Za-z0-9_\-])" + re.escape(word) + r"(?![A-Za-z0-9_\-])", text))
    return text.count(word)


class ManifestTest(unittest.TestCase):

    def test_plugin_json(self):
        data = load(ROOT / "plugin.json")
        # この $schema がないと、Copilot CLI は Claude Code 用の agents/ と .mcp.json も読み込む
        self.assertEqual(data.get("$schema"), SCHEMA + "plugin.schema.json")
        self.assertEqual(sorted(set(data) - PLUGIN_KEYS), [], "plugin.json に、Agent Plugins 1.0.0 にないキーがあります")
        self.assertEqual(data.get("name"), PLUGIN)
        self.assertRegex(data.get("version", ""), r"^\d+\.\d+\.\d+$")
        self.assertTrue(data.get("description"), "plugin.json に description がありません")

    def test_claude_plugin_json_agrees(self):
        data = load(ROOT / ".claude-plugin" / "plugin.json")
        self.assertEqual(data.get("name"), PLUGIN)
        self.assertTrue(data.get("description"), ".claude-plugin/plugin.json に description がありません")

    def test_mcp_json(self):
        data = load(ROOT / "mcp.json")
        self.assertEqual(data.get("$schema"), SCHEMA + "mcp.schema.json")
        self.assertEqual(list(data.get("mcpServers", {})), [SERVER])
        server = data["mcpServers"][SERVER]
        self.assertEqual((server.get("type"), server.get("command")), ("stdio", "uv"))
        args = server.get("args", [])
        self.assertIn("--directory", args)
        self.assertEqual(args[args.index("--directory") + 1], "${PLUGIN_ROOT}/mcp-servers/" + SERVER)
        self.assertTrue((ROOT / "mcp-servers" / SERVER / "pyproject.toml").is_file(), f"mcp-servers/{SERVER}/ がありません")
        # Copilot CLI は MCP サーバーに PATH 以外の環境変数を渡さないので、認証情報は env で渡す
        env = server.get("env", {})
        self.assertLessEqual({"COV_USER", "COV_AUTH_KEY"}, set(env))
        self.assertEqual({k: v for k, v in env.items() if v != "${" + k + "}"}, {}, 'env の値は "${名前}" にします')

    def test_claude_mcp_json_agrees(self):
        """.mcp.json は mcp.json と同じ。違いは ${CLAUDE_PLUGIN_ROOT} と、env の値の ${名前:-} だけ"""
        text = (ROOT / "mcp.json").read_text(encoding="utf-8").replace("${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}")
        want = json.loads(text)["mcpServers"]
        for server in want.values():
            server["env"] = {k: "${" + k + ":-}" for k in server.get("env", {})}
        self.assertEqual(load(ROOT / ".mcp.json"), {"mcpServers": want})

    def test_versions_agree(self):
        version = load(ROOT / "plugin.json")["version"]
        self.assertEqual(load(ROOT / ".claude-plugin" / "plugin.json").get("version"), version, ".claude-plugin/plugin.json")
        market = load(REPO / ".github" / "plugin" / "marketplace.json")
        listed = [p.get("version") for p in market["plugins"] if p.get("name") == PLUGIN]
        self.assertEqual(listed, [version], "リポジトリの .github/plugin/marketplace.json")
        top = re.search(r"(?m)^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertEqual(top and top.group(1), version, "CHANGELOG.md の最初の見出し（## x.y.z）")


class SkillTest(unittest.TestCase):

    def test_frontmatter(self):
        folders = sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir())
        self.assertTrue(folders, "skills/ にスキルがありません")
        for folder in folders:
            with self.subTest(skill=folder.name):
                data, _ = frontmatter(folder / "SKILL.md")
                self.assertEqual(sorted(set(data) - SKILL_KEYS), [], f"使えるキーは {sorted(SKILL_KEYS)} だけです")
                self.assertEqual(data.get("name"), folder.name, "name がフォルダー名と違います")
                self.assertTrue(isinstance(data.get("description"), str) and data["description"].strip(),
                                "description がありません")


class AgentTest(unittest.TestCase):
    """Copilot CLI のエージェントと、同じ名前の Claude Code のエージェント"""

    @classmethod
    def setUpClass(cls):
        cls.copilot = agents(ROOT / "com.github.copilot" / "agents", ".agent.md")
        cls.claude = agents(ROOT / "agents", ".md")

    def pairs(self):
        """[(名前, Claude Code の (frontmatter, 本文), Copilot CLI の (frontmatter, 本文))]"""
        self.assertEqual(sorted(self.claude), sorted(self.copilot), "agents/ と com.github.copilot/agents/ の名前が違います")
        return [(name, self.claude[name], self.copilot[name]) for name in sorted(self.copilot)]

    def test_frontmatter(self):
        for form, found in (("copilot", self.copilot), ("claude", self.claude)):
            for name, (data, body) in found.items():
                with self.subTest(form=form, agent=name):
                    self.assertTrue(isinstance(data.get("description"), str) and data["description"].strip(),
                                    "description がありません")
                    self.assertTrue(body.strip(), "本文がありません")
                    if form == "claude":
                        self.assertEqual(data.get("name"), name, "name がファイル名と違います")

    def test_same_agent_names(self):
        self.pairs()

    def test_same_headings_in_the_same_order(self):
        for name, (_, a), (_, b) in self.pairs():
            with self.subTest(agent=name):
                self.assertEqual(*(re.findall(r"(?m)^## (.+?)\s*$", FENCE.sub("", body)) for body in (a, b)))

    def test_same_report_templates(self):
        """``` で囲んだ部分（報告の形など）が同じ"""
        for name, (_, a), (_, b) in self.pairs():
            with self.subTest(agent=name):
                self.assertEqual(*([m.strip() for m in FENCE.findall(body)] for body in (a, b)))

    def test_tool_words_are_mapped(self):
        """ツールの呼び名は、対応するものが同じ回数だけ出てくる。もう一方の呼び名は書かない"""
        for name, (_, a), (_, b) in self.pairs():
            for claude_word, copilot_word in TOOL_WORDS:
                with self.subTest(agent=name, word=claude_word):
                    self.assertEqual(count(copilot_word, a), 0, f"agents/{name}.md に「{copilot_word}」があります")
                    self.assertEqual(count(claude_word, b), 0, f"{name}.agent.md に「{claude_word}」があります")
                    self.assertEqual(count(claude_word, a), count(copilot_word, b),
                                     f"「{claude_word}」と「{copilot_word}」の数が違います")

    def test_tools_are_mapped(self):
        """frontmatter の tools は、Copilot CLI のものを Claude Code の名前に置き換えたものと同じ。
        MCP の <サーバー>/<ツール> は、Claude Code の2つの名前（プラグインのサーバー、手で登録したサーバー）にする"""
        for name, (fa, _), (fb, _) in self.pairs():
            want = set()
            for tool in tool_list(fb.get("tools")):
                if "/" in tool:
                    server, mcp_tool = tool.split("/", 1)
                    want |= {f"mcp__plugin_{PLUGIN}_{server}__{mcp_tool}", f"mcp__{server}__{mcp_tool}"}
                else:
                    want |= COPILOT_TO_CLAUDE.get(tool, {f"（対応のない {tool}）"})
            with self.subTest(agent=name):
                self.assertEqual("tools" in fa, "tools" in fb, "tools が、片方の定義にだけあります")
                self.assertEqual(sorted(set(tool_list(fa.get("tools")))), sorted(want))


if __name__ == "__main__":
    unittest.main()
