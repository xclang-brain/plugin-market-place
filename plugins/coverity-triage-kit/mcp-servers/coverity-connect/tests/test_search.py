"""coverity_search_issues: inline pages and the issues.json file (fake Coverity Connect over httpx)."""

from __future__ import annotations

import hashlib
import json
import unittest
from unittest import mock

from support import MISSING, only_with_packages

load_tests = only_with_packages

if not MISSING:
    from support import STREAM, FakeCoverity, ServerCase, issue  # noqa: F401
    import support as cov

    class SearchInlineTest(ServerCase):

        def search(self, **kwargs):
            return self.tools.search_issues(repo_root=str(self.repo), **kwargs)

        def test_concise_page_with_pagination_fields(self) -> None:
            out = self.search(filter={"streams": [STREAM]})
            self.assertEqual([i["cid"] for i in out["issues"]], [10001, 10002])
            self.assertEqual(out["issues"][0], {
                "cid": 10001, "checker": "USE_AFTER_FREE", "file": "/builddir/build/BUILD/libev-4.33/ev_iouring.c",
                "line": 261, "function": "iouring_sqe_get", "impact": "High", "classification": "Unclassified",
                "action": "Undecided", "status": "New", "stream": STREAM})
            for key in ("stream", "count", "offset", "total", "has_more", "next_offset", "excluded"):
                self.assertIn(key, out)
            self.assertEqual((out["count"], out["total"], out["has_more"], out["next_offset"]), (2, 2, False, None))
            self.assertIsNone(out["next"])
            self.assertEqual(list(self.run_dir.iterdir()), [])  # nothing written

        def test_detailed_has_every_field(self) -> None:
            out = self.search(filter={"streams": [STREAM]}, response_format="detailed")
            first = out["issues"][0]
            for key in ("category", "cwe", "severity", "merge_key", "stream"):
                self.assertIn(key, first)
            self.assertEqual(first["cwe"], 416)

        def test_filters_on_server_and_after(self) -> None:
            self.cov.items.append(issue(10003, status="Triaged"))
            out = self.search(filter={"streams": [STREAM], "impacts": ["high"], "triage": {"status": ["new"]}})
            self.assertEqual([i["cid"] for i in out["issues"]], [10001])  # values matched without regard to case
            self.assertEqual(out["excluded"], 1)  # 10003 left out by its Status
            body = self.cov.search_bodies[-1]
            self.assertEqual(body["snapshotScope"]["show"]["scope"], "last()")
            impact = next(f for f in body["filters"] if f["columnKey"] == "displayImpact")
            self.assertEqual(impact["matchers"], [{"type": "keyMatcher", "key": "High"}])

        def test_fixed_status_searches_every_snapshot(self) -> None:
            self.cov.items.append(issue(10003, status="Fixed"))
            out = self.search(filter={"streams": [STREAM], "triage": {"status": ["Fixed"]}})
            self.assertEqual([i["cid"] for i in out["issues"]], [10003])
            self.assertEqual(self.cov.search_bodies[-1]["snapshotScope"]["show"]["scope"], "first()..last()")

        def test_pages_with_next_offset(self) -> None:
            self.cov.items = [issue(10001 + n) for n in range(25)]
            seen, offset, calls = [], 0, 0
            while True:
                out = self.search(filter={"streams": [STREAM]}, limit=10, offset=offset)
                self.assertLessEqual(len(out["issues"]), 10)
                seen += [i["cid"] for i in out["issues"]]
                calls += 1
                if not out["has_more"]:
                    self.assertIsNone(out["next_offset"])
                    break
                self.assertTrue(out["next"])
                offset = out["next_offset"]
            self.assertEqual(seen, [10001 + n for n in range(25)])
            self.assertEqual(calls, 3)

        def test_limit_default_20_and_range(self) -> None:
            self.cov.items = [issue(10001 + n) for n in range(30)]
            out = self.search(filter={"streams": [STREAM]})
            self.assertEqual((len(out["issues"]), out["next_offset"], out["has_more"]), (20, 20, True))
            message = self.assertFailure(self.search, filter={"streams": [STREAM]}, limit=101)
            self.assertIn("100", message)
            self.assertIn("output_file", message)

        def test_large_pages_are_cut_below_the_character_limit(self) -> None:
            long_name = "f" * 400
            self.cov.items = [issue(10001 + n, displayFunction=long_name) for n in range(100)]
            out = self.search(filter={"streams": [STREAM]}, limit=100, response_format="detailed")
            self.assertLess(len(json.dumps(out, ensure_ascii=False)), self.tools.CHARACTER_LIMIT)
            self.assertTrue(out["has_more"])
            self.assertIn("truncated", out)
            rest = self.search(filter={"streams": [STREAM]}, limit=100, offset=out["next_offset"],
                               response_format="detailed")
            self.assertEqual(rest["issues"][0]["cid"], out["issues"][-1]["cid"] + 1)  # none skipped

        def test_filter_file_yaml_and_json(self) -> None:
            out = self.search(filter_file=self.filter_file(checkers=["CHECKED_*"]))
            self.assertEqual([i["cid"] for i in out["issues"]], [10002])
            out = self.search(filter_file=self.filter_file("untriaged.yaml", triage={"classification": ["Unclassified"]},
                                                           note="ほかの道具が足した項目"))
            self.assertEqual([i["cid"] for i in out["issues"]], [10001, 10002])
            self.assertEqual(out["skipped_keys"], ["note"])

        def test_needs_exactly_one_filter_and_one_stream(self) -> None:
            self.assertIn("filter", self.assertFailure(self.search))
            self.assertIn("filter", self.assertFailure(self.search, filter_file=self.filter_file(),
                                                       filter={"streams": [STREAM]}))
            self.assertIn("ストリーム", self.assertFailure(self.search, filter={"streams": [STREAM, "other"]}))
            self.assertIn("streams", self.assertFailure(self.search, filter={"impacts": ["High"]}))

        def test_offset_is_only_for_inline(self) -> None:
            self.assertFailure(self.search, filter_file=self.filter_file(), output_file=str(self.run_dir / "i.json"),
                               offset=5)

        def test_filter_object_refuses_unknown_keys(self) -> None:
            message = self.assertFailure(self.search, filter={"streams": [STREAM], "status": ["New"],
                                                              "checker": ["NULL_RETURNS"]})
            self.assertIn("status", message)
            self.assertIn("checker", message)
            self.assertNotIn("POST /api/v2/issues/search", self.cov.requests)

        def test_relative_paths_are_refused(self) -> None:
            self.assertIn("絶対パス", self.assertFailure(self.tools.search_issues, repo_root="repo",
                                                       filter={"streams": [STREAM]}))
            self.assertIn("絶対パス", self.assertFailure(self.search, filter_file="filters/x.yaml"))

        def test_unknown_value_is_dropped_and_reported(self) -> None:
            self.cov.unknown_keys = {"Tested Elsewhere"}
            out = self.search(filter={"streams": [STREAM],
                                      "triage": {"classification": ["Unclassified", "Tested Elsewhere"]}})
            self.assertEqual(out["count"], 2)
            self.assertEqual(out["ignored_values"], [{"filter": "triage.classification", "value": "Tested Elsewhere"}])
            self.assertIn("ignored_note", out)

        def test_only_unknown_values_match_nothing(self) -> None:
            self.cov.unknown_keys = {"Tested Elsewhere"}
            out = self.search(filter={"streams": [STREAM], "triage": {"classification": ["Tested Elsewhere"]}})
            self.assertEqual((out["count"], out["has_more"]), (0, False))
            self.assertEqual(out["ignored_values"][0]["value"], "Tested Elsewhere")

        def test_unknown_status_is_reported(self) -> None:
            out = self.search(filter={"streams": [STREAM], "triage": {"status": ["Open"]}})
            self.assertEqual(out["count"], 0)
            self.assertEqual(out["ignored_values"], [{"filter": "triage.status", "value": "Open"}])

    class SearchFileTest(ServerCase):

        def search(self, **kwargs):
            return self.tools.search_issues(repo_root=str(self.repo), **kwargs)

        def test_issues_json_has_the_contract_fields(self) -> None:
            output = self.run_dir / "issues.json"
            out = self.search(filter_file=self.filter_file(), output_file=str(output))
            self.assertTrue(out["done"])
            self.assertEqual((out["found"], out["output_file"], out["analyzed_revision"]), (2, str(output), "a1b2c3d4e5"))
            for key in ("limit", "limited", "coverity_total", "excluded", "excluded_note", "revision_error", "seconds",
                        "next"):
                self.assertIn(key, out)
            state = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(set(state), {
                "filter_sha", "stream", "limit", "started_at", "finished_at", "coverity_total", "next_offset", "limited",
                "revision_field", "revision_checked", "analyzed_revision", "revision_error", "issues", "excluded_count",
                "excluded", "excluded_cids", "requests"})
            filter_bytes = (self.run_dir / "filter.json").read_bytes()
            self.assertEqual(state["filter_sha"], hashlib.sha256(filter_bytes).hexdigest()[:12])
            self.assertEqual((state["stream"], state["limit"], state["revision_field"]), (STREAM, 100, "sourceVersion"))
            self.assertTrue(state["finished_at"])
            first = state["issues"][0]
            self.assertEqual(first["merge_key"], "dd7cea15ef54c17c70b8c3657d010001")
            self.assertEqual(first["stream"], STREAM)
            self.assertNotIn("macro", first)  # fields without a value are left out
            self.assertIn("POST /api/v2/issues/search", [r["request"] for r in state["requests"]])
            self.assertFalse(list(self.run_dir.glob("*.tmp")))

        def test_resumes_when_done_is_false(self) -> None:
            output = self.run_dir / "issues.json"
            with mock.patch.object(self.tools.Deadline, "allows", lambda self, client, requests=1: False):
                out = self.search(filter_file=self.filter_file(), output_file=str(output))
            self.assertFalse(out["done"])
            self.assertTrue(out["next"])
            self.assertIsNone(json.loads(output.read_text(encoding="utf-8"))["finished_at"])
            out = self.search(filter_file=self.filter_file(), output_file=str(output))
            self.assertTrue(out["done"])
            self.assertEqual(out["found"], 2)

        def test_cids_are_kept_once_across_calls(self) -> None:
            self.cov.items = [issue(10001 + n) for n in range(30)]
            output = self.run_dir / "issues.json"
            calls = {"n": 0}
            make_client = self.tools.make_client

            def client(config):
                c = make_client(config)
                original = c.search

                def search(spec, limit, offset, seconds):
                    page = original(spec, min(limit, 20), offset, seconds)
                    calls["n"] += 1
                    if calls["n"] == 1:  # a new snapshot moved the rows: the next page starts 5 rows earlier
                        page.next_offset = 15
                    return page
                c.search = search
                return c

            with mock.patch.object(self.tools, "make_client", client):
                self.search(filter_file=self.filter_file(), output_file=str(output))
                out = self.search(filter_file=self.filter_file(), output_file=str(output))
            self.assertTrue(out["done"])
            cids = [i["cid"] for i in json.loads(output.read_text(encoding="utf-8"))["issues"]]
            self.assertEqual(len(cids), len(set(cids)))
            self.assertEqual(sorted(cids), [10001 + n for n in range(30)])

        def test_limit_of_the_filter_stops_and_marks_limited(self) -> None:
            self.cov.items = [issue(10001 + n) for n in range(30)]
            output = self.run_dir / "issues.json"
            out = self.search(filter_file=self.filter_file(limit=10), output_file=str(output))
            self.assertEqual((out["found"], out["limit"], out["limited"]), (10, 10, True))

        def test_without_total_rows_reads_until_empty(self) -> None:
            self.cov.omit_total = True
            self.cov.items = [issue(10001 + n, status="New" if 150 <= n < 161 else "Triaged") for n in range(230)]
            out = self.search(filter_file=self.filter_file(triage={"status": ["New"]}),
                              output_file=str(self.run_dir / "issues.json"))
            self.assertTrue(out["done"])
            self.assertEqual(out["found"], 11)
            self.assertIsNone(out["coverity_total"])
            self.assertEqual(out["excluded"], 219)
            state = json.loads((self.run_dir / "issues.json").read_text(encoding="utf-8"))
            self.assertEqual((len(state["excluded"]), len(state["excluded_cids"])), (50, 219))
            self.cov.items = [issue(10001 + n) for n in range(25)]
            first = self.tools.search_issues(repo_root=str(self.repo), filter={"streams": [STREAM]})
            self.assertEqual((len(first["issues"]), first["next_offset"], first["total"]), (20, 20, None))
            rest = self.tools.search_issues(repo_root=str(self.repo), filter={"streams": [STREAM]}, offset=20)
            self.assertEqual([i["cid"] for i in rest["issues"]], [10021 + n for n in range(5)])
            self.assertFalse(rest["has_more"])

        def test_revision_error_is_kept_and_search_still_usable(self) -> None:
            self.cov.soap_override = lambda request: (FakeCoverity.xml(500, cov.soap_fault("No snapshot", 1100))
                                                      if b"getSnapshotsForStream" in request.content else None)
            out = self.search(filter_file=self.filter_file(), output_file=str(self.run_dir / "issues.json"))
            self.assertTrue(out["done"])
            self.assertIn("No snapshot", out["revision_error"])
            self.assertIsNone(out["analyzed_revision"])

        def test_unknown_value_is_recorded(self) -> None:
            self.cov.unknown_keys = {"Tested Elsewhere"}
            output = self.run_dir / "issues.json"
            out = self.search(filter_file=self.filter_file(triage={"classification": ["Unclassified", "Tested Elsewhere"]}),
                              output_file=str(output))
            self.assertEqual((out["done"], out["found"]), (True, 2))
            self.assertEqual(out["ignored_values"], [{"filter": "triage.classification", "value": "Tested Elsewhere"}])
            state = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("ignored", [r["status"] for r in state["requests"]])

        def test_fatal_answers_stop_with_clear_messages(self) -> None:
            cases = [
                (cov.rest_401, ["認証に失敗"]),
                (cov.redirect_302, ["302", cov.LOGIN]),
                (cov.html_200, ["JSON", "text/html"]),
                (lambda: cov.json_response(403, {"code": 1003, "message": "Access denied"}), ["Access Web services"]),
            ]
            for answer, words in cases:
                with self.subTest(words=words):
                    self.cov.rest_override = lambda request, answer=answer: answer()
                    message = self.assertFailure(self.search, filter={"streams": [STREAM]})
                    for word in words:
                        self.assertIn(word, message)
            self.cov.rest_override = lambda request: (cov.json_response(500, {"code": 1004, "message": "Internal error"})
                                                      if request.url.path.endswith("/search") else None)
            self.assertIn("500", self.assertFailure(self.search, filter={"streams": [STREAM]}))

        def test_empty_columns_are_not_remembered(self) -> None:
            self.cov.columns_queue.append(lambda: cov.json_response(200, []))
            self.assertIn("cid", self.assertFailure(self.search, filter={"streams": [STREAM]}))
            self.assertEqual(len(self.search(filter={"streams": [STREAM]})["issues"]), 2)

        def test_output_folder_must_exist(self) -> None:
            message = self.assertFailure(self.search, filter_file=self.filter_file(),
                                         output_file=str(self.tmp / "nowhere" / "issues.json"))
            self.assertIn("フォルダがありません", message)


if __name__ == "__main__":
    unittest.main()
