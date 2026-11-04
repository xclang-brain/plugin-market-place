"""MCP サーバー coverity-triage（server/）の試験（REQ-108）。

Coverity Connect の代わりに、httpx.MockTransport が仕様書の見本どおりの REST と SOAP の応答を返す
（mcp_server_mock.py）。ツールの関数を直接呼んで確かめる。MCP の stdio でつなぐ試験も1つある。

動かし方:
  - server の依存（mcp、pydantic、httpx、pyyaml、truststore、anyio）がある Python では、そのまま動く。
  - 依存がない Python（tests/run.py をシステムの python3 で動かしたときなど）では、uv があれば、
    UV_PROJECT_ENVIRONMENT を一時のフォルダにして uv run --native-tls --frozen --directory <server> で
    同じ試験を子のプロセスとして動かし、その結果で合否を出す（上限 10 分）。uv もなければ skip にする。
    どちらでも、プラグインの中に server/.venv は作らない。
  - 手で動かすとき:

      UV_PROJECT_ENVIRONMENT=<一時のフォルダ> uv run --native-tls --frozen \\
        --directory <プラグインのフォルダ>/server \\
        python -m unittest discover -s <プラグインのフォルダ>/tests/stage2/task -p test_mcp_server.py -v
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[2]
SERVER_SRC = PLUGIN / "server" / "src"

NEEDED = ["mcp", "pydantic", "httpx", "yaml", "truststore", "anyio"]
MISSING = [m for m in NEEDED if importlib.util.find_spec(m) is None]
UV = shutil.which("uv")
RESULTS_ENV = "COVERITY_TRIAGE_MCP_TEST_RESULTS"  # 子のプロセスが結果を書くファイル（親が渡す）
IN_CHILD = bool(os.environ.get(RESULTS_ENV))
CHILD_TIMEOUT = 600  # 子のプロセスの上限（秒）
SKIP_REASON = ("MCP サーバーの依存（%s）がこの Python になく、uv も見つかりません。uv を入れるか、"
               "依存のある Python で動かしてください" % ", ".join(MISSING))

if not MISSING:
    if str(SERVER_SRC) not in sys.path:
        sys.path.insert(0, str(SERVER_SRC))
    import logging

    import anyio
    from mcp.server.mcpserver.exceptions import ToolError

    from coverity_triage import connect, server
    from coverity_triage.connect import ConnectClient

    import mcp_server_mock as mock_cov
    from mcp_server_mock import FakeCoverity, KEY, STREAM, USER

    logging.getLogger("httpx").setLevel(logging.WARNING)  # 要求ごとの記録を試験の出力に出さない


def _token(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def entries_token(entries: list) -> str:
    """README に書いた作り方：正規化した {"triage": entries} の JSON（キーの順で並べ、空白なし）の SHA-256 の先頭12文字。"""
    normal = [{"id": e["id"], "cids": [int(c) for c in e["cids"]], "classification": e["classification"],
               "action": e["action"], "severity": e.get("severity", ""), "comment": e.get("comment", "")}
              for e in entries]
    text = json.dumps({"triage": normal}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _token(text.encode("utf-8"))


@unittest.skipIf(MISSING, SKIP_REASON if MISSING else "")
class ServerCase(unittest.TestCase):
    """一時のリポジトリ（.coverity-triage/config.yaml）と、見本の応答を返す Coverity を用意する。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cov-mcp-test-"))
        self.addCleanup(self._cleanup)
        self.repo = self.tmp / "repo"
        (self.repo / ".coverity-triage").mkdir(parents=True)
        (self.repo / ".coverity-triage" / "config.yaml").write_text(
            "coverity:\n"
            f"  url: {mock_cov.BASE}/\n"
            "  user_env: COVTEST_USER\n"
            "  key_env: COVTEST_KEY\n"
            "vcs:\n  type: git\n", encoding="utf-8")
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.cov = FakeCoverity()
        patches = [
            mock.patch.dict(os.environ, {"COVTEST_USER": USER, "COVTEST_KEY": KEY}),
            mock.patch.object(server, "make_client",
                              lambda config: ConnectClient(config, transport=self.cov.transport())),
            mock.patch.object(server, "check_auth",
                              lambda config: connect.check_auth(config, transport=self.cov.transport())),
            mock.patch.object(tempfile, "tempdir", str(self.tmp / "systemp")),
        ]
        (self.tmp / "systemp").mkdir()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        connect._columns_cache.clear()
        self.addCleanup(connect._columns_cache.clear)

    def _cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def filter_file(self, **values) -> str:
        path = self.run_dir / "filter.json"
        path.write_text(json.dumps({"name": "t", "streams": [STREAM], **values}), encoding="utf-8")
        return str(path)

    def assertToolError(self, func, *args, **kwargs) -> str:
        with self.assertRaises(ToolError) as caught:
            func(*args, **kwargs)
        message = str(caught.exception)
        self.assertNotIn("Expecting value", message)  # JSONDecodeError のままの文ではない
        return message


