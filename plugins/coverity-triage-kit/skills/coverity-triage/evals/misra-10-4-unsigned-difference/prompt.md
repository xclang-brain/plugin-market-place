---
tags: [skill-creator]
max_turns: 30
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

CID 51004 を処置して。Coverity の警告は次のとおりです。対象の処理系の int は 32 ビットです。

- チェッカー：MISRA C-2012 Rule 10.4（Impact：Low）
- 場所：src/window.c の 12 行（window_remaining）
- 警告経路：
  1. ★ src/window.c:12 misra_violation — 「(end - start) > 0」の両方のオペランドの、本質的な型のカテゴリが異なる（unsigned と signed）

Input files: src/window.c
