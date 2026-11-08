"""SOAP API v9 of the emulated Coverity Connect (specification 3, corrected by the review).

- SOAP 1.1, document/literal; only the top-level elements are in the v9 namespace, their children
  have no namespace (XSD without elementFormDefault, confirmed on 2024.12.1A).
- WS-Security UsernameToken in the header; the Password Type is optional (the official Perl sample
  sends none). An authentication failure is HTTP 401 with the Fault "User authentication failed.",
  an XML declaration in single quotes and Content-Type ``text/xml;charset=utf-8`` (measured).
- Business errors are HTTP 500 Faults with a CoverityFault detail (errorCode, message).
"""

from __future__ import annotations

import fnmatch
import time
import xml.etree.ElementTree as ET
from html import escape
from typing import Any, Callable

from . import values as V
from .common import Response
from .state import ApiError, Store, fmt_soap, parse_time

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
V9 = "http://ws.coverity.com/v9"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
XML_DECL = "<?xml version='1.0' encoding='UTF-8'?>"
CONTENT_TYPE = "text/xml;charset=utf-8"
SERVICES = ("defectservice", "configurationservice")


def esc(value: Any) -> str:
    return escape("" if value is None else str(value), quote=False)


def fault_body(faultcode: str, faultstring: str, code: int | None = None, message: str | None = None) -> bytes:
    detail = ""
    if code is not None:
        detail = (f'<detail><ns2:CoverityFault xmlns:ns2="{V9}"><errorCode>{code}</errorCode>'
                  f"<message>{esc(message if message is not None else faultstring)}</message></ns2:CoverityFault></detail>")
    return (f'{XML_DECL}<S:Envelope xmlns:S="{SOAP_ENV}"><S:Body>'
            f'<S:Fault xmlns:ns4="http://www.w3.org/2003/05/soap-envelope"><faultcode>{faultcode}</faultcode>'
            f"<faultstring>{esc(faultstring)}</faultstring>{detail}</S:Fault></S:Body></S:Envelope>").encode("utf-8")


def fault(status: int, faultcode: str, faultstring: str, code: int | None = None) -> Response:
    return Response(status, fault_body(faultcode, faultstring, code), [("Content-Type", CONTENT_TYPE)])


def auth_fault() -> Response:
    return fault(401, "S:Server", "User authentication failed.")


def ok(operation: str, inner: str) -> Response:
    body = (f'{XML_DECL}<S:Envelope xmlns:S="{SOAP_ENV}"><S:Body><ns2:{operation}Response xmlns:ns2="{V9}">'
            f"{inner}</ns2:{operation}Response></S:Body></S:Envelope>")
    return Response(200, body.encode("utf-8"), [("Content-Type", CONTENT_TYPE)])


class SoapFault(Exception):
    def __init__(self, faultstring: str, code: int | None = None, status: int = 500, faultcode: str = "S:Server"):
        super().__init__(faultstring)
        self.faultstring, self.code, self.status, self.faultcode = faultstring, code, status, faultcode


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _ns(tag: str) -> str:
    return tag[1:].split("}")[0] if tag.startswith("{") else ""


class Reader:
    """Reads the children of a request element: unqualified children, and (unless the switch
    soap_strict_namespace is on) children in the v9 namespace too."""

    def __init__(self, strict: bool):
        self.strict = strict

    def kids(self, el: ET.Element | None, name: str) -> list[ET.Element]:
        if el is None:
            return []
        allowed = ("",) if self.strict else ("", V9)
        return [c for c in el if _local(c.tag) == name and _ns(c.tag) in allowed]

    def kid(self, el: ET.Element | None, name: str) -> ET.Element | None:
        found = self.kids(el, name)
        return found[0] if found else None

    def text(self, el: ET.Element | None, *path: str) -> str | None:
        for name in path:
            el = self.kid(el, name)
        return (el.text or "").strip() if el is not None else None

    def boolean(self, el: ET.Element | None, name: str, default: bool = False) -> bool:
        value = self.text(el, name)
        return default if value is None else value.lower() in ("true", "1")


# ---- request entry ----------------------------------------------------------------------------

