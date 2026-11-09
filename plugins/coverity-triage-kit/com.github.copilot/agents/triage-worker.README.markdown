# 作業エージェント（triage-worker）

Coverity の警告1件を、チャットでトリアージするエージェントです（2段階目）。
CID を伝えると、警告の情報を取り、本物のバグかを調べ、修正案と逸脱コメント案の両方を作って報告します。どちらを採るかは、あなたが決めます。

- 作業中のファイルは、差分を見せて、あなたが同意したときだけ直します。
- Coverity には書き込みません。

## 用意するもの

- GitHub Copilot CLI（Claude Code でも使えます。下の「Claude Code で使う」）
- 調べたいリポジトリ（Copilot CLI を、そのフォルダで起動します）
- CID だけで頼むときは、Coverity と通信する MCP サーバー（下の「Coverity と通信する」）

## 始め方

1. 調べたいリポジトリのフォルダで、Copilot CLI を起動します。
2. `/agent` で `triage-worker · plugin` を選びます（取り出して置いたときは `triage-worker · project`）。
3. チャットで頼みます。

`/clear` の後は、もう一度 `/agent` で選びます。

## 頼み方の例

```text
CID 12345 をトリアージして
```

```text
CID 20001（NULL_RETURNS、src/reader.c の 42 行、read_header）が本物のバグか調べて、修正案と逸脱コメント案を作って
```

```text
CID 20003 と 20004 は同じマクロの警告です。まとめて調べて
```

```text
CID 12345 を調べて、結果を決まった形のファイル（result.json）にも書いて。場所は ../triage/12345/
```

- CID だけを伝えたときは、Coverity と通信する MCP サーバーで、警告の情報（チェッカー、ファイルと行、警告経路）を取ります。取れないときは、足りないものを聞きます。警告の情報をチャットに貼ってもかまいません。
- 終わると、結論・判定・確信度・推奨（修正か逸脱か）・根拠（ファイルと行）・修正案の差分・逸脱コメント案を報告します。
- 修正を作業中のファイルに当てるかは、差分を見てから答えます。断ると、差分は案のまま残ります。
- リポジトリに `.coverity-triage/knowledge.md`（プロジェクトの知識）があれば、それも使います。

## Coverity と通信する

CID だけで頼むには、MCP サーバー `coverity-connect` を使います。

- プラグインを入れたときは、プラグインが登録します。認証情報は環境変数に入れてから Copilot CLI を起動します。
- 取り出して使うときは、MCP サーバーのフォルダを置き、その README のとおりに、`coverity-connect` という名前で登録します（作業エージェントの定義が、この名前でツールを使うため）。
- 接続先・認証情報・偽の Coverity での試し方は、MCP サーバーの README（[mcp-servers/coverity-connect/README.md](../../mcp-servers/coverity-connect/README.md)）にあります。

## 使う部品

