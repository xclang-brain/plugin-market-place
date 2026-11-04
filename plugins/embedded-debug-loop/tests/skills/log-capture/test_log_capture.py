# -*- coding: utf-8 -*-
"""log-capture スキル（scripts/capture.py）の試験。

機器のシリアルコンソールのまね（fake_console.py）を pty で作り、
スクリプトを別のプロセスとして動かして、JSON と終わりのコードを確かめる。
標準ライブラリだけで動く。
"""

import ast
import errno
import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fake_console import KERNEL_LINE, FakeDevice, Relay  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SKILL_DIR = os.path.join(ROOT, "skills", "log-capture")
SCRIPT = os.path.join(SKILL_DIR, "scripts", "capture.py")
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")

TEMPLATE_KEYS = [
    "port", "baud", "login_user", "login_password", "login_password_env", "prompt",
    "commands", "command_timeout_seconds", "listen_seconds", "logout",
]


def load_module():
    spec = importlib.util.spec_from_file_location("log_capture_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CaptureTestCase(unittest.TestCase):
    """機器のまねを立て、スクリプトを動かすための土台。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="edl-log-capture-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.relay = None
        self.device = None

    def start_device(self, plugged=True, device=True, **kw):
        self.relay = Relay().start()
        self.addCleanup(self.relay.stop)
        if not plugged:
            self.relay.unplug()
        if device:
            self.device = FakeDevice(self.relay, **kw).start()
            self.addCleanup(self.device.stop)
        return self.relay.port

    @property
    def config_path(self):
        return os.path.join(self.tmp, ".embedded-debug-loop", "log-capture.yaml")

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
        out = p.stdout.decode("utf-8")
        self.stderr = p.stderr.decode("utf-8")
        try:
            data = json.loads(out)
        except ValueError:
            self.fail("標準出力が JSON ではありません：%r\n標準エラー：%s" % (out, self.stderr))
        self.assertIsInstance(data, dict)
        self.assertNotIn("Traceback", out)
        return p.returncode, data

    def read_log(self, data):
        self.assertTrue(os.path.isabs(data["log"]))
        self.assertTrue(data["log"].startswith(os.path.join(self.tmp, ".embedded-debug-loop", "logs")))
        self.assertRegex(os.path.basename(data["log"]), r"^log-capture-\d{8}-\d{6}(-\d+)?\.log$")
        with open(data["log"], encoding="utf-8") as f:
            return f.read()


# ---------------------------------------------------------------------------
# run：ログイン、コマンド、出力
# ---------------------------------------------------------------------------

class RunLoginAndCommands(CaptureTestCase):

    def test_login_then_command_then_logout(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["run-app /opt/x"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["skill"], "log-capture")
        self.assertTrue(data["logged_in"])
        self.assertTrue(data["logged_out"])
        self.assertEqual(len(data["commands"]), 1)
        c = data["commands"][0]
        self.assertEqual(c["command"], "run-app /opt/x")
        self.assertEqual(c["output"], "[DBG] AVG=3")
        self.assertFalse(c["timed_out"])
        self.assertIn("1 個のコマンドを実行し", data["message"])
        self.assertEqual(self.device.login_attempts, [("root", True)])
        self.assertEqual(self.device.executed, ["run-app /opt/x", "exit"])
        self.assertEqual(self.device.state, "login")  # ログアウトして、ログインの求めに戻った
        log = self.read_log(data)
        self.assertIn("sensor-board login:", log)
        self.assertIn("[DBG] AVG=3", log)
        self.assertEqual(data["config"], self.config_path)

    def test_default_is_run_when_no_subcommand(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", commands=["echo hi"])
        code, data = self.capture()
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "hi")

    def test_console_already_at_shell_prompt(self):
        port = self.start_device(login=False)
        self.write_config(port=port, commands=["run-app /opt/x"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertFalse(data["logged_in"])
        self.assertNotIn("logged_out", data)
        self.assertEqual(data["commands"][0]["output"], "[DBG] AVG=3")
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
            "run-app /opt/sensor/sensor_app", "cat /var/log/messages", "true", "nosuchcmd -x"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        outs = [c["output"] for c in data["commands"]]
        self.assertEqual(outs[0], "[DBG] start\n[DBG] AVG=3\nsensor_app: done")
        self.assertEqual(outs[1], "line one\nline two\nline three")
        self.assertEqual(outs[2], "")
        self.assertEqual(outs[3], "-sh: nosuchcmd: not found")
        for c in data["commands"]:
            self.assertNotIn(c["command"], c["output"].split("\n")[0] if c["output"] else "")
            self.assertNotIn("root@sensor-board", c["output"])

    def test_long_command_echo_wrapped_by_shell(self):
        port = self.start_device(login=False, wrap=12)
        self.write_config(port=port, commands=["run-app /opt/sensor/sensor_app"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "[DBG] start\n[DBG] AVG=3\nsensor_app: done")

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
        self.assertEqual(outs, ["[DBG] AVG=3", "hi", "[DBG] AVG=3"])
        self.assertFalse(any(c["timed_out"] for c in data["commands"]))
        self.assertTrue(data["logged_out"])
        self.assertLess(self.elapsed, 15)

    def test_console_with_frequent_messages(self):
        port = self.start_device(login=True, ticker=0.3)
        self.write_config(port=port, login_password="root",
                          commands=["run-app /opt/x", "echo hi", "cat /var/log/messages"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.device.login_attempts, [("root", True)])
        expected = [["[DBG] AVG=3"], ["hi"], ["line one", "line two", "line three"]]
        for c, want in zip(data["commands"], expected):
            self.assertFalse(c["timed_out"], c)
            got = [line for line in c["output"].split("\n") if line != KERNEL_LINE]
            self.assertEqual(got, want, c)
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
        self.write_config(port=port, login_password="root", commands=["run-app /opt/x"])
        code, data = self.capture("run", "--command", "echo hi", "--command", "uname -a")
        self.assertEqual(code, 0, data)
        self.assertEqual([c["command"] for c in data["commands"]], ["echo hi", "uname -a"])
        self.assertEqual(data["commands"][0]["output"], "hi")
        self.assertIn("armv7l", data["commands"][1]["output"])
        self.assertEqual(self.device.executed, ["echo hi", "uname -a", "exit"])
        # 設定ファイルは書き換えない
        with open(self.config_path, encoding="utf-8") as f:
            self.assertIn('commands: ["run-app /opt/x"]', f.read())

    def test_command_option_works_with_empty_commands_in_config(self):
        port = self.start_device(login=False)
        self.write_config(port=port)
        code, data = self.capture("run", "--command", "echo hi")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "hi")

    def test_ansi_escapes_are_removed_from_output(self):
        port = self.start_device(login=True, ansi=True)
        self.write_config(port=port, login_password="root", commands=["color", "run-app /opt/x"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "[DBG] AVG=3\nOK")
        self.assertEqual(data["commands"][1]["output"], "[DBG] AVG=3")
        for c in data["commands"]:
            self.assertNotIn("\x1b", c["output"])
            self.assertNotIn("\r", c["output"])
        log = self.read_log(data)
        self.assertIn("\x1b[1;31m", log)  # ログには届いたままが残る
        self.assertIn("\x1b[?2004h", log)

    def test_custom_prompt_regex(self):
        port = self.start_device(login=False)
        self.write_config(port=port, prompt="sensor-board:~# $", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["commands"][0]["output"], "hi")

    def test_password_from_environment_variable_is_never_shown(self):
        secret = "s3cret-PW-7781"
        port = self.start_device(login=True, password=secret)
        self.write_config(port=port, login_password="", login_password_env="EDL_TEST_SERIAL_PW",
                          commands=["echo hi"])
        code, data = self.capture("run", env={"EDL_TEST_SERIAL_PW": secret})
        self.assertEqual(code, 0, data)
        self.assertEqual(self.device.login_attempts, [("root", True)])
        self.assertNotIn(secret, json.dumps(data, ensure_ascii=False))
        self.assertNotIn(secret, self.stderr)
        self.assertNotIn(secret, self.read_log(data))

    def test_environment_variable_wins_over_password(self):
        port = self.start_device(login=True, password="from-env")
        self.write_config(port=port, login_password="from-file", login_password_env="EDL_TEST_SERIAL_PW",
                          commands=["echo hi"])
        code, data = self.capture("run", env={"EDL_TEST_SERIAL_PW": "from-env"})
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


# ---------------------------------------------------------------------------
# run：時間切れ、応答なし、ポートの問題
# ---------------------------------------------------------------------------

class RunFailures(CaptureTestCase):

    def test_command_timeout_then_ctrl_c(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", command_timeout_seconds=1,
                          commands=["run-app /opt/hang", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        first, second = data["commands"]
        self.assertTrue(first["timed_out"])
        self.assertIn("[DBG] start", first["output"])
        self.assertNotIn("run-app /opt/hang", first["output"])
        self.assertFalse(second["timed_out"])
        self.assertEqual(second["output"], "hi")  # Ctrl-C の後も続けられた
        self.assertEqual(self.device.interrupts, 1)
        self.assertIn("run-app /opt/hang", data["message"])
        self.assertIn("Ctrl-C", data["message"])
        self.assertEqual(self.device.exits, 1)  # ログアウトもした

    def test_prompt_never_comes_back(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="root", command_timeout_seconds=1,
                          commands=["run-app /opt/stuck", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(len(data["commands"]), 1)
        self.assertTrue(data["commands"][0]["timed_out"])
        self.assertEqual(data["not_run"], ["echo hi"])
        self.assertIn("プロンプトが戻りませんでした", data["message"])
        self.assertNotIn("echo hi", self.device.executed)
        self.assertEqual(self.device.exits, 0)

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
        self.write_config(port="/dev/ttyEDL-test-missing", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        self.assertIn("/dev/ttyEDL-test-missing", data["message"])
        self.assertIn("見つかりません", data["message"])
        self.assertIn("USB ケーブル", data["message"])
        self.assertIn("available_ports", data)
        self.assertLess(self.elapsed, 5)

    def test_port_written_without_dev(self):
        self.write_config(port="ttyEDL-test-missing", commands=["echo hi"])
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

    def test_port_used_by_another_program(self):
        port = self.start_device(login=False)
        self.write_config(port=port, commands=["echo hi"])
        holder = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("ほかのプログラム", data["message"])
        self.assertEqual(self.device.executed, [])

    def test_usb_pulled_during_command(self):
        port = self.start_device(login=False)
        self.write_config(port=port, commands=["pull-usb", "echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 1, data)
        self.assertIn("途中で切れました", data["message"])
        self.assertEqual(len(data["commands"]), 1)
        self.assertTrue(data["commands"][0]["interrupted"])
        self.assertIn("bye", data["commands"][0]["output"])


# ---------------------------------------------------------------------------
# run：記録するだけ（listen）
# ---------------------------------------------------------------------------

class RunListen(CaptureTestCase):

    def test_listen_only_option(self):
        port = self.start_device(login=True, ticker=0.2)
        self.write_config(port=port, login_password="root", commands=["run-app /opt/x"])
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

    def test_config_option_points_to_another_file(self):
        other = os.path.join(self.tmp, "sub", "my.yaml")
        code, data = self.capture("config-path", "--config", other)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["config"], other)
        self.assertTrue(os.path.isfile(other))

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
        self.write_config(port="/dev/ttyUSB0", login_password="", login_password_env="EDL_TEST_NOT_SET_X",
                          commands=["echo hi"])
        env = {k: v for k, v in os.environ.items() if k != "EDL_TEST_NOT_SET_X"}
        code, data = self.capture("run", env=env)
        self.assertEqual(code, 2, data)
        self.assertIn("EDL_TEST_NOT_SET_X", data["problems"][0])

    def test_login_required_but_user_empty(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_user="", commands=["echo hi"])
        code, data = self.capture("run")
        self.assertEqual(code, 2, data)
        self.assertIn("login_user", data["message"])
        self.assertEqual(self.device.login_attempts, [])

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
        self.write_config(port=port, login_password="root", commands=["run-app /opt/x"])
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
        self.write_config(port="/dev/ttyEDL-test-missing")
        code, data = self.capture("check")
        self.assertEqual(code, 2, data)
        self.assertIn("見つかりません", data["problems"][0])

    def test_check_wrong_password(self):
        port = self.start_device(login=True)
        self.write_config(port=port, login_password="wrong")
        code, data = self.capture("check")
        self.assertEqual(code, 2, data)
        self.assertIn("ログインできませんでした", data["problems"][0])


# ---------------------------------------------------------------------------
# 部品ごとの試験（スクリプトを読み込んで呼ぶ）
# ---------------------------------------------------------------------------

class Parts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_clean_removes_escapes_and_cr(self):
        raw = "\x1b[?2004h\x1b[01;32mroot@b\x1b[00m:~# \x1b]0;title\x07ab\bc\r\nx\x1b(B\x1b=y"
        self.assertEqual(self.m.clean(raw), "root@b:~# ac\nxy")

    def test_remove_echo(self):
        f = self.m.remove_echo
        self.assertEqual(f(["ls -l", "a", "b"], "ls -l"), ["a", "b"])
        self.assertEqual(f(["run-app /opt/sen ", "sor_app", "out"], "run-app /opt/sensor_app"), ["out"])
        self.assertEqual(f(["out"], "ls"), ["out"])  # 折り返しがないときは、そのまま

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
                "  - run-app /opt/x\n"
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
        self.assertEqual(cfg["commands"], ["run-app /opt/x", 'echo "a"'])
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

    def test_permission_denied_message(self):
        with mock.patch.object(self.m.os, "open", side_effect=PermissionError(errno.EACCES, "x")):
            with self.assertRaises(self.m.Failure) as cm:
                self.m.open_port("/dev/ttyUSB0", 115200)
        self.assertIn("権限がありません", cm.exception.message)
        self.assertIn("dialout", cm.exception.message)

    def test_busy_message(self):
        with mock.patch.object(self.m.os, "open", side_effect=OSError(errno.EBUSY, "busy")):
            with self.assertRaises(self.m.Failure) as cm:
                self.m.open_port("/dev/ttyUSB0", 115200)
        self.assertIn("ほかのプログラム", cm.exception.message)

    def test_validate_resolves_password_without_leaking(self):
        cfg = self.m.read_yaml(TEMPLATE)
        cfg.update({"port": "/dev/ttyUSB0", "login_password": "pw1"})
        s, problems = self.m.validate(cfg)
        self.assertEqual(problems, [])
        self.assertEqual(s.password, "pw1")
        self.assertEqual(self.m.summary_of(cfg)["login_password"], "＊＊＊")


# ---------------------------------------------------------------------------
# 独り立ち：ほかの部品に頼らないこと
# ---------------------------------------------------------------------------

FORBIDDEN_WORDS = [
    "setup", "セットアップ", "cross-compile", "deploy", "デプロイ",
    "device-worker", "orchestrator", "verifier",
    "作業エージェント", "統括", "検証エージェント", "Web管理画面", "Web 管理画面",
    "extension", "エクステンション", "エージェント", "agent",
]

STDLIB_ALLOWED = {
    "argparse", "codecs", "datetime", "errno", "fcntl", "glob", "json", "os", "re",
    "select", "shutil", "struct", "sys", "termios", "time", "traceback",
}


class Independence(unittest.TestCase):

    def skill_files(self):
        for folder, _, files in os.walk(SKILL_DIR):
            if "__pycache__" in folder:
                continue
            for name in files:
                yield os.path.join(folder, name)

    def test_no_words_about_other_parts(self):
        for path in self.skill_files():
            with open(path, encoding="utf-8") as f:
                text = f.read().lower()
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(word.lower(), text, "%s に「%s」があります" % (path, word))

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
        self.assertTrue(names <= STDLIB_ALLOWED, names - STDLIB_ALLOWED)
        self.assertNotIn("sys.path", source)
        self.assertNotIn("../", source)

    def test_skill_files_are_only_its_own(self):
        rel = sorted(os.path.relpath(p, SKILL_DIR) for p in self.skill_files())
        self.assertEqual(rel, ["SKILL.md", "config-template.yaml", os.path.join("scripts", "capture.py")])

    def test_skill_md_frontmatter(self):
        with open(SKILL_MD, encoding="utf-8") as f:
            text = f.read()
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m)
        front = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
        self.assertEqual(front["name"].strip(), "log-capture")
        self.assertIn("/embedded-debug-loop:log-capture", front["description"])
        for sub in ("capture.py run", "capture.py config-path", "capture.py check"):
            self.assertIn(sub, text)

    def test_template_comments(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertTrue(lines[0].startswith("# log-capture"))
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


if __name__ == "__main__":
    unittest.main()
