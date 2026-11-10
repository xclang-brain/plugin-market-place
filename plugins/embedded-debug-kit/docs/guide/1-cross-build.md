# cross-build の使い方

機器で動かすプログラムを、PC でビルドするスキルです。

## 手順

1. ソースのフォルダで CLI を起動し、「cross-build の設定ファイルを作って」と依頼します。そのフォルダに、`.cross-build/config.yaml` ができます。
2. 設定ファイルを開き、ビルドのコマンドを `build_command` に、ビルドでできるファイルを `outputs` に書きます。
3. 「ビルドして」と依頼します。AI が、ビルドの結果を報告します。失敗したときは、エラーの行と、見直す点を示します。

設定ファイルには、各項目の上に、説明と例がコメントで書いてあります。
[skill-config](1-skill-config.md) を使うと、AI の質問に答えるだけで、AI が値を書きます。

スキルを指定して呼ぶときは、`/embedded-debug-kit:cross-build ビルドして` と入力します。

## 設定の例

```yaml
sdk_env: /opt/poky/4.0/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi
build_command: make -j4
outputs:
  - build/my_app
```

`sdk_env` には、機器向けのコンパイラーを使えるようにする環境設定スクリプトを書きます。

## ログ

ビルドの出力は、`.cross-build/logs/` に残ります。

## してはいけないこと

- `build_command` に、機器への転送や書き込みのコマンドを入れないでください。
- 同じフォルダで、2つのビルドを同時に動かさないでください。
