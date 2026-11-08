# deviation-comment（逸脱コメント）

## 何をするか

Coverity の警告を「直さない」ときに登録する逸脱コメント（200文字以内の短い文）を書き、Classification・Action・Severity の案と一緒に返すスキルです。
書いたコメントが決まりに合うかは、付属のスクリプトで確かめます。

決まり（要点）：

- 「誤検知。」「意図的。」「本物の不具合。」のどれかで始め、判定区分のあと1〜2文、200文字以内。前後に空白を入れない。
- 理由 → コードの場所（ファイルと行）の順に、条件を付けずに言い切る。
- 条件・但し書き（「ただし」「〜の場合に限る」）、推量（「おそらく」）、調べた過程、推奨や依頼（「修正を推奨」「確認してください」）を書かない。
- 言い切れないときは、推奨を修正にする。本物の不具合でも、今は実行されないことをコードで示せるときは、その事実をそのまま書く（例「本物の不具合。この機能は今の製品では無効（config.h:12）で、この処理は実行されない。」）。

## 必要なもの

- AI のエージェント（GitHub Copilot CLI、Claude Code など、スキルを読めるもの）
- スクリプトを動かすときは [uv](https://docs.astral.sh/uv/)（Python は uv が用意します。追加のライブラリは要りません）

## 置き方

このフォルダだけを、次のどこかにコピーします。フォルダの名前を変えても動きます。

- 作業フォルダの `.github/skills/deviation-comment/`（Claude Code なら `.claude/skills/deviation-comment/`）
- `~/.copilot/skills/deviation-comment/`

## 呼び方

チャットで頼みます。

- 「CID 12345（NULL_RETURNS、src/foo.c の 120 行）は誤検知だった。逸脱コメントを書いて」
- 「この逸脱コメントで大丈夫か見て：誤検知。ただし…」

返ってくるもの：逸脱コメントと字数、Classification・Action・Severity、判定区分と根拠の場所。ファイルは作らず、Coverity にも書き込みません。
ほかのエージェントから、書く場所と返す形を決めて頼まれたときは、それに従います。

確かめのスクリプトだけを動かすこともできます。

```
# Windows（PowerShell）
uv run --native-tls --quiet "<このフォルダ>/scripts/check_comment.py" --comment "誤検知。…"
# Linux（bash）
uv run --native-tls --quiet "<このフォルダ>/scripts/check_comment.py" --comment "誤検知。…"
```

引用符を含むなどでコマンドに書きにくい文は、`--stdin` を付けて、標準入力に `{"comment": "…"}` の JSON（または文そのもの）を渡します。

## 設定

設定のファイルはありません。調べるリポジトリに `.coverity-triage/knowledge.md` があれば、その「## 逸脱コメントの書き方」の決まりを優先します（チームの書き方や、社内の Classification・Action・Severity の値など）。

## 結果の見方

スクリプトは JSON を1つ出します。

```json
{"ok": false, "error": "逸脱コメントに直すところがあります", "length": 51,
 "problems": [{"code": "condition_words", "message": "条件・但し書き・推量の言い方（「ただし」）があります。…"}]}
```

| 項目 | 意味 |
| --- | --- |
| `ok` | 決まりに合えば `true`（終了コード 0）。合わなければ `false`（終了コード 1） |
| `length` | 前後の空白を除いた文字の数 |
| `problems` | 直すところ。`code` は種類、`message` は直し方 |

`code` の種類：`empty`（空）、`surrounding_whitespace`（前後の空白）、`too_long`（200文字を超える）、`no_category`（判定区分で始まらない）、`no_reason`（理由がない）、`too_many_sentences`（3文以上）、`line_break`（改行）、`markdown`（見出し・箇条書き・書式）、`condition_words`（条件・但し書き・推量）、`process_words`（調べた過程）、`request_words`（推奨・依頼）、`bad_input`（渡し方の誤り）。

この決まりは、ほかの部品と共有する取り決めです。試験のデータは `assets/contract/comment/cases.json`（入力のコメントと、合うかどうかの組）にあります。

## うまくいかないとき

| 起きること | 直し方 |
| --- | --- |
| `uv` が見つからない | uv を入れます（Windows は `winget install --id=astral-sh.uv -e`）。スクリプトを使わずに、SKILL.md の確かめる点を見て進めることもできます |
| 日本語が文字化けする | 新しい版の PowerShell を使うか、`PYTHONIOENCODING=utf-8` を付けて動かします |
| 正しいはずの文が `condition_words` で通らない | 書かれた言い方（「に対して」など）を、条件に読めない言い方に直します。決まりは曲げません |

## テストの動かし方

このフォルダで動かします（uv があれば、スクリプトをコマンドとしても試します）。

```
# Windows（PowerShell）
py -3 -m unittest discover -s tests
# Linux（bash）
python3 -m unittest discover -s tests
```

`evals/` には、実際のモデルで試す課題（skill-creator の形）があります。
