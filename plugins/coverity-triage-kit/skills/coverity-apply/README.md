# 承認の反映のスキル

Coverity の警告のトリアージの実行で、人が選んだ承認（修正・逸脱・却下）を反映するスキルです。

## できること

| 渡すもの | できること |
| --- | --- |
| 実行フォルダ（言わなければ、いちばん新しい実行） | 反映する件数・ID・影響を見せ、同意を得てから反映する |
| 「逸脱」の承認 | Coverity にトリアージを書き戻す道具に、計画のファイル `apply-plan.json` と確認の文字列を渡して書き戻す（道具がなければ、書き戻していないことを伝える） |
| 「修正」の承認（git） | 修正のブランチ（`coverity-fix/cid-<CID>`、グループは `coverity-fix/<実行の日時>-G<n>`）を作って origin に push し、プルリクエストの題名・本文・作成の URL を用意する。GitHub の道具があれば、それでプルリクエストを作る |
| 「修正」の承認（svn） | 作業コピーに差分を当てる。コミットは人がする |
| 「却下」の承認 | 何も書き込まない。グループの CID は、次の実行からグループにしない（`.coverity-triage/no-grouping.yaml` に注釈を残して足す） |

- 同意を得るまで、Coverity にも git にも何も書きません。
- 途中で失敗しても、もう一度頼むと残りだけを反映します。同じものを二重に push したり、書き戻したりしません。
- GitHub のトークンは聞きません。
- オプション（`config.yaml` の `options.knowledge_suggestions`・`options.metrics`）で、知識の追記の案と、推奨が採られた割合の集計もします。

## 必要なもの

- Windows の PC（Linux・macOS でも動きます）
- git か svn のリポジトリと、その `.coverity-triage/config.yaml`
- [uv](https://docs.astral.sh/uv/)
- トリアージの実行が残した実行フォルダ（`run.json`、`summary.html`、`approvals.json`、`items/<ID>/result.json`、`saved.json`、`patches/<ID>-fix.patch`。あれば `items/<ID>/review.json` と `verify.json` も読む）
- 逸脱を書き戻すとき：Coverity にトリアージを書き戻せる道具（なくても修正と却下は反映できます）
- git で push するとき：origin に書き込めること

## 置き方

このフォルダを、そのまま次のどこかにコピーします（フォルダの名前を変えてもかまいません）。

- 作業するリポジトリの `.github/skills/<名前>/`
- 自分の `~/.copilot/skills/<名前>/`（Windows は `%USERPROFILE%\.copilot\skills\<名前>\`）
- Claude Code なら `.claude/skills/<名前>/`

プラグインに入れて配られているときは、コピーは要りません。

## 呼び方

チャットで頼みます。スキルの名前（フォルダの名前）を `/` に続けて打ち、実行フォルダを添えてもかまいません。プラグインから入れたときは `/<プラグインの名前>:<スキルの名前>` でも呼べます。

- 「承認を反映して」
- 「最新の実行を反映して」
- 「修正のプルリクエストを作って」

## 設定

`.coverity-triage/config.yaml` の次の項目を読みます（ほかの項目は使いません）。

| 項目 | 使い方 |
| --- | --- |
| `output_dir` | 実行フォルダを探す場所（既定はリポジトリの隣の `coverity-triage-out`） |
| `vcs.type` / `vcs.base_branch` / `vcs.branch_prefix` | git か svn、プルリクエストの取り込み先、ブランチの名前の頭 |
| `options.per_run_branch` | true なら、承認した修正を1つのブランチ `coverity-fix/run-<実行の日時>` にまとめる |
| `options.knowledge_suggestions` | true なら、反映の後に知識の追記の案を出す |
| `options.metrics` | true なら、反映のたびに、推奨と人の選択を記録する |

## 結果の見方

実行フォルダに、次ができます。

| ファイル | 中身 |
| --- | --- |
| `apply-plan.json` | 反映の計画（逸脱 `triage`、修正 `code`、却下 `reject`）。確認の文字列は、このファイルのバイト列の SHA-256 の先頭12文字 |
| `apply-plan.result.json` | Coverity に書き戻す道具が書く、書き戻しの結果（このスキルは読むだけ） |
| `apply-code.result.json` | このスキルの記録：反映したもの（項目ごと）、確認の文字列ごとの結果、効果の記録 |

スクリプトの結果は JSON 1つです。`apply-code` の主な項目：`pull_requests`（`title`・`body`・`branch`・`base`・`compare_url`）、`pushed_before`、`svn_applied`、`coverity.registered` / `not_registered`、`no_grouping_added`、`errors`。

## うまくいかないとき

| 起きたこと | 直し方 |
| --- | --- |
| 「確認の文字列が、反映の内容と合いません」 | 承認を変えたか、別の反映の計画です。`preview` からやり直します |
| 「修正の差分がないので「修正」は反映できません」 | その作業の承認を「逸脱」か「却下」に選び直します |
| 「別の形で作られたもの」 | 元の道具などで作った実行は反映できません。作った道具で反映するか、新しく実行します |
| 「ブランチ … は、この修正とは別の変更を指しています」 | 同じ名前のブランチが別の変更を指しています。ブランチを消すか名前を変えて、もう一度頼みます |
| push できない | origin があるか、書き込めるか（社内の認証）を確かめます。ブランチは手元にできています。もう一度頼むと push し直します |
| svn で「当てられない所がありました」 | 作業コピーを確かめ、競合を直してからコミットします |
| 逸脱が「まだ Coverity に書き戻していません」 | Coverity にトリアージを書き戻せる道具が使えるときに、もう一度頼みます |

## スクリプトを直接動かす

```
# Windows（PowerShell）
uv run --native-tls --quiet "C:\path\to\this-skill\scripts\apply.py" preview --run "C:\work\coverity-triage-out\20261108-101500"
# Linux（bash）
uv run --native-tls --quiet "/path/to/this-skill/scripts/apply.py" preview --run "/home/me/work/coverity-triage-out/20261108-101500"
```

コマンドは `runs`・`preview`・`apply-code`・`knowledge-candidates`・`add-knowledge`・`stats` です（`--help` で引数が出ます）。

## テストの動かし方

このフォルダで、次を動かします（uv と git が要ります。svn の試験は svn があるときだけ動きます）。

```
# Windows（PowerShell）
py -3 -m unittest discover -s tests
# Linux（bash）
python3 -m unittest discover -s tests
```

`assets/contract/token/cases.json` は、確認の文字列の共通の試験のデータです。同じ文字列を作るほかの部品も、このデータで同じ答えを出します。

プルリクエストの本文と効果の記録の確信度は、人に見せる確信度（作業役の確信度を、結果の確かめの上限とビルドでの検証の問題で下げたもの。一覧と同じ答え）です。決まりは `scripts/confidence.py`（一括実行のスクリプトの写し）にあり、`assets/contract/confidence/cases.json`（その共通の試験のデータの写し）で確かめます。`assets/run.schema.json` と `assets/approvals.schema.json`（実行フォルダの取り決めの写し）があれば、試験の実行フォルダがその形に合うかも確かめます。
