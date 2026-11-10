"""文書を確認するテスト（憲章 第6条3項、仕様 REQ-008、設計書 9.1「文書」）。

- 版：plugin.json・.claude-plugin/plugin.json・仕様書・設計書・変更履歴と、マーケットプレイスの一覧（リポジトリの
  .github/plugin/marketplace.json と README）が同じ版。
- プラグインの README の並び（REQ-008）。
- 文書の相対リンクが、存在するファイルと見出しを指す。

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

CHANGELOG = pp.ROOT / "CHANGELOG.md"
README = pp.ROOT / "README.md"
MARKET_ROOT = pp.ROOT.parents[1]
MARKET_JSON = MARKET_ROOT / ".github" / "plugin" / "marketplace.json"
MARKET_README = MARKET_ROOT / "README.md"
# プラグインの README の並び（REQ-008）：最初の2つ、その後に出てくる順
# README は入口（5分で読める）。最初に「できること」と「使い方を選ぶ」、その後に始め方と文書の一覧（REQ-008）
README_FIRST = ["できること", "使い始める"]
README_THEN = ["文書"]
README_MAX_CHARS = 6000  # 入口の文書が、くわしい手順で長くならないように


def version_line(path):
    m = re.search(r"(?m)^- バージョン：(\S+)\s*$", pp.read(path))
    return m.group(1) if m else None


class VersionTest(unittest.TestCase):
    def test_versions_match(self):
        version = json.loads(pp.read(pp.ROOT / "plugin.json"))["version"]
        self.assertEqual(version_line(pp.SPEC), version, "仕様書の「- バージョン：」")
        self.assertEqual(version_line(pp.DESIGN), version, "設計書の「- バージョン：」")
        self.assertTrue(CHANGELOG.is_file(), "CHANGELOG.md がありません")
        top = re.search(r"(?m)^## ([0-9][^\s（(]*)", pp.read(CHANGELOG))
        self.assertIsNotNone(top, "変更履歴に版の見出し（## x.y.z）がありません")
        self.assertEqual(top.group(1), version, "変更履歴のいちばん上の版")
        if pp.CLAUDE_MANIFEST.is_file():
            self.assertEqual(json.loads(pp.read(pp.CLAUDE_MANIFEST)).get("version"), version, ".claude-plugin/plugin.json の版")
        if MARKET_JSON.is_file():
            label = MARKET_JSON.relative_to(MARKET_ROOT).as_posix()
            listed = [p for p in json.loads(pp.read(MARKET_JSON)).get("plugins", []) if p.get("name") == pp.PLUGIN_NAME]
            self.assertEqual([p.get("version") for p in listed], [version], f"リポジトリの {label} の版")
            self.assertEqual(listed[0].get("source"), f"./plugins/{pp.PLUGIN_NAME}", f"リポジトリの {label} の source")
        if MARKET_README.is_file():
            rows = [line for line in pp.read(MARKET_README).splitlines() if line.startswith(f"| [{pp.PLUGIN_NAME}]")]
            self.assertEqual(len(rows), 1, "リポジトリの README の一覧に、このプラグインの行がありません")
            self.assertTrue(rows[0].rstrip().endswith(f"| {version} |"), "リポジトリの README の一覧の版")


class ReadmeTest(unittest.TestCase):
    """プラグインの README（仕様 REQ-008）"""

    def setUp(self):
        if not README.is_file():
            self.fail("プラグインの README.md がありません")
        self.text = pp.read(README)
        self.headings = re.findall(r"(?m)^## (.+?)\s*$", pp.strip_code_blocks(self.text))

    def test_sections_in_order(self):
        self.assertGreaterEqual(len(self.headings), len(README_FIRST), "README の見出し（##）が足りません")
        for i, word in enumerate(README_FIRST):
            self.assertIn(word, self.headings[i], f"README の {i + 1} 番目の見出しは「{word}」です（今は「{self.headings[i]}」）")
        position = len(README_FIRST) - 1
        for word in README_THEN:
            later = [i for i, h in enumerate(self.headings) if i > position and word in h]
            self.assertTrue(later, f"README に「{word}」の見出しが、前の見出しの後にありません（REQ-008 の並び）")
            position = later[0]

    def test_mentions_the_essentials(self):
        self.assertLessEqual(len(self.text), README_MAX_CHARS, "README が長すぎます。くわしい手順はガイドに書きます（REQ-008）")
        steps = ["1-skill.md", "2-agent.md", "3-batch.md", "4-screen.md"]
        for i, name in enumerate(steps):  # チュートリアル：README からつながり、各ステップが次のステップへつながる
            path = pp.ROOT / "docs" / "guide" / name
            self.assertTrue(path.is_file(), f"チュートリアルの {name} がありません（REQ-008）")
            self.assertIn(f"(docs/guide/{name})", self.text, f"README から {name} へのリンクがありません")
            if i + 1 < len(steps):
                self.assertIn(f"({steps[i + 1]})", pp.read(path), f"{name} から次のステップ {steps[i + 1]} へのリンクがありません")
        for stage in ["1", "2", "3", "4"]:
            self.assertRegex(self.text, rf"{stage}\s*段階", f"README に {stage}段階目の説明がありません")


class LinkTest(unittest.TestCase):
    def test_links_resolve(self):
        docs = [README, CHANGELOG, TESTS / "README.md"] + sorted(pp.DOCS_DIR.rglob("*.md"))
        docs += sorted(pp.SKILLS_DIR.glob("*/README.md")) + sorted(pp.MCP_DIR.glob("*/README.md"))
        problems = []
        for doc in (d for d in docs if d.is_file()):
            for target in re.findall(r"\]\(([^)\s]+)\)", pp.strip_code_blocks(pp.read(doc))):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                    continue
                file_part, _, anchor = target.partition("#")
                dest = (doc.parent / file_part).resolve() if file_part else doc
                if not dest.exists():
                    problems.append(f"{pp.rel(doc)}: {target}（ファイルがありません）")
                elif anchor and dest.is_file() and dest.suffix in (".md", ".markdown") and anchor not in pp.heading_slugs(dest):
                    problems.append(f"{pp.rel(doc)}: {target}（見出しがありません）")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
