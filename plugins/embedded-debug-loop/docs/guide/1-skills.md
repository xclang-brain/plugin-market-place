# スキルを単体で使う（1段階目）

4つのスキルを、1つずつ使う方法です。
どのスキルも、ほかのスキルやエージェントなしで動きます。
作業エージェント（`device-worker`）に任せる方法は、[2段階目](2-device-worker.md) にあります。

| スキル | すること | 使い方 |
| --- | --- | --- |
| `cross-compile` | 機器向けにビルドする | [README](../../skills/cross-compile/README.md) |
| `deploy` | ビルドしたファイルを FTP で機器へ転送する。転送後に権限（例：755）を変更でき、サム値（チェックサム）のファイルも転送できる | [README](../../skills/deploy/README.md) |
| `log-capture` | シリアルコンソールで機器のコマンドを実行し、出力を記録する | [README](../../skills/log-capture/README.md) |
| `setup` | 質問しながら、YAML の設定ファイルの項目を埋める | [README](../../skills/setup/README.md) |

## プラグインをインストールして使う

[インストール](../../README.md#インストール)の後、`/embedded-debug-loop:<スキル名>` に続けて依頼を入力します（例：`/embedded-debug-loop:deploy 設定を確認して`）。

## プラグインをインストールせずに使う

スキルのフォルダ（[GitHub](https://github.com/xclang-brain/plugin-market-place/tree/main/plugins/embedded-debug-loop/skills)）だけをコピーして使います。

1. フォルダを、プロジェクトの `.github/skills/<スキル名>/` か `~/.copilot/skills/<スキル名>/`（すべてのプロジェクト用）にコピーします。フォルダの名前を変えるときは、`SKILL.md` の `name:` も同じにします。
2. Copilot CLI が起動中なら、`/skills reload` を実行します。
3. `/<スキル名>` と入力します。候補にスキルが出れば、準備は完了です。
4. 続けて依頼を入力します（例：`/deploy 設定を確認して`）。

## 設定ファイルとログの場所

ビルド・転送・ログ取得のスキルは、設定ファイルとログを作業フォルダ（ふつうは、Copilot CLI を起動したフォルダ）に置きます。

- 設定ファイル：`.<スキル名>/config.yaml`（例：`.deploy/config.yaml`）
- ログ：`.<スキル名>/logs/`
- `<スキル名>` は、スキルのフォルダの名前です（`deploy-a` にコピーしたら `.deploy-a/`）。
- 設定とログ（パスワードを含むことがある）は、Git にコミットされません。スキルが `.<スキル名>/` に `.gitignore`（中身は `*`）を作ります。

## 初めて使うとき

ビルド・転送・ログ取得のスキルは、次の順に設定します。

1. 作業を依頼します（例：`/embedded-debug-loop:deploy 機器に送って`）。スキルは設定ファイルを作って止まり、空の必須の項目を知らせます。
2. 設定ファイルを直接編集するか、`setup` スキルで値を入れます（例：`/embedded-debug-loop:setup .deploy/config.yaml を設定して`）。各項目の意味は、その項目のすぐ上のコメントにあります。
3. もう一度、同じ作業を依頼します。今度は作業を実行します。

ファイルを指定しないと、`setup` スキルは作業フォルダから候補を探し、どれを埋めるかを質問します（[探し方](../../skills/setup/README.md#候補のファイルの探し方)）。

## 許可の確認

SDK の場所やシリアルポートなど、作業フォルダの外のパスを使うと、Copilot CLI が「Allow path access」の確認を出すことがあります。内容を確認して許可します。

## 同じ名前のスキルがあるとき

`~/.copilot/skills/` や `.github/skills/` に同じ名前のスキルがあると、Copilot CLI はそちらを使います。
`/skills list` で、読み込まれたスキルの場所を確認できます。
