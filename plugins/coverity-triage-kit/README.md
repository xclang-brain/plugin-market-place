# coverity-triage-kit

Coverity Connect の警告を AI がトリアージし、警告ごとに「修正案」と「逸脱コメント案」の両方を用意するプラグインです。対象は C / C++（標準チェッカー、MISRA、CERT）です。GitHub Copilot CLI と Claude Code で使えます。

人が行うのは、できあがった一覧を見て「修正する」か「逸脱として扱う」かを選ぶことだけです。**人が承認して反映するまで、作業中のソースコードも Coverity も変更しません。**

> [coverity-warning-triage](../coverity-warning-triage/) の後継です。スキル名が重なるため、インストールするのはどちらか一方だけにしてください。

## できること

警告1件ごとに、次のものが得られます。

- **判定**：本物のバグ／誤検知／意図的／判定不能。確信度と、根拠にしたコードの位置が付きます。
- **修正案**：差分と、修正後のファイル。
- **逸脱コメント案**：Coverity に登録する200文字以内のコメント。
- **一覧とレポート**：`summary.html`（一覧）と、警告ごとの詳細レポート。

承認した結果は `/coverity-apply` で反映します。逸脱は Coverity に登録し、修正はブランチを push してプルリクエストにします（svn の場合は作業コピーに差分を適用します）。

## 使い方を選ぶ

任せたい範囲に合わせて、4つの段階から選べます。どの段階も、それより上の段階なしで単独で使えます。

| 段階 | 向いている場面 | 始め方（Copilot CLI） | Claude Code |
| --- | --- | --- | --- |
| 1段階目：スキル・MCP サーバーを単体で使う | 警告1件の調査や、逸脱コメントの作成だけを頼みたい | 「CID 12345 の警告が本物か調べて」 | ○ |
| 2段階目：作業エージェントに1件を任せる | 1件のトリアージを最後まで任せたい | `/agent` で `triage-worker` を選び「CID 12345 をトリアージして」 | ○ |
| 3段階目：一括トリアージ | 条件に合う警告をまとめて処理したい（夜間の無人実行など） | `/coverity-run High だけ` | ○ |
| 4段階目：Web管理画面から一括トリアージ | 画面で条件を選び、進み具合を見ながら実行したい | 「Coverity の警告をトリアージして」 | ×（Copilot CLI だけ） |

迷ったら、まず3段階目の `/coverity-run 10 件だけ` で試すのがおすすめです。

## はじめる（Copilot CLI、30分ほど）

必要なもの：GitHub Copilot CLI、[uv](https://docs.astral.sh/uv/)、git か svn、Coverity Connect のアカウントと認証キー。

1. **インストールする**

   ```shell
   copilot plugin marketplace add xclang-brain/plugin-market-place
   copilot plugin install coverity-triage-kit@plugin-market-place
   ```

2. **設定を作る**（リポジトリごとに最初の1回）

   調査したいリポジトリで Copilot CLI を起動し、`/coverity-setup` と入力します。Coverity の URL、プロジェクト名、ストリーム名、ユーザー名、認証キーを順に聞かれます。設定は `.coverity-triage/` に保存されます（コミットしてチームで共有できます）。認証情報は設定ファイルには書かず、環境変数（`COV_USER` / `COV_AUTH_KEY`）で渡します。

3. **試しに実行する**

   ```text
   /coverity-run 10 件だけ
   ```

4. **結果を確認して反映する**

   `/coverity-gui` で結果の画面を開き、警告ごとに「修正」「逸脱」などを選びます。選び終えたら `/coverity-apply` で反映します。

詳しい手順と、うまく動かないときの対処は [Copilot CLI ガイド](docs/guide/copilot-cli.md) にあります。Claude Code で使う場合は [Claude Code ガイド](docs/guide/claude-code.md) を見てください。

## コマンド一覧

| コマンド | 用途 |
| --- | --- |
| `/coverity-setup` | 設定と認証情報を用意する（最初の1回） |
| `/coverity-run` | 一括トリアージを実行する。「High だけ」「10 件だけ」「misra.yaml」などの条件を付けられる |
| `/coverity-gui` | 設定・進み具合・結果の画面を開く |
| `/coverity-apply` | 承認した結果を Coverity とリポジトリに反映する |
| `/coverity-help` | 使い方やエラーについて質問する |
| `/coverity-selftest` | インストールと接続が正しいかを診断する |

## 結果の保存場所

一括トリアージの結果は、リポジトリの外の出力先（既定ではリポジトリと同じ階層の `coverity-triage-out/<日時>/`）に保存されます。一覧は `summary.html`、警告ごとのレポートは `cid/<CID>.html`、修正案は `patches/` と `fixed/` にあります。トリアージ中に、リポジトリの中のファイルを書き換えることはありません。

## 文書

| 文書 | 読む人 | 内容 |
| --- | --- | --- |
| [Copilot CLI ガイド](docs/guide/copilot-cli.md) | Copilot CLI の利用者 | セットアップの詳細、事前の許可、トラブル対処 |
| [Claude Code ガイド](docs/guide/claude-code.md) | Claude Code の利用者 | インストール、起動方法、事前の許可 |
| [Web管理画面ガイド](docs/guide/4-screen.md) | 4段階目を使う人 | 画面の操作、Issue の起票 |
| [作業エージェント](com.github.copilot/agents/triage-worker.README.markdown)・[統括エージェント](com.github.copilot/agents/triage-orchestrator.README.markdown) | 2・3段階目を使う人 | 各エージェントの使い方 |
| 各スキルの README（[skills/](skills/)）・[MCP サーバー](mcp-servers/coverity-connect/README.md) | 1段階目を使う人 | 単体での使い方 |
| [変更履歴](CHANGELOG.md) | 利用者 | 版ごとの変更点 |
| [憲章](docs/constitution.md)・[仕様書](docs/specs/requirements.md)・[設計書](docs/design/design.md)・[コンポーネント一覧](docs/parts.md)・[テスト](tests/README.md) | 開発者 | 設計の方針と根拠、テストの実行方法 |

## ライセンス

MIT License です（[LICENSE](LICENSE)）。多くのスクリプトと資料は、coverity-warning-triage（MIT License）をもとにしています。
