---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

CID 30417 の警告が本物か見てもらえますか。FORWARD_NULL で、src/settings.c の 24 行（settings_port）です。
Coverity の経路：
1. src/settings.c:21 var_compare_op — 「p」を NULL と比べているので、「p」は NULL かもしれない
2. src/settings.c:22 — fatal() を呼ぶ
3. ★ src/settings.c:24 var_deref_op — NULL かもしれない「p」を参照している

Input files: .coverity-triage/knowledge.md, src/settings.c, src/config.h, src/fatal.c, src/fatal.h
