# ftp-deploy の使い方

PC のファイルを、FTP で機器に送るスキルです。

## 手順

1. 「ftp-deploy の設定ファイルを作って」と依頼します。CLI を起動したフォルダに、`.ftp-deploy/config.yaml` ができます。
2. 設定ファイルに値を書きます。各項目の上に、説明と例があります。[skill-config](1-skill-config.md) を使うと、AI の質問に答えて書けます。
3. 「機器に送って」と依頼します。AI が、送ったファイルを伝えます。失敗したときは、起きたことと、確かめることを伝えます。

スキルを指定するときは、`/embedded-debug-kit:ftp-deploy 機器に送って` と入力します。

## 設定

必ず書くのは、`host`、`files`、`remote_dir` です。
機器で動かすプログラムを送るときは、`file_mode` に `755` と書きます。

```yaml
host: 192.168.1.50
password_env: DEVICE_FTP_PASSWORD
files:
  - build/my_app
remote_dir: /opt/my_app
file_mode: 755
```

パスワードは、`password` に書くか、環境変数で渡します。環境変数で渡すときは、`password_env` に環境変数の名前を書きます。

## ログ

機器とのやりとりは、`.ftp-deploy/logs/` に、1回ごとに1つのファイルで残ります。パスワードは `***` に置き換わります。

## してはいけないこと

- `remote_dir` を、機器のシステムのフォルダ（`/bin` など）にしないでください。同じ名前のファイルが置き換わります。
- パスワードをチャットに書かないでください。
