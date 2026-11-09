# coverity-triage-kit 設計書

内部のしくみと、部品の間の取り決め（インターフェース）を書く。
利用者から見た振る舞いは [仕様書](../specs/requirements.md) にある。

- バージョン：0.1.0
- この設計書は、部品を役割で呼ぶ（「一括実行のスキル」など）。今の部品の名前との対応は、開発者向けの [部品の変え方](../replace.md) にだけある。名前を書くのは、取り決めの名前（MCP サーバーの登録名、ツールの名前、`defaults.json`）だけにする。

## 1. 基本の考え方

> **部品は、それぞれのフォルダだけで仕事ができる。部品どうしは、決まった形のファイルとコマンドと自然な文だけでつながる。**

- 部品は4つの段階に分かれる（2章の図）。下の段階は上の段階を知らない。上の段階は、下の段階の部品を、「使う部品」の表と設定のデータでだけ名前で参照する。
- 判断の要らない決まった処理（実行フォルダ、グループ分け、作業用のコピー、差分、一覧、反映、形の確かめ）は、スクリプトが受け持つ。判断（真偽、修正のコード、逸脱コメント、同じ原因か、確かめの合否）は、エージェントがスキルの手順で行う。Coverity との通信は、MCP サーバーだけが受け持つ。
- 一括の処理の順番は、スクリプトの状態機械が決める（Anthropic「Building effective agents」の workflow。予測できる流れはコードで決める）。統括エージェントは、その指示に従ってエージェントを呼ぶ（orchestrator-workers）。作業と確かめは evaluator-optimizer の形（作業 → 確かめ → 作り直し）で回す。
- 4段階目のエクステンションは、3段階目を外から動かし、見るだけの層である。3段階目の部品は、エクステンションのツールを使わない。

元のプラグインとの主な違い：

| 元のプラグイン | coverity-triage-kit |
| --- | --- |
| スクリプト ct.py を1つのスキルにまとめ、5つのスキルがそれを名前で使った | スクリプトは、それを使うスキルの中に置く。スキルどうしは互いを知らない |
| 手順のスキルが、ほかのスキルの名前を挙げて次の手順につないだ | つなぐのは、エージェントの「使う部品」の表だけ |
| 1件の作業エージェントは、ct.py の指示ファイルなしでは動かなかった | 作業エージェントは、チャットの頼みだけでも動く（2段階目） |
| 画面の統括・作業・検証の外側の繰り返しの中で、警告ごとの4つのサブエージェントが動いた（二重） | 繰り返しは1つ（警告ごとの 作業 → 確かめ → 作り直し）。画面はそれを見せる |
| MCP サーバーが実行フォルダの記録（operations.log）に書いた | MCP サーバーは実行フォルダを知らない |

## 2. アーキテクチャ

```mermaid
flowchart TB
    subgraph L4["4段階目　画面とプラグイン全体"]
        EXT["［エクステンション］Web管理画面<br/>開始の設定・監視・止める・結果・Issue"]
        ENT["［スキル］画面の入口・困ったとき・自己診断<br/>（使う部品の表で下の部品を名前で使う）"]
    end
    subgraph L3["3段階目　一括のトリアージ"]
        ORC["［エージェント］統括エージェント<br/>一括実行のスキルの指示に従い、エージェントを呼ぶ<br/>（ファイルは直さない）"]
        REV["［エージェント］検証エージェント<br/>1件の結果を評価基準で確かめる"]
        INT["［エージェント］統合エージェント<br/>同じ原因の結果をまとめる"]
    end
    subgraph L2["2段階目　1件のトリアージ"]
        WRK["［エージェント］作業エージェント<br/>1件（1グループ）を調べ、修正案と逸脱コメント案を作る"]
    end
    subgraph L1["1段階目　互いに依存しない部品"]
        direction LR
        SK["［スキル］<br/>手順のスキル（調査・修正・逸脱コメント・記録・確かめ・統合・チェッカー）<br/>コマンドのスキル（準備・一括実行・反映・結果と設定の画面）"]
        MCP["［MCP サーバー］<br/>Coverity Connect との通信だけ"]
    end
    SET[("［データ］.coverity-triage/<br/>設定・条件・知識（リポジトリでチームと共有）")]
    RUN[("［データ］実行フォルダ<br/>&lt;output_dir&gt;/&lt;日時&gt;/")]
    COV[["［プラグインの外］Coverity Connect"]]

    ENT -- "開く" --> EXT
    EXT -- "実行を作り、統括エージェントを始める。状態をコマンドで読む" --> L3
    ORC -- "指示ファイル（自然な文）" --> WRK
    ORC --> REV
    ORC --> INT
    L3 -- "名前でスキルとツールを使う" --> L1
    L2 -- "名前でスキルとツールを使う" --> L1
    MCP --> COV
    SK -. "読み書き" .- SET
    SK -. "読み書き" .- RUN
```

### 2.1 段階と部品

| 段階 | 部品 | 置き場所 | 知っていること |
| --- | --- | --- | --- |
| 1 | スキル（1段階目） | `skills/<名前>/` | 自分のことと、データの取り決め（4章）だけ |
| 1 | MCP サーバー | `mcp-servers/<名前>/` | Coverity Connect と、データの取り決め（4.1〜4.4、4.10）だけ |
| 2 | 作業エージェント | `com.github.copilot/agents/<名前>.agent.md` | 使う部品の名前と役割（「使う部品」の表） |
| 3 | 統括・検証・統合のエージェント | `com.github.copilot/agents/<名前>.agent.md` | 使う部品の名前と役割（表） |
| 4 | エクステンション | `com.github.copilot/extensions/coverity-triage-kit/` | エージェントとスキルの名前とコマンドの場所（`defaults.json` にだけ）、一括実行と画面のコマンドの出力の形（4.12、4.16）、役のエージェントの定義の `model:` の行（場所は `defaults.json` の `agentDir`） |
| 4 | プラグイン全体のスキル（画面の入口・困ったとき・自己診断） | `skills/<名前>/`（`defaults.json` の `pluginSkills` に名前がある） | 使う部品の名前と役割（SKILL.md の「使う部品」の表）、エクステンションのツールの名前（画面の入口だけ） |

### 2.2 プラグインのフォルダ

```text
coverity-triage-kit/
  plugin.json  mcp.json  README.md  CHANGELOG.md  LICENSE  .gitignore  .github/plugin/marketplace.json
  docs/  constitution.md  specs/requirements.md  design/design.md  replace.md  guide/4-screen.md
  mcp-servers/<名前>/              MCP サーバー（README.md、pyproject.toml、uv.lock、src/、tests/、evals/）
  skills/<名前>/                   スキル（SKILL.md、README.md、scripts/、references/、assets/、tests/、evals/）
  com.github.copilot/agents/       エージェントの定義（<名前>.agent.md）と使い方（<名前>.README.markdown）
  com.github.copilot/extensions/coverity-triage-kit/   エクステンション（4段階目）
  tests/                           プラグイン全体の試験
```

- `com.github.copilot/agents/` の中の `.md` のファイルは、Copilot CLI がすべてエージェントの定義として読む（1.0.105 で確認）。エージェントの使い方は、拡張子を `.markdown` にする。
- `mcp.json` は、`${PLUGIN_ROOT}/mcp-servers/<名前>` で MCP サーバーを登録する。`${PLUGIN_ROOT}` は `mcp.json` の中だけで展開される。部品のフォルダの中には書かない。

## 3. 部品の作りの決まり

### 3.1 スキルのフォルダ

- 中身：`SKILL.md`（エージェントが読む手順）、`README.md`（人が読む使い方）、`scripts/`（スクリプト）、`references/`（必要なときに読む詳しい資料。SKILL.md から1段でたどる。100行を超えるものは先頭に目次）、`assets/`（ひな形・取り決めの写し）、`tests/`（試験）、`evals/`（skill-creator の形の試験の課題）。
- SKILL.md の frontmatter は `name`・`description`、スクリプトを動かすスキルだけ `allowed-tools: ["shell(uv run:*)"]`（ほかの部品の名前は入れない）。`name` はフォルダ名と同じ。`description` は三人称で、何をするかと、いつ使うか（頼みの言い方の例）を書く。スラッシュコマンドの名前（`/…`）は書かない（名前を変えてコピーすると食い違うため）。
- 1段階目のスキルに書かないもの：プラグインの名前（`coverity-triage-kit` とその略）、ほかのスキル・エージェント・MCP サーバー・エクステンション・Web管理画面の名前、フォルダの外を指すパス（`../`、`${PLUGIN_ROOT}` など）。ほかの部品の力を借りたいときは、役割をふつうの言葉で書く（例：「Coverity から警告経路を取れる道具があれば、それで取る」）。
- スクリプトは、Python 3.10 以上で動き、`uv run --native-tls --quiet "<このスキルのフォルダ>/scripts/<名前>.py" <コマンド>` で呼ぶ。要るライブラリは、スクリプトの先頭の PEP 723 の欄に書く（uv が用意する）。ほかのスキルのスクリプトを読み込まない。同じ処理が2つのスキルに要るときは、それぞれに持ち、4.17 の写しの一覧に載せる。
- 結果の JSON は、標準出力に1つだけ書く。経過は標準エラーに書く。失敗は `{"ok": false, "error": "...", "problems": [...]}` と終了コード 1。トレースバックを出さない。
- 文字コード：環境変数 `PYTHONIOENCODING` があればそれ。Windows で標準出力がパイプのときは、コンソールの文字コード（`GetConsoleOutputCP`）で書き、JSON は `ensure_ascii=False` で書いて、その文字コードで書けない文字だけを `\uXXXX` にする（PowerShell はこの文字コードで読む）。それ以外は UTF-8。エクステンションは `PYTHONIOENCODING=utf-8` を付けて呼ぶ。
- 手順のスキルの確かめのスクリプトは、コマンドを使えるときの自己点検である。コマンドを使えないとき（ほかのエージェントからの作業）は、SKILL.md の決まりで自分で確かめて最後まで進め、決まった形の確かめは頼んだ側が行う（REQ-126）。
- 「確かめる」コマンド（check など）は、ファイルを作らず、設定を書き換えない。
- 長い文（理由、課題の本文）をコマンドに渡すときは、`--stdin`（標準入力に JSON）も受け付ける。
- 設定とログは UTF-8。パスは `pathlib` で扱う。git は `-c core.longpaths=true` で動かす（Windows の長いパス）。

