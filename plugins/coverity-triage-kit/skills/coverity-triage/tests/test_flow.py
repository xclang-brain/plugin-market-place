"""start, next, save, review, merge, status and finish on a git and on an svn repository."""

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import support
from support import issue
import vcs

ONE, TWO, THREE = 20001, 20002, 20003
THREE_ISSUES = [issue(ONE, function="a"), issue(TWO, "src/b.c", checker="DEADCODE"), issue(THREE, function="a")]
B_EDIT = {"src/b.c": (b"return 0;", b"return 1;")}


class Flow:
    """The tests; GitFlow and SvnFlow below run them."""

    def next(self, run, *args) -> dict:
        return self.run_triage("next", "--run", run, *args)

    def meta(self, run) -> dict:
        return json.loads((run / "run.json").read_text(encoding="utf-8"))

    def test_start_asks_for_the_list_first_and_continues_the_folder(self):
        first = self.run_triage("start", "--repo", self.repo)  # answered at once: the copies are made meanwhile
        run = Path(first["run_dir"])
        self.assertEqual(run.parent, self.top / "coverity-triage-out")
        self.assertEqual(first["fetch"], [{"tool": "coverity_search_issues", "arguments": {
            "repo_root": self.repo.as_posix(), "output_file": (run / "issues.json").as_posix()}}])
        self.assertIn(f'start --repo "{self.repo.as_posix()}"', first["next"][0])
        self.assertNotIn("message", first)
        self.assertEqual(self.start()["run_dir"], first["run_dir"], "a folder without the list is continued")
        early = self.next(run)
        self.assertEqual((early["ok"], early["error"]),  # an agent passes it on: it says what the person does
                         (False, "警告の一覧がまだありません。同じ依頼をもう一度入力すると、警告の取得から進みます"))
        self.assertIn("start --repo", early["next"][0])

        (run / "issues.json").write_text(json.dumps({"issues": THREE_ISSUES, "total_matched": 137, "limited": True}))
        ready = self.start()
        self.assertEqual((ready["run_dir"], ready["counts"]["total"], ready["counts"]["pending"]),
                         (first["run_dir"], 3, 3))
        self.assertEqual(ready["message"], "条件に合う警告は 137 件あります。設定の上限（3 件）まで取得しました")
        self.assertEqual(ready["fetch"], [], "in a chat the warning paths are fetched per CID")
        self.assertIn(f'next --run "{run.as_posix()}" --slot 1', ready["next"][0])
        self.assertEqual({k: v for k, v in self.meta(run).items() if k != "base_revision"}, {
            "repo": self.repo.as_posix(), "vcs": self.kind, "stream": "app-main", "conditions": {}, "slots": 1,
            "review": False, "all": False, "base_branch": "main" if self.kind == "git" else ""})
        self.assertTrue(self.meta(run)["base_revision"] and (run / "work" / "w1" / "src" / "a.c").is_file())

        every = self.start("--slots", 1)
        self.assertEqual(every["fetch"], [{"tool": "coverity_get_issue_details", "arguments": {  # the warning paths
            "repo_root": self.repo.as_posix(), "cids": [ONE, TWO, THREE], "output_dir": (run / "cid").as_posix()}}])
        self.work(self.next(run))
        self.assertEqual(self.start("--slots", 1)["message"], "前回の続きから進めます（残り 2 件）")
        other = self.start("--new")
        self.assertNotEqual(other["run_dir"], first["run_dir"])
        self.assertEqual(other["fetch"][0]["tool"], "coverity_search_issues")
        self.assertFalse((run / "work").exists(), "the copies of the earlier run are removed")

    def test_the_mode_of_a_run_only_grows(self):
        run = self.begin(THREE_ISSUES, "--slots", 2, "--review")
        self.work(self.next(run, "--slot", 1))
        self.assertEqual(self.next(run, "--slot", 2)["cid"], TWO)
        plain = self.start()  # a request in a chat while the agents work
        self.assertEqual({k: self.meta(run)[k] for k in ("slots", "review", "all")},
                         {"slots": 2, "review": True, "all": True})
        self.assertEqual(len(plain["next"]), 2, "the lines of both slots")
        self.assertEqual(self.next(run, "--slot", 1)["cid"], THREE, "the CID of slot 2 stays with slot 2")
        refused = self.run_triage("finish", "--run", run)
        self.assertEqual((refused["ok"], refused["pending"]), (False, [TWO, THREE]))
        self.assertEqual(self.run_triage("status", "--run", run)["review_batches"][0]["cids"], [ONE])

    def test_an_old_list_is_shown_with_the_time_it_was_fetched(self):
        run = self.begin(THREE_ISSUES)
        fetched = datetime.now() - timedelta(hours=3)
        os.utime(run / "issues.json", (fetched.timestamp(), fetched.timestamp()))
        self.assertEqual(self.start()["message"], f"条件に合う警告を 3 件取得しました（警告の取得: {fetched:%Y-%m-%d %H:%M}）")

    def test_other_conditions_or_another_stream_get_a_new_folder(self):
        self.configure("conditions:\n  impacts: [Medium, Low]\n")
        run = self.begin(THREE_ISSUES)
        self.work(self.next(run, "--cid", ONE))
        self.assertEqual(self.meta(run)["conditions"], {"impacts": ["Medium", "Low"]})
        self.assertEqual(self.start()["run_dir"], run.as_posix(), "the conditions of the folder: continued")
        high = "conditions:\n  impacts: [High]\n"
        self.configure(high)  # the person changes the conditions and asks again: the list is fetched for them
        second = self.begin(THREE_ISSUES[:1])
        self.assertNotEqual(second, run)
        self.assertEqual((self.meta(second)["stream"], self.meta(second)["conditions"]),
                         ("app-main", {"impacts": ["High"]}))
        self.assertFalse((run / "work").exists(), "the copies of the earlier folder are removed")
        self.configure(high, stream="app-next")
        third = self.start()
        self.assertNotIn(third["run_dir"], (run.as_posix(), second.as_posix()))
        self.assertEqual(third["fetch"][0]["tool"], "coverity_search_issues")
        self.assertEqual(self.meta(Path(third["run_dir"]))["stream"], "app-next")

    def test_no_warning_is_answered_once_and_the_next_request_fetches_again(self):
        first = self.start("--slots", 1)
        run = Path(first["run_dir"])
        (run / "issues.json").write_text(json.dumps({"issues": [], "total_matched": 0, "limited": False}))
        answer = self.start("--slots", 1)  # the folder with the empty list is open until this was answered
        self.assertEqual((answer["run_dir"], answer["message"], answer["next"], answer["counts"]["total"]),
                         (first["run_dir"], "条件に合う警告はありません", [], 0))
        self.assertIn("- 警告: 0 件（処置済み 0 件、失敗 0 件、未処置 0 件）", self.summary(run))
        self.settle()
        again = self.start("--slots", 1)  # the same request a minute later: Coverity is asked again
        self.assertNotEqual(again["run_dir"], first["run_dir"])
        self.assertEqual(again["fetch"][0]["tool"], "coverity_search_issues")
        self.assertFalse((run / "work").exists())

    def test_next_hands_out_every_cid_once_and_save_stores_the_fix(self):
        run = self.begin(THREE_ISSUES, "--slots", 1)
        (run / "cid" / str(TWO)).mkdir(parents=True)
        (run / "cid" / str(TWO) / "issue.json").write_text('{"events": []}')
        before = support.user_tree(self.kind, self.repo)
        handed = []
        while True:
            given = self.next(run)
            if given["cid"] is None:
                break
            handed.append(given["cid"])
            cid_dir = run / "cid" / str(given["cid"])
            self.assertEqual((given["issue_file"], given["result_file"], given["work_dir"]),
                             ((cid_dir / "issue.json").as_posix(), (cid_dir / "result.json").as_posix(),
                              (run / "work" / "w1").as_posix()))
            self.assertEqual(given["fetch"], [] if given["cid"] == TWO else [{  # only when issue.json is missing
                "tool": "coverity_get_issue_details", "arguments": {
                    "repo_root": self.repo.as_posix(), "cids": [given["cid"]],
                    "output_dir": (run / "cid").as_posix()}}])
            self.assertIn(f"save --run \"{run.as_posix()}\" --cid {given['cid']} --slot 1", given["next"][0])
            saved = self.work(given, B_EDIT if given["cid"] == TWO else None)
            self.assertTrue(saved["ok"], saved)
            self.assertEqual(saved["files"], ["src/b.c" if given["cid"] == TWO else "src/a.c"])
            self.assertIn(" finish --run" if saved["counts"]["remaining"] == 0 else " next --run", saved["next"][0])
        self.assertEqual(handed, [ONE, TWO, THREE])
        self.assertTrue(given["done"] and "finish --run" in given["next"][0])
        self.assertEqual(self.state(run, ONE)["status"], "done")
        self.assertIn(b"return p ? *p : 0;", (run / "cid" / str(ONE) / "fix.patch").read_bytes())
        self.assertEqual(support.user_tree(self.kind, self.repo), before, "the person's files are not touched")
        self.assertEqual((run / "work" / "w1" / "src" / "a.c").read_bytes(), support.FILES["src/a.c"], "a clean copy")
        self.assertEqual(len((run / "log.txt").read_text(encoding="utf-8").splitlines()), 9, "one line per command")
        # every CID is saved, but the run was not finished: the same request leads to finish, not to a new fetch
        again = self.start("--slots", 1)
        self.assertEqual((again["run_dir"], again["counts"]["done"]), (run.as_posix(), 3))
        self.assertIn("finish --run", again["next"][0])

    def test_save_fills_in_the_values_for_coverity(self):
        run = self.begin([issue(ONE), issue(TWO), {**issue(THREE), "impact": "Low"}])
        fixed = "本物のバグ。最新のコードでは a()（src/a.c:3）で NULL を確認してから参照しており、解消されている。"
        self.work(self.next(run), deviation={"comment": support.FIX_COMMENT, "action": "Fix Required"})
        self.work(self.next(run), {})
        self.work(self.next(run), {}, verdict="true_bug", deviation={"comment": fixed})  # fixed in the latest code
        self.assertEqual([self.read(run, cid, "result.json")["deviation"] for cid in (ONE, TWO, THREE)], [
            {"comment": support.FIX_COMMENT, "classification": "Bug", "action": "Ignore", "severity": "Major"},
            {"comment": support.KEEP_COMMENT, "classification": "False Positive", "action": "Ignore",
             "severity": "Unspecified"},
            {"comment": fixed, "classification": "Bug", "action": "Fix Submitted", "severity": "Minor"}])

    def test_slots_in_parallel_processes_get_every_cid_exactly_once(self):
        cids = list(range(30001, 30010))
        run = self.begin([issue(cid) for cid in cids], "--slots", 3)
        handed, barrier, errors = {1: [], 2: [], 3: []}, threading.Barrier(3), []

        def worker(slot):
            try:
                barrier.wait()
                while True:
                    given = self.next(run, "--slot", slot)
                    if given["cid"] is None:
                        return
                    handed[slot].append(given["cid"])
                    self.assertEqual(given["work_dir"], (run / "work" / f"w{slot}").as_posix())
                    self.assertTrue(self.work(given)["ok"])
            except Exception as exc:  # noqa: BLE001 - reported by the main thread
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(slot,)) for slot in handed]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(sum(handed.values(), [])), cids, handed)
        self.assertTrue(all(self.state(run, cid)["status"] == "done" for cid in cids))

    def test_batch_of_a_round_is_five_cids(self):
        run = self.begin([issue(cid) for cid in range(30001, 30008)], "--slots", 2, "--review")
        commands = self.run_triage("status", "--run", run)["next"]
        self.assertEqual([c.split(" next ")[1] for c in commands],
                         [f'--run "{run.as_posix()}" --slot {k} --batch 1' for k in (1, 2)])
        for cid in range(30001, 30006):
            given = self.next(run, "--slot", 1, "--batch", 1)
            self.assertEqual(given["cid"], cid)
            saved = self.work(given)
            self.assertIn("--slot 1 --batch 1", saved["next"][0])
        over = self.next(run, "--slot", 1, "--batch", 1)
        self.assertEqual((over["cid"], over["done"], over["next"], over["counts"]["remaining"]), (None, True, [], 2))
        for slot, line in ((1, "--slot 1 --batch 1"), (2, "--slot 2")):  # a refused save: the slot stays in its round
            refused = self.run_triage("save", "--run", run, "--cid", 30001, "--slot", slot)
            self.assertEqual((refused["ok"], refused["next"][0].split('" ')[-1]), (False, line))
        self.assertIn("--batch 2", self.run_triage("status", "--run", run)["next"][0])
        self.assertEqual(self.next(run, "--slot", 1, "--batch", 2)["cid"], 30006)

    def test_a_dead_claim_is_released(self):
        run = self.begin(THREE_ISSUES, "--slots", 2)
        self.assertEqual(self.next(run, "--slot", 1)["cid"], ONE)
        again = self.next(run, "--slot", 1)  # the slot asks again without a save: its claim was lost
        self.assertEqual((again["cid"], again["message"]), (ONE, f"CID {ONE} は、前回の結果が保存されていないため、もう一度処置します"))
        self.assertEqual(self.next(run, "--slot", 1)["cid"], TWO, "handed out twice without a save: failed")
        failed = self.state(run, ONE)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("2回", failed["error"])

        state_file = run / "cid" / str(TWO) / "state.json"
        self.assertEqual(self.next(run, "--slot", 2)["cid"], THREE, "a fresh claim of another slot is kept")
        long_ago = (datetime.now() - timedelta(minutes=31)).isoformat(timespec="seconds")
        state_file.write_text(json.dumps({**self.state(run, TWO), "claimed_at": long_ago}), encoding="utf-8")
        taken = self.next(run, "--slot", 2)  # older than 30 minutes: any slot takes it (its own THREE comes first)
        self.assertEqual(taken["cid"], THREE)
        self.work(taken)
        taken = self.next(run, "--slot", 2)
        self.assertEqual((taken["cid"], self.state(run, TWO)["slot"]), (TWO, 2))
        late = self.run_triage("save", "--run", run, "--cid", TWO, "--slot", 1)  # the slot that lost the claim
        self.assertFalse(late["ok"])
        self.assertEqual(late["error"], f"CID {TWO} は処置中ではないため、保存できません。"
                                        "同じ依頼をもう一度入力すると、続きから進みます")
        status = self.run_triage("status", "--run", run)
        self.assertEqual(status["counts"],
                         {"total": 3, "pending": 0, "working": 1, "done": 1, "failed": 1, "remaining": 1})
        self.assertEqual(self.next(run, "--slot", 2)["cid"], None, "TWO fails too; a loop gets no failed CID again")
        named = self.next(run, "--slot", 2, "--cid", ONE)  # a person asks for the failed CID: once more
        self.assertEqual((named["cid"], self.work(named)["ok"], self.state(run, ONE)["status"]), (ONE, True, "done"))
        self.assertNotIn("error", self.state(run, ONE))

    def test_save_rejects_a_bad_result_and_a_bad_comment(self):
        run = self.begin(THREE_ISSUES, "--slots", 1)
        given = self.next(run)
        cid_dir = run / "cid" / str(ONE)
        missing = self.run_triage("save", "--run", run, "--cid", ONE)
        self.assertFalse(missing["ok"])
        self.assertIn("result.json がありません", missing["problems"][0])
        bad = self.work(given, evidence=[], confidence="certain", verdict=["true_bug"],
                        deviation={"comment": "おそらく問題ない。" + "あ" * 200})
        problems = "\n".join(bad["problems"])
        self.assertEqual((bad["ok"], bad["error"]), (False, "結果に不備があります。problems の内容を訂正して、もう一度保存してください"))
        for expected in ("evidence: 根拠を1件以上", "confidence:", "verdict:", "deviation.comment: 200 文字以内",
                         "「おそらく」", "のいずれか）で始めてください"):
            self.assertIn(expected, problems)
        self.assertIn(f"save --run \"{run.as_posix()}\" --cid {ONE}", bad["next"][0])
        self.assertEqual(self.state(run, ONE)["status"], "working")
        self.assertFalse((cid_dir / "fix.patch").exists())
        self.assertIn("return p ? *p : 0;", (Path(given["work_dir"]) / "src" / "a.c").read_text(), "the work is kept")

        self.assertIn("work_dir のファイルが変更されていますが、fix がありません", "\n".join(self.work(given, {})["problems"]))
        outside = Path(given["work_dir"]).parent.parent / "note.c"  # evidence is a place in the code before the fix
        outside.write_bytes(b"int n;\n")
        places = [{"file": name, "line": line, "note": "x"} for name, line in (
            ("/build/src/a.c", 3), ("../../note.c", 1), ("src/none.c", 1), ("src/a.c", 5), ("src/a.c", 4))]
        elsewhere = "\n".join(self.work(given, {}, with_fix=True, evidence=places)["problems"])
        for expected in ("evidence[0].file: /build/src/a.c が work_dir にありません", "evidence[1].file: ../../note.c が",
                         "evidence[2].file: src/none.c が", "evidence[3].line: src/a.c は 4 行までです"):
            self.assertIn(expected, elsewhere)
        self.assertNotIn("evidence[4]", elsewhere)
        self.assertTrue(self.work(given)["ok"])
        unchanged = self.work(self.next(run), {}, fix={"summary": "直しました"}, no_fix_reason=None)
        self.assertIn("work_dir のファイルが変更されていません", "\n".join(unchanged["problems"]))

    def test_patch_keeps_shift_jis_and_crlf(self):
        run = self.begin([issue(ONE), issue(TWO, "src/sjis.c", 4)])
        given = self.next(run, "--cid", TWO)
        evidence = [{"file": "src/sjis.c", "line": 4, "note": "戻り値が違います"}]
        fixed = support.SJIS.decode("cp932").replace("return 0;", "return 1;")
        for data, problem in (  # a fix that a text tool saved in its own form is refused
                (fixed.encode("utf-8"), "src/sjis.c: 文字コードが変わっています（修正前: UTF-8 以外（Shift_JIS など）、修正後: UTF-8）"),
                (fixed.replace("\r\n", "\n").encode("cp932"), "src/sjis.c: 改行コードが変わっています（修正前: CRLF、修正後: LF）"),
                (fixed.replace("1;\r\n", "1;\n").encode("cp932"), "修正後: CRLF と LF の混在）")):
            refused = self.work(given, {"src/sjis.c": data}, evidence=evidence)
            self.assertEqual((refused["ok"], len(refused["problems"])), (False, 1), refused)
            self.assertIn(problem, refused["problems"][0])
        (Path(given["work_dir"]) / "src" / "sjis.c").write_bytes(support.SJIS)

        new_line, new_file = "    return 1; // 修正\r\n".encode("cp932"), "// 新規\nint n;\n".encode("utf-8")
        saved = self.work(given, {"src/sjis.c": (b"    return 0;\r\n", new_line), "src/new.c": new_file},
                          evidence=evidence)
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["files"], ["src/new.c", "src/sjis.c"])
        patch = Path(saved["patch_file"])
        self.assertIn(b"+" + new_line, patch.read_bytes())
        self.assertIn("// 日本語のコメント\r\n".encode("cp932"), patch.read_bytes())
        copy = support.fresh_copy(self.kind, self.top, self.repo)
        support.apply_patch(self.kind, copy, patch)
        self.assertEqual((copy / "src" / "sjis.c").read_bytes(), support.SJIS.replace(b"    return 0;\r\n", new_line))
        self.assertEqual((copy / "src" / "new.c").read_bytes(), new_file)
        self.assertEqual((self.repo / "src" / "sjis.c").read_bytes(), support.SJIS)
        for line in (" // 日本語のコメント", "+    return 1; // 修正", "+// 新規"):  # each file in its own encoding
            self.assertIn(line, self.summary(run).replace("\r", "").splitlines())

    def lock(self, copy: Path) -> None:
        """What a git or svn command leaves in a copy when it is killed."""
        if self.kind == "git":
            (self.repo / ".git" / "worktrees" / copy.name / "index.lock").write_bytes(b"")
        else:
            with contextlib.closing(sqlite3.connect(copy / ".svn" / "wc.db")) as db:
                db.execute("insert into wc_lock (wc_id, local_dir_relpath, locked_levels) values (1, '', -1)")
                db.commit()

    def test_a_copy_that_is_locked_or_lost_is_made_usable_again(self):
        run = self.begin(THREE_ISSUES, "--slots", 1)
        copy = run / "work" / "w1"
        self.lock(copy)
        given = self.next(run)
        self.assertEqual((given["cid"], self.state(run, ONE)["tries"]), (ONE, 1), "the lock costs the CID no try")
        self.assertTrue(self.work(given)["ok"])

        vcs.remove_tree(copy)  # the copy is gone, the record of the job that made it still says done
        lost = self.next(run)
        self.assertEqual((lost["ok"], lost["error"]), (False, (
            f"作業用コピーがありません: {copy.as_posix()}。同じ依頼をもう一度入力すると、作業用コピーを準備して、続きから進みます")))
        self.assertFalse((run / "cid" / str(TWO) / "state.json").exists(), "no CID is claimed")
        self.assertIn("start --repo", lost["next"][0])
        self.assertIn("next --run", self.start("--slots", 1)["next"][0])  # start makes the copy again
        given = self.next(run)
        self.assertEqual((given["cid"], (copy / "src" / "b.c").is_file()), (TWO, True))
        vcs.remove_tree(copy)
        gone = self.work(given, {})
        self.assertFalse(gone["ok"])
        self.assertIn("作業用コピーがありません", gone["error"])

    def test_rework_after_review_once(self):
        run = self.begin(THREE_ISSUES[:1], "--slots", 1, "--review")
        self.work(self.next(run, "--batch", 1))
        refused = self.run_triage("review", "--run", run, "--cid", ONE, "--outcome", "flag")
        self.assertFalse(refused["ok"])
        self.assertIn("1件以上", refused["problems"][0])
        point = "src/a.c:3 NULL のときに 0 を返すと、呼び出し元がエラーを検出できない"
        review = ("review", "--run", run, "--cid", ONE, "--outcome", "rework", "--point", point, "--cap")
        sent = self.run_triage(*review, "medium")
        self.assertEqual((sent["outcome"], sent["confidence"]), ("rework", "medium"))
        state = self.state(run, ONE)
        self.assertEqual((state["status"], state["reworked"], state["rework"]), ("rework", True, [point]))
        self.assertEqual(self.run_triage("status", "--run", run)["counts"]["pending"], 1)

        given = self.next(run, "--batch", 2)
        self.assertEqual(given["rework"], {"points": [point], "patch_applied": True})
        self.assertIn(b"return p ? *p : 0;", (Path(given["work_dir"]) / "src" / "a.c").read_bytes(), "the earlier fix")
        self.assertTrue(self.work(given, {"src/a.c": (b"return p ? *p : 0;", b"return p ? *p : -1;")})["ok"])
        self.assertIn(b"+    return p ? *p : -1;", (run / "cid" / str(ONE) / "fix.patch").read_bytes())
        self.assertNotIn("review", self.state(run, ONE), "the new result needs a new review")
        self.assertFalse(self.run_triage("finish", "--run", run)["ok"])

        second = self.run_triage(*review, "low")
        self.assertEqual((second["outcome"], second["confidence"]), ("flag", "low"), "a second rework is stored as 要確認")
        self.assertEqual(self.read(run, ONE, "review.json"),
                         {"outcome": "flag", "points": [point], "confidence_cap": "low"})
        self.assertEqual(self.state(run, ONE)["status"], "done")
        done = self.run_triage("finish", "--run", run)
        self.assertTrue(done["ok"], done)
        self.assertIn("| 低 | 要確認 |", self.summary(run))
        self.assertIn(f"- 検証: 要確認\n- 検証の指摘: {point}\n", self.summary(run))
        self.next(run, "--cid", ONE)  # the person has a remark on the result: it is made again
        self.start("--slots", 1, "--review")
        self.assertTrue(self.work(self.next(run, "--cid", ONE), {}, with_fix=True)["ok"])
        self.assertNotIn("検証の指摘", self.summary(run), "the review of the earlier result is not shown with the new one")

    def test_merge_of_cids_with_one_cause(self):
        run = self.begin(THREE_ISSUES, "--slots", 1, "--review")
        for cid in (ONE, TWO, THREE):
            self.work(self.next(run, "--batch", 1), B_EDIT if cid == TWO else None)
        status = self.run_triage("status", "--run", run)
        self.assertEqual(status["merge_candidates"], [{"cids": [ONE, THREE], "reason": "同じチェッカーと関数"}])
        self.assertIn("merge --run", status["merge_command"])
        self.assertEqual(status["review_batches"], [{"slot": 1, "work_dir": (run / "work" / "w1").as_posix(),
                                                     "cids": [ONE, TWO, THREE]}])
        self.assertIn("review --run", status["next"][0])
        self.assertFalse(self.run_triage("merge", "--run", run, "--leader", ONE, "--members", "99999")["ok"])

        self.run_triage("review", "--run", run, "--cid", ONE, "--outcome", "pass")  # alone: not as the merged item
        merged = self.run_triage("merge", "--run", run, "--leader", ONE, "--members", THREE)
        self.assertEqual((merged["ok"], merged["members"]), (True, [THREE]))
        self.assertEqual((self.state(run, ONE)["members"], self.state(run, THREE)["merged_into"]), ([THREE], ONE))
        status = self.run_triage("status", "--run", run)
        self.assertEqual((status["merge_candidates"], status["review_batches"][0]["cids"]), ([], [ONE, TWO]))
        self.assertFalse(self.run_triage("review", "--run", run, "--cid", THREE, "--outcome", "pass")["ok"])
        member = self.next(run, "--cid", THREE)  # its result is the one of the leader
        self.assertEqual((member["cid"], member["message"]), (None, f"CID {THREE} は処置済みです"))
        self.assertFalse(self.run_triage("merge", "--run", run, "--leader", TWO, "--members", ONE)["ok"], "merged once")
        for cid in (ONE, TWO):
            self.assertTrue(self.run_triage("review", "--run", run, "--cid", cid, "--outcome", "pass")["ok"])
        done = self.run_triage("finish", "--run", run)
        self.assertTrue(done["ok"], done)
        self.assertEqual([row.split(" | ")[0] for row in self.summary(run).splitlines() if row.startswith("| 2")],
                         [f"| {ONE}（同じ原因: {THREE}）", f"| {TWO}"], "one row for the merged CIDs")

    def test_merge_with_rework_sends_the_leader_back(self):
        run = self.begin([issue(ONE), issue(TWO)], "--slots", 1, "--review")
        self.work(self.next(run, "--batch", 1))
        self.work(self.next(run, "--batch", 1), {"src/a.c": (b"return *p;", b"return p ? *p : 1;")})
        status = self.run_triage("status", "--run", run)
        self.assertEqual(status["merge_candidates"], [{"cids": [ONE, TWO], "reason": "修正が同じ行を変更"}])
        self.run_triage("merge", "--run", run, "--leader", ONE, "--members", TWO, "--rework",
                        "--point", "src/a.c:3 両方を解消する")
        given = self.next(run, "--batch", 2)
        self.assertEqual((given["cid"], given.get("members")), (ONE, [TWO]), "the CIDs the one fix has to cover")
        self.assertIn(f"CID {TWO} と同じ原因です。すべての CID の警告を解消する修正", given["rework"]["points"][0])
        self.assertEqual(given["rework"]["points"][1:], ["src/a.c:3 両方を解消する"])
        self.assertTrue(self.work(given, {}, with_fix=True)["ok"], "the earlier fix was applied and is kept")
        self.assertEqual(self.state(run, ONE)["members"], [TWO])

    def test_a_leader_that_fails_gives_its_members_back(self):
        run = self.begin([issue(ONE), issue(TWO)], "--slots", 1, "--review")
        for _ in (ONE, TWO):
            self.work(self.next(run, "--batch", 1))
        self.run_triage("merge", "--run", run, "--leader", ONE, "--members", TWO, "--rework")
        for _ in range(3):  # the leader is handed out twice without a save: failed
            last = self.next(run, "--batch", 2)
        self.assertEqual((last["cid"], self.state(run, ONE)["status"]), (None, "failed"))
        self.assertTrue("members" not in self.state(run, ONE) and "merged_into" not in self.state(run, TWO))
        self.assertTrue(self.run_triage("review", "--run", run, "--cid", TWO, "--outcome", "pass")["ok"])
        self.assertTrue(self.run_triage("finish", "--run", run)["ok"])
        for row in (f"| {ONE} | NULL_RETURNS | src/a.c:3 | 失敗 |", f"| {TWO} | NULL_RETURNS | src/a.c:3 | 本物のバグ |"):
            self.assertIn(row, self.summary(run), "every fetched CID keeps its row")

    def test_finish_refuses_unfinished_work_and_writes_both_outputs(self):
        run = self.begin(THREE_ISSUES, "--slots", 1, "--review")
        refused = self.run_triage("finish", "--run", run)
        self.assertEqual((refused["ok"], refused["pending"]), (False, [ONE, TWO, THREE]))
        self.assertEqual(refused["error"], "未処置の CID が 3 件あります")
        self.assertIn("next --run", refused["next"][0])
        self.work(self.next(run, "--batch", 1))
        self.work(self.next(run, "--batch", 1), {})  # no fix: the reason why
        self.next(run, "--batch", 1)
        self.next(run, "--batch", 1)
        self.assertEqual(self.next(run, "--batch", 1)["cid"], None)  # THREE failed: handed out twice without a save
        refused = self.run_triage("finish", "--run", run)
        self.assertEqual((refused["ok"], refused["unreviewed"]), (False, [ONE, TWO]))
        self.assertEqual(refused["error"], "検証されていない CID が 2 件あります")
        self.assertFalse((run / "summary.md").exists())
        self.assertEqual(self.start("--slots", 1, "--review")["run_dir"], run.as_posix(), "not reviewed: continued")
        self.run_triage("review", "--run", run, "--cid", ONE, "--outcome", "pass")
        self.run_triage("review", "--run", run, "--cid", TWO, "--outcome", "unverified")

        done = self.run_triage("finish", "--run", run)
        self.assertEqual((done["ok"], done["counts"]["done"], done["counts"]["failed"], done["next"]), (True, 2, 1, []))
        self.assertEqual(done["summary_file"], (run / "summary.md").as_posix())
        self.assertNotIn("table", done, "the one table of the chat is the one the person answers OK to")
        summary = self.summary(run)
        one, two, three = [summary.split(f"## CID {cid}（")[1].split("## CID")[0] for cid in (ONE, TWO, THREE)]
        for section, comment in ((one, support.FIX_COMMENT), (two, support.KEEP_COMMENT)):  # both outputs, always
            self.assertIn("### 修正コード", section)
            self.assertIn("### 逸脱コメント\n\n" + comment, section)
        self.assertIn("NULL のときは 0 を返します\n\n````diff", one)
        self.assertIn("+    return p ? *p : 0;", one)
        self.assertIn("Classification: Bug／Action: Ignore／Severity: Major", one)
        self.assertIn("- 判定: 本物のバグ（確信度: 高）\n- 推奨: 修正を取り込む", one)
        self.assertIn("修正コードはありません。理由: コードに問題がないため", two)
        self.assertIn("処置できませんでした: 処置を2回開始しましたが", three)
        self.assertIn("| CID | チェッカー | 場所 | 判定 | 推奨 | 確信度 | 検証 | 修正コード | 逸脱コメント |", summary)
        self.assertIn(f"| {ONE} | NULL_RETURNS | src/a.c:3 | 本物のバグ | 修正を取り込む | 高 | 合格 | src/a.c | "
                      f"{support.FIX_COMMENT} |", summary)
        self.assertIn(f"| 誤検知 | 逸脱コメントを登録する | 高 | 未検証 | なし | {support.KEEP_COMMENT} |", summary)
        self.assertIn(f"| {THREE} | NULL_RETURNS | src/a.c:3 | 失敗 | - | - | - | - | - |", summary)
        self.assertFalse((run / "work").exists(), "the work copies are removed")
        again = self.start("--slots", 1)
        self.assertEqual((again["run_dir"], again["message"]), (run.as_posix(), (
            "前回の処置は完了しています（処置済み 2 件、失敗 1 件）。取得し直すときは「取得し直して」と入力してください")))
        (run / "written.json").write_text("{}")  # the deviation comments were registered after the OK
        self.assertNotEqual(self.start("--slots", 1)["run_dir"], run.as_posix(), "handled: the next request fetches")

    def test_a_run_stays_the_answer_until_its_results_are_handled(self):
        run = self.begin(THREE_ISSUES)  # in a chat first: one CID is saved and the summary is shown
        self.work(self.next(run, "--cid", ONE))
        self.assertTrue(self.run_triage("finish", "--run", run)["ok"] and (run / "summary.md").is_file())
        agents = ("--slots", 2, "--review")
        self.assertEqual(self.start(*agents)["run_dir"], run.as_posix())
        self.work(self.next(run, "--slot", 1, "--batch", 1), B_EDIT)
        self.work(self.next(run, "--slot", 2, "--batch", 1))
        self.run_triage("review", "--run", run, "--cid", ONE, "--outcome", "pass")
        again = self.start(*agents)  # stopped while the results are reviewed
        self.assertEqual((again["run_dir"], again["fetch"]), (run.as_posix(), []), "no new folder, no new fetch")
        self.assertIn("review --run", again["next"][0])
        self.assertTrue((run / "work" / "w2").is_dir(), "the work copies stay")
        for cid in (TWO, THREE):
            self.run_triage("review", "--run", run, "--cid", cid, "--outcome", "pass")
        complete = "前回の処置は完了しています（処置済み 3 件）。取得し直すときは「取得し直して」と入力してください"
        for flags in (agents, ()):  # the agents before finish; then a request in a chat, after finish
            again = self.start(*flags)
            self.assertEqual((again["run_dir"], again["message"], again["counts"]["done"]),
                             (run.as_posix(), complete, 3))
            self.assertTrue(again["next"][0].endswith(f'finish --run "{run.as_posix()}"'), again["next"])
            self.assertIn(f"| {THREE} |", Path(self.follow(again["next"][0])["summary_file"]).read_text("utf-8"))
        self.assertFalse((run / "work").exists(), "no work copy is made to show the results again")
        (run / "applied.json").write_text("{}")  # the fixes were taken in after the OK
        fresh = self.start(*agents)
        self.assertNotEqual(fresh["run_dir"], run.as_posix())
        self.assertEqual(fresh["fetch"][0]["tool"], "coverity_search_issues", "handled: the next request fetches")

    def test_a_cid_is_made_again_after_finish(self):
        run = self.begin(THREE_ISSUES, "--slots", 1)
        self.work(self.next(run))
        self.work(self.next(run), B_EDIT)
        for _ in range(3):  # THREE is handed out twice without a save: failed
            self.next(run)
        self.assertEqual(self.run_triage("finish", "--run", run)["counts"]["failed"], 1)
        self.assertFalse((run / "work").exists())
        for cid in (TWO, THREE):  # the person reads the table: a remark on a result, and the failed CID once more
            asked = self.next(run, "--cid", cid)
            self.assertEqual((asked["ok"], asked["error"][:12]), (False, "作業用コピーがありません"))
        self.assertEqual([self.state(run, cid)["status"] for cid in (ONE, TWO, THREE)], ["done", "rework", "rework"])
        self.assertEqual((self.state(run, TWO)["rework"], "rework" in self.state(run, THREE)), ([], False))
        ready = self.follow(asked["next"][0])  # the start line: the folder is continued, the copy is made again
        self.assertEqual((ready["run_dir"], ready["message"]), (run.as_posix(), "前回の続きから進めます（残り 2 件）"))
        given = self.follow(ready["next"][0])
        self.assertEqual((given["cid"], given["rework"]), (TWO, {"points": [], "patch_applied": True}))
        self.assertIn(b"return 1;", (Path(given["work_dir"]) / "src" / "b.c").read_bytes(), "the earlier fix")
        self.assertTrue(self.work(given, {"src/b.c": (b"return 1;", b"return 2;")})["ok"])
        self.assertIn(b"+    return 2;", (run / "cid" / str(TWO) / "fix.patch").read_bytes())
        self.assertIn("+    return 2;", self.summary(run), "the summary that finish wrote follows the new result")
        failed = self.next(run)
        self.assertEqual((failed["cid"], "rework" in failed, self.work(failed)["ok"]), (THREE, False, True))
        self.assertEqual(self.run_triage("finish", "--run", run)["counts"]["done"], 3)

    def test_one_named_cid_in_a_chat(self):
        run = self.begin(THREE_ISSUES)  # no --slots: stage 1
        self.assertFalse(self.next(run, "--cid", 99999)["ok"])
        given = self.next(run, "--cid", TWO)
        self.assertEqual(given["cid"], TWO)
        self.assertTrue(given["next"][0].endswith(f"save --run \"{run.as_posix()}\" --cid {TWO} --slot 1"))
        self.assertNotIn("message", given)
        self.assertEqual(self.start()["message"], "前回の続きから進めます（残り 3 件）", "nothing was fetched now")
        again = [self.next(run, "--cid", TWO) for _ in range(4)]  # a person asks again and again: never refused
        self.assertEqual([reply["cid"] for reply in again], [TWO] * 4)
        self.assertIn("もう一度処置します", again[0]["message"])
        given = again[-1]
        self.assertTrue(self.work(given, B_EDIT)["ok"])
        summary = self.summary(run)  # written by save
        self.assertIn(f"## CID {TWO}（DEADCODE、src/b.c:3）", summary)
        self.assertIn("| CID | チェッカー | 場所 | 判定 | 推奨 | 確信度 | 修正コード | 逸脱コメント |", summary)
        self.assertIn(f"| {ONE} | NULL_RETURNS | src/a.c:3 | 未処置 | - | - | - | - |", summary)
        self.assertNotIn("検証", summary)
        done = self.run_triage("finish", "--run", run)  # no loop to finish: it does not refuse
        self.assertEqual((done["ok"], done["counts"]["remaining"]), (True, 2))
        self.assertTrue((run / "work" / "w1").is_dir(), "the copy stays while CIDs are left")
        self.assertEqual(self.start()["run_dir"], run.as_posix())
        # the person has a remark on the saved result, and later one more: the CID is handed out again with
        # its earlier fix in the copy, as often as the person names it
        for old, new in ((b"return 1;", b"return 2;"), (b"return 2;", b"return 3;")):
            self.assertEqual(self.next(run, "--cid", TWO)["cid"], TWO)
            redo = self.next(run, "--cid", TWO)  # asked again before it was saved: still with the earlier fix
            self.assertEqual(redo["rework"], {"points": [], "patch_applied": True})
            self.assertTrue(self.work(redo, {"src/b.c": (old, new)})["ok"])
            self.assertIn(b"+    " + new, (run / "cid" / str(TWO) / "fix.patch").read_bytes())
        self.assertEqual((self.state(run, TWO)["status"], self.state(run, TWO).get("rework")), ("done", None))
        self.assertNotIn("検証", self.summary(run))

    def test_the_ok_for_one_cid_leaves_the_folder_open_for_the_others(self):
        run = self.begin(THREE_ISSUES[:2])  # in a chat: one CID, the OK, the next CID
        self.work(self.next(run, "--cid", ONE))
        (run / "applied.json").write_text("{}")  # the fix of ONE was taken in after the OK
        again = self.start()
        self.assertEqual((again["run_dir"], again.get("message")), (run.as_posix(), "前回の続きから進めます（残り 1 件）"))
        self.assertTrue(self.work(self.follow(again["next"][0]), B_EDIT)["ok"])
        self.assertNotEqual(self.start()["run_dir"], run.as_posix(), "no CID is left: the next request fetches")


