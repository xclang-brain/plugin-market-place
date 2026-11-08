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
            self.assertEqual(set(out["issues"][1]), {"cid", "checker", "file", "line", "function", "events",
                                                     "checker_description"})
            self.assertEqual((out["failed"], out["remaining"], out["stream"]), ({}, [], STREAM))
            self.assertEqual(list(self.run_dir.iterdir()), [])

        def test_detailed_adds_impact_category_cwe(self) -> None:
            first = self.details(10001, response_format="detailed")["issues"][0]
            self.assertEqual((first["impact"], first["category"], first["cwe"], first["stream"]),
                             ("High", "Memory - illegal accesses", 416, STREAM))
            self.assertEqual([e["main"] for e in first["events"]], [False, False, False, True])

        def test_stream_is_required_with_a_clear_message(self) -> None:
            for stream in (None, "", "  "):
                message = self.assertFailure(self.tools.get_issue_details, repo_root=str(self.repo), stream=stream,
                                             cids=[10001])
                self.assertEqual(message, "stream を渡してください。警告の検索の結果の各警告に stream があります")

        def test_up_to_10_cids_inline(self) -> None:
            message = self.assertFailure(self.details, *range(10001, 10012))
            self.assertIn("10", message)
            self.assertIn("output_dir", message)

        def test_cid_not_in_stream_fails_only_that_cid(self) -> None:
            out = self.details(10001, 19999)
            self.assertEqual([i["cid"] for i in out["issues"]], [10001])
            self.assertIn("19999", out["failed"])

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

        def test_up_to_50_cids_and_parent_must_exist(self) -> None:
            message = self.assertFailure(self.tools.get_issue_details, repo_root=str(self.repo), stream=STREAM,
                                         cids=list(range(1, 52)), output_dir=str(self.run_dir))
            self.assertIn("50", message)
            message = self.assertFailure(self.tools.get_issue_details, repo_root=str(self.repo), stream=STREAM,
                                         cids=[10001], output_dir=str(self.tmp / "no" / "such" / "dir"))
            self.assertIn("フォルダがありません", message)


if __name__ == "__main__":
    unittest.main()
