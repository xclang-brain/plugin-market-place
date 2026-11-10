# -*- coding: utf-8 -*-
"""cross-build のスクリプトのテスト。スキルのフォルダだけをコピーしても動く。

偽のビルドのコマンド（fake_build.py）を、既定のシェル（Windows は cmd、ほかは bash）で動かす。
"""

import ctypes
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(TESTS)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "cross_build.py")
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")
FAKE = os.path.join(TESTS, "fake_build.py")
IS_WINDOWS = os.name == "nt"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

sys.dont_write_bytecode = True  # スキルの scripts/ に __pycache__ を作らない
_spec = importlib.util.spec_from_file_location("cross_build_script", SCRIPT)
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def fake(*args):
    """偽のビルドのコマンド。既定のシェルで動く形に引用符を付ける。"""
    words = [sys.executable, FAKE] + list(args)
    return subprocess.list2cmdline(words) if IS_WINDOWS else " ".join(shlex.quote(w) for w in words)


def ps_fake(*args):
    """PowerShell で動く形の、偽のビルドのコマンド。"""
    return "& " + " ".join("'" + w.replace("'", "''") + "'" for w in [sys.executable, FAKE] + list(args))


def pid_alive(pid):
    """プロセスが動いているか（終わった後のゾンビは、動いていないとみなす）。"""
    if IS_WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    if os.path.isdir("/proc"):
        try:
            with open("/proc/{}/stat".format(pid)) as f:
                return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def wait_until_gone(pids, seconds=10):
    end = time.time() + seconds
    while time.time() < end and any(pid_alive(p) for p in pids):
        time.sleep(0.2)
    return [p for p in pids if pid_alive(p)]


def tree(folder):
    return sorted(os.path.relpath(os.path.join(d, n), folder)
                  for d, dirs, files in os.walk(folder) for n in dirs + files)


def quoted(value):
    """YAML の '…' の形にする。コマンドに記号や「: 」があっても、そのまま読める。"""
    return "'" + str(value).replace("'", "''") + "'"


class Project:
    """一時的な作業フォルダ。"""

    def __init__(self, test, script=SCRIPT):
        self.dir = tempfile.mkdtemp(prefix="cross-build-test-")
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.script = script
        self.data = os.path.join(self.dir, "." + os.path.basename(os.path.dirname(os.path.dirname(script))))
        self.config = os.path.join(self.data, "config.yaml")

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def configure(self, text=None, **values):
        """設定ファイルを書く（.gitignore は作らない）。値は、どれも '…' で囲む。リストは「- 値」の行にする。"""
        if text is None:
            lines = []
            for key, value in values.items():
                if isinstance(value, list):
                    lines += [key + ":"] + ["  - " + quoted(v) for v in value]
                else:
                    lines.append("{}: {}".format(key, quoted(value)))
            text = "\n".join(lines) + "\n"
        os.makedirs(self.data, exist_ok=True)
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(text)

    def run(self, *args):
        r = subprocess.run([sys.executable, self.script] + list(args) + ["--workdir", self.dir],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=120)
        if any(b > 127 for b in r.stdout):
            raise AssertionError("標準出力が ASCII だけではありません：{!r}".format(r.stdout))
        try:
            data = json.loads(r.stdout.decode("ascii"))
        except ValueError:
            raise AssertionError("標準出力が JSON 1つではありません：{!r} {!r}".format(r.stdout, r.stderr))
        for key in ("ok", "message", "problems", "config", "log"):
            if key not in data:
                raise AssertionError("{} がありません：{}".format(key, data))
        return r.returncode, data


