# embedded-debug-loop 設計書

内部のしくみと、部品の間の取り決め（インターフェース）を書く。
利用者から見た振る舞いは [仕様書](../specs/requirements.md) にある。

- バージョン：0.3.0

## 1. 基本の考え方

> **組み込み機器に固有のことは、すべて作業エージェント（`device-worker`）が受け持つ。**

部品は、3つの段階（部品の階層）に分かれる（2章の図）。
上の段階は、下の段階の部品を名前だけで参照する（中身に依存しない）。
そのため、作業エージェントを置き換えれば別の機器でも動き、スキルを置き換えても上の段階は変わらない。
どの段階も、上の段階なしで使える（使い方は 2.1 の表）。
文書も同じ考え方で分ける。各部品の使い方は、その部品の README にだけ書く（3.7）。

## 2. アーキテクチャ

```mermaid
flowchart TB
    subgraph L3["3段階目　機器に依存しない PDS（計画 → 作業 → 評価）のしくみ"]
        direction LR
        UI["［エクステンション］<br/>Web管理画面<br/>設定・実行のようす・結果"]
        ORC["［カスタムエージェント］<br/>統括エージェント（orchestrator）<br/>計画を立て、評価を記録する<br/>（ファイルは編集しない）"]
        VER["［カスタムエージェント］<br/>検証エージェント（verifier）<br/>合格の条件ごとに確認する<br/>（成果物は編集しない）"]
        UI <-- "記録" --> ORC
        ORC -- "依頼する" --> VER
    end

    subgraph L2["2段階目　機器に固有のことをすべて受け持つ（セットアップも依頼できる）"]
        WRK["［カスタムエージェント］<br/>作業エージェント（device-worker）<br/>修正 → ビルド → 転送 → ログ取得 を繰り返す<br/>スキルは名前だけで参照する（「使うスキル」の表）"]
    end

    subgraph L1["1段階目　互いに依存しない4つのスキル"]
        direction LR
        SETUP["［スキル］<br/>setup<br/>スキルの README を読み、設定を対話で埋める"]
        CC["［スキル］<br/>cross-compile<br/>SDK の環境を読み込んでビルド"]
        DEP["［スキル］<br/>deploy<br/>FTP で送る・権限を変える<br/>サム値（チェックサム）のファイルを作る"]
        LOG["［スキル］<br/>log-capture<br/>シリアルでコマンドを実行・記録"]
        SETUP ~~~ CC ~~~ DEP ~~~ LOG
    end

    CFG[("［設定ファイル（データ）］<br/>.#lt;スキル名#gt;/config.yaml<br/>スキルごとの設定（説明のコメントつき）")]
    DEV[["［プラグインの外］<br/>組み込み機器（Linux）<br/>FTP サーバー・シリアルコンソール"]]

    L3 -- "自然言語で依頼する（準備の確認 / 1回の試行）<br/>作業エージェントは設定で選ぶので、置き換えられる" --> L2
    L2 -- "名前でスキルを読み、書かれたとおりに使う" --> L1
    L1 -. "設定を読み書きする" .- CFG
    L1 -- "FTP で送る・シリアルでログを取る" --> DEV

    style L3 fill:#eee8f8,stroke:#6a4aa8,color:#1b2230
    style L2 fill:#e2edf6,stroke:#1d5e8c,color:#1b2230
    style L1 fill:#e3f2e8,stroke:#2f7d4f,color:#1b2230
    classDef node fill:#ffffff,color:#1b2230
    classDef dev fill:#f7eddc,stroke:#8a5a12,color:#1b2230
    classDef cfg fill:#ffffff,stroke:#586275,color:#1b2230,stroke-dasharray:5 4
    class UI,ORC,VER,WRK,SETUP,CC,DEP,LOG node
    class DEV dev
    class CFG cfg
```

### 2.1 ブロックごとの置き場所と知識

