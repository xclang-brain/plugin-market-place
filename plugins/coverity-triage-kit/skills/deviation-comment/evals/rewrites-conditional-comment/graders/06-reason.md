---
type: llm
focus: trace
weight: 1
---

The rewritten comment states why buf is never read when len is 0: send_packet() is called only from send_all(), which returns before calling it when len is 0. Mentioning the buf == NULL half of the same check as well is optional.
