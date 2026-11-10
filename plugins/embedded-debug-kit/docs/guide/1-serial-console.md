# serial-console の使い方

シリアルでつないだ機器で、コマンドを動かすスキルです。

## 手順

1. 「serial-console の設定ファイルを作って」と依頼します。CLI を起動したフォルダに、`.serial-console/config.yaml` ができます。
2. 設定ファイルに値を書きます。各項目の上に、説明と例があります。[skill-config](1-skill-config.md) を使うと、AI の質問に答えて書けます。
3. 「機器で uname -a を動かして」のように依頼します。AI が、コマンドの出力と終了コードを伝えます。

スキルを指定するときは、`/embedded-debug-kit:serial-console 機器で uname -a を動かして` と入力します。

## 設定

必ず書くのは、`port` です。機器がログインを求めるときは、ユーザー名とパスワードも書きます。

```yaml
port: COM3
login_user: root
login_password_env: DEVICE_PASSWORD
```

パスワードは、`login_password` に書くか、環境変数で渡します。環境変数で渡すときは、`login_password_env` に環境変数の名前を書きます。

いつも同じコマンドを動かすときは、`commands` に書きます。「設定のコマンドを機器で動かして」と依頼すると、上から順に動かします。

## ログ

機器の出力は、`.serial-console/logs/` に、1回ごとに1つのファイルで残ります。パスワードは `***` に置き換わります。

## してはいけないこと

- スキルが動いている間に、端末ソフトで同じポートを開かないでください。機器の出力を、正しく読めなくなります。
- パスワードをチャットに書かないでください。
