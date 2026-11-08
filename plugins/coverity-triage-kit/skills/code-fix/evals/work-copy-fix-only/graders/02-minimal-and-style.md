---
type: llm
focus: trace
weight: 1
---

work/w1/src/loader.c の変更は load_table() の中の、fp を閉じるのに要る行だけで、ファイルの書き方（(void)fclose(fp); の形とタブの字下げ）に合っている。ほかの関数と loader.h は変わっていない。
