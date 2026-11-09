# coverity-triage-kit

Coverity Connect の警告をトリアージし、警告ごとに修正案と逸脱コメント案の両方を作るプラグインです（C / C++、標準・MISRA・CERT）。GitHub Copilot CLI と Claude Code の両方で使えます。
大がかりな一括の処理も、スキル1つだけの小さな使い方もできるように、部品を4つの段階で取り出して使えます。

- GitHub Copilot CLI では、4つの段階をすべて使えます。
- Claude Code では、1〜3段階目を使えます。4段階目（Web管理画面）は Copilot CLI だけです。Claude Code での入れ方と使い方は「[Claude Code で使う](#claude-code-で使う)」にあります。

元のプラグイン [coverity-warning-triage](../coverity-warning-triage/) を作り直したもので、Web管理画面を使うときの使い方は同じです。スキルの名前が同じなので、**coverity-warning-triage とは、どちらか一方だけを入れてください。**

## できること

Coverity Connect の警告を渡すと、警告ごとに次のものができます。人は一覧を見て、修正か逸脱かを決めるだけにします。

- 本物のバグかどうかの見立て（本物のバグ・誤検知・意図的・判定できない）と、その確信度と、根拠のコードの場所
- 修正案（差分と、修正後のファイル）と、逸脱コメント案（200文字以内）の両方。どちらを採るかは人が決めます
- 一覧 `summary.html`、警告ごとの詳細レポート、承認の案 `approvals.json`（一括のときは、リポジトリの外の実行フォルダにまとまります）

承認したものは、`/coverity-apply` で反映します。逸脱は Coverity に書き戻し、修正はブランチを push してプルリクエストにします（svn なら作業コピーに差分を当てます）。人が承認して反映するまで、作業中のファイルも Coverity も変えません。

## 使い方

### 4つの段階

AI に任せる範囲に合わせて、使う部品を選びます。どの段階も、上の段階なしで使えます。

| 段階 | 使う部品 | 使えるツール | 頼み方の例（Copilot CLI） | 見る文書 |
| --- | --- | --- | --- | --- |
| 1段階目 | スキル1つ、または MCP サーバー1つ | Copilot CLI・Claude Code | 「CID 12345（NULL_RETURNS、src/foo.c の 120 行）の警告が本物か調べて」「この警告の逸脱コメントを書いて」「ストリーム app-main の、Impact が High の警告を一覧にして」 | 各スキルの README（[skills/](skills/) の下）、[MCP サーバーの README](mcp-servers/coverity-connect/README.md) |
| 2段階目 | 作業エージェント（`/agent` で `triage-worker`） | Copilot CLI・Claude Code | 「CID 12345 をトリアージして」 | [作業エージェントの README](com.github.copilot/agents/triage-worker.README.markdown) |
| 3段階目 | 統括・作業・検証・統合のエージェント | Copilot CLI・Claude Code | `/coverity-run High だけ`、または `/agent` で `triage-orchestrator` を選んで「Coverity の警告をトリアージして」 | [統括エージェントの README](com.github.copilot/agents/triage-orchestrator.README.markdown) |
| 4段階目 | 3段階目と Web管理画面 | Copilot CLI だけ | 「Coverity の警告をトリアージして」 | [4段階目のガイド](docs/guide/4-screen.md) |

Claude Code での頼み方は、「[Claude Code で使う](#claude-code-で使う)」の表にあります。

### チャットで頼むときの言葉

ここは Copilot CLI での言葉です（Claude Code では、Web管理画面のほかは同じに使えます）。

- 「Coverity の警告をトリアージして」：Web管理画面の URL が示されます（4段階目）。画面で条件を選び、開始を押します。
- `/coverity-run`：チャットで一括のトリアージをします（3段階目）。条件を添えられます（例「misra.yaml」「High だけ」「10 件だけ」「最初に解析して」「解析はしないで」）。
- `/coverity-setup`：設定と認証情報を用意します（最初に1回）。
- `/coverity-gui`：設定・監視・結果（承認の選択と逸脱コメントの手直し）の画面を開きます。
- `/coverity-apply`：承認を確かめて反映します。件数と ID を見せて、同意を得てから反映します。
- `/coverity-help`：困ったときに聞きます。
- `/coverity-selftest`：道具・偽データでの一連の流れ・社内の Coverity（読むだけ）・ビルドを確かめます。

プラグインから入れたスキルは、`/coverity-triage-kit:coverity-run` のように、プラグインの名前を付けても呼べます。

### 用意するもの

| もの | 確かめ方 |
| --- | --- |
| GitHub Copilot CLI（Claude Code で使うときは Claude Code） | `copilot --version`（`claude --version`） |
| uv（Python の道具。Python と要るライブラリを用意します） | `uv --version` |
| git か svn（いつも使っているもの） | `git --version` / `svn --version` |
| 社内の Coverity Connect のアカウントと認証キー | ブラウザで Coverity に入れる |
| （初回解析を使うときだけ）`cov-build`・`cov-analyze`・`cov-commit-defects` | `/coverity-selftest` の④ |

- 設定は、調べたいリポジトリの `.coverity-triage/` に置き、コミットしてチームで共有します（`/coverity-setup` が作ります）。
- 認証情報は、環境変数 `COV_USER` / `COV_AUTH_KEY` に置きます。設定ファイルには書きません。認証キーは、チャットではなく、ターミナルの伏せ字の欄で入れます。
- 警告は、条件ファイル（`.coverity-triage/filters/` の YAML）で絞ります。既定は「分類がまだの警告を 20 件まで」です。

### できたものの場所

- 一括のときは、結果の置き場所 `<output_dir>` の下の、実行フォルダ `<output_dir>/<日時>/` にまとまります。結果の置き場所は、リポジトリの外です（既定は、リポジトリと同じ階層の `coverity-triage-out`）。
- 一覧 `summary.html`、詳細 `cid/<ID>.html`、差分 `patches/<ID>-fix.patch`、修正後のファイル `fixed/<ID>/fix/`、承認の案 `approvals.json` ができます。修正後のファイルと差分は、警告ごとに独立しています。
- スキルやエージェントに1件だけ頼んだときは、結果はチャットに返ります（頼んだときだけファイルに書きます）。

## 準備

ここからの「準備」「任せたいツールを前もって許可する」「実行中に気をつけること」「うまく始まらないとき」は、Copilot CLI での手順です。Claude Code では「[Claude Code で使う](#claude-code-で使う)」を見ます。

### 1. インストールする

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install coverity-triage-kit@plugin-market-place
```

Copilot CLI の中では、`/plugin marketplace add xclang-brain/plugin-market-place` と `/plugin install coverity-triage-kit@plugin-market-place` でも入れられます。

### 2. Coverity と通信する部品の用意を済ませておく

初めて起動するときに要るものを取りに行くので、時間がかかります。先に1回、次を動かしておくと、すぐに起動します。`<プラグインのフォルダ>` は、このプラグインが入っているフォルダです。GitHub から入れたときは、Copilot CLI のプラグインの置き場所（公式のドキュメントでは `~/.copilot/installed-plugins/<マーケットプレイスの名前>/coverity-triage-kit`）、手元のフォルダのマーケットプレイスから入れたときは、そのフォルダの中の coverity-triage-kit のフォルダです。

```shell
uv sync --native-tls --frozen --directory "<プラグインのフォルダ>/mcp-servers/coverity-connect"
```

Windows のほかでは、Copilot CLI が MCP サーバーに認証情報の環境変数を渡せないことがあります。そのときは、[MCP サーバーの README](mcp-servers/coverity-connect/README.md) のとおりに登録し直します。

### 3. 設定を用意する

調べたいリポジトリを開いた Copilot CLI で、`/coverity-setup` と入力します（20 分ほど）。Coverity の URL・プロジェクト名・ストリーム名（チームで最初に使う人だけ）と、ユーザ名と認証キーを聞かれます。

### 4. Web管理画面を使うとき（4段階目）

Web管理画面を動かすエクステンションは、Copilot CLI の試験的な機能です。

- 初めて使うときは `copilot --experimental` で起動します（一度付けると設定に残ります）。起動した後なら、チャットで `/settings experimental on` と打ちます（自動で再起動します）。
- 読み込むときに `Extension "plugin:coverity-triage-kit:coverity-triage-kit" wants elevated permissions` という確認が出ます。許可しないと読み込まれません。「Yes, and always allow … in this directory（または in this repo）」を選ぶと、そのフォルダでは次から出ません。選び方のくわしい説明は [4段階目のガイド](docs/guide/4-screen.md) にあります。

## 任せたいツールを前もって許可する

一括のトリアージは、始めたら終わるまで質問しません。途中で許可の確認で止まらないよう、始める前に、実行フォルダとコマンドの許可を、このセッションのあいだだけ与えます。

- チャットで `/add-dir "<output_dir>"` を打ち、最初の確認で「このセッションでは許可」を選びます。
- または、Copilot CLI を次のように起動し直します。

| 指定 | 許可するもの |
| --- | --- |
| `copilot --add-dir "<output_dir>"` | 実行フォルダの読み書き |
| `copilot --allow-tool 'shell(uv:*)'` | `uv` のコマンド（スキルのスクリプト） |
| `copilot --allow-tool write` | ファイルの書き込み |
| `copilot --allow-tool 'coverity-connect(coverity_check_connection)'`（`coverity_search_issues`・`coverity_get_issue_details`・`coverity_preview_triage` も同じ形で並べる） | Coverity から読むツール |

複数は、`--allow-tool` を並べて書きます。Coverity に書き戻すツール（`coverity_update_triage`）は許可しません（サーバーの名前だけの `--allow-tool 'coverity-connect'` は、書き戻すツールまで許可するので使いません）。反映（`/coverity-apply`）のときに確認が出ます。すべてを任せる指定（`--allow-all-tools` など）もありますが、公式のドキュメントは隔離した環境でだけ使うよう勧めています。

## 実行中に気をつけること

- 実行中は `/clear` を使わないでください。Web管理画面の更新が止まり、実行が途中で切れます。
- 実行中の確認（許可、AI クレジットの上限、エージェントからの質問）には、チャットで答えます。Web管理画面には、チャットで確認を待っていることが示されます。
- AI は、作業中のファイルを変えません。修正案は、リポジトリの外の作業用のコピーで作ります。

## うまく始まらないとき

「Coverity の警告をトリアージして」と頼んでも Web管理画面の URL が示されないときは、エクステンションが読み込まれていません。次のどれかです。

- 試験的機能を有効にしていない：`copilot --experimental` で起動し直すか、チャットで `/settings experimental on` と打ちます。
- 読み込むときの確認を断った：Copilot CLI を起動し直して、出た確認を許可します。
- エクステンションを使わない設定（`/extensions mode` の Disabled）にしている：`/extensions mode` で Load & Augment か Load Only に戻します。

ほかに困ったときは、`/coverity-help` で聞けます。思っていたのと違うスキルが使われるときは、`/skills list` で、同じ名前のスキルがほかにないかを確かめます（元のプラグインと一緒に入れていないか）。

## Issue の起票

Web管理画面の終わった後の画面に、課題の一覧と、このプラグインを改善する Issue の案が並びます。案ごとの承認ボタンを押すと、その案だけが GitHub のリポジトリ（xclang-brain/plugin-market-place）に Issue として起票されます。承認していない案は起票されません。

起票するには、`gh`（GitHub の公式のコマンド）を入れて、`gh auth login` でログインしておきます。

## Claude Code で使う

同じプラグインを Claude Code にも入れられます。1〜3段階目（スキル、MCP サーバー、作業エージェント、一括のトリアージ）が使えます。4段階目の Web管理画面は Copilot CLI だけです。

### 1. インストールする

```shell
claude plugin marketplace add xclang-brain/plugin-market-place
claude plugin install coverity-triage-kit@plugin-market-place
```

Claude Code の中では、`/plugin marketplace add xclang-brain/plugin-market-place` と `/plugin install coverity-triage-kit@plugin-market-place` でも入れられます。入れた後は、Claude Code を起動し直します。

- Coverity と通信する部品（MCP サーバー）は、プラグインが登録します。`/mcp` で `plugin:coverity-triage-kit:coverity-connect` が出れば、読み込まれています。
- 初めて起動するときに要るものを取りに行くので、時間がかかります。先に済ませるときは、「準備」の2のコマンドを、Claude Code のプラグインの置き場所（`~/.claude/plugins/` の下の coverity-triage-kit のフォルダ）で動かします。
- 設定は、Copilot CLI と同じく、調べたいリポジトリの `.coverity-triage/` に置きます（`/coverity-setup` で作ります）。

### 2. 認証情報を環境変数に入れる

認証情報は、環境変数 `COV_USER` / `COV_AUTH_KEY` に入れてから、Claude Code を起動します（プロキシを使うときは `HTTPS_PROXY` / `NO_PROXY` も）。プラグインが、この4つを MCP サーバーに渡します。入れていない変数は、空として渡ります。設定ファイルとチャットには書きません。

### 3. 段階ごとの呼び方

調べたいリポジトリのフォルダで、Claude Code を起動します。

| 段階 | 呼び方 |
| --- | --- |
| 1段階目 | `/coverity-triage-kit:<スキル>`（ほかに同じ名前のスキルがなければ `/<スキル>` でも）。頼みの文に合うスキルを、AI が選ぶこともあります。MCP サーバーのツールは、チャットで頼むと使われます |
| 2段階目 | `claude --agent coverity-triage-kit:triage-worker` で起動して、「CID 12345 をトリアージして」と頼みます。差分を見せて、同意したときに作業中のファイルを直す流れは、この起動のしかたのときだけです。「triage-worker エージェントで CID 12345 をトリアージして」のように名前を挙げて頼む（`@` で選ぶ）こともできますが、そのときは、修正の差分を案として返すだけで、ファイルは直しません |
| 3段階目 | `claude --agent coverity-triage-kit:triage-orchestrator` で起動して、「High だけトリアージして」のように頼みます。または `/coverity-triage-kit:coverity-run High だけ` と入力します（会話の主役が一括実行のスキルに従い、プラグインの作業・検証・統合のエージェントを呼びます）。反映は、下の4の終わりのとおり、`--agent` を付けずに起動した Claude Code で行います |
| 4段階目 | 使えません（Copilot CLI だけ）。「Coverity の警告をトリアージして」と頼んでも、画面は開きません |

- 名前を挙げて頼んだ（`@` で選んだ）作業エージェントは、会話の主役から呼ばれるエージェントとして動きます。Claude Code は、そのエージェントに質問の道具を渡しません。足りない情報があるときは、聞くことを返事に書いて止まります。チャットで答えると、続きを頼めます。作業中のファイルを直す同意も受け取れないので、修正は差分の案のまま返します。
- `claude --agent coverity-triage-kit:triage-worker` で起動したときは、作業エージェントが会話の主役なので、質問し、差分を見せて、同意したときだけファイルを直します。
- 統括エージェントが呼んだエージェントは、裏で動くことがあります。終わると、結果が知らせとして届きます。

### 4. 前もって許可する

一括のトリアージは、始めたら終わるまで質問しません。統括エージェントが呼んだエージェントは裏で動くことがあり、そのときは許可の確認を出せないので、前もって許可していないツールは使えずに止まります。始める前に、次のどちらかをします。夜通し動かすときは、確実な「起動のときに指定する」を使います。

**起動のときに指定する**：Claude Code を次のように起動します。

```shell
claude --add-dir "<output_dir>" --allowedTools "Bash(uv run *)" "Skill(coverity-triage-kit:*)" "Read(~/.claude/plugins/**)" "Write" "Edit" "mcp__plugin_coverity-triage-kit_coverity-connect__coverity_check_connection" "mcp__plugin_coverity-triage-kit_coverity-connect__coverity_search_issues" "mcp__plugin_coverity-triage-kit_coverity-connect__coverity_get_issue_details" "mcp__plugin_coverity-triage-kit_coverity-connect__coverity_preview_triage"
```

3段階目を `--agent` で始めるときは、この後ろに `--agent coverity-triage-kit:triage-orchestrator` を足します。

**起動した後のチャットで指定する**：`/add-dir "<output_dir>"` を打ち、出た確認でこのセッションのあいだの許可を選びます。`/add-dir` は、フォルダを使えるようにするだけで、ツールを許可しません。続けて `/permissions` で、次の9つを許可します（起動のときの指定と同じものです）。

```text
Bash(uv run *)
Skill(coverity-triage-kit:*)
Read(~/.claude/plugins/**)
Write
Edit
mcp__plugin_coverity-triage-kit_coverity-connect__coverity_check_connection
mcp__plugin_coverity-triage-kit_coverity-connect__coverity_search_issues
mcp__plugin_coverity-triage-kit_coverity-connect__coverity_get_issue_details
mcp__plugin_coverity-triage-kit_coverity-connect__coverity_preview_triage
```

- `<output_dir>` は実行フォルダの置き場所です（既定は、リポジトリの隣の `coverity-triage-out`）。
- `Bash(uv run *)` を許可するのは、プラグインのスキルが Copilot CLI の書き方で許可を持っているためです。Claude Code では、前もって許可しないと毎回確認が出ます。
- `Skill(coverity-triage-kit:*)` は、このプラグインのスキルを使うことの許可です。スキルが許可（`allowed-tools`）を持っているので、Claude Code は、スキルを使う前に毎回確認します。
- `Read(~/.claude/plugins/**)` は、入れたプラグインのフォルダを読むことの許可です。スキルの参照資料はこのフォルダにあり、調べているリポジトリの外なので、許可しないと読むたびに確認が出ます。読むだけで、書くことは許可しません。`CLAUDE_CONFIG_DIR` で設定の場所を変えているときは `~/.claude` をその場所に、`--plugin-dir` で入れたときは `Read(//<プラグインのフォルダ>/**)`（絶対パスの前に `/` を1つ足す形）にします。一括実行が示す許可の案内には、実際のフォルダの名前が入っています。
- `Write` と `Edit` の許可は、実行フォルダだけでなく、このセッションのすべてのファイル（調べているリポジトリも）に効きます。書く場所を実行フォルダに限っているのは、エージェントの決まり（実行フォルダの中の、作業用のコピーと結果のファイルにだけ書く）です。許可の指定で場所を絞ることはしていません。
- Coverity に書き戻すツール（`coverity_update_triage`）は許可しません。サーバーの名前だけの `mcp__plugin_coverity-triage-kit_coverity-connect` も、書き戻すツールまで許可するので使いません。
- 反映（`/coverity-triage-kit:coverity-apply`）は、`--agent` を付けずに起動した Claude Code で行います（統括エージェントの会話には、ファイルを書く道具と、反映の確かめ・書き戻しのツールがありません）。書き戻すツールの確認は、そのときに出ます。

### 5. 気をつけること

- プラグインのフォルダそのものを作業フォルダにして、Claude Code（や Copilot CLI）を起動しないでください。プラグインの MCP サーバーの登録が、そのフォルダの設定としても読まれ、動かない登録が1つ増えます。調べたいリポジトリのフォルダで起動します。
- プラグインを入れずに、部品だけを `.claude/agents/` と `.claude/skills/` に取り出して使うこともできます。手順は、各エージェントの README の「Claude Code で使う」にあります。

## 試験

構造・部品・取り決め・独立性・模擬の Coverity との結合・文書・画面の試験は、プラグインのフォルダで1つのコマンドで動かせます。モデルを使わないので、AI クレジットを使いません。

```shell
# Windows（PowerShell）
py -3 tests/run.py
# Linux（bash）
python3 tests/run.py
```

実際のモデルで課題を解かせて採点する試験（EVAL）と、その結果は [tests/README.md](tests/README.md) と `tests/eval/REPORT.md` にあります。

## 開発者向けの文書

[憲章](docs/constitution.md)・[仕様書](docs/specs/requirements.md)・[設計書](docs/design/design.md)・[部品の変え方](docs/replace.md)・[変更履歴](CHANGELOG.md)

## ライセンス

MIT License です（[LICENSE](LICENSE)）。Coverity と通信する部品、スクリプト、手順と資料の多くは、元のプラグイン coverity-warning-triage（MIT License）をもとに作り直しています。
