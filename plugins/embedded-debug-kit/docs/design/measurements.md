# embedded-debug-kit 実測の記録

[設計書](design.md)の判断のもとになった、CLI の動きの記録である。決めたことは設計書に書き、ここには調べた事実だけを書く。
CLI のバージョンを上げたら、ここを確かめ直す。

| 節 | 中身 | 確かめたもの | 方法 |
| --- | --- | --- | --- |
| M2 | マニフェストと置き場所 | Claude Code 2.1.319、@github/copilot 1.0.107、Agent Plugins 1.0.0（2026-11-09） | 実測、公式文書、配布物、仕様 |
| M4 | スキルの検証 | skill-creator の quick_validate | 実測 |
| M5 | Claude Code のエージェント | Claude Code 2.1.319 | 実測、公式文書 |
| M6 | Copilot CLI のエージェント | Copilot CLI 1.0.108（2026-11-09） | 実測、公式文書、更新履歴 |
| M8.1 | Claude Code のフック | Claude Code 2.1.319 | 実測、公式文書 |
| M8.2 | Copilot CLI のフックの入力 | Copilot CLI 1.0.108 | 実測、配布物 |
| M8.3 | Copilot CLI のフックの動かし方 | Copilot CLI 1.0.108 | bash は実測。PowerShell は公式文書と配布物 |
| M8.4 | フックの処理を hook.sh と hook.ps1 に移した後 | Claude Code 2.1.321（2026-11-10）、CI | 実測、テスト |
| M9 | 部品の置き換え | Claude Code 2.1.321、Copilot CLI 1.0.108（2026-11-10） | 複製での入れ替え、テスト、通しのテスト |

## M2 マニフェストと置き場所

- Agent Plugins 1.0.0 の部品は、スキル（`skills/`）と MCP サーバー（`mcp.json`）だけである。
- Copilot CLI は、Agent Plugins 1.0.0 の `$schema` を持つルートの `plugin.json` を、`.claude-plugin/plugin.json` より優先して読む。
- Claude Code は `.claude-plugin/plugin.json` を読む。
- Claude Code は、`agents` にエージェントのファイルを並べると、既定の `agents/` の代わりに、並べたファイルを読む。
- `claude plugin validate` は、`agents` にフォルダを書くと受け付けない。
- Claude Code は、`hooks` を書くと、そこのフックを足す。
- Claude Code も Copilot CLI も、プラグインのスキルの説明で、スキルのフォルダの場所を示す。

## M4 スキルの検証

- `disable-model-invocation: true` は、Claude Code と Copilot CLI の両方が対応している。
- skill-creator の検証スクリプト（quick_validate）は、Agent Skills の基本の項目しか認めない。debug-dashboard は、この1行だけを理由に失敗する。

## M5 Claude Code のエージェント

- サブエージェントは、利用者に質問できない。
- サブエージェントがさらにサブエージェントを呼べるかは、環境変数 `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` で変わる。
- `Agent(…)` で依頼の相手を絞れるのは、`--agent` で動かしたときだけである。サブエージェントとして呼ばれると、この絞り込みは無視される。
- frontmatter の `skills` には、プラグインのスキルを名前だけで書ける。書いたスキルは、最初から読み込まれる。

## M6 Copilot CLI のエージェント

ツール：

- ツールの名前は、大文字と小文字を区別しない。知らない名前は、何も言わずに無視される。
- `execute` でコマンド（Windows では PowerShell、ほかでは bash）、`read` で読み取り、`edit` で編集と作成、`search` で検索、`agent` でエージェントへの依頼、`skill` でスキルを使える。
- `AskUserQuestion` は、`ask_user` の別名ではない。
- 利用者への質問（`ask_user`）は、対話のセッションで、メインのエージェントだけが使える。`-p` で動かしたときと、サブエージェントでは使えない。
- `--agent` で動かしたメインのエージェントには、`tools` に書かなくても、スキル（`skill`）とセッションのデータベース（`sql`）のツールが付く。コマンドとファイルの編集は付かない。

依頼の相手：

