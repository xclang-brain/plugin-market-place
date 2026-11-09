"""serial-console のテスト。

このスキルのフォルダで動かす：python3 -m unittest discover -s tests（Windows は py -3）
機器の代わりに、tests/fake_device.py の機器のまねを TCP（socket://127.0.0.1:<番号>）で動かす。
ほかに入れるものはなく、外のネットワークも使わない。
bash と PowerShell から --stdin で渡すテストは、そのシェルがないときは飛ばす。
"""

import configparser
import contextlib
import errno
import importlib.util
import io
import json
import os
import re
import shlex
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


def find_bash():
    """bash の場所。Windows では Git Bash（WSL の bash.exe は使わない）。"""
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    bash = git and os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe")
    return bash if bash and os.path.isfile(bash) else None


def run_main(*args):
    """スクリプトの main を、このプロセスの中で動かす。(終了コード, 標準出力) を返す。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = serial_console.main(list(args))
    return code, out.getvalue()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self):
        workdir = tempfile.mkdtemp(prefix="serial-console-test-")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.use_workdir(workdir)

    def use_workdir(self, workdir):
        self.workdir = workdir
        self.data = os.path.join(workdir, "." + SKILL_NAME)
        self.config = os.path.join(self.data, "config.ini")

    def device(self, **kw):
        kw.setdefault("password", PASSWORD)
        device = FakeDevice(**kw)
        self.addCleanup(device.close)
        return device

    def script(self, *args, env=None, script=SCRIPT, stdin=b""):
        environ = dict(os.environ)
        environ.pop(ENV_NAME, None)
        environ.update(env or {})
        proc = subprocess.run([sys.executable, script] + list(args) + ["--workdir", self.workdir],
                              input=stdin, capture_output=True, env=environ, timeout=120)
        return self.parse(proc, stderr=True)

    def parse(self, proc, stderr=False):
        """結果が ASCII の JSON 1つであることを確かめ、(終了コード, 結果) を返す。"""
        self.assertTrue(all(b < 128 for b in proc.stdout), "結果の JSON に ASCII でない文字があります")
        if stderr:
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
        for kill in (True, False):  # Ctrl-U で消せないときは、Password: で Enter を送って取り消す
            with self.subTest(kill=kill):
                self.configure(self.device(typed="ro", kill=kill))
                code, result = self.script("check")
                self.assertEqual(code, 0, result["message"])

    def test_half_typed_command_is_not_run(self):
        self.configure(self.device(state="shell", typed="echo LEFTOVER"))
        code, result = self.script("run", "--command", "true")
        self.assertEqual(code, 0, result["message"])
        self.assertNotIn("LEFTOVER", self.log_text(result))

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

    def test_bad_arguments(self):
        for args in (("check", "--command", "uname"), ("init", "--stdin"), ("bogus",),
                     ("run", "--stdin", "--command", "uname"), ("run", "--command", "echo a\necho b")):
            with self.subTest(args=args):
                code, result = self.script(*args)
                self.assertEqual(code, 2)
                self.assertTrue(result["problems"])
        self.assertEqual(self.files(), [])
        self.use_workdir(os.path.join(self.workdir, "nope"))
        code, result = self.script("check")
        self.assertEqual(code, 2)
        self.assertIn("作業フォルダ", result["message"])

    def test_config_errors_do_not_show_the_line(self):
        os.makedirs(self.data)
        cases = [  # BOM があっても読める。パスワードの行かもしれないので、誤りの行の中身は出さない
            ("\ufeff[settings]\nport = x\nlogin_password = hunter2\nhunter3\n".encode("utf-8"), "4 行目"),
            ("[settings]\nport = \u3042\n".encode("cp932"), "UTF-8"),
            (b"port = x\n", "[settings]"),
            (b"[other]\nport = x\n", "[settings]"),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected):
                with open(self.config, "wb") as fh:
                    fh.write(data)
                code, result = self.script("check")
                self.assertEqual(code, 2)
                self.assertIn(expected, result["message"])
                self.assertNotIn("hunter", json.dumps(result))

    def test_empty_environment_variable(self):
        self.configure(self.device(), login_password="", login_password_env=ENV_NAME)
        code, result = self.script("check", env={ENV_NAME: ""})
        self.assertEqual(code, 1)
        self.assertIn("環境変数 %s が空です" % ENV_NAME, result["message"])

    def test_unexpected_error_still_gives_one_json(self):
        saved = serial_console.cmd_init
        serial_console.cmd_init = lambda *args: 1 / 0
        self.addCleanup(setattr, serial_console, "cmd_init", saved)
        code, out = run_main("init", "--workdir", self.workdir)
        self.assertEqual(code, 1)
        self.assertEqual(out.count("\n"), 1)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertIn("ZeroDivisionError", result["message"])

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

    def test_output_without_newline_before_the_prompt(self):
        self.configure(self.device(state="shell"), commands="printf 42\necho next")
        code, result = self.script("run")
        self.assertEqual(code, 0, result["message"])
        self.assertEqual([c["output"] for c in result["commands"]], ["42", "next"])

    def test_long_line_is_cut_and_password_is_masked_across_log_pieces(self):
        self.configure(self.device())
        code, result = self.script("run", "--command", "echo " + PASSWORD * 600,  # ログでは 4,096 字ごとに分けて書く
                                   "--command", "echo " + "y" * 9000)
        self.assertEqual(code, 0, result["message"])
        masked, long = result["commands"]
        self.assertEqual((masked["output"], masked["truncated"]), (serial_console.MASK * 600, False))
        self.assertEqual((long["output"], long["truncated"]), ("y" * serial_console.OUTPUT_CHARS, True))
        pieces = "".join(re.findall(r"(?m)^\S+ \S+ \| (.*)$", self.log_text(result)))
        self.assertNotIn(PASSWORD, pieces)

    def test_exit_code_unreadable(self):
        self.configure(self.device(state="shell", no_rc=True))
        code, result = self.script("run", "--command", "true")
        self.assertEqual(code, 0, result["message"])
        self.assertIsNone(result["commands"][0]["exit_code"])
        self.assertIn("終了コードを読めなかった", result["message"])

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

    def test_prompt_does_not_come_back_after_ctrl_c(self):
        self.configure(self.device(state="shell"), command_timeout_seconds="1")
        code, result = self.script("run", "--command", "stuck")
        self.assertEqual(code, 1)
        self.assertIn("Ctrl-C のあとも", result["message"])

    def test_short_prompt_is_not_taken_for_a_hidden_one(self):
        # プロンプトが「# 」だと、「# 」で始まる出力の行を、隠れたプロンプトと取り違えやすい
        device = self.device(state="shell", prompt="# ")
        self.configure(device)
        code, result = self.script("run", "--command", "tap")
        self.assertEqual(code, 0, result["message"])
        self.assertEqual(result["commands"][0]["output"], "# Subtest: sensor\nok")
        self.assertEqual(bytes(device.received),  # 動いている間に Enter を送っていない
                         ("\x15\rtap\r%s\r" % serial_console.RC_COMMAND).encode("ascii"))

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

    def test_password_from_environment_is_masked(self):
        # 設定に残った古いパスワードが今のパスワードの一部でも、今のパスワードを残さず伏せる
        self.configure(self.device(), login_password=PASSWORD[:4], login_password_env=ENV_NAME)
        code, result = self.script("run", "--command", "echo " + PASSWORD, env={ENV_NAME: PASSWORD})
        self.assertEqual(code, 0, result["message"])
        self.assertEqual(result["commands"][0]["output"], serial_console.MASK)
        for text in (json.dumps(result), self.log_text(result)):
            self.assertNotIn(PASSWORD[4:], text)

    def test_paths_are_not_masked(self):
        self.use_workdir(os.path.join(self.workdir, PASSWORD))  # 作業フォルダの名前に、パスワードと同じ文字がある
        os.mkdir(self.workdir)
        self.configure(self.device(state="shell"))
        code, result = self.script("run", "--command", "true")
        self.assertEqual(code, 0, result["message"])
        self.assertTrue(os.path.isfile(result["config"]))
        self.assertTrue(os.path.isfile(result["log"]))

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


class StdinTest(Base):
    """--stdin なら、" や / で始まるコマンドも、そのまま機器に届く。"""

    COMMANDS = ["echo \"a b\" 'c'", "/opt/app/my_app --selftest", "echo 日本語"]
    OUTPUTS = ["a b c", "sh: /opt/app/my_app: not found", "日本語"]

    def setUp(self):
        Base.setUp(self)
        self.dev = self.device(state="shell")
        self.configure(self.dev, commands="echo configured")

    def check_result(self, code, result):
        self.assertEqual(code, 0, result["message"])
        self.assertEqual([c["output"] for c in result["commands"]], self.OUTPUTS)
        for command in self.COMMANDS:
            self.assertIn((command + "\r").encode("utf-8"), self.dev.received)
        self.assertNotIn(b"configured", self.dev.received)

    def test_stdin(self):
        data = ("\ufeff" + "\r\n\r\n".join(self.COMMANDS) + "\r\n").encode("utf-8")  # BOM と空の行があってもよい
        self.check_result(*self.script("run", "--stdin", stdin=data))
        code, result = self.script("run", "--stdin", stdin=b"\n")
        self.assertEqual(code, 2)
        self.assertIn("--stdin", result["message"])
        code, result = self.script("run", "--stdin", stdin="echo \u3042".encode("cp932"))
        self.assertEqual(code, 2)
        self.assertIn("UTF-8", result["message"])

    def shell_file(self, name, text, encoding="utf-8"):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        return path

    def test_from_bash(self):
        bash = find_bash()
        if not bash:
            self.skipTest("bash が見つからない")
        call = " ".join(shlex.quote(p.replace("\\", "/")) for p in (sys.executable, SCRIPT))
        call += " run %s --workdir " + shlex.quote(self.workdir.replace("\\", "/"))
        heredoc = call % "--stdin" + " <<'END'\n" + "\n".join(self.COMMANDS) + "\nEND\n"
        path = self.shell_file("stdin.sh", heredoc)
        self.check_result(*self.parse(subprocess.run([bash, path], capture_output=True, timeout=120)))
        if os.name == "nt":  # Git Bash は / で始まる引数を Windows のパスに書き換える。それを見つけて止める
            path = self.shell_file("command.sh", call % "--command /opt/app/my_app" + "\n")
            code, result = self.parse(subprocess.run([bash, path], capture_output=True, timeout=120))
            self.assertEqual(code, 2)
            self.assertIn("--stdin", result["message"])

    def test_from_powershell(self):
        shells = [s for s in (shutil.which("powershell"), shutil.which("pwsh")) if s]
        if not shells:
            self.skipTest("PowerShell が見つからない")
        quote = lambda p: "'%s'" % p.replace("'", "''")
        text = ("$OutputEncoding = [Text.UTF8Encoding]::new($false)\n@'\n" + "\n".join(self.COMMANDS) + "\n'@ | & "
                + " ".join(quote(p) for p in (sys.executable, SCRIPT)) + " run --stdin --workdir "
                + quote(self.workdir) + "\nexit $LASTEXITCODE\n")
        path = self.shell_file("stdin.ps1", text, "utf-8-sig")  # Windows PowerShell 5.1 は BOM がないと UTF-8 と読まない
        for shell in shells:
            with self.subTest(shell=shell):
                self.dev.received.clear()
                proc = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                                      capture_output=True, timeout=120)
                self.check_result(*self.parse(proc))

    def test_windows_path_made_by_git_bash_is_refused(self):
        saved = serial_console.IS_WINDOWS
        serial_console.IS_WINDOWS = True
        self.addCleanup(setattr, serial_console, "IS_WINDOWS", saved)
        for command in ("C:/Program Files/Git/opt/app/my_app --selftest", "LD_LIBRARY_PATH=C:/Program Files/Git/opt"):
            code, out = run_main("run", "--command", command, "--workdir", self.workdir)
            self.assertEqual(code, 2)
            self.assertIn("--stdin", json.loads(out)["message"])
        code, out = run_main("run", "--command", "wget http://host/x", "--workdir", os.path.join(self.workdir, "nope"))
        self.assertNotIn("--stdin", json.loads(out)["message"])  # 止めたのは、作業フォルダがないため


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
        command = "echo abcdefghijklmnop"
        drop = serial_console.drop_echo
        self.assertEqual(drop(["echo abcdefgh", "ijklmnop", "out"], command), ["out"])  # 2行に折り返した
        self.assertEqual(drop(["echo abcdefghhijklmnop", "out"], command), ["out"])  # 「h\rh」で h が2回出た
        self.assertEqual(drop(["echo abcdefgh", "out"], command), ["echo abcdefgh", "out"])
        self.assertEqual(drop(["out"], "uname"), ["out"])


if __name__ == "__main__":
    unittest.main()
