---
type: llm
focus: trace
weight: 1
---

差分は、s->buf = malloc(size); の後、memset の前に s->buf が NULL のときの確かめを足し、そこで先に確保した s を解放してから（free(s) など）、ファイルのほかの経路と同じ STORE_ERR_NOMEM を返している。
