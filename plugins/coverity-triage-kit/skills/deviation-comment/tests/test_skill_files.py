"""SKILL.md, README.md, references and evals of this skill folder."""

from __future__ import annotations

import re
import unittest

from _support import SKILL, clean_bytecode, forbidden_patterns, frontmatter, load_script, skill_text_files

NAME = SKILL.name  # the folder name (a renamed copy uses its new name)


def tearDownModule() -> None:
    clean_bytecode()


class SkillFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.fields, self.body = frontmatter(self.text)

    def test_frontmatter(self) -> None:
        self.assertEqual(set(self.fields), {"name", "description", "allowed-tools"})
        self.assertEqual(self.fields["name"], NAME)
        self.assertEqual(self.fields["allowed-tools"], '["shell(uv run:*)"]')
        description = self.fields["description"]
        self.assertLessEqual(len(description), 1024)
        self.assertNotRegex(description, r"[<>]")
        self.assertNotRegex(description, r"(^|\s)/[a-z]")  # no slash-command names
        self.assertIn("頼まれたとき", description)

    def test_body_is_short_and_links_exist(self) -> None:
        self.assertLess(len(self.text.splitlines()), 500)
        for link in re.findall(r"\]\(([^)]+)\)", self.body):
            self.assertTrue((SKILL / link).is_file(), link)
        self.assertIn("<このスキルのフォルダ>/scripts/check_comment.py", self.body)

    def test_examples_follow_the_rules(self) -> None:
        m = load_script("check_comment")
        for path in (SKILL / "SKILL.md", SKILL / "references" / "examples.md"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("> "):
                    self.assertTrue(m.check_comment(line[2:])["ok"], line)
                elif line.startswith("- 直す："):
                    self.assertTrue(m.check_comment(line[len("- 直す："):])["ok"], line)

    def test_readme_sections(self) -> None:
        readme = (SKILL / "README.md").read_text(encoding="utf-8")
        for heading in ("何をするか", "必要なもの", "置き方", "呼び方", "設定", "結果の見方", "うまくいかないとき", "テスト"):
            self.assertRegex(readme, rf"(?m)^## .*{heading}", heading)
        self.assertIn("# Windows（PowerShell）", readme)
        self.assertIn("# Linux（bash）", readme)

    def test_no_names_of_other_parts_or_outside_paths(self) -> None:
        patterns = forbidden_patterns(NAME)
        for path in skill_text_files():
            text = path.read_text(encoding="utf-8")
            for word, pattern in patterns:
                self.assertIsNone(pattern.search(text), f"{path.relative_to(SKILL)} に {word!r}")

    def test_commands_work_in_powershell_and_bash(self) -> None:
        for path in (SKILL / "SKILL.md", SKILL / "README.md"):
            text = path.read_text(encoding="utf-8")
            for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
                for bad in ("<<", "$(", "&&", "source "):
                    self.assertNotIn(bad, block, f"{path.name}: {block}")

    def test_eval_cases(self) -> None:
        cases = [p for p in (SKILL / "evals").iterdir() if p.is_dir()]
        self.assertGreaterEqual(len(cases), 2)
        for case in cases:
            prompt = (case / "prompt.md").read_text(encoding="utf-8")
            self.assertTrue(prompt.startswith("---\ntags: [skill-creator]\n"), case.name)
            yaml_text = (case / "case.yaml").read_text(encoding="utf-8")
            self.assertIn('schema_version: "1.1"', yaml_text)
            self.assertIn(f"name: {case.name}", yaml_text)
            graders = sorted((case / "graders").glob("*.md"))
            self.assertGreaterEqual(len(graders), 2, case.name)
            for grader in graders:
                self.assertTrue(grader.read_text(encoding="utf-8").startswith("---\ntype: llm\n"), grader)
            if (case / "files").is_dir():
                self.assertTrue((case / "place-files.sh").is_file(), case.name)
                self.assertIn("scaffold_script: place-files.sh", yaml_text)
                self.assertRegex(prompt, r"(?m)^Input files: ")


if __name__ == "__main__":
    unittest.main()
