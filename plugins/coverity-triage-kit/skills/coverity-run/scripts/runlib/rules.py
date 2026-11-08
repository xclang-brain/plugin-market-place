"""Fixed checks of what the agents write: result.json, the deviation comment, review.json, plan.json,
and the mechanical points the reviewer is shown.

These rules are copies of contracts other parts own (design 4.17: comment, result, lint, plan). The
tests run every case of ``assets/contract/<name>/cases.json`` when the file is there, so the copies
keep giving the same answers as the originals.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import schema

# ---- the deviation comment (contract "comment") -----------------------------------------------

MAX_COMMENT = 200
CATEGORIES = ("誤検知。", "意図的。", "本物の不具合。")
CONDITION_WORDS = ("場合に限", "場合のみ", "に限り", "に限って", "に限る", "た場合は", "た場合には", "た場合のみ", "に対して")
REQUEST_WORDS = ("ください", "下さい", "お願い", "てほしい", "要確認", "確認が必要", "確認する必要")
# 「推奨」 only as a recommendation, not as part of a noun (推奨範囲, 推奨値)
RECOMMEND_PATTERNS = (("を推奨", re.compile(r"を推奨(?![々一-鿿゠-ヿ])")),
                      ("推奨します", re.compile(r"推奨します")), ("推奨する", re.compile(r"推奨する")))
MARKDOWN_LINE = re.compile(r"^\s*(#|[-*+]\s|>|```|\d+\.\s)")


def comment_problems(comment: Any) -> list[dict[str, str]]:
    """What is wrong with a deviation comment: ``[{code, message}]`` (empty: it follows the rules)."""
    if not isinstance(comment, str):
        return [{"code": "not_text", "message": "逸脱コメントは文字列にしてください"}]
    text = comment.strip()
    if not text:
        return [{"code": "empty", "message": "逸脱コメントが空です"}]
    out: list[dict[str, str]] = []
    if text != comment:
        out.append({"code": "surrounding_space", "message": "逸脱コメントの前後に空白や改行を入れないでください"})
    if len(text) > MAX_COMMENT:
        out.append({"code": "too_long", "message": f"{MAX_COMMENT} 字以内にしてください（今は {len(text)} 字）。"
                                                   "問題にならない理由と根拠の場所だけを書きます"})
    if not text.startswith(CATEGORIES):
        out.append({"code": "no_category",
                    "message": "判定区分（" + "・".join(f"「{c}」" for c in CATEGORIES) + " のどれか）で始めてください"})
    found = [w for w in CONDITION_WORDS if w in text]
    if found:
        out.append({"code": "condition", "message": "条件や但し書きの言い方（" + "、".join(f"「{w}」" for w in found)
                    + "）があります。どんな状況でも問題にならないことを、条件を付けずに言い切ります"})
    found = [w for w, p in RECOMMEND_PATTERNS if p.search(text)] + [w for w in REQUEST_WORDS if w in text]
    if found:
        out.append({"code": "request", "message": "推奨や人への依頼の言い方（" + "、".join(f"「{w}」" for w in found)
                    + "）があります。逸脱コメントには、問題にならない理由だけを書きます"})
    body = text
    for category in CATEGORIES:
        if body.startswith(category):
            body = body[len(category):]
            break
    sentences = [s for s in re.split(r"[。！？!?]", body) if s.strip()]
    if len(sentences) > 2:
        out.append({"code": "too_many_sentences", "message": f"1〜2 文にしてください（今は {len(sentences)} 文）"})
    if any(MARKDOWN_LINE.match(line) for line in text.splitlines()) or "**" in text:
        out.append({"code": "markdown", "message": "見出しや箇条書きを使わず、ふつうの文で書いてください"})
    return out


# ---- result.json (contract "result") ------------------------------------------------------------

COMBOS = {"false_positive": ("False Positive", "Ignore", ("Unspecified",)),
          "intentional": ("Intentional", "Ignore", ("Unspecified",)),
          "true_bug": ("Bug", "Ignore", ("Major", "Moderate", "Minor"))}


def result_check(result: Any, item: str | None = None, cids: list[int] | None = None) -> dict[str, Any]:
    """``{ok, problems, warnings}`` for a result.json. ``cids`` are the item's CIDs (more than one: a group)."""
    problems = schema.validate(result, "result.schema.json")
    warnings: list[str] = []
    if not isinstance(result, dict):
        return {"ok": False, "problems": problems, "warnings": warnings}
    if item is not None and result.get("item") != item:
        problems.append(f"item は {item} にしてください（今は {result.get('item')}）")
    deviation = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
    if "comment" in deviation:
        problems += [f"deviation.comment: {p['message']}" for p in comment_problems(deviation["comment"])]
    excluded = result.get("group_excluded_cids") or []
    if isinstance(excluded, list) and all(isinstance(c, int) and not isinstance(c, bool) for c in excluded):
        group = len(cids) > 1 if cids is not None else str(item or result.get("item") or "").startswith("G")
        if excluded and not group:
            problems.append("group_excluded_cids はグループのときだけ書きます")
        elif excluded and cids is not None and set(excluded) - set(cids):
            problems.append(f"group_excluded_cids にグループにない CID があります: {sorted(set(excluded) - set(cids))}")
        elif excluded and cids is not None and not set(cids) - set(excluded):
            problems.append("グループのすべての CID を外すことはできません（1 つは残します）")
    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    combo = COMBOS.get(verdict.get("judgement"))
    if combo and deviation:
        cls, action, severities = combo
        if (deviation.get("classification"), deviation.get("action")) != (cls, action) or \
                deviation.get("severity", "Unspecified") not in severities:
            warnings.append(f"逸脱案の Classification / Action / Severity は、ふつう {cls} / {action} / "
                            f"{' か '.join(severities)} です")
    fix = result.get("fix") if isinstance(result.get("fix"), dict) else {}
    if fix and (fix.get("classification"), fix.get("action")) != ("Bug", "Fix Required"):
        warnings.append("修正案の Classification / Action は、ふつう Bug / Fix Required です")
    return {"ok": not problems, "problems": problems, "warnings": warnings}


