# embedded-debug-kit 設計書

- 版：0.1.0
- 上位の文書：[仕様書](../specs/requirements.md) 0.1.0

## 1. 全体の構成

```text
4段階目  Web管理画面（debug-dashboard スキル ＋ CLI のフック）
           │ フックで記録と停止をするだけ。エージェントには依頼しない
3段階目  統括エージェント（debug-orchestrator）
           ├─依頼文─▶ 作業エージェント（2段階目）
           └─依頼文─▶ 検証エージェント（debug-verifier）
2段階目  作業エージェント（device-debugger）
           │ スキルの名前で使う
1段階目  スキル：cross-build・ftp-deploy・serial-console・skill-config
           │ FTP とシリアル
         組み込み機器
```

上の段階は、下の段階を名前だけで使う（REQ-002）。下の段階は、上の段階を知らない。

## 2. ファイルの置き場所

1つのフォルダで、Claude Code と Copilot CLI の両方に対応する。スキルは両方で共有する。エージェントとフックは CLI ごとに違うので、CLI の名前空間のフォルダに置く。

| 置き場所 | 中身 | 読むもの |
| --- | --- | --- |
| `plugin.json` | マニフェスト（Agent Plugins 1.0.0） | Copilot CLI |
| `.claude-plugin/plugin.json` | マニフェスト。エージェントとフックの場所を `com.anthropic.claude-code/` に向ける | Claude Code |
| `skills/<名前>/` | スキル（1段階目と、4段階目の debug-dashboard） | 両方 |
| `com.anthropic.claude-code/agents/*.md` | エージェント（2・3段階目） | Claude Code |
| `com.anthropic.claude-code/hooks/hooks.json` | フック（4段階目） | Claude Code |
| `com.github.copilot/agents/*.agent.md` | エージェント（2・3段階目） | Copilot CLI |
| `com.github.copilot/hooks/hooks.json` | フック（4段階目） | Copilot CLI |
| `docs/guide/1-<スキルの名前>.md` | スキルの使い方 | 人 |
| `docs/guide/2-device-debugger.md` | 作業エージェントの使い方 | 人 |
| `docs/guide/3-debug-orchestrator.md` | 3段階目の使い方 | 人 |
| `docs/guide/4-debug-dashboard.md` | Web管理画面の使い方 | 人 |
| `docs/` のほか | 憲章・仕様書・設計書 | 人 |
| `tests/` | プラグイン全体のテスト | 人・CI |

リポジトリのルートには、`.github/plugin/marketplace.json`（Copilot CLI）と `.claude-plugin/marketplace.json`（Claude Code）の両方に、このプラグインを載せる。

確かめた事実（2026年11月9日。Claude Code 2.1.319 での実測と公式文書、@github/copilot 1.0.107 の配布物と公式文書、Agent Plugins 1.0.0 の仕様）：

- Agent Plugins 1.0.0 の部品は、スキル（`skills/`）と MCP サーバー（`mcp.json`）だけである。CLI に固有のファイルは、その CLI の名前空間のフォルダに置く（第8節）。Claude Code の名前空間は決まっていないので、`com.anthropic.claude-code` とした。
- Copilot CLI は、Agent Plugins 1.0.0 の `$schema` を持つルートの `plugin.json` を、`.claude-plugin/plugin.json` より優先して読む。
- Claude Code は `.claude-plugin/plugin.json` を読む。`agents` にエージェントのファイルを並べると、既定の `agents/` の代わりにそれを読む（`claude plugin validate` は、フォルダの指定を受け付けない）。`hooks` を書くと、そこのフックを足す。
- Claude Code のプラグインのスキルの説明は、スキルのフォルダの場所を示す。Copilot CLI も同じである。

## 3. スキルの共通の決まり

