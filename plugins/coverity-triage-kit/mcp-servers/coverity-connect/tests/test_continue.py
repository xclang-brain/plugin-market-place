"""``done: false``: a call that does not fit in its 20 seconds is continued by the same call, and what is
already saved is not fetched or written again.

The server has no switch for its time budget, so the emulator is slow instead: a request that takes more
than half of the budget is the last one of a call. The three slow first calls run at the same time.
"""

from __future__ import annotations

import json
import unittest

from support import ServerCase, warning

SLOW = 10.5  # seconds; the server expects the next request to take as long, which no longer fits
BIG = [warning(cid, "NULL_RETURNS", stream="big-main") for cid in range(1001, 1451)]  # more than two pages
OTHER = [warning(cid, "RESOURCE_LEAK", stream="other-main") for cid in (2001, 2002, 2003)]
ENTRY = {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
         "comment": "誤検知。呼び出し元が引数を検証してから呼び出す。"}


class ContinueTest(ServerCase):
    WARNINGS = BIG + OTHER

    def test_a_call_that_does_not_fit_is_continued_by_the_same_call(self) -> None:
        repo = self.repo(stream="big-main", limit=300)
        issues, folder, written_file = self.run_dir / "issues.json", self.run_dir / "cid", self.run_dir / "written.json"
        calls = {
            "search": ("coverity_search_issues", {"output_file": issues.as_posix()}),
            "details": ("coverity_get_issue_details", {"cids": [1001, 1002, 1003, 1004], "output_dir": folder.as_posix()}),
            "write": ("coverity_update_triage", {"entries_file": self.entries(
                [{"cids": [2001], **ENTRY}, {"cids": [2002, 2003], **ENTRY}], stream="other-main")})}
        self.emu.delay = {"/issues/search": SLOW, "/ws/v9/defectservice": SLOW, "/issues/triage": SLOW}
        started = {name: self.server.start(tool, repo_root=repo, **arguments) for name, (tool, arguments) in calls.items()}
        first = {name: self.server.wait(ident)["structuredContent"] for name, ident in started.items()}
        self.emu.delay = {}

        def again(name: str) -> dict:
            return self.ok(calls[name][0], repo_root=repo, **calls[name][1])

        with self.subTest("search: the next page"):
            self.assertEqual(first["search"], {"done": False, "count": 200, "total_matched": None, "limited": False,
                                               "output_file": issues.as_posix()})
            self.assertFalse(issues.exists())  # the list appears only when it is complete
            self.assertEqual(again("search"), {"done": True, "count": 300, "total_matched": 450, "limited": True,
                                               "output_file": issues.as_posix()})
            saved = json.loads(issues.read_text(encoding="utf-8"))
            self.assertEqual([issue["cid"] for issue in saved["issues"]], list(range(1001, 1301)))
            self.assertEqual((saved["total_matched"], saved["limited"], saved["conditions"]["limit"]), (450, True, 300))
            offsets = [request.split("offset=")[1].split("&")[0] for request in self.requests("/issues/search")]
            self.assertEqual(offsets, ["0", "200"])  # no page was read twice
            # Without a file the reply holds at most 100 warnings, in one call.
            inline = self.ok("coverity_search_issues", repo_root=repo)
            self.assertEqual((inline["done"], inline["count"], inline["total_matched"], inline["limited"]),
                             (True, 100, 450, True))

        with self.subTest("details: the CIDs that are left"):
            self.assertEqual(first["details"], {"done": False, "saved": [1001], "failed": {},
                                                "remaining": [1002, 1003, 1004], "output_dir": folder.as_posix()})
            self.assertEqual([path.name for path in folder.iterdir()], ["1001"])
            self.assertEqual(again("details"), {"done": True, "saved": [1001, 1002, 1003, 1004], "failed": {},
                                                "remaining": [], "output_dir": folder.as_posix()})
            self.assertEqual(len(self.requests("/ws/v9/defectservice")), 4)  # CID 1001 was not fetched again

        with self.subTest("write: the entries that are left"):
            self.assertEqual(first["write"], {"done": False, "written": [2001], "skipped": [], "failed": {},
                                              "remaining": [2002, 2003], "written_file": written_file.as_posix()})
            self.assertEqual(sorted(json.loads(written_file.read_text(encoding="utf-8"))["written"]), ["2001"])
            self.assertEqual(again("write"), {"done": True, "written": [2002, 2003], "skipped": [2001], "failed": {},
                                              "remaining": [], "written_file": written_file.as_posix()})
            self.assertEqual(sorted(json.loads(written_file.read_text(encoding="utf-8"))["written"]),
                             ["2001", "2002", "2003"])
            self.assertEqual([write["cids"] for write in self.emu.writes], [[2001], [2002, 2003]])  # each entry once


if __name__ == "__main__":
    unittest.main()
