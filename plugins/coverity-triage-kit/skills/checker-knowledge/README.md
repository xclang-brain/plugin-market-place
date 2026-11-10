# checker-knowledge（チェッカーの見方）

## 何をするか

Coverity のチェッカーごとの見方をまとめた資料のスキルです。チェッカー名に合う資料の項目を読み、次のことに答えます。

- そのチェッカー・規則が何を指摘するか
- 誤検知になりやすい形と、確かめ方
- 直し方の定石（最小の変更、まわりの書き方に合わせる）
- 逸脱コメントの言い方のヒント
- 判定（本物・誤検知・意図的・判定できない）の目安

資料は3つです。

| 資料 | 中身 |
|---|---|
| `references/standard.md` | 標準チェッカー（NULL_RETURNS、FORWARD_NULL、REVERSE_INULL、RESOURCE_LEAK、OVERRUN、BUFFER_SIZE、STRING_OVERFLOW、UNINIT、DEADCODE、UNREACHABLE、MISSING_BREAK、CHECKED_RETURN、NEGATIVE_RETURNS、INTEGER_OVERFLOW、DIVIDE_BY_ZERO、USE_AFTER_FREE、DOUBLE_FREE、SIZEOF_MISMATCH、COPY_PASTE_ERROR、NO_EFFECT、TAINTED_SCALAR、C++ の UNCAUGHT_EXCEPT・CTOR_DTOR_LEAK・UNINIT_CTOR ほか） |
| `references/misra.md` | MISRA C:2012（分類ごとの扱い、本質的な型のモデル、Rule 2・8・10・11・12・13・14・15・16・17・18・20・21・22、Directive 4）と MISRA C++ のヒント |
| `references/cert.md` | CERT C（EXP33/34、INT30〜34、ARR30/38、STR31/32、MEM30/31/34/35、FIO42/47、ERR33、DCL・ENV・POS）と CERT C++ のヒント |

ファイルを作らず、Coverity に何も書き込みません。

## 必要なもの

このフォルダだけで動きます。スクリプトはありません。

## 置き方

このフォルダだけを、次のどれかにコピーします（フォルダの名前は変えても使えます）。

- 作業フォルダの `.github/skills/checker-knowledge/`（GitHub Copilot CLI）
- `~/.copilot/skills/checker-knowledge/`（GitHub Copilot CLI、すべての作業フォルダ）

## 呼び方

チャットで聞きます。AI がこのスキルを選びます（`/checker-knowledge` でも呼べます）。

- 「MISRA 10.4 の警告はどう見ればいい？」
- 「CERT ERR33-C で fclose の戻り値を見ていない警告は、逸脱でいい？」
- 「FORWARD_NULL でよくある誤検知の形は？」

警告を調べる・直す・逸脱コメントを書く手順の途中で、AI がチェッカーの観点を確かめるためにも使います。

## 設定

設定のファイルはありません。調べたいリポジトリに `.coverity-triage/knowledge.md` があり、規則の扱い（逸脱にする規則、適用しない規則など）が書かれていれば、資料よりそちらを優先します。

## 結果の見方

答えには、どの資料のどの項目の観点かが添えられます（例「references/misra.md の Rule 10.4」）。

資料にないチェッカーは「資料にない」と答え、Coverity のチェッカーの説明にもとづいて答えます。説明がなければ、推測で答えずに、チェッカーの説明か、警告の CID とストリームの名前を聞きます。

## うまくいかないとき

| 起きたこと | 直し方 |
|---|---|
| 「資料にない」と言われる | Coverity の画面の、その警告のチェッカーの説明を貼って聞き直す |
| 答えがプロジェクトの決まりと違う | `.coverity-triage/knowledge.md` の「推奨の方針」に、プロジェクトの決まり（例「MISRA の Advisory の Rule 15.5 は適用しない」）を書く |
| 資料の中身を足したい | `references/` の該当する資料の項目に、同じ並び（指摘・誤検知になりやすい形・確かめ方・直し方・逸脱コメントのヒント・判定の目安）で書き足し、目次にも足す。資料が 100 行を超えたら目次が要る |

## テストの動かし方

このフォルダで動かします。

```
# Windows（PowerShell）
py -3 -B -m unittest discover -s tests
# Linux（bash）
python3 -B -m unittest discover -s tests
```

`tests/test_skill_files.py` が、SKILL.md の形、資料の目次、リンク、試験の課題の形を確かめます。

実際のモデルで解かせる試験の課題は `evals/` にあります（skill-creator の形。スキルありとなしを比べて採点します）。
