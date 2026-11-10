"""A triage as an agent drives it, on a git and on an svn copy of the sample (tests/fixtures/sample).

No model: flowlib.Agent is a scripted agent. It begins with the one command line an agent takes from
SKILL.md and then runs what the replies say, so the command line of triage.py is all this test knows of
the skill. The svn half is skipped only where the svn command is not installed; in CI that is a failure.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # nothing is left in the repository
import flowlib  # noqa: E402
from flowlib import CIDS, Agent  # noqa: E402


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

    def chat(self, agent: Agent, cid: int) -> dict:
        """Stage 1 up to 「CID <cid> を処置して」: fetch the list, then the reply of next for the named CID."""
        ready = agent.follow(self.request(), stop=lambda reply: reply.get("need_fetch") is False)
        return agent.run(agent.act(ready) + f" --cid {cid}")

    def read(self, run_dir, cid: int, name: str) -> bytes:
        return (Path(run_dir) / "cid" / str(cid) / name).read_bytes()

    def assert_repository_untouched(self):
        self.assertEqual(flowlib.user_tree(self.kind, self.repo), self.before, "the person's repository has changed")


class Flow:
    """The scenarios; GitFlow and SvnFlow below run them."""

    def test_stage_1_one_named_cid_in_a_chat(self):
        agent = Agent(self.repo)
        given = self.chat(agent, 20003)
        self.assertEqual(agent.calls, ["coverity_search_issues", "coverity_get_issue_details"])
        self.assertEqual((given["cid"], given["issue"]["file"]), (20003, "src/reader.c"))
        saved = agent.run(agent.act(given))
        self.assertTrue(saved["ok"], saved)
        run = Path(saved["run_dir"])
        self.assertEqual(run.parent, self.top / "coverity-triage-out", "the result folder is outside the repository")
        result = json.loads(self.read(run, 20003, "result.json"))
        self.assertEqual((result["recommendation"], result["deviation"]["classification"]), ("fix", "Bug"))
        self.assertTrue(result["deviation"]["comment"].startswith("本物の不具合。src/reader.c:49 "))
        self.assertEqual(Path(saved["patch_file"]), run / "cid" / "20003" / "fix.patch")
        self.assertIn(b"+    if (buf == NULL) {", self.read(run, 20003, "fix.patch"))
        self.assertEqual((saved["counts"]["done"], saved["counts"]["remaining"]), (1, 5))
        self.assert_repository_untouched()

    def test_stage_2_one_agent_processes_every_cid_to_the_end(self):
        agent = Agent(self.repo)
        given = agent.follow(self.request("--slots", 1), stop=handed(CIDS[-1]))
        run = Path(given["run_dir"])
        early = agent.run(flowlib.command("finish", "--run", f'"{run.as_posix()}"'))  # the last CID is not saved yet
        self.assertEqual((early["ok"], early.get("pending")), (False, CIDS[-1:]))
        self.assertIn("未処置の CID が 1 件あります", early["error"])
        self.assertFalse((run / "summary.md").exists())

        done = agent.follow(agent.act(given))
        self.assertEqual(agent.worked, CIDS, "every CID once, in the order of the list")
        self.assertEqual(done["counts"], {"total": 6, "pending": 0, "working": 0, "done": 6, "failed": 0, "remaining": 0})
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
                self.assertTrue(row.endswith(f"| 修正を取り込む | 高 | - | - | src/reader.c | {comment} |"), row)
            else:
                self.assertFalse((run / "cid" / str(cid) / "fix.patch").exists())
                self.assertIn("### 修正コード\n\n修正コードはありません。理由: ", section)
                self.assertTrue(row.endswith(f"| 逸脱コメントを登録する | 高 | - | - | なし | {comment} |"), row)
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
        self.assertIn("前回の続きから進めます（残り 4 件）", resumed["message"])
        self.assertEqual(second.calls, [], "the list is not fetched again")
        self.assertEqual((again["cid"], again["work_dir"]), (third, given["work_dir"]))
        self.assertIn("もう一度処置します", again["message"])
        self.assertEqual((first.worked, second.worked), (CIDS[:2], CIDS[2:]), "no CID twice; the abandoned one again")
        self.assertEqual((done["counts"]["done"], done["counts"]["failed"]), (6, 0))
        self.assertEqual(self.read(done["run_dir"], third, "fix.patch").count(b"+    if (buf == NULL) {"), 1,
                         "the half-done edit of the first session is not part of the fix")

    def test_fixes_in_crlf_and_shift_jis_files_apply_to_a_fresh_copy(self):
        agent = Agent(self.repo, flowlib.ENCODED)
        done = agent.follow(self.request("--slots", 1))
        self.assertEqual(agent.worked, list(flowlib.ENCODED))
        patches = [Path(done["run_dir"]) / "cid" / str(cid) / "fix.patch" for cid in flowlib.ENCODED]
        copy = flowlib.fresh_copy(self.kind, self.top, patches)
        for (cid, warning), patch in zip(flowlib.ENCODED.items(), patches):
            name = warning["file"]
            for line in flowlib.added_lines(cid):  # the patch has the bytes of the file: cp932, CRLF
                self.assertIn(b"+" + line, patch.read_bytes())
            self.assertEqual((copy / name).read_bytes(), flowlib.ENCODED_FILES[name].replace(*flowlib.FIXES[cid]), name)
            self.assertEqual((self.repo / name).read_bytes(), flowlib.ENCODED_FILES[name])
        self.assertIn("+    /* 未設定のときは 0 を返す */", Path(done["summary_file"]).read_text(encoding="utf-8"))
        self.assert_repository_untouched()

    def test_a_result_without_the_deviation_comment_is_refused(self):
        agent = Agent(self.repo)
        given = self.chat(agent, 20001)
        agent.edit(given)  # the fix is made, but the second output is missing
        bad = agent.result(given)
        del bad["deviation"]["comment"]
        flowlib.write_json(Path(given["result_file"]), bad)
        refused = agent.run(given["next"][0])
        self.assertFalse(refused["ok"])
        self.assertIn("deviation.comment: 逸脱コメントが空です", "\n".join(refused["problems"]))
        self.assertIn("problems を修正してから", refused["error"])
        self.assertEqual(refused["next"], given["next"], "the same save line again")
        self.assertFalse((Path(given["run_dir"]) / "cid" / "20001" / "fix.patch").exists())

        flowlib.write_json(Path(given["result_file"]), agent.result(given))  # corrected; the fix is still in the copy
        saved = agent.run(refused["next"][0])
        self.assertTrue(saved["ok"], saved)
        self.assertIn(b"+        fclose(fp);", self.read(saved["run_dir"], 20001, "fix.patch"))


class GitFlow(Flow, Case):
    kind = "git"


class SvnFlow(Flow, Case):
    kind = "svn"


if __name__ == "__main__":
    unittest.main()
