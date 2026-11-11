"""The four stages as a person uses them (README.md, docs/guide), on git and on svn. No model.

Every real thing takes part: the MCP server that mcp.json registers, against a Coverity Connect emulator;
the command lines of setup.py, triage.py, apply.py and gui.py as the replies give them; a git repository
with a bare origin and an svn working copy, in folders whose names have a space and Japanese. The scripted
agents of flowlib do what the agent and skill texts tell a model to do. Stages 1 and 2 run from a copy of
the plugin without the files of the stages above them. What one component does alone is tested in its own
folder. Without the svn command the svn half is skipped (the CI workflow checks that the command is there).
"""

import http.client
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import flowlib
from flowlib import CIDS, FIXES, GROUP, Agent

WORD = {"fix": "修正を取り込む", "deviation": "逸脱コメントを登録する", "none": "何もしない", None: "-"}
ASK = "よろしければ「OK」と入力してください。変更するときは「20003 は逸脱、20007 は何もしない、ほかは OK」のように入力してください"
SAVED = "patch_file"  # a key of the reply of an accepted save


class Case(unittest.TestCase):
    kind = "git"

    def person(self, **options) -> None:
        """The person's side: the repository, the Coverity, and what the repository holds before a request."""
        # svn on a Windows PC that is not set to Japanese cannot take a Japanese folder name
        plain = self.kind == "svn" and os.name == "nt" and "テスト".encode("mbcs", "replace").decode("mbcs") != "テスト"
        self.top = Path(tempfile.mkdtemp(prefix="flow test " if plain else "flow テスト ")).resolve()
        self.addCleanup(flowlib.remove_tree, self.top)
        self.repo = flowlib.make_repo(self.kind, self.top, **options)
        self.coverity = flowlib.EMULATORS[self.top]
        self.before = flowlib.user_tree(self.kind, self.repo)

    def read(self, run: Path, name: str) -> dict:
        return json.loads((run / name).read_bytes())

    def listed(self, run_dir: str) -> list[int]:
        return [issue["cid"] for issue in self.read(Path(run_dir), "issues.json")["issues"]]

    def summary(self, agent: Agent, run: Path, line: str = "") -> dict:
        """「まとめを見せて」: finish (or ``line``, a request that leads to it), then the one table and the one
        question of skill coverity-apply, for this result folder."""
        done = agent.follow(line or agent.line("triage", "finish", "--run", run))
        self.assertEqual((done["run_dir"], done["next"]), (run.as_posix(), []))
        return agent.table()

    def written(self) -> list[tuple]:
        """What Coverity received, in order: (CIDs, Classification, Action) of every entry."""
        return [(write["cids"], write["classification"], write["action"]) for write in self.coverity.writes]

    def assert_nothing_changed(self):
        """Before the OK the person's repository and Coverity are as they were."""
        self.assertEqual((flowlib.user_tree(self.kind, self.repo), self.coverity.writes), (self.before, []))

    def taken_in(self, run: Path, *items: list[int]) -> bytes:
        """The fixes of these items have arrived and nothing else has changed. git: one commit per fix on
        the branch of the folder on origin; the person's working tree, index and branch are untouched.
        svn: the working copy has the patch, nothing is committed. Returns the code with the fixes."""
        tree = flowlib.user_tree(self.kind, self.repo)
        if self.kind == "svn":
            self.assertEqual(tree, self.before + b"M       src/reader.c\n")
            return (self.repo / "src" / "reader.c").read_bytes()
        git = ("git", "--git-dir", self.top / "origin.git")
        log = flowlib.call(*git, "log", "--reverse", "--format=%s", f"main..coverity-fix/{run.name}", cwd=self.top)
        subjects = [f"Coverity CID {', '.join(map(str, item))} の修正（{flowlib.WARNINGS[item[0]]['checker']}）" for item in items]
        self.assertEqual((log.decode("utf-8").splitlines(), tree), (subjects, self.before))
        return flowlib.call(*git, "show", f"coverity-fix/{run.name}:src/reader.c", cwd=self.top)

    def screen(self, url: str, path: str, body: dict | None = None) -> dict:
        """One request of the page to its server."""
        port, token = urlsplit(url).port, parse_qs(urlsplit(url).query)["t"][0]
        page = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        page.request("POST" if body else "GET", path, json.dumps(body).encode("utf-8") if body else None,
                     {"X-Token": token, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
        return json.loads(page.getresponse().read())

    def parity(self, agent: Agent, url: str) -> tuple[dict, dict]:
        """One rule, two implementations: every row has the same choice in the table of the chat and on the
        screen, and a default 何もしない has the same reason. Returns (the plan reply, screen rows by CID)."""
        plan, state = agent.table(), self.screen(url, "/api/state")
        table, rows = flowlib.rows(plan["table"]), {row["cid"]: row for row in state["rows"]}
        self.assertEqual({cid: row[5] for cid, row in table.items()}, {cid: WORD[row["choice"]] for cid, row in rows.items()})
        self.assertEqual({cid: table[cid][6] for cid, row in rows.items() if row["choice_from"] == "default"},
                         {cid: row["reason"] for cid, row in rows.items() if row["choice_from"] == "default"})
        self.assertEqual(state["choices"], {choice: plan["counts"][choice] for choice in ("fix", "deviation", "none")})
        return plan, rows


class Flow:
    """The scenarios; GitFlow and SvnFlow below run them."""

    def test_stage_1_one_cid_at_a_time_in_a_chat(self):
        """README.md and docs/guide/1-skill.md, with no agent and no screen installed."""
        self.person(settings=None)
        plugin = flowlib.installed("agents", "coverity-gui")
        self.assertEqual([path.name for name in ("triage-*", "*gui*") for path in plugin.rglob(name)], [])
        me = Agent(self.repo, plugin, {20003: ("intentional", "deviation", "high")})
        # /coverity-triage-kit:coverity-setup <URL> <ストリーム>; 「準備ができました」 is said when both are ok
        made = me.run(me.line("setup", "init", "--repo", self.repo, "--url", self.coverity.url, "--stream", flowlib.STREAM))
        check = me.tool(made["fetch"][0])
        self.assertEqual((made["created"], made["ok"], check["ok"], check["message"]),
                         (True, True, True, "接続できました。ストリーム Sample-main を確認しました"))
        config = Path(made["config_file"])  # the person lowers the limit in the settings file
        config.write_text(config.read_text(encoding="utf-8").replace("limit: 20", "limit: 3"), encoding="utf-8")
        self.before = flowlib.user_tree(self.kind, self.repo)
        # 「条件に合う警告を取得して」
        request = me.line("triage", "start", "--repo", self.repo)
        first = me.ready(request)
        run = Path(first["run_dir"])
        self.assertEqual(first["message"], "条件に合う警告は 6 件あります。設定の上限（3 件）まで取得しました")
        self.assertEqual((self.listed(run), run.parent), (CIDS[:3], self.top / "coverity-triage-out"))
        # 「CID 20002 を処置して」, then 「まとめを見せて」: the one table and the one question
        saved = me.follow(first["next"][0] + " --cid 20002", stop=SAVED)
        plan = self.summary(me, run)
        comment = self.read(run, "cid/20002/result.json")["deviation"]["comment"]
        self.assertEqual([row[3:] for row in flowlib.rows(plan["table"]).values()], [["-", "-", "-", "未処置"], [
            "なし", comment + "（False Positive／Ignore／Unspecified）", WORD["deviation"], ""], ["-", "-", "-", "未処置"]])
        self.assertEqual(plan["question"], "この内容で、逸脱コメントの登録（1 件）を行います。" + ASK)
        self.assert_nothing_changed()
        # 「OK」: the comment is in Coverity
        me.ok(plan)
        self.assertEqual(self.coverity.writes, [{"triageStore": "Default Triage Store", "cids": [20002], "comment": comment,
                                                 "classification": "False Positive", "action": "Ignore", "severity": "Unspecified"}])
        # 「次を処置して」 in this session (the line under next of the save), 「まとめを見せて」, 「OK」: the fix arrives
        self.assertEqual(me.follow(saved["next"][0], stop=SAVED)["cid"], 20001)
        me.ok(self.summary(me, run))
        self.assertIn(FIXES[20001][1], self.taken_in(run, [20001]))
        # a new session: the list request continues the same list, 「次を処置して」 gets its last CID
        again = me.ready(request)
        self.assertEqual((again["run_dir"], again["message"]), (run.as_posix(), "前回の続きから進めます（残り 1 件）"))
        self.assertEqual(me.follow(again["next"][0], stop=SAVED)["cid"], 20003)
        # the list request before the OK for that CID: the work is complete, and the table is the one of this folder
        plan = self.summary(me, run, request)
        self.assertEqual([flowlib.rows(plan["table"])[cid][5:] for cid in CIDS[:3]],
                         [["-", "取り込み済み"], ["-", "登録済み"], [WORD["deviation"], ""]])
        me.ok(plan)
        where = f"修正をブランチ coverity-fix/{run.name} にコミットしました。" if self.kind == "git" else "修正を作業コピーに適用しました。"
        self.assertEqual((self.written()[1:], self.coverity.writes[1]["comment"].startswith(where)),
                         ([([20001], "Bug", "Fix Required"), ([20003], "Intentional", "Ignore")], True))
        self.taken_in(run, [20001])  # the OK for a comment takes nothing in
        # 「次を処置して」 after the last CID and its OK: a new list, without what was taken in or registered
        new = me.ready(again["next"][0])
        self.assertEqual((new["run_dir"] != run.as_posix(), self.listed(new["run_dir"])), (True, CIDS[3:]))

    def test_stage_2_one_agent_processes_every_warning_to_the_end(self):
        """docs/guide/2-agent.md, with only this agent installed: no other agent, no screen."""
        self.person()
        plugin = flowlib.installed("coverity-gui", *(f"triage-{name}{form}" for name in ("orchestrator", "reviewer", "integrator")
                                                     for form in (".md", ".agent.md")))
        self.assertEqual(sorted(path.name for name in ("triage-*", "*gui*") for path in plugin.rglob(name)),
                         ["triage-worker.agent.md", "triage-worker.md"])
        opinions = {20005: ("intentional", "deviation", "high"), 20006: ("undetermined", "fix", "low")}
        request = flowlib.line(plugin, "triage", "start", "--repo", self.repo, "--slots", 1)
        first = Agent(self.repo, plugin, opinions)
        given = first.follow(request, stop=lambda reply: reply.get("cid") == 20003)
        first.edit(given)  # the session ends here: the third CID is half done, nothing is saved
        second = Agent(self.repo, plugin, opinions)  # the same request again, in a new session
        second.slips = {20005}
        done = second.follow(request)  # to the end of finish, without a question
        self.assertEqual((second.replies[0]["run_dir"], second.replies[0]["message"]),
                         (given["run_dir"], "前回の続きから進めます（残り 4 件）"))
        self.assertEqual((first.worked, second.worked, second.calls), (CIDS[:2], CIDS[2:], []), "no CID twice, no second fetch")
        refused = [(reply["cid"], reply["ok"]) for reply in second.replies if "problems" in reply]
        self.assertEqual((refused, done["counts"]["done"], done["next"]), ([(20005, False)], 6, []), "one save refused, then corrected")
        run = Path(done["run_dir"])
        # the one table: every fetched CID with both outputs and a choice
        plan = second.table()
        table = flowlib.rows(plan["table"])
        self.assertEqual({cid: [row[3], *row[5:]] for cid, row in table.items()}, {
            20001: ["src/reader.c", WORD["fix"], ""], 20002: ["なし", WORD["deviation"], ""], 20003: ["src/reader.c", WORD["fix"], ""],
            20004: ["src/reader.c", WORD["fix"], ""], 20005: ["なし", WORD["deviation"], ""],
            20006: ["src/reader.c", WORD["none"], "確信度が低いため"]})
        for cid, row in table.items():
            self.assertTrue(row[4].startswith(self.read(run, f"cid/{cid}/result.json")["deviation"]["comment"] + "（"), row)
        self.assertEqual((plan["summary_file"], plan["question"]), (done["summary_file"], (
            "この内容で、修正の取り込み（3 件）、逸脱コメントの登録（2 件）を行います。"
            "取り込んだ修正は、Coverity に「Bug／Fix Required」として登録します。" + ASK)))
        self.assert_nothing_changed()
        # 「20003 は逸脱、ほかは OK」
        plan = second.table("20003=deviation")
        self.assertEqual(flowlib.rows(plan["table"])[20003][5:], [WORD["deviation"], "チャットで指定"])
        second.ok(plan)
        code = self.taken_in(run, [20001], [20004])
        self.assertEqual([cid for cid in FIXES if FIXES[cid][1] in code], [20001, 20004])
        self.assertEqual(self.written(), [([20002], "False Positive", "Ignore"), ([20003], "Bug", "Ignore"),
                                          ([20005], "Intentional", "Ignore"), ([20001], "Bug", "Fix Required"),
                                          ([20004], "Bug", "Fix Required")], "the chosen deviations, the fixes taken in, nothing else")
        # the same request again: the next list is what was left undecided
        new = Agent(self.repo, plugin).ready(request)
        self.assertEqual((self.listed(new["run_dir"]), new["message"]), ([20006], "条件に合う警告を 1 件取得しました"))

    def test_stage_3_workers_in_parallel_one_cause_merged_every_result_reviewed(self):
        """docs/guide/3-multi-agent.md; then a write to Coverity that is not made at first."""
        self.person()
        lead = Agent(self.repo)
        request = lead.line("triage", "start", "--repo", self.repo, "--review")
        first = lead.ready(request)
        lead.act(first)  # the warning paths of all CIDs, once
        run = Path(first["run_dir"])
        self.assertEqual((len(first["next"]), lead.calls), (3, ["coverity_search_issues", "coverity_get_issue_details"]))
        reviews = {20001: 'rework --point "src/reader.c:20 fclose の戻り値を使用しないことを明示する"',
                   20003: 'flag --point "src/reader.c:47 get_buf の呼び出し元の仕様で決まる"'}
        rounds, early = [], None
        for status in flowlib.team(lead, run, reviews, rounds):
            if status.get("review_batches") and early is None:  # 「まとめを見せて」 while no result is reviewed
                early = lead.run(lead.line("triage", "finish", "--run", run))
        self.assertEqual((early["ok"], early["unreviewed"]), (False, CIDS))
        got = [worker.worked for worker in rounds[0]]
        self.assertEqual((sorted(sum(got, [])), all(got)), (CIDS, True), "three workers at the same time: every CID once")
        self.assertEqual([sum((worker.worked for worker in workers), []) for workers in rounds[1:]], [[20004], [20001]],
                         "made again: the leader of the merged CIDs, then the CID that the reviewer sent back")
        # the one table: one row for the merged CIDs; the flagged result is left to the person, the others passed
        plan = lead.table()
        self.assertEqual({cid: [row[0], *row[5:]] for cid, row in flowlib.rows(plan["table"]).items()}, {
            20001: ["20001", WORD["fix"], ""], 20002: ["20002", WORD["deviation"], ""],
            20003: ["20003", WORD["none"], "検証で要確認のため"], 20004: ["20004（同じ原因: 20005, 20006）", WORD["fix"], ""]})
        summary = Path(plan["summary_file"]).read_text(encoding="utf-8")
        self.assertEqual(([summary.count(f"- 検証: {word}") for word in ("合格", "要確認", "差し戻し")],
                          "- 検証の指摘: src/reader.c:47 get_buf" in summary), ([3, 1, 0], True), "every result is reviewed")
        self.assert_nothing_changed()
        # 「20001 は何もしない、ほかは OK」: the one fix of the merged CIDs is taken in, and Coverity gets it and the comment
        lead.ok(lead.table("20001=none"))
        code = self.taken_in(run, GROUP)
        self.assertEqual([FIXES[cid][1] in code for cid in (20001, 20003, *GROUP)], [False, False, True, True, True])
        self.assertEqual(self.written(), [([20002], "False Positive", "Ignore"), (GROUP, "Bug", "Fix Required")])
        # 「修正を取り込んで」, 「20001 は修正、ほかは OK」; the person refuses the permission prompt, so the write tool is not called
        applied = lead.ok(lead.table("20001=fix"), write=False)
        self.assertEqual((applied["message"].split("。")[-1], len(applied["call"]), len(self.coverity.writes)),
                         ("続けて、取り込んだ修正 1 件を Coverity に登録します", 1, 2))
        self.assertIn(b"(void)fclose(fp);", self.taken_in(run, GROUP, [20001]))  # the fix that was made again is in
        # the same request in a new session: the folder stays open, and the request leads to its table again
        again = Agent(self.repo)
        plan = self.summary(again, run, request)
        self.assertEqual([row[5:] for row in flowlib.rows(plan["table"]).values()], [
            ["-", "取り込み済み（Coverity への登録が残っています）"], ["-", "登録済み"], [WORD["none"], "検証で要確認のため"], ["-", "取り込み済み"]])
        self.assertEqual(plan["question"], "この内容で、取り込み済みの修正の Coverity への登録（1 件）を行います。" + ASK)
        again.ok(plan)
        self.assertEqual(self.written()[2:], [([20001], "Bug", "Fix Required")])
        self.taken_in(run, GROUP, [20001])  # and no fix twice
        # after the write the same request fetches again: the list is what was left to the person
        new = Agent(self.repo).ready(request)
        self.assertEqual((new["run_dir"] != run.as_posix(), self.listed(new["run_dir"])), (True, [20003]))

    def test_stage_4_the_screen_shows_the_choices_of_the_chat_and_its_choices_are_carried_out(self):
        """docs/guide/4-screen.md, on a result folder of the multi-agent stage that holds every kind of row."""
        self.person(settings="build:\n  check_fixes: true\n  command: exit 1\n", extra=3)  # no fix passes the build check
        me = Agent(self.repo)
        # 「画面を開いて」 (and at the end 「画面を閉じて」)
        self.addCleanup(me.run, me.line("gui", "stop", "--repo", self.repo))
        url = me.run(me.line("gui", "open", "--repo", self.repo))["url"]
        first = me.ready(me.line("triage", "start", "--repo", self.repo, "--review"))
        me.act(first)
        run = Path(first["run_dir"])
        for _ in range(2):  # a worker takes a CID and is gone, twice: that CID fails
            Agent(self.repo).run(first["next"][1])
        reviews = {20007: 'flag --point "src/reader.c:13 path の出所で決まる"', 20008: "pass --cap low", 20009: None}
        for step in flowlib.team(me, run, reviews, []):
            if " finish --run " in "".join(step["next"]):  # every result is reviewed, the build check has not run yet
                rows = self.parity(me, url)[1]
        self.assertEqual((step["counts"]["failed"], rows[20003]["choice"], rows[20004]["choice"]), (1, "fix", "fix"))
        # the screen: 修正を取り込む for 20003; then, in the chat, 「CID 20003 を処置して。…」 (made again)
        chosen = self.screen(url, "/api/decision", {"run": run.name, "cid": 20003, "decision": "fix", "comment": None})["row"]
        self.assertEqual((chosen["choice"], chosen["choice_from"], chosen["build"]), ("fix", "screen", "failed"))
        time.sleep(1.01 - time.time() % 1)  # a choice counts until state.json is written in a later second
        redo = me.line("triage", "next", "--run", run, "--slot 1 --cid 20003")
        wait = me.run(redo)  # finish has removed the work copies: 20003 is unfinished until start has made them again
        rows = self.parity(me, url)[1]
        self.assertEqual({cid: (row["status"], row["choice"], row["reason"], row["members"]) for cid, row in rows.items()}, {
            20001: ("failed", None, "", []), 20002: ("done", "deviation", "", []), 20003: ("pending", None, "", []),
            20004: ("done", "none", "ビルドに失敗したため", GROUP[1:]), 20007: ("done", "none", "検証で要確認のため", []),
            20008: ("done", "none", "確信度が低いため", []), 20009: ("done", "none", "未検証のため", [])}, "every kind of row")
        me.ready(wait["next"][0])
        me.follow(redo, stop=SAVED)
        again = self.parity(me, url)[1][20003]
        self.assertEqual((again["choice"], again["choice_from"], again["reason"]), ("none", "default", "未検証のため"),
                         "a choice made before the result was made again no longer counts")
        # the screen: an edited comment for 20002, and the fix of the merged CIDs in spite of the build check
        edited = "誤検知。唯一の呼び出し元 read_config()（src/reader.c:32〜36）が path の NULL を除外してから渡している。"
        for cid, choice, comment in ((20002, "deviation", edited), (20004, "fix", None)):
            self.assertTrue(self.screen(url, "/api/decision", {"run": run.name, "cid": cid, "decision": choice, "comment": comment})["ok"])
        # 「画面で選んだ内容で進めて」, then 「OK」
        plan = self.parity(me, url)[0]
        table = flowlib.rows(plan["table"])
        self.assertEqual((table[20002][4:], table[20004][5:], table[20003][5:]), (
            [edited + "（False Positive／Ignore／Unspecified）", WORD["deviation"], "画面で選択"], [WORD["fix"], "画面で選択"],
            [WORD["none"], "未検証のため"]))
        self.assert_nothing_changed()
        me.ok(plan)
        self.assertEqual((self.coverity.writes[0]["comment"], self.written()),
                         (edited, [([20002], "False Positive", "Ignore"), (GROUP, "Bug", "Fix Required")]))
        code = self.taken_in(run, GROUP)
        self.assertEqual([FIXES[cid][1] in code for cid in (20003, *GROUP)], [False, True, True, True])
        rows = self.parity(me, url)[1]
        self.assertEqual((rows[20002]["done"], rows[20002]["comment"], rows[20004]["done"]), ("written", edited, "applied"))


class GitFlow(Flow, Case):
    kind = "git"


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn コマンドがありません")
class SvnFlow(Flow, Case):
    kind = "svn"


if __name__ == "__main__":
    unittest.main()
