---
type: llm
focus: trace
weight: 1
---

If the response shows a corrected deviation comment, that comment follows the comment rules in content too: it gives the reason first and then the code location (関数名()（ファイル:行）), and it no longer describes what was checked (「…で確かめた」) or says that it need not be fixed (「直さなくてよい」), and it has no condition or request. A response that shows no corrected comment passes this item.
