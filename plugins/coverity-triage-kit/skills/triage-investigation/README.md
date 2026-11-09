# triage-investigation（警告の真偽の調べ方）

## 何をするか

Coverity の警告1件（または同じ原因の数件）が、本物のバグか誤検知かを調べるスキルです。
警告経路を「こう動けば問題が起きる」という仮説として読み、呼び出し元や定義までコードでたどって確かめます。

返すもの：

- 判定：本物のバグ（`true_bug`）／誤検知（`false_positive`）／意図的（`intentional`）／判定できない（`undetermined`）
- 確信度（高・中・低）とその理由
- 推奨：修正（`fix`）か逸脱（`deviation`）
- 見立て（40 字ほどの1行）、調べた経過、根拠の場所（リポジトリからのパスと行）
- リビジョンのずれ、最新のコードでもう直っているか

コードは読むだけで、直しません。Coverity にも書き込みません。

## 必要なもの

- 調べたいリポジトリのコード（読むだけ）
- 警告の情報：CID、チェッカー、ファイルと行。あれば警告経路（`issue-<CID>.json` か、チャットに貼った経路）
- 警告経路を見やすく出すスクリプトを使うときは [uv](https://docs.astral.sh/uv/)（Python と要るライブラリは uv が用意します）。コマンドを使えなくても、手順は最後まで行えます。
- Coverity から警告経路を取れる道具（MCP サーバーなど）があれば、警告経路を自動で取ります。なくても、聞かれた情報を渡せば使えます。

## 置き方

このフォルダだけを、次のどれかにコピーします（フォルダの名前は変えても使えます）。

- 作業フォルダの `.github/skills/triage-investigation/`（GitHub Copilot CLI）
- `~/.copilot/skills/triage-investigation/`（GitHub Copilot CLI、すべての作業フォルダ）
- 作業フォルダの `.claude/skills/triage-investigation/`（Claude Code）

## 呼び方

チャットで、警告の情報を添えて頼みます。AI がこのスキルを選びます（`/triage-investigation` でも呼べます）。

- 「CID 12345（NULL_RETURNS、src/foo.c の 120 行）の警告が本物か調べて」
- 「この FORWARD_NULL は誤検知？ 経路はこれ：…」
- 「issue-20001.json の警告を調べて、結果を out/investigation.json に書いて」

足りない情報は聞かれます。ほかのエージェントから、入力と出力の場所を書いた頼み（指示ファイル）で呼ばれたときは、聞かずに、示された場所と形で結果を書きます。

警告経路を見やすく出すスクリプトは、手で動かすこともできます（読むだけで、ファイルを作りません）。

```
# Windows（PowerShell）
uv run --native-tls --quiet "$HOME\.copilot\skills\triage-investigation\scripts\show_path.py" show --repo "C:\work\app" --issue-file "C:\work\issue-20001.json"
# Linux（bash）
uv run --native-tls --quiet "$HOME/.copilot/skills/triage-investigation/scripts/show_path.py" show --repo "$HOME/work/app" --issue-file "$HOME/work/issue-20001.json"
```

警告経路のファイルがないときは、`--file "src/foo.c" --line 120 --cid 12345 --checker NULL_RETURNS` で警告の場所だけを出せます。
経路のイベントは `--event "<ファイル>:<行>:<タグ>:<説明>"` を経路の順に並べ、★ の番号を `--main <番号>` で示します。
前後の行数は `--context`（既定 20）です。出すコードは `--max-lines`（既定 800 行。道具の出力の上限に収まる量）までで、超えるときは、すべてのイベントが見えるよう前後の行数を減らし、それでも超える分は切ります（どちらも `notes` に出ます）。くわしくは `--help` を見てください。

## 設定

設定のファイルがなくても動きます。調べたいリポジトリに次のファイルがあれば使います。

| ファイル | 使う項目 | 使い方 |
|---|---|---|
| `.coverity-triage/knowledge.md` | `## 調査で使う事実`、`## 推奨の方針（修正か逸脱か）` | 調べる前に読む。使った知識は「プロジェクトの知識：…」と書く。推奨の方針は、ふつうの推奨の決め方より優先する |
| `.coverity-triage/config.yaml` | `coverity.path_strip_prefixes` | スクリプトが、Coverity のパスから外す頭として使う（`--strip-prefix` でも渡せる） |

ほかの項目は読みません。

## 結果の見方

チャットでは、次の順に返ります。

1. 判定、見立て（決め手の1行）
2. 確信度とその理由（高：経路を最後までコードで追えた／中：一部が推定／低：コードから決められない、またはリビジョンのずれ）
3. 推奨とその理由
4. 根拠の場所（`src/foo.c:120` の形）と、調べた経過
5. リビジョンのずれ（なし／あり／分からない）、最新でもう直っているか

スクリプトの出力は JSON です。`text` が画面に出す文、`issues` が警告と経路、`notes` がパスの対応づけ、`drift_hints` が機械的に見つけたずれの手がかり（ファイルがない、行数が足りない、関数が見つからない）、`files` が各ファイルの文字コードと改行です。

## うまくいかないとき

| 起きたこと | 直し方 |
|---|---|
| スクリプトが `ok: false` で「コードを読むフォルダがありません」 | `--repo` にリポジトリの一番上のフォルダを渡す |
| 「パスをリポジトリの中のファイルに対応づけられませんでした」 | Coverity のパスの頭（ビルドした場所）を、`config.yaml` の `coverity.path_strip_prefixes` か `--strip-prefix` で渡す |
| 「パスの候補が複数あり、決められませんでした」 | 候補から正しいファイルを選び、`--file` と `--line` で渡す |
| 「警告経路は取れていません」 | 経路を取り直すか、`--file` と `--line` で警告の場所だけを出す |
| `uv` が見つからない | uv を入れる。入れられないときは、スクリプトなしで手順を進める（ファイルを直接読む） |
| 判定の確信度がいつも低い | 解析したリビジョンと手元のコードがずれていることが多い。Coverity が解析したリビジョンのコードで調べる |

## テストの動かし方

このフォルダで動かします（uv が要ります）。

```
# Windows（PowerShell）
py -3 -B -m unittest discover -s tests
# Linux（bash）
python3 -B -m unittest discover -s tests
```

- `tests/test_show_path.py`：スクリプトの振る舞い（一時フォルダのリポジトリで）
- `tests/test_skill_files.py`：SKILL.md・README・資料・試験の課題の形

実際のモデルで解かせる試験の課題は `evals/` にあります（skill-creator の形。スキルありとなしを比べて採点します）。