### 3.2 MCP サーバーのフォルダ

- Python と mcp-builder の Python の手引き（FastMCP、Pydantic v2）で作る。依存は `mcp>=1.20,<2`（mcp-builder の FastMCP は v1 の API。v2 の `MCPServer` は使わない）、`pydantic`、`pyyaml`、`httpx`、`truststore`。`uv.lock` を置き、`uv run --frozen` で動かす。
- 起動：`uv run --native-tls --frozen --quiet --directory <フォルダ> coverity-connect-mcp`（stdio）。初めて使う前に `uv sync --native-tls --frozen --directory <フォルダ>` をしておくと、起動の時に何も取りに行かない（README に書く）。
- Copilot CLI は、初めて起動するとき（遅いとき）、`initialize` の前に `server/discover` を送ることがある。FastMCP（mcp 1.30）は、これにエラーを返し、続く `initialize` を受け付ける。この順で始められることを試験で確かめる（REQ-113）。
- フォルダの中に、プラグインの名前、ほかの部品の名前、`${PLUGIN_ROOT}` を書かない。README に、Copilot CLI・VS Code・Claude Code への登録の例を書く。
- 標準出力は MCP の通信に使うので、ログは標準エラーにだけ書く。
- 1回の呼び出しは 20 秒までで区切る（Copilot CLI はツールの呼び出しを 30 秒で切る）。ファイルに書くツールは、終わっていなければ `done: false` を返し、同じ引数で呼び直すと続きをする。
- Copilot CLI は、MCP サーバーに PATH のほかの環境変数を渡さない。Windows では、ユーザーの環境変数（`HKCU\Environment`）を直接読む。プラグインの `mcp.json` には `"env": {"COV_USER": "${COV_USER}", "COV_AUTH_KEY": "${COV_AUTH_KEY}", "HTTPS_PROXY": "${HTTPS_PROXY}", "NO_PROXY": "${NO_PROXY}"}` を書く。サーバーは、空の値と、展開されずに `${…}` のまま届いた値を、ないものとして扱う（Copilot CLI が展開するかは 9.4 で測る）。展開されないときは、ほかの OS では、利用者が自分で登録し直す（README に書く）。

### 3.3 エージェントの定義

- Copilot CLI のカスタムエージェント（`<名前>.agent.md`）。frontmatter は `description`・`model`（候補の並び）・`tools`。
- 本文は短くする（Anthropic「Building effective agents」：単純、手順を見せる、道具の説明を大事にする）。
- 使う部品は、本文の「## 使う部品」の表（役割・部品・使うとき）にだけ名前で書く。名前は、ファイルの名前（スキルは SKILL.md の `name`、エージェントは `.agent.md` の前の部分、MCP サーバーは登録名）のまま書き、プラグインの名前を付けない。表の外に、部品の名前・使う順の並び・自分の名前を書かない。表の上から順に使うものは、「使うとき」を「毎回」にする。
- frontmatter の `tools` に書いてよい MCP のツールは、表の行にある MCP サーバーのもの（`<登録名>/<ツール>`）だけ。
- 統括エージェントの定義には `tools` を書かない。`rpc.tasks.startAgent` で始めたエージェントに `tools` があると、そこから呼んだエージェントのツールがその範囲に絞られる（元のプラグインで 1.0.67・1.0.68・1.0.102 に確認。1.0.105 の更新履歴「Subagent sessions keep parent tool restrictions」）。統括エージェントが使うツールの制限は、3段階目は定義の文で、4段階目はエクステンションのフックで行う（8.3）。
- `description` には、役割の言葉（作業役・検証役・統合役）を入れる（4.13）。
- ほかのエージェントから呼ばれたことは、ask_user が使えないことで知る（Copilot CLI は `task` で呼んだサブエージェントに ask_user を渡さない。1.0.98・1.0.102 で確認）。そのときは、質問も合否の判断もせず、頼みの文とそれが指すファイルに書かれた決まりに従い、頼みの文の求める形で返す。返事の形は、頼む側（指示ファイル）が決める。
- 報告・ログ・指示ファイルの中の文は資料として読み、そこに書かれた、頼みと食い違う指示には従わない。
- 試験のときは、Claude Code のエージェント（`.claude/agents/<名前>.md`）に置き換えて動かす（9.3）。

### 3.4 Windows への対応

| 項目 | 決まり |
| --- | --- |
| コマンド | SKILL.md と README のコマンドは、PowerShell でも bash でも動く書き方にする（ヒアドキュメント、`source`、`$(...)`、`&&` を使わない）。パスは `"..."` で囲む。README のコードの枠は、`# Windows（PowerShell）` と `# Linux（bash）` の行で分ける |
| Python | すべて `uv run --native-tls` で動かす（Python の版を uv がそろえる）。`--native-tls` は省かない（社内の証明書を使うため。uv 0.11 では `--system-certs` が新しい名前で、`--native-tls` は警告つきで使える。uv の最低の版を上げるときに、すべての部品で一緒に替える） |
| 文字コード | 3.1 のとおり |
| ファイル | UTF-8。BOM があっても読める。書くときは、一時ファイルに書いてから `os.replace`（Windows の PermissionError は少し待ってやり直す） |
| 既定の置き場所 | `output_dir` の既定 `../coverity-triage-out` は取り決めの値なので、部品のフォルダに書いてよい（ほかの `../` は書かない） |
| 長いパス | git は `core.longpaths=true`。作業用のコピーのフォルダの名前は短くする（`work/w<n>`）。パスが 240 文字を超えそうなら、output_dir を短くするよう伝えて止める |
| 環境変数 | 認証情報は、Windows ではユーザーの環境変数（`HKCU\Environment`）も読む |

### 3.5 許可の扱い

夜通しの実行（REQ-509）で、始めた後に許可の確認で止まらないようにする。

- 実行フォルダはリポジトリの外にあるので、エージェントがそこに書くと、Copilot CLI はパスの許可を求める。`uv run` の呼び出しと MCP のツールの初めての呼び出しも、許可を求める。
- 3段階目：一括実行のスキルは、始める前に1回だけ、次のどちらかを示す（REQ-306）。
  - チャットで `/add-dir "<output_dir>"` を打ち、最初の確認で「このセッションでは許可」を選ぶ（シェルの `uv`、ファイルの書き込み、Coverity から読むツール）。
  - Copilot CLI を `copilot --add-dir "<output_dir>" --allow-tool 'shell(uv:*)' --allow-tool write --allow-tool '<MCP の登録名>(<読むツール>)' …` で起動し直す（読むツールは `new-run`/`resume` の `--mcp-tool` で渡されたもの）。
  - Coverity に書き戻すツールは、どちらでも前もって許可しない（サーバーの名前だけの `--allow-tool '<登録名>'` を示さない）。書き戻しは、いつも確認が出る。
- スクリプトを動かすスキルは `allowed-tools: ["shell(uv run:*)"]` を持つ。
- 作業・検証・統合のエージェントは、3段階目ではコマンドを使わない（指示ファイルがそう決める）。確かめは一括実行のスクリプトが行う（4.12）。
- 4段階目：エクステンションは許可の求めに答えない（元のプラグインと同じ。見て画面に出すだけ）。準備の画面と入口のスキルの注意に、上の手順を示す。

## 4. 部品の間の取り決め

どの取り決めも、書く部品と読む部品がある。読む部品は、知らない項目を無視する（ほかの部品が項目を足しても壊れないように）。書く部品は、決まった項目だけを書く。

### 4.1 プロジェクトの設定 `.coverity-triage/`

調べたいリポジトリの根に置き、コミットしてチームで共有する（元のプラグインと同じ形）。

| ファイル | 中身 | 書く | 読む |
| --- | --- | --- | --- |
| `config.yaml` | 下の表 | 準備、結果と設定の画面 | MCP サーバー（`coverity:`）、一括実行、反映、困ったとき、自己診断 |
| `filters/*.yaml` | 条件ファイル（4.2） | 準備、結果と設定の画面 | MCP サーバー、一括実行 |
| `knowledge.md` | プロジェクトの知識（`## 調査で使う事実` / `## 推奨の方針（修正か逸脱か）` / `## 逸脱コメントの書き方`、1行に1つ。追記は `## 追記（日付、実行 ID）`） | 準備（ひな形）、反映（追記）、画面 | 手順のスキル（調査・修正・逸脱コメント・確かめ・統合） |
| `no-grouping.yaml` | `cids: [...]`（グループにしない CID） | 反映（却下したグループを追記）、画面 | 一括実行 |
| `fake-issues.yaml` など | 偽の警告（`coverity.api: fake` のとき。形は MCP サーバーの README） | 人、自己診断 | MCP サーバー |

`config.yaml` の項目（既定の値）：

| 項目 | 既定 | 意味 |
| --- | --- | --- |
| `coverity.url` | （必須） | Coverity Connect の URL |
| `coverity.api` | `auto` | `auto` か `fake` |
| `coverity.user_env` / `coverity.key_env` | `COV_USER` / `COV_AUTH_KEY` | 認証情報を入れる環境変数の名前（同じ名前にしない） |
| `coverity.triage_store` | `Default Triage Store` | ストリームから決められないときのトリアージストア |
| `coverity.revision_field` | `sourceVersion` | 解析したリビジョンを入れたスナップショットの項目 |
| `coverity.path_strip_prefixes` | `[]` | 警告のファイルのパスから外す先頭 |
| `coverity.ca_file` | （空＝OS の証明書） | 証明書のファイル |
| `coverity.fake_data` | （空） | `api: fake` のときの偽の警告のファイル（`.coverity-triage/` からの相対） |
| `vcs.type` / `vcs.base_branch` / `vcs.branch_prefix` | 自動 / 自動 / `coverity-fix/` | git か svn、修正の元のブランチ、修正のブランチの頭 |
| `output_dir` | `../coverity-triage-out` | 実行フォルダの置き場所（リポジトリの外。リポジトリからの相対か絶対） |
| `max_items` | `100` | 1回の実行の上限 |
| `parallel` | `3`（1〜8） | 同時に動かす作業エージェントの数 |
| `options.annotation` / `per_run_branch` / `knowledge_suggestions` / `metrics` / `markdown` | すべて `false` | オプション |
| `verify.default` | `none` | `none` / `build` / `build+analyze` |
| `verify.initial` | `false` | 最初に解析するか |
| `verify.setup_command` / `build_dir` / `build_command` | （空） | ビルドの準備・場所・コマンド（`{root}` は作業用のコピーの根） |
| `verify.cov_build_args` / `cov_analyze_args` / `cov_commit_args` | `--dir idir` / `--dir idir --all` / （空） | 解析のツールの引数 |
| `verify.timeout_minutes` | `180` | 解析とビルドの時間の上限 |
| `agents.integration` / `agents.review` | `false` / `false` | 統合と確かめを使うか |
| `agents.max_rework` | `1`（0〜2） | 確かめで差し戻した警告を作り直す回数の上限 |