| ブロック | 置き場所 | 使う Copilot CLI の機能 | 持っている知識 |
| --- | --- | --- | --- |
| Web管理画面 | `extensions/embedded-debug-loop/` | 4章を参照 | 統括エージェント向けのツール、画面、Copilot CLI のイベント。既定の作業エージェントの名前は `defaults.json` にだけ置く |
| 統括エージェント | `agents/orchestrator.agent.md` | `/agent` で選ぶ。`tools`：`agent`（`task` で作業エージェントと検証エージェントを呼ぶ）・`read`・`search`・`ask_user`・エクステンションのツール | PDS の進め方。作業エージェントの名前は設定から受け取る |
| 検証エージェント | `agents/verifier.agent.md` | 統括エージェントが `task` で呼ぶ。`tools`：`read`・`search` | 合格の条件ごとに確認する方法 |
| 作業エージェント | `agents/device-worker.agent.md` | `/agent` で選ぶか、`task` で呼ばれる。`tools`：`read`・`edit`・`search`・`execute`・`skill`・`ask_user` | スキルの名前と役割（「使うスキル」の表） |
| 4つのスキル | `skills/<スキル名>/` | `/embedded-debug-loop:<スキル名>`（フォルダだけをコピーしたときは `/<スキル名>`）か `skill` ツール。スクリプトはシェルのツールで実行する。setup は `ask_user` で質問する | 自分のことだけ。setup は、設定するスキルを探し、そのスキルの README と設定ファイルのコメントから、設定の内容を読み取る（3.3） |

- `agents/` と `extensions/` は、`com.github.copilot/` の下にある。
- スキルのフォルダには、`SKILL.md`、`scripts/` の Python のスクリプト（setup は `config_tool.py`、cross-compile は `build.py`、deploy は `deploy.py`、log-capture は `capture.py`）、使い方の `README.md`、`tests/` がある。setup のほかは、設定のひな形 `config-template.yaml` も持つ。
- 作業エージェントの使い方は `agents/README.markdown` に書く。拡張子が `.md` だと、Copilot CLI がエージェントの定義として読み込み、エラーを出す（1.0.105 で確認）。`.markdown` は読み込まれない（1.0.105 で、プロジェクトの `.github/agents/` とプラグインの `com.github.copilot/agents/` の両方で確認）。
- 使っていない部品：MCP サーバー、プラグインのフックの設定（`hooks.json`。フックはエクステンションで登録する）、LSP、指示ファイル。
- スキルのスクリプトは、Python 3.8 以上の標準ライブラリだけで書く。log-capture だけは、pyserial 3.5（BSD ライセンス）を `scripts/vendor/serial/` に同梱し、自分のフォルダの `vendor` を読み込みの道の先頭に足して使う。マニフェストは `plugin.json`（Agent Plugins の形式）。

## 3. 部品の間の取り決め

### 3.1 作業エージェントとスキル

作業エージェントは、スキルを `skill` ツールで読み、`SKILL.md` のとおりに使う。
スクリプトの名前も `SKILL.md` から知る（作業エージェントの定義には書かない）。
作業エージェントがスキルに頼むのは、作業と設定の確認だけである。設定は、setup に任せる（3.3）。
ビルド・転送・ログ取得のスクリプトは、次のサブコマンドを持つ。

| 用途 | 呼び方 | 返す JSON（主なキー） |
| --- | --- | --- |
| 作業 | `<スクリプト> run` | `ok`、`message`、`log`、スキルごとの結果 |
| 設定の確認 | `<スクリプト> check` | `ok`、`problems`、`summary` |
| 設定ファイルの用意（README に書いた作り方） | `<スクリプト> config-path` | `config`、`created`、`missing` |

- 標準出力に JSON を1つだけ出す。経過は標準エラーに出す。
- 終了コード：0 は成功、1 は作業の失敗、2 は設定の問題（`check` では、機器に接続できないときも 2）。
- `config-path` は、ふつう 0（足りない項目は `missing`、読めないなどの問題は `problems` に入れる）。作業フォルダがない、設定ファイルを作れないなど、続けられないときは 2 を返すスキルもある。引数の誤りは 2。

