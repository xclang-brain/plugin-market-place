"""Coverity Connect emulator: ``python3 -m coverity_emulator.server --port 0 --data <dir> [--tls]``.

Listens on 127.0.0.1 only. The first line on stdout is JSON: ``{"url", "port", ...}``.

Windows of the emulated server:
- REST v2 under /api/v2/ (see rest.py), HTTP Basic authentication.
- SOAP v9 at /ws/v9/defectservice and /ws/v9/configurationservice (see soap.py), WS-Security.
- /login/login.htm, the target of the 302 for a request without credentials.

Administration (no authentication, 127.0.0.1 only), under /__emulator__/:
    GET  health                      liveness
    GET  config / POST config        switches and fault injection (config.py); {"reset": true} for defaults
    GET  requests?since=N            received requests (credentials masked); DELETE requests clears them
    GET  triage-writes               triage written through REST PUT or SOAP
    GET  state                       the whole state
    GET  issues?stream=S             CIDs of a stream with status and triage (for tests)
    POST seed                        users, triage stores, projects, streams, stream links
    POST issues                      a snapshot with warnings, without authentication (test preparation)
    POST commit                      what the mock cov-commit-defects sends (HTTP Basic, user and key)
    POST reset                       empty the state
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import VERSION, rest, soap
from .common import Response, error_response, json_response, login_page, redirect_body
from .config import ConfigError
from .state import ApiError, InvalidKey, Store

SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"), ("X-XSS-Protection", "0"),
                    ("Cache-Control", "no-cache, no-store, max-age=0, must-revalidate"), ("Pragma", "no-cache"),
                    ("Expires", "0"), ("X-Frame-Options", "SAMEORIGIN")]
MAX_LOGGED_BODY = 256 * 1024


def apply_seed(store: Store, data: dict) -> dict:
    """Users, triage stores, projects and streams from a seed object (admin POST seed / --seed)."""
    for u in data.get("users") or []:
        store.add_user(u["name"], password=u.get("password"), keys=u.get("keys") or u.get("authKeys"),
                       webservices=u.get("webservices", True), disabled=u.get("disabled", False),
                       full_name=u.get("fullName", ""), email=u.get("email", ""), super_user=u.get("superUser", False))
    for t in data.get("triageStores") or []:
        store.add_triage_store(t["name"] if isinstance(t, dict) else t,
                               (t.get("description", "") if isinstance(t, dict) else ""))
    for p in data.get("projects") or []:
        store.add_project(p["name"] if isinstance(p, dict) else p, (p.get("description", "") if isinstance(p, dict) else ""))
    for s in data.get("streams") or []:
        store.add_stream(s["name"], s["project"], s.get("triageStore", "Default Triage Store"),
                         s.get("description", ""), s.get("language", "MIXED"))
    for link in data.get("streamLinks") or []:
        store.link_stream(link["project"], link["stream"])
    return {"users": sorted(store.state["users"]), "projects": sorted(store.state["projects"]),
            "streams": sorted(store.state["streams"]), "triageStores": sorted(store.state["triageStores"])}


def _mask_body(text: str) -> str:
    return re.sub(r"(<(?:\w+:)?Password\b[^>]*>)(.*?)(</(?:\w+:)?Password>)", r"\1****\3", text, flags=re.S)


class Emulator:
    def __init__(self, store: Store):
        self.store = store
        self.urls: dict[str, str] = {}  # "https" / "http" -> base URL
        self.inject_lock = threading.Lock()

    def base_url(self, scheme: str) -> str:
        return self.urls.get(scheme) or next(iter(self.urls.values()))

    # ---- fault injection --------------------------------------------------------------------

    def matching_rules(self, method: str, path: str, operation: str | None) -> list[dict]:
        with self.inject_lock:
            rules = self.store.config.get("inject") or []
            hits, changed = [], False
            for rule in rules:
                if rule.get("method") and rule["method"].upper() != method:
                    continue
                if rule.get("path") and not re.search(rule["path"], path):
                    continue
                if rule.get("operation") and rule["operation"] != operation:
                    continue
                if rule.get("count") is not None:
                    if int(rule["count"]) <= 0:
                        continue
                    rule["count"] = int(rule["count"]) - 1
                    changed = True
                hits.append(dict(rule))
            if changed:
                self.store.set_config({"inject": [r for r in rules if r.get("count") is None or int(r["count"]) > 0]})
            return hits


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    emulator: Emulator
    scheme = "http"
    plain_redirect = False  # the extra plain listener next to the TLS one

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    def send_response(self, code: int, message: str | None = None) -> None:
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def do_GET(self) -> None:
        self.handle_any("GET")

    def do_POST(self) -> None:
        self.handle_any("POST")

    def do_PUT(self) -> None:
        self.handle_any("PUT")

    def do_DELETE(self) -> None:
        self.handle_any("DELETE")

    def do_PATCH(self) -> None:
        self.handle_any("PATCH")

    # ---- plumbing -----------------------------------------------------------------------------

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            return self.rfile.read(length)
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            data = b""
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return b""

    def _send(self, resp: Response, cookie: bool = True, drop_mid: bool = False) -> None:
        self.send_response(resp.status)
        names = {k.lower() for k, _ in resp.headers}
        for k, v in resp.headers:
            self.send_header(k, v)
        if cookie and "set-cookie" not in names:
            secure = "; Secure" if self.scheme == "https" else ""
            self.send_header("Set-Cookie", f"COVJSESSIONID-build={os.urandom(16).hex().upper()}; Path=/{secure}; HttpOnly")
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if drop_mid:
            self.wfile.write(resp.body[: max(1, len(resp.body) // 2)])
            self.wfile.flush()
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if self.command != "HEAD":
            self.wfile.write(resp.body)

    def _basic(self) -> tuple[str, str] | None:
        header = self.headers.get("Authorization") or ""
        if not header.lower().startswith("basic "):
            return None
        try:
            user, _, secret = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return ("", "")
        return user, secret

    # ---- main dispatch ------------------------------------------------------------------------

    def handle_any(self, method: str) -> None:
        emu = self.emulator
        store = emu.store
        started = time.monotonic()
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query, keep_blank_values=True)
        body = self._read_body()
        if path.startswith("/__emulator__/"):
            self._send(self.admin(method, path, query, body), cookie=False)
            return
        basic = self._basic()
        record: dict[str, Any] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "listener": self.scheme, "method": method, "path": path,
            "query": query, "headers": {k: self.headers.get(k) for k in ("Content-Type", "Accept", "User-Agent")
                                        if self.headers.get(k) is not None},
            "auth": {"type": "basic", "user": basic[0], "secret": "****"} if basic else None,
            "body": _mask_body(body[:MAX_LOGGED_BODY].decode("utf-8", "replace")),
        }
        if "SOAPAction" in self.headers:
            record["headers"]["SOAPAction"] = self.headers.get("SOAPAction")
        record["headers"]["Cookie"] = "present" if self.headers.get("Cookie") else None
        record["headers"]["X-XSRF-TOKEN"] = "present" if self.headers.get("X-XSRF-TOKEN") else None
        operation = None
        if path.startswith("/ws/") and body:
            m = re.search(rb"<(?:[\w.-]+:)?Body[^>]*>\s*<(?:[\w.-]+:)?([\w]+)", body)
            operation = m.group(1).decode() if m else None
            record["soap_operation"] = operation
        drop_mid = False
        try:
            resp, drop_mid = self.route(method, path, query, body, basic, record, operation)
        except ConnectionAbortedError:
            record["status"] = "dropped"
            record["seconds"] = round(time.monotonic() - started, 3)
            store.log_request(record)
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        latency = float(store.config.get("latency_seconds") or 0)
        if latency:
            time.sleep(latency)
        record["status"] = resp.status
        record["seconds"] = round(time.monotonic() - started, 3)
        if drop_mid:
            record["dropped"] = "mid_body"
        store.log_request(record)
        self._send(resp, drop_mid=drop_mid)

    def route(self, method: str, path: str, query: dict, body: bytes, basic: tuple[str, str] | None,
              record: dict, operation: str | None) -> tuple[Response, bool]:
        emu, store = self.emulator, self.emulator.store
        conf = store.config
        if self.plain_redirect and conf.get("http_to_https_redirect"):
            location = emu.base_url("https") + self.path
            return Response(int(conf["http_to_https_redirect"]), b"", [("Location", location)]), False
        rules = emu.matching_rules(method, path, operation)
        if rules:
            record["injected"] = rules
        overrides: dict[str, Any] = {}
        drop_mid = False
        for rule in rules:
            if rule.get("delay"):
                time.sleep(float(rule["delay"]))
        for rule in rules:
            if rule.get("drop") == "before_response":
                raise ConnectionAbortedError
            if rule.get("drop") == "mid_body":
                drop_mid = True
            if rule.get("omit_total_rows"):
                overrides["omit_total_rows"] = True
            if rule.get("page_overlap"):
                overrides["page_overlap"] = int(rule["page_overlap"])
        for rule in rules:
            if rule.get("redirect"):
                location = emu.base_url("https").replace("http://", "https://") + self.path
                return Response(int(rule["redirect"]), b"", [("Location", location)]), drop_mid
            if rule.get("html"):
                return Response(int(rule.get("status", 200)), login_page(emu.base_url(self.scheme)),
                                [("Content-Type", "text/html;charset=UTF-8")]), drop_mid
            if rule.get("soap_fault"):
                return soap.fault(int(rule.get("status", 500)), rule.get("faultcode", "S:Server"), rule["soap_fault"],
                                  rule.get("code")), drop_mid
            if rule.get("invalid_key"):
                return self.rest_error(ApiError(500, None, f"Invalid key provided: {rule['invalid_key']}")), drop_mid
            if rule.get("status"):
                ctype = rule.get("content_type", "application/json")
                default = json.dumps({"code": rule.get("code"), "message": rule.get("message", "injected error")})
                return Response(int(rule["status"]), str(rule.get("body", default)).encode("utf-8"),
                                [("Content-Type", ctype)]), drop_mid
        if overrides:
            saved = {k: conf.get(k) for k in overrides}
            conf.update(overrides)
            try:
                return self.normal(method, path, query, body, basic, record), drop_mid
            finally:
                conf.update(saved)
        return self.normal(method, path, query, body, basic, record), drop_mid

    def rest_error(self, exc: ApiError) -> Response:
        store = self.emulator.store
        if isinstance(exc, InvalidKey):
            mode = store.config["search_unknown_key"]
            if mode == "400":
                return error_response(400, None, exc.message, store.config["error_body"])
        resp = error_response(exc.status, exc.code, exc.message, store.config["error_body"])
        resp.headers += SECURITY_HEADERS
        return resp

    def normal(self, method: str, path: str, query: dict, body: bytes, basic: tuple[str, str] | None,
               record: dict) -> Response:
        emu, store = self.emulator, self.emulator.store
        base = emu.base_url(self.scheme)
        if path in ("/login/login.htm", "/login/login.htm/"):
            return Response(200, login_page(base), [("Content-Type", "text/html;charset=UTF-8"),
                                                    ("Set-Cookie", "XSRF-TOKEN=emulator-xsrf; Path=/")])
        m = re.fullmatch(r"/ws/v9/(\w+)", path)
        if m:
            service = m.group(1)
            if service not in soap.SERVICES:
                return Response(404, b"<html><body>Not Found</body></html>", [("Content-Type", "text/html")])
            if method == "GET":
                if "wsdl" in query:
                    return Response(200, soap.wsdl(service, base), [("Content-Type", soap.CONTENT_TYPE)])
                if "xsd" in query:
                    return Response(200, soap.xsd(service), [("Content-Type", soap.CONTENT_TYPE)])
                return Response(200, soap.services_page(base), [("Content-Type", "text/html;charset=utf-8")])
            if method != "POST":
                return Response(405, b"", [])
            return soap.handle(store, service, body, basic, record)
        if not path.startswith("/api/"):
            if path == "/":
                return Response(302, redirect_body(base + "/login/login.htm"),
                                [("Location", base + "/login/login.htm"), ("Content-Type", "text/html")])
            return Response(404, b"<html><body>Not Found</body></html>", [("Content-Type", "text/html")])
        # ---- REST: authentication first (scan8 answers 302 even for a path that does not exist) ----
        mode = store.config["auth_failure"]
        if basic is None:
            if mode == "401":
                return Response(401, b"Authentication failed.\n", [])
            location = base + "/login/login.htm"
            return Response(302, redirect_body(location), [("Location", location)] + SECURITY_HEADERS)
        result, _ = store.check_credentials(*basic)
        if result == "bad":
            if mode == "302":
                location = base + "/login/login.htm"
                return Response(302, redirect_body(location), [("Location", location)] + SECURITY_HEADERS)
            return Response(401, b"Authentication failed.\n", [])
        if result == "no_permission":
            status = int(store.config["rest_no_permission"])
            if status == 401:
                return Response(401, b"Authentication failed.\n", [])
            return self.rest_error(ApiError(403, 1003, "Access denied"))
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            with store.lock:
                resp = rest.dispatch(store, method, path, query, body, basic[0], headers)
        except InvalidKey as exc:
            if store.config["search_unknown_key"] == "200_empty" and path == "/api/v2/issues/search":
                cols = []
                try:
                    cols = (json.loads(body or b"{}").get("columns") or [])
                except ValueError:
                    pass
                return json_response({"offset": int((query.get("offset") or ["0"])[0]), "totalRows": 0,
                                      "columns": cols, "rows": []})
            return self.rest_error(exc)
        except ApiError as exc:
            return self.rest_error(exc)
        resp.headers += SECURITY_HEADERS
        return resp

    # ---- administration -----------------------------------------------------------------------

    def admin(self, method: str, path: str, query: dict, body: bytes) -> Response:
        store = self.emulator.store
        name = path[len("/__emulator__/"):].strip("/")
        try:
            data = json.loads(body.decode("utf-8")) if body.strip() else {}
        except ValueError as exc:
            return json_response({"error": f"malformed JSON: {exc}"}, 400)
        try:
            if name == "health":
                return json_response({"ok": True, "version": VERSION, "urls": self.emulator.urls})
            if name == "config":
                if method == "POST":
                    return json_response(store.set_config(data))
                return json_response(store.config)
            if name == "requests":
                if method == "DELETE":
                    store.clear_requests()
                    return json_response({"cleared": True})
                since = int((query.get("since") or ["0"])[0])
                pattern = (query.get("path") or [None])[0]
                items = [r for r in store.requests if r["seq"] > since and (not pattern or re.search(pattern, r["path"]))]
                return json_response({"requests": items, "last": store.request_seq})
            if name == "triage-writes":
                return json_response({"writes": store.state["triageWrites"]})
            if name == "state":
                return json_response(store.state)
            if name == "issues" and method == "GET":
                return json_response(self.issue_summary((query.get("stream") or [None])[0]))
            if name == "seed" and method == "POST":
                return json_response(apply_seed(store, data))
            if name == "issues" and method == "POST":
                return json_response(store.commit(data["stream"], data.get("user", "admin"), data.get("snapshot") or {},
                                                  data.get("issues") or []))
            if name == "commit" and method == "POST":
                basic = self._basic()
                if not basic:
                    return json_response({"error": "Authentication failed."}, 401)
                result, _ = store.check_credentials(*basic)
                if result == "bad":
                    return json_response({"error": "Authentication failed."}, 401)
                return json_response(store.commit(data["stream"], basic[0], data.get("snapshot") or {},
                                                  data.get("issues") or []))
            if name == "reset" and method == "POST":
                store.reset()
                if data.get("config"):
                    store.set_config({"reset": True})
                return json_response({"reset": True})
        except ConfigError as exc:
            return json_response({"error": str(exc)}, 400)
        except ApiError as exc:
            return json_response({"error": exc.message, "code": exc.code}, exc.status)
        except (KeyError, TypeError, ValueError) as exc:
            return json_response({"error": f"{type(exc).__name__}: {exc}"}, 400)
        return json_response({"error": f"unknown administration window: {method} {name}"}, 404)

    def issue_summary(self, stream: str | None) -> dict:
        store = self.emulator.store
        with store.lock:
            out = []
            streams = [stream] if stream else sorted(store.state["streams"])
            for s in streams:
                if s not in store.state["streams"]:
                    continue
                tstore = store.state["streams"][s]["triageStoreName"]
                cids = sorted({int(c) for snap in store.stream_snapshots(s) for c in snap["occurrences"]})
                for cid in cids:
                    rec = store.state["cids"][str(cid)]
                    latest = store.latest_snapshot(s)
                    occs = latest["occurrences"].get(str(cid)) or store.detected_in(cid, s)[-1]["occurrences"][str(cid)]
                    out.append({"cid": cid, "stream": s, "checker": rec["checkerName"], "mergeKey": rec["mergeKey"],
                                "function": rec.get("functionDisplayName"), "file": occs[0]["file"],
                                "line": occs[0]["line"], "occurrences": len(occs), "status": store.status(cid, s),
                                "triage": store.triage_of(tstore, cid)["attrs"]})
            return {"issues": out}


def ensure_certificate(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "cert.pem", directory / "key.pem"
    if not cert.is_file() or not key.is_file():
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return cert, key


def make_server(emulator: Emulator, port: int, scheme: str, cert: tuple[Path, Path] | None = None,
                plain_redirect: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"emulator": emulator, "scheme": scheme,
                                                 "plain_redirect": plain_redirect})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    if cert is not None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert[0]), str(cert[1]))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m coverity_emulator.server")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data", required=True, help="folder that holds the state (kept across restarts)")
    parser.add_argument("--tls", action="store_true", help="serve https (self-signed certificate in <data>/tls)")
    parser.add_argument("--http-port", type=int, default=0, help="with --tls: the extra plain http port (0: any)")
    parser.add_argument("--config", help="switches as JSON, or @file")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="one switch")
    parser.add_argument("--seed", help="seed JSON file (users, projects, streams, triage stores)")
    args = parser.parse_args(argv)

    store = Store(args.data)
    changes: dict[str, Any] = {}
    if args.config:
        text = Path(args.config[1:]).read_text("utf-8") if args.config.startswith("@") else args.config
        changes.update(json.loads(text))
    for item in args.set:
        key, _, value = item.partition("=")
        changes[key] = value
    if changes:
        store.set_config(changes)
    if args.seed:
        apply_seed(store, json.loads(Path(args.seed).read_text("utf-8")))
    emulator = Emulator(store)
    servers: list[ThreadingHTTPServer] = []
    info: dict[str, Any] = {"data": str(Path(args.data).resolve()), "pid": os.getpid()}
    if args.tls:
        cert = ensure_certificate(Path(args.data) / "tls")
        main_server = make_server(emulator, args.port, "https", cert)
        port = main_server.server_address[1]
        emulator.urls["https"] = f"https://127.0.0.1:{port}"
        plain = make_server(emulator, args.http_port, "http", plain_redirect=True)
        emulator.urls["http"] = f"http://127.0.0.1:{plain.server_address[1]}"
        servers += [main_server, plain]
        info.update({"url": emulator.urls["https"], "port": port, "http_url": emulator.urls["http"],
                     "http_port": plain.server_address[1], "ca_file": str(cert[0].resolve())})
    else:
        main_server = make_server(emulator, args.port, "http")
        port = main_server.server_address[1]
        emulator.urls["http"] = f"http://127.0.0.1:{port}"
        servers.append(main_server)
        info.update({"url": emulator.urls["http"], "port": port})
    print(json.dumps(info), flush=True)

    stop = threading.Event()

    def shutdown(*_: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True) for s in servers]
    for t in threads:
        t.start()
    stop.wait()
    for s in servers:
        s.shutdown()
        s.server_close()
    store.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
