# triage-report（結果の記録）

## 何をするか

Coverity の警告1件（またはグループ1つ）を調べた結果を、決まった項目（`result.json` の形）で書き、形と決まりを確かめるスキルです。
`result.json` を、チャットで読みやすい日本語の報告にもします。

- 項目：見立て（40文字ほどの要約）・判定・根拠の説明と場所・推奨（修正か逸脱か）・確信度と理由・逸脱案・修正案・リビジョンのずれ・グループで外した CID。
- 修正案と逸脱案は、いつも両方を書きます。どちらを採るかは人が決めます。
- 形の正は `assets/result.schema.json`（JSON Schema）です。

## 必要なもの

- AI のエージェント（GitHub Copilot CLI、Claude Code など、スキルを読めるもの）
- スクリプトを動かすときは [uv](https://docs.astral.sh/uv/)（Python と、形の確かめに使う jsonschema は uv が用意します）

## 置き方

このフォルダだけを、次のどこかにコピーします。フォルダの名前を変えても動きます。

- 作業フォルダの `.github/skills/triage-report/`（Claude Code なら `.claude/skills/triage-report/`）
- `~/.copilot/skills/triage-report/`

## 呼び方

チャットで頼みます。

- 「CID 20001 を調べた結果をまとめたので、決まった形で書いて」（チャットに6つの項目で返します）
- 「この結果を result.json に書いて」（ファイルに書き、スクリプトで確かめます）
- 「この result.json を確かめて」「この result.json を説明して」

頼まれない限り、結果のファイルを作らず、Coverity にも書き込みません。ほかのエージェントから、書く場所と返す形を決めて頼まれたときは、それに従います。

スクリプトだけを動かすこともできます。

```
# Windows（PowerShell）
uv run --native-tls --quiet "<このフォルダ>/scripts/check_result.py" check --result "C:\work\result.json" --item 20001
uv run --native-tls --quiet "<このフォルダ>/scripts/check_result.py" render --result "C:\work\result.json"
# Linux（bash）
uv run --native-tls --quiet "<このフォルダ>/scripts/check_result.py" check --result "/work/result.json" --item 20001
uv run --native-tls --quiet "<このフォルダ>/scripts/check_result.py" render --result "/work/result.json"
```

| コマンド | 引数 | すること |
| --- | --- | --- |
| `check` | `--result <ファイル>`（または `--stdin`）、`--item <ID>`、`--cids <CID,…>`、`--impact High\|Medium\|Low\|Unspecified` | 形（JSON Schema）と決まりを確かめる。ファイルは作らない |
| `render` | `--result <ファイル>`（または `--stdin`） | チャット向けの文（`text`）と1行の要約（`line`）を作る |

`--stdin` のときは、標準入力に `{"result": {…}, "item": "20001", "cids": [20001], "impact": "High"}` の JSON を渡します。

## 設定

設定のファイルはありません。調べるリポジトリに `.coverity-triage/knowledge.md` があれば、その知識（調査で使う事実、推奨の方針、逸脱コメントの書き方）を使います。

## 結果の見方

`check` の出力：

```json
{"ok": false, "error": "結果に直すところがあります", "item": "20001",
 "problems": [{"code": "schema", "path": "verdict.summary", "message": "verdict.summary: 空や空白だけにできません"}],
 "warnings": [{"code": "fix_severity", "path": "fix.severity", "message": "Impact High の修正案の Severity の目安は Major です（今は Moderate）"}]}
```

| 項目 | 意味 |
| --- | --- |
| `ok` | 決まりに合えば `true`（終了コード 0）。合わなければ `false`（終了コード 1） |
| `problems` | 直すところ。`code` は種類、`path` は項目の場所、`message` は直し方 |
| `warnings` | 目安から外れたところ（直さなくても `ok` は変わらない） |

`problems` の `code`：

| code | 意味 |
| --- | --- |
| `schema` | 形が違う（項目がない・余計な項目・値の種類・空の文字列など） |
| `item_mismatch` | `item` が `--item` と違う |
| `comment.<種類>` | 逸脱コメントが決まりに合わない（判定区分で始まらない、200文字を超える、条件の言い方がある など） |
| `group_excluded_not_group` | グループでないのに `group_excluded_cids` がある |
| `group_excluded_unknown` | `group_excluded_cids` にグループの外の CID がある |
| `group_excluded_all` | グループの CID をすべて外している |
| `bad_input` | 渡し方の誤り（ファイルがない、JSON でない など） |

`warnings` の `code`：`deviation_combo`（逸脱案の Classification・Action・Severity が目安と違う）、`comment_judgement`（逸脱コメントの判定区分が判定と違う）、`fix_combo`・`fix_severity`（修正案の組が目安と違う）、`group_unchecked`（グループの CID が分からず確かめていない）。

この決まりは、ほかの部品と共有する取り決めです。試験のデータは `assets/contract/result/cases.json` にあります（`assets/contract/comment/cases.json` は、逸脱コメントの決まりの写しの試験のデータです）。

## うまくいかないとき

| 起きること | 直し方 |
| --- | --- |
| `uv` が見つからない | uv を入れます（Windows は `winget install --id=astral-sh.uv -e`）。スクリプトを使わずに、SKILL.md の表と決まりを見て書くこともできます |
| 初めての実行で止まる・失敗する | jsonschema を取りに行くため、ネットワークが要ります。社内の証明書を使うときも `--native-tls` を付けたまま動かします |
| `schema` の問題が多く出る | 項目の名前の打ち間違いや、余計な項目がないかを見ます。`item` は文字列（`"20001"`）で書きます |
| 日本語が文字化けする | 新しい版の PowerShell を使うか、`PYTHONIOENCODING=utf-8` を付けて動かします |

## テストの動かし方

このフォルダで動かします（uv があれば、スクリプトをコマンドとしても試します）。

```
# Windows（PowerShell）
py -3 -m unittest discover -s tests
# Linux（bash）
python3 -m unittest discover -s tests
```

`evals/` には、実際のモデルで試す課題（skill-creator の形）があります。