| 項目 | 決まり |
| --- | --- |
| 名前 | フォルダの名前と SKILL.md の `name` を同じにする |
| SKILL.md | description は「何をするか」と「いつ使うか」だけ。本文は、コマンドの表、結果の読み方、最後に「してはいけないこと」の節。詳しいことは `reference/` に分ける |
| スクリプト | `scripts/` に置く。Python 3.8 以上の標準ライブラリだけを使う。ほかのものは `scripts/vendor/` に同梱する |
| 呼び方 | Windows は `py -3`、ほかは `python3`。パスは `<このスキルのフォルダ>/scripts/…` と書く |
| サブコマンド | `run`（作業）、`check`（設定を確かめる。何も作らない）、`init`（設定ファイルをひな形から作る）。`--workdir` で作業フォルダを指定できる |
| 結果 | 標準出力に JSON を1つ。共通の項目は `ok`・`message`・`problems`・`config`（設定ファイルの場所）・`log` |
| 文字コード | JSON は ASCII だけで書く（日本語は `\uXXXX`）。PowerShell も bash も、これなら化けない |
| 終了コード | 0：成功、1：作業の失敗、2：設定や引数の問題 |
| 設定ファイル | `<作業フォルダ>/.<スキルの名前>/config.ini`。節は `[settings]` だけ。リストは、項目の後の行に、行頭に空白を入れて1つずつ書く |
| 読み方 | `configparser.ConfigParser(interpolation=None)` で、`encoding="utf-8-sig"` を付けて読む。値の中の `%` をそのまま扱い、BOM があっても読める |
| ログ | `<作業フォルダ>/.<スキルの名前>/logs/` に UTF-8 で書く |
| Git に入れない | `init` と `run` は、`.<スキルの名前>/` に何かを書く前に、その直下に中身が `*` の `.gitignore` を置く |
| パスワード | 項目の名前を `password` などにし、コメントに「（秘密）」と書く。対に、環境変数の名前を書く `<項目>_env` を置く。環境変数があれば、そちらを使う。結果とログでは伏せる |

INI にしたのは、標準ライブラリで読めて、自前の読み取り処理が要らないからである。

## 4. 各スキル

| スキル | すること | 主な設定 |
| --- | --- | --- |
| cross-build | ビルドのコマンドを動かし、結果と、できたファイルの有無・更新の有無・サイズ・ELF の種類を返す。時間切れのときは、子プロセスも止める | `source_dir`、`sdk_env`、`build_command`、`outputs`、`timeout_seconds` |
| ftp-deploy | ファイルを FTP で機器に送る。一時的な名前で送り、サイズを確かめてから名前を変える。必要なら権限を変える（SITE CHMOD）。送り先は `remote_dir` の中だけ。`tls` で FTPS も選べる | `host`、`port`、`user`、`password`、`files`、`remote_dir`、`file_mode`、`tls` |
| serial-console | シリアルで機器のシェルにつなぎ、必要ならログインし、コマンドを順に動かして、出力と終了コードを返す。動かすコマンドは、設定の `commands` か、`--command` か `--stdin`（1行に1つ）で渡したもの | `port`、`baud`、`login_user`、`login_password`、`prompt`、`commands` |
| skill-config | スキルの名前を受け取り、`<作業フォルダ>/.<名前>/config.ini` のコメントを読んで、利用者に質問しながら値を書き込む。ファイルがなければ、そのスキルの `init` を使うよう伝える | なし |
| debug-dashboard | Web管理画面の起動と終了。フックから呼ばれて記録と停止をする（4段階目、8章） | なし |

- serial-console は、pyserial 3.5（BSD ライセンス）を `scripts/vendor/` に同梱する。
- skill-config の書き込みは、同梱のスクリプトで、その項目の行だけを書き換える（コメントと並びを残す）。値は標準入力で渡す。
- skill-config は自分の設定を持たないので、サブコマンドは `show`（読むだけ。何も作らない）と `set` にする。SKILL.md には、質問の進め方の節を足す。
- debug-dashboard のサブコマンドは `start`・`stop`・`status`。利用者が呼んだときだけ動くよう、SKILL.md に `disable-model-invocation: true` を書く（REQ-401。Claude Code と Copilot CLI の両方が対応している）。skill-creator の検証スクリプト（quick_validate）は、Agent Skills の基本の項目しか認めないので、この1行だけを理由に失敗する。

## 5. エージェント

| エージェント | 段階 | 動かし方 | 使うツール |
| --- | --- | --- | --- |
| device-debugger | 2 | メインの会話として動かすか、統括エージェントから依頼される | ファイルの読み書き、検索、コマンド、スキル、利用者への質問 |
| debug-orchestrator | 3 | メインの会話として動かす | 作業エージェントと検証エージェントへの依頼、ファイルの読み取り、検索、利用者への質問 |
| debug-verifier | 3 | 統括エージェントから依頼される | ファイルの読み取り、検索 |

