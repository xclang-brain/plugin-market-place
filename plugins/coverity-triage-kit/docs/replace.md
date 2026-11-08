# 部品を変える（開発者向け）

coverity-triage-kit の部品（スキル、MCP サーバー、エージェント、エクステンション）を、足す・消す・置き換えるときの手順です。
利用者は部品を置き換えません（新しい版を入れ直すだけです）。部品を変えるのは、プラグインの設計の変更です。

[仕様書](specs/requirements.md) と [設計書](design/design.md) は、部品を役割で呼びます。
役割と今の部品の名前の対応は、この文書の「今の部品」の表にだけあります。取り決めの試験（`tests/contracts/`）も、この表で役割から今の名前を読みます。

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
| 画面の入口 | 4 | `coverity-triage-screen` | `skills/coverity-triage-screen/` |
| 困ったとき | 4 | `coverity-help` | `skills/coverity-help/` |
| 自己診断 | 4 | `coverity-selftest` | `skills/coverity-selftest/` |
| エクステンション | 4 | `coverity-triage-kit` | `com.github.copilot/extensions/coverity-triage-kit/` |

- 1段階目の部品は、ほかの部品の名前を書きません。エージェントの `<名前>.README.markdown` は、その定義と一緒に変えます。
- MCP サーバーの登録名（`coverity-connect`）とツールの名前（`coverity_…`）、エクステンションのツールの名前（`coverity-triage-kit_open`）は、取り決めの名前です（設計書の 5章、8.1）。

## 名前を書いている所

部品の名前を書くのは、次の所だけです。試験と CI は、部品の名前をここから読むので、直す必要はありません。

| 所 | 書くもの |
| --- | --- |
| `com.github.copilot/agents/<名前>.agent.md` の「## 使う部品」の表 | そのエージェントが使う部品（役割・部品・使うとき） |
| `com.github.copilot/agents/<名前>.agent.md` の frontmatter の `tools` | 表にある MCP サーバーのツール（`<登録名>/<ツール>`） |
| `skills/<プラグイン全体のスキル>/SKILL.md` の「## 使う部品」の表 | 画面の入口・困ったとき・自己診断が使う部品 |
| `com.github.copilot/extensions/coverity-triage-kit/defaults.json` | エージェントの id、一括実行と画面のコマンドの場所、反映と準備のスキル、プラグイン全体のスキル |
| `mcp.json` | MCP サーバーの登録名とフォルダ |
| この文書の「今の部品」の表 | 役割と今の名前 |
| 仕様書の 3.3 の表、プラグインの README、4段階目のガイド、エージェントの README | 利用者が打つコマンド（スキルの名前）と、選ぶエージェント |

## スキルを足す

例：役割「規約の確かめ」のスキル `style-check` を足し、作業役が直した後に使う。

1. `skills/style-check/` に、skill-creator の手順でスキルを作ります（`SKILL.md`、`README.md`、`tests/`、`evals/`。要れば `scripts/`、`references/`、`assets/`）。決まりは仕様書の REQ-101 と設計書の 3.1 にあります。ほかの部品の名前は書きません。
2. 使うエージェントの表に1行を足します。例：`com.github.copilot/agents/triage-worker.agent.md` の「## 使う部品」に、次の行を足します。

   ```text
   | 規約の確かめ | `style-check` | 直した後 |
   ```

   表の上から順に毎回使うものは、「使うとき」を「毎回」にし、使う順の位置に入れます。
3. この文書の「今の部品」の表に行を足します。
4. 利用者が打つコマンドとして使うなら、仕様書の 3.3 の表にも足します（仕様の変更です）。
5. ほかの部品と同じ取り決めを持つなら、持ち主のファイルを、下の「取り決めの写し」の置き場所（データは `assets/`、スクリプトは `scripts/`、共通の試験のデータは `assets/contract/<名前>/`）にそのままコピーし、共通の試験のデータのすべての組を動かす試験を `tests/` に足します。設計書の 4.17 の表、下の「取り決めの写し」の表、`tests/contracts/test_contracts.py` の `CONTRACTS` に、その部品（役割）を足します。

## スキルを消す

例：`checker-knowledge` を消す。

1. `skills/checker-knowledge/` を消します。
2. その名前を書いた表の行を、すべて消します。探すときは、プラグインのフォルダで次を動かします。

   ```text
   git grep -n "checker-knowledge" -- com.github.copilot skills/*/SKILL.md
   ```

3. `defaults.json` に名前があれば消します（`applySkill`、`setupSkill`、`pluginSkills`、`runCommand`、`guiCommand`）。
4. この文書の「今の部品」の表から行を消します。
5. 取り決めの持ち主だったときは、ほかの部品を持ち主にし、設計書の 4.17 の表と、この文書の表を直します。写しを持っていただけのときは、設計書の 4.17 の表、下の「取り決めの写し」の表、`tests/contracts/test_contracts.py` の `CONTRACTS` から、その部品（役割）を外します。

