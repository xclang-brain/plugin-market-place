# -*- coding: utf-8 -*-
"""このスキルのフォルダそのもののテスト。

- SKILL.md の先頭の情報（name・description）があり、本文が短く、詳しいことを置いたファイルがあること。
- コマンドが Windows（PowerShell）でも動く書き方であること。
- README.md に、人向けの使い方が書いてあること。
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
README = os.path.join(SKILL_DIR, "README.md")
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


class ReadmeTest(unittest.TestCase):
    """人向けの使い方（README.md）。このフォルダだけを受け取った人が読む。"""

    def setUp(self):
        self.text = read(README)

    def section(self, start, end):
        return self.text[self.text.index(start):self.text.index(end)]

    def test_sections(self):
        for heading in ("## できること", "## 必要なもの", "## インストールする", "## 使い方",
                        "## 設定されるスキルの書き方", "## パスワードなどの項目",
                        "## うまくいかないとき", "## テストを実行する"):
            self.assertIn(heading, self.text, heading)
        need = self.section("## 必要なもの", "## インストールする")
        self.assertIn("Windows の PC", need)
        self.assertIn("Python 3.8 以上", need)
        # 確かめたことと、確かめていないことを書く（ほかのスキルの README と同じ文）
        self.assertIn("Windows では GitHub Actions でテストしています（Copilot CLI との組み合わせは未確認）。", need)
        # 個人のフォルダは、Windows の場所を先に書く
        install = self.section("## インストールする", "## 使い方")
        self.assertLess(install.index("`%USERPROFILE%\\.copilot\\skills\\"), install.index("`~/.copilot/skills/"))
        # スキルの名前は書き換えられることがあるので、名前を除いた形で確かめる
        self.assertIn("`.github/skills/", self.text)
        self.assertIn("`~/.copilot/skills/", self.text)
        self.assertIn("`/skills reload`", self.text)
        self.assertIn("フォルダの名前を変えてコピーするときは、`SKILL.md` の `name:` も、フォルダと同じ名前に変えます。",
                      self.text)
        usage = self.section("## 使い方", "## 設定されるスキルの書き方")
        self.assertIn("「セットアップしたい」", usage)
        self.assertIn("`/setup`", usage)
        self.assertIn("を直したい` | その項目だけを質問します |", usage)

    def test_layout_shared_with_other_skills(self):
        """スキルの README でそろえる書き方：節の並び、インストールの確認、コマンドの書き方、
        文字コードの文、環境変数の例。setup は「設定ファイル」「結果とログ」の代わりに自分の節を置く。"""
        text = self.text
        headings = re.findall(r"(?m)^## (.+)$", re.sub(r"```.*?```", "", text, flags=re.S))
        self.assertEqual(headings, ["できること", "必要なもの", "インストールする", "使い方",
                                    "設定されるスキルの書き方", "パスワードなどの項目",
                                    "うまくいかないとき", "スクリプトを直接実行する", "テストを実行する"])
        # README に書いた名前で確かめる。名前を変えたコピーでも README は書き換えないので、
        # フォルダの名前（SKILL_NAME）とは比べない（test_config_tool.py の名前を変えたコピーのテスト）
        self.assertIn("\n3. `/skills list` に `setup` が出れば完了です。\n", text)
        # 名前を変えたコピーの文。setup には設定とログのフォルダがないので、呼び出しだけを書く
        self.assertIn("\nフォルダの名前を変えてコピーするときは、`SKILL.md` の `name:` も、フォルダと同じ名前に変えます。\n"
                      "呼び出しは `/<新しい名前>` になります。\n", text)
        needs = self.section("## 必要なもの", "## インストールする")
        self.assertIn("\n- GitHub Copilot CLI\n", needs)
        # できることの箇条は、言い切りの形（「…する。」）
        for line in self.section("## できること", "## 必要なもの").splitlines():
            if line.startswith("- "):
                self.assertFalse(line.endswith("ます。"), line)
        # うまくいかないときの表の見出し
        self.assertIn("\n| 症状・メッセージ | 対処 |\n", text)
        # 個人のフォルダは、エクスプローラーや cmd でもそのまま使える %USERPROFILE% の書き方（ほかの文書と同じ）
        self.assertNotIn("$env:USERPROFILE", text)
        # 直接実行とテストのコマンドは、1つの囲みに Windows（PowerShell）と Linux（bash）の行を分けて書く
        self.assertIn("```\n# Windows（PowerShell）\npy -3 <このフォルダ>/scripts/config_tool.py show <設定ファイル>\n"
                      "# Linux（bash）\npython3 <このフォルダ>/scripts/config_tool.py show <設定ファイル>\n```", text)
        self.assertIn("```\n# Windows（PowerShell）\npy -3 -m unittest discover -s tests\n"
                      "# Linux（bash）\npython3 -m unittest discover -s tests\n```", text)
        for line in text.splitlines():
            self.assertFalse(line.startswith(("```powershell", "```bash")), line)
        self.assertIn("Windows では、コンソールの文字コード（日本語の Windows では cp932 など）で書きます。\n",
                      self.section("## スクリプトを直接実行する", "## テストを実行する"))
        # パスワードを環境変数で渡す例は、Windows（PowerShell）と Linux（bash）の2つ
        self.assertIn("\n   - Windows（PowerShell）：`$env:名前 = '値'`\n"
                      "   - Linux（bash）：`export 名前='値'`\n",
                      self.section("## パスワードなどの項目", "## うまくいかないとき"))

    def test_test_command_runs_from_this_folder(self):
        part = self.section("## テストを実行する", "最後に `OK` と出れば成功です。")
        self.assertIn("このフォルダで、次を実行します", part)
        # 1つの囲みに、Windows を先に、Linux を後に書く（ほかのスキルの README と同じ書き方）
        self.assertIn("```\n# Windows（PowerShell）\npy -3 -m unittest discover -s tests\n"
                      "# Linux（bash）\npython3 -m unittest discover -s tests\n```", part)
        # テストで、このフォルダに __pycache__ を残さない動かし方も示す
        self.assertIn("`-B`", self.text[self.text.index("## テストを実行する"):])

    def test_skills_are_the_ones_copilot_cli_uses(self):
        """設定できるのは、Copilot CLI が使うスキル。探す場所と順は書かない（Copilot CLI が決める）。"""
        usage = self.section("## 使い方", "## 設定されるスキルの書き方")
        self.assertIn("設定するのは、Copilot CLI が使うスキル（`/skills list` に出るもの）です。", usage)
        self.assertIn("同じ名前のスキルがいくつかあるときも、Copilot CLI が使うほうを設定します。", usage)
        trouble = self.section("## うまくいかないとき", "## スクリプトを直接実行する")
        self.assertIn("`/skills list` に、そのスキルが出るかを確認します。", trouble)
        for word in ("スキルの探し方", "探す場所", "COPILOT_HOME", "COPILOT_SKILLS_DIRS", "隣のフォルダ", "1.0.105"):
            self.assertNotIn(word, self.text, word)

    def test_secret_words_match_the_tool(self):
        """README に書いた「パスワードなどの項目」の言葉を、道具がそのとおりに見分けること。"""
        tool = load_tool_module()
        part = self.section("## パスワードなどの項目", "## うまくいかないとき")
        line = [x for x in part.split("\n") if x.startswith("`password`")][0]
        words = re.findall(r"`([^`]+)`", line)
        self.assertGreaterEqual(len(words), 9, words)
        for word in words:
            self.assertTrue(tool.is_secret_key("my_" + word), word)
            self.assertFalse(tool.is_secret_key("my_" + word + "_env"), word)
        self.assertFalse(tool.is_secret_key("user_name"))

    def test_comment_rules_match_the_tool(self):
        """README に書いた説明の書き方を、道具がそのとおりに読むこと。"""
        tool = load_tool_module()
        part = self.section("## 設定されるスキルの書き方", "## パスワードなどの項目")
        for word in ("「必須」", "「必ず」", "`# 例：…`", "`# 選べる値：a / b / c`"):
            self.assertIn(word, part, word)
        # 道具で読めない形でも、README をもとに埋める（「対応していません」とは書かない）
        self.assertIn("この形でない YAML（入れ子など）や、ほかの形式（INI・JSON など）も、README を読んで質問します。", part)
        # JSON にはコメントを書けない。書ける形式のときだけ、と書く
        self.assertIn("コメントを書ける形式なら、項目のすぐ上のコメントも読みます。", part)
        self.assertNotIn("対応していません", self.text)
        doc = tool.Document("x.yaml", (
            "# 見本\n\n"
            "# 名前（必須）。\n# 例：東\nname: \"\"\n\n"
            "# 場所。ふだん使うなら、必ず書く。\nplace: \"\"\n\n"
            "# 単位。\n# 選べる値：a / b（ふつう）/ \"\"\nunit: \"a\"\n").encode("utf-8"))
        items = {it.key: it for it in doc.items}
        self.assertTrue(items["name"].required)
        self.assertEqual(items["name"].examples(), ["東"])
        self.assertTrue(items["place"].fill_recommended)
        self.assertEqual(items["unit"].choices, ["a", "b", ""])

    def test_no_python3_only_commands(self):
        """Windows を先に書く。コマンドの例は py -3 で、Linux での言い換えを添える。"""
        lines = self.text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("python3 "):
                # python3 の行は、同じ囲みの Windows（py -3）の行の後に、Linux の行として書く
                self.assertEqual(lines[i - 1], "# Linux（bash）", line)
                self.assertEqual(lines[i - 2], "py -3 " + line[len("python3 "):], line)
                self.assertEqual(lines[i - 3], "# Windows（PowerShell）", line)
        self.assertIn("py -3 <このフォルダ>/scripts/config_tool.py show <設定ファイル>", self.text)


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
        self.assertEqual(rel, ["README.md", "SKILL.md",
                               "reference/config-tool.md", "reference/other-formats.md", "reference/secrets.md",
                               "scripts/config_tool.py",
                               "tests/__init__.py",
                               "tests/fixtures/coffee-machine.yaml", "tests/fixtures/network-camera.yaml",
                               "tests/requirements.txt",
                               "tests/test_config_tool.py", "tests/test_powershell.py", "tests/test_skill_files.py"])


if __name__ == "__main__":
    unittest.main()
