# 試験の手引き

coverity-triage-kit の試験は2つあります。

| 試験 | 何で動かすか | 費用 | CI |
| --- | --- | --- | --- |
| モデルを使わない試験 | `tests/run.py` | なし | 動かす（Windows と Linux） |
| EVAL（実際のモデルで課題を解かせて採点する） | skill-creator、`tests/eval/` の道具 | AI クレジットを使う | 動かさない |

## モデルを使わない試験

プラグインのフォルダで動かします（仕様 REQ-601）。

```text
# Windows（PowerShell）
py -3 tests/run.py
# Linux（bash）
python3 tests/run.py
```

終わると、試験の組ごとに「○ 合格」「△ 飛ばしたものがある」「× 不合格（理由）」が出ます。
件数は、組の記録の最後の `Ran N tests` の行とその後の行から数えます（試験の中で動かした別の試験の記録は数えません）。不合格は試験の数で数えます（1つの試験の中で、いくつもの場合（subTest）が不合格でも1件）。
終了コードは、0 がすべて合格（要る道具がなくて飛ばしたものはあってよい）、1 が不合格あり、2 が要る道具なしです。
結果は `tests/results/latest.json` にも書きます（git に入れません）。

### 選んで動かす

| オプション | 意味 |
| --- | --- |
| `--list` | 試験の組の名前を出す |
| `--only <名前>` | その組だけを動かす。何回でも付けられる。`parts` は部品ごとの試験すべて。部品は `skills/<名前>` か、フォルダの名前でも選べる |
| `--skip-slow` | 時間のかかる試験を飛ばす（`integration`、`flow`、`ui`） |
| `--jobs <数>` | 部品ごとの試験を同時にいくつ動かすか（既定は CPU の数。4 まで） |
| `--timeout <秒>` | 1つの組の時間の上限（既定は 900 秒） |
| `--results <ファイル>` | 結果の JSON の置き場所 |
| `-v` | すべての記録を出す（既定では、不合格の組の記録の終わりの 80 行だけ） |

例：文書と構造の試験だけを動かす。

```text
# Windows（PowerShell）
py -3 tests/run.py --only docs --only structure
# Linux（bash）
python3 tests/run.py --only docs --only structure
```

### 要る道具

| 道具 | 版 | なければ |
| --- | --- | --- |
| Python | 3.10 以上 | 動かせない（終了コード 2） |
| uv | （あること） | 部品の試験を動かせない（終了コード 2） |
| git | （あること） | 部品の試験を動かせない（終了コード 2） |
| svn（`svn` と `svnadmin`） | （あれば使う） | svn を使うテストだけをスキップします（CI では、`flow` の svn の分が不合格になります） |
| PyYAML、jsonschema（Python のパッケージ） | （あること） | `structure` と `flow`（PyYAML）、`integration` と `ui`（jsonschema）が不合格になります。インストール：`python -m pip install pyyaml jsonschema` |
| Node.js、Playwright（Chromium） | （あれば使う） | 画面をブラウザーで確認するテスト（`ui`）だけをスキップします（CI では不合格になります）。インストール：`npm install --prefix tests --no-save --no-package-lock playwright@1.56.1` と `npx --prefix tests playwright install chromium` |

要る道具は、選んだ組の分だけを確かめます（`--only docs` なら Python だけ）。

### 試験の組

| 名前 | 置き場所 | 確かめること |
| --- | --- | --- |
| 部品ごと | `skills/<名前>/tests/`、`mcp-servers/<名前>/tests/` | スクリプトとツールの振る舞い。部品のフォルダだけでも動く |
| `structure` | `tests/structure/` | マニフェスト（`plugin.json`、`.claude-plugin/plugin.json`、`mcp.json`、`.mcp.json`）が正しく、互いに一致する。バージョンが、2つのマニフェスト、リポジトリの `.github/plugin/marketplace.json`、`CHANGELOG.md` で同じ。すべての `skills/<名前>/SKILL.md` の frontmatter（`name` はフォルダー名と同じ、`description` がある、使えるキーは `name`・`description`・`allowed-tools`・`disable-model-invocation`）。すべてのエージェントの frontmatter。Claude Code のエージェント（`agents/<名前>.md`）と Copilot CLI のエージェント（`com.github.copilot/agents/<名前>.agent.md`）が、同じ名前、同じ見出し、同じコードブロックで、ツールの呼び名と `tools` だけが対応するものに置き換わっている |
| `integration` | `tests/integration/` | MCP サーバーと模擬の Coverity Connect（`tests/coverity_emulator/`）の結合。一括のトリアージを、モデルを使わずに最後まで流す（`test_flow.py`：模擬の Coverity の5件の警告、MCP サーバーでの取得、決まった振る舞いの偽のエージェント、一覧、反映の確認の文字列、書き戻し、修正のブランチの push） |
| `flow` | `tests/flow/` | `skills/coverity-triage` の処置の流れを、エージェントの代わりのスクリプトで、git と svn の両方で実行します。スクリプトは、`triage.py` が `next` に出力するコマンドをそのまま実行し、作業用コピーのファイルを修正して `result.json` を書きます。リポジトリと警告の一覧は `tests/fixtures/sample/` です。確認する内容：CID 1件の処置（1段階目）、全件の処置と `summary.md`（2段階目）、中断した後の再開、CRLF と Shift_JIS のファイルの修正が新しいチェックアウトに適用できること、逸脱コメントのない結果を `save` が受け付けないこと |
| `ui` | `tests/ui/` | 結果と設定の画面を、Playwright の Chromium で（幅 390 px と 1280 px、ライトとダーク）。タブが出る、横にはみ出さない、ダークで読める、一覧で選んだ承認と詳細で直した逸脱コメントが `approvals.json` に保存され、反映がそれを読む、サーバを止められる |
| `docs` | `tests/docs/` | `README.md` と `docs/guide/*.md` の相対リンクが、実在するファイルと見出しを指す |
| `eval` | `tests/eval/` | EVAL の道具の、モデルを使わない試験 |

