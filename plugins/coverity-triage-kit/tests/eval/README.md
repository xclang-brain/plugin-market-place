# EVAL の道具（Claude Code で試す）

実際のモデルで課題を解かせる試験（EVAL）の道具です。AI クレジットを使うので、CI では動かしません。
このプラグインの部品は GitHub Copilot CLI 向けに書いてあります。EVAL では、`claude_adapter.py` が部品を Claude Code の形に置き換えた作業フォルダを作り、`claude -p` で動かします（設計書 9.3）。

## claude_adapter.py でできること

- エージェントの定義（`<名前>.agent.md`）を Claude Code のエージェントに置き換える。
  - ツールの読み替え：read → Read・Glob・Grep、search → Grep・Glob、edit → Edit・Write、execute → Bash、agent → Agent、skill → Skill、ask_user → AskUserQuestion、`<登録名>/<ツール>` → `mcp__<登録名>__<ツール>`。
  - `model` は、候補の並びの最初の `claude-*` を Claude Code の名前にする（`claude-haiku-5.5` → `claude-haiku-5-5`）。`--agent-model` で替えられる。
  - `skills:` に、「使う部品」の表のスキルを入れる。
  - 本文は変えず、先頭に読み替えの1段落を足す。
- スキルのフォルダを `<作業フォルダ>/.claude/skills/<名前>/` に、MCP サーバーのフォルダを `<作業フォルダ>/.claude/mcp/<名前>/` にコピーする。
- MCP の設定（`.claude/eval/mcp.json`）、`--agents` の JSON（`.claude/eval/agents.json`）、エージェントの定義（`.claude/agents/<名前>.md`）を書き、`claude -p` のコマンドを示す。
- `--parts-from-table <定義>` で、そのエージェントと、表の部品をすべて集める。表にエージェントがあれば、その表もたどる（2段階目・3段階目の取り出し）。部品の名前は表から読む。

作業フォルダは、プラグインのフォルダの外にします（中だと断ります）。

## 使い方

プラグインのフォルダで動かします。結果は JSON 1つで標準出力に出ます。

部品を挙げるだけ（何も書かない）：

```text
# Windows（PowerShell）
py -3 tests/eval/claude_adapter.py collect com.github.copilot/agents/triage-orchestrator.agent.md
# Linux（bash）
python3 tests/eval/claude_adapter.py collect com.github.copilot/agents/triage-orchestrator.agent.md
```

作業フォルダを作り、試しに1回動かす（偽の Coverity の警告のファイルを MCP サーバーに渡す）：

```text
# Windows（PowerShell）
py -3 tests/eval/claude_adapter.py build --workspace "<作業フォルダ>" --parts-from-table com.github.copilot/agents/triage-worker.agent.md --fake-data "<偽の警告のファイル>" --model claude-haiku-5-5 --prompt "List the tools and skills you have, one per line" --run --inspect
# Linux（bash）
python3 tests/eval/claude_adapter.py build --workspace "<作業フォルダ>" --parts-from-table com.github.copilot/agents/triage-worker.agent.md --fake-data "<偽の警告のファイル>" --model claude-haiku-5-5 --prompt "List the tools and skills you have, one per line" --run --inspect
```

作った作業フォルダで、頼みの文を替えて動かす：

```text
# Windows（PowerShell）
py -3 tests/eval/claude_adapter.py run --workspace "<作業フォルダ>" --prompt "CID 20001 をトリアージして" --timeout 600 --inspect
# Linux（bash）
python3 tests/eval/claude_adapter.py run --workspace "<作業フォルダ>" --prompt "CID 20001 をトリアージして" --timeout 600 --inspect
```

主なオプション：

| オプション | 意味 |
| --- | --- |
| `--agent <定義>` | 表をたどらずに、このエージェントだけを置き換える（何度でも） |
| `--skill <フォルダ>` | スキルを足す。表の名前と同じなら、その行のスキルとして使う |
| `--mcp [名前=]<フォルダ>` | MCP サーバーを足す |
| `--main <名前>` | `--agent` で動かすエージェント（既定は最初のもの） |
| `--output-dir <フォルダ>` | `--add-dir` に渡す実行フォルダの置き場所（既定は作業フォルダの隣の `coverity-triage-out`） |
| `--fake-data <ファイル>` | MCP サーバーを偽の Coverity で動かす（`COVERITY_API=fake` などを渡す） |
| `--mcp-env KEY=VALUE` | MCP サーバーに渡す環境変数 |
| `--allow-missing` | 表の部品がなくても作る |
| `--inspect` | `stream-json` の始まりの知らせから、ツール・スキル・MCP サーバー・エージェントがそろったかを確かめ、`checks` に書く |

MCP サーバーを初めて動かす前に、コピーしたフォルダで `uv sync --native-tls --frozen --directory "<作業フォルダ>/.claude/mcp/<名前>"` をしておくと、起動が速くなります。

## 動かすコマンド

`claude -p "<頼みの文>" --agent <名前> --agents <agents.json> --mcp-config <mcp.json> --strict-mcp-config --setting-sources project --permission-mode acceptEdits --tools <使うツール> --allowedTools "Bash(uv run:*)" "mcp__<登録名>__*" --add-dir <output_dir>`（作業フォルダで動かす）。

- 頼みの文は `-p` のすぐ後に置きます（`--allowedTools` などは値をいくつも取るので、後ろの文を飲み込むため）。
- `--tools` には、置き換えたエージェントの組み込みのツールを合わせたものを渡します。エージェントの `tools` は、会話にあるツールを絞るだけで、足しはしません。Claude Code の版によっては、既定のツールに Glob と Grep がないためです。
- `run` は、別の Claude Code の中からでも動くよう、`CLAUDECODE` の環境変数を外して動かします。

## Copilot CLI と違うところ

- `claude -p` には AskUserQuestion がありません（メインのエージェントにも）。そのため、作業エージェントは「ほかのエージェントからの依頼」として動きます。利用者と話す道筋は、別の方法で確かめます（設計書 9.2）。
- Claude Code のサブエージェントは、さらにサブエージェントを呼べません。3段階目は、統括エージェントを `--agent` でメインにします。
- Claude Code の始まりの知らせでは、Agent のツールは `Task` と出ます。
- Claude Code に組み込みのスキル（`claude-api` など）も、スキルの一覧に出ます。
- `agents/` の `.md` の読み込み、エクステンション、モデルの名前、Copilot CLI の許可の確認は再現しません（設計書 9.4 で実測する）。

## 試験

モデルを使わない試験です（偽の `claude` で、置き換えとコマンドを確かめる）。

```text
# Windows（PowerShell）
py -3 -m unittest discover -s tests/eval -p "test_*.py"
# Linux（bash）
python3 -m unittest discover -s tests/eval -p "test_*.py"
```
