"""serial-console のテスト。

このスキルのフォルダで動かす：python3 -m unittest discover -s tests（Windows は py -3）
機器の代わりに、tests/fake_device.py の機器のまねを TCP（socket://127.0.0.1:<番号>）で動かす。
ほかに入れるものはなく、外のネットワークも使わない。
"""

import configparser
import errno
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SKILL_NAME = os.path.basename(SKILL_DIR)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "serial_console.py")
TEMPLATE = os.path.join(SKILL_DIR, "config-template.ini")
PASSWORD = "Pw%x-secret"  # % があっても、そのまま読めること
ENV_NAME = "SERIAL_CONSOLE_TEST_PASSWORD"
LINUX = sys.platform.startswith("linux")

sys.path.insert(0, HERE)
from fake_device import COLOR_PROMPT, KMSG, FakeDevice  # noqa: E402


def load_script():
    """スクリプトを読み込む。スキルのフォルダに __pycache__ を作らない。"""
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("serial_console", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


serial_console = load_script()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="serial-console-test-")
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.data = os.path.join(self.workdir, "." + SKILL_NAME)
        self.config = os.path.join(self.data, "config.ini")

    def device(self, **kw):
        kw.setdefault("password", PASSWORD)
        device = FakeDevice(**kw)
        self.addCleanup(device.close)
        return device

    def script(self, *args, env=None, script=SCRIPT):
        environ = dict(os.environ)
        environ.pop(ENV_NAME, None)
        environ.update(env or {})
        proc = subprocess.run([sys.executable, script] + list(args) + ["--workdir", self.workdir],
                              capture_output=True, env=environ, timeout=120)
        self.assertTrue(all(b < 128 for b in proc.stdout), "結果の JSON に ASCII でない文字があります")
        self.assertEqual(proc.stderr, b"")
        return proc.returncode, json.loads(proc.stdout.decode("ascii"))

    def configure(self, device=None, **values):
        """ひな形から設定ファイルを作り、values の項目を書き換える。"""
        self.assertEqual(self.script("init")[0], 0)
        parser = configparser.ConfigParser(interpolation=None)
        with open(self.config, encoding="utf-8") as fh:
            parser.read_file(fh)
        if device is not None:
            values = dict({"port": device.port, "login_password": PASSWORD}, **values)
        for key, value in values.items():
            parser.set("settings", key, value)
        with open(self.config, "w", encoding="utf-8") as fh:
            parser.write(fh)

    def files(self):
        return sorted(os.path.join(root, name) for root, dirs, names in os.walk(self.workdir)
                      for name in dirs + names)

    def log_text(self, result):
        with open(result["log"], encoding="utf-8") as fh:
            return fh.read()


class InitTest(Base):
    def test_init_creates_config_and_gitignore(self):
        code, result = self.script("init")
        self.assertEqual((code, result["ok"], result["created"]), (0, True, True))
        with open(os.path.join(self.data, ".gitignore"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "*\n")
        with open(self.config, encoding="utf-8") as fh, open(TEMPLATE, encoding="utf-8") as template:
            self.assertEqual(fh.read(), template.read())
        with open(self.config, "a", encoding="utf-8") as fh:
            fh.write("# 書き足した\n")
        code, result = self.script("init")
        self.assertEqual((code, result["created"]), (0, False))
        with open(self.config, encoding="utf-8") as fh:
            self.assertIn("# 書き足した", fh.read())

    def test_template_has_a_comment_above_every_key(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        comments = {}  # 項目 → すぐ上のコメントの行
        for i, line in enumerate(lines):
            m = re.match(r"^([a-z_]+) =", line)
            if m:
                block = []
                while i > 0 and lines[i - 1].startswith("# "):
                    i -= 1
                    block.insert(0, lines[i])
                self.assertTrue(block, m.group(1))
                comments[m.group(1)] = "\n".join(block)
        self.assertEqual(sorted(comments), sorted(serial_console.KEYS))
        self.assertIn("（必須）", comments["port"])
        self.assertIn("（秘密）", comments["login_password"])
        self.assertIn("# 例：", comments["port"])

    def test_renamed_copy_uses_its_own_name(self):
        tmp = tempfile.mkdtemp(prefix="serial-console-copy-")
        self.addCleanup(shutil.rmtree, tmp, True)
        copy = os.path.join(tmp, "my-console")
        shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        code, result = self.script("init", script=os.path.join(copy, "scripts", "serial_console.py"))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, ".my-console", "config.ini")))
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, ".my-console", ".gitignore")))
        self.assertFalse(os.path.exists(os.path.join(copy, "scripts", "vendor", "serial", "__pycache__")))


