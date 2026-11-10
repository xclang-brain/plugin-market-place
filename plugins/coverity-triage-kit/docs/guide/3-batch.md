# ステップ3：まとめてトリアージして、結果を反映する

**所要時間**：最初の3件で30分ほど　**必要なもの**：[ステップ2](2-agent.md) を終えていること（接続設定が済んでいること）

このステップでは、条件に合う警告をまとめてトリアージし、結果を画面で確認して、Coverity とリポジトリに反映します。このプラグインの中心となる使い方です。

流れは「実行する → 確認して選ぶ → 反映する」の3つです。

```text
/coverity-triage-kit:coverity-run     AI が調べる
/coverity-triage-kit:coverity-gui     人が確認して選ぶ
/coverity-triage-kit:coverity-apply   選んだ結果を反映する
```

## 1. 少ない件数で実行する

リポジトリで Copilot CLI を起動し、次のように入力します。

```text
/coverity-triage-kit:coverity-run 3 件だけ
```

AI が Coverity から未分類の警告を3件取得し、1件ずつトリアージします。実行中に、ファイルの書き込みやコマンドの許可を求められたら、「このセッションでは許可」を選んでください。

終わると、次のように報告されます。

```text
トリアージが終わりました。警告3件を処理しました。

- 一覧：C:\work\coverity-triage-out\20261110-101500\summary.html
- 推奨：修正が2件（12345、12350）、逸脱が1件（12347）
- 確信度：低はありません
```

結果は、リポジトリの外の出力先（`coverity-triage-out/<日時>/`）に保存されます。この時点では、リポジトリのファイルも Coverity も変わっていません。

## 2. 結果を確認して、警告ごとに選ぶ

```text
/coverity-triage-kit:coverity-gui
```

ブラウザーで画面が開きます。「結果」タブに、警告の一覧が確信度の低い順に並びます。

| 列 | 内容 |
| --- | --- |
| 承認 | 修正／逸脱／却下／反映しない。最初は AI の推奨が入っています |
| ID | CID とチェッカー |
| 推奨・確信度・判定 | AI の結果 |

行を選ぶと、詳細レポート（根拠、修正案の差分、逸脱コメント案）が表示されます。確認して、警告ごとに「承認」を選びます。

| 選択 | 反映したときに起きること |
| --- | --- |
| 修正 | 修正案のブランチを push し、プルリクエストを用意する |
| 逸脱 | 逸脱コメントと分類を Coverity に登録する |
| 却下 | AI の結果を採用しない（何も反映しない） |
| 反映しない | 今回は保留する |

逸脱コメントの文面は、詳細画面で直してから保存できます。

## 3. 反映する

```text
/coverity-triage-kit:coverity-apply
```

AI が、反映する内容を件数と ID で表示します。

```text
次の内容を反映します。
- 逸脱：1件（12347）→ Coverity に登録
- 修正：2件（12345、12350）→ ブランチ coverity-fix/cid-12345、coverity-fix/cid-12350 を push

反映しますか？
```

「はい」と答えると反映されます。Coverity への書き込みの前には、Copilot CLI の確認がもう一度表示されます。修正については、プルリクエストの作成用 URL が表示されます。

## 4. 件数を増やす、条件を変える

流れが分かったら、対象を広げます。条件は、コマンドの後ろに書くだけです。

```text
/coverity-triage-kit:coverity-run High だけ 20 件
/coverity-triage-kit:coverity-run MISRA 10 番台だけ
/coverity-triage-kit:coverity-run misra.yaml で
```

何も書かなければ、既定の条件（未分類の警告を20件まで）で実行します。毎回使う条件は、条件ファイルとして保存できます。指定できる条件の一覧と、条件ファイルの書き方は [条件の指定](conditions.md) にあります。

## ここまでで分かったこと

- 一括トリアージは「実行する → 確認して選ぶ → 反映する」の順に進めます。
- AI が行うのは提案までです。何を反映するかは、画面で人が選びます。
- 条件は、実行のたびに言葉で指定できます。

## 次のステップ

- 夜間などに無人で実行したい → 途中で許可の確認に止まらないよう、[事前にツールを許可します](#無人で実行する)。
- 画面で進み具合を見たい、設定を画面で変えたい → [ステップ4](4-screen.md)

---

### 補足

- **途中で止まった・止めた場合**：`/coverity-triage-kit:coverity-run 前回の続きから` で再開できます。
- **進み具合を見る**：実行中に `/coverity-triage-kit:coverity-gui` で画面を開くと、「監視」タブで状況が分かります。チャットで「進み具合は？」と聞くこともできます。
- **精度を上げるオプション**：別の AI による結果の検証、同じ原因の警告の統合、修正案のビルド確認を追加できます。`/coverity-triage-kit:coverity-setup` で「検証も使いたい」のように頼むと設定できます。時間は長くなります。
- **プロジェクト固有の知識を教える**：`.coverity-triage/knowledge.md` に「fatal_error() は戻らない」のような事実や方針を書いておくと、AI が調査の前に読みます。
- **svn の場合**：「修正」は、ブランチの push の代わりに、作業コピーへ差分を適用します。コミットは人が行います。

### 無人で実行する

夜間などに無人で実行する場合は、次のように許可を指定して Copilot CLI を起動してから、`/coverity-triage-kit:coverity-run` を実行します。出力先がリポジトリの外にあるため、許可を指定しないと、書き込みのたびに Copilot CLI の許可確認で止まります。

```text
copilot --add-dir "<出力先>" --allow-tool 'shell(uv:*)' --allow-tool write --allow-tool 'coverity-connect(coverity_check_connection)' --allow-tool 'coverity-connect(coverity_search_issues)' --allow-tool 'coverity-connect(coverity_get_issue_details)' --allow-tool 'coverity-connect(coverity_preview_triage)'
```

`<出力先>` の既定は、リポジトリと同じ階層の `coverity-triage-out` です。`/coverity-triage-kit:coverity-run` も、開始前に実際のパスを入れたコマンドを表示します。

> **Coverity に書き込むツール（`coverity_update_triage`）は、事前に許可しないでください。** サーバー名だけの `--allow-tool 'coverity-connect'` と、すべてを許可する `--allow-all-tools` も、書き込むツールまで許可するので使いません。書き込みは `/coverity-triage-kit:coverity-apply` の実行時に、必ず確認が出ます。
