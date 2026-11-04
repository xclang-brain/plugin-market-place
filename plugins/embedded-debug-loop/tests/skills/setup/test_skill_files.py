# -*- coding: utf-8 -*-
"""設定のスキルのフォルダ（skills/setup/）そのものの試験。

- SKILL.md の先頭の情報（name・description）があること。
- 対話の進め方に要ることが書いてあること。
- ほかのスキル・エージェント・プラグインのほかの部分を知らないこと（独立していること）。
- 道具が標準ライブラリだけを使い、Python 3.8 の文法で書かれていること。
"""

import ast
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SKILL_DIR = os.path.join(PLUGIN, "skills", "setup")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
TOOL = os.path.join(SKILL_DIR, "scripts", "config_tool.py")

# このスキルのフォルダに、あってはならない言葉。
FORBIDDEN = [
    # ほかのスキルの名前
    "cross-compile", "cross_compile", "deploy", "log-capture", "log_capture",
    # エージェントの名前と呼び名
    "device-worker", "device_worker", "orchestrator", "verifier",
    "作業エージェント", "統括", "検証エージェント", "検証",
    # Web の画面（拡張）
    "Web管理画面", "管理画面", "extension", "拡張", ".mjs",
    # ほかのスキルの仕事の中身（このスキルは、設定ファイルの中身を知らない）
    "ビルド", "クロスコンパイル", "コンパイル", "デプロイ", "転送", "FTP", "ftp",
    "シリアル", "serial", "ログ取得", "キャプチャ", "capture", "make", "Makefile",
    "機器", "ボード", "SDK", "sensor",
]


def skill_files():
    for root, dirs, files in os.walk(SKILL_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".pyc"):
                continue
            yield os.path.join(root, name)


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
    def setUp(self):
        self.text = read(SKILL_MD)
        self.fields, self.body = front_matter(self.text)

    def test_front_matter(self):
        self.assertIsNotNone(self.fields, "SKILL.md の先頭に --- で囲んだ情報がありません")
        self.assertEqual(self.fields.get("name"), "setup")
        desc = self.fields.get("description", "")
        self.assertTrue(desc)
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("/embedded-debug-loop:setup", desc)
        self.assertIn("YAML", desc)
        self.assertEqual(set(self.fields), {"name", "description"})

    def test_dialogue_is_described(self):
        body = self.body
        for word in ("ask_user", "`choices`",
                     "config_tool.py", "list", "show", "set", "--json", "--stdin",
                     "必須で空の項目", "今の値のまま", "空のまま", "例：", "あとで決める",
                     "secret", "env_alternative", "＊＊＊", "（空）",
                     "終わりの報告", "項目のすぐ上のコメント",
                     "推測で書きません"):
            self.assertIn(word, body, word)
        # 聞く順番：必須で空の項目を先に聞く
        self.assertLess(body.index("必須で空の項目"), body.index("1つずつ確かめる"))

    def test_ask_user_parameters_are_not_named(self):
        """ask_user の引数の名前は、Copilot CLI の版で変わる（question/choices と
        message/requestedSchema）。名前を書かず、「質問の文と選択肢」と書く。"""
        for word in ("`question`", "requestedSchema", "`message`", "`choices` に選択肢"):
            self.assertNotIn(word, self.body, word)

    def test_exit_codes_are_not_mentioned(self):
        self.assertNotIn("終了コード", self.body)

    def test_empty_secret_is_shown_as_empty(self):
        """中身が空のパスワードを ＊＊＊ と書くと、もう入っているように見える。"""
        for line in self.body.split("\n"):
            if "＊＊＊" in line and "secret" in line:
                self.assertIn("空でない", line, line)
        self.assertIn("空の値は「（空）」と書きます。`secret` が true でも同じです。", self.body)

    def test_empty_optional_item_can_stay_empty(self):
        self.assertIn("「空のまま」（必須でなく、今の値が空のとき）", self.body)
        self.assertLess(self.body.index("「空のまま」（必須でなく"), self.body.index("`choices` の値を全部"))

    def test_cancel_and_decline_are_handled(self):
        section = self.body[self.body.index("### 質問が取り消されたとき"):self.body.index("## 進め方")]
        for word in ("Esc", "断った", "それ以上は聞きません", "書き換えた項目", "`changed`",
                     "7. 終わりの報告", "ファイルの場所"):
            self.assertIn(word, section, word)

    def test_final_report_is_always_given(self):
        section = self.body[self.body.index("### 7. 終わりの報告"):self.body.index("## 守ること")]
        self.assertIn("どんな終わり方でも、最後に必ずこの報告をします", section)
        self.assertIn("途中で止めたときも同じです", section)

    def test_sentences_are_short(self):
        # 文は 60 文字ほどまで（表・コードは除く）。長すぎる文がないかを見る。
        long_ones = []
        in_code = False
        for line in self.body.split("\n"):
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or line.lstrip().startswith("|"):
                continue
            text = re.sub(r"`[^`]*`", "", line)
            for sentence in re.split(r"(?<=。)", text):
                s = sentence.strip(" -0123456789.")
                if len(s) > 75:
                    long_ones.append(s)
        self.assertEqual(long_ones, [])


class IndependenceTest(unittest.TestCase):
    def test_no_forbidden_words(self):
        found = []
        for path in skill_files():
            text = read(path)
            for word in FORBIDDEN:
                if word.isascii():
                    hit = re.search(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", text,
                                    re.IGNORECASE if word.islower() else 0)
                else:
                    hit = word in text
                if hit:
                    found.append("{}: {}".format(os.path.relpath(path, PLUGIN), word))
        self.assertEqual(found, [])

    def test_no_reference_outside_own_folder(self):
        for path in skill_files():
            text = read(path)
            self.assertNotIn("../", text, path)
            self.assertNotIn("skills/", text, path)
            self.assertNotIn("agents/", text, path)

    def test_only_stdlib_imports(self):
        tree = ast.parse(read(TOOL))
        allowed = {"json", "math", "os", "re", "sys", "tempfile", "traceback", "unicodedata", "errno"}
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
        rel = sorted(os.path.relpath(p, SKILL_DIR) for p in skill_files())
        self.assertEqual(rel, ["SKILL.md", os.path.join("scripts", "config_tool.py")])


if __name__ == "__main__":
    unittest.main()