class GitFlow(Flow, support.RunCase):
    kind = "git"

    def test_a_copy_inside_the_repository_never_stands_for_the_repository(self):
        self.configure("output_dir: out\n")  # the result folder lies inside the person's repository
        run = self.begin(THREE_ISSUES, "--slots", 1)
        given = self.next(run)
        support.write(self.repo, {"src/b.c": b"int b(void)\n{\n    return 42;\n}\n", "notes.txt": b"mine\n"})

        def person() -> tuple:  # what the person has: the commit, the changed and the new files
            return (support.call(*support.GIT, "rev-parse", "HEAD", cwd=self.repo), support.user_tree("git", self.repo),
                    (self.repo / "src" / "b.c").read_bytes(), (self.repo / "notes.txt").read_bytes())

        before = person()
        (Path(given["work_dir"]) / ".git").unlink()  # a damaged copy: git would use the repository above it
        refused = self.work(given)
        self.assertFalse(refused["ok"])
        self.assertIn("作業用コピーがありません", refused["error"])
        self.assertFalse((run / "cid" / str(ONE) / "fix.patch").exists())
        self.assertEqual(person(), before)
        # start makes the copy again; variables that point git at the person's repository change nothing
        self.env = {**os.environ, "GIT_DIR": str(self.repo / ".git"), "GIT_WORK_TREE": str(self.repo)}
        self.assertIn("next --run", self.start("--slots", 1)["next"][0])
        self.assertTrue(self.work(self.next(run))["ok"])
        self.assertEqual((self.state(run, ONE)["files"], person()), (["src/a.c"], before))


