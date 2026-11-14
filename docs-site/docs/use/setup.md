# 準備

最初の1回だけ行います。

## 必要なもの

- GitHub Copilot CLI
- [uv](https://docs.astral.sh/uv/)
- git 2.32 以降、または svn
- Coverity Connect のアカウントと認証キー

## 1. インストールする

```shell title="ターミナル"
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install coverity-triage-kit@plugin-market-place
```

## 2. 認証情報を設定する

Coverity のユーザー名を `COV_USER` に、認証キーを `COV_AUTH_KEY` に設定します。認証キーは Coverity Connect で作成し、ダウンロードしたファイルの `"key"` の値を入力します。

!!! warning "認証キーは、チャットに貼り付けない"
    自分のターミナルで入力します。チャットに貼り付けた認証キーは使われません。

=== "PowerShell（Windows）"

    <!-- ctk:credentials:powershell -->

    ユーザーの環境変数に保存されるので、次からは不要です。

=== "bash（Linux）"

    <!-- ctk:credentials:bash -->

    このターミナルの中だけで有効です。新しいターミナルで `copilot` を起動するときは、先にこの3行を実行します。

## 3. URL とストリームを登録する

リポジトリの最上位のフォルダーで `copilot` を起動し、Coverity Connect の URL とストリーム名を付けて入力します。

```text title="チャットに入力"
/coverity-triage-kit:coverity-setup https://coverity.example.co.jp:8443 app-main
```

設定ファイル `.coverity-triage/config.yaml` が作られ、接続が確認されます。**「準備ができました」と表示されれば完了です。**

ほかの表示が出たときは、その項目を直してから、同じ文を入力します。設定ファイルがあるときは書き換えずに確認だけを行うので、何度入力しても大丈夫です。主な表示と対処は、[困ったとき](trouble.md#準備と接続)にあります。

[次へ：1件ずつ処置する](first.md){ .md-button .md-button--primary }
