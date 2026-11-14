"""A small Coverity Connect for tests: what the MCP server coverity-connect calls, on 127.0.0.1.

    GET  /api/v2/streams            the streams of the warnings
    GET  /api/v2/streams/{name}     one stream and its triage store (404 for a name it does not have)
    POST /api/v2/issues/search      the warnings of a stream, filtered and in pages
    PUT  /api/v2/issues/triage      keeps the triage values and changes the warnings
    POST /ws/v9/defectservice       SOAP getStreamDefects: the events of a CID
    GET  /emulator/writes           test only: {"writes": [the triage values received, in order]}

The warnings come from a YAML file: ``issues:`` with ``cid``, ``stream``, ``checker``, ``file``,
``function``, ``line``, ``impact``, ``classification``, ``checker_description`` and ``events[]``
(``file``, ``line``, ``tag``, ``description``, ``main``; an event may hold nested ``events``).

In a test process:

    with Emulator(data_file) as emu:     # emu.url, emu.user, emu.key, emu.writes, emu.requests

As a child process (current folder: tests/), which prints one JSON line {"url", "user", "key"}:

    python -m coverity_emulator --data <file> --port 0

The answers follow recorded answers of Coverity Connect 2024.12. Every answer sets a session cookie, as
the real server does; a POST or PUT that sends it back is refused for its missing CSRF token. That
refusal is not recorded from a real server: it is the strict case, so that the MCP server never comes to
depend on a session. Python standard library and PyYAML.
"""

from __future__ import annotations

import argparse
import base64
import json
import threading
import time
import xml.etree.ElementTree as ET
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures/sample/sample-target/.coverity-triage/fake-issues.yaml"
USER, KEY = "emulator-user", "emulator-key-5b1f9c2e77"
STORE = "Default Triage Store"
SESSION = "COVJSESSIONID-build"
COLUMNS = {"cid": "cid", "checker": "checker", "displayFile": "file", "lineNumber": "line",
           "displayFunction": "function", "displayImpact": "impact", "classification": "classification"}
# The values the server knows. A search or a triage write with another value is refused as a whole.
KNOWN = {"displayImpact": {"High", "Medium", "Low", "Audit"},
         "classification": {"Unclassified", "Pending", "False Positive", "Intentional", "Bug"},
         "action": {"Undecided", "Fix Required", "Fix Submitted", "Modeling Required", "Ignore"},
         "severity": {"Unspecified", "Major", "Moderate", "Minor"}}
SOAP = ("<?xml version='1.0' encoding='UTF-8'?>"
        '<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>{}</S:Body></S:Envelope>')
TEXT, JSON, XML = "text/plain", "application/json", "text/xml;charset=utf-8"


def _json(status: int, data) -> tuple[int, str, bytes]:
    return status, JSON, json.dumps(data, ensure_ascii=False).encode("utf-8")


def _error(status: int, code: int | None, message: str) -> tuple[int, str, bytes]:
    return _json(status, {"code": code, "message": message})


def _fault(status: int, text: str, code: int | None = None) -> tuple[int, str, bytes]:
    detail = "" if code is None else (f'<detail><ns2:CoverityFault xmlns:ns2="http://ws.coverity.com/v9"><errorCode>{code}'
                                      f"</errorCode><message>{escape(text)}</message></ns2:CoverityFault></detail>")
    return status, XML, SOAP.format("<S:Fault><faultcode>S:Server</faultcode>"
                                    f"<faultstring>{escape(text)}</faultstring>{detail}</S:Fault>").encode("utf-8")


def _find(node: ET.Element, name: str) -> list[ET.Element]:
    return [found for found in node.iter() if found.tag.split("}")[-1] == name]