def handle(store: Store, service: str, raw: bytes, basic: tuple[str, str] | None,
           record: dict) -> Response:
    conf = store.config
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return fault(500, "S:Client", f"Couldn't create SOAP message due to exception: XML reader error: {exc}")
    if root.tag != f"{{{SOAP_ENV}}}Envelope":
        found = "{" + _ns(root.tag) + "}" + _local(root.tag)
        return fault(500, "S:Client", "Couldn't create SOAP message due to exception: unexpected XML tag. "
                                      f"expected: {{{SOAP_ENV}}}Envelope but found: {found}")
    header = root.find(f"{{{SOAP_ENV}}}Header")
    body = root.find(f"{{{SOAP_ENV}}}Body")
    user = secret = None
    security = None
    if header is not None:
        security = next((c for c in header if _local(c.tag) == "Security"), None)
    if security is not None:
        token = next((c for c in security if _local(c.tag) == "UsernameToken"), None)
        if token is not None:
            u = next((c for c in token if _local(c.tag) == "Username"), None)
            p = next((c for c in token if _local(c.tag) == "Password"), None)
            user = (u.text or "").strip() if u is not None else None
            secret = (p.text or "") if p is not None else None
            if p is not None:
                record["wsse_password_type"] = p.get("Type")
        record["wsse_must_understand"] = security.get(f"{{{SOAP_ENV}}}mustUnderstand")
        if conf["soap_require_must_understand"] and security.get(f"{{{SOAP_ENV}}}mustUnderstand") != "1":
            return auth_fault()
    if user is None and conf["soap_accept_basic"] and basic:
        user, secret = basic
    record["auth"] = {"type": "wsse" if security is not None else ("basic" if basic else None),
                      "user": user, "secret": "****" if secret else None}
    if not user or secret is None:
        return auth_fault()
    result, _ = store.check_credentials(user, secret)
    if result == "bad":
        return auth_fault()
    if result == "no_permission":
        return fault(500, "S:Server", f"User {user} Doesn't have permissions to perform {{invokeWS}}")
    if body is None or len(body) == 0:
        return fault(500, "S:Client", "Couldn't create SOAP message due to exception: no body")
    op = body[0]
    record["soap_operation"] = _local(op.tag)
    if _ns(op.tag) != V9 or (service, _local(op.tag)) not in OPERATIONS:
        return fault(500, "S:Client", f"Cannot find dispatch method for {{{_ns(op.tag)}}}{_local(op.tag)}")
    reader = Reader(conf["soap_strict_namespace"])
    try:
        with store.lock:
            return ok(_local(op.tag), OPERATIONS[(service, _local(op.tag))](store, reader, op, user))
    except SoapFault as exc:
        return fault(exc.status, exc.faultcode, exc.faultstring, exc.code)
    except ApiError as exc:
        return fault(500, "S:Server", exc.message, exc.code)


# ---- defectservice ------------------------------------------------------------------------------

def _localized(name: str, value: str) -> str:
    return f"<{name}><displayName>{esc(value)}</displayName><name>{esc(value)}</name></{name}>"


def _file_id(path: str, files: dict) -> str:
    return (f"<fileId><contentsMD5>{esc(files.get(path, '0' * 32))}</contentsMD5>"
            f"<filePathname>{esc(path)}</filePathname></fileId>")


def event_kind(ev: dict) -> str:
    if ev.get("eventKind"):
        return ev["eventKind"]
    if ev.get("remediation"):
        return "REMEDIATION"
    if ev.get("events"):
        return "MODEL"
    if ev.get("eventTag") == "path":
        return "PATH"
    return "NORMAL"


def _events_xml(events: list[dict] | None, files: dict) -> str:
    out = []
    for ev in events or []:
        out.append("<events>" + _events_xml(ev.get("events"), files)
                   + f"<eventDescription>{esc(ev.get('eventDescription'))}</eventDescription>"
                   + f"<eventKind>{event_kind(ev)}</eventKind>"
                   + f"<eventNumber>{ev.get('eventNumber', 1)}</eventNumber><eventSet>{ev.get('eventSet', 0)}</eventSet>"
                   + f"<eventTag>{esc(ev.get('eventTag'))}</eventTag>" + _file_id(ev.get("filePathname") or "", files)
                   + f"<id>{ev.get('id', 0)}</id><lineNumber>{ev.get('lineNumber') or 0}</lineNumber>"
                   + f"<main>{'true' if ev.get('main') else 'false'}</main><polarity>false</polarity></events>")
    return "".join(out)


