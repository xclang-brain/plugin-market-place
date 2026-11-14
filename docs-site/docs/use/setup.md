# 準備

最初の1回だけ行います。

## 前提条件

- GitHub Copilot CLI
- [uv](https://docs.astral.sh/uv/)
- git 2.32 以降、または svn
- Coverity Connect のアカウントと認証キー

## 1. インストール

```shell title="ターミナル"
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install coverity-triage-kit@plugin-market-place
```

## 2. 認証情報の設定

Coverity のユーザー名を `COV_USER` に、認証キーを `COV_AUTH_KEY` に設定します。認証キーは Coverity Connect で作成し、ダウンロードしたファイルの `"key"` の値を使います。

!!! warning "認証キーの入力場所"
    自分のターミナルで入力します。チャットに貼り付けた認証キーは使われません。

=== "PowerShell（Windows）"

    <!-- ctk:credentials:powershell -->

=== "bash（Linux）"

    <!-- ctk:credentials:bash -->

    このターミナルの中だけで有効です。新しいターミナルで `copilot` を起動するときは、先にこの3行を実行します。

## 3. URL とストリームの登録

リポジトリの最上位のフォルダーで `copilot` を起動し、Coverity Connect の URL とストリーム名を付けて、次のように入力します。

```text title="チャットに入力"
/coverity-triage-kit:coverity-setup https://coverity.example.co.jp:8443 app-main
```

設定ファイル `.coverity-triage/config.yaml` が作られ、Coverity への接続が確認されます。**「準備ができました」と表示されれば完了です。**

ほかの表示が出たときは、示された項目を直して、同じ文をもう一度入力します（設定ファイルは上書きされません）。対処は[トラブルシューティング](trouble.md#準備と接続)にあります。
