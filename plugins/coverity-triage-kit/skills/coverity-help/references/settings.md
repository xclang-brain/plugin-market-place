# 設定の項目と変え方

設定は、調べたいリポジトリの一番上の `.coverity-triage/` にあり、コミットしてチームで共有します。
認証情報は書きません（環境変数に置きます）。

## 目次

- config.yaml
- 条件ファイル（filters/*.yaml）
- 知識のファイル（knowledge.md）
- グループにしない CID（no-grouping.yaml）
- 偽の Coverity（fake-issues.yaml など）
- 環境変数
- 変え方

## config.yaml

ファイルにない項目は、既定の値で動きます。知らない項目は、どの部品も無視します。

| 項目 | 既定 | 意味 |
| --- | --- | --- |
| `coverity.url` | （必須） | Coverity Connect の URL（ブラウザーで開くアドレス）。`http://` だと認証キーが暗号化されずに送られる |
| `coverity.api` | `auto` | `auto`：Coverity Connect につなぐ／`fake`：偽の警告を使う（試しと自己診断のため） |
| `coverity.user_env` / `coverity.key_env` | `COV_USER` / `COV_AUTH_KEY` | ユーザー名・認証キーを入れる環境変数の名前。同じ名前にしない |
| `coverity.triage_store` | `Default Triage Store` | ストリームからトリアージストアを決められないときに書き戻す先 |
| `coverity.revision_field` | `sourceVersion` | 解析したリビジョンを入れてあるスナップショットの項目 |
| `coverity.path_strip_prefixes` | `[]` | 警告のファイルのパスから外す先頭（例 警告のパスが `C:/build/product/src/a.c` なら `["C:/build/product/"]`）。空でも、リポジトリにある末尾の部分で自動で対応づける |
| `coverity.ca_file` | （空＝OS の証明書） | Coverity の CA 証明書のファイル。証明書のエラーが出るときだけ書く |
| `coverity.fake_data` | （空） | `api: fake` のときの偽の警告のファイル（`.coverity-triage/` からの相対） |
| `vcs.type` | 自動 | `git` か `svn` |
| `vcs.base_branch` | 自動 | 修正の元にするブランチ（git） |
| `vcs.branch_prefix` | `coverity-fix/` | 修正のブランチの名前の頭（git） |
| `output_dir` | `../coverity-triage-out` | 実行フォルダの置き場所。**リポジトリの外**にする（中なら始めない）。リポジトリからの相対か絶対 |
| `max_items` | `100` | 1回の実行で調べる警告の上限 |
| `parallel` | `3`（1〜8） | 同時に動かす作業エージェントの数。1 なら1件ずつ |
| `options.annotation` | `false` | 逸脱案として、ソースに Coverity の注釈を入れる差分も作る |
| `options.per_run_branch` | `false` | 修正を警告ごとではなく、1つのブランチにまとめる（git） |
| `options.knowledge_suggestions` | `false` | 反映の後、知識の追記の候補を示す |
| `options.metrics` | `false` | 推奨の採用率などを記録し、反映のときに集計する |
| `options.markdown` | `false` | 一覧と詳細レポートを Markdown（`summary.md`、`cid/<ID>.md`）でも出す |
| `verify.default` | `none` | ビルドでの検証：`none` / `build` / `build+analyze` |
| `verify.initial` | `false` | 初回解析：最初にビルド・解析して Coverity に登録してから警告を取る |
| `verify.setup_command` | （空） | ビルドの前に動かす環境設定のコマンド。`{root}` は作業用のコピーの根に替わる（例 `envset.bat "{root}"`） |
| `verify.build_dir` | （空＝一番上） | ビルドするフォルダ（リポジトリからの相対） |
| `verify.build_command` | （空） | ビルドのコマンド（例 `make -f makefileXX`） |
| `verify.cov_build_args` / `verify.cov_analyze_args` | `--dir idir` / `--dir idir --all` | `cov-build` / `cov-analyze` の引数（`--dir` が要る） |
| `verify.cov_commit_args` | （空） | 初回解析の `cov-commit-defects` に足す引数。`--dir`・`--url`・`--stream`・`--user`・`--version` はスクリプトが付ける |
| `verify.timeout_minutes` | `180` | 初回解析とビルドの時間の上限（分）。ふだんの時間より長くする |
| `agents.integration` | `false` | 同じ原因の統合を使う |
| `agents.review` | `false` | 結果の確かめを使う |
| `agents.max_rework` | `1`（0〜2） | 確かめで差し戻した警告を作り直す回数の上限（警告ごと） |

例（初回解析と、統合・確かめを使う）：

```yaml
verify:
  initial: true
  build_command: make -f makefileXX
  timeout_minutes: 180
agents:
  integration: true
  review: true
  max_rework: 1
```

## 条件ファイル（filters/*.yaml）

同じ項目の中の値は「どれか」、項目どうしは「すべて」を満たす警告を選びます。書かない項目は条件にしません。既定の条件ファイルは `untriaged.yaml`（分類がまだの警告、20 件まで）。

| 項目 | 意味 |
| --- | --- |
| `name` | 表示の名前。条件を選ぶときは、ファイルの名前（例 `misra.yaml`）を使う |
| `project` | Coverity のプロジェクト（省略可） |
| `streams` | ストリーム。**ちょうど1つ** |
| `checkers` | チェッカーの名前。`*` `?` `[ ]` が使える（例 `"MISRA C-2012 Rule 10.*"`） |
| `impacts` | `High` / `Medium` / `Low` / `Unspecified` |
| `triage.classification` | Unclassified / Pending / False Positive / Intentional / Bug |
| `triage.action` | Undecided / Fix Required / Fix Submitted / Modeling Required / Ignore（管理者が足した値も書ける） |
| `triage.status` | New / Triaged / Dismissed / Absent Dismissed / Fixed |
| `limit` | 調べる件数の上限（グループにまとめる前の件数。`max_items` より多くはならない） |

例（MISRA の分類がまだの警告、20 件まで）：

```yaml
name: MISRA の未分類
project: MyProduct
streams: [MyProduct-main]
checkers: ["MISRA C-2012 *"]
triage:
  classification: [Unclassified]
limit: 20
```

一括のトリアージを頼むときに添えた条件（「High だけ」「10 件だけ」）は、選んだ条件ファイルに足されます。

## 知識のファイル（knowledge.md）

AI が調べる前に読む、プロジェクトの知識です。次の3つの見出しの下に、1行に1つ書きます。

```markdown
## 調査で使う事実
- fatal_error() は戻らない（exit する）
## 推奨の方針（修正か逸脱か）
- 初期化の関数の中の警告は、逸脱をすすめる
## 逸脱コメントの書き方
- 関数名は () を付けて書く
```

- 反映の後の追記は `## 追記（日付、実行 ID）` の見出しの下に入る。
- どの見出しに入るか：コードの振る舞いの事実は「調査で使う事実」、修正か逸脱かの決め方は「推奨の方針」、コメントの書き方は「逸脱コメントの書き方」。

## グループにしない CID（no-grouping.yaml）

`cids: [20004, 20005]` の形。却下されたグループの CID が、反映のときに自動で足されます。次の実行から、1件ずつ調べます。

## 偽の Coverity（fake-issues.yaml など）

`coverity.api: fake` のとき、`coverity.fake_data` のファイルの警告を、Coverity の代わりに使います。書き戻しは `<ファイル>.writes.jsonl` に足されます（Coverity には何も書かない）。ファイルの形は、Coverity との通信の部品の README にあります。

## 環境変数

| 名前 | 意味 |
| --- | --- |
| `COV_USER` / `COV_AUTH_KEY` | Coverity のユーザー名と認証キー（名前は `coverity.user_env` / `coverity.key_env` で変えられる）。Windows ではユーザーの環境変数に保存する。Claude Code でも同じ変数を使う（入れてから Claude Code を起動する） |
| `COVERITY_PASSPHRASE` | 初回解析の登録のときだけ、スクリプトが認証キーを渡すのに使う。人が置く必要はない |
| `HTTPS_PROXY` / `NO_PROXY` | 社内のプロキシを通すとき |
| `COVERITY_URL` など | リポジトリなしで Coverity との通信の部品を使うときの設定（その部品の README） |

## 変え方

- 設定と条件ファイルは、準備のスキルか、結果と設定の画面の「設定」で変える。どちらも、人が書いた注釈を元の場所に残す。
- 手で書き換えてもよいが、字下げはスペースにし、タブを使わない。
- 変えたら、点検のスクリプトの `check` で確かめる。
