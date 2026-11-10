# コンポーネント一覧（開発者向け）

coverity-triage-kit を構成するスキル、MCP サーバー、エージェントの一覧です。

[仕様書](specs/requirements.md) と [設計書](design/design.md) は、コンポーネントを名前ではなく役割で呼びます。役割と実際の名前の対応は、この表だけに書きます。テスト（`tests/`）も、この表から名前を読み取ります。

## 今の部品

| 役割 | 段階 | 部品 | 置き場所 |
| --- | --- | --- | --- |
| チェッカーの見方 | 1 | `checker-knowledge` | `skills/checker-knowledge/` |
| 真偽の調査 | 1 | `triage-investigation` | `skills/triage-investigation/` |
| 修正 | 1 | `code-fix` | `skills/code-fix/` |
| 逸脱コメント | 1 | `deviation-comment` | `skills/deviation-comment/` |
| 結果の記録 | 1 | `triage-report` | `skills/triage-report/` |
| 結果の確かめ | 1 | `triage-review` | `skills/triage-review/` |
| 同じ原因の統合 | 1 | `triage-integration` | `skills/triage-integration/` |
| 準備 | 1 | `coverity-setup` | `skills/coverity-setup/` |
| 一括実行 | 1 | `coverity-run` | `skills/coverity-run/` |
| 反映 | 1 | `coverity-apply` | `skills/coverity-apply/` |
| 結果と設定の画面 | 1 | `coverity-gui` | `skills/coverity-gui/` |
| Coverity との通信 | 1 | `coverity-connect` | `mcp-servers/coverity-connect/` |
| 作業役 | 2 | `triage-worker` | `com.github.copilot/agents/triage-worker.agent.md` |
| 統括 | 3 | `triage-orchestrator` | `com.github.copilot/agents/triage-orchestrator.agent.md` |
| 検証役 | 3 | `triage-reviewer` | `com.github.copilot/agents/triage-reviewer.agent.md` |
| 統合役 | 3 | `triage-integrator` | `com.github.copilot/agents/triage-integrator.agent.md` |
| 困ったとき | 4 | `coverity-help` | `skills/coverity-help/` |
| 自己診断 | 4 | `coverity-selftest` | `skills/coverity-selftest/` |

- 段階は、そのコンポーネントが単独で使える最も下の段階です。どの段階も、それより上の段階のコンポーネントなしで使えます。
- エージェントは、Claude Code 用（`agents/<名前>.md`）と Copilot CLI 用（`com.github.copilot/agents/<名前>.agent.md`）の2つの定義を持ちます。表には Copilot CLI 用の場所を書いています。
- MCP サーバーの登録名（`coverity-connect`）とツール名（`coverity_…`）は、他のコンポーネントから参照される名前です（設計書の5章）。変更すると、参照している側も直す必要があります。
- 複数のコンポーネントが同じファイル（スキーマ、共通のスクリプト）の写しを持っています。元のファイルを直したら、写しも同じ内容にします。写しの一致は `tests/contracts/` が確認します。
