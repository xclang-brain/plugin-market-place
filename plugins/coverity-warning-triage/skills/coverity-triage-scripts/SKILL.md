---
name: coverity-triage-scripts
description: Coverity トリアージの決まった処理（実行フォルダ、グループ化、修正用のコピー、差分とブランチ、レポートと一覧、承認の読み取り、反映、準備の確認、動作確認、設定・結果・監視の画面）をするスクリプト ct.py の使い方。/coverity-setup・/coverity-run・/coverity-apply・/coverity-help・/coverity-selftest の手順で ct.py を使うときに読む。ほかのスキルとサブエージェントが読む手順書で、人が直接頼むものではない。
---

# スクリプト ct.py の使い方

## 動かし方

このファイルと同じフォルダの `scripts/ct.py` を、ターミナルで uv から動かします。

```
uv run --native-tls <このスキルのフォルダ>/scripts/ct.py <コマンド> <引数>
```

- `<このスキルのフォルダ>` は、この SKILL.md があるフォルダの絶対パスです。会話の中で一度確かめたら、同じパスを使い続けます。
- uv をインストールした直後で `uv` が見つからないときは、uv のフルパスで動かします（Windows の既定は `%USERPROFILE%\.local\bin\uv.exe`）。
- 初回だけ、必要なライブラリを取ってくるため数十秒かかります。
- `--native-tls` は省かないでください。社内のネットワークでは、Windows に入っている社内の証明書を uv が使わないと、ライブラリを取れません。
- パスに空白があるときは `"..."` で囲みます。

## 結果の読み方

どのコマンドも、結果を JSON で 1 つ出します。

- `"ok": true`：できました。`next` に次にすることが書いてあります。
- `"ok": false`（終了コード 1）：できませんでした。`error` に理由、`problems` に直すところがあります。理由を読んで直すか、人に伝えます。勝手に別の方法で同じことをしようとしない（ファイルを手で書き換えるなど）。（K-73）

## コマンド

引数の `--repo` は対象リポジトリの一番上のフォルダ、`--run` は `new-run` が返す実行フォルダ（`run_dir`）、`--item` は作業の ID（CID か `G1` などのグループ）です。

### 準備（/coverity-setup）

| コマンド | すること | 主な結果 |
|---|---|---|
| `prewarm` | MCP サーバの Python 環境を作る（初回の起動が時間切れにならないように） | `environment`（作った場所。環境変数 `UV_PROJECT_ENVIRONMENT` があればその場所）、`seconds` |
| `doctor --repo R` | 準備の状況を調べる。Coverity への接続は調べない（MCP の `check_connection` で調べる） | `ready`、`checks`（`status` が ng のものを直す） |
| `detect --repo R` | git / svn、取り込み先のブランチ、出力先の候補を調べる | `vcs`、`base_branch`、`has_config`、`output_dir` |
| `init-config --repo R --url U --project P --stream S [--base-branch B] [--output-dir D] [--strip-prefix X]...` | 設定 `config.yaml`、最初の条件 `filters/untriaged.yaml`、知識 `knowledge.md` を作る。人に値を見せて同意を得てから実行する | `written` |
| `install-agent` | サブエージェント（取得・作業・統合・検証）の定義を `~/.copilot/agents`（環境変数 `COPILOT_HOME` があればその `agents`）に置く | `agents`（エージェントごとの `status`）、`restart_needed` |
| `trial-build --repo R --build-command C [--setup-command S] [--build-dir D]` | （オプション）修正前の最新のコードを試しにビルドする。時間がかかる | `build_ok`、`log`、`log_tail` |
| `set-verify --repo R --build-command C [--setup-command S] [--build-dir D] [--default build] [--initial \| --no-initial] [--cov-commit-args A] [--timeout-minutes N]` | （オプション）ビルドでの検証と初回解析の設定を書く | `written` |

### 実行（/coverity-run）

