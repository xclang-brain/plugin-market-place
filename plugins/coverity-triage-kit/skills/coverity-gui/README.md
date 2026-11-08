# 結果と設定の画面（coverity-gui）

Coverity の警告のトリアージの「結果と設定の画面」を、この PC のブラウザーで開くスキルです。

- **監視**：一括のトリアージの実行中に、AI の動き（役割ごとの枠の図）、作業ごとの段と経過時間、「⚠ 停止?」、重大なエラー、最近の動きを見る。
- **結果**：警告ごとに承認（修正・逸脱・却下・反映しない）を選び、逸脱コメントと Classification・Action・Severity を手直しする。詳細レポート・差分・修正後のファイルを見る。
- **設定**：`.coverity-triage/` の `config.yaml`・条件ファイル・`knowledge.md`・グループにしない CID を直す。Windows では認証情報も保存できる。

画面はリポジトリごとに 1 つのローカルのサーバ（127.0.0.1 だけ）で動き、開いたコマンドが終わっても動き続けます。「サーバを止める」を押すか、2 時間使われないと止まります。

## 必要なもの

- [uv](https://docs.astral.sh/uv/)（Python と PyYAML は uv が用意します）
- ブラウザー（画面を開く PC と同じ PC のもの）
- git か svn のリポジトリ（設定のタブでバージョン管理の種類を決めるため）

## 置き方

このフォルダだけを、次のどこかにコピーします（名前を変えてもかまいません）。

- 作業フォルダの `.github/skills/coverity-gui/`
- `~/.copilot/skills/coverity-gui/`
- Claude Code なら `.claude/skills/coverity-gui/`

## 呼び方

チャットで「結果の画面を開いて」「承認を選びたい」「設定を画面で直したい」「実行の様子を見せて」などと頼みます。

コマンドで直接開くこともできます（`<フォルダ>` はこのスキルのフォルダ）。

```text
# Windows（PowerShell）と Linux（bash）で同じ
uv run --native-tls --quiet "<フォルダ>/scripts/gui.py" open --repo "<リポジトリ>"
uv run --native-tls --quiet "<フォルダ>/scripts/gui.py" open --repo "<リポジトリ>" --run "<実行フォルダ>" --view summary
uv run --native-tls --quiet "<フォルダ>/scripts/gui.py" stop --repo "<リポジトリ>"
```

| コマンド | 引数 | 出力（JSON 1 つ） |
| --- | --- | --- |
| `open` | `--repo R [--run D] [--view monitor\|summary\|settings] [--item ID] [--no-browser]` | `{ok, url, reused, browser_opened, pid, note?}` |
| `stop` | `--repo R` | `{ok, stopped}` |
| `yaml-edit` | `--stdin`（`{"original": "<YAML>", "values": {…}}`） | `{ok, text, notice}`。注釈を残した書き換えの結果を返すだけで、ファイルは書かない（共通の試験のデータで確かめるため） |
| `serve` | `--repo R --ready-file F` | `open` が切り離して動かすサーバそのもの（直接は使わない） |

- `--run` には実行フォルダ（`run.json` があるフォルダ）か実行の ID（`20261108-101500` など）を渡します。`--run` があれば監視、`--item` があれば結果の詳細、どちらもなければ設定のタブが開きます。
- URL は `http://127.0.0.1:<ポート>/?t=<合言葉>#run=<実行の ID>&view=<タブ>&item=<ID>` の形です。
- 失敗は `{"ok": false, "error": "…", "problems": […]}` と終了コード 1 です。
- 出力の文字コード：環境変数 `PYTHONIOENCODING` があればそれ、Windows で出力をパイプで受けるときはコンソールの文字コード、ほかは UTF-8 です。

## 設定と、読み書きするファイル

| ファイル | 画面がすること |
| --- | --- |
| `.coverity-triage/config.yaml` | 読む・書く。変えた値の行だけを書き換え、注釈を残す。ファイルにない項目は、既定から変えたときだけ足す。知らない項目はそのまま残す |
| `.coverity-triage/filters/*.yaml` | 読む・作る・書く・消す（少なくとも 1 つ残す） |
| `.coverity-triage/knowledge.md`、`no-grouping.yaml` | 読む・書く |
| 実行フォルダ（`<output_dir>/<年月日-時分秒>/`） | `run.json`・`items/<ID>/`（`result.json`・`review.json`・`progress.json`・`saved.json`・`issue-<CID>.json`）・`operations.log`・`analysis/`・`jobs/`・`verify.json`・`apply-code.result.json`（反映したもの）・`summary.html`・`cid/`・`patches/`・`fixed/` を読む。書くのは `approvals.json` だけ |

- `run.json` の `format` が 2 でない実行（別の形で作られた実行）は、一覧に出しますが、見るだけです。
- `approvals.json` は `approvals.json.lock` を取ってから書き、ほかの作業の承認とほかの項目はそのまま残します。一覧と詳細レポートは書き直しません。
- `result.json` は `assets/result.schema.json`（結果の形の取り決めの写し）で確かめてから使います。
- 人に見せる確信度は、`scripts/confidence.py`（一括実行のスクリプトの写し）で決めます。作業役の確信度を、確かめの上限とビルドでの検証の問題で下げたものです（一覧と同じ答え）。
- 認証情報の環境変数（既定 `COV_USER` / `COV_AUTH_KEY`）は、Windows ではユーザーの環境変数（`HKCU\Environment`）も読みます。画面から保存できるのは Windows だけで、認証キーの値は画面に出しません。
- サーバの場所の記録：`~/.<このフォルダの名前>/`（環境変数 `GUI_STATE_DIR` で変えられます）。止まるまでの時間は環境変数 `GUI_IDLE_SECONDS`（既定 7200 秒）です。

## 結果の見方

画面の各タブの中身は [references/screens.md](references/screens.md) にあります。

## うまくいかないとき

| 起きたこと | 直し方 |
| --- | --- |
| 画面に「アドレスで開いてください」と出る | `open` をもう一度動かし、新しい URL を開く |
| 「画面のサーバが止まっています」 | 2 時間使われなかったか、止めた。`open` をもう一度動かす |
| 「画面のサーバを起動できませんでした」 | 出力の `log_tail` を見る。`uv --version` が動くか、ホームフォルダに書けるかを確かめる |
| 実行が一覧に出ない | `config.yaml` の `output_dir` の中の実行だけが出る。出力の `note` を見る |
| 承認を選べない | 統合か確かめを使う実行では、一覧（`summary.html`）ができてから選べる |
| 設定の保存で「注釈は動いたり消えたりします」 | `|`・`>`・`&`・`*` を使った書き方のファイル。注釈を残すなら、保存しないで手で直す |
| 認証情報を保存できない | Windows 以外では、環境変数を自分で設定する |

## テストの動かし方

このフォルダで動かします（uv が要ります。数十秒で終わります）。

```text
# Windows（PowerShell）
py -3 -m unittest discover -s tests
# Linux（bash）
python3 -m unittest discover -s tests
```

- `assets/contract/yaml-edit/cases.json`（注釈を残す書き換えの共通の試験のデータ）、`assets/contract/confidence/cases.json`（人に見せる確信度の共通の試験のデータ）、`assets/run.schema.json`、`assets/approvals.schema.json`（実行フォルダの取り決めの写し）があれば、それでも確かめます。ないときは、その試験を飛ばします。
- 試験の課題（モデルで解かせて採点するもの）は `evals/` にあります。
