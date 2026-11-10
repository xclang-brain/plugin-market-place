# Copilot CLI ガイド

GitHub Copilot CLI で coverity-triage-kit を使う人のための、セットアップと運用の手引きです。[README](../../README.md) の「はじめる」を補う内容で、必要な節だけ読めば足ります。Web管理画面の操作は [Web管理画面ガイド](4-screen.md) にあります。

## 必要なもの

| 必要なもの | 確認方法 |
| --- | --- |
| GitHub Copilot CLI | `copilot --version` |
| uv（Python と必要なライブラリを自動で用意するツール） | `uv --version` |
| git または svn | `git --version` / `svn --version` |
| Coverity Connect のアカウントと認証キー | ブラウザで Coverity にログインできること |
| `cov-build`・`cov-analyze`・`cov-commit-defects`（最初に解析も行う場合だけ） | `/coverity-selftest` の④ |

## セットアップ

### 1. インストールする

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install coverity-triage-kit@plugin-market-place
```

Copilot CLI の中からは、`/plugin marketplace add xclang-brain/plugin-market-place` と `/plugin install coverity-triage-kit@plugin-market-place` でもインストールできます。

### 2. MCP サーバーの依存ライブラリを先に取得する（任意）

Coverity と通信する MCP サーバーは、初回の起動時に依存ライブラリをダウンロードするため、起動に時間がかかります。次のコマンドを先に1回実行しておくと、すぐに起動します。

```shell
uv sync --native-tls --frozen --directory "<プラグインのフォルダ>/mcp-servers/coverity-connect"
```

`<プラグインのフォルダ>` は、GitHub からインストールした場合は `~/.copilot/installed-plugins/<マーケットプレイス名>/coverity-triage-kit` です。

Windows 以外では、Copilot CLI が認証情報の環境変数を MCP サーバーに渡せないことがあります。その場合は [MCP サーバーの README](../../mcp-servers/coverity-connect/README.md) の手順で登録し直してください。

### 3. 設定を作る

調査したいリポジトリで Copilot CLI を起動し、`/coverity-setup` と入力します（20分ほど）。

- 設定は、リポジトリの `.coverity-triage/` に保存されます。コミットすればチームで共有できます。
- 認証情報は環境変数 `COV_USER` / `COV_AUTH_KEY` で渡します。設定ファイルには書きません。認証キーはチャットに貼らず、ターミナルの入力欄（入力内容は表示されません）に入力します。
- 対象の警告は、条件ファイル（`.coverity-triage/filters/` の YAML）で絞り込みます。既定は「未分類の警告を20件まで」です。

### 4. Web管理画面を使う場合

Web管理画面は、Copilot CLI の試験的機能（エクステンション）で動きます。

- `copilot --experimental` で起動します（一度指定すれば設定に残ります）。起動後なら `/settings experimental on` でも有効にできます。
- 読み込み時に `Extension "plugin:coverity-triage-kit:coverity-triage-kit" wants elevated permissions` という確認が出ます。許可しないと読み込まれません。「Yes, and always allow … in this directory」を選ぶと、そのフォルダでは次回から確認が出ません。

## 一括トリアージの前に、ツールを許可しておく

一括トリアージは、開始したら終了まで質問をしません。ただし、出力先がリポジトリの外にあるため、何もしないと書き込みのたびに Copilot CLI の許可確認で止まります。夜間に無人で実行する場合は、開始前に次のどちらかを行ってください。

**方法 A：チャットで許可する**

`/add-dir "<出力先>"` と入力し、最初の確認で「このセッションでは許可」を選びます。

**方法 B：許可を指定して起動する**

```text
copilot --add-dir "<出力先>" --allow-tool 'shell(uv:*)' --allow-tool write --allow-tool 'coverity-connect(coverity_check_connection)' --allow-tool 'coverity-connect(coverity_search_issues)' --allow-tool 'coverity-connect(coverity_get_issue_details)' --allow-tool 'coverity-connect(coverity_preview_triage)'
```

| 指定 | 許可する内容 |
| --- | --- |
| `--add-dir "<出力先>"` | 出力先の読み書き |
| `--allow-tool 'shell(uv:*)'` | `uv` コマンド（スキルのスクリプトの実行） |
| `--allow-tool write` | ファイルの書き込み |
| `--allow-tool 'coverity-connect(…)'` | Coverity から情報を読み取る4つのツール |

`<出力先>` の既定は、リポジトリと同じ階層の `coverity-triage-out` です。`/coverity-run` も、開始前に実際のパスを入れたコマンドを表示します。

> **Coverity に書き込むツール（`coverity_update_triage`）は、事前に許可しないでください。** サーバー名だけの `--allow-tool 'coverity-connect'` も、書き込むツールまで許可してしまうので使いません。書き込みは `/coverity-apply` の実行時に、必ず確認が出ます。`--allow-all-tools` のような全許可の指定は、隔離された環境以外では使わないでください。

## 実行中の注意

- 実行中に `/clear` を使わないでください。実行が途中で止まります。
- 許可の確認、AI クレジットの上限、エージェントからの質問には、チャットで答えます。
- AI は作業中のファイルを変更しません。修正案は、リポジトリの外に作った作業用コピーの上で作成します。

## うまく動かないとき

| 症状 | 原因と対処 |
| --- | --- |
| 「Coverity の警告をトリアージして」と頼んでも、Web管理画面の URL が表示されない | エクステンションが読み込まれていません。①試験的機能が無効：`copilot --experimental` で起動し直す。②読み込み時の確認を拒否した：Copilot CLI を再起動して許可する。③`/extensions mode` が Disabled：Load & Augment に戻す |
| 意図しないスキルが使われる | 同名のスキルが他にも入っていないかを `/skills list` で確認する（coverity-warning-triage と併用していないか） |
| Coverity に接続できない、設定のエラーが出る | `/coverity-help` に症状を伝える。`/coverity-selftest` で診断できる |

## 改善の Issue を起票する

Web管理画面の終了後の画面には、実行中に見つかった課題と、このプラグインへの改善 Issue の案が表示されます。案ごとの承認ボタンを押したものだけが、GitHub（xclang-brain/plugin-market-place）に起票されます。起票には `gh` コマンドのインストールと `gh auth login` が必要です。
