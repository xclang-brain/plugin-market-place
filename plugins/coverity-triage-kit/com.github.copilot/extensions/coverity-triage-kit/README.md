# Web管理画面のエクステンション（4段階目）

Copilot CLI のエクステンションです。チャットで頼むと Web管理画面を開き、画面の「開始する」で一括のトリアージ（3段階目）を外から動かし、実行の様子と結果を見せます。
使い方（利用者向け）は、プラグインの `docs/guide/4-screen.md` にあります。この文書は、作りと試験のしかたを書きます。

## 目次

- [ほかの部品とのつながり](#ほかの部品とのつながり)
- [ファイル](#ファイル)
- [流れ](#流れ)
- [守り](#守り)
- [記録](#記録)
- [試験](#試験)

## ほかの部品とのつながり

- 部品の名前とコマンドの場所は `defaults.json` にだけあります。コードは、その値をただの文字として使います。

  | 項目 | 使い方 |
  | --- | --- |
  | `orchestrator`、`roles.worker`・`roles.reviewer`・`roles.integrator` | 統括エージェントの起動、役割ごとのモデルの上書き、枠の役割の見分け（id の全体か `:` の後ろ） |
  | `runCommand` | 一括実行のコマンド（プラグインのフォルダからの相対パス）。`settings`・`runs`・`new-run`・`status`・`summary`・`stop` を使う |
  | `guiCommand` | 結果と設定の画面のコマンド。`open --no-browser` でリンクを作る |
  | `setupSkill`・`applySkill` | 画面の案内の文に出すコマンドの名前 |
  | `pluginSkills` | プラグイン全体のスキルの名前（試験が使う） |
  | `port` | 画面のいつもの番号 |
  | `issueRepo` | Issue の起票先（`OWNER/REPO`） |
  | `coverityReadTools` | 統括エージェントに許す、Coverity から読むツールの名前 |

- コマンドの出力は、設計書 4.12（一括実行）と 4.16（結果と設定の画面）の形で読みます。知らない項目は無視します。スキルのファイルは読みません。
- 統括エージェントへの頼みの文のひな形は `core/messages.mjs` にだけあります（設計書 4.15）。
- 追加の npm パッケージは使いません。`@github/copilot-sdk` は Copilot CLI が用意します。

## ファイル

| ファイル | 中身 |
| --- | --- |
| `extension.mjs` | 入口。SDK の `joinSession` をつなぎ口に渡すだけ |
| `adapter.mjs` | Copilot CLI に依る部分のすべて：ツールの登録、フック、許可の求めの受け口、イベントの変換、窓口（`session.rpc`） |
| `core/index.mjs` | 中心の処理。画面を開くツール、状態、開始・止める・起票の窓口 |
| `core/lifecycle.mjs` | 開始、統括エージェントの始め直し、止める、終わり（終わった理由、一覧、リンク） |
| `core/poller.mjs` | 実行の様子の読み取り（重ねない、2 秒／5 秒、10 秒で打ち切り） |
| `core/events.mjs` | イベント → エージェントの木、枠、確認の待ち |
| `core/guard.mjs` | ツールの前の守り（onPreToolUse） |
| `core/commands.mjs`・`core/process.mjs` | コマンドの呼び出し（`uv run --native-tls --quiet`、`PYTHONIOENCODING=utf-8`）と、時間切れのときのプロセスの木の停止 |
| `core/request.mjs` | 頼みの文から画面の最初の値を拾う |
| `core/ending.mjs` | 終わった理由の決め方 |
| `core/analysis.mjs` | 記録から機械的に課題を見つける |
| `core/filing.mjs` | gh での Issue の起票 |
| `core/store.mjs`・`core/recovery.mjs` | 記録と、読み込み直したときの立ち直り |
| `core/messages.mjs`・`core/constants.mjs` | 人とモデルに見せる文、数の決まり |
| `ui/server.mjs`・`ui/app.mjs` | 画面のサーバーと API |
| `ui/static/` | 画面（`index.html`、`render.js`、`app.js`、`style.css`） |

## 流れ

1. 画面を開くツール（`<プラグインの名前>_open`）が呼ばれると、サーバーを起こし（127.0.0.1、いつもの番号が使われていれば空いている番号）、URL と注意の文を返す。設定の今の値は、一括実行の `settings` から読む。実行中なら、今の URL と「いまの実行が終わるまで…」を返す。
2. 画面で開始すると：役割ごとのモデルと思考の深さをこの会話の中だけ上書き → `new-run`（画面の条件、`--review`）→ 統括エージェントをバックグラウンドで起動。
3. 実行中は `status` を読み、枠・フェーズ・作業ごとの様子・検証の結果を出す。
4. 統括エージェントが、実行が終わる前に止まったら、続きから2回まで始め直す。
5. 終わったら `summary` と画面のリンクを作り、終わった理由・件数・一覧・課題を出す。上書きを外す。

## 守り

- 止めた実行のエージェント（木のすべて）のツールを拒む。
- この実行の統括エージェントには、読む・探すツール、エージェントのツール、skill、Coverity から読むツール、`defaults.json` のコマンドを `uv run` で1つずつ動かすシェルだけを許す。
- 検証役のほかのエージェントが `items/<ID>/review.json` を書くのを拒む。
- メインの会話のエージェントと、この実行の外のエージェントは拒まない。フックは例外を投げない。
- 許可の求めには答えない（`no-result`）。画面には「チャットで確認を待っています」と出す。

## 記録

`<COPILOT_HOME または ~/.copilot>/plugin-data/<プラグインの名前>/runs/<日時>-<id>/` に `log.jsonl`、`result.json`、`alive`、`issues/` を置く。20 件まで残す。

## 試験

プラグインのフォルダで動かします。Python・uv・Copilot CLI は要りません（偽の SDK と偽のコマンドで動かします）。画面の試験には Playwright（`require('playwright')`）を使い、ないときはその試験だけを飛ばします。

```text
# Windows（PowerShell）
node --test "tests/extension/*.test.mjs"
# Linux（bash）
node --test "tests/extension/*.test.mjs"
```
