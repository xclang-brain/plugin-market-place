"""文書のリンクを確認するテスト：README.md と docs/guide/*.md の相対リンクが、実在するファイルと見出しを指す。

  python3 -m unittest discover -s tests/docs -v
"""

import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
FENCE = re.compile(r"(?ms)^\s*```.*?^\s*```[^\n]*$")


def anchors(path):
    """GitHub が見出しに付けるアンカー（小文字にし、記号を消し、空白を - にする）"""
    out = set()
    for title in re.findall(r"(?m)^#{1,6}\s+(.*?)\s*#*\s*$", FENCE.sub("", path.read_text(encoding="utf-8"))):
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title.replace("`", "").lower())
        out.add(re.sub(r"\s", "-", re.sub(r"[^\w\s\-]", "", title)))
    return out


class LinkTest(unittest.TestCase):

    def test_links_resolve(self):
        guides = sorted((ROOT / "docs" / "guide").glob("*.md"))
        self.assertTrue(guides, "docs/guide/ に文書がありません")
        problems = []
        for doc in [ROOT / "README.md"] + guides:
            name = doc.relative_to(ROOT).as_posix()
            for target in re.findall(r"\]\(([^)\s]+)\)", FENCE.sub("", doc.read_text(encoding="utf-8"))):
                if re.match(r"[a-z][a-z0-9+.-]*:", target):
                    continue  # https: などの外へのリンク
                file_part, _, anchor = target.partition("#")
                dest = (doc.parent / file_part).resolve() if file_part else doc
                if not dest.exists():
                    problems.append(f"{name}: {target}（ファイルがありません）")
                elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
                    problems.append(f"{name}: {target}（見出しがありません）")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
