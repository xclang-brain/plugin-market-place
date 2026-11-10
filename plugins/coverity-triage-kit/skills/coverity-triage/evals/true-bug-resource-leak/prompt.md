---
tags: [skill-creator]
max_turns: 30
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

CID 20002 を処置して。Coverity の警告は次のとおりです。

- チェッカー：RESOURCE_LEAK（Impact：High）
- 場所：/var/build/fw/src/loader.c の 18 行（load_table）
- 警告経路：
  1. loader.c:12 alloc_fn — fopen が確保した資源を返す
  2. loader.c:12 var_assign — その資源を fp に代入
  3. loader.c:17 cond_true — 「got != 256U」が真の分岐を通る
  4. ★ loader.c:18 leaked_storage — fp がスコープを抜け、資源がリークする

Input files: src/loader.c, src/loader.h, src/calib.c
