"""REST API v2 of the emulated Coverity Connect (specification 2.1 - 2.4).

Every handler takes the store, the query (dict of lists), the parsed JSON body and the user name,
and returns a :class:`Response` or raises :class:`ApiError`.
"""

from __future__ import annotations

import fnmatch
import json
import random
import re
import time
from collections import Counter
from typing import Any, Callable
from urllib.parse import unquote

from . import values as V
from .common import Response, json_response
from .config import version_tuple
from .state import (ApiError, InvalidKey, Store, fmt_detection, fmt_history, fmt_iso_z, fmt_last_triaged,
                    fmt_mmddyy)

DEFAULT_SEARCH_COLUMNS = ["cid", "displayType", "displayImpact", "status", "firstDetected", "classification",
                          "severity", "action", "displayComponent", "displayCategory", "displayFile",
                          "displayFunction", "checker"]
NUMERIC_COLUMNS = {"cid", "cwe", "lineNumber", "occurrenceCount", "firstSnapshotId", "lastDetectedId"}


def q(query: dict[str, list[str]], name: str, default: str | None = None) -> str | None:
    values = query.get(name)
    return values[0] if values else default


def _need(store: Store, version: str) -> None:
    if version_tuple(store.config["server_version"]) < version_tuple(version):
        raise ApiError(404, None, "Not Found")


# ---- views -----------------------------------------------------------------------------------

def stream_json(store: Store, name: str) -> dict:
    s = store.state["streams"][name]
    return {"name": name, "primaryProjectName": s["primaryProjectName"], "description": s.get("description", ""),
            "allowCommitWithoutPassword": None, "analysisVersionOverride": None, "autoDeleteOnExpiry": False,
            "componentMapName": "Default", "enableDesktopAnalysis": True, "language": s.get("language", "MIXED"),
            "outdated": False, "ownerAssignmentOption": "default_component_owner", "pluginVersionOverride": None,
            "roleAssignments": [{"group": {"domainName": None, "ldapServer": None, "name": "Users"},
                                 "roleAssignmentType": "group", "roleName": "developer", "scope": "stream",
                                 "username": None}],
            "summaryExpirationDays": None, "triageStoreName": s["triageStoreName"], "versionMismatchMessage": None}


def project_streams(store: Store, project: str) -> list[str]:
    p = store.state["projects"].get(project)
    if p is None:
        return []
    primary = [n for n, s in store.state["streams"].items() if s["primaryProjectName"] == project]
    return sorted(set(primary) | set(p.get("streamLinks", [])))


def _issue_kind(store: Store, kinds: list[str]) -> str:
    shown = [V.ISSUE_KIND_DISPLAY.get(k, k) for k in kinds]
    if len(shown) > 1:
        return store.config["issue_kind_both"]
    return shown[0] if shown else ""


