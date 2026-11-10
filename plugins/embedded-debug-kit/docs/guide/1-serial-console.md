# serial-console の使い方

シリアルでつないだ機器で、コマンドを動かすスキルです。

## 手順

1. ソースのフォルダで CLI を起動し、「serial-console の設定ファイルを作って」と依頼します。そのフォルダに、`.serial-console/config.yaml` ができます。
2. 設定ファイルを開き、シリアルポートを `port` に書きます。Windows では `COM3`、Linux では `/dev/ttyUSB0` のように書きます。
3. 「機器で uname -a を動かして」のように依頼します。AI が、コマンドの出力と終了コードを報告します。

設定ファイルには、各項目の上に、説明と例がコメントで書いてあります。
[skill-config](1-skill-config.md) を使うと、AI の質問に答えるだけで、AI が値を書きます。

スキルを指定して呼ぶときは、`/embedded-debug-kit:serial-console 機器で uname -a を動かして` と入力します。

## 設定の例

```yaml
port: COM3
baud: 115200
login_user: root
login_password_env: DEVICE_PASSWORD
commands:
  - /opt/my_app/my_app --selftest
```

機器がログインを求めるときは、`login_user` にユーザー名を書きます。

パスワードは、環境変数で渡します。CLI を起動する前に、環境変数にパスワードを入れ、`login_password_env` にその環境変数の名前を書きます。
`login_password` に、パスワードを直接書くこともできます。

いつも同じコマンドを動かすときは、`commands` に書き、「設定ファイルのコマンドを動かして」と依頼します。

## ログ

機器の出力は、`.serial-console/logs/` に残ります。パスワードは `***` に置き換わります。

## してはいけないこと

- スキルが動いている間に、端末ソフト（Tera Term など）で同じポートを開かないでください。スキルが、機器の出力を正しく読めなくなります。
- パスワードをチャットに書かないでください。
