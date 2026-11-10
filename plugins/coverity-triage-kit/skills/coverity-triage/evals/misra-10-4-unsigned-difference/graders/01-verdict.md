---
type: llm
focus: trace
weight: 1
---

12 行の比較を、Rule 10.4 の実際の違反（本物のバグ、true_bug。規則の違反として本物）と判定し、推奨を修正としている。誤検知（false_positive）としていない。理由として、符号なしの式 (end - start) と符号付きの定数 0 を比較していることを述べている。
