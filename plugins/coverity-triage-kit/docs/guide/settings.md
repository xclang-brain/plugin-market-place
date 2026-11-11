# 設定

取得する条件やオプションを変えるときに読むページです。設定は、リポジトリの `.coverity-triage/config.yaml` の1ファイルです。[準備](../../README.md#準備最初の1回) で作成されたファイルを、エディターで編集します。次の依頼から有効になります。

## 取得する条件を変える

```yaml
conditions:
  checkers: []                    # チェッカー名（* と ? を使用できます）。空のときは、すべて
  impacts: []                     # High / Medium / Low。空のときは、すべて
  classification: [Unclassified]  # 警告の分類（Classification）
  limit: 20                       # 取得する件数の上限
```

| やりたいこと | 書き方 |
| --- | --- |
| Impact が High と Medium の警告だけ | `impacts: [High, Medium]` |
| NULL 参照の警告だけ | `checkers: [NULL_RETURNS, FORWARD_NULL]` |
| MISRA の10番台だけ | `checkers: ["MISRA C-2012 Rule 10.*"]` |
| 保留にした警告も対象にする | `classification: [Unclassified, Pending]` |
| 1回に50件まで処置する | `limit: 50` |

条件を変えた後の依頼では、新しい結果フォルダーを作成して、新しい条件で警告を取得し直します。処置が終わって「OK」と答えていない一覧があるときは、先に「OK」と答えてから、条件を変えてください。

## そのほかの設定

設定ファイルには、キーごとに説明のコメントが付いています。条件のほかに、結果フォルダーの場所（`output_dir`）、3段階目で同時に動かすエージェントの数（`parallel`）、git で処置の基準にし、修正を取り込む先のブランチ（`base_branch`）、下のオプション（`build`）を設定できます。

main 以外のブランチ（例 develop）の最新のコードで処置するときは、設定ファイルの `base_branch: ''` の行を `base_branch: develop` に変えます。空のときは、origin の既定のブランチ（例 main）です。

## オプションを使う

**修正をビルドして確認する**：ビルドのコマンドを書き、オプションを有効にします。すべての修正を適用した状態で、1回ビルドします。ビルドに失敗したときは、エラーが出たファイルを変更する修正（エラーからファイルを特定できないときは、すべての修正）が、一覧で「何もしない」（備考「ビルドに失敗したため」）になります。エラーの内容は、結果フォルダーの `build.log` で確認できます。

```yaml
build:
  command: 'make -j4'
  check_fixes: true
```

**取得の前に解析する**：ビルドのコマンド（`build.command`）を書き、`build.initial_analysis: true` にします。警告を取得する前に、リポジトリのフォルダーでビルド、解析、Coverity への登録を行います。`cov-build`、`cov-analyze`、`cov-commit-defects` が PATH にあることが必要です。

設定を変えた後は、`/coverity-triage-kit:coverity-setup` と入力すると、設定ファイル、認証情報、接続をまとめて確認できます。

## プロジェクトの知識を伝える

`.coverity-triage/knowledge.md` に、「fatal_error() は戻らない」のようなプロジェクトの事実や方針を書いておくと、処置の前に読み込まれます。

## 結果フォルダー

結果は、`output_dir` の下の日時のフォルダー（例 `coverity-triage-out/20261110-153000/`）に保存されます。`summary.md` に全 CID の修正コードと逸脱コメントがまとまっています。CID ごとの差分は `cid/<CID>/fix.patch` です。