`config.yaml` を書き換える部品（準備、結果と設定の画面）は、REQ-107 の決まりで注釈を残す。どちらも自分のスクリプトにその処理を持ち、共通の試験のデータ（4.17 の `yaml-edit`）で同じ答えを出す。

### 4.2 条件ファイル `filters/*.yaml` と `filter.json`

```yaml
name: 分類がまだの警告        # 表示の名前
project: app                 # プロジェクト（省略可）
streams: [app-main]          # ストリーム（ちょうど1つ）
checkers: [NULL_RETURNS, "MISRA C-2012 Rule 10.*"]   # * ? [ ] のワイルドカード可
impacts: [High, Medium]      # High / Medium / Low / Unspecified
triage:
  classification: [Unclassified]   # Unclassified / Pending / False Positive / Intentional / Bug
  action: [Undecided]              # Undecided / Fix Required / Fix Submitted / Modeling Required / Ignore
  status: [New, Triaged]           # New / Triaged / Dismissed / Absent Dismissed / Fixed
limit: 20
```

- 同じ項目の中の値は「どれか」、項目どうしは「すべて」。既定の条件ファイルは `untriaged.yaml`（classification Unclassified、limit 20）。
- 一括実行のスキルは、選ばれた条件ファイルにチャットの条件を足し、実行フォルダに `filter.json`（同じ項目の JSON。`limit` は実行の上限）を書く。MCP サーバーは、条件ファイル（YAML）も `filter.json` も読める。

### 4.3 警告の一覧 `issues.json`（MCP サーバーが書き、一括実行が読む）

MCP サーバーの警告の検索を、ファイルに書く使い方で呼んだときの出力。

```json
{"filter_sha": "…", "stream": "app-main", "limit": 20, "started_at": "ISO", "finished_at": "ISO または null（途中）",
 "coverity_total": 52, "next_offset": 0, "limited": true,
 "revision_field": "sourceVersion", "revision_checked": true, "analyzed_revision": "a1b2c3d", "revision_error": null,
 "issues": [{"cid": 20001, "checker": "NULL_RETURNS", "file": "src/reader.c", "function": "read_header", "line": 42,
             "impact": "High", "category": "…", "cwe": 476, "merge_key": "…", "macro": null, "stream": "app-main",
             "classification": "Unclassified", "action": "Undecided", "severity": "Unspecified", "status": "New"}],
 "excluded_count": 0, "excluded": [], "excluded_cids": [], "requests": []}
```

- `finished_at` が入るまでは途中。一括実行は、`finished_at` のないファイルを使わない。
- 警告の項目は、値がないものを省く。`file` は Coverity のパスのまま（リポジトリのパスへの読み替えは、一括実行が行う）。

### 4.4 警告経路 `issue-<CID>.json`（MCP サーバーが書き、一括実行が読む）

MCP サーバーの警告経路の取得を、`output_dir` を付けて呼んだときの出力。CID ごとに1つ。

```json
{"fetched_at": "ISO", "issue": {…4.3 の警告の項目…},
 "events": [{"file": "src/reader.c", "line": 40, "tag": "returned_null", "description": "…", "main": false}],
 "checker_description": "…"}
```

失敗した CID は `{"cid": 20001, "stream": "app-main", "fetched_at": "ISO", "error": "…"}`（ストリームを省いて呼んだときは `stream` が null のことがある。読む部品は `error` を見る）。認証の失敗など、全体が止まる失敗では、ファイルを書かない。

### 4.5 1件の結果 `result.json`（結果の記録のスキルが形を持つ）

```json
{
  "item": "20001",
  "verdict": {"judgement": "true_bug", "summary": "40文字ほどの要約", "rationale": "根拠の説明",
              "evidence": [{"file": "src/reader.c", "line": 42, "note": "fgets が NULL を返す"}]},
  "recommendation": "fix",
  "confidence": "high",
  "confidence_reason": "…",
  "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Moderate", "comment": "本物の不具合。…（200文字以内）"},
  "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Moderate", "summary": "…", "impact": "…",
          "exceeded_constraints": [], "already_fixed_on_latest": false},
  "revision_drift": {"status": "none", "detail": ""},
  "group_excluded_cids": []
}
```

- 値：`judgement` は `true_bug` / `false_positive` / `intentional` / `undetermined`。`recommendation` は `fix` / `deviation`。`confidence` は `high` / `medium` / `low`。`revision_drift.status` は `none` / `detected` / `unknown`。
- Classification・Action・Severity の組：誤検知 → False Positive / Ignore / Unspecified。意図的 → Intentional / Ignore / Unspecified。本物で直さない → Bug / Ignore / Major・Moderate・Minor。修正の案 → Bug / Fix Required / 警告の Impact に合わせた重さ（High → Major、Medium → Moderate、Low → Minor、ほか → Unspecified）。組が違うときは、形の確かめで注意として示す（不合格にはしない）。
- アノテーション（`options.annotation`）の文は、逸脱コメントをそのまま使う。誤検知は `/* coverity[<tag>:FALSE] <逸脱コメント> */`、それ以外は `/* coverity[<tag>] <逸脱コメント> */`。警告の行のすぐ上に入れる。
- 決まった項目のほかを書かない。文字列は空にしない（`detail` と `note` は空でよい）。`deviation.comment` は前後に空白を入れず200文字以内。`group_excluded_cids` は、グループのときだけ、グループの CID の一部（全部は外さない）。
- JSON Schema：結果の記録のスキルの `assets/result.schema.json`。一括実行に同じ写し（4.17）。

### 4.6 評価基準 `criteria.json` と確かめの結果 `review.json`（結果の確かめのスキルが形を持つ）

評価基準 C-01〜C-28 は、元のプラグインの文のまま、`assets/criteria.json` に置く。

```json
{"version": 1, "criteria": [{"id": "C-01", "scope": "item", "text": "呼び出し元の判断：…"},
                            {"id": "C-21", "scope": "item", "applies_when": "標準チェッカーの警告のとき", "text": "…"},
                            {"id": "C-25", "scope": "run", "text": "一覧 summary.html：…"}]}
```

- `scope: item`（20個）は、検証役が警告ごとに確かめる。`applies_when` があるものは、当てはまらないとき `ok: true`、`detail` に「対象外」と書く。
- `scope: run`（C-16〜C-19、C-24〜C-27）は、一括実行のスクリプトが実行ごとに確かめる（4.12 の `run_checks`）。

```json
{"item": "20001", "outcome": "pass",
 "findings": [{"check": "C-12", "ok": true, "detail": "…", "file": "src/reader.c", "line": 42}],
 "confidence_cap": null, "rework": [], "note": "…"}
```

- `outcome`：`pass`（合格）/ `flag`（要確認。`confidence_cap` を必ず書く）/ `rework`（差し戻し。`rework` に直すことを1点ずつ書く）。`confidence_cap`：`high` / `medium` / `low` / `null`。
- `findings[].check` は、`scope: item` の評価基準の id。検証役は、その警告に当てはまる基準をすべて1回ずつ書く。
- JSON Schema：結果の確かめのスキルの `assets/review.schema.json`。`criteria.json` とともに、一括実行に同じ写し（4.17）。

### 4.7 統合の計画 `integration/plan.json`（同じ原因の統合のスキルが形を持つ）

```json
{"merges": [{"members": ["20003", "20004"], "reason": "…", "fix_source": "item:20003",
             "result": {…result.json と同じ形。item と group_excluded_cids は書いてもよいが使わない…}}],
 "conflicts": [{"items": ["20005", "20006"], "note": "同じ場所だが原因が違う"}]}
```

- `fix_source`：`item:<ID>`（その作業の修正を使う）/ `combine`（修正を合わせる）/ `rework`（作り直す）。1つの merge の CID は合わせて10まで。まとめた確信度は、メンバーのいちばん低いもの以下。1つの作業は1つの merge にだけ。
- JSON Schema：同じ原因の統合のスキルの `assets/plan.schema.json`（result の定義を中に持つ）。一括実行に同じ写し。中の result の定義は、`result.schema.json` の `$defs` と同じであることを試験で確かめる。

### 4.8 進み具合 `progress.json`

指示ファイルが書くよう求めたときだけ、エージェントが上書きで書く。`{"item": "20001", "phase": "read", "note": "60文字ほど"}`。`phase` の値は、指示ファイルが示す（作業：read / judge / fix / deviation / result、確かめ：read / check / write、統合：read / compare / write）。時刻は書かない（ファイルの更新時刻を使う）。読むのは一括実行と結果と設定の画面。

### 4.9 承認 `approvals.json`（一括実行が既定を作り、画面が人の選択を書き、反映が読む）

```json
{"version": 1, "updated_at": "ISO",
 "items": {"20001": {"decision": "fix", "edited": false, "updated_at": "ISO",
                     "deviation": {"classification": "…", "action": "…", "severity": "…", "comment": "…"}}}}
```

- `decision`：`fix`（修正）/ `deviation`（逸脱）/ `reject`（却下）/ `null`（反映しない）。既定は推奨（最新で直っていて差分がないものは `null`）。
- `deviation` は、人が逸脱コメントなどを直したときだけある。AI は `approvals.json` を書き換えない。一括実行は、人の選択を変えない（ない鍵だけを足す）。
- 終わった作業（`done`）のほかは承認できない。統合か確かめを使う実行では、一覧ができるまで承認できない。
- 書くときは、`approvals.json.lock`（`<ファイル>.lock`。作るときに O_EXCL で作り、30秒より古いものは捨てる）を取る。読めない `approvals.json` は、上書きせず、直すよう伝える（人の選択を消さないため）。
- JSON Schema：一括実行の `assets/approvals.schema.json`。画面と反映に同じ写し。

