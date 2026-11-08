# -*- coding: utf-8 -*-
"""Checks of this skill folder itself (no model, standard library only).

Run from the skill folder:  python3 -m unittest discover -s tests -v
The tests read only this folder, so they pass when the folder is copied (or renamed) elsewhere.
"""

import re
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
README = SKILL_DIR / "README.md"
TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".json", ".sh", ".txt", ".c", ".h", ".py"}
TABLE_HEADER = "| 役割 | 部品 | 使うとき |"
README_SECTIONS = ["何をするか", "必要なもの", "置き方", "呼び方", "設定", "結果の見方", "うまくいかないとき", "テストの動かし方"]


def read(path):
    return Path(path).read_text(encoding="utf-8")


def front_matter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None, text
    fields = {}
    for line in m.group(1).split("\n"):
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, text[m.end():]


def table_block(text):
    """(start, end, rows) of the table under '## 使う部品'; rows are lists of 3 stripped cells."""
    m = re.search(r"^## 使う部品\s*$", text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^#{1,2} ", rest, re.M)
    section = rest[:nxt.start()] if nxt else rest
    lines = section.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.startswith("|"):
            start = i
            break
    if start is None:
        return None
    table = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        table.append(line)
    offset = m.end() + sum(len(x) + 1 for x in lines[:start])
    length = sum(len(x) + 1 for x in table)
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in table]
    return offset, offset + length, table, rows


def token(word):
    return re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(word) + r"(?![A-Za-z0-9_\-])")


def folder_text_files():
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in TEXT_SUFFIXES or path.name.startswith("."):
            yield path


class FrontMatterTest(unittest.TestCase):
    def setUp(self):
        self.fields, self.body = front_matter(read(SKILL_MD))

    def test_fields(self):
        self.assertIsNotNone(self.fields, "SKILL.md の先頭に --- で囲んだ欄がない")
        self.assertLessEqual(set(self.fields), {"name", "description", "allowed-tools"})
        self.assertRegex(self.fields.get("name", ""), r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(self.fields["name"]), 64)
        if "allowed-tools" in self.fields:
            self.assertEqual(self.fields["allowed-tools"], '["shell(uv run:*)"]')

    def test_description(self):
        desc = self.fields.get("description", "")
        self.assertTrue(desc)
        self.assertLessEqual(len(desc), 1024)
        self.assertNotRegex(desc, r"[<>]")
        # no slash-command names: a copy with another name would disagree
        self.assertNotRegex(desc, r"(?<![\w.~])/[a-z][a-z0-9-]+")
        # what it does and when it is used
        self.assertIn("使う", desc)
        self.assertIn("使わない", desc)

    def test_body(self):
        self.assertLess(len(self.body.splitlines()), 500)


class ReferencesTest(unittest.TestCase):
    def test_links_resolve_one_level(self):
        body = read(SKILL_MD)
        links = re.findall(r"\]\(([^)#\s]+)\)", body)
        self.assertTrue(links)
        for link in links:
            self.assertNotIn("..", link)
            self.assertTrue((SKILL_DIR / link).is_file(), link)
        for ref in (SKILL_DIR / "references").glob("*.md"):
            text = read(ref)
            # references are one level deep: they do not send the reader on to other references
            for link in re.findall(r"\]\(([^)#\s]+)\)", text):
                self.assertFalse(link.endswith(".md"), f"{ref.name} -> {link}")
            if len(text.splitlines()) > 100:
                self.assertRegex("\n".join(text.splitlines()[:15]), r"## 目次", ref.name)


class PartsTableTest(unittest.TestCase):
    """Names of other parts appear only in the '## 使う部品' table of SKILL.md."""

    def setUp(self):
        self.text = read(SKILL_MD)
        self.block = table_block(self.text)

    def test_table_format(self):
        self.assertIsNotNone(self.block, "## 使う部品 の表がない")
        _, _, lines, rows = self.block
        self.assertEqual(lines[0].strip(), TABLE_HEADER)
        self.assertRegex(lines[1], r"^\|\s*-+\s*\|\s*-+\s*\|\s*-+\s*\|$")
        names = []
        for row in rows[2:]:
            self.assertEqual(len(row), 3, row)
            role, part, when = row
            self.assertTrue(role and when, row)
            m = re.fullmatch(r"`([a-z0-9][a-z0-9-]*)`", part)
            self.assertIsNotNone(m, f"部品の欄は `名前` だけにする: {part}")
            names.append(m.group(1))
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)))

    def test_names_only_in_table(self):
        start, end, _, rows = self.block
        names = [r[1].strip("`") for r in rows[2:]]
        outside = {SKILL_MD: self.text[:start] + self.text[end:]}
        for path in folder_text_files():
            if path != SKILL_MD and path.parent.name != "tests":
                outside[path] = read(path)
        for path, text in outside.items():
            for name in names:
                self.assertIsNone(token(name).search(text), f"{path.relative_to(SKILL_DIR)} に {name}")

    def test_no_paths_outside(self):
        bad = ["../", "..\\", "${" + "PLUGIN_ROOT}", "PLUGIN" + "_ROOT", "plugin" + ".json", "com.github" + ".copilot"]
        for path in folder_text_files():
            if path.parent.name == "tests":
                continue
            # the documented default of output_dir (relative to the repository, a data contract) is not a path of this folder
            text = read(path).replace(".." + "/coverity-triage-out", "")
            for word in bad:
                self.assertNotIn(word, text, f"{path.relative_to(SKILL_DIR)}: {word}")


