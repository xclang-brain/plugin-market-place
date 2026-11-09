---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Coverity の警告を直す作業です。質問はせず、次のとおりに進めてください。

- 警告：CID 20002、RESOURCE_LEAK、src/loader.c の 18 行（load_table）、Impact High
  - 警告経路：12 行 fopen で fp を開く → 17 行 got != TABLE_BYTES が真の枝 → ★ 18 行 fp を閉じずに返す
  - 調べた結果：本物のバグ（true_bug）。行番号は、調べたコード（repo/）のもの
- 調べたコード：repo/（読むだけ。変えない）
- 直すフォルダ：work/w1/（取り込み先の最新のリビジョン。直してよいのはここだけ）
- 結果：out/fix.json に、summary、impact、exceeded_constraints、already_fixed_on_latest、classification、action、severity の項目だけの JSON オブジェクトで書く
- 書き終えたら「20002: <summary>」の1行だけを返す

Input files: repo/src/loader.c, repo/src/loader.h, work/w1/src/loader.c, work/w1/src/loader.h