def issue_view(store: Store, cid: int, contexts: list[tuple[str, dict]]) -> dict[str, Any]:
    """All column values of a CID (English keys), from the snapshots that the scope selected.

    ``contexts`` is a list of (stream, snapshot) in which the CID was found."""
    rec = store.state["cids"][str(cid)]
    props = rec.get("checkerProperties") or {}
    streams = sorted({s for s, _ in contexts})
    latest_stream, latest_snap = max(contexts, key=lambda c: (c[1]["dateCreated"], c[1]["id"]))
    occs = latest_snap["occurrences"][str(cid)]
    occ = occs[-1] if store.config["rest_line_from_occurrence"] == "last" else occs[0]
    all_detected = sorted((snap for s in streams for snap in store.detected_in(cid, s)),
                          key=lambda x: (x["dateCreated"], x["id"]))
    first = all_detected[0] if all_detected else latest_snap
    last = all_detected[-1] if all_detected else latest_snap

    def triage_values(name: str) -> str:
        vals = {store.triage_of(store.state["streams"][s]["triageStoreName"], cid)["attrs"][name] for s in streams}
        return vals.pop() if len(vals) == 1 else "Various"

    statuses = {store.status(cid, s) for s in streams}
    status = statuses.pop() if len(statuses) == 1 else "Various"
    triage = store.triage_of(store.state["streams"][streams[0]]["triageStoreName"], cid)
    owner = triage_values("Owner")
    owner_user = store.state["users"].get(owner) or {}
    cwe = props.get("cweCategory")
    view = {
        "cid": str(cid), "checker": rec["checkerName"], "displayImpact": props.get("impact"),
        "displayCategory": props.get("category"), "displayType": props.get("subcategoryShortDescription"),
        "cwe": cwe if cwe and cwe != "none" else None,
        "displayIssueKind": _issue_kind(store, props.get("issueKinds") or []),
        "status": status, "firstDetected": fmt_mmddyy(first["dateCreated"]),
        "owner": owner, "ownerFullName": owner_user.get("fullName") or None,
        "externalReference": triage_values("Ext. Reference") or None,
        "classification": triage_values("Classification"), "severity": triage_values("Severity"),
        "action": triage_values("Action"), "fixTarget": triage_values("Fix Target"),
        "legacy": triage_values("Legacy"), "displayComponent": rec.get("component") or "Other",
        "displayFile": occ["file"], "displayFunction": rec.get("functionDisplayName"),
        "functionMergeName": rec.get("functionMergeName"), "mergeExtra": rec.get("extra") or None,
        "mergeKey": rec["mergeKey"], "fileLanguage": rec.get("language"),
        "ruleStrength": props.get("MISRACategory"), "lastTriaged": fmt_last_triaged(triage["lastTriaged"]) or None,
        "lastTriagedUser": triage["lastTriagedUser"], "occurrenceCount": str(len(occs)),
        "displayComparison": None, "firstSnapshotDate": fmt_mmddyy(first["dateCreated"]),
        "firstSnapshotId": str(first["id"]), "firstSnapshotVersion": first.get("sourceVersion"),
        "firstSnapshotTarget": first.get("target"), "firstSnapshotDescription": first.get("description"),
        "firstSnapshotStream": first["stream"], "lastDetected": fmt_mmddyy(last["dateCreated"]),
        "lastDetectedId": str(last["id"]), "lastDetectedVersion": last.get("sourceVersion"),
        "lastDetectedTarget": last.get("target"), "lastDetectedDescription": last.get("description"),
        "lastDetectedStream": last["stream"], "score": None,
        "lineNumber": str(occ["line"]) if occ.get("line") is not None else None,
        "lastTriageComment": triage["comment"] or None,
        "project": store.state["streams"][latest_stream]["primaryProjectName"],
        "_kinds": props.get("issueKinds") or [], "_firstDetected": first["dateCreated"],
        "_projectKeys": {str(store.state["projects"][store.state["streams"][s]["primaryProjectName"]]["projectKey"])
                         for s in streams if store.state["streams"][s]["primaryProjectName"] in store.state["projects"]},
    }
    for key, _ in V.COLUMNS:
        view.setdefault(key, None)
    return view


# ---- search -------------------------------------------------------------------------------

def _known_keys(store: Store, column: str) -> list[str] | None:
    extra = store.config.get("attribute_values") or {}
    if column == "displayImpact":
        return V.IMPACTS
    if column == "classification":
        known = [c for c in V.CLASSIFICATIONS if store.config["test_advisor"] or c not in V.TEST_CLASSIFICATIONS]
        return known + extra.get("Classification", [])
    if column == "action":
        return V.ACTIONS + extra.get("Action", [])
    if column == "severity":
        return V.SEVERITIES + extra.get("Severity", []) + ["Various"]
    if column == "status":
        return V.STATUSES
    if column == "displayCategory":
        return V.CATEGORIES
    if column == "displayIssueKind":
        return V.ISSUE_KINDS
    if column == "fixTarget":
        return V.FIX_TARGETS + extra.get("Fix Target", [])
    if column == "legacy":
        return V.LEGACY
    if column == "checker":
        names = {c for c, _ in V.CHECKERS} | {r["checkerName"] for r in store.state["cids"].values()}
        return sorted(names)
    return None


def _check_key(store: Store, column: str, matcher: dict) -> None:
    """A keyMatcher with a value the server does not know is refused (the whole request, as the real server does)."""
    if matcher.get("type") in ("idMatcher", "nameMatcher", "dateMatcher"):
        return
    key = matcher.get("key")
    known = _known_keys(store, column)
    if known is not None and key not in known and not (
            column == "checker" and re.match(r"^(MISRA C|CERT |AUTOSAR )", str(key))):
        raise InvalidKey(str(key))


def _matches(store: Store, view: dict, column: str, matcher: dict) -> bool:
    kind = matcher.get("type")
    if kind == "idMatcher":
        wanted = str(matcher.get("id"))
        if column == "project":
            return wanted in view["_projectKeys"]
        return view.get(column) is not None and str(view.get(column)) == wanted
    if kind == "nameMatcher":
        name = matcher.get("name")
        if column == "displayComponent" or matcher.get("class") == "Component":
            return view.get("displayComponent") == name
        return view.get(column) == name
    if kind == "dateMatcher":
        date = matcher.get("date") or matcher.get("value")
        from .state import parse_time
        return view["_firstDetected"] >= parse_time(date)
    key = matcher.get("key")
    _check_key(store, column, matcher)
    if column == "displayIssueKind":
        return key in view["_kinds"]
    return view.get(column) == key or (column == "cid" and str(view.get("cid")) == str(key))


