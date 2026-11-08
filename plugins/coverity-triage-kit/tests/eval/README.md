# EVAL の道具（Claude Code で試す）

実際のモデルで課題を解かせて採点する試験（EVAL）の道具です。AI クレジットを使うので、CI では動かしません（設計書 9.2）。
このプラグインの部品は GitHub Copilot CLI 向けに書いてあります。EVAL では、部品を Claude Code の形に置き換えて、`claude -p` で動かします（設計書 9.3）。

| 段階 | 道具 | 確かめること |
| --- | --- | --- |
| 1（MCP サーバー） | `run_mcp_eval.py` | mcp-builder の `evaluation.xml` の問いに、MCP サーバーのツールだけで答えられるか |
| 2（作業エージェント） | `stage2/run_stage2.py`、`stage2/cases.json` | 作業エージェントと、その表の部品だけで、1件（1グループ）のトリアージを最後までできるか |
| 3（一括のトリアージ） | `stage3/run_stage3.py` | 統括エージェントが、偽の Coverity の見本の警告をすべてトリアージできるか |
| 4（画面の入口） | `stage4/run_stage4.py`、`stage4/cases.json`、`stage4/fake_open_server.py` | 頼みの言い方ごとに、画面の入口が選ばれるか（選ばれるべきでないときは選ばれないか） |
| 共通 | `claude_adapter.py`、`evalkit.py` | 部品の置き換え、`claude -p` の起動と記録の読み取り |

1段階目のスキルの EVAL は、各スキルの `evals/`（skill-creator の形）です。ここにはありません。

## 用意するもの

- Claude Code（`claude`）、uv、git、Python 3.10 以上。4段階目は、エクステンションの文を読むのに Node.js を使います（なければ写しの文を使います）。
- 動かす場所は、どれもプラグインのフォルダです。
- 結果は、既定では `<一時フォルダ>/coverity-triage-kit-evals/<段階>/<日時>/` にできます（`--out` で変えられます。プラグインのフォルダの中は断ります）。作業フォルダ・見本のリポジトリ・記録は、すべてそこにできます。
- Claude Code の中から動かしても動きます（環境変数 `CLAUDECODE` を外して起動します）。

## 動かし方

### 1段階目：MCP サーバー

```text
# Windows（PowerShell）
py -3 tests/eval/run_mcp_eval.py
# Linux（bash）
python3 tests/eval/run_mcp_eval.py
```

- `mcp.json` の起動のコマンドを使い、`--directory` を MCP サーバーのフォルダの一時コピーに替えます。偽の Coverity（`COVERITY_API=fake`、`COVERITY_FAKE_DATA=<コピー>/evals/data/.coverity-triage/fake-issues.yaml`）で動かします。
- モデルに渡すのは MCP サーバーのツールだけです（組み込みのツールなし、ほかの MCP サーバーと設定なし）。注釈が destructiveHint のツール（書き戻し）は外します（`--allow-write` で渡す）。
- mcp-builder の `scripts/evaluation.py` の `EVALUATION_PROMPT` を、システムプロンプトに足します。`<response>` の中を `<answer>` と完全に一致で比べます（前後の空白は除く）。
- 記録するもの：正解の数、課題ごとの時間・ツールの呼び出しの数・エラーの数・費用、`<summary>` と `<feedback>`。
- 結果：`report.md`（mcp-builder の報告の形）、`results.json`、`transcripts/task-NN.jsonl`。

| オプション | 意味 |
| --- | --- |
| `--model M` | モデル（既定 `claude-sonnet-5-5`） |
| `--only 1,3` | 動かす課題の番号（1 から。何度でも） |
| `--timeout S` | 1問の時間の上限（既定 300 秒） |
| `--eval-file F`、`--fake-data F`、`--server NAME` | 課題のファイル、偽の警告、登録名を替える |

### 2段階目：作業エージェント

```text
uv run --native-tls --quiet tests/eval/stage2/run_stage2.py
```

課題ごとに、自己診断のスキルの見本（`assets/sample-target`）から git のリポジトリを作り、`claude_adapter.py --parts-from-table` で作業エージェントとその表の部品だけを、そのリポジトリの `.claude/` に取り出します（`.git/info/exclude` で git の状態から外します）。MCP サーバーは偽の Coverity で動きます。

