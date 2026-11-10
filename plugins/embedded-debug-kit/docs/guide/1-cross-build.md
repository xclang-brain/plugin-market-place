# cross-build の使い方

機器で動かすプログラムを、PC でビルドするスキルです。

## 手順

1. ソースのフォルダで CLI を起動し、「cross-build の設定ファイルを作って」と依頼します。そのフォルダに、`.cross-build/config.yaml` ができます。
2. 設定ファイルを開き、コメントの説明に従って、ビルドのコマンドを `build_command` に、ビルドでできるファイルを `outputs` に書きます。[skill-config](1-skill-config.md) を使うと、AI の質問に答えるだけで済みます。
3. 「ビルドして」と依頼します。AI が、ビルドの結果を報告します。

スキルを指定して呼ぶときは、`/embedded-debug-kit:cross-build ビルドして` と入力します。

## 設定の例

```yaml
sdk_env: /opt/poky/4.0/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi
build_command: make -j4
outputs:
  - build/my_app
```

## ログ

ビルドの出力は、`.cross-build/logs/` に残ります。

## してはいけないこと

- `build_command` に、機器に送るコマンドや、機器に書き込むコマンドを入れないでください。
- 同じフォルダで、2つのビルドを同時に動かさないでください。