### 3.2 設定ファイル

- 置き場所：作業フォルダ（ふつうは、Copilot CLI を起動したフォルダ）の `.<スキル名>/config.yaml`。ログは `.<スキル名>/logs/`。フォルダには `.gitignore`（中身は `*`）を作る。
- スクリプトは、カレントディレクトリを作業フォルダとする。`--workdir` で作業フォルダを、`--config` で設定ファイルを変えられる。
- スキル名は、スクリプトのパスから求めたスキルのフォルダの名前（別の名前でコピーすれば、その名前）。プラグインの名前は使わない（3.4）。
- スキル名は、結果の JSON の `skill`、ログファイルの名前の先頭、経過の先頭の `[名前]`（付けるスキルだけ）にも使う。
- 形式：入れ子のない `項目: 値`。値は、文字列・数値・true/false・null・文字列のリスト。各項目のすぐ上に `#` で説明を書く。
- 設定ファイルがなければ、ひな形をコピーして作る。YAML を読むコードは、スキルごとに持つ。

### 3.3 setup とほかのスキル

setup は、設定ファイルを持たず、特定のスキルの名前・項目・置き場所を知らない。
設定するスキルの README を読み、そこに書かれたことだけを頼りに設定を案内する。

1. 設定するスキルを決める。頼みに名前がなければ、`config_tool.py skills` で探し、候補を ask_user で選んでもらう。
2. スキルごとに `README.md`（なければ `SKILL.md`）を読み、設定ファイルの有無・場所・作り方・項目の意味を知る。
3. 設定ファイルがなければ、README に書かれた方法（例：README にあるコマンド）で作る。
4. 項目のすぐ上にコメントがある YAML（3.2 の形式）なら、`config_tool.py show`・`set` で埋める。ほかの形式なら、README の説明をもとに、聞いた値をファイルに書く（コメントと並びを崩さない）。
5. スキルごとに、埋めた結果を表で見せる（パスワードはマスクする）。

`config_tool.py skills` は、スキルのフォルダ（`SKILL.md` があるフォルダ）を次の順に探し、名前（`SKILL.md` の `name:`）、場所、どこにあるか、README の場所、説明（`description`）、同じ名前で隠れているかを JSON で返す。setup 自身は返さない。

| 順 | 場所 | どこにあるか |
| --- | --- | --- |
| 1 | 作業フォルダの `.github/skills/` | project（Copilot CLI は、これを優先する） |
| 2 | `<COPILOT_HOME（なければ ~/.copilot）>/skills/` | personal |
| 3 | setup のフォルダの隣のフォルダ | 隣（プラグインに入っているときは、同じプラグインのスキル） |

同じ名前のスキルが2つ以上あれば、先に見つかったものを使い、後のものは「隠れている」とする。

### 3.4 スキルのフォルダ

スキルのフォルダは、それだけで完結させる。プラグインをインストールせず、フォルダだけをコピーして使う人がいるためである。
フォルダの中のファイル（コメントとエラーメッセージを含む）には、次を書かない：プラグインの名前とその略、ほかのスキル・エージェント・Web管理画面・エクステンション、フォルダの外を指すパス。

### 3.5 統括エージェントと作業エージェント

2つのエージェントは、自然言語の依頼だけでやり取りする。依頼は2種類ある。

| 依頼 | 作業エージェントの応答 |
| --- | --- |
| 準備の確認（作業はしない） | 1行目が「準備：できている」か「準備：できていない」。続けて、設定の要約と足りないもの |
| 試行（1回だけ。合否は判断しない） | 試行の番号、変えたこと、ビルド・転送・ログ取得の結果、ログの要点、ログの場所、気づいたこと |

作業エージェントは、次のどちらかに当たれば、ほかのエージェントからの依頼として扱う。依頼されたことだけを行い、利用者には質問しない。

