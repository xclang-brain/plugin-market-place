# serial-console の使い方

シリアルでつないだ機器で、コマンドを動かすスキルです。

## 手順

1. 機器がログインを求めるときは、ログインのパスワードを、環境変数に入れます。環境変数の名前は自由です（例：`DEVICE_PASSWORD`）。
2. 同じターミナルで、ソースのフォルダで CLI を起動し、「serial-console の設定ファイルを作って」と依頼します。`.github/embedded-debug-kit/serial-console.yaml` ができます。
3. 「serial-console の設定を、質問しながら埋めて」と依頼し、AI の質問に答えます。パスワードの渡し方を質問されたら、手順 1 の環境変数の名前を答えます。自分で書くときは、設定ファイルのコメントに従います。
4. 「機器で uname -a を動かして」のように依頼します。機器の出力は、`.github/embedded-debug-kit/serial-console/logs/` に残ります。

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
