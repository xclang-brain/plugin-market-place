# coverity-triage-screen（画面の入口）

## 何をするか

GitHub Copilot CLI のチャットで「Coverity の警告をトリアージして」と頼むと、Web管理画面（ブラウザーの画面）を開き、URL と4つの注意を返します。

- 画面で条件（条件ファイル、Impact、件数、最初に解析するか）と、役割ごとのモデルを選び、「開始」を押すと、トリアージが始まります。
- このスキルは、トリアージを自分でしません。エージェントも呼びません。
- 実行が終わった知らせが来たときは、結果が画面にあることだけを伝えます（画面を開き直しません）。

頼み方の例：

- 「Coverity の警告をトリアージして」
- 「未処理の Coverity 警告を調べて修正案と逸脱コメントを作って」
- 「High の Coverity 警告だけまとめて調べて」（「High だけ」は画面の最初の値に入ります）

1件の警告（CID）だけを調べる頼み、画面を使わない一括実行、準備、反映は、このスキルの仕事ではありません。そのときに案内する部品は、[SKILL.md](SKILL.md) の「使う部品」の表にあります。

## 必要なもの

- GitHub Copilot CLI と、このスキルを含むプラグイン（Web管理画面は Copilot CLI だけの機能です。Claude Code にもこのスキルは読み込まれますが、画面は開かず、Claude Code での一括のトリアージの始め方を返します）
- プラグインのエクステンション（Web管理画面）が読み込まれていること。エクステンションは Copilot CLI の試験的な機能なので、`copilot --experimental` で一度起動し、読み込みの確認を許可します
- トリアージの準備（調べたいリポジトリの `.coverity-triage/` の設定と、認証情報の環境変数）。準備がないと、画面がそのことと準備のしかたを示します

## 置き方

プラグインを入れると、一緒に入ります。

フォルダだけを `.github/skills/coverity-triage-screen/` か `~/.copilot/skills/coverity-triage-screen/`（Claude Code では `.claude/skills/coverity-triage-screen/` か `~/.claude/skills/coverity-triage-screen/`）にコピーしても読み込まれますが、画面を開くツールはプラグインのエクステンションにあるので、プラグインがないと画面は開きません（そのときは、下の「うまくいかないとき」の案内を返します）。Claude Code では、コピーしたときも、画面は開かず、Claude Code での一括のトリアージの始め方を返します。

## 呼び方

チャットで、ふつうの文で頼みます。AI がこのスキルを選びます。スラッシュコマンドで呼ぶこともできます（`/coverity-triage-screen`。プラグインの名前が前に付くこともあります）。

## 設定

このスキルには、設定はありません。トリアージの条件は画面で選びます。

## 結果の見方

GitHub Copilot CLI では、チャットに、画面の URL と、次の4つの注意が出ます。

1. URL をブラウザーで開き、条件を選んで「開始」を押すと始まる（チャットで待っていても始まらない）
2. 実行中は `/clear` を使わない
3. 実行中にエージェントが確認を求めたら、このチャットで答える
4. 任せたいツールを前もって許可するしかたは、プラグインの README にある

結果（各エージェントの結果、検証の結果、一覧、課題と Issue の案）は、画面で見ます。

## うまくいかないとき

| 症状 | 原因と直し方 |
| --- | --- |
| Copilot CLI で、「エクステンションが読み込まれていません」と、3つの原因が示された | 試験的な機能が無効（`copilot --experimental` で起動するか、`/settings experimental on`）、読み込みの確認を断った（起動し直して許可する）、`/extensions mode` が Disabled（Load & Augment か Load Only に戻す）のどれか。詳しくは [references/when-tool-missing.md](references/when-tool-missing.md) |
| 「いまの実行が終わるまで、新しい作業は始められません」と出た | 実行中です。今の画面で様子を見るか、画面で止めてから、もう一度頼みます |
| Claude Code で頼んだら、「Web管理画面は GitHub Copilot CLI の機能です」と出た | Claude Code には画面のエクステンションがありません。一括のトリアージは、統括エージェントを `claude --agent` で起動して頼むか、一括実行のスキルをスラッシュコマンドで呼んで始めます（名前は [SKILL.md](SKILL.md) の「使う部品」の表） |
| 画面の URL が開けない | Copilot CLI を動かしているマシンのブラウザーで開きます。Copilot CLI を終えると、画面も終わります |

## テストの動かし方

モデルを使わない試験（このフォルダのファイルの形）：

```text
# Windows（PowerShell）
py -3 -m unittest discover -s tests -v
# Linux（bash）
python3 -m unittest discover -s tests -v
```

このフォルダで動かします。フォルダだけを別の場所にコピーしても動きます。

実際のモデルで解かせる試験の課題は `evals/` にあります（skill-creator の形）。AI クレジットを使います。
