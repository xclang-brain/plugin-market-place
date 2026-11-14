# 仕組み

**処置の順序は文章が持ち、状態はフォルダーが持ちます。** スキルとエージェントの文章は「スクリプトの応答に書かれたコマンドを実行する」ことだけを決め、スクリプトが状態を結果フォルダーに保存します。会話が途切れても、同じ依頼で続きから再開できるのはこのためです。

## 構成

```mermaid
flowchart LR
  user(["利用者"]) <--> cli["Copilot CLI<br>スキル・エージェント"]
  cli -- "uv run" --> scripts["setup.py<br>triage.py<br>apply.py"]
  cli -- "ツール" --> mcp["MCP サーバー<br>coverity-connect"]
  mcp <--> cov[("Coverity Connect")]
  scripts --> run[("結果フォルダー")]
  mcp --> run
  run --> gui["gui.py"] <--> browser["ブラウザー"]
  scripts -. "OK の後だけ" .-> repo[("利用者のリポジトリ")]
```

| 構成要素 | 場所 | 役割 |
| --- | --- | --- |
| MCP サーバー `coverity-connect` | `mcp-servers/coverity-connect/` | Coverity Connect との通信。接続の確認、警告の検索、経路の取得、登録の4ツール |
| スキル `coverity-setup` | `skills/coverity-setup/` | 設定ファイルの作成と確認（`setup.py`） |
| スキル `coverity-triage` | `skills/coverity-triage/` | CID 1件の処置の方法（`SKILL.md`、`references/`）と進行（`triage.py`） |
| スキル `coverity-apply` | `skills/coverity-apply/` | 一覧と、OK の後の取り込み（`apply.py`） |
| スキル `coverity-gui` | `skills/coverity-gui/` | ローカルの画面（`gui.py`、`static/`） |
| エージェント | `com.github.copilot/agents/` | `triage-worker`（2段階目と、3段階目の処置役）、`triage-orchestrator`（3段階目の進行）、`triage-reviewer`（検証）、`triage-integrator`（統合） |

スキルのフォルダーは、ほかのフォルダーのコードを読み込みません。スキルの間の取り決めは、結果フォルダーのファイルの形だけです。

## スクリプトの応答

`triage.py` と `apply.py` のコマンドは、JSON を1つ返します。モデルは、この応答だけを見て次に進みます。

| キー | 内容 |
| --- | --- |
| `next` | 続けて実行するコマンドの全文。**利用者の言葉を待たずに実行してよいものだけ**を入れます。空なら、利用者の入力を待ちます |
| `fetch` | 先に呼び出す MCP のツールと引数 |
| `message` | 利用者に伝える文 |
| `ok`、`error`、`problems` | 失敗したか。`problems` は、モデルが訂正して再実行できる項目 |
| `take` | CID を受け取るコマンド。1段階目で、利用者が CID を指定したときに使います |
| `after_ok` | OK の後に実行するコマンド（`apply.py apply`） |
| `to_change` | 選択を変えるときに `--set` を書き換えて実行するコマンド |
| `call`、`if_not_called` | 呼び出す登録のツールと、呼び出せなかったときに文字を変えずに伝える文 |

- コマンドは、1回の呼び出しで1行を、書かれたとおりに実行させます。スキルの `allowed-tools` は `shell(uv run:*)` なので、シェルの変数、`;`、`&&`、パイプ、`cd` を使った形は許可されません。
- 流れを応答で決めるのは、小型のモデルが、スキルの文章の離れた箇所より、目の前の応答に従うためです。実際のモデルでの確認で、応答で決めないと1段階目の手順が守られないことが分かりました。

## 処置の流れ

```mermaid
sequenceDiagram
  autonumber
  participant A as モデル
  participant T as triage.py
  participant M as MCP サーバー
  participant R as 結果フォルダー
  A->>T: start
  T->>R: run.json、作業用コピーの準備（別プロセス）
  T-->>A: fetch: coverity_search_issues、next: start
  A->>M: coverity_search_issues
  M->>R: issues.json
  A->>T: start（準備が終わるまで繰り返す）
  T-->>A: next: next
  A->>T: next
  T-->>A: cid、work_dir、result_file、fetch: coverity_get_issue_details
  A->>M: coverity_get_issue_details
  M->>R: cid/<CID>/issue.json
  Note over A: work_dir でコードを調べ、修正を書き、result.json を書く
  A->>T: save
  T->>R: fix.patch、state.json
  T-->>A: next: 次の next、または finish（1段階目は空）
  A->>T: finish
  T->>R: summary.md
  T-->>A: next: apply.py plan
```

- 作業用コピーは、git では取り込み先のブランチの最新のコミットの worktree、svn ではチェックアウトのコピーです。利用者の作業ツリーは編集しません。並列の処置役も、互いの修正の影響を受けません。
- `save` は、結果の形式と逸脱コメントの規則（`checks.py`）を確認し、`git diff` / `svn diff` の出力をバイト列のまま `fix.patch` に保存します。文字コードと改行コードを変えないためです。
- 作業用コピーの準備、ビルド確認、最初の解析は時間がかかるため、切り離したプロセスで実行し、同じコマンドをもう一度呼んで完了を確認します。
- 取り込み先のブランチが変わった後の依頼は、新しい結果フォルダーを作ります。1つの結果フォルダーの修正は、すべて同じリビジョンが基準です。

