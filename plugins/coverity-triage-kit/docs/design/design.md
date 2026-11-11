# coverity-triage-kit 設計書

このプラグインを変更する開発者に向けて、構成と、その構成にした理由を説明します。何を作るかは [仕様書](../specs/requirements.md)、判断の基準は [憲章](../constitution.md) にあります。

## 1. 構成

処置の順序はエージェントとスキルの文章が持ち、状態はスクリプトが結果フォルダーに保存します。エージェントは、スクリプトの応答に書かれたコマンドを次々に実行するだけなので、会話が途切れても、同じ依頼で続きから再開できます。

```text
利用者 ─ チャット ─ スキル／エージェント ─┬─ triage.py ──┐
                                         ├─ apply.py ───┼─ 結果フォルダー ─ gui.py ─ ブラウザー
                                         └─ MCP サーバー ┘        │
                                                └─ Coverity Connect
```

| 構成要素 | 場所 | 役割 |
| --- | --- | --- |
| MCP サーバー `coverity-connect` | `mcp-servers/coverity-connect/` | Coverity Connect との通信（接続の確認、警告の検索、警告の経路の取得、登録） |
| スキル `coverity-setup` | `skills/coverity-setup/` | 設定ファイルの作成と確認（`setup.py`） |
| スキル `coverity-triage` | `skills/coverity-triage/` | CID 1件の処置の方法（`SKILL.md` と `references/`）と、進行の管理（`triage.py`） |
| スキル `coverity-apply` | `skills/coverity-apply/` | OK の後の、修正の取り込みと、登録する内容の作成（`apply.py`） |
| スキル `coverity-gui` | `skills/coverity-gui/` | ローカルの HTTP サーバーと画面（`gui.py`、`static/`） |
| エージェント4つ | `com.github.copilot/agents/` | `triage-worker`（2段階目、3段階目の処置役）、`triage-orchestrator`（3段階目の進行）、`triage-reviewer`（検証）、`triage-integrator`（統合） |

スキルのフォルダーは、ほかのフォルダーのコードを読み込みません。スキルの間の取り決めは、結果フォルダーのファイルの形だけです。

## 2. 結果フォルダー

`<output_dir>/<年月日-時分秒>/` が、1回の取得とその処置の状態のすべてです。ファイルごとに、書き込む構成要素は1つです。

| ファイル | 書き込む構成要素 | 内容 |
| --- | --- | --- |
| `run.json` | `triage.py` | リポジトリ、git / svn、取り込み先のリビジョン、ストリーム、条件、並列数、検証の有無 |
| `issues.json`、`cid/<CID>/issue.json` | MCP サーバー | 警告の一覧、警告の経路 |
| `cid/<CID>/state.json` | `triage.py` | CID の状態（処置中、処置済み、失敗、差し戻し）、検証の結果、統合の関係 |
| `cid/<CID>/result.json` | 処置役（`save` が Coverity の値を追記） | 判定、根拠、推奨、確信度、逸脱コメント |
| `cid/<CID>/fix.patch` | `triage.py` | 修正コード。`git diff` / `svn diff` の出力のバイト列 |
| `cid/<CID>/review.json` | `triage.py` | 検証の結果と指摘 |
| `summary.md` | `triage.py` | 全 CID の2つの出力 |
| `decisions.json` | `gui.py` | 画面での選択と、編集した逸脱コメント |
| `apply-plan.json`、`applied.json`、`coverity-entries.json` | `apply.py` | 一覧を表示した時点の計画、取り込んだ修正、Coverity に登録する内容 |
| `written.json` | MCP サーバー | Coverity に登録済みの CID |
| `work/` | `triage.py` | 作業用コピー。完了時に削除します |

## 3. 処置の流れ

`triage.py` のコマンドは7つです。どのコマンドも JSON を1つ返し、続きのコマンドの全文（`next`）、先に呼び出す MCP のツール（`fetch`）、利用者に伝える文（`message`）を含みます。

| コマンド | 行うこと |
| --- | --- |
| `start` | 結果フォルダーを決め（続きか、新規か）、作業用コピーを準備し、警告の取得を指示します |
| `next` | 未処置の CID を1件渡します。複数の処置役が同時に呼び出しても、同じ CID は渡しません |
| `save` | 結果の形式と逸脱コメントの規則を確認し、作業用コピーの差分を `fix.patch` として保存します |
| `status`、`merge`、`review` | 3段階目で、統合の候補と検証の対象を示し、統合と検証の結果を記録します |
| `finish` | 未処置や未検証の CID が残っていないことを確認し、`summary.md` を書きます |

