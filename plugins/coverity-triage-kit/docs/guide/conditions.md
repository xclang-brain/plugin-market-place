# 条件の指定

トリアージの対象にする警告と、実行時の動作を指定する方法をまとめたリファレンスです。[ステップ3](3-batch.md) を終えてから読むことを想定しています。

指定の方法は3つあります。上から順に試し、必要になったら次の方法に進むのがおすすめです。

| 方法 | 向いている場面 | 操作 |
| --- | --- | --- |
| 1. 実行時に言葉で指定する | その場かぎりの条件 | `/coverity-triage-kit:coverity-run` の後ろに書く |
| 2. 条件ファイルに保存する | 毎回使う条件、複雑な条件 | 画面で編集する、または YAML を直接書く |
| 3. 設定を変える | 既定の動作そのものを変えたい | `/coverity-triage-kit:coverity-setup` で頼む、または画面で編集する |

## 1. 実行時に言葉で指定する

`/coverity-triage-kit:coverity-run` の後ろに、条件を日本語で書きます。組み合わせることもできます。

```text
/coverity-triage-kit:coverity-run High と Medium だけ、NULL_RETURNS だけ、10 件
```

| 絞り込む対象 | 書き方の例 |
| --- | --- |
| 件数 | 「10 件だけ」「最大 30 件」 |
| Impact | 「High だけ」「High と Medium」 |
| チェッカー | 「NULL_RETURNS だけ」「MISRA 10 番台」 |
| 分類（Classification） | 「分類が Pending のもの」 |
| 対応（Action） | 「対応が Undecided のもの」 |
| 状態（Status） | 「状態が New のもの」 |
| 条件ファイル | 「misra.yaml で」 |

実行時の動作も、同じように指定できます。

| 動作 | 書き方の例 |
| --- | --- |
| 別の AI に結果を検証させる | 「検証役も使って」 |
| 同じ原因の警告を1つにまとめる | 「同じ原因はまとめて」 |
| 修正案をビルドして確認する | 「ビルドして確認して」 |
| 最初にビルド・解析して Coverity に登録する | 「最初に解析して」 |

## 2. 条件ファイルに保存する

毎回使う条件は、リポジトリの `.coverity-triage/filters/` に YAML ファイルとして保存します。`/coverity-triage-kit:coverity-setup` を実行すると、次の2つが最初から用意されます。

| ファイル | 内容 |
| --- | --- |
| `untriaged.yaml` | 未分類の警告を20件まで（何も指定しないときの既定） |
| `all.yaml` | すべての警告を100件まで |

### 画面で作る・編集する

`/coverity-triage-kit:coverity-gui` で画面を開き、「設定」タブの「条件」で、条件ファイルの追加と編集ができます。保存の前に、変更内容が一覧で表示されます。

### YAML を直接書く

`untriaged.yaml` の中身は次のとおりです。これをコピーして書き換えるのが簡単です。

```yaml
name: 未分類の警告（20 件まで）
project: App
streams: [app-main]
checkers: []            # 空なら、すべてのチェッカー
impacts: []             # 空なら、すべての Impact
triage:
  classification: [Unclassified]
  action: []
  status: []
limit: 20
```

例：MISRA の10番台のうち、Impact が High か Medium で、まだ対応が決まっていない警告を50件まで。

```yaml
name: MISRA 10番台（High・Medium）
project: App
streams: [app-main]
checkers: ["MISRA C-2012 Rule 10.*"]
impacts: [High, Medium]
triage:
  classification: [Unclassified, Pending]
  action: [Undecided]
  status: []
limit: 50
```

これを `.coverity-triage/filters/misra10.yaml` として保存すると、次のように使えます。

```text
/coverity-triage-kit:coverity-run misra10.yaml で
```

| 項目 | 書き方 |
| --- | --- |
| `streams` | ストリームを1つ |
| `checkers` | チェッカー名。`*` と `?` が使えます。複数書くと、どれかに一致する警告が対象になります |
| `impacts` | `High`・`Medium`・`Low`・`Unspecified` |
| `triage` | Coverity のトリアージ欄の値（分類・対応・状態） |
| `limit` | 件数の上限 |

条件ファイルに書いた条件と、実行時に言葉で指定した条件は、組み合わせて使えます（例：`/coverity-triage-kit:coverity-run misra10.yaml で 5 件だけ`）。

## 3. 設定を変える

既定の動作は、リポジトリの `.coverity-triage/config.yaml` で決まります。変更の方法は2つです。

- **対話で変える**：`/coverity-triage-kit:coverity-setup` を実行し、「1回の件数の上限を 50 にして」「検証を既定で使うようにして」のように頼みます。変更内容を表で確認してから保存されます。
- **画面で変える**：`/coverity-triage-kit:coverity-gui` で開く画面の「設定」タブで編集します。

主な設定：

| 設定 | 既定 |
| --- | --- |
| 結果の出力先（リポジトリの外） | リポジトリと同じ階層の `coverity-triage-out` |
| 1回の実行で扱う件数の上限 | 100 |
| 同時に調べる件数（1〜8） | 3 |
| 別の AI に結果を検証させる | 使わない |
| 同じ原因の警告を1つにまとめる | 使わない |
| 修正案をビルドして確認する | 使わない |

設定ファイルの項目名と、すべての項目の説明は、`config.yaml` の中のコメントに書いてあります。
