# embedded-debug-kit

組み込み機器で動く C/C++ プログラムの不具合を、AI が、ビルドと機器での実行を繰り返して直すプラグインです。

## 必要なもの

- GitHub Copilot CLI（1.0.107 以降）か Claude Code
- uv
- Git
- 機器向けのコンパイラー
- 機器の FTP サーバーとシリアルコンソール
- Windows か Linux の PC

## インストール

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install embedded-debug-kit@plugin-market-place
```

Claude Code では、`copilot` を `claude` に読み替えます。

## 使い方

AI に任せる範囲に合わせて、4つの段階から選びます。
不具合を直してもらうなら、2段階目から始めます。1段階目の準備も、その手順の中で済みます。

| 段階 | できること | 使い方 |
| --- | --- | --- |
| 1 | 下の3つを使う準備を、AI の質問に答えて済ませる | [skill-config](docs/guide/1-skill-config.md) |
| 1 | プログラムをビルドする | [cross-build](docs/guide/1-cross-build.md) |
| 1 | ファイルを機器に送る | [ftp-deploy](docs/guide/1-ftp-deploy.md) |
| 1 | 機器でコマンドを動かす | [serial-console](docs/guide/1-serial-console.md) |
| 2 | 不具合の調査と修正を、AI に任せる | [作業エージェント](docs/guide/2-device-debugger.md) |
| 3 | 修正を任せ、直ったかを別の AI に判定させる | [統括エージェント](docs/guide/3-debug-orchestrator.md) |
| 4 | 3段階目の進み具合と結果を、ブラウザーで見る | [Web管理画面](docs/guide/4-debug-dashboard.md) |

1段階目の4つを、スキルと呼びます。スキルと作業エージェントは、[自分で作ったものに置き換える](docs/guide/5-replace-parts.md)ことができます。

## してはいけないこと

- ソースに、コミットしていない変更を残したまま、2段階目以降を使わないでください。AI がソースを書き換えるので、AI の変更と区別できなくなります。
- パスワードをチャットに書かないでください。環境変数で渡します。
