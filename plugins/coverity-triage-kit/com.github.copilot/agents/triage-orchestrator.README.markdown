# 統括エージェント（triage-orchestrator）

条件に合う Coverity の警告を、チャットで一括でトリアージするエージェントです（3段階目）。

- 警告ごとに、作業エージェントが本物のバグかを調べ、修正案と逸脱コメント案の両方を作ります。
- 設定で使うと決めたときは、検証エージェントが結果を確かめ（差し戻しは作り直し）、統合エージェントが同じ原因の警告をまとめます。
- 終わると、実行フォルダに一覧・詳細・差分・承認の案ができます。人が承認して反映するまで、作業中のファイルも Coverity も変わりません。

統括エージェントは、ファイルを直さず、警告を自分で調べません。次に何をするかは一括実行のスキルが決め、統括エージェントはそれに従ってエージェントを呼びます。

## 用意するもの

- GitHub Copilot CLI と uv
- 調べたいリポジトリに `.coverity-triage/` の設定（`/coverity-setup` で作る）
- Coverity と通信する MCP サーバー `coverity-connect`（プラグインを入れると登録される）と、認証情報の環境変数（入れてから Copilot CLI を起動する）

## 始め方

1. 調べたいリポジトリのフォルダで、Copilot CLI を起動します。
2. 下の「前もって許可する」をします（夜通し動かすときは必ず）。
3. `/agent` で `triage-orchestrator · plugin` を選び、チャットで頼みます。
   `/agent` で選ばずに `/coverity-run` と入力しても、同じ流れで始まります。

始めた後は、終わるまで質問しません。5件ほどごとに進み具合を1行で伝えます。

## 頼み方の例

| 頼み方 | すること |
| --- | --- |
| `Coverity の警告をトリアージして` | 既定の条件ファイルの警告を調べる |
| `High だけトリアージして` | Impact が High の警告だけ |
| `misra.yaml でトリアージして` | 条件ファイルを選ぶ |
| `10 件だけトリアージして` | 調べる件数の上限 |
| `最初に解析してからトリアージして` | 開いているフォルダをビルド・解析して Coverity に登録してから（数十分かかることがある） |
| `解析はしないでトリアージして` | 初回解析をしない |
| `/coverity-run High だけ` | 統括エージェントを選ばずに、同じことをする |

- 途中で止まった実行があると、「前回の実行（日時・条件）が途中です。続きから再開しますか？」と聞かれます。再開のときは、初回解析をするかどうかを変えられません。
- 設定がないときは、準備のしかたを示して、始めません。

## 前もって許可する

実行フォルダはリポジトリの外にあるので、エージェントがそこに書くたびに、Copilot CLI が許可を求めます。`uv` のコマンドと、Coverity と通信するツールの初めての呼び出しも、許可を求めます。始める前に、次のどちらかを1回だけします。

- チャットで `/add-dir "<output_dir>"` を打ち、最初の確認で「このセッションでは許可」を選ぶ（シェルの `uv`、ファイルの書き込み、Coverity と通信するツール）。
- Copilot CLI を、次のように起動し直す。

```text
copilot --add-dir "<output_dir>" --allow-tool 'shell(uv:*)' --allow-tool write --allow-tool 'coverity-connect(coverity_check_connection)' --allow-tool 'coverity-connect(coverity_search_issues)' --allow-tool 'coverity-connect(coverity_get_issue_details)' --allow-tool 'coverity-connect(coverity_preview_triage)'
```

`<output_dir>` は実行フォルダの置き場所です（既定は、リポジトリの隣の `coverity-triage-out`）。書き戻すツール（`coverity_update_triage`）は前もって許可しません。反映のときに確認が出ます。始めるときにも、統括エージェントが同じ手順を1回だけ示します。

## できるもの

実行フォルダ `<output_dir>/<日時>/` に、次ができます。

| ファイル | 中身 |
| --- | --- |
| `summary.html` | 一覧（承認・推奨・確信度・見立て。確信度の低い順） |
| `cid/<ID>.html` | 警告ごとの詳細レポート |
| `patches/<ID>-fix.patch` | 修正の差分（git の形） |
| `fixed/<ID>/fix/<パス>` | 修正後のファイル |
| `approvals.json` | 承認の案（推奨が入っている） |

承認（修正・逸脱・却下・反映しない）の選択と逸脱コメントの手直しは、`/coverity-gui` の画面の「結果」で行い、`/coverity-apply` で反映します。

## 取り出して使う

プラグインを入れずに、3段階目の部品（4つのエージェントと、その表のスキルと MCP サーバー）だけで使えます。

1. 4つのエージェントの定義を、リポジトリの `.github/agents/`（自分だけで使うときは `%USERPROFILE%\.copilot\agents\`、Linux では `~/.copilot/agents/`）にコピーします：`triage-orchestrator.agent.md`、`triage-worker.agent.md`、`triage-reviewer.agent.md`、`triage-integrator.agent.md`。
2. 各定義の「使う部品」の表にあるスキルのフォルダ（`skills/<名前>/`）を、`.github/skills/<名前>/`（自分だけで使うときは `%USERPROFILE%\.copilot\skills\<名前>\`、Linux では `~/.copilot/skills/<名前>/`）にコピーします。統括エージェントの表と、作業・検証・統合のエージェントの表をたどると、次の11個です：`coverity-run`、`coverity-gui`、`coverity-apply`、`coverity-setup`、`checker-knowledge`、`triage-investigation`、`code-fix`、`deviation-comment`、`triage-report`、`triage-review`、`triage-integration`。
3. MCP サーバーのフォルダ（`mcp-servers/coverity-connect/`）を置き、その README のとおりに `coverity-connect` という名前で登録します。
4. Copilot CLI を起動し直し、`/agent` に4つのエージェントが、`/skills list` に11個のスキルが出れば完了です。

## うまくいかないとき

| 起きること | すること |
| --- | --- |
| 「設定がありません」と言われて始まらない | `/coverity-setup` で準備する |
| 「その役のエージェントがない」と言われる | `/agent` で、作業・検証・統合のエージェント（`triage-worker`・`triage-reviewer`・`triage-integrator`）が出るかを確かめる。取り出したときは、4つの定義を同じ場所に置く |
| 実行中に許可の確認が出て止まる | 確認に答える。次からは、始める前に「前もって許可する」をする |
| 警告を取れない（認証の失敗など） | 認証情報の環境変数を入れてから Copilot CLI を起動し直す。MCP サーバーの README の「うまくいかないとき」を見る |
| 途中で止まった | もう一度頼むか `/coverity-run` と入力すると、続きから再開できる |
| 処理できなかった警告がある | 一覧の「処理できなかった作業」に出る。再開すると、やり直す |
| AI クレジットを抑えたい | 検証と統合を使わない設定にする（`/coverity-setup`）。件数の上限を小さくする |
