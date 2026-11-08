---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Coverity の警告をトリアージして。条件は High だけで。

Input files: .coverity-triage/config.yaml, .coverity-triage/fake-issues.yaml, .coverity-triage/filters/untriaged.yaml, src/reader.c