| 課題（`stage2/cases.json`） | 形 | 主な確かめ |
| --- | --- | --- |
| `user-20001`、`user-20002` | 利用者がチャットで頼む（ストリームとリポジトリを書いた、質問の要らない頼み。「差分を見せるだけで、ファイルは変えないで」） | 推奨が `assets/expected.yaml` と合う、修正案と逸脱コメント案の両方、逸脱コメントが決まりに合う（逸脱コメントのスキルの `check_comment.py`）、根拠のファイルと行がある、作業中のファイルが変わらない |
| `brief-20003` | ほかのエージェントからの頼み（実行フォルダの形の指示ファイル） | 返事が決まった1行、`result.json` が決まりに合う（結果の記録のスキルの `check_result.py`）、修正は作業用のコピーにだけ、書いたのは許された場所だけ、コマンドを使わない、進み具合 |
| `brief-group` | 同じ原因のグループ（CID 20004〜20006） | 上に加えて、`item` と `group_excluded_cids`、すべての CID の行を直した |

- 確かめは、`must`（落ちたら不合格）と `should`（記録だけ）に分けてあります。表の「毎回」のスキルを読んだかも `should` で記録します。
- `--judge` を付けると、モデル（既定 `claude-sonnet-5-5`、ツールなし）が中身（見立て・根拠・修正・逸脱コメント）を1〜5で採点し、その JSON を記録します。合否には入れません。
- 主なオプション：`--only <名前か番号>`、`--model M`（既定は作業エージェントの定義の claude のモデル）、`--timeout S`（既定 900 秒）、`--max-budget-usd X`、`--build-only`（作るだけ）。

### 3段階目：一括のトリアージ

```text
uv run --native-tls --quiet tests/eval/stage3/run_stage3.py
```

- 自己診断のスキルの一時コピーで、見本のリポジトリ（git、push 先、偽の Coverity）を作ります（`selftest.py start --sections 2` と `sample`）。
- 統括エージェントの表を（表のエージェントの表まで）たどって、エージェント・スキル・MCP サーバーをすべて取り出し、統括エージェントをメインにして動かします（`claude -p "Coverity の警告を、条件ファイル all.yaml で、すべてトリアージしてください。途中で質問せず、最後まで進めてください。" --agent <統括> --max-turns 500`）。実行フォルダの置き場所（リポジトリの外）は `--add-dir` で渡します。
- 変種：`default`（確かめと統合なし。設定の既定）と `full`（両方あり）。`--variant` で選びます（既定は両方）。
- 確かめ：自己診断の `check-run`（2-2〜2-6：最後まで進む、すべての作業が終わる、推奨が想定と合う、グループ、成果物と作業中のファイル）、`summary.html` と `approvals.json`、すべての作業の `result.json`、確かめありなら `review.json`、利用者のリポジトリが変わらない、役のエージェントだけを呼んだ、統括エージェントがファイルを書いていない、実行フォルダが1つ。件数・推奨の一致・時間・ターン・費用も記録します。
- 主なオプション：`--limit N`（頼みの文を「N 件だけ」にする。試しは 1）、`--model M`（統括。既定はその定義の claude のモデル）、`--agent-model M`（ほかのエージェント。既定は各定義の claude のモデル）、`--timeout S`（既定 45 分）、`--max-budget-usd X`、`--build-only`。

### 4段階目：画面の入口

```text
# Windows（PowerShell）
py -3 tests/eval/stage4/run_stage4.py
# Linux（bash）
python3 tests/eval/stage4/run_stage4.py
```

- 作業フォルダ（見本のリポジトリ）の `.claude/skills/` に、画面の入口・一括実行・困ったときのスキルをコピーします。画面を開くツールは、`fake_open_server.py`（stdio の MCP サーバー）が代わりをします。呼ばれるたびに記録（`calls.jsonl`）に書き、偽の URL と注意の文を返します。ツールの説明と注意の文は、エクステンションの `core/messages.mjs` から読みます。
- 使えるツールは Read・Glob・Grep・Skill・Bash で、許可するのは Skill と画面のツールだけです。コマンドは断られます（試みたことは記録に残ります）。
- 課題（`stage4/cases.json`）：「Coverity の警告をトリアージして」「未処理の Coverity 警告を調べて修正案と逸脱コメントを作って」「Coverity の警告を画面で見ながらトリアージしたい」（画面を開く。`requestText` が頼みの文と同じ）、「/<一括実行のスキル> High だけ」「CID 12345 が本物か調べて」（画面を開かない）、画面のツールがないときの「Coverity の警告をトリアージして」（3つの原因を示し、作業を始めない）。
- **名前の読み替え**：Claude Code では、このツールは `mcp__extension__coverity-triage-kit_open` に見えます。Copilot CLI では、エクステンションのツールは `coverity-triage-kit_open` のままです。回数と `requestText` は、サーバーの記録で数えます。
- 読んだスキルは、Skill のツールの呼び出しと、ツールの引数に出るスキルのフォルダ（`.claude/skills/<名前>/`）から数えます。`/<スキル>` で始まる頼みは、`claude -p` の記録に展開が出ないので、始まりの知らせのスラッシュコマンドにその名前があれば、読んだものとします。
- 主なオプション：`--only`、`--model M`（既定 `claude-sonnet-5-5`）、`--timeout S`（既定 240 秒）、`--build-only`。

