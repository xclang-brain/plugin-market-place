"""What the person is shown for a finished item: the review as shown, and the result with the shown
confidence (C-19). The confidence rule itself is ``scripts/confidence.py`` (the contract "confidence"),
including which review counts; result.json keeps what the worker wrote.
"""

from __future__ import annotations

from typing import Any

from . import rules


def effective(result: dict[str, Any], review: Any = None, verify: dict[str, Any] | None = None,
              item: dict[str, Any] | None = None) -> dict[str, Any]:
    """A copy of ``result`` with the shown confidence and, when it was lowered, the reasons.

    ``review`` is review.json as read (or None) and ``item`` the item of run.json: the rule itself decides
    whether the review counts (once this run took it in)."""
    shown = rules.effective_confidence(result, review, verify, item)
    if shown["confidence"] == result.get("confidence") and not shown["lowered_by"]:
        return result
    return {**result, "confidence": shown["confidence"], "confidence_reason": shown["reason"]}


def shown_review(item: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any] | None:
    """The review as the person sees it: a review sent back as often as allowed counts as a flag."""
    if item.get("review") == "unverified":
        return {"outcome": "unverified", "reason": "確かめられませんでした", "findings": []}
    if not item.get("review") or not review:
        return None
    if item.get("review") == "flag" and review.get("outcome") == "rework":
        return {**review, "outcome": "flag", "reason": "作り直しの上限まで差し戻しても、指摘が残っています"}
    return review