- `ask_user` が使えない。Copilot CLI は、`task` で呼ばれたサブエージェントに `ask_user` を渡さない（1.0.98 と 1.0.102 で確認）。
- 依頼の文が「これは準備の確認です」か「あなたは作業役です」で始まる（統括エージェントの依頼のひな形の書き出し）。

### 3.6 統括エージェントとエクステンション

| 統括エージェントが使うツール（名前の頭は `embedded-debug-loop_`） | 呼ぶとき | すること |
| --- | --- | --- |
| `open` | 依頼を受けたとき | 画面のサーバーを起動し、依頼と合格の条件を「開始」前の下書きに入れ、今の設定を返す。作業エージェントの名前を渡されたら、設定に保存する |
| `prepare` | 準備の確認の応答を受けたとき | 応答を Web管理画面の「設定」に出す |
| `plan` | 試行の始め | 計画を記録する |
| `study` | 評価の後 | 条件ごとの合否と、次にすることを記録する |
| `finish` | 終わるとき | 結論と振り返りを記録する |
| `issue` | 利用者が Issue の作成に同意したとき | `gh` で Issue を作成する |

「開始」の後、エクステンションは次のことをする。

- `session.send` で開始のメッセージをチャットに送る。統括エージェントは、これを受けて試行を始める。
- 作業エージェントと検証エージェントのモデルと考える深さを、`tools.updateSubagentSettings` で設定する（定義ファイルは書き換えない）。
- 2つのエージェントの応答を、フック `onSubagentStop` で受け取り、試行の記録に入れる。
- フック `onPreToolUse` で、統括エージェントのファイル編集（edit など）・コマンド（bash など）・スキル（skill）のツールを拒否する。
- 作業の報告がある試行で、新しい計画なしに作業エージェントを呼ぶことも拒否する（報告と評価が食い違うため）。

「止める」が押されたら、`session.abort()` で今の応答を止める。その後は、`embedded-debug-loop_finish` 以外のツールをすべて拒否する。
統括エージェントが終わりを記録せずに止まったら、エクステンションが結論を書く。結論には、止めた試行の番号と工程（計画・作業・評価）と、編集のツール（edit・create・apply_patch）で変えたファイルを書く。変えたファイルは元に戻さない。

試行の記録に入れなかった作業・検証エージェントの応答（止めた後や、計画の記録の前に届いたものなど）は、理由を付けて `events.jsonl`（4章）にだけ残す。

#### 統括エージェントの tools の制限

統括エージェントにファイル編集・コマンド・スキルを使わせないため、3つの対策を重ねる。
定義の `tools` を絞るだけでは足りない。`tools` に `skill` がなくても、Copilot CLI は `skill` のツールとスキルの一覧を渡すためである。

1. 定義の `tools` を、エージェントの呼び出し・読み取り・検索・質問・記録のツールに絞る。
2. 定義で、自分では作業しない（ファイルを編集しない、コマンドを実行しない）と指示する。作業の方法と、作業エージェントの応答に出た道具・スキル・設定ファイル・手順の名前も、計画や依頼に書かせない。
3. 「開始」の後は、エクステンションがこれらのツールを拒否する。

なお、Copilot CLI 1.0.105 では、統括エージェントの `tools` を絞っても、それが呼ぶ作業エージェントは自分の `tools` を使えた（テストで確認）。

### 3.7 文書の置き場所

各部品の使い方は、その部品の README にだけ書く。下の段の部品を置き換えても、上の段の文書を直さずに済むようにするためである。

| 部品 | 文書 | 書くこと |
| --- | --- | --- |
| スキル | `skills/<名前>/README.md` | 何をするか、必要なもの、置き方、呼び方、設定（場所・作り方・項目の意味）、うまくいかないとき、テスト。フォルダだけで読める（3.4） |
| 作業エージェント | `agents/README.markdown` | 何をするか、選び方、頼み方の例、使うスキルの名前と役割、うまくいかないとき。統括・検証エージェントは、3段階目のガイドへの案内だけ |
| 3段階目 | `docs/guide/3-orchestrator.md` | 統括エージェント・検証エージェント・Web管理画面の使い方 |
| プラグイン全体 | `README.md` | コンセプト、3つの段階と見る README、必要なもの、インストール、置き換えと開発者向けの文書への案内 |
| 置き換え | `docs/guide/replace.md` | スキルと作業エージェントの置き換え方と、それぞれが守る約束 |

