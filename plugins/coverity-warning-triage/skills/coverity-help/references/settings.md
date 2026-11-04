# 設定の項目と変え方

設定は対象リポジトリの `.coverity-triage/` にあり、コミットしてチームで共有します。認証情報は書きません（環境変数に置きます）。

ここにある項目は、すべて画面の「設定」でも変えられます（`/coverity-setup` で「画面で準備する」を選ぶか、`ct.py gui --repo <フォルダ> --view settings` で開きます）。

## config.yaml

| 項目 | 意味 | 決め方・既定 |
|---|---|---|
| `coverity.url` | Coverity Connect の URL | ブラウザで開く Coverity のアドレス |
| `coverity.api` | `auto`：Coverity Connect につなぐ／`fake`：偽データ（動作確認用） | 既定 `auto` |
| `coverity.user_env` / `coverity.key_env` | ユーザ名・認証キーを入れる環境変数の名前。ユーザ名はいつも必要。認証キーの代わりにパスワードも使える（パスワードを変えたら入れ直しが要る） | 既定 `COV_USER` / `COV_AUTH_KEY` |
| `coverity.triage_store` | 書き戻しの項目にストリームがないときに書き込むトリアージストアの名前（ふだんは、警告のあるストリームが使うトリアージストアに書く） | 既定 `Default Triage Store` |
| `coverity.revision_field` | スナップショットで解析リビジョンを記録している項目 | 既定 `sourceVersion`。記録が無ければ、手元のコードで調べてずれを確かめる |
| `coverity.path_strip_prefixes` | Coverity のファイルパスから取り除く先頭部分 | 警告のパスが `C:/build/product/src/a.c` なら `["C:/build/product/"]`。空でも、リポジトリにある末尾の部分で自動で対応づける（K-81） |
| `coverity.ca_file` | Coverity の CA 証明書のファイル | 空（OS の証明書を使う）。証明書のエラーが出るときだけ指定 |
| `vcs.type` | `git` / `svn` | 自動で判定 |
| `vcs.base_branch` | 修正を取り込む先のブランチ（git） | 自動で判定 |
| `vcs.branch_prefix` | 修正のブランチ名の先頭（git） | 既定 `coverity-fix/` |
| `output_dir` | 結果を置く場所 | 既定は、リポジトリと同じ階層の `coverity-triage-out` フォルダ（リポジトリの隣）。**リポジトリの外**にする（K-72） |
| `max_items` | 1 回で調べる警告の上限 | 既定 100 |
| `parallel` | 同時に調べる数（作業ごとに別のサブエージェントと修正用のコピーを使う） | 既定 3（1〜8）。1 にすると 1 件ずつ順に調べる |
| `options.annotation` | 逸脱案として、ソースに Coverity の注釈を入れる差分も作る | 既定 false |
| `options.per_run_branch` | 修正を警告ごとではなく、1 つのブランチにまとめる（git） | 既定 false |
| `options.knowledge_suggestions` | 反映のあと、知識の追記の候補を示す | 既定 false |
| `options.metrics` | 推奨の採用率などを記録する | 既定 false |
| `options.markdown` | 一覧と詳細レポートを、HTML（`summary.html`、`cid/<ID>.html`）に加えて Markdown（`summary.md`、`cid/<ID>.md`）でも出す。VS Code のプレビューや GitHub で読みたいとき。Markdown も表示専用で、書き換えても承認にはならない | 既定 false（HTML だけ）。画面の「オプション設定」の「Markdown も出す」 |
| `verify.default` | ビルドでの検証：`none` / `build` / `build+analyze` | 既定 `none` |
| `verify.initial` | 初回解析：`/coverity-run` の最初に、開いているフォルダをビルド・解析して Coverity サーバに登録し、それから警告を取る（[usage.md](usage.md)）。`/coverity-run 最初に解析して`・`解析はしないで` で、その実行だけ変えられる | 既定 false |
| `verify.setup_command` | ビルドの前に実行する環境設定のコマンド。`{root}` はビルド用のフォルダに置き換わる | 例 `envset.bat "{root}" <引数>`。不要なら空 |
| `verify.build_dir` | ビルドするフォルダ（リポジトリからの相対パス） | 空なら一番上 |
| `verify.build_command` | ビルドのコマンド | 例 `make -f makefileXX` |
| `verify.cov_build_args` / `verify.cov_analyze_args` | `cov-build` / `cov-analyze` の引数（`--dir` が必要） | 既定 `--dir idir` / `--dir idir --all` |
| `verify.cov_commit_args` | 初回解析の `cov-commit-defects` に足す引数。`--dir`・`--url`・`--stream`・`--user`・`--version` はスクリプトが付ける。認証キーは環境変数 `COVERITY_PASSPHRASE` で渡す（コマンドラインとログには出さない） | 既定 空。認証キーのファイルを使うサイトは `--auth-key-file <ファイル>` など |
| `verify.timeout_minutes` | 初回解析を止めるまでの時間（分）。人が見ていない間に、固まったビルドを止めるため | 既定 180。ふだんのビルドと解析の時間より長くする |
| `agents.integration` | 統合：作業の AI が調べ終わったあとで、同じ原因の作業を 1 つにまとめる（[usage.md](usage.md) の「統合と検証」） | 既定 false |
| `agents.review` | 検証：統合のあとで、出来上がった結果を 1 件ずつ独立に確かめる。確信度は下げることはあっても、上げない | 既定 false |
| `agents.max_rework` | 検証から作業の AI に差し戻す回数の上限（作業ごと） | 既定 1（0〜2） |

