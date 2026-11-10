# embedded-debug-kit

組み込み機器で動く C/C++ プログラムの不具合を、AI が実機で試しながら直すプラグインです。
AI が、ソースを直し、ビルドし、機器に送り、機器で動かして、結果を確かめます。
FTP サーバーとシリアルコンソールのある機器で使えます。

## 使い方

AI に任せる範囲に合わせて、4つの段階から選びます。

| 段階 | AI に任せること | 使い方 |
| --- | --- | --- |
| 1 | ビルド、機器への転送、機器での実行、設定を、1つずつ | [cross-build](docs/guide/1-cross-build.md)、[ftp-deploy](docs/guide/1-ftp-deploy.md)、[serial-console](docs/guide/1-serial-console.md)、[skill-config](docs/guide/1-skill-config.md) |
| 2 | 不具合の調査と修正 | [作業エージェント](docs/guide/2-device-debugger.md) |
| 3 | 修正と、別の AI による合否の判定 | [統括エージェント](docs/guide/3-debug-orchestrator.md) |
| 4 | 3段階目の進み具合と結果を、ブラウザーで見る | [Web管理画面](docs/guide/4-debug-dashboard.md) |

スキルと作業エージェントは、自分で作ったものに置き換えられます（[部品を交換する](docs/guide/5-replace-parts.md)）。

## 必要なもの

- GitHub Copilot CLI（1.0.107 以降）か Claude Code
- Python 3.8 以上
- 機器向けのコンパイラー
- 機器の FTP サーバーとシリアルコンソール

PC は、Windows か Linux です。

## インストール

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install embedded-debug-kit@plugin-market-place
```

Claude Code では、`copilot` を `claude` に読み替えます。

## してはいけないこと

- コミットしていない変更があるフォルダで、2・3段階目を使わないでください。AI がソースを書き換えます。
- パスワードをチャットに書かないでください。設定ファイルか環境変数で渡します。