### 結果の読み方

どの道具も、終わると JSON を1つ標準出力に出し（`ok`、`out`、`report`、合格の数、費用）、`<out>/report.md` と `<out>/results.json` を書きます。`claude` の記録（stream-json）は課題ごとの `transcript.jsonl` にあります。
`tests/eval/REPORT.md`（仕様 REQ-602）には、各 `report.md` のまとめを、版と日付を添えて写します。

### 費用と時間の目安

`claude-haiku-5-5` で、2026-11-08 に1回ずつ動かしたときの値です。既定のモデル（sonnet など）では高くなります。

| 段階 | 動かしたもの | 時間 | 費用 |
| --- | --- | --- | --- |
| 1 | 1問 | 15 秒 | $0.004 |
| 2 | 1課題（`brief-20003`） | 40 秒 | $0.012 |
| 3 | 見本のすべて（6 件の警告、4 作業、`default`） | 150 秒 | $0.08 |
| 3 | 1件だけ（`--limit 1`） | 70 秒 | $0.03 |
| 4 | 1つの言い方 | 10 秒 | $0.004 |

## Claude Code で再現しないこと

次は Claude Code では確かめられません。設計書 9.4 の表のとおり、Copilot CLI で手で確かめ、版と日付を書きます。

- ask_user（AskUserQuestion）：`claude -p` には、メインのエージェントにもありません。そのため、2段階目の「足りない情報を聞く」「直す前に同意を得る」道筋は、ここでは動きません（頼みの文を、質問の要らない形にしてあります）。作業エージェントは「ほかのエージェントからの依頼」として動きます。
- サブエージェントの入れ子：Claude Code のサブエージェントは、さらにサブエージェントを呼べません。3段階目は、統括エージェントをメインにします（Copilot CLI では、エクステンションが `startAgent` で始めます）。
- `agents/` の `.md` の読み込み、プラグインの名前の付いたエージェントとスキルの呼び方、`mcp.json` の `${…}` の展開。
- エクステンション（Web管理画面）。4段階目は、画面を開くツールの代わりのサーバーで、入口の選ばれ方だけを確かめます。画面の流れは `tests/extension/`（偽の SDK）と手での確認です。
- モデルの名前（`gpt-6-luna` などは使えないので、定義の候補のうち claude のものを使う）と、Auto のときのモデルの選ばれ方。
- Copilot CLI の許可の確認（`/add-dir`、「このセッションでは許可」）。ここでは `--permission-mode acceptEdits`、`--allowedTools`、`--add-dir` で代えます。
- 作業フォルダ：Claude Code は `.claude/` を作業フォルダ（＝調べるリポジトリ）に置くので、エージェントの検索に部品のファイルも出ます（Copilot CLI では、プラグインの部品はリポジトリの外にあります）。

## claude_adapter.py

エージェントの定義を Claude Code の形に置き換え、作業フォルダを作ります。

- エージェントの定義（`<名前>.agent.md`）を Claude Code のエージェントに置き換える。
  - ツールの読み替え：read → Read・Glob・Grep、search → Grep・Glob、edit → Edit・Write、execute → Bash、agent → Agent、skill → Skill、ask_user → AskUserQuestion、`<登録名>/<ツール>` → `mcp__<登録名>__<ツール>`。
  - `model` は、候補の並びの最初の `claude-*` を Claude Code の名前にする（`claude-haiku-5.5` → `claude-haiku-5-5`）。`--agent-model` で替えられる。
  - `skills:` に、「使う部品」の表のスキルを入れる。本文は変えず、先頭に読み替えの1段落を足す。
