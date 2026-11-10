# -*- coding: utf-8 -*-
"""skill-config のスクリプト（scripts/config_tool.py）のテスト。スキルのフォルダだけをコピーしても動く。

設定するスキルの代わりに、一時的な作業フォルダに .sample-tool/config.yaml を置いて使う。
書いたファイルは、PyYAML で読んで確かめる（tests/requirements.txt）。

  python3 -m unittest discover -s tests（Windows は py -3）
"""

import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

TESTS = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(TESTS)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "config_tool.py")
TARGET = "sample-tool"
PASSWORD = "Pw%x-secret"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

sys.dont_write_bytecode = True  # スキルの scripts/ に __pycache__ を作らない
_spec = importlib.util.spec_from_file_location("config_tool_script", SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

CONFIG = """\
# sample-tool の設定。
# 「項目: 値」の形で書く。

# 機器の IP アドレスかホスト名（必須）。
# 例：192.168.1.50
# 例：device.local
host:

# ログインのパスワード（秘密）。
password: {password}

# パスワードを入れた環境変数の名前。あれば password より優先する。
# 例：DEVICE_PASSWORD
password_env:

# FTPS を使うか。
# 選べる値：no / yes
tls: no

# 送るファイル（必須）。2つ以上なら、次の行から「- 」に続けて1つずつ書く。
# 例：build/app
files:
  - build/app
  - build/app.map

# 説明だけの項目。
rate: 50%
""".format(password=PASSWORD)


class Project(object):
    """一時的な作業フォルダと、設定するスキルの設定ファイル。"""

    def __init__(self, test, text=CONFIG, newline="\n", bom=False, script=SCRIPT):
        self.dir = tempfile.mkdtemp(prefix="skill-config-test-")
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.test = test
        self.script = script
        self.folder = os.path.join(self.dir, "." + TARGET)
        self.config = os.path.join(self.folder, "config.yaml")
        if text is not None:
            os.makedirs(self.folder)
            self.write((b"\xef\xbb\xbf" if bom else b"") + text.replace("\n", newline).encode("utf-8"))

    def write(self, data):
        with open(self.config, "wb") as fh:
            fh.write(data)

    def raw(self):
        with open(self.config, "rb") as fh:
            return fh.read()

    def text(self):
        return self.raw().decode("utf-8-sig")

    def values(self):
        """PyYAML で読んだ値。数や true も、文字のまま読む。"""
        with open(self.config, encoding="utf-8-sig") as fh:
            return yaml.load(fh, Loader=yaml.BaseLoader) or {}

    def run(self, *args, stdin=None):
        """スクリプトを動かし、(終了コード, 結果の JSON) を返す。標準出力は ASCII だけで、JSON 1つ。
        stdin がなければ、AI の CLI のコマンドと同じく /dev/null（NUL）につなぐ。"""
        feed = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
        r = subprocess.run([sys.executable, self.script] + list(args) + ["--workdir", self.dir],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, **feed)
        self.test.assertTrue(all(b < 128 for b in r.stdout), "標準出力が ASCII だけではありません")
        data = json.loads(r.stdout.decode("ascii"))
        for key in ("ok", "message", "problems", "config"):
            self.test.assertIn(key, data)
        return r.returncode, data

    def show(self):
        return self.run("show", TARGET)

    def set(self, key, value):
        data = value if isinstance(value, bytes) else value.encode("utf-8")
        return self.run("set", TARGET, key, "--stdin", stdin=data)

    def empty(self, key):
        return self.run("set", TARGET, key, "--empty")

    def item(self, key):
        return next(i for i in self.show()[1]["items"] if i["key"] == key)


def tree(folder):
    return sorted(os.path.relpath(os.path.join(d, n), folder) for d, dirs, files in os.walk(folder)
                  for n in dirs + files)


class ShowTest(unittest.TestCase):
    def test_reads_marks_from_comments(self):
        p = Project(self)
        code, data = p.show()
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        items = {i["key"]: i for i in data["items"]}
        self.assertEqual(list(items), ["host", "password", "password_env", "tls", "files", "rate"])
        self.assertTrue(items["host"]["required"])
        self.assertEqual(items["host"]["examples"], ["192.168.1.50", "device.local"])
        self.assertEqual(items["host"]["comment"], "機器の IP アドレスかホスト名（必須）。")
        self.assertEqual(items["tls"]["choices"], ["no", "yes"])
        self.assertEqual(items["files"]["value"], ["build/app", "build/app.map"])
        self.assertEqual(items["rate"]["value"], "50%")
        self.assertFalse(items["rate"]["required"] or items["rate"]["secret"])
        self.assertEqual(items["password"]["env_key"], "password_env")
        self.assertIsNone(items["host"]["env_key"])
        self.assertEqual(data["missing"], ["host"])
        self.assertEqual(data["config"], p.config)
        self.assertNotIn("file", data)

    def test_secret_is_masked(self):
        p = Project(self)
        code, data = p.show()
        self.assertNotIn(PASSWORD, json.dumps(data))
        self.assertEqual(p.item("password")["value"], "***")
        self.assertTrue(p.item("password")["secret"])

    def test_empty_secret_shows_empty(self):
        p = Project(self, CONFIG.replace("password: " + PASSWORD, "password:"))
        self.assertEqual(p.item("password")["value"], "")

    def test_password_like_name_is_secret_without_mark(self):
        p = Project(self, "# 鍵。\napi_token: abc\n# （秘密）の値を入れる環境変数の名前。\ntoken_env: X\n")
        code, data = p.show()
        items = {i["key"]: i for i in data["items"]}
        self.assertEqual(items["api_token"]["value"], "***")
        self.assertFalse(items["token_env"]["secret"])

    def test_secret_name_is_matched_by_words(self):
        names = ["max_tokens", "login_pass", "private_key", "pwd", "api_token", "bypass", "client.secret"]
        p = Project(self, "".join("%s: x\n" % n for n in names))
        secret = {i["key"]: i["secret"] for i in p.show()[1]["items"]}
        self.assertEqual(secret, {"max_tokens": False, "login_pass": True, "private_key": True, "pwd": True,
                                  "api_token": True, "bypass": False, "client.secret": True})

    def test_half_width_marks(self):
        p = Project(self, "# 機器の名前 (必須)\n# 補足\n# 例: dev1\nhost:\n"
                          "# 合言葉 (秘密)\nword: abc\n# 動かし方。\n# 選べる値: on ／ off\nmode: on\n")
        items = {i["key"]: i for i in p.show()[1]["items"]}
        self.assertEqual((items["host"]["required"], items["host"]["examples"]), (True, ["dev1"]))
        self.assertEqual(items["host"]["comment"], "機器の名前 (必須)\n補足")
        self.assertEqual((items["word"]["secret"], items["word"]["value"]), (True, "***"))
        self.assertEqual(items["mode"]["choices"], ["on", "off"])
        self.assertEqual(p.set("mode", "OFF")[1]["value"], "off")

    def test_required_secret_with_env_name_is_not_missing(self):
        text = CONFIG.replace("（秘密）", "（必須）（秘密）").replace("password: " + PASSWORD, "password:")
        p = Project(self, text)
        self.assertIn("password", p.show()[1]["missing"])
        p.set("password_env", "DEVICE_PASSWORD")
        self.assertNotIn("password", p.show()[1]["missing"])

    def test_missing_file_says_to_use_setup(self):
        p = Project(self, text=None)
        code, data = p.show()
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertIn(TARGET, data["message"])
        self.assertIn("setup " + TARGET, data["message"])
        self.assertEqual(tree(p.dir), [])

    def test_show_creates_nothing(self):
        p = Project(self)
        before = tree(p.dir)
        p.show()
        self.assertEqual(tree(p.dir), before)

    def test_bom_and_crlf_file(self):
        p = Project(self, newline="\r\n", bom=True)
        code, data = p.show()
        self.assertEqual(code, 0)
        self.assertEqual(data["items"][0]["key"], "host")
        self.assertEqual(p.item("files")["value"], ["build/app", "build/app.map"])

    def test_duplicate_key_is_a_problem(self):
        code, data = Project(self, "host: a\nhost: b\n").show()
        self.assertEqual(code, 2)
        self.assertIn("2 行目", data["message"])
        self.assertTrue(data["problems"])

    def test_bad_skill_name_is_refused(self):
        code, data = Project(self).run("show", "../x")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])


