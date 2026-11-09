---
type: llm
focus: trace
weight: 1
---

out/fix.json が、summary、impact、exceeded_constraints、already_fixed_on_latest、classification、action、severity の7項目だけの正しい JSON で、exceeded_constraints が空の並び []（false ではない）、classification が Bug、action が Fix Required（Fix Submitted ではない）、severity が Major（Impact High）で、impact が「High」のような重さの言葉でなく、何が変わる・変わらないかを述べた文である。
