---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Coverity の ASSERT_SIDE_EFFECT という警告はどう見ればいいですか？
Coverity の説明は「assert() の引数の式に副作用がある。NDEBUG を定義したビルドでは assert() が消えるので、その副作用もなくなる」でした。
