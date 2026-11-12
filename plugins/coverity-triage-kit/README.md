# coverity-triage-kit

Coverity Connect の警告を、設定した条件で取得し、CID ごとに次の2つを作成する GitHub Copilot CLI 用のプラグインです。対象は C / C++ です。

1. **修正コード**（リポジトリにそのまま適用できる差分）
2. **逸脱コメント**（修正しない場合に Coverity に登録する、200文字以内のコメント）

一覧を確認して「OK」と答えると、プラグインが修正をリポジトリに取り込み、逸脱コメントを Coverity に登録します。

## 準備（最初の1回）

必要なものは、GitHub Copilot CLI、[uv](https://docs.astral.sh/uv/)、git（2.32 以降）または svn、Coverity Connect のアカウントと認証キーです。

**1. プラグインをインストールします。**

```shell
copilot plugin marketplace add xclang-brain/plugin-market-place
copilot plugin install coverity-triage-kit@plugin-market-place
```

**2. Coverity のユーザー名と認証キーを、環境変数に設定します。** 自分のターミナルで実行します。認証キーは、チャットには入力しません。認証キーは、Coverity Connect で作成します。ダウンロードされたファイルの `"key"` の値を入力します。

PowerShell（Windows）：

```powershell
[Environment]::SetEnvironmentVariable('COV_USER', '<ユーザー名>', 'User')
$k = Read-Host 'COV_AUTH_KEY（認証キー。入力は表示されません）' -AsSecureString
[Environment]::SetEnvironmentVariable('COV_AUTH_KEY', [Net.NetworkCredential]::new('', $k).Password, 'User')
```

bash（Linux）：

```bash
read -r -p 'COV_USER: ' COV_USER
read -r -s -p 'COV_AUTH_KEY（認証キー。入力は表示されません）: ' COV_AUTH_KEY; echo
export COV_USER COV_AUTH_KEY      # このターミナルから copilot を起動します
```

bash で設定した値は、そのターミナルの中だけで有効です。新しいターミナルで `copilot` を起動するときは、先にこの3行をもう一度実行します。

**3. リポジトリのフォルダーで `copilot` を起動し、Coverity の URL とストリーム名を入力します。**

```text
/coverity-triage-kit:coverity-setup https://coverity.example.co.jp:8443 app-main
```

設定ファイル（`.coverity-triage/config.yaml`）が作成され、接続の確認結果が表示されます。「準備ができました」と表示されれば完了です。

## 使い方

使い方は4段階あります。1から順に試すと、任せる範囲が広がります。どの段階も、単独で使えます。

| 段階 | 入力するもの | プラグインが行うこと |
| --- | --- | --- |
| [1. 1件ずつ処置する](docs/guide/1-skill.md) | 「条件に合う警告を取得して」「CID 20002 を処置して」 | 指定した CID の修正コードと逸脱コメントを作成します |
| [2. すべての警告を任せる](docs/guide/2-agent.md) | `copilot --agent coverity-triage-kit:triage-worker` で起動して、「条件に合う警告をすべて処置して」 | 1つのエージェントが、条件に合う警告を設定の上限（最初は20件）まで取得し、最後まで処置します |
| [3. 複数のエージェントに任せる](docs/guide/3-multi-agent.md) | `copilot --agent coverity-triage-kit:triage-orchestrator` で起動して、同じ依頼 | 並列に処置し、同じ原因の CID を1つにまとめ、別のエージェントがすべての結果を検証します |
| [4. 画面で確認して選ぶ](docs/guide/4-screen.md) | 「画面を開いて」 | ブラウザーの画面に、進捗と、CID ごとの修正コード・逸脱コメントを表示します |

どの段階でも、最後に次のような一覧が1つ表示されます。

| CID | 判定 | 確信度 | 修正コード | 逸脱コメント | 選択 | 備考 |
| --- | --- | --- | --- | --- | --- | --- |
| 20001 | 本物のバグ | 高 | src/reader.c | 本物のバグ。open_and_read()（src/reader.c:19〜20）は fgets の失敗時に fp を閉じずに返す。 | 修正を取り込む | |
| 20002 | 誤検知 | 高 | src/reader.c | 誤検知。唯一の呼び出し元 read_config()（src/reader.c:32〜36）が path の NULL を除外してから渡しており、NULL を参照することはない。（False Positive／Ignore／Unspecified） | 逸脱コメントを登録する | |

「OK」と答えると、選択のとおりに処理されます。変更するときは、「20002 は修正、ほかは OK」のように答えます。リポジトリのフォルダー（svn では、手元の作業コピー）と Coverity が変わるのは、「OK」と答えた後だけです。

| 選択 | プラグインが行うこと |
| --- | --- |
| 修正を取り込む | git：修正用のブランチに修正1件を1コミットとして push し、プルリクエスト作成用の URL を表示します。svn：作業コピーに差分を適用します。Coverity には「Bug／Fix Required」と修正の概要を登録します |
| 逸脱コメントを登録する | Coverity に、逸脱コメントと Classification・Action・Severity を登録します |
| 保留にする | Coverity に「Pending」（保留）と理由を登録します。次の取得からは除かれます。確信度が低い CID などは、最初からこの選択です |

保留にした警告は、Coverity で確認できます。もう一度処置するときは、[設定](docs/guide/settings.md) の `classification` に `Pending` を加えます。

## 文書

| 目的 | 文書 |
| --- | --- |
| 使い方を順に試す | 上の表の 1 → 2 → 3 → 4 |
| 取得する条件やオプションを変える | [設定](docs/guide/settings.md) |
| 変更点を確認する | [変更履歴](CHANGELOG.md) |
| 開発に参加する | [仕様書](docs/specs/requirements.md)・[設計書](docs/design/design.md)・[テスト](tests/README.md) |

MIT License です（[LICENSE](LICENSE)）。
