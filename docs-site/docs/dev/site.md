# サイトを更新する

プラグインの README、`docs/guide`、仕様書、設計書を変えたら、このサイトの該当ページも直します。

| もの | 場所 |
| --- | --- |
| ページ | `docs-site/docs/` |
| 設定とナビゲーション | `docs-site/mkdocs.yml` |
| プラグインから差し込む処理 | `docs-site/hooks.py` |
| 見た目 | `docs-site/docs/assets/` |
| 公開 | `.github/workflows/docs.yml` |

## プラグインから差し込む部分

利用者がコピーして実行するものは、ページに書き写さず、ビルドのときにプラグインのファイルから差し込みます。プラグインを変えれば、サイトの内容も変わります。

| ページに書くもの | 差し込まれるもの |
| --- | --- |
| <code>&lt;!-- ctk:credentials:powershell --&gt;</code>（`:bash` も） | `setup.py` が表示する、認証情報の設定手順 |
| <code>&lt;!-- ctk:config-template --&gt;</code> | `setup.py` が作る設定ファイル |
| <code>&lt;!-- ctk:version --&gt;</code> | `plugin.json` の版。ページ下部にも表示されます |
| <code>&#91;設計書&#93;(gh&#58;docs/design/design.md)</code> | プラグインのファイルへの GitHub のリンク |

差し込めない書き方や、存在しないファイルへのリンクがあると、ビルドが止まります。

## 手元で見る

リポジトリの最上位で実行し、表示された URL を開きます。

=== "PowerShell（Windows）"

    ```powershell
    py -3 -m venv docs-site\.venv
    docs-site\.venv\Scripts\pip install -r docs-site/requirements.txt
    docs-site\.venv\Scripts\mkdocs serve -f docs-site/mkdocs.yml
    ```

=== "bash（Linux）"

    ```bash
    python3 -m venv docs-site/.venv
    docs-site/.venv/bin/pip install -r docs-site/requirements.txt
    docs-site/.venv/bin/mkdocs serve -f docs-site/mkdocs.yml
    ```

## 公開する

`main` に push すると、`.github/workflows/docs.yml` がビルドして GitHub Pages に公開します。プルリクエストでは、`--strict` でビルドだけを行います（リンク切れや差し込みの失敗で不合格になります）。

最初の1回だけ、リポジトリの Settings → Pages で、Source を「GitHub Actions」にします。

Material for MkDocs は、2027年5月5日に保守が終わると告知されています（ビルドのときに表示されます）。後継は、同じチームの Zensical です。

## 書き方

- 1ページの目的は1つにします。読む人が次にすることを先に書きます。
- 使う側のページには、利用者が触らない内部の名前（スクリプト、応答のキー、「N段階目」）を書きません。
- 実機で確認していないことは、確認していないと書きます。
- 図（Mermaid）は、流れや状態が文章では追いにくいところにだけ使います。
- 困ったときの表の左の列には、実際に表示される文をそのまま書きます。ページの入力欄で絞り込めるようにするためです。
