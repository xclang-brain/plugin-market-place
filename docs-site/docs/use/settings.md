# 設定

設定は、リポジトリの `.coverity-triage/config.yaml` の1ファイルです。[準備](setup.md)で作られたファイルをエディターで編集すると、次の依頼から使われます。

## よく使う設定

| したいこと | 書き方 |
| --- | --- |
| Impact が High と Medium の警告だけにする | `impacts: [High, Medium]` |
| NULL 参照の警告だけにする | `checkers: [NULL_RETURNS, FORWARD_NULL]` |
| MISRA の Rule 10 系だけにする | `checkers: ["MISRA C-2012 Rule 10.*"]` |
| 保留にした警告も取得する | `classification: [Unclassified, Pending]` |
| 1回に50件まで処置する | `limit: 50` |
| いつも develop ブランチのコードで処置する（git） | `base_branch: develop` |
| 3段階目で同時に動かすエージェントを増やす | `parallel: 5`（1〜8） |

`checkers` には `*` と `?` を使えます。変えた後は、`/coverity-triage-kit:coverity-setup` と入力すると、設定と接続をまとめて確認できます。

!!! warning "条件を変える前に、残っている一覧に答える"
    条件を変えた後の依頼では、新しい結果フォルダーで警告を取得し直します。処置が終わって OK を出していない一覧があるときは、先に答えておきます。

## どのブランチのコードで処置するか（git）

`base_branch` が空のときは、チェックアウトしているブランチの最新のコード（origin にあるブランチは origin の最新）で処置し、そのブランチへのプルリクエストを用意します。ブランチを切り替えた後の依頼は、新しい結果フォルダーで警告を取得し直します。

## オプション

**修正をビルドして確認する**

```yaml
build:
  command: 'make -j4'
  check_fixes: true
```

すべての修正を適用した状態で、1回ビルドします。失敗したときは、エラーが出たファイルを変更する修正が「保留にする」（備考「ビルドに失敗したため」）になります。エラーからファイルを特定できないときは、すべての修正が対象です。エラーの内容は、結果フォルダーの `build.log` にあります。

**取得の前に解析する**

`build.command` を書き、`build.initial_analysis: true` にします。警告を取得する前に、リポジトリのフォルダーでビルド、解析、Coverity への登録を行います。`cov-build`、`cov-analyze`、`cov-commit-defects` が PATH にあることが必要です。解析の引数は `build.analyze_args`（最初は `--all`）です。

## プロジェクトの知識を伝える

`.coverity-triage/knowledge.md` に、コードだけでは分からない事実や方針を書くと、処置の前に読み込まれます。

```markdown title=".coverity-triage/knowledge.md の例"
- fatal_error() は戻らない（リセットを待つ無限ループ）
- 公開 API の引数 ctx は、呼び出し元が NULL でないことを保証する
- MISRA の Advisory の規則は、逸脱にする
```

推奨の方針や逸脱コメントの書き方を書いたときは、プラグインの基準より優先されます。

## 結果フォルダー

結果は `output_dir` の下の日時のフォルダー（例 `coverity-triage-out/20261110-153000/`）に保存されます。`output_dir` は、リポジトリの外を指定します。

| ファイル | 内容 |
| --- | --- |
| `summary.md` | 全 CID の判定、根拠、修正コード、逸脱コメント |
| `cid/<CID>/fix.patch` | CID ごとの修正コード（差分） |
| `cid/<CID>/result.json` | CID ごとの判定、根拠、逸脱コメント |

## 設定ファイルの全体

??? example "準備で作られる設定ファイル"

    <!-- ctk:config-template -->
