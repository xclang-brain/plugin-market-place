# EVAL の道具（Claude Code で試す）

実際のモデルで課題を解かせて採点する試験（EVAL）の道具です。AI クレジットを使うので、CI では動かしません（設計書 9.2）。
EVAL は、部品を Claude Code で `claude -p` で動かします（設計書 9.3）。2段階目と3段階目は、部品の形を2つから選べます（`--format`）。

| `--format` | 使う定義 | 動かし方 |
| --- | --- | --- |
| `claude-plugin` | Claude Code の定義（`agents/<名前>.md`）と表のスキルと MCP サーバーを、そのまま一時のプラグインに写す | 本物のプラグインとして `--plugin-dir <一時のプラグイン> --agent <プラグイン>:<エージェント>` で動かす。利用者と同じく `--tools` は付けない |
| `copilot-converted`（既定） | Copilot CLI の定義（`com.github.copilot/agents/<名前>.agent.md`）を、Claude Code の形に置き換える | 作業フォルダの `.claude/` に置き、`--agents` と `--mcp-config` で渡す |

- **Claude Code のエージェント（`agents/<名前>.md`）を EVAL するときは、`--format claude-plugin` を使います。** 利用者が Claude Code でプラグインを動かすときと同じ形（プラグインとして読み込み、Claude Code の既定のツールのまま）で動かします。
- `copilot-converted` は、Copilot CLI の定義（`com.github.copilot/agents/`）を確かめるための形です。`--format` を付けないときは、こちらになります。

| 段階 | 道具 | 確かめること |
| --- | --- | --- |
| 1（MCP サーバー） | `run_mcp_eval.py` | mcp-builder の `evaluation.xml` の問いに、MCP サーバーのツールだけで答えられるか |
| 2（作業エージェント） | `stage2/run_stage2.py`、`stage2/cases.json` | 作業エージェントと、その表の部品だけで、1件（1グループ）のトリアージを最後までできるか |
| 3（一括のトリアージ） | `stage3/run_stage3.py` | 統括エージェントが、偽の Coverity の見本の警告をすべてトリアージできるか |
| 共通 | `claude_adapter.py`、`evalkit.py` | 部品の置き換えと写し、`claude -p` の起動と記録の読み取り |

1段階目のスキルの EVAL は、各スキルの `evals/`（skill-creator の形）です。ここにはありません。

## 用意するもの

- Claude Code（`claude`）、uv、git、Python 3.10 以上。
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

```text
# Claude Code の定義を、本物のプラグインとして動かす
uv run --native-tls --quiet tests/eval/stage2/run_stage2.py --format claude-plugin
```

課題ごとに、見本（`tests/fixtures/sample/sample-target`）から git のリポジトリを作り、`claude_adapter.py --parts-from-table` で作業エージェントとその表の部品だけを取り出します。MCP サーバーは偽の Coverity で動きます。

- `copilot-converted`（既定）：置き換えた定義を、そのリポジトリの `.claude/` に置きます（`.git/info/exclude` で git の状態から外します）。
- `claude-plugin`：作業エージェントの定義（`agents/<名前>.md`）・表のスキル・MCP サーバーを、そのまま `<out>/<課題>/claude-plugin/` に写します（`.claude-plugin/plugin.json` と `.mcp.json` つき。リポジトリの外）。作業エージェントをメインにして、`claude -p "<頼みの文>" --plugin-dir <out>/<課題>/claude-plugin --agent <プラグイン>:<作業エージェント> ...` で動かします。モデルは、定義の `model` のままです（`--model` を付けたときは、それを使い、写しの `model` の行もそれに替えます）。

