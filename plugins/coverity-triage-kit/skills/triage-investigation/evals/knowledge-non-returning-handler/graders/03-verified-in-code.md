---
type: llm
focus: trace
weight: 1
---

fatal() が戻らないことを、知識だけでなく src/fatal.c のコード（wdt_force_reset() の後の for (;;) の無限ループ、10〜12 行あたり）でも確かめ、その場所を相対パスと行で根拠に挙げている。
