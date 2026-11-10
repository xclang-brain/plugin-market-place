"""A triage as agents drive it, on a git and on an svn copy of the sample (tests/fixtures/sample).

No model: flowlib.Agent is a scripted agent. It begins with the one command line an agent takes from
SKILL.md and then runs what the replies say, so the command line of triage.py is all this test knows of
the skill. What one command does alone is tested in skills/coverity-triage/tests. The svn half is skipped
only where the svn command is not installed; in CI that is a failure.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import flowlib
from flowlib import CIDS, Agent


def handed(cid: int):
    """Stop at the reply of next that hands out this CID."""
    return lambda reply: reply.get("cid") == cid and "work_dir" in reply


class Case(unittest.TestCase):
    kind = "git"

    def setUp(self):
        if self.kind == "svn" and not (shutil.which("svn") and shutil.which("svnadmin")):
            if os.environ.get("CI"):
                self.fail("svn コマンドがありません。CI では svn の流れも実行します")
            self.skipTest("svn コマンドがありません")
        self.top = Path(tempfile.mkdtemp(prefix="triage-flow-")).resolve()
        self.addCleanup(flowlib.remove_tree, self.top)
        self.repo = flowlib.make_repo(self.kind, self.top)
        self.before = flowlib.user_tree(self.kind, self.repo)

    def request(self, *flags) -> str:
        """The first command line of a request."""
        return flowlib.command("start", "--repo", f'"{self.repo.as_posix()}"', *flags)

    def read(self, run_dir, cid: int, name: str) -> bytes:
        return (Path(run_dir) / "cid" / str(cid) / name).read_bytes()

    def state(self, run_dir, cid: int) -> dict:
        return json.loads(self.read(run_dir, cid, "state.json"))

    def assert_repository_untouched(self):
        self.assertEqual(flowlib.user_tree(self.kind, self.repo), self.before, "the person's repository has changed")


class Flow:
    """The scenarios; GitFlow and SvnFlow below run them."""

    def test_stage_2_one_agent_processes_every_cid_to_the_end(self):
        agent = Agent(self.repo)
        given = agent.follow(self.request("--slots", 1), stop=handed(CIDS[-1]))
        self.assertEqual(agent.calls, ["coverity_search_issues", "coverity_get_issue_details"])
        run = Path(given["run_dir"])
        self.assertEqual(run.parent, self.top / "coverity-triage-out", "the result folder is outside the repository")
        early = agent.run(flowlib.command("finish", "--run", f'"{run.as_posix()}"'))  # the last CID is not saved yet
        self.assertEqual((early["ok"], early.get("pending")), (False, CIDS[-1:]))
        self.assertIn("未処置の CID が 1 件あります", early["error"])
        self.assertFalse((run / "summary.md").exists())

        done = agent.follow(agent.act(given))
        self.assertEqual(agent.worked, CIDS, "every CID once, in the order of the list")
        self.assertEqual((done["counts"]["done"], done["counts"]["remaining"]), (6, 0))
        summary = Path(done["summary_file"]).read_text(encoding="utf-8")
        self.assertIn(done["table"], summary)
        for cid in CIDS:  # both outputs for every CID: in its section and in its row of the table
            result = json.loads(self.read(run, cid, "result.json"))
            comment = result["deviation"]["comment"]
            section = summary.split(f"## CID {cid}（")[1].split("## CID ")[0]
            row = next(line for line in done["table"].splitlines() if line.startswith(f"| {cid} |"))
            self.assertIn("### 逸脱コメント\n\n" + comment, section)
            if result["recommendation"] == "fix":
                self.assertIn(f"### 修正コード\n\n{result['fix']['summary']}\n\n````diff\n", section)
                for line in flowlib.added_lines(cid):  # the fix is in the patch, and the patch is shown
                    self.assertIn(b"+" + line, self.read(run, cid, "fix.patch"))
                    self.assertIn("+" + line.decode("utf-8").rstrip(), section)
                self.assertTrue(row.endswith(f"| 修正を取り込む | 高 | src/reader.c | {comment} |"), row)
                self.assertEqual(result["deviation"]["classification"], "Bug")
            else:
                self.assertFalse((run / "cid" / str(cid) / "fix.patch").exists())
                self.assertIn("### 修正コード\n\n修正コードはありません。理由: ", section)
                self.assertTrue(row.endswith(f"| 逸脱コメントを登録する | 高 | なし | {comment} |"), row)
        fixes = [run / "cid" / str(cid) / "fix.patch" for cid in (20001, 20003, 20004)]
        fixed = (flowlib.fresh_copy(self.kind, self.top, fixes) / "src" / "reader.c").read_bytes()
        self.assertTrue(all(flowlib.FIXES[cid][1] in fixed for cid in (20001, 20003, 20004)), "the patches apply")
        self.assertFalse((run / "work").exists(), "the work copies are removed")
        self.assert_repository_untouched()

    def test_an_interrupted_run_continues_with_the_same_request(self):
        first, third = Agent(self.repo), CIDS[2]
        given = first.follow(self.request("--slots", 1), stop=handed(third))
        first.edit(given)  # the session ends here: the third CID is claimed and half done, nothing is saved

        second = Agent(self.repo)  # a new session has the request only
        done = second.follow(self.request("--slots", 1))
        resumed, again = second.replies[:2]
        self.assertEqual(resumed["run_dir"], given["run_dir"])
        self.assertEqual(resumed["message"], "前回の続きから進めます（残り 4 件）")
        self.assertEqual(second.calls, [], "the list is not fetched again")
        self.assertEqual((again["cid"], again["work_dir"]), (third, given["work_dir"]))
        self.assertIn("もう一度処置します", again["message"])
        self.assertEqual((first.worked, second.worked), (CIDS[:2], CIDS[2:]), "no CID twice; the abandoned one again")
        self.assertEqual((done["counts"]["done"], done["counts"]["failed"]), (6, 0))
        self.assertEqual(self.read(done["run_dir"], third, "fix.patch").count(b"+    if (buf == NULL) {"), 1,
                         "the half-done edit of the first session is not part of the fix")

    def test_no_warning_for_the_conditions_ends_the_request(self):
        agent = Agent(self.repo)
        agent.warnings = {}  # everything is triaged: Coverity has no warning for the conditions
        for flags in ((), ("--slots", 1), ("--review",)):  # asked in a chat, of one agent, of the orchestrator
            done = agent.follow(self.request(*flags))
            self.assertEqual((done["message"], done["next"]), ("条件に合う警告はありません", []))
        out = self.top / "coverity-triage-out"
        self.assertEqual((agent.calls, len(list(out.iterdir()))), (["coverity_search_issues"], 1), "fetched once")
        flowlib.wait_for_copies(done["run_dir"])
        long_ago = time.time() - 11 * 60
        os.utime(Path(done["run_dir"]) / "issues.json", (long_ago, long_ago))
        later = agent.follow(self.request())  # the same request later: Coverity is asked again
        self.assertEqual((later["message"], later["next"]), ("条件に合う警告はありません", []))
        self.assertEqual((agent.calls, len(list(out.iterdir()))), (["coverity_search_issues"] * 2, 2))
        flowlib.wait_for_copies(later["run_dir"])
        self.assert_repository_untouched()

    def work_round(self, lines: list[str]) -> None:
        """One round of stage 3: one worker per line of next, taking turns on the one queue. Each repeats
        next and save until next answers that the worker has nothing more to do."""
        workers = {line: Agent(self.repo) for line in lines}
        while workers:
            for line, worker in list(workers.items()):
                given = worker.run(line)
                if given["cid"] is None:
                    self.assertEqual(given["next"], [], "the worker reports to the agent that sent it")
                    del workers[line]
                else:
                    saved = worker.run(worker.act(given))
                    self.assertEqual((saved["ok"], saved["next"]), (True, [line]), saved)

    def review_round(self, lead: Agent, run: Path, rework: dict) -> list[int]:
        """The reviewers of stage 3: every batch of status, one result after the other. Each looks at the
        result, the patch and its clean copy, and records the outcome with the review line of status."""
        status = lead.run(flowlib.command("status", "--run", f'"{run.as_posix()}"'))
        line, reviewed = status["next"][0].split(" [")[0], []
        for batch in status["review_batches"]:
            clean = (Path(batch["work_dir"]) / "src" / "reader.c").read_bytes()
            self.assertEqual(clean, (self.repo / "src" / "reader.c").read_bytes(), "a reviewer reads a clean copy")
            for cid in batch["cids"]:
                self.assertIn("deviation", json.loads(self.read(run, cid, "result.json")))
                point = rework.pop(cid, None)
                outcome = f'rework --point "{point}"' if point else "pass"
                done = Agent(self.repo).run(line.replace("<CID>", str(cid)).replace("<pass|flag|rework>", outcome))
                self.assertTrue(done["ok"], done)
                reviewed.append(cid)
        return reviewed

    def test_stage_3_workers_in_parallel_one_cause_merged_every_result_reviewed(self):
        lead = Agent(self.repo)  # the orchestrator: start with review, slots as the settings say (3)
        ready = lead.follow(self.request("--review"), stop=lambda reply: "counts" in reply)
        run, lines = Path(ready["run_dir"]), ready["next"]
        self.assertEqual([line.split(" next ")[1] for line in lines],
                         [f'--run "{run.as_posix()}" --slot {k} --batch 1' for k in (1, 2, 3)])
        lead.act(ready)  # the warning paths of all CIDs, once
        self.assertEqual(lead.calls, ["coverity_search_issues", "coverity_get_issue_details"])
        status_line = flowlib.command("status", "--run", f'"{run.as_posix()}"')

        killed = Agent(self.repo)  # the worker of slot 2 takes a CID, edits, and is gone
        lost = killed.run(lines[1])
        killed.edit(lost)
        self.work_round([lines[0], lines[2]])
        status = lead.run(status_line)
        self.assertEqual((status["counts"]["done"], status["counts"]["working"]), (5, 1))
        self.work_round(status["next"])  # asked again while CIDs are left: slot 2 gets its CID once more
        self.assertEqual((self.state(run, lost["cid"])["status"], self.state(run, lost["cid"])["tries"]), ("done", 2))
        self.assertEqual({self.state(run, cid)["slot"] for cid in CIDS}, {1, 2, 3}, "three slots, one queue")

        status = lead.run(status_line)  # the integrator: three CIDs have one cause, no fix covers all three
        self.assertEqual(status["merge_candidates"],
                         [{"cids": [20004, 20005, 20006], "reason": "同じチェッカーと関数"}])
        merge = status["merge_command"].replace("<CID>", "20004").replace("<CID,CID>", "20005,20006")
        merged = lead.run(merge.replace("[--rework]", '--rework --point "src/reader.c:58 同じ変換が3行にある"'))
        self.assertTrue(merged["ok"], merged)
        self.work_round(lead.run(status_line)["next"])  # the leader goes back to a worker: one fix for all
        for cid in (20004, 20005, 20006):
            self.assertIn(b"+" + flowlib.FIXES[cid][1], self.read(run, 20004, "fix.patch"))

        early = lead.run(flowlib.command("finish", "--run", f'"{run.as_posix()}"'))
        self.assertEqual((early["ok"], early.get("unreviewed")), (False, [20001, 20002, 20003, 20004]))
        point = "src/reader.c:20 fclose の戻り値を使用しないことを明示する"
        self.assertEqual(sorted(self.review_round(lead, run, {20001: point})), [20001, 20002, 20003, 20004])
        self.work_round(lead.run(status_line)["next"])  # sent back once, with the earlier fix in the copy
        self.assertIn(b"+        (void)fclose(fp);", self.read(run, 20001, "fix.patch"))
        self.assertEqual(self.review_round(lead, run, {}), [20001], "the new result is reviewed again")

        done = lead.follow(status_line)  # nothing is left: status leads to finish
        self.assertEqual((done["counts"]["done"], done["counts"]["remaining"]), (6, 0))
        rows = done["table"].splitlines()[2:]
        self.assertEqual([row.split(" | ")[0] for row in rows],
                         ["| 20001", "| 20002", "| 20003", "| 20004（同じ原因: 20005, 20006）"], "one row per cause")
        self.assertTrue(all(" | 高 | 合格 | " in row for row in rows), rows)
        self.assertFalse((run / "work").exists())
        self.assert_repository_untouched()


class GitFlow(Flow, Case):
    kind = "git"


class SvnFlow(Flow, Case):
    kind = "svn"


if __name__ == "__main__":
    unittest.main()
