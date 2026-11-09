"""What the run leaves for people and other parts: the reports (C-25, C-26), approvals.json, run.json,
status, and the commands around a run (settings, runs, new-run checks, foreign runs, note-issue)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import unittest
from pathlib import Path

from support import RunCase, import_runlib, issue

ISSUES = [issue(20001, line=6), issue(20002, checker="RESOURCE_LEAK", function="open_file", line=12),
          issue(20003, checker="INTEGER_OVERFLOW", function="parse", line=21),
          issue(20004, checker="INTEGER_OVERFLOW", function="parse", line=22)]

STATUS_KEYS = {"ok", "run_id", "run_dir", "repo_root", "stage", "stage_label", "finished", "stopped", "halted",
               "halt_reason", "has_summary",
               "review", "integration", "max_rework", "parallel", "counts", "items", "running_tasks", "errors", "recent",
               "initial", "progress", "run_checks", "criteria", "criteria_results", "issues_found"}
ITEM_KEYS = {"id", "cids", "checker", "status", "phase", "note", "elapsed_seconds", "stalled", "preparing_copy",
             "attempts", "rework_rounds", "review", "recommendation", "confidence", "error"}


def validate(value, schema_name: str) -> list[str]:
    import_runlib()
    from runlib import schema

    return schema.validate(value, schema_name)


class FinishedRunTest(unittest.TestCase):
    """One finished run (markdown and metrics on), checked from several sides. The tests that change the run
    folder come last (the names are ordered)."""

    @classmethod
    def setUpClass(cls) -> None:
        class Helper(RunCase):
            settings = {"markdown": "true", "metrics": "true"}

        h = Helper("runTest")
        h.setUp()
        cls.addClassCleanup(h.doCleanups)
        cls.h = h
        cls.run_dir = h.new_run()["run_dir"]

        def worker(action):
            if action["item"] == "20002":
                return h.work_as_worker(action, "deviation", change=True, confidence="low",
                                        verdict={"judgement": "false_positive", "summary": "<script>alert(1)</script> 誤検知",
                                                 "rationale": "呼び出し元で閉じている & 問題ない",
                                                 "evidence": [{"file": "src/reader.c", "line": 12, "note": "<b>ここ</b>"}]})
            return h.work_as_worker(action)

        assert h.drive(cls.run_dir, ISSUES, worker=worker)["done"]
        cls.summary = h.run_json("summary", "--run", cls.run_dir)
        cls.folder = Path(cls.run_dir)

    def run_json(self, *args, **kwargs):
        return self.h.run_json(*args, **kwargs)

    def test_1_summary_html_layout(self) -> None:
        text = (self.folder / "summary.html").read_text(encoding="utf-8")
        self.assertRegex(text, r"<h1>トリアージ結果 \d{4}-\d{2}-\d{2} \d{2}:\d{2}</h1>")
        self.assertIn("条件: 分類がまだの警告 ／ 警告 4 件（作業 3、うちグループ 1）", text)
        labels = re.findall(r'<div class="label">(.*?)</div>', text)
        self.assertEqual(labels, ["警告", "作業", "修正の推奨", "逸脱の推奨", "確信度 低", "エラー", "未処理"])
        self.assertRegex(text, r"修正 2 ／ 逸脱 1 ／ 却下 0 ／ （空） 0 ／ 推奨から変えたもの 0")
        header = re.findall(r"<th>(.*?)</th>", re.search(r'<table class="items">.*?</thead>', text, re.S).group(0))
        self.assertEqual(header, ["承認", "ID", "推奨", "確信度", "見立て", "詳細"])
        order = re.findall(r'<td data-label="ID" class="id"><a href="cid/(.*?)\.html">(.*?)</a>', text)
        self.assertEqual(order[0][0], "20002")  # low confidence first
        self.assertIn(("G1", "G1（2 件）"), order)
        self.assertNotIn("<script>alert", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertNotIn("<script", text.lower().replace("&lt;script", ""))
        self.assertTrue((self.folder / "summary.md").is_file())
        md = (self.folder / "summary.md").read_text(encoding="utf-8")
        self.assertIn("| 承認 | ID | 推奨 | 確信度 | 見立て | 詳細 |", md)

    def test_2_item_report_layout(self) -> None:
        text = (self.folder / "cid" / "20001.html").read_text(encoding="utf-8")
        self.assertIn("<h1>20001 — NULL_RETURNS（High）</h1>", text)
        heads = re.findall(r"<h2>(\d\. [^<]*?)(?: <span|</h2>)", text)
        self.assertEqual(heads, ["1. 結論", "2. 警告の概要", "3. 真偽の根拠", "4. 案A: 逸脱", "5. 案B: 修正", "6. 処理情報"])
        self.assertIn('<pre class="diff">', text)
        up = os.pardir
        self.assertIn(f'href="{up}/patches/20001-fix.patch"', text)
        self.assertIn(f'href="{up}/fixed/20001/fix/src/reader.c"', text)
        self.assertIn(f'href="{up}/summary.html"', text)
        self.assertIn("coverity-fix/cid-20001", text)
        self.assertIn("★", text)
        self.assertNotIn("<script", text.lower())
        g1 = (self.folder / "cid" / "G1.html").read_text(encoding="utf-8")
        self.assertIn("<h1>G1（2 件） — INTEGER_OVERFLOW（High）</h1>", g1)
        escaped = (self.folder / "cid" / "20002.html").read_text(encoding="utf-8")
        self.assertIn("&lt;b&gt;ここ&lt;/b&gt;", escaped)
        self.assertIn("&amp; 問題ない", escaped)
        self.assertTrue((self.folder / "cid" / "20001.md").is_file())

    def test_8_approvals_defaults_keep_human_choices(self) -> None:
        path = self.folder / "approvals.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(validate(data, "approvals.schema.json"), [])
        self.assertEqual({k: v["decision"] for k, v in data["items"].items()},
                         {"20001": "fix", "20002": "deviation", "G1": "fix"})
        data["items"]["20001"] = {"decision": "reject", "edited": True, "updated_at": "2026-11-08T12:00:00"}
        data["items"]["20002"]["deviation"] = {"classification": "Intentional", "action": "Ignore",
                                               "severity": "Unspecified", "comment": "意図的。人が直した。"}
        del data["items"]["G1"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        again = self.run_json("summary", "--run", self.run_dir)
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["items"]["20001"]["decision"], "reject")  # a human's choice stays
        self.assertEqual(after["items"]["20002"]["deviation"]["comment"], "意図的。人が直した。")
        self.assertEqual(after["items"]["G1"]["decision"], "fix")  # a missing key is added again
        self.assertEqual(again["approvals_counts"]["reject"], 1)
        self.assertEqual(again["approvals_counts"]["changed"], 1)
        report = (self.folder / "cid" / "20002.html").read_text(encoding="utf-8")
        self.assertIn("意図的。人が直した。", report)
        self.assertIn("人が直した内容", report)

    def test_3_run_json_and_status_shapes(self) -> None:
        state = json.loads((self.folder / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(validate(state, "run.schema.json"), [])
        self.assertEqual(state["format"], 2)
        self.assertEqual(state["tool_version"], "1")
        self.assertEqual((state["halted"], state["halt_reason"]), (False, None))
        self.assertTrue(all("applied" not in i for i in state["items"]))  # what was applied is not the run's record
        import_runlib()
        from runlib import schema

        job_schema = {"$defs": schema.load("run.schema.json")["$defs"], "$ref": "#/$defs/job"}  # jobs/<name>.json
        jobs = sorted((self.folder / "jobs").glob("*.json"))
        self.assertTrue(jobs)
        for path in jobs:
            self.assertEqual(schema.Validator(job_schema).errors(json.loads(path.read_text(encoding="utf-8"))), [], path.name)
        broken = {"name": "x", "kind": "copy", "status": "lost", "pid": "1", "started": "t", "args": {}}
        self.assertTrue(schema.Validator(job_schema).errors(broken))
        status = self.run_json("status", "--run", self.run_dir)
        self.assertEqual(set(status) - {"stale_seconds"}, STATUS_KEYS)
        self.assertEqual(set(status["counts"]), {"pending", "in_progress", "done", "error", "merged", "total"})
        for item in status["items"]:
            self.assertEqual(set(item), ITEM_KEYS)
        self.assertEqual(set(status["initial"]), {"used", "step", "value", "max", "label"})
        self.assertEqual(status["progress"], {"value": 3, "max": 3, "label": "3 / 3"})
        self.assertTrue(status["recent"][0][:8].count(":") == 2)
        self.assertEqual(len(status["criteria"]), 28)
        self.assertEqual({c["id"] for c in status["criteria"] if c["scope"] == "run"},
                         {"C-16", "C-17", "C-18", "C-19", "C-24", "C-25", "C-26", "C-27"})
        self.assertTrue(all({"id", "scope", "passed", "failed", "not_applicable", "items_failed"} == set(c)
                            for c in status["criteria_results"]))
        self.assertEqual({c["id"] for c in status["run_checks"]}, {"C-16", "C-17", "C-18", "C-19", "C-24", "C-25", "C-26", "C-27"})
        low = next(i for i in status["items"] if i["id"] == "20002")
        self.assertEqual((low["recommendation"], low["confidence"]), ("deviation", "low"))

    def test_9_metrics_and_layout_check(self) -> None:
        metrics = json.loads((self.folder / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["totals"]["items"], 3)
        self.assertEqual({c["id"]: c["ok"] for c in self.summary["run_checks"]}["C-27"], True)
        # a patch with another item's file breaks the independence of the fixes
        patch = self.folder / "patches" / "20001-fix.patch"
        patch.write_text(patch.read_text(encoding="utf-8") + "diff --git a/src/other.c b/src/other.c\n", encoding="utf-8")
        again = self.run_json("summary", "--run", self.run_dir)
        self.assertFalse({c["id"]: c["ok"] for c in again["run_checks"]}["C-27"])

    def test_z_broken_approvals_is_never_written_over(self) -> None:
        path = self.folder / "approvals.json"
        for text in ('{"version": 1, "items": {"20001": {"decision": "reject", "edited": true}', '{"version": 1, "items": []}'):
            path.write_text(text, encoding="utf-8")
            out = self.run_json("summary", "--run", self.run_dir, ok=False)
            self.assertIn("承認のファイル", out["error"])
            self.assertIn(str(path), out["error"])
            self.assertEqual(out["approvals_file"], str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), text)  # the person's choices are kept as they were

    def test_4_note_issue(self) -> None:
        self.run_json("note-issue", "--run", self.run_dir, "--title", "経路が取れない", "--body", "2 回失敗した")
        out = self.run_json("note-issue", "--run", self.run_dir, "--stdin",
                            stdin='﻿{"title": "題", "body": "本文\\r\\n2 行目"}')
        self.assertEqual(out["issues_found"], 2)
        status = self.run_json("status", "--run", self.run_dir)
        self.assertEqual([i["title"] for i in status["issues_found"]], ["経路が取れない", "題"])
        self.run_json("note-issue", "--run", self.run_dir, "--title", "題だけ", ok=False)


class CommandsTest(RunCase):
    def test_settings_reads_only(self) -> None:
        before = sorted(p.name for p in (self.repo / ".coverity-triage").rglob("*"))
        out = self.run_json("settings", "--repo", str(self.repo))
        for key in ("max_rework", "review", "integration", "initial", "verify", "max_items", "parallel", "output_dir",
                    "filters", "problems"):
            self.assertIn(key, out)
        self.assertEqual(out["filters"], ["untriaged.yaml"])
        self.assertEqual(out["problems"], [])
        self.assertEqual(before, sorted(p.name for p in (self.repo / ".coverity-triage").rglob("*")))
        self.assertFalse((self.tmp / "out").exists())

    def test_settings_problems_in_japanese(self) -> None:
        config = self.repo / ".coverity-triage" / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8").replace("parallel: 2", "parallel: 20")
                          .replace("max_rework: 1", "max_rework: x"), encoding="utf-8")
        out = self.run_json("settings", "--repo", str(self.repo))
        self.assertIn("config.yaml の parallel: 8 以下にしてください", out["problems"])
        self.assertIn("config.yaml の agents.max_rework: 整数にしてください", out["problems"])
        self.assertFalse(out["ready"])
        failed = self.new_run_failing()
        self.assertEqual(failed["error"], "設定に直すところがあります")

    def new_run_failing(self, *extra: str) -> dict:
        return self.run_json("new-run", "--repo", str(self.repo), *extra, ok=False)

    def test_missing_config_says_how_to_prepare(self) -> None:
        shutil.rmtree(self.repo / ".coverity-triage")
        out = self.new_run_failing()
        self.assertIn("設定ファイルがありません", out["error"])
        self.assertIn(".coverity-triage/config.yaml を用意してください", out["how_to_fix"])
        self.run_json("runs", "--repo", str(self.repo), ok=False)

    def test_output_dir_inside_repo_is_refused(self) -> None:
        config = self.repo / ".coverity-triage" / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8").replace(f"output_dir: {os.pardir}/out", "output_dir: out"), encoding="utf-8")
        out = self.new_run_failing()
        self.assertIn("リポジトリの中", out["error"])
        self.assertFalse((self.repo / "out").exists())

    def test_new_run_conditions(self) -> None:
        out = self.new_run("--impact", "high", "--limit", "5", "--checker", "MISRA C-2012 Rule 10.*", "--no-review",
                           "--integration", "--max-rework", "2")
        flt = json.loads((Path(out["run_dir"]) / "filter.json").read_text(encoding="utf-8"))
        self.assertEqual(flt["impacts"], ["High"])
        self.assertEqual(flt["limit"], 5)
        self.assertEqual(flt["checkers"], ["MISRA C-2012 Rule 10.*"])
        self.assertEqual(flt["streams"], ["app-main"])
        self.assertEqual((out["review"], out["integration"], out["max_rework"]), (False, True, 2))
        self.new_run_failing("--limit", "500")
        self.new_run_failing("--impact", "Huge")
        self.new_run_failing("--max-rework", "3")
        self.new_run_failing("--filter", "missing.yaml")
        self.new_run_failing("--verify", "build")  # no build command
        self.new_run_failing("--initial")
        bad = self.run_json("new-run", "--repo", str(self.repo), "--limit", "x", ok=False)
        self.assertIn("引数が正しくありません", bad["error"])

    def test_runs_and_foreign_and_other_version(self) -> None:
        first = self.new_run()["run_dir"]
        foreign = self.tmp / "out" / "20200101-000000"
        foreign.mkdir(parents=True)
        (foreign / "run.json").write_text(json.dumps({"run_id": "20200101-000000", "items": []}), encoding="utf-8")
        runs = self.run_json("runs", "--repo", str(self.repo))["runs"]
        by_id = {r["run_id"]: r for r in runs}
        self.assertTrue(by_id["20200101-000000"]["foreign"])
        mine = by_id[Path(first).name]
        self.assertFalse(mine["foreign"])
        self.assertTrue(mine["unfinished"])
        self.assertEqual(set(mine) >= {"run_id", "run_dir", "created_at", "filter_name", "counts", "unfinished",
                                       "has_summary", "foreign"}, True)
        for command in ("tick", "resume", "summary", "status"):
            out = self.run_json(command, "--run", str(foreign), ok=False)
            self.assertTrue(out["foreign"])
        self.edit_state(first, lambda s: s.update(tool_version="0000000000000000"))
        self.assertIn("別の版", self.run_json("tick", "--run", first, ok=False)["error"])
        self.run_json("resume", "--run", first, ok=False)
        self.run_json("status", "--run", first)  # reading is fine

    def test_issues_file_given_instead_of_fetching(self) -> None:
        given = self.tmp / "given"
        self.write_issues(given / "issues.json", ISSUES[:2])
        self.write_details(given, ISSUES[0])
        out = self.new_run("--issues-file", str(given / "issues.json"))
        run_dir = out["run_dir"]
        first = self.tick(run_dir)
        self.assertEqual(first["actions"][0]["type"], "launch")  # no fetch: the warnings were given
        self.assertTrue(self.drive(run_dir, ISSUES[:2], out=first)["done"])
        self.assertIn("ここで問題", (Path(run_dir) / "items" / "20001" / "brief.md").read_text(encoding="utf-8"))
        self.assertIn("取ってある警告経路のファイルがありません",
                      (Path(run_dir) / "items" / "20002" / "brief.md").read_text(encoding="utf-8"))
        status = self.run_json("status", "--run", run_dir)
        self.assertTrue(any("取ってある警告経路" in (e["detail"] or "") or e["kind"] == "取得" for e in status["errors"]))
        self.write_issues(given / "half.json", ISSUES[:1], finished=False)
        self.new_run_failing("--issues-file", str(given / "half.json"))

    def test_brief_names_no_parts(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir)
        self.do_fetch(out["actions"][0], ISSUES[:1])
        out = self.tick(run_dir, "--finished", out["actions"][0]["task"])
        self.do_fetch(out["actions"][0], ISSUES[:1])
        out = self.tick(run_dir, "--finished", out["actions"][0]["task"])
        deadline = time.monotonic() + 90  # the work copy is made by a detached job: a slow PC needs more ticks
        while not any(a["type"] == "launch" for a in out["actions"]) and time.monotonic() < deadline:
            out = self.tick(run_dir)
        launch = next(a for a in out["actions"] if a["type"] == "launch")
        brief = self.brief_path(launch).read_text(encoding="utf-8")
        for section in ("## フォルダとファイル", "## 守ること", "## 警告", "## 返す形", "★"):
            self.assertIn(section, brief)
        self.assertIn("schemas/result.schema.json".replace("/", "/"), brief.replace("\\", "/"))
        # The brief names no part: no `kebab-case` name in backquotes (only paths and file names), no /command.
        for quoted in re.findall(r"`([^`]+)`", brief):
            self.assertNotRegex(quoted, r"^[a-z0-9]+(-[a-z0-9]+)+$", quoted)
        self.assertNotRegex(brief, r"(^|\s)/[a-z][a-z0-9-]+(\s|$)")
        self.assertIn("`20001: <推奨（修正 か 逸脱）>（確信度 <高 / 中 / 低>）— <40 文字ほどの要約>`", brief)


class EncodingTest(RunCase):
    def test_output_in_a_narrow_encoding(self) -> None:
        env = dict(self.env, PYTHONIOENCODING="cp1252")
        import subprocess

        proc = subprocess.run([*self.cmd, "new-run", "--repo", str(self.repo)], env=env, capture_output=True, timeout=60)
        text = proc.stdout.decode("cp1252")
        data = json.loads(text)
        self.assertTrue(data["ok"])
        self.assertIn("\\u", text)  # Japanese written as escapes, readable as JSON
        self.assertIn("このセッションでは許可", data["permission_steps"][0])
        proc = subprocess.run([*self.cmd, "status", "--run", str(self.tmp / "none")], env=dict(self.env, PYTHONIOENCODING="cp932"),
                              capture_output=True, timeout=60)
        self.assertIn("実行フォルダではありません", json.loads(proc.stdout.decode("cp932"))["error"])


if __name__ == "__main__":
    unittest.main()
