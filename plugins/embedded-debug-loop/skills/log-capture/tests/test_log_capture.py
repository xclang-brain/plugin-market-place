# -*- coding: utf-8 -*-
"""log-capture スキル（scripts/capture.py）のテスト。

機器のシリアルコンソールのまね（fake_console.py）を作り、
スクリプトを別のプロセスとして動かして、JSON と終わりのコードを確かめる。
主なテストは、TCP（socket://127.0.0.1:<番号>）でつなぐので、Windows でも動く。
疑似端末（pty）でつなぐテストは、Linux のときだけ動く。
スキルのフォルダで、次のように動かす（Windows では python3 の代わりに py -3）。

  python3 -m unittest discover -s tests
"""

import ast
import codecs
import errno
import hashlib
import io
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

# スキルのフォルダ（scripts/vendor/ も）に __pycache__ を作らない。テストの間ずっと有効にする
# （pyserial は、loop:// などを使うときに、後から読み込むため）
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fake_console import KERNEL_LINE, FakeDevice, Relay  # noqa: E402

# パスは、このテストのファイルの場所から求める（スキルのフォルダの外は見ない）
SKILL_DIR = os.path.dirname(HERE)
SKILL_NAME = os.path.basename(SKILL_DIR)   # フォルダの名前を変えて写したら、その名前
DATA_DIR_NAME = "." + SKILL_NAME           # 作業フォルダの中の、設定とログのフォルダ
SCRIPT = os.path.join(SKILL_DIR, "scripts", "capture.py")
VENDOR_DIR = os.path.join(SKILL_DIR, "scripts", "vendor")
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
README = os.path.join(SKILL_DIR, "README.md")

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

# 終了コードを調べるために、各コマンドの後に送るコマンド（機器のまねは前後の空白を除いて覚える）
MARKER = "echo __LOGCAP_RC=$?"
MARKER_TAG = "__LOGCAP_RC"

# テストで使う、ないはずのシリアルポート
MISSING_PORT = "COM250" if IS_WINDOWS else "/dev/ttyLOGCAP-test-missing"

_ENCODING = []


def console_code_page():
    """Windows で、このプロセスのコンソールの出力の文字コード（cp932 など）。なければ None。
    スクリプトのプロセスは、同じコンソールを受け継ぐ。"""
    if not IS_WINDOWS:
        return None
    import ctypes
    cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    if not cp:
        return None
    try:
        return codecs.lookup("cp%d" % cp).name
    except LookupError:
        return None


def child_encoding():
    """スクリプトのプロセスが、標準出力（パイプ）に使う文字コード。
    Windows でコンソールがあれば、その文字コード。なければ Python の既定。"""
    if not _ENCODING:
        encoding = console_code_page()
        if not encoding:
            p = subprocess.run([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdout.encoding)"],
                               stdout=subprocess.PIPE, timeout=60)
            encoding = p.stdout.decode("ascii").strip() or "utf-8"
        _ENCODING.append(encoding)
    return _ENCODING[0]


_ESCAPE_RE = re.compile(r"\\(?:x([0-9a-f]{2})|u([0-9a-f]{4})|U([0-9a-f]{8}))")


def unescape(text, encoding=None):
    """文字コードで書けずに \\uXXXX の形になった文字を、元に戻す（標準エラーを読むとき）。"""
    if codecs.lookup(encoding or child_encoding()).name == "utf-8":
        return text
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1) or m.group(2) or m.group(3), 16)), text)


def make_tmp(prefix):
    """一時フォルダ。Windows の短い名前（RUNNER~1 など）にならないように、本当の名前にする。"""
    return os.path.realpath(tempfile.mkdtemp(prefix=prefix))

TEMPLATE_KEYS = [
    "port", "baud", "login_user", "login_password", "login_password_env", "prompt",
    "commands", "command_timeout_seconds", "listen_seconds", "logout",
]


