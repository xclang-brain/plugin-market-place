# 画面のツールがないとき

GitHub Copilot CLI で使っているときの資料です。

`coverity-triage-kit_open` がないのは、Web管理画面を動かすエクステンション（Copilot CLI が起動する、画面のための別のプログラム）が読み込まれていないためです。
原因は次の3つのどれかです。どれか分からないので、3つとも直し方を示します。

## 1. 試験的な機能が無効になっている

エクステンションは、Copilot CLI の試験的な機能です。

- まだ起動していないとき：Copilot CLI を `copilot --experimental` で起動する。一度付けると設定に残るので、次からは付けなくてよい。
- 起動しているとき：チャットで `/settings experimental on` と打つ。Copilot CLI が自動で起動し直して、有効になる（今の会話は終わる）。

どちらの場合も、起動の後に出る読み込みの確認は、2のとおり許可する（続けて2回出ることがある）。

## 2. 読み込みの確認を断った

Copilot CLI を起動し直して、チャットに出る確認を許可する。
確認の題は `Extension "plugin:coverity-triage-kit:coverity-triage-kit" wants elevated permissions` です。

確認の本文の英語の項目の意味：

| 項目 | 意味 |
| --- | --- |
| `skip tool permission prompts` | 画面を開くツールを、確認なしで動かす。ファイルやコマンドなど、外のものは変えない |
| `register hooks` | 画面で止めた実行のエージェントが、ツールを使えないようにする。統括エージェントが、決まったツールのほか（ファイルの書き込みなど）を使えないようにする |
| `handle permission requests` | エージェントの許可の求めを見て、画面に「チャットで確認を待っています」と出す。答えはしない（許可も拒否も、利用者がチャットで行う） |

- 断ると読み込まれず、画面で開始も監視もできない。
- 「Yes」は今回だけ許可する。Copilot CLI を起動するたびと `/clear` のたびに、また確認が出る。
- 「Yes, and always allow "plugin:coverity-triage-kit:coverity-triage-kit" in this directory」（作業フォルダが Git のリポジトリなら `in this repo`）は、このフォルダでは次から確認が出ない。

## 3. エクステンションを使わない設定になっている

`/extensions mode` が Disabled になっている。`/extensions mode` で Load & Augment か Load Only に戻す。Disabled は次のセッションにも残る。

## 伝えた後

- 直したら、もう一度チャットで頼むよう伝える。
- トリアージを代わりに始めない。
