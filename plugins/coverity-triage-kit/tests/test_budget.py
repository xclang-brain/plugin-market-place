"""The caps on the size of the plugin (docs/specs/requirements.md, section 8).

    python3 tests/run.py --only budget
"""

import re
import unittest

from test_structure import PLUGIN, files, frontmatter, mcp_tools, settings_keys

# In the order of the table of the specification. A cap changes there and here together.
CAPS = {"出荷するコード（行）": 5500, "テスト（行）": 6000, "利用者向けの文書（字）": 15000, "ファイル": 160,
        "利用者が使うコマンド": 6, "設定キー": 13, "MCP のツール": 4}
CODE = re.compile(r"(skills/[^/]+/scripts|mcp-servers/[^/]+/src)/.*\.(py|js|html|css)$")
# What tests/run.py runs: the tests of the components and tests/, without the EVAL that needs a model.
TESTS = re.compile(r"((skills|mcp-servers)/[^/]+/tests|tests(?!/eval/))/.*\.(py|js)$")
DOCUMENTS = re.compile(r"(README|docs/guide/[^/]+)\.md$")


def read(name: str) -> str:
    return (PLUGIN / name).read_text(encoding="utf-8")


class Budget(unittest.TestCase):
    def test_the_caps_are_those_of_the_specification(self):
        table = read("docs/specs/requirements.md").split("\n## 8.")[1]
        cells = re.findall(r"\|([^|\n]+)\|$", table, re.M)[2:]  # the last column, below the head of the table
        # 「スキル 4、エージェント 2」 is 6 commands
        self.assertEqual([sum(int(number.replace(",", "")) for number in re.findall(r"[\d,]+", cell)) for cell in cells],
                         list(CAPS.values()))

    def test_the_plugin_is_within_the_caps(self):
        names = [path.as_posix() for path in files()]
        # the skills, and the agents that a user can start
        commands = {name.split("/")[1] for name in names if name.startswith("skills/")} | {
            name for name in names if name.startswith("com.github.copilot/agents/")
            and frontmatter(PLUGIN / name)[0].get("user-invocable") is not False}
        now = dict(zip(CAPS, (
            sum(len(read(name).splitlines()) for name in names if CODE.match(name)),
            sum(len(read(name).splitlines()) for name in names if TESTS.match(name)),
            sum(len(read(name)) for name in names if DOCUMENTS.match(name)),
            len(names), len(commands), len(settings_keys()), len(mcp_tools())), strict=True))
        over = [name for name, cap in CAPS.items() if now[name] > cap]
        self.assertEqual(over, [], "現在の値 / 上限: " + "、".join(
            f"{name} {now[name]:,} / {cap:,}" for name, cap in CAPS.items()))


if __name__ == "__main__":
    unittest.main()
