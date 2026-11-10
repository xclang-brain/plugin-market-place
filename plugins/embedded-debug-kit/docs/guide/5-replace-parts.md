# 部品を交換する

スキル（1段階目）や作業エージェント（2段階目）を、自分で作ったものに替える手順です。
たとえば、機器に FTP がなく SSH だけのときに、ftp-deploy を、scp で送るスキルに替えます。

- 読む人は、プラグインのファイルを直せる開発者です。GitHub Copilot CLI で使う場合を書きます。
- 交換は、プラグインを複製して行います。
- 直すのは、スキルなら1つのファイルの2か所、作業エージェントなら1つのファイルの6か所です。

## 1. プラグインを複製する

1. このリポジトリを clone します（fork でもかまいません）。
2. マーケットプレイスからインストールしてあれば、アンインストールします。

   ```shell
   copilot plugin uninstall embedded-debug-kit
   ```

3. これからは、複製したフォルダを指定して CLI を起動します。

   ```shell
   copilot --plugin-dir <複製した場所>/plugins/embedded-debug-kit
   ```

この後に出てくるファイルは、`plugins/embedded-debug-kit/` の中にあります。

## 2. スキルを交換する

ftp-deploy を、scp で送る scp-deploy に替える例です。

1. `skills/scp-deploy/SKILL.md` を、次の中身で作ります。コマンドは、自分の機器に合わせます。まずは、このファイルだけで動きます。

   ```markdown
   ---
   name: scp-deploy
   description: ビルドしたプログラムを、scp で組み込み機器のフォルダに送る。プログラムを機器に送る（転送・デプロイ）ときに使う。
   ---

   # scp-deploy

   1. 作業フォルダで `scp build/my_app root@192.168.1.50:/opt/my_app/` を実行する。
   2. コマンドの出力を、`<作業フォルダ>/.scp-deploy/logs/` の中のファイルに保存する。フォルダがなければ作り、`.scp-deploy/` に中身が `*` の `.gitignore` も置く。
   3. 成功したかと、ログの場所（絶対パス）を伝える。

   ## してはいけないこと

   - 失敗したときに、何も変えずに同じコマンドを繰り返さない。
   ```

2. 古いスキルのフォルダ `skills/ftp-deploy/` を消します。
3. `com.github.copilot/agents/device-debugger.agent.md` を開き、`ftp-deploy` を `scp-deploy` に置き換えます（2か所）。
4. CLI を起動し直し、`/skills` に scp-deploy が出ることを確かめます。

ほかのスキルは、直しません。

新しいスキルを書くときは、次の3つを守ります。

| 守ること | 守らないと起きること |
| --- | --- |
| description に、何をするかと、いつ使うかを書く | 作業エージェントが、このスキルを選ばない |
| 出力をファイルに残し、その場所を伝える | 3段階目で、判定が「判断できない」になる |
| 役割（ビルド、機器に送る、機器で動かす、設定）を変えない | 作業エージェントの進め方と合わなくなる |

設定ファイルを持たせて skill-config で設定できるようにするときは、既存のスキルのフォルダを手本にします。決まりは、[skill-config の使い方](1-skill-config.md#設定ファイルの決まり)にあります。

## 3. 作業エージェントを交換する

device-debugger を、自分で作った board-tester に替える例です。

1. `com.github.copilot/agents/device-debugger.agent.md` を、同じフォルダに `board-tester.agent.md` という名前でコピーします。
2. コピーしたファイルの `name:` を `board-tester` にし、本文を書き換えます。
3. 2段階目だけで使うなら、ここで終わりです。次のように起動します。

   ```shell
   copilot --plugin-dir <複製した場所>/plugins/embedded-debug-kit --agent embedded-debug-kit:board-tester
   ```

4. 統括エージェント（3段階目）からも使うなら、`com.github.copilot/agents/debug-orchestrator.agent.md` を開き、`device-debugger` をすべて `board-tester` に置き換えます（6か所）。エディターの「すべて置換」を使います。
5. 統括エージェントを起動して依頼し、作業エージェントへの依頼が断られないことを確かめます。

手順 4 の6か所のうち4か所は、依頼の相手を絞るコード（フック）の中にあります。1つでも古い名前が残ると、統括エージェントの依頼が「依頼できるのは…だけです」と断られます。

Web管理画面（4段階目）は、直しません。

本文を書き換えるときは、次の2つを守ります。

- 「ほかのエージェントから依頼されたとき」の節を残す。統括エージェントは、作業エージェントがこの節のとおりに動く前提で依頼する。
- 先頭に `disable-model-invocation: true` を書かない。書くと、統括エージェントが依頼できない。

## 補足

- 文書と通しのテストには、古い名前が残ります。動きには影響しません。残る場所は、[設計書](../design/design.md)の 1.1 にあります。
- Claude Code でも使うなら、`com.anthropic.claude-code/agents/` の同じ名前のファイルにも、同じ変更をします。
- 検証エージェント（debug-verifier）を交換する手順は、ありません。
- 確かめた範囲は、次のとおりです。Copilot CLI で AI を動かしての確認は、まだです。
  - スキルの交換：上と同じ形のスキルに替え、Claude Code で不具合を直せた。
  - 作業エージェントの交換：替えた後に、テスト（`tests/independence`）が通った。

## してはいけないこと

- Copilot CLI がプラグインをインストールしたフォルダを、直接書き換えないでください。プラグインを更新したときに、変更が消えるおそれがあります。
- 検証エージェントの名前（debug-verifier）を変えないでください。Web管理画面が、この名前で「評価」のフェーズを見分けます。
