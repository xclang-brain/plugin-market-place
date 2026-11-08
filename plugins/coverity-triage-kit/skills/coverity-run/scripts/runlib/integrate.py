"""Option agents.integration: candidates of items with one cause (fixed rules; the integrator decides),
and applying the integrator's plan (design 4.7)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import rules
from .common import read_json, write_json
from .config import load_no_grouping
from .state import Run, new_item

HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? ", re.MULTILINE)


def changed_ranges(patch: Path) -> dict[str, list[tuple[int, int]]]:
    """Lines of the original file each hunk changes, by file (git and svn unified diffs)."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    if not patch.is_file():
        return ranges
    current = None
    for line in patch.read_bytes().splitlines():
        if line.startswith(b"+++ "):
            name = line[4:].split(b"\t")[0].decode("utf-8", "replace").strip()
            current = name[2:] if name.startswith(("a/", "b/")) else name
        elif line.startswith(b"@@ ") and current:
            match = HUNK.match(line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or 1)
                ranges.setdefault(current, []).append((start, start + max(count, 1) - 1))
    return ranges


def _overlap(a: dict[str, list[tuple[int, int]]], b: dict[str, list[tuple[int, int]]]) -> bool:
    return any(x0 <= y1 and y0 <= x1 for f in a.keys() & b.keys() for x0, x1 in a[f] for y0, y1 in b[f])


def candidates(run: Run) -> list[dict[str, Any]]:
    no_grouping = load_no_grouping(run.repo)
    done = [i for i in run.state["items"] if i["status"] == "done" and not set(i["cids"]) & no_grouping]
    results = {i["id"]: read_json(run.result_file(i["id"]), {}) for i in done}
    ranges = {i["id"]: changed_ranges(run.patch_file(i["id"])) for i in done}
    reasons: dict[tuple[str, str], list[str]] = {}

    def link(a: str, b: str, why: str) -> None:
        key = (a, b) if a < b else (b, a)
        if why not in reasons.setdefault(key, []):
            reasons[key].append(why)

    def evidence(item_id: str) -> set[tuple[str, int]]:
        verdict = results[item_id].get("verdict") or {}
        return {(e.get("file"), e.get("line")) for e in verdict.get("evidence") or [] if e.get("line")}

    for n, a in enumerate(done):
        for b in done[n + 1:]:
            if a.get("cluster") and a.get("cluster") == b.get("cluster"):
                link(a["id"], b["id"], "グループ分けで同じまとまりだった（10 件ずつに分けた）")
            if (a.get("split_from") and a["split_from"] in (b.get("split_from"), b["id"])) or \
                    (b.get("split_from") and b["split_from"] == a["id"]):
                link(a["id"], b["id"], "グループから外された CID と、元のグループ（またはその仲間）")
            if _overlap(ranges[a["id"]], ranges[b["id"]]):
                link(a["id"], b["id"], "修正が同じファイルの同じ箇所を変えている")
            if evidence(a["id"]) & evidence(b["id"]):
                link(a["id"], b["id"], "根拠の場所（ファイルと行）が同じ")
    parent = {i["id"]: i["id"] for i in done}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in reasons:
        parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for item in done:
        if any(item["id"] in pair for pair in reasons):
            groups.setdefault(find(item["id"]), []).append(item["id"])
    return [{"items": members, "reasons": {f"{a}-{b}": why for (a, b), why in reasons.items()
                                           if a in members and b in members}}
            for members in groups.values()]


def plan_items(run: Run) -> dict[str, dict[str, Any]]:
    """The ``items`` argument of rules.plan_check."""
    no_grouping = load_no_grouping(run.repo)
    out = {}
    for item in run.state["items"]:
        result = read_json(run.result_file(item["id"]), {}) if item["status"] == "done" else {}
        out[item["id"]] = {"cids": item["cids"], "status": item["status"], "confidence": result.get("confidence"),
                           "no_grouping": bool(set(item["cids"]) & no_grouping)}
    return out


def check(run: Run) -> tuple[dict[str, Any] | None, list[str]]:
    """The plan and its problems (the contract "plan", which also holds the comment rules of each merge)."""
    path = run.dir / "integration" / "plan.json"
    plan = read_json(path, {}) if path.is_file() else None
    if plan is None:
        return None, [f"計画のファイル {path} がありません"]
    return plan, rules.plan_check(plan, plan_items(run))["problems"]


def apply(run: Run, plan: dict[str, Any], state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Make one item of each merge inside ``state`` (an open run.update()). Returns the new items with
    their fix_source, and the warnings of conflicts."""
    items = {i["id"]: i for i in state["items"]}
    merged_file = run.dir / "integration" / "merged.json"
    merged_info = read_json(merged_file, {})
    numbers = [int(i["id"][1:]) for i in state["items"] if re.fullmatch(r"G\d+", i["id"])]
    made = []
    for n, merge in enumerate(plan.get("merges") or [], 1):
        new_id = f"G{max(numbers, default=0) + n}"
        members = list(dict.fromkeys(merge["members"]))
        folder = run.item_dir(new_id)
        folder.mkdir(parents=True, exist_ok=True)
        for member in members:
            for cid in items[member]["cids"]:
                src = run.item_dir(member) / f"issue-{cid}.json"
                if src.is_file():
                    (folder / src.name).write_bytes(src.read_bytes())
        result = dict(merge["result"])
        result.pop("item", None)
        result.pop("group_excluded_cids", None)
        merged_info[new_id] = {"members": members, "reason": merge["reason"], "fix_source": merge["fix_source"],
                               "result": result}
        cids = [c for m in members for c in items[m]["cids"]]
        state["items"].append(new_item(new_id, cids, merged_from=members))
        for member in members:
            items[member]["status"], items[member]["merged_into"] = "merged", new_id
        made.append({"item": new_id, "members": members, "source": merge["fix_source"]})
    warnings = []
    for conflict in plan.get("conflicts") or []:
        for item_id in conflict["items"]:
            if item_id in items:
                text = (f"修正が作業 {', '.join(x for x in conflict['items'] if x != item_id)} と同じ箇所を変えています"
                        f"（原因は別）: {conflict['note']}")
                items[item_id].setdefault("warnings", []).append(text)
                warnings.append(f"{item_id}: {text}")
    write_json(merged_file, merged_info)
    return made, warnings


def undo(state: dict[str, Any], item_id: str) -> list[str]:
    """A merged item that could not be made gives its members back as they were."""
    item = next((i for i in state["items"] if i["id"] == item_id), None)
    if item is None or not item.get("merged_from"):
        return []
    for member in state["items"]:
        if member["status"] == "merged" and member.get("merged_into") == item_id:
            member["status"], member["merged_into"] = "done", None
    state["items"] = [i for i in state["items"] if i["id"] != item_id]
    return list(item["merged_from"])
