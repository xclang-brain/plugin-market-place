---
name: debug-dashboard
description: 統括エージェントへの依頼文づくり、進み具合、結果をブラウザーで見る Web管理画面を、起動・終了する。計画・作業・評価の進み具合を画面で見たいとき、画面から実行を止めたいときに使う。
disable-model-invocation: true
---

# debug-dashboard

Web管理画面のサーバーを、作業フォルダで起動・終了する。記録は、CLI のフックが作業フォルダの `.debug-dashboard/` に書く。

## コマンド

Python は、Windows では `py -3`、ほかでは `python3` で動かす（下の表は `python3` で書く）。
作業フォルダで動かすか、`--workdir <作業フォルダ>` を付ける。
利用者の頼みが終了なら `stop`、動いているかの確認なら `status`、それ以外なら `start` を動かす。

| すること | コマンド |
| --- | --- |
| 画面を起動する。もう動いていれば、その URL を返す | `python3 <このスキルのフォルダ>/scripts/dashboard.py start` |
| 画面を終了する。記録も止まる | `python3 <このスキルのフォルダ>/scripts/dashboard.py stop` |
| 動いているかを見る。何も作らない | `python3 <このスキルのフォルダ>/scripts/dashboard.py status` |

起動したら、利用者に次のことを伝える。

1. `url` をブラウザーで開く。
2. 「設定」のタブで依頼文を作り、統括エージェントを動かした別のチャットに送る。Claude Code では、別の端末の同じ作業フォルダで `claude --agent embedded-debug-kit:debug-orchestrator` を起動する。
3. 「実行を止める」は、次のツールの呼び出しから効く。すでに動いているコマンドは、終わるまで止まらない。

## 結果

標準出力に JSON を1つ返す。終了コードは、0 が成功、1 が起動や終了の失敗、2 が引数や作業フォルダの問題。

| 項目 | 中身 |
| --- | --- |
| `ok` | 成功したら `true` |
| `message` | 起きたことと、確かめること。利用者にはこれを伝える |
| `url` | 画面の URL。動いていなければ `null` |
| `running` | 画面が動いているか |
| `problems` | 引数や作業フォルダの問題の一覧（終了コード 2 のとき） |
| `config`、`log` | 設定ファイル（このスキルにはないので `null`）と、サーバーのログの場所 |

記録されるものと、記録されないときに確かめることは [reference/records.md](reference/records.md) にある。

## してはいけないこと

- `.debug-dashboard/` の中のファイルを、自分で書き換えたり消したりしない。起動と終了は、このスクリプトだけで行う。
- URL を、チャットのほか（ファイル、Issue など）に書かない。トークンが入っている。
- 頼まれていないのに、画面を終了しない。
