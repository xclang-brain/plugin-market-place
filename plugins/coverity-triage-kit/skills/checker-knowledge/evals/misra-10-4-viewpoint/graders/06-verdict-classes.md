---
type: llm
focus: trace
weight: 1
---

本当に違反していれば true_bug、Coverity の読み違いや規則の対象外なら false_positive、設計上必要で値の範囲で安全を説明できれば intentional、という判定の考え方を示している。設計上必要なものを「true_bug のまま逸脱」とだけ書き、intentional を挙げない答えは不合格。
