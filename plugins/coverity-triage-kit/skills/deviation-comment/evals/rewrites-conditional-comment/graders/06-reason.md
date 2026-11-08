---
type: llm
focus: trace
weight: 1
---

The rewritten comment states why buf is never read when len is 0: send_packet() is called only from send_all(), which returns before calling it when buf is NULL or len is 0.
