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

    def section(self, start, end):
        return self.body[self.body.index(start):self.body.index(end)]

    def test_empty_optional_item_can_stay_empty(self):
        self.assertIn("「空のまま」（必須でなく、今の値が空のとき）", self.body)

    def test_choice_order_puts_values_before_staying_empty(self):
        """「空のまま」が先頭（既定）だと、Enter だけで大事な項目が空のままになる。
        並びは、今の値 → 書ける値 → 空のまま。"""
        section = self.section("### 4. 1つの項目の聞き方", "### 5. 答えを書き込む")
        current = section.index("「今の値のまま：<value>」")
        values = section.index("「<値> と書く」")
        empty = section.index("「空のまま」（必須でなく")
        self.assertLess(current, values)
        self.assertLess(values, empty)
        self.assertIn("「空のまま」は、書ける値より前に置きません。", section)

    def test_choices_show_the_value_that_is_written(self):
        """「例：<値>」の選択肢では、「例：」まで書かれるのかが分からない。"""
        section = self.section("### 4. 1つの項目の聞き方", "### 5. 答えを書き込む")
        self.assertIn("「my_app と書く」", section)
        self.assertIn("「例：」は付けません。", section)
        self.assertNotIn("「例：<値>」", self.body)
        write = self.section("### 5. 答えを書き込む", "### 6. パスワードなど")
        self.assertIn("「<値> と書く」を選んだら", write)

    def test_fill_recommended_puts_first_example_first(self):
        section = self.section("### 4. 1つの項目の聞き方", "### 5. 答えを書き込む")
        self.assertIn("`fill_recommended` が true の項目は、`examples` の1つ目を1番目に置きます。", section)
        self.assertIn("「（おすすめ）」", section)
        self.assertIn("`fill_recommended`", self.section("`items` の1つ1つには", "## 利用者に見せる言葉"))

    def test_file_only_phrases_are_rephrased(self):
        """コメントの「下の」「このスキル」は、チャットでは何を指すか分からない。"""
        section = self.section("## 利用者に見せる言葉", "## 質問のしかた")
        for word in ("「下の」", "「上の」", "「このスキル」", "「この項目」", "名前で言い換えます"):
            self.assertIn(word, section, word)
        ask = self.section("### 4. 1つの項目の聞き方", "### 5. 答えを書き込む")
        self.assertIn("「利用者に見せる言葉」のとおりに言い換えてから使います", ask)
        secret = self.section("### 6. パスワードなど", "### 7. 終わりの報告")
        self.assertIn("「利用者に見せる言葉」のとおりに言い換えてから使います", secret)

    def test_json_names_are_not_shown(self):
        section = self.section("## 利用者に見せる言葉", "## 質問のしかた")
        self.assertIn("JSON の名前", section)
        self.assertIn("利用者に見せません", section)
        self.assertIn("項目の名前（`key`）は、見せてかまいません", section)

    def test_one_file_at_a_time(self):
        """必須の項目を全部のファイルで先に聞くと、ファイルを2回行き来する。"""
        section = self.section("### 2. ファイルごとに進める", "### 3. 聞く順番")
        self.assertIn("1つのファイルについて、2〜6 を全部終えてから、次のファイルに進みます。", section)
        self.assertIn("ほかのファイルの項目を、先にまとめて聞くことはしません。", section)

    def test_named_items_come_first(self):
        order = self.section("### 3. 聞く順番", "### 4. 1つの項目の聞き方")
        named = order.index("直したい項目の名前を示したときは、その項目から聞きます。")
        self.assertLess(named, order.index("必須で空の項目"))
        files = self.section("### 1. 設定するファイルを決める", "### 2. ファイルごとに進める")
        self.assertIn("その項目があるファイルを先にします", files)

    def test_wording_of_the_rest_question(self):
        self.assertNotIn("説明の1行目", self.body)
        self.assertNotIn("残りの N 個", self.body)
        self.assertIn("「ほかの N 個の項目は、今の値のままでよいですか。」", self.body)
        self.assertIn("列は「項目」「今の値」「説明」です。", self.body)

    def test_rewriting_by_hand_waits_for_the_user(self):
        """自分で書き換えると答えたのに、待たずに先へ進むと、書く時間がない。"""
        section = self.section("- 「自分でファイルを書き換える」なら", "### 7. 終わりの報告")
        for word in ("ほかのコマンドは実行しません。次の項目にも進みません。",
                     "「書き換えたら教えてください。」", "「書き換えた」「あとでやる」の2つ",
                     "`show` でファイルを読み直します", "「あとで書き換える項目」"):
            self.assertIn(word, section, word)
        report = self.section("### 7. 終わりの報告", "## 守ること")
        self.assertIn("「あとで書き換える項目：<key>」", report)

    def test_cancel_and_decline_are_handled(self):
        section = self.body[self.body.index("### 質問が取り消されたとき"):self.body.index("## 進め方")]
        for word in ("Esc", "断った", "それ以上は聞きません", "書き換えた項目", "`changed`",
                     "7. 終わりの報告", "ファイルの場所"):
            self.assertIn(word, section, word)

    def test_final_report_is_always_given(self):
        section = self.body[self.body.index("### 7. 終わりの報告"):self.body.index("## 守ること")]
        self.assertIn("どんな終わり方でも、最後に必ずこの報告をします", section)
        self.assertIn("途中で止めたときも同じです", section)

    def test_final_report_is_shown_in_chat_before_anything_else(self):
        """呼び出し元が続けて別の作業をすると、報告が抜けた（2回続けて）。"""
        section = self.body[self.body.index("### 7. 終わりの報告"):self.body.index("## 守ること")]
        for word in ("呼び出し元がこの後に別の作業を続けるときも、省きません。",
                     "手番を返す前の最後のこと",
                     "チャットの文として、利用者に必ず見せます。",
                     "`show` と、ほかのコマンドやツールを、一緒に呼びません。",
                     "報告を書く前に、ほかのコマンドやツールを呼びません。",
                     "`table` を、そのまま貼ります"):
            self.assertIn(word, section, word)

    def test_values_are_always_passed_through_stdin(self):
        """値を引数に書くと、値の中のパスに Copilot CLI が許可を求め、値もコマンドの行に出る。"""
        tool = self.section("## 道具", "## 利用者に見せる言葉")
        self.assertIn("値は、いつも標準入力で渡します。コマンドの引数には書きません。", tool)
        self.assertIn("--stdin <<'VALUE_END'", tool)
        self.assertIn("`set <ファイル> <項目> --stdin --json`", tool)
        self.assertIn("空の値を書くときは、`<値>` の行を書きません。", tool)
        # 値を引数に書く書き方は、どこにも示さない
        self.assertNotRegex(self.body, r"set <ファイル> <項目> <値>")
        self.assertNotRegex(self.body, r"set <ファイル> <項目> '")
        for line in self.body.split("\n"):
            if "config_tool.py set" in line:
                self.assertIn("--stdin", line, line)
        write = self.section("### 5. 答えを書き込む", "### 6. パスワードなど")
        self.assertIn("値は、いつも `--stdin` で渡します", write)
        self.assertNotIn("値を ' で囲みます", self.body)
        rules = self.section("## 守ること", "質問のツール（ask_user）が使えないとき")
        self.assertIn("`set` の値は、コマンドの引数に書きません。いつも `--stdin` で渡します。", rules)

    def test_free_text_is_written_as_typed(self):
        """利用者が正しく書いた答えに、例に合わせて直した案を出した（S5）。"""
        write = self.section("### 5. 答えを書き込む", "### 6. パスワードなど")
        for word in ("書かれたとおりに書きます。",
                     "例や今の値と形が違っても、直した案を出しません。聞き返しもしません。",
                     "確かめるのは、その項目の種類として書けないときだけです。",
                     "「、」がなければ、答え全体を1つの値にします。空白では分けません。",
                     "`number`：数でないときだけ確かめます",
                     "`boolean`：true か false でないときだけ確かめます",
                     "答えの中身は変えません"):
            self.assertIn(word, write, word)
        self.assertNotIn("気になることがあれば", self.body)
        self.assertNotIn("分け方に迷ったら", self.body)
        rules = self.section("## 守ること", "質問のツール（ask_user）が使えないとき")
        self.assertIn("自由に書かれた答えを、例に合わせて直しません。直した案も出しません。", rules)

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
