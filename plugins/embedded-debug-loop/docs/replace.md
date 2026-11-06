# 部品を変える（プラグインの設計変更）

このプラグインのスキルや作業エージェントを、足す・消す・置き換えるときの手順です。
仕様書・設計書・テスト・CI には部品の名前を書いていないので、直しません。

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

1. `com.github.copilot/agents/device-worker.agent.md` を消し、`com.github.copilot/agents/host-worker.agent.md` を置きます。「## 使うスキル」の表は、同じ形（役割・スキル・使うとき）にします。
2. `com.github.copilot/agents/README.markdown` の次の行を直します。
   - 1行目：`# 作業エージェント（device-worker）` → `# 作業エージェント（host-worker）`
   - 3行目：「作業エージェント（`device-worker`）は、…」→ `host-worker` の説明
   - 「## 始め方」の 2：`device-worker · plugin` → `host-worker · plugin`
   - 「## 使うスキル」：`device-worker.agent.md#使うスキル` → `host-worker.agent.md#使うスキル`
   - 「## うまくいかないとき」の1行目：`device-worker · plugin` → `host-worker · plugin`
3. `com.github.copilot/extensions/embedded-debug-loop/defaults.json` の `"worker": "embedded-debug-loop:device-worker"` を `"worker": "embedded-debug-loop:host-worker"` にします。

## 新しい部品が守ること

| 部品 | 守ること |
| --- | --- |
| スキル | 頼まれたら、役割の作業と設定の確認をし、成否・起きたこと・ログの場所を返す |
| スキル | 要るファイルとテストを、自分のフォルダに置く。ほかの部品の名前を書かない |
| 作業エージェント | 統括エージェントの依頼の書き出し（「これは準備の確認です」「あなたは作業役です」）と、返事の1行目（「準備：できている」か「準備：できていない」）を、この文字のまま使う |
| 作業エージェント | スキルの名前と、役割を2つ以上並べた文は、「## 使うスキル」の表にだけ書く。定義に自分の名前を書かない |
| 作業エージェント | セットアップでは、設定のスキルを読む前に、渡すスキルの名前をチャットに書く（「設定のスキルを読む前に、チャットに「<スキルの名前>…」の文を、定義に書く） |