| コマンド | すること | 主な結果 |
|---|---|---|
| `runs --repo R` | 実行の一覧（新しい順） | `runs`（`unfinished` が true なら途中） |
| `resume --run D` | 止まった実行を続けられるようにする（処理中・エラーの作業を未処理に戻す。失敗した初回解析と、できずに止まった警告の検索はやり直す） | `counts`、`next` |
| `new-run --repo R [--filter F] [--impact X]... [--checker X]... [--status X]... [--classification X]... [--action X]... [--limit N] [--verify none\|build\|build+analyze] [--initial \| --no-initial]` | 実行フォルダを作り、条件を決める。チャットで言われた条件は引数で足す（同じ項目は置き換え）。`--initial` で最初に解析してサーバに登録する（省略時は設定 `verify.initial`） | `run_dir`、`repo_root`、`work_dir`、`limit`、`initial`、`integration`、`review` |
| `tick --run D [--finished T] [--failed T --reason "理由"]` | **次にすることを返す。** /coverity-run のオーケストレーション役は、これに従って動く。サブエージェントが終わったら、その `task`（`collector:search`、`collector:paths`、`worker:<ID>`、`integrator`、`reviewer:<ID>`）を `--finished` で知らせる。途中で止まった・起動できなかったときは `--failed`。初回解析の間は、少し（最長 `--wait` 秒）待ってから返る | `stage`、`actions`（`launch` / `command` / `mcp`）、`wait`、`done`、`stop`、`say`、`running_items`、`running_roles` |
| `initial-status --run D` | 初回解析の状況（none / running / done / failed、いまの段、経過秒数、失敗したときはログの最後） | `status`、`step_label`、`elapsed_seconds`、`log_tail` |
| `plan --run D` | 検索結果（`issues.json`）から、パスの対応づけとグループ化をして作業の一覧を作る（`tick` が呼ぶ。K-24） | `items`、`groups`、`analyzed_revision`、`notes` |
| `next --run D` | 次の作業を取り出し、修正用のコピーを用意する（`tick` が呼ぶ。初回は時間がかかる） | `item`（無ければ null）、`cids`、`stream`、`item_dir`、`missing_issue_files` |
| `brief --run D --item I` | 作業エージェントへの指示ファイル `brief.md` を作る（`tick` が呼ぶ） | `prompt`、`result_file` |
| `finish --run D --item I` | 結果ファイルを確かめ、差分・ブランチ・詳細レポート `cid/<ID>.html` を作る（`tick` が呼ぶ。一覧をもう作った実行では、その作業の承認に AI の推奨も入れる） | `recommendation`、`confidence`、`warnings`、`split_into` |
| `fail --run D --item I --reason "理由"` | 作業を処理できなかったと記録する（`tick` が呼ぶ。再開でやり直せる） | `counts` |
| `status --run D` | 進み具合と最近の動き | `counts`、`in_progress`、`errors`、`summary`、`recent`（最近の動き。画面の「監視」と同じ記録から作った「HH:MM:SS 文」の文字列の配列。画面と同じく新しい順。最大 10 行） |
| `verify --run D` | （オプション）全部の修正案をまとめてビルドで確かめる。時間がかかる。問題が出た修正の推奨は、一覧とレポートで確信度を「低」として見せる（結果ファイルは変えない。K-13） | `build_ok`、`downgraded_to_low` |
| `summary --run D` | 一覧 `summary.html`（設定 `options.markdown` が true なら `summary.md` も）を作り、まだ承認の無い作業に AI の推奨を入れて `approvals.json` に保存し、修正用のコピーを片付ける | `summary`（`summary.html` の場所）、`recommended`、`errors` |

### 反映（/coverity-apply）

| コマンド | すること | 主な結果 |
|---|---|---|
| `preview --run D` | 人が画面で入れた承認と逸脱の手直し（`approvals.json`。古い実行は `summary.md` の承認欄と `cid/<ID>.md` の逸脱の節）を読み、反映の内容 `apply-plan.json` と確認用の文字列を作る | `counts`、`items`、`plan_file`、`confirmation_token`、`already_fixed_on_latest`（最新で直っていて取り込むものが無い作業）、`next` |
| `apply-code --run D --token T` | 修正を反映する（git：ブランチを push、svn：ワーキングコピーに差分を適用）。却下したグループは次から個別にする | `pull_requests`（`title`、`body`、`branch`、`compare_url`）、`svn_applied`、`coverity`、`errors` |
| `knowledge-candidates --run D` | （オプション）人が推奨を変えた・手直しした作業から、知識の候補を出す | `candidates`、`current_knowledge` |
| `add-knowledge --run D --entry "1 行の知識"...` | （オプション）人が選んだ知識を `knowledge.md` に追記する | `file` |
| `stats --repo R` | （オプション）効果測定の集計 | `recommendation_taken_rate` など |

### 画面（/coverity-setup・/coverity-run・/coverity-apply）

