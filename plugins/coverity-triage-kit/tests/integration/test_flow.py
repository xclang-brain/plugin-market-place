"""The batch triage from start to end without a model (design 9.1 「模擬の Coverity との結合」, REQ-004, REQ-301〜310,
REQ-507, REQ-508).

A tiny C repository (a git repository with a bare remote) has five warnings in the Coverity Connect emulator:
two of one cause in one function (one group), one that is intentional (a deviation), and two of one cause
with different checkers (the integrator merges them). The test does what the orchestrator does:

1. new-run, then tick in a loop. fetch actions go to the MCP server over stdio (started with the plugin's
   mcp.json command; REST v2 and SOAP v9 to the emulator). launch actions go to scripted fake agents that do
   what the instruction files ask: the worker makes a real, minimal fix in its work copy and writes result.json,
   the reviewer writes review.json (one review sends its item back once), the integrator writes plan.json.
2. summary: summary.html, the reports, the patches, the fixed files and approvals.json.
3. apply: preview, the same confirmation string from the MCP server for the plan file, a wrong string is
   refused and nothing is written to the emulator, the right one writes, a second write writes nothing new,
   apply-code pushes the fix branches to the bare remote (and does not push again).

The scripts are named in tests/structure/plugin_parts.py, the MCP server in mcp.json. The test is skipped when uv
or git is missing (in CI that fails); a missing part fails. It takes about a minute.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True  # nothing is left in the plugin's folder
try:
    from . import emulator as emu_support
    from . import flowlib as fl
except ImportError:  # discovered with this folder as the top level
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import emulator as emu_support  # type: ignore[no-redef]
    import flowlib as fl  # type: ignore[no-redef]

pp = fl.pp


def skip_reason() -> str | None:
    if os.environ.get("SKIP_SLOW_TESTS") == "1":
        return "SKIP_SLOW_TESTS=1 のため飛ばしました"
    missing = emu_support.available()
    if missing or fl.mcp_command() is None or fl.mcp_folder() is None:
        raise AssertionError(missing or "mcp.json に MCP サーバーがありません")
    return fl.prerequisites()


SKIP = skip_reason()


@unittest.skipIf(SKIP, SKIP or "")
class FlowTest(unittest.TestCase):
    """The steps share one run; a step is skipped when an earlier one failed."""

    failed: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="flow-")).resolve()
        try:
            cls.prepare()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def prepare(cls) -> None:
        cls.env = fl.base_env(cls.tmp)
        cls.env.update(TMPDIR=str(cls.tmp), TEMP=str(cls.tmp), TMP=str(cls.tmp))
        cls.run_py = pp.RUN_SCRIPT
        cls.apply_py = pp.APPLY_SCRIPT
        # the MCP server's packages, once, in a temporary environment
        venv = cls.tmp / "mcp-venv"
        sync = subprocess.run([fl.UV, "sync", "--native-tls", "--frozen", "--quiet", "--directory", str(fl.mcp_folder())],
                              env=dict(cls.env, UV_PROJECT_ENVIRONMENT=str(venv)), capture_output=True, timeout=600)
        if sync.returncode != 0:
            raise unittest.SkipTest(fl.missing_tool("MCP サーバーのライブラリを用意できません: "
                                                    + sync.stderr.decode("utf-8", "replace")[-400:]))
        # the emulator and the warnings of the analyzed revision
        cls.emu = emu_support.Emulator(cls.tmp / "emu")
        cls.emu.admin("POST", "seed", emu_support.SEED)
        cls.repo = cls.tmp / "repo"
        cls.repo.mkdir()
        cls.revision = fl.write_repo(cls.repo, cls.env)
        cls.emu.admin("POST", "issues", {"stream": fl.STREAM, "snapshot": {"sourceVersion": cls.revision},
                                         "issues": [fl.emulator_tree(w) for w in fl.WARNINGS]})
        listed = cls.emu.admin("GET", f"issues?stream={fl.STREAM}")["issues"]
        cls.cids = {}
        for w in fl.WARNINGS:
            cid = next((i["cid"] for i in listed if (i["checker"], i["function"], i["line"]) == (w["checker"], w["function"], w["line"])), None)
            if cid is None:
                raise AssertionError(f"the emulator did not list the warning {w['key']}: {listed}")
            cls.cids[w["key"]] = cid
        # the settings, committed; a bare remote to push to
        cls.out = cls.tmp / "out"
        fl.write_settings(cls.repo, cls.out, cls.emu.url, env=cls.env)
        remote = cls.tmp / "remote.git"
        fl.git(cls.tmp, "init", "-q", "--bare", str(remote), env=cls.env)
        fl.git(cls.repo, "remote", "add", "origin", str(remote), env=cls.env)
        fl.git(cls.repo, "push", "-q", "origin", "main", env=cls.env)
        cls.remote = remote
        cls.head = fl.git(cls.repo, "rev-parse", "HEAD", env=cls.env)
        # the MCP server, as the plugin registers it (credentials from the environment, as mcp.json passes them)
        command, server_env = fl.mcp_command()
        env = {k: v for k, v in cls.env.items() if not k.upper().endswith("_PROXY")}
        env.update(UV_PROJECT_ENVIRONMENT=str(venv), NO_PROXY="127.0.0.1,localhost",
                   COV_USER="triager", COV_AUTH_KEY=emu_support.USERS["triager"])
        for key, value in server_env.items():
            env[key] = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), value)
        cls.mcp = fl.McpClient(command, env, cls.tmp)
        cls.state: dict[str, Any] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        run_dir = getattr(cls, "state", {}).get("run_dir")
        if run_dir and getattr(cls, "run_py", None):
            try:
                fl.run_script(cls.run_py, "stop", "--run", str(run_dir), env=cls.env, cwd=cls.repo, ok=None, timeout=60)
            except Exception:  # noqa: BLE001 - cleaning up
                pass
        for name in ("mcp", "emu"):
            part = getattr(cls, name, None)
            if part is not None:
                try:
                    (part.close if name == "mcp" else part.stop)()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        if FlowTest.failed:
            self.skipTest(f"前の段（{FlowTest.failed}）が通らなかったため飛ばしました")
        FlowTest.failed = self._testMethodName  # cleared when the step ends without a failure

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        errors = getattr(getattr(outcome, "result", None), "errors", []) + getattr(getattr(outcome, "result", None), "failures", [])
        if outcome is not None and getattr(outcome, "success", True) and not any(t is self for t, _ in errors):
            FlowTest.failed = None

    # ---- helpers ---------------------------------------------------------------------------------

    def run_cmd(self, *args: str, ok: bool | None = True) -> dict[str, Any]:
        return fl.run_script(self.run_py, *args, env=self.env, cwd=self.repo, ok=ok)

    def apply_cmd(self, *args: str, ok: bool | None = True) -> dict[str, Any]:
        return fl.run_script(self.apply_py, *args, env=self.env, cwd=self.repo, ok=ok)

    def writes(self) -> list[dict[str, Any]]:
        return self.emu.admin("GET", "triage-writes")["writes"]

    def fetch(self, action: dict[str, Any]) -> None:
        if action["type"] == "fetch_issues":
            self.mcp.call("coverity_search_issues", {"repo_root": action["repo_root"], "filter_file": action["filter_file"],
                                                     "output_file": action["output_file"]})
        else:
            self.mcp.call("coverity_get_issue_details", {"repo_root": action["repo_root"], "stream": action["stream"],
                                                         "cids": action["cids"], "output_dir": action["output_dir"]})

    def schema(self, name: str) -> dict[str, Any] | None:
        path = pp.RUN_SCRIPT.parents[1] / "assets" / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def valid(self, data: Any, name: str, label: str) -> None:
        schema = self.schema(name)
        if schema is not None:
            self.assertEqual(pp.schema_errors(data, schema), [], f"{label} が {name} に合いません")

    # ---- 1. the run ------------------------------------------------------------------------------

    def test_1_run_to_the_end_with_fake_agents(self) -> None:
        created = self.run_cmd("new-run", "--repo", str(self.repo))
        run_dir = Path(created["run_dir"])
        self.state["run_dir"] = run_dir
        self.assertTrue(created["review"] and created["integration"], created)
        self.assertFalse(created["initial"], created)
        self.assertEqual(run_dir.parent.resolve(), self.out.resolve(), "実行フォルダが output_dir の下にありません")
        c = self.cids
        agents = fl.FakeAgents(c, review_outcomes={str(c["C"]): ["rework", "pass"]})
        orchestrator = fl.Orchestrator(self.run_py, run_dir, self.env, self.repo, agents, fetch=self.fetch)
        last = orchestrator.drive(limit_seconds=150)
        self.state.update(agents=agents, orchestrator=orchestrator)
        self.assertTrue(last.get("done"), last)
        kinds = [a["type"] for a in orchestrator.actions]
        self.assertIn("fetch_issues", kinds)
        self.assertIn("fetch_details", kinds)
        # every launch prompt points to an instruction file inside the run folder (design 4.14)
        for action in orchestrator.launches():
            self.assertTrue(fl.brief_path(action).resolve().is_relative_to(run_dir.resolve()), action["prompt"])
        roles = {a["role"] for a in orchestrator.launches()}
        self.assertEqual(roles, {"worker", "reviewer", "integrator"})
        # the reviewer sent C back once: the worker worked on it twice, and it was reviewed twice
        self.assertEqual(agents.calls.count(("worker", str(c["C"]))), 2, agents.calls)
        self.assertEqual(agents.calls.count(("reviewer", str(c["C"]))), 2, agents.calls)
        status = self.run_cmd("status", "--run", str(run_dir))
        self.state["status"] = status
        items = {i["id"]: i for i in status["items"]}
        groups = {i["id"]: sorted(i["cids"]) for i in status["items"] if len(i["cids"]) > 1}
        self.assertIn(sorted([c["A"], c["B"]]), list(groups.values()), f"A と B が1つのグループになっていません：{groups}")
        self.assertIn(sorted([c["D"], c["E"]]), list(groups.values()), f"D と E がまとめられていません：{groups}")
        final = [i for i in status["items"] if i["status"] == "done"]
        self.assertEqual(sorted(len(i["cids"]) for i in final), [1, 2, 2], f"終わった作業：{list(items)}")
        self.assertEqual(status["counts"]["error"], 0, status.get("errors"))
        for item in final:
            self.assertEqual((item.get("review") or {}).get("outcome"), "pass", item)
        self.assertTrue(status.get("finished") or status.get("stage") == "done", status.get("stage"))
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.valid(run, "run.schema.json", "run.json")
        merged = [i for i in run["items"] if i.get("status") == "merged"]
        self.assertEqual(sorted(c for i in merged for c in i["cids"]), sorted([c["D"], c["E"]]), "統合した元の作業が merged になっていません")
        self.state["final"] = {i["id"]: i for i in final}
        self.state["deviation_item"] = str(c["C"])
        self.state["fix_items"] = sorted(i["id"] for i in final if i["id"] != str(c["C"]))

    # ---- 2. the summary --------------------------------------------------------------------------

    def test_2_summary_reports_patches_and_approvals(self) -> None:
        run_dir = self.state["run_dir"]
        summary = self.run_cmd("summary", "--run", str(run_dir))
        self.assertTrue(Path(summary["summary_html"]).is_file())
        self.assertEqual(Path(summary["summary_html"]).resolve(), (run_dir / "summary.html").resolve())
        html = (run_dir / "summary.html").read_text(encoding="utf-8")
        for item_id in self.state["final"]:
            with self.subTest(item=item_id):
                self.assertTrue((run_dir / "cid" / f"{item_id}.html").is_file(), f"cid/{item_id}.html がありません")
                self.assertIn(item_id, html)
                result = json.loads((run_dir / "items" / item_id / "result.json").read_text(encoding="utf-8"))
                self.valid(result, "result.schema.json", f"{item_id} の result.json")
                patch = run_dir / "patches" / f"{item_id}-fix.patch"
                self.assertTrue(patch.is_file(), f"patches/{item_id}-fix.patch がありません")
                text = patch.read_text(encoding="utf-8")
                self.assertRegex(text, r"(?m)^diff --git a/src/\w+\.c b/src/\w+\.c$")
                fixed = list((run_dir / "fixed" / item_id / "fix").rglob("*.c"))
                self.assertTrue(fixed, f"fixed/{item_id}/fix/ に修正後のファイルがありません")
        # the fixes are what the workers made
        g1 = next(i for i, v in self.state["final"].items() if sorted(v["cids"]) == sorted([self.cids["A"], self.cids["B"]]))
        self.assertIn("if (buf == NULL)", (run_dir / "patches" / f"{g1}-fix.patch").read_text(encoding="utf-8"))
        merged = next(i for i, v in self.state["final"].items() if sorted(v["cids"]) == sorted([self.cids["D"], self.cids["E"]]))
        self.assertIn("fclose(fp);", (run_dir / "patches" / f"{merged}-fix.patch").read_text(encoding="utf-8"))
        approvals = json.loads((run_dir / "approvals.json").read_text(encoding="utf-8"))
        self.valid(approvals, "approvals.schema.json", "approvals.json")
        decisions = {k: v.get("decision") for k, v in approvals["items"].items()}
        expected = {**{i: "fix" for i in self.state["fix_items"]}, self.state["deviation_item"]: "deviation"}
        self.assertEqual({k: decisions.get(k) for k in expected}, expected, "承認の案が推奨と違います")
        problems = [c for c in summary.get("run_checks") or [] if c.get("ok") is False]
        self.assertEqual(problems, [], "実行ごとの評価基準に合わないものがあります")
        self.assertEqual(summary["counts"].get("errors", 0), 0, summary["counts"])
        # the person's repository is as it was (the agents changed only their work copies)
        self.assertEqual(fl.git(self.repo, "rev-parse", "HEAD", env=self.env), self.head)
        self.assertEqual(fl.git(self.repo, "status", "--porcelain", env=self.env), "", "利用者のリポジトリが変わっています")
        self.assertFalse((run_dir / "work").exists() and any((run_dir / "work").iterdir()),
                         "一覧を作った後に、作業用のコピーが残っています")

    # ---- 3. apply --------------------------------------------------------------------------------

    def test_3_apply_writes_back_once_with_the_right_token_and_pushes_the_fixes(self) -> None:
        run_dir = self.state["run_dir"]
        preview = self.apply_cmd("preview", "--run", str(run_dir))
        self.assertFalse(preview["nothing_to_apply"], preview)
        self.assertEqual(preview["counts"], {"fix": 2, "deviation": 1, "reject": 0})
        plan_file = Path(preview["plan_file"])
        token = preview["confirmation_token"]
        self.assertRegex(token, r"^[0-9a-f]{12}$")
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in plan["triage"]], [self.state["deviation_item"]])
        self.assertEqual(sorted(e["id"] for e in plan["code"]), self.state["fix_items"])
        # the MCP server gives the same confirmation string for the same plan file (design 4.10)
        shown = self.mcp.call("coverity_preview_triage", {"plan_file": str(plan_file)})
        self.assertEqual(fl.find_token(shown), token, "反映のスクリプトと MCP サーバーの確認の文字列が違います")
        self.assertEqual(self.writes(), [], "書き戻しの前に、模擬の Coverity に書き込みがあります")
        # a wrong string is refused and nothing is written
        wrong = "0" * 12 if token != "0" * 12 else "f" * 12
        try:
            refused = self.mcp.call("coverity_update_triage", {"confirmation_token": wrong, "repo_root": str(self.repo),
                                                               "plan_file": str(plan_file)})
            self.assertFalse(refused.get("ok", True) and refused.get("written"), f"違う確認の文字列で書き戻しました：{refused}")
        except fl.ToolError:
            pass
        self.assertEqual(self.writes(), [], "違う確認の文字列で、模擬の Coverity に書き込みました")
        refused_code = self.apply_cmd("apply-code", "--run", str(run_dir), "--token", wrong, ok=False)
        self.assertIn("確認の文字列", refused_code["error"])
        self.assertEqual(fl.git(self.repo, "ls-remote", "--heads", "origin", env=self.env).count("refs/heads/"), 1,
                         "違う確認の文字列で push しました")
        # the right string writes the deviation once
        done = self.mcp.call("coverity_update_triage", {"confirmation_token": token, "repo_root": str(self.repo),
                                                        "plan_file": str(plan_file)})
        writes = self.writes()
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0]["cids"], [self.cids["C"]])
        self.assertEqual(writes[0]["attributes"].get("Classification"), "Intentional")
        self.assertEqual(writes[0]["attributes"].get("Action"), "Ignore")
        self.assertIn("hal_write", json.dumps(writes[0], ensure_ascii=False))
        self.assertTrue((plan_file.parent / "apply-plan.result.json").is_file(), done)
        again = self.mcp.call("coverity_update_triage", {"confirmation_token": token, "repo_root": str(self.repo),
                                                         "plan_file": str(plan_file)})
        self.assertEqual(len(self.writes()), 1, f"同じ内容を二重に書き戻しました：{again}")
        found = self.mcp.call("coverity_search_issues", {"repo_root": str(self.repo),
                                                         "filter": {"streams": [fl.STREAM], "triage": {"classification": ["Intentional"]}}})
        self.assertEqual([i["cid"] for i in found["issues"]], [self.cids["C"]])
        # apply-code pushes one branch per fix to the remote, and reports the write-back
        code = self.apply_cmd("apply-code", "--run", str(run_dir), "--token", token)
        self.assertEqual(code["coverity"]["registered"], [self.state["deviation_item"]], code["coverity"])
        self.assertEqual(code["errors"], [], code)
        branches = sorted(p["branch"] for p in code["pull_requests"])
        self.assertEqual(len(branches), 2, code["pull_requests"])
        remote = fl.git(self.repo, "ls-remote", "--heads", "origin", env=self.env)
        for branch in branches:
            with self.subTest(branch=branch):
                self.assertIn(f"refs/heads/{branch}", remote)
                diff = fl.git(self.repo, "diff", "--stat", "main", branch, env=self.env)
                self.assertRegex(diff, r"src/(config|log)\.c")
        for pr in code["pull_requests"]:
            self.assertTrue(pr.get("title") and pr.get("body"), pr)
        # the same string again pushes nothing new
        heads = remote
        second = self.apply_cmd("apply-code", "--run", str(run_dir), "--token", token)
        self.assertTrue(second["repeated"], second)
        self.assertEqual(second["pull_requests"], [], second)
        self.assertEqual(sorted(p["branch"] for p in second["pushed_before"]), branches)
        self.assertEqual(fl.git(self.repo, "ls-remote", "--heads", "origin", env=self.env), heads)
        self.assertEqual(len(self.writes()), 1)
        # the person's main branch and working tree are untouched
        self.assertEqual(fl.git(self.repo, "rev-parse", "main", env=self.env), self.head)
        self.assertEqual(fl.git(self.repo, "status", "--porcelain", env=self.env), "")
        # a new preview finds nothing left to apply
        last = self.apply_cmd("preview", "--run", str(run_dir))
        self.assertTrue(last["nothing_to_apply"], last)


if __name__ == "__main__":
    unittest.main()
