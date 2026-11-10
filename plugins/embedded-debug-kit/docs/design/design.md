# embedded-debug-kit 設計書

- バージョン：0.1.0
- 上位の文書：[仕様書](../specs/requirements.md) 0.1.0

この文書には、決めたことと、その理由だけを書く。
CLI の動きを調べた結果は、[実測の記録](measurements.md)に分けてある。本文の「M5」などは、その節の番号である。

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

### 1.1 部品の置き換え

部品は、同じ名前の部品に置き換える。上の段階は名前だけで使うので、ほかのファイルは直さずに済む（REQ-002）。手順は、[スキルと作業エージェントを置き換える](../guide/5-replace-parts.md)に書く。

名前を変えるときは、その名前を書いている場所を合わせる。

| 部品 | 動かすために合わせる場所 | 文書とテストで名前を書いている場所 |
| --- | --- | --- |
| スキル | 作業エージェントの定義の2か所（`skills` の一覧と、本文の「使うスキル」） | README、`docs/guide/` のそのスキルの使い方と作業エージェントの使い方、設計書、`tests/e2e/run_e2e.py`、`evals/` の採点基準 |
| 作業エージェント | 統括エージェントの定義。Copilot CLI は6か所（うち4か所は、6.1 のフックのコード）。Claude Code は2か所と、マニフェストのエージェントの一覧 | README、`docs/guide/` の作業エージェントと統括エージェントの使い方、設計書、`tests/e2e/run_e2e.py`、`evals/` の採点基準 |
| 検証エージェント | 統括エージェントの定義と、debug-dashboard の `server.py`・`app.js` | 設計書、`tests/e2e/run_e2e.py` |

- 定義は、両方の CLI のものを同じように合わせる。本文が同じかを、テストで確かめているためである。
- `tests/independence/` は、部品の名前を定義から読む。名前を変えても、テストはそのまま使える。
- 置き換える部品が満たすこと（作業エージェントは、依頼文の手順と報告の形に従い、判断に必要な出力をファイルに残す）は、使い方の文書に書いた約束である。満たしているかを確かめるしくみは、まだない。
- 統括エージェントの定義には、機器向けの言葉がある（「組み込み機器の不具合の修正などを」、「デバッグ用の出力」、Issue の送り先）。別の分野の作業エージェントに置き換えるときは、ここも合わせる。

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
| `docs/guide/5-replace-parts.md` | スキルと作業エージェントを置き換える手順 | 人 |
| `docs/`（`guide/` 以外） | 憲章・仕様書・設計書・実測の記録 | 人 |
| `tests/` | プラグイン全体のテスト | 人・CI |
| `.gitattributes` | シェルスクリプトの改行を LF に固定する（8.3） | Git |

- CLI に固有のファイルは、その CLI の名前空間のフォルダに置く（Agent Plugins 1.0.0 の第8節）。
- Claude Code の名前空間は決まっていないので、`com.anthropic.claude-code` とした。
- Claude Code のマニフェストの `agents` には、エージェントのファイルを1つずつ並べる。フォルダは指定できない（M2）。
- このプラグインは、リポジトリのルートの `.github/plugin/marketplace.json`（Copilot CLI）と `.claude-plugin/marketplace.json`（Claude Code）の両方に載せる。

## 3. スキルの共通の決まり

