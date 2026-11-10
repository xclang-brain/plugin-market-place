# triage-integration（同じ原因の統合）

## 何をするか

調べ終わった Coverity の警告の結果をいくつか比べ、**原因が1つ**のものだけを1つにまとめる計画（`integration/plan.json` の形）を作るスキルです。計画が決まりに合うかは、付属のスクリプトで確かめます。コードは変えません。

- まとめるのは、1か所を直せば（または1つの理由で）すべての警告が片付くものだけ。場所が近い・チェッカーが同じだけではまとめず、迷うときはまとめない。
- まとめる作業は2つ以上、1つの作業は1つのまとまりにだけ、CID は合わせて10件まで。
- まとめた確信度は、まとめる作業のいちばん低い確信度以下。逸脱コメントは、すべての CID に当てはまる1つの文。
- 修正の作り方を `item:<ID>`（その作業の修正を使う）・`combine`（修正を合わせる）・`rework`（作り直す）から選ぶ。
- 原因は違うのに同じ場所を変える修正は、まとめずに「ぶつかる修正」（`conflicts`）に書く。

## 必要なもの

- AI のエージェント（GitHub Copilot CLI など、スキルを読めるもの）
- 比べる警告の結果（`result.json` の形か文）と、読むコード
- スクリプトを動かすときは [uv](https://docs.astral.sh/uv/)（Python と、形の確かめに使う jsonschema は uv が用意します）

## 置き方

このフォルダだけを、次のどこかにコピーします。フォルダの名前を変えても動きます。

- 作業フォルダの `.github/skills/triage-integration/`
- `~/.copilot/skills/triage-integration/`

## 呼び方

チャットで頼みます。

- 「CID 30101、30102、30103 の結果がこのフォルダにある。同じ原因ならまとめて」
- 「この警告たちは同じ原因？」

チャットでは、まとめるかどうかと理由、まとめ方、まとめた逸脱コメント、確信度、ぶつかる修正をチャットに返し、ファイルは作りません。ほかのエージェントから、書く場所（`integration/plan.json`）と返す形を決めて頼まれたときは、それに従います。返す形が決まっていなければ1行で返し（`統合: 1つにまとめる（30101, 30102）／まとめない: 30103（原因が違う：…）`）、まとめなかった候補とその理由もその行に書きます。

スクリプトだけを動かすこともできます。

```
# Windows（PowerShell）
uv run --native-tls --quiet "<このフォルダ>/scripts/check_plan.py" --plan "C:\work\plan.json" --member "30101:30101:high" --member "30102:30102:medium"
# Linux（bash）
uv run --native-tls --quiet "<このフォルダ>/scripts/check_plan.py" --plan "/work/plan.json" --member "30101:30101:high" --member "30102:30102:medium"
```

| 引数 | 意味 |
| --- | --- |
| `--plan <ファイル>` | 確かめる計画 |
| `--member <ID>:<CID,…>[:<確信度>]` | まとめる候補の作業（くり返す）。グループなら `G1:30001,30002:high` |
| `--no-grouping <CID>` | グループにしない CID（くり返す） |
| `--items <ファイル>` | 候補の作業の JSON（`{"<ID>": {"cids": [30101], "status": "done", "confidence": "high", "no_grouping": false}}`）。`--member` の代わり |
| `--stdin` | 標準入力から `{"plan": {…}, "items": {…}}` を読む |

候補の作業を渡さないと、作業の有無・CID の数・確信度は確かめません（`warnings` に出ます）。ファイルは作りません。

## 設定

設定のファイルはありません。調べるリポジトリの `.coverity-triage/knowledge.md`（プロジェクトの知識）と `.coverity-triage/no-grouping.yaml`（グループにしない CID）があれば、それに従います。

## 結果の見方

```json
{"ok": false, "error": "計画に直すところがあります", "merges": 1, "conflicts": 0,
 "problems": [{"code": "confidence_too_high", "path": "merges[0].result.confidence",
               "message": "merges[0].result.confidence: まとめた確信度は、まとめる作業のいちばん低い確信度（medium）以下にします（今は high）"}],
 "warnings": []}
```

| problems の code | 意味 |
| --- | --- |
| `schema` | 形が違う（項目がない・余計な項目・`fix_source` の値など） |
| `member_duplicate` / `members_too_few` | 同じ作業が2回ある／別の作業が2つない |
| `member_unknown` / `member_not_done` / `member_no_grouping` | 候補にない／調べ終わっていない／グループにしない CID を含む |
| `member_in_two_merges` | 1つの作業が2つのまとまりに入っている |
| `too_many_cids` | CID が合わせて10件を超える |
| `confidence_too_high` | まとめた確信度が、まとめる作業のいちばん低い確信度より高い |
| `fix_source_not_member` | `item:<ID>` の作業が、まとめる作業でない |
| `comment.<種類>` | まとめた逸脱コメントが決まりに合わない |
| `conflict_duplicate` / `conflict_unknown_item` | ぶつかる修正に、同じ作業が2回ある／候補にない作業がある |
| `bad_input` | 渡し方の誤り |

`warnings`：`deviation_combo`・`comment_judgement`・`fix_combo`（Classification などの組が目安と違う）、`items_unchecked`（候補の作業が渡されていない）。

`assets/plan.schema.json` と、この決まりの試験のデータ `assets/contract/plan/cases.json` は、ほかの部品と共有する取り決めの正（持ち主はこのスキル）です。`assets/result.schema.json` は1件の結果の形の写しで、計画の中の結果の定義と同じであることを試験で確かめます。`assets/contract/comment/cases.json` は、逸脱コメントの決まりの写しの試験のデータです。

## うまくいかないとき

| 起きること | 直し方 |
| --- | --- |
| `uv` が見つからない | uv を入れます（Windows は `winget install --id=astral-sh.uv -e`）。スクリプトを使わずに、SKILL.md の決まりを見て進めることもできます |
| 初めての実行で止まる・失敗する | jsonschema を取りに行くため、ネットワークが要ります。`--native-tls` は付けたままにします |
| `--member` が受け付けられない | `<ID>:<CID>:<確信度>` の形で、確信度は high・medium・low のどれかにします。PowerShell では `"…"` で囲みます |
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
