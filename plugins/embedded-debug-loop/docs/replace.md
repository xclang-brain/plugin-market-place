# 部品を変える（プラグインの設計変更）

このプラグインのスキルや作業エージェントを、足す・消す・置き換えるときの手順です。

## スキルを足す

例：役割「電源の入れ直し」のスキル `power-control` を足し、機器が応答しないときに使う。

1. `skills/power-control/` に、新しいスキルのフォルダを置きます。テストに要るパッケージがあれば、`skills/power-control/tests/requirements.txt` に書きます。
2. `com.github.copilot/agents/device-worker.agent.md` の「## 使うスキル」の表に、次の行を足します。

   ```text
   | 電源の入れ直し | `power-control` | 機器が応答しないとき |
   ```

   「使うとき」の書き方：試すたびに使うなら `試行`（表の上から順に使うので、使う順の位置に入れる）。設定のスキルなら `設定`。それ以外は、使う条件を書く。

## スキルを消す

例：`log-capture` を消す。

1. `skills/log-capture/` を消します。
2. `com.github.copilot/agents/device-worker.agent.md` の「## 使うスキル」の表から、`` | 機器のログ取得 | `log-capture` | 試行 | `` の行を消します。

## スキルを置き換える

例：`deploy` を、同じ役割の `flash` に置き換える。

1. `skills/deploy/` を消し、`skills/flash/` を置きます。
2. `com.github.copilot/agents/device-worker.agent.md` の「## 使うスキル」の表で、`` `deploy` `` を `` `flash` `` にします。

## 作業エージェントを置き換える

例：`device-worker` を `host-worker` に置き換える。

1. `com.github.copilot/agents/device-worker.agent.md` を消し、`com.github.copilot/agents/host-worker.agent.md` を置きます。
2. `com.github.copilot/agents/README.markdown` を、`host-worker` の README に替えます。
3. `com.github.copilot/extensions/embedded-debug-loop/defaults.json` の `"worker": "embedded-debug-loop:device-worker"` を `"worker": "embedded-debug-loop:host-worker"` にします。

## 新しいスキルを作るときの決まり

- 作業エージェントから「作業して」と頼まれたら作業し、「設定を確かめて」と頼まれたら設定を確かめる。どちらも、できたか・何が起きたか・ログの場所を返す。
- 要るファイルとテストは、スキルのフォルダの中に置く。ほかのスキルや作業エージェントの名前は書かない。

## 新しい作業エージェントを作るときの決まり

- 「## 使うスキル」の表（役割・スキル・使うとき）を持つ。スキルの名前は、この表にだけ書く。
- 統括エージェントから「これは準備の確認です」で始まる依頼が来たら、作業はせず、1行目に「準備：できている」か「準備：できていない」と書いて返す。
- 「あなたは作業役です」で始まる依頼が来たら、1回だけ試して、結果を返す。合否は決めない。