| 課題（`stage2/cases.json`） | 形 | 主な確かめ |
| --- | --- | --- |
| `user-20001`、`user-20002` | 利用者がチャットで頼む（ストリームとリポジトリを書いた、質問の要らない頼み。「差分を見せるだけで、ファイルは変えないで」） | 推奨が見本の `expected.yaml` と合う、修正案と逸脱コメント案の両方、逸脱コメントが決まりに合う（逸脱コメントのスキルの `check_comment.py`）、根拠のファイルと行がある、作業中のファイルが変わらない |
| `brief-20003` | ほかのエージェントからの頼み（実行フォルダの形の指示ファイル） | 返事が決まった1行、`result.json` が決まりに合う（結果の記録のスキルの `check_result.py`）、修正は作業用のコピーにだけ、書いたのは許された場所だけ、コマンドを使わない、進み具合 |
| `brief-group` | 同じ原因のグループ（CID 20004〜20006） | 上に加えて、`item` と `group_excluded_cids`、すべての CID の行を直した |

- 確かめは、`must`（落ちたら不合格）と `should`（記録だけ）に分けてあります。表の「毎回」のスキルを読んだかも `should` で記録します。
- `--judge` を付けると、モデル（既定 `claude-sonnet-5-5`、ツールなし）が中身（見立て・根拠・修正・逸脱コメント）を1〜5で採点し、その JSON を記録します。合否には入れません。
- 主なオプション：`--format F`、`--only <名前か番号>`、`--model M`（既定は作業エージェントの定義の claude のモデル。`claude-plugin` では定義の `model`）、`--agent-model M`、`--tools-from-definitions`（`claude-plugin` で、定義の組み込みのツールを合わせた `--tools` を付ける。既定は付けない）、`--timeout S`（既定 900 秒）、`--max-budget-usd X`、`--build-only`（作るだけ）。

### 3段階目：一括のトリアージ

```text
uv run --native-tls --quiet tests/eval/stage3/run_stage3.py
# Claude Code の定義を、本物のプラグインとして動かす
uv run --native-tls --quiet tests/eval/stage3/run_stage3.py --format claude-plugin
```

- 見本（`tests/fixtures/sample/sample-target`）から、リポジトリ（git、push 先、偽の Coverity）を作ります。
- 統括エージェントの表を（表のエージェントの表まで）たどって、エージェント・スキル・MCP サーバーをすべて取り出し、統括エージェントをメインにして動かします（`claude -p "Coverity の警告を、条件ファイル all.yaml で、すべてトリアージしてください。途中で質問せず、最後まで進めてください。" --agent <統括> --max-turns 500`）。実行フォルダの置き場所（リポジトリの外）は `--add-dir` で渡します。
- `claude-plugin` では、統括エージェントと、表でたどれるエージェント（`agents/<名前>.md`）・スキル・MCP サーバーを、そのまま `<out>/<変種>/claude-plugin/` に写し、`--plugin-dir <out>/<変種>/claude-plugin --agent <プラグイン>:<統括>` で動かします。どの表にもないスキルは写しません（Claude Code は、プラグインの `skills/` のスキルをすべて読むため）。
- `claude-plugin` で `--model` を付けたときは、`claude` の `--model` に渡すだけでなく、写した統括エージェントの定義の `model` の行もそれに替えます（`--agent` で動かすメインのエージェントに、`--model` と定義の `model` のどちらが効くかによらず、そのモデルで動くようにするため）。ほかのエージェントの `model` の行は、`--agent-model` のときだけ替えます。
- 変種：`default`（確かめと統合なし。設定の既定）と `full`（両方あり）。`--variant` で選びます（既定は両方）。
- `bare` は `--variant bare` のときだけ動きます。条件のない頼み「Coverity の警告をトリアージしてください。途中で質問せず、最後まで進めてください。」で、設定は `default` と同じです。`--limit` とは一緒に使えません。
- どの変種でも、一括実行のスキルを読んだこと（`run_skill_loaded`）と、`new-run` を動かしたこと（`new_run_called`）を確かめます。
- 確認すること：最後まで進む、すべての作業が終わる（`all_items_done`）、終わった作業の `result.json` の項目と詳細レポートがそろう（`item_files`）、推奨とグループが見本の `expected.yaml` と合う（`expected_conclusions`、`expected_group`。`--limit` のときは確認しません）、`summary.html` と `approvals.json`、すべての作業の `result.json`、確かめありなら `review.json`、利用者のリポジトリが変わらない、役のエージェントだけを呼んだ、統括エージェントがファイルを書いていない、実行フォルダが1つ。件数・推奨の一致・時間・ターン・費用も記録します。
- 主なオプション：`--format F`、`--limit N`（頼みの文を「N 件だけ」にする。試しは 1）、`--model M`（統括。既定はその定義の claude のモデル。`claude-plugin` では定義の `model`。付けたときは、写した統括の `model` の行も替える）、`--agent-model M`（ほかのエージェント。既定は各定義のモデル。`claude-plugin` では、写した定義の `model` の行を替える。統括は `--model` が先）、`--tools-from-definitions`（`claude-plugin` で `--tools` を付ける。既定は付けない）、`--timeout S`（既定 45 分）、`--max-budget-usd X`、`--build-only`。

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

