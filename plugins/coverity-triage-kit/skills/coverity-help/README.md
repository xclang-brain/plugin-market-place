# coverity-help（困ったとき）

## 何をするか

Coverity Connect の警告のトリアージの道具一式について、使い方と困りごとに答えます。

- 使い方の全体（4つの段階、チャットでの頼み方、画面、承認と反映、統合と確かめ、オプション）
- 設定の項目と既定の値、条件ファイル、知識のファイル
- エラーの文の意味と直し方、実行が止まったとき
- 点検：道具（uv・git・svn・Coverity の解析のコマンド）、設定、認証情報の環境変数（値は出さない）、結果の置き場所、実行フォルダの一覧
- 結果と設定の画面を開き直す、Coverity との接続を確かめる

推測では答えず、資料（`references/`）と点検のスクリプト（`scripts/doctor.py`）で確かめてから答えます。設定と知識は、変える内容を見せて同意を得てから変えます。

頼み方の例：「このエラーは？」「HTTP 401 が出た」「実行が止まった」「MISRA だけ調べたい」「知識を追加したい」「画面を開いて」

## 必要なもの

- uv（点検のスクリプトを動かす。Python と要るライブラリは uv が用意する）
- 調べたいリポジトリ（`.coverity-triage/` の設定。なくても、ないことを点検で伝える）
- 画面を開く・接続を確かめる・実行の様子を詳しく見るときは、[SKILL.md](SKILL.md) の「使う部品」の表の部品

## 置き方

プラグインを入れると、一緒に入ります。フォルダだけを `.github/skills/coverity-help/` か `~/.copilot/skills/coverity-help/` にコピーしても使えます（資料と点検は、このフォルダだけで動きます）。

## 呼び方

ふつうの文で聞くと、AI がこのスキルを選びます。スラッシュコマンドでも呼べます（`/coverity-help このエラーは？`。プラグインの名前が前に付くこともあります）。

点検のスクリプトだけを動かすこともできます。

```text
uv run --native-tls --quiet "<このフォルダ>/scripts/doctor.py" check --repo "<リポジトリ>"
uv run --native-tls --quiet "<このフォルダ>/scripts/doctor.py" run --run "<実行フォルダ>"
```

PowerShell でも bash でも、同じ書き方で動きます。

## 設定

このスキルの設定はありません。読むのは、リポジトリの `.coverity-triage/`（`config.yaml`、`filters/*.yaml`、`knowledge.md`）と、結果の置き場所の実行フォルダの `run.json` です。

- 実行フォルダの `run.json` は、`assets/run.schema.json`（一括実行の形の写し）があれば、その形で確かめます。なければ、形の確かめを飛ばします（`schema_checked: false`）。

## 結果の見方

点検のスクリプトは、JSON を1つ返します。

| 項目 | 意味 |
| --- | --- |
| `ok` | 点検が動いたか。false なら `error` に理由 |
| `ready` | ng の項目がないか |
| `checks[]` | `check`（確かめたこと）、`status`（`ok` / `ng` / `warning` / `info`）、`detail`、`fix`（直し方） |
| `settings` | 既定の値を足した設定の今の値 |
| `runs[]` | 実行フォルダ（新しい順に10件）：`run_id`、`counts`（状態ごとの件数）、`has_summary`、`stopped`、`foreign`（別の形の実行フォルダ） |

認証情報は、あるかどうかだけを示し、値は `****` です。Windows では、ユーザーの環境変数（`HKCU\Environment`）も読みます。

## うまくいかないとき

| 症状 | 直し方 |
| --- | --- |
| `uv` が見つからない | uv を入れて、Copilot CLI を起動し直す |
| `"ok": false` と「フォルダがありません」 | `--repo` に、あるフォルダの絶対パスを渡す |
| 「実行フォルダではありません」 | `--run` に、`check` の `runs[].run_dir` を渡す |
| 日本語が化ける（Windows） | `PYTHONIOENCODING` を手で設定していれば消す（スクリプトはコンソールの文字コードで書く） |

## テストの動かし方

このフォルダで動かします（uv が要ります。ない環境では、スクリプトを動かす試験は飛ばします）。

```text
# Windows（PowerShell）
py -3 -m unittest discover -s tests -v
# Linux（bash）
python3 -m unittest discover -s tests -v
```

フォルダだけを別の場所にコピーしても動きます。実際のモデルで解かせる試験の課題は `evals/` にあります（skill-creator の形。AI クレジットを使います）。
