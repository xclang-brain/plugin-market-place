---
type: llm
focus: trace
weight: 1
---

The reply says that /add-dir alone is not enough for an overnight batch run in Claude Code, and it shows how to allow the tools before starting: starting Claude Code with --add-dir and --allowedTools (or adding the rules with /permissions), including Bash(uv run *), a Skill rule for the plugin's skills, a read-only Read rule for the folder where the plugin is installed, an Edit rule limited to the run folder, and the Coverity read tools. It does not tell the user to allow Write or Edit for every file.
