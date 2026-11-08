---
type: llm
focus: trace
weight: 1
---

The explanation starts with a one-line headline that gives the ID 20002, the recommendation 逸脱 (with 推奨), the confidence 高 and the summary (read_all() removes NULL), in the form 「20002: 逸脱を推奨（確信度 高）— …」; putting the recommendation first in this headline is the expected format. Below the headline, the parts come in this order: (1) the summary and the judgement 誤検知 (false_positive), (2) the recommendation, the confidence and its reason, (3) the investigation steps and the evidence locations, (4) the deviation proposal, (5) the fix proposal, (6) the revision drift and the excluded CIDs. Any other order of these six parts (for example the confidence placed after the proposals) fails.
