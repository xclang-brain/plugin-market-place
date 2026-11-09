---
type: llm
focus: trace
weight: 1
---

The server behind the URL keeps running after the agent's turn: when graded, a request to the URL from the final answer gets HTTP 200, and the same URL with a wrong ?t= value gets 403.
