# 変更するとき

## 一緒に変える場所

左の列を変えるときに、合わせて変える場所と、実行するテストのスイートです（[テストする](test.md)）。

| 変えるもの | 合わせて変える場所 | テスト |
| --- | --- | --- |
| エージェントの本文 | Copilot CLI 用の `com.github.copilot/agents/<名前>.agent.md` と、開発用の `agents/<名前>.md` の本文を同じにします。frontmatter は書式が違います | `agents`、EVAL |
| エージェントのモデル（frontmatter の `model`） | 上の2つのファイルの両方 | EVAL |
| スキルの文章（`SKILL.md`） | 文章に書いた応答のキーとコマンドが、スクリプトの実際の応答にあること | `agents`、`flow`、EVAL |
| スクリプトの応答（`triage.py`、`apply.py`） | その応答を扱うスキルとエージェントの文章 | `skills/<名前>`、`agents`、`flow` |
| 設定キー | `setup.py` のひな形と検証、キーを読むスクリプト、仕様書5章、`tests/test_structure.py` の `SETTINGS`、`docs/guide/settings.md` | `structure`、`budget` |
| MCP のツール | `tools.py` と `server.py`、エージェントの `tools`（2つのファイル）、`tests/test_structure.py` の `TOOLS` | `structure`、`mcp-servers/coverity-connect` |
| Coverity に登録する値 | `checks.py`（判定ごとの Classification・Action・Severity）、`apply.py`（取り込んだ修正と保留）、仕様書4章 | `skills/coverity-triage`、`skills/coverity-apply` |
| 逸脱コメントの規則 | `checks.py`、スキル `coverity-triage` の4章、画面での編集のチェック（`gui.py`） | `skills/coverity-triage`、`skills/coverity-gui` |
| 画面 | `gui.py`、`static/` | `skills/coverity-gui` |
| 利用者が読む文書 | 下の「文書の規則」と、このサイトの該当ページ | `docs`、`budget` |

!!! note "設定ファイルを読むコードは5か所にある"
    `setup.py`、`triage.py`、`apply.py`、`gui.py`、MCP サーバーの `settings.py` が、それぞれ設定ファイルを読みます。スキルのフォルダーが、ほかのフォルダーのコードを読み込まない設計だからです。キーの意味や既定値を変えるときは、5か所すべてを確認します。

## 機能を足す前に

設定キー（13）、MCP のツール（4）、コマンド（スキル4、エージェント2）は、すでに上限に達しています。足すときは、先に同じ数だけ減らせないかを考えます。上限そのものを変えるときは、仕様書8章の表と `tests/test_budget.py` の `CAPS` を一緒に変えます（`budget` が両者の一致を確認します）。

## 文書の規則

プラグインの文書（README、`docs/`、`SKILL.md` など）について、`docs` のテストが次を確認します。

- 相対リンクと、見出しへのアンカーが解決すること（アンカーは GitHub の付け方で確認）
- 使わない言葉がないこと（`tests/test_docs.py` の `BANNED`）
- 開発に使うツールの名前を、開発用の文書（憲章、設計書、`tests/`）以外に書かないこと
- 文書にある `/coverity-triage-kit:<スキル>` と `--agent coverity-triage-kit:<エージェント>` が実在すること
- README の認証情報の手順が、`setup.py` の表示と同じであること
- 3つ目の選択の語が「保留にする」であること（「何もしない」は、コードについてだけ使います）

## 版を上げる

次の4か所を同じ版にします。`docs` のテストが一致を確認します。

| 場所 | 書くもの |
| --- | --- |
| `plugin.json` | `version` |
| `.claude-plugin/plugin.json` | `version` |
| リポジトリの `.github/plugin/marketplace.json` | このプラグインの `version` |
| `CHANGELOG.md` | 先頭に `## 0.4.0（2026-12-01）` の形の見出しと、利用者から見て変わったこと |

リポジトリの `README.md` のプラグインの一覧にも版があります（テストの対象外）。

設定ファイルや結果フォルダーの形式が変わり、前の版のものが使えなくなるときは、CHANGELOG に移行の手順を書きます。
