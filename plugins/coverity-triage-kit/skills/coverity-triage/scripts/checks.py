"""Field checks of result.json and of a review, and the rules of a deviation comment.

Every check returns a list of Japanese sentences that say what to correct (empty: nothing to correct).
Standard library only.
"""

from __future__ import annotations

import re

# verdict -> (how the deviation comment starts, Classification in Coverity)
VERDICTS = {"true_bug": ("本物の不具合。", "Bug"), "false_positive": ("誤検知。", "False Positive"),
            "intentional": ("意図的。", "Intentional"), "undetermined": None}
CATEGORIES = {start: classification for start, classification in filter(None, VERDICTS.values())}
RECOMMENDATIONS = ("fix", "deviation")
CONFIDENCES = ("high", "medium", "low")  # from the highest
ACTIONS = ("Undecided", "Fix Required", "Fix Submitted", "Modeling Required", "Ignore")
SEVERITIES = ("Unspecified", "Major", "Moderate", "Minor")
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
# Requests, recommendations and instructions to people.
REQUEST_WORDS = ("ください", "下さい", "お願い", "てほしい", "て欲しい", "要確認", "確認が必要", "確認する必要",
                 "すべき", "べきである", "が望ましい", "見直", "推奨します", "推奨する")
POINT = re.compile(r"\S+?:\d+(\s|$)")  # file:line, then the remark


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _quoted(words: list[str]) -> str:
    return "、".join(f"「{w}」" for w in words)


def check_comment(comment) -> list[str]:
    """The rules of a deviation comment: one line of at most 200 characters that starts with the category
    and states, without conditions, why the warning is not a problem."""
    if not _text(comment):
        return ["逸脱コメントが空です。判定区分、問題にならない理由、根拠の場所（ファイルと行）を1〜2文で記入してください"]
    problems = []
    if comment != comment.strip():
        problems.append("前後の空白と改行を削除してください")
    body = comment.strip()
    if len(body) > COMMENT_MAX:
        problems.append(f"{COMMENT_MAX} 文字以内にしてください（現在 {len(body)} 文字）")
    start = next((c for c in CATEGORIES if body.startswith(c)), None)
    if start is None:
        problems.append(f"{'、'.join(f'「{c}」' for c in CATEGORIES)}のいずれかで始めてください")
    elif not body[len(start):].strip():
        problems.append("判定区分の後に、問題にならない理由と根拠の場所（ファイルと行）を記入してください")
    if "\n" in body or "\r" in body:
        problems.append("改行を入れずに、1行で記入してください")
    found = [w for w in CONDITION_WORDS if w in body]
    if found:
        problems.append(f"条件や推測の表現（{_quoted(found)}）があります。どの状況でも問題にならないことを断定してください。"
                        "断定できない場合は、推奨を修正（fix）にします")
    found = [w for w in PROCESS_WORDS if w in body]
    if found:
        problems.append(f"調査の過程の表現（{_quoted(found)}）があります。問題にならない理由だけを記入してください")
    found = [w for w in REQUEST_WORDS if w in body]
    if found:
        problems.append(f"依頼や指示の表現（{_quoted(found)}）があります。問題にならない理由だけを記入してください")
    return problems


def check_result(result, cid: int) -> list[str]:
    """The fields of cid/<CID>/result.json."""
    if not isinstance(result, dict):
        return ["result.json は JSON のオブジェクトにしてください"]
    problems = []
    if result.get("cid") != cid:
        problems.append(f"cid: {cid} にしてください")
    verdict = result.get("verdict")
    if verdict not in VERDICTS:
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
    fix = result.get("fix")
    if fix is not None and not (isinstance(fix, dict) and _text(fix.get("summary"))):
        problems.append("fix: summary（修正の内容）を記入してください")
    if (fix is None) != _text(result.get("no_fix_reason")):
        problems.append("fix（修正の内容）と no_fix_reason（修正コードがない理由）は、どちらか一方を記入してください")
    if result.get("recommendation") == "fix" and fix is None:
        problems.append("recommendation が fix のときは、fix.summary を記入してください")
    deviation = result.get("deviation")
    if not isinstance(deviation, dict):
        return problems + ["deviation: comment、classification、action、severity を記入してください"]
    comment = deviation.get("comment")
    problems += [f"deviation.comment: {p}" for p in check_comment(comment)]
    start = next((c for c in CATEGORIES if _text(comment) and comment.strip().startswith(c)), None)
    if start and VERDICTS.get(verdict) and VERDICTS[verdict][0] != start:
        problems.append(f"deviation.comment: verdict が {verdict} のときは「{VERDICTS[verdict][0]}」で始めてください")
    classification = deviation.get("classification")
    if start and classification != CATEGORIES[start]:
        problems.append(f"deviation.classification: 「{start}」で始まる逸脱コメントでは {CATEGORIES[start]} にしてください")
    elif classification not in CATEGORIES.values():
        problems.append(f"deviation.classification: {' / '.join(CATEGORIES.values())} のいずれかにしてください")
    if deviation.get("action") not in ACTIONS:
        problems.append(f"deviation.action: {' / '.join(ACTIONS)} のいずれかにしてください")
    if deviation.get("severity") not in SEVERITIES:
        problems.append(f"deviation.severity: {' / '.join(SEVERITIES)} のいずれかにしてください")
    return problems


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
