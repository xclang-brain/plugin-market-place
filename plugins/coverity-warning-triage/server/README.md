# MCP サーバー coverity-triage

Coverity Connect と通信する MCP サーバーです。stdio でつなぎます。coverity-warning-triage のプラグインがなくても、これだけで使えます。

## できること

| ツール | すること |
| --- | --- |
| `check_connection` | 設定と認証情報で Coverity に1回だけ問い合わせ、認証できるかを確かめる |
| `search_issues` | 条件に合う警告を検索する |
| `get_issues` | CID ごとに、警告経路（イベント）とチェッカーの説明を取る |
| `preview_triage` | 書き戻す内容を確かめ、確認の文字列を返す。Coverity とは通信しない |
| `update_triage` | 確認の文字列があるときだけ、トリアージ（分類・対応・重大度・コメント）を Coverity に書き込む |

結果の受け取り方は2つあります。

- ファイル：`output_file`・`output_dir`・`plan_file` を渡すと、結果をファイルに保存します。大きな結果を会話に流さずに済みます。
- ツールの結果：渡さなければ、結果をツールの結果で返します。1回に返す量は小さく抑えます（検索は 100 件まで、警告経路は 10 件まで）。

1回の呼び出しは、約 20 秒で返ります。続きがあるときの呼び直し方は「[続きの取り方](#続きの取り方)」にあります。

## 要るもの

- [uv](https://docs.astral.sh/uv/)
- Python 3.12 以上（`pyproject.toml` の `requires-python`）
- 書き戻し（`update_triage`）には、Coverity Connect 2022.6.0 以上（書き戻しに使う `PUT /api/v2/issues/triage` は、この版からあります）。検索は REST の v2、警告経路とスナップショットは SOAP の v9 を使います。
- 設定ファイル `<repo_root>/.coverity-triage/config.yaml`。`<repo_root>` は、ツールに `repo_root` として渡すフォルダです。
- 認証情報の環境変数（ユーザ名と認証キー）

### 設定ファイル

読むのは `coverity` の節だけです。ほかの節は読みません。

```yaml
coverity:
  url: https://coverity.example.com:8443
```

| 項目 | 既定 | 中身 |
| --- | --- | --- |
| `url` | （必須） | Coverity Connect の URL |
| `user_env` | `COV_USER` | ユーザ名を入れた環境変数の名前 |
| `key_env` | `COV_AUTH_KEY` | 認証キーを入れた環境変数の名前 |
| `triage_store` | `Default Triage Store` | 書き込むトリアージストアの名前 |
| `revision_field` | `sourceVersion` | 解析したリビジョンが入るスナップショットの項目。空にすると取らない |
| `ca_file` | なし | 社内の CA 証明書のファイル。なければ OS の証明書ストアを使う |
| `api` | `auto` | `fake` にすると、Coverity の代わりに `fake_data` の YAML のファイルを読む（試験用。書き戻しは `<fake_data>.writes.jsonl` に足す） |
| `fake_data` | なし | `api: fake` のときのファイル。相対パスは `.coverity-triage` のフォルダから見る |
| `path_strip_prefixes` | `[]` | サーバーは使わない |

`coverity` の節に、この表にない項目があると、エラーになります。

### 認証情報の環境変数

- `COV_USER` にユーザ名、`COV_AUTH_KEY` に認証キーを入れます（名前は `user_env`・`key_env` で変えられます）。設定ファイルには書きません。
- 認証キーの値は、ツールの結果にも記録にも出しません。
- Windows では、ユーザーの環境変数に保存しておけば、サーバーが直接読みます（MCP のクライアントを起動し直さなくてよい）。
- ほかの OS では、MCP のクライアントから、この2つをサーバーに渡します。下の登録の例は、どれも `env` で渡しています。

## MCP のクライアントに登録する

`<server>` は、このフォルダ（`pyproject.toml` があるフォルダ）の絶対パスに置き換えます。JSON の中では、Windows のパスの `\` を `\\` と書きます。

起動のコマンドは、どのクライアントでも同じです。

```shell
uv run --native-tls --frozen --directory <server> coverity-triage-mcp
```

- `--native-tls`：OS の証明書ストアを使って依存を取ります（社内の証明書が要るネットワークのため）。
- `--frozen`：`uv.lock` のとおりの版を使います。
- 初めての起動は、依存を取ってくるので時間がかかります。先にターミナルで `uv sync --native-tls --frozen --directory <server>` を1回動かしておくと、すぐに起動します。

### GitHub Copilot CLI（`~/.copilot/mcp-config.json`）

Copilot CLI は、PATH のほかの環境変数を、そのままでは MCP サーバーに渡しません。`env` に書きます（`${名前}` で、Copilot CLI を起動した環境の値を使えます）。`tools` は必須です。

```json
{
  "mcpServers": {
    "coverity-triage": {
      "type": "local",
      "command": "uv",
      "args": ["run", "--native-tls", "--frozen", "--directory", "<server>", "coverity-triage-mcp"],
      "env": {
        "COV_USER": "${COV_USER}",
        "COV_AUTH_KEY": "${COV_AUTH_KEY}"
      },
      "tools": ["*"]
    }
  }
}
```

### VS Code（`.vscode/mcp.json`）

最上位の項目は `servers` です。環境変数は `${env:名前}` で書きます。

```json
{
  "servers": {
    "coverity-triage": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--native-tls", "--frozen", "--directory", "<server>", "coverity-triage-mcp"],
      "env": {
        "COV_USER": "${env:COV_USER}",
        "COV_AUTH_KEY": "${env:COV_AUTH_KEY}"
      }
    }
  }
}
```

### Claude Code（プロジェクトの一番上の `.mcp.json`）

環境変数は `${名前}` で書きます。対話で使うときは、初めに、Claude Code がこのサーバーを使ってよいかを聞きます。

```json
{
  "mcpServers": {
    "coverity-triage": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--native-tls", "--frozen", "--directory", "<server>", "coverity-triage-mcp"],
      "env": {
        "COV_USER": "${COV_USER}",
        "COV_AUTH_KEY": "${COV_AUTH_KEY}"
      }
    }
  }
}
```

## ツールの引数と結果

パスの引数は、どれも絶対パスで渡します（サーバーは自分のフォルダで動くので、相対パスはそこから見えてしまいます）。期待どおりにいかなかったときは、ツールのエラー（何を直せばよいかの文）を返します。

### check_connection

| 引数 | 中身 |
| --- | --- |
| `repo_root` | 設定ファイルがあるフォルダ |

結果：`ok`（認証できたか）、`result`（結果の文）、`seconds`、`user`・`key`（環境変数があるか。値は出さない）、`url`。URL が http のときは `warning` も付きます。`api: fake` のときは、Coverity と通信せず、`fake_data` のファイルがあるかだけを確かめます。リダイレクト（3xx）や、JSON でない応答（ログインの画面など）は、`ok: false` にして、行き先や応答の形を `result` に書きます。

### search_issues

| 引数 | 中身 |
| --- | --- |
| `repo_root` | 設定ファイルがあるフォルダ |
| `filter` | 条件（下の形の object） |
| `filter_file` | 条件を書いた JSON のファイル（形は `filter` と同じ） |
| `output_file` | 結果を保存するファイル。なければ、ツールの結果で返す |
| `offset` | ツールの結果で返すときの、続きの位置（前の結果の `next_offset`）。既定は 0 |

`filter` と `filter_file` は、どちらか一方を渡します。

条件の形：

```json
{
  "streams": ["app-main"],
  "checkers": ["NULL_RETURNS", "MISRA C-2012 *"],
  "impacts": ["High", "Medium"],
  "triage": {"status": ["New"], "classification": ["Unclassified"], "action": ["Undecided"]},
  "limit": 20
}
```

- `streams` は、ちょうど1つです。ほかの項目は省けます。
- 同じ項目の中の値は、どれかに合えばよく、項目どうしは、すべてに合う警告を返します。
- `checkers` には `*`・`?`・`[` のワイルドカードが使えます。
- `project` は、Coverity Connect の検索の絞り込みには使いません。どの警告を探すかは `streams` で決まります（`project` で絞るのは `api: fake` のときだけです）。
- Status と、ワイルドカードを含むチェッカーは、Coverity の検索のあとでサーバーが絞ります。
- `limit` は、ファイルに保存するときは保存する件数の上限（既定 100）、ツールの結果で返すときは1回に返す件数（既定 20、上限 100）です。
- `name`（条件の名前）も書けます。検索には使いません。
- `filter` に、ここに挙げた項目（`streams`・`checkers`・`impacts`・`triage` の `status`・`classification`・`action`・`limit`・`project`・`name`）のほかの項目があると、エラーにします。書き間違えた項目を黙って捨てて、絞らずに検索しないためです。`filter_file` は、ほかの項目があっても読み飛ばします。

ツールの結果で返すとき（`output_file` なし）：

| 項目 | 中身 |
| --- | --- |
| `issues` | 警告の一覧。`cid`、`checker`、`file`、`line`、`function`、`impact`、`category`、`cwe`、`classification`、`action`、`severity`、`status` のうち、値のあるもの |
| `found` | `issues` の件数 |
| `next_offset` | 続きの位置。`null` なら終わり |
| `coverity_total` | Coverity が検索で返した件数（ストリーム・チェッカー・Impact・Classification・Action で絞った数）。Coverity が件数を返さなければ `null` |
| `excluded` | 検索のあとの条件（Status など）で外した件数 |
| `next` | 続きがあるときの、呼び直し方 |

ファイルに保存するとき（`output_file` あり）：結果は `done`、`found`、`limit`、`limited`（上限で止めたか）、`coverity_total`、`excluded`、`excluded_note`、`analyzed_revision`（最新のスナップショットの `revision_field` の値）、`revision_error`、`output_file`、`next` です。`output_file` には、警告（`issues`）、外した警告と合わなかった値（`excluded`、50 件まで）、送った要求（`requests`）が入ります。

### get_issues

| 引数 | 中身 |
| --- | --- |
| `repo_root` | 設定ファイルがあるフォルダ |
| `stream` | ストリームの名前 |
| `cids` | CID の一覧。ツールの結果で返すときは 10 件まで、ファイルに保存するときは 50 件まで |
| `output_dir` | 結果を保存するフォルダ（あるフォルダ）。なければ、ツールの結果で返す |

ツールの結果で返すとき（`output_dir` なし）：

| 項目 | 中身 |
| --- | --- |
| `issues` | CID ごとに `cid`、`checker`、`file`、`line`、`function`、`events`、`checker_description`（値のない項目は省く） |
| `events` | 警告経路。`file`、`line`、`tag`、`description`、`main`（警告の本体のイベントなら true）。入れ子のイベントも、たどった順に並べる |
| `failed` | 取れなかった CID と、その理由 |
| `remaining` | 時間が足りず、取らなかった CID |

ファイルに保存するとき（`output_dir` あり）：CID ごとに `issue-<CID>.json` を保存します。取れなかった CID は、理由を `error` に書いた `issue-<CID>.json` を保存します。保存済みの CID は取り直しません。結果は `done`、`saved`、`already_saved`、`failed`、`remaining`、`next` です。

認証や権限の失敗は、その CID の `error` のファイルを残さずに、エラーで止まります（ほかの CID も同じ理由で取れないため）。

### preview_triage

| 引数 | 中身 |
| --- | --- |
| `entries` | 書き戻す内容の一覧（下の形） |
| `plan_file` | 書き戻す内容を `{"triage": [...]}` の形で書いた JSON のファイル |

どちらか一方を渡します。

```json
[
  {"id": "10001", "cids": [10001], "classification": "False Positive", "action": "Ignore",
   "comment": "ループの前で NULL を確かめている"},
  {"id": "G1", "cids": [10002, 10003], "classification": "Bug", "action": "Fix Required", "severity": "Major"}
]
```

- `id` は、項目を見分けるための文字列です（結果の `written` や `failed` に出ます）。
- `severity` と `comment` は省けます。空の値は Coverity に送りません。

結果：`counts`（`entries` 書き戻す項目の数、`cids` CID の数）、`items`（書き戻す内容）、`confirmation_token`（確認の文字列）、`next`。

`counts` と `items` を人に見せ、同意を得てから `update_triage` を呼びます。

### update_triage

| 引数 | 中身 |
| --- | --- |
| `repo_root` | 設定ファイルがあるフォルダ |
| `confirmation_token` | `preview_triage` が返した確認の文字列 |
| `entries` または `plan_file` | `preview_triage` に渡したものと同じもの |

- 確認の文字列が中身と合わなければ、何も書かずにエラーにします。
- 書き込んだ結果を、次のファイルに残します（`result_file`）。
  - `plan_file` のとき：同じフォルダの `<plan_file の名前>.result.json`（例 `apply-plan.json` なら `apply-plan.result.json`）
  - `entries` のとき：一時のフォルダの `coverity-triage-mcp-<ユーザー名>/triage-<文字列>.result.json`。
    - フォルダはユーザーごとです。Linux と macOS では、自分だけが読み書きできるように作ります。
    - ファイルは、Coverity の URL（`coverity.url`）とトリアージストア（`coverity.triage_store`）の組ごとに1つです。`<文字列>` はこの組から作り、ファイルの中にも `url` と `triage_store` を書きます。
    - 一時のフォルダは、Python の `tempfile.gettempdir()` が返すフォルダです（環境変数 `TMPDIR`・`TEMP`・`TMP` の順に見ます）。
- 書き込み済みの項目は、書き直しません。項目は中身（`id`・`cids`・値・コメント）で見分けるので、項目の並べ方や確認の文字列が変わっても、同じ項目は二重に書きません。
- Coverity が1つの項目を断ったときは、その項目を `failed` にして、ほかの項目を書き込みます。
  - 5xx や認証の失敗で止まったときは、止まった項目を記録しません。サーバーが戻ってから同じ引数で呼ぶと、書き込みます。
  - 値の誤りなどで HTTP 4xx（401・403 を除く）で断られた項目は、同じ確認の文字列では書き直しません。値を直して、`preview_triage` からやり直します。
- 次のときは、そこで止まります（ほかの項目も同じ理由で書けないため）。書けなかった項目は `failed` に残さず、同じ引数で呼び直すと続きから書きます。
  - 認証や権限の失敗（HTTP 401・403）
  - HTTP 5xx（500・502・503・504 など。サーバーの中の誤り、前に置いたプロキシの失敗、サーバーが止まっているとき）

結果：`done`、`written`（今回書き込んだ項目の `id`）、`written_total`、`entries`、`failed`（`id`、`cids`、`error`）、`remaining`、`result_file`、`next`。

## 確認の文字列の作り方

確認の文字列は、SHA-256 の16進の先頭12文字です。`preview_triage` が返すので、ふつうは作る必要はありません。

- `plan_file` のとき：ファイルのバイト列の SHA-256。
- `entries` のとき：各項目を `id`、`cids`、`classification`、`action`、`severity`、`comment` の6つにそろえ（`severity` と `comment` がなければ空の文字列）、`{"triage": entries}` を次の形の JSON の文字列にして、UTF-8 のバイト列にした SHA-256。

```python
import hashlib, json

def token(entries):
    normal = [{"id": e["id"], "cids": [int(c) for c in e["cids"]],
               "classification": e["classification"], "action": e["action"],
               "severity": e.get("severity", ""), "comment": e.get("comment", "")} for e in entries]
    text = json.dumps({"triage": normal}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
```

## 続きの取り方

Copilot CLI は、既定では 30 秒でツールの呼び出しを打ち切ります。このため、1回の呼び出しは約 20 秒で返し、残りがあれば次のように知らせます。

| ツールと受け取り方 | 残りがあるときの印 | 呼び直し方 |
| --- | --- | --- |
| ファイルに保存（`search_issues`、`get_issues`） | `done: false` | 同じ引数で、`done: true` になるまで呼び直す。保存済みの続きから進む |
| `update_triage`（`plan_file` でも `entries` でも） | `done: false` | 同じ引数で、`done: true` になるまで呼び直す。書き込み済みの項目は書き直さない |
| `search_issues`（ツールの結果） | `next_offset` が `null` でない | `offset` に `next_offset` を渡して呼び直す |
| `get_issues`（ツールの結果） | `remaining` が空でない | `remaining` の CID だけを `cids` に渡して呼び直す |

## 実物の Coverity の応答への備え

- リダイレクト（3xx）は、成功にせず、行き先（Location）を示して失敗にします。URL がログインの画面やプロキシ、https のサーバーの http の URL に向いているときに起きます。
- JSON（REST）や SOAP の XML でない応答（ログインの画面の HTML など）は、応答の形を示すエラーにします。
- SOAP の認証の失敗（HTTP 401 と "User authentication failed."）と権限の不足は、呼び出し全体を止めます。ほかの SOAP の失敗は、その CID だけの失敗にします。
- REST の 403 では、ユーザ名と認証キーのほかに、Web サービスを使う権限（Access Web services）の確認も案内します。認証が正しくても、この権限がなければ API は使えません。
- 書き戻しで 5xx（500・502・503・504 など）になったときは、呼び出し全体を止めます。401・403 も止めます。それ以外の 4xx（サーバーが値を断った）は、その項目だけの失敗にします。
- 検索の応答に全件の数（totalRows）がなくても、行が空になるまで次のページを取ります。

## 記録

`output_file` のフォルダ（`search_issues`）、`output_dir` の2つ上のフォルダ（`get_issues`）、`plan_file` のフォルダ（`update_triage`）に `run.json` があると、そのフォルダの `operations.log` に、呼び出しの始まりと終わり（またはエラー）を1行ずつ足します。認証情報の値は `****` に伏せます。
