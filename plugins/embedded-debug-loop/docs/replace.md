# 部品を変える（プラグインの設計変更）

このプラグインのスキルや作業エージェントを、足す・消す・置き換えるときの手順です。

## スキルを足す

例：機器の電源を入れ直すスキル `power-control` を足す。

1. `skills/power-control/` に、新しいスキルのフォルダを置きます。テストに要るパッケージがあれば、`skills/power-control/tests/requirements.txt` に書きます。
   SKILL.md の description には、何をするかと、いつ使うか（例：機器が応答しないとき）を書きます。作業エージェントは、これを見て使うときを決めます。
2. `com.github.copilot/agents/device-worker.agent.md` の「## 使うスキル」の一覧に、`` `power-control` `` を足します。

## スキルを消す

例：`log-capture` を消す。

1. `skills/log-capture/` を消します。
2. `com.github.copilot/agents/device-worker.agent.md` の「## 使うスキル」の一覧から、`` `log-capture` `` を消します。

## スキルを置き換える

例：`deploy` を、同じ役割の `flash` に置き換える。

1. `skills/deploy/` を消し、`skills/flash/` を置きます。
2. `com.github.copilot/agents/device-worker.agent.md` の「## 使うスキル」の一覧で、`` `deploy` `` を `` `flash` `` にします。

## 作業エージェントを置き換える

例：`device-worker` を `host-worker` に置き換える。

1. `com.github.copilot/agents/device-worker.agent.md` を消し、`com.github.copilot/agents/host-worker.agent.md` を置きます。
2. `docs/guide/2-device-worker.md` を消し、`host-worker` の使い方を `docs/guide/2-host-worker.md` に書きます。プラグインの README の案内も替えます。
3. `com.github.copilot/extensions/embedded-debug-loop/defaults.json` の `"worker": "embedded-debug-loop:device-worker"` を `"worker": "embedded-debug-loop:host-worker"` にします。

新しい作業エージェントは、ほかのエージェントからの依頼では、頼みの文に書かれた手順と返事の形に従います（[仕様書](specs/requirements.md) の REQ-201〜REQ-208）。

## 変えた後に確かめる

プラグインのフォルダで、すべてのテストを動かします。独立性のテストが、部品の決まりを守っているかを確かめます。

```
# Windows（PowerShell）
py -3 tests/run.py
# Linux（bash）
python3 tests/run.py
```

新しいスキルが守ること（フォルダだけで完結する、README とテストを持つ など）は、[仕様書](specs/requirements.md) の REQ-108 と [設計書](design/design.md) の 3.4 にあります。