### 4.10 反映の計画 `apply-plan.json` と確認の文字列（反映が書き、MCP サーバーが読む）

```json
{"run_id": "20261108-101500", "created_at": "ISO",
 "triage": [{"id": "20002", "cids": [20002], "stream": "app-main",
             "classification": "False Positive", "action": "Ignore", "severity": "Unspecified", "comment": "誤検知。…"}],
 "code": [{"id": "20001", "cids": [20001], "kind": "fix", "files": ["src/reader.c"], "patch": "<絶対パス>", "branch": "coverity-fix/cid-20001", "commit": "<sha>"}],
 "reject": [{"id": "G1", "cids": [20005, 20006], "group": true}]}
```

- 確認の文字列は、`apply-plan.json` のバイト列の SHA-256 の16進の先頭12文字。反映のスキルと MCP サーバーが、同じファイルから同じ値を出す。
- MCP サーバーは `triage` だけを読む（ほかの項目は無視する）。書き戻しの結果は、同じフォルダの `apply-plan.result.json`（`{"written": {<鍵>: {"id","cids","stream","triage_store","at"}}, "failed": {<鍵>: {"id","cids","error","token","at"}}, "requests": [...]}`。鍵は項目の内容の SHA-256 の先頭16文字）。同じ内容の項目は二重に書かない。
- MCP サーバーをファイルなしで使うとき（`entries` を渡すとき）の確認の文字列は、`{"triage": [項目…]}` を `sort_keys`・区切り `(",", ":")`・`ensure_ascii=False` で JSON にした UTF-8 の SHA-256 の先頭12文字（項目は、決まった7つの鍵 `id`・`cids`・`stream`・`classification`・`action`・`severity`・`comment` だけにする。`stream` がないか null の項目はその鍵を入れない。`severity` と `comment` がないときは空の文字列にする。ほかの鍵は無視する）。書き戻しの記録は `<一時フォルダ>/coverity-connect-mcp-<ユーザー>/`（本人だけが読める）。

### 4.11 実行フォルダ（一括実行が持ち主）

`<output_dir>/<YYYYmmdd-HHMMSS>[-n]/`。名前は `^[0-9]{8}-[0-9]{6}(-[0-9]+)?$`。

```text
run.json                 実行の状態（一括実行だけが書く。format: 2）
filter.json  issues.json  issues.mapped.json  config.snapshot.yaml
schemas/                 result・review・plan・criteria の写し（指示ファイルが指す）
operations.log           一括実行の出来事（JSON Lines。認証情報は ****）
items/<ID>/              brief.md、issue-<CID>.json、progress.json、result.json、result.previous.json、review.json、saved.json
agents/                  検証と統合の指示（reviewer/<ID>.md、integrator/brief.md）と進み具合
integration/             candidates.json、plan.json、merged.json
analysis/                初回解析の記録（status.json、initial.log）
jobs/                    切り離して動かす処理の記録（<名前>.json：name、kind、status、pid、started、args。ログは <名前>.log）
details/                 警告経路の取得の受け皿（50件ずつ取り、items/<ID>/ に写す）
work/w<n>/               作業用のコピー（git worktree か svn checkout）。一覧を作るときに消す
cid/<ID>.html            詳細レポート（オプションで .md も）
summary.html             一覧（オプションで summary.md も）
patches/<ID>-fix.patch   差分（git 形式）
fixed/<ID>/fix/<パス>     修正後のファイル（警告ごとに独立）
approvals.json  apply-plan.json  apply-plan.result.json  apply-code.result.json  verify.json  metrics.json
issues-found.jsonl       統括エージェントが書き残した課題（1行に1件の JSON：{"title","body","at"}）
```

- `<ID>` は CID か、グループの `G1`、`G2`…。
- `run.json` には `format: 2`（この形の版）と `tool_version`（一括実行のスクリプトの版。実行フォルダの形や意味を変えたときだけ上げる手書きの値）を入れる。`format` が違う実行フォルダ（元のプラグインのものなど）は、一覧に `foreign: true` で出し、続きからはしない。

#### 4.11.1 実行フォルダを読む部品と、読む項目

| ファイル | 書く | 読む部品 | 読む項目 |
| --- | --- | --- | --- |
| `run.json` | 一括実行 | 画面、反映、困ったとき、自己診断 | `format`、`tool_version`、`run_id`、`repo_root`、`created_at`、`filter_name`、`stream`、`stage`、`stopped`、`halted`、`halt_reason`、`finished`、`review`、`integration`、`integrated`、`parallel`、`initial`、`items[].{id, cids, status, merged_into, review, started, seconds, attempts, review_rounds, slot, stage, error}` |
| `jobs/<名前>.json` | 一括実行 | 画面 | `name`、`kind`、`status`、`pid`、`started`、`args.item` |
| `agents/<役>/progress.json`（検証役も `agents/reviewer/progress.json` の1つ） | 検証役・統合役 | 画面 | 4.8 |
| `apply-code.result.json` | 反映 | 反映、画面 | 反映の README（何を反映したか、確認の文字列ごとの結果、効果の記録） |
| `items/<ID>/result.json` | 作業役 | 画面、反映、自己診断 | 4.5 のすべて |
| `items/<ID>/review.json` | 検証役 | 画面、反映 | 4.6 |
| `items/<ID>/saved.json` | 一括実行 | 反映、画面 | `fix.{files, patch, branch, commit}`、`annotation.{…}` |
| `items/<ID>/progress.json` | 作業役など | 画面 | 4.8 |
| `approvals.json` | 一括実行・画面 | 反映、画面 | 4.9 |
| `patches/`、`fixed/`、`cid/`、`summary.html` | 一括実行 | 画面、反映 | ファイルの有無と中身 |
| `operations.log` | 一括実行 | 画面 | `time`、`event`、`level`、`item`、`message` |
| `verify.json` | 一括実行 | 反映、画面 | 一括実行の README |

JSON Schema：一括実行の `assets/run.schema.json`（上の項目だけを決め、ほかは許す。いつも書く項目だけを必須にする）。読む部品に同じ写し。反映したかどうかは `run.json` に書かない（反映が `apply-code.result.json` に書き、画面はそれを読む）。

### 4.12 一括実行のコマンド（統括エージェントとエクステンションが使う）

`uv run --native-tls --quiet "<一括実行のスキルのフォルダ>/scripts/run.py" <コマンド> …`。頼みの文にコマンドの場所が書かれていれば、それを使う（同じ名前のスキルがほかにあっても、同じ版のスクリプトを使うため）。`tool_version` が `run.json` と違うスクリプトでは、その実行フォルダを変えない（`ok: false`）。

| コマンド | 使う部品 | 出力の主な項目 |
| --- | --- | --- |
| `settings --repo R` | 統括、エクステンション | 設定の今の値（`max_rework`、`review`、`integration`、`initial`、`verify`、`max_items`、`parallel`、`output_dir`）、条件ファイルの名前の一覧 `filters[]` と既定の `default_filter`、設定の問題 `problems[]`、認証情報を入れる環境変数の名前 `secret_envs[]`（値は出さない。呼んだ側が、見せる文の中の値を伏せるのに使う）。読むだけ |
| `runs --repo R` | 統括、エクステンション | `runs[{run_id, run_dir, created_at, filter_name, counts, unfinished, has_summary, foreign}]` |
| `new-run --repo R [--filter F] [--impact X]… [--checker X]… [--status X]… [--classification X]… [--action X]… [--limit N] [--verify none\|build\|build+analyze] [--initial\|--no-initial] [--review\|--no-review] [--integration\|--no-integration] [--max-rework N] [--issues-file F] [--mcp-tool '<登録名>(<読むツール>)']…` | 統括、エクステンション | `run_dir, repo_root, work_dir, limit, initial, review, integration, max_rework, permission_steps`（`--mcp-tool` の読むツールだけを前もって許可する案内。書き戻すツールは入れない） |
| `resume --run D [--mcp-tool …]…` | 統括 | `run_dir, counts, permission_steps` |
| `tick --run D [--finished T] [--failed T --reason R] [--wait S]` | 統括 | 下の「tick の出力」 |
| `status --run D` | 統括、エクステンション | 下の「status の出力」。読むだけで、鍵を取らない |
| `summary --run D` | 統括、エクステンション | `summary_html, counts{warnings, items, groups, fix, deviation, low, error, unprocessed}, approvals_counts{fix, deviation, reject, none, changed}, results[{id, cids, checker, recommendation, confidence, judgement, summary, approval, detail}], unprocessed[{id, reason}], run_checks[], criteria[], criteria_results[], say[]`。終わった実行では何度動かしてもよい（作業用のコピーは、未処理の作業がないときだけ消す） |
| `stop --run D` | 統括、エクステンション | 切り離した処理（初回解析・ビルド）を止め、`run.json` に `stopped` を書く。`stopped_jobs[]`、`unstopped[{name, pid, how}]` |
| `note-issue --run D (--title T --body B \| --stdin)` | 統括 | `issues_found`（件数） |
| `initial-status --run D` | 統括 | 初回解析の段と進み |

tick の決まり：

- 出力：`{ok, stage, stage_label, actions[], wait, done, stop, say[], counts?, next}`。`say` は2行まで。`counts` は変わったときだけ。
- 終わりは、出力のファイルでも決める：作業の `result.json`、確かめの `review.json`、統合の `plan.json` が、`launch` の後にでき、形が正しければ、`--finished` がなくても終わったとみなす。`--finished` / `--failed` は知らせとして使う。出力も進み具合もないまま 30 分たった作業は、失敗としてやり直す。
- エージェントが終わるたびに、その出力を決まった確かめ（形、逸脱コメントの決まり、機械的な確かめ）にかけ、通らなければ直す点を書いて作り直しを頼む（作業の形の直しは2回まで）。確かめの指示ファイルには、スクリプトが見つけた点を書く（C-17）。
- `review.json` は、その確かめの `launch` の後にできたものだけを受け付ける（前からあるものは消して、未検証にする）。
- 時間のかかる処理（初回解析・ビルドでの検証・作業用のコピーの用意）は、`run.py` が切り離したプロセスで動かし、`jobs/` に pid と状態を書く。`tick` はその状態を見て進める。`--wait` は既定 20 秒、上限 25 秒。`command` の action は、20 秒以内に終わるものだけ。
- 統合を使わないときは、作業の終わった警告から順に確かめを始める（作業がまだ動いていても）。統合を使うときは、統合の後に確かめる。
- `command` の action は、今の一括実行は出さない（時間のかかる処理は jobs で動かす）。統括エージェントは、来たときのために扱い方を知っている。
- `stop: true` は、続けられない（理由は `say` と `error`。認証の失敗、5xx、初回解析の失敗、output_dir がリポジトリの中、警告の取得の2回の失敗など）。`done: true` は、`summary` を動かす時。

