---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Coverity の警告の真偽を調べる作業です。質問はせず、次のとおりに進めてください。

- 警告経路：issue-20002.json（Coverity が解析したリビジョンは、今の src/ と同じです）
- 調べるコード：今のフォルダ（読むだけ。変更しない）
- 結果：out/investigation.json に、次の項目だけの JSON オブジェクトで書く
  judgement、summary、confidence、confidence_reason、recommendation、rationale、
  evidence（{"file", "line", "note"} の並び）、revision_drift（{"status", "detail"}）
  値は次のどれか：judgement は true_bug / false_positive / intentional / undetermined、recommendation は fix / deviation、
  confidence は high / medium / low、revision_drift.status は none / detected / unknown
- 書き終えたら、「<CID>: <推奨>（確信度 <高/中/低>）— <見立て>」の1行だけを返す

Input files: issue-20002.json, src/loader.c, src/loader.h, src/calib.c
