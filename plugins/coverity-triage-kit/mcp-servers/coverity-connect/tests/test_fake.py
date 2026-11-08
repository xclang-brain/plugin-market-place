"""The fake Coverity (api: fake): issues from a YAML file, write-backs to <file>.writes.jsonl."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import MISSING, only_with_packages

load_tests = only_with_packages

FAKE = """\
snapshot:
  sourceVersion: 0f1e2d3
streams:
  app-release: {triage_store: Release Triage Store}
issues:
  - cid: 20001
    project: app
    stream: app-main
    checker: NULL_RETURNS
    file: src/reader.c
    function: read_header
    line: 42
    impact: High
    cwe: 476
    status: New
    classification: Unclassified
    action: Undecided
    checker_description: 戻り値が NULL のことがある関数の結果を、確かめずに使っている。
    events:
      - {file: src/reader.c, line: 40, tag: returned_null, description: "fgets が NULL を返すことがある。"}
      - {file: src/reader.c, line: 42, tag: dereference, description: "NULL かもしれない値を使う。", main: true}
  - cid: 20002
    project: app
    stream: app-main
    checker: MISRA C-2012 Rule 10.4
    file: src/calc.c
    function: add_offset
    line: 17
    impact: Low
    status: Triaged
    classification: Pending
    action: Undecided
    severity: Minor
    events:
      - {file: src/calc.c, line: 17, tag: misra_violation, description: "型の違う値を足している。", main: true}
  - cid: 20003
    project: app
    stream: app-release
    checker: RESOURCE_LEAK
    file: src/io.c
    function: open_log
    line: 88
    impact: High
    status: Fixed
    classification: Bug
    action: Fix Required
    events:
      - {file: src/io.c, line: 80, tag: alloc_fn, description: "fopen で開く。"}
      - {file: src/io.c, line: 88, tag: leaked_storage, description: "閉じずに戻る。", main: true}
