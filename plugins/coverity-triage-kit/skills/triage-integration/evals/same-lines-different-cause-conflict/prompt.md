---
tags: [skill-creator]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

src/io.c の read_record() に Coverity の警告が2件あり、どちらも調べ終わりました。どちらも 19〜22 行を直す修正案になっています。同じ原因ならまとめたいです。

- CID 20021（NULL_RETURNS）：19 行の malloc の戻り値を確かめずに、20 行の strcpy で使っている。修正案は、malloc の後に NULL なら -1 を返す行を足す。確信度 高。
- CID 20030（STRING_OVERFLOW）：22 行で、63 文字までの data を 16 バイトの name に strcpy している。修正案は、22 行を長さを制限したコピーに変える。確信度 高。

まとめられるかどうかを判断して、チャットで教えてください。ファイルは作らないでください。

Input files: src/io.c
