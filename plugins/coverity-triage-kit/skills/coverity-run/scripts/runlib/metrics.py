"""Option options.metrics: ``metrics.json``, what the run knows about each finished item (made by summary).

``{"version": 1, "run_id", "created_at", "items": [{item, cids, recommendation, confidence, shown_confidence,
judgement, seconds, attempts, rework_rounds, review, build_ok}], "totals": {...}}``. The person's
approvals are not in it (they are in approvals.json).
"""

from __future__ import annotations

from typing import Any

from .common import now_iso, try_json, write_json
from .state import Run


def write(run: Run, rows: list[dict[str, Any]]) -> dict[str, Any]:
    verify = try_json(run.dir / "verify.json") or {}
    by_id = {i["id"]: i for i in run.state["items"]}
    items = []
    for row in rows:
        item = by_id[row["id"]]
        raw = try_json(run.result_file(row["id"])) or {}
        items.append({"item": row["id"], "cids": row["cids"], "recommendation": raw.get("recommendation"),
                      "confidence": raw.get("confidence"), "shown_confidence": row["confidence"],
                      "judgement": row["judgement"], "seconds": item.get("seconds"), "attempts": item.get("attempts"),
                      "rework_rounds": item.get("review_rounds", 0), "review": row.get("review"),
                      "build_ok": (verify.get(row["id"]) or {}).get("build_ok") if isinstance(verify, dict) else None})
    seconds = [i["seconds"] for i in items if isinstance(i["seconds"], (int, float))]
    totals = {"items": len(items), "cids": sum(len(i["cids"]) for i in items),
              "fix": sum(1 for i in items if i["recommendation"] == "fix"),
              "deviation": sum(1 for i in items if i["recommendation"] == "deviation"),
              "lowered": sum(1 for i in items if i["confidence"] != i["shown_confidence"]),
              "errors": sum(1 for i in run.state["items"] if i["status"] == "error"),
              "average_seconds": round(sum(seconds) / len(seconds), 1) if seconds else None}
    data = {"version": 1, "run_id": run.state["run_id"], "created_at": now_iso(), "items": items, "totals": totals}
    write_json(run.dir / "metrics.json", data)
    return data