def _instance_xml(rec: dict, occ: dict, files: dict) -> str:
    props = rec.get("checkerProperties") or {}
    cwe = props.get("cweCategory")
    parts = ["<defectInstances>", _events_xml(occ["events"], files)]
    for k, v in sorted((occ.get("properties") or {}).items()):
        parts.append(f"<properties><key>{esc(k)}</key><value>{esc(v)}</value></properties>")
    parts.append(_localized("category", props.get("category", "")))
    parts.append(f"<checkerName>{esc(rec['checkerName'])}</checkerName><component>{esc(rec.get('component', 'Other'))}</component>")
    if cwe and cwe != "none":
        parts.append(f"<cwe>{int(cwe)}</cwe>")
    parts.append(f"<domain>{esc(rec.get('domain', 'STATIC_C'))}</domain>")
    for caption in props.get("eventSetCaptions") or []:
        parts.append(f"<eventSetCaptions>{esc(caption)}</eventSetCaptions>")
    parts.append(f"<extra>{esc(occ.get('extra') if occ.get('extra') is not None else rec.get('extra', ''))}</extra>")
    if rec.get("functionDisplayName"):
        parts.append("<function>" + _file_id(occ["file"], files)
                     + f"<functionDisplayName>{esc(rec['functionDisplayName'])}</functionDisplayName>"
                     + f"<functionMangledName>{esc(rec.get('functionMangledName') or rec['functionDisplayName'])}</functionMangledName>"
                     + f"<functionMergeName>{esc(rec.get('functionMergeName') or rec['functionDisplayName'])}</functionMergeName></function>")
    parts.append(f"<id><id>{occ.get('instanceId', 0)}</id></id>")
    parts.append(_localized("impact", props.get("impact", "")))
    for kind in props.get("issueKinds") or []:
        parts.append(f"<issueKinds><displayName>{esc(V.ISSUE_KIND_DISPLAY.get(kind, kind))}</displayName>"
                     f"<name>{esc(kind)}</name></issueKinds>")
    parts.append(f"<localEffect>{esc(props.get('subcategoryLocalEffect'))}</localEffect>")
    parts.append(f"<longDescription>{esc(props.get('subcategoryLongDescription'))}</longDescription>")
    if props.get("MISRACategory"):
        parts.append(f"<misraCategory>{esc(props['MISRACategory'])}</misraCategory>")
    parts.append(f"<subcategory>{esc(rec.get('subcategory', 'none'))}</subcategory>")
    parts.append(_localized("type", props.get("subcategoryShortDescription", "")))
    parts.append("</defectInstances>")
    return "".join(parts)


def _attr_xml(name: str, value: str, tag: str = "defectStateAttributeValues") -> str:
    return (f"<{tag}><attributeDefinitionId><name>{esc(name)}</name></attributeDefinitionId>"
            f"<attributeValueId><name>{esc(value)}</name></attributeValueId></{tag}>")