class TemplateTest(unittest.TestCase):

    def setUp(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            self.lines = f.read().splitlines()

    def comments_above(self, key):
        i = next(n for n, line in enumerate(self.lines) if line.startswith(key + ":"))
        block = []
        while i > 0 and self.lines[i - 1].startswith("#"):
            i -= 1
            block.insert(0, self.lines[i])
        return block

    def test_every_key_has_a_comment_right_above(self):
        keys = [line.split(":")[0] for line in self.lines if line and line[0].isalpha()]
        self.assertEqual(tuple(keys), build.KEYS)
        for key in keys:
            self.assertTrue(self.comments_above(key), key)

    def test_marks_required_keys_and_choices(self):
        for key in build.KEYS:
            text = "\n".join(self.comments_above(key))
            self.assertEqual("（必須）" in text, key in ("build_command", "outputs"), key)
            self.assertNotIn("（秘密）", text)
        self.assertIn("# 選べる値：auto / bash / cmd / powershell", self.comments_above("shell"))

    def test_reads_as_settings(self):
        values = {key: value for key, value, _, _ in build.read_yaml(self.lines)}
        self.assertEqual(tuple(values), build.KEYS)
        self.assertEqual((values["source_dir"], values["shell"], values["timeout_seconds"]), (".", "auto", "480"))
        self.assertEqual((values["sdk_env"], values["build_command"], values["outputs"]), ("", "", ""))


class InitTest(unittest.TestCase):

    def test_creates_gitignore_and_config_from_template(self):
        p = Project(self)
        code, data = p.run("init")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["created"])
        self.assertEqual(data["config"], p.config)
        with open(os.path.join(p.data, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")
        with open(p.config, "rb") as a, open(TEMPLATE, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_keeps_an_existing_config(self):
        p = Project(self)
        p.configure(build_command="make")
        code, data = p.run("init")
        self.assertEqual(code, 0, data)
        self.assertFalse(data["created"])
        with open(p.config, encoding="utf-8") as f:
            self.assertEqual(f.read(), "build_command: 'make'\n")

    def test_missing_workdir(self):
        p = Project(self)
        r = subprocess.run([sys.executable, SCRIPT, "init", "--workdir", p.path("nope")],
                           stdout=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(p.path("nope")))

    def test_renamed_copy_uses_its_folder_name(self):
        base = tempfile.mkdtemp(prefix="cross-build-copy-")
        self.addCleanup(shutil.rmtree, base, True)
        copy = os.path.join(base, "my-build")
        shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
        p = Project(self, os.path.join(copy, "scripts", "cross_build.py"))
        code, data = p.run("init")
        self.assertEqual(code, 0, data)
        self.assertTrue(os.path.isfile(p.path(".my-build", "config.yaml")))
        self.assertFalse(os.path.exists(p.path(".cross-build")))


class ArgumentTest(unittest.TestCase):

    def test_bad_arguments(self):
        p = Project(self)
        for args in ([], ["build"], ["run", "--workdir"]):
            r = subprocess.run([sys.executable, SCRIPT] + args, stdout=subprocess.PIPE, timeout=60, cwd=p.dir)
            self.assertEqual(r.returncode, 2, args)
            data = json.loads(r.stdout.decode("ascii"))
            self.assertFalse(data["ok"])
            self.assertTrue(data["problems"])
        self.assertEqual(tree(p.dir), [])


class CheckTest(unittest.TestCase):

    def test_missing_config_creates_nothing(self):
        p = Project(self)
        code, data = p.run("check")
        self.assertEqual(code, 2, data)
        self.assertIn("init", data["message"])
        self.assertEqual(tree(p.dir), [])

    def test_template_needs_required_keys(self):
        p = Project(self)
        p.run("init")
        code, data = p.run("check")
        self.assertEqual(code, 2, data)
        self.assertEqual(len(data["problems"]), 2, data)
        self.assertIn("build_command", data["problems"][0])
        self.assertIn("outputs", data["problems"][1])

    def test_valid_config_creates_nothing(self):
        p = Project(self)
        p.configure(build_command=fake("write", "app"), outputs=["app", "app.map"])
        before = tree(p.dir)
        code, data = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["settings"]["outputs"], ["app", "app.map"])
        self.assertEqual(tree(p.dir), before)

    def test_reports_missing_folder_and_sdk_env(self):
        p = Project(self)
        p.configure(source_dir="nope", sdk_env="nope.sh", build_command="make", outputs="app")
        code, data = p.run("check")
        self.assertEqual(code, 2, data)
        text = "".join(data["problems"])
        self.assertIn("source_dir", text)
        self.assertIn("sdk_env", text)

    def test_reports_unknown_key_shell_and_timeout(self):
        p = Project(self)
        p.configure(build_comand="make", build_command="make", outputs="app", shell="zsh",
                    timeout_seconds="abc")
        code, data = p.run("check")
        self.assertEqual(code, 2, data)
        text = "".join(data["problems"])
        for word in ("build_comand", "zsh", "timeout_seconds"):
            self.assertIn(word, text)

    def test_command_not_on_path_is_a_note(self):
        p = Project(self)
        p.configure(build_command="no-such-command-xyz all", outputs="app")
        code, data = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertIn("no-such-command-xyz", data["notes"][0])
        self.assertIn("no-such-command-xyz", data["message"])

    def test_variables_and_paths_in_the_command(self):
        p = Project(self)
        os.mkdir(p.path("tools"))
        name = "mk.bat" if IS_WINDOWS else "mk"
        with open(p.path("tools", name), "w") as f:
            f.write("")
        os.chmod(p.path("tools", name), 0o755)
        s = {"shell": "cmd" if IS_WINDOWS else "bash", "source_dir": p.dir}
        self.assertTrue(build.command_exists("tools/mk", s))  # Windows では .bat を補う
        self.assertFalse(build.command_exists("tools/none", s))
        self.assertTrue(build.command_exists("$NO_SUCH_VAR_XYZ/mk", s))  # 今は決まらない変数は調べない
        with mock.patch.dict(os.environ, {"MY_TOOLS_XYZ": p.path("tools")}):
            self.assertTrue(build.command_exists("$MY_TOOLS_XYZ/mk", s))
            self.assertFalse(build.command_exists("$MY_TOOLS_XYZ/none", s))

    def load(self, p, shell_program="shell", windows=IS_WINDOWS):
        """設定を、このプロセスの中で読む（見つかるシェルと OS を決めて）。"""
        with mock.patch.object(build, "find_shell", return_value=shell_program), \
                mock.patch.object(build, "IS_WINDOWS", windows):
            return build.load(p.config, p.dir)

    def test_sdk_env_must_match_the_shell(self):
        p = Project(self)
        with open(p.path("sdk.sh"), "w") as f:
            f.write("")
        p.configure(shell="powershell", sdk_env="sdk.sh", build_command="make", outputs="app")
        problems = self.load(p)[1]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(".ps1", problems[0])

    def test_cmd_cannot_use_a_network_folder(self):
        p = Project(self)
        p.configure(source_dir="//server/share/fw", build_command="make", outputs="app")
        problems = self.load(p, windows=True)[1]
        self.assertTrue(any("ネットワークのフォルダ" in x for x in problems), problems)

    def test_unreadable_config(self):
        p = Project(self)
        for text in ("[settings]\nbuild_command = make\n", "  build_command: make\n", "a: 1\na: 2\n",
                     "build_command: [make]\n", "outputs: app\n  - app.map\n"):
            p.configure(text)
            code, data = p.run("check")
            self.assertEqual(code, 2, (text, data))
            self.assertIn("設定ファイル", data["message"])

    def test_reads_bom_and_percent(self):
        p = Project(self)
        p.configure("\ufeffbuild_command: {}\noutputs: app\n".format(quoted(fake("say") + " 100%")))
        code, data = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["settings"]["build_command"].endswith("100%"))

    @unittest.skipIf(IS_WINDOWS, "Windows では cmd を使える")
    def test_cmd_only_on_windows(self):
        p = Project(self)
        p.configure(shell="cmd", build_command="make", outputs="app")
        code, data = p.run("check")
        self.assertEqual(code, 2, data)
        self.assertIn("Windows", data["message"])


class FirstWordTest(unittest.TestCase):

    def test_first_word(self):
        self.assertEqual(build.first_word("CC=gcc make -j4", "bash"), "make")
        self.assertEqual(build.first_word("(cd fw && make)", "bash"), "cd")
        self.assertEqual(build.first_word('"C:\\Program Files\\make.exe" all', "cmd"), "C:\\Program Files\\make.exe")
        self.assertEqual(build.first_word("@mingw32-make", "cmd"), "mingw32-make")
        self.assertEqual(build.first_word("& 'C:\\tools\\make.exe' all", "powershell"), "C:\\tools\\make.exe")
        with self.assertRaises(ValueError):
            build.first_word("make 'all", "bash")


class FindShellTest(unittest.TestCase):

    def test_windows_bash_skips_the_wsl_launchers(self):
        root = tempfile.mkdtemp(prefix="cross-build-shell-")
        self.addCleanup(shutil.rmtree, root, True)
        places = [os.path.join(root, *parts) for parts in
                  (("Windows", "System32"), ("Microsoft", "WindowsApps"), ("Git", "bin"))]
        for place in places:
            os.makedirs(place)
            with open(os.path.join(place, "bash.exe"), "w") as f:
                f.write("")
        env = {"PATH": os.pathsep.join(places), "SystemRoot": os.path.join(root, "Windows")}
        with mock.patch.object(build, "IS_WINDOWS", True), mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(build.find_shell("bash"), os.path.join(places[2], "bash.exe"))
            os.environ["PATH"] = os.pathsep.join(places[:2])
            self.assertIsNone(build.find_shell("bash"))


class ScanTest(unittest.TestCase):

    def scan(self, *lines):
        scanner = build.Scanner(io.StringIO())
        for line in lines:
            scanner.add(line)
        return scanner

    def test_error_and_warning_lines_of_many_compilers(self):
        errors = ["main.c:3:5: error: 'x' undeclared", "main.c(3): error C2065: 'x': undeclared identifier",
                  'Error[Pe020]: identifier "x" is undefined',
                  '"../main.c", line 12: error #20: identifier "foo" is undefined',
                  "main.o: undefined reference to `foo'"]
        warnings = ["main.c:1:1: warning: unused variable", '"../main.c", line 3: warning #225-D: declared implicitly']
        others = ["gcc -Werror -c error.c -o error.o", "gcc -Wno-error=unused -c src/error/x.c", "0 Error(s)"]
        s = self.scan(*errors + warnings + others)
        self.assertEqual(s.errors, errors)
        self.assertEqual(s.warnings, warnings)


class ShellCommandTest(unittest.TestCase):

    def test_cmd_does_not_run_the_build_when_sdk_env_fails(self):
        s = {"shell": "cmd", "shell_program": r"C:\Windows\System32\cmd.exe", "source_dir": r"C:\fw",
             "sdk_env": r"C:\ti\setenv.bat", "build_command": "gmake clean & gmake all"}
        argv = build.shell_command(s, tempfile.gettempdir(), "loaded")[0]
        self.assertTrue(argv.endswith('&& (gmake clean & gmake all)"'), argv)

    @unittest.skipUnless(sys.maxsize > 2 ** 32, "64 ビットの大きさを確かめる")
    def test_job_limits_has_the_windows_size(self):
        self.assertEqual(ctypes.sizeof(build.JobLimits), 144)  # 64 ビットの Windows と同じ並び


class DecodeTest(unittest.TestCase):

    def test_line_that_is_not_utf8_uses_the_os_encoding(self):
        with mock.patch.object(build, "FALLBACK", "cp932"):
            self.assertEqual(build.decode("エラー".encode("cp932")), "エラー")
            self.assertEqual(build.decode("エラー".encode("utf-8")), "エラー")

    def test_os_encoding_ignores_utf8_mode(self):
        with mock.patch.object(build, "IS_WINDOWS", False), \
                mock.patch.object(build.locale, "getencoding", create=True, return_value="cp932"), \
                mock.patch.object(build.locale, "getpreferredencoding", return_value="utf-8"):
            self.assertEqual(build.fallback_encoding(), "cp932")

    def test_windows_uses_the_ansi_code_page(self):
        windll = mock.Mock()
        windll.kernel32.GetACP.return_value = 932
        with mock.patch.object(build, "IS_WINDOWS", True), \
                mock.patch.object(build.ctypes, "windll", windll, create=True):
            self.assertEqual(build.fallback_encoding(), "cp932")


class RunTest(unittest.TestCase):

    def test_success_reports_outputs(self):
        p = Project(self)
        p.configure(build_command=fake("elf", "app", "40", "+", "elf", "lib.so", "183", "+", "elf", "pc", "62",
                                       "+", "elf", "mips", "8", "big", "+", "write", "app.hex"),
                    outputs=["app", "lib.so", "pc", "mips", "app.hex"])
        code, data = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["exit_code"], 0)
        self.assertFalse(data["timed_out"])
        self.assertEqual([o["elf_machine"] for o in data["outputs"]], ["ARM", "AArch64", "x86-64", "MIPS", None])
        for o in data["outputs"]:
            self.assertTrue(o["exists"] and o["updated"], o)
        self.assertEqual(data["outputs"][0]["path"], p.path("app"))
        self.assertEqual(data["outputs"][0]["size"], os.path.getsize(p.path("app")))
        self.assertIn("ARM", data["message"])
        with open(os.path.join(p.data, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")
        self.assertEqual(os.path.dirname(data["log"]), os.path.join(p.data, "logs"))
        with open(data["log"], "rb") as f:
            log = f.read().decode("utf-8")
        self.assertIn("wrote app.hex", log)

    def test_unchanged_output_is_not_updated(self):
        p = Project(self)
        p.configure(build_command=fake("once", "app"), outputs="app")
        self.assertTrue(p.run("run")[1]["outputs"][0]["updated"])
        code, data = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertFalse(data["outputs"][0]["updated"])
        self.assertIn("更新されていません", data["message"])

    def test_missing_output(self):
        p = Project(self)
        p.configure(build_command=fake("say", "done"), outputs="build/app")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["outputs"][0]["exists"])
        self.assertIsNone(data["outputs"][0]["size"])
        self.assertIn("build/app", data["message"])

    def test_failure_picks_error_and_warning_lines(self):
        p = Project(self)
        p.configure(build_command=fake("say", "gcc -Werror -c error.c -o error.o",
                                       "+", "say", "main.c:3:5: error: 'x' undeclared",
                                       "+", "say", "    3 |   error: here",
                                       "+", "say", "main.c:1:1: warning: unused variable",
                                       "+", "exit", "2"),
                    outputs="app")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["exit_code"], 2)
        self.assertEqual(data["error_lines"], ["main.c:3:5: error: 'x' undeclared"])
        self.assertEqual(data["warning_lines"], ["main.c:1:1: warning: unused variable"])
        self.assertIn("main.c:3:5: error:", data["message"])
        self.assertEqual(data["tail"][-1], "main.c:1:1: warning: unused variable")

    def test_line_limits(self):
        p = Project(self)
        args = []
        for n in range(25):
            args += ["say", "f.c:{}: error: e{}".format(n, n), "+", "say", "f.c:{}: warning: w{}".format(n, n), "+"]
        p.configure(build_command=fake(*args + ["exit", "1"]), outputs="app")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(len(data["error_lines"]), 20)
        self.assertEqual(len(data["warning_lines"]), 20)
        self.assertEqual(len(data["tail"]), 30)
        self.assertEqual(data["tail"][-1], "f.c:24: warning: w24")

    def test_output_that_is_not_utf8(self):
        p = Project(self)
        p.configure(build_command=fake("bytes", "834783898140", "+", "say", "日本語の行"), outputs="app")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["tail"][-1], "日本語の行")
        with open(data["log"], "rb") as f:
            f.read().decode("utf-8")  # ログはいつも UTF-8

    def test_command_not_found(self):
        p = Project(self)
        p.configure(build_command="no-such-command-xyz all", outputs="app")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("コマンドが見つかりません", data["message"])
        self.assertIn("no-such-command-xyz", data["message"])

    def test_real_error_comes_before_a_missing_command(self):
        p = Project(self)
        p.configure(build_command=fake("say", "/bin/sh: 1: git-not-installed: not found",
                                       "+", "say", "main.c:3:5: error: 'x' undeclared", "+", "exit", "2"),
                    outputs="app")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertTrue(data["message"].startswith("ビルドが失敗しました"), data["message"])
        self.assertIn("main.c:3:5: error:", data["message"])
        self.assertIn("git-not-installed", data["message"])

    def test_other_not_recognized_lines_are_not_a_missing_command(self):
        p = Project(self)
        p.configure(build_command=fake("say", "option --xyz is not recognized", "+", "exit", "1"), outputs="app")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertNotIn("コマンドが見つかりません", data["message"])

    def test_shell_that_cannot_start(self):
        p = Project(self)
        p.configure(build_command="make", outputs="app")
        with mock.patch.object(build, "find_shell", return_value=p.path("no-such-shell")):
            code, data = build.cmd_run(p.dir)
        self.assertEqual(code, 1, data)
        self.assertIsNone(data["exit_code"])
        self.assertIn("起動できませんでした", data["message"])

    def test_second_run_in_the_same_folder_does_not_build(self):
        p = Project(self)
        p.configure(build_command=fake("write", "app"), outputs="app")
        lock = build.lock_run(p.data)
        self.addCleanup(lock.close)
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("ほかの run", data["message"])
        self.assertFalse(os.path.exists(p.path("app")))
        lock.close()
        self.assertEqual(p.run("run")[0], 0)

    def test_timeout_stops_the_whole_tree(self):
        self.check_timeout()

    @unittest.skipIf(IS_WINDOWS, "SIGTERM は POSIX だけ")
    def test_timeout_stops_children_that_ignore_term(self):
        self.check_timeout("ignore-term")

    @unittest.skipIf(IS_WINDOWS, "SIGTERM は POSIX だけ")
    def test_timeout_gives_the_build_time_to_clean_up(self):
        p = Project(self)
        p.configure(build_command=fake("term", p.path("cleaned.txt")), outputs="app", timeout_seconds=2)
        code, data = p.run("run")
        self.assertTrue(data["timed_out"], data)
        self.assertTrue(os.path.exists(p.path("cleaned.txt")), "SIGTERM の後、片付ける前に止められました")

    @unittest.skipIf(IS_WINDOWS, "SIGTERM は POSIX だけ")
    def test_stopping_the_script_stops_the_build(self):
        p = Project(self)
        pids_file = p.path("pids.txt")
        p.configure(build_command=fake("spawn", pids_file), outputs="app", timeout_seconds=100)
        proc = subprocess.Popen([sys.executable, SCRIPT, "run", "--workdir", p.dir], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(proc.kill)
        end = time.time() + 30
        while time.time() < end and not os.path.exists(pids_file):
            time.sleep(0.1)
        time.sleep(0.5)  # 記録ファイルを書き終えるのを待つ
        proc.send_signal(signal.SIGTERM)
        out = proc.communicate(timeout=60)[0]
        data = json.loads(out.decode("ascii"))
        self.assertEqual(proc.returncode, 1, data)
        self.assertIn("途中で止められた", data["message"])
        self.assertTrue(data["log"], data)
        with open(pids_file, encoding="utf-8") as f:
            pids = [int(x) for x in f.read().split()]
        self.assertEqual(wait_until_gone(pids), [], "ビルドの子のプロセスが残っています")

    def test_sdk_env_that_exits_midway(self):
        p = Project(self)
        sdk = self.write_sdk(p, "exit 0\n", "@exit 0\n")
        p.configure(sdk_env=sdk, outputs="app", build_command=fake("write", "app"))
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("途中で", data["message"])
        self.assertFalse(os.path.exists(p.path("app")))

    def check_timeout(self, *spawn_args):
        p = Project(self)
        p.configure(build_command=fake("spawn", p.path("pids.txt"), *spawn_args), outputs="app",
                    timeout_seconds=5)
        start = time.time()
        code, data = p.run("run")
        self.assertLess(time.time() - start, 60)
        self.assertEqual(code, 1, data)
        self.assertTrue(data["timed_out"])
        self.assertIsNone(data["exit_code"])
        self.assertIn("5 秒", data["message"])
        with open(p.path("pids.txt"), encoding="utf-8") as f:
            pids = [int(x) for x in f.read().split()]
        self.assertEqual(len(pids), 2)
        self.assertEqual(wait_until_gone(pids), [], "ビルドの子のプロセスが残っています")

    @unittest.skipIf(IS_WINDOWS, "bash の動きを確かめる")
    def test_does_not_wait_for_a_child_that_keeps_the_output_open(self):
        p = Project(self)
        p.configure(build_command="sleep 30 & " + fake("write", "app"), outputs="app")
        start = time.time()
        code, data = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertLess(time.time() - start, 20)

    def write_sdk(self, p, unix, windows):
        name = "sdk.bat" if IS_WINDOWS else "sdk.sh"
        with open(p.path(name), "w", newline="") as f:
            f.write(windows.replace("\n", "\r\n") if IS_WINDOWS else unix)
        return name

    def test_sdk_env_is_loaded_in_the_same_shell(self):
        p = Project(self)
        os.mkdir(p.path("src"))
        sdk = self.write_sdk(p, "export MY_VAR=hello\ncd /\n", "@set MY_VAR=hello\n@cd /d %TEMP%\n")
        p.configure(source_dir="src", sdk_env=sdk, outputs="app",
                    build_command=fake("env", "MY_VAR", "+", "cwd", "+", "write", "app"))
        code, data = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertIn("MY_VAR=hello", data["tail"])
        cwd = data["tail"][data["tail"].index("MY_VAR=hello") + 1]
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(p.path("src")))

    @unittest.skipIf(IS_WINDOWS, "bash の関数を確かめる")
    def test_functions_from_sdk_env_and_pipefail(self):
        p = Project(self)
        sdk = self.write_sdk(p, "my_build() { " + fake("write", "app") + "; }\n", "")
        p.configure(sdk_env=sdk, outputs="app", build_command="my_build && " + fake("exit", "3") + " | cat")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["exit_code"], 3)
        self.assertTrue(data["outputs"][0]["updated"])

    def test_sdk_env_failure_stops_the_build(self):
        p = Project(self)
        sdk = self.write_sdk(p, "return 3\n", "@exit /b 3\n")
        p.configure(sdk_env=sdk, outputs="app", build_command=fake("write", "app"))
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("sdk_env", data["message"])
        self.assertFalse(os.path.exists(p.path("app")))

    def test_config_problem_does_not_build(self):
        p = Project(self)
        p.configure(build_command=fake("write", "app"))
        code, data = p.run("run")
        self.assertEqual(code, 2, data)
        self.assertIsNone(data["log"])
        self.assertFalse(os.path.exists(p.path("app")))


@unittest.skipUnless(POWERSHELL, "PowerShell（pwsh か powershell）がない")
class PowerShellTest(unittest.TestCase):

    def test_success_and_failure(self):
        p = Project(self)
        p.configure(shell="powershell", outputs="app", build_command=ps_fake("write", "app"))
        code, data = p.run("run")
        self.assertEqual(code, 0, data)
        p.configure(shell="powershell", outputs="app", build_command=ps_fake("exit", "4"))
        code, data = p.run("run")
        self.assertEqual((code, data["exit_code"]), (1, 4), data)

    def test_command_not_found(self):
        p = Project(self)
        p.configure(shell="powershell", outputs="app", build_command="no-such-command-xyz all")
        code, data = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("コマンドが見つかりません", data["message"])

    def test_sdk_env(self):
        p = Project(self)
        with open(p.path("sdk.ps1"), "w", encoding="utf-8-sig") as f:
            f.write("$env:MY_VAR = 'hello'\nSet-Location ..\n")
        p.configure(shell="powershell", sdk_env="sdk.ps1", outputs="app",
                    build_command=ps_fake("env", "MY_VAR", "+", "write", "app"))
        code, data = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertIn("MY_VAR=hello", data["tail"])


if __name__ == "__main__":
    unittest.main()
