"""Start and stop the Coverity Connect emulator (tests/coverity_emulator) for integration tests.

    emu = Emulator(data_dir)              # python3 -m coverity_emulator.server --port 0 --data <dir>
    emu.admin("POST", "seed", SEED)       # users, triage stores, projects, streams
    emu.admin("POST", "issues", {"stream": "app-main", "snapshot": {...}, "issues": [warning(...)]})
    emu.stop()

The emulator listens on 127.0.0.1 only; its first stdout line is JSON with the URL.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parents[1]  # the folder that holds coverity_emulator
PACKAGE = TESTS / "coverity_emulator"

USERS = {"admin": "emu-admin-key-0001", "triager": "emu-triager-key-0001", "viewer": "emu-viewer-key-0001"}
SEED = {
    "users": [{"name": "admin", "password": "coverity", "keys": [USERS["admin"]], "superUser": True},
              {"name": "triager", "password": "triage-pass", "keys": [USERS["triager"]]},
              {"name": "viewer", "password": "viewer-pass", "keys": [USERS["viewer"]], "webservices": False}],
    "triageStores": ["Release Triage Store"],
    "projects": ["app"],
    "streams": [{"name": "app-main", "project": "app"},
                {"name": "app-release", "project": "app", "triageStore": "Release Triage Store"}],
}


def available() -> str | None:
    """Why the emulator cannot run here, or None."""
    if not (PACKAGE / "server.py").is_file():
        return f"the emulator is missing: {PACKAGE}"
    return None


def warning(checker: str, function: str | None, file: str, line: int, impact: str = "Medium", cwe: str = "476",
            category: str = "Null pointer dereferences", extra: str = "p", nested: bool = False) -> dict[str, Any]:
    """One warning tree as the analysis commits it (the emulator turns it into a CID)."""
    events = [{"eventDescription": f'Condition "x{line}", taking false branch.', "eventTag": "path", "eventNumber": 1,
               "eventTreePosition": "1", "main": False, "filePathname": file, "lineNumber": line - 2, "events": None},
              {"eventDescription": f'Dereferencing "{extra}".', "eventTag": "dereference", "eventNumber": 2,
               "eventTreePosition": "2", "main": True, "filePathname": file, "lineNumber": line, "events": None}]
    if nested:
        events[0]["events"] = [{"eventDescription": '"release" frees "p".', "eventTag": "freed_arg", "eventNumber": 1,
                                "eventTreePosition": "1.1", "main": False, "filePathname": file, "lineNumber": 5,
                                "events": None}]
    return {"checkerName": checker, "subcategory": "none", "type": checker.lower(), "subtype": "none", "extra": extra,
            "functionDisplayName": function, "functionMangledName": function, "mainEventFilePathname": file,
            "strippedMainEventFilePathname": file, "mainEventLineNumber": line, "domain": "STATIC_C", "language": "C",
            "events": events,
            "checkerProperties": {"category": category, "categoryDescription": category, "cweCategory": cwe,
                                  "issueKinds": ["QUALITY"], "eventSetCaptions": [], "impact": impact,
                                  "impactDescription": impact, "subcategoryLocalEffect": "The program may crash.",
                                  "subcategoryShortDescription": f"{checker} short",
                                  "subcategoryLongDescription": f"{checker} long description"}}


class Emulator:
    def __init__(self, data: str | os.PathLike, *args: str):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen([sys.executable, "-B", "-m", "coverity_emulator.server", "--port", "0",
                                      "--data", str(data), *args], cwd=str(TESTS), env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=os.name == "posix")
        line = self.proc.stdout.readline().decode("utf-8")
        if not line:
            self.proc.wait(timeout=10)
            raise RuntimeError("the emulator did not start")
        self.info: dict[str, Any] = json.loads(line)
        self.url: str = self.info["url"]
        self.admin_url: str = self.info.get("http_url") or self.url
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1: never a proxy

    def admin(self, method: str, name: str, body: Any = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"{self.admin_url}/__emulator__/{name}", data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        with self._opener.open(request, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def config(self, **switches: Any) -> Any:
        return self.admin("POST", "config", switches)

    def stop(self) -> None:
        if self.proc.poll() is None:
            if os.name == "posix":
                self.proc.send_signal(signal.SIGTERM)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self.proc.stdout:
            self.proc.stdout.close()
