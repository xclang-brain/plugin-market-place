# スキルを単体で使う（1段階目）

このプラグインの使い方は3段階あります（[README の「3つの段階」](../../README.md#3つの段階)）。
この文書では、1段階目として、スキルを1つずつ使う方法を説明します。
作業エージェント（`device-worker`）に任せる2段階目は、[作業エージェントを使う](2-device-worker.md) にあります。

4つのスキル（ビルド、機器への転送、機器のログ取得、設定）は、どれも単体で使えます。
ほかのスキルやエージェントは要りません。
プラグインをインストールして使うほか、スキルのフォルダだけをコピーして使うこともできます。

| スキル | すること | 使い方 |
| --- | --- | --- |
| `cross-compile` | 機器向けにビルドする | [skills/cross-compile/README.md](../../skills/cross-compile/README.md) |
| `deploy` | ビルドしたファイルを FTP で機器へ転送する。転送後に、ファイルの権限（例：755）を変更できる。チェックサムのファイルも一緒に転送できる | [skills/deploy/README.md](../../skills/deploy/README.md) |
| `log-capture` | シリアルコンソールで機器のコマンドを実行し、出力を記録する | [skills/log-capture/README.md](../../skills/log-capture/README.md) |
| `setup` | 利用者に質問しながら、YAML の設定ファイルの項目を埋める | [skills/setup/README.md](../../skills/setup/README.md) |

各スキルの README（表の「使い方」の列）には、次のことが書いてあります。

- 依頼できる作業
- 設定ファイルの項目
- 結果とログの見方
- うまくいかないときの対処
- テストの実行方法

## プラグインをインストールして使う

インストールの方法は、[README の「インストール」](../../README.md#インストール) を見てください。

インストールしたら、Copilot CLI のチャットで `/embedded-debug-loop:<スキル名>` の形のコマンドを入力します。

```text
/embedded-debug-loop:cross-compile
/embedded-debug-loop:deploy
/embedded-debug-loop:log-capture
/embedded-debug-loop:setup
```

コマンドの後ろに、依頼の内容を続けて書けます（例：`/embedded-debug-loop:deploy 設定を確認して`）。

コマンドを使わずに、ふつうの文で依頼することもできます。
依頼の内容がスキルの説明に合えば、Copilot CLI がそのスキルを選んで使うことがあります。
スキルの説明は、`SKILL.md` の `description:` に書いてあります。

## プラグインをインストールせずに使う

スキルのフォルダ（例：`skills/deploy/`）だけをコピーすれば、プラグインなしで使えます。
スキルのフォルダは、GitHub にあります（[xclang-brain/plugin-market-place/plugins/embedded-debug-loop/skills](https://github.com/xclang-brain/plugin-market-place/tree/main/plugins/embedded-debug-loop/skills)）。

Copilot CLI は、`SKILL.md` の `name:` の値でスキルを呼び出します。
そのため、フォルダの名前を変えてコピーするときは、`name:` もフォルダと同じ名前に変えます。

1. スキルのフォルダを、次のどちらかにコピーします。
   - そのプロジェクトだけで使う：プロジェクトの `.github/skills/<スキル名>/`
   - すべてのプロジェクトで使う：`~/.copilot/skills/<スキル名>/`
2. Copilot CLI がすでに起動しているときは、`/skills reload` を実行します。
3. Copilot CLI で `/<スキル名>`（例：`/deploy`）と入力します。候補にスキルが表示されれば、準備は完了です。
4. 続けて、依頼の内容を入力します（例：`/deploy 設定を確認して`）。

コピーの方法は、各スキルの README にもくわしく書いてあります。

## 初めて使うとき

ビルド・転送・ログ取得のスキルは、最初に設定ファイルの必須の項目を埋める必要があります。
次の順に進めます。

1. スキルに作業を依頼します（例：`/embedded-debug-loop:deploy 機器に送って`）。
   スキルは設定ファイルを作り、作業をせずに止まります。そして、値が入っていない必須の項目を知らせます。
2. 必須の項目に値を入れます。方法は次のどちらかです。
   - 設定ファイルを直接編集する
   - `setup` スキルの質問に答える（例：`/embedded-debug-loop:setup .deploy/config.yaml を設定して`）
3. もう一度、同じ作業を依頼します。必須の項目がすべて埋まっていれば、今度はスキルが作業を実行します。

設定ファイルでは、各項目のすぐ上のコメントに、その項目の意味が書いてあります。

## 設定ファイルとログの場所

ビルド・転送・ログ取得のスキルは、作業フォルダの `.<スキル名>/` に設定ファイルとログを置きます。
作業フォルダは、スキルがコマンドを実行するフォルダです。
ふつうは、Copilot CLI を起動したプロジェクトのフォルダです。

| スキル | 設定ファイル | ログ |
| --- | --- | --- |
| `cross-compile` | `.cross-compile/config.yaml` | `.cross-compile/logs/` |
| `deploy` | `.deploy/config.yaml` | `.deploy/logs/` |
| `log-capture` | `.log-capture/config.yaml` | `.log-capture/logs/` |

- `<スキル名>` は、スキルのフォルダの名前です。フォルダの名前を変えてコピーしたときは、その名前になります（例：`deploy-a` なら `.deploy-a/`）。
- 設定ファイルとログは、Git にコミットされません。パスワードを含むことがあるので、スキルが `.<スキル名>/` に `.gitignore`（中身は `*`）を作ります。
- `setup` スキルには、自分の設定ファイルはありません。

## setup スキルが埋めるファイル

`setup` スキルは、依頼の文で指定した YAML の設定ファイルを埋めます。

```text
/embedded-debug-loop:setup .deploy/config.yaml を設定して
```

- ファイルを指定しないときは、作業フォルダの中から候補を探し、どれを埋めるかを質問します。
- 候補になるのは、説明のコメントが付いた項目がある YAML ファイルです。
- スキルのフォルダの中にある設定ファイルのひな形（`config-template.yaml`）は、候補になりません。

## 許可の確認

スキルを使うと、Copilot CLI が許可を求めることがあります。
表示された内容を確認し、問題がなければ許可します。

- スキルがコマンド（`python3 …`）を実行するときに、実行してよいかを聞かれます。
- SDK の場所やシリアルポートなど、作業フォルダの外のパスを使うときに、「Allow path access」の確認が出ることがあります。

## 同じ名前のスキルがあるとき

次の場所に同じ名前のスキル（`deploy`、`setup` など）があると、Copilot CLI はそちらを使います。
このプラグインのスキルは使われません。

- 個人のフォルダ（`~/.copilot/skills/`）
- プロジェクト（`.github/skills/`）

思ったスキルが使われないときは、`/skills list` を実行します。どの場所のスキルが読み込まれているかを確認できます。