## スキルを置き換える

例：`code-fix` を、同じ役割の `minimal-fix` に置き換える。

1. `skills/code-fix/` を消し、`skills/minimal-fix/` を置きます。
2. その名前を書いた表で、`` `code-fix` `` を `` `minimal-fix` `` にします（いまは作業役の表）。
3. `defaults.json` に名前があれば替えます。一括実行を置き換えるときは `runCommand`、結果と設定の画面を置き換えるときは `guiCommand` の場所も替えます。
4. この文書の「今の部品」の表の名前と置き場所を替えます。
5. 古いスキルが取り決めの写しを持っていたら（下の「取り決めの写し」の表で、古いスキルのフォルダが出てくる行）、新しいスキルにも同じ写しを同じ置き場所に置き、写したスクリプトが使うライブラリを PEP 723 の欄に書き、共通の試験のデータのすべての組を動かす試験も持たせます。持ち主だったら、新しいスキルが持ち主になり、ほかの部品の写しは新しい持ち主のものと同じにします。
6. 利用者が打つコマンドの名前が変わるときは、仕様書の 3.3 の表、プラグインの README、4段階目のガイド、エージェントの README も替えます。

## MCP サーバーを置き換える

登録名 `coverity-connect` とツールの名前は、エージェントとの取り決めです（設計書の 5章）。新しい MCP サーバーも、同じ登録名と同じツールの名前・引数にします。

1. `mcp-servers/<新しい名前>/` を置き、古いフォルダを消します（`README.md`、`pyproject.toml`、`uv.lock`、`src/`、`tests/`、`evals/`）。
2. `mcp.json` の `--directory` を `${PLUGIN_ROOT}/mcp-servers/<新しい名前>` にし、起動のコマンドの名前を替えます。登録名の鍵（`coverity-connect`）と `env` は、そのままにします。
3. 確認の文字列の写しを置きます（設計書の 4.10、4.17。書き戻しのツールは、反映が書いた計画のファイルの確認の文字列を、反映と同じ決まりで確かめます）。
   - 持ち主のスクリプト `skills/coverity-apply/scripts/plantoken.py` を、新しいサーバーのパッケージ（`src/<パッケージ>/plantoken.py`）にそのままコピーします。書き戻しのツール（`coverity_preview_triage`、`coverity_update_triage`）は、それを読み込んで使います（同じ決まりを書き直しません）。標準ライブラリだけを使うので、`pyproject.toml` に足すライブラリはありません。
   - 共通の試験のデータ `skills/coverity-apply/assets/contract/token/cases.json` を、`mcp-servers/<新しい名前>/tests/contract/token/cases.json` にコピーし、そのすべての組を、写した `plantoken.py` と書き戻しのツールで動かす試験を `tests/` に置きます。

   ```text
   # Windows（PowerShell）
   Copy-Item "skills/coverity-apply/scripts/plantoken.py" "mcp-servers/<新しい名前>/src/<パッケージ>/plantoken.py"
   Copy-Item "skills/coverity-apply/assets/contract/token/cases.json" "mcp-servers/<新しい名前>/tests/contract/token/cases.json"
   # Linux（bash）
   cp "skills/coverity-apply/scripts/plantoken.py" "mcp-servers/<新しい名前>/src/<パッケージ>/plantoken.py"
   cp "skills/coverity-apply/assets/contract/token/cases.json" "mcp-servers/<新しい名前>/tests/contract/token/cases.json"
   ```

4. 警告の一覧 `issues.json` と警告経路 `issue-<CID>.json` は、MCP サーバーが持ち主の取り決めです（設計書の 4.3、4.4、4.17）。写すファイルはありませんが、新しいサーバーも同じ形で書きます。一括実行と真偽の調査がそれを読めることは、取り決めの試験が確かめます。
5. Windows では、認証情報をユーザーの環境変数（`HKCU\Environment`）からも読みます（設計書の 3.2。写しではなく、サーバーが自分で持ちます）。
6. この文書の「今の部品」の表の置き場所を替えます。
7. 登録名を変えるときは（取り決めの変更）、`coverity-connect` を書いた表の行、エージェントの `tools` の `coverity-connect/<ツール>`、設計書の 5章、利用者向けの文書も替えます。

## 作業役のエージェントを置き換える

例：`triage-worker` を `triage-worker-b` に置き換える。

