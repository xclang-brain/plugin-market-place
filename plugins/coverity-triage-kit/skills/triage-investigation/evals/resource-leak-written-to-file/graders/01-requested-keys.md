---
type: llm
focus: trace
weight: 1
---

out/investigation.json を JSON として読むと、キーがちょうど judgement、summary、confidence、confidence_reason、recommendation、rationale、evidence、revision_drift の8つで、evidence は file・line・note を持つものの並び、revision_drift は status と detail を持つ。