## Claude Code で再現しないこと

次は Claude Code では確かめられません。設計書 9.4 の表のとおり、Copilot CLI で手で確かめ、版と日付を書きます。

- ask_user（AskUserQuestion）：`claude -p` には、メインのエージェントにもありません。そのため、2段階目の「足りない情報を聞く」「直す前に同意を得る」道筋は、ここでは動きません（頼みの文を、質問の要らない形にしてあります）。作業エージェントは「ほかのエージェントからの依頼」として動きます。
- Claude Code では、サブエージェントとして始めた作業エージェントにも AskUserQuestion がないので、差分を「案」として返すだけです。差分を示し、同意を得てから直す道筋は、`claude --agent coverity-triage-kit:triage-worker` で起動した会話（`-p` なし）で、手で確かめます。
- Copilot CLI での、`com.github.copilot/agents/` の `.md` の読み込み、プラグインの名前の付いたエージェントとスキルの呼び方、`mcp.json` の `${…}` の展開。Claude Code での同じこと（`agents/`、`<プラグイン>:<名前>`、`.mcp.json` の `${CLAUDE_PLUGIN_ROOT}`）は、`--format claude-plugin` で確かめられます。
- モデルの名前（`gpt-6-luna` などは使えないので、定義の候補のうち claude のものを使う）と、Auto のときのモデルの選ばれ方。
- Copilot CLI の許可の確認（`/add-dir`、「このセッションでは許可」）。ここでは `--allowedTools` と `--add-dir` で代えます（`copilot-converted` は `--permission-mode acceptEdits` も付けます。`claude-plugin` は付けず、利用者に勧める許可と同じにします）。
- 作業フォルダ：`copilot-converted` では、`.claude/` を作業フォルダ（＝調べるリポジトリ）に置くので、エージェントの検索に部品のファイルも出ます。`claude-plugin` では、部品はリポジトリの外の一時のプラグインにあります（Copilot CLI と同じ）。

## claude_adapter.py

エージェントの定義を Claude Code の形に置き換え（`--format copilot-converted`、既定）、または Claude Code の定義を一時のプラグインに写して（`--format claude-plugin`）、作業フォルダとコマンドを作ります。

