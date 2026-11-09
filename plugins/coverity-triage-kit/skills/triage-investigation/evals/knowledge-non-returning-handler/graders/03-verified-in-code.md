---
type: llm
focus: trace
weight: 1
---

fatal() が戻らないことを、知識だけでなく src/fatal.c のコードでも確かめ、根拠に挙げた src/fatal.c の行に、wdt_force_reset() の後の for (;;) の行（10 行）が入っている（10〜12 行のような範囲でも、関数全体の 6〜13 行でもよいが、for (;;) が戻らない理由だと述べていること）。
