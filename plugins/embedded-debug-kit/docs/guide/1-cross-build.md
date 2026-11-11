# cross-build の使い方

機器で動かすプログラムを、PC でビルドするスキルです。

## 手順

1. [skill-config](1-skill-config.md) で、準備を済ませます。
2. ソースのフォルダで CLI を起動し、「ビルドして」と依頼します。

スキルを名前で指定して呼ぶときは、`/embedded-debug-kit:cross-build ビルドして` と入力します。

## 設定の例

```yaml
sdk_env: /opt/poky/4.0/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi
build_command: make -j4
outputs:
  - build/my_app
```

## してはいけないこと

- `build_command` に、機器に送るコマンドや、フラッシュに書き込むコマンドを入れないでください。ビルドのたびに、機器が書き換わります。
- 同じフォルダで、2つのビルドを同時に動かさないでください。