# ---- mechanical points for the reviewer (contract "lint") ------------------------------------

LINT_TEXT = {
    "evidence_missing_file": "根拠のファイルが調べるコードにありません",
    "evidence_line_out_of_range": "根拠の行がファイルの範囲の外です",
    "undetermined_high": "判定が「判定できない」なのに、確信度が「高」です",
    "drift_high": "リビジョンのずれがあるのに、確信度が「高」です",
    "true_bug_deviation": "本物のバグなのに、逸脱を推奨しています（プロジェクトの方針によることがあります）",
    "not_bug_fix": "誤検知（または意図どおり）なのに、修正を推奨しています",
    "no_evidence": "根拠の場所（evidence）がありません",
}


def _line_count(text: str) -> int:
    return len(text.splitlines()) or (1 if text else 0)


def lint_points(result: dict[str, Any], read_file: Callable[[str], str | None]) -> list[dict[str, str]]:
    """Points found by fixed rules: ``[{code, message}]``, one per code, sorted by code.
    ``read_file(path)`` gives the text of a repository file, or None when it is not there."""
    found: dict[str, list[str]] = {}

    def add(code: str, detail: str = "") -> None:
        found.setdefault(code, [])
        if detail and detail not in found[code]:
            found[code].append(detail)

    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    evidence = verdict.get("evidence") if isinstance(verdict.get("evidence"), list) else []
    for ev in evidence:
        if not isinstance(ev, dict) or not isinstance(ev.get("file"), str):
            continue
        text = read_file(ev["file"])
        if text is None:
            add("evidence_missing_file", ev["file"])
        elif isinstance(ev.get("line"), int) and ev["line"] > _line_count(text):
            add("evidence_line_out_of_range", f"{ev['file']}:{ev['line']}（{_line_count(text)} 行まで）")
    judgement, confidence = verdict.get("judgement"), result.get("confidence")
    recommendation = result.get("recommendation")
    drift = (result.get("revision_drift") or {}).get("status") if isinstance(result.get("revision_drift"), dict) else None
    if judgement == "undetermined" and confidence == "high":
        add("undetermined_high")
    if drift == "detected" and confidence == "high":
        add("drift_high")
    if judgement == "true_bug" and recommendation == "deviation":
        add("true_bug_deviation")
    if judgement in ("false_positive", "intentional") and recommendation == "fix":
        add("not_bug_fix")
    if not evidence:
        add("no_evidence")
    return [{"code": code, "message": LINT_TEXT[code] + (f": {'、'.join(details)}" if details else "")}
            for code, details in sorted(found.items())]