- `verify.setup_command`・`verify.build_dir`・`verify.build_command`・`verify.cov_build_args`・`verify.cov_analyze_args` は、ビルドでの検証と初回解析で共有します。
- ビルドでの検証と初回解析の設定は、`/coverity-setup` の「ビルドでの検証」か、画面の「オプション設定」の「ビルドでの検証と初回解析」で、試しにビルドしてから保存するのが安全です（試しのビルドでは、サーバに登録しません）。
- 統合と検証は、使うと実行の時間が延びます。また、`/coverity-run` が終わって一覧ができるまで、承認を選べません。
- `options.*` の 5 つは、true にしたときだけ動きます（K-14）。

例（初回解析と、統合・検証を使う）：

```yaml
verify:
  initial: true
  default: none
  setup_command: ""
  build_dir: ""
  build_command: make -f makefileXX
  cov_build_args: --dir idir
  cov_analyze_args: --dir idir --all
  cov_commit_args: ""
  timeout_minutes: 180
agents:
  integration: true
  review: true
  max_rework: 1
```

## 条件ファイル（filters/*.yaml）

同じ項目の中の値は「どれか」、項目どうしは「すべて」を満たすものを選びます。書かない項目は条件にしません。チャットの `/coverity-run` で、条件を足すこともできます（K-18）。

| 項目 | 意味 |
|---|---|
| `name` | 表示名（一覧 `summary.html` の見出しや実行の一覧に出る。空ならファイル名）。`/coverity-run` で条件を選ぶときは、ファイル名を使う |
| `project` / `streams` | Coverity のプロジェクトとストリーム（ストリームは 1 つだけ）。どちらも config.yaml ではなく条件ファイルに書く |
| `checkers` | チェッカー名（`*` が使える。例 `"MISRA C-2012 *"`） |
| `impacts` | `High` / `Medium` / `Low` / `Unspecified`（Impact が未指定の警告。High・Medium・Low だけでは対象にならない。K-101） |
| `triage.classification` / `triage.action` / `triage.status` | 今のトリアージの状態。Classification：Unclassified / Pending / False Positive / Intentional / Bug。Action：Undecided / Fix Required / Fix Submitted / Modeling Required / Ignore（管理者が足した値も書ける）。Status：New / Triaged / Dismissed / Absent Dismissed / Fixed |
| `limit` | この条件で Coverity から取り出して調べる警告の件数の上限（グループにまとめる前の件数。`max_items` より多くはならない） |

例（MISRA の未分類だけ、20 件まで）：

```yaml
name: MISRA の未分類
project: MyProduct
streams: [MyProduct-main]
checkers: ["MISRA C-2012 *"]
triage:
  classification: [Unclassified]
limit: 20
```

## そのほかのファイル

- `knowledge.md`：AI が調べる前に読む、プロジェクトの知識（[usage.md](usage.md)）。
- `no-grouping.yaml`：却下されたグループの CID。次の実行から 1 件ずつ調べます（自動で追記されます。K-26）。
