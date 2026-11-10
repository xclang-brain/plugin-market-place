# coverity-triage-kit

Coverity Connect の警告を AI がトリアージし、警告ごとに「修正案」と「逸脱コメント案」の両方を用意するプラグインです。GitHub Copilot CLI 用で、対象は C / C++（標準チェッカー、MISRA、CERT）です。

あなたが行うのは、AI の結果を見て、警告ごとに「修正する」か「逸脱として扱う」かを選ぶことです。選んだ結果は、コマンド1つで Coverity とリポジトリに反映できます。

## できること

警告1件ごとに、次のものが得られます。

| 得られるもの | 内容 |
| --- | --- |
| 判定 | 本物のバグ／誤検知／意図的／判定不能。確信度と、根拠にしたコードの位置が付きます |
| 修正案 | 差分と、修正後のファイル |
| 逸脱コメント案 | Coverity に登録する200文字以内のコメント |
| 一覧とレポート | 警告の一覧と、警告ごとの詳細レポート（HTML） |

例：`CID 20002（FORWARD_NULL、src/reader.c の 15 行）の警告が本物か調べて` と頼むと、次のように返ってきます。

```text
結論：誤検知です。

判定：誤検知　確信度：高　推奨：逸脱

根拠：
- src/reader.c:13 — open_and_read() は static 関数で、呼び出し元は read_config() だけ
- src/reader.c:32〜35 — read_config() は、path が NULL なら return -1 する
```

## 使い始める

**チュートリアルを、ステップ1から順に進めてください。** 各ステップは、実際に操作しながら読む構成です。前のステップで覚えたことに、1つずつ機能を足していきます。途中のステップで止めても、そこまでの機能はそのまま使えます。

| ステップ | やること | 任せる範囲 | 時間 |
| --- | --- | --- | --- |
| [1. 警告を1件、AI に調べてもらう](docs/guide/1-skill.md) | インストールして、警告1件の情報を伝えて調査を頼む | 頼んだことだけ（1段階目） | 15分 |
| [2. Coverity に接続して、1件を任せる](docs/guide/2-agent.md) | 対話で接続を設定し、CID を伝えてトリアージを任せる | 1件の調査から修正案・逸脱コメント案まで（2段階目） | 30分 |
| [3. まとめてトリアージして、反映する](docs/guide/3-batch.md) | 条件に合う警告を一括で処理し、画面で確認して反映する | 複数件の一括処理（3段階目） | 30分〜 |
| [4. 画面で進み具合と結果を確認する](docs/guide/4-screen.md) | 画面を開き、進み具合を見て、警告ごとに修正か逸脱かを選ぶ | 3の確認と選択を、画面で行う（4段階目） | 10分 |

## 必要なもの

| 必要なもの | 確認方法 |
| --- | --- |
| GitHub Copilot CLI | `copilot --version` |
| uv（Python と必要なライブラリを自動で用意するツール） | `uv --version` |
| git または svn | `git --version` / `svn --version` |
| Coverity Connect のアカウントと認証キー | ブラウザーで Coverity にログインできること |
| `cov-build`・`cov-analyze`・`cov-commit-defects`（最初に解析も行う場合だけ） | `/coverity-triage-kit:coverity-setup`（最初の解析を使う設定のとき、コマンドが PATH にあるかを確認します） |

## 日常の使い方（チュートリアルを終えた人向け）

```text
/coverity-triage-kit:coverity-run High だけ 20 件     ← AI がトリアージする
/coverity-triage-kit:coverity-gui                     ← 結果を確認して、警告ごとに選ぶ
/coverity-triage-kit:coverity-apply                   ← 選んだ結果を反映する
```

| コマンド | 用途 |
| --- | --- |
| `/coverity-triage-kit:coverity-setup` | 接続と設定を対話で用意する（最初の1回。設定の変更にも使う） |
| `/coverity-triage-kit:coverity-run` | 一括トリアージを実行する |
| `/coverity-triage-kit:coverity-gui` | 設定・進み具合・結果の画面を開く |
| `/coverity-triage-kit:coverity-apply` | 承認した結果を Coverity とリポジトリに反映する |

## 文書

| 目的 | 文書 |
| --- | --- |
| 使い方を覚える | チュートリアル：[ステップ1](docs/guide/1-skill.md) → [2](docs/guide/2-agent.md) → [3](docs/guide/3-batch.md) → [4](docs/guide/4-screen.md) |
| 対象の警告を絞り込む、設定を変える | [条件の指定](docs/guide/conditions.md) |
| 無人で実行する | [ステップ3の「無人で実行する」](docs/guide/3-batch.md#無人で実行する) |
| 個々の機能を詳しく知る | 各スキルのフォルダ（[skills/](skills/)）・[MCP サーバー](mcp-servers/coverity-connect/README.md) |
| 変更点を確認する | [変更履歴](CHANGELOG.md) |
| 開発に参加する | [憲章](docs/constitution.md)・[仕様書](docs/specs/requirements.md)・[設計書](docs/design/design.md)・[テスト](tests/README.md) |

## 補足

- AI が行うのは提案までです。作業中のソースコードと Coverity は、あなたが承認して反映するまで変更されません。
- どの段階も、上の段階を使わずに単独で使えます。
- MIT License です（[LICENSE](LICENSE)）。