class SetTest(unittest.TestCase):
    def test_round_trip_changes_only_that_line(self):
        p = Project(self)
        code, data = p.set("host", "192.168.1.50\n")
        self.assertEqual((code, data["ok"], data["changed"], data["value"]), (0, True, True, "192.168.1.50"))
        self.assertEqual(p.text(), CONFIG.replace("host:\n", "host: 192.168.1.50\n"))
        self.assertEqual(p.item("host")["value"], "192.168.1.50")
        self.assertEqual(p.show()[1]["missing"], [])

    def test_values_that_need_quotes_are_quoted(self):
        # そのまま書くと YAML として別の意味になる値は、'…' で囲んで書く
        p = Project(self)
        for value in ("[#$] $", "a: b", "# x", "it's", "*.c", "x #y", "'q'", '"C:\\ti"', "true", "0755", "C:\\ti\\env.bat"):
            self.assertEqual(p.set("rate", value)[0], 0, value)
            self.assertEqual(p.values()["rate"], value)
            self.assertEqual(p.item("rate")["value"], value)
        values = ["echo a: b", "# c", "grep 'x y' f"]
        self.assertEqual(p.set("files", "\n".join(values))[0], 0)
        self.assertEqual(p.values()["files"], values)
        self.assertEqual(p.item("files")["value"], values)
        self.assertEqual(p.item("host")["comment"], "機器の IP アドレスかホスト名（必須）。")

    def test_percent_and_japanese_value(self):
        p = Project(self)
        self.assertEqual(p.set("rate", "100% 二階の台所")[0], 0)
        self.assertEqual(p.values()["rate"], "100% 二階の台所")

    def test_multiple_lines_become_a_list(self):
        p = Project(self)
        code, data = p.set("files", "out/a\n\n  out/b  \nout/c\n")
        self.assertEqual(data["value"], ["out/a", "out/b", "out/c"])
        self.assertIn("files:\n  - out/a\n  - out/b\n  - out/c\n\n# 説明だけの項目。", p.text())
        self.assertEqual(p.values()["files"], ["out/a", "out/b", "out/c"])
        p.set("files", "out/one")
        self.assertIn("files: out/one\n\n# 説明だけの項目。", p.text())

    def test_indented_comment_inside_a_list_is_kept(self):
        p = Project(self, CONFIG.replace("  - build/app\n", "  - build/app\n  # 残すコメント\n"))
        self.assertEqual(p.set("files", "x")[0], 0)
        self.assertIn("files: x\n  # 残すコメント\n", p.text())
        self.assertEqual(p.values()["files"], "x")

    def test_empty_value(self):
        p = Project(self)
        self.assertEqual(p.empty("files")[0], 0)
        self.assertIn("files:\n\n", p.text())
        self.assertEqual(p.show()[1]["missing"], ["host", "files"])

    def test_missing_stdin_does_not_clear(self):
        # heredoc を付け忘れると、標準入力は空になる。空にする依頼と取り違えない
        p = Project(self, CONFIG.replace("host:", "host: 10.0.0.1"))
        before = p.raw()
        for args, stdin in ((("--stdin",), None), (("--stdin",), b""), (("--stdin",), b"\n  \r\n"),
                            (("--stdin", "--empty"), b"a"), ((), b"a")):
            code, data = p.run("set", TARGET, "host", *args, stdin=stdin)
            self.assertEqual((code, data["ok"]), (2, False), args)
            self.assertIn("--empty", data["message"])
            self.assertEqual(p.raw(), before)

    def test_choices_are_checked(self):
        p = Project(self)
        before = p.raw()
        for code, data in (p.set("tls", "maybe"), p.set("tls", "no\nyes"), p.empty("tls")):
            self.assertEqual(code, 2)
            self.assertIn("no / yes", data["message"])
        self.assertEqual(p.raw(), before)
        self.assertEqual(p.set("tls", "YES")[1]["value"], "yes")
        self.assertEqual(p.values()["tls"], "yes")

    def test_unknown_key_is_refused(self):
        p = Project(self)
        before = p.raw()
        code, data = p.set("hots", "a")
        self.assertEqual(code, 2)
        self.assertIn("host", data["message"])
        self.assertEqual(p.raw(), before)

    def test_secret_value_is_refused_but_can_be_emptied(self):
        p = Project(self)
        before = p.raw()
        code, data = p.set("password", "new-secret")
        self.assertEqual(code, 2)
        self.assertNotIn("new-secret", json.dumps(data))
        self.assertIn("password_env", data["message"])
        self.assertEqual(p.raw(), before)
        code, data = p.empty("password")
        self.assertEqual((code, data["value"]), (0, ""))
        self.assertIn("password:\n", p.text())

    def test_env_name_is_written(self):
        p = Project(self)
        self.assertEqual(p.set("password_env", "DEVICE_PASSWORD")[0], 0)
        self.assertEqual(p.values()["password_env"], "DEVICE_PASSWORD")

    def test_password_in_env_name_item_is_refused(self):
        p = Project(self)
        before = p.raw()
        for bad in ("S3cr3t!x", "1ABC", "A\nB"):
            code, data = p.set("password_env", bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("環境変数の名前", data["message"])
            self.assertNotIn("S3cr3t", json.dumps(data))
        self.assertEqual(p.raw(), before)

    def test_env_item_without_secret_pair_takes_any_value(self):
        # 名前が _env で終わっても、（秘密）の項目と対でなければ、ふつうの項目
        p = Project(self, "# SDK の環境を整えるスクリプト。\nsdk_env:\n")
        self.assertEqual(p.set("sdk_env", "/opt/sdk/environment-setup-armv7")[0], 0)

    def test_bom_and_crlf_are_kept(self):
        p = Project(self, newline="\r\n", bom=True)
        p.set("host", "a")
        p.set("files", "x\ny")
        raw = p.raw()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"host: a\r\n", raw)
        self.assertIn(b"files:\r\n  - x\r\n  - y\r\n", raw)
        self.assertEqual(p.values()["files"], ["x", "y"])

    def test_last_line_without_newline(self):
        p = Project(self, "# 送るファイル。\nfiles: a")
        p.set("files", "x\ny")
        self.assertEqual(p.text(), "# 送るファイル。\nfiles:\n  - x\n  - y")

    def test_same_value_does_not_rewrite(self):
        p = Project(self, CONFIG.replace("tls: no", "tls:   'no'"))
        before = p.raw()
        code, data = p.set("tls", "no")
        self.assertEqual((code, data["changed"]), (0, False))
        self.assertEqual(p.raw(), before)

    def test_layout_that_cannot_be_rewritten_is_left_alone(self):
        # リストの途中の、行頭からのコメントは、書き換えると次の項目のコメントになってしまう
        p = Project(self, "files:\n  - a\n# 途中のコメント\n  - b\nhost: x\n")
        before = p.raw()
        code, data = p.set("files", "x")
        self.assertEqual(code, 2)
        self.assertIn("元のまま", data["message"])
        self.assertEqual(p.raw(), before)

    def test_refusals_leave_the_file_and_do_not_echo(self):
        leak = "Pw-LEAK"
        cases = [  # (設定ファイル, 引数, 標準入力, 終了コード, message の一部)
            (CONFIG, ("set", TARGET, "host", "--stdin"), "a\x07b", 2, "制御文字"),
            (CONFIG, ("set", TARGET, "HOST", "--stdin"), "a", 2, "項目 HOST はありません"),
            ("host: 台所\n".encode("cp932"), ("show", TARGET), None, 2, "UTF-8"),
            ("host: a\npassword " + leak + "\n", ("set", TARGET, "host", "--stdin"), "b", 2, "2 行目"),
            ("host: a\nprompt: [" + leak + "]\n", ("show", TARGET), None, 2, "'…' で囲んでください"),
            ("  host: a\n", ("show", TARGET), None, 2, "1 行目"),
            ("[settings]\nhost = a\n", ("show", TARGET), None, 2, "1 行目"),  # 前の形（INI）のファイル
        ]
        for text, args, stdin, want, phrase in cases:
            p = Project(self, text if isinstance(text, str) else None)
            if not isinstance(text, str):
                os.makedirs(p.folder)
                p.write(text)
            before = p.raw()
            code, data = p.run(*args, stdin=None if stdin is None else stdin.encode("utf-8"))
            self.assertEqual(code, want, phrase)
            self.assertIn(phrase, data["message"])
            self.assertEqual(data["problems"], [data["message"]])
            self.assertEqual(p.raw(), before, phrase)
            self.assertNotIn(leak, json.dumps(data, ensure_ascii=False))

    def test_gitignore_is_placed_before_writing(self):
        p = Project(self)
        p.set("host", "a")
        with open(os.path.join(p.folder, ".gitignore"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "*\n")

    def test_set_leaves_no_log(self):
        p = Project(self)
        p.set("host", "a")
        self.assertEqual(tree(p.dir), [".sample-tool", ".sample-tool/.gitignore", ".sample-tool/config.yaml"])


PYPROJECT = '[project]\nname = "alpha"\nversion = "0.0.0"\nrequires-python = ">=3.8"\ndependencies = []\n'


class SetupTest(unittest.TestCase):
    """setup は、隣のスキルのひな形から設定ファイルを作り、uv で依存パッケージを入れる。"""

    def skills(self, templates=("alpha", "beta"), project=None):
        """skill-config の写しと、ひな形を持つ隣のスキルを置いたフォルダを作る。"""
        root = tempfile.mkdtemp(prefix="skill-config-skills-")
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copytree(SKILL_DIR, os.path.join(root, "skill-config"), ignore=shutil.ignore_patterns("__pycache__"))
        for name in templates:
            os.makedirs(os.path.join(root, name, "assets"))
            with open(os.path.join(root, name, "assets", "config-template.yaml"), "w", encoding="utf-8") as fh:
                fh.write("# %s の項目（必須）。\nhost:\n" % name)
            if project is not None:
                with open(os.path.join(root, name, "pyproject.toml"), "w", encoding="utf-8") as fh:
                    fh.write(project)
        os.makedirs(os.path.join(root, "no-template"))
        return os.path.join(root, "skill-config", "scripts", "config_tool.py")

    def test_creates_config_files_from_templates(self):
        p = Project(self, text=None, script=self.skills())
        code, data = p.run("setup")
        self.assertEqual(code, 0, data["message"])
        self.assertEqual([(s["name"], s["created"], s["packages"]) for s in data["skills"]],
                         [("alpha", True, True), ("beta", True, True)])
        self.assertEqual(tree(p.dir), [".alpha", ".alpha/.gitignore", ".alpha/config.yaml",
                                       ".beta", ".beta/.gitignore", ".beta/config.yaml"])
        self.assertEqual(p.run("show", "alpha")[1]["missing"], ["host"])

    def test_existing_config_is_kept(self):
        p = Project(self, text=None, script=self.skills())
        p.run("setup")
        p.run("set", "alpha", "host", "--stdin", stdin=b"a")
        code, data = p.run("setup")
        self.assertEqual([s["created"] for s in data["skills"]], [False, False])
        with open(os.path.join(p.dir, ".alpha", "config.yaml"), encoding="utf-8") as fh:
            self.assertIn("host: a", fh.read())

    def test_one_skill_only(self):
        p = Project(self, text=None, script=self.skills())
        code, data = p.run("setup", "beta")
        self.assertEqual([s["name"] for s in data["skills"]], ["beta"])
        self.assertEqual(tree(p.dir), [".beta", ".beta/.gitignore", ".beta/config.yaml"])

    def test_skill_without_template_is_refused(self):
        p = Project(self, text=None, script=self.skills())
        code, data = p.run("setup", "no-template")
        self.assertEqual(code, 2)
        self.assertIn("alpha、beta", data["message"])
        self.assertEqual(tree(p.dir), [])

    @unittest.skipUnless(shutil.which("uv"), "uv がないので飛ばす")
    def test_packages_are_synced_with_uv(self):
        p = Project(self, text=None, script=self.skills(("alpha",), PYPROJECT))
        code, data = p.run("setup")
        self.assertEqual(code, 0, data["message"])
        self.assertTrue(data["skills"][0]["packages"])

    def test_uv_failure_is_reported(self):
        p = Project(self, text=None, script=self.skills(("alpha",), PYPROJECT))
        failed = subprocess.CompletedProcess([], 1)
        with mock.patch.object(tool, "SKILLS_DIR", os.path.dirname(os.path.dirname(os.path.dirname(p.script)))), \
                mock.patch.object(tool.subprocess, "run", return_value=failed), \
                mock.patch.object(tool.shutil, "which", return_value="uv"):
            result = {"ok": False, "message": "", "problems": [], "config": None}
            with self.assertRaises(tool.Stop) as stop:
                tool.cmd_setup(None, p.dir, result)
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("依存パッケージ", stop.exception.message)
        self.assertFalse(result["skills"][0]["packages"])
        self.assertTrue(os.path.isfile(os.path.join(p.dir, ".alpha", "config.yaml")))


class YamlTest(unittest.TestCase):
    """スクリプトの読み取りが、PyYAML と同じ値を読むこと。読めない書き方は、断ること。"""

    SAMPLE = """\
# コメント
host: 192.168.1.50
port: 21
empty:
only_comment:   # コメントだけ
prompt: '[#$] $'
windows: C:\\ti\\sdk\\setenv.bat
double: "a \\"b\\" c\\\\d"
single: 'it''s: ok # コメントではない'
url: socket://192.168.1.10:4001
command: make -j4 && echo done  # ビルド
regex: root@myboard:.*# $
percent: 100% 二階
files:
  - build/app
  - 'conf/my app.conf'
  # リストの中のコメント

  - last
commands:
- uname -a
- dmesg | tail -n 20
- grep "a b" /var/log/messages
tls: true
"""

    def test_reads_the_same_values_as_pyyaml(self):
        ours = {key: value for key, value, _, _ in tool.read_yaml(self.SAMPLE.split("\n"))}
        self.assertEqual(ours, yaml.load(self.SAMPLE, Loader=yaml.BaseLoader))
        self.assertEqual(ours["files"], ["build/app", "conf/my app.conf", "last"])

    def test_refuses_what_it_does_not_read(self):
        for text in ("prompt: [#$] $", "a: b: c", "a: 'x", 'a: "C:\\ti"', "  a: b", "- x", "a: 1\na: 2",
                     "a: x\n  - y", "---", "a: *x", "a: |", "a: {b: c}", "a: b:", "[settings]", "a = b"):
            with self.assertRaises(ValueError, msg=text):
                tool.read_yaml(text.split("\n"))


def run_in_process(p, key, value=None, tty=False):
    """このプロセスの中でスクリプトの main を動かす（os.replace などを差し替えるため）。"""
    out = io.StringIO()
    stdin = mock.Mock()
    stdin.buffer = io.BytesIO((value or "").encode("utf-8"))
    stdin.buffer.isatty = lambda: tty
    with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out):
        code = tool.main(["set", TARGET, key, "--stdin", "--workdir", p.dir])
    return code, json.loads(out.getvalue())