class SearchInlineTest(ServerCase):

    def test_req108_search_inline_returns_issues(self) -> None:
        out = server.search_issues(str(self.repo), filter={"streams": [STREAM]})
        self.assertEqual([i["cid"] for i in out["issues"]], [10001, 10002])
        first = out["issues"][0]
        self.assertEqual(first, {"cid": 10001, "checker": "USE_AFTER_FREE",
                                 "file": "/builddir/build/BUILD/libev-4.33/ev_iouring.c", "line": 261,
                                 "function": "iouring_sqe_get", "impact": "High",
                                 "category": "Memory - illegal accesses", "cwe": 416, "classification": "Unclassified",
                                 "action": "Undecided", "severity": "Unspecified", "status": "New"})
        self.assertIsNone(out["next_offset"])
        self.assertEqual(out["coverity_total"], 2)
        self.assertFalse((self.run_dir / "issues.json").exists())

    def test_req108_search_inline_filters_on_server_and_after(self) -> None:
        self.cov.items.append(mock_cov.issue(10003, status="Triaged"))
        out = server.search_issues(str(self.repo), filter={"streams": [STREAM], "impacts": ["High"],
                                                           "triage": {"status": ["New"]}})
        self.assertEqual([i["cid"] for i in out["issues"]], [10001])
        self.assertEqual(out["excluded"], 1)  # 10003 は Status で外れた

    def test_req108_search_inline_pages_with_next_offset(self) -> None:
        self.cov.items = [mock_cov.issue(10001 + n) for n in range(25)]
        seen, offset, calls = [], 0, 0
        while True:
            out = server.search_issues(str(self.repo), filter={"streams": [STREAM], "limit": 10}, offset=offset)
            self.assertLessEqual(len(out["issues"]), 10)
            seen += [i["cid"] for i in out["issues"]]
            calls += 1
            if out["next_offset"] is None:
                self.assertIsNone(out["next"])
                break
            self.assertTrue(out["next"])
            offset = out["next_offset"]
        self.assertEqual(seen, [10001 + n for n in range(25)])
        self.assertEqual(calls, 3)

    def test_req108_search_inline_limit_default_20_and_max_100(self) -> None:
        self.cov.items = [mock_cov.issue(10001 + n) for n in range(30)]
        out = server.search_issues(str(self.repo), filter={"streams": [STREAM]})
        self.assertEqual(len(out["issues"]), 20)
        self.assertEqual(out["next_offset"], 20)
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM], "limit": 101})
        self.assertIn("100", message)
        self.assertIn("output_file", message)

    def test_req108_search_inline_with_filter_file(self) -> None:
        out = server.search_issues(str(self.repo), filter_file=self.filter_file(checkers=["CHECKED_*"]))
        self.assertEqual([i["cid"] for i in out["issues"]], [10002])

    def test_req108_search_needs_exactly_one_filter(self) -> None:
        message = self.assertToolError(server.search_issues, str(self.repo))
        self.assertIn("filter", message)
        message = self.assertToolError(server.search_issues, str(self.repo), filter_file=self.filter_file(),
                                       filter={"streams": [STREAM]})
        self.assertIn("filter", message)
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM, "other"]})
        self.assertIn("ストリーム", message)

    def test_req108_search_offset_is_only_for_inline(self) -> None:
        self.assertToolError(server.search_issues, str(self.repo), filter_file=self.filter_file(),
                             output_file=str(self.run_dir / "issues.json"), offset=5)

    def test_req108_search_inline_filter_refuses_unknown_keys(self) -> None:
        # 書き間違えた項目（triage の外の status、checkers ではなく checker）を黙って捨てて、絞らずに返さない
        message = self.assertToolError(server.search_issues, str(self.repo),
                                       filter={"streams": [STREAM], "status": ["New"], "checker": ["NULL_RETURNS"]})
        self.assertIn("status", message)
        self.assertIn("checker", message)
        self.assertNotIn("POST /api/v2/issues/search", self.cov.requests)
        # 条件のファイル（filter_file）は今のまま、知らない項目を読み飛ばす
        out = server.search_issues(str(self.repo), filter_file=self.filter_file(note="ほかの道具が足した項目"))
        self.assertEqual([i["cid"] for i in out["issues"]], [10001, 10002])


