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
件数は、組の記録の最後の `Ran N tests` の行とその後の行から数えます（試験の中で動かした別の試験の記録は数えません）。
終了コードは、0 がすべて合格（要る道具がなくて飛ばしたものはあってよい）、1 が不合格あり、2 が要る道具なしです。
結果は `tests/results/latest.json` にも書きます（git に入れません）。

### 選んで動かす

| オプション | 意味 |
| --- | --- |
| `--list` | 試験の組の名前を出す |
| `--only <名前>` | その組だけを動かす。何回でも付けられる。`parts` は部品ごとの試験すべて。部品は `skills/<名前>` か、フォルダの名前でも選べる |
| `--skip-slow` | 時間のかかる試験を飛ばす（部品をコピーして動かす独立性の試験、部品を動かす取り決めの試験、結合の試験、画面の試験） |
| `--jobs <数>` | 部品ごとの試験を同時にいくつ動かすか（既定は CPU の数。4 まで） |
| `--timeout <秒>` | 1つの組の時間の上限（既定は 900 秒。独立性の試験は 3600 秒） |
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
| Node.js | 22 以上 | エクステンションの試験を動かせない（`--only extension` のときなど。終了コード 2）。`ui` は飛ばす |
| svn、cppcheck | （あれば使う） | それを使う試験だけを飛ばす |
| Playwright（Chromium） | （あれば使う） | 画面をブラウザーで確かめる試験（`extension` の一部と `ui`）だけを飛ばす。入れるとき：`npm install --prefix tests --no-save --no-package-lock playwright@1.56.1` と `npx --prefix tests playwright install chromium` |

要る道具は、選んだ組の分だけを確かめます（`--only docs` なら Python だけ）。

### 試験の組

| 名前 | 置き場所 | 確かめること |
| --- | --- | --- |
| 部品ごと | `skills/<名前>/tests/`、`mcp-servers/<名前>/tests/` | スクリプトとツールの振る舞い。部品のフォルダだけでも動く |
| `structure` | `tests/structure/` | `plugin.json`・`mcp.json` を公式の schema で。SKILL.md の frontmatter・長さ・指すファイル・目次、`evals/` の形。エージェントの定義の形 |
| `contracts` | `tests/contracts/` | 設計書 4.17 の写し（データ・持ち主のスクリプト・共通の試験のデータ）が、持ち主と同じバイト列。写しは部品の中をファイルの名前で探す（まだない写しは理由を書いて飛ばす）。写したスクリプトを写す側が使い、要るライブラリを持つ。`defaults.json` と統括エージェントの表と役割の言葉が合う。MCP サーバーが偽の Coverity で書いたファイルを、一括実行と真偽の調査のスクリプトが受け付ける。確認の文字列（計画のファイルは MCP サーバーのツールで、entries は確認の文字列の関数で） |
| `independence` | `tests/independence/` | 1段階目の部品に、ほかの部品の名前・プラグインの名前・外を指すパスがない（`output_dir` の既定の値 `../coverity-triage-out` だけは許す。英語の言葉は単語として数え、`uv.lock` は見ない）。部品のフォルダだけをコピーして（スキルは名前を変えたコピーも）試験が通る。エージェントは名前を表にだけ書く。2段階目と3段階目の組が名前で閉じている。上の段の文書に下の段の中身がない。一括実行が実際に書いた指示ファイル（作業・確かめ・作り直し・統合）と頼みの文に、部品の名前がない |
| `integration` | `tests/integration/` | MCP サーバーと模擬の Coverity Connect（`tests/coverity_emulator/`）の結合。一括のトリアージを、モデルを使わずに最後まで流す（`test_flow.py`：模擬の Coverity の5件の警告、MCP サーバーでの取得、決まった振る舞いの偽のエージェント、一覧、反映の確認の文字列、書き戻し、修正のブランチの push） |
| `ui` | `tests/ui/` | 結果と設定の画面を、Playwright の Chromium で（幅 390 px と 1280 px、ライトとダーク）。タブが出る、横にはみ出さない、ダークで読める、一覧で選んだ承認と詳細で直した逸脱コメントが `approvals.json` に保存され、反映がそれを読む、サーバを止められる |
| `docs` | `tests/docs/` | 版の一致、要求の番号の追跡、仕様書と設計書に部品の名前がない、`docs/replace.md` の表が今の部品と合う、README の並び、リンク |
| `eval` | `tests/eval/` | EVAL の道具の、モデルを使わない試験 |
| `extension` | `tests/extension/` | エクステンション（偽の SDK と偽のコマンド、画面） |

`structure`・`contracts`・`independence`・`integration`・`ui`・`docs` は、部品の名前を書いていません。部品は、フォルダ・`mcp.json`・`defaults.json`・「使う部品」の表・[部品の変え方](../docs/replace.md) の「今の部品」の表から読みます。
まだない部品は、理由を書いて飛ばします。

### モデルを使わずに流す一括のトリアージ（`tests/integration/flowlib.py`）

