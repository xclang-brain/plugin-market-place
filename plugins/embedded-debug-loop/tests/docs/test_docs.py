"""文書の食い違いを確かめるテスト。

- 版：plugin.json・仕様書・設計書・変更履歴と、マーケットプレイスの一覧（リポジトリの根の marketplace.json と README）が同じ版を書いている。
- 要求の番号：設計書とテストが挙げる REQ-xxx は、仕様書にある。仕様書の要求は、どれも設計書の「要求の追跡」の表にある（憲章 第6条3項）。

  python3 -m unittest discover -s tests/docs -v
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "specs" / "requirements.md"
DESIGN = ROOT / "docs" / "design" / "design.md"
CHANGELOG = ROOT / "CHANGELOG.md"
# マーケットプレイスの一覧（このプラグインが、リポジトリの plugins/ の下にあるときだけ確かめる）
MARKET_ROOT = ROOT.parents[1]
MARKET_JSON = MARKET_ROOT / ".github" / "plugin" / "marketplace.json"
MARKET_README = MARKET_ROOT / "README.md"
REQ = re.compile(r"REQ-(\d{3})")
REQ_RANGE = re.compile(r"REQ-(\d{3})〜REQ-(\d{3})")


def read(path):
    return path.read_text(encoding="utf-8")


def version_line(path):
    m = re.search(r"(?m)^- バージョン：(\S+)$", read(path))
    return m.group(1) if m else None


def spec_requirements():
    return sorted(set(re.findall(r"(?m)^- (REQ-\d{3})：", read(SPEC))))


def mentioned(text):
    """文に出てくる要求の番号。「REQ-201〜REQ-208」のような範囲は、両端だけ（範囲の中は、仕様書の番号で埋める）"""
    return {f"REQ-{n}" for n in REQ.findall(text)}


def covered(text, known):
    """表で触れている要求。範囲は、仕様書にある番号のうち、範囲に入るものすべて"""
    found = mentioned(text)
    for lo, hi in REQ_RANGE.findall(text):
        found |= {r for r in known if int(lo) <= int(r[4:]) <= int(hi)}
    return found


class VersionTest(unittest.TestCase):
    def test_versions_match(self):
        version = json.loads(read(ROOT / "plugin.json"))["version"]
        self.assertEqual(version_line(SPEC), version, "仕様書の版")
        self.assertEqual(version_line(DESIGN), version, "設計書の版")
        top = re.search(r"(?m)^## ([0-9][^\s（(]*)", read(CHANGELOG))
        self.assertIsNotNone(top, "変更履歴に版の見出しがありません")
        self.assertEqual(top.group(1), version, "変更履歴のいちばん上の版")
        if MARKET_JSON.is_file():
            listed = [p for p in json.loads(read(MARKET_JSON))["plugins"] if p.get("name") == ROOT.name]
            self.assertEqual([p.get("version") for p in listed], [version], "マーケットプレイスの marketplace.json の版")
        if MARKET_README.is_file():
            row = [line for line in read(MARKET_README).splitlines() if line.startswith(f"| [{ROOT.name}]")]
            self.assertEqual(len(row), 1, "マーケットプレイスの README の一覧に、このプラグインの行がありません")
            self.assertTrue(row[0].rstrip().endswith(f"| {version} |"), "マーケットプレイスの README の版")


class TraceTest(unittest.TestCase):
    def test_references_exist_in_the_spec(self):
        known = set(spec_requirements())
        self.assertTrue(known, "仕様書に要求がありません")
        files = [DESIGN] + sorted((ROOT / "tests").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.mjs"))
        problems = []
        for path in files:
            if path == Path(__file__).resolve():
                continue
            for req in sorted(mentioned(read(path)) - known):
                problems.append(f"{path.relative_to(ROOT)}：{req}")
        self.assertEqual(problems, [], "仕様書にない要求の番号があります：\n" + "\n".join(problems))

    def test_every_requirement_is_traced(self):
        known = spec_requirements()
        text = read(DESIGN)
        self.assertIn("## 6. 要求の追跡", text)
        table = text.split("## 6. 要求の追跡", 1)[1]
        missing = sorted(set(known) - covered(table, known))
        self.assertEqual(missing, [], "設計書の「要求の追跡」の表にない要求があります")


if __name__ == "__main__":
    unittest.main()
