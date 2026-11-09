# -*- coding: utf-8 -*-
"""Behaviour of scripts/selftest.py.

Run from the skill folder:  python3 -m unittest discover -s tests -v
The script runs through uv as written in SKILL.md; the tests are skipped when uv or git is missing.
A batch run is not started here: the tests write run folders in the shape the batch run leaves.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "scripts" / "selftest.py"
UV = shutil.which("uv")
GIT = shutil.which("git")
SECRET_KEY = "k3y-Very-Secret-0123"
SECRET_USER = "hanako-inhouse"
HOST = "coverity.corp-internal.example"


def uv_cache_dir():
    """The tests move HOME; uv keeps using the usual cache (so nothing is downloaded again)."""
    if not UV:
        return None
    proc = subprocess.run([UV, "cache", "dir"], capture_output=True, text=True)
    return proc.stdout.strip() or None


UV_CACHE = os.environ.get("UV_CACHE_DIR") or uv_cache_dir()


def load_module():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("selftest_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path, data):
    write(path, json.dumps(data, ensure_ascii=False))


@unittest.skipUnless(UV and GIT, "uv か git がないので、スクリプトを動かす試験を飛ばす")
class SelftestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="selftest-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("COV_USER", "COV_AUTH_KEY", "COVERITY_PASSPHRASE", "PYTHONIOENCODING")}
        self.env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "HOME": str(self.home),
                         "USERPROFILE": str(self.home)})
        if UV_CACHE:
            self.env["UV_CACHE_DIR"] = UV_CACHE
        self.dir = self.tmp / "results"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, *args, stdin=None, env=None, script=SCRIPT, expect=0):
        proc = subprocess.run([UV, "run", "--native-tls", "--quiet", str(script), *args], input=stdin,
                              capture_output=True, env=env or self.env, timeout=300)
        enc = (env or self.env).get("PYTHONIOENCODING", "utf-8")
        result = json.loads(proc.stdout.decode(enc))
        self.assertEqual(proc.returncode, expect, result)
        return result

    def start(self, *extra):
        return self.call("start", "--dir", str(self.dir), *extra)

    def check(self, check_id):
        return json.loads((self.dir / "selftest.json").read_text(encoding="utf-8"))["checks"].get(check_id)

    # a run folder in the shape the batch run leaves (design 4.11, assets/run.schema.json)
    def make_run(self, out, items=None, summary=True, **state):
        run_dir = Path(out) / "20261108-101500"
        items = items or [
            ("20001", [20001], "done", "fix"), ("20002", [20002], "done", "deviation"),
            ("20003", [20003], "done", "fix"), ("G1", [20004, 20005, 20006], "done", "deviation")]
        finished = summary and all(s in ("done", "error", "merged") for _, _, s, _ in items)
        data = {"format": 2, "tool_version": "1", "run_id": run_dir.name, "repo_root": str(getattr(self, "sample", self.tmp)),
                "created_at": "2026-11-08T10:15:00", "filter_name": "all", "stream": "app-main", "limit": 20,
                "initial": False, "review": True, "integration": False, "max_rework": 1, "parallel": 2,
                "stage": "done" if finished else "items", "stopped": False, "stopped_at": None,
                "finished": finished, "finished_at": "2026-11-08T11:00:00" if finished else None,
                "halted": False, "halt_reason": None,
                "items": [{"id": i, "cids": c, "status": s, "merged_into": None, "merged_from": [],
                           "review": "pass" if s == "done" else None, "slot": None, "stage": None, "attempts": 1,
                           "review_rounds": 0, "error": "取れませんでした" if s == "error" else "",
                           "started": 1762564500.0, "seconds": 120.0} for i, c, s, _ in items]}
        data.update(state)
        write_json(run_dir / "run.json", data)
        for item_id, cids, status, rec in items:
            if status != "done":
                continue
            write_json(run_dir / "items" / item_id / "result.json", {
                "item": item_id, "verdict": {"judgement": "true_bug", "summary": "要約", "rationale": "根拠", "evidence": []},
                "recommendation": rec, "confidence": "high", "confidence_reason": "読んだ",
                "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Minor", "comment": "本物の不具合。"},
                "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Minor", "summary": "直す",
                        "impact": "なし", "exceeded_constraints": [], "already_fixed_on_latest": False},
                "revision_drift": {"status": "none", "detail": ""}, "group_excluded_cids": []})
            write(run_dir / "cid" / f"{item_id}.html", "<html></html>")
            write(run_dir / "patches" / f"{item_id}-fix.patch", "diff --git a/x b/x\n")
        if summary:
            write(run_dir / "summary.html", "<html></html>")
            write_json(run_dir / "approvals.json", {"version": 1, "items": {}})
        return run_dir


class StartTest(SelftestBase):
    def test_start_marks_other_sections_skipped(self):
        result = self.start("--sections", "①②", "--client", "Copilot CLI")
        self.assertEqual(sorted(result["sections"]), ["1", "2"])
        self.assertEqual({c["id"][0] for c in result["checks"]}, {"1", "2"})
        self.assertEqual(self.check("3-1")["status"], "skip")
        self.assertIsNone(self.check("1-1"))

    def test_claude_code_client_and_screen_skip(self):
        # in Claude Code the screen tool (1-8) is recorded as skipped: the screen is Copilot CLI only
        self.start("--sections", "1", "--client", "Claude Code")
        self.call("record", "--dir", str(self.dir), "--id", "1-8", "--status", "skip",
                  "--actual", "Web管理画面は Copilot CLI だけ")
        self.assertEqual(self.check("1-8")["status"], "skip")
        report = Path(self.call("report", "--dir", str(self.dir))["report"]).read_text(encoding="utf-8")
        self.assertIn("使っているもの: Claude Code", report)
        self.assertIn("Web管理画面は Copilot CLI だけ", report)

    def test_sections_3_and_4_need_a_repository(self):
        result = self.call("start", "--dir", str(self.dir), "--sections", "3", expect=1)
        self.assertFalse(result["ok"])
        self.assertIn("--repo", result["error"])

    def test_bad_sections_and_non_empty_folder(self):
        self.assertFalse(self.call("start", "--dir", str(self.dir), "--sections", "5", expect=1)["ok"])
        write(self.dir / "x.txt", "x")
        self.assertFalse(self.call("start", "--dir", str(self.dir), expect=1)["ok"])

    def test_default_folder_is_under_home(self):
        result = self.call("start", "--sections", "1")
        folder = Path(result["dir"])
        self.assertEqual(folder.parent, self.home / f"{SKILL_DIR.name}-results")
        self.assertTrue((folder / "selftest.json").is_file())

    def test_commands_need_a_started_folder(self):
        result = self.call("check-tools", "--dir", str(self.tmp), expect=1)
        self.assertIn("結果のフォルダではありません", result["error"])

    def test_help_and_bad_arguments(self):
        self.assertIn("start", self.call()["help"])
        self.assertFalse(self.call("record", "--dir", str(self.tmp), "--status", "great", expect=1)["ok"])


class SectionOneAndTwoTest(SelftestBase):
    def setUp(self):
        super().setUp()
        self.start("--sections", "1,2")
        self.sample = Path(self.call("sample", "--dir", str(self.dir))["repo_root"])
        self.out = self.dir / "coverity-triage-out"

    def test_tools(self):
        result = self.call("check-tools", "--dir", str(self.dir))
        self.assertEqual(result["checks"][0]["status"], "pass")

    def test_sample_repository(self):
        self.assertEqual(self.check("2-1")["status"], "pass")
        self.assertTrue((self.sample / ".coverity-triage" / "fake-issues.yaml").is_file())
        self.assertTrue((self.sample / ".coverity-triage" / "filters" / "all.yaml").is_file())
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.sample, capture_output=True, text=True).stdout
        self.assertEqual(len(log.strip().splitlines()), 1)
        heads = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=self.sample, capture_output=True,
                               text=True).stdout
        self.assertIn("refs/heads/main", heads)
        # made again from the start
        write(self.sample / "extra.txt", "x")
        self.call("sample", "--dir", str(self.dir))
        self.assertFalse((self.sample / "extra.txt").exists())

    def test_good_run(self):
        run_dir = self.make_run(self.out)
        result = self.call("check-run", "--dir", str(self.dir), "--run", str(run_dir))
        self.assertEqual({c["id"]: c["status"] for c in result["checks"]},
                         {"2-2": "pass", "2-3": "pass", "2-4": "pass", "2-5": "pass", "2-6": "pass"})

    def test_run_problems(self):
        run_dir = self.make_run(self.out, items=[
            ("20001", [20001], "done", "fix"), ("20002", [20002], "done", "fix"),
            ("20003", [20003], "error", "fix"), ("G1", [20004, 20005], "done", "deviation"),
            ("20006", [20006], "done", "deviation")], summary=False)
        (run_dir / "patches" / "20001-fix.patch").unlink()
        result = self.call("check-run", "--dir", str(self.dir), "--run", str(run_dir))
        got = {c["id"]: c for c in result["checks"]}
        self.assertEqual(got["2-2"]["status"], "fail")  # no summary.html
        self.assertEqual(got["2-3"]["status"], "fail")  # 20003 error
        self.assertIn("20003", got["2-3"]["detail"])
        self.assertEqual(got["2-4"]["status"], "review")  # 20002 is not as expected; never "fail"
        self.assertIn("20002", got["2-4"]["detail"])
        self.assertEqual(got["2-5"]["status"], "review")  # the group lost one CID
        self.assertEqual(got["2-6"]["status"], "review")  # a missing patch only

    def test_a_halted_or_stopped_run_did_not_reach_its_end(self):
        run_dir = self.make_run(self.out, finished=False, stage="fetch", halted=True,
                                halt_reason="Coverity の認証に失敗しました（401）")
        got = {c["id"]: c for c in self.call("check-run", "--dir", str(self.dir), "--run", str(run_dir))["checks"]}
        self.assertEqual(got["2-2"]["status"], "fail")
        self.assertIn("認証に失敗", got["2-2"]["detail"])
        shutil.rmtree(run_dir)
        run_dir = self.make_run(self.out, finished=False, stage="stopped", stopped=True, stopped_at="2026-11-08T10:30:00")
        got = {c["id"]: c for c in self.call("check-run", "--dir", str(self.dir), "--run", str(run_dir))["checks"]}
        self.assertEqual(got["2-2"]["status"], "fail")
        self.assertIn("止められ", got["2-2"]["detail"])

    def test_a_run_json_of_the_wrong_shape_fails(self):
        run_dir = self.make_run(self.out, stage="somewhere")
        got = {c["id"]: c for c in self.call("check-run", "--dir", str(self.dir), "--run", str(run_dir))["checks"]}
        self.assertEqual(got["2-2"]["status"], "fail")
        self.assertIn("run.json の形", got["2-2"]["detail"])

    def test_changed_working_file_fails(self):
        run_dir = self.make_run(self.out)
        write(self.sample / "src" / "reader.c", "changed\n")
        result = self.call("check-run", "--dir", str(self.dir), "--run", str(run_dir))
        self.assertEqual({c["id"]: c["status"] for c in result["checks"]}["2-6"], "fail")

    def test_not_a_run_folder(self):
        self.assertFalse(self.call("check-run", "--dir", str(self.dir), "--run", str(self.tmp), expect=1)["ok"])

    def writes(self, *records):
        path = self.sample / ".coverity-triage" / "fake-issues.yaml.writes.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def test_wrong_token_refused(self):
        result = self.call("check-refused", "--dir", str(self.dir), "--refused", "yes", "--actual", "確認の文字列が合いません")
        self.assertEqual(result["checks"][0]["status"], "pass")
        self.assertEqual(self.check("2-7")["detail"], "確認の文字列が合いません")
        self.assertEqual(self.call("check-refused", "--dir", str(self.dir), "--refused", "no")["checks"][0]["status"], "fail")
        self.writes({"cids": [20002], "classification": "False Positive"})
        self.assertEqual(self.call("check-refused", "--dir", str(self.dir), "--refused", "yes")["checks"][0]["status"], "fail")

    def test_apply(self):
        run_dir = self.make_run(self.out)
        self.assertFalse(self.call("check-apply", "--dir", str(self.dir), "--run", str(run_dir), expect=1)["ok"])
        write_json(run_dir / "apply-plan.json", {
            "run_id": run_dir.name, "triage": [{"id": "20002", "cids": [20002], "stream": "Sample-main"},
                                               {"id": "G1", "cids": [20004, 20005, 20006], "stream": "Sample-main"}],
            "code": [{"id": "20001", "cids": [20001], "kind": "fix", "branch": "coverity-fix/cid-20001"}], "reject": []})
        self.writes({"cids": [20002]}, {"cids": [20004, 20005, 20006]})
        result = self.call("check-apply", "--dir", str(self.dir), "--run", str(run_dir))
        self.assertEqual(result["checks"][0]["status"], "fail")  # the branch is not pushed yet
        self.assertIn("coverity-fix/cid-20001", result["checks"][0]["detail"])
        subprocess.run(["git", "branch", "coverity-fix/cid-20001"], cwd=self.sample, check=True)
        subprocess.run(["git", "push", "-q", "origin", "coverity-fix/cid-20001"], cwd=self.sample, check=True,
                       capture_output=True)
        result = self.call("check-apply", "--dir", str(self.dir), "--run", str(run_dir))
        self.assertEqual(result["checks"][0]["status"], "pass", result)
        write_json(run_dir / "apply-plan.result.json",
                   {"written": {}, "failed": {"abc": {"id": "G1", "cids": [20004], "error": "HTTP 500"}}})
        result = self.call("check-apply", "--dir", str(self.dir), "--run", str(run_dir))
        self.assertEqual(result["checks"][0]["status"], "fail")
        self.assertIn("HTTP 500", result["checks"][0]["detail"])

    def test_record_and_report(self):
        self.call("record", "--dir", str(self.dir), "--id", "1-3", "--status", "pass",
                  "--actual", "作業役のエージェントがあり、説明もある")
        stdin = json.dumps({"id": "1-2", "status": "review", "actual": "分からないと言われた"}, ensure_ascii=False)
        self.call("record", "--dir", str(self.dir), "--stdin", stdin=stdin.encode("utf-8"))
        self.assertFalse(self.call("record", "--dir", str(self.dir), "--id", "9-9", "--status", "pass",
                                   "--actual", "x", expect=1)["ok"])
        result = self.call("report", "--dir", str(self.dir))
        report = Path(result["report"]).read_text(encoding="utf-8")
        for check_id in ("1-1", "1-8", "2-1", "2-8", "3-1", "4-3"):
            self.assertIn(check_id, report)
        self.assertIn("要確認", report)
        self.assertEqual([r["id"] for r in result["review"]], ["1-2"])
        self.assertIn("1-1", result["not_recorded"])
        self.assertNotIn("3-1", result["not_recorded"])  # out of range: skipped


class MaskingTest(SelftestBase):
    def setUp(self):
        super().setUp()
        self.repo = self.home / "work" / "app"
        write(self.repo / ".coverity-triage" / "config.yaml",
              f"coverity:\n  url: https://{HOST}:8443\nvcs:\n  type: git\n")
        write(self.repo / ".coverity-triage" / "filters" / "untriaged.yaml", "streams: [app-main]\nlimit: 20\n")
        self.env.update({"COV_USER": SECRET_USER, "COV_AUTH_KEY": SECRET_KEY})
        self.start("--sections", "3", "--repo", str(self.repo))

    def test_values_are_masked_in_records_and_report(self):
        actual = (f"https://{SECRET_USER}@{HOST}:8443/api に {HOST} で接続、key={SECRET_KEY}、"
                  f"{self.home}/work/app、10.1.2.3、{SECRET_USER}@corp.example、http://127.0.0.1:8731/")
        self.call("record", "--dir", str(self.dir), "--id", "3-2", "--status", "pass", "--actual", actual)
        result = self.call("report", "--dir", str(self.dir))
        for text in (Path(result["report"]).read_text(encoding="utf-8"),
                     (self.dir / "selftest.json").read_text(encoding="utf-8")):
            for secret in (SECRET_KEY, SECRET_USER, HOST, "10.1.2.3", str(self.home)):
                self.assertNotIn(secret, text)
        self.assertIn("https://****:8443/api", self.check("3-2")["actual"])
        self.assertIn("~/work/app", self.check("3-2")["actual"])
        self.assertIn("127.0.0.1:8731", self.check("3-2")["actual"])

    def test_config_check(self):
        result = self.call("check-config", "--dir", str(self.dir))
        self.assertEqual(result["checks"][0]["status"], "pass", result)
        self.assertEqual([Path(f).name for f in result["filter_files"]], ["untriaged.yaml"])
        env = dict(self.env)
        del env["COV_AUTH_KEY"]
        result = self.call("check-config", "--dir", str(self.dir), env=env)
        self.assertEqual(result["checks"][0]["status"], "fail")
        self.assertIn("COV_AUTH_KEY", result["checks"][0]["detail"])

    def test_config_check_problems(self):
        write(self.repo / ".coverity-triage" / "config.yaml", "coverity:\n  url: https://h\n  api: fake\noutput_dir: out\n")
        result = self.call("check-config", "--dir", str(self.dir))
        self.assertEqual(result["checks"][0]["status"], "fail")
        self.assertIn("リポジトリの中", result["checks"][0]["detail"])
        (self.repo / ".coverity-triage" / "config.yaml").unlink()
        result = self.call("check-config", "--dir", str(self.dir))
        self.assertEqual(result["checks"][0]["status"], "fail")


class BuildTest(SelftestBase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        write(self.repo / "main.c", "int main(void) { return 0; }\n")

    def configure(self, verify):
        write(self.repo / ".coverity-triage" / "config.yaml", "coverity:\n  url: https://h.example\n" + verify)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "s"],
                       cwd=self.repo, check=True)
        self.start("--sections", "4", "--repo", str(self.repo))

    def py(self, code):
        return f'"{sys.executable}" -c "{code}"'

    def test_no_build_command_skips(self):
        self.configure("")
        result = self.call("check-build", "--dir", str(self.dir))
        self.assertEqual([c["status"] for c in result["checks"]], ["skip", "skip", "skip"])

    def test_broken_settings_fail(self):
        self.configure("verify:\n  build_command: 'unclosed\n")
        result = self.call("check-build", "--dir", str(self.dir))
        self.assertEqual([c["status"] for c in result["checks"]], ["fail"])

    def test_build_in_a_copy_with_setup_and_without_key(self):
        setup = self.py("open(r'{root}/setup-ran', 'w').close()")
        build = self.py("import os,sys; sys.exit(0 if os.path.exists('setup-ran') and not os.environ.get('COV_AUTH_KEY') else 3)")
        self.configure(f"verify:\n  setup_command: {json.dumps(setup)}\n  build_command: {json.dumps(build)}\n")
        self.env["COV_AUTH_KEY"] = SECRET_KEY
        result = self.call("check-build", "--dir", str(self.dir))
        got = {c["id"]: c["status"] for c in result["checks"]}
        self.assertEqual(got["4-1"], "pass", result)
        self.assertEqual(got["4-2"], "info")  # no build verification / initial analysis configured
        self.assertFalse((self.repo / "setup-ran").exists())  # the repository itself is not touched

    def test_failed_build_keeps_no_source_lines(self):
        build = self.py("import sys; print('main.c:12: error: expected ;'); print('   12 |   int x = secret_code();'); sys.exit(2)")
        self.configure(f"verify:\n  initial: true\n  build_command: {json.dumps(build)}\n")
        result = self.call("check-build", "--dir", str(self.dir))
        got = {c["id"]: c for c in result["checks"]}
        self.assertEqual(got["4-1"]["status"], "fail")
        self.assertIn("終了コード 2", got["4-1"]["actual"])
        self.assertIn("expected ;", got["4-1"]["detail"])
        self.assertNotIn("secret_code", got["4-1"]["detail"])
        if not shutil.which("cov-commit-defects"):
            self.assertEqual(got["4-3"]["status"], "fail")  # initial analysis needs it


@unittest.skipUnless(UV and GIT, "uv か git がない")
class SchemaCopyTest(SelftestBase):
    def test_run_schema_copy_is_used_when_present(self):
        copy = self.tmp / "renamed-selftest"
        shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns("__pycache__", "evals", "tests"))
        write_json(copy / "assets" / "run.schema.json", {
            "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "required": ["format", "tool_version"]})
        write_json(copy / "assets" / "approvals.schema.json", {
            "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "required": ["updated_at"]})
        script = copy / "scripts" / "selftest.py"
        self.call("start", "--dir", str(self.dir), "--sections", "2", script=script)
        self.call("sample", "--dir", str(self.dir), script=script)
        run_dir = self.make_run(self.dir / "coverity-triage-out")
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        del state["tool_version"]
        write_json(run_dir / "run.json", state)
        result = self.call("check-run", "--dir", str(self.dir), "--run", str(run_dir), script=script)
        two = {c["id"]: c for c in result["checks"]}["2-2"]
        self.assertEqual(two["status"], "fail")
        self.assertIn("tool_version", two["detail"])
        self.assertIn("approvals.json の形", two["detail"])

    def test_console_code_page(self):
        env = dict(self.env, PYTHONIOENCODING="cp932")
        self.call("start", "--dir", str(self.dir), "--sections", "1", env=env)
        result = self.call("record", "--dir", str(self.dir), "--id", "1-2", "--status", "pass",
                           "--actual", "出ていた😀", env=env)
        self.assertEqual(result["checks"][0]["actual"], "出ていた😀")


class UserEnvTest(unittest.TestCase):
    def test_reads_user_variables_on_windows(self):
        selftest = load_module()
        reg = types.ModuleType("winreg")
        reg.HKEY_CURRENT_USER, reg.REG_SZ, reg.REG_EXPAND_SZ = object(), 1, 2

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        values = {"COV_AUTH_KEY": ("abc", 1), "COV_USER": ("", 1)}
        reg.OpenKey = lambda root, path: Key()
        reg.QueryValueEx = lambda key, name: values[name] if name in values else (_ for _ in ()).throw(OSError(name))
        with mock.patch.dict(sys.modules, {"winreg": reg}), mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COV_AUTH_KEY", None)
            os.environ.pop("COV_USER", None)
            self.assertEqual(selftest.user_env("COV_AUTH_KEY"), "abc")
            self.assertIsNone(selftest.user_env("COV_USER"))
            self.assertIsNone(selftest.user_env("MISSING"))

    def test_sections_parse(self):
        selftest = load_module()
        self.assertEqual(selftest.parse_sections("①②"), ["1", "2"])
        self.assertEqual(selftest.parse_sections("3, 4"), ["3", "4"])

    def test_restart_hint_names_the_client(self):
        # the 1-7 detail tells the user to start again the client given to start --client
        selftest = load_module()
        self.assertEqual(selftest.client_name({"client": "Copilot CLI"}), "Copilot CLI")
        self.assertEqual(selftest.client_name({"client": "Claude Code"}), "Claude Code")
        self.assertIn("Claude Code", selftest.client_name({"client": ""}))
        self.assertIn("Copilot CLI", selftest.client_name({}))

    def test_log_tail_drops_source_excerpts(self):
        selftest = load_module()
        text = "a.c:3: error: x\n    3 |   y = secret();\n      |       ^\nmake: *** [all] Error 1\n"
        tail = selftest.log_tail(text)
        self.assertNotIn("secret", tail)
        self.assertIn("make: *** [all] Error 1", tail)


class CleanupTest(unittest.TestCase):
    """The temporary copy of check-build is removed best-effort: Windows keeps files that another process
    still holds (a build helper left running), and the results of the build are never lost for that."""

    def setUp(self):
        self.selftest = load_module()
        self.tmp = Path(tempfile.mkdtemp(prefix="selftest-cleanup-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_rmtree(self, busy_times):
        """rmtree that hands one entry to the error handler, whose retry fails ``busy_times`` times."""
        calls = []

        def unlink(name):
            calls.append(name)
            if len(calls) <= busy_times:
                raise PermissionError(13, "別のプロセスが使用中です", name)

        def rmtree(path, onexc=None, onerror=None):
            held = Path(path) / "build.log"
            held.write_bytes(b"")
            (onexc or onerror)(unlink, str(held), PermissionError(13, "busy"))

        return rmtree, calls

    def test_a_busy_file_is_tried_again(self):
        folder = self.tmp / "work"
        folder.mkdir()
        rmtree, calls = self.fake_rmtree(busy_times=3)
        with mock.patch.object(self.selftest.shutil, "rmtree", rmtree), mock.patch.object(self.selftest.time, "sleep"):
            self.selftest.remove_tree(folder)
        self.assertEqual(len(calls), 4)

    def test_gives_up_after_the_retries(self):
        folder = self.tmp / "work"
        folder.mkdir()
        rmtree, calls = self.fake_rmtree(busy_times=100)
        with mock.patch.object(self.selftest.shutil, "rmtree", rmtree), mock.patch.object(self.selftest.time, "sleep"):
            with self.assertRaises(PermissionError):
                self.selftest.remove_tree(folder)
        self.assertEqual(len(calls), 10)

    def test_a_file_already_gone_is_fine(self):
        folder = self.tmp / "work"
        folder.mkdir()

        def rmtree(path, onexc=None, onerror=None):
            (onexc or onerror)(os.unlink, str(Path(path) / "gone"), FileNotFoundError())

        with mock.patch.object(self.selftest.shutil, "rmtree", rmtree):
            self.selftest.remove_tree(folder)

    def test_check_build_keeps_the_results_when_the_copy_cannot_be_removed(self):
        import importlib.util

        if importlib.util.find_spec("yaml") is None:  # the check is called in this process, not through uv
            self.skipTest("この Python に pyyaml がないため、ビルドの確かめをこのプロセスで呼べません")
        repo = self.tmp / "repo"
        write(repo / "main.c", "int main(void) { return 0; }\n")
        build = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
        write(repo / ".coverity-triage" / "config.yaml",
              "coverity:\n  url: https://h.example\nverify:\n  build_command: " + json.dumps(build) + "\n")
        results_dir = self.tmp / "results"
        with mock.patch.object(self.selftest, "progress", lambda *a, **k: None):
            self.selftest.cmd_start(str(results_dir), "4", str(repo), "test")
        left = []

        def still_held(path):
            left.append(path)
            raise PermissionError(32, "プロセスはファイルにアクセスできません", str(Path(path) / "build.log"))

        with mock.patch.object(self.selftest, "remove_tree", still_held), \
                mock.patch.object(self.selftest, "progress", lambda *a, **k: None):
            result = self.selftest.cmd_check_build(str(results_dir))
        for path in left:
            shutil.rmtree(path, ignore_errors=True)
        got = {c["id"]: c for c in result["checks"]}
        self.assertTrue(result["ok"])
        self.assertEqual(got["4-1"]["status"], "pass", result)
        self.assertIn("一時フォルダを消せませんでした", got["4-1"]["detail"])
        saved = json.loads((results_dir / "selftest.json").read_text(encoding="utf-8"))["checks"]
        self.assertEqual(saved["4-1"]["status"], "pass")
        self.assertIn("一時フォルダを消せませんでした", saved["4-1"]["detail"])
        self.assertIn("4-2", saved)
        self.assertIn("4-3", saved)


if __name__ == "__main__":
    unittest.main()
