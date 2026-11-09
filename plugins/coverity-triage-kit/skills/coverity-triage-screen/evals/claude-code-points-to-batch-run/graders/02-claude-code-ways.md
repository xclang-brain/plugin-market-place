---
type: llm
focus: trace
weight: 1
---

The reply gives both Claude Code ways to start a batch triage, with the names from the "使う部品" table of the skill: start Claude Code with claude --agent and the orchestrator agent's name (with the plugin name and ":" before it), then ask for the triage; or call the batch-run skill with its slash command. A reply that gives only one of them, or describes them without the command or name, fails.
