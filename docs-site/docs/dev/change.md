# 変更時の手順

## 変更の連動箇所

| 変更対象 | 関連する変更箇所 | テスト |
| --- | --- | --- |
| エージェントの本文 | Copilot CLI 用の `com.github.copilot/agents/<名前>.agent.md` と、開発用の `agents/<名前>.md` の本文を一致させる（frontmatter は形式が異なる） | `agents`、EVAL |
| エージェントのモデル（frontmatter の `model`） | 上記2ファイルの両方 | EVAL |
| スキルの本文（`SKILL.md`） | 本文中の応答キーとコマンドが、スクリプトの実際の応答と一致すること | `agents`、`flow`、EVAL |
| スクリプトの応答（`triage.py`、`apply.py`） | その応答を扱うスキルとエージェントの本文 | `skills/<名前>`、`agents`、`flow` |
| 設定キー | `setup.py` のテンプレートと検証処理、キーを読み込むスクリプト、仕様書5章、`tests/test_structure.py` の `SETTINGS`、`docs/guide/settings.md` | `structure`、`budget` |
| MCP ツール | `tools.py` と `server.py`、エージェントの `tools`（2ファイルとも）、`tests/test_structure.py` の `TOOLS` | `structure`、`mcp-servers/coverity-connect` |
| Coverity に登録する値 | `checks.py`（判定ごとの Classification・Action・Severity）、`apply.py`（取り込んだ修正と保留）、仕様書4章 | `skills/coverity-triage`、`skills/coverity-apply` |
| 逸脱コメントの規則 | `checks.py`、スキル `coverity-triage` の4章、画面での編集時の検査（`gui.py`） | `skills/coverity-triage`、`skills/coverity-gui` |
| Web 画面 | `gui.py`、`static/` | `skills/coverity-gui` |
| README と `docs/guide` | 後述の「文書の規則」、本サイトの該当ページ | `docs`、`budget` |

テストスイートの内容は[テスト](test.md)を参照してください。

!!! note "設定ファイルの読み込み箇所（5か所）"
    `setup.py`、`triage.py`、`apply.py`、`gui.py`、MCP サーバーの `settings.py` が、それぞれ設定ファイルを読み込みます。各スキルがほかのフォルダーのコードを読み込まない設計のためです。キーの意味や既定値を変更する場合は、5か所すべてを確認してください。

## 機能追加時の制約

設定キー（13）、MCP ツール（4）、コマンド（スキル4、エージェント2）は、いずれも上限に達しています。追加する場合は、同数を削減できないかを先に検討してください。上限自体を変更する場合は、仕様書8章の表と `tests/test_budget.py` の `CAPS` を同時に更新します（`budget` が両者の一致を検査します）。

## 文書の規則

プラグインの文書（README、`docs/`、`SKILL.md` など）は、`docs` テストで次の項目を検査します。

- 相対リンクと見出しのアンカーが解決できること（アンカーは GitHub の生成規則で判定）
- 禁止語を含まないこと（`tests/test_docs.py` の `BANNED`）
- 開発ツール名を、開発用の文書（憲章、設計書、`tests/`）以外に記載しないこと
- 文書中の `/coverity-triage-kit:<スキル>` と `--agent coverity-triage-kit:<エージェント>` が実在すること
- README の認証情報の設定手順が、`setup.py` の出力と一致すること
- 3つ目の選択肢の表記が「保留にする」であること（「何もしない」はコードに関してのみ使用）

## バージョンの更新

次の4か所のバージョンを一致させます。`docs` テストが一致を検査します。

| ファイル | 記載内容 |
| --- | --- |
| `plugin.json` | `version` |
| `.claude-plugin/plugin.json` | `version` |
| リポジトリの `.github/plugin/marketplace.json` | 本プラグインの `version` |
| `CHANGELOG.md` | 先頭に `## 0.4.0（2026-12-01）` 形式の見出しと、利用者から見た変更点 |

リポジトリの `README.md` のプラグイン一覧にもバージョンの記載があります（テストの対象外）。

設定ファイルや結果フォルダーの形式が変わり、旧バージョンのものが使えなくなる場合は、CHANGELOG に移行手順を記載します。
