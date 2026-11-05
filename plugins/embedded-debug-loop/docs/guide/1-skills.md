# スキルを単体で使う（1段階目）

このプラグインの4つのスキル（ビルド、機器への転送、機器のログ取得、設定）は、どれも単体で使えます。
ほかのスキルやエージェントは要りません。
プラグインをインストールして使うほか、スキルのフォルダだけをコピーして使うこともできます。

| スキル | すること | 使い方 |
| --- | --- | --- |
| `cross-compile` | 機器向けにビルドする | [skills/cross-compile/README.md](../../skills/cross-compile/README.md) |
| `deploy` | ビルドしたファイルを FTP で機器へ送る。送った後に権限を付けられる。サム値（チェックサム）のファイルも一緒に送れる | [skills/deploy/README.md](../../skills/deploy/README.md) |
| `log-capture` | シリアルコンソールで機器のコマンドを実行し、出力を記録する | [skills/log-capture/README.md](../../skills/log-capture/README.md) |
| `setup` | YAML の設定ファイルの項目を、質問に答えながら埋める | [skills/setup/README.md](../../skills/setup/README.md) |

各スキルの詳しい使い方は、スキルのフォルダの中の README にあります。
README には、頼めること、設定ファイルの項目、結果とログの見方、うまくいかないときの対処、テストの動かし方が書いてあります。

## プラグインをインストールして使う

Copilot CLI のチャットで、プラグインの名前を付けたコマンドを入力します。

```text
/embedded-debug-loop:cross-compile
/embedded-debug-loop:deploy
/embedded-debug-loop:log-capture
/embedded-debug-loop:setup
```

コマンドの後ろに、頼みたいことを続けて書けます（例：`/embedded-debug-loop:deploy 設定を確かめて`）。

コマンドを入力しなくても、依頼の内容がスキルの説明に合えば、AI がスキルを選んで使うことがあります。

## プラグインをインストールせずに使う

スキルのフォルダ（例：`skills/deploy/`）だけをコピーすれば、プラグインなしで使えます。
スキルのフォルダは、GitHub の [xclang-brain/plugin-market-place の plugins/embedded-debug-loop/skills/](https://github.com/xclang-brain/plugin-market-place/tree/main/plugins/embedded-debug-loop/skills) にあります。

1. スキルのフォルダを、次のどちらかにコピーします。
   - プロジェクトの `.github/skills/<スキル名>/`
   - 個人のフォルダの `~/.copilot/skills/<スキル名>/`
2. Copilot CLI を起動したままコピーしたときは、`/skills reload` を実行します。
3. `/<スキル名>`（例：`/deploy`）で呼びます。

フォルダの名前を変えてコピーするときは、`SKILL.md` の `name:` も同じ名前に変えます。
Copilot CLI は、`name:` の名前でスキルを呼びます。
設定ファイルとログのフォルダの名前は、スキルのフォルダの名前で決まります。

くわしくは、各スキルの README を見てください。

## 設定ファイルとログの場所

ビルド・転送・ログ取得のスキルは、設定ファイルとログを、作業フォルダの `.<スキル名>/` に置きます。
作業フォルダは、ふつうは Copilot CLI を起動したプロジェクトのフォルダです。

| スキル | 設定ファイル | ログ |
| --- | --- | --- |
| `cross-compile` | `.cross-compile/config.yaml` | `.cross-compile/logs/` |
| `deploy` | `.deploy/config.yaml` | `.deploy/logs/` |
| `log-capture` | `.log-capture/config.yaml` | `.log-capture/logs/` |

初めて使うときは、次の順に進めます。

1. スキルに作業を頼みます。スキルは設定ファイルを作って止まり、まだ空の必須の項目を伝えます。
2. 設定ファイルの項目を埋めます。ファイルを直接書き換えるか、`setup` スキルで質問に答えて埋めます。
3. もう一度、同じ作業を頼みます。

- 項目の意味は、設定ファイルの中で、各項目のすぐ上に書いてあります。
- スキルは、それぞれのフォルダに `.gitignore`（中身は `*`）を作ります。設定とログ（パスワードを含むことがあります）は、Git に入りません。
- `setup` スキルは、自分の設定ファイルを持ちません。埋めるファイルを渡されないときは、作業フォルダの中から候補を探して示します。
- `setup` スキルの候補は、項目の説明のコメントがある YAML です。スキルのフォルダの中のひな形は、候補にしません。

## 許可の確認

スキルを使うと、Copilot CLI が許可を求めることがあります。

- スキルがコマンド（`python3 …`）を実行するときに、Copilot CLI が、実行してよいかを聞きます。
- SDK の場所やシリアルポートなど、作業フォルダの外のパスを使うときに、「Allow path access」の確認が出ることがあります。

## 同じ名前のスキルがあるとき

Copilot CLI は、次の場所のスキルを、このプラグインの同じ名前のスキル（`deploy`、`setup` など）より優先します。

- 個人のフォルダ（`~/.copilot/skills/`）
- プロジェクト（`.github/skills/`）

動きがおかしいときは、`/skills list` で確認してください。