| 項目 | 決まり |
| --- | --- |
| 名前 | フォルダの名前と SKILL.md の `name` を同じにする。スクリプトは、フォルダの名前を自分の名前として使う。名前を変えてコピーすると、設定とログのフォルダも `.<新しい名前>/` になる |
| SKILL.md | description は「何をするか」と「いつ使うか」だけ。本文は、コマンドの表、結果の読み方、最後に「してはいけないこと」の節。詳しいことは `reference/` に分ける |
| スクリプト | `scripts/` に置く。Python 3.8 以上の標準ライブラリだけを使う。ほかのものは `scripts/vendor/` に同梱する |
| 呼び方 | Windows は `py -3`、ほかは `python3`。パスは `<このスキルのフォルダ>/scripts/…` と書く |
| サブコマンド | `run`（作業）、`check`（設定を確かめる。何も作らない）、`init`（設定ファイルをひな形から作る）。`--workdir` で作業フォルダを指定できる |
| 結果 | 標準出力に JSON を1つ。共通の項目は `ok`・`message`・`problems`・`config`（設定ファイルの場所）・`log` |
| 文字コード | JSON は ASCII だけで書く（日本語は `\uXXXX`）。PowerShell も bash も、これなら化けない |
| 終了コード | 0：成功、1：作業の失敗、2：設定や引数の問題 |
| 設定ファイル | `<作業フォルダ>/.<スキルの名前>/config.yaml`。YAML のうち、`項目: 値` と、項目の次の行からの `- 値` のリストだけを使う |
| 読み方 | 各スキルが持つ、同じ中身の関数（`read_yaml`）で読む。値は、数や `true` も文字として読む。BOM があっても読める |
| ログ | `<作業フォルダ>/.<スキルの名前>/logs/` に UTF-8 で書く |
| Git に入れない | `init` と `run` は、`.<スキルの名前>/` に何かを書く前に、その直下に中身が `*` の `.gitignore` を置く |
| パスワード | 項目の名前を `password` などにし、コメントに「（秘密）」と書く。対になる項目として、環境変数の名前を書く `<項目>_env` を置く。環境変数があれば、そちらを使う。結果とログでは伏せる |

YAML の読み取りは、Python の標準ライブラリにない。PyYAML を各スキルに同梱すると大きくなるので、使う書き方だけを読む関数を、各スキルが持つ（REQ-109、憲章 第10条）。
関数が4つのスキルで同じ中身であることと、ひな形を PyYAML でも同じ値に読めることを、`tests/independence/` で確かめる。

## 4. 各スキル

| スキル | すること | 主な設定 |
| --- | --- | --- |
| cross-build | ビルドのコマンドを動かし、結果と、できたファイルの有無・更新の有無・サイズ・ELF の種類を返す。時間切れのときは、子プロセスも止める | `source_dir`、`sdk_env`、`build_command`、`outputs`、`timeout_seconds` |
| ftp-deploy | ファイルを FTP で機器に送る。一時的な名前で送り、サイズを確かめてから名前を変える。必要なら権限を変える（SITE CHMOD）。送り先は `remote_dir` の中だけ。`tls` で FTPS も選べる | `host`、`port`、`user`、`password`、`files`、`remote_dir`、`file_mode`、`tls` |
| serial-console | シリアルで機器のシェルにつなぎ、必要ならログインし、コマンドを順に動かして、出力と終了コードを返す。動かすコマンドは、設定の `commands`、`--command`、`--stdin`（1行に1つ）のどれかで渡す | `port`、`baud`、`login_user`、`login_password`、`prompt`、`commands` |
| skill-config | スキルの名前を受け取り、`<作業フォルダ>/.<名前>/config.yaml` のコメントを読んで、利用者に質問しながら値を書き込む。ファイルがなければ、そのスキルの `init` を使うよう伝える | なし |
| debug-dashboard | Web管理画面の起動と終了。フックから呼ばれて記録と停止をする（4段階目、8章） | なし |

3章の決まりと違うところ：

| スキル | 違うところ | 理由 |
| --- | --- | --- |
| serial-console | pyserial 3.5（BSD ライセンス）を `scripts/vendor/` に同梱する | 標準ライブラリでは、シリアルポートを扱えない |
| skill-config | サブコマンドは `show`（読むだけ。何も作らない）と `set`。SKILL.md に、質問の進め方の節を足す | 自分の設定を持たない |
| skill-config | `set` は、その項目の行だけを書き換える。値は標準入力で渡す | コメントと並びを残す。値がシェルで変わらないようにする |
| debug-dashboard | サブコマンドは `start`・`stop`・`status`。SKILL.md に `disable-model-invocation: true` を書く | 利用者が呼んだときだけ動かす（REQ-401、M4） |

## 5. エージェント

| エージェント | 段階 | 動かし方 | 使うツール |
| --- | --- | --- | --- |
| device-debugger | 2 | メインの会話として動かすか、統括エージェントから依頼される | ファイルの読み書き、検索、コマンド、スキル、利用者への質問 |
| debug-orchestrator | 3 | メインの会話として動かす | 作業エージェントと検証エージェントへの依頼、ファイルの読み取り、検索、利用者への質問 |
| debug-verifier | 3 | 統括エージェントから依頼される | ファイルの読み取り、検索 |

