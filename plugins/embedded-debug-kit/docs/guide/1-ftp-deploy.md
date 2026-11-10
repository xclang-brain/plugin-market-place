# ftp-deploy の使い方

PC のファイルを、FTP で機器に送るスキルです。

## 手順

1. ソースのフォルダで CLI を起動し、「ftp-deploy の設定ファイルを作って」と依頼します。そのフォルダに、`.ftp-deploy/config.yaml` ができます。
2. 設定ファイルを開き、機器のアドレスを `host` に、送るファイルを `files` に、機器の送り先のフォルダを `remote_dir` に書きます。
3. 「機器に送って」と依頼します。AI が、送ったファイルを報告します。失敗したときは、原因と、見直す点を示します。

設定ファイルには、各項目の上に、説明と例がコメントで書いてあります。
[skill-config](1-skill-config.md) を使うと、AI の質問に答えるだけで、AI が値を書きます。

スキルを指定して呼ぶときは、`/embedded-debug-kit:ftp-deploy 機器に送って` と入力します。

## 設定の例

```yaml
host: 192.168.1.50
user: root
password_env: DEVICE_FTP_PASSWORD
files:
  - build/my_app
remote_dir: /opt/my_app
file_mode: 755
```

機器で動かすプログラムを送るときは、`file_mode` に `755` と書きます。

パスワードは、環境変数で渡します。CLI を起動する前に、環境変数にパスワードを入れ、`password_env` にその環境変数の名前を書きます。
`password` に、パスワードを直接書くこともできます。

## ログ

機器とのやりとりは、`.ftp-deploy/logs/` に残ります。パスワードは `***` に置き換わります。

## してはいけないこと

- `remote_dir` を、機器のシステムのフォルダ（`/bin` など）にしないでください。同じ名前のファイルが置き換わります。
- パスワードをチャットに書かないでください。