- 統括エージェントは、メインの会話として動かす。サブエージェントは利用者に質問できないためである。また、サブエージェントがさらにサブエージェントを呼べるかは、環境変数 `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` で変わる（Claude Code 2.1.319 の公式文書と実測）。Claude Code では `claude --agent embedded-debug-kit:debug-orchestrator`、Copilot CLI では `/agent` で選ぶ。
- `Agent(…)` で依頼できる相手を絞れるのも、`--agent` で動かしたときだけである（サブエージェントとして呼ばれると無視され、利用者への質問もできない。公式文書と実測）。そのため description で「メインの会話としてだけ使う」と書き、本文で、利用者に質問できないときは何も依頼せずに終えるようにする。
- 統括エージェントのツールに、ファイルの編集・コマンド・スキルを入れない。依頼できる相手も、Claude Code では `Agent(embedded-debug-kit:device-debugger, embedded-debug-kit:debug-verifier)` と書いて絞る（REQ-303）。Copilot CLI では相手を絞る書き方がないので、定義の本文で絞る。
- 作業エージェントの定義は、使うスキルの名前だけを並べる（REQ-202）。Claude Code では frontmatter の `skills` に書き、スキルを最初から読み込ませる（実測で、プラグインのスキルを名前だけで指定できることを確かめた）。
- description は「何をするか」と「いつ使うか」だけを書く。CLI は、これを見て任せる相手を決める（両方の CLI の公式文書）。
- 本文は、役割 → 進め方 → 報告の形 → してはいけないこと の順に書く。本文にはツールの名前を書かない（「利用者に質問する」「エージェントに依頼する」と書く）。そのため、本文は両方の CLI で同じにできる。

### 5.1 統括エージェントの依頼文

統括エージェントと、作業・検証エージェントの間の取り決めは、統括エージェントの依頼文のひな形だけで決める。作業エージェントと検証エージェントの定義には、統括エージェントとの決まりを書かない。

| 依頼 | 相手 | 報告・判定の形 |
| --- | --- | --- |
| 作業（1回だけ試す） | 作業エージェント | 変えたこと、手順ごとの結果、判断に要るログの行、ログの場所。設定が足りないときや聞きたいことがあるときは、作業をせず、それを書く |
| 評価 | 検証エージェント | 条件ごとに1行、「C<番号>：<判定>。<理由>」。判定は「合格」「不合格」「判断できない」のどれか1つ。最後に「次に試すとよいこと：…」 |

- 作業の依頼文には、「合否は決めない」「利用者には質問しない」と書く（REQ-207）。
- 統括エージェントは、作業エージェントの質問を利用者に聞き、答えを次の依頼文に入れる（REQ-305）。

### 5.2 Issue

振り返りで改善点があれば、統括エージェントは、Issue にするかを利用者に聞く（REQ-309）。

1. 題と本文をチャットに出し、公開のリポジトリに載ることを伝えて、直したいところがないかを聞く。
2. 題と本文を入れた作成ページの URL（`https://github.com/xclang-brain/plugin-market-place/issues/new?title=…&body=…`）を示す。URL は 8,000 バイトまでにする。
3. 利用者が開いて「作成」を押す。毎回、利用者の承認を得たことになる（憲章 第24条）。

本文には、このプラグインの改善点だけを、一般的な言葉で書く。

## 6. CLI ごとの違い

| 項目 | Claude Code | Copilot CLI |
| --- | --- | --- |
| エージェントの定義 | `com.anthropic.claude-code/agents/<名前>.md` | `com.github.copilot/agents/<名前>.agent.md` |
| ツールの書き方 | `Read, Edit, Bash, Agent, Skill, AskUserQuestion` など | `read, edit, execute, agent, skill, ask_user` など |
| 使うスキル | frontmatter の `skills` | 本文の「使うスキル」の節 |
| 依頼できる相手 | `Agent(…)` で絞る | 本文で絞る |