| 決めたこと | 理由 |
| --- | --- |
| 統括エージェントは、メインの会話として動かす | サブエージェントは、利用者に質問できない。依頼の相手を絞る `Agent(…)` も、メインの会話でしか効かない（M5） |
| 統括エージェントの description に「メインの会話としてだけ使う」と書く。本文に、利用者に質問できないときは何も依頼せずに終えるよう書く | サブエージェントとして呼ばれても、作業が進まないようにする |
| 統括エージェントのツールに、ファイルの編集・コマンド・スキルを入れない。依頼できる相手も、作業エージェントと検証エージェントに絞る | REQ-303。絞り方は CLI ごとに違う（6章） |
| 作業エージェントの定義は、使うスキルの名前だけを並べる。frontmatter の `skills` に書く | REQ-202。`skills` に書いたスキルは、最初から読み込まれる（M5、M6） |
| description は「何をするか」と「いつ使うか」だけを書く | CLI は、これを見て任せる相手を決める |
| 本文は、役割 → 進め方 → 報告の形 → 困ったとき（必要なときだけ）→ してはいけないこと の順に書く | どの定義も同じ並びで読める |
| 本文には、ツールの名前を書かない（「利用者に質問する」「エージェントに依頼する」と書く） | 本文を、両方の CLI で同じにできる |

### 5.1 統括エージェントの依頼文

統括エージェントと、作業・検証エージェントの間の取り決めは、統括エージェントの依頼文のひな形にだけ書く。作業エージェントと検証エージェントの定義には書かない。

| 依頼 | 相手 | 報告・判定の形 |
| --- | --- | --- |
| 作業（1回だけ試す） | 作業エージェント | 変えたこと、手順ごとの結果、判断に必要なログの行、ログの場所。設定が足りないときや聞きたいことがあるときは、作業をせず、足りない設定や聞きたいことを書く |
| 評価 | 検証エージェント | 条件ごとに1行、「C<番号>：<判定>。<理由>」。判定は「合格」「不合格」「判断できない」のどれか1つ。最後に「次に試すとよいこと：…」 |

- 作業の依頼文には、「合否は決めない」「利用者には質問しない」と書く（REQ-207）。
- 統括エージェントは、作業エージェントの質問を利用者に聞き、答えを次の依頼文に入れる（REQ-305）。

### 5.2 Issue

振り返りで改善点があれば、統括エージェントは、Issue にするかを利用者に聞く（REQ-309）。

1. タイトルと本文をチャットに出し、公開のリポジトリに載ることを伝えて、直したいところがないかを聞く。
2. タイトルと本文を入れた作成ページの URL（`https://github.com/xclang-brain/plugin-market-place/issues/new?title=…&body=…`）を示す。URL は 8,000 バイトまでにする。
3. 利用者が開いて「作成」を押す。毎回、利用者の承認を得たことになる（憲章 第24条）。

本文には、このプラグインの改善点だけを、一般的な言葉で書く。

## 6. CLI ごとの違い

| 項目 | Claude Code | Copilot CLI |
| --- | --- | --- |
| エージェントの定義 | `com.anthropic.claude-code/agents/<名前>.md` | `com.github.copilot/agents/<名前>.agent.md` |
| エージェントの起動 | `claude --agent embedded-debug-kit:<名前>` | `copilot --agent embedded-debug-kit:<名前>`、または `/agent` で選ぶ |
| 作業エージェントのツール | `Read, Edit, Write, Grep, Glob, Bash, PowerShell, Skill, AskUserQuestion` | `read, edit, search, execute, skill, ask_user` |
| 統括エージェントのツール | `Agent(…), Read, Grep, Glob, AskUserQuestion` | `agent, read, search, ask_user` |
| 検証エージェントのツール | `Read, Grep, Glob` | `read, search` |
| 使うスキル | frontmatter の `skills` | frontmatter の `skills`（同じ） |
| 依頼できる相手 | `Agent(…)` で絞る | 定義のフックで、ほかへの依頼を断る（6.1） |
| 統括エージェントを、ほかのエージェントから呼ばせない | description に書く | `disable-model-invocation: true` |
| 検証エージェントを、利用者が選ぶ一覧に出さない | しない | `user-invocable: false` |
| モデル | `model: inherit` | 書かない（セッションのモデルを使う） |
| 利用者がスキルを呼ぶ | `/<スキルの名前>` | `/embedded-debug-kit:<スキルの名前>` |

