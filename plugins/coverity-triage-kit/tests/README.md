# テスト

coverity-triage-kit を変更する開発者に向けて、テストの実行方法と、スイートごとの確認内容を説明します。

## 実行する

プラグインのフォルダーで実行します。モデルは使用しません。すべて実行して、4分ほどです。

```text
python3 tests/run.py                              すべて実行します（Windows は py -3 tests/run.py）
python3 tests/run.py --list                       スイートの名前を表示します
python3 tests/run.py --only docs --only budget    指定したスイートだけを実行します
python3 tests/run.py -v                           合格したスイートの出力も表示します
```

スイートごとに、結果が1行表示されます（○ は合格、× は不合格）。スキップしたテストは、理由と件数が表示されます。不合格のスイートは、テストの出力も表示されます。終了コードは、すべて合格したときが 0、それ以外が 1 です。リポジトリの中にファイルを残したスイートは、不合格になります。

| 必要なもの | ないとき |
| --- | --- |
| Python 3.10 以上と PyYAML（`python -m pip install pyyaml`）、uv、git | 実行できません |
| svn（`svn` と `svnadmin`） | svn のテストをスキップします |
| Node.js と Playwright（Chromium） | 画面をブラウザーで操作するテスト（1件）をスキップします |

Playwright は、リポジトリの外にインストールして、環境変数 `NODE_PATH` で指定します。コマンドは、CI の定義（リポジトリの `.github/workflows/coverity-triage-kit.yml`）にあります。

## スイート

| 名前 | 場所 | 確認する内容 |
| --- | --- | --- |
| `skills/<名前>` | `skills/<名前>/tests/` | スキルのスクリプトの動作。コマンドごとの応答と結果フォルダーのファイルを、git と svn の両方で確認します。`coverity-gui` は、HTTP の API と、ブラウザーでの操作も確認します |
| `mcp-servers/coverity-connect` | `mcp-servers/coverity-connect/tests/` | MCP サーバーのツール4つ。`mcp.json` のコマンドで起動し、Coverity Connect の代わりの HTTP サーバー（`tests/coverity_emulator/`）に接続します |
| `structure` | `tests/test_structure.py` | 2つのマニフェストの一致、MCP サーバーの登録、スキル4つの frontmatter、エージェント4つのファイル、設定キー13個、MCP のツール4つ |
| `agents` | `tests/test_agents.py` | エージェントの2つの形（Copilot CLI 用と開発用）の本文が同じであること、frontmatter、依頼先を制限するフック（その PC にある bash と PowerShell で実行します）、3つの依頼文。本文に書かれた応答の項目とコマンドが、`triage.py` の実際の応答（git と svn）にあること |
| `stages` | `tests/test_stages.py` | どの段階も、上の段階なしで使えること。下の段階のファイルに上の段階の構成要素の名前がないことと、スキルのコードがほかのスキルのフォルダーを使用しないことを確認します |
| `budget` | `tests/test_budget.py` | 仕様書8章の規模の上限。不合格のときは、現在の値を表示します |
| `docs` | `tests/test_docs.py` | 文書のリンクと見出し、バージョンの一致、使用しない言葉、文書にあるコマンドのスキルとエージェントが存在すること、README の認証情報の設定手順が `setup.py` の表示と同じであること |
| `flow` | `tests/flow/` | 1〜4段階の、準備から取得、処置、「OK」の後の取り込みと登録までの流れ。エージェントの代わりのスクリプトが、実際の MCP サーバー（接続先はエミュレーター）と、`triage.py` の応答のとおりにコマンドを実行します。git と svn の両方で確認します |

`tests/fixtures/sample/` は、テストと EVAL が使用するサンプルです（C のコード、エミュレーターが返す警告、想定する結論）。

## モデルを使用する確認（EVAL）

実際のモデルが、エージェントとスキルの文に従って警告を最後まで処置できることと、出力の質は、上のテストでは確認できません。EVAL で確認します。モデルの利用料がかかるため、`tests/run.py` と CI では実行しません。手順は [tests/eval/README.md](eval/README.md) にあります。

## CI

`.github/workflows/coverity-triage-kit.yml` が、Windows と Linux のそれぞれで、Python 3.10 と最新版を使用して `python tests/run.py` を実行します。svn と Playwright をインストールするので、Linux ではスキップはありません。Windows では、次の3件をスキップします。

- `skills/coverity-gui`：ブラウザーがコマンドの出力を引き継がないことの確認（Windows では、出力を引き継がない方法でブラウザーを開くため）
- `skills/coverity-apply`：svn が表示できないファイル名の確認（svn の出力を、シェルスクリプトで置き換えるため）
- `skills/coverity-apply`：git が 2.32 より古いときのメッセージの確認（git の出力を、シェルスクリプトで置き換えるため）

CI の Windows は日本語の設定ではないため、svn に日本語の名前を渡せません。svn のテストは、その場合だけ、フォルダーとファイルの名前を英数字にして実行します。日本語の設定の Windows での svn と日本語の名前の組み合わせは、CI では確認していません。