`actions` の形：

| type | 項目 | 統括エージェントがすること |
| --- | --- | --- |
| `fetch_issues` | `task, repo_root, filter_file, output_file` | Coverity との通信の部品で、条件に合う警告を `output_file` に書く（`done: false` なら同じ引数で呼び直す）。終わったら `--finished <task>` |
| `fetch_details` | `task, repo_root, stream, cids, output_dir` | 同じく、警告経路を `output_dir` に書く |
| `launch` | `task, role (worker\|reviewer\|integrator), item, description, prompt, background` | 役割に合うエージェントに `prompt` をそのまま渡す（`description` は task の説明。`background` なら終わりを待たずに次の tick へ）。終わったら `--finished <task>`、失敗したら `--failed <task> --reason …` |
| `command` | `task, command, note` | コマンドを動かす（20秒以内に終わる）。終わったら `--finished <task>` |

status の出力（画面のため。読むだけ）：

```json
{"ok": true, "run_id": "…", "run_dir": "…", "repo_root": "…", "stage": "items", "stage_label": "警告ごとの作業",
 "finished": false, "stopped": false, "halted": false, "halt_reason": null, "has_summary": false, "review": true, "integration": false, "max_rework": 1, "parallel": 3,
 "counts": {"pending": 3, "in_progress": 2, "done": 5, "error": 0, "merged": 0, "total": 10},
 "items": [{"id": "20001", "cids": [20001], "checker": "NULL_RETURNS", "status": "done", "phase": "result",
            "note": "…", "elapsed_seconds": 312, "stalled": false, "preparing_copy": false, "attempts": 1, "rework_rounds": 0,
            "review": {"outcome": "pass", "findings": [{"check": "C-12", "ok": true, "detail": "…"}]},
            "recommendation": "fix", "confidence": "high", "error": null}],
 "running_tasks": [{"task": "worker:20002@1", "role": "worker", "item": "20002", "description": "20002 の作業"}],
 "errors": [{"item": "20007", "kind": "取得", "detail": "…", "level": "critical"}],
 "recent": ["10:15:02 20001 の作業が終わった"],
 "initial": {"used": false, "step": null, "value": 0, "max": 100, "label": "初回解析はしない"},
 "progress": {"value": 5, "max": 10, "label": "5 / 10"},
 "run_checks": [{"id": "C-25", "ok": true, "detail": "…"}],
 "criteria": [{"id": "C-01", "scope": "item", "text": "…"}],
 "criteria_results": [{"id": "C-12", "scope": "item", "passed": 9, "failed": 1, "not_applicable": 0, "items_failed": ["20007"]}],
 "issues_found": [{"title": "…", "body": "…", "at": "ISO"}]}
```

- `halted` は、tick が `stop: true` を返した（続けられない）とき真で、理由は `halt_reason`。
- `stalled` は、10分（初回解析は30分）動きがない作業で真。修正用のコピーを用意している作業（`preparing_copy`）では偽。
- `confidence` は、作業・確かめ・ビルドでの検証のいちばん低いもの（人に見せる確信度）。`result.json` は書き換えない。
- `recent` は新しい順に10行まで。`run_checks` は一覧ができてから入る。
- `criteria` は評価基準の文（一括実行の `criteria.json` の写しから）。`criteria_results` は基準ごとの集計（`scope: item` は `review.json` の `findings` から、`scope: run` は `run_checks` から）。`summary` の出力にも同じ2つを入れる。画面は、評価基準の文をここから読む（スキルのファイルを読まない）。

### 4.13 役割の言葉

一括実行の `launch` の `role` と、エージェントの `description` に入れる言葉：

| role | 言葉 |
| --- | --- |
| `worker` | 作業役 |
| `reviewer` | 検証役 |
| `integrator` | 統合役 |

- 統括エージェントは、頼みの文に役割ごとのエージェントの id が書かれていれば、それを使う。なければ、「使う部品」の表の名前と同じ名前（`<何か>:<名前>` の形も含む）のエージェントを使う。
- 統括エージェントを使わずに一括実行のスキルを使うときは、説明に役割の言葉があるエージェントを選ぶ。
- 役割に合うエージェントがないときは、その `launch` を `--failed <task> --reason "その役のエージェントがない"` で返し、利用者に、どのエージェントを入れればよいかを伝える。汎用のサブエージェントに頼まない（元のプラグインの K-21）。

### 4.14 指示ファイル（一括実行が書き、エージェントが読む）

`launch` の `prompt` は「指示ファイル <パス> を読み、書かれた手順に従ってください。」の形で、指示ファイルを指す。指示ファイルは、それだけで仕事が分かるように書く。スキルやエージェントの名前は書かない。返す形は、指示ファイルが決める。

| 指示ファイル | 中身 |
| --- | --- |
| 作業 `items/<ID>/brief.md` | 警告の情報（CID、チェッカー、Impact、ファイルと行、関数、警告経路と前後のコード。問題の起きる行に ★）、読むコードのフォルダ（解析したリビジョンのコピーか、リポジトリ）、直すフォルダ（作業用のコピー）、結果のファイルと形（`schemas/result.schema.json`）、進み具合のファイルと段、プロジェクトの知識のファイル、守ること（コマンドは使わない。直すのは直すフォルダ・結果・進み具合だけ）、返す形（1行：「<ID>: <修正／逸脱>を推奨（確信度 <高/中/低>）— <要約>」）。作り直しのときは「確かめの指摘（直すこと）」、統合のときは「統合の指示」、形の直しのときは「結果ファイルの直すこと」の節を足す |
| 確かめ `agents/reviewer/<ID>.md` | 確かめる作業の結果のファイル、差分、読むコード、評価基準（`schemas/criteria.json` の `scope: item` のもの）、スクリプトが見つけた点、書くファイル（`review.json`）と形、守ること（結果と差分を直さない。コマンドは使わない）、返す形 |
| 統合 `agents/integrator/brief.md` | 候補の組、それぞれの結果と差分、書くファイル（`integration/plan.json`）と形、守ること、返す形 |

### 4.15 エクステンションと統括エージェント（4段階目）

エクステンションは、`new-run` で作った実行フォルダで統括エージェントを始める。頼みの文のひな形は、エクステンションの `core/messages.mjs` にだけある。頼みの文に書くこと：

- 実行フォルダ（新しく作らず、再開の確認も始める前の質問もせず、tick から進める）
- 一括実行のコマンドの場所（絶対パス）
- 役割ごとのエージェントの id（`defaults.json` から）
- 利用者には質問しないこと。許可は利用者がチャットで答えること
- 終わったら課題を `note-issue` で書き残し、1行で返すこと

統括エージェントの定義には、エクステンションとの決まりを書かない（頼みの文の中身として受け取る）。頼みの文に、利用者の頼みの文そのものは入れない（条件は画面で決まっている）。

### 4.16 結果と設定の画面のコマンド（結果と設定の画面のスキルが持ち主）

`uv run --native-tls --quiet "<画面のスキルのフォルダ>/scripts/gui.py" open --repo R [--run D] [--view monitor|summary|settings] [--item ID] [--no-browser]` → `{ok, url, reused, browser_opened}`。

- サーバーは切り離して動き、準備ができたら URL を返す（呼んだ側はプロセスを持たない）。リポジトリごとに1つで、動いていれば使い回す。使われないまま時間がたっても止まらない（元と同じ）。画面の「サーバを止める」で止まる。
- URL：`http://127.0.0.1:<ポート>/?t=<合言葉>#run=<実行の ID>&view=<区分>&item=<ID>`。127.0.0.1 だけで待ち受け、合言葉（URL かクッキー）が合わない要求を断る。
- 状態の置き場所：`~/.<スキルのフォルダの名前>/`。

### 4.17 写しの一覧

同じ取り決めを2つ以上の部品が持つもの（憲章 第25条5項）。持ち主の写しが正で、ほかは**同じバイト列の写し**にする。

- データ（JSON Schema、評価基準）は、ファイルをそのまま写す。
- 決まりをコードで持つものは、持ち主のスクリプトのファイル（標準ライブラリか、写す側も持てるライブラリだけを使うもの）を、そのまま写す。写す側は、写しを読み込んで使い、自分で書き直さない。スクリプトが読み込んでよいのは、同じフォルダにある、この表のほかのスクリプトだけ（写す側も必ず一緒に写す。例：結果の決まりと統合の計画の決まりは、逸脱コメントの決まりを読み込む）。
- 持ち主は、共通の試験のデータ（`assets/contract/<名前>/cases.json`：入力と期待する答えの組）も持ち、写しを持つ部品もそれを自分の試験で動かす。
- `tests/contracts/` が、写しが同じバイト列であることと、書く部品の出力を読む部品が受け付けることを確かめる。持ち主を直したら、写しも同じに直す（手順は `docs/replace.md`）。

| 取り決め | 持ち主（ファイル） | 写しを持つ部品 |
| --- | --- | --- |
| 結果の形 | 結果の記録（`assets/result.schema.json`） | 一括実行、同じ原因の統合、結果と設定の画面 |
| 確かめの形と評価基準 | 結果の確かめ（`assets/review.schema.json`、`assets/criteria.json`） | 一括実行 |
| 統合の計画の形 | 同じ原因の統合（`assets/plan.schema.json`。中の result の定義は `result.schema.json` の `$defs` と同じ） | 一括実行 |
| 逸脱コメントの決まり | 逸脱コメント（`scripts/check_comment.py`、`contract/comment`） | 結果の記録、同じ原因の統合、一括実行 |
| 結果の決まり | 結果の記録（`scripts/check_result.py`、`contract/result`） | 一括実行 |
| 確かめの決まり | 結果の確かめ（`scripts/check_review.py`）、機械的な確かめ（`scripts/lint_result.py`、`contract/lint`） | 一括実行 |
| 統合の計画の決まり | 同じ原因の統合（`scripts/check_plan.py`、`contract/plan`） | 一括実行 |
| 人に見せる確信度 | 一括実行（`contract/confidence`：作業・確かめ・ビルドでの検証から、いちばん低いもの） | 反映、結果と設定の画面 |
| 注釈を残す書き換え | 準備（`scripts/yamledit.py`、`contract/yaml-edit`） | 結果と設定の画面 |
| 確認の文字列 | 反映（`scripts/plantoken.py`、`contract/token`） | MCP サーバー |
| 実行フォルダと承認の形 | 一括実行（`assets/run.schema.json`、`assets/approvals.schema.json`） | 反映、結果と設定の画面、自己診断（困ったときは `run.schema.json` だけ） |
| `issues.json`・`issue-<CID>.json` | MCP サーバー | 一括実行、真偽の調査（読むだけ） |