- Copilot CLI 版のエージェントは、Claude Code 版から、frontmatter だけを替えて作る。本文は同じにし、テストで確かめる。
- description も同じ文にし、CLI の名前（`claude --agent`）だけを替える。
- フックの違いは 8.2 と 8.3 に書く。Copilot CLI の動きの根拠は M6 にある。

### 6.1 Copilot CLI で依頼の相手を絞るフック

Copilot CLI では、`tools` で依頼の相手を絞れない。そのままでは、コマンドを動かせる組み込みのエージェント（`general-purpose` など）にも依頼できる（M6）。
そのため、統括エージェントの定義にフックを書いて絞る（REQ-303）。

| 項目 | 決めたこと |
| --- | --- |
| 置き場所 | 統括エージェントの frontmatter の `hooks`。PreToolUse で、`matcher: Agent`。統括エージェントが動いている間だけ働く |
| 通す依頼 | 入力に `"agent_type":"embedded-debug-kit:device-debugger"` か `"agent_type":"embedded-debug-kit:debug-verifier"` の文字があるもの。何も出さずに 0 で終える |
| 断る依頼 | それ以外。`permissionDecision` を `deny` にして返す。断った理由はエージェントに届く |
| 書き方 | Python を使わず、`bash` の欄と `powershell` の欄に書き分ける。断る理由の日本語は `\u` で書く（PowerShell の出力の文字コードに左右されないため） |
| 確かめ方 | `tests/independence/` で、bash と PowerShell（Windows の CI）の両方で動かす。書き間違えると、作業・検証エージェントへの依頼も止まるためである |

依頼文の中に名前を書いても通らない。入力は空白のない JSON で渡り、依頼文の中の `"` は `\"` になるためである（M6）。

## 7. 3段階目の記録

3段階目の実行の記録は、CLI が残す会話の記録と、各スキルの `logs/` とする（REQ-311）。統括エージェントは、試行ごとの計画・報告の要約・判定を、終わりの報告に残す。振り返りは、これをもとにする。

## 8. 4段階目：Web管理画面

### 8.1 動き

1. 利用者は、ふつうのセッションで debug-dashboard スキルを呼び、画面を起動する。スキルは、記録中フラグを置き、サーバーをバックグラウンドで起動して URL を返す。
2. 利用者は、画面の「設定」で依頼文を作り、統括エージェントのセッションに送る（REQ-403）。
3. CLI のフックが、依頼、ツールの呼び出し、エージェントの報告を `.debug-dashboard/sessions/<セッション>/events.jsonl` に書く。
4. 画面は、その記録を読んで、進み具合と結果を表示する。依頼文や報告の中身は読まず、そのまま表示する（REQ-409）。
5. 「実行を止める」を押すと、サーバーが、そのセッションの停止フラグを置く。フックは、停止フラグがあればツールの呼び出しを断り、作業を終えるよう伝える（REQ-405）。
6. スキルで画面を終了すると、サーバーが記録中フラグを消す。

- 画面の起動と終了は、統括エージェントとは別のセッションで行う。統括エージェントには、スキルを持たせないためである。
- 停止フラグは、そのセッションで次の依頼が送られたときに、フックが消す。画面の起動と終了のときも、残っているものをすべて消す。
- すでに動いているコマンドは、終わるまで止まらない。

フラグは、あるかないかで状態を表すファイルである。

| フラグ | ファイル | 意味 |
| --- | --- | --- |
| 記録中フラグ | `.debug-dashboard/recording` | 記録中。中身は、サーバーのプロセス番号・ポート・更新の時刻。サーバーが 30 秒ごとに時刻を書き直す。フックは、2 分より古いものを、ないものとして扱う |
| 停止フラグ | `.debug-dashboard/sessions/<セッション>/stop` | このセッションのツールの呼び出しを断る |
| 報告済みフラグ | `.debug-dashboard/sessions/<セッション>/handback-<agent_id>` | このサブエージェントの最後の報告を、`SubagentHandback` で記録した。同じ `agent_id` の SubagentStop で消す（8.2） |

`.debug-dashboard/` の決まり：

- 作るときは、まず中身が `*` の `.gitignore` を置く。
- 本人だけが読み書きできる権限で作る（Windows では、作業フォルダの権限を引き継ぐ）。
- `.debug-dashboard/` か、その中のフォルダがシンボリックリンクなら、フックもサーバーも何もしない。

