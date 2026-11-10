"""start, next, save, review, merge, status and finish on a git and on an svn repository."""

import json
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import support
from support import issue

ONE, TWO, THREE = 20001, 20002, 20003
THREE_ISSUES = [issue(ONE, function="a"), issue(TWO, "src/b.c", checker="DEADCODE"), issue(THREE, function="a")]
B_EDIT = {"src/b.c": (b"return 0;", b"return 1;")}


class Flow:
    """The tests; GitFlow and SvnFlow below run them."""

    def next(self, run, *args) -> dict:
        return self.run_triage("next", "--run", run, *args)

    def test_start_creates_the_run_folder_and_continues_it(self):
        first = self.start()
        run = Path(first["run_dir"])
        self.assertTrue(first["ok"] and first["need_fetch"] and not first["wait"])
        self.assertEqual(run.parent, self.top / "coverity-triage-out")
        self.assertEqual(first["fetch"], [{"tool": "coverity_search_issues", "arguments": {
            "repo_root": self.repo.as_posix(), "output_file": (run / "issues.json").as_posix()}}])
        self.assertIn(f'start --repo "{self.repo.as_posix()}"', first["next"][0])
        meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual({k: meta[k] for k in ("repo", "vcs", "stream", "slots", "review", "all")},
                         {"repo": self.repo.as_posix(), "vcs": self.kind, "stream": "app-main", "slots": 1,
                          "review": False, "all": False})
        self.assertTrue(meta["base_revision"] and (run / "work" / "w1" / "src" / "a.c").is_file())
        self.assertEqual(self.start()["run_dir"], first["run_dir"], "a folder without the list is continued")
        early = self.next(run)
        self.assertEqual((early["ok"], early["error"][:13]), (False, "警告の一覧がまだありません"))
        self.assertIn("start --repo", early["next"][0])

        (run / "issues.json").write_text(json.dumps({"issues": THREE_ISSUES, "total_matched": 137, "limited": True}))
        again = self.start("--slots", 1)
        self.assertEqual((again["run_dir"], again["counts"]["total"], again["counts"]["pending"]), (first["run_dir"], 3, 3))
        self.assertIn("条件に合う警告は 137 件あります。設定の上限（3 件）まで取得しました", again["message"])
        self.assertEqual(again["fetch"], [{"tool": "coverity_get_issue_details", "arguments": {  # the warning paths
            "repo_root": self.repo.as_posix(), "cids": [ONE, TWO, THREE], "output_dir": (run / "cid").as_posix()}}])
        self.assertIn(f'next --run "{run.as_posix()}" --slot 1', again["next"][0])
        self.assertTrue(json.loads((run / "run.json").read_text(encoding="utf-8"))["all"])
        self.work(self.next(run))
        self.assertIn("前回の続きから進めます（残り 2 件）", self.start("--slots", 1)["message"])

        other = self.start("--new")
        self.assertNotEqual(other["run_dir"], first["run_dir"])
        self.assertTrue(other["need_fetch"])
        self.assertFalse((run / "work").exists(), "the copies of the earlier run are removed")

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
                "tool": "coverity_get_issue_details", "arguments": {"repo_root": self.repo.as_posix(), "cids": [given["cid"]],
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
        self.assertEqual((run / "work" / "w1" / "src" / "a.c").read_bytes(), support.FILES["src/a.c"], "the copy is clean again")
        self.assertEqual(len((run / "log.txt").read_text(encoding="utf-8").splitlines()), 9, "one line per command")

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
        self.assertIn("--batch 2", self.run_triage("status", "--run", run)["next"][0])
        self.assertEqual(self.next(run, "--slot", 1, "--batch", 2)["cid"], 30006)

    def test_a_dead_claim_is_released(self):
        run = self.begin(THREE_ISSUES, "--slots", 2)
        self.assertEqual(self.next(run, "--slot", 1)["cid"], ONE)
        again = self.next(run, "--slot", 1)  # the slot asks again without a save: its claim was lost
        self.assertEqual(again["cid"], ONE)
        self.assertIn("もう一度", again["message"])
        self.assertEqual(self.next(run, "--slot", 1)["cid"], TWO, "handed out twice without a save: failed")
        failed = self.state(run, ONE)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("2回", failed["error"])

        state_file = run / "cid" / str(TWO) / "state.json"
        self.assertEqual(self.next(run, "--slot", 2)["cid"], THREE, "a fresh claim of another slot is kept")
        old = {**self.state(run, TWO), "claimed_at": (datetime.now() - timedelta(minutes=31)).isoformat(timespec="seconds")}
        state_file.write_text(json.dumps(old), encoding="utf-8")
        taken = self.next(run, "--slot", 2)  # older than 30 minutes: any slot takes it (its own THREE comes first)
        self.assertEqual(taken["cid"], THREE)
        self.work(taken)
        taken = self.next(run, "--slot", 2)
        self.assertEqual((taken["cid"], self.state(run, TWO)["slot"]), (TWO, 2))
        late = self.work({**taken, "slot": 1}, B_EDIT)  # the slot that lost the claim cannot save any more
        self.assertFalse(late["ok"])
        self.assertIn("スロット 1 が処置中の CID ではありません", late["error"])
        status = self.run_triage("status", "--run", run)
        self.assertEqual(status["counts"], {"total": 3, "pending": 0, "working": 1, "done": 1, "failed": 1, "remaining": 1})
        self.assertEqual(status["in_progress"], [{"slot": 2, "cid": TWO, "minutes": 0}])

    def test_save_rejects_a_bad_result_and_a_bad_comment(self):
        run = self.begin(THREE_ISSUES, "--slots", 1)
        given = self.next(run)
        cid_dir = run / "cid" / str(ONE)
        missing = self.run_triage("save", "--run", run, "--cid", ONE)
        self.assertFalse(missing["ok"])
        self.assertIn("result.json がありません", missing["problems"][0])
        bad_comment = {"comment": "おそらく問題ない。" + "あ" * 200, "classification": "Bug", "action": "Ignore", "severity": "Minor"}
        bad = self.work(given, evidence=[], confidence="certain", deviation=bad_comment)
        problems = "\n".join(bad["problems"])
        self.assertFalse(bad["ok"])
        for expected in ("evidence: 根拠を1件以上", "confidence:", "deviation.comment: 200 文字以内", "「おそらく」",
                         "のいずれかで始めてください"):
            self.assertIn(expected, problems)
        self.assertIn(f"save --run \"{run.as_posix()}\" --cid {ONE}", bad["next"][0])
        self.assertEqual(self.state(run, ONE)["status"], "working")
        self.assertFalse((cid_dir / "fix.patch").exists())
        self.assertIn("return p ? *p : 0;", (Path(given["work_dir"]) / "src" / "a.c").read_text(), "the work is kept")

        elsewhere = self.work(given, {}, fix={"summary": "直しました"}, no_fix_reason=None,
                              evidence=[{"file": "/build/src/a.c", "line": 3, "note": "x"}])
        self.assertIn("work_dir のファイルが変更されていますが、fix がありません", "\n".join(self.work(given, {})["problems"]))
        self.assertIn("evidence[0].file: /build/src/a.c が work_dir にありません", "\n".join(elsewhere["problems"]))
        self.assertTrue(self.work(given)["ok"])
        unchanged = self.work(self.next(run), {}, fix={"summary": "直しました"}, no_fix_reason=None)
        self.assertIn("work_dir のファイルが変更されていません", "\n".join(unchanged["problems"]))

    def test_patch_keeps_shift_jis_and_crlf(self):
        run = self.begin([issue(ONE), issue(TWO, "src/sjis.c", 4)], "--slots", 1)
        given = self.next(run, "--cid", TWO)
        new_line = "    return 1; // 修正\r\n".encode("cp932")
        saved = self.work(given, {"src/sjis.c": (b"    return 0;\r\n", new_line), "src/new.c": "// 新規\r\nint n;\r\n".encode("cp932")},
                          evidence=[{"file": "src/sjis.c", "line": 4, "note": "戻り値が違います"}])
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["files"], ["src/new.c", "src/sjis.c"])
        patch = Path(saved["patch_file"])
        self.assertIn(b"+" + new_line, patch.read_bytes())
        self.assertIn("// 日本語のコメント\r\n".encode("cp932"), patch.read_bytes())
        copy = support.fresh_copy(self.kind, self.top, self.repo)
        support.apply_patch(self.kind, copy, patch)
        self.assertEqual((copy / "src" / "sjis.c").read_bytes(), support.SJIS.replace(b"    return 0;\r\n", new_line))
        self.assertEqual((copy / "src" / "new.c").read_bytes(), "// 新規\r\nint n;\r\n".encode("cp932"))
        self.assertEqual((self.repo / "src" / "sjis.c").read_bytes(), support.SJIS)

    def test_rework_after_review_once(self):
        run = self.begin(THREE_ISSUES[:1], "--slots", 1, "--review")
        self.work(self.next(run, "--batch", 1))
        refused = self.run_triage("review", "--run", run, "--cid", ONE, "--outcome", "flag")
        self.assertFalse(refused["ok"])
        self.assertIn("1件以上", refused["problems"][0])
        point = "src/a.c:3 NULL のときに 0 を返すと、呼び出し元がエラーを検出できない"
        sent = self.run_triage("review", "--run", run, "--cid", ONE, "--outcome", "rework", "--point", point, "--cap", "medium")
        self.assertEqual((sent["outcome"], sent["confidence"]), ("rework", "medium"))
        state = self.state(run, ONE)
        self.assertEqual((state["status"], state["reworked"], state["rework"]), ("rework", True, [point]))
        self.assertEqual(self.run_triage("status", "--run", run)["counts"]["pending"], 1)

        given = self.next(run, "--batch", 2)
        self.assertEqual(given["rework"], {"points": [point], "patch_applied": True})
        self.assertIn(b"return p ? *p : 0;", (Path(given["work_dir"]) / "src" / "a.c").read_bytes(), "the earlier fix is in the copy")
        self.assertTrue(self.work(given, {"src/a.c": (b"return p ? *p : 0;", b"return p ? *p : -1;")})["ok"])
        self.assertIn(b"+    return p ? *p : -1;", (run / "cid" / str(ONE) / "fix.patch").read_bytes())
        self.assertNotIn("review", self.state(run, ONE), "the new result needs a new review")
        self.assertFalse(self.run_triage("finish", "--run", run)["ok"])

        second = self.run_triage("review", "--run", run, "--cid", ONE, "--outcome", "rework", "--point", point, "--cap", "low")
        self.assertEqual((second["outcome"], second["confidence"]), ("flag", "low"), "a second rework is stored as 要確認")
        review = json.loads((run / "cid" / str(ONE) / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review, {"outcome": "flag", "points": [point], "confidence_cap": "low", "round": 2})
        self.assertEqual(self.state(run, ONE)["status"], "done")
        done = self.run_triage("finish", "--run", run)
        self.assertTrue(done["ok"], done)
        self.assertIn("| 低 | 要確認 |", done["table"])

    def test_merge_of_cids_with_one_cause(self):
        run = self.begin(THREE_ISSUES, "--slots", 1, "--review")
        for cid in (ONE, TWO, THREE):
            self.work(self.next(run, "--batch", 1), B_EDIT if cid == TWO else None)
        status = self.run_triage("status", "--run", run)
        self.assertEqual(status["merge_candidates"], [{"cids": [ONE, THREE], "reason": "同じチェッカーと関数"}])
        self.assertIn("merge --run", status["merge_command"])
        self.assertEqual(status["review_batches"], [{"slot": 1, "work_dir": (run / "work" / "w1").as_posix(), "cids": [ONE, TWO, THREE]}])
        self.assertIn("review --run", status["next"][0])
        self.assertFalse(self.run_triage("merge", "--run", run, "--leader", ONE, "--members", "99999")["ok"])

        merged = self.run_triage("merge", "--run", run, "--leader", ONE, "--members", THREE)
        self.assertEqual((merged["ok"], merged["members"]), (True, [THREE]))
        self.assertEqual((self.state(run, ONE)["members"], self.state(run, THREE)["merged_into"]), ([THREE], ONE))
        status = self.run_triage("status", "--run", run)
        self.assertEqual((status["merge_candidates"], status["review_batches"][0]["cids"]), ([], [ONE, TWO]))
        self.assertFalse(self.run_triage("review", "--run", run, "--cid", THREE, "--outcome", "pass")["ok"])
        self.assertFalse(self.run_triage("merge", "--run", run, "--leader", TWO, "--members", str(ONE))["ok"], "merged once")
        for cid in (ONE, TWO):
            self.assertTrue(self.run_triage("review", "--run", run, "--cid", cid, "--outcome", "pass")["ok"])
        done = self.run_triage("finish", "--run", run)
        self.assertTrue(done["ok"], done)
        self.assertIn(f"| {ONE}（同じ原因: {THREE}） |", done["table"])
        self.assertEqual(done["table"].count("\n"), 3, "one row for the merged CIDs")

    def test_merge_with_rework_sends_the_leader_back(self):
        run = self.begin([issue(ONE), issue(TWO)], "--slots", 1, "--review")
        self.work(self.next(run, "--batch", 1))
        self.work(self.next(run, "--batch", 1), {"src/a.c": (b"return *p;", b"return p ? *p : 1;")})
        status = self.run_triage("status", "--run", run)
        self.assertEqual(status["merge_candidates"], [{"cids": [ONE, TWO], "reason": "修正が同じ行を変更"}])
        self.run_triage("merge", "--run", run, "--leader", ONE, "--members", TWO, "--rework", "--point", "src/a.c:3 両方を解消する")
        given = self.next(run, "--batch", 2)
        self.assertEqual(given["cid"], ONE)
        self.assertIn(f"CID {TWO} と同じ原因です", given["rework"]["points"][0])
        self.assertEqual(given["rework"]["points"][1:], ["src/a.c:3 両方を解消する"])
        self.assertTrue(self.work(given, {}, with_fix=True)["ok"], "the earlier fix was applied and is kept")
        self.assertEqual(self.state(run, ONE)["members"], [TWO])

    def test_finish_refuses_unfinished_work_and_writes_both_outputs(self):
        run = self.begin(THREE_ISSUES, "--slots", 1, "--review")
        refused = self.run_triage("finish", "--run", run)
        self.assertEqual((refused["ok"], refused["pending"]), (False, [ONE, TWO, THREE]))
        self.assertIn("未処置の CID が 3 件あります", refused["error"])
        self.assertIn("next --run", refused["next"][0])
        self.work(self.next(run, "--batch", 1))
        self.work(self.next(run, "--batch", 1), {})  # no fix: the reason why
        self.next(run, "--batch", 1)
        self.next(run, "--batch", 1)
        self.assertEqual(self.next(run, "--batch", 1)["cid"], None)  # THREE failed: handed out twice without a save
        refused = self.run_triage("finish", "--run", run)
        self.assertEqual((refused["ok"], refused["unreviewed"]), (False, [ONE, TWO]))
        self.assertIn("検証されていない CID が 2 件あります", refused["error"])
        self.assertFalse((run / "summary.md").exists())
        self.run_triage("review", "--run", run, "--cid", ONE, "--outcome", "pass")
        self.run_triage("review", "--run", run, "--cid", TWO, "--outcome", "unverified")

        done = self.run_triage("finish", "--run", run)
        self.assertEqual((done["ok"], done["counts"]["done"], done["counts"]["failed"], done["next"]), (True, 2, 1, []))
        self.assertEqual(done["summary_file"], (run / "summary.md").as_posix())
        summary = (run / "summary.md").read_text(encoding="utf-8")
        self.assertIn(done["table"], summary)
        one, two, three = [summary.split(f"## CID {cid}（")[1].split("## CID")[0] for cid in (ONE, TWO, THREE)]
        for section, comment in ((one, support.FIX_COMMENT), (two, support.KEEP_COMMENT)):  # both outputs, always
            self.assertIn("### 修正コード", section)
            self.assertIn("### 逸脱コメント\n\n" + comment, section)
        self.assertIn("NULL のときは 0 を返します\n\n````diff", one)
        self.assertIn("+    return p ? *p : 0;", one)
        self.assertIn("Classification: Bug／Action: Ignore／Severity: Minor", one)
        self.assertIn("- 判定: 本物のバグ（確信度: 高）\n- 推奨: 修正を取り込む", one)
        self.assertIn("修正コードはありません。理由: コードに問題がないため", two)
        self.assertIn("処置できませんでした: 処置を2回開始しましたが", three)
        self.assertIn(f"| {ONE} | NULL_RETURNS | src/a.c:3 | 本物のバグ | 修正を取り込む | 高 | 合格 | - | src/a.c | {support.FIX_COMMENT} |", summary)
        self.assertIn(f"| 誤検知 | 逸脱コメントを登録する | 高 | 未検証 | - | なし | {support.KEEP_COMMENT} |", summary)
        self.assertIn(f"| {THREE} | NULL_RETURNS | src/a.c:3 | 失敗 |", summary)
        self.assertFalse((run / "work").exists(), "the work copies are removed")
        self.assertNotEqual(self.start("--slots", 1)["run_dir"], run.as_posix(), "a finished run is not continued")

    def test_one_named_cid_in_a_chat(self):
        run = self.begin(THREE_ISSUES)  # no --slots: stage 1
        self.assertFalse(self.next(run, "--cid", 99999)["ok"])
        given = self.next(run, "--cid", TWO)
        self.assertEqual((given["cid"], given["slot"]), (TWO, 1))
        self.assertTrue(self.work(given, B_EDIT)["ok"])
        summary = (run / "summary.md").read_text(encoding="utf-8")  # written by save
        self.assertIn(f"## CID {TWO}（DEADCODE、src/b.c:3）", summary)
        self.assertIn(f"| {ONE} | NULL_RETURNS | src/a.c:3 | 未処置 |", summary)
        again = self.next(run, "--cid", TWO)
        self.assertEqual((again["ok"], again["cid"]), (True, None))
        self.assertEqual(again["message"], f"CID {TWO} は処置済みです")
        done = self.run_triage("finish", "--run", run)  # no loop to finish: it does not refuse
        self.assertEqual((done["ok"], done["counts"]["remaining"]), (True, 2))
        self.assertTrue((run / "work" / "w1").is_dir(), "the copy stays while CIDs are left")
        self.assertEqual(self.start()["run_dir"], run.as_posix())


class GitFlow(Flow, support.RunCase):
    kind = "git"


class SvnFlow(Flow, support.RunCase):
    kind = "svn"


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
