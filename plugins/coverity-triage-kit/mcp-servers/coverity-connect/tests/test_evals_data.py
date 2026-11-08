"""The answers of evals/evaluation.xml, found again through the tools on evals/data (fake mode),
the way an agent would: page through the searches and read the warning paths. When the data or a
tool changes, this test says which answer no longer holds."""

from __future__ import annotations

import os
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any
from unittest import mock

from support import MISSING, PART, only_with_packages

load_tests = only_with_packages

DATA = PART / "evals" / "data" / ".coverity-triage"
XML = PART / "evals" / "evaluation.xml"


class EvalDataTest(unittest.TestCase):

    def setUp(self) -> None:
        from coverity_mcp import tools

        self.tools = tools
        env = {"COVERITY_API": "fake", "COVERITY_FAKE_DATA": str(DATA / "fake-issues.yaml")}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)

    def issues(self, stream: str, **filter_values: Any) -> list[dict[str, Any]]:
        found, offset = [], 0
        while True:
            page = self.tools.search_issues(filter={"streams": [stream], **filter_values}, offset=offset, limit=10,
                                            response_format="detailed")
            found += page["issues"]
            if not page["has_more"]:
                return found
            offset = page["next_offset"]

    def details(self, stream: str, cids: list[int]) -> list[dict[str, Any]]:
        out = []
        for start in range(0, len(cids), 10):
            page = self.tools.get_issue_details(stream=stream, cids=cids[start:start + 10], response_format="detailed")
            self.assertEqual((page["failed"], page["remaining"]), ({}, []))
            out += page["issues"]
        return out

    def answers(self) -> list[str]:
        main, release = self.issues("app-main"), self.issues("app-release")
        out = []
        # 1. High and Unclassified in app-main: the most events
        wanted = [i["cid"] for i in self.issues("app-main", impacts=["High"], triage={"classification": ["Unclassified"]})]
        counts = {d["cid"]: len(d["events"]) for d in self.details("app-main", wanted)}
        best = max(counts.values())
        self.assertEqual(sum(1 for v in counts.values() if v == best), 1, "question 1 needs one answer")
        out.append(str(next(c for c, v in counts.items() if v == best)))
        # 2. in both streams, a different classification
        by_main = {i["cid"]: i["classification"] for i in main}
        out.append(str(sum(1 for i in release if i["cid"] in by_main and by_main[i["cid"]] != i["classification"])))
        # 3. MISRA 10.4 under src/core/
        misra = [i for i in self.issues("app-main", checkers=["MISRA C-2012 Rule 10.4"]) if i["file"].startswith("src/core/")]
        self.assertEqual(len(misra), 1)
        out.append(misra[0]["function"])
        # 4. Fixed in app-main
        fixed = self.issues("app-main", triage={"status": ["Fixed"]})
        self.assertEqual(len(fixed), 1)
        out.append(fixed[0]["function"])
        # 5. only in app-release, High
        in_main = {i["cid"] for i in self.issues("app-main", triage={"status": ["New", "Triaged", "Dismissed", "Fixed",
                                                                                 "Absent Dismissed"]})}
        out.append(str(sum(1 for i in release if i["cid"] not in in_main and i["impact"] == "High")))
        # 6. USE_AFTER_FREE in app-main: the line that frees
        uaf = self.issues("app-main", checkers=["USE_AFTER_FREE"])
        self.assertEqual(len(uaf), 1)
        events = self.details("app-main", [uaf[0]["cid"]])[0]["events"]
        out.append(str(next(e["line"] for e in events if "frees" in e["description"])))
        # 7. Fix Required in app-main: the largest CWE
        fix = [i for i in self.issues("app-main", triage={"action": ["Fix Required"]}) if i.get("cwe")]
        out.append(max(fix, key=lambda i: i["cwe"])["checker"])
        # 8. the file with the most issues in app-main
        files = Counter(i["file"] for i in main).most_common()
        self.assertGreater(files[0][1], files[1][1], "question 8 needs one answer")
        out.append(files[0][0])
        # 9. CID 31026: the tag of the event before the main one (find its stream first)
        stream = next(s for s, issues in (("app-main", main), ("app-release", release))
                      if any(i["cid"] == 31026 for i in issues))
        events = self.details(stream, [31026])[0]["events"]
        index = next(n for n, e in enumerate(events) if e["main"])
        out.append(events[index - 1]["tag"])
        # 10. a warning path that reaches another file
        cross = [d["cid"] for d in self.details("app-main", [i["cid"] for i in main])
                 if any(e["file"] != d["file"] for e in d["events"])]
        self.assertEqual(len(cross), 1, "question 10 needs one answer")
        out.append(str(cross[0]))
        return out

    def test_answers_of_the_evaluation_hold(self) -> None:
        pairs = ET.parse(XML).getroot().findall("qa_pair")
        self.assertEqual(len(pairs), 10)
        expected = [p.findtext("answer").strip() for p in pairs]
        self.assertEqual(self.answers(), expected)

    def test_questions_stand_alone(self) -> None:
        for pair in ET.parse(XML).getroot().findall("qa_pair"):
            question = pair.findtext("question")
            self.assertTrue(question and pair.findtext("answer"))
            for word in ("repo_root", "\\", "coverity_"):  # no path or tool name to copy: the agent finds its way
                self.assertNotIn(word, question)


if __name__ == "__main__" and not MISSING:
    unittest.main()