def get_stream_defects(store: Store, r: Reader, op: ET.Element, user: str) -> str:
    conf = store.config
    ids = r.kids(op, "mergedDefectIdDataObjs")
    if conf["soap_max_cids"] and len(ids) > conf["soap_max_cids"]:
        raise SoapFault("Number of values for parameter mergedDefectIdDataObjs exceeds maximum.", 1007)
    spec = r.kid(op, "filterSpec")
    include_instances = r.boolean(spec, "includeDefectInstances")
    include_history = r.boolean(spec, "includeHistory")
    include_total = r.boolean(spec, "includeTotalDefectInstanceCount")
    max_text = r.text(spec, "maxDefectInstances")
    max_instances = int(max_text) if max_text not in (None, "") else None
    names = [r.text(s, "name") for s in r.kids(spec, "streamIdList")]
    for name in names:
        if name not in store.state["streams"]:
            raise SoapFault(f"No stream found for name {name}.", 1300)
    out = []
    for item in ids:
        cid_text, mk = r.text(item, "cid"), r.text(item, "mergeKey")
        if not cid_text and not mk:
            raise SoapFault("A cid and/or mergeKey is required.", 1006)
        cid = int(cid_text) if cid_text else store.state["mergeKeys"].get(mk)
        rec = store.state["cids"].get(str(cid)) if cid is not None else None
        streams = names or (store.cid_streams(cid) if rec else [])
        found_any = False
        for stream in streams:
            snaps = store.detected_in(cid, stream) if rec else []
            if not snaps:
                continue
            found_any = True
            snap = snaps[-1]
            occs = snap["occurrences"][str(cid)]
            if max_instances in (None, 0):
                mode = conf["soap_max_instances_missing"]
                chosen = occs if mode == "all" else ([] if mode == "none" else occs[:1])
            else:
                chosen = occs[:max_instances]
            store_name = store.state["streams"][stream]["triageStoreName"]
            triage = store.triage_of(store_name, cid)
            parts = [f"<return><checkerName>{esc(rec['checkerName'])}</checkerName><cid>{cid}</cid>"]
            if include_instances:
                parts += [_instance_xml(rec, o, snap.get("files", {})) for o in chosen]
            parts.append(_attr_xml("DefectStatus", store.status(cid, stream) or "New"))
            for key in ("Classification", "Action", "Severity", "Fix Target", "Legacy", "Owner"):
                parts.append(_attr_xml(V.SOAP_ATTRIBUTE_NAMES[key], triage["attrs"][key]))
            owner = store.state["users"].get(triage["attrs"]["Owner"])
            if owner:
                parts.append(_attr_xml("OwnerName", owner.get("fullName") or triage["attrs"]["Owner"]))
            parts.append(_attr_xml("Ext. Reference", triage["attrs"]["Ext. Reference"]))
            if triage["comment"]:
                parts.append(_attr_xml("Comment", triage["comment"]))
            parts.append(f"<domain>{esc(rec.get('domain', 'STATIC_C'))}</domain>")
            if include_history:
                for h in triage["history"]:
                    parts.append("<history>" + f"<dateCreated>{fmt_soap(h['dateCreated'])}</dateCreated>"
                                 + "".join(_attr_xml(V.SOAP_ATTRIBUTE_NAMES[k], h["attrs"][k])
                                           for k in ("Classification", "Action", "Severity"))
                                 + (_attr_xml("Comment", h["comment"]) if h.get("comment") else "")
                                 + f"<userCreated>{esc(h['userCreated'])}</userCreated></history>")
            parts.append(f"<id><defectTriageId>{triage['defectTriageId'] or 10000 + cid % 10000}</defectTriageId>"
                         f"<defectTriageVerNum>{triage['verNum']}</defectTriageVerNum><id>{20000 + cid % 10000}</id>"
                         f"<verNum>{triage['verNum']}</verNum></id>")
            parts.append(f"<streamId><name>{esc(stream)}</name></streamId>")
            if include_total:
                parts.append(f"<totalDefectInstanceCount>{len(occs)}</totalDefectInstanceCount>")
            parts.append("</return>")
            out.append("".join(parts))
        if not found_any and conf["soap_cid_not_in_stream"] == "fault":
            raise SoapFault(f"No defect found for CID {cid_text or mk}"
                            + (f" in stream {', '.join(names)}." if names else "."), 1400)
    return "".join(out)


def update_triage(store: Store, r: Reader, op: ET.Element, user: str) -> str:
    from . import rest
    store_name = r.text(op, "triageStore", "name")
    ids = r.kids(op, "mergedDefectIdDataObjs")
    if store.config["soap_max_cids"] and len(ids) > store.config["soap_max_cids"]:
        raise SoapFault("Number of values for parameter mergedDefectIdDataObjs exceeds maximum.", 1007)
    state = r.kid(op, "defectState")
    pairs = [{"attributeName": r.text(a, "attributeDefinitionId", "name"),
              "attributeValue": r.text(a, "attributeValueId", "name")}
             for a in r.kids(state, "defectStateAttributeValues")]
    body = {"cids": [int(r.text(i, "cid")) for i in ids if r.text(i, "cid")],
            "mergeKeys": [r.text(i, "mergeKey") for i in ids if r.text(i, "mergeKey") and not r.text(i, "cid")],
            "attributeValuesList": pairs}
    rest.put_triage(store, {"triageStoreName": [store_name or ""]}, body, user, {}, rest_api=False)
    return ""


# ---- configurationservice -------------------------------------------------------------------

def get_snapshots_for_stream(store: Store, r: Reader, op: ET.Element, user: str) -> str:
    name = r.text(op, "streamId", "name")
    if name not in store.state["streams"]:
        raise SoapFault(f"No stream found for name {name}.", 1300)
    spec = r.kid(op, "filterSpec")
    snaps = store.stream_snapshots(name)
    for field, key in (("descriptionPattern", "description"), ("versionPattern", "sourceVersion"),
                       ("targetPattern", "target")):
        pattern = r.text(spec, field)
        if pattern:
            snaps = [s for s in snaps if fnmatch.fnmatchcase(s.get(key) or "", pattern)]
    start, end = r.text(spec, "startDate"), r.text(spec, "endDate")
    if start:
        snaps = [s for s in snaps if s["dateCreated"] >= parse_time(start)]
    if end:
        snaps = [s for s in snaps if s["dateCreated"] <= parse_time(end)]
    order = store.config["snapshot_order"]
    if order == "id_desc":
        snaps = sorted(snaps, key=lambda s: s["id"], reverse=True)
    elif order == "date_desc":
        snaps = sorted(snaps, key=lambda s: s["dateCreated"], reverse=True)
    else:
        snaps = sorted(snaps, key=lambda s: s["id"])
    return "".join(f"<return><id>{s['id']}</id></return>" for s in snaps)


