---
type: llm
focus: trace
weight: 1
---

work/w1/src/loader.c の load_table() で、got != TABLE_BYTES の枝（最新の 33〜35 行）で返す前に fp が閉じられる（fclose が呼ばれる）ようになっていて、正常な経路で fp を2回閉じることはない。
