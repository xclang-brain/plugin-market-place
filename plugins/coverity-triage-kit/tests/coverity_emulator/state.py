"""State of the emulated Coverity Connect: users, projects, streams, triage stores, snapshots, CIDs
and their triage, saved as JSON in the data folder (``state.json``, ``config.json``,
``requests.jsonl``) so that it survives a restart.

Model (specification 1.2):
- a stream belongs to one triage store; streams of the same store share the triage of a CID.
- a CID groups the occurrences with the same mergeKey. CIDs and snapshot IDs are separate counters
  that start at 10001 (guess based on a fresh server).
- one warning is kept as one "warning tree" (checker, subcategory, function, extra, events, ...);
  the REST rows, the SOAP defectInstances and the JSON v7 come from that tree.
- the status is derived: in the latest snapshot of the stream -> New / Triaged / Dismissed from the
  classification; only in earlier snapshots -> Fixed (or Absent Dismissed).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config as cfg
from . import values as V

SCHEMA = 1


class ApiError(Exception):
    """An error with a Coverity error code (REST code / SOAP errorCode) and an HTTP status."""

    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class InvalidKey(ApiError):
    def __init__(self, value: str):
        super().__init__(500, None, f"Invalid key provided: {value}")
        self.value = value


# ---- time formats (5.12, confirmed shapes) ----------------------------------------------------

def _local(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone()


def fmt_mmddyy(epoch: float | None) -> str:
    return _local(epoch).strftime("%m/%d/%y") if epoch else ""


def fmt_last_triaged(epoch: float | None) -> str:
    return _local(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch else ""


def fmt_history(epoch: float) -> str:
    """2015-07-23T07:54:24.366-0600 (triageHistory, no colon in the offset)."""
    d = _local(epoch)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}" + d.strftime("%z")


def fmt_detection(epoch: float) -> str:
    """2013-07-23T07:53:46.288-0600 MDT (detectionHistory)."""
    d = _local(epoch)
    return fmt_history(epoch) + " " + (d.tzname() or "UTC")


def fmt_soap(epoch: float) -> str:
    """xs:dateTime as JAXB writes it: 2013-03-19T14:40:32.022-07:00 (Z for UTC)."""
    d = _local(epoch)
    off = d.strftime("%z")
    zone = "Z" if off in ("+0000", "") else f"{off[:3]}:{off[3:]}"
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}" + zone


def fmt_iso_z(epoch: float) -> str:
    d = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def parse_time(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(text.replace("Z", "+0000"), fmt)
            if d.tzinfo is None:
                d = d.astimezone()
            return d.timestamp()
        except ValueError:
            continue
    raise ValueError(f"cannot read the time: {value}")


def merge_key(checker: str, function: str | None, file: str, extra: str) -> str:
    """Mock mergeKey (proposal 1.2): it changes with checker, function (or file name) and extra, never with
    the line. A function named main also takes the file name."""
    base = os.path.basename(file.replace("\\", "/"))
    where = function if function else base
    if function in ("main", "main()"):
        where = f"{function}\0{base}"
    return hashlib.md5(f"{checker}\0{where}\0{extra}".encode("utf-8")).hexdigest()


def _new_state() -> dict[str, Any]:
    now = time.time()
    return {
        "schema": SCHEMA,
        "counters": {"cid": 10001, "snapshot": 10001, "project": 10001, "stream": 10001, "history": 2001,
                     "instance": 30001, "event": 20001, "streamDefect": 20001, "defectTriage": 10001,
                     "store": 10001},
        "users": {},
        "triageStores": {"Default Triage Store": {"id": 10000, "description": "Default Triage Store",
                                                  "dateCreated": now},
                         "Empty Triage Store": {"id": 10001, "description": "Empty Triage Store",
                                                "dateCreated": now}},
        "projects": {},
        "streams": {},
        "snapshots": {},
        "cids": {},
        "mergeKeys": {},
        "triage": {},
        "triageWrites": [],
    }


class Store:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.state_path = self.dir / "state.json"
        self.config_path = self.dir / "config.json"
        self.requests_path = self.dir / "requests.jsonl"
        self.state = json.loads(self.state_path.read_text("utf-8")) if self.state_path.is_file() else _new_state()
        self.config = cfg.merge(cfg.DEFAULTS, json.loads(self.config_path.read_text("utf-8"))) \
            if self.config_path.is_file() else copy.deepcopy(cfg.DEFAULTS)
        self.requests: list[dict] = []
        if self.requests_path.is_file():
            for line in self.requests_path.read_text("utf-8").splitlines():
                try:
                    self.requests.append(json.loads(line))
                except ValueError:
                    pass
        self.request_seq = max((r.get("seq", 0) for r in self.requests), default=0)
        self.unstable_counter = 0

    # ---- persistence ------------------------------------------------------------------------

    def save(self) -> None:
        with self.lock:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.state_path)

    def save_config(self) -> None:
        with self.lock:
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, self.config_path)

    def set_config(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.config = cfg.merge(self.config, changes)
            self.save_config()
            return self.config

    def log_request(self, record: dict) -> None:
        with self.lock:
            self.request_seq += 1
            record["seq"] = self.request_seq
            self.requests.append(record)
            with open(self.requests_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def clear_requests(self) -> None:
        with self.lock:
            self.requests = []
            self.requests_path.write_text("", "utf-8")

    def reset(self) -> None:
        with self.lock:
            self.state = _new_state()
            self.save()

    def next_id(self, name: str) -> int:
        value = self.state["counters"][name]
        self.state["counters"][name] = value + 1
        return value

    # ---- seeding ----------------------------------------------------------------------------

    def add_user(self, name: str, password: str | None = None, keys: list[str] | None = None,
                 webservices: bool = True, disabled: bool = False, full_name: str = "", email: str = "",
                 super_user: bool = False) -> dict:
        with self.lock:
            user = self.state["users"].setdefault(name, {"dateCreated": time.time()})
            user.update({"password": password, "authKeys": list(keys or []), "webservices": webservices,
                         "disabled": disabled, "fullName": full_name, "email": email, "superUser": super_user})
            self.save()
            return user

    def add_triage_store(self, name: str, description: str = "") -> None:
        with self.lock:
            if name not in self.state["triageStores"]:
                self.state["triageStores"][name] = {"id": self.next_id("store"), "description": description,
                                                    "dateCreated": time.time()}
            self.save()

    def add_project(self, name: str, description: str = "", created_by: str = "admin") -> dict:
        with self.lock:
            projects = self.state["projects"]
            if name not in projects:
                now = time.time()
                projects[name] = {"projectKey": self.next_id("project"), "description": description,
                                  "dateCreated": now, "dateModified": now, "createdBy": created_by,
                                  "streamLinks": []}
            self.save()
            return projects[name]

    def add_stream(self, name: str, project: str, triage_store: str = "Default Triage Store",
                   description: str = "", language: str = "MIXED") -> dict:
        with self.lock:
            if project not in self.state["projects"]:
                self.add_project(project)
            if triage_store not in self.state["triageStores"]:
                self.add_triage_store(triage_store)
            streams = self.state["streams"]
            if name not in streams:
                streams[name] = {"id": self.next_id("stream"), "primaryProjectName": project,
                                 "triageStoreName": triage_store, "description": description,
                                 "language": language, "dateCreated": time.time()}
            else:
                streams[name].update({"primaryProjectName": project, "triageStoreName": triage_store})
            self.save()
            return streams[name]

    def link_stream(self, project: str, stream: str) -> None:
        with self.lock:
            links = self.state["projects"][project].setdefault("streamLinks", [])
            if stream not in links:
                links.append(stream)
            self.save()

    # ---- authentication ---------------------------------------------------------------------

    def check_credentials(self, name: str, secret: str) -> tuple[str, dict | None]:
        """("ok" | "bad" | "no_permission" | "disabled", user)."""
        user = self.state["users"].get(name)
        if user is None:
            return "bad", None
        if secret not in [k for k in user.get("authKeys", []) if k] and \
                not (user.get("password") and secret == user["password"]):
            return "bad", user
        if user.get("disabled"):
            return "bad", user
        if not user.get("webservices", True):
            return "no_permission", user
        return "ok", user

    # ---- snapshots and scopes ---------------------------------------------------------------

    def stream_snapshots(self, stream: str) -> list[dict]:
        """Snapshots of a stream, oldest first (by dateCreated, then id)."""
        snaps = [s for s in self.state["snapshots"].values() if s["stream"] == stream]
        return sorted(snaps, key=lambda s: (s["dateCreated"], s["id"]))

    def latest_snapshot(self, stream: str) -> dict | None:
        snaps = self.stream_snapshots(stream)
        return snaps[-1] if snaps else None

    def select_snapshots(self, stream: str, scope: str | None) -> list[dict]:
        """Snapshots of the stream named by the scope grammar ([BD:snapshot-comparison-scope-grammar])."""
        snaps = self.stream_snapshots(stream)
        if not snaps:
            return []
        scope = (scope or "last()").strip()
        chosen: dict[int, dict] = {}
        for item in _split_top(scope, ","):
            item = item.strip()
            if not item:
                continue
            if ".." in item:
                left, right = item.split("..", 1)
                a, b = _resolve(left, snaps), _resolve(right, snaps)
                if a is None or b is None:
                    continue
                lo, hi = sorted((a, b))
                for s in snaps[lo:hi + 1]:
                    chosen[s["id"]] = s
            else:
                pos = _resolve(item, snaps)
                if pos is not None:
                    chosen[snaps[pos]["id"]] = snaps[pos]
        return sorted(chosen.values(), key=lambda s: (s["dateCreated"], s["id"]))

    # ---- commit -----------------------------------------------------------------------------

    def commit(self, stream: str, user: str, info: dict, issues: list[dict]) -> dict:
        """A new snapshot of ``stream`` with ``issues`` (JSON v7-like warning trees).

        A mergeKey already known keeps its CID; a new one gets a new CID. CIDs of the previous
        snapshot that are missing now are Fixed in this stream (derived, see :meth:`status`)."""
        with self.lock:
            if stream not in self.state["streams"]:
                raise ApiError(404, 1300, f"No stream found for name {stream}.")
            previous = self.latest_snapshot(stream)
            snap_id = int(info["id"]) if info.get("id") else self.next_id("snapshot")
            if str(snap_id) in self.state["snapshots"]:
                raise ApiError(400, 1006, f"Snapshot {snap_id} exists already.")
            now = time.time()
            created = parse_time(info["dateCreated"]) if info.get("dateCreated") else now
            occurrences: dict[str, list[dict]] = {}
            new_cids: list[int] = []
            for issue in issues:
                mk = issue.get("mergeKey") or merge_key(issue["checkerName"], issue.get("functionDisplayName"),
                                                        issue.get("mainEventFilePathname", ""), issue.get("extra", ""))
                cid = self.state["mergeKeys"].get(mk)
                if cid is None:
                    cid = self.next_id("cid")
                    self.state["mergeKeys"][mk] = cid
                    new_cids.append(cid)
                    self.state["cids"][str(cid)] = self._cid_record(cid, mk, issue)
                occurrences.setdefault(str(cid), []).append(self._occurrence(issue))
            for occ_list in occurrences.values():
                occ_list.sort(key=lambda o: (o["file"], o["line"]))
            snap = {
                "id": snap_id, "stream": stream, "dateCreated": created, "commitUser": user,
                "sourceVersion": info.get("sourceVersion"), "target": info.get("target"),
                "description": info.get("description"),
                "analysisVersion": info.get("analysisVersion"), "analysisInternalVersion": info.get("analysisInternalVersion"),
                "analysisCommandLine": info.get("analysisCommandLine"), "analysisHost": info.get("analysisHost"),
                "analysisIntermediateDir": info.get("analysisIntermediateDir"),
                "analysisTime": info.get("analysisTime", 0), "buildCommandLine": info.get("buildCommandLine"),
                "buildHost": info.get("buildHost"), "buildIntermediateDir": info.get("buildIntermediateDir"),
                "buildTime": info.get("buildTime", 0), "buildFailureCount": info.get("buildFailureCount", 0),
                "buildSuccessCount": info.get("buildSuccessCount", 0),
                "codeVersionDate": parse_time(info["codeVersionDate"]) if info.get("codeVersionDate") else created,
                "enabledCheckers": sorted(info.get("enabledCheckers") or []),
                "hasSummaries": True, "purgedOfDetails": False,
                "files": info.get("files") or {},
                "occurrences": occurrences,
            }
            self.state["snapshots"][str(snap_id)] = snap
            gone = []
            if previous is not None:
                gone = sorted(int(c) for c in previous["occurrences"] if c not in occurrences)
            self.save()
            return {"snapshotId": snap_id, "cids": sorted(int(c) for c in occurrences), "newCids": new_cids,
                    "fixedCids": gone, "occurrences": sum(len(v) for v in occurrences.values())}

    def _cid_record(self, cid: int, mk: str, issue: dict) -> dict:
        keys = ("checkerName", "subcategory", "type", "subtype", "domain", "language", "code-language", "extra",
                "functionDisplayName", "functionMangledName", "checkerProperties")
        rec = {k: copy.deepcopy(issue.get(k)) for k in keys if k in issue}
        rec["cid"] = cid
        rec["mergeKey"] = mk
        rec["functionMergeName"] = issue.get("functionMergeName") or issue.get("functionDisplayName")
        rec["component"] = issue.get("componentName") or "Other"
        return rec

    def _occurrence(self, issue: dict) -> dict:
        path = issue.get("strippedMainEventFilePathname") or issue.get("mainEventFilePathname") or ""

        def convert(events: list[dict] | None) -> list[dict] | None:
            if events is None:
                return None
            out = []
            for ev in events:
                ev2 = {k: v for k, v in ev.items() if k != "events"}
                ev2["filePathname"] = ev.get("strippedFilePathname") or ev.get("filePathname")
                ev2["id"] = self.next_id("event")
                ev2["events"] = convert(ev.get("events"))
                out.append(ev2)
            return out

        return {"file": path, "line": issue.get("mainEventLineNumber"),
                "events": convert(issue.get("events")) or [], "instanceId": self.next_id("instance"),
                "properties": copy.deepcopy(issue.get("properties") or {}),
                "functionDisplayName": issue.get("functionDisplayName"), "extra": issue.get("extra")}

    # ---- per CID ----------------------------------------------------------------------------

    def cid_streams(self, cid: int) -> list[str]:
        names = {s["stream"] for s in self.state["snapshots"].values() if str(cid) in s["occurrences"]}
        return sorted(names)

    def detected_in(self, cid: int, stream: str) -> list[dict]:
        return [s for s in self.stream_snapshots(stream) if str(cid) in s["occurrences"]]

    def triage_of(self, store: str, cid: int) -> dict:
        record = self.state["triage"].get(store, {}).get(str(cid))
        attrs = dict(V.DEFAULT_TRIAGE)
        if record:
            attrs.update(record["attrs"])
        return {"attrs": attrs, "comment": (record or {}).get("comment"),
                "lastTriaged": (record or {}).get("lastTriaged"), "lastTriagedUser": (record or {}).get("lastTriagedUser"),
                "history": (record or {}).get("history", []), "verNum": (record or {}).get("verNum", 1),
                "defectTriageId": (record or {}).get("defectTriageId")}

    def status(self, cid: int, stream: str) -> str | None:
        latest = self.latest_snapshot(stream)
        if latest is None:
            return None
        store = self.state["streams"][stream]["triageStoreName"]
        classification = self.triage_of(store, cid)["attrs"]["Classification"]
        detected = str(cid) in latest["occurrences"]
        was = bool(self.detected_in(cid, stream))
        return V.status_of(classification, detected, was)

    def write_triage(self, store: str, cids: list[int], attrs: dict[str, str], comment: str | None,
                     user: str) -> None:
        with self.lock:
            now = time.time()
            book = self.state["triage"].setdefault(store, {})
            for cid in cids:
                rec = book.get(str(cid))
                if rec is None:
                    rec = {"attrs": dict(V.DEFAULT_TRIAGE), "comment": None, "history": [],
                           "defectTriageId": self.next_id("defectTriage"), "verNum": 1}
                    book[str(cid)] = rec
                rec["attrs"].update(attrs)
                if comment is not None:
                    rec["comment"] = comment
                rec["lastTriaged"] = now
                rec["lastTriagedUser"] = user
                rec["verNum"] = rec.get("verNum", 1) + 1
                rec["history"].append({"id": self.next_id("history"), "attrs": dict(rec["attrs"]),
                                       "comment": comment, "userCreated": user, "dateCreated": now})
            self.state["triageWrites"].append({"time": fmt_iso_z(now), "store": store, "cids": list(cids),
                                               "attributes": dict(attrs), "comment": comment, "user": user})
            self.save()


# ---- scope grammar helpers ----------------------------------------------------------------------

def _split_top(text: str, sep: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _date_ref(text: str) -> float | None:
    m = re.fullmatch(r"(\d+)\s+days?\s+ago", text.strip())
    if m:
        return (datetime.now().astimezone() - timedelta(days=int(m.group(1)))).timestamp()
    try:
        return parse_time(text)
    except ValueError:
        return None


def _resolve(ref: str, snaps: list[dict]) -> int | None:
    """Index into ``snaps`` (oldest first) of a scope reference."""
    ref = ref.strip()
    if not ref:
        return None
    if ref == "first()":
        return 0
    if ref == "last()":
        return len(snaps) - 1
    m = re.fullmatch(r"(firstAfter|lastBefore)\((.*)\)", ref)
    if m:
        inner = m.group(2).strip()
        pos = _resolve(inner, snaps)
        when = _date_ref(inner) if pos is None else None
        if m.group(1) == "firstAfter":
            if pos is not None:
                return pos + 1 if pos + 1 < len(snaps) else None
            return next((i for i, s in enumerate(snaps) if s["dateCreated"] > when), None) if when else None
        if pos is not None:
            return pos - 1 if pos > 0 else None
        cands = [i for i, s in enumerate(snaps) if when and s["dateCreated"] < when]
        return cands[-1] if cands else None
    if ref.isdigit():
        return next((i for i, s in enumerate(snaps) if s["id"] == int(ref)), None)
    when = _date_ref(ref)
    if when is not None:
        cands = [i for i, s in enumerate(snaps) if s["dateCreated"] <= when]
        return cands[-1] if cands else None
    return None
