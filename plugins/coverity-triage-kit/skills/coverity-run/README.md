# 一括のトリアージ（Coverity の警告）

## できること

Coverity Connect の警告のうち、条件に合うものを一括でトリアージします。

- 条件ファイル（と頼みの言い方）から実行フォルダを作り、警告と警告経路を取り、同じ原因らしい警告をグループにします。
- 警告ごと（グループごと）に、作業役のエージェントに真偽の調査と、修正案・逸脱コメント案の両方を頼みます。
  修正案は、リポジトリの外に作る作業用のコピー（git worktree か svn checkout）の上で作ります。作業中のファイルは変えません。
- 設定により、検証役のエージェントに結果を確かめさせ（差し戻しは作り直す）、統合役のエージェントに同じ原因のものをまとめさせます。
  ビルド（と再解析）での確かめ、始める前の初回解析（ビルド・解析・サーバへの登録）もできます。
- 終わったら、一覧 `summary.html`、詳細 `cid/<ID>.html`、差分 `patches/<ID>-fix.patch`、修正後のファイル `fixed/<ID>/fix/`、
  推奨を入れた承認の案 `approvals.json` を作ります。

次に何をするかはスクリプト（`tick`）が決めます。チャットの AI は、その指示どおりに Coverity との通信の道具を呼び、役のエージェントに頼むだけです。
人が承認して反映するまで、作業中のファイルも Coverity のトリアージも変えません。

## 必要なもの

