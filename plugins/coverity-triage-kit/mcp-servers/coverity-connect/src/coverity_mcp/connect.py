"""Coverity Connect client.

REST API v2: issue search (``POST /api/v2/issues/search``; the latest snapshot, or every snapshot
when the status Fixed or Absent Dismissed is wanted), the triage store of a stream
(``GET /api/v2/streams/{name}``) and triage write-back (``PUT /api/v2/issues/triage``, Coverity
Connect 2022.6.0 or later). SOAP API v9: warning path (``getStreamDefects``) and snapshot
information (``getSnapshotsForStream`` / ``getSnapshotInformation``).

How answers that are not the expected data are handled (from answers recorded on a real server,
Coverity Connect 2024.12, and the published XSD):

- A 3xx is a failure that names its Location. Redirects are not followed: a redirect means the
  URL leads to a login page, a proxy, or the http address of an https server.
- A body that is not JSON (REST) or not a SOAP envelope (SOAP), such as a login page in HTML,
  is a fatal error that says so, not a parser error.
- REST 401/403, 5xx and connection failures stop the whole call. Another 4xx fails only the request
  it answers. A 403 can come with right credentials and a user without the permission
  "Access Web services", which the API needs.
- SOAP: a bad key is answered with HTTP 401 and the fault "User authentication failed.". HTTP
  401/403 and the faults of authentication and permission stop the whole call. Other faults fail
  only the request they answer.
- REST search: a page without totalRows does not end the search; pages are read until one is
  empty. A filter value the server does not know (HTTP 500 "Invalid key provided: X") is left out
  and reported; no issue can hold it, so the result is the same without it.
"""

from __future__ import annotations

import re
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .coverity import (ABSENT_STATUSES, CoverityClient, CoverityError, SearchPage, filter_mismatches, issue_value,
                       unknown_statuses)
from .envvars import get_env
from .models import Event, Issue, IssueDetail, TriageAttributes
from .settings import CoverityConfig, FilterSpec

TIMEOUT = 15.0  # seconds for one request (a tool call has 20; clients stop a call after 30)
AUTH_CHECK_TIMEOUT = 10.0
MIN_PAGE, MAX_PAGE = 20, 200
SOAP_NS = "http://ws.coverity.com/v9"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
PASSWORD_TEXT = ("http://docs.oasis-open.org/wss/2004/01/"
                 "oasis-200401-wss-username-token-profile-1.0#PasswordText")

# Issue fields and the REST column keys that hold them. Keys a server does not list in
# GET /api/v2/issues/columns are not requested and the field stays empty.
COLUMN_KEYS = {
    "cid": "cid", "checker": "checker", "file": "displayFile", "line": "lineNumber",
    "function": "displayFunction", "impact": "displayImpact", "category": "displayCategory",
    "cwe": "cwe", "merge_key": "mergeKey", "classification": "classification",
    "action": "action", "severity": "severity", "status": "status",
}
REQUIRED_FIELDS = ("cid", "checker", "file")
# Filter keys and the column names they filter on server side. Wildcard checker names and the
# status are checked after the search. Keys the server filtered are not checked again: the row
# holds the display value, which can differ from the key the server matched.
FILTER_COLUMNS = {"checkers": "Checker", "impacts": "Impact",
                  "classification": "Classification", "action": "Action"}
FILTER_NAMES = {"checkers": "checkers", "impacts": "impacts",
                "classification": "triage.classification", "action": "triage.action"}
# Fixed and Absent Dismissed are the status of an issue an earlier snapshot has and the latest one
# does not, so the scope last() never holds them. Only when one of them is wanted, the search covers
# every snapshot of the stream; the status is still checked after the search.
LATEST_SCOPE, ALL_SCOPE = "last()", "first()..last()"
INVALID_KEY = re.compile(r"Invalid key provided:\s*(.+?)\s*$")
AUTH_FAULT = re.compile(r"authentication failed|doesn't have permissions to perform", re.IGNORECASE)
MAX_DEFECT_INSTANCES = 1  # only the first defect instance is read

_columns_cache: dict[str, dict[str, str]] = {}
_columns_lock = threading.Lock()


