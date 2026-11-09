"""The server over stdio, as an MCP client sees it: server/discover first, then initialize, the tool
list, and calls in fake mode (REQ-113)."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from support import PART, SRC, only_with_packages

load_tests = only_with_packages

TOOLS = {"coverity_check_connection", "coverity_search_issues", "coverity_get_issue_details",
         "coverity_preview_triage", "coverity_update_triage"}
# Words a description must not hold: they would tie this server to other tools.
FOREIGN = ("ct.py", "/coverity-", "skill", "スキル", "エージェント", "agent", "${")
FAKE = """\
snapshot: {sourceVersion: abc1234}
issues:
  - {cid: 30001, stream: s-main, checker: NULL_RETURNS, file: src/a.c, function: f, line: 3, impact: High,
     status: New, classification: Unclassified, action: Undecided,
     events: [{file: src/a.c, line: 3, tag: dereference, description: d, main: true}]}
"""


class StdioClient:
    """Newline-delimited JSON-RPC over the pipes of a child process."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: str):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, cwd=cwd)
        self.lines: "queue.Queue[str | None]" = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.next_id = 0

    def _read(self) -> None:
        for raw in self.proc.stdout:
            self.lines.put(raw.decode("utf-8"))
        self.lines.put(None)

    def _read_err(self) -> None:
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace"))

    def send(self, message: dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        while True:
            line = self.lines.get(timeout=timeout)
            if line is None:
                raise AssertionError("the server ended: " + "".join(self.stderr)[-2000:])
            message = json.loads(line)
            if message.get("id") == self.next_id:
                return message

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass


class StdioTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="cov-mcp-stdio-")
        tmp = Path(cls.tmp.name)
        fake = tmp / "fake.yaml"
        fake.write_text(FAKE, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("COVERITY_")}
        env.update({"PYTHONPATH": str(SRC) + os.pathsep + env.get("PYTHONPATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                    "COVERITY_API": "fake", "COVERITY_FAKE_DATA": str(fake), "TMPDIR": str(tmp), "TEMP": str(tmp),
                    "TMP": str(tmp), "COV_USER": "stdio-user", "COV_AUTH_KEY": "stdio-secret-key-0042",
                    "HTTPS_PROXY": "${HTTPS_PROXY}"})
        cls.client = StdioClient([sys.executable, "-B", "-m", "coverity_mcp"], env, str(PART))
        cls.discover = cls.client.request("server/discover", {"protocolVersion": "2026-07-28"})
        cls.init = cls.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                     "clientInfo": {"name": "test", "version": "0"}})
        cls.client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        cls.tools = {t["name"]: t for t in cls.client.request("tools/list")["result"]["tools"]}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.tmp.cleanup()

    def test_discover_first_then_initialize(self) -> None:
        self.assertIn("error", self.discover)
        info = self.init["result"]["serverInfo"]
        self.assertEqual(info["name"], "coverity_mcp")
        from coverity_mcp import __version__
        self.assertEqual(info["version"], __version__)
        self.assertIn("coverity_search_issues", self.init["result"]["instructions"])

    def test_instructions_do_not_assume_every_tool_is_there(self) -> None:
        text = self.init["result"]["instructions"]
        self.assertIn("使えるツールだけを使います", text)
        self.assertIn("読むツール", text)  # a client may expose only the read tools
        self.assertIn("coverity_update_triage が使えるときだけ", text)

    def test_the_five_tools_and_their_annotations(self) -> None:
        self.assertEqual(set(self.tools), TOOLS)
        hints = {n: t["annotations"] for n, t in self.tools.items()}
        self.assertTrue(hints["coverity_check_connection"]["readOnlyHint"])
        self.assertTrue(hints["coverity_preview_triage"]["readOnlyHint"])
        self.assertFalse(hints["coverity_preview_triage"]["openWorldHint"])
        for name in ("coverity_search_issues", "coverity_get_issue_details"):
            self.assertEqual((hints[name]["readOnlyHint"], hints[name]["destructiveHint"], hints[name]["idempotentHint"],
                              hints[name]["openWorldHint"]), (False, False, True, True))
        self.assertEqual((hints["coverity_update_triage"]["readOnlyHint"], hints["coverity_update_triage"]["idempotentHint"]),
                         (False, True))

    def test_schemas_describe_every_argument_and_refuse_others(self) -> None:
        search = self.tools["coverity_search_issues"]["inputSchema"]
        self.assertEqual(set(search["properties"]), {"repo_root", "filter", "filter_file", "offset", "limit",
                                                     "response_format", "output_file"})
        self.assertIs(search["additionalProperties"], False)
        self.assertEqual((search["properties"]["limit"]["maximum"], search["properties"]["limit"]["default"]), (100, 20))
        self.assertEqual(search["properties"]["response_format"]["enum"], ["concise", "detailed"])
        ref = next(s["$ref"] for s in search["properties"]["filter"]["anyOf"] if "$ref" in s)
        self.assertIs(search["$defs"][ref.rsplit("/", 1)[-1]]["additionalProperties"], False)
        self.assertEqual(self.tools["coverity_update_triage"]["inputSchema"]["required"], ["confirmation_token"])
        details = self.tools["coverity_get_issue_details"]["inputSchema"]
        self.assertEqual(details["required"], ["cids"])  # the stream can be left out
        check = self.tools["coverity_check_connection"]["inputSchema"]
        self.assertEqual(set(check["properties"]), {"repo_root", "response_format"})
        for tool in self.tools.values():
            for name, prop in tool["inputSchema"]["properties"].items():
                self.assertTrue(prop.get("description"), f"{tool['name']}.{name} has no description")

    def test_descriptions_are_complete_and_stand_alone(self) -> None:
        texts = [self.init["result"]["instructions"]] + [t["description"] for t in self.tools.values()]
        for tool in self.tools.values():
            for word in ("使うとき", "使わないとき", "返す値"):
                self.assertIn(word, tool["description"], tool["name"])
        for text in texts:
            for word in FOREIGN:
                self.assertNotIn(word, text)

    def test_calls_in_fake_mode(self) -> None:
        result = self.client.call("coverity_search_issues", {"filter": {"streams": ["s-main"]}})
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["issues"][0]["cid"], 30001)
        details = self.client.call("coverity_get_issue_details", {"stream": "s-main", "cids": [30001]})
        self.assertEqual(details["structuredContent"]["issues"][0]["events"][0]["main"], True)
        found = self.client.call("coverity_get_issue_details", {"cids": [30001]})["structuredContent"]
        self.assertEqual((found["issues"][0]["stream"], found["streams_searched"]), ("s-main", 1))
        check = self.client.call("coverity_check_connection", {})["structuredContent"]
        self.assertEqual(check["streams"], ["s-main"])
        entries = [{"id": "30001", "cids": [30001], "stream": "s-main", "classification": "False Positive",
                    "action": "Ignore", "comment": "誤検知。"}]
        preview = self.client.call("coverity_preview_triage", {"entries": entries})
        token = preview["structuredContent"]["confirmation_token"]
        refused = self.client.call("coverity_update_triage", {"confirmation_token": "000000000000", "entries": entries})
        self.assertTrue(refused["isError"])
        self.assertIn("coverity_preview_triage", refused["content"][0]["text"])
        written = self.client.call("coverity_update_triage", {"confirmation_token": token, "entries": entries})
        self.assertEqual(written["structuredContent"]["written"], ["30001"])

    def test_unknown_arguments_and_bad_values_are_errors(self) -> None:
        unknown = self.client.call("coverity_search_issues", {"filter": {"streams": ["s-main"], "status": ["New"]}})
        self.assertTrue(unknown["isError"])
        self.assertIn("status", unknown["content"][0]["text"])
        top = self.client.call("coverity_get_issue_details", {"stream": "s-main", "cids": [30001], "cid": 1})
        self.assertTrue(top["isError"])
        self.assertIn("cid", top["content"][0]["text"])
        relative = self.client.call("coverity_search_issues", {"repo_root": "repo", "filter": {"streams": ["s-main"]}})
        self.assertTrue(relative["isError"])
        self.assertIn("絶対パス", relative["content"][0]["text"])

    def test_calls_at_the_same_time_are_answered(self) -> None:
        ids = []
        for _ in range(3):
            self.client.next_id += 1
            ids.append(self.client.next_id)
            self.client.send({"jsonrpc": "2.0", "id": self.client.next_id, "method": "tools/call",
                              "params": {"name": "coverity_check_connection", "arguments": {}}})
        answers = {}
        while len(answers) < 3:
            message = json.loads(self.client.lines.get(timeout=60))
            if message.get("id") in ids:
                answers[message["id"]] = message
        self.assertTrue(all(a["result"]["structuredContent"]["ok"] for a in answers.values()))

    def test_stderr_never_holds_the_key(self) -> None:
        self.client.call("coverity_check_connection", {})
        self.assertNotIn("stdio-secret-key-0042", "".join(self.client.stderr))


if __name__ == "__main__":
    unittest.main()
