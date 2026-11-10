# serial-console の使い方

シリアルでつないだ機器で、コマンドを動かすスキルです。

## 手順

1. ソースのフォルダで CLI を起動し、「serial-console の設定ファイルを作って」と依頼します。そのフォルダに、`.serial-console/config.yaml` ができます。
2. 設定ファイルを開き、コメントの説明に従って、シリアルポートを `port` に書きます。[skill-config](1-skill-config.md) を使うと、AI の質問に答えるだけで済みます。
3. 「機器で uname -a を動かして」のように依頼します。AI が、コマンドの出力と終了コードを報告します。

スキルを指定して呼ぶときは、`/embedded-debug-kit:serial-console 機器で uname -a を動かして` と入力します。

## 機器がログインを求めるとき

手順 3 の前に、次のことをします。

1. 設定ファイルの `login_user` にユーザー名を書きます。
2. パスワードを入れる環境変数の名前を決め、`login_password_env` に書きます。
3. CLI を終了します。環境変数にパスワードを入れ、同じターミナルで CLI を起動し直します。

## いつも同じコマンドを動かすとき

設定ファイルの `commands` にコマンドを書き、「設定ファイルのコマンドを動かして」と依頼します。

## 設定の例

```yaml
port: COM3
login_user: root
login_password_env: DEVICE_PASSWORD
commands:
  - /opt/my_app/my_app --selftest
```

## ログ

機器の出力は、`.serial-console/logs/` に残ります。パスワードは `***` に置き換わります。

## してはいけないこと

- スキルが動いている間に、端末ソフト（Tera Term など）で同じポートを開かないでください。機器の出力が端末ソフトに取られ、スキルに届かなくなります。
- パスワードをチャットに書かないでください。
