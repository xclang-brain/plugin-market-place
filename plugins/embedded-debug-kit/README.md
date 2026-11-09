# embedded-debug-kit

組み込み機器で動く C/C++ プログラムの不具合を、AI が実機で試しながら直すプラグインです。
ソースを直し、クロスビルドし、機器に送り、機器で動かしてログを取ることを繰り返します。
対象は、FTP サーバーと、シリアルでつながるシェルを持つ機器です。

## 4つの段階

AI に任せる範囲に合わせて、4つの段階で使えます。どの段階も、上の段階なしで使えます。

| 段階 | 部品 | できること | 使い方 |
| --- | --- | --- | --- |
| 1 | スキル | 1つの作業だけを頼む（ビルド、機器への転送、機器での実行とログの取得、設定） | [cross-build](docs/guide/1-cross-build.md)、[ftp-deploy](docs/guide/1-ftp-deploy.md)、[serial-console](docs/guide/1-serial-console.md)、[skill-config](docs/guide/1-skill-config.md) |
| 2 | 作業エージェント | 不具合の調査と修正を任せる | [device-debugger](docs/guide/2-device-debugger.md) |
| 3 | 統括エージェントと検証エージェント | 作業と合否の判定を別のエージェントに分け、合格するまで試行を繰り返す | [debug-orchestrator](docs/guide/3-debug-orchestrator.md) |
| 4 | Web管理画面 | 3段階目の依頼文を作り、進み具合と結果をブラウザーで見る | [debug-dashboard](docs/guide/4-debug-dashboard.md) |

## 要るもの

- Claude Code か GitHub Copilot CLI（1.0.107 以降）
- Python 3.8 以上（Windows は `py -3`、ほかは `python3` で動くもの）
- 機器向けのクロスコンパイラーと、ビルドの方法（make など）
- 機器の FTP サーバーと、シリアルのシェル

利用者の PC は Windows を想定しています。Linux でも動きます。

## 入れ方

Claude Code：

```shell
claude plugin marketplace add xclang-brain/plugin-market-place
claude plugin install embedded-debug-kit@plugin-market-place
```

GitHub Copilot CLI：

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install embedded-debug-kit@plugin-market-place
```

スキルだけを使うときは、プラグインを入れずに、スキルのフォルダをプロジェクトにコピーしても使えます（各スキルの使い方を参照）。

## フォルダの構成

| 場所 | 中身 |
| --- | --- |
| `plugin.json` | マニフェスト（Agent Plugins 1.0.0。Copilot CLI が読む） |
| `.claude-plugin/plugin.json` | Claude Code が読むマニフェスト |
| `skills/` | スキル（1段階目と、4段階目の debug-dashboard）。両方の CLI で同じものを使う |
| `com.anthropic.claude-code/` | Claude Code 用のエージェントとフック |
| `com.github.copilot/` | Copilot CLI 用のエージェントとフック |
| `docs/` | 憲章、仕様書、設計書、使い方 |
| `tests/` | 部品の独立性、文書、通しのテスト |
| `evals/`、`skills/<名前>/evals/` | `claude plugin eval` のケース |

## テスト

```shell
python3 -m unittest discover -s tests/independence -v
python3 -m unittest discover -s tests/docs -v
claude plugin validate .
```

各スキルのテストは、スキルのフォルダで `python3 -m unittest discover -s tests` を動かします。
通しのテスト（`tests/e2e/run_e2e.py`）と `claude plugin eval .` は、AI の利用料がかかります。

## してはいけないこと

- コミットしていない変更があるフォルダで、2・3段階目を使わないでください。AI はソースを直接書き換えます。
- パスワードをチャットに書かないでください。設定ファイルか環境変数で渡します。