class SearchFileTest(ServerCase):

    def test_req108_search_file_mode_keeps_issues_json(self) -> None:
        (self.run_dir / "run.json").write_text("{}", encoding="utf-8")
        output = self.run_dir / "issues.json"
        out = server.search_issues(str(self.repo), filter_file=self.filter_file(), output_file=str(output))
        self.assertTrue(out["done"])
        self.assertEqual(out["found"], 2)
        self.assertEqual(out["output_file"], str(output))
        self.assertEqual(out["analyzed_revision"], "a1b2c3d4e5")
        for key in ("limit", "limited", "coverity_total", "excluded", "excluded_note", "revision_error", "seconds", "next"):
            self.assertIn(key, out)
        state = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(state["filter_sha"], _token((self.run_dir / "filter.json").read_bytes()))
        self.assertEqual(state["stream"], STREAM)
        self.assertTrue(state["finished_at"])
        self.assertEqual(state["issues"][0]["merge_key"], "dd7cea15ef54c17c70b8c3657d010001")
        self.assertEqual(state["issues"][0]["stream"], STREAM)
        self.assertIn("POST /api/v2/issues/search", [r["request"] for r in state["requests"]])
        log = [json.loads(line) for line in (self.run_dir / "operations.log").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["phase"] for r in log], ["start", "end"])
        self.assertEqual(log[0]["event"], "mcp:search_issues")

    def test_req108_search_file_mode_resumes_when_done_false(self) -> None:
        output = self.run_dir / "issues.json"
        with mock.patch.object(server.Deadline, "allows", lambda self, client, requests=1: False):
            out = server.search_issues(str(self.repo), filter_file=self.filter_file(), output_file=str(output))
        self.assertFalse(out["done"])
        self.assertTrue(out["next"])
        out = server.search_issues(str(self.repo), filter_file=self.filter_file(), output_file=str(output))
        self.assertTrue(out["done"])
        self.assertEqual(out["found"], 2)

    def test_req108_search_without_total_rows_reads_until_empty(self) -> None:
        # totalRows のない応答：行が空になるまで次のページを取る（Status は検索のあとで絞る）
        self.cov.omit_total = True
        self.cov.items = [mock_cov.issue(10001 + n, status="New" if 150 <= n < 161 else "Triaged") for n in range(230)]
        out = server.search_issues(str(self.repo), filter_file=self.filter_file(triage={"status": ["New"]}),
                                   output_file=str(self.run_dir / "issues.json"))
        self.assertTrue(out["done"])
        self.assertEqual(out["found"], 11)
        self.assertIsNone(out["coverity_total"])
        self.cov.items = [mock_cov.issue(10001 + n) for n in range(25)]
        connect._columns_cache.clear()
        first = server.search_issues(str(self.repo), filter={"streams": [STREAM]})
        self.assertEqual(len(first["issues"]), 20)
        self.assertEqual(first["next_offset"], 20)
        rest = server.search_issues(str(self.repo), filter={"streams": [STREAM]}, offset=20)
        self.assertEqual([i["cid"] for i in rest["issues"]], [10021 + n for n in range(5)])
        self.assertIsNone(rest["next_offset"])

    def test_req108_search_empty_columns_are_not_remembered(self) -> None:
        self.cov.columns_queue.append(lambda: mock_cov.json_response(200, []))
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM]})
        self.assertIn("cid", message)
        out = server.search_issues(str(self.repo), filter={"streams": [STREAM]})
        self.assertEqual(len(out["issues"]), 2)

    def test_req108_search_rest_401_is_auth_error(self) -> None:
        self.cov.rest_override = lambda request: mock_cov.rest_401()
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM]})
        self.assertIn("認証に失敗", message)

    def test_req108_search_rest_302_names_location(self) -> None:
        self.cov.rest_override = lambda request: mock_cov.redirect_302()
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM]})
        self.assertIn("302", message)
        self.assertIn(mock_cov.LOGIN, message)

    def test_req108_search_rest_html_200_is_clear_error(self) -> None:
        self.cov.rest_override = lambda request: mock_cov.html_200()
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM]})
        self.assertIn("JSON", message)
        self.assertIn("text/html", message)

    def test_req108_search_invalid_key_value_is_dropped(self) -> None:
        self.cov.unknown_keys = {"Tested Elsewhere"}
        output = self.run_dir / "issues.json"
        out = server.search_issues(str(self.repo),
                                   filter_file=self.filter_file(triage={"classification": ["Unclassified", "Tested Elsewhere"]}),
                                   output_file=str(output))
        self.assertTrue(out["done"])
        self.assertEqual(out["found"], 2)
        state = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("ignored", [r["status"] for r in state["requests"]])

    def test_req108_search_server_error_stops(self) -> None:
        self.cov.rest_override = lambda request: (mock_cov.json_response(500, {"code": 1004, "message": "Internal error"})
                                                  if request.url.path.endswith("/search") else None)
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM]})
        self.assertIn("500", message)


