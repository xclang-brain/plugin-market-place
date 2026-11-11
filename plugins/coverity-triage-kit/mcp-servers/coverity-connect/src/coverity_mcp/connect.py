"""Coverity Connect client: the requests the four tools need.

REST API v2: the streams the user can see (GET /api/v2/streams; also the connection check), one stream
and its triage store (GET /api/v2/streams/{name}), the warnings of the latest snapshot in pages
(POST /api/v2/issues/search) and the triage write (PUT /api/v2/issues/triage, Coverity Connect 2022.6.0
or later). SOAP API v9: the events of a CID (getStreamDefects).

How answers are sorted (from answers recorded on Coverity Connect 2024.12):
- 401 / 403, 5xx, a connection failure, a 3xx and a body that is not JSON or SOAP (the URL leads to a
  login page or a proxy) stop the call: Failure.
- Another 4xx, a SOAP fault, and HTTP 500 "Invalid key provided: X" (a value the server does not know)
  fail only the request they answer: Refused.
"""

from __future__ import annotations

import re
import ssl
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from html import escape
from urllib.parse import quote

import httpx
import truststore

from .settings import Failure, credentials

TIMEOUT = 15.0  # seconds for one request (a tool call has 20; clients stop a call after 30)
PAGE = 200  # the most rows asked for in one search request
COLUMNS = ("cid", "checker", "displayFile", "lineNumber", "displayFunction", "displayImpact")
FILTER_COLUMNS = {"checkers": "checker", "impacts": "displayImpact", "classification": "classification"}
INVALID_KEY = re.compile(r"Invalid key provided:\s*(.+?)\s*$")
AUTH_FAULT = re.compile(r"authentication failed|doesn't have permissions to perform", re.IGNORECASE)
STREAM_NOT_FOUND = "1300"  # errorCode of a SOAP fault
SOAP_NS = "http://ws.coverity.com/v9"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
PASSWORD_TEXT = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"


class Refused(Failure):
    """Coverity refused one request. ``reason`` is the sentence for one CID or entry."""

    def __init__(self, status: int, text: str):
        self.status, self.text = status, text
        self.reason = f"Coverity が要求を受け付けませんでした（HTTP {status}: {text}）"
        super().__init__(self.reason + "。設定ファイルの coverity.url と conditions を確認してください")


def unknown_stream(stream: str) -> Failure:
    return Failure(f"ストリーム {stream} が見つかりません。設定ファイルの coverity.stream を確認してください")


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _local(child.tag) == name]


def _text(node: ET.Element | None, *path: str) -> str:
    for name in path:
        node = next(iter(_children(node, name)), None) if node is not None else None
    return (node.text or "").strip() if node is not None else ""


