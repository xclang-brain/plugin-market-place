# -*- coding: utf-8 -*-
"""このスキルのフォルダそのもののテスト。

- SKILL.md の先頭の情報（name・description）があり、本文が短く、詳しいことを置いたファイルがあること。
- コマンドが Windows（PowerShell）でも動く書き方であること。
- フォルダに README.md を置かないこと（人向けの使い方は、フォルダの外に置く）。
- フォルダの外を指さないこと。
- 道具が標準ライブラリだけを使い、Python 3.8 の文法で書かれていること。

このスキルのフォルダで、次のように動かす。
  python3 -m unittest discover -s tests -v
"""

import ast
import importlib.util
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SKILL_NAME = os.path.basename(SKILL_DIR)  # フォルダの名前を変えて写したら、その名前
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
TOOL = os.path.join(SKILL_DIR, "scripts", "config_tool.py")
# フォルダの外へ出る参照（「..」と「/」）
PARENT_REF = os.pardir + "/"


def skill_files():
    for root, dirs, files in os.walk(SKILL_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".pyc"):
                continue
            yield os.path.join(root, name)


def load_tool_module():
    """道具を、モジュールとして読み込む（__pycache__ を作らない）。"""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("config_tool_for_file_tests", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def front_matter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None, text
    fields = {}
    for line in m.group(1).split("\n"):
        k, _, v = line.partition(":")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        fields[k.strip()] = v
    return fields, text[m.end():]


class SkillMdTest(unittest.TestCase):
    """SKILL.md の形を確かめる。文言は確かめない（決まった文を固定すると、テストに合わせた指示が増えるため）。"""

    def setUp(self):
        self.text = read(SKILL_MD)
        self.fields, self.body = front_matter(self.text)

    def test_front_matter(self):
        self.assertIsNotNone(self.fields, "SKILL.md の先頭に --- で囲んだ情報がありません")
        # name: は、フォルダの名前と同じ（名前を変えて写すときは、両方を変える）
        self.assertEqual(self.fields.get("name"), SKILL_NAME)
        desc = self.fields.get("description", "")
        self.assertTrue(desc)
        self.assertLessEqual(len(desc), 1024)
        self.assertEqual(set(self.fields), {"name", "description"})
        # 「/名前:スキル名」の形の呼び方は書かない（入れ物の名前で変わる）
        self.assertNotRegex(self.text, r"/[\w.-]+:[\w.-]+")

    def test_body_is_short(self):
        self.assertLess(len(self.body.splitlines()), 500)

    def test_references_exist(self):
        """詳しいことは、SKILL.md から1段でたどれるファイルに置く。"""
        links = re.findall(r"\]\(([^)#]+)\)", self.body)
        self.assertTrue(links)
        for link in links:
            self.assertTrue(os.path.isfile(os.path.join(SKILL_DIR, link)), link)

    def test_tool_commands(self):
        """道具を呼ぶ行は <python> <このスキルのフォルダ>/scripts/config_tool.py の形（Windows でも動く）。"""
        for line in self.body.split("\n"):
            s = line.lstrip()
            if "config_tool.py" in s and s.startswith(("<python>", "python", "py ")):
                self.assertTrue(s.startswith("<python> <このスキルのフォルダ>/scripts/config_tool.py"), line)
            if "config_tool.py" in s and "'@ |" in s:
                self.assertIn("| <python> <このスキルのフォルダ>/scripts/config_tool.py set", s)
        self.assertNotRegex(self.body, r"(?m)^\s*python3 ")

    def test_no_data_folder(self):
        """このスキルは、作業フォルダに、自分のフォルダを持たない。"""
        self.assertNotRegex(self.text, r"`\.[\w-]+/`")
        self.assertNotIn("config.yaml", self.text)



class FolderTest(unittest.TestCase):
    def test_no_reference_outside_own_folder(self):
        for path in skill_files():
            self.assertNotIn(PARENT_REF, read(path), path)

    def test_only_stdlib_imports(self):
        tree = ast.parse(read(TOOL))
        allowed = {"json", "codecs", "ctypes", "math", "os", "re", "stat", "sys", "tempfile", "traceback", "unicodedata", "errno",
                   "hashlib", "codecs", "locale", "winreg"}
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "相対の import は使わない")
                names.add(node.module.split(".")[0])
        self.assertTrue(names <= allowed, names - allowed)

    @unittest.skipIf(sys.version_info < (3, 8), "Python 3.8 以上で確かめる")
    def test_python38_syntax(self):
        ast.parse(read(TOOL), feature_version=(3, 8))

    def test_only_expected_files(self):
        rel = sorted(os.path.relpath(p, SKILL_DIR).replace(os.sep, "/") for p in skill_files())
        self.assertEqual(rel, ["SKILL.md",
                               "reference/config-tool.md", "reference/other-formats.md", "reference/secrets.md",
                               "scripts/config_tool.py",
                               "tests/__init__.py",
                               "tests/fixtures/coffee-machine.yaml", "tests/fixtures/network-camera.yaml",
                               "tests/requirements.txt",
                               "tests/test_config_tool.py", "tests/test_powershell.py", "tests/test_skill_files.py"])


if __name__ == "__main__":
    unittest.main()
