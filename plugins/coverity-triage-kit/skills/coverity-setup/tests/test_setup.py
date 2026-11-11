"""setup.py as a user meets it: init and check in a git and in an svn working tree (standard library only)."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup.py"
# setup.py needs PyYAML: this Python when it has it, else uv as in real use
RUN = [sys.executable] if importlib.util.find_spec("yaml") else [shutil.which("uv") or "uv", "run", "--native-tls", "--quiet"]
URL, STREAM = "https://coverity.example.co.jp:8443", "app-main"
SET = {"COV_USER": "tester-name", "COV_AUTH_KEY": "s3cret-key-value"}
TEMPLATE = """\
coverity:
  # Coverity Connect の URL
  url: "<URL>"
  # 対象のストリーム
  stream: "<ストリーム>"

conditions:
  # 取得するチェッカー名。* と ? のワイルドカードを使用できます（例: ["NULL_*", "MISRA C-2012 Rule 10.*"]）。空のときは、すべてのチェッカー
  checkers: []
  # 取得する Impact（High / Medium / Low）。空のときは、すべて
  impacts: []
  # 取得する警告の分類（Classification）
  classification: [Unclassified]
  # 取得する件数の上限
  limit: 20

# 結果フォルダーの場所（リポジトリからの相対パス、または絶対パス。リポジトリの外を指定します）
output_dir: ../coverity-triage-out
# 3段階目で同時に動かすエージェントの数（1〜8）
parallel: 3
# git で修正を取り込む先のブランチ。空のときは、自動で判定します
base_branch: ''

build:
  # ビルドのコマンド（下の2つのオプションで使用します）。例: 'make -j4'
  command: ''
  # オプション: 修正を適用した状態でビルドして確認します
  check_fixes: false
  # オプション: 取得の前に、解析と Coverity への登録を行います
  initial_analysis: false
  # オプションの解析で cov-analyze に渡す引数
  analyze_args: '--all'
"""
BLOCKS = {"powershell": """\
[Environment]::SetEnvironmentVariable('COV_USER', '<ユーザー名>', 'User')
$k = Read-Host 'COV_AUTH_KEY（認証キー。入力は表示されません）' -AsSecureString
[Environment]::SetEnvironmentVariable('COV_AUTH_KEY', [Net.NetworkCredential]::new('', $k).Password, 'User')""",
          "bash": """\
read -r -p 'COV_USER: ' COV_USER
read -r -s -p 'COV_AUTH_KEY（認証キー。入力は表示されません）: ' COV_AUTH_KEY; echo
export COV_USER COV_AUTH_KEY      # このターミナルから copilot を起動します"""}
NOT_A_REPO = "git または svn のリポジトリの最上位フォルダーで実行してください"


def setup(*args, cwd=None, **env) -> dict:
    """Run setup.py: it exits 0 and prints one JSON object in UTF-8 that holds no value of a credential."""
    clean = {k: v for k, v in os.environ.items() if k not in SET}
    proc = subprocess.run([*RUN, str(SCRIPT), *map(str, args)], capture_output=True, cwd=cwd, timeout=300,
                          env={**clean, "PYTHONDONTWRITEBYTECODE": "1", **env})
    text = proc.stdout.decode("utf-8")
    assert proc.returncode == 0 and not [v for v in SET.values() if v in text + proc.stderr.decode("utf-8", "replace")], text
    return json.loads(text)