- Windows の PC（Linux でも動きます）
- [uv](https://docs.astral.sh/uv/)（Python とライブラリは uv が用意します）
- git か svn（調べるリポジトリのもの）
- 調べるリポジトリの `.coverity-triage/config.yaml` と `filters/*.yaml`
- Coverity から警告と警告経路を取れる道具（MCP サーバーなど）。なければ、取ってある警告の一覧で始められます（下の `--issues-file`）
- 役のエージェント：説明に「作業役」「検証役」「統合役」とあるもの（検証役と統合役は、使う設定のときだけ）
- （初回解析・ビルドでの確かめを使うとき）`cov-build`、`cov-analyze`、`cov-format-errors`、`cov-commit-defects` とビルドのコマンド

## 置き方

このフォルダを、そのままスキルのフォルダに置きます（名前を変えてもかまいません）。

```
# Windows（PowerShell）
New-Item -ItemType Directory -Force "$env:USERPROFILE\.copilot\skills" | Out-Null
Copy-Item -Recurse "<このフォルダ>" "$env:USERPROFILE\.copilot\skills\<名前>"
# Linux（bash）
mkdir -p ~/.copilot/skills
cp -r "<このフォルダ>" ~/.copilot/skills/<名前>
```

作業フォルダの `.github/skills/<名前>/` に置いても使えます。プラグインに入れて配られているときは、置く必要はありません。

## 呼び方

新しく始めるときは、スラッシュで呼び、条件を続けて書きます。例：`/<スキルの名前> High だけ 10 件`、`/<スキルの名前> misra.yaml で、最初に解析して`、`/<スキルの名前> 前回の続きから`（プラグインから入れたときは `/<プラグインの名前>:<スキルの名前>` でも呼べます）。
スラッシュなしの「Coverity の警告をトリアージして」だけでは、新しい実行を始めません（コマンドとして呼んだときだけ始めます）。すでにある実行の「進み具合は？」「一覧を作り直して」は、チャットで頼めます。

AI は、始める前に 1 回だけ、実行フォルダの読み書きと `uv` のコマンドを、このセッションのあいだ許可するしかたを示します。
Copilot CLI では `/add-dir` と `copilot --add-dir … --allow-tool …` を、Claude Code では `/add-dir` と `/permissions`（`Bash(uv run *)`）と `claude --add-dir … --allowedTools …` を示します。どちらでも、Coverity に書き戻すツールは前もって許可しません。
始めたら、終わるまで質問しません。

## スクリプトを直接動かす

```
uv run --native-tls --quiet "<このフォルダ>/scripts/run.py" <コマンド> <引数>
```

| コマンド | すること |
|---|---|
| `settings --repo R` | 設定の今の値と条件ファイルの一覧、設定の問題（読むだけ） |
| `runs --repo R` | 実行の一覧（新しい順。別の形の実行は `foreign: true`） |
| `new-run --repo R [--filter F] [--impact X]… [--checker X]… [--status X]… [--classification X]… [--action X]… [--limit N] [--verify none\|build\|build+analyze] [--initial\|--no-initial] [--review\|--no-review] [--integration\|--no-integration] [--max-rework N] [--issues-file F]` | 実行フォルダを作る |
| `resume --run D` | 途中の実行を続きからにする |
| `tick --run D [--finished T] [--failed T --reason R] [--wait S]` | 次にすること（`actions`）を返す |
| `status --run D` | 実行の様子（読むだけ。鍵を取らない） |
| `summary --run D` | 一覧と承認の案を作る。作業用のコピーを消す |
| `stop --run D` | 切り離して動かしている処理を止め、止めた印をつける |
| `note-issue --run D --title T --body B`（または `--stdin`） | 振り返りの課題を書き残す |
| `initial-status --run D` | 初回解析の段と進み |

どのコマンドも、標準出力に JSON を 1 つ返します。失敗は `{"ok": false, "error": "…", "problems": […]}` と終了コード 1 です。
出力の項目は [references/actions.md](references/actions.md) にあります。

`--issues-file` は、Coverity と通信できないときに、取ってある警告の一覧（MCP サーバーの検索の出力と同じ形、`finished_at` 入り）で始めます。
同じフォルダに `issue-<CID>.json`（警告経路）があれば、それも使います。

### tick の出力（あらまし）

```json
{"ok": true, "stage": "items", "stage_label": "警告ごとの作業", "actions": [
  {"type": "launch", "task": "worker:20001@1", "role": "worker", "item": "20001", "description": "20001 の作業",
   "prompt": "指示ファイル …/items/20001/brief.md を読み、書かれた手順に従ってください。", "background": true}],
 "wait": false, "done": false, "stop": false, "say": [], "next": "…"}
```

`actions` の `type` は `fetch_issues`（警告の取得）、`fetch_details`（警告経路の取得）、`launch`（役のエージェントに頼む）、
`command`（短いコマンド）です。終わったら `--finished <task>`、失敗したら `--failed <task> --reason "…"` で知らせます。
結果のファイルができていれば、知らせがなくても進みます。30 分動きのない仕事は失敗としてやり直します。

### status の出力（あらまし）

`stage`、`stopped`（止めた）、`halted` と `halt_reason`（続けられないため止まっている。認証の失敗、5xx、初回解析の失敗など。
直して `resume` すると `false` に戻る）、`counts`、作業ごとの `items[]`（段・経過時間・`stalled`（10 分動きなし）・確かめの結果・人に見せる確信度）、
`running_tasks[]`、`errors[]`、`recent[]`（新しい順に 10 行）、`initial`（初回解析の進み）、`progress`（処理した件数 / 全体）、
`run_checks[]`（実行ごとの評価基準の合否）、`criteria[]`（評価基準の文）、`criteria_results[]`（基準ごとの合格・不合格の件数）、
`issues_found[]`（書き残した課題）。

## 設定（`.coverity-triage/` で読むもの）

| 項目 | 既定 | 使い方 |
|---|---|---|
| `coverity.url`、`coverity.api` | （必須）、`auto` | 初回解析の登録先。`api: fake` なら登録を飛ばす |
| `coverity.user_env` / `coverity.key_env` | `COV_USER` / `COV_AUTH_KEY` | 初回解析の登録の認証情報（キーは環境変数 `COVERITY_PASSPHRASE` でだけ渡す）。記録では `****` に伏せる |
| `coverity.path_strip_prefixes` | `[]` | 警告のパスから外す先頭 |
| `vcs.type` / `vcs.base_branch` / `vcs.branch_prefix` | 自動 / 自動 / `coverity-fix/` | 作業用のコピー、修正の元のリビジョン、ブランチの名前 |
| `output_dir` | リポジトリの隣の `coverity-triage-out` | 実行フォルダの置き場所（リポジトリの中なら始めない） |
| `max_items`、`parallel` | `100`、`3`（1〜8） | 1 回の上限、同時に動かす作業役の数 |
| `options.annotation` / `per_run_branch` / `markdown` / `metrics` | `false` | 注釈の案も作る / ブランチを実行ごとに 1 つにする（作らない）/ `.md` も作る / `metrics.json` を作る |
| `verify.default`、`verify.initial`、`verify.setup_command`、`build_dir`、`build_command`、`cov_build_args`、`cov_analyze_args`、`cov_commit_args`、`timeout_minutes` | `none`、`false`、…、`180` | ビルドでの確かめと初回解析 |
| `agents.review` / `agents.integration` / `agents.max_rework` | `false` / `false` / `1`（0〜2） | 確かめと統合を使うか、作り直しの上限 |

- 知らない項目は無視します。空の値は既定の値になります。値の間違いは「config.yaml の parallel: 8 以下にしてください」の形で示します。
- 条件ファイル `filters/*.yaml`（`name`、`project`、`streams`（1 つ）、`checkers`、`impacts`、`triage.classification` / `action` / `status`、`limit`）、
  `knowledge.md`（指示ファイルで作業役などに示す）、`no-grouping.yaml`（グループにしない CID）も読みます。

## 結果の見方（実行フォルダ）

`<output_dir>/<YYYYmmdd-HHMMSS>/`：

```text
run.json                 実行の状態（format: 2、tool_version。このスクリプトだけが書く。形は assets/run.schema.json）
filter.json  issues.json  issues.mapped.json  config.snapshot.yaml
schemas/                 結果・確かめ・統合の形と評価基準の写し（指示ファイルが指す）
operations.log           出来事（1 行に 1 つの JSON：time、event、level、item、message。認証情報は ****）
details/                 取った警告経路（issue-<CID>.json）
items/<ID>/              brief.md、issue-<CID>.json、progress.json、result.json、result.previous.json、review.json、saved.json
agents/                  確かめと統合の指示（reviewer/<ID>.md、integrator/brief.md）と進み具合
integration/             candidates.json、plan.json、merged.json
analysis/                初回解析（status.json、initial.log）
jobs/                    切り離して動かす処理（<名前>.json：pid、状態、始めた時刻。<名前>.log）
work/w<n>/ a/ m/ …        作業用のコピー（一覧を作るときに消す）
cid/<ID>.html  summary.html  patches/<ID>-fix.patch  fixed/<ID>/fix/<パス>
approvals.json  verify.json  metrics.json  issues-found.jsonl
```

- `summary.html` と `cid/<ID>.html` は 1 つのファイルで、スクリプトを含まず、AI の書いた文はすべてエスケープしてあります。
- 人に見せる確信度は、作業役・確かめ・ビルドでの確かめのいちばん低いものです（`result.json` は変えません）。
  決まりは `scripts/confidence.py`（下の「決まった確かめ」）。
- `approvals.json` には推奨が入ります（最新で直っていて差分がないものは「反映しない」）。人が選んだ値は書き換えず、ない作業の鍵だけを足します。
  ファイルが壊れている（JSON として読めない、形が `assets/approvals.schema.json` と違う）ときは、書き換えずに `ok: false` で止め、ファイルの場所を示します。
- `run.json` の `tool_version` は、実行フォルダの形の版です（`scripts/runlib/state.py` の `TOOL_VERSION`）。
  実行フォルダの形か意味を変えたときだけ、手で 1 つ上げます。違う版のスクリプトは、その実行フォルダを読むだけで変えません（再開もしません）。
  形を変えない直し（文の直し、不具合の直し）では上げません。
- `run.json` で、ほかの部品が読む項目は `assets/run.schema.json` に決めてあります（`jobs/<名前>.json` の形は、その中の `$defs.job`）。
  反映したかどうかは、`run.json` に書きません（反映の記録は反映する側が持ちます）。
- `verify.json`：作業ごとの `{mode, applied, build_ok, problems, note, resolved_cids, remaining_cids, new_issue_count, log}`。
- `metrics.json`（`options.metrics`）：`{"version": 1, "run_id", "items": [{item, cids, recommendation, confidence, shown_confidence, judgement, seconds, attempts, rework_rounds, review, build_ok}], "totals": {…}}`。

## 決まった確かめ（ほかの部品と同じ決まり）

エージェントが書いたものは、次のスクリプトで確かめます。どれも、持ち主の部品のスクリプトを、そのまま（同じバイト列で）写したものです。
`scripts/runlib/` は、これらを読み込んで使うだけで、同じ決まりを自分では書きません。持ち主を直したら、写しも同じに直します。

| ファイル | 決まり |
|---|---|
| `scripts/check_result.py` | 1 件の結果（`result.json`）の形と決まり（逸脱コメントの決まりを含む） |
| `scripts/check_comment.py` | 逸脱コメントの決まり |
| `scripts/check_review.py` | 確かめの結果（`review.json`）：評価基準の `scope: item` をすべて 1 回ずつ（当てはまらないものも `ok: true` と「対象外」で書く）、`flag` は確信度の上限つき、など |
| `scripts/lint_result.py` | 検証役に示す、決まった方法で見つけた点 |
| `scripts/check_plan.py` | 統合の計画（`integration/plan.json`）の形と決まり（逸脱コメントの決まりを含む） |

`scripts/confidence.py` は、このスキルが持ち主の決まり（人に見せる確信度）です。標準ライブラリだけを使い、ほかのファイルを読まないので、
ほかの部品はこのファイルをそのまま写して使えます。共通の試験のデータは `assets/contract/confidence/cases.json` です。
この決まりだけを動かすときは、`uv run --native-tls --quiet "<このフォルダ>/scripts/confidence.py" --stdin` に
`{"result": {…}, "review": {…} か null, "verify": {…} か null}` を渡します。

このほか、スクリプトは tick ごとに、決まりにない確かめもします（作業用のコピーに変更があるか、確かめの間に結果と差分が変わっていないか、など）。

## うまくいかないとき

[references/troubleshooting.md](references/troubleshooting.md) にまとめてあります。よくあるもの：

- 「設定ファイルがありません」：`.coverity-triage/config.yaml` を用意します。
- 「実行フォルダの置き場所がリポジトリの中です」：`output_dir` をリポジトリの外にします。
- 「別の版のスクリプトで作られました」「別の形で作られたものです」：その実行は続きからできません。新しく始めます。
- 「承認のファイル … が壊れているため、書き換えません」：示されたファイルを直すか、選んだ承認が要らなければ消してから、もう一度 `summary` を動かします。
- Windows で日本語が化けるとき：スクリプトはコンソールの文字コードで書きます。`PYTHONIOENCODING` を付けると、その文字コードで書きます。

## テストを動かす

このフォルダで動かします（uv があれば uv で、なければ pyyaml と jsonschema のある Python でスクリプトを動かします。git が要ります。svn の試験は svn があるときだけ）。

```
# Windows（PowerShell）
py -3 -m unittest discover -s tests
# Linux（bash）
python3 -m unittest discover -s tests
```

試験は一時フォルダに git のリポジトリ・偽の Coverity の出力・偽のエージェントの出力・偽の `cov-*` を作って、`tick` の流れを最後まで動かします。
`assets/contract/<名前>/cases.json`（ほかの部品と共通の試験のデータ：comment、result、lint、plan、confidence）があれば、
`scripts/runlib/` が使う道筋で、すべての組を動かします。
試験の課題（モデルで解かせて採点する）は `evals/` にあります。
