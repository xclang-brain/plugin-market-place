# ステップ1：警告を1件、AI に調べてもらう

**所要時間**：15分　**必要なもの**：GitHub Copilot CLI、調査したいリポジトリ、Coverity の警告1件の情報（ブラウザーで見えているもので十分です）

このステップでは、プラグインをインストールして、警告を1件だけ調べてもらいます。Coverity への接続設定は、まだ要りません。「トリアージの結果がどんな形で返ってくるか」をつかむのが目的です。

## 1. インストールする

ターミナルで次の2行を実行します。

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install coverity-triage-kit@plugin-market-place
```

## 2. リポジトリで Copilot CLI を起動する

調査したいリポジトリのフォルダに移動して、`copilot` を起動します。

## 3. 警告を1件、調べてもらう

Coverity の画面で警告を1件選び、**CID、チェッカー名、ファイル名、行番号**をチャットに書いて頼みます。

```text
CID 20002（FORWARD_NULL、src/reader.c の 15 行）の警告が本物か調べて
```

AI がコードを読み、呼び出し元までたどって、次のような結果を返します。

```text
結論：誤検知です。

判定：誤検知（false_positive）
確信度：高
推奨：逸脱

根拠：
- src/reader.c:13 — open_and_read() は static 関数で、呼び出し元は read_config() だけ
- src/reader.c:32〜35 — read_config() は、path が NULL なら return -1 する
- src/reader.c:36 — open_and_read() は、その確認を通った path だけで呼ばれる
```

見るポイントは3つです。

| 項目 | 意味 |
| --- | --- |
| 判定 | 本物のバグ／誤検知／意図的／判定不能 のどれか |
| 確信度 | 高・中・低。低いものほど、人が丁寧に確認します |
| 根拠 | AI が実際に読んだコードの位置。ここを開けば、判定の理由を自分で確かめられます |

## 4. 続けて、修正案と逸脱コメントを作ってもらう

同じチャットで、続けて頼めます。

```text
この警告の逸脱コメントを書いて
```

```text
誤検知。open_and_read() は static 関数で、呼び出し元は read_config()（src/reader.c:36）だけであり、
そこでは path が NULL なら src/reader.c:32〜35 で戻るため、fopen() に NULL が渡ることはない。
（145文字）
```

```text
修正する場合の差分も見せて
```

```diff
 static int open_and_read(const char *path, char *out)
 {
+    if (path == NULL) {
+        return -1;
+    }
     FILE *fp = fopen(path, "r");
```

チェッカーの意味が分からないときは、そのまま質問できます。

```text
MISRA C-2012 Rule 10.3 はどんな規則？ よくある直し方は？
```

## ここまでで分かったこと

- トリアージの結果は「判定・確信度・推奨・根拠」と、「修正案」「逸脱コメント案」の組です。
- 1段階目では、頼んだことだけを AI が行います。調査、修正案、逸脱コメントを、必要なものだけ個別に頼めます。

## 次のステップ

毎回、警告の情報を手で書き写すのは手間です。[ステップ2](2-agent.md) では、Coverity に接続して、CID を伝えるだけで調査から修正案・逸脱コメント案までを一度に任せます。

---

### 補足

- Claude Code でも同じように使えます。インストール方法だけ異なります（[Claude Code ガイド](claude-code.md)）。
- 1段階目で使えるスキルは、[skills/](../../skills/) にあります。それぞれのフォルダの README に、単体での使い方があります。スキルのフォルダだけをコピーして、プラグインなしで使うこともできます。
- この段階では、AI はコードを読むだけです。ファイルも Coverity も変更しません。