# ---- integration/plan.json (contract "plan") ----------------------------------------------------

ORDER = {"low": 0, "medium": 1, "high": 2}
MAX_GROUP = 10


def plan_check(plan: Any, items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """``{ok, problems}`` for an integration plan. ``items``: ``{id: {cids, status, confidence, no_grouping}}``."""
    problems = schema.validate(plan, "plan.schema.json")
    if problems or not isinstance(plan, dict):
        return {"ok": False, "problems": problems}
    used: set[str] = set()
    for n, merge in enumerate(plan.get("merges") or [], 1):
        members = merge["members"]
        where = f"merges[{n}]"
        if len(set(members)) < 2:
            problems.append(f"{where}: まとめる作業は 2 つ以上にします")
        if len(set(members)) != len(members):
            problems.append(f"{where}: 同じ作業が 2 回あります")
        total = 0
        lowest = None
        for member in dict.fromkeys(members):
            info = items.get(member)
            if info is None or info.get("status") != "done":
                problems.append(f"{where}: {member} は調べ終わった作業ではありません")
            else:
                total += len(info.get("cids") or [])
                if info.get("no_grouping"):
                    problems.append(f"{where}: {member} はグループにしない CID を含みます")
                conf = info.get("confidence")
                if conf in ORDER and (lowest is None or ORDER[conf] < ORDER[lowest]):
                    lowest = conf
            if member in used:
                problems.append(f"{where}: {member} が 2 つのまとまりに入っています")
            used.add(member)
        if total > MAX_GROUP:
            problems.append(f"{where}: まとめる CID は合わせて {MAX_GROUP} 件までにします（今は {total} 件）")
        source = merge["fix_source"]
        if source.startswith("item:") and source[5:] not in members:
            problems.append(f"{where}: fix_source の item: には、まとめる作業の ID を書きます")
        confidence = merge["result"].get("confidence")
        if lowest and confidence in ORDER and ORDER[confidence] > ORDER[lowest]:
            problems.append(f"{where}: result.confidence は、まとめる作業のいちばん低い確信度（{lowest}）以下にします")
    return {"ok": not problems, "problems": problems}


# ---- review.json ------------------------------------------------------------------------------


def item_criteria() -> list[dict[str, Any]]:
    return [c for c in schema.load("criteria.json")["criteria"] if c.get("scope") == "item"]


def review_check(review: Any, item: str) -> dict[str, Any]:
    """``{ok, problems}`` for a review.json: the form, the item, and one finding per criterion of the item scope
    (a criterion with ``applies_when`` may be left out)."""
    problems = schema.validate(review, "review.schema.json")
    if problems or not isinstance(review, dict):
        return {"ok": False, "problems": problems}
    if review.get("item") != item:
        problems.append(f"item は {item} にしてください（今は {review.get('item')}）")
    if review.get("outcome") == "flag" and not review.get("confidence_cap"):
        problems.append("outcome が flag のときは confidence_cap（high / medium / low）を書きます")
    criteria = {c["id"]: c for c in item_criteria()}
    seen: dict[str, int] = {}
    for finding in review.get("findings") or []:
        check = finding.get("check")
        if check not in criteria:
            problems.append(f"findings の check「{check}」は評価基準の id（scope が item のもの）ではありません")
            continue
        seen[check] = seen.get(check, 0) + 1
    twice = sorted(k for k, v in seen.items() if v > 1)
    if twice:
        problems.append("findings に同じ評価基準が 2 回以上あります: " + "、".join(twice))
    missing = [cid for cid, c in criteria.items() if not c.get("applies_when") and cid not in seen]
    if missing:
        problems.append("findings に次の評価基準がありません（すべて 1 回ずつ書きます）: " + "、".join(missing))
    return {"ok": not problems, "problems": problems}


NOT_APPLICABLE = "対象外"
