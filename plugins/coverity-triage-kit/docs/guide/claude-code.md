# Claude Code ガイド

Claude Code で coverity-triage-kit を使う人のための手引きです。1〜3段階目（スキル・MCP サーバー、作業エージェント、一括トリアージ）が使えます。4段階目の Web管理画面は Copilot CLI だけの機能で、Claude Code では使えません。

## セットアップ

### 1. インストールする

```shell
claude plugin marketplace add xclang-brain/plugin-market-place
claude plugin install coverity-triage-kit@plugin-market-place
```

Claude Code の中からは、`/plugin marketplace add xclang-brain/plugin-market-place` と `/plugin install coverity-triage-kit@plugin-market-place` でもインストールできます。インストール後は Claude Code を再起動します。

`/mcp` に `plugin:coverity-triage-kit:coverity-connect` が表示されれば、Coverity と通信する MCP サーバーも読み込まれています。

### 2. 認証情報を環境変数に設定する

環境変数 `COV_USER` と `COV_AUTH_KEY` を設定してから、Claude Code を起動します（プロキシを使う場合は `HTTPS_PROXY` / `NO_PROXY` も）。プラグインがこの4つを MCP サーバーに渡します。認証情報は、設定ファイルにもチャットにも書きません。

### 3. 設定を作る

調査したいリポジトリで Claude Code を起動し、`/coverity-triage-kit:coverity-setup` を実行します。設定は Copilot CLI と共通で、リポジトリの `.coverity-triage/` に保存されます。

## 使い方

調査したいリポジトリのフォルダで Claude Code を起動します。

| 段階 | 起動方法と頼み方 |
| --- | --- |
| 1段階目（スキル・MCP サーバー） | `/coverity-triage-kit:<スキル名>` で呼び出します（同名のスキルが他になければ `/<スキル名>` でも可）。依頼の内容に合うスキルを AI が自動で選ぶこともあります |
| 2段階目（1件のトリアージ） | `claude --agent coverity-triage-kit:triage-worker` で起動し、「CID 12345 をトリアージして」と頼みます |
| 3段階目（一括トリアージ） | `claude --agent coverity-triage-kit:triage-orchestrator` で起動し、「High だけトリアージして」と頼みます。または、通常どおり起動して `/coverity-triage-kit:coverity-run High だけ` と入力します |
| 4段階目（Web管理画面） | 使えません（Copilot CLI だけ） |

知っておくこと：

- **2段階目は `claude --agent` で起動するのがおすすめです。** この起動方法のときだけ、作業エージェントが質問をし、差分を見せ、同意を得てからファイルを修正します。通常のセッションで「triage-worker エージェントで CID 12345 をトリアージして」と名前を指定して頼むこともできますが、その場合は修正の差分を提案として返すだけで、ファイルは変更しません（サブエージェントは利用者に質問できないためです）。
- **反映（`/coverity-triage-kit:coverity-apply`）は、`--agent` を付けずに起動した Claude Code で行います。** 統括エージェントのセッションは、ファイルの書き込みと Coverity への書き込みのツールを持っていません。
- 統括エージェントが呼び出したエージェントは、バックグラウンドで動くことがあります。終わると結果が通知されます。

## 一括トリアージの前に、ツールを許可しておく

バックグラウンドで動くエージェントは、許可の確認を表示できません。事前に許可していないツールは使えず、そこで処理が止まります。一括トリアージを始める前に、次のどちらかの方法で許可してください。夜間に無人で実行する場合は、確実な方法 A を使います。

**方法 A：許可を指定して起動する（おすすめ）**

```shell
claude --add-dir "<output_dir>" --allowedTools "Bash(uv run *)" "Skill(coverity-triage-kit:*)" "Read(~/.claude/plugins/cache/plugin-market-place/coverity-triage-kit/**)" "Edit(//<output_dir の絶対パス>/**)" "mcp__plugin_coverity-triage-kit_coverity-connect__coverity_check_connection" "mcp__plugin_coverity-triage-kit_coverity-connect__coverity_search_issues" "mcp__plugin_coverity-triage-kit_coverity-connect__coverity_get_issue_details" "mcp__plugin_coverity-triage-kit_coverity-connect__coverity_preview_triage"
```

統括エージェントで始める場合は、末尾に `--agent coverity-triage-kit:triage-orchestrator` を追加します。この方法の許可は、そのセッションの間だけ有効です。

**方法 B：起動後にチャットで許可する**

`/add-dir "<output_dir>"` を実行し、続けて `/permissions` で次の8つを許可します。`/add-dir` はフォルダを追加するだけで、ツールは許可されません。

