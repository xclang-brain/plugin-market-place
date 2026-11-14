# 設定

設定は、リポジトリの `.coverity-triage/config.yaml` の1ファイルです。[準備](setup.md)で作られたファイルをエディターで編集すると、次の依頼から使われます。

## 主な設定例

| 目的 | 記述例 |
| --- | --- |
| Impact を High と Medium に限定 | `impacts: [High, Medium]` |
| NULL 参照の警告に限定 | `checkers: [NULL_RETURNS, FORWARD_NULL]` |
| MISRA の Rule 10 系に限定 | `checkers: ["MISRA C-2012 Rule 10.*"]` |
| 保留にした警告も取得 | `classification: [Unclassified, Pending]` |
| 1回あたりの件数を50件に変更 | `limit: 50` |
| 常に develop ブランチのコードで処置（git） | `base_branch: develop` |
| 複数のエージェントの同時実行数の変更 | `parallel: 5`（1〜8） |

`checkers` には `*` と `?` を使えます。変えた後に `/coverity-triage-kit:coverity-setup` と入力すると、設定と接続をまとめて確認できます。

!!! warning "条件の変更時の注意"
    条件を変えた後の依頼では、新しい結果フォルダーを作って警告を取得し直します。OK を出していない一覧があるときは、先に答えておきます。

## 処置対象のブランチ（git）

`base_branch` が空のときは、チェックアウトしているブランチの最新のコード（origin にあるブランチは origin の最新）で処置し、そのブランチへのプルリクエストを用意します。ブランチを切り替えた後の依頼では、新しい結果フォルダーで警告を取得し直します。

## オプション

### 修正のビルド確認

```yaml
build:
  command: 'make -j4'
  check_fixes: true
```

すべての修正を適用した状態で、1回ビルドします。失敗したときは、エラーが出たファイルを変更する修正（ファイルを特定できないときは、すべての修正）が「保留にする」になり、備考に「ビルドに失敗したため」と出ます。エラーの内容は、結果フォルダーの `build.log` にあります。

### 取得前の解析

`build.command` を書き、`build.initial_analysis: true` にします。警告を取得する前に、リポジトリのフォルダーでビルド、解析、Coverity への登録を行います。`cov-build`、`cov-analyze`、`cov-commit-defects` に PATH が通っている必要があります。cov-analyze に渡す引数は `build.analyze_args`（初期値 `--all`）です。

## プロジェクト固有の知識

`.coverity-triage/knowledge.md` に、コードだけでは分からない事実や方針を書いておくと、処置の前に読み込まれます。

```markdown title=".coverity-triage/knowledge.md の例"
- fatal_error() は戻らない（リセットを待つ無限ループ）
- 公開 API の引数 ctx は、呼び出し元が NULL でないことを保証する
- MISRA の Advisory の規則は、逸脱にする
```

推奨の方針や逸脱コメントの書き方を書いた場合は、プラグインの基準より優先されます。

## 結果の保存先

結果は、`output_dir` の下の日時のフォルダー（例：`coverity-triage-out/20261110-153000/`）に保存されます。`output_dir` には、リポジトリの外を指定します。

| ファイル | 内容 |
| --- | --- |
| `summary.md` | 全 CID の判定、根拠、修正コード、逸脱コメント |
| `cid/<CID>/fix.patch` | CID ごとの修正コード（差分） |
| `cid/<CID>/result.json` | CID ごとの判定、根拠、逸脱コメント |

## 設定ファイルの全体

??? example "設定ファイルの初期内容"

    <!-- ctk:config-template -->