"""

if not MISSING:
    from support import ServerCase

    class FakeModeTest(ServerCase):

        def setUp(self) -> None:
            super().setUp()
            (self.repo / ".coverity-triage" / "fake-issues.yaml").write_text(FAKE, encoding="utf-8")
            self.write_config("  url: https://coverity.example.com\n  api: fake\n  fake_data: fake-issues.yaml\n")
            self.writes = self.repo / ".coverity-triage" / "fake-issues.yaml.writes.jsonl"

        def search(self, **kwargs):
            return self.tools.search_issues(repo_root=str(self.repo), **kwargs)

        def test_check_connection_counts_issues_without_network(self) -> None:
            out = self.tools.check_connection(repo_root=str(self.repo))
            self.assertTrue(out["ok"])
            self.assertEqual(out["api"], "fake")
            self.assertIn("3 件", out["result"])
            self.assertEqual(out["fake_data"], str(self.repo / ".coverity-triage" / "fake-issues.yaml"))
            self.assertEqual(self.cov.requests, [])

        def test_search_filters_like_the_real_one(self) -> None:
            out = self.search(filter={"streams": ["app-main"], "checkers": ["MISRA C-2012 Rule 10.*"]})
            self.assertEqual([i["cid"] for i in out["issues"]], [20002])
            out = self.search(filter={"streams": ["app-main"], "triage": {"status": ["New"]}, "project": "app"})
            self.assertEqual([i["cid"] for i in out["issues"]], [20001])
            out = self.search(filter={"streams": ["app-main"], "project": "other"})
            self.assertEqual(out["count"], 0)
            out = self.search(filter={"streams": ["app-release"], "triage": {"status": ["Fixed"]}})
            self.assertEqual([i["cid"] for i in out["issues"]], [20003])
            out = self.search(filter={"streams": ["app-release"]})  # not in the latest snapshot
            self.assertEqual(out["count"], 0)

        def test_pages(self) -> None:
            first = self.search(filter={"streams": ["app-main"]}, limit=1)
            self.assertEqual((first["count"], first["total"], first["has_more"], first["next_offset"]), (1, 2, True, 1))
            rest = self.search(filter={"streams": ["app-main"]}, limit=1, offset=1)
            self.assertEqual(([i["cid"] for i in rest["issues"]], rest["has_more"]), ([20002], False))

        def test_details_check_the_stream(self) -> None:
            out = self.tools.get_issue_details(repo_root=str(self.repo), stream="app-main", cids=[20001, 20003])
            self.assertEqual([i["cid"] for i in out["issues"]], [20001])
            self.assertEqual(out["issues"][0]["events"][1], {"file": "src/reader.c", "line": 42, "tag": "dereference",
                                                             "description": "NULL かもしれない値を使う。", "main": True})
            self.assertIn("20003", out["failed"])
            self.assertIn("ほかの見られるストリーム（1 個）: app-release", out["failed_note"])

        def test_check_connection_lists_the_streams_of_the_file(self) -> None:
            out = self.tools.check_connection(repo_root=str(self.repo))
            self.assertEqual((out["streams"], out["streams_total"]), (["app-main", "app-release"], 2))
            out = self.tools.check_connection(repo_root=str(self.repo), response_format="detailed")
            self.assertEqual(out["streams"], [
                {"name": "app-main", "project": "app", "triage_store": "Default Triage Store"},
                {"name": "app-release", "project": "app", "triage_store": "Release Triage Store"}])

        def test_details_without_stream_find_the_stream(self) -> None:
            out = self.tools.get_issue_details(repo_root=str(self.repo), cids=[20003, 20001, 29999])
            self.assertEqual([(i["cid"], i["stream"]) for i in out["issues"]], [(20003, "app-release"), (20001, "app-main")])
            self.assertEqual(out["streams_searched"], 2)
            self.assertIn("app-main, app-release", out["failed"]["29999"])

        def test_a_cid_in_two_streams_names_the_other_one(self) -> None:
            path = self.repo / ".coverity-triage" / "fake-issues.yaml"
            path.write_text(FAKE + "  - {cid: 20001, stream: app-release, checker: NULL_RETURNS, file: src/reader.c,"
                                   " events: [{file: src/reader.c, line: 9, tag: dereference, description: d, main: true}]}\n",
                            encoding="utf-8")
            first = self.tools.get_issue_details(repo_root=str(self.repo), cids=[20001])["issues"][0]
            self.assertEqual((first["stream"], first["other_streams"], first["events"][-1]["line"]),
                             ("app-main", ["app-release"], 42))
            other = self.tools.get_issue_details(repo_root=str(self.repo), stream="app-release", cids=[20001])
            self.assertEqual(other["issues"][0]["events"][0]["line"], 9)
            self.assertNotIn("other_streams", other["issues"][0])

        def test_search_of_a_stream_that_is_not_there_says_so(self) -> None:
            message = self.assertFailure(self.search, filter={"streams": ["app-dev"]})
            self.assertIn("app-dev は、見られるストリームにありません", message)
            self.assertIn("app-main, app-release", message)

        def test_files_have_the_contract_shapes(self) -> None:
            issues = self.run_dir / "issues.json"
            out = self.search(filter_file=self.filter_file(streams=["app-main"]), output_file=str(issues))
            self.assertEqual((out["done"], out["found"], out["analyzed_revision"]), (True, 2, "0f1e2d3"))
            state = json.loads(issues.read_text(encoding="utf-8"))
            self.assertEqual(state["issues"][0]["checker"], "NULL_RETURNS")
            self.assertEqual(state["issues"][1]["severity"], "Minor")
            folder = self.run_dir / "20001"
            out = self.tools.get_issue_details(repo_root=str(self.repo), stream="app-main", cids=[20001],
                                               output_dir=str(folder))
            self.assertEqual(out["saved"], [20001])
            saved = json.loads((folder / "issue-20001.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["issue"]["impact"], "High")
            self.assertEqual(saved["events"][0]["main"], False)

        def test_writes_go_to_the_jsonl_file_once(self) -> None:
            entries = [{"id": "20001", "cids": [20001], "stream": "app-main", "classification": "False Positive",
                        "action": "Ignore", "severity": "Unspecified", "comment": "誤検知。"},
                       {"id": "20003", "cids": [20003], "stream": "app-release", "classification": "Bug",
                        "action": "Ignore", "severity": "Moderate", "comment": "本物の不具合。"},
                       {"id": "G9", "cids": [1], "stream": "nowhere", "classification": "Bug", "action": "Ignore"}]
            token = self.tools.preview_triage(entries=entries)["confirmation_token"]
            out = self.tools.update_triage(confirmation_token=token, repo_root=str(self.repo), entries=entries)
            self.assertEqual(out["written"], ["20001", "20003"])
            self.assertEqual([f["id"] for f in out["failed"]], ["G9"])
            lines = [json.loads(line) for line in self.writes.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(w["cids"], w["triage_store"]) for w in lines],
                             [([20001], "Default Triage Store"), ([20003], "Release Triage Store")])
            again = self.tools.update_triage(confirmation_token=token, repo_root=str(self.repo), entries=entries)
            self.assertEqual(again["written"], [])
            self.assertEqual(len(self.writes.read_text(encoding="utf-8").splitlines()), 2)

        def test_env_only_fake_mode(self) -> None:
            env = {"COVERITY_API": "fake", "COVERITY_FAKE_DATA": str(self.repo / ".coverity-triage" / "fake-issues.yaml")}
            with mock.patch.dict(os.environ, env):
                self.assertTrue(self.tools.check_connection()["ok"])
                self.assertEqual(self.tools.search_issues(filter={"streams": ["app-main"]})["count"], 2)

        def test_broken_fake_files_are_explained(self) -> None:
            path = self.repo / ".coverity-triage" / "fake-issues.yaml"
            path.write_text("issues:\n  - {cid: x}\n", encoding="utf-8")
            out = self.tools.check_connection(repo_root=str(self.repo))
            self.assertFalse(out["ok"])
            self.assertIn("1 番目", out["result"])
            path.unlink()
            self.assertIn("ありません", self.tools.check_connection(repo_root=str(self.repo))["result"])


if __name__ == "__main__":
    unittest.main()