- 上の段の文書（`README.md`、`agents/README.markdown` の統括・検証エージェントの部分、`3-orchestrator.md`、`replace.md`）には、スキルの名前と、転送・ログ取得の具体（FTP、シリアル、COM ポートなど）を、例としても書かない。
- `agents/README.markdown` は、スキルの名前を「使うスキル」の表にだけ書く。転送・ログ取得の具体と、設定の項目は書かない（作業エージェントの定義と同じ知識に留める）。
- どちらも、独立性のテスト（5章）で確かめる。

### 3.8 Windows への対応

利用者の PC は Windows である。Copilot CLI は、Windows ではコマンドを PowerShell（6 以上）で実行する。Linux でも同じスキルが動く。

| 項目 | 決まり |
| --- | --- |
| SKILL.md のコマンド | bash だけの書き方（ヒアドキュメント、`source`、`$(...)` など）に頼らない。Python は `<python>` と書き、Windows では `py -3`（なければ `python`）、ほかでは `python3` と決める |
| 標準出力 | Python の既定の文字コードで、`sys.stdout` に文字列で書く（`errors="backslashreplace"`）。UTF-8 のバイト列を直接書かない（日本語の Windows の PowerShell は cp932 で読むため） |
| ファイル | 設定ファイルとログは、いつも UTF-8 で読み書きする |
| 標準入力（setup の `set --stdin`） | バイト列で読み、UTF-8（BOM は外す）で読めなければ OS の既定の文字コードで読む。最後の改行1つ（`\r\n` か `\n`）を外す。SKILL.md には、PowerShell の here-string をパイプで渡す書き方と、bash のヒアドキュメントを両方書く |
| パス | `os.path`・`pathlib` で扱い、区切りの `/` を決め打ちしない（機器の中のパスは別。機器は Linux） |
| cross-compile のシェル | 設定の `shell`（`auto`・`cmd`・`powershell`・`bash`）。`auto` は Windows では `cmd`、ほかでは `bash`。`sdk_env` は `call`（cmd）・`.`（powershell）・`source`（bash）で読み込む。時間切れでは、Windows でも子のプロセスまで止める（`taskkill /T /F`） |
| log-capture のポート | pyserial の `serial_for_url` で開く。`COM3`・`/dev/ttyUSB0`・`socket://<host>:<port>`（テスト用）を、同じ `port` の項目で扱う。ポートが見つからないときの候補は `serial.tools.list_ports.comports()` で出す |

スキルのテストは、GitHub Actions の Windows のマシンでも動かす（5章）。Copilot CLI と組み合わせた Windows での動作は、まだ確認していない。

## 4. エクステンションの中

追加のパッケージは使わない。

| ファイル | 役目 |
| --- | --- |
| `extension.mjs` | 入口。Copilot SDK の `joinSession` を `adapter.mjs` に渡す。SIGTERM・SIGINT では、記録を書き終え、サーバーを閉じて終わる |
| `adapter.mjs` | Copilot CLI との接続部分。ツール・フック・イベント（`session.on`）を登録する。`session` の `send`・`abort`・`log`（チャットへの通知）・`rpc`（エージェントとモデルの一覧、エージェントの切り替え、サブエージェントの設定）を `core/control.mjs` に渡す |
| `defaults.json` | 既定の値（3つのエージェントの名前、繰り返しの上限、Issue の作成先）。コードには書かない |
| `core/messages.mjs` | モデルと利用者に見せる文 |
| `core/store.mjs` | 状態（設定・下書き・今の実行・過去の実行の一覧）と、画面に渡すスナップショット |
| `core/persist.mjs` | 記録のファイルの読み書き |
| `core/tools.mjs` | 6つのツール（3.6） |
| `core/events.mjs` | イベントから、試行の記録、動いているエージェント、入力待ち（質問・許可）の通知、画面の「くわしい記録」の行を作る |
| `core/guard.mjs` | `onPreToolUse` で拒否するかを決める（3.6） |
| `core/control.mjs` | 画面からの操作（下の API） |
| `ui/server.mjs` | 画面のサーバー。127.0.0.1 だけで待ち受け、トークン（URL の `k` か、初回に渡すクッキー）が合わない要求を拒否する |
| `ui/static/` | 画面（`index.html`・`app.js`・`style.css`）。状態を 1.5 秒ごと（隠れているときは 5 秒ごと）に読む |

