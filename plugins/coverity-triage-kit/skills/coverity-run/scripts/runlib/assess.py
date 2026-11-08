"""The confidence shown to the person: the lowest of the worker's, the review's cap and the build check's (C-19).

result.json keeps what the worker wrote; this only lowers what the reports and status show. A review
that flags or sends back without a cap counts as low. A build check with problems lowers a fix
recommendation to low (a deviation keeps its confidence: its fix is only the other proposal).
"""

from __future__ import annotations

from typing import Any

ORDER = {"low": 0, "medium": 1, "high": 2}
JA = {"high": "高", "medium": "中", "low": "低"}


def review_cap(review: dict[str, Any] | None) -> str | None:
    if not review:
        return None
    cap = review.get("confidence_cap")
    if review.get("outcome") in ("flag", "rework") and not cap:
        cap = "low"
    return cap if cap in ORDER else None


def effective(result: dict[str, Any], review: dict[str, Any] | None = None,
              verify: dict[str, Any] | None = None) -> dict[str, Any]:
    """A copy of ``result`` with the shown confidence and the reasons it was lowered."""
    confidence = result.get("confidence", "low")
    if confidence not in ORDER:
        confidence = "low"
    reasons = []
    cap = review_cap(review)
    if cap and ORDER[cap] < ORDER[confidence]:
        confidence = cap
        note = str((review or {}).get("note") or "").strip() or "要確認"
        reasons.append(f"結果の確かめで「{JA[cap]}」まで（{note}）")
    if verify and verify.get("problems") and result.get("recommendation") == "fix" and confidence != "low":
        confidence = "low"
        reasons.append("ビルドでの確かめで問題: " + "、".join(verify["problems"]))
    if confidence == result.get("confidence"):
        return result
    return {**result, "confidence": confidence,
            "confidence_reason": f"{result.get('confidence_reason', '')}（{'。'.join(reasons)}）"}


def shown_review(item: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any] | None:
    """The review as the person sees it: a review sent back as often as allowed counts as a flag."""
    if item.get("review") == "unverified":
        return {"outcome": "unverified", "reason": "確かめられませんでした", "findings": []}
    if not item.get("review") or not review:
        return None
    if item.get("review") == "flag" and review.get("outcome") == "rework":
        return {**review, "outcome": "flag", "reason": "作り直しの上限まで差し戻しても、指摘が残っています"}
    return review