class Git(unittest.TestCase):
    kind = "git"

    def setUp(self):
        if self.kind == "svn" and not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn / svnadmin コマンドがありません")
        folder = tempfile.TemporaryDirectory(prefix="setup-test-")
        self.addCleanup(folder.cleanup)
        self.top = Path(folder.name).resolve()
        made = [["git", "init", "-q", "made"]] if self.kind == "git" else [
            ["svnadmin", "create", "svnrepo"], ["svn", "checkout", "-q", (self.top / "svnrepo").as_uri(), "made"]]
        for line in made:
            subprocess.run(line, check=True, capture_output=True, cwd=self.top)
        self.repo = self.top / "リポジトリ 1"  # a space and Japanese in the path (svn in a C locale cannot make it)
        (self.top / "made").rename(self.repo)
        self.file = self.repo / ".coverity-triage" / "config.yaml"
        self.call = [{"tool": "coverity_check_connection", "arguments": {"repo_root": self.repo.as_posix()}}]

    def check(self, settings: str | None = None, **env) -> dict:
        """The reply of check, for a settings file with these lines after the two required keys."""
        if settings is not None:
            self.file.parent.mkdir(exist_ok=True)
            self.file.write_text(f"coverity:\n  url: {URL}\n  stream: {STREAM}\n{settings}", encoding="utf-8")
        return setup("check", "--repo", self.repo, **{**SET, **env})

    def test_init_writes_the_template_and_checks(self):
        out = setup("init", "--repo", self.repo.name, "--url", URL + "/", "--stream", STREAM, cwd=self.top)
        self.assertEqual(self.file.read_bytes(), TEMPLATE.replace("<URL>", URL).replace("<ストリーム>", STREAM).encode())
        self.assertEqual(out, {
            "ok": False, "config_file": self.file.as_posix(), "created": True, "vcs": self.kind,
            "checks": [{"name": "設定ファイル", "ok": True, "message": "設定ファイルを確認しました"},  # it parses, no key is unknown
                       {"name": "認証情報", "ok": False, "message": "環境変数 COV_USER と COV_AUTH_KEY を設定してください"}],
            "credentials": BLOCKS, "message": f"設定ファイルを作成しました: {self.file.as_posix()}", "next": []})

    def test_awkward_url_and_stream_are_read_back_as_given(self):
        url, stream = "https://cov.example.co.jp:8443/a b#c?x=%20&y='1'", 'アプリ　"main": #1 \\ {x} [y] - \'z\''
        self.assertTrue(setup("init", "--repo", self.repo, "--url", url, "--stream", f" {stream} ")["created"])
        self.assertIn(f"  url: {json.dumps(url)}\n", self.file.read_text(encoding="utf-8"))
        out = setup("init", "--repo", self.repo, "--url", url, "--stream", stream, **SET)  # the same values again
        self.assertEqual((out["ok"], out["created"], out["message"]), (True, False, "設定を確認しました"))
        self.assertEqual((out["fetch"], "credentials" in out), (self.call, False))

    def test_an_existing_settings_file_is_never_rewritten(self):
        self.file.parent.mkdir()
        self.file.write_bytes("# チームの設定\r\ncoverity: {url: 'https://old.example.co.jp', stream: old}\r\n".encode())
        before = (self.file.read_bytes(), self.file.stat().st_mtime_ns)
        out = setup("init", "--repo", self.repo, "--url", URL, "--stream", STREAM)
        self.assertEqual((self.file.read_bytes(), self.file.stat().st_mtime_ns), before)
        self.assertEqual((out["created"], out["message"]), (False, (
            "設定ファイルは既にあります。URL やストリームを変更するときは、"
            f"{self.file.as_posix()} の coverity.url と coverity.stream を編集してください")))

    def test_outside_the_top_folder_nothing_is_written(self):
        (self.repo / "src").mkdir()
        for folder in (self.top, self.repo / "src"):
            for args in (["init", "--url", URL, "--stream", STREAM], ["check"]):
                self.assertEqual(setup(*args, "--repo", folder), {"ok": False, "error": NOT_A_REPO, "next": []})
        for url, stream in (("ftp://coverity.example.co.jp", STREAM), (URL, " ")):
            self.assertFalse(setup("init", "--repo", self.repo, "--url", url, "--stream", stream)["ok"])
        self.assertEqual(list(self.top.rglob(".coverity-triage")), [])

    def test_settings_file_check(self):
        shown = self.file.as_posix()
        out = self.check()
        self.assertEqual(out["checks"][0], {"name": "設定ファイル", "ok": False, "message": (
            f"設定ファイルがありません: {shown}。/coverity-triage-kit:coverity-setup <URL> <ストリーム> を実行してください")})
        self.assertEqual((out["ok"], "fetch" in out, out["message"]), (False, False, "設定を確認しました。対応が必要な項目があります"))
        for broken in ("build: [\n", "base_branch: 2026-13-01\n"):
            self.assertTrue(self.check(broken)["checks"][0]["message"].startswith(f"設定ファイルを読み取れません: {shown}（"))
        self.file.write_text(f"coverity:\n  url: {URL}\n  strem: app-main\n", encoding="utf-8")
        self.assertEqual(self.check()["checks"][0]["message"], f"設定ファイルを訂正してください: {shown}。"
                         "coverity.stream を記入してください。使用しないキーがあります: coverity.strem")
        out = self.check("conditions: {impacts: [Critical], limit: 0, checkers: [1]}\nparallel: 9\noutput_dir: [a]\n"
                         "build:\n  check_fixes: 'yes'\n  command: |\n    make\n    make test\n")
        for wrong in ("conditions.impacts（High / Medium / Low のリスト）", "conditions.limit（1 以上の整数）", "parallel（1〜8 の整数）",
                      "conditions.checkers（文字列のリスト）", "output_dir（文字列）", "build.check_fixes（true または false）",
                      "build.command（1行の文字列）"):
            self.assertIn(f"{wrong}の値が正しくありません", out["checks"][0]["message"])
        self.assertEqual((out["ok"], "fetch" in out), (False, False))
        for inside in ("out", (self.repo / "結果 フォルダー").as_posix()):  # relative to the repository, and absolute
            self.assertEqual(self.check(f"output_dir: '{inside}'\n")["checks"][0], {"name": "設定ファイル", "ok": False, "message": (
                f"設定ファイルを訂正してください: {shown}。output_dir は、リポジトリの外のフォルダーを指定してください")})
        self.assertTrue(self.check(f"output_dir: '{self.repo.as_posix()} の結果'\n")["ok"])  # beside the repository
        # a scalar for a list and empty values are fine; unknown keys are named and are not a failure
        out = self.check("conditions: {checkers: NULL_RETURNS, impacts: High, limit: }\nparallel: ''\nbuild:\nfilters: [a]\n"
                         "coverity.project: app\nbuild.command: make\n")
        self.assertEqual(out["checks"][0], {"name": "設定ファイル", "ok": True, "message": (
            "使用しないキーがあります: coverity.project, build.command, filters")})
        self.assertEqual(len(out["checks"]), 2)
        self.assertEqual((out["ok"], out["fetch"], out["message"]), (True, self.call, "設定を確認しました"))

    def test_credentials_check(self):
        for env in ({"COV_USER": "", "COV_AUTH_KEY": ""}, {"COV_AUTH_KEY": " "}, {"COV_USER": ""}):
            out = self.check("", **env)
            self.assertEqual(out["checks"][1], {"name": "認証情報", "ok": False,
                                                "message": "環境変数 COV_USER と COV_AUTH_KEY を設定してください"})
            self.assertEqual((out["ok"], out["credentials"], "fetch" in out), (False, BLOCKS, False))
        out = self.check("")
        self.assertEqual(out["checks"][1], {"name": "認証情報", "ok": True, "message": "COV_USER と COV_AUTH_KEY は設定済みです"})
        self.assertEqual((out["ok"], "credentials" in out, out["fetch"]), (True, False, self.call))

    def test_build_options_check(self):
        tools = self.top / "bin"
        tools.mkdir()
        self.assertEqual(len(self.check("build: {command: make, check_fixes: false}\n")["checks"]), 2)  # no option is on
        out = self.check("build: {check_fixes: true, initial_analysis: true}\n", PATH=str(tools))
        self.assertEqual(out["checks"][2], {"name": "ビルドのオプション", "ok": False, "message": (
            "build.check_fixes、build.initial_analysis を使用するには、設定ファイルの build.command にビルドのコマンドを記入してください。"
            "build.initial_analysis で使用する cov-build、cov-analyze、cov-commit-defects コマンドが見つかりません。"
            "インストールして、PATH を設定してください")})
        self.assertEqual((out["ok"], out["fetch"]), (False, self.call))  # the connection can still be checked
        for name in ("cov-build", "cov-analyze", "cov-commit-defects"):
            (tools / (name + (".cmd" if os.name == "nt" else ""))).touch(0o755)
        out = self.check("build: {command: make, initial_analysis: true}\n", PATH=str(tools))
        self.assertEqual(out["checks"][2], {"name": "ビルドのオプション", "ok": True, "message": "ビルドのオプションを使用できます"})
        self.assertTrue(out["ok"])


class Svn(Git):
    kind = "svn"


if __name__ == "__main__":
    unittest.main()