- 作業用コピーは、git では取り込み先のブランチの最新のコミットの worktree、svn ではチェックアウトのコピーです。利用者の作業ツリーは編集しません。
- 作業用コピーの準備、修正のビルド確認、最初の解析は、時間がかかるため、切り離したプロセスで実行し、同じコマンドをもう一度呼び出して完了を確認します。
- 2回渡しても保存されなかった CID は、失敗として記録し、一覧に理由を表示します。取得した CID は、すべて `summary.md` に載ります。
- 同じ依頼は、最新の結果に「OK」が出て、その登録が済むまで、同じ結果フォルダーを続けます。その後の依頼は、新しい結果フォルダーを作り、警告を取得し直します。

## 4. OK の後

1. `apply.py plan` が、CID ごとの選択を決めて一覧を作ります。優先順位は、チャットでの指定、画面での選択、推奨の順です。確信度が低い、検証で要確認、ビルドに失敗、のいずれかに当たる CID は「何もしない」が最初の選択です。
2. 利用者が「OK」と答えると、`apply.py apply` が修正を取り込みます。git では、利用者の作業ツリーとは別の一時的な worktree で、ファイルを展開せずに、ブランチ `coverity-fix/<結果フォルダー名>` に修正1件を1コミットとして積み、push します。svn では、作業コピーに `svn patch` で適用します。
3. `apply.py` は、Coverity に登録する内容を `coverity-entries.json` に書き、そのハッシュ値を別のファイルに記録します。MCP のツール `coverity_update_triage` は、ハッシュ値が一致するときだけ登録し、登録した CID を `written.json` に記録します。一覧を表示した後に結果や選択が変わったときは、取り込みも登録も行わず、一覧の確認からやり直します。

取り込んだ修正は、Coverity に「Bug／Fix Required」として登録します。登録しないと、その CID は未分類のまま残り、次の取得でもう一度処置されるためです。

## 5. 画面

`gui.py` は、127.0.0.1 だけで待ち受ける HTTP サーバーです。結果フォルダーを読み取って表示し、書き込むのは `decisions.json` だけです。処置の開始、取り込み、登録は行いません（チャットで依頼する文を表示します）。URL のトークン、`Host` と `Origin` の確認で、ほかのページやほかの PC からの要求を拒否します。

## 6. 設計上の決定

| 決定 | 理由 |
| --- | --- |
| 順序は文章、状態はフォルダー | エージェントの文章を短くでき、中断しても続きから再開できます |
| どの段階も同じコマンドを使う | 1段階目だけの特別な処理をなくし、どの段階の結果も同じ画面と同じ「OK」の手順で扱えます |
| 修正は作業用コピーで作る | 利用者の作業ツリーを OK の前に変更しません。並列の処置役が、互いの修正の影響を受けません |
| 差分はバイト列で保存する | 文字コード（Shift_JIS など）と改行コード（CRLF）を変えません |
| 一覧は1つ、質問は1回 | 利用者が確認する対象と、OK の意味を1つにします |
| Coverity に書き込むツールは事前に許可しない | 書き込みの直前に、Copilot CLI の確認が必ず表示されます |
| テスト用の動作を出荷するコードに入れない | テストは、Coverity Connect の代わりになる HTTP サーバー（`tests/coverity_emulator`）に接続します |

## 7. 開発の方法

- テストは `python3 tests/run.py` で実行します。構成は [tests/README.md](../../tests/README.md) にあります。
- 仕様書の規模の上限は、`tests/test_budget.py` が確認します。
- エージェントの定義は、Copilot CLI 用（`com.github.copilot/agents/*.agent.md`）と、開発用（`agents/*.md`）の2つの形で置きます。本文は同じです。開発用の形は、Claude Code でプラグインを読み込み、実際のモデルで2段階目と3段階目を動かして品質を確認するために使います（`tests/eval/`）。利用者向けの機能と文書は、Copilot CLI だけを対象にします。

## 8. 実機で確認していないこと

このバージョンは、実機の GitHub Copilot CLI と、実機の Coverity Connect では動作を確認していません。確認したのは、モデルを使わないテストと、開発用の形での実際のモデルによる実行です。特に次の点は、実機での確認が必要です。

- `uv run` の `--native-tls` は、現在の uv では非推奨です（後継の `--system-certs` は、古い uv では使用できません）。uv が `--native-tls` を廃止したときは、コマンドの置き換えが必要です。
- Copilot CLI が表示する許可の確認の回数と、[2段階目の手順](../guide/2-agent.md#席を離れて実行する) の起動オプションで確認が表示されなくなること
- サブエージェントの起動と、統括エージェントの委任先の制限（フック）
- 実機の Coverity Connect の API の応答（バージョンによる違い）
- 3段階目が2段階目より早く終わる件数。見本の警告6件では、2段階目が約2分、3段階目が約3〜4分でした（並列の処置は約半分の時間で終わりますが、統合と検証の時間が加わります）