class SvnFlow(Flow, support.RunCase):
    kind = "svn"


class CleanupTest(support.RunCase):
    """Work that is done is not lost when the cleanup after it fails. In this process, so that it can fail."""

    def setUp(self):
        super().setUp()
        try:
            import triage
        except ImportError:
            self.skipTest("この Python に PyYAML がありません")
        self.triage = triage

    def test_a_work_folder_that_cannot_be_deleted_and_a_copy_that_cannot_be_reset(self):
        run = self.begin(THREE_ISSUES[:1], "--slots", 1)
        given = self.run_triage("next", "--run", run)
        Path(given["work_dir"], "src", "a.c").write_bytes(b"int a(int *p)\n{\n    return p ? *p : 0;\n}\n")
        Path(given["result_file"]).write_text(json.dumps(support.result(ONE), ensure_ascii=False), encoding="utf-8")
        args = argparse.Namespace(run=run, cid=ONE, slot=1)
        with mock.patch.object(vcs.Git, "reset", side_effect=vcs.VcsError("index.lock")):
            saved = self.triage.cmd_save(args)  # the result is stored before the copy is made clean
        self.assertEqual((saved["ok"], saved["files"], self.state(run, ONE)["status"]), (True, ["src/a.c"], "done"))
        held = vcs.VcsError("フォルダーを削除できませんでした。ほかのプログラムが使用している可能性があります: w1")
        with mock.patch.object(vcs, "remove_tree", side_effect=held):
            done = self.triage.cmd_finish(args)
            self.assertEqual((done["ok"], done["message"]), (True, f"作業用コピーが残っています。{held}"))
            self.assertIn(f"| {ONE} |", self.summary(run))
            other = self.triage.find_run(self.repo, self.triage.settings(self.repo), "git", True)  # 取得し直して
        self.assertTrue(other != run and other.is_dir() and (run / "work").is_dir(), "the next fetch is not stopped")


