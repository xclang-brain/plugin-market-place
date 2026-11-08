---
type: llm
focus: trace
weight: 1
---

判定の考え方を、true_bug・false_positive・intentional・undetermined の区分で示している：NDEBUG を定義した製品のビルドで副作用が消えて動きが変わるなら true_bug、副作用が結果に影響しないなら false_positive か intentional、製品のビルドの設定が分からず決められなければ undetermined。そして、問題にならないと言い切れないなら逸脱でなく修正を推奨すると述べている。
