---
tags: [skill-creator]
max_turns: 30
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

CID 31001 の警告が本物か調べて。チェッカーは FORWARD_NULL、場所は src/textbuf.c の 13 行（buf_append）。
警告経路はこれです。
1. src/textbuf.c:10 var_compare_op — 「s」を NULL と比較しているので、「s」は NULL の可能性がある
2. ★ src/textbuf.c:13 var_deref_model — NULL の可能性がある「s」を strlen に渡していて、strlen はそれを参照する

Input files: src/textbuf.c, src/textbuf.h, src/report.c