- Copilot CLI 版のエージェントは、Claude Code 版から、frontmatter だけを替えて作る。本文は同じにし、同じであることをテストで確かめる。
- フックの違いは 8.3 に書く。
- Copilot CLI の対応は、Claude Code 版のテストを終えてから行う。Copilot CLI のフックの入力の項目（エージェントの名前、最後の報告）は、そのときに確かめる。

## 7. 3段階目の記録

3段階目の実行の記録は、CLI が残す会話の記録と、各スキルの `logs/` とする（REQ-311）。統括エージェントは、試行ごとの計画・報告の要約・判定を、終わりの報告に残す。振り返りは、これをもとにする。

## 8. 4段階目：Web管理画面

### 8.1 動き

1. 利用者は、ふつうのチャットで debug-dashboard スキルを呼び、画面を起動する。スキルは、作業フォルダの `.debug-dashboard/` に記録中の目印のファイルを置き、サーバーをバックグラウンドで起動して URL を返す。統括エージェントにはスキルを持たせないので、画面の起動と終了は、統括エージェントとは別のチャットで行う。
2. 利用者は、画面の「設定」で依頼文を作り、統括エージェントを選んだチャットに送る（REQ-403）。
3. CLI のフックが、依頼、ツールの呼び出し、エージェントの報告を `.debug-dashboard/sessions/<セッション>/events.jsonl` に書く。
4. 画面は、その記録を読んで、進み具合と結果を見せる。依頼文や報告の中身は読まず、そのまま見せる（REQ-409）。
5. 「実行を止める」を押すと、画面のサーバーが、そのセッションの止める目印のファイルを置く。フックは、目印があればツールの呼び出しを断り、作業を終えるよう伝える。そのセッションで次の依頼が送られたら、フックが目印を消す（REQ-405）。画面の起動と終了のときも、残っている止める目印をすべて消す。すでに動いているコマンドは、終わるまで止まらない。
6. スキルで画面を終了すると、サーバーが記録中の目印を消す。

目印のファイル：

| ファイル | 意味 |
| --- | --- |
| `.debug-dashboard/recording` | 記録中。中身は、サーバーのプロセス番号・ポート・更新の時刻。サーバーが 30 秒ごとに時刻を書き直す。フックは、2 分より古いものを、ないものとして扱う |
| `.debug-dashboard/sessions/<セッション>/stop` | このセッションを止める |
| `.debug-dashboard/sessions/<セッション>/handback-<agent_id>` | このサブエージェントの最後の報告を、`SubagentHandback` で記録した。同じ `agent_id` の SubagentStop で消す（8.2） |

`.debug-dashboard/` を作るときは、まず中身が `*` の `.gitignore` を置く。フォルダは本人だけが読み書きできる権限で作る（Windows では、作業フォルダの権限を引き継ぐ）。`.debug-dashboard/` かその中のフォルダがシンボリックリンクなら、フックもサーバーも何もしない。

### 8.2 フック

| イベント | すること |
| --- | --- |
| UserPromptSubmit | 新しい依頼として記録する。このセッションの止める目印を消す。ただし、`<task-notification>` で始まるものは、バックグラウンドのサブエージェントが終わった知らせなので、何もしない |
| PreToolUse | ツールの名前と入力を記録する（8.4）。止める目印があれば断り、断ったことも記録する。`SubagentHandback` は断らず、その `message` をサブエージェントの最後の報告として記録する |
| SubagentStop | エージェントの名前と、最後の報告を記録する |
| Stop | メインの会話の最後の報告を記録する。ただし、`background_tasks` に種類が `subagent` のものがあれば、まだサブエージェントを待っているので記録しない |

- サブエージェントかどうかは、入力の `agent_id` があるかで見分ける。どのエージェントかは `agent_type` の最後の `:` の後ろで見分ける。`--agent` で動かしたメインの会話では、`agent_id` がなく、`agent_type` は統括エージェントになる。
- SubagentStop は、`agent_type` が空でなく、同じセッションでそのエージェントへの依頼（`Agent` の呼び出し）を記録しているときだけ記録する。CLI が中で動かすエージェント（入力の候補や `/btw`）も SubagentStop を起こし、その `agent_type` はメインの会話のエージェントか空になるためである。
- 自動モードでは、サブエージェントは最後の報告を `SubagentHandback` で渡し、SubagentStop の `last_assistant_message` は締めの一言だけになる。そのため、`SubagentHandback` を記録したら、セッションのフォルダに `agent_id` の目印を置き、同じ `agent_id` の SubagentStop は記録しない。`SubagentHandback` を断ると報告が届かなくなるので、止める目印があっても断らない。
- 作業フォルダは、入力の `cwd` ではなく、環境変数 `CLAUDE_PROJECT_DIR`（Copilot CLI は `COPILOT_PROJECT_DIR` も）で決める。エージェントが途中でフォルダを移っても、記録と停止が続くようにするためである。
- 断るときも、JSON を出して終了コード 0 で終える。記録に失敗しても、ツールの呼び出しは通す。断るのは、止める目印があるときだけである。

