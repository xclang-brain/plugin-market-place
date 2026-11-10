"""Field checks of result.json and of a review, the rules of a deviation comment, and the form of a fixed file.

Every check returns a list of Japanese sentences that say what to correct (empty: nothing to correct).
Standard library only.
"""

from __future__ import annotations

import re

# verdict -> (how the deviation comment starts, Classification in Coverity)
VERDICTS = {"true_bug": ("本物のバグ。", "Bug"), "false_positive": ("誤検知。", "False Positive"),
            "intentional": ("意図的。", "Intentional"), "undetermined": None}
CATEGORIES = {start: classification for start, classification in filter(None, VERDICTS.values())}
SEVERITIES = {"High": "Major", "Medium": "Moderate", "Low": "Minor"}  # Impact of a real bug -> Severity
RECOMMENDATIONS = ("fix", "deviation")
CONFIDENCES = ("high", "medium", "low")  # from the highest
OUTCOMES = ("pass", "flag", "rework", "unverified")
COMMENT_MAX = 200  # characters

# The comment says that the warning is harmless in every situation: no conditions, no guesses.
CONDITION_WORDS = ("ただし", "但し", "場合に限", "場合のみ", "に限り", "に限って", "に限る", "場合は", "場合には",
                   "に対して", "前提", "間だけ", "間のみ", "仮に", "おそらく", "恐らく", "たぶん", "多分", "かもしれ",
                   "かも知れ", "と思う", "と思い", "と思われ", "と思っ", "と思え", "とは思", "ように思", "と考える",
                   "と考えます", "と考えられ", "と考えた", "と考えてい", "とは考え", "考えにく", "と推測", "と推定",
                   "と見られ", "とみられ", "だろう", "でしょう")
# How it was investigated and what could not be checked belong to the evidence, not to the comment.
PROCESS_WORDS = ("分からなかった", "確認できなかった", "追えなかった", "追いきれ", "調べきれ", "再解析",
                 "解析したリビジョン")
# Requests, recommendations and instructions to people. A sentence that ends in こと is an instruction too.
REQUEST_WORDS = ("ください", "下さい", "お願い", "てほしい", "て欲しい", "要確認", "確認が必要", "確認する必要",
                 "すべき", "べきである", "が望ましい", "見直", "を推奨", "推奨します", "推奨する")
MARKUP = re.compile(r"`|\*\*[^*\s](?:[^*]*[^*\s])?\*\*")  # code and bold marks; not the ** of "char **argv"
POINT = re.compile(r".+?:\d+(\s|$)")  # file:line, then the remark


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _quoted(words: list[str]) -> str:
    return "、".join(f"「{w}」" for w in words)


def _sentences(body: str) -> list[str]:
    """The sentences of a text, without the 。 that ends them. A 。 inside brackets (a quoted code comment)
    ends no sentence."""
    depth, found = 0, [""]
    for ch in body:
        depth += (ch in "（(「『") - (ch in "）)」』")
        if ch == "。" and depth <= 0:
            found.append("")
        else:
            found[-1] += ch
    return [s.strip() for s in found if s.strip()]


def check_comment(comment) -> list[str]:
    """The rules of a deviation comment: one line of at most 200 characters that starts with the verdict
    and states in one or two sentences, without conditions, why the warning is not a problem."""
    if not _text(comment):
        return ["逸脱コメントが空です。判定、問題にならない理由、根拠の場所（ファイルと行）を1〜2文で記入してください"]
    problems = []
    if comment != comment.strip():
        problems.append("前後の空白と改行を削除してください")
    body = comment.strip()
    if len(body) > COMMENT_MAX:
        problems.append(f"{COMMENT_MAX} 文字以内にしてください（現在 {len(body)} 文字）")
    start = next((c for c in CATEGORIES if body.startswith(c)), None)
    sentences = _sentences(body[len(start or ""):])
    if start is None:
        problems.append(f"判定（{_quoted(list(CATEGORIES))}のいずれか）で始めてください")
    elif not sentences:
        problems.append("判定の後に、問題にならない理由と根拠の場所（ファイルと行）を記入してください")
    elif len(sentences) > 2:
        problems.append(f"判定の後は1〜2文にしてください（現在 {len(sentences)} 文）")
    if "\n" in body or "\r" in body:
        problems.append("改行を入れずに、1行で記入してください")
    if MARKUP.search(body):
        problems.append("` や ** などの書式を使わずに記入してください")
    found = [w for w in CONDITION_WORDS if w in body]
    if found:
        problems.append(f"条件や推測の表現（{_quoted(found)}）があります。どの状況でも問題にならないことを断定してください。"
                        "断定できない場合は、推奨を修正（fix）にします")
    found = [w for w in PROCESS_WORDS if w in body]
    if found:
        problems.append(f"調査の過程の表現（{_quoted(found)}）があります。問題にならない理由だけを記入してください")
    found = [w for w in REQUEST_WORDS if w in body] + ["〜こと。"] * any(s.endswith("こと") for s in sentences)
    if found:
        problems.append(f"依頼や指示の表現（{_quoted(found)}）があります。問題にならない理由だけを記入してください")
    return problems


