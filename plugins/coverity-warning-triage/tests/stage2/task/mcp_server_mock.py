"""試験 test_mcp_server.py が使う、Coverity Connect の応答を返す httpx の MockTransport。

応答の形は、試験用の Coverity Connect エミュレーターの仕様書の見本に合わせる。
- REST：GET /api/v2/issues/columns（列の一覧の配列）、POST /api/v2/issues/search（{offset, totalRows, columns, rows}。
  値はすべて文字列）、PUT /api/v2/issues/triage（成功は 200 で本文なし）。
- 認証の失敗：Authorization ヘッダーがあって誤っていれば 401 と本文 "Authentication failed.\\n"（Content-Type なし）。
  ヘッダーがなければ 302 で /login/login.htm へ。
- SOAP v9：getStreamDefects、getSnapshotsForStream、getSnapshotInformation。子の要素は名前空間なし。
  認証の失敗は HTTP 401 と faultstring "User authentication failed." の Fault（detail なし）。
  業務のエラーは HTTP 500 と detail の CoverityFault（errorCode、message）。
"""
from __future__ import annotations

import base64
import json
import xml.etree.ElementTree as ET
from html import escape
from typing import Any, Callable, Optional

import httpx

BASE = "https://coverity.example.com:8443"
USER = "triage-bot"
KEY = "k3y-Secret-Value-0123456789"
STREAM = "libev-main"
LOGIN = BASE + "/login/login.htm"

# GET /api/v2/issues/columns の公式の見本から、プラグインが使う列とそのほかの列を抜いたもの
COLUMNS = [
    {"columnKey": "cid", "name": "CID"},
    {"columnKey": "checker", "name": "Checker"},
    {"columnKey": "displayImpact", "name": "Impact"},
    {"columnKey": "displayCategory", "name": "Category"},
    {"columnKey": "displayType", "name": "Type"},
    {"columnKey": "cwe", "name": "CWE"},
    {"columnKey": "displayIssueKind", "name": "Issue Kind"},
    {"columnKey": "status", "name": "Status"},
    {"columnKey": "firstDetected", "name": "First Detected"},
    {"columnKey": "owner", "name": "Owner"},
    {"columnKey": "classification", "name": "Classification"},
    {"columnKey": "severity", "name": "Severity"},
    {"columnKey": "action", "name": "Action"},
    {"columnKey": "displayComponent", "name": "Component"},
    {"columnKey": "displayFile", "name": "File"},
    {"columnKey": "displayFunction", "name": "Function"},
    {"columnKey": "functionMergeName", "name": "Function Merge Name"},
    {"columnKey": "mergeExtra", "name": "Merge Extra"},
    {"columnKey": "mergeKey", "name": "Merge Key"},
    {"columnKey": "lineNumber", "name": "Line Number"},
    {"columnKey": "lastTriageComment", "name": "Last Triage Comment"},
    {"columnKey": "column_standard_CERT C", "name": "Standard: CERT C"},
    {"columnKey": "project", "name": "Project"},
]

# 認証なしの要求に返った 302 の本文（実測の見本）
LOGIN_HTML = ('<html><body><p>Redirecting to <a href="https:&#47;&#47;coverity.example.com:8443&#47;login&#47;login.htm">'
              'https:&#47;&#47;coverity.example.com:8443&#47;login&#47;login.htm</a></p></body></html>')
SET_COOKIE = "COVJSESSIONID-build=8BC68328F1F177922B6CC0297AB000C4; Path=/; Secure; HttpOnly"

SOAP_XML_TYPE = "text/xml;charset=utf-8"
SOAP_AUTH_FAULT = ("<?xml version='1.0' encoding='UTF-8'?>"
                   '<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>'
                   '<S:Fault xmlns:ns4="http://www.w3.org/2003/05/soap-envelope"><faultcode>S:Server</faultcode>'
                   "<faultstring>User authentication failed.</faultstring></S:Fault></S:Body></S:Envelope>")


def soap_fault(faultstring: str, error_code: Optional[int] = None) -> str:
    detail = ""
    if error_code is not None:
        detail = ('<detail><ns2:CoverityFault xmlns:ns2="http://ws.coverity.com/v9">'
                  f"<errorCode>{error_code}</errorCode><message>{escape(faultstring)}</message>"
                  "</ns2:CoverityFault></detail>")
    return ("<?xml version='1.0' encoding='UTF-8'?>"
            '<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>'
            '<S:Fault xmlns:ns4="http://www.w3.org/2003/05/soap-envelope"><faultcode>S:Server</faultcode>'
            f"<faultstring>{escape(faultstring)}</faultstring>{detail}</S:Fault></S:Body></S:Envelope>")


