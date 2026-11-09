# オプションと設定の項目

## 目次

1. ビルドでの検証と初回解析の進め方
2. set の書き方
3. config.yaml の項目
4. 条件ファイル（filters/*.yaml）

## 1. ビルドでの検証と初回解析の進め方

ビルドのコマンドを設定すると、次の2つに使える。どちらも既定では使わない。

| 使い方 | すること |
| --- | --- |
| 修正案の確かめ（`verify.default`） | トリアージの最後に、修正案をすべて当てたコードをビルドして確かめる（`build`）。Coverity で解析し直して、警告が消えたかも確かめる（`build+analyze`） |
| 初回解析（`verify.initial`） | トリアージの最初に、開いているフォルダをビルドして Coverity で解析し、結果を Coverity に登録してから警告を取る。手で「解析して登録する」をしていたなら、その代わりになる |

1つずつ聞く（推測で決めない）。

1. ビルドの前に動かす環境設定のコマンド（例 `envset.bat "{root}" <引数>`）。引数の数と、それぞれに何を渡すかを聞く。ビルドするフォルダを渡す引数は `{root}` と書く。要らなければ空。
2. ビルドするフォルダ（makefile がある場所。リポジトリからの相対パス）。一番上でよければ空。
3. ビルドのコマンド（例 `make -f makefileXX`）。
4. 修正案の確かめ方：しない（`none`。初回解析だけを使うとき）／ビルドだけ（`build`）／ビルドと Coverity の再解析（`build+analyze`）。
5. 初回解析を使うか。聞く前に、次を短く伝える。
   - 開いているフォルダを、そのままビルド・解析する。結果は、条件ファイルのストリームに、今のコミット（またはリビジョン）を版として登録する。登録が終わってから警告を取る。
   - Coverity のトリアージ（分類やコメント）は変えない。スナップショットが1つ増えるだけ（手で解析して登録していたときと同じ）。
   - ビルドの生成物と、解析の中間フォルダ（`cov-build` の `--dir`）は、開いているフォルダの中にできる。
   - 失敗したら、トリアージを始めずに止まり、失敗した段（ビルド・解析・登録）とログの最後を伝える。
6. （初回解析を使うとき）`cov-commit-defects` に足す引数。`--dir`・`--url`・`--stream`・`--user`・`--version` は自動で付く。認証キーは環境変数 `COVERITY_PASSPHRASE` で渡す（コマンドの引数とログには出さない）ので、ふつうは空。認証キーのファイルを使うサイトでは、`--auth-key-file <ファイル>` などを書く。
7. （初回解析を使うとき）時間の上限の分（既定 180）。ふだんのビルドと解析より長くする。超えると止めて失敗にする（人が見ていない間に、固まったビルドを止めるため）。

続けて、次の順に進める。

1. 「修正前のコードで試しにビルドします（時間がかかることがあります。Coverity には登録しません）」と伝えて、`trial-build --build-command "<3>" --setup-command "<1>" --build-dir "<2>"` を動かす。リポジトリの外（結果を置くフォルダの `_trial-build`）にコピーしてビルドするので、作業中のファイルは変わらない。
2. `build_ok` が false なら、`log_tail` を見て、コマンドの誤りか環境の問題かを伝え、直してもう一度試す。
3. 成功したら、`set-verify --build-command "<3>" --setup-command "<1>" --build-dir "<2>" --default <4>` に、初回解析を使うなら `--initial --cov-commit-args="<6>" --timeout-minutes <7>`、使わないなら `--no-initial` を足し、さらに `--check` を付けて動かす。返った `changes` を表で見せ、`notice` があればその文も伝えて、「この内容で保存しますか？」と聞いて返事を終える。
4. 見せた表に同意の返事をもらったら、`--check` を付けずに同じ引数で動かし、`check` で確かめる。初回解析を使うときは、`check` の「初回解析の cov-build」などの行を伝える（ng なら、Coverity の解析のツールの bin フォルダを PATH に足すのは社内の手順で。warning は「環境設定のコマンドで PATH に足されるなら問題ありません」）。

- `--cov-commit-args` などの値が `--` で始まるときは、`=` でつなぐ（`--cov-commit-args="--auth-key-file C:/key.txt"`）。
- 省いた項目は、今の設定のまま残る。

## 2. set の書き方

```
uv run --native-tls --quiet "<このスキルのフォルダ>/scripts/setup.py" set --repo "<リポジトリ>" --value max_items=50 --value agents.integration=true --check
```

- 値は項目の型で読む：true / false、整数、文字列。リストは `[a, b]` か、`a, b`。
- 項目を消す：`--unset coverity.ca_file`。
- 長い値や、引用符の多い値は、`--stdin` で JSON を渡す。`echo` でパイプにすると、PowerShell でも bash でも動く：
  `echo '{"values": {"verify.setup_command": "call env.bat \"{root}\""}}' | uv run --native-tls --quiet "<このスキルのフォルダ>/scripts/setup.py" set --repo "<リポジトリ>" --stdin --check`。
  Windows PowerShell 5.1 ではパイプの日本語が化けることがあるので、日本語は `\uXXXX` で書く。
- ファイルにない項目は、既定の値と違うときだけ足す（既定と同じなら `skipped_default`）。ファイルにある項目は、値を変えなければ、空の値でもそのまま残る。
- 知らない項目や、型の違う値は断る（`ok: false`）。何も書かない。

## 3. config.yaml の項目

| 項目 | 既定 | 意味 |
| --- | --- | --- |
| `coverity.url` | （必須） | Coverity Connect の URL |
| `coverity.api` | `auto` | `auto`（社内の Coverity）か `fake`（偽の警告で試す） |
| `coverity.user_env` / `coverity.key_env` | `COV_USER` / `COV_AUTH_KEY` | 認証情報を入れる環境変数の名前（同じ名前にしない） |
| `coverity.triage_store` | `Default Triage Store` | ストリームから決められないときのトリアージストア |
| `coverity.revision_field` | `sourceVersion` | 解析したリビジョンを入れたスナップショットの項目 |
| `coverity.path_strip_prefixes` | `[]` | 警告のファイルのパスから外す先頭 |
| `coverity.ca_file` | （なし＝OS の証明書） | 証明書のファイル |
| `coverity.fake_data` | （なし） | `api: fake` のときの偽の警告のファイル（`.coverity-triage/` からの相対） |
| `vcs.type` / `vcs.base_branch` / `vcs.branch_prefix` | 自動 / 自動 / `coverity-fix/` | git か svn、修正の元にするブランチ、修正のブランチの名前の頭 |
| `output_dir` | リポジトリの隣の `coverity-triage-out` | 結果を置くフォルダ（リポジトリの外。リポジトリからの相対か絶対） |
| `max_items` | `100` | 1回の実行の上限 |
| `parallel` | `3`（1〜8） | 同時に動かす作業の数 |
| `options.annotation` | `false` | 逸脱の案として、コードに注釈を入れる差分も作る |
| `options.per_run_branch` | `false` | git：承認した修正を1つのブランチにまとめる |
| `options.knowledge_suggestions` | `false` | 反映の後に、knowledge.md に足す知識の案を出す |
| `options.metrics` | `false` | 反映のたびに、推奨が採られた割合などを記録する |
| `options.markdown` | `false` | HTML のレポートのほかに Markdown も作る |
| `verify.default` | `none` | `none` / `build` / `build+analyze` |
| `verify.initial` | `false` | 最初に解析して登録するか |
| `verify.setup_command` / `build_dir` / `build_command` | （空） | ビルドの準備・場所・コマンド（`{root}` はビルドするフォルダ） |
| `verify.cov_build_args` / `cov_analyze_args` / `cov_commit_args` | `--dir idir` / `--dir idir --all` / （空） | 解析のツールの引数 |
| `verify.timeout_minutes` | `180` | 初回解析の時間の上限（分） |
| `agents.integration` / `agents.review` | `false` / `false` | 統合と確かめを使うか |
| `agents.max_rework` | `1`（0〜2） | 確かめで差し戻した警告を作り直す回数の上限 |

## 4. 条件ファイル（filters/*.yaml）

`init` は2つ作る：`untriaged.yaml`（分類がまだの警告を 20 件まで。既定の条件）と `all.yaml`（分類にかかわらず 100 件まで）。

| 項目 | 意味 |
| --- | --- |
| `name` | 表示の名前 |
| `project` | プロジェクト（省略可） |
| `streams` | ストリーム（ちょうど1つ） |
| `checkers` | チェッカー（`*` `?` `[ ]` のワイルドカード可） |
| `impacts` | High / Medium / Low / Unspecified |
| `triage.classification` | Unclassified / Pending / False Positive / Intentional / Bug |
| `triage.action` | Undecided / Fix Required / Fix Submitted / Modeling Required / Ignore |
| `triage.status` | New / Triaged / Dismissed / Absent Dismissed / Fixed |
| `limit` | 調べる件数の上限 |

同じ項目の中の値は「どれか」、項目どうしは「すべて」。条件ファイルは、コピーして名前を変えれば増やせる（例 `misra.yaml` に `checkers: ["MISRA C-2012 *"]`）。