1. `com.github.copilot/agents/triage-worker.agent.md` と `triage-worker.README.markdown` を消し、`triage-worker-b.agent.md` と `triage-worker-b.README.markdown` を置きます（`agents/` の `.md` は、Copilot CLI がすべてエージェントとして読むので、使い方は `.README.markdown` にします）。
2. `defaults.json` の `"worker": "coverity-triage-kit:triage-worker"` を `"worker": "coverity-triage-kit:triage-worker-b"` にします。
3. 統括エージェントの表（`triage-orchestrator.agent.md` の「## 使う部品」）の作業役の行の名前を `triage-worker-b` にします。
4. 自己診断のスキルの表に名前があれば替えます。この文書の「今の部品」の表も替えます。

新しい作業役が守ること（設計書の 3.3、4.13、7章）：

- `description` に「作業役」を入れる。ほかの役割の言葉（検証役・統合役）を入れない。
- 部品の名前は「## 使う部品」の表（役割・部品・使うとき）にだけ書く。自分の名前を書かない。`tools` の MCP のツールは、表の MCP サーバーのものだけ。
- ask_user が使えないとき（ほかのエージェントからの頼み）は、質問も合否の判断もせず、頼みの文とそれが指すファイルの決まりに従い、頼みの文が求める形で返す。

エクステンションは `defaults.json` から名前を読むので、直しません。

## ほかのエージェントを置き換える

- 検証役・統合役：作業役と同じ手順です。`defaults.json` の `roles.reviewer`・`roles.integrator` と、統括エージェントの表の行を替えます。`description` には「検証役」「統合役」を入れます。
- 統括エージェント：定義と README を置き換え、`defaults.json` の `orchestrator` を替えます。統括エージェントには `tools` を書きません（設計書の 3.3）。表には、役割ごとのエージェント（`roles`）と、`defaults.json` のスキル（一括実行・結果と設定の画面・反映・準備）を入れます。手順は一括実行のスキルの SKILL.md にあるので、定義に写しません。

## プラグイン全体のスキルを足す・置き換える

1. `skills/<名前>/` を置きます。部品の名前は、SKILL.md の「## 使う部品」の表（役割・部品・使うとき）にだけ書きます。
2. `defaults.json` の `pluginSkills` に名前を足します（置き換えるときは替えます）。1段階目の部品と区別するのに使います。
3. この文書の「今の部品」の表を直します。

## 取り決めの写し

同じ取り決めを2つ以上の部品が持つときは、持ち主のファイルが正です（設計書の 4.17）。写しは、持ち主のファイルと**同じバイト列**にします（書き直さず、そのままコピーします）。持ち主を変えたら、すべての写しを持ち主のものに合わせてコピーします。

写しの置き場所は、持ち主と同じ名前で、データは写す側の `assets/`、スクリプトは写す側の `scripts/`、共通の試験のデータは写す側の `assets/contract/<名前>/` です。MCP サーバーは、スクリプトをパッケージ（今は `src/coverity_mcp/`）に、共通の試験のデータを `tests/contract/<名前>/` に置きます。自分の部品の `assets/` を探すスクリプト（`check_result.py`、`check_review.py`、`check_plan.py`）は、`scripts/` の直下に置き、探すファイルも `assets/` に置きます。写す側は、写したスクリプトを読み込んで使います（同じ決まりを自分で書き直しません）。写したスクリプトが使うライブラリ（`jsonschema`、`pyyaml` など）は、写す側のスクリプトの PEP 723 の欄（MCP サーバーは `pyproject.toml`）にも書きます。

