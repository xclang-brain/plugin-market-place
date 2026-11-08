---
type: llm
focus: trace
weight: 1
---

差分は、s->buf が NULL で返す前に s を解放していて（free(s) など）、新しいメモリの漏れを作っていない。