`integration` の `test_flow.py`、`independence` の指示ファイルの試験、`ui` は、同じ道具を使います。

- 小さな C のリポジトリ（git、`main`）と、5件の警告：同じ関数の2件（グループになる）、意図的な1件（逸脱）、
  チェッカーは違うが同じ修正で直る2件（統合役がまとめる）。何が正しいかも道具が持っています。
- 偽のエージェント：指示ファイルの表から、直すフォルダ・結果・確かめの結果・計画のファイルを読み、書かれたとおりに書きます。
  作業役は、作業用のコピーで本当にコードを直します。検証役は、評価基準をすべて1回ずつ書きます（1件は一度差し戻します）。
- 統括エージェントの代わり：`tick` の `actions` のとおりに、警告の取得を MCP サーバー（`mcp.json` の起動のコマンド）か、
  用意したファイル（`new-run --issues-file`）で行い、`launch` を偽のエージェントに渡し、`--finished` で知らせます。
- 部品は、`defaults.json`（`runCommand`、`guiCommand`、`applySkill`）と `mcp.json` と、スキルの SKILL.md が動かすよう書いた
  スクリプトから見つけます。uv・git・模擬の Coverity・部品がないときは、理由を書いて飛ばします。

### 部品の試験の動かし方の決まり

`tests/run.py` と独立性の試験は、部品の試験を、部品のフォルダで次のように動かします。部品の README にも同じ動かし方を書きます。

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

- そのフォルダだけを別の場所にコピーしても通る（プラグインの `tests/` やほかの部品を読まない）。スキルは、フォルダの名前を変えて（SKILL.md の `name` も変えて）コピーしても通る。
- リポジトリの中にファイルを残さない（一時フォルダを使う）。`tests/run.py` は `PYTHONDONTWRITEBYTECODE=1` を付け、uv の環境（`UV_PROJECT_ENVIRONMENT`）を一時フォルダにする。試験の後にリポジトリに残ったもの（`__pycache__`、`.venv` など）があれば、「後片付け」として不合格にする。
- 速く終わる（1つの部品で 90 秒ほどまで）。時間のかかる試験は、環境変数 `SKIP_SLOW_TESTS=1` のときに飛ばす。

試験に渡る環境変数：

| 変数 | 意味 |
| --- | --- |
| `SKIP_SLOW_TESTS` | `1` なら時間のかかる試験を飛ばす（`--skip-slow`） |
| `TESTS_JOBS` | 同時に動かす数（`--jobs`） |
| `TESTS_PART_TIMEOUT` | 独立性の試験で、コピーした部品の試験の時間の上限（秒。既定 900） |

### CI

リポジトリの `.github/workflows/coverity-triage-kit.yml` が、`plugins/coverity-triage-kit/` が変わったときに、同じ試験を Windows と Linux で動かします。
部品の名前は書いていないので、部品を足す・消す・置き換えても、直さずに済みます。

| ジョブ | 動かすもの |
| --- | --- |
| 部品ごとの試験 | `tests/run.py --only parts`（Python 3.12 と最新） |
| プラグイン全体の試験 | `structure`、`contracts`、`independence`、`integration`、`docs`、`eval`。最初に、`tests/run.py --list` のどの組も、どれかのジョブで動くことを確かめる（Linux だけ） |
| 結果と設定の画面 | `ui`（Node 22。Playwright と Chromium を入れ、あることを確かめてから動かす） |
| Web管理画面 | `extension`（Node 22 と 24） |

`tests/` に試験の組を足したら、どれかのジョブの `--only` に足します。足し忘れると、プラグイン全体の試験のジョブが不合格になります。

## EVAL（実際のモデル）

実際のモデルで課題を解かせて採点します（仕様 REQ-602）。AI クレジットを使うので、CI では動かしません。結果は `tests/eval/REPORT.md` にまとめます。

| 段階 | 課題の置き場所 | 動かし方 |
| --- | --- | --- |
| 1（スキル） | `skills/<名前>/evals/<課題>/`（`prompt.md`、`case.yaml`、`graders/`、`files/`、`place-files.sh`） | skill-creator の手順で、スキルありとなしを比べて採点する。Claude Code では、スキルのフォルダで `claude plugin eval . --allow-tools Write,Edit,Bash --scaffold` でも動く |
| 1（MCP サーバー） | `mcp-servers/<名前>/evals/`（mcp-builder の形の `evaluation.xml` と偽の Coverity のデータ） | その `evals/README.md` のとおり |
| 2〜4 | `tests/eval/` | [EVAL の道具](eval/README.md) のとおり。`claude_adapter.py` が、Copilot CLI の部品を Claude Code の形に置き換えて動かす（設計書 9.3） |

- 作業の場所は、リポジトリの外にします。
- Claude Code では確かめられないこと（Copilot CLI の許可の確認、エクステンション、サブエージェントの ask_user など）は、設計書 9.4 の表で、Copilot CLI で手で確かめ、版と日付を書きます。
- 部品を変えたら、その部品の EVAL を動かし直します。
