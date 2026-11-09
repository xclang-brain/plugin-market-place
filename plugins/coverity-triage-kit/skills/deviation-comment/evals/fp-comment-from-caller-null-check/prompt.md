---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Coverity の CID 20002（NULL_RETURNS）の逸脱コメントを書いてください。

- 警告の場所：src/io/reader.c の 23 行（copy_name() の memcpy）
- 警告経路：get_buf() が NULL を返しうる（15 行）→ その戻り値が copy_name() の dst として渡り、23 行で使われる
- 調べた結果：誤検知。copy_name() を呼んでいるのは read_all() だけで、そこで NULL を除いてから渡している

コードはこのフォルダにあります。Coverity には登録しないで、案だけください。

Input files: src/io/reader.c
