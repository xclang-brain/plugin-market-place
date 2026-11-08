"""Checks of this skill's own files: SKILL.md frontmatter and size, links, references, README, evals.

Run from the skill folder:  python3 -m unittest discover -s tests
Only this folder is read, so the test also passes on a copy of the folder (renamed or not).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
README_SECTIONS = ["何をするか", "必要なもの", "置き方", "呼び方", "設定", "結果の見方", "うまくいかないとき", "テストの動かし方"]
GRADER_HEADER = "---\ntype: llm\nfocus: trace\nweight: 1\n---\n"


def tearDownModule() -> None:
    shutil.rmtree(HERE / "__pycache__", ignore_errors=True)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise AssertionError("SKILL.md does not start with a --- frontmatter block")
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or line.startswith((" ", "\t")):
            raise AssertionError(f"frontmatter line is not 'key: value': {line!r}")
        fields[key.strip()] = value.strip()
    return fields, m.group(2)


def slug(heading: str) -> str:
    """The anchor GitHub makes for a heading (letters, digits, _ and - kept; spaces become -)."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def code_blocks(text: str) -> list[str]:
    return re.findall(r"```[^\n]*\n(.*?)```", text, re.S)


class SkillFilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        cls.fields, cls.body = split_frontmatter(cls.skill_md)
        cls.readme = (SKILL / "README.md").read_text(encoding="utf-8")

    def test_frontmatter_keys(self) -> None:
        allowed = {"name", "description", "allowed-tools"}
        self.assertLessEqual(set(self.fields), allowed)
        self.assertIn("name", self.fields)
        self.assertIn("description", self.fields)

    def test_name(self) -> None:
        name = self.fields["name"]
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(name), 64)
        self.assertNotIn("anthropic", name)
        self.assertNotIn("claude", name)

    def test_description(self) -> None:
        desc = self.fields["description"]
        self.assertTrue(desc)
        self.assertLessEqual(len(desc), 1024)
        self.assertNotRegex(desc, r"[<>]")
        # no slash command names (a renamed copy would not match them)
        self.assertNotRegex(desc, r"(^|[\s「（(])/[a-z]")
        # says when to use it, with example phrasings
        self.assertIn("「", desc)
        self.assertRegex(desc, r"使う")

    def test_allowed_tools_only_when_scripts_run(self) -> None:
        scripts = sorted((SKILL / "scripts").glob("*.py")) if (SKILL / "scripts").is_dir() else []
        if scripts:
            self.assertEqual(json.loads(self.fields.get("allowed-tools", "null")), ["shell(uv run:*)"])
        else:
            self.assertNotIn("allowed-tools", self.fields)

    def test_body_size(self) -> None:
        self.assertLess(len(self.body.splitlines()), 500)

    def test_forward_slashes_in_skill_md(self) -> None:
        self.assertNotIn("\\", self.skill_md)

    def test_commands_work_in_powershell_and_bash(self) -> None:
        for name, text in (("SKILL.md", self.skill_md), ("README.md", self.readme)):
            for block in code_blocks(text):
                for bad in ("<<", "$(", "&&", "source "):
                    self.assertNotIn(bad, block, f"{name}: {bad!r} in a code block")

    def test_referenced_files_exist(self) -> None:
        for name, text in (("SKILL.md", self.skill_md), ("README.md", self.readme)):
            links = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text)
            paths = re.findall(r"(?<![\w.-])((?:references|scripts|assets|tests|evals)/[A-Za-z0-9_./-]*[A-Za-z0-9_])", text)
            for rel in links + paths:
                if rel.startswith(("http://", "https://")):
                    continue
                self.assertTrue((SKILL / rel).exists(), f"{name} refers to a missing file: {rel}")

    def test_references_are_one_level_and_have_toc(self) -> None:
        ref_dir = SKILL / "references"
        if not ref_dir.is_dir():
            return
        self.assertEqual([p for p in ref_dir.iterdir() if p.is_dir()], [])
        for ref in sorted(ref_dir.glob("*.md")):
            text = ref.read_text(encoding="utf-8")
            self.assertIn(f"references/{ref.name}", self.skill_md, f"{ref.name} is not linked from SKILL.md")
            self.assertNotRegex(text, r"\]\((?!#|https?://)[^)]+\)", f"{ref.name} links to another file")
            if len(text.splitlines()) > 100:
                head = "\n".join(text.splitlines()[:15])
                self.assertIn("## 目次", head, f"{ref.name} has more than 100 lines but no table of contents")
                slugs = {slug(h) for h in re.findall(r"^#{2,4} (.+)$", text, re.M)}
                for anchor in re.findall(r"\]\(#([^)]+)\)", text):
                    self.assertIn(anchor, slugs, f"{ref.name}: table of contents points to a missing heading {anchor}")

    def test_readme_sections(self) -> None:
        headings = re.findall(r"^#{2,3} (.+)$", self.readme, re.M)
        for section in README_SECTIONS:
            self.assertTrue(any(section in h for h in headings), f"README.md has no section about {section}")

    def test_evals(self) -> None:
        cases = sorted(p for p in (SKILL / "evals").iterdir() if p.is_dir())
        self.assertGreaterEqual(len(cases), 2)
        for case in cases:
            with self.subTest(case=case.name):
                self.assertRegex(case.name, r"^[a-z][a-z0-9-]*$")
                prompt = (case / "prompt.md").read_text(encoding="utf-8")
                m = re.match(r"^---\n(.*?)\n---\n\n?(.+)$", prompt, re.S)
                self.assertIsNotNone(m, "prompt.md needs the settings block")
                settings = dict(line.split(":", 1) for line in m.group(1).splitlines())
                self.assertLessEqual(set(settings), {"tags", "max_turns", "timeout_seconds", "allowed_tools"})
                case_yaml = (case / "case.yaml").read_text(encoding="utf-8")
                self.assertIn('schema_version: "1.1"', case_yaml)
                self.assertIn(f"name: {case.name}\n", case_yaml)
                self.assertIn("expected_outcome: |", case_yaml)
                graders = sorted((case / "graders").glob("*.md"))
                self.assertGreaterEqual(len(graders), 2)
                for grader in graders:
                    self.assertRegex(grader.name, r"^\d\d-[a-z0-9-]+\.md$")
                    text = grader.read_text(encoding="utf-8")
                    self.assertTrue(text.startswith(GRADER_HEADER), grader.name)
                    self.assertTrue(text[len(GRADER_HEADER):].strip(), grader.name)
                if (case / "files").is_dir():
                    self.assertTrue((case / "place-files.sh").is_file())
                    self.assertIn("scaffold_script: place-files.sh", case_yaml)
                    last = m.group(2).strip().splitlines()[-1]
                    self.assertTrue(last.startswith("Input files: "), last)
                    for rel in last[len("Input files: "):].split(","):
                        self.assertTrue((case / "files" / rel.strip()).exists(), rel)


if __name__ == "__main__":
    unittest.main()