```text
Bash(uv run *)
Skill(coverity-triage-kit:*)
Read(~/.claude/plugins/cache/plugin-market-place/coverity-triage-kit/**)
Edit(//<output_dir の絶対パス>/**)
mcp__plugin_coverity-triage-kit_coverity-connect__coverity_check_connection
mcp__plugin_coverity-triage-kit_coverity-connect__coverity_search_issues
mcp__plugin_coverity-triage-kit_coverity-connect__coverity_get_issue_details
mcp__plugin_coverity-triage-kit_coverity-connect__coverity_preview_triage
```

`/permissions` で追加した許可は設定ファイルに保存され、後のセッションにも残ります。保存先には個人用の設定（`.claude/settings.local.json`）を選び、実行が終わったら `/permissions` で削除してください。

### 一括トリアージは、開始前に一度だけ待ちます

`/coverity-run`（または統括エージェント）は、許可の方法を表示したところで一度止まり、「始めて」と返信するまで待ちます。先に許可を済ませてもらうためです。方法 A で起動して無人で実行する場合は、依頼に「許可は起動のときに済ませました」または「途中で質問せず」と書き添えると、待たずに始まります。

```text
High だけトリアージして。許可は起動のときに済ませました
```

### それぞれの許可の意味

| 許可 | 意味 |
| --- | --- |
| `Bash(uv run *)` | スキルのスクリプトの実行 |
| `Skill(coverity-triage-kit:*)` | このプラグインのスキルの使用。許可しないと、スキルを使うたびに確認が出ます |
| `Read(~/.claude/plugins/cache/…/**)` | インストールしたこのプラグインのフォルダの読み取り。スキルの参照資料がここにあります。読み取りだけの許可です |
| `Edit(//<output_dir の絶対パス>/**)` | 出力先の中への書き込み（新規ファイルの作成を含む）。それ以外の場所、たとえば調査中のリポジトリに書き込もうとすると確認が出ます |
| 残りの4つ（`coverity_check_connection` など） | Coverity から情報を読み取るツール |

- `<output_dir>` は出力先です。既定は、リポジトリと同じ階層の `coverity-triage-out` です。`/coverity-run` も、開始前に実際のパスを入れたコマンドを表示します。
- **パスの書き方**：絶対パスの先頭に `/` をもう1つ付けます。`/home/me/out` は `Edit(//home/me/out/**)` です。Windows では、ドライブ名を小文字にして `\` を `/` に置き換え、`C:\Users\me\out` を `Edit(//c/Users/me/out/**)` と書きます。`/` が1つだけだと、設定ファイルからの相対パスとして扱われます。
- **Coverity に書き込むツール（`coverity_update_triage`）は、事前に許可しないでください。** サーバー名だけの `mcp__plugin_coverity-triage-kit_coverity-connect` も、書き込むツールまで許可してしまうので使いません。書き込みは反映の実行時に、必ず確認が出ます。

### プラグインの場所が標準と違う場合

`Read(…)` のパスを、実際の場所に合わせて書き換えます。

| 場合 | `Read` の書き方 |
| --- | --- |
| `CLAUDE_CONFIG_DIR` で設定の場所を変えている | `Read(//<その場所>/plugins/cache/plugin-market-place/coverity-triage-kit/**)`（例：`/srv/claude` なら `Read(//srv/claude/plugins/cache/plugin-market-place/coverity-triage-kit/**)`） |
| `--plugin-dir` で読み込んでいる、またはマーケットプレイスをローカルのフォルダから追加した | `Read(//<プラグインのフォルダ>/**)`（Windows の `C:\work\kit` は `Read(//c/work/kit/**)`） |
| スキルを `.claude/skills/` にコピーして使っている | [統括エージェントの README](../../com.github.copilot/agents/triage-orchestrator.README.markdown) の「Claude Code で使う」を参照 |

## 注意

- プラグインのフォルダそのものを作業フォルダにして Claude Code を起動しないでください。プラグインの MCP サーバーの登録が二重に読み込まれ、動かない登録が1つ増えます。
- プラグインをインストールせずに、エージェントとスキルだけを `.claude/agents/` と `.claude/skills/` にコピーして使うこともできます。手順は各エージェントの README にあります。
- MCP サーバーは、初回の起動時に依存ライブラリをダウンロードします。先に済ませる場合は、プラグインのフォルダ（`~/.claude/plugins/cache/plugin-market-place/coverity-triage-kit/<バージョン>/`）で `uv sync --native-tls --frozen --directory mcp-servers/coverity-connect` を実行します。