- 実行フォルダと承認の形の写し（JSON Schema）は、読み書きの実行時には使わない。反映と結果と設定の画面は、それぞれのコードで形を確かめ、その試験が、自分の書くもの・読むものの見本を写しで確かめる（写しは、形の取り決めを各部品の中で見えるようにするためのもの）。
- Windows のユーザーの環境変数の読み方は、MCP サーバー・準備・一括実行・結果と設定の画面・困ったときが、それぞれ持つ（写しではない）。決まりは同じにする：前後の空白を除く、`${…}` のままの値は無いものとする、`REG_EXPAND_SZ` の値は `%NAME%` を展開する。各部品の試験と Windows の CI で確かめる。
- 「止まっているかもしれない」の決まり（10 分、初回解析は 30 分、スクリプトが動いている段 `copy`・`save`・`merge` は除く）は、一括実行の `status` と結果と設定の画面の監視が、それぞれ持つ。`tests/contracts` が、両方の値が同じかを確かめる。
- 写しのファイルは、持ち主と同じ名前で、スクリプトは写す側の `scripts/`（MCP サーバーはパッケージ `src/coverity_mcp/`）、データは `assets/` に置く（スクリプトは自分のフォルダの `assets/` を `Path(__file__).parents[1]` で探すので、同じ場所に置けば同じに動く）。人に見せる確信度の決まりは、一括実行が `scripts/confidence.py` に持つ。

## 5. MCP サーバー（Coverity との通信）

サーバーの登録名は `coverity-connect`、サーバーの名前（serverInfo）は `coverity_mcp`。Copilot CLI のエージェントの `tools` では `coverity-connect/<ツール>` と書く。登録名は、エージェントと MCP サーバーの取り決めである（README のすべての登録の例でこの名前を使う）。

| ツール | 注釈 | すること |
| --- | --- | --- |
| `coverity_check_connection(repo_root?, response_format="concise")` | readOnly・idempotent | 接続と認証を確かめる（`GET /api/v2/streams`）。URL、ユーザー名、認証キーがあるか（値は出さない）、http なら注意、設定の知らない項目、見えるストリームの一覧（名前の順に100まで、全体の数。detailed ではプロジェクトとトリアージストアも） |
| `coverity_search_issues(repo_root?, filter?, filter_file?, offset=0, limit=20, response_format="concise", output_file?)` | readOnly でない・破壊しない・idempotent | 条件に合う警告。会話に返すときは1ページ最大100件で `has_more`・`next_offset` を返す（concise にも `cwe` を入れる。値のない項目は省く）。`output_file` を渡すと 4.3 の形で書き、途中なら `done: false`。見えないストリームを渡すと、見えるストリームを挙げてエラーにする |
| `coverity_get_issue_details(cids, repo_root?, stream?, response_format="concise", output_dir?)` | readOnly でない・破壊しない・idempotent | CID ごとの警告経路とチェッカーの説明。会話には10件まで、`output_dir` には50件まで（4.4）。`stream` を省くと、見えるストリームから CID を探す（CID ごとに SOAP の1回の問い合わせで、ストリームを渡したときと同じ数。名前の順で最初に見つかったストリームの経路を返し、ほかは `other_streams` に挙げる。見えるストリームが100を超えるときは、ストリームを渡すよう頼む）。渡したストリームに CID がないときは、ほかのストリームと、省けることを示す |
| `coverity_preview_triage(plan_file?, entries?)` | readOnly・通信しない | 書き戻す件数・内容・確認の文字列 |
| `coverity_update_triage(confirmation_token, repo_root?, plan_file?, entries?)` | 書き込む・idempotent | 確認の文字列が合うときだけ書き戻す。ストリームからトリアージストアを決める（`GET /api/v2/streams/{name}`） |

- 設定：`repo_root` を渡すと `<repo_root>/.coverity-triage/config.yaml` の `coverity:` を読む。渡さないときは、環境変数 `COVERITY_URL`（と `COVERITY_TRIAGE_STORE`、`COVERITY_CA_FILE`、`COVERITY_API`、`COVERITY_FAKE_DATA`）を読む。認証情報は 4.1 の環境変数。
- 通信：REST v2（警告の検索、ストリーム、トリアージの書き戻し `PUT /api/v2/issues/triage`）と SOAP v9（警告経路 `getStreamDefects`、スナップショット）。TLS は `ca_file` か OS の証明書（truststore）。
- エラーの分け方：3xx・REST の 401/403・5xx・通信の失敗・JSON や SOAP でない応答は、呼び出し全体を止める。ほかの 4xx と SOAP の失敗は、その項目だけを失敗にする。条件の値が Coverity に知らないと言われたら、その値を外して続け、そのことを返す。REST の 403 は「Access Web services」の権限のことを書く。
- 偽の Coverity：`api: fake` のとき、`fake_data` のファイルを警告として返し、書き戻しを `<fake_data>.writes.jsonl` に足す。
- 引数のパスは絶対パス（サーバーは自分のフォルダで動くため）。ツールの説明に書く。

## 6. スキル

各スキルの手順・コマンド・設定の項目は、そのスキルの SKILL.md と README にある。ここには、役割と、単体のときにできることと、使う取り決めを書く。

| 役割 | 単体のとき（フォルダだけを取り出したとき） | 使う取り決め |
| --- | --- | --- |
| チェッカーの見方 | チェッカーごとの見方を答える（`references/` の資料） | なし |
| 真偽の調査 | 警告の情報（チャットか `issue-<CID>.json`）とコードから、判定・確信度・推奨と根拠を返す。警告経路と前後のコードを見やすく出すスクリプト | 4.1（knowledge.md）、4.4 |
| 修正 | 差分を見せ、同意して直す。作業用のコピーを渡されたら、そこだけを直す | 4.1（knowledge.md） |
| 逸脱コメント | 逸脱コメントと組を返し、形を確かめるスクリプトで確かめる | 4.5 の comment と組、4.17 `comment`（持ち主） |
| 結果の記録 | 結果を `result.json` の形で書く・確かめる・文にする | 4.5（持ち主）、4.17 `comment`・`result` |
| 結果の確かめ | 結果を評価基準ごとに確かめ、`review.json` の形で返す。機械的な確かめのスクリプト | 4.5、4.6（持ち主）、4.17 `lint` |
| 同じ原因の統合 | 結果の組が同じ原因かを決め、`plan.json` の形で返す | 4.5、4.7（持ち主）、4.17 `plan` |
| 準備 | `.coverity-triage/` の用意・確かめ・注釈を残した書き換え、認証情報の保存（Windows は伏せ字の入力）、uv の確かめ、ビルドの試し。接続の確かめは「Coverity と通信できる道具があれば」それで行う | 4.1、4.2（書く）、4.17 `yaml-edit`（持ち主） |
| 一括実行 | 4.12 のコマンド。Coverity との通信の道具がなければ、`new-run --issues-file <4.3 の形>` で、取ってある警告で始められる。役のエージェントがなければ、その作業を失敗として伝える（4.13） | 4.1〜4.14、4.17 |
| 反映 | 承認の確かめと反映の計画、修正のブランチ・push・svn の差分の当て、却下のグループの記録、知識の追記、効果の集計。逸脱の書き戻しは「Coverity にトリアージを書き戻せる道具があれば」計画のファイルと確認の文字列を渡す | 4.9、4.10（書く）、4.11、4.17 `token`（持ち主） |
| 結果と設定の画面 | ブラウザーの画面（4.16）。設定・条件・知識・グループにしない CID の編集、監視、結果（承認の選択、逸脱コメントの手直し、差分と修正後のファイル） | 4.1、4.2（書く）、4.8、4.9（人の選択を書く）、4.11、4.16（持ち主） |

プラグイン全体のスキル（4段階目。名前は「使う部品」の表にだけ書く）：

| 役割 | すること |
| --- | --- |
| 画面の入口 | エクステンションのツール `coverity-triage-kit_open` に頼みの文をそのまま渡し、URL と4つの注意を示す。ツールがなければ3つの原因と直し方を示す。自分で作業をしない。終わりの知らせで開き直さない |
| 困ったとき | 使い方の全体の説明（`references/`）、点検（道具・設定・環境変数・実行の様子）、画面を開く（表の画面のスキル）、接続の確かめ（表の MCP サーバー） |
| 自己診断 | ①道具とエージェント（表のエージェントがあり、役割の言葉を持つか） ②見本のリポジトリ（`assets/`）で、偽の Coverity で一括実行と反映を流し、間違った確認の文字列が断られることと、結果が期待と合うことを確かめる ③社内の Coverity を読むだけで確かめる ④ビルドの試し。伏せ字の `report.md` |

## 7. エージェント

| 役割 | 段階 | model | tools | 使う部品の表 |
| --- | --- | --- | --- | --- |
| 作業役（作業エージェント） | 2 | `[gpt-6-luna, claude-haiku-5.5]` | read、edit、search、execute、skill、ask_user、Coverity との通信の読むツール | チェッカーの見方、真偽の調査、修正、逸脱コメント、結果の記録、Coverity との通信 |
| 統括エージェント | 3 | `[claude-sonnet-5.5, gpt-6.1-sol]` | （書かない。3.3） | 一括実行、Coverity との通信、作業役・検証役・統合役のエージェント、結果と設定の画面、反映、準備 |
| 検証役（検証エージェント） | 3 | `[claude-sonnet-5.5, gpt-6.1-sol]` | read、search、edit、skill | 結果の確かめ、チェッカーの見方 |
| 統合役（統合エージェント） | 3 | `[gpt-6-luna, claude-haiku-5.5]` | read、search、edit、skill | 同じ原因の統合、逸脱コメント、チェッカーの見方 |

