# 部品を変える（プラグインの設計変更）

このプラグインのスキルや作業エージェントを、足す・消す・置き換えるときの手順です。

## スキルを足す

例：役割「電源の入れ直し」のスキル `power-control` を足し、機器が応答しないときに使う。

1. `skills/power-control/` に、新しいスキルのフォルダを置きます。テストに要るパッケージがあれば、`skills/power-control/tests/requirements.txt` に書きます。
2. `com.github.copilot/agents/device-worker.agent.md` の「## 使うスキル」の表に、次の行を足します。

   ```text
   | 電源の入れ直し | `power-control` | 機器が応答しないとき |
   ```

   「使うとき」の書き方：試すたびに使うなら `試行`（表の上から順に使うので、使う順の位置に入れる）。それ以外は、使う条件を書く。

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

新しい作業エージェントは、ほかのエージェントからの依頼（ask_user が使えないとき）では、質問も合否の判断もせず、頼みの文が求める形で返します（[仕様書](specs/requirements.md) の REQ-201〜REQ-208）。

## 変えた後に確かめる

プラグインのフォルダで、すべてのテストを動かします。独立性のテストが、部品の決まりを守っているかを確かめます。

```
# Windows（PowerShell）
py -3 tests/run.py
# Linux（bash）
python3 tests/run.py
```

新しいスキルが守ること（フォルダだけで完結する、README とテストを持つ など）は、[仕様書](specs/requirements.md) の REQ-108 と [設計書](design/design.md) の 3.4 にあります。
