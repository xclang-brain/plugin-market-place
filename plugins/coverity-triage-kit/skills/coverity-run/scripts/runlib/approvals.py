"""Approvals ``approvals.json`` (design 4.9): this skill puts in the defaults, the person chooses on a screen.

The default is the recommendation, except a fix with nothing to take in (the latest revision is already
fixed, so there is no diff), which is null (not applied). Existing entries are never changed: only
missing items are added. A human's choice always stays: a file that cannot be read or does not have the
form of assets/approvals.schema.json is never written over (the command fails and names the file).
"""

from __future__ import annotations

from typing import Any

from . import schema
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
    for key, entry in (items if isinstance(items, dict) else {}).items():
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


def broken(file) -> RunError:
    return RunError(f"承認のファイル {file} が壊れているため、書き換えません（人が選んだ承認を消さないため）。"
                    "ファイルを直すか、選んだ承認が要らなければファイルを消してから、もう一度動かしてください",
                    approvals_file=str(file))


def ensure_defaults(run: Run, results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Add the recommendation for finished items that have no entry yet; keep every existing entry.
    A broken approvals.json is refused (RunError), never replaced."""
    file = path(run)
    with file_lock(file):
        if file.is_file():
            try:
                data = read_json(file)
            except RunError:
                raise broken(file) from None
            problems = schema.validate(data, "approvals.schema.json")
            if problems:
                error = broken(file)
                error.details["problems"] = problems
                raise error
        else:
            data = {"version": VERSION, "updated_at": now_iso(), "items": {}}
        items = data["items"]
        added = False
        for item_id, result in results.items():
            if item_id not in items:
                items[item_id] = {"decision": default_decision(run, item_id, result), "edited": False,
                                  "updated_at": now_iso()}
                added = True
        if added or not file.is_file():
            data["updated_at"] = now_iso()
            write_json(file, data)
    return load(run)


def tally(decided: list[dict[str, Any]]) -> dict[str, int]:
    out = {k: sum(1 for d in decided if d.get("decision") == k) for k in DECISIONS}
    out["none"] = sum(1 for d in decided if d.get("decision") not in DECISIONS)
    out["changed"] = sum(1 for d in decided if d.get("edited"))
    return out