| 取り決め | 持ち主のファイル | 写し |
| --- | --- | --- |
| 結果の形 | `skills/triage-report/assets/result.schema.json` | `skills/coverity-run/assets/`、`skills/triage-integration/assets/`（`plan.schema.json` と比べるため）、`skills/coverity-gui/assets/` |
| 確かめの形と評価基準 | `skills/triage-review/assets/review.schema.json`、`criteria.json` | `skills/coverity-run/assets/` |
| 統合の計画の形 | `skills/triage-integration/assets/plan.schema.json` | `skills/coverity-run/assets/` |
| 逸脱コメントの決まり | `skills/deviation-comment/scripts/check_comment.py`、`assets/contract/comment/cases.json` | `skills/triage-report/`、`skills/triage-integration/`、`skills/coverity-run/` の `scripts/` と `assets/contract/comment/` |
| 結果の決まり | `skills/triage-report/scripts/check_result.py`、`assets/contract/result/cases.json` | `skills/coverity-run/` の `scripts/` と `assets/contract/result/` |
| 確かめの決まり | `skills/triage-review/scripts/check_review.py` | `skills/coverity-run/scripts/` |
| 機械的な確かめ | `skills/triage-review/scripts/lint_result.py`、`assets/contract/lint/cases.json` | `skills/coverity-run/` の `scripts/` と `assets/contract/lint/` |
| 統合の計画の決まり | `skills/triage-integration/scripts/check_plan.py`、`assets/contract/plan/cases.json` | `skills/coverity-run/` の `scripts/` と `assets/contract/plan/` |
| 人に見せる確信度 | `skills/coverity-run/scripts/confidence.py`、`assets/contract/confidence/cases.json` | `skills/coverity-apply/`、`skills/coverity-gui/` の `scripts/` と `assets/contract/confidence/` |
| 注釈を残す書き換え | `skills/coverity-setup/scripts/yamledit.py`、`assets/contract/yaml-edit/cases.json` | `skills/coverity-gui/` の `scripts/`（その下のフォルダでもよい）と `assets/contract/yaml-edit/` |
| 確認の文字列 | `skills/coverity-apply/scripts/plantoken.py`、`assets/contract/token/cases.json` | `mcp-servers/coverity-connect/src/coverity_mcp/plantoken.py`、`mcp-servers/coverity-connect/tests/contract/token/cases.json` |
| 実行フォルダの形 | `skills/coverity-run/assets/run.schema.json` | `skills/coverity-apply/assets/`、`skills/coverity-gui/assets/`、`skills/coverity-selftest/assets/`、`skills/coverity-help/assets/` |
| 承認の形 | `skills/coverity-run/assets/approvals.schema.json` | `skills/coverity-apply/assets/`、`skills/coverity-gui/assets/`、`skills/coverity-selftest/assets/` |
| 警告の一覧と警告経路の形（`issues.json`、`issue-<CID>.json`） | `mcp-servers/coverity-connect/`（設計書の 4.3、4.4 の形で書く） | 写すファイルはない。一括実行と真偽の調査が読むだけ（読めることを取り決めの試験が確かめる） |

Windows のユーザーの環境変数（`HKCU\Environment`）の読み方は、写しではありません。`mcp-servers/coverity-connect/`、`skills/coverity-setup/`、`skills/coverity-run/`、`skills/coverity-gui/`、`skills/coverity-help/` が、それぞれ自分で持ちます（設計書の 4.17）。これらを置き換えるときは、新しい部品も自分で持ち、その試験と Windows の CI で確かめます。

コピーのしかた（例：逸脱コメントの決まりを、一括実行に写す）：

```text
# Windows（PowerShell）
Copy-Item "skills/deviation-comment/scripts/check_comment.py" "skills/coverity-run/scripts/check_comment.py"
Copy-Item "skills/deviation-comment/assets/contract/comment/cases.json" "skills/coverity-run/assets/contract/comment/cases.json"
# Linux（bash）
cp "skills/deviation-comment/scripts/check_comment.py" "skills/coverity-run/scripts/check_comment.py"
cp "skills/deviation-comment/assets/contract/comment/cases.json" "skills/coverity-run/assets/contract/comment/cases.json"
```

コピーした後は、取り決めの試験で、写しが同じであることを確かめます。

```text
# Windows（PowerShell）
py -3 tests/run.py --only contracts
# Linux（bash）
python3 tests/run.py --only contracts
```

取り決めの試験は、部品のフォルダの中を**ファイルの名前**で探し、同じ名前のファイルをすべて写しとして確かめます。表にない部品が同じ名前のファイルを持つと、不合格になります（設計書の 4.17 の表に足すか、名前を変えます）。まだ写しを持たない部品は、理由を書いて飛ばします。

新しい取り決めを足すときは、設計書の 4.17 の表に行を足し、`tests/contracts/test_contracts.py` の `CONTRACTS` に、持ち主のフォルダの中のパスと、持ち主と写しを持つ部品の役割（この文書の「今の部品」の表の役割の名前）を足します。部品の名前は書きません。

## 変えた後に確かめる

プラグインのフォルダで、モデルを使わない試験をすべて動かします。

```text
# Windows（PowerShell）
py -3 tests/run.py
# Linux（bash）
python3 tests/run.py
```

- 独立性の試験（`tests/independence/`）：表の外に名前がないこと、1段階目の部品がほかの部品を知らないこと、部品のフォルダだけをコピーして（スキルは名前を変えて）試験が通ること、2段階目と3段階目の組が名前で閉じていること。
- 取り決めの試験（`tests/contracts/`）：写しが同じであること、`defaults.json` と統括エージェントの表と役割の言葉が合うこと、書く部品の出力を読む部品が受け付けること。
- 文書の試験（`tests/docs/`）：この文書の「今の部品」の表が、今ある部品と合うこと。

変えた部品の EVAL（実際のモデルの試験）も、動かし直します。動かし方は [試験の手引き](../tests/README.md) にあります。