### 8.2 フック

| イベント | すること |
| --- | --- |
| UserPromptSubmit | 新しい依頼として記録する。このセッションの停止フラグを消す。ただし、`<task-notification>` で始まるものは、バックグラウンドのサブエージェントが終わった知らせなので、何もしない |
| PreToolUse | ツールの名前と入力を記録する（8.4）。停止フラグがあれば断り、断ったことも記録する。`SubagentHandback` は断らず、その `message` をサブエージェントの最後の報告として記録する |
| SubagentStop | エージェントの名前と、最後の報告を記録する |
| Stop | メインの会話の最後の報告を記録する。ただし、`background_tasks` に種類が `subagent` のものがあれば、まだサブエージェントを待っているので記録しない |

| 決めたこと | 理由 |
| --- | --- |
| サブエージェントかどうかは、入力の `agent_id` があるかで見分ける。どのエージェントかは、`agent_type` の最後の `:` の後ろで見分ける | `--agent` で動かしたメインの会話の入力には、`agent_id` がない（M8.1） |
| SubagentStop は、`agent_type` が空でなく、同じセッションでそのエージェントへの依頼を記録しているときだけ記録する | CLI が中で動かすエージェント（入力の候補、`/btw`）も、SubagentStop を起こす（M8.1） |
| `SubagentHandback` を記録したら報告済みフラグを置き、同じ `agent_id` の SubagentStop は記録しない | 自動モードでは、最後の報告は `SubagentHandback` で渡り、SubagentStop には締めの一言しか入らない（M8.1） |
| `SubagentHandback` は、停止フラグがあっても断らない | 断ると、報告が届かなくなる |
| 作業フォルダは、入力の `cwd` ではなく、環境変数 `CLAUDE_PROJECT_DIR`（Copilot CLI は `COPILOT_PROJECT_DIR` も）で決める | エージェントが途中でフォルダを移っても、記録と停止が続く |
| 断るときも、JSON を出して終了コード 0 で終える。記録に失敗しても、ツールの呼び出しは通す | 断るのは、停止フラグがあるときだけにする（REQ-408） |

Copilot CLI では、イベントを PascalCase の名前（`UserPromptSubmit` など）で登録する。こうすると、入力の項目の名前が Claude Code と同じ snake_case になり、同じ hook.py を使える。
それでも残る違いは、hook.py で吸収する（M8.2）。

| Copilot CLI の動き | 対応 |
| --- | --- |
| サブエージェントの中のフックでは、`session_id` がサブエージェント自身の ID になる | `parent_agent_id` のセッションに記録し、そのセッションの停止フラグで断る |
| サブエージェントも、始めに UserPromptSubmit、終わりに Stop を起こす（`agent_id` つき） | 記録せず、停止フラグも消さない。依頼と報告は、PreToolUse（`Agent`）と SubagentStop で記録する |
| Stop の入力に、最後の報告がない | 入力の `transcript_path` の会話の記録から読む（下の手順） |
| 作業フォルダの環境変数が、Git のリポジトリのいちばん上のフォルダになる。エージェントのシェルには渡らない | debug-dashboard のスクリプトは、`COPILOT_CLI` があるとき、今のフォルダから上へ `.git` を探し、見つかったフォルダを作業フォルダにする（REQ-407） |

Copilot CLI の最後の報告は、会話の記録（1行に1件の JSON）から、次の手順で読む。見るのは、メインのエージェントの行（`agentId` がなく、`checkpoint` でない）だけである。

1. ターンの終わりを探す。`type` が `hook.start`、`data.hookType` が `agentStop`、`timestamp` が入力と同じ行である。
2. ターンの始まりを探す。その前の、`type` が `user.message` の行か、1 と同じ形で `timestamp` が違う行（前のターンの終わり）である。
3. 始まりと終わりの間の、最後の発言（`type` が `assistant.message`、`data.content` が空でない）を、最後の報告にする。なければ記録しない。
4. 終わりの行は少し遅れて書かれるので、3 秒まで待つ。書かれなければ記録しない。

会話の記録の形は、公式文書にない。Copilot CLI のバージョンを上げたら、確かめ直す（M8.2）。

### 8.3 フックの呼び方