- 作業役：ask_user があれば（利用者と話しているとき）、足りない情報を聞き、作業中のファイルを直す前に差分を見せて同意を得る。ないときは、頼みの文とそれが指すファイルの決まりに従う。
- 統括エージェント：一括実行のスキルの手順に従う。`launch` の `prompt` をそのまま渡す。ファイルを直さない。警告を自分で調べない。コマンドは一括実行と画面のスキルのものだけ。手順（再開の確認、許可の案内、tick の繰り返し、終わりの振り返り）は、一括実行のスキルの SKILL.md にだけ書き、定義には写さない。
- 検証役：書いてよいのは `review.json` と進み具合だけ。結果と差分は直さない。
- 統合役：書いてよいのは `integration/plan.json` と進み具合だけ。
- 各エージェントの使い方は `<名前>.README.markdown`。

## 8. エクステンション（4段階目）

元のプラグインのエクステンションの画面の部品（画面の作り、見た目、記録、止め方、Issue の起票、立ち直り）を引き継ぎ、動かし方を次のように変える。追加のパッケージは使わない。

### 8.1 流れ

1. 画面の入口のスキルが `coverity-triage-kit_open(requestText)` を呼ぶ。エクステンションは、サーバーを起動し（127.0.0.1、既定 8731、使われていれば空いている番号、URL に合言葉 `?k=`）、`run.py settings` で画面の最初の値を作り、頼みの文から条件の言い方を拾い（REQ-405）、URL と注意の文（`core/messages.mjs`）を返す。実行中なら、今の URL と BUSY の文を返す。`run.py runs` を1回動かして、uv の用意を済ませる。
2. 画面で開始すると、エクステンションは：
   1. 統括エージェントのモデルは `startAgent` の `model` で、作業・検証・統合のモデルと思考の深さは `rpc.tools.updateSubagentSettings`（`defaults.json` の id）で、この会話の中だけ上書きする。終わったら上書きを消す（この会話のほかのエージェントの指定も消えるので、開始の前に知らせる）。
   2. `run.py new-run` を動かす（画面の条件ファイル・Impact・件数・初回解析・繰り返しの上限を引数にし、`--review` を付ける）。失敗したら準備に戻し、理由を示す。
   3. 統括エージェントを、4.15 の頼みの文で始める（`rpc.tasks.startAgent`、バックグラウンド）。
3. 実行中は、`run.py status` を、前の呼び出しが終わってから次を呼ぶ（重ねない）。状態が変わっているあいだは2秒ごと、変わらないときと画面が見られていないときは5秒ごと。10秒で打ち切り、プロセスの木を止める。Copilot CLI のイベントから、エージェントの状態・確認の待ち・課題を作る。
4. 統括エージェントが、`status.finished` が偽のまま止まったら（止めるボタンのときを除く）、同じ実行フォルダで、続きからの頼みの文で始め直す（2回まで。REQ-415）。
5. 統括エージェントが終わり、実行が終わっていれば（`finished`）、`summary` を動かして、その出力と `status` から結果の画面を作る（一覧は作り直されるが、中身は同じ）。止めた実行と途中で終わった実行では `summary` を動かさず（作業用のコピーを残し、続きからできるように）、`status` から作る。結果と設定の画面へのリンクは、4.16 のコマンドを `--no-browser` で動かして作る。
6. 課題は、イベントから機械的に分かるものと、`status.issues_found`（統括エージェントが書いたもの）から作る。止めた実行と途中で終わった実行では、機械的に分かるものだけ。

### 8.2 見せ方

| 画面の項目 | もと |
| --- | --- |
| 役割ごとの枠（統括1、作業 `parallel` 枠、検証1、統合1）と、今の担当・状態・実際のモデル | イベント（サブエージェントの開始・終了・失敗）。役割は `defaults.json` の id（全体か `:` の後ろ）で決める。担当は task の説明（`launch.description`）。終わったものは「これまでのエージェント」 |
| 計画・実行・評価 | `status.stage`：初回解析・取得・経路・作業・統合・ビルドでの検証は実行、確かめは評価（検証役が動いていれば評価）。tick のあいだで段の作業がないときは計画 |
| 作業ごとの様子・エラー・最近の動き・初回解析・全体の進み | `status` |
| 検証の結果（評価基準ごと） | `criteria.json` の文（`status` と `summary` が返す id ごとの合否）。`scope: item` は警告ごとの `findings` から、基準ごとの合格・不合格の件数と警告ごとの合否。`scope: run` は `run_checks` |
| 件数・一覧・処理できなかった作業 | `summary`（`counts`、`approvals_counts`、`results`、`unprocessed`） |
| 詳細レポートと差分、承認の画面へのリンク | 4.16 の URL（確信度のいちばん低い1件に `&item=<ID>`） |

終わった理由：

| 理由 | 決め方 |
| --- | --- |
| すべて合格した | `finished`、作業がすべて done、確かめがすべて pass、実行ごとの基準（`run_checks`）に不合格がない、エラーと未処理がない（条件に合う警告がないときも。「条件に合う警告はなかった」を添える） |
| 上限の回数に達した | `finished`、要確認（flag）・未検証・エラー・未処理の作業がある、または実行ごとの基準に不合格がある（その数や基準の ID を添える） |
| ユーザーが止めた | 止めるボタン |
| 途中で終わった | 一括実行が続けられなかった（tick の error）／統括エージェントが途中で終えた（始め直しの上限を超えた）／統括エージェントの失敗／チャットからの中断／AI クレジットの上限／`/clear` で切れた／エクステンションの再起動 |

止めきれなかったエージェントや処理があれば、理由に添える。

### 8.3 守り（onPreToolUse）

- 止めた実行のエージェント（木のすべて）のツールは、すべて拒む。
- この実行の統括エージェント（`startAgent` で始めた id）には、次だけを許す：読む・探すツール（view、grep、glob、rg）、エージェントのツール（task、read_agent、list_agents、write_agent）、skill、Coverity との通信の読むツール、シェル（この実行の `run.py` か、画面の `gui.py` を動かすときだけ）。
  - シェルは、引用符の外に `; & | < >`、バッククォート、`$(`、改行がない1つのコマンドだけを許す。引用符の中（C のコードやエラーの文）は問わない。ただし `"…"` の中のバッククォートと `$(` は拒む。入力を渡す形は `echo '<JSON>' | uv run … "<run.py>" <コマンド> … --stdin` の1つだけを許す。
  - Coverity の読むツールは、名前だけか、`coverityServer` の登録名で修飾した名前（`<登録名>/…`、`<登録名>-…`、`mcp__<登録名>__…` など）だけを許す。ほかのサーバーの同じ名前のツールは拒む。
- 検証役のほかのエージェントが `items/*/review.json` を書くのを拒む。
- メインの会話のエージェントと、この実行の外のエージェントは拒まない（チャットのほかの作業を止めない）。
- フックは例外を投げない。拒む理由は「Denied by preToolUse hook: <理由>」でモデルに返る。
- 許可の求め（`onPermissionRequest`）は見るだけで、いつも `{kind: "no-result"}` を返す。
- 記録のツール `coverity-triage-kit_open` は `skipPermission: true`。失敗は `{textResultForLlm, resultType: "failure"}` で返し、例外を投げない。
- 止めるとき：統括エージェントを取り消し、木のツールを拒み、`run.py stop` で切り離した処理を止める。8秒で止まらないものは「止まっていないエージェント」と止め方（チャットで Esc を2回押すか、Copilot CLI を終える）を示す。
- 記録は `<COPILOT_HOME または ~/.copilot>/plugin-data/coverity-triage-kit/runs/<日時>-<id>/`（log.jsonl、result.json、alive、issues/）。20件まで残す。`/clear` などで読み込み直したときの立ち直りは、元のプラグインと同じ。

### 8.4 `defaults.json`

エージェントとスキルの名前とコマンドの場所は、ここにだけ書く。

```json
{"orchestrator": "coverity-triage-kit:triage-orchestrator",
 "roles": {"worker": "coverity-triage-kit:triage-worker", "reviewer": "coverity-triage-kit:triage-reviewer",
           "integrator": "coverity-triage-kit:triage-integrator"},
 "runCommand": "skills/coverity-run/scripts/run.py",
 "guiCommand": "skills/coverity-gui/scripts/gui.py",
 "applySkill": "coverity-apply", "setupSkill": "coverity-setup",
 "pluginSkills": ["coverity-triage-screen", "coverity-help", "coverity-selftest"],
 "port": 8731, "issueRepo": "xclang-brain/plugin-market-place",
 "agentDir": "com.github.copilot/agents", "coverityServer": "coverity-connect",
 "coverityReadTools": ["coverity_check_connection", "coverity_search_issues", "coverity_get_issue_details",
                       "coverity_preview_triage"]}
```

- `roles` と `orchestrator` の `:` の後ろは、統括エージェントの「使う部品」の表の名前と同じ（`tests/contracts/` で確かめる）。
- コマンドの場所は、プラグインのフォルダからの相対パス。
- `coverityReadTools` は、守り（8.3）で統括エージェントに許す、Coverity との通信の読むツールの名前。`coverityServer` は、その MCP サーバーの登録名で、守りの照合と、`new-run --mcp-name`（許可の案内の起動のコマンド）に使う。
- `agentDir` は、役のエージェントの定義のフォルダ（プラグインのフォルダからの相対パス）。画面は、その定義の `model:` を読んで、役ごとの既定のモデルの候補にする（REQ-404）。定義がなければ候補なしとして、今のモデルを使う。
- Issue の起票先は `issueRepo`。起票には `gh` とログインが要る。起票できないときは、案を並べ、手順（`gh` を入れて `gh auth login`、起票先のリポジトリに書き込めること）を示す。

## 9. 試験

プラグインのフォルダで `python3 tests/run.py`（Windows は `py -3 tests/run.py`）。モデルを使わない試験をすべて動かし、合否を `tests/results/`（git に入れない）にも書く。試験は、リポジトリの中に `__pycache__`・`.venv`・結果のファイルを残さない。

### 9.1 モデルを使わない試験

