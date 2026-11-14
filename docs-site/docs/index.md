---
title: ホーム
hide:
  - navigation
  - toc
---

<div class="ctk-hero" markdown>

# coverity-triage-kit

Coverity Connect の警告を CID ごとに調べ、**修正コード**と**逸脱コメント**を両方用意する、GitHub Copilot CLI のプラグインです。対象は C / C++ です。どちらを採用するかは、最後に一覧で決めます。

</div>

```mermaid
flowchart LR
  subgraph before["OK 前（リポジトリと Coverity は変更なし）"]
    direction LR
    A["警告の取得"] --> B["CID ごとの調査と<br>修正コード・逸脱コメントの作成"] --> C["一覧の表示"]
  end
  C == "OK" ==> after
  subgraph after["OK 後"]
    direction TB
    F["修正を取り込む<br>リポジトリ ＋ Coverity"]
    D["逸脱コメントを登録する<br>Coverity"]
    P["保留にする<br>Coverity"]
  end
```

<div class="grid cards" markdown>

-   :material-rocket-launch-outline:{ .lg } **初めての利用**

    ---

    準備をして、1件の警告で、調査から OK までを試します。

    [:octicons-arrow-right-24: 準備](use/setup.md)

-   :material-robot-outline:{ .lg } **一括処置**

    ---

    条件に合う警告を、エージェントに最後まで任せます。

    [:octicons-arrow-right-24: エージェントによる一括処置](use/delegate.md)

-   :material-lifebuoy:{ .lg } **動作の不具合**

    ---

    表示されたメッセージを貼り付けて、対処を探します。

    [:octicons-arrow-right-24: トラブルシューティング](use/trouble.md)

-   :material-source-branch:{ .lg } **プラグインの開発**

    ---

    設計原則と、それを検査するテストから確認します。

    [:octicons-arrow-right-24: 概要と原則](dev/index.md)

</div>
