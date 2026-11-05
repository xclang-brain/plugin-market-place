# -*- coding: utf-8 -*-
"""このスキルのフォルダそのもののテスト。

- SKILL.md の先頭の情報（name・description）があること。
- 対話の進め方に要ることが書いてあること（スキルを探す → README を読む → 設定ファイルを用意する → 埋める）。
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
import shutil
import sys
import tempfile
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


def tool_search_order():
    """道具が探す場所を、Git のリポジトリを模した一時フォルダで調べる。(where, 場所の形) の組。"""
    tool = load_tool_module()
    base = tempfile.mkdtemp(prefix="setup-test-order-")
    try:
        work = os.path.join(base, "work")
        os.makedirs(os.path.join(work, ".git"))
        with open(os.path.join(work, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/main\n")
        # このスキルが ~/.copilot/skills/ にあっても同じ結果になるよう、置き場所を一時フォルダにする
        tool.SKILL_DIR = os.path.join(base, "plugin", "skills", SKILL_NAME)
        home = os.path.expanduser("~")
        names = {os.path.join(tool.copilot_home(), "skills"): "<copilot-home>/skills",
                 os.path.join(home, ".agents", "skills"): "<home>/.agents/skills",
                 os.path.dirname(tool.SKILL_DIR): "<the folder next to this skill>"}
        out = []
        for where, place in tool.skill_places(work):
            if place in names:
                out.append((where, names[place]))
            else:
                out.append((where, "<work>/" + os.path.relpath(place, work).replace(os.sep, "/")))
        return out
    finally:
        shutil.rmtree(base, True)


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
        # どのスキルでも設定できるように、スキルの README から設定を知ると書く
        self.assertIn("README", desc)
        self.assertIn("「セットアップしたい」", desc)
        # スラッシュコマンドでも呼べることは、コマンドの名前を書かずに示す
        self.assertIn("スラッシュコマンドで、このスキルだけを呼んでも使える。", desc)
        self.assertEqual(set(self.fields), {"name", "description"})
        # 「/名前:スキル名」の形の呼び方は書かない（入れ物の名前で変わる）
        self.assertNotRegex(self.text, r"/[\w.-]+:[\w.-]+")

    def test_skills_are_chosen(self):
        """名前が渡されればそのスキル、なければ skills で探して、利用者に選んでもらう。"""
        part = self.section("### 1. 設定するスキルを決める", "### 2. README を読む")
        for word in ("まず `skills` を実行します。",
                     "頼みにスキルの名前があれば、その名前のスキルを、示された順に設定します。",
                     "`hidden` が false のものを設定します。",
                     "「<名前> というスキルが見つかりませんでした。」",
                     "候補が1つでも聞きます。",
                     "「どのスキルを設定しますか。」",
                     "「<name>：<description の1文目>」",
                     "`hidden` が true のスキルは、選択肢に入れません。",
                     "候補が2つ以上なら、最後に「全部」を加えます。",
                     "「設定できるスキルが見つかりませんでした。」"):
            self.assertIn(word, part, word)
        # 設定ファイルの場所は、利用者にも呼び出し元にも聞かない
        self.assertNotIn("ファイルの場所を ask_user で聞きます", self.body)
        rules = self.section("## 守ること", "質問のツール（ask_user）が使えないとき")
        self.assertIn("設定ファイルの場所は、README から知ります。利用者や呼び出し元に、場所を聞きません。", rules)

    def test_skills_search_order_matches_the_tool(self):
        """SKILL.md と README に書いた探す順が、道具の探す順と同じであること。"""
        part = self.section("### `skills` の結果", "### `show` の結果")
        lines = ["1. `.github/skills/`（`where` は `project`）",
                 "2. `.agents/skills/`（`where` は `project`）",
                 "3. `.claude/skills/`（`where` は `project`）",
                 "4. 環境変数 `COPILOT_HOME`（なければ `~/.copilot`）の `skills/`（`where` は `personal`）",
                 "5. `~/.agents/skills/`（`where` は `personal`）",
                 "6. このスキルのフォルダの隣のフォルダ（`where` は `sibling`）"]
        for line in lines:
            self.assertIn(line, part, line)
        for a, b in zip(lines, lines[1:]):
            self.assertLess(part.index(a), part.index(b))
        self.assertIn("1〜3 は、作業フォルダから上のフォルダへ、Git のリポジトリの一番上まで探します。", part)
        self.assertEqual(tool_search_order(), [
            ("project", "<work>/.github/skills"), ("project", "<work>/.agents/skills"),
            ("project", "<work>/.claude/skills"), ("personal", "<copilot-home>/skills"),
            ("personal", "<home>/.agents/skills"), ("sibling", "<the folder next to this skill>")])
        self.assertIn("このスキル自身は返しません。", part)
        for name in ("`name`", "`path`", "`where`", "`readme`", "`skill_md`", "`description`",
                     "`hidden`", "`hidden_by`", "`searched`", "`unreadable`"):
            self.assertIn(name, part, name)

    def test_readme_is_read_and_nothing_is_guessed(self):
        """README に設定ファイルの説明がなくても、SKILL.md にあれば設定できる。"""
        part = self.section("### 2. README を読む", "### 3. 設定ファイルを用意する")
        for word in ("`readme` が null か、`readme` に設定ファイルの説明がなければ、`skill_md` も読みます。",
                     "ここから先の「README」は、読んだ README と SKILL.md のことです。",
                     "設定ファイルの場所", "設定ファイルがないときの作り方", "項目の意味",
                     "書いてないことは、推測しません。",
                     "「<name> の README と SKILL.md には、設定ファイルの説明がありません。」"):
            self.assertIn(word, part, word)

    def test_config_file_is_created_the_way_the_readme_says(self):
        part = self.section("### 3. 設定ファイルを用意する", "### 4. 項目を埋める")
        for word in ("`\"exists\": false` なら、README に書かれた作り方で作ります。",
                     "そのスキルの `path` に置き換えます。",
                     "Python の呼び方（`python3` など）は、`<python>` に合わせます。",
                     "作り方が書いてなければ、作りません。",
                     "YAML のファイルを作ったら、もう一度 `show` を実行します。"):
            self.assertIn(word, part, word)
        tool = self.section("## 道具", "### `skills` の結果")
        self.assertIn("ファイルがないとき、`show` の結果には `\"exists\": false` が入ります。", tool)
        # --workdir は、サブコマンドの前でも後でもよい（道具がどちらも受け付ける）
        self.assertIn("サブコマンドの前でも後でもかまいません。", tool)

    def test_other_formats_are_filled_from_the_readme(self):
        """「項目: 値」だけの .conf も show で読めてしまう。YAML かは README と名前で決める。
        ファイルを読めないときは、手で書き換えずに飛ばす（文字コードを壊さない）。"""
        prepare = self.section("### 3. 設定ファイルを用意する", "### 4. 項目を埋める")
        self.assertIn("YAML のファイルとは、README に YAML と書いてあるか、名前が `.yaml`・`.yml` で終わるファイルです。",
                      prepare)
        part = self.section("### 4. 項目を埋める", "#### 4-1. 聞く順番")
        for word in ("`\"ok\": false` で `\"unsupported\": true` がなければ、ファイルの問題です",
                     "`error` を利用者に伝えて、次のスキルに進みます。",
                     "YAML のファイルで `\"ok\": true` なら、4-1〜4-5 のとおりに、`show` と `set` で埋めます。",
                     "`comment` が空の項目の意味は、README から読み取ります。",
                     "YAML でないファイルと、`\"unsupported\": true` のファイルです。"):
            self.assertIn(word, part, word)
        self.assertNotIn("`\"ok\": true` なら、YAML の設定ファイルです。", self.body)
        tool = self.section("## 道具", "### `skills` の結果")
        self.assertIn("`\"unsupported\": true`", tool)
        other = self.section("#### 4-6. `show` と `set` を使わないファイル", "### 5. 終わりの報告")
        for word in ("項目のすぐ上のコメントと README の説明をもとに埋めます。",
                     "YAML のときと同じく、「必須」「必ず」「例：」「選べる値：」の言葉で決めます。",
                     "聞く順番・表・質問の決まりは、YAML のときと同じです（4-1〜4-4）。",
                     "ファイルの編集のツールで、その項目の値の部分だけを書き換えます。",
                     "コメント・空行・項目の並びは変えません。",
                     "ファイルを書き換えられないときは、エラーを伝えて、次のスキルに進みます。"):
            self.assertIn(word, other, word)

    def test_show_is_used_only_for_yaml(self):
        """INI に show を使うと、失敗と「「項目: 値」の形で書いて」が画面に出る。形式を先に決める。"""
        prepare = self.section("### 3. 設定ファイルを用意する", "### 4. 項目を埋める")
        for word in ("まず、README と拡張子で、ファイルの形式を決めます。",
                     "YAML なら、`show <設定ファイル>` を実行して、ファイルがあるかを確かめます。",
                     "YAML でなければ、`show` を使いません。"):
            self.assertIn(word, prepare, word)
        self.assertLess(prepare.index("ファイルの形式を"), prepare.index("`show <設定ファイル>`"))
        self.assertIn("設定ファイルの形式", self.section("### 2. README を読む", "### 3. 設定ファイルを用意する"))
        self.assertIn("YAML の設定ファイルを読み書きするとき（`show`・`get`・`set`）は、このスキルの道具を使います。",
                      self.section("## 道具", "### `skills` の結果"))
        # 道具の事情（読めない書き方など）は、利用者に話さない
        self.assertIn("道具の事情（読めない書き方、形式による扱いの違いなど）は話しません。",
                      self.section("## 利用者に見せる言葉", "## 質問のしかた"))

    def test_secrets_in_other_formats_are_not_read_back(self):
        """show のない形式では、書き換わったかを確かめるには、ファイルの値を読むしかない。読まずに伝える。"""
        other = self.section("#### 4-6. `show` と `set` を使わないファイル", "### 5. 終わりの報告")
        for word in ("パスワードなどの項目（名前か README から分かるもの）は、4-4 のとおりに聞きます。",
                     "値は、チャットにも表にも書きません。",
                     "ファイルを読み直して確かめません。",
                     "「<key> が書き換わったかは、確かめていません。」",
                     "表の値は「（確かめていません）」にします。"):
            self.assertIn(word, other, word)
        report = self.section("### 5. 終わりの報告", "## 守ること")
        self.assertIn("4-6 で書き換わったかを確かめていない値は、「（確かめていません）」にします。", report)

    def test_commands_work_on_windows(self):
        """Windows の Copilot CLI はコマンドを PowerShell で動かす。bash だけの書き方に頼らない。"""
        rules = self.section("## コマンドの書き方", "## 道具")
        self.assertIn("`<python>` は、Windows では `py -3`（ない場合は `python`）、それ以外では `python3` です。", rules)
        self.assertIn("Windows では、コマンドは PowerShell で動きます。", rules)
        # 道具を呼ぶ行は、どれも <python> <このスキルのフォルダ>/scripts/config_tool.py の形
        for line in self.body.split("\n"):
            if "config_tool.py" in line and line.lstrip().startswith(("<python>", "python", "py ")):
                self.assertTrue(line.lstrip().startswith("<python> <このスキルのフォルダ>/scripts/config_tool.py"), line)
            if "config_tool.py" in line and "'@ |" in line:
                self.assertIn("| <python> <このスキルのフォルダ>/scripts/config_tool.py set", line)
        self.assertNotRegex(self.body, r"(?m)^\s*python3 ")
        # set の値の渡し方は、PowerShell の here-string と bash のヒアドキュメントの両方を示す
        tool = self.section("### 値の渡し方（`set`）", "## 利用者に見せる言葉")
        self.assertIn("```powershell\n@'\n<値>\n'@ | <python> <このスキルのフォルダ>/scripts/config_tool.py set <ファイル> <項目> --stdin\n```",
                      tool)
        self.assertIn("--stdin <<'VALUE_END'\n<値>\nVALUE_END\n```", tool)
        self.assertLess(tool.index("```powershell"), tool.index("```bash"))  # Windows を先に
        self.assertIn("PowerShell では、`@'` は行の最後に、`'@` は行の先頭に書きます。", tool)
        secret = self.section("#### 4-4. パスワードなど", "#### 4-5. 書き終えたら")
        self.assertIn("PowerShell：`$env:名前 = '値'`", secret)
        self.assertIn("bash：`export 名前=値`", secret)

    def test_no_data_folder(self):
        """このスキルは、作業フォルダに、自分のフォルダを持たない。"""
        self.assertNotRegex(self.text, r"`\.[\w-]+/`")
        self.assertNotIn("config.yaml", self.text)

    def test_dialogue_is_described(self):
        body = self.body
        for word in ("ask_user", "`choices`",
                     "config_tool.py", "skills", "show", "set", "--json", "--stdin",
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
        section = self.section("#### 4-2. 1つの項目の聞き方", "#### 4-3. 答えを書き込む")
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
        section = self.section("#### 4-2. 1つの項目の聞き方", "#### 4-3. 答えを書き込む")
        current = section.index("「今の値のまま：<value>」")
        values = section.index("「<値> と書く」")
        empty = section.index("「空のまま」（`can_be_empty`")
        self.assertLess(current, values)
        self.assertLess(values, empty)
        self.assertIn("「空のまま」は、書ける値より前に置きません。", section)

    def test_choices_show_the_value_that_is_written(self):
        """「例：<値>」の選択肢では、「例：」まで書かれるのかが分からない。"""
        section = self.section("#### 4-2. 1つの項目の聞き方", "#### 4-3. 答えを書き込む")
        self.assertIn("どれも「<値> と書く」の形にします。", section)
        self.assertIn("「例：」は付けません。", section)
        self.assertNotIn("「例：<値>」", self.body)
        write = self.section("#### 4-3. 答えを書き込む", "#### 4-4. パスワードなど")
        self.assertIn("「<値> と書く」を選んだら", write)

    def test_values_are_typed_in_the_input_field(self):
        """「自分の値を書く（おすすめ）」を選ぶと、もう1回質問が出た（Other と同じことが2通り）。
        値は Copilot CLI の入力欄（Other）に書いてもらう。例の値には、いつも「（例）」を付ける。"""
        # 「自分の値を書く」は、足さない選択肢として名前を挙げるだけ
        lines = [x for x in self.body.split("\n") if "自分の値を書く" in x]
        self.assertEqual([x.strip() for x in lines], ["「その他」「自分の値を書く」のような選択肢は足しません。"])
        self.assertNotIn("に書く値を書いてください。", self.body)
        ask = self.section("## 質問のしかた", "### 質問が取り消されたとき")
        self.assertIn("下の入力欄（Other）に自由に書けます。", ask)
        self.assertIn("「その他」「自分の値を書く」のような選択肢は足しません。", ask)
        section = self.section("#### 4-2. 1つの項目の聞き方", "#### 4-3. 答えを書き込む")
        for word in ("「値は、下の入力欄（Other）にも書けます。」と足します。",
                     "`examples` の値。「<値> と書く（例）」の形にします。",
                     "例の値に「（おすすめ）」は付けません。"):
            self.assertIn(word, section, word)
        self.assertNotIn("と書く（おすすめ）」", self.body)
        self.assertNotIn("`examples` の1つ目を1番目に置きます", self.body)

    def test_fill_recommended_sentence_is_kept(self):
        """「必ず書く」の文を省くと、空のままでよい項目に見える。"""
        section = self.section("#### 4-2. 1つの項目の聞き方", "#### 4-3. 答えを書き込む")
        self.assertIn("今の値が空で、`fill_recommended` が true なら、コメントの「必ず書く」の文を、省かずに入れます。",
                      section)
        self.assertIn("`fill_recommended`", self.section("`items` の1つ1つには", "## 利用者に見せる言葉"))

    def test_recommended_items_are_asked_one_by_one(self):
        """「必ず書く」項目を「今の値のままでよいですか」にまとめると、機器で動かない設定のまま終わった。"""
        order = self.section("#### 4-1. 聞く順番", "#### 4-2. 1つの項目の聞き方")
        required = order.index("2. 必須で空の項目（`required` と `empty` がどちらも true）を、ファイルの順に聞きます。")
        recommended = order.index("3. 次に、推奨で空の項目（`fill_recommended` と `empty` がどちらも true）を、"
                                  "ファイルの順に1つずつ聞きます。")
        rest = order.index("4. ほかの項目をどうするかを、ask_user で1回だけ聞きます。")
        self.assertLess(required, recommended)
        self.assertLess(recommended, rest)
        # 表の説明には、1行目のほかに「必ず」の文も入れる
        self.assertIn("「必ず」を含む文が1行目になければ、その文も足します。", order)
        # 空の推奨の項目は 3 で全部聞くので、表（「今の値のままにする」の質問）には残らない。
        # 当てはまる場面のない決まりは置かない（「ファイルに空で残っている」と読まれる）
        self.assertNotIn("推奨で空の項目が残っているときは", order)

    def test_previous_answers_are_offered(self):
        """前のスキルで答えた機器の IP アドレスを、次のスキルでもう一度書かされた。"""
        section = self.section("#### 4-2. 1つの項目の聞き方", "#### 4-3. 答えを書き込む")
        first = section.index("1. 「<値> と書く（前に答えた値）」")
        self.assertLess(first, section.index("2. 「今の値のまま：<value>」"))
        for word in ("前に答えた値：",
                     "この項目と同じ意味のものがあれば、選択肢の1番目に出します。",
                     "両方の項目のコメントと README で決めます。",
                     "`secret` が true の項目の値は、出しません。",
                     # choices のない項目（IP アドレスなど）でも出す
                     "`choices` がある項目では、`choices` にない値も出しません。",
                     # examples や true/false と同じ値を、2つ並べない
                     "今の値・前に答えた値と同じものは入れません。",
                     "書くのは、利用者がこの選択肢を選んだときだけです。"):
            self.assertIn(word, section, word)
        write = self.section("#### 4-3. 答えを書き込む", "#### 4-4. パスワードなど")
        self.assertIn("「（例）」「（前に答えた値）」は書きません。", write)

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
        order = self.section("#### 4-1. 聞く順番", "#### 4-2. 1つの項目の聞き方")
        for word in ("`ask_alone` が false の項目は、1〜6 のどれでも扱いません。",
                     "表にも、「直す項目を選ぶ」の選択肢にも入れません。N にも数えません。",
                     "もとの項目で「環境変数で渡す」を選んだときだけ聞きます。"):
            self.assertIn(word, order, word)
        self.assertIn("`ask_alone`", self.section("`items` の1つ1つには", "## 利用者に見せる言葉"))

    def test_file_only_phrases_are_rephrased(self):
        """コメントの「下の」「このスキル」は、チャットでは何を指すか分からない。"""
        section = self.section("## 利用者に見せる言葉", "## 質問のしかた")
        for word in ("「下の」", "「上の」", "「このスキル」", "「この項目」", "名前で言い換えます"):
            self.assertIn(word, section, word)
        ask = self.section("#### 4-2. 1つの項目の聞き方", "#### 4-3. 答えを書き込む")
        self.assertIn("「利用者に見せる言葉」のとおりに言い換えてから使います", ask)
        secret = self.section("#### 4-4. パスワードなど", "#### 4-5. 書き終えたら")
        self.assertIn("「利用者に見せる言葉」のとおりに言い換えてから使います", secret)

    def test_json_names_are_not_shown(self):
        section = self.section("## 利用者に見せる言葉", "## 質問のしかた")
        self.assertIn("JSON の名前", section)
        self.assertIn("利用者に見せません", section)
        self.assertIn("項目の名前（`key`）は、見せてかまいません", section)

    def test_one_skill_at_a_time(self):
        """必須の項目を全部のスキルで先に聞くと、ファイルを2回行き来する。"""
        section = self.section("### 1. 設定するスキルを決める", "### 2. README を読む")
        self.assertIn("1つのスキルについて、2〜4 を全部終えてから、次のスキルに進みます。", section)
        self.assertIn("ほかのスキルの項目を、先にまとめて聞くことはしません。", section)

    def test_named_items_only(self):
        """1つの項目を直したいと頼んだのに、ほかの項目もまとめて聞かれた。"""
        order = self.section("#### 4-1. 聞く順番", "#### 4-2. 1つの項目の聞き方")
        named = order.index("直したい項目の名前を示したときは、その項目だけを聞きます。")
        self.assertLess(named, order.index("必須で空の項目"))
        self.assertIn("2〜6 はしません。ほかの項目は、必須で空でも聞きません。", order)
        self.assertIn("「<ファイル> に <名前> という項目はありません。」", order)

    def test_wording_of_the_rest_question(self):
        self.assertNotIn("説明の1行目", self.body)
        self.assertNotIn("残りの N 個", self.body)
        order = self.section("#### 4-1. 聞く順番", "#### 4-2. 1つの項目の聞き方")
        # まだ何も聞いていないときに「ほかの」と言うと、何のほかかが分からない。
        self.assertIn("聞いた項目があるとき：「ほかの N 個の項目は、今の値のままでよいですか。」", order)
        self.assertIn("まだ1つも聞いていないとき：「この N 個の項目は、今の値のままでよいですか。」", order)
        self.assertIn("表の前の文でも「ほかの」を使いません。", order)
        self.assertIn("列は「項目」「今の値」「説明」です。", self.body)

    def test_rewriting_by_hand_waits_for_the_user(self):
        """自分で書き換えると答えたのに、待たずに先へ進むと、書く時間がない。"""
        section = self.section("- 「自分でファイルを書き換える」なら", "#### 4-5. 書き終えたら")
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
        report = self.section("### 5. 終わりの報告", "## 守ること")
        self.assertIn("「あとで書き換える項目：<key>」", report)

    def test_cancel_and_decline_are_handled(self):
        section = self.body[self.body.index("### 質問が取り消されたとき"):self.body.index("## 進め方")]
        for word in ("Esc", "断った", "それ以上は聞きません", "書き換えた項目", "`changed`",
                     "5. 終わりの報告", "ファイルの場所"):
            self.assertIn(word, section, word)

    def test_final_report_is_always_given(self):
        section = self.body[self.body.index("### 5. 終わりの報告"):self.body.index("## 守ること")]
        self.assertIn("どんな終わり方でも、最後に必ずこの報告をします", section)
        self.assertIn("途中で止めたときも同じです", section)

    def test_final_report_is_shown_in_chat_before_anything_else(self):
        """呼び出し元が続けて別の作業をすると、報告が抜けた。"""
        section = self.body[self.body.index("### 5. 終わりの報告"):self.body.index("## 守ること")]
        for word in ("呼び出し元がこの後に別の作業を続けるときも、省きません。",
                     "この報告が、このスキルでする最後のことです。",
                     "チャットの文として、利用者に必ず見せます。",
                     "`show` と、ほかのコマンドやツールを、一緒に呼びません。",
                     "報告を書く前に、ほかのコマンドやツールを呼びません。",
                     "`table` を、そのまま貼ります"):
            self.assertIn(word, section, word)

    def test_caller_may_continue_after_the_report(self):
        """報告を「手番を返す前の最後」と書くと、呼び出し元まで手番を終えた。"""
        section = self.section("### 5. 終わりの報告", "## 守ること")
        self.assertNotIn("手番を返す前", self.body)
        for word in ("報告の後は、呼び出し元の作業に戻ります。",
                     "呼び出し元は、報告の後に、自分の作業を続けてかまいません。",
                     "このスキルの報告で、呼び出し元の手番を終えることはしません。"):
            self.assertIn(word, section, word)

    def test_table_cut_is_described(self):
        section = self.section("### 5. 終わりの報告", "## 守ること")
        self.assertIn("真ん中を「…」で省いて、60 文字ほどにしてあります。", section)
        self.assertNotIn("40 文字で切り", self.body)

    def test_values_are_always_passed_through_stdin(self):
        """値を引数に書くと、値の中のパスに Copilot CLI が許可を求め、値もコマンドの行に出る。"""
        tool = self.section("## 道具", "## 利用者に見せる言葉")
        self.assertIn("値は、いつも標準入力で渡します。コマンドの引数には書きません。", tool)
        self.assertIn("--stdin <<'VALUE_END'", tool)
        self.assertIn("`set <ファイル> <項目> --stdin --json`", tool)
        self.assertIn("空の値を書くときは、`<値>` の行を書きません。", tool)
        self.assertIn("'@ | <python>", tool)
        # 値を引数に書く書き方は、どこにも示さない
        self.assertNotRegex(self.body, r"set <ファイル> <項目> <値>")
        self.assertNotRegex(self.body, r"set <ファイル> <項目> '")
        for line in self.body.split("\n"):
            if "config_tool.py set" in line:
                self.assertIn("--stdin", line, line)
        write = self.section("#### 4-3. 答えを書き込む", "#### 4-4. パスワードなど")
        self.assertIn("値は、いつも `--stdin` で渡します", write)
        self.assertNotIn("値を ' で囲みます", self.body)
        rules = self.section("## 守ること", "質問のツール（ask_user）が使えないとき")
        self.assertIn("`set` の値は、コマンドの引数に書きません。いつも `--stdin` で渡します。", rules)

    def test_free_text_is_written_as_typed(self):
        """利用者が正しく書いた答えに、例に合わせて直した案を出した。"""
        write = self.section("#### 4-3. 答えを書き込む", "#### 4-4. パスワードなど")
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

    def section(self, start, end):
        return self.text[self.text.index(start):self.text.index(end)]

    def test_sections(self):
        for heading in ("## できること", "## 必要なもの", "## インストールする", "## 使い方",
                        "## スキルの探し方", "## 設定されるスキルの書き方", "## パスワードなどの項目",
                        "## うまくいかないとき", "## テストを実行する"):
            self.assertIn(heading, self.text, heading)
        need = self.section("## 必要なもの", "## インストールする")
        self.assertIn("Windows の PC", need)
        self.assertIn("Python 3.8 以上", need)
        # 確かめたことと、確かめていないことを書く（ほかのスキルの README と同じ文）
        self.assertIn("Windows では、GitHub Actions でテストを確認しています。"
                      "Windows の Copilot CLI との組み合わせは、まだ確認していません。", need)
        # 個人のフォルダは、Windows の場所を先に書く
        install = self.section("## インストールする", "## 使い方")
        self.assertLess(install.index("`%USERPROFILE%\\.copilot\\skills\\"), install.index("`~/.copilot/skills/"))
        # スキルの名前は書き換えられることがあるので、名前を除いた形で確かめる
        self.assertIn("`.github/skills/", self.text)
        self.assertIn("`~/.copilot/skills/", self.text)
        self.assertIn("`/skills reload`", self.text)
        self.assertIn("フォルダの名前を変えてコピーするときは、`SKILL.md` の `name:` も、フォルダと同じ名前に変えます。",
                      self.text)
        usage = self.section("## 使い方", "## スキルの探し方")
        self.assertIn("「セットアップしたい」", usage)
        self.assertIn("`/setup`", usage)
        self.assertIn("を直したい` | その項目だけを質問します |", usage)

    def test_test_command_runs_from_this_folder(self):
        part = self.section("## テストを実行する", "最後に `OK` と出れば成功です。")
        self.assertIn("このフォルダで、次を実行します", part)
        self.assertIn("```\npy -3 -m unittest discover -s tests -v\n```", part)  # Windows を先に
        self.assertIn("`python3`", part)
        # テストで、このフォルダに __pycache__ を残さない動かし方も示す
        self.assertIn("`-B`", self.text[self.text.index("## テストを実行する"):])

    def test_search_rules_match_the_tool(self):
        part = self.section("## スキルの探し方", "## 設定されるスキルの書き方")
        # 個人のフォルダは、Windows の場所を先に書く
        places = ["1. `.github/skills/`", "2. `.agents/skills/`", "3. `.claude/skills/`",
                  "4. `%USERPROFILE%\\.copilot\\skills\\`（Linux では `~/.copilot/skills/`",
                  "5. `%USERPROFILE%\\.agents\\skills\\`（Linux では `~/.agents/skills/`）",
                  "6. このフォルダの隣のフォルダ"]
        for line in places:
            self.assertIn(line, part, line)
        for a, b in zip(places, places[1:]):
            self.assertLess(part.index(a), part.index(b))
        self.assertEqual(len(tool_search_order()), len(places))
        self.assertIn("1〜3 は、作業フォルダから上のフォルダへ、Git のリポジトリの一番上まで探します", part)
        self.assertIn("`COPILOT_HOME`", part)
        self.assertIn("名前がドットで始まるフォルダは見ません。", part)
        self.assertIn("先に見つかったほうを設定します。", part)
        # Copilot CLI と順が違う場合も書く（確かめた版とともに）
        self.assertIn("Copilot CLI 1.0.105 がスキルを選ぶ順と同じです。", part)

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
        self.assertNotRegex(self.text, r"(?m)^python3 ")
        self.assertIn("py -3 <このフォルダ>/scripts/config_tool.py skills", self.text)


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
        self.assertEqual(rel, ["README.md", "SKILL.md", "scripts/config_tool.py",
                               "tests/__init__.py",
                               "tests/fixtures/coffee-machine.yaml", "tests/fixtures/network-camera.yaml",
                               "tests/test_config_tool.py", "tests/test_skill_files.py",
                               "tests/test_skills_command.py"])


if __name__ == "__main__":
    unittest.main()
