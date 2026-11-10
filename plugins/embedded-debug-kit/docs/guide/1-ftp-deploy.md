# ftp-deploy の使い方

PC のファイルを、FTP で機器に送るスキルです。

## 手順

1. 機器の FTP のパスワードを、環境変数に入れます。環境変数の名前は自由です（例：`DEVICE_FTP_PASSWORD`）。
2. 同じターミナルで、ソースのフォルダで CLI を起動し、「ftp-deploy の設定ファイルを作って」と依頼します。`.github/embedded-debug-kit/ftp-deploy.yaml` ができます。
3. 「ftp-deploy の設定を、質問しながら埋めて」と依頼し、AI の質問に答えます。パスワードの渡し方を質問されたら、手順 1 の環境変数の名前を答えます。自分で書くときは、設定ファイルのコメントに従います。
4. 「機器に送って」と依頼します。送信の記録は、`.github/embedded-debug-kit/ftp-deploy/logs/` に残ります。

スキルを名前で指定して呼ぶときは、`/embedded-debug-kit:ftp-deploy 機器に送って` と入力します。

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

## してはいけないこと

- `remote_dir` を、機器のシステムのフォルダ（`/bin` など）にしないでください。機器にある同じ名前のファイルが、上書きされます。
- パスワードをチャットに書かないでください。
