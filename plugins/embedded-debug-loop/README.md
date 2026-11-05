# embedded-debug-loop

組み込み機器の C/C++ の不具合を、AI が調べて直す GitHub Copilot CLI のプラグインです。

## 3つの段階

AI に任せる範囲に応じて、使う部品を選びます。

| 段階 | 使う部品 | 見る文書 |
| --- | --- | --- |
| 1 | スキル。1つの作業だけを頼む | 各スキルの README（[skills/](skills/) の下のフォルダ） |
| 2 | 作業エージェント。不具合の調査と修正を、チャットで任せる | [作業エージェントの README](com.github.copilot/agents/README.markdown) |
| 3 | 統括エージェント（`orchestrator`）・検証エージェント（`verifier`）・Web管理画面。直ったかを別の AI に確認させ、進み具合をブラウザーで見る | [3段階目のガイド](docs/guide/3-orchestrator.md) |

## 必要なもの

- Windows の PC（Linux でも動きます）
- GitHub Copilot CLI（1.0.105 で確認。Windows では未確認）

部品ごとに要るものは、上の表の「見る文書」にあります。

## インストール

Copilot CLI を起動し、次の2つを入力します。

```text
/plugin marketplace add xclang-brain/plugin-market-place
/plugin install embedded-debug-loop@plugin-market-place
```

## 開発者向けの文書

[仕様書](docs/specs/requirements.md)・[設計書](docs/design/design.md)・[部品の変え方](docs/replace.md)・[憲章](docs/constitution.md)