class AtomicWriteTest(unittest.TestCase):
    """書き込みの途中で失敗しても、元のファイルが残り、一時ファイルも残らないこと。"""

    run_in_process = staticmethod(run_in_process)

    def test_failed_replace_keeps_the_file(self):
        p = Project(self)
        before = p.raw()
        with mock.patch.object(tool.os, "replace", side_effect=PermissionError(13, "Permission denied")):
            code, data = self.run_in_process(p, "host", "a")
        self.assertEqual(code, 1)
        self.assertIn("元のまま", data["message"])
        self.assertEqual(p.raw(), before)
        self.assertEqual(sorted(os.listdir(p.folder)), [".gitignore", "config.yaml"])

    def test_success_leaves_no_temporary_file(self):
        p = Project(self)
        replace = mock.Mock(wraps=os.replace)
        with mock.patch.object(tool.os, "replace", replace):
            code, data = self.run_in_process(p, "host", "a")
        self.assertEqual(code, 0)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(sorted(os.listdir(p.folder)), [".gitignore", "config.yaml"])

    def test_read_only_file_is_left_alone(self):
        p = Project(self)
        before = p.raw()
        os.chmod(p.config, stat.S_IREAD)
        self.addCleanup(os.chmod, p.config, stat.S_IREAD | stat.S_IWRITE)
        code, data = p.set("host", "a")
        self.assertEqual(code, 1)
        self.assertIn("読み取り専用", data["message"])
        self.assertEqual(p.raw(), before)


