# cross-build の使い方

機器で動かすプログラムを、PC でビルドするスキルです。

## 手順

1. 「cross-build の設定ファイルを作って」と依頼します。CLI を起動したフォルダに、`.cross-build/config.yaml` ができます。
2. 設定ファイルに値を書きます。各項目の上に、説明と例があります。[skill-config](1-skill-config.md) を使うと、AI の質問に答えて書けます。
3. 「ビルドして」と依頼します。AI が、結果を伝えます。失敗したときは、エラーの行と、確かめることを伝えます。

スキルを指定するときは、`/embedded-debug-kit:cross-build ビルドして` と入力します。

## 設定

必ず書くのは、`build_command` と `outputs` です。
機器向けのコンパイラーを使うための環境設定スクリプトがあれば、`sdk_env` に書きます。

```yaml
sdk_env: /opt/poky/4.0/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi
build_command: make -j4
outputs:
  - build/my_app
```

## ログ

ビルドの出力は、`.cross-build/logs/` に、1回ごとに1つのファイルで残ります。

## してはいけないこと

- `build_command` に、機器への転送や書き込みのコマンドを入れないでください。
- 同じフォルダで、2つのビルドを同時に動かさないでください。
