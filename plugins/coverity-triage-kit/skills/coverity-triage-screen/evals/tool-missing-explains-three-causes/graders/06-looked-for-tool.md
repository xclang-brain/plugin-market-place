---
type: llm
focus: trace
weight: 1
---

Before saying the screen tool is missing, the run looked for coverity-triage-kit_open: the raw tool calls show a search of the available tools for it (for example ToolSearch with that name) before the reply. A run that says the tool is missing without having searched fails.
