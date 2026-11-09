#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""A stand-in for the extension's open tool, as a tiny stdio MCP server (EVAL stage 4). Stdlib only.

In GitHub Copilot CLI the extension registers a tool ``<plugin>_open(requestText)``. Claude Code has no
extensions, so this server offers a tool with the same name, description and parameter, logs every
call (one JSON line per call: at, name, arguments) and answers like the extension does: the URL of
the screen and the notices to pass on. It does nothing else.

Note: Claude Code shows the tool as ``mcp__<server>__<tool>``; Copilot CLI shows extension tools by
their own name. The harness counts calls by the log file, so the prefix does not matter there.

  python3 fake_open_server.py --config <config.json>
  config.json: {"tool": "...", "description": "...", "param_description": "...", "url": "...",
                "notices": ["..."], "log": "<calls.jsonl>"}

Messages: newline-delimited JSON-RPC 2.0 (MCP stdio): initialize, notifications/initialized,
ping, tools/list, tools/call. Logs go to stderr only (stdout is the protocol).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROTOCOL = "2025-06-18"
DEFAULTS = {
    "tool": "plugin_open",
    "description": "Web管理画面を開き、URL と、ユーザーに伝える注意を返します。ユーザーの頼みの文を requestText にそのまま渡してください。",
    "param_description": "ユーザーの頼みの文（書き換えずに、そのまま）",
    "url": "http://127.0.0.1:8731/?k=eval-0000",
    "notices": [],
    "log": None,
}


def load_config(argv: list[str]) -> dict:
    config = dict(DEFAULTS)
    if "--config" in argv:
        path = Path(argv[argv.index("--config") + 1])
        config.update(json.loads(path.read_text(encoding="utf-8")))
    return config


def open_text(url: str, notices: list[str]) -> str:
    """Same shape as the extension's answer: the URL, then the notices to pass on."""
    lines = [f"Web管理画面の URL：{url}"]
    notices = [n for n in notices if isinstance(n, str) and n]
    if notices:
        lines += ["", "ユーザーに伝えてください：", *[f"- {n}" for n in notices]]
    return "\n".join(lines)


def log_call(config: dict, name: str, arguments: dict) -> None:
    if not config.get("log"):
        return
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "name": name, "arguments": arguments}
    with open(config["log"], "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def tool_list(config: dict) -> list[dict]:
    return [{"name": config["tool"], "description": config["description"],
             "inputSchema": {"type": "object",
                             "properties": {"requestText": {"type": "string", "description": config["param_description"]}},
                             "required": ["requestText"], "additionalProperties": False},
             "annotations": {"title": "Web管理画面を開く", "readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False}}]


def handle(config: dict, msg: dict) -> dict | None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if msg_id is None:  # a notification
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "fake-open", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list(config)}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name != config["tool"]:
            result = {"content": [{"type": "text", "text": f"ツール {name} はありません"}], "isError": True}
        else:
            log_call(config, name, arguments)
            result = {"content": [{"type": "text", "text": open_text(config["url"], config["notices"])}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main(argv: list[str] | None = None) -> int:
    config = load_config(sys.argv[1:] if argv is None else argv)
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write((json.dumps({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700, "message": "Parse error"}}) + "\n").encode("utf-8"))
            out.flush()
            continue
        if not isinstance(msg, dict):
            continue
        reply = handle(config, msg)
        if reply is not None:
            out.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
