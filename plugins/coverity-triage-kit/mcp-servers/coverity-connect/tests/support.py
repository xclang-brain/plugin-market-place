"""Helpers of the tests: the server process as the plugin registers it (mcp.json), driven over stdio, the
emulator (tests/coverity_emulator) as the Coverity Connect it talks to, and repositories with a settings file.

Run them in the environment of the server, from its folder:

    uv run --native-tls --frozen --quiet python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
PLUGIN = SERVER.parents[1]
REGISTERED = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["coverity-connect"]
MISSING = [name for name in ("mcp", "httpx", "truststore", "yaml") if importlib.util.find_spec(name) is None]
MISSING += [name for name in (REGISTERED["command"],) if shutil.which(name) is None]

sys.dont_write_bytecode = True
sys.path.insert(0, str(PLUGIN / "tests"))
if not MISSING:
    from coverity_emulator import KEY, USER, Emulator


def warning(cid: int, checker: str, stream: str = "app-main", impact: str = "High",
            classification: str = "Unclassified", line: int = 12) -> dict:
    """One warning as the emulator's data file holds it."""
    file = f"/build/src/m{cid}.c"
    return {"cid": cid, "stream": stream, "checker": checker, "file": file, "function": f"f{cid}", "line": line,
            "impact": impact, "classification": classification, "checker_description": f"{checker} の説明です。",
            "events": [{"file": file, "line": line - 2, "tag": "cond_true", "description": "条件が真の分岐を通る"},
                       {"file": file, "line": line, "tag": "dereference", "description": "NULL の可能性がある値を参照",
                        "main": True}]}


class Server:
    """The server process, started with the command of mcp.json and driven as an MCP client drives it:
    newline-delimited JSON-RPC over its pipes."""

    def __init__(self, **env: str):
        clean = {name: value for name, value in os.environ.items() if not name.upper().endswith("_PROXY")}
        # The env block of mcp.json arrives as a client passes it that does not expand it: "${COV_USER}", ...
        clean.update(REGISTERED["env"], PYTHONDONTWRITEBYTECODE="1", **env)
        arguments = [argument.replace("${PLUGIN_ROOT}", PLUGIN.as_posix()) for argument in REGISTERED["args"]]
        self.proc = subprocess.Popen([REGISTERED["command"], *arguments], env=clean, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.received: list[str] = []  # every line the server wrote, on stdout and on stderr
        self.answers: dict[int, dict] = {}
        self.ready = threading.Condition()
        self.ended = False
        self.last = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.received.extend(
            raw.decode("utf-8", "replace") for raw in self.proc.stderr), daemon=True).start()
        self.wait(self.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "0"}}))
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8")
            self.received.append(line)
            message = json.loads(line)
            with self.ready:
                if "id" in message:
                    self.answers[message["id"]] = message
                self.ready.notify_all()
        with self.ready:
            self.ended = True
            self.ready.notify_all()

    def _write(self, message: dict) -> None:
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def send(self, method: str, params: dict) -> int:
        self.last += 1
        self._write({"jsonrpc": "2.0", "id": self.last, "method": method, "params": params})
        return self.last

    def wait(self, ident: int) -> dict:
        with self.ready:
            self.ready.wait_for(lambda: ident in self.answers or self.ended, timeout=60)
            if ident not in self.answers:
                raise AssertionError("no answer from the server: " + "".join(self.received)[-2000:])
            return self.answers.pop(ident)["result"]

    def start(self, tool: str, **arguments) -> int:
        """Send a tool call without waiting for its result (see wait)."""
        return self.send("tools/call", {"name": tool, "arguments": arguments})

    def call(self, tool: str, **arguments) -> dict:
        return self.wait(self.start(tool, **arguments))

    def close(self) -> None:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()


class ServerCase(unittest.TestCase):
    """One server process for the class. Each test has its own emulator and its own folder, whose path has a
    space and Japanese characters."""

    WARNINGS: list[dict] = []

    @classmethod
    def setUpClass(cls) -> None:
        if MISSING:
            raise unittest.SkipTest(f"run the tests in the environment of the server (see support.py): no {MISSING}")
        tmp = tempfile.TemporaryDirectory(prefix="cov-mcp-")
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name).resolve() / "作業 フォルダー"
        cls.root.mkdir()
        (cls.root / "warnings.yaml").write_text(json.dumps({"issues": cls.WARNINGS}, ensure_ascii=False),
                                                encoding="utf-8")  # JSON is YAML
        cls.server = cls.start_server(COV_USER=USER, COV_AUTH_KEY=KEY)

    @classmethod
    def start_server(cls, **env: str) -> Server:
        server = Server(**env)
        cls.addClassCleanup(server.close)
        return server

    def setUp(self) -> None:
        self.emu = self.emulator(self.root / "warnings.yaml")
        self.dir = self.root / self._testMethodName
        self.run_dir = self.dir / "結果 フォルダー" / "20261110-153000"
        self.run_dir.mkdir(parents=True)

    def emulator(self, *data) -> Emulator:
        emu = Emulator(*data)
        emu.__enter__()
        self.addCleanup(emu.__exit__)
        return emu

    def repo(self, stream: str = "app-main", url: str | None = None, **conditions) -> str:
        """A repository with a settings file; returns its path as the scripts pass it (forward slashes)."""
        number = len(list(self.dir.glob("リポジトリ*"))) + 1
        folder = self.dir / f"リポジトリ{number}"
        (folder / ".coverity-triage").mkdir(parents=True)
        settings = {"coverity": {"url": url or self.emu.url + "/", "stream": stream}, "conditions": conditions}
        (folder / ".coverity-triage" / "config.yaml").write_text(json.dumps(settings, ensure_ascii=False),
                                                                 encoding="utf-8")
        return folder.as_posix()

    def entries(self, entries: list[dict], stream: str = "app-main", name: str = "coverity-entries.json") -> str:
        """The entries file and its .sha256, as apply.py writes them."""
        file = self.run_dir / name
        file.write_text(json.dumps({"stream": stream, "entries": entries}, ensure_ascii=False), encoding="utf-8")
        Path(f"{file}.sha256").write_text(hashlib.sha256(file.read_bytes()).hexdigest() + "\n", encoding="ascii")
        return file.as_posix()

    def ok(self, tool: str, **arguments) -> dict:
        """The reply of a call that succeeds: the same object as text and as structured content."""
        result = (arguments.pop("server", None) or self.server).call(tool, **arguments)
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    def refused(self, tool: str, **arguments) -> str:
        """The sentence of a call that ends as a tool error."""
        result = (arguments.pop("server", None) or self.server).call(tool, **arguments)
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def requests(self, part: str) -> list[str]:
        return [request for request in self.emu.requests if part in request]