使うスキルと MCP サーバーは、[作業エージェントの定義](triage-worker.agent.md#使う部品)の「使う部品」の表にあります。
各スキルの使い方は、スキルのフォルダの README（[skills/](../../skills/) の下）にあります。

## 取り出して使う

プラグインを入れずに、この作業エージェントと、その表の部品だけで使えます。

1. 定義 `triage-worker.agent.md` を、リポジトリの `.github/agents/` にコピーします（自分だけで使うときは `%USERPROFILE%\.copilot\agents\`、Linux では `~/.copilot/agents/`）。
2. 定義の「使う部品」の表にあるスキルのフォルダ（`skills/<名前>/`）を、リポジトリの `.github/skills/<名前>/` にコピーします（自分だけで使うときは `%USERPROFILE%\.copilot\skills\<名前>\`、Linux では `~/.copilot/skills/<名前>/`）。
3. MCP サーバーを使うときは、上の「Coverity と通信する」のとおりに登録します。
4. Copilot CLI を起動し直すか、`/skills reload` をします。`/skills list` に表のスキルが、`/agent` に `triage-worker` が出れば完了です。

コピーのコマンドの例（`<プラグイン>` はこのプラグインのフォルダ、`<リポジトリ>` は調べたいリポジトリ）：

```text
# Windows（PowerShell）
New-Item -ItemType Directory -Force "<リポジトリ>\.github\agents", "<リポジトリ>\.github\skills"
Copy-Item "<プラグイン>\com.github.copilot\agents\triage-worker.agent.md" "<リポジトリ>\.github\agents\"
foreach ($s in "checker-knowledge", "triage-investigation", "code-fix", "deviation-comment", "triage-report") { Copy-Item -Recurse -Force "<プラグイン>\skills\$s" "<リポジトリ>\.github\skills\" }
# Linux（bash）
mkdir -p "<リポジトリ>/.github/agents" "<リポジトリ>/.github/skills"
cp "<プラグイン>/com.github.copilot/agents/triage-worker.agent.md" "<リポジトリ>/.github/agents/"
for s in checker-knowledge triage-investigation code-fix deviation-comment triage-report; do cp -r "<プラグイン>/skills/$s" "<リポジトリ>/.github/skills/"; done
```

並べたスキルの名前は、定義の表のスキルと同じです。表が変わったら、表に合わせます。

## Claude Code で使う

Claude Code 用の定義は、プラグインの `agents/triage-worker.md` です。使う部品の表と決まりは、Copilot CLI 用の定義と同じです。

### 始め方

- プラグインを入れたとき：調べたいリポジトリのフォルダで、`claude --agent coverity-triage-kit:triage-worker` で起動して、上の「頼み方の例」のとおりに頼みます。作業エージェントが会話の主役なので、足りない情報を聞き、差分を見せて、あなたが同意したときだけ作業中のファイルを直します。
- 起動のときに `--agent` を付けずに、「triage-worker エージェントで CID 12345 をトリアージして」のように名前を挙げて頼む（`@` で選ぶ）こともできます。そのときは、会話の主役から呼ばれるエージェントとして動き、Claude Code は質問の道具を渡しません。足りない情報があるときは、聞くことを返事に書いて止まります（チャットで答えると、続きを頼めます）。同意も受け取れないので、修正の差分は案として返すだけで、作業中のファイルは直しません。
- MCP サーバーは、プラグインが `plugin:coverity-triage-kit:coverity-connect` として登録します（`/mcp` で見られます）。認証情報の環境変数は、入れてから Claude Code を起動します。

### 取り出して使う

1. 定義 `agents/triage-worker.md` を、リポジトリの `.claude/agents/` にコピーします（自分だけで使うときは `~/.claude/agents/`。Windows では `%USERPROFILE%\.claude\agents\`）。
2. 定義の「使う部品」の表にあるスキルのフォルダ（`skills/<名前>/`）を、リポジトリの `.claude/skills/<名前>/` にコピーします（自分だけで使うときは `~/.claude/skills/<名前>/`）。
3. MCP サーバーを使うときは、MCP サーバーの README の「Claude Code」のとおりに、`coverity-connect` という名前で登録します（定義は、この名前のツールも使えるように書いてあります）。
4. Claude Code を起動し直します。`/agents` に `triage-worker` が出れば完了です。取り出したときは、`claude --agent triage-worker` で起動できます。

コピーのコマンドの例（`<プラグイン>` はこのプラグインのフォルダ、`<リポジトリ>` は調べたいリポジトリ）：

```text
# Windows（PowerShell）
New-Item -ItemType Directory -Force "<リポジトリ>\.claude\agents", "<リポジトリ>\.claude\skills"
Copy-Item "<プラグイン>\agents\triage-worker.md" "<リポジトリ>\.claude\agents\"
foreach ($s in "checker-knowledge", "triage-investigation", "code-fix", "deviation-comment", "triage-report") { Copy-Item -Recurse -Force "<プラグイン>\skills\$s" "<リポジトリ>\.claude\skills\" }
# Linux（bash）
mkdir -p "<リポジトリ>/.claude/agents" "<リポジトリ>/.claude/skills"
cp "<プラグイン>/agents/triage-worker.md" "<リポジトリ>/.claude/agents/"
for s in checker-knowledge triage-investigation code-fix deviation-comment triage-report; do cp -r "<プラグイン>/skills/$s" "<リポジトリ>/.claude/skills/"; done
```

スクリプトを動かすスキルは、`uv` のコマンドで許可の確認が出ます。許可（`allowed-tools`）を持つスキルは、使う前にも確認が出ます。毎回の確認を省くときは、`/permissions` で `Bash(uv run *)` と、`Skill(triage-investigation)`・`Skill(deviation-comment)`・`Skill(triage-report)` を許可します（リポジトリの `.claude/skills/` に取り出したときは、参照資料を読む許可はいりません。`~/.claude/skills/` に置いたときは、`Read(~/.claude/skills/**)`（読むだけ）も許可します。プラグインとして入れたときは `Skill(coverity-triage-kit:*)` と `Read(~/.claude/plugins/cache/plugin-market-place/coverity-triage-kit/**)` です）。`/permissions` で足した許可は、設定ファイルに保存され、後のセッションにも残ります。自分だけの、このプロジェクトの設定（`.claude/settings.local.json`）に保存し、要らなくなったら `/permissions` で消します。

## うまくいかないとき

| 起きること | すること |
| --- | --- |
| `/agent` の一覧に `triage-worker` がない | プラグインが入っているかを確かめる。取り出したときは、`.github/agents/` に `triage-worker.agent.md` があるかを確かめる |
| Claude Code で `/agents` に `triage-worker` がない | プラグインが入っているかを `/plugin` で確かめる。取り出したときは、`.claude/agents/` に `triage-worker.md` があるかを確かめる |
| 警告の情報を取れないと言われる | MCP サーバーが `coverity-connect` の名前で登録されているかを `/mcp show` で確かめる。認証情報の環境変数は、入れてから Copilot CLI を起動し直す。警告の情報をチャットに貼っても続けられる |
| スキルを使えないと言われる | `/skills list` で、表のスキルが読み込まれているかを確かめる |
| 思っていたのと違うスキルが使われる | リポジトリの `.github/skills/` や `%USERPROFILE%\.copilot\skills\` に同じ名前のスキルがあると、そちらが使われる。`/skills list` で場所を確かめる |
| 作業中のファイルを直してほしくない | 「直さずに差分だけ見せて」と頼む |
| Claude Code で、差分の案だけが返り、同意を聞かれずファイルも直らない | 名前を挙げて頼んだ（`@` で選んだ）ときは、そう動きます。`claude --agent coverity-triage-kit:triage-worker` で起動し直して頼む |
| 判定に自信がないと言われる（確信度 低） | 根拠に書かれたファイルと行を見て、足りない事実（呼び出し元の決まりなど）をチャットで伝え、もう一度頼む |
