# -*- coding: utf-8 -*-
"""Behaviour of scripts/doctor.py (read-only checks).

Run from the skill folder:  python3 -m unittest discover -s tests -v
The script runs through uv as written in SKILL.md; the tests are skipped when uv is missing.
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
SCRIPT = SKILL_DIR / "scripts" / "doctor.py"
UV = shutil.which("uv")
GIT = shutil.which("git")
SECRET_USER = "alice-in-house"
SECRET_KEY = "k3y-Very-Secret-0123"


def load_module():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("doctor_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("COV_USER", "COV_AUTH_KEY", "MY_USER", "MY_KEY", "PYTHONIOENCODING")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def run_doctor(*args, env=None, script=SCRIPT):
    proc = subprocess.run([UV, "run", "--native-tls", "--quiet", str(script), *args], capture_output=True,
                          env=env or clean_env(), timeout=180)
    encoding = (env or {}).get("PYTHONIOENCODING", "utf-8")
    text = proc.stdout.decode(encoding)
    return proc.returncode, json.loads(text), text


def snapshot(*folders):
    return sorted(str(p) for folder in folders for p in Path(folder).rglob("*"))


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def by_name(result, name):
    found = [c for c in result["checks"] if c["check"] == name]
    return found[0] if found else None


GOOD_CONFIG = """# 見本の設定
coverity:
  url: https://coverity.example.co.jp:8443
  api: fake
  fake_data: fake-issues.yaml
vcs:
  type: git
  base_branch: main