def get_snapshot_information(store: Store, r: Reader, op: ET.Element, user: str) -> str:
    out = []
    for item in r.kids(op, "snapshotIds"):
        sid = r.text(item, "id")
        snap = store.state["snapshots"].get(str(sid))
        if snap is None:
            raise SoapFault(f"No snapshot found for ID {sid}.", 1304)

        def el(name: str, value: Any) -> str:
            return "" if value in (None, "") else f"<{name}>{esc(value)}</{name}>"
        parts = ["<return>", el("analysisCommandLine", snap.get("analysisCommandLine")),
                 el("analysisHost", snap.get("analysisHost")),
                 el("analysisIntermediateDir", snap.get("analysisIntermediateDir")),
                 el("analysisInternalVersion", snap.get("analysisInternalVersion")),
                 el("analysisTime", snap.get("analysisTime")), el("analysisVersion", snap.get("analysisVersion")),
                 el("buildCommandLine", snap.get("buildCommandLine")),
                 f"<buildFailureCount>{int(snap.get('buildFailureCount') or 0)}</buildFailureCount>",
                 el("buildHost", snap.get("buildHost")), el("buildIntermediateDir", snap.get("buildIntermediateDir")),
                 el("buildSuccessCount", snap.get("buildSuccessCount")), el("buildTime", snap.get("buildTime")),
                 el("codeVersionDate", fmt_soap(snap["codeVersionDate"]) if snap.get("codeVersionDate") else None),
                 el("commitUser", snap.get("commitUser")), el("dateCreated", fmt_soap(snap["dateCreated"])),
                 el("description", snap.get("description"))]
        parts += [f"<enabledCheckers>{esc(c)}</enabledCheckers>" for c in snap.get("enabledCheckers", [])]
        parts += ["<hasSummaries>true</hasSummaries>", "<impactHashVersion>0</impactHashVersion>",
                  "<purgedOfDetails>false</purgedOfDetails>", f"<snapshotId><id>{snap['id']}</id></snapshotId>",
                  el("sourceVersion", snap.get("sourceVersion")), el("target", snap.get("target")), "</return>"]
        out.append("".join(parts))
    return "".join(out)


def get_version(store: Store, r: Reader, op: ET.Element, user: str) -> str:
    version = store.config["server_version"]
    return (f"<return><externalVersion>{esc(version)}</externalVersion>"
            f"<internalVersion>e0e0e0e0e0 im-{'.'.join(version.split('.')[:2])}-push-1</internalVersion></return>")


def get_streams(store: Store, r: Reader, op: ET.Element, user: str) -> str:
    pattern = r.text(op, "filterSpec", "namePattern") or "*"
    out = []
    for name in sorted(store.state["streams"]):
        if not fnmatch.fnmatchcase(name, pattern):
            continue
        s = store.state["streams"][name]
        out.append(f"<return><autoDeleteOnExpiry>false</autoDeleteOnExpiry><componentMapId><name>Default</name></componentMapId>"
                   f"<description>{esc(s.get('description', ''))}</description><id><name>{esc(name)}</name></id>"
                   f"<language>{esc(s.get('language', 'MIXED'))}</language><outdated>false</outdated>"
                   f"<primaryProjectId><name>{esc(s['primaryProjectName'])}</name></primaryProjectId>"
                   f"<triageStoreId><name>{esc(s['triageStoreName'])}</name></triageStoreId>"
                   "<enableDesktopAnalysis>true</enableDesktopAnalysis>"
                   "<ownerAssignmentOption>default_component_owner</ownerAssignmentOption></return>")
    return "".join(out)


OPERATIONS: dict[tuple[str, str], Callable[[Store, Reader, ET.Element, str], str]] = {
    ("defectservice", "getStreamDefects"): get_stream_defects,
    ("defectservice", "updateTriageForCIDsInTriageStore"): update_triage,
    ("configurationservice", "getSnapshotsForStream"): get_snapshots_for_stream,
    ("configurationservice", "getSnapshotInformation"): get_snapshot_information,
    ("configurationservice", "getVersion"): get_version,
    ("configurationservice", "getStreams"): get_streams,
}