class CheckConnectionTest(ServerCase):

    def test_req108_check_connection_ok_and_401(self) -> None:
        out = server.check_connection(str(self.repo))
        self.assertTrue(out["ok"])
        self.assertNotIn(KEY, json.dumps(out, ensure_ascii=False))
        self.cov.rest_override = lambda request: mock_cov.rest_401()
        out = server.check_connection(str(self.repo))
        self.assertFalse(out["ok"])
        self.assertIn("401", out["result"])

    def test_req108_check_connection_redirect_is_failure(self) -> None:
        self.cov.rest_override = lambda request: mock_cov.redirect_302()
        out = server.check_connection(str(self.repo))
        self.assertFalse(out["ok"])
        self.assertIn(mock_cov.LOGIN, out["result"])

    def test_req108_check_connection_html_200_is_failure(self) -> None:
        self.cov.rest_override = lambda request: mock_cov.html_200()
        out = server.check_connection(str(self.repo))
        self.assertFalse(out["ok"])
        self.assertIn("JSON", out["result"])

    def test_req108_rest_403_names_web_services_permission(self) -> None:
        # 認証が正しくても、Access Web services の権限がなければ断られる（エラーの本文は {code, message}）
        self.cov.rest_override = lambda request: mock_cov.json_response(403, {"code": 1003, "message": "Access denied"})
        out = server.check_connection(str(self.repo))
        self.assertFalse(out["ok"])
        self.assertIn("403", out["result"])
        self.assertIn("Access Web services", out["result"])
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM]})
        self.assertIn("Access Web services", message)
        self.cov.rest_override = lambda request: mock_cov.rest_401()
        self.assertNotIn("Access Web services", server.check_connection(str(self.repo))["result"])


class GetIssuesTest(ServerCase):

    def test_req108_get_issues_inline_returns_events(self) -> None:
        out = server.get_issues(str(self.repo), STREAM, [10001, 10002])
        self.assertEqual([i["cid"] for i in out["issues"]], [10001, 10002])
        first = out["issues"][0]
        self.assertEqual(first["checker"], "USE_AFTER_FREE")
        self.assertEqual((first["file"], first["line"], first["function"]),
                         ("/builddir/build/BUILD/libev-4.33/ev_iouring.c", 261, "iouring_sqe_get"))
        self.assertEqual([e["line"] for e in first["events"]], [274, 280, 641, 261])  # 入れ子もたどる
        self.assertEqual(first["events"][1]["tag"], "freed_arg")
        self.assertEqual([e["main"] for e in first["events"]], [False, False, False, True])
        self.assertIn("Reads target of a freed pointer", first["checker_description"])
        self.assertNotIn("issue", first)
        self.assertEqual(out["failed"], {})
        self.assertEqual(out["remaining"], [])
        second = out["issues"][1]
        self.assertEqual(set(second), {"cid", "checker", "file", "line", "function", "events", "checker_description"})
        self.assertEqual(list((self.run_dir).iterdir()), [])

    def test_req108_get_issues_inline_allows_up_to_10_cids(self) -> None:
        message = self.assertToolError(server.get_issues, str(self.repo), STREAM, list(range(10001, 10012)))
        self.assertIn("10", message)

    def test_req108_get_issues_inline_cid_not_in_stream_fails_only_that_cid(self) -> None:
        out = server.get_issues(str(self.repo), STREAM, [10001, 19999])
        self.assertEqual([i["cid"] for i in out["issues"]], [10001])
        self.assertIn("19999", out["failed"])

    def test_req108_get_issues_soap_401_stops_and_saves_nothing(self) -> None:
        folder = self.run_dir / "items" / "10001"
        folder.mkdir(parents=True)
        os.environ["COVTEST_KEY"] = "wrong-key"
        message = self.assertToolError(server.get_issues, str(self.repo), STREAM, [10001, 10002], str(folder))
        self.assertIn("認証", message)
        self.assertIn("User authentication failed.", message)
        self.assertEqual(list(folder.iterdir()), [])  # 取り直せなくなる error のファイルを残さない

    def test_req108_get_issues_soap_permission_fault_stops(self) -> None:
        self.cov.soap_override = lambda request: FakeCoverity._xml(
            500, mock_cov.soap_fault(f"User {USER} Doesn't have permissions to perform {{invokeWS}}"))
        message = self.assertToolError(server.get_issues, str(self.repo), STREAM, [10001])
        self.assertIn("invokeWS", message)

    def test_req108_get_issues_other_fault_fails_only_that_cid(self) -> None:
        folder = self.run_dir / "items" / "G1"
        folder.mkdir(parents=True)
        self.cov.soap_faults[10001] = FakeCoverity._xml(500, mock_cov.soap_fault("Defect not found.", 1400))
        out = server.get_issues(str(self.repo), STREAM, [10001, 10002], str(folder))
        self.assertTrue(out["done"])
        self.assertEqual(out["saved"], [10002])
        self.assertIn("Defect not found.", out["failed"]["10001"])
        self.assertIn("error", json.loads((folder / "issue-10001.json").read_text(encoding="utf-8")))

    def test_req108_get_issues_soap_redirect_and_html_are_clear(self) -> None:
        self.cov.soap_override = lambda request: mock_cov.redirect_302()
        message = self.assertToolError(server.get_issues, str(self.repo), STREAM, [10001])
        self.assertIn(mock_cov.LOGIN, message)
        self.cov.soap_override = lambda request: mock_cov.html_200()
        message = self.assertToolError(server.get_issues, str(self.repo), STREAM, [10001])
        self.assertIn("XML", message)

    def test_req108_get_issues_filter_spec_in_xsd_order(self) -> None:
        server.get_issues(str(self.repo), STREAM, [10001])
        spec = next(n for n in self.cov.soap_requests[0] if n.tag == "filterSpec")
        self.assertEqual([n.tag for n in spec], ["includeDefectInstances", "includeHistory",
                                                 "includeTotalDefectInstanceCount", "maxDefectInstances", "streamIdList"])
        values = {n.tag: (n.text or "") for n in spec}
        self.assertEqual(values["includeTotalDefectInstanceCount"], "false")
        self.assertGreaterEqual(int(values["maxDefectInstances"]), 1)

    def test_req108_get_issues_file_mode_resumes(self) -> None:
        folder = self.run_dir / "items" / "G1"
        folder.mkdir(parents=True)

        def once(deadline, client, requests=1):
            used = getattr(deadline, "_used", False)
            deadline._used = True
            return not used

        with mock.patch.object(server.Deadline, "allows", once):
            out = server.get_issues(str(self.repo), STREAM, [10001, 10002], str(folder))
        self.assertFalse(out["done"])
        self.assertEqual((out["saved"], out["remaining"]), ([10001], [10002]))
        out = server.get_issues(str(self.repo), STREAM, [10001, 10002], str(folder))
        self.assertTrue(out["done"])
        self.assertEqual((out["already_saved"], out["saved"]), ([10001], [10002]))
        saved = json.loads((folder / "issue-10001.json").read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"fetched_at", "issue", "events", "checker_description"})
        self.assertEqual(saved["issue"]["cid"], 10001)