parallel: 2
"""
GOOD_FILTER = "name: 全件\nproject: Sample\nstreams: [Sample-main]\nlimit: 5\n"


@unittest.skipUnless(UV, "uv がないので、スクリプトを動かす試験を飛ばす")
class DoctorCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="doctor-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_repo(self, config=GOOD_CONFIG, filters=True, git=True):
        write(self.repo / ".coverity-triage" / "config.yaml", config)
        write(self.repo / ".coverity-triage" / "fake-issues.yaml", "issues: []\n")
        if filters:
            write(self.repo / ".coverity-triage" / "filters" / "all.yaml", GOOD_FILTER)
        if git and GIT:
            env = clean_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@localhost", GIT_COMMITTER_NAME="t",
                            GIT_COMMITTER_EMAIL="t@localhost")
            subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True, env=env)
            subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, env=env)
            subprocess.run(["git", "commit", "-q", "-m", "s"], cwd=self.repo, check=True, env=env)

    def test_missing_folder_is_an_error(self):
        code, result, _ = run_doctor("check", "--repo", str(self.tmp / "nothing"))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertIn("problems", result)

    def test_missing_config(self):
        code, result, _ = run_doctor("check", "--repo", str(self.repo))
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["ready"])
        self.assertEqual(by_name(result, "設定ファイル")["status"], "ng")

    @unittest.skipUnless(GIT, "git がない")
    def test_good_settings_are_ready(self):
        self.make_repo()
        before = snapshot(self.tmp)
        code, result, _ = run_doctor("check", "--repo", str(self.repo))
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ready"], [c for c in result["checks"] if c["status"] == "ng"])
        self.assertEqual(result["settings"]["parallel"], 2)
        self.assertEqual(result["settings"]["agents"]["max_rework"], 1)  # default added
        self.assertEqual(Path(result["output_dir"]), (self.tmp / "coverity-triage-out").resolve())
        # a check creates no files
        self.assertEqual(before, snapshot(self.tmp))

    def test_values_out_of_range(self):
        self.make_repo(config="coverity:\n  url: https://c.example\n  api: real\n  user_env: X\n  key_env: X\n"
                              "parallel: 9\nagents:\n  max_rework: 3\noptions:\n  metrics: yes-please\n", git=False)
        _, result, _ = run_doctor("check", "--repo", str(self.repo))
        detail = by_name(result, "設定ファイル")["detail"]
        self.assertEqual(by_name(result, "設定ファイル")["status"], "ng")
        for word in ["parallel", "agents.max_rework", "coverity.api", "options.metrics", "同じ名前"]:
            self.assertIn(word, detail)

    def test_yaml_error_names_the_line(self):
        self.make_repo(config="coverity:\n  url: [unclosed\n", git=False)
        _, result, _ = run_doctor("check", "--repo", str(self.repo))
        entry = by_name(result, "設定ファイル")
        self.assertEqual(entry["status"], "ng")
        self.assertIn("行目", entry["detail"])

    def test_output_dir_inside_repo(self):
        self.make_repo(config=GOOD_CONFIG + "output_dir: out\n", git=False)
        _, result, _ = run_doctor("check", "--repo", str(self.repo))
        entry = by_name(result, "結果の置き場所")
        self.assertEqual(entry["status"], "ng")
        self.assertIn("リポジトリの中", entry["detail"])
        self.assertFalse(result["ready"])

    def test_http_url_and_unknown_keys(self):
        self.make_repo(config=GOOD_CONFIG.replace("https://", "http://") + "extra_key: 1\n", git=False)
        _, result, _ = run_doctor("check", "--repo", str(self.repo))
        self.assertEqual(by_name(result, "Coverity の URL")["status"], "warning")
        self.assertIn("extra_key", by_name(result, "設定の知らない項目")["detail"])

    def test_filters(self):
        self.make_repo(filters=False, git=False)
        write(self.repo / ".coverity-triage" / "filters" / "two.yaml", "streams: [a, b]\n")
        write(self.repo / ".coverity-triage" / "filters" / "odd.yaml",
              "streams: [a]\ntriage:\n  classification: [Maybe]\n")
        _, result, _ = run_doctor("check", "--repo", str(self.repo))
        self.assertEqual(by_name(result, "条件ファイル two.yaml")["status"], "ng")
        self.assertIn("1 つだけ", by_name(result, "条件ファイル two.yaml")["detail"])
        self.assertEqual(by_name(result, "条件ファイル odd.yaml")["status"], "warning")

    def test_no_filters(self):
        self.make_repo(filters=False, git=False)
        _, result, _ = run_doctor("check", "--repo", str(self.repo))
        self.assertEqual(by_name(result, "条件ファイル")["status"], "ng")

    def test_credentials_are_masked(self):
        self.make_repo(config=GOOD_CONFIG.replace("api: fake", "api: auto"), git=False)
        env = clean_env(COV_USER=SECRET_USER, COV_AUTH_KEY=SECRET_KEY)
        _, result, text = run_doctor("check", "--repo", str(self.repo), env=env)
        self.assertNotIn(SECRET_USER, text)
        self.assertNotIn(SECRET_KEY, text)
        self.assertEqual(by_name(result, "Coverity の認証キー")["status"], "ok")
        self.assertIn("****", by_name(result, "Coverity の認証キー")["detail"])

    def test_renamed_and_unexpanded_variables(self):
        config = GOOD_CONFIG.replace("api: fake", "api: auto").replace("coverity:\n", "coverity:\n  user_env: MY_USER\n  key_env: MY_KEY\n")
        self.make_repo(config=config, git=False)
        env = clean_env(MY_USER="u", MY_KEY="${MY_KEY}")
        _, result, _ = run_doctor("check", "--repo", str(self.repo), env=env)
        self.assertEqual(by_name(result, "Coverity のユーザー名")["status"], "ok")
        key = by_name(result, "Coverity の認証キー")
        self.assertEqual(key["status"], "ng")  # an unexpanded ${...} counts as missing
        self.assertIn("MY_KEY", key["detail"])

    def test_initial_analysis_needs_build_command(self):
        self.make_repo(config=GOOD_CONFIG + "verify:\n  initial: true\n", git=False)
        _, result, _ = run_doctor("check", "--repo", str(self.repo))
        self.assertEqual(by_name(result, "ビルドのコマンド")["status"], "ng")
        self.assertIn(by_name(result, "cov-commit-defects")["status"], ("ok", "ng"))

    def make_runs(self):
        out = self.tmp / "coverity-triage-out"
        new = out / "20261108-101500"
        write(new / "run.json", json.dumps({
            "format": 2, "run_id": "20261108-101500", "created_at": "2026-11-08T10:15:00",
            "filter_name": "all.yaml", "review": True, "integration": False,
            "items": [{"id": "20001", "cids": [20001], "status": "done"},
                      {"id": "G1", "cids": [20004, 20005], "status": "error", "error": "結果の形が直らない"},
                      {"id": "20003", "cids": [20003], "status": "pending"}]}))
        write(new / "summary.html", "<html></html>")
        old = out / "20261107-090000"
        write(old / "run.json", json.dumps({"format": 1, "items": []}))
        write(out / "notes" / "run.json", "{}")  # not a run folder name
        return out, new

    def test_runs_are_listed(self):
        self.make_repo(git=False)
        self.make_runs()
        _, result, _ = run_doctor("check", "--repo", str(self.repo))
        self.assertEqual(result["runs_total"], 2)
        first, second = result["runs"]
        self.assertEqual(first["run_id"], "20261108-101500")
        self.assertEqual(first["counts"], {"done": 1, "error": 1, "pending": 1, "total": 3})
        self.assertTrue(first["has_summary"])
        self.assertFalse(first["foreign"])
        self.assertTrue(second["foreign"])
        self.assertFalse(second["has_summary"])
        self.assertFalse(first["schema_checked"] and not (SKILL_DIR / "assets" / "run.schema.json").is_file())

    def test_run_details(self):
        _, run_dir = self.make_runs()
        code, result, _ = run_doctor("run", "--run", str(run_dir))
        self.assertEqual(code, 0)
        self.assertEqual([i["id"] for i in result["items"]], ["20001", "G1", "20003"])
        self.assertEqual([e["id"] for e in result["errors"]], ["G1"])
        code, result, _ = run_doctor("run", "--run", str(self.tmp))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])

    def test_schema_copy_is_used_when_present(self):
        copy = self.tmp / "renamed-help"
        shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns("__pycache__", "evals"))
        write(copy / "assets" / "run.schema.json", json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "required": ["format", "run_id"], "properties": {"format": {"type": "integer"}}}))
        _, run_dir = self.make_runs()
        write(run_dir / "run.json", json.dumps({"format": "two", "items": []}))
        _, result, _ = run_doctor("run", "--run", str(run_dir), script=copy / "scripts" / "doctor.py")
        self.assertTrue(result["schema_checked"])
        joined = " ".join(result["schema_problems"])
        self.assertIn("format", joined)
        self.assertIn("run_id", joined)

    def test_help_and_bad_arguments(self):
        code, result, _ = run_doctor()
        self.assertEqual(code, 0)
        self.assertIn("check", result["help"])
        code, result, _ = run_doctor("check", "--nope")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])

    def test_console_code_page_escapes_what_it_cannot_write(self):
        self.make_repo(filters=False, git=False)
        write(self.repo / ".coverity-triage" / "filters" / "emoji.yaml", "name: 見本😀\nstreams: [s]\n")
        env = clean_env(PYTHONIOENCODING="cp932")
        _, result, text = run_doctor("check", "--repo", str(self.repo), env=env)
        self.assertIn("\\ud83d\\ude00", text)  # not writable in cp932: JSON escape
        self.assertIn("見本", text)  # written as is
        self.assertIn("見本😀", by_name(result, "条件ファイル emoji.yaml")["detail"])


class UserEnvTest(unittest.TestCase):
    """Windows: credentials saved as user variables (HKCU\\Environment) are read without a restart."""

    def setUp(self):
        self.doctor = load_module()

    def fake_winreg(self, values):
        module = types.ModuleType("winreg")
        module.HKEY_CURRENT_USER = object()
        module.REG_SZ, module.REG_EXPAND_SZ = 1, 2

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        module.OpenKey = lambda root, path: Key()

        def query(key, name):
            if name not in values:
                raise FileNotFoundError(name)
            return values[name]

        module.QueryValueEx = query
        return module

    def test_reads_user_variables_on_windows(self):
        reg = self.fake_winreg({"COV_USER": ("taro", 1), "COV_AUTH_KEY": ("", 1), "EXP": ("%HOMEX%\\k", 2)})
        with mock.patch.dict(sys.modules, {"winreg": reg}), mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"HOMEX": "C:\\Users\\t"}, clear=False):
            os.environ.pop("COV_USER", None)
            os.environ.pop("COV_AUTH_KEY", None)
            self.assertEqual(self.doctor.user_env("COV_USER"), ("taro", "user"))
            self.assertEqual(self.doctor.user_env("COV_AUTH_KEY"), (None, None))  # empty counts as missing
            self.assertEqual(self.doctor.user_env("NOT_THERE"), (None, None))
            self.assertEqual(self.doctor.user_env("EXP")[0], "C:\\Users\\t\\k")

    def test_process_variable_comes_first(self):
        with mock.patch.dict(os.environ, {"COV_USER": "from-process"}):
            self.assertEqual(self.doctor.user_env("COV_USER"), ("from-process", "process"))
        with mock.patch.dict(os.environ, {"COV_USER": "${COV_USER}"}), mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(self.doctor.user_env("COV_USER"), (None, None))


if __name__ == "__main__":
    unittest.main()