def check_result(result, cid: int) -> list[str]:
    """The fields of cid/<CID>/result.json that the worker writes."""
    if not isinstance(result, dict):
        return ["result.json は JSON のオブジェクトにしてください"]
    problems = []
    if result.get("cid") != cid:
        problems.append(f"cid: {cid} にしてください")
    verdict = result.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        problems.append(f"verdict: {' / '.join(VERDICTS)} のいずれかにしてください")
    if not _text(result.get("summary")):
        problems.append("summary: 判定の要約を記入してください")
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        problems.append("evidence: 根拠を1件以上記入してください（file、line、note）")
    else:
        for n, item in enumerate(evidence):
            line = item.get("line") if isinstance(item, dict) else None
            if not (isinstance(item, dict) and _text(item.get("file")) and _text(item.get("note"))
                    and isinstance(line, int) and not isinstance(line, bool) and line >= 1):
                problems.append(f"evidence[{n}]: file（リポジトリからの相対パス）、line（1以上の整数）、note を記入してください")
    if result.get("recommendation") not in RECOMMENDATIONS:
        problems.append(f"recommendation: {' / '.join(RECOMMENDATIONS)} のどちらかにしてください")
    if result.get("confidence") not in CONFIDENCES:
        problems.append(f"confidence: {' / '.join(CONFIDENCES)} のいずれかにしてください")
    elif verdict == "undetermined" and result["confidence"] != "low":
        problems.append("confidence: verdict が undetermined のときは low にしてください")
    fix = result.get("fix")
    if fix is not None and not (isinstance(fix, dict) and _text(fix.get("summary"))):
        problems.append("fix: summary（修正の内容）を記入してください")
    if (fix is None) != _text(result.get("no_fix_reason")):
        problems.append("fix（修正の内容）と no_fix_reason（修正コードがない理由）は、どちらか一方を記入してください")
    if result.get("recommendation") == "fix" and fix is None:
        problems.append("recommendation が fix のときは、fix.summary を記入してください")
    deviation = result.get("deviation")
    if not isinstance(deviation, dict):
        return problems + ["deviation: comment（逸脱コメント）を記入してください"]
    comment = deviation.get("comment")
    problems += [f"deviation.comment: {p}" for p in check_comment(comment)]
    start = next((c for c in CATEGORIES if _text(comment) and comment.strip().startswith(c)), None)
    expected = VERDICTS.get(verdict) if isinstance(verdict, str) else None
    if start and expected and expected[0] != start:
        problems.append(f"deviation.comment: verdict が {verdict} のときは「{expected[0]}」で始めてください")
    return problems


def coverity_fields(result: dict, impact) -> dict:
    """Classification, Action and Severity that are registered in Coverity with the comment of a checked
    result. A real bug without fix code is one that the latest code has fixed already: Fix Submitted."""
    comment = result["deviation"]["comment"]
    classification = next(c for start, c in CATEGORIES.items() if comment.startswith(start))
    return {"classification": classification,
            "action": "Fix Submitted" if result["verdict"] == "true_bug" and result.get("fix") is None else "Ignore",
            "severity": SEVERITIES.get(impact, "Unspecified") if classification == "Bug" else "Unspecified"}


def check_form(name: str, before: bytes, after: bytes) -> list[str]:
    """A fix keeps the line ends and the character encoding of the file (``before``: the clean file)."""
    def form(data: bytes) -> tuple[str | None, str | None]:  # None: the bytes do not tell
        crlf = data.count(b"\r\n")
        lf = data.count(b"\n") - crlf
        try:
            code = None if data.isascii() else data.decode("utf-8") and "UTF-8"
        except UnicodeDecodeError:
            code = "UTF-8 以外（Shift_JIS など）"
        return ("CRLF と LF の混在" if crlf and lf else "CRLF" if crlf else "LF" if lf else None), code

    return [f"{name}: {label}が変わっています（修正前: {old}、修正後: {new}）。修正前と同じ{label}で保存してください"
            for label, old, new in zip(("改行コード", "文字コード"), form(before), form(after))
            if old and new and old != new and "混在" not in old]


def check_review(outcome: str, points: list[str], cap: str | None) -> list[str]:
    """What a reviewer records for one result."""
    problems = []
    if outcome not in OUTCOMES:
        problems.append(f"outcome: {' / '.join(OUTCOMES)} のいずれかにしてください")
    if outcome in ("flag", "rework") and not points:
        problems.append("point: 要確認と差し戻しでは、指摘を1件以上記入してください（例: src/a.c:120 解放後に参照している）")
    problems += [f"point: 「ファイル:行 指摘」の形にしてください（{p}）" for p in points if not POINT.match(p)]
    if cap is not None and cap not in CONFIDENCES:
        problems.append(f"cap: {' / '.join(CONFIDENCES)} のいずれかにしてください")
    return problems


def lower_confidence(value: str | None, cap: str | None) -> str | None:
    """The confidence that is shown: the lower of the worker's value and the reviewer's cap."""
    known = [v for v in (value, cap) if v in CONFIDENCES]
    return max(known, key=CONFIDENCES.index) if known else None