- 依頼の相手の一覧には、ほかから呼べるすべてのエージェントが並ぶ。コマンドとファイルの編集を持つ組み込みのエージェント（`general-purpose`、`task` など）も入る。
- 実測で、統括エージェントが `general-purpose` に依頼し、`general-purpose` がコマンドを動かした。
- `tools` に `agent/<名前>` のように書くと、依頼そのものができなくなる。
- エージェントの frontmatter の `hooks` は、Copilot CLI だけの機能である。そのエージェントが動いている間だけ働く。ほかのエージェントが動いているときと、サブエージェントのツールの呼び出しでは働かない。
- エージェントのフックは、プラグインのエージェントでも、`-p` でも働く。
- フックの入力は、空白のない JSON で渡る。依頼文の中の `"` は `\"` になる。
- フックが断った理由は、エージェントに届く。

frontmatter の項目：

- `disable-model-invocation: true` のエージェントは、依頼の相手の一覧から外れる。`--agent` と `/agent` では選べる。
- `user-invocable: false` のエージェントは、`/agent` の一覧に出ない。ほかのエージェントからは呼べる。`--agent` で名前を指定すれば動かせる。
- `skills` に書いたスキルは、エージェントが動き始めるときに中身が読み込まれる。公式文書の表にはないが、更新履歴（「Custom agents can now declare a `skills` field to eagerly load skill content into agent context at startup」）と実測で確かめた。

コマンド（Copilot CLI 1.0.108 のヘルプと、認証なしで動くコマンドで確かめた。2026-11-10）：

- `/agent [名前]` で、エージェントを一覧から選ぶか、名前で指定する。`/skills` でスキル、`/env` で読み込まれた部品の全体を見られる。
- `copilot --plugin-dir <フォルダ>` で、手元のフォルダのプラグインを読み込む。`copilot --plugin-dir <フォルダ> skill list` に、そのプラグインのスキルが出る。
- `copilot plugin install <所有者>/<リポジトリ>:<パス>` で、リポジトリの中のフォルダからインストールできる。

名前：

- エージェントの名前は、`embedded-debug-kit:<ファイルの名前から .agent.md を除いたもの>` になる。プラグインの名前を省くと見つからない。
- プラグインのスキルを利用者が呼ぶときは、`/embedded-debug-kit:<スキルの名前>` と入力する。`/<スキルの名前>` だけでは「Unknown command」になる。

## M8.1 Claude Code のフック

- `--agent` で動かしたメインの会話の入力には、`agent_id` がない。`agent_type` は、そのエージェントの名前になる。
- CLI が中で動かすエージェント（入力の候補、`/btw`）も、SubagentStop を起こす。その `agent_type` は、メインの会話のエージェントか、空になる。
- 自動モードでは、サブエージェントは最後の報告を `SubagentHandback` というツールで渡す。SubagentStop の `last_assistant_message` は、締めの一言だけになる。
- Git Bash のない Windows では、フックが PowerShell で動く。フックが失敗すると、ツールの呼び出しが止まることがある。`"onFailure": "continue"` は、Claude Code 2.1.295 以降で使える。

## M8.2 Copilot CLI のフックの入力

- イベントを PascalCase の名前で登録すると、入力の項目の名前が snake_case になる。
- `--agent` で動かしたメインの会話の入力には、`agent_type` がない。
- サブエージェントの中で動くフックでは、入力の `session_id` が、サブエージェント自身の ID（`agent_id` と同じ）になる。親は `parent_agent_id` に入る。
- 統括エージェントが直接依頼したエージェントなら、`parent_agent_id` のセッションはメインの会話である。依頼されたエージェントがさらに依頼すると、その先は別のセッションになる（このプラグインの作業・検証エージェントは、依頼をしない）。
- サブエージェントも、始めに UserPromptSubmit（依頼文）、終わりに Stop を起こす（`agent_id` つき）。
- Stop の入力に、最後の報告がない。
- 会話の記録は、入力の `transcript_path`（`session-state/<セッション>/events.jsonl`。1行に1件の JSON）にある。
- 会話の記録の形は、公式文書にない（載っているのはファイルの名前だけ）。配布物の `schemas/session-events.schema.json` と実測から調べた。
- 会話の記録の、ターンの終わりの行は、フックが動き始めてから少し遅れて書かれる（実測で 0.25 秒ほど）。
- 作業フォルダの環境変数（`COPILOT_PROJECT_DIR`。`CLAUDE_PROJECT_DIR` も同じ値）は、Git のリポジトリの中で起動すると、CLI を起動したフォルダではなく、リポジトリのいちばん上のフォルダになる。
- この環境変数は、エージェントのシェルには渡らない。
- フックが断った理由は、エージェントに届く。