def to_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class Client:
    def __init__(self, url: str):
        self.url = url
        self.user, self.key = credentials()
        self.slowest = 0.0  # seconds of the slowest request so far
        try:  # company PCs hold the in-house CA in the certificate store of the OS
            # httpx takes the proxy of the OS (Windows: the registry) but never asks the OS for its exceptions
            direct = bool(urllib.request.proxy_bypass(httpx.URL(url).host))
            self.http = httpx.Client(base_url=url, timeout=TIMEOUT, follow_redirects=False, trust_env=not direct,
                                     verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        except httpx.InvalidURL as exc:
            raise self._no_connection(exc) from None

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.http.close()

    def _clean(self, text) -> str:
        """Text of the server or of an exception: on one line, short, and never with the key."""
        return " ".join(str(text or "").replace(self.key, "****").split())[:200]

    def _no_connection(self, exc: Exception) -> Failure:
        reason = self._clean(f"{type(exc).__name__} {exc}")
        return Failure(f"Coverity に接続できませんでした（{self.url}: {reason}）。"
                       "設定ファイルの coverity.url とネットワークを確認してください")

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        # Every request authenticates by itself. The session cookie of an earlier answer is not sent back:
        # with it, a server can ask a POST or PUT for the CSRF token of a browser session.
        self.http.cookies.clear()
        started = time.monotonic()
        try:
            return self.http.request(method, path, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise self._no_connection(exc) from None
        finally:
            self.slowest = max(self.slowest, time.monotonic() - started)

    @staticmethod
    def _not_authenticated(status: int) -> Failure:
        # A 403 can come with the right credentials: the user lacks the permission the API needs.
        permission = "、ユーザーの権限（Access Web services）" if status == 403 else " "
        text = f"認証できませんでした（HTTP {status}）。COV_USER と COV_AUTH_KEY{permission}を確認してください"
        if sys.platform == "win32":  # a value saved again reaches only a copilot that is started after it
            text += "。保存し直したときは、新しいターミナルで copilot を起動し直してください"
        return Failure(text)

    @staticmethod
    def _not_coverity(status: int) -> Failure:
        return Failure(f"Coverity Connect の応答ではありません（HTTP {status}）。"
                       "設定ファイルの coverity.url を確認してください")

    def _rest(self, method: str, path: str, params: dict | None = None, body=None):
        """The JSON of the answer (None for an empty body)."""
        resp = self._send(method, path, params=params, json=body, auth=(self.user, self.key),
                          headers={"Accept": "application/json"})
        status = resp.status_code
        if status in (401, 403):
            raise self._not_authenticated(status)
        try:
            data, is_json = (resp.json() if resp.content else None), True
        except ValueError:
            data, is_json = None, False
        if status >= 400:
            text = self._clean(data.get("message") if isinstance(data, dict) else resp.text)
            if status < 500 or INVALID_KEY.search(text):
                raise Refused(status, text)
            raise Failure(f"Coverity がエラーを返しました（HTTP {status}: {text}）。"
                          "しばらくしてから、もう一度実行してください")
        if status >= 300 or not is_json:
            raise self._not_coverity(status)
        return data

    # ---- streams ----------------------------------------------------------------------------

    def streams(self) -> list[str]:
        """The names of the streams the user can see, sorted."""
        # excludeRoles: the role assignments of every stream make the answer large.
        data = self._rest("GET", "/api/v2/streams", {"namePattern": "*", "excludeRoles": "true", "locale": "en_us"})
        items = data.get("streams") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise self._not_coverity(200)
        return sorted({str(item.get("name") or "").strip() for item in items if isinstance(item, dict)} - {""})

    def stream(self, name: str) -> dict:
        """One stream (``triageStoreName`` is the store its triage is written to)."""
        try:
            data = self._rest("GET", f"/api/v2/streams/{quote(name, safe='')}", {"locale": "en_us"})
        except Refused as exc:
            if exc.status == 404:
                raise unknown_stream(name) from None
            raise
        items = data.get("streams") if isinstance(data, dict) else None  # a list even for one name
        return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}

    # ---- search -----------------------------------------------------------------------------

    def search(self, stream: str, filters: dict[str, list[str]], offset: int, rows: int) -> tuple[list[dict], int | None]:
        """One page of the latest snapshot: the rows as {column key: value}, and totalRows."""
        wanted = [{"columnKey": "streams", "matchMode": "oneOrMoreMatch",
                   "matchers": [{"class": "Stream", "name": stream, "type": "nameMatcher"}]}]
        wanted += [{"columnKey": FILTER_COLUMNS[name], "matchMode": "oneOrMoreMatch",
                    "matchers": [{"type": "keyMatcher", "key": value} for value in values]}
                   for name, values in filters.items() if values]
        body = {"filters": wanted, "columns": list(COLUMNS),
                "snapshotScope": {"show": {"scope": "last()", "includeOutdatedSnapshots": False}}}
        data = self._rest("POST", "/api/v2/issues/search", {
            "includeColumnLabels": "true", "locale": "en_us", "offset": offset, "queryType": "bySnapshot",
            "rowCount": rows, "sortOrder": "asc"}, body)
        if not isinstance(data, dict):
            raise self._not_coverity(200)
        found = [{cell.get("key"): cell.get("value") for cell in row if isinstance(cell, dict)}
                 for row in data.get("rows") or []]
        return found, to_int(data.get("totalRows"))

    # ---- events -----------------------------------------------------------------------------

    def events(self, cid: int, stream: str) -> dict | None:
        """``{"events", "checker_description"}`` of a CID; None when the stream does not hold it."""
        envelope = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:ws="{SOAP_NS}"><soapenv:Header><wsse:Security xmlns:wsse="{WSSE}">'
            f"<wsse:UsernameToken><wsse:Username>{escape(self.user)}</wsse:Username>"
            f'<wsse:Password Type="{PASSWORD_TEXT}">{escape(self.key)}</wsse:Password>'
            "</wsse:UsernameToken></wsse:Security></soapenv:Header><soapenv:Body><ws:getStreamDefects>"
            f"<mergedDefectIdDataObjs><cid>{cid}</cid></mergedDefectIdDataObjs>"
            # filterSpec in the order of the XSD; only the first defect instance is read
            "<filterSpec><includeDefectInstances>true</includeDefectInstances>"
            "<includeHistory>false</includeHistory>"
            "<includeTotalDefectInstanceCount>false</includeTotalDefectInstanceCount>"
            f"<maxDefectInstances>1</maxDefectInstances><streamIdList><name>{escape(stream)}</name></streamIdList>"
            "</filterSpec></ws:getStreamDefects></soapenv:Body></soapenv:Envelope>")
        resp = self._send("POST", "/ws/v9/defectservice", content=envelope.encode("utf-8"),
                          headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""})
        status = resp.status_code
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            root = None
        nodes = list(root.iter()) if root is not None else []
        fault = next((node for node in nodes if _local(node.tag) == "Fault"), None)
        text = self._clean(_text(fault, "faultstring"))
        if status in (401, 403) or (fault is not None and AUTH_FAULT.search(text)):
            raise self._not_authenticated(status if status in (401, 403) else 403)
        if root is None or _local(root.tag) != "Envelope":
            raise self._not_coverity(status)
        if fault is not None:
            if any(_local(node.tag) == "errorCode" and (node.text or "").strip() == STREAM_NOT_FOUND for node in nodes):
                raise unknown_stream(stream)
            raise Refused(status, text)
        response = next((node for node in nodes if _local(node.tag) == "getStreamDefectsResponse"), None)
        if response is None:
            raise self._not_coverity(status)
        instance = next((found for defect in _children(response, "return")
                         for found in _children(defect, "defectInstances")), None)
        if instance is None:
            return None
        events: list[dict] = []

        def walk(node: ET.Element) -> None:
            for event in _children(node, "events"):
                path = _text(event, "fileId", "filePathname")
                if path:
                    events.append({"file": path, "line": to_int(_text(event, "lineNumber")),
                                   "tag": _text(event, "eventTag"), "description": _text(event, "eventDescription"),
                                   "main": _text(event, "main").lower() == "true"})
                walk(event)  # nested events, for example inside a called function

        walk(instance)
        description = "\n\n".join(part for part in (_text(instance, "longDescription"),
                                                    _text(instance, "localEffect")) if part)
        return {"events": events, "checker_description": description}

    # ---- triage -----------------------------------------------------------------------------

    def write_triage(self, store: str, cids: list[int], entry: dict) -> None:
        values = [{"attributeName": name, "attributeValue": entry[name.lower()]}
                  for name in ("Classification", "Action", "Severity", "Comment")]
        self._rest("PUT", "/api/v2/issues/triage", {"triageStoreName": store},
                   {"cids": cids, "attributeValuesList": values})