def search(store: Store, query: dict, body: Any, user: str) -> Response:
    conf = store.config
    try:
        offset = int(q(query, "offset", "0"))
        row_count = int(q(query, "rowCount", "200"))
    except ValueError as exc:
        raise ApiError(400, 1006, f"Invalid argument: {exc}") from exc
    if row_count == -1 and conf["rowcount_minus1"] == "400":
        raise ApiError(400, 1006, "rowCount must be between 1 and 1000")
    if row_count < -1 or offset < 0:
        raise ApiError(400, 1006, "Invalid argument passed to service")
    query_type = q(query, "queryType", "bySnapshot")
    if query_type not in ("bySnapshot", "byProject"):
        raise ApiError(400, 1006, f"Invalid queryType: {query_type}")
    locale = (q(query, "locale", "en_us") or "en_us").lower()
    body = body or {}
    if not isinstance(body, dict):
        raise ApiError(400, 1006, "Invalid request body")
    columns = body.get("columns") or DEFAULT_SEARCH_COLUMNS
    for c in columns:
        if c not in V.COLUMN_KEYS:
            raise ApiError(400, 1006, f"Invalid column key: {c}")
    scope = ((body.get("snapshotScope") or {}).get("show") or {}).get("scope")

    streams_wanted: set[str] | None = None
    streams_excluded: set[str] = set()
    projects_wanted: set[str] | None = None
    other: list[dict] = []
    for f in body.get("filters") or []:
        column, mode = f.get("columnKey"), f.get("matchMode", "oneOrMoreMatch")
        matchers = f.get("matchers") or []
        if column == "streams":
            names = {m.get("name") for m in matchers}
            for name in names:
                if name not in store.state["streams"] and conf["unknown_stream"] == "404":
                    raise ApiError(404, 1300, f"No stream found for name {name}.")
            if mode == "noneMatch":
                streams_excluded |= names
            else:
                streams_wanted = (streams_wanted or set()) | names
        elif column == "project":
            names = set()
            for m in matchers:
                if m.get("type") == "idMatcher":
                    names |= {n for n, p in store.state["projects"].items() if str(p["projectKey"]) == str(m.get("id"))}
                else:
                    names.add(m.get("name"))
            projects_wanted = (projects_wanted or set()) | names
        elif column not in V.COLUMN_KEYS:
            raise ApiError(400, 1006, f"Invalid column key: {column}")
        else:
            other.append(f)
    if conf["old_server_needs_project"] and streams_wanted is not None and projects_wanted is None:
        streams_wanted = None  # older servers: the stream filter has no effect without a project filter
    selected = set(store.state["streams"])
    if projects_wanted is not None:
        selected &= {s for p in projects_wanted for s in project_streams(store, p)}
    if streams_wanted is not None:
        selected &= streams_wanted
    selected -= streams_excluded

    contexts: dict[int, list[tuple[str, dict]]] = {}
    for stream in sorted(selected):
        snaps = store.stream_snapshots(stream) if query_type == "byProject" else store.select_snapshots(stream, scope)
        for snap in snaps:
            for cid in snap["occurrences"]:
                contexts.setdefault(int(cid), []).append((stream, snap))
    views = [issue_view(store, cid, ctx) for cid, ctx in contexts.items()]

    for f in other:
        column, mode = f["columnKey"], f.get("matchMode", "oneOrMoreMatch")
        matchers = f.get("matchers") or []
        if mode not in ("oneOrMoreMatch", "noneMatch"):
            raise ApiError(400, 1006, f"Invalid matchMode: {mode}")
        for m in matchers:  # every value is checked, not only those reached while matching (any() stops early)
            _check_key(store, column, m)
        kept = []
        for v in views:
            hit = any(_matches(store, v, column, m) for m in matchers)
            if hit == (mode == "oneOrMoreMatch"):
                kept.append(v)
        views = kept

    sort_column = q(query, "sortColumn")
    descending = (q(query, "sortOrder", "asc") or "asc").lower() == "desc"
    if sort_column:
        if sort_column not in V.COLUMN_KEYS:
            raise ApiError(400, 1006, f"Invalid sort column: {sort_column}")

        def sort_key(v: dict) -> tuple:
            value = v.get(sort_column)
            if sort_column in NUMERIC_COLUMNS:
                return (value is None, int(value) if value not in (None, "") else 0, int(v["cid"]))
            return (value is None, str(value or ""), int(v["cid"]))
        views.sort(key=sort_key, reverse=descending)
    elif conf["search_default_sort"] == "unstable":
        store.unstable_counter += 1
        random.Random(store.unstable_counter).shuffle(views)
    else:
        views.sort(key=lambda v: int(v["cid"]), reverse=descending)

    total = len(views)
    start = offset
    overlap = int(conf.get("page_overlap") or 0)
    if offset > 0 and overlap:
        start = max(0, offset - overlap)
    page = views[start:] if row_count == -1 else views[start:start + row_count]

    ja = locale.startswith("ja")
    empty = conf["search_empty_value"]
    rows = []
    for v in page:
        row = []
        for c in columns:
            value = v.get(c)
            if ja and c == "displayImpact":
                value = V.JA_IMPACT.get(value, value)
            if ja and c == "displayCategory":
                value = V.JA_CATEGORY.get(value, value)
            if value in (None, ""):
                if empty == "omit":
                    continue
                value = None if empty == "null" else ""
            row.append({"key": c, "value": value})
        rows.append(row)
    out: dict[str, Any] = {"offset": offset, "totalRows": total, "columns": list(columns), "rows": rows}
    if conf.get("omit_total_rows"):
        out.pop("totalRows")
    return json_response(out)