## M8.3 Copilot CLI のフックの動かし方

- `bash` の欄は、`/bin/bash --norc --noprofile -c '<欄の文字>'` で動く。
- `powershell` の欄は、PowerShell 7 以降（`pwsh`）があればそれで、なければ Windows PowerShell（`powershell.exe`）で動く。どちらも `-NoProfile -NoLogo` が付く。
- PreToolUse のフックが 0 以外で終わるか落ちると、ツールの呼び出しを断る。時間切れのときは通す。
- 入力の JSON は、シェルの標準入力に UTF-8 のまま渡る（日本語も `\u` に変わらない）。シェルから起動した Python は、その標準入力を受け継ぐ。
- PowerShell で `$input` を使うと、PowerShell が先に標準入力を読み、文字コードを変えてしまう。
- プラグインのフォルダは、Agent Plugins の仕様にある環境変数 `PLUGIN_ROOT` で渡る。作業フォルダは `COPILOT_PROJECT_DIR` で渡る。ほかに `COPILOT_PLUGIN_ROOT`・`CLAUDE_PLUGIN_ROOT`・`CLAUDE_PROJECT_DIR`・`COPILOT_CLI=1` も渡る。
- 欄の中の `${PLUGIN_ROOT}` などは、シェルに渡す前に、文字として置き換えられる。
- プラグインのフックは、`-p` で動かしたときも、信頼していないフォルダでも動く。

## M8.4 フックの処理を hook.sh と hook.ps1 に移した後

フックの処理は、初めは hooks.json に直接書いていた。M8.1〜M8.3 は、そのときの記録である。
hook.sh と hook.ps1 に移した後（設計書 8.3）に確かめたことを、ここに書く。

| 確かめたこと | 方法 | 結果 |
| --- | --- | --- |
| Claude Code で、記録と「実行を止める」が働く | Claude Code 2.1.321（Linux）で、通しのテストの stop のケース（2026-11-10） | 働いた |
| hooks.json のコマンドが、bash で働く | `skills/debug-dashboard/tests/` を、CI の ubuntu-24.04 と windows-latest（Git Bash）で動かす（2026-11-10） | 通った |
| hooks.json の `powershell` の欄が働く | 同じテストを、CI の windows-latest で動かす（2026-11-10）。Copilot CLI と同じ引数の PowerShell に、実行ポリシー Restricted を指定する | 通った |

## M9 部品の置き換え

プラグインの複製で部品を置き換え、Claude Code 2.1.321 の通しのテストで確かめた（2026-11-10）。

| 置き換え | ほかに直したファイル | 結果 |
| --- | --- | --- |
| ftp-deploy のフォルダを、同じ名前の、SKILL.md だけのスキル（コピーで機器に送る）に置き換えた | なし | worker のケースが通った。不具合が直り、置き換えたスキルが使われ、ログがファイルに残った |
| 上に加えて、作業エージェントの定義を、同じ名前の、本文を短く書き直した定義に置き換えた | なし | worker と orchestrator のケースが通った。統括エージェントが作業エージェントに依頼し、検証エージェントが判定した |
| ftp-deploy を、名前の違うスキルに置き換えた | 作業エージェントの定義の2か所。通しのテストの、FTP の設定と「使ったスキル」の確かめ方 | worker のケースが通った |
| device-debugger を、名前だけ変えた作業エージェントに置き換えた | 統括エージェントの定義（Copilot CLI は6か所、Claude Code は2か所）、マニフェストの一覧 | `tests/independence`、`tests/docs`、debug-dashboard のテストが通った。統括エージェントのフックが、新しい名前への依頼を通した |

- 名前の違うスキルは、`copilot --plugin-dir <フォルダ> skill list`（Copilot CLI 1.0.108）の一覧に出た。
- 作業エージェントの定義は、Claude Code 用のものを置き換えた。Copilot CLI 用の定義では、試していない。

## まだ確かめていないこと

- Windows の Copilot CLI そのもので、`powershell` の欄を動かすこと。テストは、Copilot CLI と同じ引数の PowerShell で動かしているが、Copilot CLI そのものは通していない。
- hook.sh と hook.ps1 に移した後の、Copilot CLI そのものでの記録と停止（Linux・Windows とも）。
- 部品を置き換えた後に、Copilot CLI で AI を動かすこと（M9 は、Claude Code とテストで確かめた）。
