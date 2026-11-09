# コマンドと出力の形

目次
- コマンドの一覧
- tick の出力
- actions の形
- task の名前と知らせ方
- status の主な項目
- summary の主な項目

## コマンドの一覧

どれも `uv run --native-tls --quiet "<このスキルのフォルダ>/scripts/run.py" <コマンド> …` で動かします。

| コマンド | すること |
|---|---|
| `settings --repo R` | 設定の今の値（`max_rework`、`review`、`integration`、`initial`、`verify`、`max_items`、`parallel`、`output_dir`）、条件ファイルの名前の一覧 `filters`、設定の問題 `problems`。読むだけ |
| `runs --repo R [--limit N]` | 実行の一覧（新しい順）。`runs[{run_id, run_dir, created_at, filter_name, counts, unfinished, has_summary, foreign, same_tool}]` |
| `new-run --repo R [条件の引数] [--client copilot\|claude-code] [--mcp-tool T]…` | 実行フォルダを作る。`run_dir, repo_root, work_dir, limit, initial, review, integration, max_rework, permission_steps`。`--client`（既定 copilot）は許可の案内の形。`--mcp-tool` は、案内で前もって許可する読むツール（copilot は `<登録名>(<ツール>)`、claude-code は `mcp__<サーバー>__<ツール>` か `mcp__plugin_<プラグイン>_<サーバー>__<ツール>`。形の違う名前と、claude-code で書き込みの言葉（update など）を含む名前は入れない） |
| `resume --run D [--client …] [--mcp-tool T]…` | 途中の実行を続きからにする（処理中・失敗の作業を「まだ」に戻す）。`run_dir, counts, permission_steps` |
| `tick --run D [--finished T] [--failed T --reason R] [--wait S]` | 次にすること（下の「tick の出力」）。`--wait` は待つ最長の秒数（既定 20、上限 25） |
| `status --run D` | 実行の様子（下の「status の主な項目」）。読むだけ |
| `summary --run D` | 一覧と承認の案を作る（下の「summary の主な項目」） |
| `stop --run D` | 切り離して動かしている処理（初回解析・ビルドなど）を止め、実行に止めた印をつける。`stopped_jobs[]`、`unstopped[{name, pid, how}]` |
| `note-issue --run D --title T --body B`（または `--stdin`） | 振り返りの課題を書き残す。`issues_found`（件数） |
| `initial-status --run D` | 初回解析の段と進み（`status`、`step_label`、`value`、`max`、`label`、失敗のとき `error` と `log_tail`） |

## tick の出力

```json
{"ok": true, "stage": "items", "stage_label": "警告ごとの作業", "actions": [], "wait": false, "done": false,
 "stop": false, "say": ["…"], "counts": {"pending": 3, "in_progress": 2, "done": 5, "error": 0, "merged": 0, "total": 10},
 "next": "…"}
```

- `stage`：`initial`（初回解析）→ `fetch`（警告の取得）→ `details`（警告経路の取得）→ `items`（警告ごとの作業）→
  `integrate`（同じ原因の統合）→ `review`（結果の確かめ）→ `verify`（ビルドでの検証）→ `done`。止めた実行は `stopped`。
- `say` は 2 行まで。`counts` は変わったときだけ入ります。`next` は次にすることの短い説明です。
- `stop: true` のときは `error` も入ります（警告の取得の 2 回の失敗、初回解析の失敗、作業用のコピーを作れない など）。

## actions の形

| type | 項目 |
|---|---|
| `fetch_issues` | `task, repo_root, filter_file, output_file` |
| `fetch_details` | `task, repo_root, stream, cids, output_dir`（`cids` は 50 件まで） |
| `launch` | `task, role（worker / reviewer / integrator）, item, description, prompt, background` |
| `command` | `task, command, note` |

例：

```json
{"type": "launch", "task": "worker:20001@1", "role": "worker", "item": "20001", "description": "20001 の作業",
 "prompt": "指示ファイル <実行フォルダ>/items/20001/brief.md を読み、書かれた手順に従ってください。", "background": true}
```

- `prompt` は書き換えずに渡します。指示ファイルに、仕事・読むもの・書くもの・返す形がすべて書いてあります。
- `background: true` は、終わりを待たずに次へ進めてよいという意味です（同時にいくつも動かせます）。

## task の名前と知らせ方

| task | 何の仕事か |
|---|---|
| `fetch:issues@<回>` | 警告の取得 |
| `fetch:details:<番号>@<回>` | 警告経路の取得 |
| `worker:<ID>@<回>[#<直しの回>]` | 作業役の仕事（`<ID>` は CID か `G1` などのグループ） |
| `reviewer:<ID>@<回>[#<直しの回>]` | 検証役の確かめ |
| `integrator@<回>[#<直しの回>]` | 統合役のまとめ |

- 終わったら `tick --run D --finished <task>`、失敗したら `tick --run D --failed <task> --reason '<理由>'`。
- 結果のファイルができていれば、知らせがなくても tick は終わったとみなします（後で届いた知らせは使いません）。
- 30 分のあいだ出力も進み具合もない仕事は、失敗としてやり直します。

## status の主な項目

`run_id, run_dir, repo_root, stage, stage_label, finished, stopped, halted, halt_reason, has_summary, review, integration,
max_rework, parallel, counts, items[], running_tasks[], errors[], recent[], initial, progress, run_checks[], criteria[],
criteria_results[], issues_found[]`

- `halted` は、最後の tick が `stop: true` を返した（続けられない。認証の失敗、5xx、初回解析の失敗、警告の取得の失敗など）とき真で、
  `halt_reason` にその理由が入ります。利用者が止めたときは `stopped` が真になり、`halted` は偽です。直して `resume` すると偽に戻ります。

- `items[]`：`id, cids, checker, status, phase, note, elapsed_seconds, stalled, preparing_copy, attempts, rework_rounds,
  review, recommendation, confidence, error`。`confidence` は人に見せる確信度（作業役・確かめ・ビルドでの検証のいちばん低いもの）。
- `stalled` は 10 分（初回解析は 30 分）動きがないとき真です。作業用のコピーを用意している作業では偽です。
- `recent` は新しい順に 10 行（"HH:MM:SS できごと"）。`run_checks` は一覧を作ってから入ります。

## summary の主な項目

`summary_html, counts, approvals_counts, results[], unprocessed[], run_checks[], criteria[], criteria_results[], say[]`

- `counts`：`warnings`（警告）、`items`（作業）、`groups`、`fix`（修正の推奨）、`deviation`（逸脱の推奨）、`low`（確信度 低）、
  `errors`（処理できなかった作業）、`unprocessed`（未処理）。
- `approvals_counts`：`fix`、`deviation`、`reject`、`none`（反映しない）、`changed`（推奨から変えたもの）。
- `results[]`：確信度の低い順。`id, title, cids, checker, approval, edited, recommendation, confidence, judgement, summary, report`。
- `run_checks[]`：実行ごとの評価基準（C-16〜C-19、C-24〜C-27）の `{id, ok, detail}`。`ok` が null のものは対象外です。
