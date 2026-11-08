---
type: llm
focus: trace
weight: 1
---

out/fix.json の summary か impact が直した場所を行で示すときは、最新のリビジョン（work/w1/src/loader.c）の行（短い読みの枝は 33〜35 行）で書いていて、調べたコードの 18 行を最新の場所として書いていない。行を書いていなければ、関数名 load_table() と枝（got != TABLE_BYTES のとき）で場所が分かる。
