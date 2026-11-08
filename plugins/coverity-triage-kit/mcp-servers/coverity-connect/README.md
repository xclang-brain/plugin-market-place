# coverity-connect（Coverity Connect の MCP サーバー）

チャットから Coverity Connect の警告を探し、警告経路を読み、トリアージ（Classification・Action・Severity・コメント）を書き戻せるようにする MCP サーバーです。このフォルダだけで動きます。

- [できること](#できること)
- [必要なもの](#必要なもの)
- [置き方と準備](#置き方と準備)
- [登録のしかた](#登録のしかた)
- [設定](#設定)
- [ツール](#ツール)
- [ファイルで受け渡す使い方](#ファイルで受け渡す使い方)
- [偽の Coverity](#偽の-coverity)
- [うまくいかないとき](#うまくいかないとき)
- [試験](#試験)

## できること

| 頼み方の例 | 使うツール |
| --- | --- |
| 「Coverity につながるか確かめて」「どんなストリームがある？」 | `coverity_check_connection` |
| 「ストリーム app-main の、Impact が High の警告を一覧にして」 | `coverity_search_issues` |
| 「未分類の NULL_RETURNS は何件ある？続きも見せて」 | `coverity_search_issues`（ページを続けて取る） |
| 「CID 20001 の警告経路とチェッカーの説明を見せて」（ストリームが分からなくてもよい） | `coverity_get_issue_details` |
| 「CID 20002 を False Positive、Ignore、コメント『誤検知。…』で書き戻して」 | `coverity_preview_triage` → 同意 → `coverity_update_triage` |

- 書き戻しは2段です。先に件数と内容と確認の文字列が示されます。示されただけでは何も書き込まれません。確認の文字列が書き戻す内容と合わなければ、何も書き込まれません。
- 同じ書き戻しをもう一度頼んでも、同じ項目は二重に書き込まれません。
- 1回の呼び出しは 20 秒ほどで区切ります（クライアントは 30 秒で呼び出しを切ることがあるため）。続きがあるときは、そう返ります。

## 必要なもの

- [uv](https://docs.astral.sh/uv/)（Python と、要るライブラリを用意します。Python 3.10 以上を uv が選びます）
- Coverity Connect の URL と、ユーザー名と認証キー。ユーザーには「Access Web services」の権限が要ります。
- 書き戻しには Coverity Connect 2022.6.0 以上（REST の `PUT /api/v2/issues/triage`）が要ります。

uv がないとき：

```text
# Windows（PowerShell）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# Linux（bash）
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 置き方と準備

このフォルダを好きな場所に置きます（下の例では `C:/tools/coverity-connect`、`/home/me/tools/coverity-connect`）。
初めて使う前に、ふつうのターミナルで一度だけ次を動かします。ライブラリを取ってきて用意するので、MCP のクライアントからの起動で待たされません（社内の証明書は `--native-tls` で OS のものを使います）。

```text
# Windows（PowerShell）
uv sync --native-tls --frozen --directory "C:/tools/coverity-connect"
# Linux（bash）
uv sync --native-tls --frozen --directory "/home/me/tools/coverity-connect"
```

起動のコマンド（クライアントに登録するもの）は次のとおりです。標準入出力（stdio）で話します。

```text
uv run --native-tls --frozen --quiet --directory "<このフォルダの絶対パス>" coverity-connect-mcp
```

## 登録のしかた

登録の名前は、どのクライアントでも `coverity-connect` にします（ほかの道具がこの名前でツールを呼ぶことがあります）。

認証キーは設定ファイルにもチャットにも書かず、環境変数に置きます（[認証情報](#認証情報)）。

### GitHub Copilot CLI

```text
# Windows（PowerShell）
copilot mcp add coverity-connect -- uv run --native-tls --frozen --quiet --directory "C:/tools/coverity-connect" coverity-connect-mcp
# Linux（bash）
copilot mcp add coverity-connect -- uv run --native-tls --frozen --quiet --directory "/home/me/tools/coverity-connect" coverity-connect-mcp
```

Copilot CLI は、MCP サーバーに PATH のほかの環境変数を渡しません。

- Windows では、サーバーがユーザーの環境変数（`HKCU\Environment`）を直接読むので、このままで使えます。
- Linux と macOS では、`~/.copilot/mcp-config.json` の登録に `env` を書いて渡します（値は `${名前}` で、Copilot CLI を起動したシェルの環境変数から取ります）。

```json
{
  "mcpServers": {
    "coverity-connect": {
      "type": "local",
      "command": "uv",
      "args": ["run", "--native-tls", "--frozen", "--quiet", "--directory", "/home/me/tools/coverity-connect", "coverity-connect-mcp"],
      "env": {"COV_USER": "${COV_USER}", "COV_AUTH_KEY": "${COV_AUTH_KEY}", "HTTPS_PROXY": "${HTTPS_PROXY}", "NO_PROXY": "${NO_PROXY}"},
      "tools": ["*"]
    }
  }
}
```

`user_env`・`key_env` で環境変数の名前を変えたときは、その名前を `env` に書きます。値が空のときと、`${…}` のまま届いたときは、「設定されていない」とみなします。

### VS Code（`.vscode/mcp.json`）

```json
{
  "servers": {
    "coverity-connect": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--native-tls", "--frozen", "--quiet", "--directory", "C:/tools/coverity-connect", "coverity-connect-mcp"],
      "env": {"COV_USER": "${env:COV_USER}", "COV_AUTH_KEY": "${env:COV_AUTH_KEY}"}
    }
  }
}
```

### Claude Code

```text
# Windows（PowerShell）
claude mcp add --transport stdio coverity-connect -- uv run --native-tls --frozen --quiet --directory "C:/tools/coverity-connect" coverity-connect-mcp
# Linux（bash）
claude mcp add --transport stdio coverity-connect -- uv run --native-tls --frozen --quiet --directory "/home/me/tools/coverity-connect" coverity-connect-mcp
```

プロジェクトの `.mcp.json` に書くときは、`"env": {"COV_USER": "${COV_USER}", "COV_AUTH_KEY": "${COV_AUTH_KEY}"}` を添えます。

## 設定

設定は次のどちらかで渡します。

1. **リポジトリの設定ファイル**（ふだんの使い方）：ツールに `repo_root`（調べるリポジトリの絶対パス）を渡すと、`<repo_root>/.coverity-triage/config.yaml` の `coverity:` を読みます。ほかの項目（`vcs:` など）は読みません。
2. **環境変数だけ**（リポジトリなし）：`repo_root` を渡さないと、下の環境変数を読みます。

### `config.yaml` の `coverity:`

```yaml
coverity:
  url: https://coverity.example.com:8443   # Coverity Connect の URL（必須）
  # user_env: COV_USER                     # ユーザー名を入れた環境変数の名前
  # key_env: COV_AUTH_KEY                  # 認証キーを入れた環境変数の名前
  # triage_store: Default Triage Store     # 書き戻しの項目に stream がないときのトリアージストア
```

| 項目 | 既定 | 意味 |
| --- | --- | --- |
| `url` | （必須。`api: fake` のときは省ける） | Coverity Connect の URL（`https://` か `http://`） |
| `api` | `auto` | `auto`（Coverity Connect と通信する）か `fake`（[偽の Coverity](#偽の-coverity)） |
| `user_env` / `key_env` | `COV_USER` / `COV_AUTH_KEY` | ユーザー名と認証キーを入れた環境変数の名前（同じ名前にしない） |
| `triage_store` | `Default Triage Store` | 書き戻しの項目に `stream` がないときに書くトリアージストア |
| `revision_field` | `sourceVersion` | 解析したリビジョンが入ったスナップショットの項目（空なら読まない） |
| `path_strip_prefixes` | `[]` | このサーバーは使いません（ほかの道具のための項目） |
| `ca_file` | （空＝OS の証明書） | 証明書のファイル（`.coverity-triage/` からの相対か絶対） |
| `fake_data` | （空） | `api: fake` のときの偽の警告のファイル（`.coverity-triage/` からの相対か絶対） |

`coverity:` の中の知らない項目は使いません。接続の確かめ（`coverity_check_connection`）の結果の `unknown_keys` に出るので、書き間違いに気づけます。

### 環境変数だけで使うとき

| 環境変数 | 意味 |
| --- | --- |
| `COVERITY_URL` | Coverity Connect の URL（`COVERITY_API` が `fake` でなければ必須） |
| `COVERITY_TRIAGE_STORE` | `triage_store` と同じ（既定 `Default Triage Store`） |
| `COVERITY_CA_FILE` | 証明書のファイル（絶対パス） |
| `COVERITY_API` | `auto` か `fake` |
| `COVERITY_FAKE_DATA` | 偽の警告のファイル（絶対パス） |

### 認証情報

- ユーザー名は `COV_USER`、認証キーは `COV_AUTH_KEY` に入れます（名前は `user_env`・`key_env` で変えられます）。設定ファイルには書きません。
- Windows では、プロセスにない変数を、ユーザーの環境変数（`HKCU\Environment`）から読みます。保存した後にクライアントを起動し直さなくても読めます。
- 認証キーの値は、ツールの結果にもログにも出しません。ログと記録のファイルでは、ユーザー名と認証キーの値を `****` に置き換えます。
- 社内のプロキシは `HTTPS_PROXY`・`NO_PROXY` で渡します。証明書は `ca_file` か OS の証明書を使います。リダイレクトはたどりません。

## ツール

パスの引数は、どれも絶対パスで渡します（サーバーは自分のフォルダで動くため）。結果は JSON（structuredContent）で返ります。

### `coverity_check_connection(repo_root?, response_format="concise")`

接続と認証を確かめ、このユーザーが見られるストリームの一覧を返します（読み取りの問い合わせを1回：`GET /api/v2/streams`）。`ok`、説明（`result`）、URL、環境変数があるか（値は出さない）、`http` なら注意、知らない項目を返します。

- `streams`：ストリームの名前の並び（名前の順、100 個まで）と、その数 `streams_total`。100 個を超えたら `streams_note` で知らせます。
- `response_format="detailed"` では、`streams` の各項目が `{"name", "project", "triage_store"}` になります（`triage_store` は、そのストリームの警告のトリアージを書き戻す先）。
- 偽の Coverity では、警告のファイルのストリームを返します。

### `coverity_search_issues(repo_root?, filter | filter_file, offset=0, limit=20, response_format="concise", output_file?)`

1つのストリームの、条件に合う警告を探します。

```json
{"streams": ["app-main"], "checkers": ["NULL_RETURNS", "MISRA C-2012 Rule 10.*"], "impacts": ["High"],
 "triage": {"classification": ["Unclassified"], "action": ["Undecided"], "status": ["New", "Triaged"]}}
```

- `streams` はちょうど1つ。同じ項目の値は「どれか」、項目どうしは「すべて」。`checkers` は `* ? [ ]` のワイルドカードが使えます。値の大文字と小文字は区別しません（`high` → `High`）。
- `filter`（引数の object）に知らない項目があれば、エラーにします（書き間違いで絞り込みが外れないように）。`filter_file`（YAML か JSON）の知らない項目は読み飛ばし、`skipped_keys` に出します。
- 探すのは最新のスナップショットの警告です。`triage.status` を省くと、最新のスナップショットのすべての警告（`New`・`Triaged`・`Dismissed`）を返します。`Fixed` と `Absent Dismissed` の警告は前のスナップショットにしかないので、`triage.status` にそれがあるときだけ、前のスナップショットの警告も探します。Status とワイルドカードのチェッカーは、検索のあとで絞ります（外した数は `excluded`）。
- 見られるストリームにないストリーム（書き間違いなど）は、「0 件」ではなくエラーにし、見られるストリームの名前を添えます（警告が1件もないときだけ、ストリームの一覧で確かめます）。
- Coverity にない値（たとえば Test Advisor のない Coverity での `Tested Elsewhere`）は、外して探し、`ignored_values` に出します。
- 会話に返すとき：1ページ `limit` 件（1〜100）。`has_more` が true なら、`offset` に `next_offset` を渡して続きを取ります。`total` は Coverity が返した件数（Status とワイルドカードで絞る前）です。結果が 25,000 文字を超えそうなときは、ページを短く区切ります。
- `response_format`：`concise` は `cid, checker, file, line, function, impact, cwe, classification, action, status, stream`、`detailed` はそれに `category, severity, merge_key` を足します。どちらも、値のない項目は省きます（例：CWE のない MISRA の警告には `cwe` がない）。
- `output_file` を渡すと、ファイルに書きます（[ファイルで受け渡す使い方](#ファイルで受け渡す使い方)）。

### `coverity_get_issue_details(cids, repo_root?, stream?, response_format="concise", output_dir?)`

CID ごとの警告経路（イベント）とチェッカーの説明を返します。各警告に、経路を取ったストリーム（`stream`）が入ります。

- `stream` を渡すと、そのストリームの経路を返します（警告の検索の結果の各警告に `stream` があります）。
- `stream` を省くと、見られるすべてのストリームから CID を探し、見つけたストリームの経路を返します（結果の `streams_searched` は探したストリームの数）。2つ以上のストリームにある CID は、名前の順で最初のストリームの経路を返し、ほかのストリームを `other_streams` に入れます。そのストリームの経路が要るときは、`stream` を渡して呼び直します。
- 会話に返すときは 10 件まで。イベントは経路の順で、`file, line, tag, description` と、問題の起きる場所なら `main: true`。`concise` ではイベントが 40 件を超えると間を省きます（`omitted_events`）。`detailed` は `impact, category, cwe` と、すべてのイベントを返します。
- 取れなかった CID は、その CID だけが `failed` になります。`stream` を渡していたときは、`failed_note` に、ほかの見られるストリームの名前と、`stream` を省けば探せることを書きます。時間か大きさで取りきれなかった CID は `remaining` に入ります。その CID だけで呼び直します。

`stream` を省いたときの問い合わせ（本物の Coverity での費用）：

- ストリームの一覧（`GET /api/v2/streams`）を1回。一覧は 5 分のあいだ覚えておきます（接続の確かめでも覚えます）。
- CID ごとに SOAP の `getStreamDefects` を1回。見られるストリームをすべて `streamIdList` に並べ、答えのストリームごとの警告から選びます。ストリームを1つずつ問い合わせると、本物の Coverity では1回に 1〜5 秒かかるので、1回の呼び出し（20 秒）に収まらず、結果が問い合わせの順で変わります。まとめて1回にすると、問い合わせの数は `stream` を渡したときと同じで、選ぶストリームは名前の順で決まります。
- 見られるストリームが 100 個を超えるときは、問い合わせが大きくなりすぎるので探さず、`stream` を渡すようにエラーで知らせます。

### `coverity_preview_triage(plan_file | entries)`

書き戻す件数（`counts`）と内容（`items`）と、確認の文字列（`confirmation_token`）を返します。Coverity とは通信せず、何も書き込みません。

```json
[{"id": "20002", "cids": [20002], "stream": "app-main", "classification": "False Positive",
  "action": "Ignore", "severity": "Unspecified", "comment": "誤検知。…"}]
```

- `stream` を書くと、そのストリームが使うトリアージストアに書き戻します（書かなければ `triage_store`）。
- `plan_file` は `{"triage": [項目…]}` の JSON のファイルです（ほかの鍵は読みません。項目の中の知らない鍵も読み飛ばします）。`entries` の項目に知らない鍵があれば、エラーにします。

### `coverity_update_triage(confirmation_token, repo_root?, plan_file | entries)`

人が件数と内容を見て同意した後に、`coverity_preview_triage` に渡したのと同じ `plan_file` か `entries` と、返った `confirmation_token` を渡すと書き込みます。

- 書き込み済みの項目（`id`・`cids`・`stream`・値・コメントが同じもの）は、並べ方や確認の文字列が変わっても書き直しません。
- Coverity が値の誤り（401・403 のほかの 4xx）で1項目を断っても、ほかの項目は書きます。断られた項目は、同じ確認の文字列では送り直しません（値を直して preview からやり直します）。
- 認証の失敗（401・403）、5xx、接続の失敗では、呼び出し全体を止めます。止まった項目は記録しないので、直ってから同じ引数で呼ぶと書き込みます。
- 記録：`plan_file` なら同じフォルダの `<名前>.result.json`、`entries` なら一時のフォルダの `coverity-connect-mcp-<ユーザー名>/`（本人だけが読める。Coverity の URL とトリアージストアごとのファイル）。

確認の文字列の作り方（ツールを作る人のため。チャットでは自分で作らず、preview の値を使います）：

- `plan_file`：ファイルのバイト列の SHA-256 の16進の先頭12文字。
- `entries`：各項目を `id, cids（整数）, classification, action, severity（既定 ""）, comment（既定 ""）` と、あるときだけ `stream` にそろえ、`{"triage": [項目…]}` を、鍵の順に並べ、区切り `(",", ":")`、`ensure_ascii=False` で JSON にした UTF-8 の SHA-256 の先頭12文字。
- この決まりは `src/coverity_mcp/plantoken.py` にあります。計画のファイルを書く側（承認を反映する部品）のスクリプトと同じバイト列の写しで、サーバーはそれをそのまま使います。知らない鍵や空の `entries` を断るのは、ツールの入力の確かめです（決まりの外）。

### 続きの取り方

| ツール | 印 | 続け方 |
| --- | --- | --- |
| `coverity_search_issues`（会話） | `has_more: true` | `offset` に `next_offset` を渡す |
| `coverity_search_issues`（ファイル） | `done: false` | 同じ引数でもう一度 |
| `coverity_get_issue_details`（会話） | `remaining` がある | その CID だけで呼ぶ |
| `coverity_get_issue_details`（ファイル） | `done: false` | 同じ引数でもう一度 |
| `coverity_update_triage` | `done: false` | 同じ引数でもう一度（書き込み済みは飛ばす） |

## ファイルで受け渡す使い方

大きな結果を会話に流さないため、ファイルに書けます。途中なら `done: false` が返り、同じ引数で呼び直すと、保存済みの続きからします。

- `coverity_search_issues` に `output_file` を渡すと、条件の `limit` 件（既定 100）までを書き、最新のスナップショットの解析したリビジョンも記録します。`finished_at` が入るまでは途中です。

```json
{"filter_sha": "…", "stream": "app-main", "limit": 20, "started_at": "ISO", "finished_at": "ISO（途中は null）",
 "coverity_total": 52, "next_offset": null, "limited": true,
 "revision_field": "sourceVersion", "revision_checked": true, "analyzed_revision": "a1b2c3d", "revision_error": null,
 "issues": [{"cid": 20001, "checker": "NULL_RETURNS", "file": "src/reader.c", "function": "read_header", "line": 42,
             "impact": "High", "category": "…", "cwe": 476, "merge_key": "…", "stream": "app-main",
             "classification": "Unclassified", "action": "Undecided", "severity": "Unspecified", "status": "New"}],
 "excluded_count": 0, "excluded": [], "excluded_cids": [], "requests": []}
```

  値のない項目は省きます。`file` は Coverity のパスのままです。`excluded` は検索のあとの条件で外した警告の見本（50 件まで）と、合わなかった項目の値です。

- `coverity_get_issue_details` に `output_dir` を渡すと、CID ごとに `issue-<CID>.json` を書きます（50 件まで）。保存済みの CID は取り直しません。

```json
{"fetched_at": "ISO", "issue": {"cid": 20001, "checker": "NULL_RETURNS", "file": "src/reader.c", "line": 42, "stream": "app-main"},
 "events": [{"file": "src/reader.c", "line": 40, "tag": "returned_null", "description": "…", "main": false}],
 "checker_description": "…"}
```

  取れなかった CID は `{"cid": 20001, "stream": "app-main", "fetched_at": "ISO", "error": "…"}`。認証の失敗など全体が止まる失敗では、ファイルを書きません。

## 偽の Coverity

`api: fake` にすると、Coverity Connect の代わりに、YAML のファイルの警告を返します（試しと試験のため。通信しません）。書き戻しは `<fake_data>.writes.jsonl` に1行ずつ足します。

```yaml
# .coverity-triage/config.yaml
coverity:
  api: fake
  fake_data: fake-issues.yaml
```

```yaml
# .coverity-triage/fake-issues.yaml
snapshot:
  sourceVersion: a1b2c3d          # 解析したリビジョン（revision_field の名前の項目）
streams:                          # 省略可：ストリームが使うトリアージストア（ないストリームは既定のストア）
  app-release: {triage_store: Release Triage Store}
issues:
  - cid: 20001                    # 必須
    project: app
    stream: app-main
    checker: NULL_RETURNS         # 必須
    file: src/reader.c            # 必須
    function: read_header
    line: 42
    impact: High
    cwe: 476
    status: New
    classification: Unclassified
    action: Undecided
    severity: Unspecified         # 省略可
    checker_description: 戻り値が NULL のことがある関数の結果を、確かめずに使っている。
    events:
      - {file: src/reader.c, line: 40, tag: returned_null, description: "fgets が NULL を返すことがある。"}
      - {file: src/reader.c, line: 42, tag: dereference, description: "NULL かもしれない値を使う。", main: true}
```

- 同じ CID を、ストリームごとに別の項目として書けます（ストリームごとに違うトリアージを持てます）。
- 本物と同じく、`status` が `Fixed` か `Absent Dismissed` の警告は、条件の `triage.status` にそれがあるときだけ見つかります。
- 警告経路は、CID とストリームの両方が合う項目から返します。
- ストリームの一覧は、警告の `stream` と `streams:` の名前です。プロジェクトは `streams:` の `project` か、そのストリームの最初の警告の `project` です。

## うまくいかないとき

| 出たこと | 直し方 |
| --- | --- |
| 「設定ファイルがありません」 | `<repo_root>/.coverity-triage/config.yaml` を作って `coverity.url` を書く。リポジトリなしなら `repo_root` を渡さず、`COVERITY_URL` を設定する |
| 「ユーザ名または認証キーが設定されていません」 | `COV_USER`・`COV_AUTH_KEY`（または `user_env`・`key_env` の名前）を設定する。Linux・macOS の Copilot CLI では登録の `env` に書く |
| 「認証に失敗しました（401）」 | ユーザー名と認証キーを確かめる。キーを作り直したら環境変数も直す |
| 「認証に失敗しました（403）」 | ユーザーに「Access Web services」の権限があるかを Coverity の管理者に確かめる |
| 「リダイレクトを返しました」 | `coverity.url` の `https` / `http` とポート番号を確かめる。ログインの画面やプロキシに向いていないかを確かめる |
| 「応答が JSON ではありません」「SOAP の XML ではありません」 | URL がログインの画面やプロキシの画面を返していないかを確かめる |
| 「Coverity に接続できません」 | URL、社内のネットワーク、`HTTPS_PROXY`・`NO_PROXY`、証明書（`ca_file`）を確かめる |
| 「確認の文字列が書き戻しの内容と合いません」 | `coverity_preview_triage` からやり直す（内容を変えたら確認の文字列も変わる） |
| 「ストリーム … は、見られるストリームにありません」 | 添えてある名前から選ぶ（大文字と小文字も区別する）。一覧は `coverity_check_connection` の `streams` にもある |
| 「見られるストリームが … 個あり、100 個を超えるので、CID だけでは探しません」 | `stream` を渡す（警告の検索の結果の各警告に `stream` がある） |
| `done: false` / `has_more: true` / `remaining` | 失敗ではありません。[続きの取り方](#続きの取り方)のとおり呼び直す |
| クライアントにツールが出てこない | 先に `uv sync`（[置き方と準備](#置き方と準備)）をしておく。ふつうのターミナルで起動のコマンドを動かし、標準エラーに「起動（stdio）」と出るかを見る（Ctrl+C で止める） |

ログは標準エラーに出ます（標準出力は MCP の通信に使います）。

## 試験

このフォルダで動かします。偽の Coverity Connect（httpx の MockTransport）と偽のデータで、ツール、stdio での始まり（`server/discover` を先に送られても `initialize` から始められること）、ファイルの形、確認の文字列、評価の課題の答えを確かめます。

```text
uv run --native-tls --frozen --directory "<このフォルダ>" python -B -m unittest discover -s tests
```

- サーバーのライブラリのない Python で `python3 -B -m unittest discover -s tests` を動かしたときは、uv の一時の環境で同じ試験を動かします（このフォルダに `.venv` を作りません）。`-B` を付けると `__pycache__` も残りません。
- リポジトリの中で動かすときは、`UV_PROJECT_ENVIRONMENT` を一時のフォルダにすると、このフォルダに `.venv` を作りません。
- 確認の文字列の取り決めの試験は、共通の試験のデータ（`tests/contract/token/cases.json`）があるときだけ動きます。すべての組を `plantoken.py` の関数で確かめ、ツールが受け付ける組では、ツールの確認の文字列も同じになることを確かめます。

モデルに課題を解かせて採点する試験（mcp-builder の形）は [evals/README.md](evals/README.md) にあります。