class InputTest(unittest.TestCase):
    def test_terminal_stdin_is_not_waited_for(self):
        p = Project(self)
        before = p.raw()
        code, data = run_in_process(p, "host", "a", tty=True)
        self.assertEqual(code, 2)
        self.assertIn("標準入力で渡されていません", data["message"])
        self.assertEqual(p.raw(), before)

    def test_unexpected_error_is_reported(self):
        p = Project(self)
        with mock.patch.object(tool, "load", side_effect=RuntimeError("boom")):
            code, data = run_in_process(p, "host", "a")
        self.assertEqual(code, 1)
        self.assertIn("思わぬエラー", data["message"])
        self.assertIn("RuntimeError", data["message"])

    def test_decode_input(self):
        self.assertEqual(tool.decode_input("﻿台所\r\n".encode("utf-8"), []), "台所\r\n")
        self.assertEqual(tool.decode_input("台所".encode("cp932"), ["cp932"]), "台所")

    def test_stdin_with_bom_from_powershell(self):
        p = Project(self)
        p.set("host", "﻿二階\r\n".encode("utf-8"))
        self.assertEqual(p.values()["host"], "二階")


class PowerShellTest(unittest.TestCase):
    """SKILL.md の PowerShell の例を、そのまま動かす（Windows PowerShell 5.1 は日本語を ? に変えやすい）。"""

    @unittest.skipUnless(POWERSHELL, "PowerShell（powershell か pwsh）がないので飛ばす")
    def test_skill_md_example_passes_japanese(self):
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            block = re.search(r"```powershell\n(.*?)```", fh.read(), re.S).group(1)
        p = Project(self)

        def quote(text):
            return "'" + text.replace("'", "''") + "'"
        block = (block.replace('uv run --project "<このスキルのフォルダ>" ', "& %s " % quote(sys.executable))
                 .replace('"<このスキルのフォルダ>/scripts/config_tool.py"', quote(SCRIPT))
                 .replace("<スキルの名前>", TARGET).replace("<項目>", "host").replace("<値>", "二階の台所")
                 .replace("--stdin", "--stdin --workdir " + quote(p.dir)))
        self.assertNotIn("<", block, "SKILL.md の PowerShell の例の書き方が変わった")
        ps1 = os.path.join(p.dir, "set.ps1")
        with open(ps1, "w", encoding="utf-8-sig") as fh:  # 5.1 は BOM がないと cp932 で読む
            fh.write(block)
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", ps1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        data = json.loads(r.stdout.decode("ascii"))
        self.assertTrue(data["ok"], data["message"])
        self.assertEqual(p.values()["host"], "二階の台所")


if __name__ == "__main__":
    unittest.main()
