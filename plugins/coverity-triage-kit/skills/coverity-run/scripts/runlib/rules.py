"""The fixed checks of what the agents write: result.json, review.json, plan.json, and the points the
reviewer is shown. The confidence shown to people is in ``confidence.py``.

Every rule here is another part's script, copied byte for byte into this skill's ``scripts/`` folder
(design 4.17): check_result.py (result rules, with the deviation comment rules inside), check_review.py,
lint_result.py and check_plan.py (plan rules, with the comment rules inside); check_comment.py is the
comment rules on their own. This module only loads those copies and turns their answers into the plain
messages tick writes into the briefs. It holds no rule of its own: change a rule in its owner and copy it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

from .common import ASSETS_DIR, SCRIPTS_DIR

COPIES = ("check_comment", "check_result", "check_review", "lint_result", "check_plan", "confidence")


@lru_cache(maxsize=None)
def script(name: str) -> ModuleType:
    """One of the copied scripts in this skill's scripts/ folder, imported by its path."""
    if name not in COPIES:
        raise ValueError(name)
    module_name = f"_copy_{name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@lru_cache(maxsize=None)
def asset(name: str) -> dict[str, Any]:
    return json.loads((ASSETS_DIR / name).read_text(encoding="utf-8"))


def _messages(found: list[dict[str, Any]]) -> list[str]:
    return [str(p.get("message") or p.get("code")) for p in found]


def item_criteria() -> list[dict[str, Any]]:
    """The criteria the reviewer checks for each item (scope: item), in their order."""
    return [c for c in asset("criteria.json")["criteria"] if c.get("scope") == "item"]


def max_cids() -> int:
    """How many CIDs one merge of the integration may hold."""
    return int(script("check_plan").MAX_CIDS)


# ---- result.json (contract "result", with the comment rules) -----------------------------------


def result_check(result: Any, item: str | None = None, cids: list[int] | None = None) -> dict[str, Any]:
    """``{ok, problems, warnings}`` (messages) for a result.json. ``cids``: the item's CIDs (more than one: a group)."""
    out = script("check_result").check_result(result, asset("result.schema.json"), item=item, cids=cids)
    return {"ok": out["ok"], "problems": _messages(out["problems"]), "warnings": _messages(out["warnings"])}


# ---- review.json (the review rules: every scope:item criterion once) ---------------------------


def applicability(checker: str | None, merged: bool) -> dict[str, bool | None]:
    """Criterion id -> True (applies to this item) / False (write ok: true and 対象外) / None (cannot tell)."""
    return script("check_review").applicability(item_criteria(), checker, merged)


def review_check(review: Any, item: str, checker: str | None, merged: bool) -> dict[str, Any]:
    """``{ok, problems, warnings}`` (messages) for a review.json of the item."""
    out = script("check_review").check_review(review, asset("review.schema.json"), asset("criteria.json"),
                                              item=item, checker=checker, merged=merged)
    return {"ok": out["ok"], "problems": _messages(out["problems"]), "warnings": _messages(out["warnings"])}


def not_applicable_word() -> str:
    return str(script("check_review").NOT_APPLICABLE)


# ---- the points shown to the reviewer (contract "lint") ------------------------------------------


def lint_findings(result: dict[str, Any], code_root: Path) -> list[dict[str, Any]]:
    """``[{code, message, file?, line?}]`` found by fixed rules in the code the worker read."""
    return script("lint_result").lint(result if isinstance(result, dict) else {}, Path(code_root))


def lint_points(findings: list[dict[str, Any]]) -> list[str]:
    """The sorted unique codes of the findings (what the contract compares)."""
    return script("lint_result").points(findings)


# ---- integration/plan.json (contract "plan", with the comment rules) -----------------------------


def plan_check(plan: Any, items: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    """``{ok, problems, warnings}`` (messages). ``items``: ``{id: {cids, status, confidence, no_grouping}}``."""
    out = script("check_plan").check_plan(plan, asset("plan.schema.json"), items)
    return {"ok": out["ok"], "problems": _messages(out["problems"]), "warnings": _messages(out["warnings"])}


# ---- the confidence shown to people (contract "confidence", owned by this skill) ------------------


def effective_confidence(result: Any, review: Any = None, verify: Any = None) -> dict[str, Any]:
    """``{confidence, reason, lowered_by}``: the lowest of the worker's, the review's cap and the build check's."""
    return script("confidence").effective_confidence(result, review, verify)
