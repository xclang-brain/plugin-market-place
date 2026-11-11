# serial-console の使い方

シリアルでつないだ機器で、コマンドを動かすスキルです。

## 手順

1. [skill-config](1-skill-config.md) で、準備を済ませます。
2. 機器がログインを求めるときは、ログインのパスワードを、準備で答えた名前の環境変数に入れます。
3. 同じターミナルで、ソースのフォルダで CLI を起動し、「機器で uname -a を動かして」のように依頼します。

いつも同じコマンドを動かすときは、設定ファイルの `commands` に書き、「設定ファイルのコマンドを動かして」と依頼します。

スキルを名前で指定して呼ぶときは、`/embedded-debug-kit:serial-console 機器で uname -a を動かして` と入力します。

## 設定の例

```yaml
port: COM3
login_user: root
login_password_env: DEVICE_PASSWORD
commands:
  - /opt/my_app/my_app --selftest
```

## してはいけないこと

- スキルを使うときは、端末ソフト（Tera Term など）で同じポートを開いたままにしないでください。機器の出力を端末ソフトが先に受け取り、スキルに届かなくなります。
- パスワードをチャットに書かないでください。
