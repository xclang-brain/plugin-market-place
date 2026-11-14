# テスト

## 実行する

プラグインのフォルダー（`plugins/coverity-triage-kit`）で実行します。モデルは使いません。

```text title="ターミナル"
python3 tests/run.py                              すべて実行（Windows は py -3 tests/run.py）
python3 tests/run.py --list                       スイートの名前を表示
python3 tests/run.py --only docs --only budget    指定したスイートだけ実行
python3 tests/run.py -v                           合格したスイートの出力も表示
```

スイートごとに1行（○ 合格、× 不合格）が表示され、スキップしたテストは理由と件数が出ます。終了コードは、すべて合格で 0、それ以外は 1 です。**リポジトリの中にファイルを残したスイートは、不合格になります。**

## スイート

| 名前 | 確認すること |
| --- | --- |
| `skills/<名前>` | スキルのスクリプトの動作。コマンドごとの応答と結果フォルダーのファイルを、git と svn の両方で。`coverity-gui` は HTTP の API とブラウザーでの操作も |
| `mcp-servers/coverity-connect` | MCP サーバーの4つのツール。`mcp.json` のコマンドで起動し、Coverity Connect の代わりのサーバーに接続します |
| `structure` | マニフェスト、MCP サーバーの登録、スキルとエージェントのファイル、設定キー、MCP のツール |
| `agents` | エージェントの2つの形の本文が同じこと、依頼先を制限するフック、本文に書いた応答のキーとコマンドが実際の応答にあること |
| `stages` | どの段階も、上の段階なしで使えること |
| `budget` | 仕様書8章の規模の上限。不合格のときは、現在の値を表示します |
| `docs` | 文書のリンクと見出し、版の一致、使わない言葉、コマンドの存在、README の認証情報の手順 |
| `flow` | 1〜4段階の、準備から OK の後の取り込みと登録までの流れ。エージェントの代わりのスクリプトが、応答のとおりにコマンドを実行します |

Coverity Connect の代わりは、`tests/coverity_emulator/` の HTTP サーバーです。サンプルのコードと警告は `tests/fixtures/sample/` にあります。

## CI

`.github/workflows/coverity-triage-kit.yml` が、Windows と Linux で、Python 3.10 と最新版を使って `python tests/run.py` を実行します。Windows では、次の3件がスキップされます。

- `skills/coverity-gui`：ブラウザーがコマンドの出力を引き継がないことの確認
- `skills/coverity-apply`：svn が表示できないファイル名の確認
- `skills/coverity-apply`：git が 2.32 より古いときのメッセージの確認

CI の Windows は日本語の設定ではないため、svn のテストはフォルダーとファイルの名前を英数字にして実行します。日本語の設定の Windows での svn と日本語の名前の組み合わせは、CI では確認していません。

## 実際のモデルで確認する（EVAL）

エージェントとスキルの文に、実際のモデルが従えるかと、出力の質は、上のテストでは分かりません。文やモデルを変えたときは、EVAL を手動で実行します。モデルの利用料がかかるため、`tests/run.py` と CI では実行しません。

```text title="ターミナル"
python3 tests/eval/run_eval.py --stage 1 --vcs git
python3 tests/eval/run_eval.py --stage 2 --vcs git --answer-ok
python3 tests/eval/run_eval.py --stage 3 --vcs svn --answer-ok
```

開発用の形のプラグイン（`agents/*.md`、`.claude-plugin/plugin.json`、`.mcp.json`）を claude CLI で実行し、結果を採点します。

| 段階 | 時間 | 費用の目安 |
| --- | --- | --- |
| 1段階目（5つの依頼） | 約2分 | 約0.03〜0.04ドル |
| 2段階目 | 約2分 | 約0.05ドル |
| 3段階目 | 約3〜4分 | 約0.6〜0.8ドル |

見本の警告での実測です。確認する内容とオプションは [tests/eval/README.md](gh:tests/eval/README.md) にあります。
