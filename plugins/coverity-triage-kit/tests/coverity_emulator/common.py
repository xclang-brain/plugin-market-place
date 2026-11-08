"""Small helpers shared by the REST and SOAP handlers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape
from typing import Any


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)


def json_response(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                    [("Content-Type", "application/json")])


def error_response(status: int, code: int | None, message: str, form: str) -> Response:
    """The REST error body. The JSON form {"code", "message"} is "likely" (spec 1.4); text and html are
    the alternatives of the switch error_body."""
    if form == "text":
        return Response(status, message.encode("utf-8"), [("Content-Type", "text/plain;charset=UTF-8")])
    if form == "html":
        reason = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                  500: "Internal Server Error", 503: "Service Unavailable"}.get(status, "Error")
        page = (f"<!doctype html><html lang=\"en\"><head><title>HTTP Status {status} – {reason}</title></head>"
                f"<body><h1>HTTP Status {status} – {reason}</h1><hr class=\"line\" /><p><b>Message</b> "
                f"{escape(message)}</p></body></html>")
        return Response(status, page.encode("utf-8"), [("Content-Type", "text/html;charset=utf-8")])
    return json_response({"code": code, "message": message}, status)


def login_page(base_url: str) -> bytes:
    """A small stand-in for /login/login.htm (the real page is a single page application)."""
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Coverity Connect</title>"
            f"<link rel=\"stylesheet\" href=\"{escape(base_url)}/login/login.css?v=emu\"></head>"
            "<body><form method=\"post\" action=\"/login/login.htm\"><input type=\"hidden\" name=\"_csrf\" "
            "value=\"emulator\"><input name=\"username\"><input name=\"password\" type=\"password\">"
            "<button>Sign In</button></form></body></html>").encode("utf-8")


def redirect_body(location: str) -> bytes:
    """The HTML of the 302 that scan8 answered to a request without credentials (measured)."""
    shown = escape(location).replace("/", "&#47;")
    return f'<html><body><p>Redirecting to <a href="{shown}">{shown}</a></p></body></html>'.encode("utf-8")
