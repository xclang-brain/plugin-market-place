---
type: llm
focus: trace
weight: 1
---

The comment names the place where read_all() removes NULL in the form path:line with the full path from the repository root: src/io/reader.c:32, or a narrow range that includes line 32 and stays within lines 29-35 (for example src/io/reader.c:32〜34). A shortened path such as reader.c:32, or a range such as the whole file, fails.
