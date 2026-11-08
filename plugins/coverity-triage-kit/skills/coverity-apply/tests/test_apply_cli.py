"""apply.py のコマンドの試験（REQ-104、REQ-106、REQ-508）。一時フォルダのリポジトリと bare の origin、
一括の実行が残す形の実行フォルダで動かす。"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from datetime import date
from pathlib import Path

from apply_testlib import (CONFIG, FIXED, READER, RUN_ID, SKILL, Fixture, TempDirCase, git, needs_git, needs_uv,
                           run_item, run_state, run_script, write_json)

FOUR = [
    {"id": "20001", "cids": [20001], "recommendation": "fix", "fix": "commit"},
    {"id": "20002", "cids": [20002], "recommendation": "deviation"},
    {"id": "20003", "cids": [20003], "recommendation": "fix", "fix": "patch",
     "files": {"src/other.c": "int other(void) { return 2; }\n"}},
    {"id": "G1", "cids": [20005, 20006], "recommendation": "deviation", "decision": "reject"},
]


def preview(run: Path) -> dict:
    code, out, _ = run_script("preview", "--run", str(run))
    assert code == 0, out
    return out


def apply_code(run: Path, token: str) -> tuple[int, dict]:
    code, out, _ = run_script("apply-code", "--run", str(run), "--token", token)
    return code, out


@needs_uv
@needs_git
class Runs(TempDirCase):
    def test_lists_runs_with_a_summary_newest_first(self) -> None:
        fx = Fixture(self.tmp)
        old = fx.make_run(FOUR[1:2], run_id="20261101-090000")
        fx.make_run(FOUR[1:2], run_id="20261102-090000", summary=False)
        new = fx.make_run(FOUR[1:2], run_id="20261103-090000")
        foreign = fx.make_run(FOUR[1:2], run_id="20261104-090000", run_format=1)
        (fx.out / "not-a-run").mkdir()
        code, out, _ = run_script("runs", "--repo", str(fx.repo / "src"))
        self.assertEqual(code, 0, out)
        self.assertEqual([r["run_id"] for r in out["runs"]], [foreign.name, new.name, old.name])
        self.assertTrue(out["runs"][0]["foreign"])
        self.assertEqual(out["latest"], str(new))
        self.assertEqual(out["without_summary"], 1)

    def test_no_settings(self) -> None:
        code, out, _ = run_script("runs", "--repo", str(self.tmp))
        self.assertEqual(code, 1)
        self.assertIn("設定ファイルがありません", out["error"])


@needs_uv
@needs_git
class Preview(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.fx = Fixture(self.tmp)

    def test_plan_counts_ids_and_token(self) -> None:
        run = self.fx.make_run(FOUR)
        out = preview(run)
        self.assertEqual(out["counts"], {"fix": 2, "deviation": 1, "reject": 1})
        self.assertEqual([i["id"] for i in out["items"]], ["20001", "20003", "20002", "G1"])
        plan_path = Path(out["plan_file"])
        self.assertEqual(plan_path, run / "apply-plan.json")
        raw = plan_path.read_bytes()
        self.assertEqual(out["confirmation_token"], hashlib.sha256(raw).hexdigest()[:12])
        plan = json.loads(raw)
        self.assertEqual(plan["run_id"], RUN_ID)
        self.assertEqual(plan["triage"], [{"id": "20002", "cids": [20002], "stream": "app-main",
                                           "classification": "False Positive", "action": "Ignore",
                                           "severity": "Unspecified",
                                           "comment": "誤検知。呼び出し元で path を検証している（src/main.c:10）。"}])
        fix = {c["id"]: c for c in plan["code"]}
        self.assertEqual(fix["20001"]["branch"], "coverity-fix/cid-20001")
        self.assertRegex(fix["20001"]["commit"], r"^[0-9a-f]{40}$")
        self.assertIsNone(fix["20003"]["commit"])
        self.assertTrue(Path(fix["20003"]["patch"]).is_absolute())
        self.assertEqual(plan["reject"], [{"id": "G1", "cids": [20005, 20006], "group": True}])
        self.assertTrue(any("push" in e for e in out["effects"]))
        self.assertEqual(self.fx.remote_branches().keys(), {"main"}, "preview で push した")

    def test_person_edit_of_the_deviation_is_used(self) -> None:
        edit = {"classification": "Intentional", "action": "Ignore", "severity": "Unspecified",
                "comment": "意図的。仕様で NULL を許す（docs/spec.md:3）。"}
        run = self.fx.make_run([{**FOUR[1], "edit": edit}])
        plan = json.loads(Path(preview(run)["plan_file"]).read_bytes())
        self.assertEqual(plan["triage"][0]["classification"], "Intentional")
        self.assertEqual(plan["triage"][0]["comment"], edit["comment"])

    def test_a_long_comment_of_the_person_is_told_before_consent(self) -> None:
        edit = {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                "comment": "誤検知。" + "呼び出し元で必ず確かめている。" * 15}
        run = self.fx.make_run([{**FOUR[1], "edit": edit}])
        out = preview(run)
        long = [e for e in out["effects"] if "200 文字を超えて" in e]
        self.assertEqual(len(long), 1, out["effects"])
        self.assertIn(f"20002（{len(edit['comment'])} 文字）", long[0])
        plan = json.loads(Path(out["plan_file"]).read_bytes())
        self.assertEqual(plan["triage"][0]["comment"], edit["comment"])  # the person's text, not cut
        self.assertFalse(any("200 文字" in e for e in preview(self.fx.make_run(FOUR[1:2], run_id="20261108-101501"))["effects"]))

    def test_the_stream_is_the_one_of_run_json(self) -> None:
        run = self.fx.make_run(FOUR[1:2])
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        state["stream"] = "app-release"
        write_json(run / "run.json", state)
        write_json(run / "filter.json", {"name": "untriaged", "streams": ["app-main"]})  # not read
        plan = json.loads(Path(preview(run)["plan_file"]).read_bytes())
        self.assertEqual(plan["triage"][0]["stream"], "app-release")

    def test_fix_without_a_patch_is_a_problem_and_nothing_is_written(self) -> None:
        run = self.fx.make_run([{"id": "20009", "cids": [20009], "recommendation": "fix", "fix": None}])
        code, out, _ = run_script("preview", "--run", str(run))
        self.assertEqual(code, 1)
        self.assertIn("20009", out["problems"][0])
        self.assertFalse((run / "apply-plan.json").exists())

    def test_already_fixed_on_latest_takes_nothing_in(self) -> None:
        run = self.fx.make_run([{"id": "20010", "cids": [20010], "recommendation": "fix", "fix": None,
                                 "already_fixed": True}])
        out = preview(run)
        self.assertTrue(out["nothing_to_apply"])
        self.assertEqual(out["already_fixed_on_latest"], ["20010"])
        self.assertIsNone(out["confirmation_token"])

    def test_items_not_approved_or_not_done_are_left_out(self) -> None:
        run = self.fx.make_run([{**FOUR[1], "decision": None},
                                {"id": "20011", "cids": [20011], "recommendation": "deviation", "status": "error"}])
        out = preview(run)
        self.assertTrue(out["nothing_to_apply"])
        self.assertEqual(out["not_approved"], ["20002"])

    def test_refuses_runs_it_cannot_apply(self) -> None:
        cases = {"一覧": self.fx.make_run(FOUR[1:2], run_id="20261101-000000", summary=False),
                 "approvals.json": self.fx.make_run(FOUR[1:2], run_id="20261101-000001", approvals=False),
                 "別の形": self.fx.make_run(FOUR[1:2], run_id="20261101-000002", run_format=1)}
        for words, run in cases.items():
            with self.subTest(words=words):
                code, out, _ = run_script("preview", "--run", str(run))
                self.assertEqual(code, 1)
                self.assertIn(words, out["error"])
                self.assertFalse((run / "apply-plan.json").exists())


@needs_uv
@needs_git
class ApplyCode(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.fx = Fixture(self.tmp)

    def test_wrong_token_does_nothing(self) -> None:
        run = self.fx.make_run(FOUR)
        preview(run)
        code, out = apply_code(run, "000000000000")
        self.assertEqual(code, 1)
        self.assertIn("確認の文字列", out["error"])
        self.assertEqual(self.fx.remote_branches().keys(), {"main"})
        self.assertFalse((run / "apply-code.result.json").exists())
        self.assertFalse((self.fx.repo / ".coverity-triage" / "no-grouping.yaml").exists())

    def test_pushes_branches_from_commits_and_patches(self) -> None:
        run = self.fx.make_run(FOUR)
        out = preview(run)
        code, result = apply_code(run, out["confirmation_token"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["errors"], [])
        remote = self.fx.remote_branches()
        self.assertIn("coverity-fix/cid-20001", remote)
        self.assertIn("coverity-fix/cid-20003", remote)
        made = git(self.fx.repo, "show", "coverity-fix/cid-20003:src/other.c")
        self.assertEqual(made, "int other(void) { return 2; }")
        message = git(self.fx.repo, "log", "-1", "--format=%B", "coverity-fix/cid-20003")
        self.assertEqual(message, "Coverity CID 20003: 修正\n\nfopen の失敗を確かめる")
        pulls = {p["branch"]: p for p in result["pull_requests"]}
        self.assertEqual(pulls["coverity-fix/cid-20001"]["title"], "Coverity CID 20001: fopen の失敗を確かめる")
        self.assertEqual(pulls["coverity-fix/cid-20001"]["base"], "main")
        self.assertIn("## 見立て", pulls["coverity-fix/cid-20001"]["body"])
        self.assertIn("確信度: 高", pulls["coverity-fix/cid-20001"]["body"])
        self.assertIsNone(pulls["coverity-fix/cid-20001"]["compare_url"], "GitHub でないリモート")
        self.assertEqual(result["coverity"], {"registered": [], "not_registered": ["20002"]})
        self.assertEqual(result["rejected"], ["G1"])
        self.assertIn("まだ Coverity に書き戻していません", result["next"])
        self.assertEqual(git(self.fx.repo, "status", "--porcelain", "--", ".", ":!.coverity-triage/no-grouping.yaml"), "",
                         "作業中のファイルが変わった（却下したグループの CID を足す no-grouping.yaml のほか）")
        self.assertEqual((self.fx.repo / "src" / "reader.c").read_text(encoding="utf-8"), READER)

    def test_same_token_never_pushes_twice(self) -> None:
        run = self.fx.make_run(FOUR[:1])
        token = preview(run)["confirmation_token"]
        apply_code(run, token)
        git(self.fx.root, "--git-dir", str(self.fx.origin), "update-ref", "-d", "refs/heads/coverity-fix/cid-20001")
        code, again = apply_code(run, token)
        self.assertEqual(code, 0, again)
        self.assertTrue(again["repeated"])
        self.assertEqual(again["pull_requests"], [])
        self.assertEqual([p["branch"] for p in again["pushed_before"]], ["coverity-fix/cid-20001"])
        self.assertNotIn("coverity-fix/cid-20001", self.fx.remote_branches(), "同じ確認の文字列で push し直した")
        self.assertTrue(preview(run)["nothing_to_apply"], "反映した修正をもう一度計画に入れた")

    def test_reports_what_was_written_back_and_why_not(self) -> None:
        run = self.fx.make_run([FOUR[1], {**FOUR[1], "id": "20004", "cids": [20004]},
                                {**FOUR[1], "id": "20007", "cids": [20007]}])
        out = preview(run)
        token = out["confirmation_token"]
        write_json(run / "apply-plan.result.json", {
            "written": {"k1": {"id": "20002", "cids": [20002], "stream": "app-main", "triage_store": "TS", "at": "x"}},
            "failed": {"k2": {"id": "20004", "cids": [20004], "error": "Coverity REST PUT が失敗しました（400）: bad",
                              "token": token, "at": "x"},
                       "k3": {"id": "20007", "cids": [20007], "error": "Coverity REST PUT が失敗しました（503）",
                              "token": token, "at": "x"}},
            "requests": []})
        code, result = apply_code(run, token)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["coverity"]["registered"], ["20002"])
        self.assertEqual(result["coverity"]["not_registered"], ["20004", "20007"])
        self.assertIn("断りました", result["next"])
        self.assertIn("一時的な失敗", result["next"])
        again = preview(run)
        self.assertEqual([i["id"] for i in again["items"]], ["20004", "20007"], "書き戻したものをまた計画に入れた")

    def test_partial_failure_then_the_rest_on_the_next_apply(self) -> None:
        run = self.fx.make_run(FOUR[:1] + [FOUR[2]])
        # someone pushed another change to the branch name the patch fix would use
        git(self.fx.repo, "branch", "coverity-fix/cid-20003", "main")
        token = preview(run)["confirmation_token"]
        code, result = apply_code(run, token)
        self.assertEqual(code, 0, result)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("20003", result["errors"][0])
        self.assertIn("coverity-fix/cid-20001", self.fx.remote_branches())
        git(self.fx.repo, "branch", "-D", "coverity-fix/cid-20003")
        second = preview(run)
        self.assertEqual([i["id"] for i in second["items"]], ["20003"], "残りだけを計画に入れる")
        code, result = apply_code(run, second["confirmation_token"])
        self.assertEqual(result["errors"], [])
        self.assertIn("coverity-fix/cid-20003", self.fx.remote_branches())

    def test_rejected_group_is_added_to_no_grouping_keeping_comments(self) -> None:
        path = self.fx.repo / ".coverity-triage" / "no-grouping.yaml"
        path.write_text("# チームで足した CID\ncids:\n- 20100   # マクロが別\n# ここまで\n", encoding="utf-8")
        run = self.fx.make_run([FOUR[3], {"id": "20012", "cids": [20012], "recommendation": "fix", "decision": "reject"}])
        token = preview(run)["confirmation_token"]
        code, result = apply_code(run, token)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["no_grouping_added"], [20005, 20006])
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "# チームで足した CID\ncids:\n- 20100   # マクロが別\n- 20005\n- 20006\n# ここまで\n")

    def test_broken_no_grouping_file_is_reported_and_retried(self) -> None:
        path = self.fx.repo / ".coverity-triage" / "no-grouping.yaml"
        path.write_text("cids: [20100\n", encoding="utf-8")
        run = self.fx.make_run([FOUR[0], FOUR[3]])
        code, result = apply_code(run, preview(run)["confirmation_token"])
        self.assertEqual(code, 0, result)
        self.assertIn("no-grouping.yaml", result["errors"][0])
        self.assertEqual(result["rejected"], [])
        self.assertIn("coverity-fix/cid-20001", self.fx.remote_branches())
        path.write_text("cids: [20100]\n", encoding="utf-8")
        again = preview(run)
        self.assertEqual([i["id"] for i in again["items"]], ["G1"], "push した修正をまた計画に入れた")
        _, result = apply_code(run, again["confirmation_token"])
        self.assertEqual(result["no_grouping_added"], [20005, 20006])

    def test_no_grouping_flow_list_and_empty_value(self) -> None:
        path = self.fx.repo / ".coverity-triage" / "no-grouping.yaml"
        for before, after in (("cids: [20100]   # 前から\n", "cids: [20100, 20005, 20006]   # 前から\n"),
                              ("# 空\ncids:\n", "# 空\ncids: [20005, 20006]\n")):
            with self.subTest(before=before):
                path.write_text(before, encoding="utf-8")
                shutil.rmtree(self.fx.out, ignore_errors=True)
                run = self.fx.make_run([FOUR[3]])
                apply_code(run, preview(run)["confirmation_token"])
                self.assertEqual(path.read_text(encoding="utf-8"), after)

    def test_compare_url_for_a_github_remote(self) -> None:
        git(self.fx.repo, "remote", "set-url", "origin", "https://github.com/acme/envmon.git")
        git(self.fx.repo, "remote", "set-url", "--push", "origin", str(self.fx.origin))
        git(self.fx.repo, "fetch", "-q", str(self.fx.origin), "main:refs/remotes/origin/main")
        run = self.fx.make_run(FOUR[:1])
        _, result = apply_code(run, preview(run)["confirmation_token"])
        url = result["pull_requests"][0]["compare_url"]
        self.assertTrue(url.startswith("https://github.com/acme/envmon/compare/main...coverity-fix/cid-20001?expand=1"
                                       "&title="), url)
        self.assertIn("&body=", url)

    def test_confidence_in_the_pull_request_never_above_the_review(self) -> None:
        review = {"item": "20001", "outcome": "flag", "findings": [], "confidence_cap": "medium", "rework": [],
                  "note": "要確認"}
        run = self.fx.make_run([{**FOUR[0], "review": review}])
        _, result = apply_code(run, preview(run)["confirmation_token"])
        self.assertIn("確信度: 中", result["pull_requests"][0]["body"])

    def test_confidence_in_the_pull_request_is_low_when_the_build_check_failed(self) -> None:
        run = self.fx.make_run(FOUR[:1])
        write_json(run / "verify.json", {"20001": {"mode": "build", "batch": True, "applied": True, "build_ok": False,
                                                    "problems": ["ビルドエラーが、この修正案の変えたファイルで出ています"]}})
        _, result = apply_code(run, preview(run)["confirmation_token"])
        self.assertIn("確信度: 低", result["pull_requests"][0]["body"])

    def test_one_branch_per_run(self) -> None:
        settings = self.fx.repo / ".coverity-triage" / "config.yaml"
        settings.write_text(settings.read_text(encoding="utf-8") + "options:\n  per_run_branch: true\n", encoding="utf-8")
        run = self.fx.make_run([{**FOUR[0], "branch": None}, FOUR[2]])
        out = preview(run)
        self.assertEqual([c["branch"] for c in json.loads(Path(out["plan_file"]).read_bytes())["code"]], [None, None])
        _, result = apply_code(run, out["confirmation_token"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["pull_requests"]), 1)
        pull = result["pull_requests"][0]
        self.assertEqual(pull["branch"], f"coverity-fix/run-{RUN_ID}")
        self.assertEqual(pull["title"], f"Coverity トリアージ {RUN_ID}（2 件）")
        self.assertEqual(git(self.fx.repo, "show", f"{pull['branch']}:src/reader.c") + "\n", FIXED)
        self.assertIn(pull["branch"], self.fx.remote_branches())

    def test_metrics_and_stats(self) -> None:
        settings = self.fx.repo / ".coverity-triage" / "config.yaml"
        settings.write_text(settings.read_text(encoding="utf-8") + "options:\n  metrics: true\n", encoding="utf-8")
        edit = {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                "comment": "誤検知。人が直した文。"}
        run = self.fx.make_run([FOUR[0], {**FOUR[1], "edit": edit}, FOUR[3]])
        apply_code(run, preview(run)["confirmation_token"])
        code, stats, _ = run_script("stats", "--repo", str(self.fx.repo))
        self.assertEqual(code, 0, stats)
        self.assertEqual(stats["items"], 3)
        self.assertEqual(stats["approvals"], {"fix": 1, "deviation": 1, "reject": 1})
        self.assertEqual(stats["recommendation_taken_rate"], round(2 / 3, 3))
        self.assertEqual(stats["deviation_edited_rate"], 1.0)
        self.assertEqual(stats["average_seconds_per_item"], 120.0)
        record = json.loads((run / "apply-code.result.json").read_text(encoding="utf-8"))
        self.assertEqual(record["metrics"]["20001"]["shown_confidence"], "high")
        # seconds may be null (not known): those items are left out of the average
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        state["items"][0]["seconds"] = None
        state["items"][1]["seconds"] = 60
        write_json(run / "run.json", state)
        _, stats, _ = run_script("stats", "--repo", str(self.fx.repo))
        self.assertEqual(stats["average_seconds_per_item"], 90.0)

    def test_svn_patch_goes_to_the_working_copy(self) -> None:
        if not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn がないので飛ばします")
        server = self.tmp / "svnrepo"
        subprocess.run(["svnadmin", "create", str(server)], check=True, capture_output=True)
        wc = self.tmp / "wc"
        subprocess.run(["svn", "checkout", "-q", server.as_uri(), str(wc)], check=True, capture_output=True)
        (wc / "src").mkdir()
        (wc / "src" / "reader.c").write_text(READER, encoding="utf-8")
        (wc / ".coverity-triage").mkdir()
        (wc / ".coverity-triage" / "config.yaml").write_text(
            CONFIG.replace("type: git", "type: svn").replace("OUT", (self.tmp / "svnout").as_posix()), encoding="utf-8")
        subprocess.run(["svn", "add", "-q", "src", ".coverity-triage"], cwd=wc, check=True, capture_output=True)
        subprocess.run(["svn", "commit", "-q", "-m", "first"], cwd=wc, check=True, capture_output=True)
        run_dir = self.tmp / "svnout" / RUN_ID
        patch = run_dir / "patches" / "20001-fix.patch"
        patch.parent.mkdir(parents=True)
        (wc / "src" / "reader.c").write_text(FIXED, encoding="utf-8")
        patch.write_bytes(subprocess.run(["svn", "diff"], cwd=wc, capture_output=True, check=True).stdout)
        subprocess.run(["svn", "revert", "-q", "-R", "."], cwd=wc, check=True)
        write_json(run_dir / "items" / "20001" / "result.json", json.loads(json.dumps(
            __import__("apply_testlib").result_of("20001", "fix"))))
        write_json(run_dir / "items" / "20001" / "saved.json",
                   {"fix": {"files": ["src/reader.c"], "patch": str(patch), "branch": None, "commit": None}})
        write_json(run_dir / "run.json", run_state(RUN_ID, wc, [run_item("20001", [20001])], stream="s"))
        (run_dir / "summary.html").write_text("x", encoding="utf-8")
        write_json(run_dir / "approvals.json", {"version": 1, "items": {"20001": {"decision": "fix", "edited": False}}})
        out = preview(run_dir)
        _, result = apply_code(run_dir, out["confirmation_token"])
        self.assertEqual(result["svn_applied"], ["20001"])
        self.assertEqual((wc / "src" / "reader.c").read_text(encoding="utf-8"), FIXED)


@needs_uv
@needs_git
class Knowledge(TempDirCase):
    def test_candidates_and_append(self) -> None:
        fx = Fixture(self.tmp)
        knowledge = fx.repo / ".coverity-triage" / "knowledge.md"
        knowledge.write_text("# 知識\n\n## 調査で使う事実\n\n- fatal() は戻らない\n", encoding="utf-8")
        run = fx.make_run([FOUR[0], {**FOUR[1], "decision": "fix", "fix": "commit", "cids": [20002]}, FOUR[3]])
        write_json(run / "items" / "20002" / "issue-20002.json",
                   {"fetched_at": "x", "issue": {"cid": 20002, "checker": "NULL_RETURNS", "file": "src/reader.c",
                                                 "function": "read_header", "line": 5}, "events": []})
        apply_code(run, preview(run)["confirmation_token"])
        code, out, _ = run_script("knowledge-candidates", "--run", str(run))
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(c["item"] for c in out["candidates"]), ["20002", "G1"])
        first = next(c for c in out["candidates"] if c["item"] == "20002")
        self.assertEqual((first["checker"], first["ai_recommendation"], first["person_decision"]),
                         ("NULL_RETURNS", "逸脱", "修正"))
        self.assertIn("fatal() は戻らない", out["current_knowledge"])
        code, out, _ = run_script("add-knowledge", "--run", str(run), "--entry", "read_header() の path は  検証済み",
                                  "--entry", "  ")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["added"], 1)
        text = knowledge.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 知識\n\n## 調査で使う事実\n\n- fatal() は戻らない\n"))
        self.assertTrue(text.endswith(f"\n## 追記（{date.today().isoformat()}、実行 {RUN_ID}）\n\n"
                                      "- read_header() の path は 検証済み\n"))
        code, out, _ = run_script("add-knowledge", "--run", str(run), "--stdin",
                                  stdin=json.dumps({"entries": ["二つ目の知識"]}, ensure_ascii=False))
        self.assertEqual(out["added"], 1)
        self.assertIn("- 二つ目の知識\n", knowledge.read_text(encoding="utf-8"))


@needs_uv
class Output(TempDirCase):
    def test_errors_are_one_json_object_without_traceback(self) -> None:
        for args in (("nosuch",), ("preview",), ("preview", "--run", str(self.tmp)),
                     ("apply-code", "--run", str(self.tmp), "--token", "x", "--secret=abc")):
            with self.subTest(args=args):
                code, out, err = run_script(*args)
                self.assertEqual(code, 1)
                self.assertFalse(out["ok"])
                self.assertNotIn("Traceback", err)
                self.assertNotIn("abc", json.dumps(out, ensure_ascii=False))

    def test_narrow_encoding_escapes_only_what_it_cannot_write(self) -> None:
        from apply_testlib import base_env
        code, out, _ = run_script("preview", "--run", str(self.tmp), env=base_env(PYTHONIOENCODING="ascii"))
        self.assertEqual(code, 1)
        self.assertIn("実行フォルダ", out["error"])


SCHEMAS = {"run.schema.json": "run.json", "approvals.schema.json": "approvals.json"}


@needs_uv
@needs_git
class SharedSchemas(TempDirCase):
    """一括の実行の取り決め（run.schema.json・approvals.schema.json）の写しがあれば、試験の実行フォルダが合うか確かめる"""

    def test_fixture_run_folder_matches_the_schemas(self) -> None:
        present = {name: SKILL / "assets" / name for name in SCHEMAS if (SKILL / "assets" / name).is_file()}
        if not present:
            self.skipTest("assets/run.schema.json・approvals.schema.json の写しがまだないので飛ばします")
        run = Fixture(self.tmp).make_run(FOUR)
        checker = ("import json,sys,jsonschema\n"
                   "schema=json.load(open(sys.argv[1],encoding='utf-8'));data=json.load(open(sys.argv[2],encoding='utf-8'))\n"
                   "jsonschema.validate(data,schema)\n")
        for name, schema in present.items():
            with self.subTest(schema=name):
                proc = subprocess.run([shutil.which("uv"), "run", "--native-tls", "--quiet", "--with", "jsonschema",
                                       "python", "-B", "-c", checker, str(schema), str(run / SCHEMAS[name])],
                                      capture_output=True, check=False, timeout=300)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-2000:])


if __name__ == "__main__":
    unittest.main()
