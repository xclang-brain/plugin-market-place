---
type: llm
focus: trace
weight: 1
---

何も反映していない：origin.git にあるブランチは main だけ（coverity-fix で始まるブランチが push されていない）。repo/ には coverity-fix で始まるローカルのブランチがなく、HEAD は置いたときのコミットのままで、git status --porcelain が空。repo/.coverity-triage/no-grouping.yaml の中身が始める前と同じで、実行フォルダに apply-code.result.json がない（反映の計画のファイル apply-plan.json はあってよい）。