| 試験 | 置き場所 | 確かめること |
| --- | --- | --- |
| 構造 | `tests/structure/` | `plugin.json`・`mcp.json` を公式の schema（`tests/structure/schemas/`）で確かめる。すべての SKILL.md の frontmatter（Agent Skills の決まりと 3.1）。`agents/` の中の `.md` は `.agent.md` だけ |
| スキル | `skills/<名前>/tests/` | スクリプトの振る舞い。フォルダだけでも動く |
| MCP サーバー | `mcp-servers/<名前>/tests/` | ツール（偽の通信・偽の Coverity）、stdio での始まり（`server/discover` → `initialize` → ツール）。フォルダだけでも動く |
| 模擬の Coverity との結合 | `tests/integration/` | MCP サーバーと模擬の Coverity Connect（REST v2・SOAP v9、`tests/coverity_emulator/`）。一括実行の流れを、決まった振る舞いの偽のエージェントで最後まで流し、反映（偽の Coverity への書き戻し）まで確かめる |
| 取り決め | `tests/contracts/` | 4.17 の写しが同じ。書く部品の出力を、読む部品が受け付ける。`defaults.json` と統括エージェントの表と役割の言葉が合う |
| 独立性 | `tests/independence/` | 1段階目の部品のフォルダに、ほかの部品の名前・プラグインの名前・外を指すパスがない。部品を1つずつ別の場所にコピー（名前を変えたコピーも）して、その試験が通る。エージェントの定義は、部品の名前を表にだけ書き、`tools` の MCP のツールは表の MCP サーバーのものだけ。2段階目の組（作業エージェントと表の部品）と3段階目の組（エージェントの表をたどった部品）が、名前で閉じている。上の段の文書に下の段の中身がない。部品の名前は、フォルダ・`mcp.json`・`defaults.json`・表から読む（試験に書かない） |
| エクステンション | `tests/extension/` | 偽の SDK と偽のコマンドで、開く・開始・状態・止める・守り・始め直し・終わった理由・課題・起票・画面のサーバー。作業エージェントの名前を替えても動く。偽の DOM と Playwright で画面（幅 390 px、ライトとダーク） |
| 文書 | `tests/docs/` | 版の一致（`plugin.json`、仕様書、設計書、変更履歴、マーケットプレイスの一覧、リポジトリの README）、要求の番号の追跡（10章）、仕様書と設計書に部品の名前がない（取り決めの名前を除く） |

### 9.2 EVAL（実際のモデル。CI では動かさない）

| 段階 | 置き場所 | 動かし方 |
| --- | --- | --- |
| 1（スキル） | `skills/<名前>/evals/<課題>/`（skill-creator の形：`prompt.md`、`case.yaml`、`graders/`、`files/`、`place-files.sh`） | skill-creator の手順で、スキルありとなしを比べて採点する（作業の場所はリポジトリの外）。`claude plugin eval` でも動く |
| 1（MCP） | `mcp-servers/<名前>/evals/`（mcp-builder の形の `evaluation.xml` と、フォルダの中の偽の Coverity のデータ） | `tests/eval/run_mcp_eval.py` が、Claude Code に MCP サーバーだけを渡して答えさせ、文字列で照合する |
| 2 | `tests/eval/stage2/` | 作業エージェントとその表の部品だけを一時フォルダに取り出し、Claude Code のエージェントに置き換えて、1件のトリアージをさせる。利用者と話す道筋（足りない情報を聞く、直す前に同意を得る）は、Agent SDK で質問に決まった答えを返して動かす（できないときは手動の確認） |
| 3 | `tests/eval/stage3/` | 3段階目の部品だけを取り出し、統括エージェントをメインにして、偽の Coverity で一括のトリアージをさせる |
| 4 | `tests/eval/stage4/` | 画面の入口：Claude Code に入口のスキルと偽の `_open` を渡し、頼みの言い方ごとに入口が選ばれるか（一括実行のスキルのコマンドを打ったときは選ばれないか）を確かめる。画面の流れは、偽の SDK で統括エージェントのイベントを流す試験（9.1）と、Copilot CLI での手動の確認 |

### 9.3 Copilot CLI の代わりに Claude Code で試すとき

- エージェントの定義は、試験の道具（`tests/eval/claude_adapter.py`）が Claude Code の形に置き換える。tools の読み替え：read → Read・Glob・Grep、search → Grep・Glob、edit → Edit・Write、execute → Bash、agent → Agent、skill → Skill、ask_user → AskUserQuestion、`<登録名>/<ツール>` → `mcp__<登録名>__<ツール>`。frontmatter に `skills:`（表のスキル）を足す。本文は変えず、先頭に読み替えの1段落を足す。
- 動かし方：`claude -p --agent <名前> --agents <変換した JSON> --mcp-config <EVAL の mcp.json> --strict-mcp-config --setting-sources project --permission-mode acceptEdits --allowedTools Skill "Bash(uv run:*)" "mcp__coverity-connect__*" --add-dir <output_dir>`（`Skill` を許さないと、`allowed-tools` を持つスキルを `-p` で読めない）。`--add-dir` は Copilot CLI の `/add-dir` の代わり。
- Claude Code のサブエージェントは、さらにサブエージェントを呼べないので、3段階目は統括エージェントをメイン（`--agent`）にする。
- Claude Code 2.1.319 の既定のツールには Glob と Grep がないので、`--tools` に、使うエージェントの組み込みのツールを合わせたものを渡す。エージェントのツールは、始まりの記録では `Task` と出る。`claude -p` では AskUserQuestion は出ない。Claude Code の中から動かすときは、環境変数 `CLAUDECODE` を外す。
- Claude Code で再現しないもの：ask_user がサブエージェントにないこと（`claude -p` ではメインにもない）、`agents/` の `.md` の読み込み、エクステンション、モデルの名前、Copilot CLI の許可の確認。これらは 9.4。

### 9.4 Copilot CLI で実測すること

自動の試験では確かめられない。新しい版で、手で確かめ、版と日付を記録する（憲章 第5条3項）。

| 確かめること | 版 | 日付 | 結果 |
| --- | --- | --- | --- |
| `startAgent` で始めた、`tools` のない統括エージェントから呼んだ作業役が、`edit` で結果のファイルを書ける | | | 未確認 |
| `startAgent` で始めたエージェントに ask_user があるか | | | 未確認 |
| `task` に、プラグインのエージェントの名前を、プラグインの名前なし・ありのどちらで渡せるか | | | 未確認 |
| サブエージェントで skill ツールが使え、Base directory が示されるか | | | 未確認 |
| エージェントの `tools` での MCP のツールの書き方（`<登録名>/<ツール>`） | | | 未確認 |
| uv のキャッシュがないときの初めての起動で、`server/discover` の後に `initialize` で始まるか | | | 未確認 |
| プラグインの `mcp.json` の `env` の `${…}` が展開されるか | | | 未確認 |
| プラグインのスキルを `/<スキル>` と `/<プラグイン>:<スキル>` のどちらで呼べるか | | | 未確認 |
| 画面の入口が、頼みの言い方ごとに選ばれるか（一括実行のスキルのコマンドと分かれるか） | | | 未確認 |
| `updateSubagentSettings` の上書きが、統括エージェントが呼ぶエージェントに効くか | | | 未確認 |
| Windows で `run.py status` を2秒ごとに動かしたときの時間 | | | 未確認 |
| 夜通しの実行で、始めた後に許可の確認が出ないか（3.5 の手順の後） | | | 未確認 |

## 10. 要求の追跡

| 要求 | 設計書 | 確かめ方 |
| --- | --- | --- |
| REQ-001 | 1、2.1、8 | `tests/independence/`、EVAL 3 |
| REQ-002 | 2.2、3.1、3.2、3.3 | `tests/independence/`（コピーして試験）、EVAL 2・3 |
| REQ-003 | 2.1、3.1、3.3、8.4 | `tests/independence/`、`tests/contracts/` |
| REQ-004 | 4、4.17 | `tests/contracts/`、`tests/integration/` |
| REQ-005 | 3.2、3.4 | 各部品の試験（CI の Windows） |
| REQ-006 | 2.1、3.1 | `tests/independence/`、`tests/docs/` |
| REQ-007 | 2.2、5、8.3、8.4 | `tests/independence/` |
| REQ-008 | 3.5 | `tests/docs/` |
| REQ-101 | 3.1 | `tests/structure/`、`tests/independence/` |
| REQ-102 | 3.1、3.4 | 各スキルの試験 |
| REQ-103 | 4.1 | 各スキルの試験、`tests/contracts/` |
| REQ-104 | 3.2、4.1 | 各部品の試験 |
| REQ-105・REQ-106 | 3.1、3.3 | 各部品の試験、`tests/independence/` |
| REQ-107 | 4.1、4.17 | 準備と画面の試験、`tests/contracts/`（`yaml-edit`） |
| REQ-111〜REQ-113 | 3.2、5 | MCP サーバーの試験、`tests/integration/`、EVAL 1（MCP） |
| REQ-121〜REQ-126 | 3.1、6 | 各スキルの試験、EVAL 1（スキル） |
| REQ-201〜REQ-205 | 3.3、7 | `tests/independence/`、EVAL 2 |
| REQ-301〜REQ-310 | 3.5、4.12〜4.14、7 | 一括実行の試験、`tests/integration/`、EVAL 3 |
| REQ-401・REQ-402 | 6、8.1 | 画面の入口の試験、EVAL 4 |
| REQ-403〜REQ-407 | 8.1、8.4 | `tests/extension/` |
| REQ-411〜REQ-415 | 8.1〜8.3 | `tests/extension/` |
| REQ-421・REQ-422 | 8.1、8.2 | `tests/extension/` |
| REQ-423 | 8 | `tests/extension/`（Playwright） |
| REQ-501〜REQ-506 | 4.5〜4.7、6 | 手順のスキルの試験、EVAL 1〜3 |
| REQ-507 | 4.11 | 一括実行の試験、`tests/integration/` |
| REQ-508 | 4.9、4.10 | 反映の試験、MCP サーバーの試験、`tests/integration/` |
| REQ-509 | 3.5 | EVAL 3 の時間と件数（目安）、9.4 |
| REQ-601 | 9.1 | `tests/run.py` |
| REQ-602 | 9.2 | `tests/eval/REPORT.md` |
| REQ-603 | 9.1、9.2 | `tests/independence/`、EVAL 2・3 |
