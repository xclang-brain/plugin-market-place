---
type: llm
focus: trace
weight: 1
---

判定の考え方を区分で示している：NDEBUG を定義した製品のビルドで副作用が消えて動きが変わるなら true_bug、副作用が結果に影響しないなら false_positive か intentional。そして、問題にならないと言い切れないなら逸脱でなく修正を推奨すると述べている。
