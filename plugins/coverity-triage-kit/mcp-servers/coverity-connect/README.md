# coverity-connect（MCP サーバー）

Coverity Connect から警告を取得し、利用者が OK と答えた内容を Coverity Connect に登録する MCP サーバーです。プラグインの `mcp.json` に登録されていて、Copilot CLI が起動します。

## 読み取るもの

| 内容 | 場所 |
| --- | --- |
| 接続先の URL、ストリーム、取得する条件 | `<リポジトリ>/.coverity-triage/config.yaml` の `coverity.url`、`coverity.stream`、`conditions.*` |
| ユーザー名と認証キー | 環境変数 `COV_USER`、`COV_AUTH_KEY`（Windows では、ユーザーの環境変数に保存した値も読み取ります） |

## 4つのツール

どのツールにも、`repo_root`（リポジトリの最上位フォルダーの絶対パス）を指定します。

| ツール | 行うこと | 応答の主な項目 |
| --- | --- | --- |
| `coverity_check_connection` | Coverity Connect に接続し、設定したストリームがあることを確認します | `ok`、`message`（利用者に伝える文）、`streams`（参照できるストリーム） |
| `coverity_search_issues` | 設定した条件に合う警告を取得します。`output_file` を指定すると、一覧をそのファイル（結果フォルダーの `issues.json`）に保存します | `count`（取得した件数）、`total_matched`（条件に合う総数）、`limited`（上限を超える警告がある） |
| `coverity_get_issue_details` | CID ごとに、警告の経路（イベント）とチェッカーの説明を取得します。`output_dir` を指定すると、`<output_dir>/<CID>/issue.json` に保存します | `saved`（保存した CID）、`failed`（取得できなかった CID と理由） |
| `coverity_update_triage` | `entries_file`（登録する内容のファイル）の Classification・Action・Severity・コメントを、CID ごとに登録します。登録した CID は、同じフォルダーの `written.json` に記録します | `written`（登録した CID）、`skipped`（登録済みの CID）、`failed`（登録できなかった CID と理由） |

## 共通の動作

- 1回の呼び出しは、約 20 秒以内に応答します。応答の `done` が `false` のときは、同じ引数でもう一度呼び出すと、続きから進みます。保存済みの警告と登録済みの CID は、そのまま使用します。
- `coverity_update_triage` は、`entries_file` と、作成時に記録されたハッシュ値（`<entries_file>.sha256`）が一致するときだけ登録します。Copilot CLI は、このツールを呼び出す前に許可の確認を表示します。
- エラーは、原因と対処を示す日本語の文で返します。CID 1件だけの失敗は `failed` に入り、ほかの CID の処理は続きます。
- 認証キーの値は、応答にもファイルにも含めません。
