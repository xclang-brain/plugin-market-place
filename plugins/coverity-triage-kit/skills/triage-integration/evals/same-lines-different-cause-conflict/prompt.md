---
tags: [skill-creator]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

src/io.c の read_record() に Coverity の NULL_RETURNS の警告が2件あり（CID 20021 と 20030）、どちらも調べ終わりました。
結果は results/<CID>.json、修正の差分は patches/<CID>-fix.patch にあります。
どちらも data の NULL の確かめ忘れなので、同じ原因ならまとめて1つにしたいです。まとめられるかどうかを判断して、チャットで教えてください。ファイルは作らないでください。

Input files: src/io.c, results/20021.json, results/20030.json, patches/20021-fix.patch, patches/20030-fix.patch
