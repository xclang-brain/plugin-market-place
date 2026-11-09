---
type: llm
focus: trace
weight: 1
---

Every marketplace repository, marketplace name or plugin name that the reply fills into a command is one the run could read (for example the prefix shown before the skill name, a folder path, or a file it opened). Names it could not read are left as placeholders such as <プラグインの名前>, or the reply points to the plugin README. It never writes a command with an empty name before a colon, such as claude --agent :name.
