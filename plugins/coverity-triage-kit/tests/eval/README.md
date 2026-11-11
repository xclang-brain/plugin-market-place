# EVAL（2段階目と3段階目）

実際のモデルでエージェントを実行して、結果を採点する、開発者向けのツールです。モデルの利用料がかかるため、`tests/run.py` からは実行しません。エージェントやスキルの文を変更したときに、手動で実行します。

## 確認する内容

開発用の形のプラグイン（`agents/*.md`、`.claude-plugin/plugin.json`、`.mcp.json`）のエージェントに、`claude -p` で「条件に合う警告をすべて処置して」と依頼します。Coverity は `tests/coverity_emulator`、リポジトリは見本（`tests/fixtures/sample/sample-target`）のコードです。Coverity に書き込むツール（`coverity_update_triage`）は、どの実行でも許可しません。

| 対象 | 確認する内容 |
| --- | --- |
| 共通 | 条件に合う警告をすべて取得し、どの CID も、処置済みまたは理由のある失敗になっている。`summary.md` に、取得した CID がすべて載っている。`result.json` の項目がそろっている。推奨と判定が `expected.yaml` の想定に合っている。`fix.patch` を、処置を開始したリビジョンに適用できる |
| 2段階目（`triage-worker`） | 最後の一覧より前に、利用者に質問していない（質問のツールの使用と、質問で終わる文章がない）。最後のメッセージに、すべての CID と `apply.py plan` の質問文がある |
| 3段階目（`triage-orchestrator`） | `run.json` の `review` が `true` で、すべての結果に検証の記録（合格・要確認）がある。1回の応答で `triage-worker` を2つ以上起動している。同じ原因の CID だけが1つに統合されている。`triage-reviewer` と `triage-integrator` を起動し、検証の記録と統合を任せている（起動を拒否された依頼は数えません。`triage-orchestrator` が自分で記録または統合したときは、不合格です） |
| OK の前 | 修正を取り込んでいない（`applied.json`、origin のブランチ、作業コピーの変更がない）。Coverity に書き込んでいない |
| `--answer-ok` | 同じセッションに「OK」と回答すると、修正を取り込む（git: origin にブランチ `coverity-fix/<結果フォルダー名>`、svn: 作業コピーの変更）。書き込むツールを許可していないので、Coverity への書き込みは 0 件のままで、最後のメッセージが、登録が残っていることを伝える（「登録しました」のような記載があるときは、不合格です）。最初の依頼が正常に終了しなかったときは、回答しません |

`tests/fixtures/sample/expected.yaml` は、`items` で CID ごとの推奨（`fix` / `deviation`）を、`group` で同じ原因の CID を定義しています。推奨が `fix` の CID は判定が `true_bug`、`deviation` の CID は判定が `false_positive` または `intentional` のときに合格です。`items` にない CID は、推奨と判定を確認しません。3段階目では、`group` の CID だけが統合されているときに合格です。

## 実行

```text
python3 tests/eval/run_eval.py --stage 2 --vcs git
python3 tests/eval/run_eval.py --stage 3 --vcs svn --answer-ok --work <フォルダー>
```

確認ごとに「合格」または「不合格」と理由を1行ずつ表示し、最後に結果を JSON で1行表示します。終了コードは、0 がすべて合格、1 が不合格あり、2 が実行できなかったとき、または停止されたときです。停止のシグナル（SIGTERM など）を受け取ると、エージェントとエミュレーターも停止します。

必要なもの: claude CLI（モデルを使用できるアカウントでログイン済み）、uv、PyYAML を使用できる Python 3.10 以上、git（`--vcs svn` では svn と svnadmin）。

時間の上限は、1回の実行につき、2段階目が 30 分、3段階目が 60 分です。2段階目の実測（git、`--answer-ok`、claude CLI 2.1.322、モデルは claude-haiku-5-5、3回）は、約 2 分、入力 約 200〜235 万トークン（大半はキャッシュの読み取り）、出力 約 2 万トークン、約 0.05 ドルでした。3段階目の実測は、まだありません。JSON の `usage` は、claude CLI が返すセッションの合計です（`transcript-ok` の値は、最初の依頼の分を含みます）。

## 記録と結果フォルダー

作業フォルダーは、リポジトリの外に作成します。`--work <フォルダー>` を指定したときは `<フォルダー>/評価 作業`、指定しないときはシステムの一時フォルダーの下です（パスに空白と日本語が入ります）。実行後も残るので、不要になったら削除してください。

| 場所 | 内容 |
| --- | --- |
| `transcript.jsonl`、`transcript-ok.jsonl` | エージェントの実行の記録（stream-json）。標準エラーは `.err` |
| `coverity-triage-out/<年月日-時分秒>/` | 結果フォルダー |
| `repo/`、`origin.git/`（svn は `svnrepo/`）、`plugin/`、`fresh/` | 見本のリポジトリ、修正の取り込み先、実行に使用したプラグインのコピー（`tests/` と `docs/` を除く）、`fix.patch` の適用を確認したコピー |
