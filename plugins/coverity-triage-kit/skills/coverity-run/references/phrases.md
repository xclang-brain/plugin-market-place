# 頼みの言い方と new-run の引数

頼みの文の言い方を、`new-run` の引数にする対応です。いくつ組み合わせてもかまいません。
どれにも当たらない言い方（「速く」「丁寧に」など）は使わず、「頼みの文の「…」は使いません」と短く伝えます。

| 頼みの言い方（例） | 引数 |
|---|---|
| 「misra.yaml で」「条件は misra」 | `--filter misra.yaml`（`.coverity-triage/filters/` の名前。パスでもよい） |
| 「High だけ」「Impact が High と Medium」 | `--impact High`（値ごとに繰り返す：`--impact High --impact Medium`） |
| 「Unspecified だけ」 | `--impact Unspecified` |
| 「10 件だけ」「最大 30 件」 | `--limit 10`（1 から設定 `max_items` まで） |
| 「NULL_RETURNS だけ」「MISRA 10 番台」 | `--checker NULL_RETURNS`、`--checker "MISRA C-2012 Rule 10.*"`（`*` `?` が使える） |
| 「分類が Pending のもの」 | `--classification Pending` |
| 「対応が Undecided のもの」 | `--action Undecided` |
| 「状態が New のもの」 | `--status New` |
| 「最初に解析して」「解析してから」「サーバに登録してから」 | `--initial` |
| 「解析はしないで」「解析なしで」 | `--no-initial` |
| 「ビルドして確かめて」 | `--verify build` |
| 「再解析まで確かめて」「解析でも確かめて」 | `--verify build+analyze` |
| 「確かめもして」「検証役も使って」 | `--review` |
| 「確かめはいらない」 | `--no-review` |
| 「同じ原因はまとめて」 | `--integration` |
| 「作り直しは 2 回まで」 | `--max-rework 2`（0 から 2） |

- 言い方がなければ、設定（`.coverity-triage/config.yaml`）のとおりです。条件ファイルの既定は `untriaged.yaml`。
- 値は大文字・小文字を問いません（「high だけ」でも `--impact High` になります）。
- 取ってある警告の一覧を使う（Coverity と通信できない）ときは `--issues-file "<issues.json の形のファイル>"`。
  このときは初回解析はできません。

## そのまま使う文

| とき | 文 |
|---|---|
| 途中の実行があるとき | 前回の実行（日時・条件）が途中です。続きから再開しますか？ |
| 初回解析をするとき | 最初に、開いているフォルダをビルド・解析して、Coverity サーバに登録します（数十分かかることがあります） |
| 設定がないとき | .coverity-triage/config.yaml を用意してください（準備の手順があれば、それで作れます） |
| 役のエージェントがないとき | 説明に「作業役」（または「検証役」「統合役」）とあるエージェントを入れてください |
