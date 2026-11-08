"""coverity_get_issue_details: warning paths in the result and in issue-<CID>.json (fake SOAP)."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import MISSING, only_with_packages

load_tests = only_with_packages

if not MISSING:
    import support as cov
    from support import STREAM, USER, FakeCoverity, ServerCase

    class DetailsInlineTest(ServerCase):

        def details(self, *cids, **kwargs):
            kwargs.setdefault("stream", STREAM)
            return self.tools.get_issue_details(repo_root=str(self.repo), cids=list(cids), **kwargs)

        def test_concise_events_include_nested_ones(self) -> None:
            out = self.details(10001, 10002)
            self.assertEqual([i["cid"] for i in out["issues"]], [10001, 10002])
            first = out["issues"][0]
            self.assertEqual((first["checker"], first["file"], first["line"], first["function"]),
                             ("USE_AFTER_FREE", "/builddir/build/BUILD/libev-4.33/ev_iouring.c", 261, "iouring_sqe_get"))
            self.assertEqual([e["line"] for e in first["events"]], [274, 280, 641, 261])  # nested ones too
            self.assertEqual(first["events"][1]["tag"], "freed_arg")
            self.assertEqual([e.get("main", False) for e in first["events"]], [False, False, False, True])
            self.assertIn("Reads target of a freed pointer", first["checker_description"])
            self.assertEqual(set(out["issues"][1]), {"cid", "stream", "checker", "file", "line", "function", "events",
                                                     "checker_description"})
            self.assertEqual((out["failed"], out["remaining"], out["stream"]), ({}, [], STREAM))
            self.assertNotIn("failed_note", out)
            self.assertEqual(self.cov.requests, ["POST /ws/v9/defectservice"] * 2)  # no stream list when all is found
            self.assertEqual(list(self.run_dir.iterdir()), [])

        def test_detailed_adds_impact_category_cwe(self) -> None:
            first = self.details(10001, response_format="detailed")["issues"][0]
            self.assertEqual((first["impact"], first["category"], first["cwe"], first["stream"]),
                             ("High", "Memory - illegal accesses", 416, STREAM))
            self.assertEqual([e["main"] for e in first["events"]], [False, False, False, True])

        def two_streams(self) -> None:
            """10001 in both streams, 10002 only in libev-release."""
            released = cov.use_after_free(10001)
            released["stream"] = "libev-release"
            only = cov.checked_return(10002)
            only["stream"] = "libev-release"
            self.cov.items = [cov.use_after_free(10001), released, only]

        def test_without_stream_every_visible_stream_is_searched(self) -> None:
            self.two_streams()
            for stream in (None, "", "  "):
                with self.subTest(stream=stream):
                    out = self.details(10001, 10002, stream=stream)
                    self.assertNotIn("stream", out)
                    self.assertEqual(out["streams_searched"], 2)
                    first, second = out["issues"]
                    self.assertEqual((first["cid"], first["stream"], first["other_streams"]),
                                     (10001, STREAM, ["libev-release"]))
                    self.assertEqual((second["cid"], second["stream"]), (10002, "libev-release"))
                    self.assertNotIn("other_streams", second)
                    self.assertEqual((out["failed"], out["remaining"]), ({}, []))
            # one stream list (kept for the next calls) and one warning-path request per CID
            self.assertEqual(self.cov.requests.count("GET /api/v2/streams"), 1)
            self.assertEqual(self.cov.requests.count("POST /ws/v9/defectservice"), 6)
            spec = next(n for n in self.cov.soap_requests[-1] if n.tag == "filterSpec")
            self.assertEqual([n.tag for n in spec][-2:], ["streamIdList", "streamIdList"])
            self.assertEqual([n[0].text for n in spec if n.tag == "streamIdList"], [STREAM, "libev-release"])

        def test_the_choice_does_not_depend_on_the_order_of_the_answer(self) -> None:
            self.two_streams()
            self.cov.reverse_returns = True
            first = self.details(10001, stream=None)["issues"][0]
            self.assertEqual((first["stream"], first["other_streams"]), (STREAM, ["libev-release"]))
            detailed = self.details(10001, stream=None, response_format="detailed")["issues"][0]
            self.assertEqual((detailed["stream"], detailed["cwe"]), (STREAM, 416))

        def test_cid_in_no_stream_fails_with_the_streams_searched(self) -> None:
            out = self.details(19999, 10001, stream=None)
            self.assertEqual([i["cid"] for i in out["issues"]], [10001])
            self.assertIn("見られる 2 個のストリーム（libev-main, libev-release）", out["failed"]["19999"])
            self.assertIn("CID が正しいか", out["failed"]["19999"])
            self.assertNotIn("failed_note", out)  # the message says it all

        def test_failed_note_names_the_other_streams(self) -> None:
            self.two_streams()
            out = self.details(10002)  # only in libev-release
            self.assertIn("10002", out["failed"])
            note = out["failed_note"]
            self.assertIn(f"ストリーム {STREAM} で取れませんでした", note)
            self.assertIn("libev-release", note)
            self.assertIn("stream を省いて", note)

        def test_a_stream_that_is_not_visible_is_named(self) -> None:
            out = self.details(10001, stream="libev-dev")
            self.assertIn("No stream found", out["failed"]["10001"])
            self.assertIn("libev-dev は、見られるストリームにありません", out["failed_note"])
            self.assertIn(f"{STREAM}, libev-release", out["failed_note"])

        def test_too_many_streams_are_not_searched(self) -> None:
            self.cov.streams = {f"s{n:03}": "Default Triage Store" for n in range(self.tools.MAX_LOOKUP_STREAMS + 1)}
            message = self.assertFailure(self.details, 10001, stream=None)
            self.assertIn(str(self.tools.MAX_LOOKUP_STREAMS + 1), message)
            self.assertIn("stream を渡して", message)
            self.assertNotIn("POST /ws/v9/defectservice", self.cov.requests)

        def test_a_failing_stream_list_stops_a_lookup_but_not_a_note(self) -> None:
            self.cov.rest_override = lambda request: (cov.json_response(500, {"code": 1004, "message": "Internal error"})
                                                      if request.url.path == "/api/v2/streams" else None)
            self.assertIn("500", self.assertFailure(self.details, 10001, stream=None))
            out = self.details(19999)  # with a stream: the note goes without the names
            self.assertIn("19999", out["failed"])
            self.assertNotIn("ほかの見られるストリーム", out["failed_note"])
            self.assertIn("stream を省いて", out["failed_note"])

        def test_without_visible_streams_the_message_says_why(self) -> None:
            self.cov.streams = {}
            self.assertIn("権限", self.assertFailure(self.details, 10001, stream=None))

        def test_up_to_10_cids_inline(self) -> None:
            message = self.assertFailure(self.details, *range(10001, 10012))
            self.assertIn("10", message)
            self.assertIn("output_dir", message)

        def test_cid_not_in_stream_fails_only_that_cid(self) -> None:
            out = self.details(10001, 19999)
            self.assertEqual([i["cid"] for i in out["issues"]], [10001])
            self.assertIn("19999", out["failed"])
            self.assertIn("ほかの見られるストリーム（1 個）: libev-release", out["failed_note"])

        def test_many_events_are_shortened_in_concise_form(self) -> None:
            item = cov.use_after_free(10001)
            item["events"] = [cov.event(f"step {n}", "PATH", "path", 100 + n) for n in range(99)] + [
                cov.event("use", "NORMAL", "use_after_free", 999, main=True)]
            self.cov.items = [item]
            first = self.details(10001)["issues"][0]
            self.assertEqual(len(first["events"]), self.tools.CONCISE_EVENTS)
            self.assertEqual(first["omitted_events"], 60)
            self.assertTrue(first["events"][-1]["main"])
            full = self.details(10001, response_format="detailed")["issues"][0]
            self.assertEqual(len(full["events"]), 100)

        def test_big_results_are_split_with_remaining(self) -> None:
            items = []
            for n in range(10):
                item = cov.use_after_free(10001 + n)
                item["events"] = [cov.event("x" * 300, "PATH", "path", 100 + k) for k in range(39)] + [
                    cov.event("use", "NORMAL", "use_after_free", 999, main=True)]
                items.append(item)
            self.cov.items = items
            out = self.details(*range(10001, 10011))
            self.assertTrue(out["remaining"])
            self.assertLess(len(json.dumps(out, ensure_ascii=False)), self.tools.CHARACTER_LIMIT)
            self.assertEqual([i["cid"] for i in out["issues"]] + out["remaining"], list(range(10001, 10011)))
            self.assertIn("remaining", out["next"])

        def test_soap_401_stops_and_saves_nothing(self) -> None:
            folder = self.run_dir / "items" / "10001"
            folder.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"COVTEST_KEY": "wrong-key"}):
                message = self.assertFailure(self.details, 10001, 10002, output_dir=str(folder))
            self.assertIn("認証", message)
            self.assertIn("User authentication failed.", message)
            self.assertEqual(list(folder.iterdir()), [])  # no error file that would stop a later fetch

        def test_permission_fault_stops_and_masks_the_user(self) -> None:
            self.cov.soap_override = lambda request: FakeCoverity.xml(
                500, cov.soap_fault(f"User {USER} Doesn't have permissions to perform {{invokeWS}}"))
            message = self.assertFailure(self.details, 10001)
            self.assertIn("invokeWS", message)
            self.assertNotIn(USER, message)
            self.assertIn("****", message)

        def test_other_fault_fails_only_that_cid_and_writes_error_file(self) -> None:
            folder = self.run_dir / "items" / "G1"
            folder.mkdir(parents=True)
            self.cov.soap_faults[10001] = FakeCoverity.xml(500, cov.soap_fault("Defect not found.", 1400))
            out = self.details(10001, 10002, output_dir=str(folder))
            self.assertTrue(out["done"])
            self.assertEqual(out["saved"], [10002])
            self.assertIn("Defect not found.", out["failed"]["10001"])
            saved = json.loads((folder / "issue-10001.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"cid", "stream", "fetched_at", "error"})

        def test_soap_redirect_and_html_are_clear(self) -> None:
            self.cov.soap_override = lambda request: cov.redirect_302()
            self.assertIn(cov.LOGIN, self.assertFailure(self.details, 10001))
            self.cov.soap_override = lambda request: cov.html_200()
            self.assertIn("XML", self.assertFailure(self.details, 10001))

        def test_filter_spec_in_xsd_order(self) -> None:
            self.details(10001)
            spec = next(n for n in self.cov.soap_requests[0] if n.tag == "filterSpec")
            self.assertEqual([n.tag for n in spec], ["includeDefectInstances", "includeHistory",
                                                     "includeTotalDefectInstanceCount", "maxDefectInstances",
                                                     "streamIdList"])
            values = {n.tag: (n.text or "") for n in spec}
            self.assertEqual(values["includeTotalDefectInstanceCount"], "false")
            self.assertGreaterEqual(int(values["maxDefectInstances"]), 1)

    class DetailsFileTest(ServerCase):

        def test_file_mode_resumes_and_keeps_saved_ones(self) -> None:
            (self.run_dir / "items").mkdir()
            folder = self.run_dir / "items" / "G1"  # made by the tool (its parent exists)

            def once(deadline, client, requests=1):
                used = getattr(deadline, "_used", False)
                deadline._used = True
                return not used

            with mock.patch.object(self.tools.Deadline, "allows", once):
                out = self.tools.get_issue_details(repo_root=str(self.repo), stream=STREAM, cids=[10001, 10002],
                                                   output_dir=str(folder))
            self.assertFalse(out["done"])
            self.assertEqual((out["saved"], out["remaining"]), ([10001], [10002]))
            out = self.tools.get_issue_details(repo_root=str(self.repo), stream=STREAM, cids=[10001, 10002, 10001],
                                               output_dir=str(folder))
            self.assertTrue(out["done"])
            self.assertEqual((out["already_saved"], out["saved"]), ([10001], [10002]))
            saved = json.loads((folder / "issue-10001.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"fetched_at", "issue", "events", "checker_description"})
            self.assertEqual((saved["issue"]["cid"], saved["issue"]["stream"]), (10001, STREAM))
            self.assertEqual(saved["events"][3], {"file": "/builddir/build/BUILD/libev-4.33/ev_iouring.c",
                                                  "line": 261, "tag": "use_after_free",
                                                  "description": 'Using freed pointer "loop->iouring_sq_ring".',
                                                  "main": True})

        def test_file_mode_without_stream_records_the_stream_found(self) -> None:
            item = cov.checked_return(10002)
            item["stream"] = "libev-release"
            self.cov.items = [cov.use_after_free(10001), item]
            folder = self.run_dir / "G2"
            out = self.tools.get_issue_details(repo_root=str(self.repo), cids=[10001, 10002, 19999],
                                               output_dir=str(folder))
            self.assertEqual((out["saved"], list(out["failed"]), out["streams_searched"]), ([10001, 10002], ["19999"], 2))
            saved = json.loads((folder / "issue-10002.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["issue"]["stream"], "libev-release")
            failed = json.loads((folder / "issue-19999.json").read_text(encoding="utf-8"))
            self.assertEqual((set(failed), failed["stream"]), ({"cid", "stream", "fetched_at", "error"}, None))
            again = self.tools.get_issue_details(repo_root=str(self.repo), cids=[10001, 10002], output_dir=str(folder))
            self.assertEqual((again["already_saved"], again["saved"]), ([10001, 10002], []))
            self.assertNotIn("streams_searched", again)  # nothing left to look for: no stream list either

        def test_up_to_50_cids_and_parent_must_exist(self) -> None:
            message = self.assertFailure(self.tools.get_issue_details, repo_root=str(self.repo), stream=STREAM,
                                         cids=list(range(1, 52)), output_dir=str(self.run_dir))
            self.assertIn("50", message)
            message = self.assertFailure(self.tools.get_issue_details, repo_root=str(self.repo), stream=STREAM,
                                         cids=[10001], output_dir=str(self.tmp / "no" / "such" / "dir"))
            self.assertIn("フォルダがありません", message)


if __name__ == "__main__":
    unittest.main()