def _events_xml(events: list[dict] | None) -> str:
    """eventDataObj in the order of the XSD: nested events come first."""
    return "".join(
        "<events>" + _events_xml(event.get("events"))
        + f"<eventDescription>{escape(str(event.get('description') or ''))}</eventDescription>"
        f"<eventKind>NORMAL</eventKind><eventNumber>{number}</eventNumber><eventSet>0</eventSet>"
        f"<eventTag>{escape(str(event.get('tag') or ''))}</eventTag><fileId><contentsMD5>{'0' * 32}</contentsMD5>"
        f"<filePathname>{escape(str(event.get('file') or ''))}</filePathname></fileId><id>{number}</id>"
        f"<lineNumber>{event.get('line') or 0}</lineNumber><main>{'true' if event.get('main') else 'false'}</main>"
        "<polarity>false</polarity></events>" for number, event in enumerate(events or [], 1))


class Emulator:
    def __init__(self, data: str | Path = SAMPLE, port: int = 0):
        loaded = yaml.safe_load(Path(data).read_text(encoding="utf-8-sig")) or {}
        self.issues = {int(issue["cid"]): {"classification": "Unclassified", **issue}
                       for issue in loaded.get("issues") or []}
        self.user, self.key = USER, KEY
        self.writes: list[dict] = []  # the triage values received
        self.requests: list[str] = []  # "METHOD path?query" of every request
        # For tests in the same process: part of a path -> seconds to wait, or the HTTP status to answer.
        self.delay: dict[str, float] = {}
        self.fail: dict[str, int] = {}
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.emulator = self
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> Emulator:
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def streams(self) -> list[str]:
        return sorted({str(issue["stream"]) for issue in self.issues.values()})

    def answer(self, method: str, path: str, query: dict, body: bytes, authorization: str) -> tuple[int, str, bytes]:
        if path == "/emulator/writes":
            return _json(200, {"writes": self.writes})
        if (method, path) == ("POST", "/ws/v9/defectservice"):
            return self.stream_defects(ET.fromstring(body))
        if not path.startswith("/api/v2/"):
            return 404, "text/html", b"<html><body>Not Found</body></html>"
        if authorization != "Basic " + base64.b64encode(f"{self.user}:{self.key}".encode()).decode():
            return 401, TEXT, b"Authentication failed.\n"
        if (method, path) == ("GET", "/api/v2/streams"):
            return _json(200, {"streams": [self.stream(name) for name in self.streams()]})
        if method == "GET" and path.startswith("/api/v2/streams/"):
            name = path[len("/api/v2/streams/"):]
            if name not in self.streams():
                return _error(404, 1300, f"No stream found for name {name}.")
            return _json(200, {"streams": [self.stream(name)]})
        if (method, path) == ("POST", "/api/v2/issues/search"):
            return self.search(query, json.loads(body))
        if (method, path) == ("PUT", "/api/v2/issues/triage"):
            return self.triage(query, json.loads(body))
        return _error(404, None, "Not Found")

    @staticmethod
    def stream(name: str) -> dict:
        return {"name": name, "primaryProjectName": "emulator", "triageStoreName": STORE}

    def search(self, query: dict, body: dict) -> tuple[int, str, bytes]:
        found = list(self.issues.values())  # in the order of the data file
        for wanted in body.get("filters") or []:
            column = wanted["columnKey"]
            if column == "streams":  # a stream it does not have: no warning, not an error
                names = {matcher["name"] for matcher in wanted["matchers"]}
                found = [issue for issue in found if issue["stream"] in names]
                continue
            keys = [matcher["key"] for matcher in wanted["matchers"]]
            known = KNOWN.get(column) or {issue["checker"] for issue in self.issues.values()}
            for key in keys:
                if key not in known and not (column == "checker" and key.startswith(("MISRA ", "CERT ", "AUTOSAR "))):
                    return _error(500, None, f"Invalid key provided: {key}")
            found = [issue for issue in found if issue.get(COLUMNS[column]) in keys]
        offset, count = int(query["offset"][0]), int(query["rowCount"][0])
        rows = [[{"key": column, "value": str(issue.get(COLUMNS[column]) or "")} for column in body["columns"]]
                for issue in found[offset:offset + count]]  # every value is text
        return _json(200, {"offset": offset, "totalRows": len(found), "columns": body["columns"], "rows": rows})

    def triage(self, query: dict, body: dict) -> tuple[int, str, bytes]:
        store = (query.get("triageStoreName") or [""])[0]
        if store != STORE:
            return _error(404, 2000, f"No triage store found for name {store}.")
        values = {pair["attributeName"].lower(): pair["attributeValue"] for pair in body["attributeValuesList"]}
        for cid in body["cids"]:
            if cid not in self.issues:
                return _error(404, 1400, f"No defect found for CID {cid}.")
        for name, value in values.items():
            if name in KNOWN and value not in KNOWN[name]:
                return _error(500, None, f"Invalid key provided: {value}")
        for cid in body["cids"]:
            self.issues[cid].update({name: value for name, value in values.items() if name != "comment"})
        self.writes.append({"triageStore": store, "cids": body["cids"], **values})
        return 200, TEXT, b""

    def stream_defects(self, root: ET.Element) -> tuple[int, str, bytes]:
        login = [(node.text or "") for name in ("Username", "Password") for node in _find(root, name)]
        if login != [self.user, self.key]:
            return _fault(401, "User authentication failed.")
        cid = int(_find(root, "cid")[0].text)
        out = []
        for stream in [(_find(node, "name")[0].text or "") for node in _find(root, "streamIdList")]:
            if stream not in self.streams():
                return _fault(500, f"No stream found for name {stream}.", 1300)
            issue = self.issues.get(cid)
            if issue is None or issue["stream"] != stream:
                continue  # a CID the stream does not hold: no return
            checker = escape(str(issue["checker"]))
            out.append(
                f"<return><checkerName>{checker}</checkerName><cid>{cid}</cid><defectInstances>"
                + _events_xml(issue.get("events")) + f"<checkerName>{checker}</checkerName><function>"
                f"<functionDisplayName>{escape(str(issue.get('function') or ''))}</functionDisplayName></function>"
                f"<longDescription>{escape(str(issue.get('checker_description') or ''))}</longDescription>"
                f"</defectInstances><streamId><name>{escape(stream)}</name></streamId></return>")
        return 200, XML, SOAP.format('<ns2:getStreamDefectsResponse xmlns:ns2="http://ws.coverity.com/v9">'
                                     + "".join(out) + "</ns2:getStreamDefectsResponse>").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        emulator: Emulator = self.server.emulator
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        url = urlsplit(self.path)
        path = unquote(url.path)
        emulator.requests.append(f"{self.command} {self.path}")
        time.sleep(max([seconds for part, seconds in emulator.delay.items() if part in path], default=0))
        status = next((status for part, status in emulator.fail.items() if part in path), None)
        if status is not None:
            answer = _error(status, None, "emulated failure")
        elif self.command != "GET" and SESSION in (self.headers.get("Cookie") or ""):
            answer = _error(403, 1003, "Invalid CSRF Token 'null' was found on the request parameter '_csrf' "
                                       "or header 'X-XSRF-TOKEN'.")
        else:
            with emulator.lock:
                answer = emulator.answer(self.command, path, parse_qs(url.query), body,
                                         self.headers.get("Authorization") or "")
        self.send_response(answer[0])
        self.send_header("Content-Type", answer[1])
        self.send_header("Content-Length", str(len(answer[2])))
        self.send_header("Set-Cookie", f"{SESSION}=8BC68328F1F177922B6CC0297AB000C4; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(answer[2])

    do_POST = do_PUT = do_GET


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m coverity_emulator")
    parser.add_argument("--data", default=str(SAMPLE), help="YAML file with the warnings")
    parser.add_argument("--port", type=int, default=0, help="0: any free port")
    args = parser.parse_args(argv)
    emulator = Emulator(args.data, args.port)
    print(json.dumps({"url": emulator.url, "user": emulator.user, "key": emulator.key}), flush=True)
    try:
        emulator.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