### 8.3 フックの呼び方

フックは、画面を使っていないときも、すべてのツールの呼び出しで動く。そのため、まずシェルで記録中の目印があるかを確かめ、なければ Python を起動せずに 0 で終える（REQ-408）。

| CLI | 呼び方 |
| --- | --- |
| Claude Code | `bash` で動かす。目印がなければ `exit 0`。`py` があれば `py -3`、なければ `python3` でスクリプトを動かし、失敗しても `true` で 0 にする。Git Bash のない Windows では、フックが PowerShell で動いて失敗すると、ツールの呼び出しが止まることがあるので、PreToolUse には `"onFailure": "continue"` を付ける（Claude Code 2.1.295 以降） |
| Copilot CLI | `bash` と `powershell` の欄に書き分ける。どちらも目印を確かめてから Python を動かし、最後に 0 で終える。Copilot CLI は、0 以外の終了をツールの拒否とみなすためである |

断るときの出力は、CLI ごとの形にする。Claude Code は `hookSpecificOutput` の `permissionDecision`、Copilot CLI は `permissionDecision`。どちらかは、Copilot CLI が渡す環境変数 `COPILOT_CLI` で見分ける。

### 8.4 記録する内容

| もの | 記録する内容 |
| --- | --- |
| 利用者の依頼、エージェントへの依頼文、エージェントの最後の報告 | そのまま |
| コマンド | 先頭の 1,000 字 |
| ファイルの読み書き・検索 | ツールの名前とファイルのパスだけ。書いた中身は残さない |
| 利用者への質問 | 質問だけ。答えは残さない |
| ツールの結果 | 残さない |

- 書く前に、名前に `pass`・`token`・`secret`・`key` を含む項目の値（`名前=値`、`名前: 値`）と、URL の `//利用者:パスワード@` のパスワードを `***` に替える（REQ-407）。
- 1件は 8 KB まで。1つのセッションの記録が 20 MB を超えたら、それより後は書かず、「記録を省いた」を1件だけ残す。セッションは新しい 30 件まで残す。

### 8.5 画面のサーバー

- Python の標準ライブラリだけで書く。127.0.0.1 の、OS が選ぶポートで待ち受ける（REQ-402）。
- 起動ごとに乱数のトークンを作り、URL を `http://127.0.0.1:<ポート>/#k=<トークン>` とする（`#` より後はサーバーに送られない）。画面は、トークンを sessionStorage に移し、アドレスバーから消す。
- 記録を返す要求と POST は、ヘッダー `X-Dashboard-Token` のトークンが合うものだけを受ける。比べるときは `hmac.compare_digest` を使う。クッキーは使わない（クッキーはポートを区別しないため）。
- Host ヘッダーが `127.0.0.1:<ポート>` か `localhost:<ポート>` のものだけを受ける。POST は JSON だけを受け、Origin が同じものだけを受ける。
- どの応答にも CSP（`default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store` を付ける。
- 返すファイルは、決まった一覧のものだけにする。記録から出す文字は、すべて textContent で表示する。
- サーバーは、標準入出力を切り離し、POSIX では新しいセッション、Windows では切り離したプロセスとして起動する。終了は、トークンを付けた POST `/api/shutdown` で行う。

### 8.6 画面

| タブ | 中身 |
| --- | --- |
| 設定 | 直したいこと・合格の条件・試行の上限。入れると、依頼文と「コピー」ボタンが出る |
| 進み具合 | 今の段（計画・作業・評価）、依頼文と報告・判定を届いた順に、使われたツール（新しい順）、「実行を止める」ボタン |
| 結果 | 統括エージェントの最後の報告、検証エージェントの最後の判定、前の実行の一覧 |

