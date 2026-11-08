"""Grouping of CIDs that likely share one cause (REQ-506).

Two CIDs join when they have the same checker and the same macro, the same file and function, or
the same file and line (union-find). CIDs of no-grouping.yaml never join. A cluster is cut into
items of at most 10 CIDs; a single CID is the item ``"<cid>"``, a group is ``G1``, ``G2``, ... in the
order of the issues. The worker moves CIDs whose cause is different out of a group.
"""

from __future__ import annotations

from typing import Any

MAX_GROUP = 10


def _keys(issue: dict[str, Any]) -> list[tuple[Any, ...]]:
    keys: list[tuple[Any, ...]] = []
    checker, file = issue.get("checker"), issue.get("file")
    if issue.get("macro"):
        keys.append(("macro", checker, issue["macro"]))
    if issue.get("function"):
        keys.append(("function", checker, file, issue["function"]))
    if issue.get("line") is not None:
        keys.append(("line", checker, file, issue["line"]))
    return keys


def clusters(issues: list[dict[str, Any]], no_grouping: set[int] | None = None) -> list[list[int]]:
    no_grouping = no_grouping or set()
    parent = {issue["cid"]: issue["cid"] for issue in issues}

    def find(cid: int) -> int:
        while parent[cid] != cid:
            parent[cid] = parent[parent[cid]]
            cid = parent[cid]
        return cid

    owner: dict[tuple[Any, ...], int] = {}
    for issue in issues:
        if issue["cid"] in no_grouping:
            continue
        for key in _keys(issue):
            if key in owner:
                parent[find(issue["cid"])] = find(owner[key])
            else:
                owner[key] = issue["cid"]
    members: dict[int, list[int]] = {}
    for issue in issues:
        members.setdefault(find(issue["cid"]), [])
        if issue["cid"] not in members[find(issue["cid"])]:
            members[find(issue["cid"])].append(issue["cid"])
    return list(members.values())


def build_work_items(issues: list[dict[str, Any]], no_grouping: set[int] | None = None,
                     first_group: int = 1) -> list[dict[str, Any]]:
    """``[{id, cids, cluster}]`` in the order of the issues (the first CID of each item)."""
    items: list[dict[str, Any]] = []
    group_no = first_group - 1
    for number, cluster in enumerate(clusters(issues, no_grouping), 1):
        for start in range(0, len(cluster), MAX_GROUP):
            cids = cluster[start:start + MAX_GROUP]
            if len(cids) == 1:
                items.append({"id": str(cids[0]), "cids": cids, "cluster": number})
            else:
                group_no += 1
                items.append({"id": f"G{group_no}", "cids": cids, "cluster": number})
    return items