class CheckTest(Base):
    def test_check_without_config_creates_nothing(self):
        code, result = self.script("check")
        self.assertEqual(code, 2)
        self.assertTrue(result["problems"])
        self.assertEqual(self.files(), [])

    def test_check_logs_in_runs_nothing_and_creates_nothing(self):
        device = self.device()
        self.configure(device, commands="echo configured")
        before = self.files()
        code, result = self.script("check")
        self.assertEqual(code, 0, result["message"])
        self.assertIn("ログインしました", result["message"])
        self.assertIsNone(result["log"])
        self.assertEqual(self.files(), before)
        self.assertNotIn(b"configured", device.received)

    def test_check_when_already_logged_in(self):
        self.configure(self.device(state="shell"))
        code, result = self.script("check")
        self.assertEqual(code, 0, result["message"])
        self.assertIn("ログインは求められませんでした", result["message"])
        self.assertIn("--command", result["message"])  # commands が空

    def test_half_typed_user_name_is_cancelled(self):
        self.configure(self.device(typed="ro"))
        code, result = self.script("check")
        self.assertEqual(code, 0, result["message"])

    def test_wrong_password(self):
        self.configure(self.device(), login_password="wrong")
        code, result = self.script("check")
        self.assertEqual(code, 1)
        self.assertIn("ログインできません", result["message"])
        self.assertIn("login_password", result["message"])

    def test_password_from_environment_wins(self):
        self.configure(self.device(), login_password="wrong", login_password_env=ENV_NAME)
        code, result = self.script("check", env={ENV_NAME: PASSWORD})
        self.assertEqual(code, 0, result["message"])

    def test_missing_environment_variable(self):
        self.configure(self.device(), login_password="", login_password_env=ENV_NAME)
        code, result = self.script("check")
        self.assertEqual(code, 2)
        self.assertIn(ENV_NAME, result["message"])

    def test_login_requested_without_user(self):
        self.configure(self.device(), login_user="")
        code, result = self.script("check")
        self.assertEqual(code, 2)
        self.assertIn("login_user", result["message"])

    def test_command_option_only_for_run(self):
        code, result = self.script("check", "--command", "uname")
        self.assertEqual(code, 2)

    def test_bad_settings(self):
        self.configure(port="", baud="fast", prompt="[", command_timeout_seconds="0", colour="x")
        code, result = self.script("check")
        self.assertEqual(code, 2)
        text = "".join(result["problems"])
        for word in ("port", "baud", "prompt", "command_timeout_seconds", "colour"):
            self.assertIn(word, text)

    def test_silent_device(self):
        self.configure(self.device(mute=True))
        code, result = self.script("check")
        self.assertEqual(code, 1)
        self.assertIn("何も届きません", result["message"])
        self.assertIn("baud", result["message"])