今の段は、動いているエージェントの名前で決める。検証エージェントが動いていれば評価、作業エージェントが動いていれば作業、どちらも動いていなければ計画とする。

## 9. テスト

| 種類 | 置き場所 | 中身 |
| --- | --- | --- |
| スキルの単体テスト | `skills/<名前>/tests/` | スクリプトの動き。偽のビルド、FTP のテスト用サーバー、模擬のシリアルの機器を使う。フォルダだけをコピーしても動く |
| Web管理画面 | `skills/debug-dashboard/tests/` | 記録と停止、パスワードを伏せること、目印がないときと古いときに何もしないこと、サーバーのアクセス制限（トークン・Host・Origin）、記録の中の HTML が動かないこと |
| 独立性 | `tests/independence/` | 1段階目のスキルが外を参照しないこと。上の段階が下の段階の中身を書かないこと。作業エージェントがスキルの名前だけを持つこと。両方の CLI のエージェントの本文が同じこと。各ファイルの最後に「してはいけないこと」の節があること |
| 文書 | `tests/docs/` | 版の一致（両方のマーケットプレイスを含む）と、要求の追跡 |
| スキルの選ばれ方 | `skills/<名前>/evals/`、`evals/` | `claude plugin eval` の形（prompt.md と graders/）のケースを、`claude plugin eval .` で動かす。ふつうの頼みから、そのスキルが選ばれるか（スキルのフォルダだけでも動く）。関係のない頼みや Web管理画面を開く頼みで、スキルが選ばれないか |
| 通しのテスト（E2E） | `tests/e2e/` | 模擬の機器と `claude -p` で、2段階目と3段階目が不具合を直せるか。止める目印でツールが断られるか。AI の利用料がかかるので、CI では動かさない |

プラグインの検証には、`claude plugin validate`、plugin-dev の plugin-validator・skill-reviewer・検証スクリプト、skill-creator の検証スクリプトを使う。

## 10. 要求の追跡

| 要求 | 設計 | 実装 | 確かめ方 |
| --- | --- | --- | --- |
| REQ-001〜REQ-003 | 1、5 | エージェントの定義、スキル | `tests/independence/` |
| REQ-004 | 2、6 | 2つのマニフェスト、`com.*/` | `claude plugin validate`、Copilot CLI での読み込み、`tests/independence/` |
| REQ-005 | 3、8.3 | スクリプト、フックの設定 | スキルと画面の単体テスト（CI の Windows） |
| REQ-006 | 2 | `docs/guide/` | `tests/independence/` |
| REQ-007 | 3、5 | 定義と使い方の文書 | `tests/independence/`（最後の節）、人が読む |
| REQ-008 | 1、5 | 名前 | `tests/independence/` |
| REQ-101〜REQ-110 | 3、4 | `skills/<1段階目>/` | スキルの単体テスト、`claude plugin eval`（ふつうの頼みでスキルが選ばれるか）、`tests/independence/`（REQ-108） |
| REQ-111 | 3、4 | `skills/skill-config/` | skill-config の単体テスト、`claude plugin eval` |
| REQ-201〜REQ-209 | 5 | 作業エージェントの定義 | `tests/e2e/`（直せるか、スキルを使ったか、デバッグ用の出力を消したか）、`tests/independence/`（REQ-202）、ほかは人が読む |
| REQ-301〜REQ-308、REQ-310 | 5、5.1 | 統括・検証エージェントの定義 | `tests/e2e/`（直せるか、依頼の相手、自分で直さないこと、結論と条件ごとの判定）、`tests/independence/`（REQ-304）、ほかは人が読む |
| REQ-309 | 5.2 | 統括エージェントの定義 | `tests/e2e/`（振り返り）、人が読む（Issue の流れ） |
| REQ-311 | 7 | 統括エージェントの定義 | 人が読む |
| REQ-401〜REQ-409 | 8 | `skills/debug-dashboard/`、フックの設定 | debug-dashboard の単体テスト、`tests/e2e/`、`claude plugin eval`（AI が画面を勝手に起動しないか）、`tests/independence/`（REQ-409）。REQ-403 の依頼文は、人が画面で確かめる |
