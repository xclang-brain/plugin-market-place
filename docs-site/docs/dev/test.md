# テスト

## 前提環境

| ソフトウェア | 不足時の動作 |
| --- | --- |
| Python 3.10 以上と PyYAML、uv、git | テスト実行不可 |
| svn（`svn` と `svnadmin`） | svn のテストのスキップ |
| Node.js と Playwright（Chromium） | ブラウザー操作のテストのスキップ |

## 実行方法

プラグインのフォルダー（`plugins/coverity-triage-kit`）で実行します。モデルは使用しません。

```text title="ターミナル"
python3 tests/run.py                              すべて実行（Windows は py -3 tests/run.py）
python3 tests/run.py --list                       スイート名を表示
python3 tests/run.py --only docs --only budget    指定したスイートのみ実行
python3 tests/run.py -v                           合格したスイートの出力も表示
```

スイートごとに結果が1行（○ 合格、× 不合格）表示され、スキップしたテストは理由と件数も表示されます。終了コードは、全スイート合格で 0、それ以外は 1 です。**実行後にリポジトリ内にファイルが残ったスイートは、不合格になります。**

## テストスイート

| スイート | 検査内容 |
| --- | --- |
| `skills/<名前>` | 各スキルのスクリプトの動作。コマンドごとの応答と結果フォルダーの内容を、git と svn の両方で検査。`coverity-gui` は HTTP API とブラウザー操作も検査 |
| `mcp-servers/coverity-connect` | MCP サーバーの4ツール。`mcp.json` のコマンドで起動し、Coverity Connect の代替サーバーに接続して検査 |
| `structure` | マニフェスト、MCP サーバーの登録、スキルとエージェントのファイル、設定キー、MCP ツール |
| `agents` | エージェントの2ファイルの本文の一致、委任先を制限するフック、本文中の応答キーとコマンドが実際の応答に存在すること |
| `stages` | 各段階が、上位の段階なしで動作すること |
| `budget` | 仕様書8章の規模の上限。不合格時は現在値を表示 |
| `docs` | 文書のリンクと見出し、バージョンの一致、禁止語、コマンドの実在、README の認証情報の手順 |
| `flow` | 1〜4段階それぞれの、準備から OK の後の取り込みと登録までの一連の流れ。エージェントの代わりにスクリプトが、応答どおりにコマンドを実行 |

テストは、Coverity Connect の代替となる HTTP サーバー（`tests/coverity_emulator/`）に接続します。配布するコードにテスト用の分岐を入れないためです。サンプルのコードと警告は `tests/fixtures/sample/` にあります。

## CI

`.github/workflows/coverity-triage-kit.yml` で、Windows と Linux、Python 3.10 と最新版の組み合わせで `python tests/run.py` を実行します。Windows では、次の3件がスキップされます。

- `skills/coverity-gui`：ブラウザーがコマンドの出力を引き継がないことの確認
- `skills/coverity-apply`：svn が表示できないファイル名の確認
- `skills/coverity-apply`：git が 2.32 より古い場合のメッセージの確認

CI の Windows は日本語ロケールではないため、svn のテストではフォルダー名とファイル名を英数字にしています。日本語ロケールの Windows における、svn と日本語のファイル名の組み合わせは、CI では検証していません。

## 実際のモデルでの評価（EVAL）

実際のモデルがエージェントとスキルの本文どおりに動作するか、出力の品質が十分かは、上記のテストでは検証できません。本文やモデルを変更した場合は、EVAL を手動で実行してください。モデルの利用料が発生するため、`tests/run.py` と CI では実行しません。

```text title="ターミナル"
python3 tests/eval/run_eval.py --stage 1 --vcs git
python3 tests/eval/run_eval.py --stage 2 --vcs git --answer-ok
python3 tests/eval/run_eval.py --stage 3 --vcs svn --answer-ok
```

開発用の定義（`agents/*.md`、`.claude-plugin/plugin.json`、`.mcp.json`）を使って claude CLI でプラグインを実行し、結果を採点します。

| 段階 | 所要時間 | 費用の目安 |
| --- | --- | --- |
| 1段階目（5つの依頼） | 約2分 | 約0.03〜0.04ドル |
| 2段階目 | 約2分 | 約0.05ドル |
| 3段階目 | 約3〜4分 | 約0.6〜0.8ドル |

いずれもサンプルの警告での実測値です。評価項目とオプションは [tests/eval/README.md](gh:tests/eval/README.md) を参照してください。