def soap_ok(operation: str, inner: str) -> str:
    return ("<?xml version='1.0' encoding='UTF-8'?>"
            '<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>'
            f'<ns2:{operation}Response xmlns:ns2="http://ws.coverity.com/v9">{inner}</ns2:{operation}Response>'
            "</S:Body></S:Envelope>")


def event(description: str, kind: str, tag: str, line: int, main: bool = False,
          nested: Optional[list] = None, file: str = "/builddir/build/BUILD/libev-4.33/ev_iouring.c") -> dict:
    return {"description": description, "kind": kind, "tag": tag, "line": line, "main": main,
            "nested": nested or [], "file": file}


def issue(cid: int, checker: str = "USE_AFTER_FREE", status: str = "New", **values: str) -> dict:
    """検索の1行（列キー → 文字列）と、SOAP の警告経路を作るための値。"""
    row = {"cid": str(cid), "checker": checker, "displayFile": "/builddir/build/BUILD/libev-4.33/ev_iouring.c",
           "lineNumber": str(200 + cid % 100), "displayFunction": "iouring_sqe_get", "displayImpact": "High",
           "displayCategory": "Memory - illegal accesses", "displayType": "Read from pointer after free",
           "cwe": "416", "displayIssueKind": "Quality", "mergeKey": "dd7cea15ef54c17c70b8c3657d0%05d" % cid,
           "classification": "Unclassified", "action": "Undecided", "severity": "Unspecified",
           "status": status, "owner": "", "lastTriageComment": "", "project": "libev"}
    row.update(values)
    return {"row": row, "stream": STREAM, "events": [], "long": "", "local": ""}


def use_after_free(cid: int = 10001) -> dict:
    """仕様書の getStreamDefects の見本の警告（入れ子の MODEL のイベントを持つ）。"""
    item = issue(cid, lineNumber="261")
    item["events"] = [
        event('Condition "res >= 0", taking false branch.', "PATH", "path", 274),
        event('"iouring_poll" frees "loop->iouring_sq_ring".', "MODEL", "freed_arg", 280, nested=[
            event('Condition "iouring_handle_cq(loop)", taking false branch.', "PATH", "path", 641)]),
        event('Using freed pointer "loop->iouring_sq_ring".', "NORMAL", "use_after_free", 261, main=True),
    ]
    item["long"] = "Reads target of a freed pointer"
    item["local"] = "Incorrect values may be read from freed memory, causing incorrect computations."
    return item


def checked_return(cid: int = 10002) -> dict:
    item = issue(cid, checker="CHECKED_RETURN", displayImpact="Medium", displayCategory="Error handling issues",
                 cwe="252", displayFunction="ev_run", lineNumber="3112", displayFile="/builddir/build/BUILD/libev-4.33/ev.c")
    item["events"] = [event('Calling "read" without checking return value (as is done elsewhere 4 out of 5 times).',
                            "NORMAL", "check_return", 3112, main=True, file="/builddir/build/BUILD/libev-4.33/ev.c")]
    item["long"] = "Value returned from a function is not checked for errors before being used"
    return item


def _events_xml(events: list) -> str:
    out = []
    for i, ev in enumerate(events, 1):
        out.append(
            "<events>" + _events_xml(ev["nested"])
            + f"<eventDescription>{escape(ev['description'])}</eventDescription><eventKind>{ev['kind']}</eventKind>"
            f"<eventNumber>{i}</eventNumber><eventSet>0</eventSet><eventTag>{ev['tag']}</eventTag>"
            f"<fileId><contentsMD5>0123456789abcdef0123456789abcdef</contentsMD5><filePathname>{escape(ev['file'])}"
            f"</filePathname></fileId><id>{20000 + i}</id><lineNumber>{ev['line']}</lineNumber>"
            f"<main>{'true' if ev['main'] else 'false'}</main><polarity>false</polarity></events>")
    return "".join(out)


