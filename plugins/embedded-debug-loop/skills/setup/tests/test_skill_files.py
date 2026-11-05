# -*- coding: utf-8 -*-
"""このスキルのフォルダそのものの試験。

- SKILL.md の先頭の情報（name・description）があること。
- 対話の進め方に要ることが書いてあること。
- README.md に、人向けの使い方が書いてあること。
- フォルダの外を指さないこと。
- 道具が標準ライブラリだけを使い、Python 3.8 の文法で書かれていること。

このスキルのフォルダで、次のように動かす。
  python3 -m unittest discover -s tests -v
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import shutil
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


def tool_constant(name):
    """道具の中の、そのままの値で書いた定数を読む（道具を import しない）。"""
    for node in ast.parse(read(TOOL)).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("道具に {} がありません".format(name))


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
        # name: は、フォルダの名前と同じ（名前を変えて写すときは、両方を変える）
        self.assertEqual(self.fields.get("name"), SKILL_NAME)
        desc = self.fields.get("description", "")
        self.assertTrue(desc)
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("YAML", desc)
        # スラッシュコマンドでも呼べることは、コマンドの名前を書かずに示す
        self.assertIn("スラッシュコマンドで、このスキルだけを呼んでも使える。", desc)
        self.assertEqual(set(self.fields), {"name", "description"})
        # 「/名前:スキル名」の形の呼び方は書かない（入れ物の名前で変わる）
        self.assertNotRegex(self.text, r"/[\w.-]+:[\w.-]+")

    def test_files_are_chosen_from_candidates(self):
        """ファイルの場所が渡されないときは、作業フォルダの中の候補から選んでもらう。"""
        files = self.section("### 1. 設定するファイルを決める", "### 2. ファイルごとに進める")
        for word in ("`list` を実行して、候補のファイルを探します。",
                     "候補が1つでも聞きます。",
                     "「どのファイルを設定しますか。ほかのファイルなら、その場所を書いてください。」",
                     "`name` は、作業フォルダからのファイルの場所です。",
                     "候補が2つ以上なら、最後に「全部」を加えます。",
                     "候補がなければ、「作業フォルダの中に、設定できるファイルが見つかりませんでした。」",
                     "ファイルの場所を ask_user で聞きます。選択肢は付けません。",
                     "`unreadable`"):
            self.assertIn(word, files, word)
        tool = self.section("## 道具", "### 値の渡し方")
        self.assertIn("作業フォルダと、その下 3 段までのフォルダを探します。", tool)
        self.assertIn("項目のすぐ上に `#` の説明があるファイルが候補です。", tool)

    def test_no_data_folder(self):
        """このスキルは、作業フォルダに、自分のフォルダを持たない。"""
        self.assertNotRegex(self.text, r"`\.[\w-]+/`")
        self.assertNotIn("config.yaml", self.text)

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
        self.assertIn("「空のまま」（`can_be_empty` が true で、今の値が空のとき）", self.body)

    def test_empty_options_only_when_empty_is_allowed(self):
        """選べる値の決まった項目（空は選べない）に「空にする」を出した。"""
        section = self.section("### 4. 1つの項目の聞き方", "### 5. 答えを書き込む")
        for word in ("「空にする」（`can_be_empty` が true で",
                     "「あとで決める」（`can_be_empty` が false で、今の値が空のとき）",
                     "`can_be_empty` が false の項目には、「空のまま」「空にする」を出しません。",
                     "`choices` がある項目は、空の値が `choices` にあるときだけ出します。"):
            self.assertIn(word, section, word)
        self.assertNotIn("（必須でなく、今の値が空でない", self.body)
        self.assertIn("`can_be_empty`", self.section("`items` の1つ1つには", "## 利用者に見せる言葉"))
        tool = self.section("## 道具", "### 値の渡し方")
        self.assertIn("空の値も、`choices` に空の値がなければ書きません。", tool)

    def test_choice_order_puts_values_before_staying_empty(self):
        """「空のまま」が先頭（既定）だと、Enter だけで大事な項目が空のままになる。
        並びは、今の値 → 書ける値 → 空のまま。"""
        section = self.section("### 4. 1つの項目の聞き方", "### 5. 答えを書き込む")
        current = section.index("「今の値のまま：<value>」")
        values = section.index("「<値> と書く」")
        empty = section.index("「空のまま」（`can_be_empty`")
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

    def test_fill_recommended_asks_for_the_users_own_value(self):
        """ある環境だけの例の値を（おすすめ）にすると、その値を書けばよいと読める。"""
        section = self.section("今の値が空で、`fill_recommended` が true の項目は", "### 5. 答えを書き込む")
        for word in ("質問の文には、コメントの「必ず書く」の文を、省かずに入れます。",
                     "1番目の選択肢は「自分の値を書く（おすすめ）」にします。",
                     "「<値> と書く（例）」",
                     "例の値に「（おすすめ）」は付けません。",
                     "「<key> に書く値を書いてください。」",
                     "「自由に書かれた答え」のとおりに書き込みます"):
            self.assertIn(word, section, word)
        self.assertNotIn("`examples` の1つ目を1番目に置きます", self.body)
        self.assertNotIn("と書く（おすすめ）」", self.body)
        self.assertIn("`fill_recommended`", self.section("`items` の1つ1つには", "## 利用者に見せる言葉"))

    def test_option_values_are_the_shown_text(self):
        """選択肢の値が英語の短い名前だと、それが会話の記録に出る。"""
        section = self.section("## 質問のしかた", "### 質問が取り消されたとき")
        for word in ("選択肢の値は、見える文と同じ日本語の文にします。",
                     "値と見える文を分けて書けるときも、同じ文にします。",
                     "選ばれた値を、そのまま会話の記録に出す",
                     "英語の短い名前や記号を、値に使いません。"):
            self.assertIn(word, section, word)

    def test_default_option_rule(self):
        """「空のまま」が既定だと、Enter だけで空のままになる。"""
        section = self.section("## 質問のしかた", "### 質問が取り消されたとき")
        for word in ("既定の選択肢（Enter だけで選ばれるもの）は、ふつうは付けません。",
                     "付けるときは、1番目の選択肢だけを既定にします。",
                     "「空のまま」「空にする」は、既定にしません。"):
            self.assertIn(word, section, word)

    def test_env_partner_items_are_not_asked_alone(self):
        """環境変数の名前の項目を、表に出したり単独で聞いたりした。"""
        order = self.section("### 3. 聞く順番", "### 4. 1つの項目の聞き方")
        for word in ("`ask_alone` が false の項目は、1〜5 のどれでも扱いません。",
                     "表にも、「直す項目を選ぶ」の選択肢にも入れません。N にも数えません。",
                     "もとの項目で「環境変数で渡す」を選んだときだけ聞きます。"):
            self.assertIn(word, order, word)
        self.assertIn("`ask_alone`", self.section("`items` の1つ1つには", "## 利用者に見せる言葉"))

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
        order = self.section("### 3. 聞く順番", "### 4. 1つの項目の聞き方")
        # まだ何も聞いていないときに「ほかの」と言うと、何のほかかが分からない。
        self.assertIn("聞いた項目があるとき：「ほかの N 個の項目は、今の値のままでよいですか。」", order)
        self.assertIn("まだ1つも聞いていないとき：「この N 個の項目は、今の値のままでよいですか。」", order)
        self.assertIn("表の前の文でも「ほかの」を使いません。", order)
        self.assertIn("列は「項目」「今の値」「説明」です。", self.body)

    def test_rewriting_by_hand_waits_for_the_user(self):
        """自分で書き換えると答えたのに、待たずに先へ進むと、書く時間がない。"""
        section = self.section("- 「自分でファイルを書き換える」なら", "### 7. 終わりの報告")
        for word in ("ほかのコマンドは実行しません。次の項目にも進みません。",
                     "「書き換えたら教えてください。」", "「書き換えた」「あとでやる」の2つ",
                     "`show` でファイルを読み直します", "「あとで書き換える項目」"):
            self.assertIn(word, section, word)
        # 前から値があったパスワードは、空かどうかでは書き換わったか分からない。
        for word in ("`empty` と `value_digest` を、書き換える前と比べます。",
                     "「<key> は、まだ前の値のままです。」",
                     "「<key> は、まだ空のままです。」",
                     "2 をもう一度聞きます。",
                     "「前の値のままにする」",
                     "書き換わったと確かめられないときに、書き換わったとは言いません。"):
            self.assertIn(word, section, word)
        self.assertNotIn("空でなくなっていれば、書き換わった", self.body)
        self.assertIn("`value_digest`", self.section("`items` の1つ1つには", "## 利用者に見せる言葉"))
        self.assertIn("利用者には見せません。", self.section("`value_digest` は、", "## 利用者に見せる言葉"))
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
        """呼び出し元が続けて別の作業をすると、報告が抜けた。"""
        section = self.body[self.body.index("### 7. 終わりの報告"):self.body.index("## 守ること")]
        for word in ("呼び出し元がこの後に別の作業を続けるときも、省きません。",
                     "この報告が、このスキルでする最後のことです。",
                     "チャットの文として、利用者に必ず見せます。",
                     "`show` と、ほかのコマンドやツールを、一緒に呼びません。",
                     "報告を書く前に、ほかのコマンドやツールを呼びません。",
                     "`table` を、そのまま貼ります"):
            self.assertIn(word, section, word)

    def test_caller_may_continue_after_the_report(self):
        """報告を「手番を返す前の最後」と書くと、呼び出し元まで手番を終えた。"""
        section = self.section("### 7. 終わりの報告", "## 守ること")
        self.assertNotIn("手番を返す前", self.body)
        for word in ("報告の後は、呼び出し元の作業に戻ります。",
                     "呼び出し元は、報告の後に、自分の作業を続けてかまいません。",
                     "このスキルの報告で、呼び出し元の手番を終えることはしません。"):
            self.assertIn(word, section, word)

    def test_table_cut_is_described(self):
        section = self.section("### 7. 終わりの報告", "## 守ること")
        self.assertIn("真ん中を「…」で省いて、60 文字ほどにしてあります。", section)
        self.assertNotIn("40 文字で切り", self.body)

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
        """利用者が正しく書いた答えに、例に合わせて直した案を出した。"""
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


class ReadmeTest(unittest.TestCase):
    """人向けの使い方（README.md）。このフォルダだけを受け取った人が読む。"""

    def setUp(self):
        self.text = read(README)

    def test_sections(self):
        for heading in ("## 何をするスキルか", "## 必要なもの", "## インストールする", "## 使い方",
                        "## 埋められるファイル", "## 候補のファイルの探し方", "## 結果の見方",
                        "## うまくいかないとき", "## テストを実行する"):
            self.assertIn(heading, self.text, heading)
        self.assertIn("Python 3.8 以上", self.text)
        # スキルの名前は書き換えられることがあるので、名前を除いた形で確かめる
        self.assertIn("`.github/skills/", self.text)
        self.assertIn("`~/.copilot/skills/", self.text)
        self.assertIn("`/skills reload`", self.text)
        self.assertIn("フォルダの名前を変えてコピーするときは、`SKILL.md` の `name:` も、フォルダと同じ名前に変えます。",
                      self.text)

    def test_test_command_runs_from_this_folder(self):
        self.assertIn("このフォルダで、次を実行します。\n\n```bash\npython3 -m unittest discover -s tests -v\n```",
                      self.text)
        # 試験で、このフォルダに __pycache__ を残さない動かし方も示す
        self.assertIn("`python3 -B -m unittest discover -s tests -v`", self.text)

    def test_search_rules_match_the_tool(self):
        section = self.text[self.text.index("## 候補のファイルの探し方"):self.text.index("## 結果の見方")]
        for name in tool_constant("SKIP_DIRS"):
            self.assertIn("`{}`".format(name), section, name)
        self.assertIn("その下 {} 階層までのフォルダを探します。".format(tool_constant("SEARCH_DEPTH")), section)
        self.assertIn("名前がドットで始まるフォルダ", section)
        self.assertIn("このスキルのフォルダ", section)

    def test_example_yaml_can_be_filled(self):
        """README の例のファイルを、道具がそのとおりに読めること。"""
        blocks = re.findall(r"```yaml\n(.*?)```", self.text, re.S)
        self.assertEqual(len(blocks), 1)
        work = tempfile.mkdtemp(prefix="setup-test-readme-")
        self.addCleanup(shutil.rmtree, work, True)
        path = os.path.join(work, "example.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(blocks[0])
        out = subprocess.run([sys.executable, TOOL, "show", path], stdout=subprocess.PIPE, timeout=60).stdout
        data = json.loads(out.decode("utf-8"))
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["header"])
        it = {i["key"]: i for i in data["items"]}
        self.assertTrue(it["name"]["required"])
        self.assertEqual(it["name"]["examples"], ["東の温室"])
        self.assertEqual(it["interval_seconds"]["type"], "number")
        self.assertEqual(it["unit"]["choices"], ["c", "f"])
        self.assertEqual(it["notify_to"]["type"], "list")
        self.assertTrue(it["password"]["secret"])
        self.assertEqual(it["password"]["env_alternative"], "password_env")
        self.assertFalse(it["password_env"]["ask_alone"])
        listed = subprocess.run([sys.executable, TOOL, "list"], cwd=work, stdout=subprocess.PIPE, timeout=60).stdout
        self.assertEqual(json.loads(listed.decode("utf-8"))["files"], [path])


class FolderTest(unittest.TestCase):
    def test_no_reference_outside_own_folder(self):
        for path in skill_files():
            self.assertNotIn(PARENT_REF, read(path), path)

    def test_only_stdlib_imports(self):
        tree = ast.parse(read(TOOL))
        allowed = {"json", "math", "os", "re", "sys", "tempfile", "traceback", "unicodedata", "errno",
                   "hashlib"}
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
        self.assertEqual(rel, ["README.md", "SKILL.md", "scripts/config_tool.py",
                               "tests/__init__.py",
                               "tests/fixtures/coffee-machine.yaml", "tests/fixtures/network-camera.yaml",
                               "tests/test_config_tool.py", "tests/test_skill_files.py"])


if __name__ == "__main__":
    unittest.main()
