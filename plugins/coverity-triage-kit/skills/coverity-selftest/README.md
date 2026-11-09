# coverity-selftest（自己診断）

## 何をするか

Coverity の警告のトリアージの道具一式が、この PC で動くかを確かめ、結果を伏せ字の `report.md` にまとめます。

| 範囲 | 確かめること |
| --- | --- |
| ① 道具とエージェント | uv と git、Coverity との通信のツール、作業役・検証役・統合役・統括のエージェント（一覧にあり、説明に役割の言葉があるか）、スラッシュコマンド、Web管理画面のツール |
| ② 偽データでの一連の流れ | 見本のリポジトリ（`assets/sample-target`）と偽の Coverity で、一括のトリアージを最後まで流し、間違った確認の文字列 `000000000000` が断られること、反映（逸脱の書き戻しと修正のブランチ）、結果が想定（`assets/expected.yaml`）と合うことを確かめる |
| ③ 社内の Coverity（読むだけ） | 接続と認証、1件の検索、1件の警告経路。**書き込まない** |
| ④ ビルドでの検証 | 設定のビルドのコマンドが、リポジトリのコピーで通るか。Coverity の解析のコマンドがあるか（登録は試さない） |

頼み方の例：「自己診断して」「②だけ確かめて」「社内の Coverity につながるか確かめて」

## 必要なもの

- uv（スクリプトを動かす）と git（見本のリポジトリ）
- ①②：[SKILL.md](SKILL.md) の「使う部品」の表の部品（Coverity との通信、一括実行、反映、エージェント）。ないものは、その項目が失敗として記録される
- ③④：準備の済んだ、調べたいリポジトリ（`.coverity-triage/` の設定と、認証情報の環境変数）

## 置き方

プラグインを入れると、一緒に入ります。フォルダだけを `.github/skills/coverity-selftest/` か `~/.copilot/skills/coverity-selftest/`（Claude Code なら `.claude/skills/coverity-selftest/` か `~/.claude/skills/coverity-selftest/`）にコピーしても、スクリプトと見本はこのフォルダだけで動きます（確かめる相手の部品は、別に要ります）。

## 呼び方

ふつうの文で頼むと、AI がこのスキルを選びます。スラッシュコマンドでも呼べます（`/coverity-selftest ②`。プラグインの名前が前に付くこともあります）。範囲を書かなければ ①〜④ すべてです。③④ は、調べたいリポジトリを開いているときだけ行います。

スクリプトだけを動かすこともできます（PowerShell でも bash でも同じ書き方）。

```text
uv run --native-tls --quiet "<このフォルダ>/scripts/selftest.py" start --sections 1,2 --client "Copilot CLI"
uv run --native-tls --quiet "<このフォルダ>/scripts/selftest.py" report --dir "<start が返した dir>"
```

コマンドの一覧は [references/checks.md](references/checks.md) にあります。

## 設定

このスキルの設定はありません。

- 見本の設定（`assets/sample-target/.coverity-triage/config.yaml`）は、偽の Coverity（`api: fake`、`fake-issues.yaml`）を使い、結果の確かめ（検証役）を動かします。
- ③④ は、調べたいリポジトリの `.coverity-triage/config.yaml` を読みます（変えません）。④ は `verify.setup_command`・`verify.build_dir`・`verify.build_command`・`verify.timeout_minutes` を使います。
- `assets/run.schema.json`・`assets/approvals.schema.json`（一括実行の形の写し）があれば、② で実行フォルダの `run.json` と `approvals.json` の形も確かめます。

## 結果の見方

結果のフォルダは、ホームの下の `coverity-selftest-results/<日時>/` です（フォルダの名前は、このスキルのフォルダの名前から決まります）。

- `report.md`：範囲ごとの表（項目・確かめること・結果・見たこと）と、失敗・要確認の詳しいこと。結果は 成功・失敗・要確認・未実施・記録。
- `selftest.json`：記録そのもの。
- ② の見本のリポジトリと実行フォルダも、このフォルダの中にできます（消してかまいません）。

伏せ字：認証キー、ユーザー名、ホスト名、IP アドレス、メールアドレスは `****`、ホームのフォルダは `~` になります。社内のソースコードは記録しません（④ のビルドの失敗も、ソースを引用する行は除いて、最後の数行だけ）。ストリーム名やファイルのパスは残るので、確かめてから共有してください。

## うまくいかないとき

| 症状 | 直し方 |
| --- | --- |
| 1-1・1-3〜1-6 が失敗 | プラグインが入っているか、Copilot CLI（か Claude Code）を起動し直したか確かめる。`/mcp` と `/agent`（Claude Code では `/agents`）の一覧を見る |
| 1-8 が未実施（Claude Code） | Web管理画面は Copilot CLI だけの機能なので、Claude Code では確かめません |
| 1-7 が失敗 | uv か git を入れて PATH を通し、Copilot CLI（か Claude Code）を起動し直す |
| 2-2〜2-8 が失敗 | 一括実行か反映が途中で止まった。`report.md` の詳しいことと、見本の実行フォルダ（`coverity-triage-out/`）を見る |
| 2-2〜2-6 が失敗（Claude Code） | 裏で動くエージェントに許可がなかった。一括実行が示す許可を `/add-dir` と `/permissions` で与えてから、② をやり直す |
| 2-7 が要確認 | 書き戻すツールの確認を断ったので、確かめられなかった。② をやり直し、その確認を、その1回だけ許可する（間違った確認の文字列なので、書き込まれない） |
| ② が範囲から外れた（Claude Code） | `--agent` で統括のエージェントを主役にして起動したセッションには、書き戻しのツールがない。`--agent` を付けずに Claude Code を起動して、② を確かめる |
| 2-4・2-5 が要確認 | AI の結論が想定と違った。結論は実行ごとに揺れるので、失敗ではない。詳細レポートで理由を見る |
| 3-1 が失敗 | 設定・条件ファイル・認証情報の環境変数のどれかがない。準備のスキルで直す |
| 3-2 が失敗 | URL、認証キー、証明書、プロキシを確かめる |
| 4-1 が失敗 | ふだんの方法でビルドできるか、`verify.setup_command` と `verify.build_command` を確かめる。コピーは最新のコミットなので、コミットしていない変更は入らない |
| 「自己診断の結果のフォルダではありません」 | `--dir` に、`start` が返した `dir` を渡す |

## テストの動かし方

このフォルダで動かします（uv と git が要ります。ない環境では、スクリプトを動かす試験は飛ばします）。

```text
# Windows（PowerShell）
py -3 -m unittest discover -s tests -v
# Linux（bash）
python3 -m unittest discover -s tests -v
```

フォルダだけを別の場所にコピーしても動きます。一括実行そのものは動かさず、一括実行が残す形の実行フォルダを試験の中で作って確かめます。実際のモデルで解かせる試験の課題は `evals/` にあります（skill-creator の形。AI クレジットを使います）。