class RunTest(Base):
    def test_output_and_exit_codes(self):
        self.configure(self.device(), commands="\n".join(["echo hi", "false", "nope", "echo $?"]))
        code, result = self.script("run")
        self.assertEqual(code, 0, result["message"])
        got = [(c["command"], c["output"], c["exit_code"], c["timed_out"]) for c in result["commands"]]
        self.assertEqual(got, [("echo hi", "hi", 0, False), ("false", "", 1, False),
                               ("nope", "sh: nope: not found", 127, False),
                               ("echo $?", "0", 0, False)])  # $? は、終了コードを調べるコマンドのものになる
        self.assertEqual(result["not_run"], [])
        self.assertIn("「false」（1）", result["message"])
        self.assertIn("| hi", self.log_text(result))

    def test_command_option_replaces_configured_commands(self):
        device = self.device()
        self.configure(device, commands="echo configured")
        code, result = self.script("run", "--command", "echo one", "--command", "echo two")
        self.assertEqual(code, 0, result["message"])
        self.assertEqual([c["output"] for c in result["commands"]], ["one", "two"])
        self.assertNotIn(b"configured", device.received)

    def test_no_commands(self):
        self.configure(self.device())
        code, result = self.script("run")
        self.assertEqual(code, 2)
        self.assertIn("--command", result["message"])

    def test_long_output_keeps_last_lines_and_log_keeps_all(self):
        self.configure(self.device(state="shell"))
        code, result = self.script("run", "--command", "seq 200")
        self.assertEqual(code, 0, result["message"])
        item = result["commands"][0]
        self.assertEqual(item["output"].split("\n"), [str(i) for i in range(141, 201)])
        self.assertTrue(item["truncated"])
        logged = re.findall(r"(?m)^\S+ \S+ \| (\d+)$", self.log_text(result))
        self.assertEqual(logged, [str(i) for i in range(1, 201)])

    def test_color_codes_are_removed(self):
        self.configure(self.device(prompt=COLOR_PROMPT), commands="color")
        code, result = self.script("run")
        self.assertEqual(code, 0, result["message"])
        self.assertEqual(result["commands"][0]["output"], "red and green")
        self.assertNotIn("\x1b", self.log_text(result))

    def test_timeout_sends_ctrl_c_and_stops(self):
        device = self.device()
        self.configure(device, command_timeout_seconds="1",
                       commands="\n".join(["echo before", "hang", "echo after"]))
        code, result = self.script("run")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        before, hang = result["commands"]
        self.assertEqual((before["exit_code"], before["timed_out"]), (0, False))
        self.assertEqual((hang["exit_code"], hang["timed_out"]), (None, True))
        self.assertEqual(result["not_run"], ["echo after"])
        self.assertIn(b"\x03", device.received)
        self.assertNotIn(b"echo after", device.received)
        self.assertIn("Ctrl-C", result["message"])

    def test_prompt_hidden_by_device_message(self):
        self.configure(self.device(state="shell"), commands="kmsg\necho next")
        code, result = self.script("run")
        self.assertEqual(code, 0, result["message"])
        kmsg, nxt = result["commands"]
        self.assertIn(KMSG, kmsg["output"])
        self.assertEqual((kmsg["exit_code"], kmsg["timed_out"]), (0, False))
        self.assertEqual((nxt["output"], nxt["exit_code"]), ("next", 0))

    def test_password_is_masked(self):
        self.configure(self.device())
        code, result = self.script("run", "--command", "echo " + PASSWORD)
        self.assertEqual(code, 0, result["message"])
        self.assertEqual(result["commands"][0]["output"], serial_console.MASK)
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertNotIn(PASSWORD, self.log_text(result))

    def test_gitignore_is_put_back_before_writing(self):
        self.configure(self.device(state="shell"))
        os.remove(os.path.join(self.data, ".gitignore"))
        code, result = self.script("run", "--command", "true")
        self.assertEqual(code, 0, result["message"])
        with open(os.path.join(self.data, ".gitignore"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "*\n")

    def test_disconnected_in_the_middle(self):
        self.configure(self.device(state="shell"), commands="\n".join(["echo a", "bye", "echo b"]))
        code, result = self.script("run")
        self.assertEqual(code, 1)
        self.assertIn("切れました", result["message"])
        self.assertEqual(result["commands"][0]["output"], "a")
        self.assertEqual(result["not_run"], ["echo b"])


class PortTest(Base):
    def test_port_not_found_lists_ports(self):
        self.configure(port="COM199" if os.name == "nt" else "/dev/ttyNOSUCH0")
        code, result = self.script("check")
        self.assertEqual(code, 1)
        self.assertIn("見つかりません", result["message"])
        self.assertIsInstance(result["ports"], list)

    def test_socket_refused(self):
        self.configure(port="socket://127.0.0.1:%d" % free_port())
        code, result = self.script("check")
        self.assertEqual(code, 1)
        self.assertIn("つながりません", result["message"])

    @unittest.skipUnless(LINUX, "疑似端末は Linux だけで試す")
    def test_run_over_a_real_tty(self):
        self.configure(self.device(pty=True), commands="echo hi")
        code, result = self.script("run")
        self.assertEqual(code, 0, result["message"])
        self.assertEqual(result["commands"][0]["output"], "hi")

    @unittest.skipUnless(LINUX, "flock は Linux だけで試す")
    def test_busy_port(self):
        import fcntl
        device = self.device(pty=True)
        fd = os.open(device.port, os.O_RDWR | os.O_NOCTTY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.configure(device)
        code, result = self.script("check")
        self.assertEqual(code, 1)
        self.assertIn("ほかのプログラムが使っています", result["message"])

    def test_open_error_messages(self):
        error = serial_console.serial.SerialException
        self.assertIn("dialout", serial_console.open_error("/dev/ttyUSB0", error(errno.EACCES, "x"), {}))
        self.assertIn("ほかのプログラム", serial_console.open_error("/dev/ttyUSB0", error(errno.EBUSY, "x"), {}))
        saved = serial_console.IS_WINDOWS
        serial_console.IS_WINDOWS = True
        try:
            text = "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"
            self.assertIn("Tera Term", serial_console.open_error("COM3", error(text), {}))
        finally:
            serial_console.IS_WINDOWS = saved


class TextTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(serial_console.clean("\x1b[01;32mok\x1b[0m\r\nab\bc\x1b]0;title\x07\x07"), "ok\nac")

    def test_drop_echo_of_a_wrapped_command(self):
        command = "echo " + "x" * 90
        wrapped = ["echo " + "x" * 74, "xx" + "x" * 15, "out"]  # 折り返しで1文字が2回出た
        self.assertEqual(serial_console.drop_echo(wrapped, command), ["out"])
        self.assertEqual(serial_console.drop_echo(["out"], "uname"), ["out"])


if __name__ == "__main__":
    unittest.main()
