---
tags: [skill-creator]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Coverity のトリアージの設定で、結果を別の AI が確かめる「確かめ」を使うようにして、差し戻しは 2 回までにしてください。変わる項目を見せてくれれば、そのまま保存してかまいません。チームのメモ（# の注釈）は消さないでね。

Input files: .coverity-triage/config.yaml, src/main.c
