"""Shared parts of the mock commands: banners, the intermediate directory, paths, the warning tree."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any

EMIT = "emit"
OUTPUT = "output"
TUS_FILE = "tus.json"
ISSUES_FILE = "emulator-issues.json"


def version() -> str:
    return os.environ.get("COV_EMU_VERSION", "2024.12.1")


def banner(tool: str) -> str:
    v = version()
    return (f"Coverity {tool} version {v} on {platform.system()} {platform.release()} {platform.machine()}\n"
            f"Internal version numbers: e0e0e0e0e0 p-{'.'.join(v.split('.')[:2])}-push-1\n")


def ticker(mode: str) -> str:
    if mode == "none":
        return ""
    return "|0----------25-----------50----------75---------100|\n****************************************************\n"


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def path_map_from_env() -> list[tuple[str, str]]:
    out = []
    for item in (os.environ.get("COV_EMU_PATH_MAP") or "").split(";"):
        if "=" in item:
            local, remote = item.split("=", 1)
            out.append((os.path.normpath(local), remote.rstrip("/") or "/"))
    return out


def map_path(path: str, mapping: list[tuple[str, str]]) -> str:
    norm = os.path.normpath(path)
    for local, remote in mapping:
        if norm == local or norm.startswith(local + os.sep):
            return remote + norm[len(local):].replace(os.sep, "/")
    return norm.replace(os.sep, "/")


def strip_path(path: str, prefixes: list[str]) -> str:
    """--strip-path: the official example keeps the leading "/" (/test on /test/me/a.c gives /me/a.c);
    COV_EMU_STRIP_KEEP_SLASH=0 drops it as in the real 2020.06 output (main.cpp)."""
    keep = os.environ.get("COV_EMU_STRIP_KEEP_SLASH", "1") != "0"
    for p in sorted(prefixes, key=len, reverse=True):
        base = p.rstrip("/")
        if path == base or path.startswith(base + "/"):
            rest = path[len(base):]
            return rest if keep else rest.lstrip("/")
    return path


def cov_lstr(desc: str) -> str:
    """covLStrEventDescription from the plain description: quoted code becomes {0}, {1}, ..."""
    args: list[str] = []

    def repl(m: re.Match) -> str:
        args.append(m.group(1))
        return "{" + str(len(args) - 1) + "}"
    template = re.sub(r'"([^"]*)"', repl, desc)
    return "{CovLStrv2{{t{" + template + "}" + "".join("{{code{" + a + "}}}" for a in args) + "}}}"


def build_tree(flat: list[dict], mapping: list[tuple[str, str]], strip: list[str]) -> list[dict]:
    """Nested JSON v7 events (13 keys, confirmed order) from flat events with a depth."""
    root: list[dict] = []
    stack: list[tuple[int, list[dict]]] = [(-1, root)]
    for e in flat:
        depth = max(0, int(e.get("depth", 0)))
        while len(stack) > 1 and stack[-1][0] >= depth:
            stack.pop()
        siblings = stack[-1][1]
        if depth > stack[-1][0] + 1 and siblings:  # a gap in depth: attach under the last sibling
            depth = stack[-1][0] + 1
        remote = map_path(e["file"], mapping)
        node = {"covLStrEventDescription": cov_lstr(e["desc"]), "eventDescription": e["desc"],
                "eventNumber": len(siblings) + 1, "eventTreePosition": "", "eventSet": 0, "eventTag": e["tag"],
                "filePathname": remote, "strippedFilePathname": strip_path(remote, strip), "lineNumber": e["line"],
                "main": bool(e.get("main")) and depth == 0, "moreInformationId": None,
                "remediation": e["tag"] == "remediation", "events": None, "eventKind": e.get("kind", "NORMAL")}
        siblings.append(node)
        node_children: list[dict] = []
        node["_children"] = node_children
        stack.append((depth, node_children))

    def finish(nodes: list[dict], prefix: str) -> None:
        for n in nodes:
            n["eventTreePosition"] = f"{prefix}{n['eventNumber']}"
            children = n.pop("_children")
            n["events"] = children or None
            if children:
                n["eventKind"] = "MODEL"
                finish(children, n["eventTreePosition"] + ".")
    finish(root, "")
    if root and not any(n["main"] for n in root):
        root[-1]["main"] = True
    return root


V7_ISSUE_KEYS = ["mergeKey", "occurrenceCountForMK", "occurrenceNumberInMK", "referenceOccurrenceCountForMK",
                 "checkerName", "subcategory", "type", "subtype", "code-language", "extra", "domain", "language",
                 "mainEventFilePathname", "strippedMainEventFilePathname", "mainEventLineNumber", "properties",
                 "functionDisplayName", "functionMangledName", "localStatus", "ordered", "events", "stateOnServer",
                 "checkerProperties"]
V7_EVENT_KEYS = ["covLStrEventDescription", "eventDescription", "eventNumber", "eventTreePosition", "eventSet",
                 "eventTag", "filePathname", "strippedFilePathname", "lineNumber", "main", "moreInformationId",
                 "remediation", "events"]


def to_v7_event(e: dict) -> dict:
    out = {k: e.get(k) for k in V7_EVENT_KEYS}
    out["events"] = [to_v7_event(c) for c in e["events"]] if e.get("events") else None
    return out


def to_v7_issue(issue: dict, strip: list[str] | None = None) -> dict:
    out = {}
    for k in V7_ISSUE_KEYS:
        if k == "subtype" and "subtype" not in issue:
            continue  # some checkers have no subtype key at all (PW.*), confirmed
        out[k] = issue.get(k)
    out["events"] = [to_v7_event(e) for e in issue["events"]]
    if strip:
        out["strippedMainEventFilePathname"] = strip_path(out["strippedMainEventFilePathname"], strip)

        def restrip(events: list[dict] | None) -> None:
            for e in events or []:
                e["strippedFilePathname"] = strip_path(e["strippedFilePathname"], strip)
                restrip(e["events"])
        restrip(out["events"])
    return out


def die(message: str, code: int) -> None:
    sys.stdout.write(message.rstrip("\n") + "\n")
    sys.stdout.flush()
    sys.exit(code)
