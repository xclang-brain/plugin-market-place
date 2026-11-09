# plugin-market-place

[plugin-creator](https://github.com/xclang-brain/plugin-creator) で作った GitHub Copilot CLI のプラグインを集めたマーケットプレイスです。

## 入れ方

マーケットプレイスを登録してから、使いたいプラグインを入れます。

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install <プラグインの名前>@plugin-market-place
```

- 入っているプラグインの一覧は、`copilot plugin marketplace browse plugin-market-place` でも見られます。
- プラグインを使う前に要る準備（試験的機能を有効にする、など）は、それぞれのプラグインの README に書いてあります。

## プラグインの一覧

| 名前 | できること | 版 |
| --- | --- | --- |
| [weekly-sales-report](plugins/weekly-sales-report/) | 毎週の売上データ（sales.csv）から、部長に出す週報 weekly_report.md を作る | 0.1.0 |
| [embedded-debug-loop](plugins/embedded-debug-loop/) | 組み込み機器の C/C++ の不具合を、スキルを使って機器で試すことを繰り返して調べ、直す。スキルだけ、作業エージェントだけ、統括エージェントと Web管理画面つき、の3つの段階で使える | 0.4.0 |
| [coverity-warning-triage](plugins/coverity-warning-triage/) | Coverity Connect の警告をトリアージし、警告ごとに修正案と逸脱コメント案の両方を作る（C / C++、標準・MISRA・CERT）。既存のプラグイン coverity-triage を plugin-creator で作り直したもの | 0.1.0 |
| [coverity-triage-kit](plugins/coverity-triage-kit/) | Coverity Connect の警告をトリアージし、警告ごとに修正案と逸脱コメント案の両方を作る（C / C++、標準・MISRA・CERT）。スキルや MCP サーバー1つ、作業エージェント1つ、一括のトリアージ、Web管理画面つき、の4つの段階で使える。Claude Code でも使える（Web管理画面のほかの1〜3段階目） | 0.2.0 |
| [ng-repro-pick](plugins/ng-repro-pick/) | 試験部門から毎週届く試験結果の Excel と試験仕様書の Word から、最初に実機の台で再現させる NG を1つ選び、pick.md のメモにする | 0.1.0 |
| [handoff-compare-report](plugins/handoff-compare-report/) | 病棟の申し送りの3つの形（AI なし／AI が全体を下書き／事実の欄だけ AI）の机上の比較で、形ごとに漏れや時間を数え、結論つきの比較報告 compare_report.md を作る | 0.1.0 |

weekly-sales-report は、plugin-creator の試験で作った見本です。AI が演じたユーザー（営業チームのリーダー）と plugin-creator が会話して作りました。中に出てくる人の名前や売上の数字は、架空のものです。

coverity-warning-triage は、plugin-creator の作り直しの試験で作りました。元のプラグイン coverity-triage（[coverity-triage-agent](https://github.com/xclang-brain/coverity-triage-agent)、MIT License）の部品を流用しています。plugin-creator の問いには、利用者の代わりに AI（Claude）が、元のプラグインの資料をもとに答えました。

coverity-triage-kit は、coverity-warning-triage を、embedded-debug-loop の作り方にならって作り直したものです。利用者から見た使い方は同じにし、部品（スキル、MCP サーバー、エージェント、Web管理画面）を、スキル1つ・作業エージェント1つ・一括のトリアージ・Web管理画面つきの4つの段階で、それぞれ取り出して使えるようにしました。スキルの名前が同じなので、coverity-warning-triage とは、どちらか一方だけを入れてください。

coverity-triage-kit は、Claude Code でも使えます（Web管理画面の4段階目は Copilot CLI だけ）。Claude Code では、`claude plugin marketplace add xclang-brain/plugin-market-place` の後に `claude plugin install coverity-triage-kit@plugin-market-place` で入れます。Claude Code の一覧（`.claude-plugin/marketplace.json`）には、このプラグインだけが載っています。くわしくは、プラグインの README の「Claude Code で使う」にあります。

ng-repro-pick は、plugin-creator のアイデア創出スキル（make-idea）で作ったアイデアを題材にした、plugin-creator の試験で作りました。AI が演じたユーザー（組み込みファームウェアの担当者）と plugin-creator が会話して作りました。中に出てくる人の名前、試験の項目、数は、架空のものです。

handoff-compare-report も、make-idea で作ったアイデア（AI の申し送りを入れる前に、AI なしの良い形と並べて3つの形を比べる）を題材にした、plugin-creator の試験で作りました。AI が演じたユーザー（病院の看護の質の担当者）と plugin-creator が会話して作りました。中に出てくる患者、病棟、数は、架空のものです。医療の判断に使うものではありません。

## プラグインを足すとき

1. プラグインのフォルダを `plugins/<プラグインの名前>/` に置きます。
2. `.github/plugin/marketplace.json` の `plugins` に、名前・説明・版と、`"source": "./plugins/<プラグインの名前>"` を足します。
3. 上の一覧に1行足します。
