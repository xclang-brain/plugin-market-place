---
type: llm
focus: trace
weight: 1
---

修正コードを差分（unified diff）で示している。差分は、load_table() の got != TABLE_BYTES の分岐で、返す前に fp を閉じる。書き方はファイルに合っている（(void)fclose(fp); の形、タブのインデント）。変更はその分岐に必要な行だけで、正常な経路で fp を2回閉じることはなく、load_header() と関数の引数・戻り値の型は変更していない。