# ---- WSDL / XSD -------------------------------------------------------------------------------
S, L, I, B, DT = "xs:string", "xs:long", "xs:int", "xs:boolean", "xs:dateTime"
_TYPES: dict[str, list[tuple[str, str, str]]] = {
    # name -> [(element, type, occurs)] where occurs is "1", "?" (0..1) or "*" (0..n); order = XSD order
    "getStreamDefects": [("mergedDefectIdDataObjs", "tns:mergedDefectIdDataObj", "*"),
                         ("filterSpec", "tns:streamDefectFilterSpecDataObj", "?")],
    "getStreamDefectsResponse": [("return", "tns:streamDefectDataObj", "*")],
    "mergedDefectIdDataObj": [("cid", L, "?"), ("mergeKey", S, "?")],
    "streamDefectFilterSpecDataObj": [("defectStateEndDate", DT, "?"), ("defectStateStartDate", DT, "?"),
                                      ("includeDefectInstances", B, "1"), ("includeHistory", B, "1"),
                                      ("includeTotalDefectInstanceCount", B, "1"), ("maxDefectInstances", I, "1"),
                                      ("streamIdList", "tns:streamIdDataObj", "*")],
    "streamIdDataObj": [("name", S, "?")],
    "streamDefectDataObj": [("checkerName", S, "?"), ("cid", L, "?"), ("defectInstances", "tns:defectInstanceDataObj", "*"),
                            ("defectStateAttributeValues", "tns:defectStateAttributeValueDataObj", "*"),
                            ("domain", S, "?"), ("history", "tns:defectStateDataObj", "*"),
                            ("id", "tns:streamDefectIdDataObj", "?"), ("streamId", "tns:streamIdDataObj", "?"),
                            ("totalDefectInstanceCount", I, "?")],
    "defectInstanceDataObj": [("events", "tns:eventDataObj", "*"), ("properties", "tns:propertyDataObj", "*"),
                              ("category", "tns:localizedValueDataObj", "?"), ("checkerName", S, "?"),
                              ("component", S, "?"), ("cwe", I, "?"), ("domain", S, "?"), ("eventSetCaptions", S, "*"),
                              ("extra", S, "?"), ("function", "tns:functionInfoDataObj", "?"),
                              ("id", "tns:defectInstanceIdDataObj", "?"), ("impact", "tns:localizedValueDataObj", "?"),
                              ("issueKinds", "tns:localizedValueDataObj", "*"), ("localEffect", S, "?"),
                              ("longDescription", S, "?"), ("misraCategory", S, "?"), ("subcategory", S, "?"),
                              ("type", "tns:localizedValueDataObj", "?")],
    "eventDataObj": [("events", "tns:eventDataObj", "*"), ("eventDescription", S, "?"), ("eventKind", S, "?"),
                     ("eventNumber", I, "1"), ("eventSet", I, "1"), ("eventTag", S, "?"),
                     ("fileId", "tns:fileIdDataObj", "?"), ("id", L, "?"), ("lineNumber", I, "1"), ("main", B, "1"),
                     ("moreInformationId", S, "?"), ("pathCondition", S, "?"), ("polarity", B, "1")],
    "fileIdDataObj": [("contentsMD5", S, "?"), ("filePathname", S, "?")],
    "functionInfoDataObj": [("fileId", "tns:fileIdDataObj", "?"), ("functionDisplayName", S, "?"),
                            ("functionMangledName", S, "?"), ("functionMergeName", S, "?")],
    "localizedValueDataObj": [("displayName", S, "?"), ("name", S, "?")],
    "propertyDataObj": [("key", S, "?"), ("value", S, "?")],
    "defectInstanceIdDataObj": [("id", L, "?")],
    "defectStateAttributeValueDataObj": [("attributeDefinitionId", "tns:attributeDefinitionIdDataObj", "?"),
                                         ("attributeValueId", "tns:attributeValueIdDataObj", "?")],
    "attributeDefinitionIdDataObj": [("name", S, "?")],
    "attributeValueIdDataObj": [("name", S, "?")],
    "defectStateDataObj": [("dateCreated", DT, "?"),
                           ("defectStateAttributeValues", "tns:defectStateAttributeValueDataObj", "*"),
                           ("userCreated", S, "?")],
    "streamDefectIdDataObj": [("defectTriageId", L, "?"), ("defectTriageVerNum", I, "?"), ("id", L, "?"), ("verNum", I, "?")],
    "updateTriageForCIDsInTriageStore": [("triageStore", "tns:triageStoreIdDataObj", "?"),
                                         ("mergedDefectIdDataObjs", "tns:mergedDefectIdDataObj", "*"),
                                         ("defectState", "tns:defectStateSpecDataObj", "?")],
    "updateTriageForCIDsInTriageStoreResponse": [],
    "triageStoreIdDataObj": [("name", S, "?")],
    "defectStateSpecDataObj": [("defectStateAttributeValues", "tns:defectStateAttributeValueDataObj", "*")],
    "CovRemoteServiceException": [("errorCode", I, "1"), ("message", S, "?")],
    "getSnapshotsForStream": [("streamId", "tns:streamIdDataObj", "?"), ("filterSpec", "tns:snapshotFilterSpecDataObj", "?")],
    "getSnapshotsForStreamResponse": [("return", "tns:snapshotIdDataObj", "*")],
    "snapshotFilterSpecDataObj": [("descriptionPattern", S, "?"), ("endDate", DT, "?"), ("hasSummaries", B, "?"),
                                  ("lastBeforeCodeVersionDate", DT, "?"), ("startDate", DT, "?"),
                                  ("targetPattern", S, "?"), ("versionPattern", S, "?")],
    "snapshotIdDataObj": [("id", L, "1")],
    "getSnapshotInformation": [("snapshotIds", "tns:snapshotIdDataObj", "*")],
    "getSnapshotInformationResponse": [("return", "tns:snapshotInfoDataObj", "*")],
    "snapshotInfoDataObj": [(n, t, o) for n, t, o in [
        ("analysisCommandLine", S, "?"), ("analysisConfiguration", S, "?"), ("analysisHost", S, "?"),
        ("analysisIntermediateDir", S, "?"), ("analysisInternalVersion", S, "?"), ("analysisTime", L, "?"),
        ("analysisVersion", S, "?"), ("buildCommandLine", S, "?"), ("buildConfiguration", S, "?"),
        ("buildFailureCount", I, "1"), ("buildHost", S, "?"), ("buildIntermediateDir", S, "?"),
        ("buildSuccessCount", I, "?"), ("buildTime", L, "?"), ("codeVersionDate", DT, "?"), ("commitUser", S, "?"),
        ("dateCreated", DT, "?"), ("description", S, "?"), ("enabledCheckers", S, "*"), ("hasSummaries", B, "1"),
        ("impactHashVersion", I, "?"), ("portableAnalysisSettings", S, "?"), ("purgedOfDetails", B, "1"),
        ("snapshotId", "tns:snapshotIdDataObj", "1"), ("sourceVersion", S, "?"), ("target", S, "?")]],
    "getVersion": [],
    "getVersionResponse": [("return", "tns:versionDataObj", "?")],
    "versionDataObj": [("externalVersion", S, "?"), ("internalVersion", S, "?")],
    "getStreams": [("filterSpec", "tns:streamFilterSpecDataObj", "?")],
    "getStreamsResponse": [("return", "tns:streamDataObj", "*")],
    "streamFilterSpecDataObj": [("languageList", S, "*"), ("descriptionPattern", S, "?"), ("namePattern", S, "?")],
    "streamDataObj": [("autoDeleteOnExpiry", B, "1"), ("componentMapId", "tns:componentMapIdDataObj", "?"),
                      ("description", S, "?"), ("id", "tns:streamIdDataObj", "?"), ("language", S, "?"),
                      ("outdated", B, "?"), ("primaryProjectId", "tns:projectIdDataObj", "?"),
                      ("triageStoreId", "tns:triageStoreIdDataObj", "?"), ("enableDesktopAnalysis", B, "?"),
                      ("ownerAssignmentOption", S, "?")],
    "componentMapIdDataObj": [("name", S, "?")],
    "projectIdDataObj": [("name", S, "?")],
}
_SERVICE_OPS = {
    "defectservice": ["getStreamDefects", "updateTriageForCIDsInTriageStore"],
    "configurationservice": ["getSnapshotsForStream", "getSnapshotInformation", "getVersion", "getStreams"],
}