### モデルを使わずに流す一括のトリアージ（`tests/integration/flowlib.py`）

`integration` の `test_flow.py` と `ui` は、同じ `flowlib.py` を使います。

- 小さな C のリポジトリ（git、`main`）と、5件の警告：同じ関数の2件（グループになる）、意図的な1件（逸脱）、
  チェッカーは違うが同じ修正で直る2件（統合役がまとめる）。何が正しいかも道具が持っています。
- 偽のエージェント：指示ファイルの表から、直すフォルダ・結果・確かめの結果・計画のファイルを読み、書かれたとおりに書きます。
  作業役は、作業用のコピーで本当にコードを直します。検証役は、評価基準をすべて1回ずつ書きます（1件は一度差し戻します）。
- 統括エージェントの代わり：`tick` の `actions` のとおりに、警告の取得を MCP サーバー（`mcp.json` の起動のコマンド）か、
  用意したファイル（`new-run --issues-file`）で行い、`launch` を偽のエージェントに渡し、`--finished` で知らせます。
- スクリプトのパスは `flowlib.py` に書いてあります。MCP サーバーは、`mcp.json` の `coverity-connect` のコマンドで起動します。
  uv か git がないときは、理由を表示してスキップします（CI では不合格になります）。スクリプトや MCP サーバーがないときは、不合格になります。

### 部品の試験の動かし方の決まり

`tests/run.py` は、部品の試験を、部品のフォルダで次のように動かします。

- フォルダに `pyproject.toml` があるとき（MCP サーバー）：uv のプロジェクトとして動かす。

  ```text
  uv run --native-tls --frozen --quiet --directory "<部品のフォルダ>" python -m unittest discover -s tests -v
  ```

- ないとき（スキル）：

  ```text
  # Windows（PowerShell）
  py -3 -m unittest discover -s tests -v
  # Linux（bash）
  python3 -m unittest discover -s tests -v
  ```

部品の試験が守ること：

- リポジトリの中にファイルを残さない（一時フォルダを使う）。`tests/run.py` は `PYTHONDONTWRITEBYTECODE=1` を付け、uv の環境（`UV_PROJECT_ENVIRONMENT`）を一時フォルダにする。試験の後にリポジトリに残ったもの（`__pycache__`、`.venv` など）があれば、「後片付け」として不合格にする。
- 速く終わる（1つの部品で 90 秒ほどまで）。時間のかかる試験は、環境変数 `SKIP_SLOW_TESTS=1` のときに飛ばす。

試験に渡る環境変数：

| 変数 | 意味 |
| --- | --- |
| `SKIP_SLOW_TESTS` | `1` なら時間のかかる試験を飛ばす（`--skip-slow`） |
| `TESTS_JOBS` | 同時に動かす数（`--jobs`） |

### CI

リポジトリの `.github/workflows/coverity-triage-kit.yml` が、`plugins/coverity-triage-kit/` が変わったときに、同じ試験を Windows と Linux で動かします。
部品の名前は書いていないので、部品の構成が変わっても、直さずに済みます。

| ジョブ | 動かすもの |
| --- | --- |
| 部品ごとの試験 | `tests/run.py --only parts`（Python 3.10、3.12、最新。Subversion をインストールしてから実行する） |
| プラグイン全体の試験 | `structure`、`integration`、`flow`、`docs`、`eval`（Subversion をインストールしてから実行する）。最初に、`tests/run.py --list` のどの組も、どれかのジョブで動くことを確かめる（Linux だけ） |
| 結果と設定の画面 | `ui`（Node 22。Playwright と Chromium を入れ、あることを確かめてから動かす） |

`tests/` に試験の組を足したら、どれかのジョブの `--only` に足します。足し忘れると、プラグイン全体の試験のジョブが不合格になります。

## EVAL（実際のモデル）

実際のモデルで課題を解かせて採点します（仕様 REQ-602）。AI クレジットを使うので、CI では動かしません。結果は `tests/eval/REPORT.md` にまとめます。

| 段階 | 課題の置き場所 | 動かし方 |
| --- | --- | --- |
| 1（スキル） | `skills/<名前>/evals/<課題>/`（`prompt.md`、`case.yaml`、`graders/`、`files/`、`place-files.sh`） | skill-creator の手順で、スキルありとなしを比べて採点する。Claude Code では、スキルのフォルダで `claude plugin eval . --allow-tools Write,Edit,Bash --scaffold` でも動く |
| 1（MCP サーバー） | `mcp-servers/<名前>/evals/`（mcp-builder の形の `evaluation.xml` と偽の Coverity のデータ） | その `evals/README.md` のとおり |
| 2〜3 | `tests/eval/` | [EVAL の道具](eval/README.md) のとおり。`claude_adapter.py` が、Copilot CLI の部品を Claude Code の形に置き換えて動かす（設計書 9.3） |

- 作業の場所は、リポジトリの外にします。
- Claude Code では確かめられないこと（Copilot CLI の許可の確認、サブエージェントの ask_user など）は、設計書 9.4 の表で、Copilot CLI で手で確かめ、版と日付を書きます。
- 部品を変えたら、その部品の EVAL を動かし直します。