フックは、画面を使っていないときも、すべてのツールの呼び出しで動く。
そのため、hooks.json には短いコマンドだけを書き、処理は debug-dashboard スキルの `scripts/` の3つのファイルに置く。

| ファイル | 使う CLI | すること |
| --- | --- | --- |
| `hook.sh` | Claude Code と、Windows 以外の Copilot CLI | 記録中フラグがなければ、Python を起動せずに 0 で終える（REQ-408）。あれば hook.py を動かす。失敗しても 0 で終える |
| `hook.ps1` | Windows の Copilot CLI | hook.sh と同じ |
| `hook.py` | 上の2つから呼ばれる | 記録と停止（8.2） |

hooks.json に書くコマンドは、イベントごとに次の形にする。違うのは、最後のイベントの名前だけである。

| CLI | コマンド |
| --- | --- |
| Claude Code | `bash "${CLAUDE_PLUGIN_ROOT}/skills/debug-dashboard/scripts/hook.sh" <イベント>` |
| Copilot CLI の `bash` の欄 | `bash "$PLUGIN_ROOT/skills/debug-dashboard/scripts/hook.sh" <イベント> \|\| true` |
| Copilot CLI の `powershell` の欄 | hook.ps1 を読み、スクリプトブロックにして、イベントの名前を渡して動かす。最後に `exit 0` |

| 決めたこと | 理由 |
| --- | --- |
| Python は、bash では `py -3`・`python3`、PowerShell では `py -3`・`python`・`python3` の順に試す | Windows には py ランチャー、ほかには `python3` が入っている |
| Copilot CLI のコマンドは、必ず 0 で終える | Copilot CLI は、PreToolUse のフックが 0 以外で終わるか落ちると、ツールの呼び出しを断る（M8.3） |
| Claude Code の PreToolUse には、`"onFailure": "continue"` を付ける | Git Bash のない Windows では、フックが PowerShell で動いて失敗し、ツールの呼び出しが止まることがある（M8.1） |
| hook.ps1 は、ファイルとして動かさず、読んでスクリプトブロックにする | PowerShell の実行ポリシーが `.ps1` の実行を禁じていても動く（Windows の初めの設定は、禁じている） |
| hook.ps1 では、`$input` を使わない | 使うと、PowerShell が先に標準入力を読み、文字コードを変えてしまう。Python は、標準入力を受け継いで、UTF-8 のバイトのまま読む（M8.3） |
| Copilot CLI では、プラグインのフォルダを環境変数（`$PLUGIN_ROOT`、`$env:PLUGIN_ROOT`）で読む | `${PLUGIN_ROOT}` と書くと、Copilot CLI が文字として置き換える。フォルダの名前に引用符などがあると壊れる（M8.3） |
| `*.sh` の改行は、`.gitattributes` で LF に固定する | Windows の Git が改行を CRLF に替えると、bash が読めない |
| 時間切れは 10 秒にする | 記録が遅れても、ツールの呼び出しを長く待たせない |

Claude Code では、プラグインなしでも使える。次の2つをしてから、CLI を起動し直す。Copilot CLI は、プラグインとしてインストールしたときだけ記録する。

1. `skills/debug-dashboard/` フォルダを、`<プロジェクト>/.claude/skills/debug-dashboard/` にコピーする。フォルダの名前は変えない（フックの設定が、この名前を使う）。
2. `com.anthropic.claude-code/hooks/hooks.json` の `hooks` の中身を、`<プロジェクト>/.claude/settings.json` の `hooks` に写す。写したら、`${CLAUDE_PLUGIN_ROOT}/skills` を `${CLAUDE_PROJECT_DIR}/.claude/skills` に書き換える。

断るときの出力は、CLI ごとの形にする。Claude Code は `hookSpecificOutput` の `permissionDecision`、Copilot CLI は `permissionDecision` である。どちらの CLI かは、Copilot CLI が渡す環境変数 `COPILOT_CLI` で見分ける。

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
| 設定 | 直したいこと・合格の条件・試行の上限。入力すると、依頼文と「コピー」ボタンが出る |
| 進み具合 | 今のフェーズ（計画・作業・評価）、依頼文と報告・判定を届いた順に、使われたツール（新しい順）、「実行を止める」ボタン |
| 結果 | 統括エージェントの最後の報告、検証エージェントの最後の判定、前のセッションの一覧 |