def xsd(service: str) -> bytes:
    ops = _SERVICE_OPS[service]
    elements = [f'<xs:element name="{n}" type="tns:{n}"/>' for op in ops for n in (op, op + "Response")]
    elements.append('<xs:element name="CoverityFault" type="tns:CovRemoteServiceException"/>')
    types = []
    for name, fields in _TYPES.items():
        seq = []
        for el, typ, occ in fields:
            attrs = {"1": "", "?": ' minOccurs="0"', "*": ' nillable="true" minOccurs="0" maxOccurs="unbounded"'}[occ]
            if typ.startswith("xs:") and occ == "*":
                attrs = ' nillable="true" minOccurs="0" maxOccurs="unbounded"'
            seq.append(f'<xs:element name="{el}" type="{typ}"{attrs}/>')
        types.append(f'<xs:complexType name="{name}"><xs:sequence>{"".join(seq)}</xs:sequence></xs:complexType>')
    text = (f"{XML_DECL}<!-- Published by XML-WS Runtime (https://github.com/eclipse-ee4j/metro-jax-ws). "
            "Runtime's version is XML-WS Runtime 4.0.1 git-revision#298606e. (emulator) -->"
            f'<xs:schema xmlns:tns="{V9}" xmlns:xs="http://www.w3.org/2001/XMLSchema" version="1.0" '
            f'targetNamespace="{V9}">' + "".join(elements) + "".join(types) + "</xs:schema>")
    return text.encode("utf-8")