# ---- triage write ---------------------------------------------------------------------------

def put_triage(store: Store, query: dict, body: Any, user: str, headers: dict[str, str],
               rest_api: bool = True) -> Response:
    if rest_api:
        _need(store, "2022.6.0")
    conf = store.config
    if conf["csrf_on_session_cookie"] and "COVJSESSIONID-build" in headers.get("cookie", "") \
            and not headers.get("x-xsrf-token"):
        raise ApiError(403, 1003, "Invalid CSRF Token 'null' was found on the request parameter '_csrf' or header 'X-XSRF-TOKEN'.")
    store_name = q(query, "triageStoreName")
    if not store_name:
        raise ApiError(400, 1006, "Required parameter triageStoreName is missing.")
    if store_name not in store.state["triageStores"]:
        raise ApiError(404, 2000, f"No triage store found for name {store_name}.")
    if store_name == "Empty Triage Store":
        raise ApiError(400, 2003, f"Invalid triage store: {store_name}.")
    if not isinstance(body, dict):
        raise ApiError(400, 1006, "Invalid request body")
    cids = [int(c) for c in body.get("cids") or []]
    for mk in body.get("mergeKeys") or []:
        cid = store.state["mergeKeys"].get(mk)
        if cid is None:
            raise ApiError(404, 1400, f"No defect found for merge key {mk}.")
        cids.append(cid)
    if not cids:
        raise ApiError(400, 1006, "Either cids or mergeKeys is required.")
    if conf["triage_max_cids"] and len(cids) > conf["triage_max_cids"]:
        raise ApiError(400, 1007, "Number of values for parameter cids exceeds maximum.")
    for cid in cids:
        if str(cid) not in store.state["cids"]:
            raise ApiError(404, 1400, f"No defect found for CID {cid}.")
    users_of_store = {n for n, s in store.state["streams"].items() if s["triageStoreName"] == store_name}
    if conf["triage_wrong_store"] == "error":
        for cid in cids:
            if not set(store.cid_streams(cid)) & users_of_store:
                raise ApiError(400, 2003, f"CID {cid} is not in any stream of the triage store {store_name}.")
    canonical = {n.lower(): n for n in V.WRITABLE_ATTRIBUTES}
    attrs: dict[str, str] = {}
    comment: str | None = None
    seen = set()
    for pair in body.get("attributeValuesList") or []:
        raw_name = str(pair.get("attributeName", ""))
        name = canonical.get(raw_name.lower())
        if name is None:
            raise ApiError(400, 1200, f"Attribute name not found: {raw_name}")
        if name in seen:
            raise ApiError(400, 1207, f"Attribute list contains duplicates: {name}")
        seen.add(name)
        value = pair.get("attributeValue")
        value = "" if value is None else str(value)
        if name == "Comment":
            comment = value
            continue
        known = {"Classification": _known_keys(store, "classification"), "Action": _known_keys(store, "action"),
                 "Severity": V.SEVERITIES + (conf.get("attribute_values") or {}).get("Severity", []),
                 "Fix Target": _known_keys(store, "fixTarget"), "Legacy": V.LEGACY}.get(name)
        if known is not None and value not in known:
            raise InvalidKey(value)
        if name == "Owner" and value != "Unassigned":
            owner = store.state["users"].get(value)
            if owner is None:
                raise ApiError(404, 1100, f"No user found for name {value}.")
            if owner.get("disabled"):
                raise ApiError(400, 1317, "Unable to assign a disabled user as defect owner")
        attrs[name] = value
    if not attrs and comment is None:
        raise ApiError(400, 1006, "attributeValuesList is required.")
    if conf["triage_require_comment"]:
        changes = False
        for cid in cids:
            current = store.triage_of(store_name, cid)["attrs"]
            if any(current.get(k) != v for k, v in attrs.items() if k in ("Classification", "Severity", "Action")):
                changes = True
        if changes and not (comment or "").strip():
            raise ApiError(400, 1006, "A comment is required when modifying triage attributes")
    if conf["triage_test_values_only_for_test"] and attrs.get("Classification") in V.TEST_CLASSIFICATIONS:
        for cid in cids:
            kinds = (store.state["cids"][str(cid)].get("checkerProperties") or {}).get("issueKinds") or []
            if "TEST" not in kinds:
                raise ApiError(400, 1006, f"Classification {attrs['Classification']} cannot be used for CID {cid}.")
    store.write_triage(store_name, cids, attrs, comment, user)
    mode = conf["triage_success"]
    if mode == "204":
        return Response(204)
    if mode == "200_json":
        return json_response({"code": None, "message": None})
    return Response(200)


