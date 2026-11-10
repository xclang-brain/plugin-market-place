# ftp-deploy の使い方

PC のファイルを、FTP で機器に送るスキルです。

## 手順

1. ソースのフォルダで CLI を起動し、「ftp-deploy の設定ファイルを作って」と依頼します。そのフォルダに、`.ftp-deploy/config.yaml` ができます。
2. 設定ファイルを開き、コメントの説明に従って、機器のアドレスを `host` に、送るファイルを `files` に、機器の送り先のフォルダを `remote_dir` に書きます。[skill-config](1-skill-config.md) を使うと、AI の質問に答えるだけで済みます。
3. パスワードを入れる環境変数の名前を決め、`password_env` に書きます。
4. CLI を終了します。環境変数にパスワードを入れ、同じターミナルで CLI を起動し直します。
5. 「機器に送って」と依頼します。AI が、送ったファイルを報告します。

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

## ログ

機器とのやりとりは、`.ftp-deploy/logs/` に残ります。パスワードは `***` に置き換わります。

## してはいけないこと

- `remote_dir` を、機器のシステムのフォルダ（`/bin` など）にしないでください。機器にある同じ名前のファイルが、上書きされます。
- パスワードをチャットに書かないでください。
