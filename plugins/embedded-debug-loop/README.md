# embedded-debug-loop

組み込み機器の C/C++ の不具合を、AI が調べて直す GitHub Copilot CLI のプラグインです。
部品は3段に分かれていて、下の段の部品を置き換えても、上の段はそのまま使えます。

## 3つの段階

AI に任せる範囲で、使う部品が変わります。使い方と設定は、表の README にあります。

| 段階 | 使う部品 | 見る README |
| --- | --- | --- |
| 1 | スキル。1つの作業だけを頼む | 各スキルの README（[skills/](skills/) の下のフォルダ） |
| 2 | 作業エージェント（`device-worker`）。不具合の調査と修正を、チャットで任せる | [作業エージェントの README](com.github.copilot/agents/README.markdown) |
| 3 | 統括エージェント（`orchestrator`）・検証エージェント（`verifier`）・Web管理画面。直ったかを別の AI に確認させ、進み具合をブラウザーで見る | [3段階目のガイド](docs/guide/3-orchestrator.md) |

## 必要なもの

- Windows の PC（Linux でも動きます）
- GitHub Copilot CLI（動作を確認したバージョン：1.0.105。Copilot CLI と組み合わせた確認は Linux の PC で行い、Windows ではまだ行っていません）

部品ごとに要るものは、上の表の README にあります。

## インストール

Copilot CLI を起動し、次の2つを入力します。

```text
/plugin marketplace add xclang-brain/plugin-market-place
/plugin install embedded-debug-loop@plugin-market-place
```

## 部品を置き換える

作業の方法や機器が違うときは、スキルか作業エージェントを置き換えます（[置き換えのガイド](docs/guide/replace.md)）。

## 開発者向けの文書

[仕様書](docs/specs/requirements.md)・[設計書](docs/design/design.md)・[憲章](docs/constitution.md)
