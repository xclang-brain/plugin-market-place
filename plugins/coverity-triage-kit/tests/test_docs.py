"""The documents and every shipped text: links, versions, wording, the commands a user types, the credential blocks.

    python3 tests/run.py --only docs
"""

import re
import unittest

from test_structure import NAME, PLUGIN, files, frontmatter, load, setup_init

FENCE = re.compile(r"(?ms)^\s*```.*?^\s*```[^\n]*$")
# Words that are not ordinary technical Japanese, and the noun that hides what happens after the OK.
BANNED = ["確かめ", "頼みの文", "道具", "部品", "手番", "要るもの", "夜通し", "進み具合", "反映"]
# The tool the plugin is developed with and the plugin before this one are named in these places only.
# (The pattern is written in pieces, so that this file does not match it.)
DEVELOPMENT = re.compile("claude" " code|coverity-warning" "-triage", re.I)
MAY_NAME_IT = ("docs/constitution.md", "docs/design/design.md", "tests/README.md", "tests/eval/")


def read(name) -> str:
    return (PLUGIN / name).read_text(encoding="utf-8", errors="replace")


def anchors(text: str) -> set[str]:
    """The anchors GitHub gives the headings: lower case, no punctuation, a hyphen for every space."""
    titles = re.findall(r"(?m)^#{1,6}\s+(.*?)\s*$", FENCE.sub("", text))
    return {re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title.replace("`", "").lower())) for title in titles}


class Docs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.names = [path.as_posix() for path in files()]
        cls.shipped = [name for name in cls.names if "tests" not in name.split("/")]

    def test_relative_links_and_their_anchors_resolve(self):
        broken = []
        for name in self.names:
            if not re.match(r"(README|CHANGELOG|docs/.+)\.md$", name):
                continue
            for target in re.findall(r"\]\(([^)\s]+)\)", FENCE.sub("", read(name))):
                if re.match(r"[a-z][a-z0-9+.-]*:", target):
                    continue  # a link to another site
                file, _, anchor = target.partition("#")
                found = (PLUGIN / name).parent / file if file else PLUGIN / name
                if not found.exists() or anchor and found.suffix == ".md" and anchor not in anchors(read(found)):
                    broken.append(f"{name}: {target}")
        self.assertEqual(broken, [])

    def test_the_versions_agree(self):
        version = load("plugin.json")["version"]
        market = load("../../.github/plugin/marketplace.json")["plugins"]
        newest = re.search(r"(?m)^## (\d+\.\d+\.\d+)", read("CHANGELOG.md"))
        self.assertEqual((load(".claude-plugin/plugin.json")["version"],
                          [entry["version"] for entry in market if entry["name"] == NAME], newest and newest.group(1)),
                         (version, [version], version))

    def test_no_shipped_text_has_a_banned_word(self):
        self.assertEqual([f"{name}: {word}" for name in self.shipped for word in BANNED if word in read(name)], [])

    def test_the_third_choice_has_one_word(self):
        """保留にする (in a chat sentence 保留): the CID is marked as Pending in Coverity. 何もしない is said of code only."""
        self.assertEqual([name for name in self.shipped if "何もしない" in read(name) and "/references/" not in name], [])
        for skill in ("coverity-apply", "coverity-triage"):  # the texts that turn the person's answer into --set
            self.assertIn("修正は `fix`、逸脱は `deviation`、保留は `none`", read(f"skills/{skill}/SKILL.md"), skill)

    def test_only_the_development_documents_name_the_development_tool(self):
        self.assertEqual([name for name in self.names
                          if not name.startswith(MAY_NAME_IT) and DEVELOPMENT.search(read(name))], [])

    def test_every_command_names_a_skill_or_an_agent_that_a_user_can_start(self):
        skills = {name.split("/")[1] for name in self.names if name.startswith("skills/")}
        agents = {name.split("/")[-1].split(".")[0] for name in self.names if name.startswith("com.github.copilot/agents/")
                  and frontmatter(PLUGIN / name)[0].get("user-invocable") is not False}
        unknown = []
        for name in self.shipped:
            for start, known in ((f"/{NAME}:", skills), (f"--agent {NAME}:", agents)):
                named = set(re.findall(re.escape(start) + "([a-z0-9-]+)", read(name)))
                unknown += [f"{name}: {start}{word}" for word in sorted(named - known)]
        self.assertEqual(unknown, [])

    def test_the_readme_has_the_credential_blocks_that_setup_prints(self):
        blocks = {shell: re.search(rf"```{shell}\n(.*?)\n```", read("README.md"), re.S).group(1)
                  for shell in ("powershell", "bash")}
        self.assertEqual(blocks, setup_init()[1].get("credentials"))


if __name__ == "__main__":
    unittest.main()