| コマンド | すること | 主な結果 |
|---|---|---|
| `gui --repo R [--run D] [--view monitor\|summary\|settings] [--no-browser] [--foreground]` | 画面をブラウザで開く。画面には「監視」（AI の動きと重大なエラー）、「結果」（実行の一覧、一覧、詳細レポート、承認の入力、差分と修正後のファイル）、「設定」（設定・条件・知識・グループ化しない CID・認証情報）がある。`--run` で実行フォルダを、`--view` で最初に見せる区分を選ぶ（`--run` があれば既定は `monitor`）。画面は裏で動き、コマンドはすぐ戻る。同じリポジトリの画面が動いていれば、新しく起動せずに使い回す。`--foreground` は画面のサーバが止まるまで戻らない（人が自分のターミナルで動かすとき。動いている画面は止めて、新しく起動する） | `url`、`browser_opened`、`reused`、`log`（起動できなかったときは `error`、`log_tail`） |

- 画面のサーバは、人が画面の「サーバを止める」を押すまで動き続けます（タブを閉じても止まりません）。止めるコマンドはありません。同じリポジトリで新しい画面のサーバが起動したとき（`--foreground` など）は、古いほうが自分で止まります。
- 承認（`approvals.json`）は人が画面で入れます。AI は書き換えません。
- `--run` を持つコマンドが `ok: false` で終わると、その実行の `operations.log` に記録され、画面の「監視」の「重大なエラー」に出ます。

### 動作確認（/coverity-selftest）

`selftest start | static | sample | check-run | check-apply | check-coverity | check-build | record | report`。使い方はスキル `coverity-selftest` に書いてあります。

- `selftest sample --dir <dir> --assets <フォルダ>` は、見本のフォルダ（`sample-target` と `expected.yaml` があるフォルダ）を `--assets` で受け取ります。`--assets` は省けません。

## MCP のツールとの関係

Coverity との通信は MCP サーバ `coverity-triage` のツールが行い、結果を ct.py が示したファイルに保存します。

| MCP のツール | 渡すもの（ct.py の結果から） |
|---|---|
| `check_connection` | `repo_root` |
| `search_issues` | `repo_root`、`filter_file`、`output_file` = `issues_file`（`new-run` の結果） |
| `get_issues` | `repo_root`、`stream`、`cids`、`output_dir` = `item_dir`（`next` の結果、または取得エージェントへの指示ファイル） |
| `preview_triage` | `plan_file`（`preview` の結果）。書き戻す件数（`counts`）と内容（`items`）と、`preview` と同じ確認用の文字列（`confirmation_token`）を返す。読むだけで、Coverity とは通信しない |
| `update_triage` | `repo_root`、`plan_file`、`confirmation_token`（`preview` の結果） |

MCP のツールが `done: false` を返したら、`done: true` になるまで同じ引数で呼び直します。/coverity-run では、`search_issues` と `get_issues` は取得エージェントが呼びます（取得エージェントが使えないときは、`tick` が返す `mcp` の actions で、オーケストレーション役が代わりに呼びます）。

ct.py を使わずに、チャットで MCP のツールだけを使うとき（警告を 1 件調べるときなど）は、ファイルの引数を渡さずに呼ぶと、結果がツールの結果で返ります。

| MCP のツール | 渡すもの | 返るもの |
|---|---|---|
| `search_issues` | `repo_root`、`filter`（`filter.json` と同じ形。`streams` は 1 つだけ）、`offset`（続きを取るとき） | `issues`（CID、チェッカー、ファイル、行、関数、Impact など）。`next_offset` が null でなければ、`offset` に渡して呼び直すと続きが返る。1 回の件数は既定 20、`limit` で 100 まで |
| `get_issues` | `repo_root`、`stream`、`cids`（10 件まで） | CID ごとの警告経路（`events`。`main` が true のものが ★）とチェッカーの説明（`checker_description`）。`remaining` が空でなければ、その CID で呼び直す |
| `preview_triage` | `entries`（`[{id, cids, stream, classification, action, severity, comment}]`。`stream`（警告のあるストリーム。書くと、そのストリームが使うトリアージストアに書く）、`severity`、`comment` は省ける） | 書き戻す件数と内容と、確認用の文字列（`confirmation_token`） |
| `update_triage` | `repo_root`、`preview_triage` に渡したものと同じ `entries`、`confirmation_token` | 書き込んだ項目と、書き込めなかった項目（`failed`） |

書き戻すときは、`preview_triage` の件数と内容を人に見せ、同意を得てから `update_triage` を呼びます。
