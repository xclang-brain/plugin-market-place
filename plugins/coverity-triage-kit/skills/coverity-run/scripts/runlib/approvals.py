"""Approvals ``approvals.json`` (design 4.9): this skill puts in the defaults, the person chooses on a screen.

The default is the recommendation, except a fix with nothing to take in (the latest revision is already
fixed, so there is no diff), which is null (not applied). Existing entries are never changed: only
missing items are added. A human's choice always stays.
"""

from __future__ import annotations

from typing import Any

from .common import RunError, file_lock, now_iso, read_json, write_json
from .state import Run

FILE = "approvals.json"
VERSION = 1
DECISIONS = ("fix", "deviation", "reject")


def path(run: Run):
    return run.dir / FILE


def load(run: Run) -> dict[str, dict[str, Any]]:
    """``{item id: entry}``; values that cannot be used are dropped (never applied)."""
    try:
        data = read_json(path(run), {})
    except RunError:
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    out = {}
    for key, entry in (items or {}).items():
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        if entry.get("decision") not in DECISIONS:
            entry["decision"] = None
        entry["edited"] = bool(entry.get("edited"))
        out[str(key)] = entry
    return out


def default_decision(run: Run, item_id: str, result: dict[str, Any]) -> str | None:
    fix = result.get("fix") or {}
    if result.get("recommendation") == "fix" and fix.get("already_fixed_on_latest") and \
            not read_json(run.item_dir(item_id) / "saved.json", {}).get("fix"):
        return None
    return result.get("recommendation")


def ensure_defaults(run: Run, results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Add the recommendation for finished items that have no entry yet; keep every existing entry."""
    with file_lock(path(run)):
        try:
            data = read_json(path(run), {})
        except RunError:
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            data = {"version": VERSION, "updated_at": now_iso(), "items": {}}
        items = data["items"]
        added = False
        for item_id, result in results.items():
            if item_id not in items:
                items[item_id] = {"decision": default_decision(run, item_id, result), "edited": False,
                                  "updated_at": now_iso()}
                added = True
        if added or not path(run).is_file():
            data["version"] = data.get("version", VERSION)
            data["updated_at"] = now_iso()
            write_json(path(run), data)
    return load(run)


def tally(decided: list[dict[str, Any]]) -> dict[str, int]:
    out = {k: sum(1 for d in decided if d.get("decision") == k) for k in DECISIONS}
    out["none"] = sum(1 for d in decided if d.get("decision") not in DECISIONS)
    out["changed"] = sum(1 for d in decided if d.get("edited"))
    return out
