"""文書の食い違いを確かめるテスト。

- バージョン：2つのマニフェスト、仕様書、設計書、マーケットプレイスの一覧が、同じバージョンを書いている。
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
MANIFESTS = [ROOT / "plugin.json", ROOT / ".claude-plugin" / "plugin.json"]
# マーケットプレイスの一覧（このプラグインが、リポジトリの plugins/ の下にあるときだけ確かめる）
MARKET = ROOT.parents[1]
MARKET_JSONS = [MARKET / ".github" / "plugin" / "marketplace.json", MARKET / ".claude-plugin" / "marketplace.json"]
MARKET_README = MARKET / "README.md"
TRACE = "## 10. 要求の追跡"
REQ = re.compile(r"REQ-(\d{3})")
REQ_RANGE = re.compile(r"REQ-(\d{3})〜REQ-(\d{3})")


def read(path):
    return path.read_text(encoding="utf-8")


def version_line(path):
    m = re.search(r"(?m)^- バージョン：(\S+)$", read(path))
    return m.group(1) if m else None


def spec_requirements():
    return sorted(set(re.findall(r"(?m)^- (REQ-\d{3})：", read(SPEC))))


def covered(text, known):
    """表で触れている要求。「REQ-101〜REQ-110」のような範囲は、仕様書にある番号のうち、範囲に入るものすべて"""
    found = {f"REQ-{n}" for n in REQ.findall(text)}
    for lo, hi in REQ_RANGE.findall(text):
        found |= {r for r in known if int(lo) <= int(r[4:]) <= int(hi)}
    return found


class VersionTest(unittest.TestCase):
    def test_versions_match(self):
        versions = {str(p.relative_to(ROOT)): json.loads(read(p))["version"] for p in MANIFESTS}
        self.assertEqual(len(set(versions.values())), 1, f"マニフェストのバージョンが違います：{versions}")
        version = next(iter(versions.values()))
        self.assertEqual(version_line(SPEC), version, "仕様書のバージョン")
        self.assertEqual(version_line(DESIGN), version, "設計書のバージョン")
        for market in MARKET_JSONS:
            if market.is_file():
                listed = [p for p in json.loads(read(market))["plugins"] if p.get("name") == ROOT.name]
                self.assertEqual([p.get("version") for p in listed], [version], market.relative_to(MARKET))
        if MARKET_README.is_file():
            row = [line for line in read(MARKET_README).splitlines() if line.startswith(f"| [{ROOT.name}]")]
            self.assertEqual(len(row), 1, "マーケットプレイスの README の一覧に、このプラグインの行がありません")
            self.assertTrue(row[0].rstrip().endswith(f"| {version} |"), "マーケットプレイスの README のバージョン")


class TraceTest(unittest.TestCase):
    def test_references_exist_in_the_spec(self):
        known = set(spec_requirements())
        self.assertTrue(known, "仕様書に要求がありません")
        files = [DESIGN] + [p for p in sorted(ROOT.rglob("*.py")) if "vendor" not in p.parts]
        problems = []
        for path in files:
            if path == Path(__file__).resolve():
                continue
            found = {f"REQ-{n}" for n in REQ.findall(read(path))}
            problems += [f"{path.relative_to(ROOT)}：{r}" for r in sorted(found - known)]
        self.assertEqual(problems, [], "仕様書にない要求の番号があります：\n" + "\n".join(problems))

    def test_every_requirement_is_traced(self):
        known = spec_requirements()
        text = read(DESIGN)
        self.assertIn(TRACE, text)
        missing = sorted(set(known) - covered(text.split(TRACE, 1)[1], known))
        self.assertEqual(missing, [], "設計書の「要求の追跡」の表にない要求があります")


if __name__ == "__main__":
    unittest.main()
