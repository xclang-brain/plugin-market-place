---
type: llm
focus: trace
weight: 1
---

work/w1/src/loader.c の load_table() で、got != TABLE_BYTES の枝で返す前に fp が閉じられる（fclose が呼ばれる）ようになっている。
