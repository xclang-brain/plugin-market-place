---
name: coverity-setup
description: Coverity の警告を処置するための準備を行う。設定ファイル（.coverity-triage/config.yaml）を作成し、設定、認証情報（環境変数 COV_USER と COV_AUTH_KEY）、Coverity への接続を確認する。/coverity-triage-kit:coverity-setup に Coverity Connect の URL とストリームを付けて入力されたとき、「Coverity の準備をして」「設定を確認して」「Coverity に接続できるか確認して」と依頼されたとき、設定ファイルがないと表示されたときに使う。
allowed-tools: ["shell(uv run:*)"]
---

# Coverity の準備

設定ファイル `<リポジトリ>/.coverity-triage/config.yaml` を作成し、設定、認証情報、Coverity への接続を確認します。`<リポジトリ>` は、git または svn の作業ツリーの最上位フォルダーです。

## 1. スクリプトを実行する

Coverity Connect の URL とストリームが入力されたとき（例 `/coverity-triage-kit:coverity-setup https://coverity.example.co.jp:8443 app-main`）は、設定ファイルを作成して確認します。設定ファイルが既にあるときは、書き換えずに確認します。

```
uv run --native-tls --quiet "<このスキルのフォルダー>/scripts/setup.py" init --repo "<リポジトリ>" --url "<URL>" --stream "<ストリーム>"
```

URL とストリームの入力がないときは、確認だけを行います。

```
uv run --native-tls --quiet "<このスキルのフォルダー>/scripts/setup.py" check --repo "<リポジトリ>"
```

## 2. 結果を伝える

スクリプトは JSON を1つ返します。`error` があるときは、その文を利用者に伝えて終了します。それ以外は、次の順に伝えます。

1. `message` を、そのまま伝えます。
2. `checks` のうち、`ok` が `false` の項目の `name` と `message` を伝えます。`message` が「使用しないキーがあります」で始まる項目も伝えます。
3. `message` が「設定ファイルがありません」で始まる項目があるときは、「`/coverity-triage-kit:coverity-setup https://coverity.example.co.jp:8443 app-main` のように、Coverity Connect の URL とストリームを入力してください」と依頼します。
4. `credentials` があるときは、利用者の OS に合わせて、次の文と、その下にコードブロックをそのまま表示します。
   - Windows: 「別の PowerShell で次を実行してから、同じコマンドをもう一度入力してください」と `credentials.powershell`
   - Linux: 「Copilot CLI を終了し、ターミナルで次を実行してから、同じターミナルで copilot を起動して、同じコマンドをもう一度入力してください」と `credentials.bash`

認証キーは、利用者が自分のターミナルで入力します。チャットに貼り付けられた認証キーは使用しません。

## 3. 接続を確認する

`fetch` があるときは、そこに書かれたツールを `arguments` のとおりに呼び出し、応答の `message` を利用者に伝えます。スクリプトの `ok` とツールの応答の `ok` がどちらも `true` のときは、最後に「準備ができました。「条件に合う警告を取得して」と入力してください」と伝えます。