def wsdl(service: str, base_url: str) -> bytes:
    ops = _SERVICE_OPS[service]
    port = "DefectService" if service == "defectservice" else "ConfigurationService"
    messages, port_ops, bind_ops = [], [], []
    for op in ops:
        messages.append(f'<message name="{op}"><part name="parameters" element="tns:{op}"/></message>'
                        f'<message name="{op}Response"><part name="parameters" element="tns:{op}Response"/></message>')
        port_ops.append(f'<operation name="{op}"><input wsam:Action="{V9}/{port}/{op}Request" message="tns:{op}"/>'
                        f'<output wsam:Action="{V9}/{port}/{op}Response" message="tns:{op}Response"/>'
                        f'<fault message="tns:CovRemoteServiceException" name="CovRemoteServiceException" '
                        f'wsam:Action="{V9}/{port}/{op}/Fault/CovRemoteServiceException"/></operation>')
        bind_ops.append(f'<operation name="{op}"><soap:operation soapAction=""/><input><soap:body use="literal"/></input>'
                        f'<output><soap:body use="literal"/></output><fault name="CovRemoteServiceException">'
                        f'<soap:fault name="CovRemoteServiceException" use="literal"/></fault></operation>')
    url = f"{base_url}/ws/v9/{service}"
    text = (f"{XML_DECL}<!-- Published by XML-WS Runtime (https://github.com/eclipse-ee4j/metro-jax-ws). "
            "Runtime's version is XML-WS Runtime 4.0.1 git-revision#298606e. (emulator) -->"
            f'<definitions xmlns:wsam="http://www.w3.org/2007/05/addressing/metadata" '
            f'xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/" xmlns:tns="{V9}" '
            f'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns="http://schemas.xmlsoap.org/wsdl/" '
            f'targetNamespace="{V9}" name="{port}Service">'
            f'<types><xsd:schema><xsd:import namespace="{V9}" schemaLocation="{url}?xsd=1"/></xsd:schema></types>'
            + "".join(messages)
            + '<message name="CovRemoteServiceException"><part name="fault" element="tns:CoverityFault"/></message>'
            + f'<portType name="{port}">' + "".join(port_ops) + "</portType>"
            + f'<binding name="{port}PortBinding" type="tns:{port}"><soap:binding transport="http://schemas.xmlsoap.org/soap/http" style="document"/>'
            + "".join(bind_ops) + "</binding>"
            + f'<service name="{port}Service"><port name="{port}Port" binding="tns:{port}PortBinding">'
            + f'<soap:address location="{url}"/></port></service></definitions>')
    return text.encode("utf-8")


def services_page(base_url: str) -> bytes:
    rows = "".join(
        f"<tr><td>{{{V9}}}{('DefectService' if s == 'defectservice' else 'ConfigurationService')}Service</td>"
        f"<td>Address: {base_url}/ws/v9/{s}<br/>WSDL: <a href=\"{base_url}/ws/v9/{s}?wsdl\">{base_url}/ws/v9/{s}?wsdl</a>"
        f"<br/>Implementation class: com.coverity.ws.v9.{s}</td></tr>" for s in SERVICES)
    return (f"<html><head><title>Web Services</title></head><body><h1>Web Services</h1><table border=\"1\">"
            f"<tr><th>Endpoint</th><th>Information</th></tr>{rows}</table></body></html>").encode("utf-8")