- 今のフェーズは、動いているエージェントの名前で決める。検証エージェントが動いていれば評価、作業エージェントが動いていれば作業、どちらも動いていなければ計画とする。
- Copilot CLI では、統括エージェントの最後の報告を読めないことがある（8.2 の手順 4）。そのときは、「結果」のタブに最後の報告が出ない。
- 検証エージェントの判定は、どちらの CLI でも、SubagentStop の入力から記録する。

## 9. テスト

| 種類 | 置き場所 | 中身 |
| --- | --- | --- |
| スキルの単体テスト | `skills/<名前>/tests/` | スクリプトの動き。偽のビルド、FTP のテスト用サーバー、模擬のシリアルの機器を使う。フォルダだけをコピーしても動く。skill-config のテストは、書いた設定ファイルを PyYAML で読んで確かめる |
| Web管理画面 | `skills/debug-dashboard/tests/` | 記録と停止、パスワードを伏せること、記録中フラグがないときと古いときに何もしないこと、サーバーのアクセス制限（トークン・Host・Origin）、記録の中の HTML が動かないこと |
| 独立性 | `tests/independence/` | 1段階目のスキルが外を参照しないこと。上の段階が下の段階の中身を書かないこと。作業エージェントがスキルの名前だけを持つこと。両方の CLI のエージェントの本文が同じで、Copilot CLI の frontmatter（ツール、スキル）が合うこと。Copilot CLI の統括エージェントのフックが、作業・検証エージェントへの依頼だけを通すこと。各ファイルの最後に「してはいけないこと」の節があること |
| 文書 | `tests/docs/` | バージョンの一致（両方のマーケットプレイスを含む）と、要求の追跡 |
| スキルの選ばれ方 | `skills/<名前>/evals/`、`evals/` | `claude plugin eval` の形（prompt.md と graders/）のケースを、`claude plugin eval .` で動かす。ふつうの依頼から、そのスキルが選ばれるか（スキルのフォルダだけでも動く）。関係のない依頼や Web管理画面を開く依頼で、スキルが選ばれないか |
| 通しのテスト（E2E） | `tests/e2e/` | 模擬の機器と `claude -p` で、2段階目と3段階目が不具合を直せるか。停止フラグでツールが断られるか。AI の利用料がかかるので、CI では動かさない |

テストの動かし方（プラグインのフォルダで）：

```shell
python3 -m unittest discover -s tests/independence -v
python3 -m unittest discover -s tests/docs -v
claude plugin validate .
```

- スキルのテストは、スキルのフォルダで `python3 -m unittest discover -s tests` を動かす。ftp-deploy と skill-config は、先に `python3 -m pip install -r tests/requirements.txt` を動かす。
- 通しのテスト（`tests/e2e/run_e2e.py`）と `claude plugin eval .` は、AI の利用料がかかる。

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
| REQ-101〜REQ-110 | 3、4 | `skills/<1段階目>/` | スキルの単体テスト、`claude plugin eval`（ふつうの依頼でスキルが選ばれるか）、`tests/independence/`（REQ-108） |
| REQ-111 | 3、4 | `skills/skill-config/` | skill-config の単体テスト、`claude plugin eval` |
| REQ-201〜REQ-209 | 5 | 作業エージェントの定義 | `tests/e2e/`（直せるか、スキルを使ったか、デバッグ用の出力を消したか）、`tests/independence/`（REQ-202）、ほかは人が読む |
| REQ-301〜REQ-308、REQ-310 | 5、5.1、6 | 統括・検証エージェントの定義 | `tests/e2e/`（直せるか、依頼の相手、自分で直さないこと、結論と条件ごとの判定）、`tests/independence/`（REQ-303 の Copilot CLI のフック、REQ-304）、ほかは人が読む |
| REQ-309 | 5.2 | 統括エージェントの定義 | `tests/e2e/`（振り返り）、人が読む（Issue の流れ） |
| REQ-311 | 7 | 統括エージェントの定義 | 人が読む |
| REQ-401〜REQ-409 | 8 | `skills/debug-dashboard/`、フックの設定 | debug-dashboard の単体テスト、`tests/e2e/`、`claude plugin eval`（AI が画面を勝手に起動しないか）、`tests/independence/`（REQ-409）。REQ-403 の依頼文は、人が画面で確かめる |