## OK の後

```mermaid
sequenceDiagram
  autonumber
  actor U as 利用者
  participant A as モデル
  participant P as apply.py
  participant M as MCP サーバー
  A->>P: plan
  P-->>A: table、question、to_change、after_ok
  A->>U: 一覧と質問（1回だけ）
  U->>A: 「20003 は保留、ほかは OK」
  A->>P: to_change（--set 20003=none）
  P-->>A: 新しい after_ok
  A->>P: apply
  Note over P: 一覧を表示した時点の計画と照合し、<br>変わっていれば何もしない
  Note over P: git：一時 worktree で coverity-fix/… にコミットして push<br>svn：svn patch
  Note over P: coverity-entries.json とハッシュ値を書く
  P-->>A: call: coverity_update_triage
  A->>M: coverity_update_triage（Copilot CLI が許可を確認）
  Note over M: ハッシュ値が一致するときだけ登録し、<br>written.json に記録
  M-->>A: written、failed
```

- `apply` を `next` ではなく `after_ok` に入れるので、応答に従うだけのモデルが OK の前に取り込むことはありません。
- 選択の優先順位は、チャットでの指定、画面での選択、推奨の順です。
- 保留の CID にも「Pending」を登録します。未分類のまま残すと、次の取得で（ほかの人の PC でも）もう一度処置されるためです。

## 結果フォルダー

`<output_dir>/<年月日-時分秒>/` が、1回の取得とその処置の状態のすべてです。**ファイルごとに、書き込む構成要素は1つです。**

| ファイル | 書き込むもの | 内容 |
| --- | --- | --- |
| `run.json` | `triage.py` | リポジトリ、git / svn、取り込み先のリビジョン、ストリーム、条件、並列数、検証の有無 |
| `issues.json`、`cid/<CID>/issue.json` | MCP サーバー | 警告の一覧、警告の経路 |
| `cid/<CID>/state.json` | `triage.py` | CID の状態、検証の結果、統合の関係 |
| `cid/<CID>/result.json` | 処置役のモデル（`save` が Coverity の値を追記） | 判定、根拠、推奨、確信度、逸脱コメント |
| `cid/<CID>/fix.patch`、`review.json` | `triage.py` | 修正コード、検証の結果と指摘 |
| `summary.md` | `triage.py` | 全 CID の2つの出力 |
| `decisions.json` | `gui.py` | 画面での選択と、編集した逸脱コメント |
| `apply-plan.json`、`applied.json`、`coverity-entries.json` | `apply.py` | 一覧を表示した時点の計画、取り込んだ修正、登録する内容 |
| `written.json` | MCP サーバー | Coverity に登録済みの CID |
| `work/` | `triage.py` | 作業用コピー。完了時に削除 |

## CID の状態

```mermaid
stateDiagram-v2
  direction LR
  state "未処置" as none
  state "working（処置中）" as working
  state "done（処置済み）" as done
  state "failed（失敗）" as failed
  state "rework（作り直し）" as rework
  [*] --> none
  none --> working: next
  working --> done: save
  working --> failed: 2回渡しても保存されない
  done --> rework: 差し戻し・統合・利用者の指摘
  failed --> rework: 利用者が CID を指定
  rework --> working: next
```

- 統合された CID は `merged_into` を持ち、代表の CID の修正と逸脱コメントを共有します。
- 検証の結果は、合格（`pass`）、要確認（`flag`）、差し戻し（`rework`）、未検証（`unverified`）です。差し戻しは1回だけ作り直します。

## 3段階目の進め方

`triage-orchestrator` は、`status` の応答を見て、最初に当てはまるものを行い、また `status` に戻ります。

1. `next` に `next` のコマンドがある：その行の数だけ `triage-worker` を同時に起動します。
2. 統合の候補（同じチェッカーと関数、または修正が同じ行を変更）がある：`triage-integrator` に1回だけ依頼します。
3. `next` が `review` のコマンド：`triage-reviewer` を同時に起動します。2回依頼しても残った CID は、未検証として記録します。
4. `next` が `finish` のコマンド：完了して、一覧に進みます。

依頼先は3つのエージェントだけです。`tools` では依頼先を絞れないため、エージェントの frontmatter のフック（bash と PowerShell）で、ほかへの依頼を拒否します。

## 画面

`gui.py` は、127.0.0.1 だけで待ち受ける HTTP サーバーです。結果フォルダーを読んで表示し、書き込むのは `decisions.json` だけです。URL のトークンと、`Host`・`Origin` の確認で、ほかのページやほかの PC からの要求を拒否します。要求が30分ないと止まります。

## 設計上の決定

| 決定 | 理由 |
| --- | --- |
| どの段階も同じコマンドを使う | どの段階の結果も、同じ画面と同じ OK の手順で扱えます |
| 一覧は1つ、質問は1回 | 利用者が確認する対象と、OK の意味を1つにします |
| 取り込み先は、チェックアウトしているブランチ | 利用者が作業しているブランチの最新のコードで処置し、そのブランチへのプルリクエストを用意します |
| テスト用の動作を出荷するコードに入れない | テストは、Coverity Connect の代わりの HTTP サーバー（`tests/coverity_emulator`）に接続します |

詳しくは[設計書](gh:docs/design/design.md)にあります。
