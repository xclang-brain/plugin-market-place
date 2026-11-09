---
type: llm
focus: trace
weight: 1
---

The reply stays on the 401. It does not tell the user to fix things that cannot cause a 401 (for example that the folder is not a git repository), and it does not present COV_USER / COV_AUTH_KEY as missing or unset as the cause: a 401 means the user name and key were sent and rejected. Saying briefly that the check from this session cannot see the variables is fine.
