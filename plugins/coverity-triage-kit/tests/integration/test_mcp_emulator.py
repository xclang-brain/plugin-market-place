"""The MCP server against the Coverity Connect emulator, over the real protocol (stdio JSON-RPC to the
server started with its registration command, REST v2 and SOAP v9 to the emulator).

The server folder and its command are read from the plugin's mcp.json. The test needs uv and the
locked packages (downloaded once into a temporary environment); without them it is skipped.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]

try:
    from . import emulator as emu_support  # discovered as the package tests.integration
except ImportError:  # discovered with this folder as the top level
    import sys

    sys.path.insert(0, str(HERE))
    import emulator as emu_support  # type: ignore[no-redef]

UV = shutil.which("uv")
SERVER_TIMEOUT = 300  # seconds for the first answer (uv may still be preparing the environment)


def server_command() -> tuple[str, Path, list[str]] | None:
    """(registration name, server folder, command after the folder) of the first stdio server in mcp.json."""
    try:
        data = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))
        name, entry = next(iter(data["mcpServers"].items()))
        args = list(entry["args"])
        folder = args[args.index("--directory") + 1].replace("${PLUGIN_ROOT}", str(PLUGIN))
        return name, Path(folder), args[args.index("--directory") + 2:]
    except (OSError, ValueError, KeyError, StopIteration, IndexError):
        return None


class McpClient:
    """Newline-delimited JSON-RPC over the pipes of the server process."""

    def __init__(self, command: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, start_new_session=os.name == "posix")
        self.lines: "queue.Queue[str | None]" = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        self.next_id = 0
        init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "integration", "version": "0"}},
                            timeout=SERVER_TIMEOUT)
        if "result" not in init:
            raise AssertionError(f"initialize failed: {init}")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _pump(stream, lines) -> None:
        for raw in stream:
            lines.put(raw.decode("utf-8"))
        lines.put(None)

    def _pump_err(self) -> None:
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace"))

    def send(self, message: dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            line = self.lines.get(timeout=timeout)
            if line is None:
                raise AssertionError("the server ended: " + "".join(self.stderr)[-3000:])
            message = json.loads(line)
            if message.get("id") == self.next_id:
                return message

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        """structuredContent of a successful call; AssertionError with the text of a failed one."""
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        if result.get("isError"):
            raise ToolCallError(result["content"][0]["text"])
        return result["structuredContent"]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.proc.kill()
            self.proc.wait(timeout=15)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass


class ToolCallError(AssertionError):
    pass


def _skip_reason() -> str | None:
    if not UV:
        return "uv is not installed"
    if server_command() is None:
        return "mcp.json has no stdio server with --directory"
    return emu_support.available()


@unittest.skipIf(_skip_reason(), _skip_reason() or "")
class McpEmulatorTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="cov-mcp-emu-"))
        name, cls.folder, cls.tail = server_command()
        cls.base_env = {k: v for k, v in os.environ.items()
                        if not k.upper().endswith("_PROXY") and not k.startswith(("COVERITY_", "COV_"))}
        cls.base_env.update({"UV_PROJECT_ENVIRONMENT": str(cls.tmp / "venv"), "PYTHONDONTWRITEBYTECODE": "1",
                             "NO_PROXY": "127.0.0.1,localhost", "TMPDIR": str(cls.tmp), "TEMP": str(cls.tmp),
                             "TMP": str(cls.tmp)})
        cls.base_env.pop("VIRTUAL_ENV", None)
        sync = subprocess.run([UV, "sync", "--native-tls", "--frozen", "--quiet", "--directory", str(cls.folder)],
                              env=dict(os.environ, UV_PROJECT_ENVIRONMENT=str(cls.tmp / "venv")),
                              capture_output=True, text=True, timeout=600)
        if sync.returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest(f"the server's packages could not be prepared: {sync.stderr[-500:]}")
        cls.emu = emu_support.Emulator(cls.tmp / "emu")
        cls.emu.admin("POST", "seed", emu_support.SEED)
        w = emu_support.warning
        first = [w("NULL_RETURNS", "read_header", "/build/app/src/reader.c", 42, "High", nested=True),
                 w("RESOURCE_LEAK", "open_log", "/build/app/src/io.c", 88, "High", "404", "Resource leaks", "fp"),
                 w("CHECKED_RETURN", "uart_init", "/build/app/src/uart.c", 57, "Medium", "252",
                   "Error handling issues", "hal"),
                 w("MISRA C-2012 Rule 10.4", "add_offset", "/build/app/src/calc.c", 17, "Low", "", "Coding standard",
                   "x"),
                 w("UNINIT", "loop_init", "/build/app/src/loop.c", 90, "High", "457", "Uninitialized variables", "fd")]
        cls.emu.admin("POST", "issues", {"stream": "app-main", "snapshot": {"sourceVersion": "rev-1"}, "issues": first})
        cls.emu.admin("POST", "issues", {"stream": "app-main", "snapshot": {"sourceVersion": "rev-2"},
                                         "issues": first[:4]})  # UNINIT fixed in the latest snapshot
        cls.emu.admin("POST", "issues", {"stream": "app-release", "snapshot": {"sourceVersion": "rel-1"},
                                         "issues": first[:2]})
        cls.cids = {i["checker"]: i["cid"] for i in cls.emu.admin("GET", "issues?stream=app-main")["issues"]}
        cls.repo = cls.make_repo("repo", "")
        cls.viewer_repo = cls.make_repo("repo-viewer", "  user_env: EMU_VIEWER_USER\n  key_env: EMU_VIEWER_KEY\n")
        cls.wrong_repo = cls.make_repo("repo-wrong", "  user_env: EMU_WRONG_USER\n  key_env: EMU_WRONG_KEY\n")
        env = dict(cls.base_env, COV_USER="triager", COV_AUTH_KEY=emu_support.USERS["triager"],
                   EMU_VIEWER_USER="viewer", EMU_VIEWER_KEY=emu_support.USERS["viewer"],
                   EMU_WRONG_USER="triager", EMU_WRONG_KEY="wrong-key")
        cls.client = McpClient(cls.command(), env)

    @classmethod
    def command(cls) -> list[str]:
        return [UV, "run", "--native-tls", "--frozen", "--quiet", "--directory", str(cls.folder), *cls.tail]

    @classmethod
    def make_repo(cls, name: str, extra: str, url: str | None = None) -> str:
        repo = cls.tmp / name
        (repo / ".coverity-triage").mkdir(parents=True)
        (repo / ".coverity-triage" / "config.yaml").write_text(
            f"coverity:\n  url: {url or cls.emu.url}\n{extra}output_dir: ../out\n", encoding="utf-8")
        return str(repo)

    @classmethod
    def tearDownClass(cls) -> None:
        for name in ("client", "emu"):
            part = getattr(cls, name, None)
            if part is not None:
                (part.close if name == "client" else part.stop)()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        return self.client.call(tool, **arguments)

    def writes(self) -> list[dict[str, Any]]:
        return self.emu.admin("GET", "triage-writes")["writes"]

    # ---- the tools ----------------------------------------------------------------------------

    def test_check_connection(self) -> None:
        out = self.call("coverity_check_connection", repo_root=self.repo)
        self.assertTrue(out["ok"], out)
        self.assertIn("warning", out)  # http
        self.assertNotIn(emu_support.USERS["triager"], json.dumps(out, ensure_ascii=False))
        viewer = self.call("coverity_check_connection", repo_root=self.viewer_repo)
        self.assertFalse(viewer["ok"])
        self.assertIn("Access Web services", viewer["result"])
        wrong = self.call("coverity_check_connection", repo_root=self.wrong_repo)
        self.assertFalse(wrong["ok"])
        self.assertIn("401", wrong["result"])

    def test_search_pages_filters_and_scope(self) -> None:
        seen, offset = [], 0
        while True:
            page = self.call("coverity_search_issues", repo_root=self.repo, filter={"streams": ["app-main"]},
                             limit=2, offset=offset)
            seen += [i["cid"] for i in page["issues"]]
            if not page["has_more"]:
                break
            offset = page["next_offset"]
        self.assertEqual(sorted(seen), sorted(v for k, v in self.cids.items() if k != "UNINIT"))
        high = self.call("coverity_search_issues", repo_root=self.repo,
                         filter={"streams": ["app-main"], "impacts": ["High"]}, response_format="detailed")
        self.assertEqual({i["checker"] for i in high["issues"]}, {"NULL_RETURNS", "RESOURCE_LEAK"})
        self.assertEqual(high["issues"][0]["stream"], "app-main")
        fixed = self.call("coverity_search_issues", repo_root=self.repo,
                          filter={"streams": ["app-main"], "triage": {"status": ["Fixed"]}})
        self.assertEqual([i["cid"] for i in fixed["issues"]], [self.cids["UNINIT"]])
        misra = self.call("coverity_search_issues", repo_root=self.repo,
                          filter={"streams": ["app-main"], "checkers": ["MISRA C-2012 *"]})
        self.assertEqual([i["checker"] for i in misra["issues"]], ["MISRA C-2012 Rule 10.4"])

    def test_unknown_value_is_left_out_and_reported(self) -> None:
        out = self.call("coverity_search_issues", repo_root=self.repo,
                        # after a value that matches: the server refuses the whole request all the same
                        filter={"streams": ["app-main"], "triage": {"classification": ["Unclassified", "Tested Elsewhere"]}})
        self.assertEqual(out["count"], 4)
        self.assertEqual(out["ignored_values"], [{"filter": "triage.classification", "value": "Tested Elsewhere"}])

    def test_search_to_a_file(self) -> None:
        run = self.tmp / "run-search"
        run.mkdir()
        (run / "filter.json").write_text(json.dumps({"streams": ["app-main"], "impacts": ["High", "Medium"]}),
                                         encoding="utf-8")
        out = self.call("coverity_search_issues", repo_root=self.repo, filter_file=str(run / "filter.json"),
                        output_file=str(run / "issues.json"))
        self.assertEqual((out["done"], out["found"], out["analyzed_revision"]), (True, 3, "rev-2"))
        state = json.loads((run / "issues.json").read_text(encoding="utf-8"))
        self.assertTrue(state["finished_at"])
        self.assertTrue(all(i["file"].startswith("/build/app/") for i in state["issues"]))

    def test_warning_paths(self) -> None:
        cid = self.cids["NULL_RETURNS"]
        out = self.call("coverity_get_issue_details", repo_root=self.repo, stream="app-main", cids=[cid, 99999])
        first = out["issues"][0]
        self.assertEqual((first["cid"], first["line"], first["function"]), (cid, 42, "read_header"))
        self.assertEqual([e["line"] for e in first["events"]], [40, 5, 42])  # the nested event after its parent
        self.assertTrue(first["events"][-1]["main"])
        self.assertIn("NULL_RETURNS long description", first["checker_description"])
        self.assertIn("99999", out["failed"])
        folder = self.tmp / "run-details" / "items"
        folder.mkdir(parents=True)
        saved = self.call("coverity_get_issue_details", repo_root=self.repo, stream="app-main", cids=[cid],
                          output_dir=str(folder / str(cid)))
        self.assertEqual(saved["saved"], [cid])
        data = json.loads((folder / str(cid) / f"issue-{cid}.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"fetched_at", "issue", "events", "checker_description"})
        with self.assertRaises(ToolCallError) as caught:
            self.call("coverity_get_issue_details", repo_root=self.repo, cids=[cid])
        self.assertIn("stream を渡してください", str(caught.exception))

    def test_write_back_with_entries(self) -> None:
        cid = self.cids["RESOURCE_LEAK"]
        entries = [{"id": str(cid), "cids": [cid], "stream": "app-release", "classification": "False Positive",
                    "action": "Ignore", "severity": "Unspecified", "comment": "誤検知。fp はこの関数の外で閉じる（src/io.c:95）。"}]
        before = len(self.writes())
        with self.assertRaises(ToolCallError):
            self.call("coverity_update_triage", repo_root=self.repo, confirmation_token="000000000000", entries=entries)
        self.assertEqual(len(self.writes()), before)
        preview = self.call("coverity_preview_triage", entries=entries)
        self.assertEqual(len(self.writes()), before)  # the preview writes nothing
        out = self.call("coverity_update_triage", repo_root=self.repo,
                        confirmation_token=preview["confirmation_token"], entries=entries)
        self.assertEqual(out["written"], [str(cid)])
        writes = self.writes()[before:]
        self.assertEqual([(w["store"], w["cids"]) for w in writes], [("Release Triage Store", [cid])])
        self.assertEqual(writes[0]["attributes"]["Classification"], "False Positive")
        again = self.call("coverity_update_triage", repo_root=self.repo,
                          confirmation_token=preview["confirmation_token"], entries=entries)
        self.assertEqual((again["written"], again["written_total"]), ([], 1))
        self.assertEqual(len(self.writes()), before + 1)
        release = self.call("coverity_search_issues", repo_root=self.repo,
                            filter={"streams": ["app-release"], "triage": {"classification": ["False Positive"]}})
        self.assertEqual([i["cid"] for i in release["issues"]], [cid])

    def test_write_back_with_a_plan_file(self) -> None:
        cid = self.cids["CHECKED_RETURN"]
        run = self.tmp / "run-plan"
        run.mkdir()
        plan = run / "apply-plan.json"
        plan.write_text(json.dumps({"run_id": "20261108-101500", "created_at": "2026-11-08T10:15:00+09:00",
                                    "triage": [{"id": str(cid), "cids": [cid], "stream": "app-main",
                                                "classification": "Intentional", "action": "Ignore",
                                                "severity": "Unspecified", "comment": "意図的。戻り値は使わない。"}],
                                    "code": [], "reject": []}, ensure_ascii=False, indent=2), encoding="utf-8")
        token = self.call("coverity_preview_triage", plan_file=str(plan))["confirmation_token"]
        out = self.call("coverity_update_triage", repo_root=self.repo, confirmation_token=token, plan_file=str(plan))
        self.assertEqual(out["written"], [str(cid)])
        result = json.loads((run / "apply-plan.result.json").read_text(encoding="utf-8"))
        self.assertEqual([v["triage_store"] for v in result["written"].values()], ["Default Triage Store"])

    def test_server_errors_stop_the_call_and_it_recovers(self) -> None:
        self.emu.config(inject=[{"path": "^/api/v2/issues/search$", "status": 503, "count": 1}])
        try:
            with self.assertRaises(ToolCallError) as caught:
                self.call("coverity_search_issues", repo_root=self.repo, filter={"streams": ["app-main"]})
            self.assertIn("503", str(caught.exception))
            self.assertEqual(self.call("coverity_search_issues", repo_root=self.repo,
                                       filter={"streams": ["app-main"]})["count"], 4)
        finally:
            self.emu.config(reset=True)

    def test_redirect_is_reported(self) -> None:
        repo = self.make_repo("repo-login", "", url=self.emu.url + "/login")
        out = self.call("coverity_check_connection", repo_root=repo)
        self.assertFalse(out["ok"])

    # ---- other settings -------------------------------------------------------------------------

    def test_environment_only_mode(self) -> None:
        env = dict(self.base_env, COVERITY_URL=self.emu.url, COV_USER="triager", COV_AUTH_KEY=emu_support.USERS["triager"],
                   HTTPS_PROXY="${HTTPS_PROXY}", HTTP_PROXY="${HTTP_PROXY}")
        client = McpClient(self.command(), env)
        try:
            self.assertTrue(client.call("coverity_check_connection")["ok"])
            self.assertEqual(client.call("coverity_search_issues", filter={"streams": ["app-main"]})["count"], 4)
        finally:
            client.close()
        self.assertNotIn(emu_support.USERS["triager"], "".join(client.stderr))

    @unittest.skipUnless(shutil.which("openssl"), "openssl is needed for the self-signed certificate")
    def test_https_with_a_ca_file(self) -> None:
        tls = emu_support.Emulator(self.tmp / "emu-tls", "--tls")
        try:
            tls.admin("POST", "seed", emu_support.SEED)
            trusted = self.make_repo("repo-tls", f"  ca_file: {tls.info['ca_file']}\n", url=tls.url)
            untrusted = self.make_repo("repo-tls-os", "", url=tls.url)
            out = self.call("coverity_check_connection", repo_root=trusted)
            self.assertTrue(out["ok"], out)
            self.assertNotIn("warning", out)
            out = self.call("coverity_check_connection", repo_root=untrusted)
            self.assertFalse(out["ok"])
            self.assertRegex(out["result"], re.compile("接続できません|CERTIFICATE|certificate", re.I))
        finally:
            tls.stop()


if __name__ == "__main__":
    unittest.main()
