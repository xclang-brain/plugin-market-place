# cross-build の使い方

機器で動かすプログラムを、PC でビルドするスキルです。

## 手順

1. ソースのフォルダで CLI を起動し、「cross-build の設定ファイルを作って」と依頼します。そのフォルダに、`.cross-build/config.yaml` ができます。
2. 「cross-build の設定を、質問しながら埋めて」と依頼し、AI の質問に答えます。自分で書くときは、設定ファイルのコメントに従います。
3. 「ビルドして」と依頼します。AI が、ビルドの結果を報告します。ビルドの出力は、`.cross-build/logs/` に残ります。

スキルを名前で指定して呼ぶときは、`/embedded-debug-kit:cross-build ビルドして` と入力します。

## 設定の例

```yaml
sdk_env: /opt/poky/4.0/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi
build_command: make -j4
outputs:
  - build/my_app
```

## してはいけないこと

- `build_command` に、機器に送るコマンドや、機器に書き込むコマンドを入れないでください。
- 同じフォルダで、2つのビルドを同時に動かさないでください。
