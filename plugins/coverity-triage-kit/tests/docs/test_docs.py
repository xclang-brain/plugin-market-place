"""文書を確かめる試験（憲章 第6条3項、仕様 REQ-006〜REQ-008、設計書 9.1「文書」）。

- 版：plugin.json・.claude-plugin/plugin.json・仕様書・設計書・変更履歴と、マーケットプレイスの一覧（リポジトリの
  .github/plugin/marketplace.json と .claude-plugin/marketplace.json と README）が同じ版。
- 要求の番号：仕様書の要求は、どれも設計書の「要求の追跡」の表にある。設計書と試験が挙げる REQ-xxx は、仕様書にある。
- 部品の名前：仕様書と設計書は、部品を役割で呼ぶ。書いてよいのは取り決めの名前（MCP サーバーの登録名、ツールの名前、
  エクステンションのツールの名前、defaults.json の例、取り決めのファイルの名前）だけ。仕様書は、利用者が打つコマンドとして
  スキルの名前を書いてよい（表と /<スキル> の形）。エージェントのファイルの名前とスクリプトの名前は、どちらにも書かない。
- docs/parts.md の「今の部品」の表が、今ある部品と合う（役割と名前の対応は、ここにだけある）。
- プラグインの README の並び（REQ-008）と、元のプラグインとどちらか一方だけを入れること（REQ-007）。
  README に「Claude Code で使う」の節があり、Claude Code で入れるときの名前（<プラグイン>@<マーケットプレイス>）を書く。
- 文書の相対リンクが、あるファイルと見出しを指す。

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
CLAUDE_MARKET_JSON = MARKET_ROOT / ".claude-plugin" / "marketplace.json"
CLAUDE_SECTION = "Claude Code で使う"
MARKET_README = MARKET_ROOT / "README.md"
REQ = re.compile(r"REQ-(\d{3})")
REQ_RANGE = re.compile(r"REQ-(\d{3})\s*〜\s*REQ-(\d{3})")
# プラグインの README の並び（REQ-008）：最初の2つ、その後に出てくる順
# README は入口（5分で読める）。最初に「できること」と「使い方を選ぶ」、その後に始め方と文書の一覧（REQ-008）
README_FIRST = ["できること", "使い始める"]
README_THEN = ["文書"]
README_MAX_CHARS = 6000  # 入口の文書が、くわしい手順で長くならないように
COPILOT_GUIDE = pp.ROOT / "docs" / "guide" / "operation.md"  # 運用ガイド（事前の許可、対処）


def version_line(path):
    m = re.search(r"(?m)^- バージョン：(\S+)\s*$", pp.read(path))
    return m.group(1) if m else None


def spec_requirements():
    return sorted(set(re.findall(r"(?m)^\s*- (REQ-\d{3})：", pp.read(pp.SPEC))))


def mentioned(text):
    return {f"REQ-{n}" for n in REQ.findall(text)}


def covered(text, known):
    found = mentioned(text)
    for lo, hi in REQ_RANGE.findall(text):
        found |= {r for r in known if int(lo) <= int(r[4:]) <= int(hi)}
    return found


def trace_table():
    text = pp.read(pp.DESIGN)
    m = re.search(r"(?ms)^## \d+\. 要求の追跡\s*\n(.*?)(?=^## |\Z)", text)
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
        for market in (MARKET_JSON, CLAUDE_MARKET_JSON):
            if not market.is_file():
                continue
            label = market.relative_to(MARKET_ROOT).as_posix()
            listed = [p for p in json.loads(pp.read(market)).get("plugins", []) if p.get("name") == pp.PLUGIN_NAME]
            self.assertEqual([p.get("version") for p in listed], [version], f"リポジトリの {label} の版")
            self.assertEqual(listed[0].get("source"), f"./plugins/{pp.PLUGIN_NAME}", f"リポジトリの {label} の source")
        if MARKET_README.is_file():
            rows = [line for line in pp.read(MARKET_README).splitlines() if line.startswith(f"| [{pp.PLUGIN_NAME}]")]
            self.assertEqual(len(rows), 1, "リポジトリの README の一覧に、このプラグインの行がありません")
            self.assertTrue(rows[0].rstrip().endswith(f"| {version} |"), "リポジトリの README の一覧の版")


class TraceTest(unittest.TestCase):
    def test_every_requirement_is_traced(self):
        known = spec_requirements()
        self.assertTrue(known, "仕様書に要求（- REQ-xxx：）がありません")
        table = trace_table()
        self.assertIsNotNone(table, "設計書に「## <番号>. 要求の追跡」の節がありません")
        missing = sorted(set(known) - covered(table, known))
        self.assertEqual(missing, [], "設計書の「要求の追跡」の表にない要求があります")

    def test_references_exist_in_the_spec(self):
        """設計書と試験が挙げる要求の番号は、仕様書にある（仕様書の9章は、元のプラグインの番号を挙げるので除く）"""
        known = set(spec_requirements())
        table = trace_table() or ""
        self.assertEqual(sorted(mentioned(table) - known), [], "設計書の「要求の追跡」の表に、仕様書にない要求があります")
        files = [pp.DESIGN] + sorted(TESTS.rglob("*.py")) + sorted(TESTS.rglob("*.mjs"))
        problems = []
        for path in files:
            if any(p in ("node_modules", ".venv") for p in path.parts):
                continue
            for req in sorted(mentioned(pp.read(path)) - known):
                problems.append(f"{pp.rel(path)}：{req}")
        self.assertEqual(problems, [], "仕様書にない要求の番号があります：\n" + "\n".join(problems))


class PartNamesTest(unittest.TestCase):
    """仕様書と設計書は、部品を役割で呼ぶ（設計書の冒頭、仕様書の冒頭）"""

    def names(self):
        skills = pp.skill_names()
        agents = pp.agent_names()
        scripts = sorted(pp.script_owners())
        if not (skills or agents or scripts):
            self.skipTest("部品がまだありません")
        return skills, agents, scripts

    def test_spec_names_skills_only_as_commands(self):
        skills, agents, scripts = self.names()
        problems = []
        for lineno, line in enumerate(pp.read(pp.SPEC).splitlines(), 1):
            for name in agents:
                if pp.name_token(name).search(line):
                    problems.append(f"{lineno}: エージェント {name} … {line.strip()[:100]}")
            for name in scripts:
                if pp.script_token(name).search(line):
                    problems.append(f"{lineno}: スクリプト {name} … {line.strip()[:100]}")
            if pp.is_table_line(line):
                continue
            for name in skills:
                for m in pp.name_token(name).finditer(line):
                    if not line[:m.start()].endswith("/"):
                        problems.append(f"{lineno}: スキル {name}（表の外は /{name} の形で）… {line.strip()[:100]}")
        self.assertEqual(problems, [], "仕様書に、部品の名前があります：\n" + "\n".join(problems))

    def test_design_names_only_contracts(self):
        skills, agents, scripts = self.names()
        text = pp.read(pp.DESIGN)
        # defaults.json の例（設計書の defaults.json の節の JSON）は、名前を書く取り決めなので除く
        text = re.sub(r"(?ms)(^#{2,4} [^\n]*defaults\.json[^\n]*\n.*?)(^```json\s*\n.*?^```)",
                      lambda m: m.group(1) + "\n" * m.group(2).count("\n"), text)  # 行の番号は変えない
        allowed_scripts = pp.contract_command_scripts()
        # 写しの一覧（4.17）に挙げた持ち主のスクリプトは、取り決めのファイルの名前なので書いてよい
        section = re.search(r"(?ms)^### 4\.17 .*?(?=^## |^### 4\.18 |\Z)", text)
        if section:
            allowed_scripts |= set(re.findall(r"scripts/([A-Za-z0-9_.-]+\.py)", section.group(0)))
        patterns = [(f"スキル {n}", pp.name_token(n)) for n in skills] + [(f"エージェント {n}", pp.name_token(n)) for n in agents]
        patterns += [(f"スクリプト {n}", pp.script_token(n)) for n in scripts if n not in allowed_scripts]
        problems = pp.hits_in_text("設計書", text, patterns)
        self.assertEqual(problems, [], "設計書に、取り決めでない部品の名前があります（役割で書く）：\n" + "\n".join(problems))


class ReplaceDocTest(unittest.TestCase):
    """docs/parts.md の「今の部品」の表（役割と今の名前の、ただ1つの対応表）"""

    def setUp(self):
        if not pp.REPLACE_DOC.is_file():
            self.fail("docs/parts.md がありません")
        rows, error = pp.parts_table(pp.read(pp.REPLACE_DOC), pp.CURRENT_PARTS_HEADING, pp.CURRENT_PARTS_HEADER)
        self.assertIsNone(error, f"docs/parts.md：{error}")
        self.rows = pp.current_parts()

    def test_rows_point_to_parts(self):
        roles = [r["role"] for r in self.rows]
        self.assertEqual(len(roles), len(set(roles)), "同じ役割の行が2つあります")
        for row in self.rows:
            with self.subTest(role=row["role"]):
                self.assertIn(row["stage"], {"1", "2", "3", "4"}, "段階は 1〜4")
                place = pp.ROOT / row["place"]
                self.assertTrue(place.exists(), f"{row['place']} がありません")
                self.assertIn(row["name"], {Path(row["place"]).name, pp.agent_stem(Path(row["place"]).name)},
                              "部品の名前と置き場所の名前が違います")

    def test_every_part_is_listed(self):
        places = {row["place"] for row in self.rows}
        parts = [f"skills/{n}" for n in pp.skill_names()] + [pp.rel(p) for p in pp.mcp_dirs()]
        parts += [pp.rel(p) for p in pp.agent_files()]
        if pp.EXTENSIONS_DIR.is_dir():
            parts += [pp.rel(p) for p in pp.EXTENSIONS_DIR.iterdir() if p.is_dir()]
        self.assertEqual(sorted(set(parts) - places), [], "docs/parts.md の「今の部品」の表にない部品があります")


class ReadmeTest(unittest.TestCase):
    """プラグインの README（仕様 REQ-007、REQ-008）"""

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
        for guide in (COPILOT_GUIDE,):
            self.assertTrue(guide.is_file(), f"{guide.name} がありません（REQ-008）")
            self.assertIn(f"(docs/guide/{guide.name})", self.text, f"README から {guide.name} へのリンクがありません")
        steps = ["1-skill.md", "2-agent.md", "3-batch.md", "4-screen.md"]
        for i, name in enumerate(steps):  # チュートリアル：README からつながり、各ステップが次のステップへつながる
            path = pp.ROOT / "docs" / "guide" / name
            self.assertTrue(path.is_file(), f"チュートリアルの {name} がありません（REQ-008）")
            self.assertIn(f"(docs/guide/{name})", self.text, f"README から {name} へのリンクがありません")
            if i + 1 < len(steps):
                self.assertIn(f"({steps[i + 1]})", pp.read(path), f"{name} から次のステップ {steps[i + 1]} へのリンクがありません")
        copilot = pp.read(COPILOT_GUIDE)
        for word in ["--allow-tool", "--add-dir", "--experimental", "/extensions mode", "gh auth login"]:
            self.assertIn(word, copilot, f"運用ガイドに {word} がありません（REQ-008）")
        for stage in ["1", "2", "3", "4"]:
            self.assertRegex(self.text, rf"{stage}\s*段階", f"README に {stage}段階目の説明がありません")

    def test_claude_code_section(self):
        """「Claude Code で使う」の節があり、Claude Code で入れるときの名前（<プラグイン>@<マーケットプレイス>）を書く"""
        guide = self.text  # Claude Code のことは README の1か所にだけ書く（REQ-008）
        if CLAUDE_MARKET_JSON.is_file():
            market = json.loads(pp.read(CLAUDE_MARKET_JSON)).get("name")
            self.assertIn(f"{pp.PLUGIN_NAME}@{market}", guide, f"README に、Claude Code で入れるときの名前 {pp.PLUGIN_NAME}@{market} がありません")

    def test_changelog_has_the_claude_code_version(self):
        """Claude Code の形を足した版（0.2.0）の見出しが、変更履歴にある"""
        self.assertRegex(pp.read(CHANGELOG), r"(?m)^## 0\.2\.0(?![0-9])", "変更履歴に 0.2.0 の見出しがありません")


class LinkTest(unittest.TestCase):
    def test_links_resolve(self):
        docs = [README, CHANGELOG, TESTS / "README.md"] + sorted(pp.DOCS_DIR.rglob("*.md"))
        docs += sorted(pp.AGENTS_DIR.glob("*.markdown")) if pp.AGENTS_DIR.is_dir() else []
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
