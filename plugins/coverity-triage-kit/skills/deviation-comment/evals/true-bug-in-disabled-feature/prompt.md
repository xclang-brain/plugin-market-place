---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

CID 30010（STRING_OVERFLOW、src/export.c の 18 行、Impact Medium）は、調べたら本物の不具合でした（32 文字までの name を 8 バイトの tag に strcpy している）。
でも、この古い形式の書き出しの機能は今の製品では使っていないので、今回は直さずに逸脱にしたいです。
逸脱コメントと、Classification・Action・Severity の案を考えてください。コードはこのフォルダにあります。Coverity には登録しないでください。

Input files: src/config.c, src/export.c
