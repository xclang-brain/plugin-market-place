# Copilot CLI ガイド（運用のリファレンス）

GitHub Copilot CLI で coverity-triage-kit を運用するときに必要になる情報をまとめています。使い方は、[チュートリアル](1-skill.md) を順に進めると分かります。この文書は、必要になった節だけ読んでください。

| 知りたいこと | 節 |
| --- | --- |
| 無人で実行したい | [一括トリアージの前に、ツールを許可しておく](#一括トリアージの前にツールを許可しておく) |
| うまく動かない | [うまく動かないとき](#うまく動かないとき) |
| 必要なソフトウェアを確認したい | [必要なもの](#必要なもの) |

## 必要なもの

| 必要なもの | 確認方法 |
| --- | --- |
| GitHub Copilot CLI | `copilot --version` |
| uv（Python と必要なライブラリを自動で用意するツール） | `uv --version` |
| git または svn | `git --version` / `svn --version` |
| Coverity Connect のアカウントと認証キー | ブラウザで Coverity にログインできること |
| `cov-build`・`cov-analyze`・`cov-commit-defects`（最初に解析も行う場合だけ） | `/coverity-selftest` の④ |

## MCP サーバーの初回起動を速くする（任意）

Coverity と通信する MCP サーバーは、初回の起動時に依存ライブラリをダウンロードするため、起動に時間がかかります。次のコマンドを先に1回実行しておくと、すぐに起動します。

```shell
uv sync --native-tls --frozen --directory "<プラグインのフォルダ>/mcp-servers/coverity-connect"
```

`<プラグインのフォルダ>` は、GitHub からインストールした場合は `~/.copilot/installed-plugins/<マーケットプレイス名>/coverity-triage-kit` です。

Windows 以外では、Copilot CLI が認証情報の環境変数を MCP サーバーに渡せないことがあります。その場合は [MCP サーバーの README](../../mcp-servers/coverity-connect/README.md) の手順で登録し直してください。

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
