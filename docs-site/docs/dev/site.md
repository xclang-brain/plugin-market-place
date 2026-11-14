# サイトの更新

プラグインの README、`docs/guide`、仕様書、設計書を変更した場合は、本サイトの該当ページも更新してください。記載が食い違う場合は、プラグインの文書を優先します。

| 対象 | 場所 |
| --- | --- |
| ページ | `docs-site/docs/` |
| 設定とナビゲーション | `docs-site/mkdocs.yml` |
| プラグインからの差し込み処理 | `docs-site/hooks.py` |
| スタイルとスクリプト | `docs-site/docs/assets/` |
| 公開用のワークフロー | `.github/workflows/docs.yml` |

## プラグインからの差し込み

利用者がコピーして実行する内容は、ページに転記せず、ビルド時にプラグインのファイルから差し込みます。

| ページ内の記述 | 差し込み内容 |
| --- | --- |
| <code>&lt;!-- ctk:credentials:powershell --&gt;</code>（`:bash` も同様） | `setup.py` が出力する、認証情報の設定手順 |
| <code>&lt;!-- ctk:config-template --&gt;</code> | `setup.py` が作成する設定ファイル |
| <code>&lt;!-- ctk:version --&gt;</code> | `plugin.json` のバージョン（ページ下部にも表示） |
| <code>&#91;設計書&#93;(gh&#58;docs/design/design.md)</code> | プラグイン内のファイルへの GitHub のリンク |

差し込めない記述や、存在しないファイルへのリンクがあると、ビルドは失敗します。

## ローカルでの確認

リポジトリの最上位で次を実行し、表示された URL を開きます。

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

## 公開

`main` に push すると、`.github/workflows/docs.yml` がビルドし、GitHub Pages に公開します。プルリクエストでは、`--strict` でビルドするだけで、公開はしません。GitHub Pages の公開元（Settings → Pages の Source）は「GitHub Actions」に設定済みです。

Material for MkDocs は、2027年5月5日に保守を終了すると告知されています（ビルド時に表示されます）。後継は、同じ開発チームによる Zensical です。

## 記述の方針

- 1ページの目的は1つに絞り、読者が次に行うことを先に書く
- 「使い方」の各ページには、利用者が直接扱わない内部名（スクリプト名、応答のキー、「N段階目」）を書かない
- 実環境で確認していない事項は、未確認と明記する
- 図（Mermaid）は、処理の流れや状態遷移など、文章では追いにくい箇所に限って使う
- 見出し、表の列名、項目名は名詞（体言止め）にする。画面の選択肢、チャットへの入力文、表示されるメッセージの引用は除く
- 「トラブルシューティング」には表示されるメッセージだけを載せ、左列にはその文言をそのまま書く（ページ内の絞り込みで検索できるようにするため）。症状や操作の説明は、該当するページに書く
