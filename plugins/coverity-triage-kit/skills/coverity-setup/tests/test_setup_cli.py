"""setup.py のコマンドの試験（REQ-102〜REQ-107）。一時フォルダの git リポジトリで動かす。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from setup_testlib import (GIT, SKILL, TempDirCase, base_env, git, make_git_repo, needs_git, needs_uv,
                           run_script, tree)

NOTICE_HEAD = "このファイルでは、注釈は動いたり消えたりします"


def init(repo: Path, *extra: str) -> tuple[int, dict, str]:
    return run_script("init", "--repo", str(repo), "--url", "https://cov.example.com:8443/", "--project", "envmon",
                      "--stream", "envmon-main", *extra)


@needs_uv
@needs_git
class Detect(TempDirCase):
    def test_finds_the_root_from_a_sub_folder(self) -> None:
        repo = make_git_repo(self.tmp)
        code, out, _ = run_script("detect", "--repo", str(repo / "src"))
        self.assertEqual(code, 0, out)
        self.assertEqual(Path(out["repo_root"]), repo)
        self.assertEqual(out["vcs"], "git")
        self.assertEqual(out["base_branch"], "main")
        self.assertFalse(out["has_config"])
        self.assertTrue(out["output_dir_outside_repo"])
        self.assertEqual(Path(out["output_dir_resolved"]), self.tmp / "coverity-triage-out")

    def test_a_folder_that_is_not_a_repository(self) -> None:
        code, out, _ = run_script("detect", "--repo", str(self.tmp))
        self.assertEqual(code, 0)
        self.assertIsNone(out["vcs"])
        self.assertIn(".git", out["vcs_note"])

    def test_remote_credentials_are_masked(self) -> None:
        repo = make_git_repo(self.tmp, with_origin=False)
        git(repo, "remote", "add", "origin", "https://alice:ghp_secret123@github.example.com/o/r.git")
        _, out, _ = run_script("detect", "--repo", str(repo))
        self.assertNotIn("ghp_secret123", json.dumps(out))
        self.assertIn("****@github.example.com", out["remote"])


@needs_uv
@needs_git
class Init(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_git_repo(self.tmp)

    def test_check_shows_the_table_and_writes_nothing(self) -> None:
        before = tree(self.tmp)
        code, out, _ = init(self.repo, "--check")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["check"])
        self.assertEqual(out["written"], [])
        labels = [row["項目"] for row in out["table"]]
        self.assertIn("Coverity Connect の URL", labels)
        self.assertIn("ストリーム", labels)
        self.assertEqual(tree(self.tmp), before, "--check でファイルができた")

    def test_writes_commented_config_filters_and_knowledge(self) -> None:
        code, out, _ = init(self.repo)
        self.assertEqual(code, 0, out)
        folder = self.repo / ".coverity-triage"
        names = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
        self.assertEqual(names, ["config.yaml", "filters/all.yaml", "filters/untriaged.yaml", "knowledge.md"])
        config = (folder / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("url: https://cov.example.com:8443 ", config, "URL の最後の / は外す")
        for line in config.splitlines():
            if ":" in line and not line.lstrip().startswith("#") and not line.rstrip().endswith(":"):
                self.assertRegex(line, r"\s#\s\S", f"項目に注釈がない: {line}")
        self.assertNotIn("COV_AUTH_KEY:", config)
        untriaged = (folder / "filters" / "untriaged.yaml").read_text(encoding="utf-8")
        self.assertIn("streams: [envmon-main]", untriaged)
        self.assertIn("classification: [Unclassified]", untriaged)
        self.assertIn("limit: 20", untriaged)
        self.assertIn("project: envmon", (folder / "filters" / "all.yaml").read_text(encoding="utf-8"))
        knowledge = (folder / "knowledge.md").read_text(encoding="utf-8")
        for heading in ("## 調査で使う事実", "## 推奨の方針（修正か逸脱か）", "## 逸脱コメントの書き方"):
            self.assertIn(heading, knowledge)
        code, report, _ = run_script("check", "--repo", str(self.repo))
        statuses = {c["check"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["設定ファイル"], "ok", report)
        self.assertEqual(statuses["条件ファイル untriaged.yaml"], "ok")
        self.assertEqual(statuses["条件ファイル all.yaml"], "ok")

    def test_refuses_an_existing_config_and_keeps_knowledge_on_overwrite(self) -> None:
        init(self.repo)
        knowledge = self.repo / ".coverity-triage" / "knowledge.md"
        knowledge.write_text("# チームの知識\n- fatal() は戻らない\n", encoding="utf-8")
        code, out, _ = init(self.repo)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("--overwrite", out["error"])
        code, out, _ = init(self.repo, "--overwrite")
        self.assertEqual(code, 0, out)
        self.assertEqual(knowledge.read_text(encoding="utf-8"), "# チームの知識\n- fatal() は戻らない\n")

    def test_refuses_an_output_folder_inside_the_repository(self) -> None:
        code, out, _ = init(self.repo, "--output-dir", "results")
        self.assertEqual(code, 1)
        self.assertIn("リポジトリの外", out["error"])
        self.assertFalse((self.repo / ".coverity-triage").exists())

    def test_svn_settings_have_no_branches(self) -> None:
        if not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn がないので飛ばします")
        server = self.tmp / "svnrepo"
        subprocess.run(["svnadmin", "create", str(server)], check=True, capture_output=True)
        work = self.tmp / "wc"
        subprocess.run(["svn", "checkout", "-q", server.as_uri(), str(work)], check=True, capture_output=True)
        code, out, _ = init(work)
        self.assertEqual(code, 0, out)
        config = (work / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("type: svn", config)
        self.assertNotIn("base_branch", config)


@needs_uv
@needs_git
class Check(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_git_repo(self.tmp)
        init(self.repo)

    def test_creates_no_file_and_never_shows_credential_values(self) -> None:
        before = tree(self.tmp)
        env = base_env(COV_USER="alice-user-xyz", COV_AUTH_KEY="s3cr3t-key-value")
        code, out, _ = run_script("check", "--repo", str(self.repo), env=env)
        self.assertEqual(code, 0, out)
        text = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("s3cr3t-key-value", text)
        self.assertNotIn("alice-user-xyz", text)
        statuses = {c["check"]: c for c in out["checks"]}
        self.assertEqual(statuses["Coverity の認証キー"]["status"], "ok")
        self.assertEqual(statuses["Coverity のユーザー名"]["status"], "ok")
        self.assertTrue(out["ready"], out["missing"])
        self.assertEqual(tree(self.tmp), before, "check がファイルを作った")

    def test_missing_credentials_and_bad_values_are_ng(self) -> None:
        config = self.repo / ".coverity-triage" / "config.yaml"
        text = config.read_text(encoding="utf-8")
        text = re.sub(r"output_dir: \S+", "output_dir: inside", text)
        config.write_text(text.replace("parallel: 3", "parallel: 9").replace("key_env: COV_AUTH_KEY", "key_env: COV_USER"),
                          encoding="utf-8")
        code, out, _ = run_script("check", "--repo", str(self.repo))
        self.assertEqual(code, 0)
        self.assertFalse(out["ready"])
        detail = {c["check"]: c["detail"] for c in out["checks"]}
        self.assertIn("parallel: 1〜8", detail["設定ファイル"])
        self.assertIn("同じ名前", detail["設定ファイル"])
        self.assertIn("結果を置くフォルダ", out["missing"])
        self.assertIn("Coverity のユーザー名", out["missing"])

    def test_no_config(self) -> None:
        shutil.rmtree(self.repo / ".coverity-triage")
        code, out, _ = run_script("check", "--repo", str(self.repo))
        self.assertEqual(code, 0)
        self.assertIn("設定ファイル", out["missing"])


COMMENTED = """\
# チームで決めた設定
coverity:
  url: https://cov.example.com:8443   # 社内
  api: auto
  # 証明書（情シスから）
  ca_file: C:/certs/ca.pem
  fake_data:
vcs:
  type: git
  base_branch: main   # 既定
output_dir: OUT
max_items: 20   # 夜は 20 件
"""


@needs_uv
@needs_git
class SetValues(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_git_repo(self.tmp)
        self.config = self.repo / ".coverity-triage" / "config.yaml"
        self.config.parent.mkdir()
        self.write(COMMENTED)

    def write(self, text: str) -> None:
        self.config.write_bytes(text.replace("OUT", str(self.tmp / "out")).encode("utf-8"))

    def text(self) -> str:
        return self.config.read_bytes().decode("utf-8").replace(str(self.tmp / "out"), "OUT")

    def test_changes_only_the_value_lines_and_keeps_comments(self) -> None:
        code, out, _ = run_script("set", "--repo", str(self.repo), "--value", "max_items=50",
                                  "--value", "coverity.url=https://cov2.example.com")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["comments"], "kept")
        self.assertEqual(self.text(), COMMENTED.replace("max_items: 20", "max_items: 50")
                         .replace("url: https://cov.example.com:8443", "url: https://cov2.example.com"))

    def test_check_writes_nothing(self) -> None:
        before = self.config.read_bytes()
        code, out, _ = run_script("set", "--repo", str(self.repo), "--value", "agents.review=true", "--check")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["check"])
        self.assertEqual([c["action"] for c in out["changes"]], ["added"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_absent_key_is_added_only_when_it_differs_from_the_default(self) -> None:
        before = self.config.read_bytes()
        _, out, _ = run_script("set", "--repo", str(self.repo), "--value", "agents.review=false",
                               "--value", "parallel=3")
        self.assertEqual([c["action"] for c in out["changes"]], ["skipped_default", "skipped_default"])
        self.assertFalse(out["written"])
        self.assertEqual(self.config.read_bytes(), before)
        _, out, _ = run_script("set", "--repo", str(self.repo), "--value", "agents.review=true",
                               "--value", "agents.max_rework=2")
        self.assertTrue(out["written"])
        self.assertTrue(self.text().endswith("max_items: 20   # 夜は 20 件\nagents:\n  review: true\n  max_rework: 2\n"))

    def test_present_value_stays_even_when_empty_or_default(self) -> None:
        _, out, _ = run_script("set", "--repo", str(self.repo), "--value", "max_items=30")
        self.assertIn("  fake_data:\n", self.text(), "空の値の項目が消えた")
        _, out, _ = run_script("set", "--repo", str(self.repo), "--value", "max_items=100")
        self.assertEqual(out["changes"][0]["action"], "changed")
        self.assertIn("max_items: 100   # 夜は 20 件", self.text(), "ファイルにある項目は既定の値でも残る")

    def test_unset_removes_the_key_and_its_comment(self) -> None:
        code, out, _ = run_script("set", "--repo", str(self.repo), "--unset", "coverity.ca_file")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.text(), COMMENTED.replace("  # 証明書（情シスから）\n  ca_file: C:/certs/ca.pem\n", ""))

    def test_stdin_json(self) -> None:
        payload = json.dumps({"values": {"coverity.path_strip_prefixes": ["/build/ci/", "C:/work/"],
                                         "options.metrics": True}}, ensure_ascii=False)
        code, out, _ = run_script("set", "--repo", str(self.repo), "--stdin", stdin=payload)
        self.assertEqual(code, 0, out)
        self.assertIn("path_strip_prefixes:\n  - /build/ci/\n  - C:/work/\n", self.text())
        self.assertIn("options:\n  metrics: true\n", self.text())

    def test_notice_when_comments_cannot_be_kept(self) -> None:
        self.write(COMMENTED + "verify:\n  build_command: |\n    make all\n")
        before = self.config.read_bytes()
        code, out, _ = run_script("set", "--repo", str(self.repo), "--value", "max_items=5", "--check")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["comments"], "lost")
        self.assertTrue(out["notice"].startswith(NOTICE_HEAD))
        self.assertEqual(self.config.read_bytes(), before, "--check で書いた")
        code, out, _ = run_script("set", "--repo", str(self.repo), "--value", "max_items=5")
        self.assertTrue(out["notice"].startswith(NOTICE_HEAD))
        self.assertTrue(out["written"])
        self.assertTrue(self.text().startswith("# チームで決めた設定\n"), "行の頭の注釈は先頭に残る")
        self.assertIn("max_items: 5\n", self.text())

    def test_refuses_unknown_keys_and_wrong_values_and_writes_nothing(self) -> None:
        before = self.config.read_bytes()
        for value in ("agents.reviw=true", "parallel=9", "agents.review=maybe", "coverity.key_env=COV_USER"):
            with self.subTest(value=value):
                code, out, _ = run_script("set", "--repo", str(self.repo), "--value", value)
                self.assertEqual(code, 1, out)
                self.assertFalse(out["ok"])
                self.assertTrue(out["error"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_crlf_file_stays_crlf(self) -> None:
        self.config.write_bytes(self.config.read_bytes().replace(b"\n", b"\r\n"))
        _, out, _ = run_script("set", "--repo", str(self.repo), "--value", "max_items=7")
        self.assertEqual(out["comments"], "kept")
        raw = self.config.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
        self.assertIn("max_items: 7   # 夜は 20 件\r\n".encode("utf-8"), raw)

    def test_set_verify_saves_the_build_settings(self) -> None:
        code, out, _ = run_script("set-verify", "--repo", str(self.repo), "--build-command", "make all",
                                  "--setup-command", "call env.bat {root}", "--no-initial", "--check")
        self.assertEqual(code, 0, out)
        self.assertEqual({c["key"]: c["action"] for c in out["changes"]},
                         {"verify.build_command": "added", "verify.default": "added",
                          "verify.setup_command": "added", "verify.initial": "skipped_default"})
        code, out, _ = run_script("set-verify", "--repo", str(self.repo), "--build-command", "make all",
                                  "--initial", "--cov-commit-args=--auth-key-file C:/k.txt", "--timeout-minutes", "90")
        self.assertEqual(code, 0, out)
        self.assertIn("verify:\n  default: build\n  initial: true\n  build_command: make all\n"
                      "  cov_commit_args: --auth-key-file C:/k.txt\n  timeout_minutes: 90\n", self.text())

    def test_set_verify_needs_a_build_command(self) -> None:
        code, out, _ = run_script("set-verify", "--repo", str(self.repo), "--build-command", " ")
        self.assertEqual(code, 1)
        self.assertIn("verify.build_command", " ".join(out["problems"]))


@needs_uv
class Credentials(TempDirCase):
    def test_commands_never_hold_the_key(self) -> None:
        env = base_env(COV_AUTH_KEY="s3cr3t-key-value")
        code, out, _ = run_script("credentials", "--repo", str(self.tmp), "--user", "O'Brien", env=env)
        self.assertEqual(code, 0, out)
        self.assertNotIn("s3cr3t-key-value", json.dumps(out, ensure_ascii=False))
        self.assertTrue(out["key_set"])
        self.assertIn("Read-Host 'COV_AUTH_KEY を入力してください（表示されません）' -AsSecureString", out["windows"]["key"])
        self.assertIn("[Environment]::SetEnvironmentVariable('COV_AUTH_KEY',", out["windows"]["key"])
        self.assertTrue(out["windows"]["key_from_cmd"].startswith('powershell -NoProfile -Command "$s = Read-Host'))
        self.assertEqual(out["windows"]["user"], "[Environment]::SetEnvironmentVariable('COV_USER', 'O''Brien', 'User')")
        self.assertIn("read -rs", out["posix"]["key"])
        self.assertIn("umask 077", out["posix"]["key"])

    def test_the_key_cannot_be_passed_as_an_argument(self) -> None:
        code, out, _ = run_script("credentials", "--repo", str(self.tmp), "--key", "abc")
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertNotIn("abc", json.dumps(out, ensure_ascii=False))
        code, out, _ = run_script("credentials", "--repo", str(self.tmp), "--key=abc")
        self.assertEqual(code, 1)
        self.assertNotIn("abc", json.dumps(out, ensure_ascii=False))

    def test_names_come_from_the_settings(self) -> None:
        folder = self.tmp / ".coverity-triage"
        folder.mkdir()
        (folder / "config.yaml").write_text("coverity:\n  url: https://x\n  user_env: MY_U\n  key_env: MY_K\n",
                                            encoding="utf-8")
        _, out, _ = run_script("credentials", "--repo", str(self.tmp))
        self.assertEqual((out["user_env"], out["key_env"]), ("MY_U", "MY_K"))
        self.assertIn("'MY_K'", out["windows"]["key"])
        self.assertIn("<ユーザー名>", out["windows"]["user"])


@needs_uv
@needs_git
class TrialBuild(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_git_repo(self.tmp)
        init(self.repo, "--output-dir", str(self.tmp / "out"))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "settings")

    def python_line(self, code: str) -> str:
        return f'"{sys.executable}" -c "{code}"'

    def test_builds_a_copy_outside_the_repository_without_credentials(self) -> None:
        env = base_env(COV_AUTH_KEY="s3cr3t-key-value", COVERITY_PASSPHRASE="pass-value")
        command = self.python_line("import os;open('built.txt','w').write(os.environ.get('COV_AUTH_KEY','none')"
                                   "+os.environ.get('COVERITY_PASSPHRASE','none'))")
        status_before = git(self.repo, "status", "--porcelain")
        code, out, _ = run_script("trial-build", "--repo", str(self.repo), "--build-command", command,
                                  "--build-dir", "src", env=env)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["build_ok"], out)
        built = Path(out["built_in"])
        self.assertFalse(str(built).startswith(str(self.repo)), "リポジトリの中でビルドした")
        self.assertEqual((built / "src" / "built.txt").read_text(), "nonenone", "ビルドに認証情報が渡った")
        self.assertFalse((self.repo / "src" / "built.txt").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), status_before)
        self.assertNotIn("s3cr3t-key-value", Path(out["log"]).read_text(encoding="utf-8", errors="replace"))

    def test_failure_gives_the_end_of_the_log(self) -> None:
        command = self.python_line("import sys;print('compile error: foo.c:12');sys.exit(2)")
        code, out, _ = run_script("trial-build", "--repo", str(self.repo), "--build-command", command)
        self.assertEqual(code, 0, out)
        self.assertFalse(out["build_ok"])
        self.assertIn("compile error: foo.c:12", out["log_tail"])

    def test_svn_working_copy_is_exported_outside(self) -> None:
        if not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn がないので飛ばします")
        server = self.tmp / "svnrepo"
        subprocess.run(["svnadmin", "create", str(server)], check=True, capture_output=True)
        work = self.tmp / "wc"
        subprocess.run(["svn", "checkout", "-q", server.as_uri(), str(work)], check=True, capture_output=True)
        (work / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        init(work, "--output-dir", str(self.tmp / "svnout"))
        subprocess.run(["svn", "add", "-q", "main.c", ".coverity-triage"], cwd=work, check=True, capture_output=True)
        subprocess.run(["svn", "commit", "-q", "-m", "first"], cwd=work, check=True, capture_output=True)
        code, out, _ = run_script("trial-build", "--repo", str(work),
                                  "--build-command", self.python_line("open('built.txt','w').write('ok')"))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["build_ok"], out)
        self.assertEqual(out["revision"], "1")
        self.assertTrue((Path(out["built_in"]) / "main.c").is_file())
        self.assertFalse((work / "built.txt").exists())

    def test_build_folder_must_stay_inside_the_copy(self) -> None:
        code, out, _ = run_script("trial-build", "--repo", str(self.repo), "--build-command", "make",
                                  "--build-dir", os.pardir)
        self.assertEqual(code, 1)
        self.assertIn("相対パス", out["error"])


@needs_uv
class Output(TempDirCase):
    def test_errors_are_one_json_object_without_traceback(self) -> None:
        for args in (("nosuch",), ("init", "--repo", str(self.tmp)), ("set", "--repo", str(self.tmp), "--value", "x")):
            with self.subTest(args=args):
                code, out, err = run_script(*args)
                self.assertEqual(code, 1)
                self.assertFalse(out["ok"])
                self.assertIsInstance(out["problems"], list)
                self.assertNotIn("Traceback", err)

    def test_narrow_encoding_escapes_only_what_it_cannot_write(self) -> None:
        env = base_env(PYTHONIOENCODING="ascii")
        code, out, _ = run_script("detect", "--repo", str(self.tmp), env=env)
        self.assertEqual(code, 0)
        self.assertIn("リポジトリ", out["vcs_note"])
        if GIT:
            env = base_env(PYTHONIOENCODING="cp932")
            _, out, _ = run_script("detect", "--repo", str(self.tmp), env=env)
            self.assertIn("リポジトリ", out["vcs_note"])


class EnvVars(unittest.TestCase):
    """Windows のユーザーの環境変数（HKCU\\Environment）の読み方。winreg の代わりの部品で確かめる"""

    def load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("setup_envvars_under_test", SKILL / "scripts" / "envvars.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_unexpanded_and_empty_values_count_as_absent(self) -> None:
        envvars = self.load()
        old = {k: os.environ.get(k) for k in ("T_A", "T_B", "T_C")}
        try:
            os.environ.update({"T_A": "${T_A}", "T_B": "  ", "T_C": "v"})
            self.assertIsNone(envvars.get_env("T_A") if sys.platform != "win32" else None)
            self.assertIsNone(envvars.get_env("T_B") if sys.platform != "win32" else None)
            self.assertEqual(envvars.get_env("T_C"), "v")
            self.assertEqual(envvars.where("T_C"), "process")
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_reads_the_user_variables_on_windows(self) -> None:
        envvars = self.load()

        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeWinreg:
            HKEY_CURRENT_USER = object()

            @staticmethod
            def OpenKey(root, path):
                assert path == "Environment"
                return FakeKey()

            @staticmethod
            def QueryValueEx(key, name):
                if name == "SAVED_IN_REGISTRY":
                    return "from-registry", 1
                raise OSError("missing")

        saved_platform, saved_module = sys.platform, sys.modules.get("winreg")
        try:
            sys.platform = "win32"
            sys.modules["winreg"] = FakeWinreg
            os.environ.pop("SAVED_IN_REGISTRY", None)
            self.assertEqual(envvars.get_env("SAVED_IN_REGISTRY"), "from-registry")
            self.assertEqual(envvars.where("SAVED_IN_REGISTRY"), "user")
            self.assertIsNone(envvars.get_env("NOT_SAVED_ANYWHERE"))
        finally:
            sys.platform = saved_platform
            if saved_module is None:
                sys.modules.pop("winreg", None)
            else:
                sys.modules["winreg"] = saved_module


if __name__ == "__main__":
    unittest.main()
