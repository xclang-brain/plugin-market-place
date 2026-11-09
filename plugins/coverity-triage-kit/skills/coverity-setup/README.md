# Coverity トリアージの準備のスキル

Coverity Connect の警告をトリアージする前の準備を、チャットで1つずつ進めるスキルです。

## できること

| 渡すもの | できるもの |
| --- | --- |
| Coverity Connect の URL・プロジェクト・ストリーム（チームで最初の1人だけ） | 調べたいリポジトリの `.coverity-triage/`：`config.yaml`（項目ごとに日本語の注釈つき）、条件ファイル `filters/untriaged.yaml`・`filters/all.yaml`、知識のファイル `knowledge.md` |
| Coverity のユーザー名（PC ごと） | ユーザー名と認証キーを環境変数に保存するコマンド（認証キーはターミナルの伏せ字の欄で入れる。チャットには出さない） |
| 変えたい設定（例「確かめも使う」「max_items を 50 に」） | 人が書いた注釈を元の場所に残したままの `config.yaml` |
| ビルドのコマンド（使うときだけ） | ビルドでの検証・初回解析の設定と、リポジトリの外でのビルドの試し |

- 足りないものだけを補うので、何度使ってもかまいません。
- 設定ファイルを書く前に、書く値を表で見せて、そこで一度止まります。表に「はい」と返事をもらってから、次の返事で書きます（「表を見せたら書いてよい」と先に言ってあっても、表を見せたところで止まります）。
- git・svn・VS Code・Coverity の設定は変えません。

## 必要なもの

- Windows の PC（Linux・macOS でも動きます）
- git か svn のリポジトリ
- [uv](https://docs.astral.sh/uv/)（Python とライブラリを用意する道具）。なければ、このスキルが入れ方を示します。
- Coverity Connect のアカウントと認証キー

## 置き方

このフォルダを、そのまま次のどこかにコピーします（フォルダの名前を変えてもかまいません）。

- 作業するリポジトリの `.github/skills/<名前>/`
- 自分の `~/.copilot/skills/<名前>/`（Windows は `%USERPROFILE%\.copilot\skills\<名前>\`）
- Claude Code なら `.claude/skills/<名前>/`

プラグインに入れて配られているときは、コピーは要りません。

## 呼び方

チャットで頼みます。スキルの名前（フォルダの名前）を `/` に続けて打ってもかまいません。プラグインから入れたときは `/<プラグインの名前>:<スキルの名前>` でも呼べます。

- 「Coverity のトリアージの準備をして」
- 「認証キーを登録したい」
- 「config.yaml の max_items を 50 にして」
- 「ビルドでの検証も使いたい」

## 設定

作る設定は、調べたいリポジトリの `.coverity-triage/` にあります。チームで共有するため、コミットします。認証情報は入りません。
項目の一覧と既定の値は [references/options.md](references/options.md) にあります。

認証情報は環境変数に置きます（既定の名前は `COV_USER` と `COV_AUTH_KEY`。`config.yaml` の `coverity.user_env` / `coverity.key_env` で変えられます）。

- Windows：ユーザーの環境変数に保存します（PowerShell の伏せ字の欄で入れます）。
- Linux・macOS：ユーザー名は `~/.bashrc`（zsh なら `~/.zshrc`）に、認証キーは本人だけが読めるファイル `~/.cov_auth_key.env` に書き、シェルの設定ファイルから読み込みます。新しいターミナルを開いてから、Copilot CLI などを起動し直します。

## 結果の見方

スクリプトは、結果を JSON 1つで返します（`"ok": true` か、`"ok": false` と理由の `error`）。

- `check`：項目ごとに `ok` / `ng` / `warning`。`ng` のものを上から直します。環境変数は「あり」「なし」だけを示し、値は出しません。
- `set --check`：変わる項目（`changes`）と、注釈が残るか（`comments`: `kept` / `lost`）。`lost` のときは、保存の前に「このファイルでは、注釈は動いたり消えたりします」から始まる知らせ（`notice`）が返ります。
- `trial-build`：ビルドできたか（`build_ok`）と、失敗したときのログの最後（`log_tail`）。ログは結果を置くフォルダの `_trial-build/trial-build.log` にあります（前のフォルダを消せなかったときは `_trial-build-<日時>` を使い、そのことを `note` に書きます。場所は `log` に出ます）。

## うまくいかないとき

| 起きたこと | 直し方 |
| --- | --- |
| `uv` が見つからない | 入れた直後は、Windows なら `%USERPROFILE%\.local\bin\uv.exe` のフルパスで動かします。ターミナルを開き直しても直ります |
| 「git か svn のリポジトリの中ではありません」 | `.git` か `.svn` があるフォルダを指定します |
| 「結果を置くフォルダは、リポジトリの外にしてください」 | `output_dir` をリポジトリの外のフォルダにします（誤ってコミットしないため） |
| 認証キーが「なし」のまま | 保存の後に開いたターミナルから動かします。Windows では、ユーザーの環境変数も直接読みます |
| 認証キーをチャットに貼ってしまった | そのキーは使わず、Coverity Connect で作り直して、伏せ字の欄で入れ直します |
| 「このファイルでは、注釈は動いたり消えたりします」 | 何行かにわたる値（`\|` や `>` で始まる値）や、`&`・`*` の書き方があると、ファイル全体を書き直します。注釈を残したいときは保存しないで、メモ帳などで直します |

## スクリプトを直接動かす

```
# Windows（PowerShell）
uv run --native-tls --quiet "C:\path\to\this-skill\scripts\setup.py" check --repo "C:\work\app"
# Linux（bash）
uv run --native-tls --quiet "/path/to/this-skill/scripts/setup.py" check --repo "/home/me/work/app"
```

コマンドは `detect`・`init`・`check`・`set`・`set-verify`・`credentials`・`trial-build` です（`--help` で引数が出ます）。

## テストの動かし方

このフォルダで、次を動かします（uv と git が要ります。ないものの試験は飛ばします）。

```
# Windows（PowerShell）
py -3 -m unittest discover -s tests
# Linux（bash）
python3 -m unittest discover -s tests
```

`assets/contract/yaml-edit/cases.json` は、注釈を残した書き換えの共通の試験のデータです。同じ書き換えをするほかの部品も、このデータで同じ答えを出します。