def defect_xml(item: dict) -> str:
    """streamDefectDataObj を XSD の順で組み立てる。"""
    row = item["row"]
    function = (f"<function><fileId><contentsMD5>0123456789abcdef0123456789abcdef</contentsMD5><filePathname>"
                f"{escape(row['displayFile'])}</filePathname></fileId><functionDisplayName>{escape(row['displayFunction'])}"
                f"</functionDisplayName><functionMangledName>{escape(row['displayFunction'])}</functionMangledName>"
                f"<functionMergeName>{escape(row['displayFunction'])}</functionMergeName></function>")
    instance = ("<defectInstances>" + _events_xml(item["events"])
                + f"<category><displayName>{escape(row['displayCategory'])}</displayName><name>{escape(row['displayCategory'])}"
                f"</name></category><checkerName>{row['checker']}</checkerName><component>Other</component>"
                f"<cwe>{row['cwe']}</cwe><domain>STATIC_C</domain><extra>iouring_sq_ring,loop</extra>{function}"
                f"<id><id>30001</id></id><impact><displayName>{row['displayImpact']}</displayName><name>{row['displayImpact']}"
                "</name></impact><issueKinds><displayName>Quality</displayName><name>QUALITY</name></issueKinds>"
                + (f"<localEffect>{escape(item['local'])}</localEffect>" if item["local"] else "")
                + (f"<longDescription>{escape(item['long'])}</longDescription>" if item["long"] else "")
                + "<subcategory>deref_read_after_free</subcategory>"
                f"<type><displayName>{escape(row['displayType'])}</displayName><name>{escape(row['displayType'])}</name></type>"
                "</defectInstances>")
    return (f"<return><checkerName>{row['checker']}</checkerName><cid>{row['cid']}</cid>{instance}"
            "<defectStateAttributeValues><attributeDefinitionId><name>DefectStatus</name></attributeDefinitionId>"
            f"<attributeValueId><name>{row['status']}</name></attributeValueId></defectStateAttributeValues>"
            "<domain>STATIC_C</domain><id><defectTriageId>10024</defectTriageId><defectTriageVerNum>1</defectTriageVerNum>"
            f"<id>20045</id><verNum>1</verNum></id><streamId><name>{escape(item['stream'])}</name></streamId></return>")


def json_response(status: int, data: Any) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"})


def rest_401() -> httpx.Response:
    return httpx.Response(401, content=b"Authentication failed.\n", headers={"Set-Cookie": SET_COOKIE})


def redirect_302(location: str = LOGIN) -> httpx.Response:
    return httpx.Response(302, content=LOGIN_HTML.encode("utf-8"),
                          headers={"Location": location, "Set-Cookie": SET_COOKIE,
                                   "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate"})


def html_error(status: int) -> httpx.Response:
    """前に置いたプロキシやロードバランサーが返す 502・503・504 の HTML のページ。"""
    reason = {502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}.get(status, "Error")
    page = f"<html><head><title>{status} {reason}</title></head><body><h1>{status} {reason}</h1></body></html>"
    return httpx.Response(status, content=page.encode("utf-8"), headers={"Content-Type": "text/html"})


def html_200() -> httpx.Response:
    page = ("<!DOCTYPE html><html><head><title>Coverity Connect | Login</title></head>"
            "<body><form action=\"/login/login.htm\" method=\"post\"><input name=\"username\"><br></form></body></html>")
    return httpx.Response(200, content=page.encode("utf-8"), headers={"Content-Type": "text/html;charset=UTF-8"})


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _find(node: ET.Element, name: str) -> Optional[ET.Element]:
    return next((n for n in node.iter() if _local(n.tag) == name), None)


