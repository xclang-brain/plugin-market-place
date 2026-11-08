---
type: llm
focus: trace
weight: 1
---

textbuf_name() が buf_append に渡す前に name の NULL を除いている（src/textbuf.c の 35〜37 行）ことを、根拠か調べた経過で述べている。