ENTRIES = [
    {"id": "10001", "cids": [10001], "classification": "False Positive", "action": "Ignore",
     "comment": "ループの前で NULL を確かめている"},
    {"id": "G1", "cids": [10002, 10003], "classification": "Bug", "action": "Fix Required", "severity": "Major"},
]


class PreviewUpdateTest(ServerCase):

    def write_plan(self, entries=ENTRIES) -> Path:
        path = self.run_dir / "apply-plan.json"
        path.write_text(json.dumps({"run_id": "r1", "triage": entries, "code": [], "reject": []},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_req108_preview_entries_token_and_items(self) -> None:
        out = server.preview_triage(entries=ENTRIES)
        self.assertEqual(out["confirmation_token"], entries_token(ENTRIES))
        self.assertEqual(out["counts"], {"entries": 2, "cids": 3})
        self.assertEqual(out["items"][1], {"id": "G1", "cids": [10002, 10003], "classification": "Bug",
                                           "action": "Fix Required", "severity": "Major", "comment": ""})
        self.assertIn("update_triage", out["next"])
        self.assertEqual(self.cov.requests, [])  # Coverity と通信しない

    def test_req108_preview_plan_file_token_is_file_sha(self) -> None:
        plan = self.write_plan()
        out = server.preview_triage(plan_file=str(plan))
        self.assertEqual(out["confirmation_token"], _token(plan.read_bytes()))
        self.assertEqual(out["counts"], {"entries": 2, "cids": 3})
        self.assertEqual(self.cov.requests, [])

    def test_req108_preview_needs_exactly_one_source(self) -> None:
        self.assertToolError(server.preview_triage)
        self.assertToolError(server.preview_triage, plan_file=str(self.write_plan()), entries=ENTRIES)

    def test_req108_update_entries_wrong_token_writes_nothing(self) -> None:
        message = self.assertToolError(server.update_triage, str(self.repo), "000000000000", entries=ENTRIES)
        self.assertIn("preview_triage", message)
        self.assertNotIn("ct.py", message)
        self.assertEqual(self.cov.writes, [])
        self.assertNotIn("PUT /api/v2/issues/triage", self.cov.requests)

    def test_req108_update_entries_writes_once(self) -> None:
        token = server.preview_triage(entries=ENTRIES)["confirmation_token"]
        out = server.update_triage(str(self.repo), token, entries=ENTRIES)
        self.assertTrue(out["done"])
        self.assertEqual(out["written"], ["10001", "G1"])
        result_file = Path(out["result_file"])
        self.assertEqual(result_file.parent.parent, self.tmp / "systemp")
        self.assertTrue(result_file.parent.name.startswith("coverity-triage-mcp-"))
        self.assertTrue(result_file.is_file())
        self.assertEqual(self.cov.writes[0], {
            "store": "Default Triage Store", "cids": [10001],
            "attributeValuesList": [{"attributeName": "Classification", "attributeValue": "False Positive"},
                                    {"attributeName": "Action", "attributeValue": "Ignore"},
                                    {"attributeName": "Comment", "attributeValue": "ループの前で NULL を確かめている"}]})
        again = server.update_triage(str(self.repo), token, entries=ENTRIES)
        self.assertEqual((again["written"], again["written_total"]), ([], 2))
        self.assertEqual(len(self.cov.writes), 2)  # 二重に書かない

    def test_req108_update_500_stops_the_call(self) -> None:
        # ナレッジ K-82：5xx は全体を止める。止まった項目は記録しないので、呼び直すと書き込む
        self.cov.triage_responses[10001] = lambda: mock_cov.json_response(
            500, {"code": 1004, "message": "Invalid key provided: Ignore"})
        token = entries_token(ENTRIES)
        message = self.assertToolError(server.update_triage, str(self.repo), token, entries=ENTRIES)
        self.assertIn("500", message)
        self.assertEqual(self.cov.writes, [])

    def test_req108_update_401_stops(self) -> None:
        self.cov.triage_responses[10001] = mock_cov.rest_401
        message = self.assertToolError(server.update_triage, str(self.repo), entries_token(ENTRIES), entries=ENTRIES)
        self.assertIn("認証に失敗", message)
        self.assertEqual(self.cov.writes, [])

    def test_req108_update_plan_file_keeps_result_file(self) -> None:
        plan = self.write_plan()
        token = _token(plan.read_bytes())
        message = self.assertToolError(server.update_triage, str(self.repo), "abcdefabcdef", plan_file=str(plan))
        self.assertIn("一致しません", message)
        out = server.update_triage(str(self.repo), token, plan_file=str(plan))
        self.assertEqual(out["result_file"], str(self.run_dir / "apply-plan.result.json"))
        self.assertEqual((out["written"], out["written_total"], out["entries"]), (["10001", "G1"], 2, 2))
        result = json.loads((self.run_dir / "apply-plan.result.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(v["id"] for v in result["written"].values()), ["10001", "G1"])
        out = server.update_triage(str(self.repo), token, plan_file=str(plan))
        self.assertEqual(out["written"], [])
        self.assertEqual(len(self.cov.writes), 2)

    def puts(self) -> int:
        return self.cov.requests.count("PUT /api/v2/issues/triage")

    def test_req108_update_5xx_stop_the_call(self) -> None:
        # 5xx は、ほかの項目も同じく通らないことが多いので、止める（K-82。失敗の記録も残さない）
        token = entries_token(ENTRIES)
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.cov.triage_responses[10001] = lambda status=status: mock_cov.html_error(status)
                before = self.puts()
                message = self.assertToolError(server.update_triage, str(self.repo), token, entries=ENTRIES)
                self.assertIn(str(status), message)
                self.assertEqual(self.puts() - before, 1)  # G1 は送らない
                self.assertEqual(self.cov.writes, [])
        del self.cov.triage_responses[10001]  # サーバーが戻った
        out = server.update_triage(str(self.repo), token, entries=ENTRIES)
        self.assertEqual((out["written"], out["failed"]), (["10001", "G1"], []))

    def test_req108_update_plan_file_500_then_written_again(self) -> None:
        plan = self.write_plan()
        token = _token(plan.read_bytes())
        self.cov.triage_responses[10002] = lambda: mock_cov.json_response(500, {"code": 1004, "message": "Internal error"})
        message = self.assertToolError(server.update_triage, str(self.repo), token, plan_file=str(plan))
        self.assertIn("500", message)
        result = json.loads((self.run_dir / "apply-plan.result.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(v["id"] for v in result["written"].values()), ["10001"])  # 止まる前の項目は残る
        self.assertEqual(result["failed"], {})  # 止まった項目は記録しない
        del self.cov.triage_responses[10002]  # サーバーが戻った
        again = server.update_triage(str(self.repo), token, plan_file=str(plan))
        self.assertEqual((again["written"], again["written_total"], again["failed"]), (["G1"], 2, []))
        self.assertEqual(len(self.cov.writes), 2)  # 10001 は二重に書かない

    def test_req108_update_4xx_refusal_is_not_sent_again_with_same_token(self) -> None:
        # 値の誤りで断られた項目は、同じ確認の文字列で呼び直しても送らない（値を直して確かめ直す）
        self.cov.triage_responses[10001] = lambda: mock_cov.json_response(
            400, {"code": 1200, "message": "Attribute name not found"})
        token = entries_token(ENTRIES)
        out = server.update_triage(str(self.repo), token, entries=ENTRIES)
        self.assertEqual(out["written"], ["G1"])
        self.assertIn("400", out["failed"][0]["error"])
        before = self.puts()
        again = server.update_triage(str(self.repo), token, entries=ENTRIES)
        self.assertEqual((again["written"], [f["id"] for f in again["failed"]]), ([], ["10001"]))
        self.assertEqual(self.puts(), before)

    def test_req108_update_entries_result_file_per_user_and_server(self) -> None:
        token = entries_token(ENTRIES)
        with mock.patch("getpass.getuser", return_value="alice"):
            out = server.update_triage(str(self.repo), token, entries=ENTRIES)
            result_file = Path(out["result_file"])
            self.assertEqual(result_file.parent, self.tmp / "systemp" / "coverity-triage-mcp-alice")
            if os.name == "posix":
                self.assertEqual(result_file.parent.stat().st_mode & 0o077, 0)  # ほかのユーザーは読めない
            saved = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual((saved["url"], saved["triage_store"]), (mock_cov.BASE, "Default Triage Store"))
            # 並べ方を変えても（確認の文字列が変わっても）、書き込み済みの項目は書き直さない
            reordered = list(reversed(ENTRIES))
            again = server.update_triage(str(self.repo), entries_token(reordered), entries=reordered)
            self.assertEqual((again["written"], again["written_total"]), ([], 2))
            self.assertEqual(again["result_file"], out["result_file"])
            self.assertEqual(len(self.cov.writes), 2)
            # 別のトリアージストア（同じ Coverity）には、改めて書く
            repo2 = self.tmp / "repo2"
            (repo2 / ".coverity-triage").mkdir(parents=True)
            (repo2 / ".coverity-triage" / "config.yaml").write_text(
                f"coverity:\n  url: {mock_cov.BASE}\n  user_env: COVTEST_USER\n  key_env: COVTEST_KEY\n"
                "  triage_store: Store Two\n", encoding="utf-8")
            other = server.update_triage(str(repo2), token, entries=ENTRIES)
            self.assertEqual(other["written"], ["10001", "G1"])
            self.assertNotEqual(other["result_file"], out["result_file"])
            self.assertEqual([w["store"] for w in self.cov.writes[2:]], ["Store Two", "Store Two"])
        with mock.patch("getpass.getuser", return_value="bob"):
            out = server.update_triage(str(self.repo), token, entries=ENTRIES)
            self.assertEqual(Path(out["result_file"]).parent.name, "coverity-triage-mcp-bob")

    @unittest.skipUnless(hasattr(os, "getuid"), "フォルダの持ち主を見るのは Linux と macOS だけ")
    def test_req108_update_entries_refuses_folder_of_another_user(self) -> None:
        (self.tmp / "systemp" / "coverity-triage-mcp-alice").mkdir()
        with mock.patch("getpass.getuser", return_value="alice"), \
                mock.patch("os.getuid", return_value=os.getuid() + 1):  # 別のユーザーが先に作ったフォルダ
            message = self.assertToolError(server.update_triage, str(self.repo), entries_token(ENTRIES),
                                           entries=ENTRIES)
        self.assertIn("ほかのユーザー", message)
        self.assertEqual(self.cov.writes, [])


class MessagesTest(ServerCase):

    def test_req108_errors_say_what_to_fix_without_plugin_commands(self) -> None:
        (self.repo / ".coverity-triage" / "config.yaml").unlink()
        message = self.assertToolError(server.check_connection, str(self.repo))
        self.assertIn(str(self.repo / ".coverity-triage" / "config.yaml"), message)
        self.assertIn("coverity.url", message)
        for name in ("/coverity-", "ct.py"):
            self.assertNotIn(name, message)
        (self.repo / ".coverity-triage" / "config.yaml").write_text(
            f"coverity:\n  url: {mock_cov.BASE}\n  user_env: COVTEST_NONE_USER\n  key_env: COVTEST_NONE_KEY\n",
            encoding="utf-8")
        out = server.check_connection(str(self.repo))
        self.assertFalse(out["ok"])
        self.assertIn("COVTEST_NONE_USER", out["result"])
        self.assertNotIn("/coverity-", out["result"])
        message = self.assertToolError(server.search_issues, str(self.repo), filter={"streams": [STREAM]})
        self.assertIn("COVTEST_NONE_KEY", message)
        self.assertNotIn("/coverity-", message)


@unittest.skipIf(MISSING, SKIP_REASON if MISSING else "")
class StdioTest(unittest.TestCase):

    def test_req108_stdio_lists_tools_without_plugin_paths(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = dict(os.environ, PYTHONPATH=str(SERVER_SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        params = StdioServerParameters(command=sys.executable, args=["-m", "coverity_triage.server"],
                                       env=env, cwd=str(PLUGIN / "server"))

        async def talk():
            with anyio.fail_after(60):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        tools = await session.list_tools()
                        preview = await session.call_tool("preview_triage", {"entries": ENTRIES})
                        unknown = await session.call_tool("search_issues", {
                            "repo_root": str(PLUGIN / "no-such-repo"),
                            "filter": {"streams": [STREAM], "status": ["New"]}})
                        return init, tools, preview, unknown

        init, tools, preview, unknown = anyio.run(talk)
        self.assertEqual(init.server_info.name, "coverity-triage")
        by_name = {t.name: t for t in tools.tools}
        self.assertEqual(set(by_name), {"check_connection", "search_issues", "get_issues", "preview_triage", "update_triage"})
        self.assertEqual(set(by_name["search_issues"].input_schema["properties"]),
                         {"repo_root", "filter_file", "output_file", "filter", "offset"})
        self.assertEqual(by_name["search_issues"].input_schema["required"], ["repo_root"])
        self.assertTrue(by_name["preview_triage"].annotations.read_only_hint)
        texts = [init.instructions or ""] + [t.description or "" for t in tools.tools]
        for text in texts:
            for name in ("ct.py", "/coverity-", "coverity-triage-scripts", "plugin_paths"):
                self.assertNotIn(name, text)
        self.assertFalse(preview.is_error)
        self.assertEqual(preview.structured_content["confirmation_token"], entries_token(ENTRIES))
        # 条件の object に知らない項目があれば、MCP のクライアントから呼んでも断る（スキーマにも書いてある）
        schema = by_name["search_issues"].input_schema
        filter_ref = next(s["$ref"] for s in schema["properties"]["filter"]["anyOf"] if "$ref" in s)
        self.assertIs(schema["$defs"][filter_ref.rsplit("/", 1)[-1]]["additionalProperties"], False)
        self.assertTrue(unknown.is_error)
        self.assertIn("filter.status", unknown.content[0].text)


# ---- 依存のない Python から、uv の一時の環境で同じ試験を動かす --------------------------------------

def _run_with_limit(command: list, env: dict) -> tuple:
    """終了コードと出力。上限の時間を過ぎたら、子のプロセスをまとめて止めて、終了コードを None にする。"""
    posix = os.name == "posix"
    proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=posix)
    try:
        out, _ = proc.communicate(timeout=CHILD_TIMEOUT)
        return proc.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            if posix:
                os.killpg(proc.pid, signal.SIGKILL)  # uv が動かした python も止める
            else:
                proc.kill()
        except OSError:
            pass
        out, _ = proc.communicate()
        return None, out.decode("utf-8", "replace")


class UvChildTest(unittest.TestCase):
    """依存のない Python のときだけ動く。同じ試験を uv の一時の環境で子のプロセスとして動かす。"""

    def test_req108_mcp_server_tests_in_uv_environment(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="cov-mcp-uv-"))
        self.addCleanup(shutil.rmtree, work, True)
        results = work / "results.json"
        env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(work / "venv"))
        env[RESULTS_ENV] = str(results)
        env.pop("VIRTUAL_ENV", None)  # uv が別の環境を指していると警告するため
        command = [UV, "run", "--native-tls", "--frozen", "--directory", str(PLUGIN / "server"),
                   "python", str(Path(__file__).resolve())]
        code, output = _run_with_limit(command, env)
        if code is None:
            self.fail(f"uv の環境での試験が {CHILD_TIMEOUT} 秒で終わりませんでした: {output[-1500:]}")
        if not results.is_file():
            self.fail(f"uv の環境で試験を動かせませんでした（終了コード {code}）: {output[-1500:]}")
        tests = json.loads(results.read_text(encoding="utf-8"))
        self.assertTrue(tests, "uv の環境で試験が1つも動きませんでした")
        for test in tests:
            with self.subTest(test["name"]):
                if test["status"] != "passed":  # uv の環境では skip もあってはならない
                    self.fail(f"{test['status']}: {test['reason']}")
        if all(t["status"] == "passed" for t in tests):
            self.assertEqual(code, 0, output[-1500:])


class ChildDependencyTest(unittest.TestCase):
    """子のプロセスなのに依存がないとき（uv の環境が壊れている）。skip にせず、不合格にする。"""

    def test_req108_uv_environment_has_server_dependencies(self) -> None:
        self.fail("uv の環境に MCP サーバーの依存（%s）がありません" % ", ".join(MISSING))


def _flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _flatten(test)
        else:
            yield test


def load_tests(loader, standard_tests, pattern):
    """依存があれば試験そのもの。なければ uv の子のプロセスで動かす1件（uv もなければ、すべて skip）。"""
    if MISSING and IN_CHILD:
        return loader.loadTestsFromTestCase(ChildDependencyTest)
    if MISSING and UV:
        return loader.loadTestsFromTestCase(UvChildTest)
    return unittest.TestSuite(t for t in _flatten(standard_tests)
                              if not isinstance(t, (UvChildTest, ChildDependencyTest)))


class _CollectingResult(unittest.TextTestResult):
    """子のプロセスの結果を {name, status, reason} にためる（親の試験が読む）。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.items: list = []

    def _add(self, test, status: str, err=None, reason: str = "") -> None:
        if err is not None:
            reason = "".join(traceback.format_exception_only(err[0], err[1])).strip()
        name = test.id().replace("__main__.", "test_mcp_server.", 1)
        self.items.append({"name": name, "status": status, "reason": reason[:1000]})

    def addSuccess(self, test) -> None:
        super().addSuccess(test)
        self._add(test, "passed")

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self._add(test, "failed", err)

    def addError(self, test, err) -> None:
        super().addError(test, err)
        self._add(test, "failed", err)

    def addSkip(self, test, reason) -> None:
        super().addSkip(test, reason)
        self._add(test, "skipped", reason=reason)

    def addSubTest(self, test, subtest, err) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._add(subtest, "failed", err)

    def addExpectedFailure(self, test, err) -> None:
        super().addExpectedFailure(test, err)
        self._add(test, "passed")

    def addUnexpectedSuccess(self, test) -> None:
        super().addUnexpectedSuccess(test)
        self._add(test, "failed", reason="失敗するはずの試験が通った")


def _run_as_child(results_path: str) -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2, resultclass=_CollectingResult).run(suite)
    Path(results_path).write_text(json.dumps(result.items, ensure_ascii=False), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if IN_CHILD:
        sys.exit(_run_as_child(os.environ[RESULTS_ENV]))
    unittest.main()