class SelftestRulesTest(unittest.TestCase):
    """The self test: fixed checks in the script, never a write to the in-house Coverity."""

    def test_no_screen_tool_name(self):
        # only the screen entry names the extension tool
        for path in folder_text_files():
            if path.parent.name != "tests":
                self.assertNotRegex(read(path), r"[a-z0-9-]+_open\b", path.name)

    def test_every_check_is_documented(self):
        ids = re.findall(r'^    \("(\d-\d+)", ', read(SKILL_DIR / "scripts" / "selftest.py"), re.M)
        self.assertGreaterEqual(len(ids), 20)
        table = read(SKILL_DIR / "references" / "checks.md")
        for check_id in ids:
            self.assertRegex(table, r"(?m)^\| " + re.escape(check_id) + r" \|", check_id)

    def test_rules(self):
        text = read(SKILL_MD)
        self.assertIn("000000000000", text)
        self.assertIn("社内の Coverity に書き込まない", text)
        self.assertIn("report.md", text)

    def test_script_commands(self):
        for path in [SKILL_MD, README]:
            for line in read(path).splitlines():
                if line.lstrip().startswith("uv run"):
                    self.assertRegex(line, r'^uv run --native-tls --quiet "<この(スキルの)?フォルダ>/scripts/[a-z_]+\.py" ')

    def test_sample_assets(self):
        sample = SKILL_DIR / "assets" / "sample-target"
        config = read(sample / ".coverity-triage" / "config.yaml")
        self.assertRegex(config, r"(?m)^  api: fake$")
        self.assertRegex(config, r"(?m)^  fake_data: fake-issues.yaml$")
        fake = read(sample / ".coverity-triage" / "fake-issues.yaml")
        self.assertTrue((sample / ".coverity-triage" / "filters" / "all.yaml").is_file())
        self.assertTrue((sample / "src" / "reader.c").is_file())
        self.assertIn("*.writes.jsonl", read(sample / ".gitignore"))
        expected = read(SKILL_DIR / "assets" / "expected.yaml")
        cids = re.findall(r'"(\d+)": \{recommendation: (?:fix|deviation)', expected)
        group = re.search(r"cids: \[([\d, ]+)\]", expected).group(1).split(", ")
        self.assertEqual(len(cids), 3)
        for cid in cids + group:
            self.assertRegex(fake, r"cid: " + cid + r"\b", cid)


class CommandsTest(unittest.TestCase):
    """Commands in the documents work in PowerShell and in bash."""

    def test_no_bash_only_syntax(self):
        for path in [SKILL_MD, README, *sorted((SKILL_DIR / "references").glob("*.md"))]:
            for block in re.findall(r"```[a-z]*\n(.*?)```", read(path), re.S):
                for word in ["<<", "$(", "&&", "source ", "export "]:
                    self.assertNotIn(word, block, f"{path.name}: {word}")


class ReadmeTest(unittest.TestCase):
    def test_sections(self):
        text = read(README)
        heads = re.findall(r"^## (.+?)\s*$", text, re.M)
        for name in README_SECTIONS:
            self.assertIn(name, heads)

    def test_os_lines(self):
        text = read(README)
        if "# Windows（PowerShell）" in text:
            self.assertIn("# Linux（bash）", text)


class EvalsTest(unittest.TestCase):
    """Test cases in the skill-creator layout (evals/<case>/)."""

    def cases(self):
        return sorted(p for p in (SKILL_DIR / "evals").iterdir() if p.is_dir())

    def test_cases(self):
        cases = self.cases()
        self.assertGreaterEqual(len(cases), 2)
        for case in cases:
            self.assertRegex(case.name, r"^[a-z][a-z0-9-]*$")
            prompt = read(case / "prompt.md")
            m = re.match(r"^---\n(.*?)\n---\n\n?(.+)", prompt, re.S)
            self.assertIsNotNone(m, case.name)
            keys = {line.split(":")[0].strip() for line in m.group(1).split("\n") if line.strip()}
            self.assertLessEqual(keys, {"tags", "max_turns", "timeout_seconds", "allowed_tools"}, case.name)
            spec = read(case / "case.yaml")
            self.assertRegex(spec, r'(?m)^schema_version: "1\.1"$')
            self.assertRegex(spec, r"(?m)^name: " + re.escape(case.name) + r"$")
            self.assertRegex(spec, r"(?m)^expected_outcome: \|")
            graders = sorted((case / "graders").glob("*.md"))
            self.assertTrue(graders, case.name)
            for grader in graders:
                self.assertRegex(grader.name, r"^\d\d-[a-z0-9-]+\.md$")
                self.assertRegex(read(grader), r"^---\ntype: llm\nfocus: trace\nweight: 1\n---\n\n\S")
            if (case / "files").is_dir():
                self.assertTrue((case / "place-files.sh").is_file(), case.name)
                self.assertIn("scaffold_script: place-files.sh", spec)
                self.assertRegex(m.group(2).rstrip(), r"\nInput files: [^\n]+$")


if __name__ == "__main__":
    unittest.main()
