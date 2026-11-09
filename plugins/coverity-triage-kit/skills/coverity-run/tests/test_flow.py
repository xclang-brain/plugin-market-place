"""The tick state machine end to end, in a temporary git repository, with fake tool outputs and fake agents."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from support import RunCase, issue

ISSUES = [issue(20001, line=6), issue(20002, checker="RESOURCE_LEAK", function="open_file", line=12),
          issue(20003, checker="INTEGER_OVERFLOW", function="parse", line=21),
          issue(20004, checker="INTEGER_OVERFLOW", function="parse", line=22)]


class BasicFlowTest(RunCase):
    def test_permission_steps_allow_only_the_read_tools_given(self) -> None:
        created = self.new_run("--mcp-tool", "srv-x(read_a)", "--mcp-tool", "srv-x(read_b)")
        step = created["permission_steps"][1]
        self.assertIn("--allow-tool 'srv-x(read_a)' --allow-tool 'srv-x(read_b)'", step)
        self.assertNotIn("--allow-tool 'srv-x' ", step, "never the whole server (it holds the tool that writes back)")
        again = self.run_json("resume", "--run", created["run_dir"], "--mcp-tool", "srv-x(read_a)")
        self.assertIn("--allow-tool 'srv-x(read_a)'", again["permission_steps"][1])
        odd = self.new_run("--mcp-tool", "x'; rm -rf /", "--mcp-tool", "srv-x")  # not <name>(<tool>): left out
        self.assertNotIn("rm -rf", odd["permission_steps"][1])
        self.assertNotIn("'srv-x'", odd["permission_steps"][1])

    def test_permission_steps_for_claude_code(self) -> None:
        tools = ["mcp__srv-x__read_a", "mcp__plugin_kit-x_srv-x__get_b_details"]
        args = [a for t in tools for a in ("--mcp-tool", t)]
        created = self.new_run("--client", "claude-code", *args)
        first, restart = created["permission_steps"]
        out = Path(created["run_dir"]).parent
        self.assertIn(f'/add-dir "{out}"', first)
        self.assertIn("/permissions で Bash(uv run *)", first)
        self.assertIn(f'claude --add-dir "{out}" --allowedTools "Bash(uv run *)" "Write" "Edit" '
                      '"mcp__srv-x__read_a" "mcp__plugin_kit-x_srv-x__get_b_details" で起動し直します', restart)
        self.assertIn("--agent", restart)
        for step in (first, restart):
            self.assertNotIn("copilot", step)
            self.assertNotIn("--allow-tool", step)
            self.assertNotIn("<", step, "no placeholder in the command to copy (REQ-306)")
        again = self.run_json("resume", "--run", created["run_dir"], "--client", "claude-code", "--mcp-tool", tools[0])
        self.assertIn('"Edit" "mcp__srv-x__read_a" で起動し直します', again["permission_steps"][1])

    def test_claude_code_tool_names_are_strict_and_never_write(self) -> None:
        bad = ["mcp__srv-x", "mcp__srv-x__*", "mcp__srv-x__", "mcp__plugin_kit-x_srv-x", "srv-x(read_a)",
               'mcp__srv-x__read_a" --dangerously-skip-permissions "x', "mcp__srv-x__read a", "mcp__srv-x___read_a",
               "mcp__srv-x__read__a", "mcp__srv x__read_a", "Bash(rm *)", "mcp__srv-x__update_c",
               "mcp__plugin_kit-x_srv-x__srv_write_d", "mcp__srv-x__delete-e", "mcp__srv-x__" + "a" * 65]
        args = [a for t in bad + ["mcp__srv-x__read_a"] for a in ("--mcp-tool", t)]
        restart = self.new_run("--client", "claude-code", *args)["permission_steps"][1]
        self.assertIn('"Edit" "mcp__srv-x__read_a" で起動し直します', restart)
        self.assertEqual(restart.count('"mcp__'), 1, restart)
        for word in ("skip-permissions", "update", "write", "delete", "rm ", "*\"", "srv-x(read_a)"):
            self.assertNotIn(word, restart)
        # without a tool that reads, the read tools are allowed at the first confirmation
        plain = self.new_run("--client", "claude-code", "--mcp-tool", "mcp__srv-x__update_c")["permission_steps"][1]
        self.assertIn('"Write" "Edit" で起動し直します', plain)
        self.assertIn("最初の確認で許可を選びます", plain)

    def test_copilot_is_the_default_and_unchanged(self) -> None:
        default = self.new_run("--mcp-tool", "srv-x(read_a)")["permission_steps"]
        named = self.new_run("--client", "copilot", "--mcp-tool", "srv-x(read_a)", "--mcp-tool", "mcp__srv-x__read_a")
        out = Path(named["run_dir"]).parent
        self.assertEqual(default, named["permission_steps"])
        self.assertEqual(default[1], f"または、Copilot CLI を copilot --add-dir \"{out}\" --allow-tool 'shell(uv:*)' "
                         "--allow-tool write --allow-tool 'srv-x(read_a)' で起動し直します"
                         "（Coverity に書き戻すツールは許可しません。反映するときに確認が出ます）。")
        self.assertNotIn("claude", " ".join(default))
        wrong = self.run_json("new-run", "--repo", str(self.repo), "--client", "vscode", ok=False)
        self.assertFalse(wrong["ok"])

    def test_whole_run_without_options(self) -> None:
        created = self.new_run()
        run_dir = created["run_dir"]
        for key in ("run_dir", "repo_root", "work_dir", "limit", "initial", "review", "integration", "max_rework",
                    "permission_steps"):
            self.assertIn(key, created)
        self.assertFalse(created["initial"])
        self.assertEqual(len(created["permission_steps"]), 2)
        self.assertNotIn("<", created["permission_steps"][1], "no placeholder in the command to copy (REQ-306)")
        self.assertNotIn("--allow-tool '", created["permission_steps"][1].split(" で起動し直します")[0].replace(
            "--allow-tool 'shell(uv:*)'", ""), "without --mcp-name the Coverity flag is left out")
        self.assertTrue((Path(run_dir) / "schemas" / "result.schema.json").is_file())

        first = self.tick(run_dir)
        self.assertEqual(first["stage"], "fetch")
        self.assertEqual([a["type"] for a in first["actions"]], ["fetch_issues"])
        fetch = first["actions"][0]
        self.assertEqual(set(fetch), {"type", "task", "repo_root", "filter_file", "output_file"})
        self.assertEqual(fetch["task"], "fetch:issues@1")
        self.assertTrue(Path(fetch["filter_file"]).is_file())

        self.do_fetch(fetch, ISSUES)
        details = self.tick(run_dir, "--finished", fetch["task"])
        self.assertEqual(details["stage"], "details")
        self.assertEqual(details["actions"][0]["type"], "fetch_details")
        self.assertEqual(sorted(details["actions"][0]["cids"]), [20001, 20002, 20003, 20004])
        self.assertEqual(set(details["actions"][0]), {"type", "task", "repo_root", "stream", "cids", "output_dir"})
        self.assertIn("counts", details)

        out = self.drive(run_dir, ISSUES, out=details)
        self.assertTrue(out["done"], out)
        state = self.state(run_dir)
        ids = sorted(i["id"] for i in state["items"])
        self.assertEqual(ids, ["20001", "20002", "G1"])  # 20003 and 20004: same checker and function
        self.assertTrue(all(i["status"] == "done" for i in state["items"]))
        group = next(i for i in state["items"] if i["id"] == "G1")
        self.assertEqual(group["cids"], [20003, 20004])
        saved = json.loads((Path(run_dir) / "items" / "20001" / "saved.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["fix"]["branch"], "coverity-fix/cid-20001")
        g_saved = json.loads((Path(run_dir) / "items" / "G1" / "saved.json").read_text(encoding="utf-8"))
        self.assertEqual(g_saved["fix"]["branch"], f"coverity-fix/{state['run_id']}-G1")
        patch = (Path(run_dir) / "patches" / "20001-fix.patch").read_text(encoding="utf-8")
        self.assertIn("/* fix 20001 */", patch)
        self.assertNotIn("fix 20002", patch)  # the fixes are independent per item
        self.assertTrue((Path(run_dir) / "fixed" / "20001" / "fix" / "src" / "reader.c").is_file())
        log = subprocess_git(self.repo, self.env, "log", "--format=%B", "-n", "1", saved["fix"]["commit"])
        self.assertTrue(log.startswith("Coverity CID 20001: 修正\n\nNULL を確かめる"), log)
        # the person's files are untouched
        self.assertEqual(subprocess_git(self.repo, self.env, "status", "--porcelain"), "")

        summary = self.run_json("summary", "--run", run_dir)
        for key in ("summary_html", "counts", "approvals_counts", "results", "unprocessed", "run_checks", "say",
                    "criteria", "criteria_results"):
            self.assertIn(key, summary)
        self.assertEqual(summary["counts"]["items"], 3)
        self.assertEqual(summary["counts"]["warnings"], 4)
        approvals = json.loads((Path(run_dir) / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(approvals["items"]["20001"]["decision"], "fix")
        self.assertFalse(approvals["items"]["20001"]["edited"])
        checks = {c["id"]: c for c in summary["run_checks"]}
        self.assertTrue(checks["C-24"]["ok"], checks["C-24"])
        self.assertTrue(checks["C-25"]["ok"], checks["C-25"])
        self.assertTrue(checks["C-26"]["ok"], checks["C-26"])
        self.assertTrue(checks["C-27"]["ok"], checks["C-27"])
        self.assertIsNone(checks["C-16"]["ok"])  # the review is off
        self.assertEqual(summary["removed_work_copies"], sorted(summary["removed_work_copies"]))
        self.assertFalse(any((Path(run_dir) / "work").iterdir()))
        for line in summary["say"]:
            self.assertNotIn("coverity-", line)

        status = self.run_json("status", "--run", run_dir)
        self.assertTrue(status["finished"])
        self.assertTrue(status["has_summary"])

    def test_resumed_run_after_a_worker_failed(self) -> None:
        run_dir = self.new_run()["run_dir"]
        calls = {"n": 0}

        def flaky(action):
            calls["n"] += 1
            if action["item"] == "20002" and calls["n"] < 3:
                return "fail"
            return self.work_as_worker(action)

        out = self.drive(run_dir, ISSUES[:2], worker=flaky)
        self.assertTrue(out["done"])
        state = self.state(run_dir)
        failed = next(i for i in state["items"] if i["id"] == "20002")
        self.assertEqual(failed["status"], "error")
        self.assertIn("テストで失敗させた", failed["error"])
        self.assertTrue(any(a["kind"] == "作業" for a in state["alerts"]))
        summary = self.run_json("summary", "--run", run_dir)
        self.assertEqual(summary["counts"]["errors"], 1)
        self.assertEqual([u["id"] for u in summary["unprocessed"]], ["20002"])
        runs = self.run_json("runs", "--repo", str(self.repo))
        self.assertTrue(runs["runs"][0]["unfinished"] is False or runs["runs"][0]["unfinished"] is True)

        resumed = self.run_json("resume", "--run", run_dir)
        self.assertEqual(resumed["back_to_pending"]["error"], 1)
        out = self.drive(run_dir, ISSUES[:2])
        self.assertTrue(out["done"])
        self.assertEqual(next(i for i in self.state(run_dir)["items"] if i["id"] == "20002")["status"], "done")

    def test_result_fix_requests_and_limit(self) -> None:
        run_dir = self.new_run()["run_dir"]
        seen = []

        def bad_then_good(action):
            seen.append(action["task"])
            if action["item"] == "20001" and len([t for t in seen if t.startswith("worker:20001")]) == 1:
                return self.work_as_worker(action, comment="この警告は問題ないと思います。修正を推奨します。")
            return self.work_as_worker(action)

        out = self.drive(run_dir, ISSUES[:1], worker=bad_then_good)
        self.assertTrue(out["done"])
        self.assertIn("worker:20001@1#2", seen)
        brief = (Path(run_dir) / "items" / "20001" / "brief.md").read_text(encoding="utf-8")
        self.assertIn("## 結果ファイルの直すこと", brief)
        self.assertIn("deviation.comment: 「誤検知。」「意図的。」「本物の不具合。」のどれかで始め", brief)  # the comment rules' words
        self.assertIn("「を推奨」", brief)

    def test_an_item_fails_after_two_fix_requests(self) -> None:
        run_dir = self.new_run()["run_dir"]

        def always_bad(action):
            if action["item"] == "20001":
                return self.work_as_worker(action, comment="")
            return self.work_as_worker(action)

        out = self.drive(run_dir, ISSUES[:1], worker=always_bad)
        self.assertTrue(out["done"])
        item = next(i for i in self.state(run_dir)["items"] if i["id"] == "20001")
        self.assertEqual(item["status"], "error")
        self.assertIn("2 回直しても", item["error"])

    def test_no_change_in_the_fix_folder_is_a_fix_request(self) -> None:
        run_dir = self.new_run()["run_dir"]
        tries = []

        def lazy(action):
            tries.append(action["task"])
            if action["item"] == "20002" and len([t for t in tries if "20002" in t]) == 1:
                return self.work_as_worker(action, change=False)
            return self.work_as_worker(action)

        self.assertTrue(self.drive(run_dir, ISSUES[1:2], worker=lazy)["done"])
        self.assertIn("worker:20002@1#2", tries)
        self.assertEqual(next(i for i in self.state(run_dir)["items"] if i["id"] == "20002")["status"], "done")


class GroupSplitTest(RunCase):
    def test_excluded_cid_becomes_its_own_item(self) -> None:
        run_dir = self.new_run()["run_dir"]

        def worker(action):
            if action["item"] == "G1":
                return self.work_as_worker(action, excluded=[20004])
            return self.work_as_worker(action)

        self.assertTrue(self.drive(run_dir, ISSUES[2:], worker=worker)["done"])
        state = self.state(run_dir)
        items = {i["id"]: i for i in state["items"]}
        self.assertEqual(items["G1"]["cids"], [20003])
        self.assertEqual(items["20004"]["split_from"], "G1")
        self.assertEqual(items["20004"]["status"], "done")
        self.assertTrue((Path(run_dir) / "items" / "20004" / "issue-20004.json").is_file())
        saved = json.loads((Path(run_dir) / "items" / "G1" / "saved.json").read_text(encoding="utf-8"))
        commit = subprocess_git(self.repo, self.env, "log", "--format=%s", "-n", "1", saved["fix"]["commit"])
        self.assertEqual(commit, "Coverity CID 20003: 修正")


class FileDrivenCompletionTest(RunCase):
    settings = {"parallel": 1}

    def test_result_file_counts_without_a_report(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir)
        self.do_fetch(out["actions"][0], ISSUES[:1])
        out = self.tick(run_dir)  # the issues file is found without a report
        self.assertEqual(out["stage"], "details")
        self.do_fetch(out["actions"][0], ISSUES[:1])
        out = self.tick_until(run_dir)  # the work copy is made by a detached job first
        launch = out["actions"][0]
        self.assertEqual(launch["type"], "launch")
        self.assertEqual(set(launch), {"type", "task", "role", "item", "description", "prompt", "background"})
        self.assertEqual(launch["role"], "worker")
        self.assertEqual(launch["task"], "worker:20001@1")
        self.assertTrue(launch["prompt"].startswith("指示ファイル "))
        self.work_as_worker(launch)
        # no report: the file counts once it has settled, then the save job runs
        out = self.tick_until(run_dir, until=lambda o: o["done"])
        self.assertTrue(out["done"], out)
        # a late report of the same task is ignored
        late = self.tick(run_dir, "--finished", launch["task"])
        self.assertTrue(late["done"])

    def test_stale_worker_is_tried_again_then_fails(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir)
        self.do_fetch(out["actions"][0], ISSUES[:1])
        out = self.tick(run_dir, "--finished", out["actions"][0]["task"])
        self.do_fetch(out["actions"][0], ISSUES[:1])
        out = self.tick_until(run_dir, "--finished", out["actions"][0]["task"])
        self.assertEqual(out["actions"][0]["task"], "worker:20001@1", out)

        def age(state):
            for task in state["tasks"].values():
                task["launched_at"] -= 3600
        self.edit_state(run_dir, age)
        old = time.time() - 3600
        for path in (Path(run_dir) / "items" / "20001").glob("*"):
            os.utime(path, (old, old))
        out = self.tick_until(run_dir)
        self.assertEqual(out["actions"][0]["task"], "worker:20001@2", out)
        self.edit_state(run_dir, age)
        for path in (Path(run_dir) / "items" / "20001").glob("*"):
            os.utime(path, (old, old))
        out = self.tick(run_dir)
        self.assertTrue(out["done"], out)
        item = self.state(run_dir)["items"][0]
        self.assertEqual(item["status"], "error")
        self.assertIn("30 分", item["error"])


class FetchFailureTest(RunCase):
    def test_two_failed_searches_stop_the_run(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir)
        out = self.tick(run_dir, "--failed", out["actions"][0]["task"], "--reason", "接続できませんでした")
        self.assertEqual(out["actions"][0]["task"], "fetch:issues@2")
        out = self.tick(run_dir, "--failed", out["actions"][0]["task"], "--reason", "接続できませんでした")
        self.assertTrue(out["stop"])
        self.assertIn("error", out)

    def test_authentication_failure_stops_at_once(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir)
        out = self.tick(run_dir, "--failed", out["actions"][0]["task"], "--reason", "HTTP 401: 認証に失敗しました")
        self.assertTrue(out["stop"], out)
        status = self.run_json("status", "--run", run_dir)
        self.assertTrue(status["halted"])
        self.assertFalse(status["stopped"])
        self.assertIn("警告", status["halt_reason"])
        self.run_json("resume", "--run", run_dir)
        status = self.run_json("status", "--run", run_dir)
        self.assertEqual((status["halted"], status["halt_reason"]), (False, None))
        self.assertEqual(self.tick(run_dir)["actions"][0]["type"], "fetch_issues")

    def test_paths_fetched_twice_then_the_work_goes_on(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir)
        self.do_fetch(out["actions"][0], ISSUES[:2])
        out = self.tick(run_dir, "--finished", out["actions"][0]["task"])
        details = out["actions"][0]
        # the tool writes an error file for 20002 and nothing for 20001
        Path(details["output_dir"], "issue-20002.json").write_text(json.dumps(
            {"cid": 20002, "stream": "app-main", "fetched_at": "x", "error": "SOAP の失敗"}), encoding="utf-8")
        out = self.tick(run_dir, "--finished", details["task"])
        self.assertEqual(out["actions"][0]["type"], "fetch_details")
        self.assertEqual(sorted(out["actions"][0]["cids"]), [20001, 20002])
        self.assertTrue(out["actions"][0]["task"].endswith("@2"))
        out = self.tick_until(run_dir, "--finished", out["actions"][0]["task"],
                              until=lambda o: any(a["type"] == "launch" for a in o["actions"]))
        self.assertTrue(out["actions"] and all(a["type"] == "launch" for a in out["actions"]), out)
        status = self.run_json("status", "--run", run_dir)
        self.assertTrue(any(e["kind"] == "取得" for e in status["errors"]))
        brief = self.brief_path(out["actions"][0]).read_text(encoding="utf-8")
        self.assertIn("取れませんでした", brief)

    def test_unknown_task_is_an_error(self) -> None:
        run_dir = self.new_run()["run_dir"]
        self.run_json("tick", "--run", run_dir, "--finished", "nonsense", ok=False)


class EmptyRunTest(RunCase):
    def test_no_issue_is_done_at_once(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.tick(run_dir)
        self.do_fetch(out["actions"][0], [])
        out = self.tick(run_dir, "--finished", out["actions"][0]["task"])
        self.assertTrue(out["done"])
        summary = self.run_json("summary", "--run", run_dir)
        self.assertEqual(summary["counts"]["items"], 0)
        self.assertIn("条件に合う警告はありませんでした。", summary["say"])


def subprocess_git(repo: Path, env: dict[str, str], *args: str) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True).stdout.decode("utf-8").strip()


if __name__ == "__main__":
    unittest.main()
