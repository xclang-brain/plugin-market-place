"""The local web server of the screen.

Safety: it listens on 127.0.0.1 only. Every request must carry the Host of this server
(``127.0.0.1:<port>`` or ``localhost:<port>``) and the random token: ``/?t=<token>`` once, which sets a
cookie (HttpOnly, SameSite=Strict) for the page, its API calls and the files of the runs; a program may
send the header ``X-Token`` instead. A POST must also be ``Content-Type: application/json`` from the
same origin (``Origin`` header), with a body of at most 4 MiB. The page has a strict Content Security
Policy (scripts only with this response's nonce); the files of a run are shown sandboxed, without
scripts. Requests are not logged (the token is in the address). Key values never reach the page.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import approvals, monitor, runs, settings, views
from .common import ASSETS, SKILL_DIR, GuiError, NotFound

PAGE = ASSETS / "gui.html"
MAX_BODY = 4 * 1024 * 1024
FORBIDDEN_TEXT = ("この画面は、画面を開くコマンドが示したアドレス（?t= が付いたもの）で開いてください。"
                  "サーバを起動し直したときは、もう一度開くコマンドを使います。")
NOT_FOUND_TEXT = "ファイルが見つかりません"
FILE_CSP = ("sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
            "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'")
RUN_ROUTE = re.compile(r"^/api/runs/([^/]+)/(summary|activity|approvals|items/([^/]+))$")


def page_csp(nonce: str) -> str:
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; "
            "img-src data:; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def code_version() -> str:
    """Fingerprint of these scripts and the page: a server started by other scripts is replaced, not reused."""
    digest = hashlib.sha256()
    files = sorted((SKILL_DIR / "scripts").rglob("*.py")) + [PAGE]
    for path in files:
        if "__pycache__" in path.parts:
            continue
        try:
            digest.update(path.relative_to(SKILL_DIR).as_posix().encode() + b"\0" + path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:16]


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64  # Windows refuses connections beyond the backlog (a page opens several at once)

    def __init__(self, repo: Path, token: str, port: int = 0, idle_seconds: float = 0.0):
        super().__init__(("127.0.0.1", port), Handler)
        self.repo = repo
        self.token = token
        self.version = code_version()
        self.idle_seconds = idle_seconds
        self.last_used = time.monotonic()
        self.closed_by = ""
        self._closing = threading.Event()

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    @property
    def cookie_name(self) -> str:
        # Cookies of 127.0.0.1 are shared by every port: one name per server, so two screens do not mix.
        return f"gui_t_{self.port}"

    def used(self) -> None:
        self.last_used = time.monotonic()

    def close_later(self, reason: str) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        self.closed_by = reason
        threading.Thread(target=self.shutdown, daemon=True).start()

    def watch_idle(self, stopped: threading.Event) -> None:
        """Stop after ``idle_seconds`` without a request of a person (the page's ping does not count)."""
        step = max(0.2, min(30.0, self.idle_seconds / 4))
        while not stopped.wait(step):
            if time.monotonic() - self.last_used > self.idle_seconds:
                self.close_later("idle")
                return


class Handler(BaseHTTPRequestHandler):
    server: GuiServer
    protocol_version = "HTTP/1.1"
    server_version = "gui"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        pass  # the requests carry the token: they stay out of the logs

    # ---- answers ----

    def _send(self, status: int, body: bytes, content_type: str, *, csp: str = "", framed: bool = False,
              cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if framed else "DENY")
        self.send_header("Content-Security-Policy", csp or (FILE_CSP if framed else "default-src 'none'; frame-ancestors 'none'"))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _text(self, status: int, text: str, framed: bool = False) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8", framed=framed)

    # ---- checks ----

    def _host_ok(self) -> bool:
        port = self.server.port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self, value: str | None) -> bool:
        return isinstance(value, str) and bool(value) and secrets.compare_digest(value.encode(), self.server.token.encode())

    def _cookie_ok(self) -> bool:
        for header in self.headers.get_all("Cookie") or []:
            for pair in header.split(";"):
                name, _, value = pair.strip().partition("=")
                if name == self.server.cookie_name and self._token_ok(value):
                    return True
        return False

    def _authorized(self) -> bool:
        return self._host_ok() and (self._cookie_ok() or self._token_ok(self.headers.get("X-Token")))

    def _same_origin(self) -> bool:
        port = self.server.port
        origin = self.headers.get("Origin")
        if origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return site in (None, "same-origin")

    def _json_type(self) -> bool:
        kind = (self.headers.get("Content-Type") or "").strip().lower()
        return kind == "application/json" or re.fullmatch(r"application/json\s*;\s*charset=utf-8", kind) is not None

    def _length(self) -> int | None:
        text = (self.headers.get("Content-Length") or "0").strip()
        if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,12}", text):
            return None
        length = int(text)
        return length if length <= MAX_BODY else None

    def _body(self) -> dict[str, Any]:
        length = self._length()
        if length is None:
            self.close_connection = True  # where the body ends is not known: the connection cannot go on
            raise ValueError("長さ（Content-Length）が正しくないか、大きすぎます")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise ValueError("JSON として読めません") from None
        if not isinstance(data, dict):
            raise ValueError("JSON のオブジェクト（{ } で囲んだもの）ではありません")
        return data

    def _skip_body(self) -> None:
        """Read the body of a refused request, so the next request on the connection starts where it should."""
        length = self._length()
        if length is None:
            self.close_connection = True
        elif length:
            self.rfile.read(length)

    def _answer(self, handle: Any, *args: Any) -> None:
        try:
            self._json(handle(*args))
        except NotFound as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
        except GuiError as exc:
            self._json({"ok": False, "error": str(exc), **exc.details})
        except OSError as exc:
            self._json({"ok": False, "error": f"ファイルの操作に失敗しました: {exc}"})
        except Exception as exc:  # noqa: BLE001 - the page shows it and the server keeps running
            self._json({"ok": False, "error": f"予期しないエラー: {type(exc).__name__}: {exc}"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)

    # ---- GET ----

    def do_HEAD(self) -> None:  # noqa: N802
        self._text(HTTPStatus.METHOD_NOT_ALLOWED, "")

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if not self._host_ok():
            self._text(HTTPStatus.FORBIDDEN, FORBIDDEN_TEXT)
            return
        if parts.path == "/":
            self._page(parse_qs(parts.query).get("t", [None])[0])
            return
        if not self._authorized():
            if parts.path.startswith(("/files/", "/view/")):
                self._text(HTTPStatus.FORBIDDEN, FORBIDDEN_TEXT, framed=True)
            else:
                self._json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if parts.path != "/api/ping":
            self.server.used()
        if parts.path.startswith(("/files/", "/view/")):
            self._file(parts.path)
            return
        self._answer(self._get, parts.path, parse_qs(parts.query))

    def _page(self, token: str | None) -> None:
        if not (self._token_ok(token) or self._cookie_ok()):
            self._text(HTTPStatus.FORBIDDEN, FORBIDDEN_TEXT)
            return
        self.server.used()
        nonce = secrets.token_urlsafe(18)
        body = PAGE.read_bytes().replace(b"__NONCE__", nonce.encode())
        cookie = f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
        self._send(HTTPStatus.OK, body, "text/html; charset=utf-8", csp=page_csp(nonce), cookie=cookie)

    def _get(self, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        repo = self.server.repo
        if path == "/api/ping":
            return {"ok": True, "repo": str(repo), "version": self.server.version}
        if path == "/api/state":
            return {"ok": True, **settings.load_state(repo)}
        if path == "/api/credentials":
            names = [(query.get(key) or [""])[0] for key in ("user_env", "key_env")]
            return {"ok": True, **settings.credentials(*names, settings.saved_key_env(repo), settings.saved_user_env(repo))}
        if path == "/api/runs":
            return runs.runs_list(repo)
        route = RUN_ROUTE.match(path)
        if route and route.group(2) == "summary":
            return runs.run_summary(repo, route.group(1))
        if route and route.group(2) == "activity":
            return {"ok": True, **monitor.board(runs.find_run(repo, route.group(1)), repo)}
        if route and route.group(3) is not None:
            return runs.item_detail(repo, route.group(1), unquote(route.group(3)))
        raise NotFound("not found")

    def _file(self, path: str) -> None:
        """``/files/<run id>/<path>``: a file of a run as it is; ``/view/<run id>/<path>``: its page."""
        kind, _, rest = path[1:].partition("/")
        run_id, _, rel = rest.partition("/")
        try:
            target, parts = runs.run_file(self.server.repo, run_id, rel, view=kind == "view")
            data = target.read_bytes()
        except (GuiError, OSError):
            self._text(HTTPStatus.NOT_FOUND, NOT_FOUND_TEXT, framed=True)
            return
        if kind == "view":
            self._send(HTTPStatus.OK, views.view_page(run_id, parts, data).encode("utf-8"), "text/html; charset=utf-8",
                       framed=True)
        elif parts[0] in ("summary.html", "cid"):
            self._send(HTTPStatus.OK, data, "text/html; charset=utf-8", framed=True)
        else:  # an HTML file among the fixed files is shown as text too
            self._send(HTTPStatus.OK, data, f"text/plain; charset={views.charset(data)}", framed=True)

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not (self._authorized() and self._same_origin() and self._json_type()):
            self._skip_body()
            self._json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        try:
            body = self._body()
        except ValueError as exc:
            self._json({"ok": False, "error": f"送られた内容を読めません: {exc}"}, HTTPStatus.BAD_REQUEST)
            return
        self.server.used()
        self._answer(self._post, path, body)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        repo = self.server.repo
        if path == "/api/save":
            return settings.save(repo, body)
        if path == "/api/save-check":
            return settings.save_check(repo, body)
        if path == "/api/close":
            self.server.close_later("button")
            return {"ok": True}
        route = RUN_ROUTE.match(path)
        if route and route.group(2) == "approvals":
            return approvals.update(repo, route.group(1), body)
        raise NotFound("not found")

    def do_PUT(self) -> None:  # noqa: N802
        self._skip_body()
        self._json({"ok": False, "error": "method not allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)

    do_DELETE = do_PUT
    do_PATCH = do_PUT
