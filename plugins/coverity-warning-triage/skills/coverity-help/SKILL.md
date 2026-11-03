---
name: coverity-help
description: Coverity トリアージの使い方（AI の役割と進め方、初回解析・統合・検証のオプション、画面での監視・結果の確認・承認を含む）、エラーや実行が止まったときの意味と対処、設定の変更、知識の追加、効果の集計について、利用者の質問に答える。画面を開き直すときにも使う。人が /coverity-help で聞く。引数は省略可で、聞きたいことを書く（例 このエラーは？ / MISRA だけ調べたい / 知識を追加したい）。
allowed-tools: ["coverity-triage", "shell(uv run:*)"]
---

# Coverity トリアージの質問への答え方

利用者はこのプラグインの仕組みを知りません。**一度に 1 つずつ**、短く、ふつうの言葉で答えます。（K-94）

| 知りたいこと | 資料 |
|---|---|
| 使い方（AI の役割と進め方、実行、初回解析、画面（監視・結果・設定）、一覧の読み方、統合と検証、承認、反映、再開、知識） | [references/usage.md](references/usage.md) |
| 設定の項目と変え方（初回解析、統合と検証のオプションを含む） | [references/settings.md](references/settings.md) |
| エラー、困ったとき、実行が止まったとき（初回解析の失敗、エージェントが見つからない、未検証、統合できなかった） | [references/troubleshooting.md](references/troubleshooting.md) |

コマンドを使うときは、スキル `coverity-triage-scripts` を読んで `ct.py` の使い方を確かめます。

## 守ること

- 分からないことを推測で答えない。資料を読み、`ct.py doctor`・`ct.py status`・`ct.py initial-status`（初回解析）・MCP の `check_connection` で確かめる。
- ターミナルでコマンドを実行する前に、何のためかを 1 行で伝える。（K-94）
- 認証キーをチャットで聞かない（入れ方はスキル `coverity-setup` の手順と同じ。ターミナルの伏せ字の欄で入れる）。
- 設定ファイル（`.coverity-triage/` の中）を変える前に、変える内容を見せて同意を得る。変えたら `ct.py doctor` で確かめる。
- 準備が足りないと分かったら、`/coverity-setup` を使うよう案内する。

## よくある頼まれごと

- **設定を変えたい**（例「MISRA だけ調べたい」「結果の置き場所を変えたい」）：[settings.md](references/settings.md) を見て、変える内容を見せて同意を得てから編集する。条件は、新しい条件ファイルを足すのがよい。
- **最初に解析してからトリアージしたい**（初回解析）：[usage.md](references/usage.md) の「初回解析」を短く伝える。いつも使うなら、`/coverity-setup` の「ビルドでの検証」で設定するよう案内する（試しのビルドをしてから保存できる）。その実行だけなら `/coverity-run 最初に解析して` でよい。
- **同じ原因の警告をまとめたい・結果を別の AI に確かめさせたい**（統合と検証）：[settings.md](references/settings.md) の `agents` を見て、`.coverity-triage/config.yaml` に足す内容を見せ、同意を得てから書く。既定は使わないこと、使うと時間が延びること、一覧ができるまで承認を選べないことも伝える。
- **AI がどう動いているか知りたい**：[usage.md](references/usage.md) の「AI の役割と進め方」で答える。チャットの AI は次にすることを自分で決めず、スクリプト（`ct.py tick`）が決めた順に、取得・作業・統合・検証の AI を動かすこと。
- **実行が止まった・初回解析が失敗した・「未検証」「統合はできませんでした」と出た**：[troubleshooting.md](references/troubleshooting.md) の「実行が止まったとき」で探す。
- **知識を足したい**（例「fatal_error は戻らないと覚えて」）：`.coverity-triage/knowledge.md` に足す 1 行を見せ、同意を得てから追記する。コミットして共有するよう伝える。
- **どれくらい役に立っているか**：設定 `options.metrics` が true なら `ct.py stats --repo <repo_root>` で、推奨がそのまま採用された割合、手直しの割合、1 件あたりの時間を伝える。false なら、オプションで記録できることを伝える。
- **画面を開きたい・閉じてしまった**：「画面を開きます」と伝えて `ct.py gui --repo <repo_root>` を実行する。実行の結果を見たいときは `--run <run_dir> --view summary`、AI の動きを見たいときは `--run <run_dir>` を足す（`run_dir` は `ct.py runs --repo <repo_root>` のいちばん新しいもの）。動いている画面があれば、それを使い回す。`browser_opened` が false なら `url` を伝える。
- **画面を止めたい**：画面の右上のメニューの「サーバを止める」を押してもらう（確認が出る）。止めても、実行フォルダのファイルも設定も消えない。また使うときは `ct.py gui` で開き直す。
- **エラーが出た**：[troubleshooting.md](references/troubleshooting.md) の表で探す。実行中のエラーは、画面の「監視」の「重大なエラー」に、内容と一緒に出ている（選ぶと詳しい内容が開く）ので、その表示を教えてもらうと早い。無ければ、実行フォルダの `operations.log`（認証情報は伏せてある）を添えて、プラグインの管理者に連絡するよう伝える。
