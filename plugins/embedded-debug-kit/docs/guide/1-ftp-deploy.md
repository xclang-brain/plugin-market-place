# ftp-deploy の使い方

PC のファイルを、FTP で機器に送るスキルです。

## 手順

1. [skill-config](1-skill-config.md) で、準備を済ませます。
2. 機器の FTP のパスワードを、準備で答えた名前の環境変数に入れます。
3. 同じターミナルで、ソースのフォルダで CLI を起動し、「機器に送って」と依頼します。

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