def load_module():
    spec = importlib.util.spec_from_file_location("log_capture_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODULE = []


def script_module():
    """スクリプトを1回だけ読み込んで、同じものを返す。"""
    if not _MODULE:
        _MODULE.append(load_module())
    return _MODULE[0]


class CaptureTestCase(unittest.TestCase):
    """機器のまねを立て、スクリプトを動かすための土台。"""

    relay = None
    device = None
    _tmp = None

    @property
    def tmp(self):
        """このテストだけの作業フォルダ（初めて使うときに作り、テストの後で消す）。"""
        if self._tmp is None:
            self._tmp = make_tmp("log-capture-test-")
            self.addCleanup(shutil.rmtree, self._tmp, True)
        return self._tmp

    def start_device(self, plugged=True, device=True, mode="tcp", **kw):
        """機器のまねを立て、スクリプトの port に書く値を返す。
        mode="tcp" なら socket://127.0.0.1:<番号>、mode="pty"（Linux だけ）なら /dev/pts/N。"""
        self.relay = Relay(mode).start()
        self.addCleanup(self.relay.stop)
        if not plugged:
            self.relay.unplug()
        if device:
            self.device = FakeDevice(self.relay, **kw).start()
            self.addCleanup(self.device.stop)
        return self.relay.port

    @property
    def config_path(self):
        return os.path.join(self.tmp, DATA_DIR_NAME, "config.yaml")

    def write_config(self, **values):
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        for key, value in values.items():
            line = "%s: %s" % (key, json.dumps(value, ensure_ascii=False))
            text, n = re.subn(r"(?m)^%s:.*$" % re.escape(key), lambda m: line, text)
            self.assertEqual(n, 1, key)
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(text)
        return self.config_path

    def capture(self, *args, env=None, timeout=90, workdir=True):
        e = dict(os.environ)
        e.update(env or {})
        argv = [sys.executable, SCRIPT] + list(args)
        if workdir:
            argv += ["--workdir", self.tmp]
        started = time.monotonic()
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=e, timeout=timeout, cwd=self.tmp)
        self.elapsed = time.monotonic() - started
        # スクリプトは、Python の既定の文字コードで書く（Windows では cp1252 や cp932 など）
        out = p.stdout.decode(child_encoding())
        self.stderr = unescape(p.stderr.decode(child_encoding(), "replace"))
        try:
            data = json.loads(out)
        except ValueError:
            self.fail("標準出力が JSON ではありません：%r\n標準エラー：%s" % (out, self.stderr))
        self.assertIsInstance(data, dict)
        self.assertNotIn("Traceback", out)
        return p.returncode, data

    def read_log(self, data):
        self.assertTrue(os.path.isabs(data["log"]))
        self.assertTrue(data["log"].startswith(os.path.join(self.tmp, DATA_DIR_NAME, "logs") + os.sep))
        self.assertRegex(os.path.basename(data["log"]), r"^%s-\d{8}-\d{6}(-\d+)?\.log$" % re.escape(SKILL_NAME))
        with open(data["log"], encoding="utf-8") as f:
            return f.read()

    def transcript(self, data):
        """ログの、機器から届いた文字の部分（\r を除き、行に分けたもの）。"""
        with open(data["log"], encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertNotIn(MARKER_TAG, text)
        body = text.split("そのまま ----\n", 1)[1].split("# ---- ここまで ----", 1)[0]
        return body.replace("\r", "").split("\n")

    def assert_log_has_no_marker(self, data):
        """終了コードを調べるコマンドの印は、ログのファイルのどこにもない。"""
        with open(data["log"], "rb") as f:
            raw = f.read()
        self.assertNotIn(MARKER_TAG.encode("ascii"), raw)
        return raw.decode("utf-8")


# ---------------------------------------------------------------------------
# run：ログイン、コマンド、出力
# ---------------------------------------------------------------------------

class RunLoginAndCommands(CaptureTestCase):

    def test_login_then_command_then_logout(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["start-app /opt/x"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["skill"], SKILL_NAME)
        # 経過の表示（標準エラー）の頭は、このスキルのフォルダの名前
        self.assertIn("[%s] ログ：" % SKILL_NAME, self.stderr)
        for line in self.stderr.splitlines():
            self.assertTrue(line.startswith("[%s] " % SKILL_NAME), line)
        self.assertTrue(data["logged_in"])
        self.assertTrue(data["logged_out"])
        self.assertEqual(len(data["commands"]), 1)
        c = data["commands"][0]
        self.assertEqual(c["command"], "start-app /opt/x")
        self.assertEqual(c["output"], "[DBG] VALUE=3")
        self.assertFalse(c["timed_out"])
        self.assertEqual(c["exit_code"], 0)
        self.assertIn("1 個のコマンドを実行し", data["message"])
        self.assertNotIn("終了コード", data["message"])
        self.assertEqual(self.device.login_attempts, [("root", True)])
        self.assertEqual(self.device.executed, ["start-app /opt/x", MARKER, "exit"])
        self.assertEqual(self.device.state, "login")  # ログアウトして、ログインの求めに戻った
        log = self.read_log(data)
        self.assertIn("myboard login:", log)
        self.assertIn("[DBG] VALUE=3", log)
        self.assertEqual(data["config"], self.config_path)

    def test_default_is_run_when_no_subcommand(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["echo hi"])
        code, data = self.capture()
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "hi")

    def test_console_already_at_shell_prompt(self):
        port = self.start_device(login=False)
        self.write_config(port=port, commands=["start-app /opt/x"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertFalse(data["logged_in"])
        self.assertNotIn("logged_out", data)
        self.assertEqual(data["commands"][0]["output"], "[DBG] VALUE=3")
        self.assertEqual(self.device.exits, 0)  # 自分でログインしていないので exit は送らない
        self.assertEqual(self.device.login_attempts, [])

    def test_console_left_at_password_prompt(self):
        port = self.start_device(login=True, left_at_password=True)
        self.write_config(port=port, login_password="root", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.device.login_attempts, [("admin", False), ("root", True)])
        self.assertEqual(data["commands"][0]["output"], "hi")

    def test_logout_false_keeps_session(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["echo hi"], logout=False)
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.device.exits, 0)
        self.assertEqual(self.device.state, "shell")

    def test_output_without_echoed_command(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=[
            "start-app /opt/myapp/my_app", "cat /var/log/messages", "true", "nosuchcmd -x"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        outs = [c["output"] for c in data["commands"]]
        self.assertEqual(outs[0], "[DBG] start\n[DBG] VALUE=3\nmy_app: done")
        self.assertEqual(outs[1], "line one\nline two\nline three")
        self.assertEqual(outs[2], "")
        self.assertEqual(outs[3], "-sh: nosuchcmd: not found")
        for c in data["commands"]:
            self.assertNotIn(c["command"], c["output"].split("\n")[0] if c["output"] else "")
            self.assertNotIn("root@myboard", c["output"])

    def test_long_command_echo_wrapped_by_shell(self):
        port = self.start_device(login=False, wrap=12)
        self.write_config(port=port, commands=["start-app /opt/myapp/my_app"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "[DBG] start\n[DBG] VALUE=3\nmy_app: done")

    def test_long_command_echo_with_repeated_char_at_wrap(self):
        # 本物の bash は、折り返した行の頭の1文字を「a\ra」と2回出すことがある
        command = "echo " + "a" * 70 + " end"
        port = self.start_device(login=False, wrap=20, wrap_repeat=True,
                                 responses={command: "a" * 70 + " end"})
        self.write_config(port=port, commands=[command, "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "a" * 70 + " end")
        self.assertEqual(data["commands"][1]["output"], "hi")

    def test_pause_in_output_does_not_cut_it(self):
        port = self.start_device(login=False)
        self.write_config(port=port, commands=["slow", "tricky"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "first\nsecond")
        self.assertEqual(data["commands"][1]["output"], "value # done")

    def test_console_message_right_after_prompt(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["noisy", "echo hi", "noisy"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        outs = [c["output"] for c in data["commands"]]
        self.assertEqual(outs, ["[DBG] VALUE=3", "hi", "[DBG] VALUE=3"])
        self.assertFalse(any(c["timed_out"] for c in data["commands"]))
        self.assertEqual([c["exit_code"] for c in data["commands"]], [0, 0, 0])
        self.assertTrue(data["logged_out"])
        self.assertLess(self.elapsed, 15)

    def test_console_with_frequent_messages(self):
        port = self.start_device(login=True, ticker=0.3)
        self.write_config(port=port, login_password="root",
                          commands=["start-app /opt/x", "echo hi", "cat /var/log/messages"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.device.login_attempts, [("root", True)])
        expected = [["[DBG] VALUE=3"], ["hi"], ["line one", "line two", "line three"]]
        for c, want in zip(data["commands"], expected):
            self.assertFalse(c["timed_out"], c)
            got = [line for line in c["output"].split("\n") if line != KERNEL_LINE]
            self.assertEqual(got, want, c)
            self.assertEqual(c["exit_code"], 0, c)
        self.assertEqual(self.device.state, "login")

    def test_long_output_keeps_last_60_lines(self):
        port = self.start_device(login=False)
        self.write_config(port=port, commands=["seq 100"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        c = data["commands"][0]
        self.assertTrue(c["truncated"])
        self.assertEqual(c["output_lines"], 100)
        lines = c["output"].split("\n")
        self.assertEqual(len(lines), 60)
        self.assertEqual(lines[0], "41")
        self.assertEqual(lines[-1], "100")
        self.assertIn("\n1\n", self.read_log(data).replace("\r", ""))  # 全部はログにある

    def test_command_option_overrides_config(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["start-app /opt/x"])
        code, data = self.capture("run", "--command", "echo hi", "--command", "uname -a")
        self.assertEqual(code, 0, data)
        self.assertEqual([c["command"] for c in data["commands"]], ["echo hi", "uname -a"])
        self.assertEqual(data["commands"][0]["output"], "hi")
        self.assertIn("armv7l", data["commands"][1]["output"])
        self.assertEqual(self.device.executed, ["echo hi", MARKER, "uname -a", MARKER, "exit"])
        # 設定ファイルは書き換えない
        with open(self.config_path, encoding="utf-8") as f:
            self.assertIn('commands: ["start-app /opt/x"]', f.read())

    def test_command_option_works_with_empty_commands_in_config(self):
        port = self.start_device(login=False)
        self.write_config(port=port)
        code, data = self.capture("run", "--command", "echo hi")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "hi")

    def test_ansi_escapes_are_removed_from_output(self):
        port = self.start_device(login=True, ansi=True)
        self.write_config(port=port, login_password="root", commands=["color", "start-app /opt/x"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "[DBG] VALUE=3\nOK")
        self.assertEqual(data["commands"][1]["output"], "[DBG] VALUE=3")
        for c in data["commands"]:
            self.assertNotIn("\x1b", c["output"])
            self.assertNotIn("\r", c["output"])
        log = self.read_log(data)
        self.assertIn("\x1b[1;31m", log)  # ログには届いたままが残る
        self.assertIn("\x1b[?2004h", log)

    def test_custom_prompt_regex(self):
        port = self.start_device(login=False)
        self.write_config(port=port, prompt="myboard:~# $", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "hi")

    def test_password_from_environment_variable_is_never_shown(self):
        secret = "s3cret-PW-7781"
        port = self.start_device(login=True, password=secret)
        self.write_config(port=port, login_password="", login_password_env="LOG_CAPTURE_TEST_PW",
                          commands=["echo hi"])
        code, data = self.capture("run", env={"LOG_CAPTURE_TEST_PW": secret})
        self.assertEqual(code, 0, data)
        self.assertEqual(self.device.login_attempts, [("root", True)])
        self.assertNotIn(secret, json.dumps(data, ensure_ascii=False))
        self.assertNotIn(secret, self.stderr)
        self.assertNotIn(secret, self.read_log(data))

    def test_environment_variable_wins_over_password(self):
        port = self.start_device(login=True, password="from-env")
        self.write_config(port=port, login_password="from-file", login_password_env="LOG_CAPTURE_TEST_PW",
                          commands=["echo hi"])
        code, data = self.capture("run", env={"LOG_CAPTURE_TEST_PW": "from-env"})
        self.assertEqual(code, 0, data)

    def test_wrong_password(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="wrong", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        self.assertIn("ログインできませんでした", data["message"])
        self.assertIn("login_password", data["message"])
        self.assertEqual(self.device.login_attempts, [("root", False)])
        self.assertEqual(self.device.executed, [])

    def test_empty_password_says_where_to_write_it(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="", login_password_env="", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("ログインできませんでした", data["message"])
        self.assertIn("login_password も login_password_env も空", data["message"])
        self.assertIn(self.config_path, data["message"])
        self.assertEqual(self.device.executed, [])

    def test_empty_password_from_environment_variable(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="", login_password_env="LOG_CAPTURE_TEST_PW",
                          commands=["echo hi"])
        code, data = self.capture("run", env={"LOG_CAPTURE_TEST_PW": ""})
        self.assertEqual(code, 1, data)
        self.assertIn("環境変数 LOG_CAPTURE_TEST_PW の中身が空", data["message"])


# ---------------------------------------------------------------------------
# run：機器のコマンドの終了コード
# ---------------------------------------------------------------------------

class RunExitCodes(CaptureTestCase):

    def assert_no_marker(self, data):
        for c in data["commands"]:
            self.assertNotIn(MARKER_TAG, c["output"], c)
        self.assertNotIn(MARKER_TAG, data["message"])
        log = self.assert_log_has_no_marker(data)
        # 各コマンドの出力の後に、終了コードの行が1つずつある
        want = ["（終了コード：%s）" % ("読めませんでした" if c["exit_code"] is None else c["exit_code"])
                for c in data["commands"]]
        got = re.findall(r"（終了コード：[^）]*）", log.split("そのまま ----", 1)[1])
        self.assertEqual(got, want)

    def test_success_is_zero(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root",
                          commands=["start-app /opt/myapp/my_app", "true"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual([c["exit_code"] for c in data["commands"]], [0, 0])
        self.assertEqual(data["commands"][0]["output"], "[DBG] start\n[DBG] VALUE=3\nmy_app: done")
        self.assertEqual(data["commands"][1]["output"], "")
        self.assertNotIn("終了コード", data["message"])
        self.assert_no_marker(data)
        # 各コマンドの後に、終了コードを調べるコマンドを1つずつ送った
        self.assertEqual(self.device.executed,
                         ["start-app /opt/myapp/my_app", MARKER, "true", MARKER, "exit"])

    def test_failure_is_reported_in_message(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["start-app /opt/noconf/my_app"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)  # 取ること自体はできた
        self.assertTrue(data["ok"])
        c = data["commands"][0]
        self.assertEqual(c["exit_code"], 1)
        self.assertFalse(c["timed_out"])
        self.assertEqual(c["output"], "start-app: /opt/noconf/my_app: config file not found")
        self.assertIn(
            "コマンド「start-app /opt/noconf/my_app」は終了コード 1 で終わりました"
            "（最後の行：start-app: /opt/noconf/my_app: config file not found）。",
            data["message"])
        self.assertTrue(data["message"].startswith("シリアルポート %s で 1 個のコマンドを実行し" % port))
        self.assert_no_marker(data)

    def test_each_failed_command_is_named(self):
        port = self.start_device(login=False)
        self.write_config(port=port, commands=[
            "false", "echo hi", "nosuchcmd -x", "start-app /opt/denied/my_app"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual([c["exit_code"] for c in data["commands"]], [1, 0, 127, 126])
        msg = data["message"]
        self.assertIn("コマンド「false」は終了コード 1 で終わりました（出力はありませんでした）。", msg)
        self.assertIn("コマンド「nosuchcmd -x」は終了コード 127 で終わりました（最後の行：-sh: nosuchcmd: not found）。", msg)
        self.assertIn("コマンド「start-app /opt/denied/my_app」は終了コード 126 で終わりました"
                      "（最後の行：start-app: /opt/denied/my_app: Permission denied）。", msg)
        self.assertNotIn("「echo hi」", msg)
        self.assertEqual(data["commands"][3]["output"],
                         "[DBG] start\nstart-app: /opt/denied/my_app: Permission denied")
        self.assert_no_marker(data)

    def test_timeout_gives_null(self):
        port = self.start_device(login=False)
        self.write_config(port=port, command_timeout_seconds=1, commands=["start-app /opt/hang", "false"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        first, second = data["commands"]
        self.assertTrue(first["timed_out"])
        self.assertIsNone(first["exit_code"])
        self.assertEqual(second["exit_code"], 1)  # Ctrl-C の後のコマンドは、ふつうに読む
        self.assertIn("Ctrl-C で止めました", data["message"])
        self.assertIn("コマンド「false」は終了コード 1 で終わりました", data["message"])
        self.assertNotIn("「start-app /opt/hang」は終了コード", data["message"])
        # 止めたコマンドの後には、終了コードを調べるコマンドを送らない
        self.assertEqual(self.device.executed, ["start-app /opt/hang", "false", MARKER])
        self.assert_no_marker(data)

    def test_marker_lines_are_removed_from_output(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=[
            "true", "echo hi", "cat /var/log/messages", "false", "seq 100"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        outs = [c["output"] for c in data["commands"]]
        self.assertEqual(outs[:4], ["", "hi", "line one\nline two\nline three", ""])
        self.assertEqual(data["commands"][4]["output_lines"], 100)
        self.assertEqual(data["commands"][4]["output"].split("\n")[-1], "100")
        self.assert_no_marker(data)
        self.assertNotIn(MARKER_TAG, json.dumps(data["commands"], ensure_ascii=False))
        # ログには、印の行の代わりに、終了コードの行がある。ほかの文字は残る
        log = self.read_log(data)
        self.assertNotIn(MARKER_TAG, log)
        self.assertIn("line three\n（終了コード：0）\n", log.replace("\r", ""))
        self.assertIn("# false\n（終了コード：1）\n", log.replace("\r", ""))
        self.assertIn("\n1\n", log.replace("\r", ""))
        self.assertIn("\n100\n（終了コード：0）\n", log.replace("\r", ""))

    def test_log_is_the_transcript_without_marker(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root",
                          commands=["start-app /opt/myapp/my_app", "false", "nosuchcmd"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assert_no_marker(data)
        lines = self.transcript(data)
        self.assertIn("myboard login: root", lines)
        self.assertIn("Last login: Tue Nov  4 12:00:00 on ttyS0", lines)
        first = lines.index("root@myboard:~# start-app /opt/myapp/my_app")
        self.assertEqual(lines[first:], [
            "root@myboard:~# start-app /opt/myapp/my_app",
            "[DBG] start", "[DBG] VALUE=3", "my_app: done",
            "（終了コード：0）",
            "root@myboard:~# false",
            "（終了コード：1）",
            "root@myboard:~# nosuchcmd",
            "-sh: nosuchcmd: not found",
            "（終了コード：127）",
            "root@myboard:~# exit", "logout", "", "myboard login: ",
            "",
        ])
        # 印のコマンドは、機器には送っている（終了コードを読むため）
        self.assertEqual(self.device.executed.count(MARKER), 3)

    def test_log_keeps_escapes_but_not_marker(self):
        port = self.start_device(login=False, ansi=True, status_prompt=True)
        self.write_config(port=port, commands=["color", "false"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assert_no_marker(data)
        with open(data["log"], encoding="utf-8", newline="") as f:
            log = f.read()
        self.assertIn("\x1b[1;31m[DBG]\x1b[0m VALUE=\x1b[33m3\x1b[0m\r\n\x1b[32mOK\x1b[0m\r\n（終了コード：0）\n", log)
        # 印を送った行（その前のプロンプトを含む）は、行ごと除く
        self.assertIn("# false\r\n（終了コード：1）\n", log)
        # ほかのプロンプト（color の前、false の前、最後）は残る
        self.assertEqual(log.count("\x1b[?2004h"), 3)
        self.assertEqual(log.count("\x1b[01;32mroot@myboard\x1b[00m:\x1b[01;34m~\x1b[00m# false\r\n"), 1)

    def test_ansi_prompt_that_shows_the_status(self):
        # 直前の終了コードをプロンプトに出すシェル（色つき）。コマンドが失敗すると、プロンプトの形が変わる
        port = self.start_device(login=True, ansi=True, status_prompt=True)
        self.write_config(port=port, login_password="root",
                          commands=["start-app /opt/noconf/my_app", "color", "nosuchcmd", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual([c["exit_code"] for c in data["commands"]], [1, 0, 127, 0])
        self.assertEqual([c["output"] for c in data["commands"]], [
            "start-app: /opt/noconf/my_app: config file not found",
            "[DBG] VALUE=3\nOK",
            "-sh: nosuchcmd: not found",
            "hi",
        ])
        self.assertTrue(data["logged_out"])
        self.assert_no_marker(data)

    def test_prompt_on_two_lines(self):
        # 2行のプロンプト。上の行が出力に混ざらず、「最後の行」も正しく引く
        port = self.start_device(login=True, status_prompt=True,
                                 prompt_text="\n[root@myboard ~]\n# ")
        self.write_config(port=port, login_password="root",
                          commands=["start-app /opt/noconf/my_app", "echo hi", "false", "true"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual([c["exit_code"] for c in data["commands"]], [1, 0, 1, 0])
        self.assertEqual([c["output"] for c in data["commands"]], [
            "start-app: /opt/noconf/my_app: config file not found", "hi", "", ""])
        self.assertIn("（最後の行：start-app: /opt/noconf/my_app: config file not found）", data["message"])
        self.assertIn("コマンド「false」は終了コード 1 で終わりました（出力はありませんでした）。", data["message"])
        self.assert_no_marker(data)

    def test_shell_without_exit_status(self):
        # $? が使えないシェル：終了コードは null。待ちすぎず、印の行も出力に混ざらない
        port = self.start_device(login=False, dollar_q=False)
        self.write_config(port=port, commands=["start-app /opt/noconf/my_app", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual([c["exit_code"] for c in data["commands"]], [None, None])
        self.assertEqual([c["output"] for c in data["commands"]],
                         ["start-app: /opt/noconf/my_app: config file not found", "hi"])
        self.assertNotIn("終了コード", data["message"])
        for c in data["commands"]:
            self.assertNotIn("fish", c["output"])
        self.assertLess(self.elapsed, 8)
        self.assert_no_marker(data)
        lines = self.transcript(data)
        self.assertEqual(lines.count("（終了コード：読めませんでした）"), 2)
        # 機器の返事（印を含まない行）は、ログに残る
        self.assertEqual(lines[lines.index("hi") + 1:lines.index("hi") + 3], [
            "fish: $? is not the exit status. In fish, please use $status.",
            "（終了コード：読めませんでした）"])

    def test_listen_only_sends_nothing(self):
        port = self.start_device(login=False, ticker=0.2)
        self.write_config(port=port, commands=["false"])
        code, data = self.capture("run", "--listen-only", "1")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"], [])
        self.assertEqual(bytes(self.relay.from_pc), b"")
        self.assertNotIn(MARKER_TAG, self.read_log(data))


# ---------------------------------------------------------------------------
# run：時間切れ、応答なし、ポートの問題
# ---------------------------------------------------------------------------

class RunFailures(CaptureTestCase):

    def test_command_timeout_then_ctrl_c(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", command_timeout_seconds=1,
                          commands=["start-app /opt/hang", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        first, second = data["commands"]
        self.assertTrue(first["timed_out"])
        self.assertIsNone(first["exit_code"])  # Ctrl-C で止めたときは読まない
        self.assertIn("[DBG] start", first["output"])
        self.assertNotIn("start-app /opt/hang", first["output"])
        self.assertFalse(second["timed_out"])
        self.assertEqual(second["output"], "hi")  # Ctrl-C の後も続けられた
        self.assertEqual(second["exit_code"], 0)
        self.assertEqual(self.device.executed, ["start-app /opt/hang", "echo hi", MARKER, "exit"])
        self.assertEqual(self.device.interrupts, 1)
        self.assertIn("start-app /opt/hang", data["message"])
        self.assertIn("Ctrl-C", data["message"])
        self.assertEqual(self.device.exits, 1)  # ログアウトもした
        # ログ：止めたコマンドの後は「読めませんでした」。印は残らない
        lines = self.transcript(data)
        first = lines.index("root@myboard:~# start-app /opt/hang")
        self.assertEqual(lines[first:first + 7], [
            "root@myboard:~# start-app /opt/hang", "[DBG] start", "^C",
            "（終了コード：読めませんでした）",
            "root@myboard:~# echo hi", "hi", "（終了コード：0）"])

    def test_prompt_never_comes_back(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", command_timeout_seconds=1,
                          commands=["start-app /opt/stuck", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(len(data["commands"]), 1)
        self.assertTrue(data["commands"][0]["timed_out"])
        self.assertIsNone(data["commands"][0]["exit_code"])
        self.assertEqual(data["not_run"], ["echo hi"])
        self.assertIn("プロンプトが戻りませんでした", data["message"])
        self.assertNotIn("echo hi", self.device.executed)
        self.assertEqual(self.device.exits, 0)
        lines = self.transcript(data)
        self.assertEqual(lines[-4:], ["[DBG] start", "^C", "（終了コード：読めませんでした）", ""])

    def test_no_response_at_all(self):
        port = self.start_device(plugged=False)  # ケーブルが抜けている
        self.write_config(port=port, login_password="root", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        self.assertEqual(
            data["message"],
            "シリアルポート %s から応答がありません。ケーブルと機器の電源を確かめてください。" % port)
        self.assertEqual(bytes(self.relay.sent_while_unplugged), b"\r\r")  # 2回呼びかけた
        self.assertGreaterEqual(self.elapsed, 9.5)
        self.assertLess(self.elapsed, 25)
        self.assertTrue(os.path.isfile(data["log"]))

    def test_text_arrives_but_no_prompt(self):
        port = self.start_device(login=True, garbage=True)  # 速さが合っていないときのまね
        self.write_config(port=port, login_password="root", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("文字は届きますが", data["message"])
        self.assertIn("baud", data["message"])

    def test_missing_port(self):
        self.write_config(port=MISSING_PORT, commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        self.assertIn(MISSING_PORT, data["message"])
        self.assertIn("見つかりません", data["message"])
        self.assertIn("USB ケーブル", data["message"])
        self.assertIn("available_ports", data)
        self.assertLess(self.elapsed, 5)

    def test_nothing_listens_on_the_socket(self):
        # socket:// の先に、何も待っていない
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        free = holder.getsockname()[1]
        holder.close()
        self.write_config(port="socket://127.0.0.1:%d" % free, commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("を開けません", data["message"])
        self.assertTrue(os.path.isfile(data["log"]))

    def test_usb_pulled_during_command(self):
        port = self.start_device(login=False)
        self.write_config(port=port, commands=["pull-usb", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("途中で切れました", data["message"])
        self.assertEqual(len(data["commands"]), 1)
        self.assertTrue(data["commands"][0]["interrupted"])
        self.assertIsNone(data["commands"][0]["exit_code"])
        self.assertIn("bye", data["commands"][0]["output"])
        self.assertEqual(data["not_run"], ["echo hi"])
        self.assertIn("残りの 1 個", data["message"])
        lines = self.transcript(data)
        self.assertEqual(lines[-3:], ["bye", "（終了コード：読めませんでした）", ""])


# ---------------------------------------------------------------------------
# run：記録するだけ（listen）
# ---------------------------------------------------------------------------

class RunListen(CaptureTestCase):

    def test_listen_only_option(self):
        port = self.start_device(login=True, ticker=0.2)
        self.write_config(port=port, login_password="root", commands=["start-app /opt/x"])
        code, data = self.capture("run", "--listen-only", "1.2")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["commands"], [])
        self.assertFalse(data["logged_in"])
        self.assertIn(KERNEL_LINE, data["listen"]["output"])
        self.assertGreaterEqual(data["listen"]["output_lines"], 3)
        self.assertEqual(bytes(self.relay.from_pc), b"")  # 何も送っていない
        self.assertEqual(self.device.executed, [])
        self.assertIn("記録しました", data["message"])

    def test_listen_only_from_config(self):
        port = self.start_device(login=True, ticker=0.2)
        self.write_config(port=port, commands=[], listen_seconds=1)
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertIn(KERNEL_LINE, data["listen"]["output"])
        self.assertEqual(bytes(self.relay.from_pc), b"")

    def test_listen_after_commands(self):
        port = self.start_device(login=True, ticker=0.3)
        self.write_config(port=port, login_password="root", commands=["echo hi"], listen_seconds=1)
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "hi")
        self.assertIn(KERNEL_LINE, data["listen"]["output"])
        self.assertIn("そのあと 1 秒", data["message"])
        self.assertEqual(self.device.exits, 1)

    def test_listen_with_no_output(self):
        port = self.start_device(login=False)
        self.write_config(port=port)
        code, data = self.capture("run", "--listen-only", "0.5")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["listen"]["output"], "")
        self.assertIn("何も出力されませんでした", data["message"])


# ---------------------------------------------------------------------------
# 本物のシリアルポートの道（疑似端末。Linux だけ）
# ---------------------------------------------------------------------------

@unittest.skipUnless(IS_LINUX, "疑似端末（pty）のテストは、Linux のときだけ動かす")
class PtyPort(CaptureTestCase):
    """port に /dev/pts/N を書いて、pyserial のシリアルポートの道（serialposix）を通す。"""

    def test_login_command_and_logout(self):
        port = self.start_device(mode="pty", login=True)
        self.assertTrue(port.startswith("/dev/"), port)
        self.write_config(port=port, login_password="root",
                          commands=["start-app /opt/myapp/my_app", "false"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual([c["output"] for c in data["commands"]],
                         ["[DBG] start\n[DBG] VALUE=3\nmy_app: done", ""])
        self.assertEqual([c["exit_code"] for c in data["commands"]], [0, 1])
        self.assertTrue(data["logged_out"])
        self.assertEqual(self.device.executed,
                         ["start-app /opt/myapp/my_app", MARKER, "false", MARKER, "exit"])

    def test_timeout_then_ctrl_c(self):
        port = self.start_device(mode="pty", login=False)
        self.write_config(port=port, command_timeout_seconds=1, commands=["start-app /opt/hang", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertTrue(data["commands"][0]["timed_out"])
        self.assertEqual(data["commands"][1]["output"], "hi")
        self.assertEqual(self.device.interrupts, 1)

    def test_check_and_listen_only(self):
        port = self.start_device(mode="pty", login=True, ticker=0.2)
        self.write_config(port=port, login_password="root")
        code, data = self.capture("check")
        self.assertEqual(code, 0, data)
        self.assertIn("ログインできました", data["message"])
        code, data = self.capture("run", "--listen-only", "1")
        self.assertEqual(code, 0, data)
        self.assertIn(KERNEL_LINE, data["listen"]["output"])

    def test_no_response_at_all(self):
        port = self.start_device(mode="pty", plugged=False)
        self.write_config(port=port, commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("応答がありません", data["message"])
        self.assertEqual(bytes(self.relay.sent_while_unplugged), b"\r\r")

    def test_usb_pulled_during_command(self):
        port = self.start_device(mode="pty", login=False)
        self.write_config(port=port, commands=["pull-usb", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("途中で切れました", data["message"])
        self.assertEqual(data["not_run"], ["echo hi"])
        self.assertIn("bye", data["commands"][0]["output"])

    def test_port_used_by_another_program(self):
        import fcntl
        port = self.start_device(mode="pty", login=False)
        self.write_config(port=port, commands=["echo hi"])
        holder = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("ほかのプログラム", data["message"])
        self.assertIn("minicom", data["message"])
        self.assertEqual(self.device.executed, [])


@unittest.skipIf(IS_WINDOWS, "Linux などのデバイスのファイルのテスト")
class PosixPortPaths(CaptureTestCase):

    def test_port_written_without_dev(self):
        self.write_config(port=os.path.basename(MISSING_PORT), commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("/dev から書いてください", data["message"])

    def test_port_is_not_a_serial_port(self):
        plain = os.path.join(self.tmp, "not-a-port")
        with open(plain, "w") as f:
            f.write("x")
        self.write_config(port=plain, commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("シリアルポートではありません", data["message"])

    def test_port_is_a_folder(self):
        self.write_config(port=self.tmp, commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("フォルダで、シリアルポートではありません", data["message"])


# ---------------------------------------------------------------------------
# 設定の問題と config-path
# ---------------------------------------------------------------------------

class Settings(CaptureTestCase):

    def test_config_created_from_template_on_first_run(self):
        code, data = self.capture("run")
        self.assertEqual(code, 2, data)
        self.assertFalse(data["ok"])
        self.assertTrue(data["created"])
        self.assertEqual(data["config"], self.config_path)
        self.assertTrue(any("port" in p for p in data["problems"]))
        # 問題の文と「設定ファイルは…」の文のあいだに「。」がある
        self.assertEqual(
            data["message"],
            "設定ファイルがなかったので、ひな形から作りました。"
            "port（機器につながるシリアルポート）が空です。"
            "COM3（Windows）や /dev/ttyUSB0（Linux）のように書いてください。"
            "設定ファイルは %s です。" % self.config_path)
        with open(TEMPLATE, encoding="utf-8") as a, open(self.config_path, encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read())

    def test_config_path_subcommand(self):
        code, data = self.capture("config-path")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["config"], self.config_path)
        self.assertTrue(data["created"])
        self.assertEqual(data["missing"], ["port"])
        code, data = self.capture("config-path")
        self.assertFalse(data["created"])
        self.write_config(port="/dev/ttyUSB0")
        code, data = self.capture("config-path")
        self.assertEqual(data["missing"], [])

    def test_new_folder_gets_gitignore(self):
        self.capture("config-path")
        gi = os.path.join(self.tmp, DATA_DIR_NAME, ".gitignore")
        with open(gi, "rb") as f:
            self.assertEqual(f.read(), b"*\n")  # Windows でも \r\n にしない
        with open(gi, "w", encoding="utf-8") as f:
            f.write("logs/\n")
        self.capture("config-path")
        with open(gi, encoding="utf-8") as f:
            self.assertEqual(f.read(), "logs/\n")  # あるときは、そのまま

    def test_existing_folder_without_gitignore_gets_one(self):
        """ほかのものが先にフォルダを作っても、.gitignore がなければ作る。"""
        os.makedirs(os.path.join(self.tmp, DATA_DIR_NAME))
        self.capture("config-path")
        self.assertTrue(os.path.isfile(self.config_path))
        with open(os.path.join(self.tmp, DATA_DIR_NAME, ".gitignore"), "rb") as f:
            self.assertEqual(f.read(), b"*\n")

    def test_gitignore_is_made_again_when_removed(self):
        self.capture("config-path")
        gi = os.path.join(self.tmp, DATA_DIR_NAME, ".gitignore")
        for command in ("config-path", "check", "run"):
            os.unlink(gi)
            self.capture(command)
            with open(gi, "rb") as f:
                self.assertEqual(f.read(), b"*\n", command)

    def test_gitignore_in_work_folder_with_config_elsewhere(self):
        os.makedirs(os.path.join(self.tmp, DATA_DIR_NAME))
        self.capture("config-path", "--config", os.path.join(self.tmp, "sub", "my.yaml"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, DATA_DIR_NAME, ".gitignore")))

    # --- ひな形に増えた項目を、古い設定ファイルに足す ---

    def template_blocks(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            return load_module().template_blocks(f.read())

    def old_config(self, *without, **values):
        """ひな形から項目を除いた、古い設定ファイルを置く。中身（バイト）を返す。"""
        self.write_config(**values)
        with open(self.config_path, encoding="utf-8") as f:
            text = f.read()
        for key, lines in self.template_blocks():
            if key in without:
                text = text.replace("\n".join(lines) + "\n", "")
        data = (text.rstrip("\n") + "\n").encode("utf-8")
        with open(self.config_path, "wb") as f:
            f.write(data)
        return data

    def read_config_bytes(self):
        with open(self.config_path, "rb") as f:
            return f.read()

    def test_template_blocks(self):
        blocks = self.template_blocks()
        self.assertEqual([k for k, _ in blocks], TEMPLATE_KEYS)
        for key, lines in blocks:
            self.assertTrue(lines[-1].startswith(key + ":"), key)
            self.assertTrue(all(l.startswith("#") for l in lines[:-1]), key)
        with open(TEMPLATE, encoding="utf-8") as f:
            head = f.readline().rstrip("\n")   # ファイルの頭の説明
        self.assertNotIn(head, blocks[0][1])

    def test_missing_keys_are_appended_with_comments(self):
        before = self.old_config("listen_seconds", "logout", port="/dev/ttyUSB9")
        code, data = self.capture("config-path")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["added"], ["listen_seconds", "logout"])
        self.assertEqual(data["message"],
                         "設定ファイルに、新しい項目 listen_seconds、logout を足しました（今の値はひな形のまま）。")
        after = self.read_config_bytes()
        self.assertEqual(after[:len(before)], before)
        blocks = dict(self.template_blocks())
        expected = "\n" + "\n".join(blocks["listen_seconds"]) + "\n\n" + "\n".join(blocks["logout"]) + "\n"
        self.assertEqual(after[len(before):].decode("utf-8"), expected)
        values = load_module().read_yaml(self.config_path)
        self.assertEqual(values["port"], "/dev/ttyUSB9")
        self.assertEqual(values["listen_seconds"], 0)
        self.assertIs(values["logout"], True)

        code, data = self.capture("config-path")
        self.assertEqual(data["added"], [])
        self.assertNotIn("message", data)
        self.assertEqual(self.read_config_bytes(), after)

    def test_new_config_reports_nothing_added(self):
        code, data = self.capture("config-path")
        self.assertTrue(data["created"])
        self.assertEqual(data["added"], [])

    def test_run_and_check_report_added(self):
        self.old_config("command_timeout_seconds")
        code, data = self.capture("check")
        self.assertEqual(code, 2, data)
        self.assertEqual(data["added"], ["command_timeout_seconds"])
        self.assertTrue(data["message"].endswith(
            "設定ファイルに、新しい項目 command_timeout_seconds を足しました（今の値はひな形のまま）。"),
            data["message"])
        self.old_config("prompt")
        code, data = self.capture("run")
        self.assertEqual(code, 2, data)
        self.assertEqual(data["added"], ["prompt"])
        self.assertIn("新しい項目 prompt を足しました", data["message"])

    def test_broken_file_is_not_touched(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "wb") as f:
            f.write(b'port: "/dev/ttyUSB0\n')
        code, data = self.capture("config-path")
        self.assertEqual(data["added"], [])
        self.assertIn("problems", data)
        self.assertEqual(self.read_config_bytes(), b'port: "/dev/ttyUSB0\n')

    def test_config_option_points_to_another_file(self):
        other = os.path.join(self.tmp, "sub", "my.yaml")
        code, data = self.capture("config-path", "--config", other)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["config"], other)
        self.assertTrue(os.path.isfile(other))

    def test_config_cannot_be_created(self):
        blocker = os.path.join(self.tmp, "afile")
        with open(blocker, "w") as f:
            f.write("x")
        bad = os.path.join(blocker, "config.yaml")
        code, data = self.capture("config-path", "--config", bad)
        self.assertEqual(code, 0, data)
        self.assertFalse(data["created"])
        self.assertIn("作れません", data["problems"][0])
        code, data = self.capture("run", "--config", bad)
        self.assertEqual(code, 2, data)
        self.assertEqual(data["config"], bad)
        self.assertIn("作れません", data["message"])
        code, data = self.capture("check", "--config", bad)
        self.assertEqual(code, 2, data)

    def test_nothing_to_do(self):
        self.write_config(port="/dev/ttyUSB0", commands=[], listen_seconds=0)
        code, data = self.capture("run")
        self.assertEqual(code, 2, data)
        self.assertIn("commands", data["problems"][0])

    def test_bad_values(self):
        self.write_config(port="/dev/ttyUSB0", baud=12345, prompt="[", command_timeout_seconds=0,
                          logout="maybe", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 2, data)
        text = "\n".join(data["problems"])
        self.assertIn("baud", text)
        self.assertIn("prompt", text)
        self.assertIn("command_timeout_seconds", text)
        self.assertIn("logout", text)

    def test_unreadable_yaml_line(self):
        self.write_config(port="/dev/ttyUSB0", commands=["echo hi"])
        with open(self.config_path, "a", encoding="utf-8") as f:
            f.write("prompt2 [#$]\n")
        code, data = self.capture("run")
        self.assertEqual(code, 2, data)
        self.assertRegex(data["problems"][0], r"\d+ 行目")

    def test_password_env_not_set(self):
        self.write_config(port="/dev/ttyUSB0", login_password="", login_password_env="LOG_CAPTURE_TEST_NOT_SET",
                          commands=["echo hi"])
        env = {k: v for k, v in os.environ.items() if k != "LOG_CAPTURE_TEST_NOT_SET"}
        code, data = self.capture("run", env=env)
        self.assertEqual(code, 2, data)
        self.assertIn("LOG_CAPTURE_TEST_NOT_SET", data["problems"][0])

    def test_login_required_but_user_empty(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_user="", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 2, data)
        self.assertIn("login_user", data["message"])
        self.assertEqual(self.device.login_attempts, [])

    def test_bad_arguments_are_sentences(self):
        for args in (["frobnicate"], ["run", "--listen-only", "0"], ["run", "--no-such-option"],
                     ["run", "--workdir"]):
            code, data = self.capture(*args)
            self.assertEqual(code, 2, (args, data))
            self.assertTrue(data["message"].endswith("。"), data["message"])
            self.assertNotIn("。。", data["message"])

    def test_bad_arguments(self):
        for args in (["run", "--listen-only", "abc"],
                     ["run", "--listen-only", "0"],
                     ["run", "--command", "x", "--listen-only", "1"],
                     ["check", "--command", "x"],
                     ["run", "--command", ""],
                     ["frobnicate"],
                     ["run", "--no-such-option"]):
            code, data = self.capture(*args)
            self.assertEqual(code, 2, (args, data))
            self.assertFalse(data["ok"])
            self.assertIn("引数", data["message"])

    def test_help_is_json(self):
        code, data = self.capture("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage", data)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

class Check(CaptureTestCase):

    def test_check_with_login(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["start-app /opt/x"])
        code, data = self.capture("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["problems"], [])
        self.assertEqual(data["summary"]["port"], port)
        self.assertEqual(data["summary"]["login_password"], "＊＊＊")
        self.assertNotIn('"root"', json.dumps(data["summary"]["login_password"]))
        self.assertIn("ログインできました", data["message"])
        self.assertEqual(self.device.login_attempts, [("root", True)])
        self.assertEqual(self.device.executed, ["exit"])  # コマンドは実行しない
        self.assertEqual(self.device.state, "login")
        log = self.assert_log_has_no_marker(data)
        self.assertIn("myboard login: root", log)
        self.assertNotIn("（終了コード", log)

    def test_check_shell_without_login(self):
        port = self.start_device(login=False)
        self.write_config(port=port)
        code, data = self.capture("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertIn("ログインは不要", data["message"])
        self.assertEqual(self.device.executed, [])

    def test_check_fresh_config(self):
        code, data = self.capture("check")
        self.assertEqual(code, 2, data)
        self.assertFalse(data["ok"])
        self.assertTrue(data["created"])
        self.assertTrue(any("port" in p for p in data["problems"]))

    def test_check_missing_port(self):
        self.write_config(port=MISSING_PORT)
        code, data = self.capture("check")
        self.assertEqual(code, 2, data)
        self.assertIn("見つかりません", data["problems"][0])

    def test_check_wrong_password(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="wrong")
        code, data = self.capture("check")
        self.assertEqual(code, 2, data)
        self.assertIn("ログインできませんでした", data["problems"][0])

    def test_check_empty_password(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="")
        code, data = self.capture("check")
        self.assertEqual(code, 2, data)
        self.assertIn("login_password も login_password_env も空", data["problems"][0])
        self.assertEqual(data["summary"]["login_password"], "")


# ---------------------------------------------------------------------------
# 部品ごとの試験（スクリプトを読み込んで呼ぶ）
# ---------------------------------------------------------------------------

class Parts(unittest.TestCase):

    @property
    def m(self):
        return script_module()

    def test_clean_removes_escapes_and_cr(self):
        raw = "\x1b[?2004h\x1b[01;32mroot@b\x1b[00m:~# \x1b]0;title\x07ab\bc\r\nx\x1b(B\x1b=y"
        self.assertEqual(self.m.clean(raw), "root@b:~# ac\nxy")

    def test_remove_echo(self):
        f = self.m.remove_echo
        self.assertEqual(f(["ls -l", "a", "b"], "ls -l"), ["a", "b"])
        self.assertEqual(f(["start-app /opt/my_ ", "app", "out"], "start-app /opt/my_app"), ["out"])
        self.assertEqual(f(["out"], "ls"), ["out"])  # 折り返しがないときは、そのまま
        # 折り返しで1文字が2回出たとき（「…my_a\rapp…」の \r を除いたもの）
        self.assertEqual(f(["start-app /opt/my_aapp", "out"], "start-app /opt/my_app"), ["out"])
        # コマンドと違う行は、取り除かない
        error = ["start-app: /opt/myapp/my_app: Permission denied"]
        self.assertEqual(f(error, "start-app /opt/myapp/my_app"), error)

    def test_find_exit_code(self):
        f = self.m.find_exit_code
        self.assertEqual(f(" echo __LOGCAP_RC=$?\n__LOGCAP_RC=0\nroot@b:~# "), 0)
        self.assertEqual(f("echo __LOGCAP_RC=$?\n__LOGCAP_RC=127\n[127] root@b:~# "), 127)
        # 返事の行の前後に、プロンプトや機器のメッセージが付いても読む
        self.assertEqual(f("root@b:~# __LOGCAP_RC=2\n"), 2)
        self.assertEqual(f("[  1.0] usb 1-1: x__LOGCAP_RC=1\n# "), 1)
        # エコーだけ（数がない）なら読めない
        self.assertIsNone(f("root@b:~# echo __LOGCAP_RC=$?\n"))
        self.assertIsNone(f("fish: $? is not the exit status.\necho __LOGCAP_RC=$?\n> "))
        self.assertIsNone(f(""))

    def test_drop_marker_lines(self):
        lines = ["__LOGCAP_RC=0", "root@b:~#  echo __LOGCAP_RC=$?", "[DBG] VALUE=3", "done"]
        self.assertEqual(self.m.drop_marker_lines(lines), ["[DBG] VALUE=3", "done"])

    def test_strip_marker(self):
        f = self.m.strip_marker
        self.assertEqual(f("root@b:~#  echo __LOGCAP_RC=$?\r\n"), ("", False))  # 送った行は行ごと
        self.assertEqual(f("__LOGCAP_RC=0\r\n"), ("", True))
        self.assertEqual(f("\x1b[?2004l\r__LOGCAP_RC=127\r\n"), ("", True))
        # 機器のメッセージと同じ行に来たら、メッセージは残す
        self.assertEqual(f("[  1.0] usb 1-1: x__LOGCAP_RC=1\r\n"), ("[  1.0] usb 1-1: x\r\n", True))
        self.assertEqual(f("echo __LOGCAP_RC=$?\n"), ("", False))
        self.assertEqual(f("[DBG] VALUE=3\r\n"), ("[DBG] VALUE=3\r\n", False))
        self.assertEqual(f("fish: $? is not the exit status.\n"), ("fish: $? is not the exit status.\n", False))

    def log_file(self):
        import io
        buf = io.StringIO()
        return self.m.LogFile(buf), buf

    def test_log_file_replaces_marker_with_exit_line(self):
        log, buf = self.log_file()
        # 文字は、行の途中で切れて届く
        for chunk in ["root@b:~# true\r", "\n", "root@b:~# ", " echo __LOG", "CAP_RC=$?\r\n__LOGCAP", "_RC=0\r\nroot@b:~# "]:
            if chunk == " echo __LOG":
                log.hold()
            log.device(chunk)
        log.write_exit_line(0)
        log.device("exit\r\nlogout\r\n")
        log.flush()
        self.assertEqual(buf.getvalue(), "root@b:~# true\r\n（終了コード：0）\nroot@b:~# exit\r\nlogout\r\n")

    def test_log_file_exit_line_goes_where_the_answer_was(self):
        log, buf = self.log_file()
        log.device("hi\r\n[1] \r\n# ")
        log.hold()
        log.device(" echo __LOGCAP_RC=$?\r\n__LOGCAP_RC=1\r\n[0] \r\n# ")
        log.write_exit_line(1)
        log.note("\n# end\n")
        self.assertEqual(buf.getvalue(), "hi\r\n[1] \r\n（終了コード：1）\n[0] \r\n# \n# end\n")

    def test_log_file_when_exit_code_is_unknown(self):
        log, buf = self.log_file()
        log.device("hi\r\n$ ")
        log.hold()
        log.device(" echo __LOGCAP_RC=$?\r\nfish: $? is not the exit status.\r\necho __LOGCAP_RC=$?\r\n> ")
        log.write_exit_line(None)
        log.flush()
        self.assertEqual(buf.getvalue(),
                         "hi\r\nfish: $? is not the exit status.\r\n（終了コード：読めませんでした）\n> ")

    def test_log_file_when_prompt_did_not_come_back(self):
        log, buf = self.log_file()
        log.device("[DBG] start\r\n^C")
        log.write_exit_line(None, prompt_is_back=False)
        log.note("\n# end\n")
        self.assertEqual(buf.getvalue(), "[DBG] start\r\n^C\n（終了コード：読めませんでした）\n# end\n")

    def test_log_file_drops_late_marker_lines(self):
        # 返事が遅れて、次のコマンドの出力に紛れ込んだとき。終了コードの行は増やさない
        log, buf = self.log_file()
        log.device("root@b:~# echo hi\r\n__LOGCAP_RC=0\r\nhi\r\nroot@b:~#  echo __LOGCAP_RC=$?")
        log.flush()
        self.assertEqual(buf.getvalue(), "root@b:~# echo hi\r\nhi\r\n")

    def test_log_file_flushes_held_lines_on_error(self):
        log, buf = self.log_file()
        log.device("out\r\n# ")
        log.hold()
        log.device(" echo __LOGCAP_RC=$?\r\n[  5.0] usb 1-1: x\r\n")
        log.note("\n# 思わぬエラー\n")
        self.assertEqual(buf.getvalue(), "out\r\n[  5.0] usb 1-1: x\r\n# 思わぬエラー\n")
        self.assertNotIn("LOGCAP", buf.getvalue())

    def test_log_file_writes_long_lines_without_newline(self):
        log, buf = self.log_file()
        log.device("x" * 70000)
        self.assertEqual(len(buf.getvalue()), 70000)  # 改行を待たずに書いた
        log.device("y\r\n")
        self.assertEqual(buf.getvalue(), "x" * 70000 + "y\r\n")

    def test_exit_code_notes(self):
        f = self.m.exit_code_notes
        self.assertEqual(f([{"command": "a", "output": "x", "exit_code": 0},
                            {"command": "b", "output": "x", "exit_code": None}]), "")
        self.assertEqual(
            f([{"command": "start-app /opt/x", "output": "start\nstart-app: /opt/x: config file not found\n",
                "exit_code": 1}]),
            "コマンド「start-app /opt/x」は終了コード 1 で終わりました（最後の行：start-app: /opt/x: config file not found）。")
        self.assertEqual(f([{"command": "false", "output": "", "exit_code": 1}]),
                         "コマンド「false」は終了コード 1 で終わりました（出力はありませんでした）。")
        long_line = "x" * 500
        note = f([{"command": "c", "output": long_line, "exit_code": 3}])
        self.assertIn("x" * 200 + "…）", note)
        self.assertNotIn("x" * 201, note)

    def test_trim_prompt_head(self):
        f = self.m.trim_prompt_head
        head = ["", "[0] root@b ~"]
        self.assertEqual(f(["out", "", "[1] root@b ~"], head), ["out"])  # 数の違いは見ない
        self.assertEqual(f(["out", "", "root@other ~"], head), ["out", "", "root@other ~"])
        self.assertEqual(f(["out"], []), ["out"])
        self.assertEqual(f(["out", ""], [""]), ["out", ""])  # 空の行だけなら何もしない

    def test_template_is_read_as_expected(self):
        cfg = self.m.read_yaml(TEMPLATE)
        self.assertEqual(list(cfg.keys()), TEMPLATE_KEYS)
        self.assertEqual(cfg["port"], "")
        self.assertEqual(cfg["baud"], 115200)
        self.assertEqual(cfg["login_user"], "root")
        self.assertEqual(cfg["prompt"], "[#$] $")
        self.assertEqual(cfg["commands"], [])
        self.assertEqual(cfg["command_timeout_seconds"], 30)
        self.assertEqual(cfg["listen_seconds"], 0)
        self.assertIs(cfg["logout"], True)

    def test_yaml_forms(self):
        path = os.path.join(tempfile.mkdtemp(), "x.yaml")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# 先頭のコメント\n"
                "port: /dev/ttyUSB0  # 横のコメント\n"
                "baud: 9600\n"
                "prompt: '\\$ $'\n"
                "login_user: 'it''s'\n"
                "commands:\n"
                "  - start-app /opt/x\n"
                "  # 間のコメント\n"
                "  - \"echo \\\"a\\\"\"\n"
                "listen_seconds: 1.5\n"
                "logout: false\n"
                "login_password:\n"
                "flow: [a, 'b c', \"d\", 3]\n"
            )
        cfg = self.m.read_yaml(path)
        self.assertEqual(cfg["port"], "/dev/ttyUSB0")
        self.assertEqual(cfg["baud"], 9600)
        self.assertEqual(cfg["prompt"], "\\$ $")
        self.assertEqual(cfg["login_user"], "it's")
        self.assertEqual(cfg["commands"], ["start-app /opt/x", 'echo "a"'])
        self.assertEqual(cfg["listen_seconds"], 1.5)
        self.assertIs(cfg["logout"], False)
        self.assertIsNone(cfg["login_password"])
        self.assertEqual(cfg["flow"], ["a", "b c", "d", 3])

    def test_yaml_errors_name_the_line(self):
        path = os.path.join(tempfile.mkdtemp(), "x.yaml")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        for body, line in (("port: \"abc\n", 1), ("a: 1\nprompt: \"\\$ $\"\n", 2),
                           ("a: 1\na: 2\n", 2), ("a: [1, 2\n", 1), ("  b: 1\n", 1)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            with self.assertRaises(self.m.ConfigSyntaxError) as cm:
                self.m.read_yaml(path)
            self.assertEqual(cm.exception.lineno, line, body)

    # --- シリアルポートを開けないとき（pyserial のエラーを、OS ごとに文にする） ---

    def open_fails(self, error, windows, port="/dev/ttyUSB0", ports=()):
        """pyserial が error を出したときに、open_port が出す Failure。"""
        with mock.patch.object(self.m, "IS_WINDOWS", windows), \
                mock.patch.object(self.m, "find_ports", return_value=list(ports)), \
                mock.patch.object(self.m, "_lock_owner", return_value=None), \
                mock.patch.object(self.m.serial, "serial_for_url", side_effect=error):
            with self.assertRaises(self.m.Failure) as cm:
                self.m.open_port(port, 115200)
        self.assertTrue(cm.exception.message.endswith("。"), cm.exception.message)
        return cm.exception

    def serial_error(self, *args):
        return self.m.serial.SerialException(*args)

    def test_permission_denied_message(self):
        f = self.open_fails(self.serial_error(
            errno.EACCES, "could not open port /dev/ttyUSB0: [Errno 13] Permission denied: '/dev/ttyUSB0'"), False)
        self.assertIn("権限がありません", f.message)
        self.assertIn("dialout", f.message)

    def test_busy_message(self):
        f = self.open_fails(self.serial_error(errno.EBUSY, "could not open port /dev/ttyUSB0: busy"), False)
        self.assertIn("ほかのプログラム", f.message)
        self.assertIn("minicom", f.message)
        # pyserial の exclusive=True（flock）で、ほかのプログラムが使っていると分かったとき
        f = self.open_fails(self.serial_error(
            errno.EWOULDBLOCK, "Could not exclusively lock port /dev/ttyUSB0: [Errno 11] Resource temporarily unavailable"),
            False)
        self.assertIn("ほかのプログラム", f.message)

    def test_missing_port_message_lists_ports(self):
        f = self.open_fails(self.serial_error(
            errno.ENOENT, "could not open port /dev/ttyUSB9: [Errno 2] No such file or directory"), False,
            port="/dev/ttyUSB9", ports=["/dev/ttyACM0", "/dev/ttyUSB0"])
        self.assertIn("シリアルポート /dev/ttyUSB9 が見つかりません", f.message)
        self.assertIn("この PC で見つかったシリアルポートは、/dev/ttyACM0、/dev/ttyUSB0 です。", f.message)
        self.assertEqual(f.extra["available_ports"], ["/dev/ttyACM0", "/dev/ttyUSB0"])

    def test_not_a_serial_port_message(self):
        f = self.open_fails(self.serial_error(
            "Could not configure port: (25, 'Inappropriate ioctl for device')"), False, port="/tmp/x")
        self.assertIn("シリアルポートではありません", f.message)

    def test_windows_port_in_use(self):
        # Windows では、ほかのプログラムが開いている COM ポートは「アクセスが拒否されました」になる
        f = self.open_fails(self.serial_error(
            "could not open port 'COM3': PermissionError(13, 'アクセスが拒否されました。', None, 5)"), True,
            port="COM3", ports=["COM3"])
        self.assertIn("シリアルポート COM3 は、ほかのプログラムが使っています", f.message)
        self.assertIn("Tera Term", f.message)
        self.assertNotIn("dialout", f.message)

    def test_windows_port_not_found(self):
        error = self.serial_error(
            "could not open port 'COM9': FileNotFoundError(2, '指定されたファイルが見つかりません。', None, 2)")
        f = self.open_fails(error, True, port="COM9", ports=["COM3", "COM10"])
        self.assertIn("シリアルポート COM9 が見つかりません", f.message)
        self.assertIn("この PC で見つかったシリアルポートは、COM3、COM10 です。", f.message)
        self.assertNotIn("/dev", f.message)
        self.assertNotIn("COM3 のように", f.message)
        # COM の形でない名前なら、書き方も伝える
        f = self.open_fails(error, True, port="ttyUSB0")
        self.assertIn("port は COM3 のように書いてください。", f.message)

    def test_windows_other_errors(self):
        # 番号の分からないエラーで、ポートが一覧にない：見つからないと伝える
        error = self.serial_error("could not open port 'COM7': OSError(22, 'x', None, 1167)")
        f = self.open_fails(error, True, port="COM7", ports=["COM3"])
        self.assertIn("見つかりません", f.message)
        # 一覧にあるなら、エラーの文をそのまま添える
        f = self.open_fails(error, True, port="COM7", ports=["COM7"])
        self.assertIn("シリアルポート COM7 を開けません（could not open port", f.message)
        # 速さなどを設定できない
        f = self.open_fails(self.serial_error(
            "Cannot configure port, something went wrong. Original message: OSError(22, 'x', None, 87)"), True,
            port="COM3", ports=["COM3"])
        self.assertIn("速さなどを設定できません", f.message)

    def test_unknown_url(self):
        f = self.open_fails(ValueError("invalid URL, protocol 'foo' not known"), False, port="foo://x")
        self.assertIn("シリアルポート foo://x を開けません", f.message)

    def test_real_open_and_console_over_loop_url(self):
        """pyserial の loop://（書いたものが、そのまま読める）で、開く・送る・読む・閉じるを通す。"""
        ser = self.m.open_port("loop://", 115200)
        try:
            con = self.m.Console(ser, "loop://", None)
            con.send("hello\r\n")
            self.assertGreater(con.read(1.0), 0)
            con.drain()
            self.assertEqual(con.segment(), "hello\r\n")
            self.assertEqual(con.read(0.1), 0)  # もう何も届かない
        finally:
            self.m.close_port(ser)

    def test_find_ports_uses_pyserial(self):
        class Info(object):
            def __init__(self, device):
                self.device = device
        found = [Info("COM10"), Info("COM3"), Info("COM3"), Info("COM1")]
        with mock.patch.object(self.m.serial.tools.list_ports, "comports", return_value=found):
            self.assertEqual(self.m.find_ports(), ["COM1", "COM3", "COM10"])
        with mock.patch.object(self.m.serial.tools.list_ports, "comports", side_effect=OSError("x")):
            self.assertEqual(self.m.find_ports(), [])

    def test_lock_owner_is_not_checked_on_windows(self):
        # Windows の os.kill(pid, 0) はプロセスを止めてしまうので、呼ばない
        with mock.patch.object(self.m, "IS_WINDOWS", True), mock.patch.object(self.m.os, "kill") as kill:
            self.assertIsNone(self.m._lock_owner("COM3"))
        kill.assert_not_called()
        self.assertIsNone(self.m._lock_owner("socket://127.0.0.1:1"))

    # --- 標準出力の文字コード ---

    def test_streams_follow_the_console_on_windows(self):
        """Windows で、出力先がコンソールでないときは、コンソールの文字コードにする。
        出力先がコンソールのときと、コンソールがない（Windows 以外も）ときは、Python の既定のまま。"""

        def stream(tty):
            class Buffer(io.BytesIO):
                def isatty(self):
                    return tty
            return io.TextIOWrapper(Buffer(), encoding="utf-8")

        for console, tty, expected in (("cp932", False, "cp932"), ("cp932", True, "utf-8"),
                                       (None, False, "utf-8")):
            out, err = stream(tty), stream(tty)
            with mock.patch.object(self.m, "console_code_page", return_value=console), \
                    mock.patch.object(self.m.sys, "stdout", out), mock.patch.object(self.m.sys, "stderr", err):
                self.m.setup_streams()
            for s in (out, err):
                self.assertEqual(codecs.lookup(s.encoding).name, expected, (console, tty))
                self.assertEqual(s.errors, "backslashreplace")
        with mock.patch.object(self.m, "IS_WINDOWS", False):
            self.assertIsNone(self.m.console_code_page())

    def test_for_stream_keeps_json_readable(self):
        f = self.m.for_stream
        text = json.dumps({"a": "日本語 é ＊＊＊ \U0001F600"}, ensure_ascii=False)
        self.assertEqual(f(text, "utf-8"), text)
        for encoding in ("cp932", "cp1252", "ascii"):
            out = f(text, encoding)
            out.encode(encoding)  # 書ける
            self.assertEqual(json.loads(out), {"a": "日本語 é ＊＊＊ \U0001F600"}, encoding)
        self.assertIn("日本語", f(text, "cp932"))   # 書ける文字は、そのまま
        self.assertIn("\\u00e9", f(text, "cp932"))  # 書けない文字は \uXXXX
        self.assertEqual(f(text, None), text)

    def test_every_problem_ends_a_sentence(self):
        """問題の文が「例：…」で終わると、次の文とつながって読めない。"""
        cases = [
            {"port": "", "baud": "", "command_timeout_seconds": ""},
            {"port": ["a"], "baud": 12345, "login_user": ["x"], "login_password": ["y"],
             "login_password_env": ["z"], "prompt": ["p"], "commands": {"a": 1},
             "command_timeout_seconds": 0, "listen_seconds": -1, "logout": "maybe"},
            {"port": "/dev/ttyUSB0", "prompt": "[", "commands": ["", "echo hi"]},
            {"port": "/dev/ttyUSB0", "login_password": "", "login_password_env": "LOG_CAPTURE_TEST_NOT_SET_2"},
        ]
        seen = 0
        for over in cases:
            cfg = self.m.read_yaml(TEMPLATE)
            cfg.update(over)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LOG_CAPTURE_TEST_NOT_SET_2", None)
                _s, problems = self.m.validate(cfg)
            self.assertTrue(problems, over)
            for p in problems:
                seen += 1
                self.assertTrue(p.endswith("。"), p)
                self.assertNotRegex(p, r"例：[^）]*$", p)
        self.assertGreaterEqual(seen, 14)

    def test_single_problem_message_is_punctuated(self):
        msg = self.m.problems_message(["prompt の値が読めません：x"], "/w/c.yaml", False)
        self.assertEqual(msg, "prompt の値が読めません：x。設定ファイルは /w/c.yaml です。")
        msg = self.m.problems_message(["port が空です。"], "/w/c.yaml", False)
        self.assertNotIn("。。", msg)

    def test_port_messages_are_sentences(self):
        with mock.patch.object(self.m, "find_ports", return_value=["/dev/ttyUSB0", "/dev/ttyUSB1"]):
            msg = self.m._not_found_message("ttyUSB9")
        self.assertTrue(msg.endswith("。"), msg)
        self.assertIn("この PC で見つかったシリアルポートは、/dev/ttyUSB0、/dev/ttyUSB1 です。", msg)

    def test_template_examples_are_generic(self):
        """例は一般の名前にする。特定のプロジェクトの名前だと、写すだけで埋まってしまう。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("sensor", text)
        self.assertIn('# 例：["/opt/myapp/my_app"]', text)

    def test_template_comments_do_not_point_by_position(self):
        """コメントは、ファイルを開けない相手（チャットなど）にも、そのまま読まれることがある。
        「下の」などの位置の言葉や「このスキル」は、そこでは通じない。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            if not re.match(r"^[a-z_]+:", line):
                continue
            j = i
            while j > 0 and lines[j - 1].startswith("#"):
                j -= 1
            for comment in lines[j:i]:
                for word in ("下の", "上の", "上から", "このスキル", "この項目"):
                    self.assertNotIn(word, comment, comment)

    def test_validate_resolves_password_without_leaking(self):
        cfg = self.m.read_yaml(TEMPLATE)
        cfg.update({"port": "/dev/ttyUSB0", "login_password": "pw1"})
        s, problems = self.m.validate(cfg)
        self.assertEqual(problems, [])
        self.assertEqual(s.password, "pw1")
        self.assertEqual(self.m.summary_of(cfg)["login_password"], "＊＊＊")


# ---------------------------------------------------------------------------
# 独り立ち：このフォルダだけで動くこと
# ---------------------------------------------------------------------------

STDLIB_ALLOWED = {
    "argparse", "codecs", "ctypes", "datetime", "errno", "json", "os", "re",
    "shutil", "struct", "sys", "time", "traceback",
}
VENDORED = {"serial"}  # scripts/vendor/ に同梱したもの

# 同梱の pyserial 3.5 のファイル（PyPI の pyserial-3.5-py2.py3-none-any.whl と、
# sdist の LICENSE.txt）。中身を変えていないことを、SHA-256 で確かめる。
# 改行は \n にそろえてから比べる（Windows の git が \r\n にして取り出すことがあるため）。
PYSERIAL_FILES = {
    "serial/LICENSE.txt": "f91cb9813de6a5b142b8f7f2dede630b5134160aedaeaf55f4d6a7e2593ca3f3",
    "serial/__init__.py": "5dec897fdef45a0eaf63eacda1e01d62e1e75490302584ae0238f75376592344",
    "serial/serialposix.py": "5d56f98513391e1766a1847a04e2e1202ced4172a152803ba1c823a149a9b6f9",
    "serial/serialutil.py": "3c84f8c7c319f161a85d6f8db5bedc4f65255714589bdcbeee6f2daf65a17c0a",
    "serial/serialwin32.py": "17681ea9a650120c71db1aae9b8881301a40d38c47b34bb0da050e3f5bfbbe00",
    "serial/win32.py": "964eaba1df661e4ab381c8666aa077ca08939260164cd434d0827df39663bd32",
    "serial/tools/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "serial/tools/list_ports.py": "7830e8c888684b77fd0f7095a6d86a95052a891da5f97d154c618e063b8ce1ec",
    "serial/tools/list_ports_common.py": "c791c88211b8348cfe5dfe625fa1a4ef165f75e69db36b6a0ac7f226187721fb",
    "serial/tools/list_ports_linux.py": "5275355583f534923b27c667ee063e03699b8b596e81b159495ced7d7f0fd695",
    "serial/tools/list_ports_osx.py": "7a879f3068ae26a0be382bbd6805aaa895fbe701a5073a2a67a91d98c03cd8b3",
    "serial/tools/list_ports_posix.py": "118a83e6445b9347f66b9b1c6914b8b605860729e4a551fbee36bf1ba4b75211",
    "serial/tools/list_ports_windows.py": "53813370e02253ae8b5a83dd317236a0e3382c2779bcac0edc381e4d3e8cdea7",
    "serial/urlhandler/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "serial/urlhandler/protocol_loop.py": "e5b6abaeefe17f03646b28e3073e30dec9c1267d06ec2f9f1bbf9098769e4d6a",
    "serial/urlhandler/protocol_socket.py": "428b5a1c23ddeadf74dd6ffd7dad8bbffe6e125ab49e88abd828b5d1af7ced73",
}

# フォルダの外へ出るパスの書き方。この試験のファイルにも、そのままは書かない
OUTSIDE = os.pardir + "/"

# 写すときに付いてくることがある、ごみ
JUNK = ("__pycache__", "*.pyc")

# スキルのものではないフォルダ。利用者が git で管理すると .git ができ、
# スキルのフォルダを作業フォルダにして動かすと、設定とログのフォルダができる
NOT_OURS = (".git", DATA_DIR_NAME)


def skill_files():
    """スキルのフォルダの中のファイル（__pycache__ などのごみと、NOT_OURS は除く）。"""
    for folder, dirs, files in os.walk(SKILL_DIR):
        dirs[:] = sorted(d for d in dirs
                         if d != "__pycache__" and not (folder == SKILL_DIR and d in NOT_OURS))
        for name in sorted(files):
            if not name.endswith(".pyc"):
                yield os.path.join(folder, name)


class SelfContained(unittest.TestCase):

    def test_no_path_goes_outside_the_folder(self):
        """どのテキストにも、フォルダの外へ出るパスを書かない。"""
        seen = 0
        for path in skill_files():
            with open(path, "rb") as f:
                raw = f.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            seen += 1
            self.assertNotIn(OUTSIDE, text, path)
        self.assertGreaterEqual(seen, 6)

    def test_script_uses_only_stdlib_and_own_folder(self):
        with open(SCRIPT, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, feature_version=(3, 8))  # Python 3.8 で読める書き方
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "相対の import は使わない")
                names.add(node.module.split(".")[0])
        self.assertTrue(names <= STDLIB_ALLOWED | VENDORED, names - STDLIB_ALLOWED - VENDORED)
        # 読み込みの道に足すのは、自分のフォルダの vendor だけ
        self.assertEqual(re.findall(r"sys\.path\.\w+\([^)]*\)", source), ["sys.path.insert(0, VENDOR_DIR)"])
        self.assertIn('VENDOR_DIR = os.path.join(SCRIPT_DIR, "vendor")', source)
        self.assertNotIn(OUTSIDE, source)

    def test_vendored_pyserial_is_unchanged(self):
        found = {}
        for folder, dirs, files in os.walk(VENDOR_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".pyc"):
                    continue
                path = os.path.join(folder, name)
                with open(path, "rb") as f:
                    data = f.read().replace(b"\r\n", b"\n")
                found[os.path.relpath(path, VENDOR_DIR).replace(os.sep, "/")] = hashlib.sha256(data).hexdigest()
        self.assertEqual(found, PYSERIAL_FILES)

    def test_vendored_pyserial_is_loaded_first(self):
        """ほかに serial という名前のものが入っていても、同梱の pyserial を使う。"""
        m = script_module()
        self.assertTrue(os.path.abspath(m.serial.__file__).startswith(os.path.join(VENDOR_DIR, "serial") + os.sep),
                        m.serial.__file__)
        self.assertEqual(m.serial.__version__, "3.5")
        place = make_tmp("log-capture-other-serial-")
        self.addCleanup(shutil.rmtree, place, True)
        os.makedirs(os.path.join(place, "serial"))
        with open(os.path.join(place, "serial", "__init__.py"), "w", encoding="utf-8") as f:
            f.write("raise ImportError('this is not the bundled pyserial')\n")
        env = dict(os.environ, PYTHONPATH=place, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, SCRIPT, "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, cwd=place, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout.decode(child_encoding()))["ok"])

    def test_running_the_script_leaves_no_bytecode(self):
        """PYTHONDONTWRITEBYTECODE がなくても、スキルのフォルダに __pycache__ を作らない。"""
        place = make_tmp("log-capture-bytecode-")
        self.addCleanup(shutil.rmtree, place, True)
        copy = os.path.join(place, SKILL_NAME)
        shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns(*(JUNK + NOT_OURS)))
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        work = os.path.join(place, "work")
        os.makedirs(work)
        p = subprocess.run([sys.executable, os.path.join(copy, "scripts", "capture.py"), "config-path"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=work, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        made = [d for d, ds, _ in os.walk(copy) if os.path.basename(d) == "__pycache__"]
        self.assertEqual(made, [])

    def test_output_encoding(self):
        """標準出力と標準エラーは、Windows ではコンソールの文字コード（cp932 など）、
        それ以外では Python の既定（PYTHONIOENCODING の値など）で書く。
        UTF-8 モード（-X utf8。Python 3.15 からの既定）でも、Windows ではコンソールに合わせる。
        書けない文字は \\uXXXX の形にするので、どの文字コードでも JSON として読める。"""
        place = make_tmp("log-capture-encoding-")
        self.addCleanup(shutil.rmtree, place, True)
        work = os.path.join(place, "作業-é")
        os.makedirs(work)
        expected = os.path.join(work, DATA_DIR_NAME, "config.yaml")
        os.makedirs(os.path.dirname(expected))
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read().replace('port: ""', 'port: "%s"' % MISSING_PORT)
        with open(expected, "w", encoding="utf-8") as f:
            f.write(text)
        for io_encoding, options in (("cp932", []), ("ascii", []), ("utf-8", []), (None, ["-X", "utf8"])):
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            for key in ("PYTHONIOENCODING", "PYTHONUTF8"):
                env.pop(key, None)
            if io_encoding:
                env["PYTHONIOENCODING"] = io_encoding
            encoding = console_code_page() or io_encoding or "utf-8"   # 読む側の文字コード
            for args in (["config-path"], ["check", "--config", expected]):
                p = subprocess.run([sys.executable] + options + [SCRIPT] + args, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, cwd=work, timeout=60)
                where = (io_encoding, options, args)
                out = p.stdout.decode(encoding)
                err = p.stderr.decode(encoding)
                self.assertNotIn("Traceback", out + err)
                data = json.loads(out)
                self.assertEqual(data["config"], expected, where)
                if args[0] == "check":
                    self.assertIn("シリアルポート %s が見つかりません。" % MISSING_PORT, data["message"], where)
                    err = unescape(err, encoding)
                    self.assertIn("シリアルポート %s を開きます" % MISSING_PORT, err, where)
                    self.assertIn("ログ：%s" % os.path.join(work, DATA_DIR_NAME, "logs"), err, where)
                if encoding == "cp932":
                    self.assertIn("作業", out)        # cp932 で書ける文字は、そのまま
                    self.assertIn("\\u00e9", out)    # é は cp932 で書けない

    def test_skill_md_does_not_show_exit_codes(self):
        """スクリプト自身の終了コードで場合分けを書くと、モデルが利用者に見せてしまう。

        機器のコマンドの終了コード（exit_code）は、「終了コード N」の形で伝える。
        """
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
        for line in text.splitlines():
            if "スクリプト" in line and ("終了コード" in line or "終わりのコード" in line):
                self.assertIn("伝えません", line, line)
        self.assertIn("自身の終了コードは、利用者に伝えません", text)
        self.assertNotRegex(text, r"[0-2] ?[がは](成功|問題)")
        self.assertIn("`.gitignore`", text)

    def test_skill_md_explains_gitignore_and_added_keys(self):
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("フォルダを新しく作るときは", text)
        self.assertIn("`.gitignore` がなければ", text)
        self.assertIn("足りない項目", text)
        self.assertIn("ほかの所は変えません", text)
        self.assertIn("`added`", text.split("## 結果の伝え方", 1)[1])

    def test_skill_md_hides_json_names(self):
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
        section = text.split("## 結果の伝え方", 1)[1].split("## 守ること", 1)[0]
        self.assertIn("JSON の項目名", section)
        self.assertIn("利用者に見せません", section)
        self.assertIn("設定ファイルの項目名（commands、listen_seconds など）は、見せてかまいません", section)
        self.assertNotIn("sensor", text)

    def test_skill_md_tells_device_exit_codes(self):
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
        section = text.split("## 結果の伝え方", 1)[1].split("## 守ること", 1)[0]
        self.assertIn("`exit_code` が 0 以外の数なら", section)
        self.assertIn("「終了コード N」", section)
        self.assertIn("`exit_code` という項目名は見せません", section)

    def test_skill_md_marks_guesses_as_guesses(self):
        """結果にない原因を、前のやり取りから推測して、事実のように書かないこと。"""
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
        section = text.split("## 結果の伝え方", 1)[1].split("## 守ること", 1)[0]
        self.assertIn("結果にない原因を、事実として書きません。", section)
        self.assertIn("推測だと分かるように書きます。", section)

    def test_skill_md_does_not_show_the_marker(self):
        """終了コードを調べるための印は、SKILL.md にも書かない（利用者への答えに写されないように）。"""
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(MARKER_TAG, text)
        self.assertNotIn("$?", text)
        self.assertIn("ログにも残しません", text)
        self.assertIn("「（終了コード：0）」", text)
        self.assertIn("「（終了コード：読めませんでした）」", text)

    def test_messages_that_ask_to_check_name_concrete_things(self):
        """「確かめてください」の文は、何を確かめるかを具体的に言う。"""
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("状態を確かめて", text)
        self.assertNotIn("ようすを確かめて", text)

    def test_skill_files_are_only_its_own(self):
        rel = sorted(os.path.relpath(p, SKILL_DIR) for p in skill_files())
        self.assertEqual(rel, sorted([
            "README.md", "SKILL.md", "config-template.yaml",
            os.path.join("scripts", "capture.py"),
            os.path.join("tests", "__init__.py"),
            os.path.join("tests", "fake_console.py"),
            os.path.join("tests", "test_log_capture.py"),
        ] + [os.path.join("scripts", "vendor", *name.split("/")) for name in PYSERIAL_FILES]))

    def test_skill_md_frontmatter(self):
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m)
        front = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
        # name は、フォルダの名前と同じにする（名前を変えて写したときは、両方を変える）
        self.assertEqual(front["name"].strip(), SKILL_NAME)
        self.assertIn("スラッシュコマンド", front["description"])
        self.assertNotRegex(text, r"/[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]")  # 「/なにか:名前」の形のコマンドは書かない
        for sub in ("capture.py run", "capture.py config-path", "capture.py check"):
            self.assertIn(sub, text)
        self.assertIn("`config.yaml`", text)
        self.assertIn("`logs`", text)

    def test_readme_covers_the_settings_and_the_test_command(self):
        with open(README, encoding="utf-8") as f:
            text = f.read()
        for key in TEMPLATE_KEYS:
            self.assertIn("`%s`" % key, text, key)
        for word in ("/skills reload", ".github/skills/", "~/.copilot/skills/", "config.yaml",
                     "py -3 -m unittest discover -s tests", "python3 -m unittest discover -s tests",
                     "Python 3.8", "pyserial 3.5", "COM3", "/dev/ttyUSB0"):
            self.assertIn(word, text, word)
        # Windows を先に書く
        self.assertLess(text.index("COM3"), text.index("/dev/ttyUSB0"))
        self.assertLess(text.index("py -3"), text.index("python3"))

    def test_template_comments(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        # 頭の説明。名前を変えて写しても食い違わないように、スキルの名前は書かない
        self.assertTrue(lines[0].startswith("# "), lines[0])
        self.assertIn("スキルの設定ファイルです。", lines[0])
        self.assertNotIn(SKILL_NAME, lines[0])
        keys = []
        for i, line in enumerate(lines):
            m = re.match(r"^([a-z_]+):", line)
            if not m:
                continue
            keys.append(m.group(1))
            self.assertTrue(lines[i - 1].startswith("#"), "%s の上にコメントがありません" % m.group(1))
            j = i - 1
            while j >= 0 and lines[j].startswith("#"):
                j -= 1
            first = lines[j + 1]
            self.assertFalse(first.startswith("# 例："), m.group(1))
            if m.group(1) == "port":
                self.assertIn("必須", first)
        self.assertEqual(keys, TEMPLATE_KEYS)



# ---------------------------------------------------------------------------
# スキルのフォルダだけを写して使う
# ---------------------------------------------------------------------------

class CopiedFolder(unittest.TestCase):
    """このフォルダだけを、空の場所に写して動かす（名前を変えて写すこともある）。"""

    def use_copy(self, name):
        place = make_tmp("log-capture-copy-")
        self.addCleanup(shutil.rmtree, place, True)
        skills = os.path.join(place, "skills")
        work = os.path.join(place, "work")    # 空の作業フォルダ
        home = os.path.join(place, "home")    # 空のホーム
        for folder in (skills, work, home):
            os.makedirs(folder)
        copy = os.path.join(skills, name)
        shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns(*(JUNK + NOT_OURS)))
        copied = sorted(os.path.relpath(os.path.join(d, f), copy)
                        for d, _, fs in os.walk(copy) for f in fs)
        script = os.path.join(copy, "scripts", "capture.py")
        # Windows のホームは USERPROFILE
        env = dict(os.environ, HOME=home, USERPROFILE=home, PYTHONDONTWRITEBYTECODE="1")
        for key in ("PYTHONPATH", "COPILOT_HOME"):
            env.pop(key, None)

        def capture(*args):
            p = subprocess.run([sys.executable, script] + list(args), cwd=work, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            out = p.stdout.decode(child_encoding())
            err = unescape(p.stderr.decode(child_encoding(), "replace"))
            self.assertNotIn("Traceback", out + err)
            # 経過の表示の頭は、写したフォルダの名前
            for line in err.splitlines():
                self.assertTrue(line.startswith("[%s] " % name), line)
            return p.returncode, json.loads(out)

        data_dir = os.path.join(work, "." + name)

        # config-path：設定ファイルを、作業フォルダの .<写したフォルダの名前>/ に作る
        code, data = capture("config-path")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["config"], os.path.join(data_dir, "config.yaml"))
        self.assertTrue(data["created"])
        self.assertEqual(os.listdir(work), ["." + name])
        self.assertEqual(sorted(os.listdir(data_dir)), [".gitignore", "config.yaml"])
        with open(os.path.join(data_dir, ".gitignore"), "rb") as f:
            self.assertEqual(f.read(), b"*\n")
        with open(TEMPLATE, encoding="utf-8") as a, open(data["config"], encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read())

        # check：ログを、同じフォルダの logs/ に作る
        with open(data["config"], encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r'(?m)^port: ""$', lambda m: 'port: "%s"' % MISSING_PORT, text)
        with open(data["config"], "w", encoding="utf-8") as f:
            f.write(text)
        code, data = capture("check")
        self.assertEqual(code, 2, data)
        self.assertEqual(data["skill"], name)
        self.assertIn("見つかりません", data["problems"][0])
        self.assertEqual(os.path.dirname(data["log"]), os.path.join(data_dir, "logs"))
        self.assertTrue(os.path.basename(data["log"]).startswith(name + "-"), data["log"])
        self.assertEqual(os.listdir(work), ["." + name])
        self.assertEqual(sorted(os.listdir(data_dir)), [".gitignore", "config.yaml", "logs"])

        # ほかの場所には、何も作らない（点で始まるほかのフォルダもできない）
        self.assertEqual(sorted(os.listdir(place)), ["home", "skills", "work"])
        self.assertEqual(os.listdir(home), [])
        self.assertEqual(os.listdir(skills), [name])
        self.assertEqual(sorted(os.path.relpath(os.path.join(d, f), copy)
                                for d, _, fs in os.walk(copy) for f in fs), copied)
        dot_dirs = sorted(os.path.join(d, x) for d, ds, _ in os.walk(place) for x in ds if x.startswith("."))
        self.assertEqual(dot_dirs, [data_dir])

    def test_copy_with_the_same_name(self):
        self.use_copy(SKILL_NAME)

    def test_copy_with_another_name(self):
        other = "serial-console-log"
        if other == SKILL_NAME:
            other += "-2"
        self.use_copy(other)


if __name__ == "__main__":
    unittest.main()
