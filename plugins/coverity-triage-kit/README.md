# coverity-triage-kit

Coverity Connect の警告をトリアージし、警告ごとに修正案と逸脱コメント案の両方を作る GitHub Copilot CLI のプラグインです（C / C++、標準・MISRA・CERT）。
大がかりな一括の処理も、スキル1つだけの小さな使い方もできるように、部品を4つの段階で取り出して使えます。

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

| 段階 | 使う部品 | 頼み方の例 | 見る文書 |
| --- | --- | --- | --- |
| 1段階目 | スキル1つ、または MCP サーバー1つ | 「CID 12345（NULL_RETURNS、src/foo.c の 120 行）の警告が本物か調べて」「この警告の逸脱コメントを書いて」「ストリーム app-main の、Impact が High の警告を一覧にして」 | 各スキルの README（[skills/](skills/) の下）、[MCP サーバーの README](mcp-servers/coverity-connect/README.md) |
| 2段階目 | 作業エージェント（`/agent` で `triage-worker`） | 「CID 12345 をトリアージして」 | [作業エージェントの README](com.github.copilot/agents/triage-worker.README.markdown) |
| 3段階目 | 統括・作業・検証・統合のエージェント | `/coverity-run High だけ`、または `/agent` で `triage-orchestrator` を選んで「Coverity の警告をトリアージして」 | [統括エージェントの README](com.github.copilot/agents/triage-orchestrator.README.markdown) |
| 4段階目 | 3段階目と Web管理画面 | 「Coverity の警告をトリアージして」 | [4段階目のガイド](docs/guide/4-screen.md) |

### チャットで頼むときの言葉

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
| GitHub Copilot CLI | `copilot --version` |
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

### 1. インストールする

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install coverity-triage-kit@plugin-market-place
```

Copilot CLI の中では、`/plugin marketplace add xclang-brain/plugin-market-place` と `/plugin install coverity-triage-kit@plugin-market-place` でも入れられます。

### 2. Coverity と通信する部品の用意を済ませておく

初めて起動するときに要るものを取りに行くので、時間がかかります。先に1回、次を動かしておくと、すぐに起動します（`<プラグインのフォルダ>` は、このプラグインが入っているフォルダ）。

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
| `copilot --allow-tool 'coverity-connect'` | Coverity と通信するツール |

複数は、`--allow-tool` を並べて書きます。すべてを任せる指定（`--allow-all-tools` など）もありますが、公式のドキュメントは隔離した環境でだけ使うよう勧めています。

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