# ---- other endpoints ----------------------------------------------------------------------

def columns(store: Store, query: dict, body: Any, user: str) -> Response:
    locale = q(query, "locale")
    ja = (locale or "").lower().startswith("ja") or (locale is None and store.config["columns_names"] == "ja")
    return json_response([{"columnKey": k, "name": V.COLUMN_NAMES_JA.get(k, n) if ja else n} for k, n in V.COLUMNS])


def server_version(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2022.6.0")
    version = store.config["server_version"]
    major_minor = ".".join(version.split(".")[:2])
    return json_response({"internalVersion": f"e0e0e0e0e0 im-{major_minor}-push-1", "externalVersion": version,
                          "code": None, "message": None})


def server_time(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2022.6.0")
    from .state import fmt_soap
    return json_response({"serverTime": fmt_soap(time.time()), "code": None, "message": None})


def streams_list(store: Store, query: dict, body: Any, user: str) -> Response:
    pattern = q(query, "namePattern", "*") or "*"
    desc = q(query, "descriptionPattern")
    names = [n for n in sorted(store.state["streams"]) if fnmatch.fnmatchcase(n, pattern)
             and (desc is None or fnmatch.fnmatchcase(store.state["streams"][n].get("description", ""), desc))]
    return json_response({"streams": [stream_json(store, n) for n in names], "code": None, "message": None})


def stream_one(store: Store, name: str) -> Response:
    if name not in store.state["streams"]:
        raise ApiError(404, 1300, f"No stream found for name {name}.")
    return json_response({"streams": [stream_json(store, name)], "code": None, "message": None})


def stream_snapshots(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2021.12.0")
    name = q(query, "name")
    if name not in store.state["streams"]:
        raise ApiError(404, 1300, f"No stream found for name {name}.")
    snaps = sorted(store.stream_snapshots(name), key=lambda s: s["id"])
    return json_response({"snapshotsForStream": [{"id": s["id"]} for s in snaps], "code": None, "message": None})


def stream_latest(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2021.12.0")
    name = q(query, "name")
    if name not in store.state["streams"]:
        raise ApiError(404, 1300, f"No stream found for name {name}.")
    latest = store.latest_snapshot(name)
    return json_response({"snapshotsForStream": [{"id": latest["id"]}] if latest else []})


def snapshot_one(store: Store, snap_id: str) -> Response:
    _need(store, "2021.12.0")
    snap = store.state["snapshots"].get(snap_id)
    if snap is None:
        raise ApiError(404, 1304, f"No snapshot found for ID {snap_id}.")
    return json_response({
        "analysisCommandLine": snap.get("analysisCommandLine"), "analysisConfiguration": None,
        "analysisHost": snap.get("analysisHost"), "analysisIntermediateDir": snap.get("analysisIntermediateDir"),
        "analysisInternalVersion": snap.get("analysisInternalVersion"), "analysisTime": snap.get("analysisTime"),
        "analysisVersion": snap.get("analysisVersion"), "buildCommandLine": snap.get("buildCommandLine"),
        "buildConfiguration": None, "buildFailureCount": snap.get("buildFailureCount"),
        "buildHost": snap.get("buildHost"), "buildIntermediateDir": snap.get("buildIntermediateDir"),
        "buildSuccessCount": snap.get("buildSuccessCount"), "buildTime": snap.get("buildTime"),
        "codeVersionDate": snap.get("codeVersionDate"), "commitUser": snap.get("commitUser"),
        "sourceVersion": snap.get("sourceVersion"), "target": snap.get("target"),
        "description": snap.get("description"), "dateCreated": snap["dateCreated"],
        "enabledCheckers": snap.get("enabledCheckers", []), "hasSummaries": True, "impactHashVersion": 0,
        "portableAnalysisSettings": None, "purgedOfDetails": False, "snapshotId": snap["id"],
        "streamId": store.state["streams"].get(snap["stream"], {}).get("id"), "streamName": snap["stream"],
        "code": None, "message": None})


def _project_json(store: Store, name: str, include_streams: bool) -> dict:
    p = store.state["projects"][name]
    out = {"createdBy": p.get("createdBy", "admin"), "dateCreated": fmt_iso_z(p["dateCreated"]),
           "dateModified": fmt_iso_z(p.get("dateModified", p["dateCreated"])), "description": p.get("description", ""),
           "modifiedBy": p.get("createdBy", "admin"), "name": name, "projectKey": p["projectKey"],
           "roleAssignments": [], "streamLinks": [{"name": n} for n in p.get("streamLinks", [])]}
    if include_streams:
        out["streams"] = [stream_json(store, n) for n in sorted(store.state["streams"])
                          if store.state["streams"][n]["primaryProjectName"] == name]
    return out


def projects(store: Store, query: dict, body: Any, user: str, name: str | None = None) -> Response:
    include = (q(query, "includeStreams", "false") or "").lower() == "true"
    if name is not None:
        if name not in store.state["projects"]:
            raise ApiError(404, 1302, f"No project found for name {name}.")
        names = [name]
    else:
        pattern = q(query, "namePattern", "*") or "*"
        names = [n for n in sorted(store.state["projects"]) if fnmatch.fnmatchcase(n, pattern)]
    return json_response({"projects": [_project_json(store, n, include) for n in names], "code": None, "message": None})


def triage_store(store: Store, name: str | None) -> Response:
    _need(store, "2021.12.0")

    def one(n: str) -> dict:
        return {"roleAssignments": [{"group": {"domainName": None, "ldapServer": None, "name": "Users"},
                                     "roleAssignmentType": "group", "roleName": "developer", "scope": "triageStore",
                                     "username": None}],
                "description": store.state["triageStores"][n].get("description", ""), "name": n,
                "streamNames": sorted(s for s, v in store.state["streams"].items() if v["triageStoreName"] == n)}
    if name is None:
        return json_response({"triageStores": [one(n) for n in sorted(store.state["triageStores"])],
                              "code": None, "message": None})
    if name not in store.state["triageStores"]:
        raise ApiError(404, 2000, f"No triage store found for name {name}.")
    return json_response({**one(name), "code": None, "message": None})


def triage_attributes(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2022.3.0")
    return json_response({"attributes": V.triage_attributes_payload(store.config.get("attribute_values"),
                                                                     store.config["test_advisor"]),
                          "code": None, "message": None})


def issue_status_values(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2022.9.0")
    return json_response(list(V.STATUSES))


def checker_attribute(store: Store, name: str) -> Response:
    _need(store, "2022.9.0")
    display = {"displayType": "Type", "displayCategory": "Category", "checker": "Checker"}
    if name not in display:
        raise ApiError(400, 1006, f"Invalid checker attribute: {name}")
    if name == "checker":
        keys = _known_keys(store, "checker")
    elif name == "displayCategory":
        keys = V.CATEGORIES
    else:
        keys = sorted({p["short"] for p in V.CHECKERS.values()} |
                      {(r.get("checkerProperties") or {}).get("subcategoryShortDescription", "")
                       for r in store.state["cids"].values()} - {""})
    return json_response({"checkerAttribute": {"name": name, "displayName": display[name]},
                          "checkerAttributedata": [{"key": k, "value": k} for k in keys]})


def _history_list(store: Store, store_name: str, cid: int, lower: bool) -> list[dict]:
    triage = store.triage_of(store_name, cid)
    out = []
    names = V.HISTORY_NAMES if lower else {k: k for k in V.HISTORY_NAMES}
    for h in reversed(triage["history"]):
        pairs = [{"attributeName": names[k], "attributeValue": h["attrs"].get(k, V.DEFAULT_TRIAGE[k])}
                 for k in ("Classification", "Action", "Fix Target", "Severity", "Legacy", "Owner")]
        if h["attrs"].get("Ext. Reference"):
            pairs.append({"attributeName": names["Ext. Reference"], "attributeValue": h["attrs"]["Ext. Reference"]})
        out.append({"id": h["id"], "pairs": pairs, "user": h["userCreated"], "comment": h.get("comment"),
                    "date": fmt_history(h["dateCreated"])})
    if out:  # the system record that a manual triage overrides (confirmed shape)
        pairs = [{"attributeName": names[k], "attributeValue": V.DEFAULT_TRIAGE[k]}
                 for k in ("Classification", "Action", "Fix Target", "Severity", "Legacy", "Owner")]
        out.append({"id": 2000, "pairs": pairs, "user": "system", "comment": "default triage, overridden by manual triage",
                    "date": "0001-01-01T00:00:00.000-0700"})
    return out


def triage_history(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2022.6.0")
    try:
        cid = int(q(query, "cid", ""))
    except ValueError as exc:
        raise ApiError(400, 1006, "Required parameter cid is missing.") from exc
    if str(cid) not in store.state["cids"]:
        raise ApiError(404, 1400, f"No defect found for CID {cid}.")
    stores = query.get("triageStoreNames") or sorted({store.state["streams"][s]["triageStoreName"]
                                                      for s in store.cid_streams(cid)})
    out = []
    for name in stores:
        if name not in store.state["triageStores"]:
            raise ApiError(404, 2000, f"No triage store found for name {name}.")
        for h in _history_list(store, name, cid, lower=True):
            pairs = list(h["pairs"])
            pairs.append({"attributeName": "userCreated", "attributeValue": h["user"]})
            if h["comment"]:
                pairs.append({"attributeName": "comment", "attributeValue": h["comment"]})
            pairs.append({"attributeName": "dateCreated", "attributeValue": h["date"]})
            out.append({"id": h["id"], "attributeValuesList": pairs})
    return json_response({"triageHistories": out, "code": None, "message": None})


def triage_history_search(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2025.6.0")
    body = body or {}
    out = []
    for info in body.get("mergedDefectInfo") or []:
        cid = info.get("cid") or store.state["mergeKeys"].get(info.get("mergeKey"))
        if cid is None or str(cid) not in store.state["cids"]:
            raise ApiError(404, 1400, f"No defect found for CID {cid}.")
        for stream in body.get("streamNames") or store.cid_streams(int(cid)):
            if stream not in store.state["streams"]:
                raise ApiError(404, 1300, f"No stream found for name {stream}.")
            sname = store.state["streams"][stream]["triageStoreName"]
            triage = store.triage_of(sname, int(cid))
            current = [{"attributeName": k, "attributeValue": v} for k, v in triage["attrs"].items()]
            hist = [{"issueStateAttributeValues": h["pairs"], "dateCreated": h["date"], "userCreated": h["user"]}
                    for h in _history_list(store, sname, int(cid), lower=False)]
            out.append({"cid": int(cid), "currentIssueStateAttributeValues": current, "streamName": stream,
                        "streamDefectAndDefectTriageId": {"id": 20000 + int(cid) % 10000,
                                                          "defectTriageId": triage["defectTriageId"] or 10000 + int(cid) % 10000,
                                                          "defectTriageVerNum": triage["verNum"], "verNum": triage["verNum"]},
                        "triageHistories": hist})
    return json_response({"triageHistories": out})


def detection_history(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2022.6.0")
    cid = int(q(query, "cid", "0") or 0)
    stream = q(query, "streamName")
    if str(cid) not in store.state["cids"]:
        raise ApiError(404, 1400, f"No defect found for CID {cid}.")
    if stream not in store.state["streams"]:
        raise ApiError(404, 1300, f"No stream found for name {stream}.")
    snaps = store.detected_in(cid, stream)
    latest = store.latest_snapshot(stream)
    out = []
    if snaps:
        for kind, snap in (("FIRST_DETECTED", snaps[0]), ("LAST_DETECTED", snaps[-1])):
            out.append({"issueDetection": kind, "detectionTime": fmt_detection(snap["dateCreated"]),
                        "inCurrentSnapshot": latest is not None and snap["id"] == latest["id"],
                        "snapshotId": snap["id"], "streamName": stream, "username": snap.get("commitUser")})
    return json_response({"detectionHistoryResponseList": out, "code": None, "message": None})


def flatten_events(events: list[dict] | None) -> list[dict]:
    out = []
    for ev in events or []:
        out.append(ev)
        out.extend(flatten_events(ev.get("events")))
    return out


def source_code_info(store: Store, query: dict, body: Any, user: str) -> Response:
    _need(store, "2023.9.0")
    cid = int(q(query, "cid", "0") or 0)
    stream = q(query, "streamName")
    if str(cid) not in store.state["cids"]:
        raise ApiError(404, 1400, f"No defect found for CID {cid}.")
    if stream not in store.state["streams"]:
        raise ApiError(404, 1300, f"No stream found for name {stream}.")
    snaps = store.detected_in(cid, stream)
    rec = store.state["cids"][str(cid)]
    occs = snaps[-1]["occurrences"][str(cid)] if snaps else []
    files = snaps[-1].get("files", {}) if snaps else {}
    items = []
    for occ in occs:
        evs = []
        for ev in flatten_events(occ["events"]):
            path = ev.get("filePathname") or ""
            evs.append({"eventDescription": ev.get("eventDescription"), "eventTag": ev.get("eventTag"),
                        "eventKind": ev.get("eventKind", "NORMAL"), "eventNumber": str(ev.get("eventNumber")),
                        "eventSet": str(ev.get("eventSet", 0)), "moreInformationUrl": None, "pathCondition": None,
                        "polarity": None, "main": bool(ev.get("main")), "lineNumber": str(ev.get("lineNumber")),
                        "file": {"contentsMD5": files.get(path, "0" * 32),
                                 "filePathname": path if path.startswith("/") else "/" + path}})
        items.append({"id": str(occ["instanceId"]), "domain": rec.get("domain", "STATIC_C"), "events": evs,
                      "localEffect": (rec.get("checkerProperties") or {}).get("subcategoryLocalEffect"),
                      "longDescription": (rec.get("checkerProperties") or {}).get("subcategoryLongDescription"),
                      "secureCodeWarrior": None})
    out = {"checkerName": rec["checkerName"], "domain": "static", "issueOccurrences": items}
    if (q(query, "includeTotalIssueOccurrencesCount", "false") or "").lower() == "true":
        out["issueOccurrencesCount"] = len(items)
    return json_response(out)


def search_by_category(store: Store, query: dict, body: Any, user: str) -> Response:
    group = q(query, "groupBy", "severity")
    inner = dict(query)
    inner["offset"], inner["rowCount"] = ["0"], ["-1"]
    body = dict(body or {})
    body["columns"] = ["cid", group] if group in V.COLUMN_KEYS else ["cid"]
    saved = store.config["rowcount_minus1"]
    store.config["rowcount_minus1"] = "all"
    try:
        data = json.loads(search(store, inner, body, user).body)
    finally:
        store.config["rowcount_minus1"] = saved
    counts = Counter(next((c["value"] for c in row if c["key"] == group), "") for row in data["rows"])
    rows = [{"category": k, "count": v} for k, v in counts.items()]
    rows.sort(key=lambda r: r["count"], reverse=(q(query, "sortOrder", "desc") or "desc") == "desc")
    return json_response({"offset": 0, "totalRows": len(rows), "groupBy": group, "rows": rows})


def user_one(store: Store, name: str) -> Response:
    u = store.state["users"].get(name)
    if u is None:
        raise ApiError(404, 1100, f"No user found for name {name}.")
    return json_response({"users": [{
        "name": name, "domainName": None, "givenName": None, "familyName": None, "email": u.get("email") or None,
        "local": True, "locked": False, "disabled": u.get("disabled", False), "superUser": u.get("superUser", False),
        "dateCreated": fmt_iso_z(u.get("dateCreated", time.time())), "dateModified": fmt_iso_z(u.get("dateCreated", time.time())),
        "dateDeleted": None, "lastLogin": None, "passwordChanged": None, "createdBy": None, "modifiedBy": "system",
        "deletedBy": None, "groupNames": ["Users"], "locale": "en_US", "roleAssignments": []}],
        "totalRowCount": 1, "code": None, "message": None})


ROUTES: list[tuple[str, str, Callable]] = [
    ("GET", r"/api/v2/issues/columns", columns),
    ("POST", r"/api/v2/issues/search", search),
    ("POST", r"/api/v2/issues/searchByCategory", search_by_category),
    ("GET", r"/api/v2/issues/triageHistory", triage_history),
    ("POST", r"/api/v2/issues/triageHistory/search", triage_history_search),
    ("GET", r"/api/v2/issues/detectionHistory", detection_history),
    ("GET", r"/api/v2/issues/sourceCodeInfo", source_code_info),
    ("GET", r"/api/v2/serverInfo/version", server_version),
    ("GET", r"/api/v2/serverInfo/time", server_time),
    ("GET", r"/api/v2/streams", streams_list),
    ("GET", r"/api/v2/streams/stream/snapshots", stream_snapshots),
    ("GET", r"/api/v2/streams/latestsnapshot", stream_latest),
    ("GET", r"/api/v2/triageAttributes", triage_attributes),
    ("GET", r"/api/v2/attributes", triage_attributes),
    ("GET", r"/api/v2/issueAttributes/status", issue_status_values),
    ("GET", r"/api/v2/projects", projects),
]


def dispatch(store: Store, method: str, path: str, query: dict, raw_body: bytes, user: str,
             headers: dict[str, str]) -> Response:
    body: Any = None
    if raw_body.strip():
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except ValueError as exc:
            raise ApiError(400, 1006, f"Malformed JSON: {exc}") from exc
    if version_tuple(store.config["server_version"]) < (2021, 3, 0) and path.startswith("/api/v2/issues/search"):
        raise ApiError(404, None, "Not Found")
    for m, pattern, func in ROUTES:
        if m == method and re.fullmatch(pattern, path):
            return func(store, query, body, user)
    if method == "PUT" and path == "/api/v2/issues/triage":
        return put_triage(store, query, body, user, headers)
    if method == "GET":
        for pattern, func in ((r"/api/v2/streams/([^/]+)", stream_one), (r"/api/v2/snapshots/(\d+)", snapshot_one),
                              (r"/api/v2/triageStores/([^/]+)", triage_store),
                              (r"/api/v2/checkerAttributes/([^/]+)", checker_attribute),
                              (r"/api/v2/users/([^/]+)", user_one)):
            m2 = re.fullmatch(pattern, path)
            if m2:
                return func(store, unquote(m2.group(1)))
        m2 = re.fullmatch(r"/api/v2/projects/([^/]+)", path)
        if m2:
            return projects(store, query, body, user, unquote(m2.group(1)))
        if path == "/api/v2/triageStores":
            return triage_store(store, None)
    if any(re.fullmatch(p, path) for _, p, _ in ROUTES) or path == "/api/v2/issues/triage":
        raise ApiError(405, None, f"Request method '{method}' is not supported")
    raise ApiError(404, None, "Not Found")
