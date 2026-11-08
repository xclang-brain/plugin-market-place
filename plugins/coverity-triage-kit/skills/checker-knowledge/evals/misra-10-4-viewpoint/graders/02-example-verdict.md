---
type: llm
focus: trace
weight: 1
---

例の count > 0（count は uint16_t）を、符号なしと符号付きの定数 0 の組み合わせとして本当の違反（true_bug、規則の違反として本物）と判定し、誤検知（false_positive）として片付けていない。直し方として 0U を示し、この1つの変数との比べでは 0U にしても動きが変わらないことも述べている。
