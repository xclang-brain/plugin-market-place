# 結果の例

どの例も、確かめのスクリプトを通ります。

## 誤検知（逸脱を推奨）

呼び出し元が NULL を除いている例です。誤検知でも、修正案を作っています。

```json
{
  "item": "20002",
  "verdict": {
    "judgement": "false_positive",
    "summary": "唯一の呼び出し元 read_all() で NULL を除いている",
    "rationale": "get_buf() は 15 行で NULL を返しうる。その戻り値を受ける copy_name() の呼び出し元は read_all() だけで（リポジトリを検索して確かめた）、read_all() は 32 行で NULL のときに -1 を返して戻る。そのため 23 行の memcpy に NULL が渡ることはない。",
    "evidence": [
      {"file": "src/io/reader.c", "line": 15, "note": "get_buf() が NULL を返す"},
      {"file": "src/io/reader.c", "line": 32, "note": "read_all() で NULL を除く"}
    ]
  },
  "recommendation": "deviation",
  "confidence": "high",
  "confidence_reason": "呼び出し元はリポジトリに1つだけで、経路の条件をすべてコードで確かめた",
  "deviation": {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                "comment": "誤検知。copy_name() の唯一の呼び出し元 read_all()（src/io/reader.c:32）で NULL を除いてから渡しているため、NULL を参照することはない。"},
  "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Minor",
          "summary": "copy_name() の先頭で dst が NULL なら 0 を返す",
          "impact": "dst が NULL のときだけ何もせず戻る。今の呼び出し元では動きは変わらない",
          "exceeded_constraints": [], "already_fixed_on_latest": false},
  "revision_drift": {"status": "none", "detail": ""}
}
```

## グループ（1つの CID を外す）

同じマクロが原因の MISRA の警告3件のグループで、1件だけ原因が違うので外した例です。

```json
{
  "item": "G1",
  "verdict": {
    "judgement": "true_bug",
    "summary": "マクロ BUF_LEN が符号付きで、符号なしと比べている",
    "rationale": "CID 30001 と 30002 は、どちらも BUF_LEN（include/buf.h:10、値は int の 64）を size_t の変数と比べている（src/buf.c:20、src/buf.c:35）。CID 30003 は src/buf.c:51 の別の式（int と unsigned char の足し算）で、BUF_LEN とは関係がない。",
    "evidence": [
      {"file": "include/buf.h", "line": 10, "note": "#define BUF_LEN 64"},
      {"file": "src/buf.c", "line": 20, "note": "len < BUF_LEN"},
      {"file": "src/buf.c", "line": 35, "note": "pos >= BUF_LEN"}
    ]
  },
  "recommendation": "fix",
  "confidence": "high",
  "confidence_reason": "2か所の比べ方と、マクロの定義をコードで確かめた",
  "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Minor",
                "comment": "本物の不具合。BUF_LEN（include/buf.h:10）は 64 で、比べる相手の size_t の値は 0〜64 に収まるため、比べた結果は変わらない。"},
  "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Minor",
          "summary": "BUF_LEN を 64u にする",
          "impact": "BUF_LEN を使う式の型が unsigned int になる。値は変わらないので、動きは変わらない",
          "exceeded_constraints": [], "already_fixed_on_latest": false},
  "revision_drift": {"status": "none", "detail": ""},
  "group_excluded_cids": [30003]
}
```

## 最新のリビジョンで、もう直っている

解析したリビジョンでは問題があったが、修正するフォルダ（最新）ではもう直っていた例です。修正は作らず、そのことを書きます。

```json
{
  "item": "20005",
  "verdict": {
    "judgement": "true_bug",
    "summary": "解析時は戻り値を確かめずに使っていた（最新では直っている）",
    "rationale": "解析したリビジョンの src/cfg.c:44 は parse() の戻り値を確かめずに使っていた。修正するフォルダ（最新）では src/cfg.c:44〜46 で NULL を確かめてから使っている。",
    "evidence": [{"file": "src/cfg.c", "line": 44, "note": "最新では NULL を確かめている"}]
  },
  "recommendation": "fix",
  "confidence": "medium",
  "confidence_reason": "解析したリビジョンと最新の両方を読んだが、最新で直した変更の意図は記録がない",
  "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Moderate",
                "comment": "本物の不具合。解析したコードの parse() の戻り値の扱い（src/cfg.c:44）は最新のコードで直っており、直さない理由は説明できない。"},
  "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Moderate",
          "summary": "最新のリビジョンで直っている（src/cfg.c:44〜46 で NULL を確かめている）",
          "impact": "変更なし",
          "exceeded_constraints": [], "already_fixed_on_latest": true},
  "revision_drift": {"status": "detected", "detail": "src/cfg.c:44 が解析したリビジョンと最新で違う"}
}
```