### copilot-converted（既定）

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
| `--format F` | `copilot-converted`（既定）か `claude-plugin`（下の「claude-plugin」） |
| `--plugin-dir <フォルダ>` | `claude-plugin` で、一時のプラグインを作る場所（既定は作業フォルダの隣の `claude-plugin`） |
| `--tools-from-definitions` | `claude-plugin` で、写した定義の組み込みのツールを合わせた `--tools` を付ける（既定は付けない） |
| `--main-model M` | `claude-plugin` で、メインのエージェントの写しの `model` の行だけを替える（`--agent-model` より先） |
| `--agent <定義>` | 表をたどらずに、このエージェントだけを置き換える（何度でも） |
| `--skill <フォルダ>` | スキルを足す。表の名前と同じなら、その行のスキルとして使う |
| `--mcp [名前=]<フォルダ>` | MCP サーバーを足す |
| `--main <名前>` | `--agent` で動かすエージェント（既定は最初のもの） |
| `--output-dir <フォルダ>` | `--add-dir` に渡す実行フォルダの置き場所（既定は作業フォルダの隣の `coverity-triage-out`） |
| `--fake-data <ファイル>` | MCP サーバーを偽の Coverity で動かす（`COVERITY_API=fake` などを渡す） |
| `--mcp-env KEY=VALUE` | MCP サーバーに渡す環境変数（EVAL の道具は `UV_PROJECT_ENVIRONMENT` を一時フォルダにする） |
| `--allow-missing` | 表の部品がなくても作る |
| `--inspect` | `stream-json` の始まりの知らせから、ツール・スキル・MCP サーバー・エージェントがそろったかを確かめる |

作るコマンド（copilot-converted）：`claude -p "<頼みの文>" --agent <名前> --agents <agents.json> --mcp-config <mcp.json> --strict-mcp-config --setting-sources project --permission-mode acceptEdits --tools <使うツール> --allowedTools "Bash(uv run:*)" Skill "mcp__<登録名>__*" --add-dir <output_dir>`（作業フォルダで動かす）。

- 頼みの文は `-p` のすぐ後に置きます（`--allowedTools` などは値をいくつも取るので、後ろの文を飲み込むため）。
- `--tools` には、置き換えたエージェントの組み込みのツールを合わせたものを渡します（Claude Code の既定のツールに Glob と Grep がないため）。エージェントの `tools` は、会話にあるツールを絞るだけです。
- `--allowedTools` の `Skill`：Claude Code は、frontmatter に `allowed-tools` のあるスキル（スクリプトを動かすスキル）を読む前に許可を求め、`claude -p` はそれを断ります。`Skill` を許可しないと、そのスキルを読めません。
- Claude Code の始まりの知らせでは、Agent のツールは `Task` と出ます。Claude Code に組み込みのスキル（`claude-api` など）も、スキルの一覧に出ます。

### claude-plugin

Claude Code の定義（`agents/<名前>.md`）から、表の部品をたどって集め、一時のプラグインに写します。

- 写すもの：エージェントの定義（変えずに。`--agent-model` と `--main-model` のときだけ `model` の行を替える）、表のスキル（`skills/<名前>/`）、表の MCP サーバー（元と同じ場所。`mcp-servers/<名前>/`）。どの表にもないスキルは写しません。
- `.claude-plugin/plugin.json`：元のマニフェストの写し（名前と版は同じ。場所を指すキー `skills` などは外す）。
- `.mcp.json`：元の `.mcp.json` の登録（`${CLAUDE_PLUGIN_ROOT}` のまま）に、偽の Coverity の環境変数と `--mcp-env` を足したもの。表に MCP サーバーがなければ作りません。
- 一時のプラグインの場所は `--plugin-dir`（既定は作業フォルダの隣の `claude-plugin`）。コピー元のフォルダ（プラグイン本体）の中、作業フォルダの中と上は断ります。
- 作ったフォルダには、印のファイル `.claude-eval-plugin`（中身は `{"made_by": "claude_adapter"}`）を置きます。次に作るときは、この印のあるフォルダだけを空にしてから作り直します。印のないフォルダは、空でなければ断り、何も消しません（`.claude-plugin/plugin.json` のある本物のプラグインのフォルダも同じです）。
- 作業フォルダには、コマンド（`.claude/eval/command.json`）だけを書きます（エージェントとスキルは、プラグインから読まれる）。

