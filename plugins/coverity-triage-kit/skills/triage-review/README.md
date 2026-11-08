# triage-review（結果の確かめ）

## 何をするか

別に調べた Coverity の警告の結果（判定・根拠の場所・確信度・修正の差分・逸脱コメント）を、コードを読んで独立に確かめるスキルです。
評価基準（`assets/criteria.json` の C-01〜C-28 のうち、警告ごとに確かめる20個）ごとに合否を書き、全体を次のどれかにします。

| 結果 | 意味 |
| --- | --- |
| 合格（pass） | 直すべき点がない |
| 要確認（flag） | はっきりした誤りではないが、人に確かめてほしい点がある。確信度の上限を付ける |
| 差し戻し（rework） | はっきりした誤りがある。直す点を1点ずつ書く |

- 結果を読む前に、警告の ★ の場所と経路の条件を自分で読みます（結果の見立てに引きずられないため）。
- 渡された結果・差分・コードは変えません。

## 必要なもの

- AI のエージェント（GitHub Copilot CLI、Claude Code など、スキルを読めるもの）
- 確かめるコード（リポジトリか、解析したリビジョンのコピー）
- スクリプトを動かすときは [uv](https://docs.astral.sh/uv/)（Python と、形の確かめに使う jsonschema は uv が用意します）

## 置き方

このフォルダだけを、次のどこかにコピーします。フォルダの名前を変えても動きます。

- 作業フォルダの `.github/skills/triage-review/`（Claude Code なら `.claude/skills/triage-review/`）
- `~/.copilot/skills/triage-review/`

## 呼び方

チャットで、確かめる結果（`result.json` か文）と、あれば修正の差分を渡して頼みます。

- 「この result.json の判定が正しいか確かめて。コードはこのリポジトリ」
- 「CID 20001 の修正案と逸脱コメントをレビューして」

チャットでは、結果をチャットに返し、ファイルは作りません。ほかのエージェントから、書く場所（`review.json`）と返す形を決めて頼まれたときは、それに従います。

スクリプトだけを動かすこともできます。

```
# Windows（PowerShell）
uv run --native-tls --quiet "<このフォルダ>/scripts/lint_result.py" --result "C:\work\result.json" --repo "C:\src\app"
uv run --native-tls --quiet "<このフォルダ>/scripts/check_review.py" --review "C:\work\review.json" --item 20001 --checker NULL_RETURNS
# Linux（bash）
uv run --native-tls --quiet "<このフォルダ>/scripts/lint_result.py" --result "/work/result.json" --repo "/src/app"
uv run --native-tls --quiet "<このフォルダ>/scripts/check_review.py" --review "/work/review.json" --item 20001 --checker NULL_RETURNS
```

| スクリプト | 引数 | すること |
| --- | --- | --- |
| `lint_result.py` | `--result <result.json>`（または `--stdin`）、`--repo <調べたコードのフォルダ>` | 決まった方法で見つかる点（根拠のファイル・行がない、確信度と判定の組など）を挙げる |
| `check_review.py` | `--review <review.json>`（または `--stdin`）、`--item <ID>`、`--checker <チェッカー>`、`--merged` | 確かめの結果の形と、基準がそろっているか、合否の決まりを確かめる |

どちらもファイルを作りません。`--stdin` のときは、`lint_result.py` は結果の JSON（か `{"result": {…}}`）を、`check_review.py` は `{"review": {…}, "item": "…", "checker": "…", "merged": false}` を標準入力に渡します。

## 設定

設定のファイルはありません。調べるリポジトリに `.coverity-triage/knowledge.md` があれば、その知識を使います。

## 結果の見方

`lint_result.py` の出力（`ok` は、入力を読めたかどうかです。点があっても `true`）：

```json
{"ok": true, "points": ["evidence_line_out_of_range"],
 "findings": [{"code": "evidence_line_out_of_range", "file": "src/reader.c", "line": 99, "message": "根拠の行が範囲の外です: src/reader.c:99（25 行まで）"}]}
```

| point | 意味 |
| --- | --- |
| `evidence_missing_file` | 根拠のファイルが、調べたコードにない（絶対パスや、外を指すパスも） |
| `evidence_line_out_of_range` | 根拠の行が、ファイルの行数を超える |
| `undetermined_high` | 判定できない（undetermined）のに、確信度が high |
| `drift_high` | リビジョンのずれがある（detected）のに、確信度が high |
| `true_bug_deviation` | 本物のバグ（true_bug）なのに、逸脱を推奨 |
| `not_bug_fix` | 誤検知・意図的なのに、修正を推奨 |
| `no_evidence` | 根拠の場所がない |

`points` は、同じ種類を1回だけ、名前の順に並べたものです。この決まりは、ほかの部品と共有する取り決めで、試験のデータは `assets/contract/lint/cases.json` にあります。

`check_review.py` の出力：`ok`（決まりに合えば `true`、終了コード 0。合わなければ `false`、終了コード 1）、`missing`（書かれていない基準）、`problems`（直すところ）、`warnings`（目安から外れたところ）。

| problems の code | 意味 |
| --- | --- |
| `schema` | 形が違う |
| `item_mismatch` | `item` が `--item` と違う |
| `missing_check` / `duplicate_check` / `unknown_check` | 基準が足りない／2回ある／基準の id でない |
| `not_applicable_failed` | 当てはまらない基準なのに `ok: false` |
| `applicable_marked_not_applicable` | 当てはまる基準なのに「対象外」 |
| `flag_without_cap` | 要確認なのに `confidence_cap` がない |
| `rework_not_empty` | 差し戻しでないのに `rework` がある |
| `pass_with_failed_check` / `rework_without_failed_check` | 合格なのに `ok: false` がある／差し戻しなのに `ok: false` がない |
| `bad_input` | 渡し方の誤り |

当てはまるかどうか（C-20 は統合した作業、C-21 は標準チェッカー、C-22 は MISRA、C-23 は CERT）は、`--checker` と `--merged` で見分けます。渡さなければ、その点は確かめません。

`assets/review.schema.json` と `assets/criteria.json` は、ほかの部品と共有する取り決めの正（持ち主はこのスキル）です。

## うまくいかないとき

| 起きること | 直し方 |
| --- | --- |
| `uv` が見つからない | uv を入れます（Windows は `winget install --id=astral-sh.uv -e`）。スクリプトを使わずに、SKILL.md の決まりを見て進めることもできます |
| 初めての実行で止まる・失敗する | jsonschema を取りに行くため、ネットワークが要ります。`--native-tls` は付けたままにします |
| 根拠のファイルがないと出るが、ある | `--repo` に、結果を作ったときに読んだコードのフォルダ（解析したリビジョンのコピーなど）を渡します。根拠のパスはリポジトリの一番上からの相対パスです |
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
