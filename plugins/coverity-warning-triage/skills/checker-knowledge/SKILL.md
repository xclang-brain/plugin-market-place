---
name: checker-knowledge
description: Coverity のチェッカー（標準チェッカー、MISRA C/C++、CERT C/C++）ごとの判断観点。C / C++ の Coverity 警告を調査するときに、チェッカー名に応じて参照する。チャットで「MISRA C-2012 Rule 10.4 の警告はどう見ればいい？」のように、チェッカーや規則の見方を直接聞かれたときにも使う。
---

# チェッカー別の判断観点

チェッカー名に応じて、次の資料を読んでから調査してください。

| チェッカー名 | 資料 |
|---|---|
| `MISRA C-2012 ...`、`MISRA C++-...` など MISRA で始まるもの | [references/misra.md](references/misra.md)（K-60） |
| `CERT ...` で始まるもの | [references/cert.md](references/cert.md)（K-61） |
| 上記以外（`NULL_RETURNS`、`RESOURCE_LEAK`、`OVERRUN` などの標準チェッカー） | [references/standard.md](references/standard.md)（K-59） |

資料にないチェッカーは、指示ファイルの「チェッカーの説明（Coverity）」を読み、スキル `triage-investigation` の手順で調べます。（K-62）

## 指示ファイルがないとき（チャットで直接頼まれたとき）

人がチャットで、チェッカーや規則の見方を聞いたとき（例「MISRA C-2012 Rule 10.4 の警告はどう見ればいい？」）の答え方です。

- チェッカー名で上の表から資料を選んで読み、その観点で答える。資料のどの項目の観点かも示す。
- 資料にないチェッカーは、資料にないことを伝える。そのうえで、チェッカーの説明（頼んだ人が示したもの。または、MCP サーバー coverity-triage のツール `get_issues` の結果の `checker_description`）があれば、それを読んで答える。説明がなければ、推測で答えずに、チェッカーの説明か、警告の CID とストリーム名を、頼んだ人に聞く。
- 1 件の警告が本物かを聞かれたときは、スキル `triage-investigation` があれば、その手順で調べる。なければ、資料の観点を示して答える。