```text
# Windows（PowerShell）
py -3 tests/eval/claude_adapter.py collect --format claude-plugin agents/triage-orchestrator.md
py -3 tests/eval/claude_adapter.py build --format claude-plugin --workspace "<作業フォルダ>" --plugin-dir "<一時のプラグイン>" --parts-from-table agents/triage-worker.md --fake-data "<偽の警告のファイル>" --prompt "List the tools and skills you have, one per line" --run --inspect
# Linux（bash）
python3 tests/eval/claude_adapter.py collect --format claude-plugin agents/triage-orchestrator.md
python3 tests/eval/claude_adapter.py build --format claude-plugin --workspace "<作業フォルダ>" --plugin-dir "<一時のプラグイン>" --parts-from-table agents/triage-worker.md --fake-data "<偽の警告のファイル>" --prompt "List the tools and skills you have, one per line" --run --inspect
```

作るコマンド（claude-plugin）：`claude -p "<頼みの文>" --plugin-dir <一時のプラグイン> --agent <プラグイン>:<名前> --setting-sources project --allowedTools "Bash(uv run *)" "Skill(<プラグイン>:*)" "Read(//<一時のプラグイン>/**)" "Edit(//<output_dir>/**)" "mcp__plugin_<プラグイン>_<登録名>__*" --add-dir <output_dir>`（作業フォルダで動かす）。

- `Skill(<プラグイン>:*)`、`Read(//<一時のプラグイン>/**)`、`Edit(//<output_dir>/**)` は、README で利用者に勧める許可と同じ形です。`--permission-mode acceptEdits` は付けません（利用者にも勧めていないため）。書けるのは `<output_dir>` の中だけで、作業フォルダ（調べるリポジトリ）に書くときは確認が出て、`claude -p` では断られます。MCP のツールの `*` は、偽の Coverity を使う EVAL だけの書き方です。スキルは `allowed-tools` を持つので使う前に確認が出、一時のプラグインは作業フォルダの外なので参照資料を読むたびに確認が出ます。`claude -p` と裏で動くエージェントは、それを断ります（2.1.319 で確認。これがないと、作業エージェントが参照資料を読めずに止まります）。

- 既定では `--tools` を付けません。利用者がプラグインを動かすときと同じ、Claude Code の既定のツールのままです（既定のツールに足りないものがあれば、利用者にも足りないので、EVAL でもそれが出るようにするため）。`--tools-from-definitions` を付けたときだけ、copilot-converted と同じ決まりで、写した定義の `tools` の組み込みのツールを合わせたものを `--tools` に渡します（`tools` のない定義は基本のツールすべて）。
- `--mcp-config` と `--strict-mcp-config` は付けません（プラグインの MCP サーバーは、`.mcp.json` から読まれます）。
- `--inspect` は、始まりの知らせに、写したスキルすべて、メインのエージェント、その定義の `tools` の行のツール（`disallowedTools` を除く。`tools` の行のない定義は、`--tools` を付けたときだけ基本のツール）、写した MCP サーバーがあるかを確かめます。名前の前の `<プラグイン>:` は除いて比べます。
- MCP サーバーは、`plugin:<プラグイン>:<登録名>` の名前（または最後の `:` の後が同じ名前）で、状態が `connected` であることを確かめます。始まりの知らせにないとき（`.mcp.json` が読まれなかったとき）も、つながっていないときも、確かめに出ます。プラグインのものでないサーバー（利用者の設定のものなど）は確かめず、`extra_mcp_servers` に分けて示します。

## 試験

モデルを使わない試験です（偽の `claude` と、画面を開くツールの代わりのサーバーで、道具の動きを確かめる）。プラグインの部品・git・uv がないときは、その試験を理由を書いて飛ばします。

```text
# Windows（PowerShell）
py -3 -m unittest discover -s tests/eval -p "test_*.py"
# Linux（bash）
python3 -m unittest discover -s tests/eval -p "test_*.py"
```