画面のサーバーの API：

| API | すること |
| --- | --- |
| `GET /api/state` | スナップショット（選べるエージェントとモデルを含む）を返す |
| `POST /api/settings` | 設定を保存する |
| `POST /api/start` | 開始する |
| `POST /api/stop` | 止める |
| `GET /api/runs/<実行の番号>` | 過去の実行の記録を返す |

記録の置き場所は `<COPILOT_HOME または ~/.copilot>/plugin-data/embedded-debug-loop/`。

- `settings.json`：Web管理画面の設定。`folders` のキーが作業フォルダのパス、値がその設定。
  - この作業フォルダは、セッションの作業フォルダ（`/cd` で変わる）。取得できなければ、エクステンションのプロセスの今のフォルダ（`process.cwd()`）。
  - 設定がなければ、`defaults.json` の値を使う。フォルダで分けていない古い形式は読まない。
- `runs/<実行の番号>/run.json`：実行の記録。
- `runs/<実行の番号>/events.jsonl`：イベントのログ（1行に1件の JSON。拒否したツールなども残す）。振り返りに使う。

## 5. テスト

プラグインのフォルダで `python3 tests/run.py`（Windows では `py -3 tests/run.py`）を実行すると、すべてのテストが動く。
すべて合格なら終了コード 0、不合格か動かせないテストがあれば 1。

| テスト | 置き場所 | 確認すること |
| --- | --- | --- |
| スキル | `skills/<スキル名>/tests/` | スクリプトの振る舞い（テストの中で立てる FTP サーバーと、TCP で動く模擬のシリアルコンソールを使う）。フォルダだけをコピーしても、`python -m unittest discover -s tests` で動く |
| 独立性 | `tests/independence/` | 段階をまたぐ依存がない。スキルのフォルダが 3.4 の決まりを守る。スキルのフォルダ1つだけを（名前を変えても）コピーして動き、データは `.<フォルダの名前>/` にだけできる。上の段の文書に下の段の中身がない（3.7）。スキルを、別の中身・別の設定の形のスキルに置き換えても、setup の `skills` が見つけ、作業エージェント・統括エージェントの定義を変えずに済む |
| エクステンション | `tests/extension/` | 模擬のセッションで、ツール・開始・イベント・停止・API・`onPreToolUse` の拒否。模擬の DOM で、画面（`app.js`）。3段階目の独立性（作業の中身を参照せず、`defaults.json` の作業エージェントの名前を置き換えても動く） |

GitHub Actions（リポジトリの根の `.github/workflows/embedded-debug-loop.yml`）でも、次のテストを自動で動かす。

| ジョブ | マシン | 動かすもの |
| --- | --- | --- |
| スキル | `windows-latest`・`ubuntu-latest` × Python 3.8・3.12 | 各スキルのテスト（スキルのフォルダで `python -m unittest discover -s tests`）。pyftpdlib を入れて、deploy の FTP のテストも動かす |
| 独立性 | `windows-latest`・`ubuntu-latest` × Python 3.12 | `tests/independence/` |
| Web管理画面 | `ubuntu-latest`・`windows-latest` × Node 20 | `tests/extension/` |
| 標準入力と文字コード | `windows-latest` | PowerShell から setup の `set --stdin` に日本語の値を渡し、設定ファイル（UTF-8）と標準出力が正しいかを確かめる |
