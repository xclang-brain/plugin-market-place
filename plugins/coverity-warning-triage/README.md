# coverity-warning-triage

Coverity Connect の警告をトリアージし、警告ごとに修正案と逸脱コメント案の両方を作るプラグイン（C / C++、標準・MISRA・CERT）。作業の画面で条件を選び、進み具合と結果を見られる。

## できること

Coverity Connect の警告（C / C++、標準・MISRA・CERT）を渡すと、警告ごとに、本物のバグかどうかの見立て、確信度、修正案と逸脱コメント案ができあがります。

Coverity の警告のトリアージ（本物のバグか、修正するか、逸脱として登録するかの判断）を、人が1件ずつ行う代わりに AI が下調べし、人の作業時間を減らす。人は一覧を見て、修正か逸脱かを決めるだけにする。

できたものは、一覧（summary.html）、警告ごとの詳細レポート、差分、修正後のファイル、承認の一覧で、結果のフォルダにまとまります。

coverity-warning-triage は、GitHub Copilot CLI のプラグインです。作業の設定・開始・監視・結果の確認は、ブラウザーの画面（Web管理画面）で行います。作業は、統括・作業・検証の3種類のエージェントが、計画 → 実行 → 評価 を繰り返して進めます。

## 使い方

初めて使う前に、下の「使うために要る準備」を1回行います。

### チャットで頼むときの言葉

チャットでは、次のように頼みます。

- 「Coverity の警告をトリアージして」と頼むと、作業の画面が開きます。
- 「/coverity-run」と入力し、条件を添えることもできます（例「misra.yaml」「High だけ」「10 件だけ」「最初に解析して」「解析はしないで」）。/coverity-run と頼むと、条件で実行フォルダを作り、監視の画面を開き、ct.py tick に従って取得・作業・統合・検証のサブエージェントを動かし、最後に summary.html と AI の推奨入りの approvals.json を作って、画面の「結果」へ案内する。
- 「/coverity-setup」と入力すると、対話か画面かを選んで、uv・MCP サーバの環境・サブエージェント4つの配置・設定ファイル・認証情報・接続の確認・オプションのうち、足りないものだけを補う。
- 「/coverity-apply」と入力すると、承認（approvals.json）を ct.py preview で確かめ、件数（修正・逸脱・却下）と ID を見せて同意を得てから反映する。逸脱は Coverity に書き戻し（update_triage）、修正は git ならブランチを push してプルリクエスト（作れなければ compare_url を示す）、svn ならワーキングコピーに差分を当てる。
- 「/coverity-help」と入力すると、資料と doctor / status / initial-status / check_connection で確かめて答える。設定の変更や知識の追記は同意を得てから行う。
- 「/coverity-selftest」と入力すると、①組み込み ②偽データでの一連の流れ ③社内の Coverity（読み取りだけ） ④ビルドでの検証 を確かめ、伏せ字にした report.md にまとめる。

### 用意するもの