def clear_caches() -> None:
    with _columns_lock:
        _columns_cache.clear()


def _ssl_context(ca_file: str | None) -> ssl.SSLContext:
    if ca_file:
        if not Path(ca_file).is_file():
            raise CoverityError(f"証明書のファイルがありません: {ca_file}（coverity.ca_file か環境変数 COVERITY_CA_FILE を"
                                "確かめてください）")
        return ssl.create_default_context(cafile=ca_file)
    import truststore  # noqa: PLC0415 - company PCs hold the in-house CA in the OS certificate store
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    return next((c for c in node if _local(c.tag) == name), None)


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in node if _local(c.tag) == name]


def _text(node: ET.Element | None, *path: str) -> str:
    for name in path:
        node = _child(node, name)
    return (node.text or "").strip() if node is not None else ""


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def credentials(config: CoverityConfig) -> tuple[str | None, str | None]:
    return get_env(config.user_env), get_env(config.key_env)


def _body_info(resp: httpx.Response) -> str:
    text = " ".join(resp.text.split())
    return (f"HTTP {resp.status_code}、Content-Type {resp.headers.get('Content-Type') or 'なし'}"
            + (f"、本文の先頭 {text[:120]!r}" if text else ""))


def _redirect_error(where: str, resp: httpx.Response) -> CoverityError:
    location = resp.headers.get("Location") or "なし"
    return CoverityError(f"Coverity {where} がリダイレクトを返しました（HTTP {resp.status_code}、行き先 {location}）。"
                         "coverity.url（https か http か、ポート番号）が正しいか、"
                         "ログインの画面やプロキシに向いていないかを確かめてください", status=resp.status_code)


def _what_to_check(status: int) -> str:
    return ("ユーザ名と認証キー" + ("、Web サービスを使う権限（Access Web services）" if status == 403 else "")
            + "を確認してください")


def _not_json(where: str, resp: httpx.Response) -> str:
    return (f"Coverity {where} の応答が JSON ではありません（{_body_info(resp)}）。"
            "coverity.url がログインの画面やプロキシに向いていないかを確かめてください")


def http_kwargs(config: CoverityConfig, timeout: float, transport: httpx.BaseTransport | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"base_url": config.url.rstrip("/"), "timeout": timeout, "follow_redirects": False}
    if transport is not None:
        kwargs["transport"] = transport
    else:
        kwargs["verify"] = _ssl_context(config.ca_file)
    return kwargs


