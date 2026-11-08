"""The warnings of an item as the briefs and reports show them: repository-relative paths, the warning
path of each CID (``items/<ID>/issue-<CID>.json``, design 4.4) and notes about the paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import RunError, read_json
from .pathmap import PathMapper
from .state import Run

ISSUE_KEYS = ("cid", "checker", "file", "function", "line", "impact", "category", "cwe", "merge_key", "macro",
              "stream", "classification", "action", "severity", "status")


def check_issues_file(data: Any) -> list[str]:
    """Problems of an issues.json (design 4.3) this skill relies on."""
    if not isinstance(data, dict):
        return ["一番外側は JSON のオブジェクトにしてください"]
    problems = []
    if not data.get("finished_at"):
        problems.append("finished_at がありません（取得の途中のファイルは使えません）")
    issues = data.get("issues")
    if not isinstance(issues, list):
        return problems + ["issues（警告の配列）がありません"]
    seen = set()
    for n, issue in enumerate(issues):
        if not isinstance(issue, dict):
            problems.append(f"issues[{n}] はオブジェクトにしてください")
            continue
        cid = issue.get("cid")
        if isinstance(cid, bool) or not isinstance(cid, int):
            problems.append(f"issues[{n}].cid は整数にしてください")
        elif cid in seen:
            problems.append(f"issues[{n}].cid {cid} が 2 回あります")
        seen.add(cid)
        for key in ("checker", "file"):
            if not isinstance(issue.get(key), str) or not issue.get(key):
                problems.append(f"issues[{n}].{key} がありません")
        if issue.get("line") is not None and (isinstance(issue["line"], bool) or not isinstance(issue["line"], int)):
            problems.append(f"issues[{n}].line は整数にしてください")
    return problems


def clean_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {k: issue[k] for k in ISSUE_KEYS if issue.get(k) is not None}


def mapped(run: Run) -> dict[int, dict[str, Any]]:
    data = read_json(run.dir / "issues.mapped.json", [])
    return {d["cid"]: d for d in data if isinstance(d, dict) and "cid" in d}


def detail(run: Run, item_id: str, cid: int) -> dict[str, Any]:
    """The issue file of a CID: ``{issue, events, checker_description}``, or ``{error}``."""
    path = run.item_dir(item_id) / f"issue-{cid}.json"
    try:
        data = read_json(path)
    except RunError as exc:
        return {"error": str(exc) if path.is_file() else "警告経路のファイルがありません"}
    if not isinstance(data, dict):
        return {"error": "警告経路のファイルの形が正しくありません"}
    return data


def view(run: Run, item: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]], str, list[str]]:
    """(issues, events by CID, checker description, notes) with repository-relative paths."""
    issues_by_cid = mapped(run)
    mapper = PathMapper(run.repo, run.settings["coverity"]["path_strip_prefixes"])
    notes: list[str] = []
    events: dict[int, list[dict[str, Any]]] = {}
    description = ""
    issues = []
    for cid in item["cids"]:
        issue = issues_by_cid.get(cid) or {"cid": cid, "checker": "-", "file": "-"}
        issues.append(issue)
        notes += [n for n in issue.get("path_notes") or [] if n not in notes]
        data = detail(run, item["id"], cid)
        if data.get("error") and not data.get("events"):
            notes.append(f"CID {cid} の警告経路を取れませんでした: {data['error']}")
        evs = []
        for event in data.get("events") or []:
            if not isinstance(event, dict):
                continue
            evs.append({"file": mapper.mapped(str(event.get("file") or issue.get("file") or "-"), notes),
                        "line": event.get("line") if isinstance(event.get("line"), int) else None,
                        "tag": str(event.get("tag") or ""), "description": str(event.get("description") or ""),
                        "main": bool(event.get("main"))})
        events[cid] = evs
        description = description or str(data.get("checker_description") or "")
    return issues, events, description, notes


def read_lines(root: Path, rel: str) -> list[str] | None:
    path = root / rel
    if not path.is_file():
        return None
    data = path.read_bytes()
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding).splitlines()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace").splitlines()


def read_text(root: Path, rel: str) -> str | None:
    lines = read_lines(root, rel)
    return None if lines is None else "\n".join(lines) + ("\n" if lines else "")


def excerpt(root: Path, events: list[dict[str, Any]], issue: dict[str, Any], context: int = 4,
            limit: int = 120) -> list[str]:
    """The code around the events and the warning line, per file, with ★ on the lines where the problem occurs."""
    wanted: dict[str, set[int]] = {}
    stars: dict[str, set[int]] = {}
    for event in events:
        if event.get("line"):
            wanted.setdefault(event["file"], set()).add(event["line"])
            if event.get("main"):
                stars.setdefault(event["file"], set()).add(event["line"])
    if issue.get("line") and issue.get("file"):
        wanted.setdefault(issue["file"], set()).add(issue["line"])
        if not any(e.get("main") for e in events):
            stars.setdefault(issue["file"], set()).add(issue["line"])
    out: list[str] = []
    used = 0
    for rel, lines_wanted in wanted.items():
        lines = read_lines(root, rel)
        if lines is None:
            out += [f"`{rel}`: （調べるコードにこのファイルがありません）", ""]
            continue
        shown: set[int] = set()
        for line in lines_wanted:
            shown |= set(range(max(1, line - context), min(len(lines), line + context) + 1))
        if not shown:
            out += [f"`{rel}`: （警告の行 {sorted(lines_wanted)} がファイルの範囲の外です。{len(lines)} 行まで）", ""]
            continue
        block = []
        last = None
        for number in sorted(shown):
            if used >= limit:
                block.append("…（長いため、ここまで）")
                break
            if last is not None and number != last + 1:
                block.append("…")
            mark = "★" if number in stars.get(rel, set()) else " "
            block.append(f"{mark}{number:5d} | {lines[number - 1]}")
            last = number
            used += 1
        fence = "`" * max(3, max((len(m) for m in _ticks("\n".join(block))), default=0) + 1)
        out += [f"`{rel}`", "", fence + "text", *block, fence, ""]
    return out


def _ticks(text: str) -> list[str]:
    import re

    return re.findall(r"`+", text)