- 対象は、たまった大量の未処理の警告と、リリース前などの節目にまとめて出る警告。使う人は複数のチーム・部署の開発者で、各自が自分の Windows PC に入れて使う。
- 準備で人に聞かれるのは、Coverity の URL・プロジェクト名・ストリーム名（チームで最初の人だけ）と、ユーザ名と認証キーだけ。
- 認証情報は環境変数 COV_USER / COV_AUTH_KEY に置き、設定ファイルには書かない。認証キーをチャットで聞かず、ターミナルの伏せ字の欄で入れてもらう。チャットに貼られたら使わず、作り直しを頼む。ログと記録では認証情報を **** に伏せる。
- 設定は対象リポジトリの .coverity-triage/（config.yaml、filters/*.yaml、knowledge.md、no-grouping.yaml）に置き、コミットしてチームで共有する。
- 警告は条件ファイル（.coverity-triage/filters/*.yaml）で絞る。項目は name / project / streams（ストリームは1つだけ）/ checkers（*?[ のワイルドカード可）/ impacts / triage.classification・action・status / limit。同じ項目の中の値は「どれか」、項目どうしは「すべて」。チャットで条件を足せる。
- 条件ファイルはリポジトリごとに違い、.coverity-triage/filters/ に足して増やせる（misra.yaml は例で、どのリポジトリにもあるとは限らない）。決まった一覧にない条件ファイルも使える。
- 条件を指定しないときの既定は「分類がまだの警告を 20 件まで」（条件ファイル untriaged.yaml、classification Unclassified、limit 20）。1回の実行の上限は max_items（既定 100）。
- Impact の値は High / Medium / Low / Unspecified。Impact で絞るときは、Unspecified も選べる。

### 画面で決めること、見ること

作業の画面（ブラウザーの画面）で、条件ファイル、Impact で絞るか、調べる件数の上限、最初に解析するかを、開始の前に決めます。作業の画面から始めたときに、前の実行が途中で止まっていても、聞かずに、画面で決めた条件で新しく始めます。途中の実行は残り、チャットの `/coverity-run` で続きから再開できます。

- 実行中は、ブラウザの画面（ct.py gui、127.0.0.1）の「監視」で AI の動きと重大なエラーを見られます。「結果」で承認の選択・逸脱コメントの手直し・差分の確認をし、「設定」で設定・条件・知識を編集できます。
- 毎回の流れは次のとおりです。1. 調べたいリポジトリを開き、/coverity-run（例 /coverity-run High だけ）と入力する。2. ブラウザの画面の「監視」で AI の動きと重大なエラーを見る（最初に1回だけ修正用のフォルダの読み書きを許可し、始めたら終わるまで質問されない）。3. AI が警告を1件ずつ調べ、一覧と詳細レポートができる。4. 画面の「結果」で承認欄を見て、変えたい行だけ「修正」「逸脱」「却下」「反映しない」から選び直す（詳細レポートを開き、逸脱コメントを直したり差分と修正後のファイルを見たりする）。5. /coverity-apply で件数を確かめて同意し、逸脱は Coverity に書き込まれ、修正はプルリクエストになる（svn は作業中のフォルダに入り、人が確かめてコミットする）。
- オプション（1つのブランチにまとめる、Markdown も出す、知識の追記の提案、効果測定、アノテーション）は、設定で有効にしたときだけ動く。既定はすべて無効。

### できたものの場所

- 結果を置く場所（output_dir）はリポジトリの外にする（既定 ../coverity-triage-out）。中なら実行を始めない。
- 実行フォルダ `<output_dir>/<YYYYmmdd-HHMMSS>/` の中に、一覧 summary.html（Markdown の設定のときは summary.md も）、詳細 cid/<ID>.html（と .md）、差分 patches/<ID>-fix.patch（git 形式）、修正後のファイル fixed/<ID>/fix/<リポジトリ内のパス>、approvals.json などができる。修正後のファイルと差分は警告ごとに独立したコピーで作り、ほかの修正を混ぜない。

頼んだ後は、ブラウザーの画面で進めます。

1. チャットに、Web管理画面の URL が示されます。URL には合言葉が入っています。ブラウザーで開きます。この URL は、coverity-warning-triage を動かしているマシンのブラウザーからだけ開けます。
2. 画面で、役割（統括・作業・検証）ごとに使うモデル、エージェントごとの思考の深さ、繰り返しの上限の回数、この作業だけの設定を決めて、開始ボタンを押します。Copilot CLI のモデルが Auto のときは、役割ごとのモデルが効かないことがあります。そのことと、実際に動いたモデルが画面に示されます。
3. 実行中は、画面で、各エージェントの状態と担当、今が計画・実行・評価のどれか、エージェント間のデータの受け渡しを見られます。止めるときは、止めるボタンを押します。
4. 終わったら、画面で、各エージェントの実行結果、検証結果、まとめ、終わった理由、課題の一覧を見られます。

## 使うために要る準備

次の4つを、最初に1回行います。

### 1. インストールする

マーケットプレイス plugin-market-place を登録してから、プラグインを入れます。

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install coverity-warning-triage@plugin-market-place
```

- Copilot CLI の中では、`/plugin marketplace add xclang-brain/plugin-market-place` と `/plugin install coverity-warning-triage@plugin-market-place` でも入れられます。
- 元のプラグイン coverity-triage も入れているときは、どちらか一方だけにします。どちらも同じ名前のサブエージェント（`coverity-triage-worker` など）を Copilot の設定のフォルダに置くためです。

### 2. 試験的機能を有効にする

画面を動かす仕組み（エクステンション。Copilot CLI が起動する別のプログラム）は、試験的な機能です（Copilot CLI 1.0.102 の時点）。有効にしないと、coverity-warning-triage は使えません。試験的な機能なので、Copilot CLI の更新で使い方が変わることがあります。

初めて使うときは、`--experimental` を付けて Copilot CLI を起動します。一度付けると設定に残るので、次からは付けなくても有効です。

```shell
copilot --experimental
```

すでに起動しているときは、チャットで次のように打ちます。Copilot CLI が自動で再起動して、有効になります。再起動すると、今の会話は終わります。再起動の後は、まだ信頼していないフォルダでは、フォルダを信頼するかの確認が出て、続けて次の「3. 読み込むときの確認を許可する」の確認が出ます（読み込みの確認は、続けて2回出ることがあります）。

```text
/settings experimental on
```

### 3. 読み込むときの確認を許可する

coverity-warning-triage を読み込むとき、Copilot CLI がチャットに確認を出します。題は `Extension "plugin:coverity-warning-triage:coverity-warning-triage" wants elevated permissions` で、本文は次のとおりです。

```text
This extension wants to: skip tool permission prompts, register hooks, handle permission requests.
Denying will prevent this extension from loading.
```

Copilot CLI 1.0.101 から、この確認は必ずあなたに出ます。許可を自動で判定する設定（Assisted の許可）を使っていても、自動では許可されません。

確認の意味は次のとおりです。

- `skip tool permission prompts`（ツールの許可の確認を省く）：coverity-warning-triage の記録のツールは、確認なしで動きます。画面に出す状態を書くだけで、ファイルやコマンドなど外を変えません。
- `register hooks`（フックを登録する）：止めた実行のエージェントのツールと、統括エージェントに許していないツール（ファイルの書き込みやコマンドなど。統括は、それらを作業エージェントに頼みます）を拒むために、Copilot CLI の「ツールを使ってよいか」の問い合わせを受けます。
- `handle permission requests`（許可の求めを扱う）：エージェントの許可の求めを見て、画面に「チャットで確認を待っています」と示します。coverity-warning-triage は許可の求めに答えません。許可も拒否も、あなたがチャットで行います。

許可する理由は、断ると読み込まれず、画面で開始も監視もできないからです。選び方は次のとおりです。

| 選択肢 | 意味 |
| --- | --- |
| Yes | 今回だけ許可します。Copilot CLI を起動するたびと、`/clear` のたびに、また確認が出ます |
| Yes, and always allow "plugin:coverity-warning-triage:coverity-warning-triage" in this directory（後ろにフォルダの場所が付きます。作業フォルダが Git のリポジトリのときは、`in this directory` の代わりに `in this repo` と出ます） | このフォルダでは常に許可します。このフォルダでは、次から確認が出ません（`/clear` の後も、起動し直しても）。ほかのフォルダでは、また出ます |
| Yes, and don't ask again for "plugin:coverity-warning-triage:coverity-warning-triage" this session | フォルダが分からないときに、上の選択肢の代わりに出ます。このセッション（Copilot CLI の1つの会話。`/clear` で新しいセッションになります）の間は、確認が出ません |
| No (Esc) | 断ります。coverity-warning-triage は読み込まれません |

初めて使うときと、`/settings experimental on` で再起動した後は、フォルダを信頼するかの確認の後に、この確認が続けて2回出ることがあります。

### 4. Coverity の準備をする

調べたいリポジトリを開いた Copilot CLI で、チャットに `/coverity-setup` と入力します（20 分ほど）。Coverity の URL・プロジェクト名・ストリーム名（チームで最初に使う人だけ）と、Coverity のユーザ名と認証キーを聞かれます。認証キーはチャットに書かず、ターミナルの伏せ字の欄か、準備の画面のパスワードの欄で入れます。

要るものは次のとおりです。

| もの | 確かめ方 |
| --- | --- |
| git か svn（いつも使っているもの） | `git --version` / `svn --version` |
| 社内の Coverity Connect のアカウントと認証キー | ブラウザで Coverity に入れる |
| Copilot で GPT-6 Luna が使えること | チャットのモデルの一覧に出る |
| （初回解析を使うときだけ）Coverity の解析のツール（`cov-build`・`cov-analyze`・`cov-commit-defects`） | `/coverity-selftest ④` で確かめられる |

uv（このプラグインが使う Python の道具）は、無ければ準備のときに入ります。正しく動くかは `/coverity-selftest` で確かめられます。

## 実行中に気をつけること

- 実行中は `/clear` を使わないでください。`/clear` をすると、画面の更新が止まり、前の画面の URL では開けなくなります。そのときは、チャットでもう一度作業を頼み、示された画面を開くと、前の実行が途中で切れたことが示されます。
- 実行中の確認には、チャットで答えます。ファイルの書き込みやコマンドの実行の許可、AI クレジットの上限の確認、エージェントからの質問が、チャットに出ます。画面には、チャットで確認を待っていることと、何の確認かが示されます。画面からは答えられません。
- 止めた後、チャットに確認が残っていることがあります。その確認には、「Yes」と答えても「No」と答えても、ツールは動きません。
- 止めきれなかったエージェントがいるときは、画面にそのエージェントと止め方（チャットで Esc を2回押す、または Copilot CLI を終える）が示されます。

### 任せたいツールを前もって許可する

確認のたびにチャットに戻る手間を減らすには、Copilot CLI を起動するときに、任せたいツールを `--allow-tool` で指定します。指定は、その起動の間だけ有効です。

| 指定 | 許可するもの |
| --- | --- |
| `copilot --allow-tool=write` | ファイルの書き込み |
| `copilot --allow-tool='shell(git:*)'` | `git` のコマンド |
| `copilot --allow-tool='read, write'` | ファイルの読み取りと書き込み（複数は、引用符で囲んで、カンマで区切ります） |
| `copilot --allow-tool='shell(git:*)' --deny-tool='shell(git push)'` | `git push` 以外の `git` のコマンド |

すべてを任せる指定（`--allow-all-tools`、`--allow-all`、`--yolo`）もあります。公式のドキュメントは、隔離した環境でだけ使うよう強く勧めています。

## うまく始まらないとき

チャットで作業を頼んでも画面の URL が示されないときは、coverity-warning-triage のエクステンションが読み込まれていません。次のどれかです。

- 試験的機能を有効にしていない：`copilot --experimental` で起動し直します。すでに起動しているときは、チャットで `/settings experimental on` と打ちます（自動で再起動します）。
- 読み込むときの確認を断った：Copilot CLI を起動し直して、出た確認を許可します。選び方は「3. 読み込むときの確認を許可する」にあります。
- エクステンションを使わない設定（`/extensions mode` の Disabled）にしている：`/extensions mode` で Load & Augment か Load Only に戻します。Disabled は、次に Copilot CLI を起動したときにも残ります。

## 課題の一覧と Issue の起票

終わった後の画面に、課題の一覧と、coverity-warning-triage 自身を改善するための Issue の案が並びます。案ごとの承認ボタンを押すと、その案が coverity-warning-triage 自身の GitHub リポジトリに Issue として起票されます。承認していない案は起票されません。

起票できるようにするには、次の手順を行います。起票できないときも案は並び、この手順が画面に示されます。

1. coverity-warning-triage を GitHub に公開します。
2. `plugin.json` の `repository` に、そのリポジトリの URL（例：`https://github.com/OWNER/REPO`）を書きます。そのあと、手元のフォルダのマーケットプレイスからインストールしたときは、Copilot CLI を終了して起動し直すか、`/clear` で新しい会話を始めます。GitHub からインストールしたときは、直した `plugin.json` を GitHub に送り、次のコマンドで coverity-warning-triage をアンインストールしてから、もう一度インストールします。

   ```shell
   copilot plugin uninstall coverity-warning-triage
   copilot plugin install coverity-warning-triage@plugin-market-place
   ```

3. `gh`（GitHub の公式のコマンド）を入れて、ログインします。

   ```shell
   gh auth login
   ```

## 部品を単体で使う

作業の画面を使わずに、部品を1つずつ使うこともできます。

下の `<プラグインのフォルダ>` は、coverity-warning-triage が入っているフォルダです。手元のフォルダのマーケットプレイスから入れたときは、そのフォルダの中の coverity-warning-triage のフォルダです。GitHub から入れたときは、Copilot CLI のプラグインの置き場所（公式のドキュメントでは `~/.copilot/installed-plugins/<マーケットプレイスの名前>/coverity-warning-triage`）です。

### MCP サーバー coverity-triage

Coverity Connect との通信だけを受け持つ部品です。チャットで、接続の確かめ、警告の検索、警告経路の取得、Coverity への書き戻しを頼めます。coverity-warning-triage を入れると自動で登録されます。入れずに使うときは、MCP のクライアントに登録します。

要るもの：

- uv
- 調べたいリポジトリの `.coverity-triage/config.yaml` に、Coverity の URL（`coverity:` の `url`）
- 環境変数 COV_USER と COV_AUTH_KEY に、Coverity のユーザ名と認証キー（設定ファイルには書きません）。Windows では、ユーザーの環境変数に保存しておけば、MCP サーバーが直接読みます。ほかの OS では、MCP のクライアントがこの2つをサーバーに渡す必要があります（Copilot CLI は、PATH のほかの環境変数を MCP サーバーに渡しません）。

登録の例です。`<server>` は、このリポジトリの `server` フォルダの絶対パスに置き換えます。初めて起動するときは、MCP サーバーの環境を作るので時間がかかります。先に `uv sync --native-tls --frozen --directory <server>` を1回動かしておくと、すぐに起動します。

Windows 以外では、認証情報の環境変数（COV_USER と COV_AUTH_KEY）を、MCP の設定の `env` で渡す必要があります。下の例には `env` がないので、[server/README.md](server/README.md) の「MCP のクライアントに登録する」の例（`env` つき）を使ってください。

- Copilot CLI：

  ```shell
  copilot mcp add coverity-triage -- uv run --native-tls --frozen --quiet --directory <server> coverity-triage-mcp
  ```

- VS Code（`.vscode/mcp.json`）：

  ```json
  {
    "servers": {
      "coverity-triage": {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--native-tls", "--frozen", "--quiet", "--directory", "<server>", "coverity-triage-mcp"]
      }
    }
  }
  ```

- Claude Code：

  ```shell
  claude mcp add --transport stdio coverity-triage -- uv run --native-tls --frozen --quiet --directory <server> coverity-triage-mcp
  ```

頼み方の例です。

- 「C:\work\app の Coverity の接続を確かめて」
- 「ストリーム app-main の、Impact が High の警告を一覧にして」
- 「CID 12345 の警告経路を見せて」
- 「CID 12345 を False Positive にして、逸脱コメントを書き戻して」：書き戻す件数と内容と、確認の文字列が示されます。同意すると書き込まれます。

ツールの引数と結果の形、確認の文字列の作り方は、[server/README.md](server/README.md) にあります。

### 手順書のスキル

警告を調べる、直すなどの仕事を、チャットで直接頼めます。結果はチャットに返り、結果のファイルは作られず、Coverity にも書き込まれません。

| スキル | すること | 頼み方の例 |
| --- | --- | --- |
| triage-investigation | 警告が本物のバグかを調べ、判定・確信度・推奨を返す | 「CID 12345（NULL_RETURNS、src/foo.c の 120 行）の警告が本物か調べて」 |
| code-fix | 警告を直す | 「この警告を直して」 |
| deviation-comment | 逸脱コメントと、Classification・Action・Severity の案を返す | 「この警告の逸脱コメントを書いて」 |
| triage-report | 調べた結果を、決まった項目で書く | 「調べた結果を決まった形で書いて」 |
| triage-review | 別に調べた結果を確かめ、合格・要確認・差し戻しを返す | 「この結果を確かめて」 |
| triage-integration | 同じ原因の警告をまとめるかを決める | 「この3件、同じ原因ならまとめて」 |
| checker-knowledge | チェッカーごとの見方を答える | 「MISRA 10.4 の警告はどう見ればいい？」 |

- 直すときは、差分がチャットに示されます。差分を見て同意すると、作業中のファイルが直ります。
- 調べたいリポジトリで頼みます。`.coverity-triage/knowledge.md` があれば、その知識も使われます。
- 警告の情報（CID、チェッカー、ファイルと行など）が足りないときは、聞かれます。MCP サーバー coverity-triage が使えるときは、警告経路が Coverity から取られます。

### スクリプト ct.py

判断の要らない決まった処理（実行フォルダ、警告のグループ化、修正用のコピー、差分、レポートと一覧、反映）をするコマンドです。ターミナルで動かします。

```shell
uv run --native-tls <プラグインのフォルダ>/skills/coverity-triage-scripts/scripts/ct.py <コマンド> <引数>
```

下の表の `ct.py` は、上の `uv run --native-tls <プラグインのフォルダ>/skills/coverity-triage-scripts/scripts/ct.py` の略です。

| コマンドの例 | すること |
| --- | --- |
| `ct.py --help` | コマンドの一覧を出す |
| `ct.py runs --repo <リポジトリ>` | 実行の一覧を出す |
| `ct.py status --run <実行フォルダ>` | 実行の進み具合を出す |

- どのコマンドも、結果を JSON で1つ出します。できなかったときは `"ok": false` と理由（`error`）を出し、終了コード 1 で終わります。
- 要るものは uv と、git か svn です。Python と要るライブラリは uv が用意します。`--native-tls` は省きません（社内の証明書を使うため）。
- Coverity との通信は MCP サーバーが受け持ちます。ct.py だけでは、警告の取得も、Coverity への書き戻しもできません。
- プラグインのフォルダの中に置いたまま使います。コマンドと引数の一覧は、`skills/coverity-triage-scripts/SKILL.md` にあります。

### ct.py の画面

設定・結果・監視を見るブラウザーの画面です。MCP サーバーも Copilot CLI も要りません。

```shell
uv run --native-tls <プラグインのフォルダ>/skills/coverity-triage-scripts/scripts/ct.py gui --repo <リポジトリ>
```

- `--run <実行フォルダ>` で実行を、`--view monitor|summary|settings` で最初に見せる区分を選べます。`--no-browser` を付けると、ブラウザーを開かずに URL だけを返します。
- 画面は、このマシンのブラウザーからだけ開けます。止めるときは、画面の「サーバを止める」を押します。
- 設定を保存するには、git か svn のリポジトリが要ります。認証情報を画面で保存できるのは Windows だけです。

## 試験

構造の検査と部品の試験は、リポジトリの1つのコマンドで動かせます。モデルを使わないので、AI クレジットを使いません。plugin-creator を入れていなくても動きます。

```shell
python3 tests/run.py
```

- 最初に、試験に要る道具がそろっているかを確かめ、足りなければ足りない道具を示して止まります。
- 段階ごとの合否と、不合格の試験とその理由が出ます。同じ内容がファイルにも書かれ、終了コードでも合否が分かります。
- coverity-warning-triage を直した後も、同じコマンドで試験をやり直せます。

EVAL（実際のモデルで課題を実行して採点する試験）は、plugin-creator を入れた Copilot CLI で、coverity-warning-triage の試験を頼むと行われます。`tests/eval/` の課題と合否の基準を使います。AI クレジットを使うので、始める前に、実行の回数と使う AI クレジットの見込みが示されます。よいと答えてから始まります。

## ライセンス

MIT License です（[LICENSE](LICENSE)）。Coverity と通信する部品（`server/`）、スクリプト（`ct.py`）、手順書と資料の多くは、[coverity-triage-agent](https://github.com/xclang-brain/coverity-triage-agent) の coverity-triage プラグイン（MIT License）を流用しています。