def check_auth(config: CoverityConfig, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Whether credentials are set, and one light authenticated request with its time."""
    user, key = credentials(config)
    out: dict[str, Any] = {
        "user": f"環境変数 {config.user_env}: " + ("設定あり" if user else "なし"),
        "key": f"環境変数 {config.key_env}: " + (f"設定あり（{len(key)} 文字）" if key else "なし"),
        "ok": False, "seconds": None}
    if not user or not key:
        out["result"] = (f"ユーザ名または認証キーが設定されていません。環境変数 {config.user_env} に"
                         f"ユーザ名を、{config.key_env} に認証キーを設定してください")
        return out
    started = time.monotonic()
    try:
        with httpx.Client(**http_kwargs(config, AUTH_CHECK_TIMEOUT, transport)) as http:
            resp = http.get("/api/v2/issues/columns", auth=(user, key), headers={"Accept": "application/json"},
                            params={"queryType": "bySnapshot", "retrieveGroupByColumns": "false"})
    except httpx.TimeoutException:
        out["result"] = f"{AUTH_CHECK_TIMEOUT:.0f} 秒以内に応答がありません（URL と社内のネットワークを確認してください）"
        return out
    except httpx.HTTPError as exc:
        out["result"] = f"Coverity に接続できません: {exc}"
        return out
    except CoverityError as exc:
        out["result"] = str(exc)
        return out
    finally:
        out["seconds"] = round(time.monotonic() - started, 1)
    if 300 <= resp.status_code < 400:
        out["result"] = str(_redirect_error("REST GET /api/v2/issues/columns", resp))
    elif resp.status_code in (401, 403):
        out["result"] = f"認証に失敗しました（HTTP {resp.status_code}）。" + _what_to_check(resp.status_code)
    elif resp.status_code >= 400:
        out["result"] = f"Coverity が HTTP {resp.status_code} を返しました（{_body_info(resp)}）"
    else:
        try:
            resp.json()
        except ValueError:
            out["result"] = _not_json("REST GET /api/v2/issues/columns", resp)
            return out
        out["ok"] = True
        out["result"] = f"認証できました（{out['seconds']} 秒）"
    return out


class ConnectClient(CoverityClient):
    FIRST_REQUEST_SECONDS = 5.0  # one request took about 4.6 seconds on a production server

    def __init__(self, config: CoverityConfig, transport: httpx.BaseTransport | None = None):
        super().__init__()
        user, key = credentials(config)
        if not user or not key:
            raise CoverityError(f"環境変数 {config.user_env} / {config.key_env} が設定されていません"
                                "（ユーザ名と認証キーを設定してください）")
        self.config = config
        self.user, self.key = user, key
        self.http = httpx.Client(**http_kwargs(config, TIMEOUT, transport))
        self._stores: dict[str, str] = {}  # stream -> the triage store it uses

    def close(self) -> None:
        self.http.close()

    def _record(self, request: str, started: float, status: Any, **extra: Any) -> None:
        self.requests.append({"request": request, "status": status,
                              "seconds": round(time.monotonic() - started, 2), **extra})

    # ---- HTTP -------------------------------------------------------------------------------

    def _rest(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        """JSON of the answer. 401/403, 3xx and 5xx are fatal; other 4xx are not."""
        started = time.monotonic()
        extra = {"offset": params["offset"]} if params and "offset" in params else {}
        try:
            resp = self.http.request(method, path, params=params, json=body, auth=(self.user, self.key),
                                     headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            self._record(f"{method} {path}", started, type(exc).__name__, **extra)
            raise CoverityError(f"Coverity に接続できません: {exc}") from exc
        self._record(f"{method} {path}", started, resp.status_code, **extra)
        if 300 <= resp.status_code < 400:
            raise _redirect_error(f"REST {method} {path}", resp)
        if resp.status_code in (401, 403):
            raise CoverityError(f"Coverity の認証に失敗しました（{resp.status_code}）。" + _what_to_check(resp.status_code),
                                status=resp.status_code)
        if resp.status_code >= 400:
            try:
                data = resp.json()
                message = data.get("message", "") if isinstance(data, dict) else str(data)[:300]
            except ValueError:
                message = " ".join(resp.text.split())[:300]
            raise CoverityError(f"Coverity REST {method} {path} が失敗しました（{resp.status_code}）: {message}",
                                fatal=resp.status_code >= 500, status=resp.status_code)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise CoverityError(_not_json(f"REST {method} {path}", resp)) from exc

    def _soap(self, service: str, operation: str, body_xml: str) -> ET.Element:
        envelope = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:ws="{SOAP_NS}"><soapenv:Header><wsse:Security xmlns:wsse="{WSSE}">'
            f"<wsse:UsernameToken><wsse:Username>{escape(self.user)}</wsse:Username>"
            f'<wsse:Password Type="{PASSWORD_TEXT}">{escape(self.key)}</wsse:Password>'
            "</wsse:UsernameToken></wsse:Security></soapenv:Header>"
            f"<soapenv:Body><ws:{operation}>{body_xml}</ws:{operation}></soapenv:Body></soapenv:Envelope>"
        )
        started = time.monotonic()
        try:
            resp = self.http.post(f"/ws/v9/{service}", content=envelope.encode("utf-8"),
                                  headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""})
        except httpx.HTTPError as exc:
            self._record(f"SOAP {operation}", started, type(exc).__name__)
            raise CoverityError(f"Coverity に接続できません: {exc}") from exc
        self._record(f"SOAP {operation}", started, resp.status_code)
        if 300 <= resp.status_code < 400:
            raise _redirect_error(f"SOAP {operation}", resp)
        try:
            root: ET.Element | None = ET.fromstring(resp.content)
        except ET.ParseError:
            root = None
        node = next((n for n in root.iter() if _local(n.tag) == "faultstring"), None) if root is not None else None
        fault = (node.text or "").strip() if node is not None else None
        if resp.status_code in (401, 403) or (fault and AUTH_FAULT.search(fault)):
            raise CoverityError(f"Coverity の認証に失敗しました（SOAP {operation}、HTTP {resp.status_code}"
                                + (f"、{fault}" if fault else "") + "）。ユーザ名と認証キー、"
                                "Web サービスを使う権限（Access Web services）を確認してください",
                                status=resp.status_code)
        if root is None or _local(root.tag) != "Envelope":
            raise CoverityError(f"Coverity SOAP {operation} の応答が SOAP の XML ではありません（{_body_info(resp)}）。"
                                "coverity.url がログインの画面やプロキシに向いていないかを確かめてください")
        if fault is not None:
            raise CoverityError(f"Coverity SOAP {operation} が失敗しました: {fault}", fatal=False, status=resp.status_code)
        if resp.status_code >= 500:
            raise CoverityError(f"Coverity SOAP {operation} が失敗しました（HTTP {resp.status_code}）")
        response = next((n for n in root.iter() if _local(n.tag) == f"{operation}Response"), None)
        if response is None:
            raise CoverityError(f"Coverity SOAP {operation} の応答が想定と違います（HTTP {resp.status_code}）")
        return response

    # ---- search -----------------------------------------------------------------------------

    def columns(self) -> dict[str, str]:
        """Column name -> column key, kept for the life of the server process. An empty list is not
        kept, so that one wrong answer does not last until the server restarts."""
        key = self.config.url.rstrip("/")
        with _columns_lock:
            if key in _columns_cache:
                return _columns_cache[key]
        data = self._rest("GET", "/api/v2/issues/columns",
                          {"queryType": "bySnapshot", "retrieveGroupByColumns": "false"}) or []
        found = {c["name"]: c["columnKey"] for c in data
                 if isinstance(c, dict) and c.get("name") and c.get("columnKey")} if isinstance(data, list) else {}
        if found:
            with _columns_lock:
                _columns_cache[key] = found
        return found

    def _filters(self, spec: FilterSpec, stream: str) -> tuple[list[dict], frozenset[str]]:
        """Server-side filters, and the filter keys they cover."""
        filters = [{"columnKey": "streams", "matchMode": "oneOrMoreMatch",
                    "matchers": [{"class": "Stream", "name": stream, "type": "nameMatcher"}]}]
        names = {n.lower(): k for n, k in self.columns().items()}
        values = {"checkers": spec.checkers, "impacts": spec.impacts,
                  "classification": spec.triage.classification, "action": spec.triage.action}
        covered = {"streams"}
        for field, column in FILTER_COLUMNS.items():
            wanted = values[field]
            key = names.get(column.lower())
            if not wanted or not key or any(ch in v for v in wanted for ch in "*?["):
                continue
            filters.append({"columnKey": key, "matchMode": "oneOrMoreMatch", "field": field,
                            "matchers": [{"type": "keyMatcher", "key": v} for v in wanted]})
            covered.add(field)
        return filters, frozenset(covered)

    def search(self, spec: FilterSpec, limit: int, offset: int, seconds: float) -> SearchPage:
        started = time.monotonic()
        available = set(self.columns().values())
        missing = [COLUMN_KEYS[f] for f in REQUIRED_FIELDS if COLUMN_KEYS[f] not in available]
        if missing:
            raise CoverityError(f"Coverity の列の一覧（GET /api/v2/issues/columns）に {', '.join(missing)} がありません"
                                "（Coverity の管理者に確認してください）")
        keys = [k for k in COLUMN_KEYS.values() if k in available]
        stream = spec.streams[0]
        filters, on_server = self._filters(spec, stream)
        scope = ALL_SCOPE if ABSENT_STATUSES & set(spec.triage.status) else LATEST_SCOPE
        page_size = min(MAX_PAGE, max(limit, MIN_PAGE))
        issues: list[Issue] = []
        ends: list[int] = []
        excluded: list[dict[str, Any]] = []
        excluded_ends: list[int] = []
        ignored = unknown_statuses(spec)
        seen: set[int] = set()  # pages can overlap (the issues change between requests): each CID once
        total: int | None = None
        while True:
            page_started = time.monotonic()
            body = {"filters": [{k: v for k, v in f.items() if k != "field"} for f in filters], "columns": keys,
                    "snapshotScope": {"show": {"scope": scope, "includeOutdatedSnapshots": False}}}
            try:
                data = self._rest("POST", "/api/v2/issues/search", {
                    "includeColumnLabels": "true", "locale": "en_us", "offset": offset,
                    "queryType": "bySnapshot", "rowCount": page_size, "sortOrder": "asc"}, body) or {}
            except CoverityError as exc:
                if not self._drop_unknown_value(filters, exc, ignored):
                    raise
                if any(not f["matchers"] for f in filters):
                    # Every wanted value of one key is unknown: nothing can match.
                    return SearchPage(issues, 0, None, excluded, ends, excluded_ends, ignored)
                continue
            if not isinstance(data, dict):
                raise CoverityError(f"Coverity REST POST /api/v2/issues/search の応答の形が想定と違います: {str(data)[:120]}")
            rows = data.get("rows") or []
            total = _int(data.get("totalRows")) if total is None else total
            for index, row in enumerate(rows):
                issue = self._issue({c.get("key"): c.get("value") for c in row if isinstance(c, dict)}, stream)
                if issue is None or issue.cid in seen:
                    continue
                seen.add(issue.cid)
                mismatched = filter_mismatches(issue, spec, skip=on_server)
                if mismatched:
                    excluded.append({"cid": issue.cid,
                                     "mismatched": {k: issue_value(issue, k) for k in mismatched}})
                    excluded_ends.append(offset + index + 1)
                else:
                    issues.append(issue)
                    ends.append(offset + index + 1)
            offset += len(rows)
            # Without totalRows the search goes on until a page is empty.
            if not rows or (total is not None and offset >= total):
                return SearchPage(issues, total, None, excluded, ends, excluded_ends, ignored)
            now = time.monotonic()
            if len(issues) >= limit or (now - started) + (now - page_started) > seconds:
                return SearchPage(issues, total, offset, excluded, ends, excluded_ends, ignored)

    def _drop_unknown_value(self, filters: list[dict], exc: CoverityError, ignored: list[dict[str, str]]) -> bool:
        found = INVALID_KEY.search(str(exc))
        if not found:
            return False
        value = found.group(1)
        dropped = False
        for f in filters[1:]:
            kept = [m for m in f["matchers"] if m.get("key") != value]
            if len(kept) != len(f["matchers"]):
                f["matchers"], dropped = kept, True
                name = FILTER_NAMES.get(f.get("field", ""), f["columnKey"])
                ignored.append({"filter": name, "value": value})
                self.requests.append({"request": f"{f['columnKey']} の条件から外した値（サーバーにない）",
                                      "status": "ignored", "filter": name, "value": value, "seconds": 0})
        return dropped

    @staticmethod
    def _issue(row: dict[str, Any], stream: str) -> Issue | None:
        cid = _int(row.get("cid"))
        if cid is None:
            return None

        def value(field: str) -> str | None:
            v = row.get(COLUMN_KEYS[field])
            return str(v).strip() or None if v is not None else None

        return Issue(cid=cid, checker=value("checker") or "", file=value("file") or "",
                     line=_int(row.get("lineNumber")), function=value("function"),
                     impact=value("impact"), category=value("category"), cwe=_int(row.get("cwe")),
                     merge_key=value("merge_key"), classification=value("classification"),
                     action=value("action"), severity=value("severity"), status=value("status"),
                     stream=stream)

    # ---- warning path -----------------------------------------------------------------------

    def issue_detail(self, cid: int, stream: str) -> IssueDetail:
        # filterSpec in the order of the XSD.
        body = (f"<mergedDefectIdDataObjs><cid>{cid}</cid></mergedDefectIdDataObjs>"
                "<filterSpec><includeDefectInstances>true</includeDefectInstances>"
                "<includeHistory>false</includeHistory>"
                "<includeTotalDefectInstanceCount>false</includeTotalDefectInstanceCount>"
                f"<maxDefectInstances>{MAX_DEFECT_INSTANCES}</maxDefectInstances>"
                f"<streamIdList><name>{escape(stream)}</name></streamIdList></filterSpec>")
        response = self._soap("defectservice", "getStreamDefects", body)
        defect = instance = None
        for candidate in _children(response, "return"):
            instance = _child(candidate, "defectInstances")
            if instance is not None:
                defect = candidate
                break
        if instance is None or defect is None:
            raise CoverityError(f"CID {cid} の警告経路がストリーム {stream} にありません", fatal=False)
        events: list[Event] = []

        def walk(node: ET.Element) -> None:
            for ev in _children(node, "events"):
                path = _text(ev, "fileId", "filePathname")
                if path:
                    events.append(Event(file=path, line=_int(_text(ev, "lineNumber")),
                                        tag=_text(ev, "eventTag"), description=_text(ev, "eventDescription"),
                                        main=_text(ev, "main").lower() == "true"))
                walk(ev)  # nested events, for example inside a called function

        walk(instance)
        main = next((e for e in events if e.main), events[-1] if events else None)
        issue = Issue(cid=cid, checker=_text(instance, "checkerName") or _text(defect, "checkerName"),
                      file=main.file if main else "", line=main.line if main else None,
                      function=_text(instance, "function", "functionDisplayName") or None,
                      impact=_text(instance, "impact", "displayName") or None,
                      category=_text(instance, "category", "displayName") or None,
                      cwe=_int(_text(instance, "cwe")), stream=stream)
        description = "\n\n".join(t for t in (_text(instance, "longDescription"),
                                              _text(instance, "localEffect")) if t)
        return IssueDetail(issue=issue, events=events, checker_description=description)

    # ---- snapshots --------------------------------------------------------------------------

    def snapshot_revision(self, stream: str, field: str) -> str | None:
        response = self._soap("configurationservice", "getSnapshotsForStream",
                              f"<streamId><name>{escape(stream)}</name></streamId>")
        ids = [i for i in (_int(_text(r, "id")) for r in _children(response, "return")) if i is not None]
        if not ids:
            return None
        info = self._soap("configurationservice", "getSnapshotInformation",
                          f"<snapshotIds><id>{max(ids)}</id></snapshotIds>")
        record = _child(info, "return")
        return (_text(record, field) or None) if record is not None else None

    # ---- write-back -------------------------------------------------------------------------

    def triage_store_of(self, stream: str) -> str:
        """``streams[0].triageStoreName`` of GET /api/v2/streams/{name} (a list even for one name)."""
        if stream not in self._stores:
            path = f"/api/v2/streams/{quote(stream, safe='')}"
            try:
                data = self._rest("GET", path, {"locale": "en_us"})
            except CoverityError as exc:
                if exc.status == 404:
                    raise CoverityError(f"ストリーム {stream} が Coverity にありません。書き戻しの項目の stream に、"
                                        "警告のあるストリームの名前を書いてください", fatal=False, status=404) from exc
                raise
            streams = data.get("streams") if isinstance(data, dict) else None
            first = streams[0] if isinstance(streams, list) and streams and isinstance(streams[0], dict) else {}
            name = str(first.get("triageStoreName") or "").strip()
            if not name:
                raise CoverityError(f"ストリーム {stream} が使うトリアージストアが、Coverity の応答（REST GET {path}）に"
                                    "ありません。Coverity の管理者に、ストリームのトリアージストアを確認してください",
                                    fatal=False)
            self._stores[stream] = name
        return self._stores[stream]

    def write_triage(self, cids: list[int], attributes: TriageAttributes, comment: str,
                     stream: str | None = None) -> str:
        # The store the stream uses: another store would change the triage of the streams that use it.
        store = self.triage_store_of(stream) if stream else self.config.triage_store
        values = [("Classification", attributes.classification), ("Action", attributes.action),
                  ("Severity", attributes.severity), ("Comment", comment)]
        body = {"cids": list(cids),
                "attributeValuesList": [{"attributeName": n, "attributeValue": v} for n, v in values if v]}
        self._rest("PUT", "/api/v2/issues/triage", {"triageStoreName": store}, body)
        return store