class EvalCasesTest(unittest.TestCase):

    def test_the_scaffold_puts_every_case_on_the_path_of_a_real_request(self):
        if not shutil.which("uv"):
            self.skipTest("uv がありません")
        evals = support.SCRIPTS.parent / "evals"
        cases = sorted(p for p in evals.iterdir() if p.is_dir())
        self.assertEqual(len(cases), 2)
        for case in cases:
            top = Path(tempfile.mkdtemp(prefix="triage-test-")).resolve()
            self.addCleanup(vcs.remove_tree, top)
            run = support.call(sys.executable, evals / "scaffold.py", case, top / "work", cwd=top).decode().strip()
            cid = json.loads((case / "warning.json").read_text(encoding="utf-8"))["cid"]
            while "counts" not in support.triage("start", "--repo", top / "work"):
                pass
            given = support.triage("next", "--run", run, "--cid", cid)
            self.assertEqual((given["cid"], given["fetch"]), (cid, []), "no Coverity server is needed")
            self.assertTrue((Path(given["work_dir"]) / given["issue"]["file"]).is_file(), given["issue"])


class ErrorsTest(support.RunCase):

    def test_problems_the_person_can_fix_are_japanese_json(self):
        (self.repo / ".coverity-triage" / "config.yaml").unlink()
        out = self.run_triage("start", "--repo", self.repo)
        self.assertEqual(out["ok"], False)
        self.assertIn("設定ファイルがありません", out["error"])
        self.configure("parallel: many\n")
        self.assertIn("設定ファイルを読み取れません", self.run_triage("start", "--repo", self.repo)["error"])
        support.write(self.top, {".coverity-triage/config.yaml": b"coverity: {url: u, stream: s}\n"})
        self.assertIn("git または svn のリポジトリではありません", self.run_triage("start", "--repo", self.top)["error"])
        self.assertIn("引数が正しくありません", self.run_triage("next")["error"])
        missing = self.run_triage("status", "--run", self.top / "nowhere")
        self.assertEqual((missing["ok"], missing["run_dir"]), (False, (self.top / "nowhere").as_posix()))
        self.assertIn("結果フォルダーではありません", missing["error"])


if __name__ == "__main__":
    unittest.main()
