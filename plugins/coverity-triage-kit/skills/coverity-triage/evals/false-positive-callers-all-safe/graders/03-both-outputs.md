---
type: llm
focus: trace
weight: 1
---

result.json の deviation.comment は「誤検知。」で始まり、条件（ただし、〜の場合に限る）、推測、依頼がなく、根拠の場所を src/textbuf.c:38 のような「パス:行」の形で示している。修正コードは、外部から見える動作を変えない差分（cid/31001/fix.patch。たとえば buf_append の NULL の分岐で返す）か、修正コードがない理由（no_fix_reason）のどちらかである。recommendation は fix か deviation のどちらか1つで、修正コードがないのに fix にしていない。作業フォルダー（利用者のリポジトリ）の src/ のファイルは変更されていない。