class FakeCoverity:
    """要求を記録し、見本の形で答える。属性を変えると、異常の応答を返す。"""

    def __init__(self, items: Optional[list] = None):
        self.items = items if items is not None else [use_after_free(), checked_return()]
        self.columns_queue: list[Callable[[], httpx.Response]] = []  # 先頭から1回ずつ、列の一覧の代わりに返す
        self.omit_total = False
        self.unknown_keys: set[str] = set()  # 検索で 500「Invalid key provided」にする key
        self.rest_override: Optional[Callable[[httpx.Request], Optional[httpx.Response]]] = None
        self.soap_override: Optional[Callable[[httpx.Request], Optional[httpx.Response]]] = None
        self.soap_faults: dict[int, httpx.Response] = {}  # CID → getStreamDefects の応答
        self.triage_responses: dict[int, Callable[[], httpx.Response]] = {}  # CID → PUT の応答
        self.snapshots = [10001, 10005]
        self.source_version = "a1b2c3d4e5"
        self.requests: list[str] = []
        self.writes: list[dict] = []
        self.soap_requests: list[ET.Element] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- 共通 -----------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if request.url.path.startswith("/ws/v9/"):
            if self.soap_override:
                answer = self.soap_override(request)
                if answer is not None:
                    return answer
            return self._soap(request)
        if self.rest_override:
            answer = self.rest_override(request)
            if answer is not None:
                return answer
        header = request.headers.get("Authorization")
        if not header:
            return redirect_302()
        if header != "Basic " + base64.b64encode(f"{USER}:{KEY}".encode()).decode():
            return rest_401()
        path = request.url.path
        if request.method == "GET" and path == "/api/v2/issues/columns":
            return self.columns_queue.pop(0)() if self.columns_queue else json_response(200, COLUMNS)
        if request.method == "POST" and path == "/api/v2/issues/search":
            return self._search(request)
        if request.method == "PUT" and path == "/api/v2/issues/triage":
            return self._triage(request)
        return json_response(404, {"code": 1004, "message": f"No handler for {path}"})

    # ---- REST -----------------------------------------------------------------------------

    def _search(self, request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        count = int(request.url.params.get("rowCount", "200"))
        body = json.loads(request.content)
        matched = list(self.items)
        for spec in body["filters"]:
            for m in spec["matchers"]:
                if m.get("type") == "keyMatcher" and m.get("key") in self.unknown_keys:
                    return json_response(500, {"code": 1006, "message": f"Invalid key provided: {m['key']}"})
            if spec["columnKey"] == "streams":
                names = {m["name"] for m in spec["matchers"]}
                matched = [i for i in matched if i["stream"] in names]
            else:
                keys = {m["key"] for m in spec["matchers"]}
                matched = [i for i in matched if i["row"].get(spec["columnKey"]) in keys]
        rows = [[{"key": k, "value": i["row"].get(k, "")} for k in body["columns"]]
                for i in matched[offset:offset + count]]
        data: dict[str, Any] = {"offset": offset, "totalRows": len(matched), "columns": body["columns"], "rows": rows}
        if self.omit_total:
            del data["totalRows"]
        return json_response(200, data)

    def _triage(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        for cid in body["cids"]:
            if cid in self.triage_responses:
                return self.triage_responses[cid]()
        self.writes.append({"store": request.url.params.get("triageStoreName"), **body})
        return httpx.Response(200)

    # ---- SOAP -----------------------------------------------------------------------------

    def _soap(self, request: httpx.Request) -> httpx.Response:
        root = ET.fromstring(request.content)
        user, password = _find(root, "Username"), _find(root, "Password")
        if user is None or password is None or (user.text, password.text) != (USER, KEY):
            return httpx.Response(401, content=SOAP_AUTH_FAULT.encode("utf-8"), headers={"Content-Type": SOAP_XML_TYPE})
        body = _find(root, "Body")
        op = list(body)[0]
        name = _local(op.tag)
        self.soap_requests.append(op)
        if name == "getStreamDefects":
            cid = int(_find(op, "cid").text)
            stream = _find(_find(op, "streamIdList"), "name").text
            if cid in self.soap_faults:
                return self.soap_faults[cid]
            found = [i for i in self.items if i["row"]["cid"] == str(cid) and i["stream"] == stream]
            inner = defect_xml(found[0]) if found else ""  # ストリームにない CID は return なしの空の応答
            return self._xml(200, soap_ok(name, inner))
        if name == "getSnapshotsForStream":
            return self._xml(200, soap_ok(name, "".join(f"<return><id>{i}</id></return>" for i in self.snapshots)))
        if name == "getSnapshotInformation":
            sid = _find(op, "id").text
            inner = (f"<return><analysisHost>build01</analysisHost><buildFailureCount>0</buildFailureCount>"
                     f"<dateCreated>2026-10-30T12:14:55.821+09:00</dateCreated><hasSummaries>true</hasSummaries>"
                     f"<purgedOfDetails>false</purgedOfDetails><snapshotId><id>{sid}</id></snapshotId>"
                     f"<sourceVersion>{self.source_version}</sourceVersion><target>Linux x86_64</target></return>")
            return self._xml(200, soap_ok(name, inner))
        return self._xml(500, soap_fault(f"Unknown operation {name}"))

    @staticmethod
    def _xml(status: int, text: str) -> httpx.Response:
        return httpx.Response(status, content=text.encode("utf-8"), headers={"Content-Type": SOAP_XML_TYPE})
