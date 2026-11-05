# スキルを単体で使う（1段階目）

ビルド、機器への転送、機器のログ取得、設定の4つのスキルがあります。
どれも、それだけで使えます。ほかのスキルやエージェントは要りません。

各スキルの使い方（頼めること、設定ファイルの項目、結果とログの見方、うまくいかないとき、試験の動かし方）は、スキルのフォルダの中の README に書いてあります。

| スキル | すること | 使い方 |
| --- | --- | --- |
| `cross-compile` | 機器向けにビルドする | [skills/cross-compile/README.md](../../skills/cross-compile/README.md) |
| `deploy` | ビルドしたファイルを FTP で機器へ送る。送った後に権限を付け、サム値（チェックサム）のファイルも送れる | [skills/deploy/README.md](../../skills/deploy/README.md) |
| `log-capture` | シリアルコンソールで機器のコマンドを動かし、出力を記録する | [skills/log-capture/README.md](../../skills/log-capture/README.md) |
| `setup` | YAML の設定ファイルの項目を、質問に答えながら埋める | [skills/setup/README.md](../../skills/setup/README.md) |

## プラグインを入れて使う

Copilot CLI のチャットで、プラグインの名前を付けたコマンドを打ちます。
コマンドの後ろに、やってほしいことを続けて書くこともできます（例：`/embedded-debug-loop:deploy 設定を確かめて`）。

```text
/embedded-debug-loop:cross-compile
/embedded-debug-loop:deploy
/embedded-debug-loop:log-capture
/embedded-debug-loop:setup
```

コマンドを打たなくても、依頼の内容がスキルの説明に合えば、AI がスキルを選んで使うこともあります。

## プラグインを入れずに、スキルだけを使う

スキルのフォルダ（例：`skills/deploy/`）だけを写して使えます。プラグインは要りません。

1. スキルのフォルダを、プロジェクトの `.github/skills/<スキルの名前>/` か、個人の `~/.copilot/skills/<スキルの名前>/` に写します。
2. Copilot CLI を起動したまま写したときは、`/skills reload` と打ちます。
3. `/<スキルの名前>`（例：`/deploy`）で呼びます。

くわしくは、各スキルの README を見てください。

## 設定ファイルとログの場所

ビルド・転送・ログ取得のスキルは、プロジェクトのフォルダ（Copilot CLI を起動したフォルダ）の `.<スキルの名前>/` に、設定ファイル（`config.yaml`）とログ（`logs/`）を置きます。

| スキル | 設定ファイル | ログ |
| --- | --- | --- |
| `cross-compile` | `.cross-compile/config.yaml` | `.cross-compile/logs/` |
| `deploy` | `.deploy/config.yaml` | `.deploy/logs/` |
| `log-capture` | `.log-capture/config.yaml` | `.log-capture/logs/` |

- 初めて使うと、スキルは設定ファイルを作って止まり、まだ空の必須の項目を伝えます。項目を埋めてから、もう一度頼みます。
- 項目の意味は、ファイルの中で、各項目のすぐ上に書いてあります。直接書き換えても、`setup` スキルで質問に答えて埋めてもかまいません。
- それぞれのフォルダには、スキルが `.gitignore`（中身は `*`）を作ります。設定やログ（パスワードを含むことがあります）は Git に入りません。
- `setup` スキルは、設定ファイルを持ちません。埋めるファイルを渡されなかったときは、プロジェクトのフォルダの中から、項目の説明のコメントがある YAML を探して、候補として示します。

## 許可の確認について

- スキルはコマンド（`python3 …`）を動かすので、Copilot CLI が、実行してよいかを聞いてきます。
- SDK の場所やシリアルポートなど、作業フォルダの外のパスを使うときに、「Allow path access」の確認が出ることがあります。

## 同じ名前のスキルがあるとき

個人のフォルダ（`~/.copilot/skills/`）やプロジェクト（`.github/skills/`）に、同じ名前（`deploy`、`setup` など）のスキルがあると、Copilot CLI はそちらを使います。動きがおかしいときは、`/skills list` で確かめてください。