- スキルのフォルダを `<作業フォルダ>/.claude/skills/<名前>/` に、MCP サーバーのフォルダを `<作業フォルダ>/.claude/mcp/<名前>/` にコピーする。
- MCP の設定（`.claude/eval/mcp.json`）、`--agents` の JSON（`.claude/eval/agents.json`）、エージェントの定義（`.claude/agents/<名前>.md`）、コマンド（`.claude/eval/command.json`）を書く。
- `--parts-from-table <定義>` で、そのエージェントと、表の部品をすべて集める。表にエージェントがあれば、その表もたどる。部品の名前は表から読む。
- 作業フォルダは、プラグインのフォルダの外にします（中だと断ります）。

```text
# Windows（PowerShell）
py -3 tests/eval/claude_adapter.py collect com.github.copilot/agents/triage-orchestrator.agent.md
py -3 tests/eval/claude_adapter.py build --workspace "<作業フォルダ>" --parts-from-table com.github.copilot/agents/triage-worker.agent.md --fake-data "<偽の警告のファイル>" --model claude-haiku-5-5 --prompt "List the tools and skills you have, one per line" --run --inspect
# Linux（bash）
python3 tests/eval/claude_adapter.py collect com.github.copilot/agents/triage-orchestrator.agent.md
python3 tests/eval/claude_adapter.py build --workspace "<作業フォルダ>" --parts-from-table com.github.copilot/agents/triage-worker.agent.md --fake-data "<偽の警告のファイル>" --model claude-haiku-5-5 --prompt "List the tools and skills you have, one per line" --run --inspect
```

| オプション | 意味 |
| --- | --- |
| `--agent <定義>` | 表をたどらずに、このエージェントだけを置き換える（何度でも） |
| `--skill <フォルダ>` | スキルを足す。表の名前と同じなら、その行のスキルとして使う |
| `--mcp [名前=]<フォルダ>` | MCP サーバーを足す |
| `--main <名前>` | `--agent` で動かすエージェント（既定は最初のもの） |
| `--output-dir <フォルダ>` | `--add-dir` に渡す実行フォルダの置き場所（既定は作業フォルダの隣の `coverity-triage-out`） |
| `--fake-data <ファイル>` | MCP サーバーを偽の Coverity で動かす（`COVERITY_API=fake` などを渡す） |
| `--mcp-env KEY=VALUE` | MCP サーバーに渡す環境変数（EVAL の道具は `UV_PROJECT_ENVIRONMENT` を一時フォルダにする） |
| `--allow-missing` | 表の部品がなくても作る |
| `--inspect` | `stream-json` の始まりの知らせから、ツール・スキル・MCP サーバー・エージェントがそろったかを確かめる |

作るコマンド：`claude -p "<頼みの文>" --agent <名前> --agents <agents.json> --mcp-config <mcp.json> --strict-mcp-config --setting-sources project --permission-mode acceptEdits --tools <使うツール> --allowedTools "Bash(uv run:*)" Skill "mcp__<登録名>__*" --add-dir <output_dir>`（作業フォルダで動かす）。

- 頼みの文は `-p` のすぐ後に置きます（`--allowedTools` などは値をいくつも取るので、後ろの文を飲み込むため）。
- `--tools` には、置き換えたエージェントの組み込みのツールを合わせたものを渡します（Claude Code の既定のツールに Glob と Grep がないため）。エージェントの `tools` は、会話にあるツールを絞るだけです。
- `--allowedTools` の `Skill`：Claude Code は、frontmatter に `allowed-tools` のあるスキル（スクリプトを動かすスキル）を読む前に許可を求め、`claude -p` はそれを断ります。`Skill` を許可しないと、そのスキルを読めません。
- Claude Code の始まりの知らせでは、Agent のツールは `Task` と出ます。Claude Code に組み込みのスキル（`claude-api` など）も、スキルの一覧に出ます。

## 試験

モデルを使わない試験です（偽の `claude` と、画面を開くツールの代わりのサーバーで、道具の動きを確かめる）。プラグインの部品・git・uv がないときは、その試験を理由を書いて飛ばします。

```text
# Windows（PowerShell）
py -3 -m unittest discover -s tests/eval -p "test_*.py"
# Linux（bash）
python3 -m unittest discover -s tests/eval -p "test_*.py"
```
