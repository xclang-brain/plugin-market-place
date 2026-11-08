# -*- coding: utf-8 -*-
"""cross-compile スキル（scripts/build.py）のテスト。

主なテストは、OS に頼らないビルドのコマンド（tests/fake_build.py。Python でファイルを作る）で、
この PC で使えるシェル（cmd・PowerShell・bash。Windows の bash は Git for Windows などのもの）ごとに動かす。
PC の make・gcc・bash で、小さな C のプロジェクトを実際にビルドするテストもある
（Windows 以外で、make・gcc・bash があるときだけ）。
ARM 向けの SDK の環境設定ファイルがあれば、ARM 向けのビルドも試す。

実行（このスキルのフォルダで）：
  py -3 -m unittest discover -s tests -v      （Windows）
  python3 -m unittest discover -s tests -v    （Linux など）
"""

import codecs
import ast
import contextlib
import importlib.util
import io
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)  # このテストのフォルダ（tests）の親が、スキルのフォルダ
SKILL_NAME = os.path.basename(SKILL_DIR)
DATA_DIR = "." + SKILL_NAME  # 作業フォルダの中の、設定とログのフォルダ
SCRIPT = os.path.join(SKILL_DIR, "scripts", "build.py")
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
FAKE = os.path.join(HERE, "fake_build.py")
SDK_ENV = "/opt/poky/4.0/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi"
IS_WINDOWS = os.name == "nt"
DEFAULT_SHELL = "cmd" if IS_WINDOWS else "bash"

MAKEFILE = (
    "all: app\n"
    "\n"
    "app: app.c\n"
    "\t$(CC) -Wall -o app app.c\n"
    "\n"
    "clean:\n"
    "\trm -f app\n"
)
BROKEN_SDK = (
    'echo "Your environment is misconfigured" >&2\n'
    'echo "  see: http://example.invalid/help" >&2\n'
    'return 1\n'
)
GOOD_C = '#include <stdio.h>\nint main(void) { puts("hello"); return 0; }\n'
BAD_C = '#include <stdio.h>\nint main(void) { puts("hello") return 0; }\n'

TEMPLATE_KEYS = ["source_dir", "shell", "sdk_env", "build_command", "clean_command",
                 "outputs", "timeout_seconds"]


def load_module():
    spec = importlib.util.spec_from_file_location("cross_compile_build_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BUILD = load_module()


def have(cmd):
    return shutil.which(cmd) is not None


def sdk_compiler():
    """SDK を読み込んだ後の CC の最初の語（コンパイラーの名前）。使えなければ None。"""
    if IS_WINDOWS or not os.path.isfile(SDK_ENV) or not have("bash"):
        return None
    r = subprocess.run(
        ["bash", "-c", '. "$1" >/dev/null 2>&1 || exit 1; set -- $CC; '
         '[ -n "$1" ] && command -v "$1" >/dev/null && printf "%s" "$1"',
         "x", SDK_ENV],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    word = r.stdout.decode("utf-8", "replace").strip()
    return word if r.returncode == 0 and word else None


SDK_CC = sdk_compiler()


def shell_available(shell):
    if shell == "cmd":
        return IS_WINDOWS
    if shell == "powershell":
        return have("pwsh") or have("powershell")
    # Windows では、Git for Windows などの bash（WSL の bash は使わない）
    return BUILD.find_shell("bash") is not None


# make・gcc・bash で、実際に C のプログラムをビルドするテスト
needs_build_tools = unittest.skipUnless(
    not IS_WINDOWS and have("make") and have("gcc") and have("bash"),
    "Windows 以外で、make・gcc・bash があるときだけ動かします")


def render(value):
    return json.dumps(value, ensure_ascii=False)


def quote_for(shell, arg):
    """コマンドの1語を、そのシェルの書き方で囲む。"""
    if shell == "bash":
        return shlex.quote(arg.replace("\\", "/") if IS_WINDOWS else arg)
    if shell == "cmd":
        return '"{}"'.format(arg) if re.search(r'[\s&|<>()^;,=\'"]', arg) or not arg else arg
    return "'{}'".format(arg.replace("'", "''"))


def py_command(shell, *args):
    """fake_build.py を動かすコマンド（shell の書き方）。"""
    words = [quote_for(shell, w) for w in [sys.executable, FAKE] + list(args)]
    text = " ".join(words)
    return "& " + text if shell == "powershell" else text


# Windows で、スクリプトをコンソールなしで動かす（スクリプトは Python の既定の文字コードで書く）
NO_CONSOLE = {"creationflags": subprocess.DETACHED_PROCESS} if IS_WINDOWS else {}


def console_code_page():
    """Windows で、このテストのプロセスのコンソールの出力の文字コード（cp932 など）。なければ None。
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


def stdout_encoding(env, console=True):
    """スクリプトが標準出力と標準エラー出力に書く文字コード（パイプのとき）。
    Windows でコンソールを受け継ぐときは、コンソールの文字コード。ほかは Python の既定。"""
    if console and console_code_page():
        return console_code_page()
    value = (env or os.environ).get("PYTHONIOENCODING", "")
    return value.split(":")[0] or locale.getpreferredencoding(False)


def as_written(text, env=None):
    """スクリプトが、その文字コードで書いた後の文字（書けない文字は \\uXXXX になる）。"""
    encoding = stdout_encoding(env)
    return text.encode(encoding, "backslashreplace").decode(encoding)


class Project(object):
    """作業フォルダ（一時フォルダ）と、小さな C のプロジェクト。"""

    def __init__(self, testcase, source=GOOD_C):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="cc-test-"))
        testcase.addCleanup(shutil.rmtree, self.dir, True)
        self.write("Makefile", MAKEFILE)
        self.write("app.c", source)
        self.config = os.path.join(self.dir, DATA_DIR, "config.yaml")

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def write(self, name, text):
        p = self.path(name)
        d = os.path.dirname(p)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def configure(self, **values):
        """ひな形をコピーし、指定した項目の行だけを書き換える（コメントは残す）。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        out = []
        for line in lines:
            m = re.match(r"^([A-Za-z_]+):", line)
            if m and m.group(1) in values:
                line = "{}: {}".format(m.group(1), render(values[m.group(1)]))
            out.append(line)
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        with open(self.config, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")

    def fake(self, shell=DEFAULT_SHELL, **values):
        """fake_build.py で app を作る設定にする。"""
        values.setdefault("shell", shell)
        shell = shell.lower()
        values.setdefault("build_command", py_command(
            DEFAULT_SHELL if shell == "auto" else shell, "write", "app"))
        values.setdefault("outputs", ["app"])
        self.configure(**values)

    def run(self, *args, **kw):
        return run_script(list(args) + ["--workdir", self.dir], **kw)


def run_script(args, cwd=None, timeout=120, env=None, script=SCRIPT):
    if env is None:
        # テストを動かす端末の CC に左右されないよう、外しておく
        env = {k: v for k, v in os.environ.items() if k.upper() != "CC"}
    r = subprocess.run([sys.executable, script] + args, cwd=cwd, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    out = r.stdout.decode(stdout_encoding(env))
    try:
        data = json.loads(out)
    except ValueError:
        raise AssertionError("標準出力が JSON 1つになっていません：\n" + out)
    if not isinstance(data, dict):
        raise AssertionError("JSON がオブジェクトではありません：" + out)
    return r.returncode, data, r.stderr.decode(stdout_encoding(env), "replace")


def read_log(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")  # ログは、いつも UTF-8


def pid_alive(pid):
    """プロセスが動いているか（終わった後のゾンビは、動いていないとみなす）。"""
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    if os.path.isdir("/proc"):
        try:
            with open("/proc/{}/stat".format(pid), encoding="utf-8", errors="replace") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state not in ("Z", "X")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until_gone(pids, seconds=10):
    end = time.time() + seconds
    alive = [p for p in pids if pid_alive(p)]
    while alive and time.time() < end:
        time.sleep(0.2)
        alive = [p for p in alive if pid_alive(p)]
    return alive


# ---------------------------------------------------------------------------
# 設定のひな形と、YAML の読み方
# ---------------------------------------------------------------------------

class TemplateTest(unittest.TestCase):

    def test_keys_in_order_with_comment_above(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        keys = []
        for i, line in enumerate(lines):
            m = re.match(r"^([A-Za-z_]+):", line)
            if not m:
                continue
            keys.append(m.group(1))
            self.assertTrue(i > 0 and lines[i - 1].startswith("#"),
                            "{} のすぐ上にコメントがありません".format(m.group(1)))
            # コメントのかたまりの1行目は、日本語の説明
            j = i - 1
            while j > 0 and lines[j - 1].startswith("#"):
                j -= 1
            self.assertRegex(lines[j], r"^# [^\x00-\x7f]", "説明が日本語ではありません：" + lines[j])
        self.assertEqual(keys, TEMPLATE_KEYS)

    def test_header_comment_names_owner(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        header = []
        for line in lines:
            if not line.startswith("#"):
                break
            header.append(line)
        self.assertTrue(1 <= len(header) <= 3, header)
        self.assertIn("設定ファイルです", header[0])
        # フォルダの名前を変えてコピーしても食い違わないよう、スキルの名前（英字）は書かない
        self.assertNotRegex(header[0], r"[A-Za-z]", header[0])

    def test_only_outputs_is_required(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        blocks = re.findall(r"((?:^#.*\n)+)([A-Za-z_]+):", text, re.M)
        required = [key for comment, key in blocks if "必須" in comment]
        self.assertEqual(required, ["outputs"])

    def test_sdk_env_comment_says_it_is_needed_for_the_device(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        block = re.search(r"((?:^#.*\n)+)sdk_env:", text, re.M).group(1)
        # 設定を案内する側が「必ず書く」の言い方で、ふだん埋める項目だと分かるように
        self.assertIn("機器向けにビルドするなら、必ず書く。", block)
        self.assertNotIn("必須", block, "必須の印を付けると、空のときにビルドできなくなる")
        # WSL の中の SDK を使うときは空にする（「必ず書く」の例外として書く）
        self.assertIn("ただし、WSL の中の SDK を使うときは空にして", block)
        # シェルごとのファイルの種類。どの項目のことかが分かるように、shell の名前で言う
        self.assertIn("shell が cmd（Windows の auto も同じ）なら .bat・.cmd、powershell なら .ps1 にする。", block)
        # 例が何の例か（Windows で使う SDK と、Linux で使う Yocto の SDK）が分かる
        self.assertIn("例の1つ目は Windows で使う SDK（cmd 用）、2つ目は Linux で使う Yocto の SDK（bash 用）。",
                      block)
        examples = re.findall(r"^# 例：(.*)$", block, re.M)
        self.assertEqual(len(examples), 2, examples)
        self.assertRegex(examples[0], r"^'[A-Za-z]:\\.*\.bat'$")
        self.assertIn("environment-setup-", examples[1])

    def test_shell_comment_lists_the_values_and_auto(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        block = re.search(r"((?:^#.*\n)+)shell:", text, re.M).group(1)
        self.assertIn("auto・cmd・powershell・bash", block)
        self.assertIn("Windows では cmd、それ以外では bash", block)

    def test_clean_command_comment_says_the_old_build_stays(self):
        """空のままだと「前のビルドのまま」になることを、コメントだけで分かるようにする。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        block = re.search(r"((?:^#.*\n)+)clean_command:", text, re.M).group(1)
        self.assertIn("作り直されず", block)
        self.assertIn("前のビルドでできたファイルがそのまま残る", block)
        self.assertIn("make clean", block)

    def test_template_examples_are_generic(self):
        """例は、どのプロジェクトにも当てはまらない名前にする。そのままコピーされても困らないように。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('# 例：["my_app"]', text)

    def test_template_comments_do_not_point_by_position(self):
        """項目の説明のコメントは、項目と一緒に、設定ファイルの最後へ足されることがある。
        「下の」など位置を指す言葉は、そこでは通じない。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            if not re.match(r"^[A-Za-z_]+:", line):
                continue
            j = i
            while j > 0 and lines[j - 1].startswith("#"):
                j -= 1
            for comment in lines[j:i]:
                for word in ("下の", "上の", "上から", "このスキル", "この項目"):
                    self.assertNotIn(word, comment, comment)

    def test_template_values(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            data = BUILD.parse_yaml(f.read())
        self.assertEqual(data, {
            "source_dir": ".", "shell": "auto", "sdk_env": "", "build_command": "make",
            "clean_command": "", "outputs": [], "timeout_seconds": 600})

    def test_template_lines_are_short_enough(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.startswith("# 例："):
                    continue
                self.assertLessEqual(len(line), 70, line)

    def test_windows_example_path_parses(self):
        """コメントの Windows のパスの例は、そのまま値に書いても読める書き方にする。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            examples = re.findall(r"^# 例：('[A-Za-z]:\\.*')$", f.read(), re.M)
        self.assertTrue(examples)
        for example in examples:
            value = BUILD.parse_yaml("sdk_env: " + example)["sdk_env"]
            self.assertRegex(value, r"^[A-Za-z]:\\[^\x00-\x1f]+$")


class YamlSubsetTest(unittest.TestCase):

    def test_scalars(self):
        data = BUILD.parse_yaml(
            "# top\n"
            "a: \"x # not comment\"\n"
            "b: 'it''s'\n"
            "c: plain text  # comment\n"
            "d: 12\n"
            "e: -3\n"
            "f: 1.5\n"
            "g: true\n"
            "h: False\n"
            "i: null\n"
            "j:\n"
            "k: ~\n"
            "l: \"\\u3042\\n\"\n")
        self.assertEqual(data, {
            "a": "x # not comment", "b": "it's", "c": "plain text", "d": 12, "e": -3,
            "f": 1.5, "g": True, "h": False, "i": None, "j": None, "k": None,
            "l": "\u3042\n"})

    def test_lists(self):
        data = BUILD.parse_yaml(
            "a: [\"x\", 'y', z, 3]\n"
            "b: []\n"
            "c:\n"
            "  - one\n"
            "  # comment inside\n"
            "\n"
            "  - \"two\"\n"
            "d:\n"
            "- three\n"
            "e: [ \"p\" , \"q\", ]\n")
        self.assertEqual(data, {"a": ["x", "y", "z", 3], "b": [], "c": ["one", "two"],
                                "d": ["three"], "e": ["p", "q"]})

    def test_bom_and_document_start(self):
        self.assertEqual(BUILD.parse_yaml("\ufeff---\na: 1\n"), {"a": 1})

    def test_windows_paths(self):
        data = BUILD.parse_yaml(
            "a: 'C:\\sdk\\setenv.bat'\n"
            "b: C:\\sdk\\setenv.bat\n"
            "c: \"C:/sdk/setenv.bat\"\n"
            "d: \"C:\\\\sdk\\\\setenv.bat\"\n")
        self.assertEqual(data, {"a": "C:\\sdk\\setenv.bat", "b": "C:\\sdk\\setenv.bat",
                                "c": "C:/sdk/setenv.bat", "d": "C:\\sdk\\setenv.bat"})

    def test_bad_escape_says_how_to_write_a_backslash(self):
        with self.assertRaises(BUILD.YamlError) as cm:
            BUILD.parse_yaml('sdk_env: "C:\\sdk\\setenv.bat"\n')
        self.assertIn("「'」で囲んで", str(cm.exception))

    def test_errors_name_the_line(self):
        bad = [
            "a: 1\nb c\n",
            "a: \"open\n",
            "a: 'open\n",
            "a: [\"x\"\n",
            "a: [[1]]\n",
            "a: {x: 1}\n",
            "a: |\n  text\n",
            "a: 1\na: 2\n",
            "a: 1\n  - x\n",
            "a: \"x\" y\n",
        ]
        for text in bad:
            with self.assertRaises(BUILD.YamlError, msg=text) as cm:
                BUILD.parse_yaml(text)
            self.assertIn("行目", str(cm.exception))

    def test_matches_pyyaml_when_available(self):
        try:
            import yaml  # noqa: F401  (利用者の PC には無くてよい。あれば比べる)
        except ImportError:
            self.skipTest("PyYAML がありません")
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(BUILD.parse_yaml(text), yaml.safe_load(text))


class LinePickTest(unittest.TestCase):

    def test_error_lines_ignore_compiler_flags(self):
        lines = [
            "==> ビルドのコマンドを実行します",
            "gcc -Wall -Werror -Wno-error=format -fmax-errors=5 -o app app.c",
            "app.c:2:40: error: expected ';' before 'return'",
            "コンパイルエラーがあります",
            "make: *** [Makefile:4: app] Error 1",
        ]
        self.assertEqual(BUILD.error_lines_of(lines), [
            "app.c:2:40: error: expected ';' before 'return'",
            "コンパイルエラーがあります",
            "make: *** [Makefile:4: app] Error 1",
        ])

    def test_error_lines_include_link_errors(self):
        lines = [
            "/usr/bin/ld: main.o: in function `main':",
            "main.c:(.text+0x9): undefined reference to `foo'",
            "collect2: error: ld returned 1 exit status",
        ]
        self.assertEqual(BUILD.error_lines_of(lines), lines[1:])

    def test_error_lines_limit(self):
        lines = ["x.c:{}: error: bad".format(i) for i in range(50)]
        self.assertEqual(len(BUILD.error_lines_of(lines)), 20)

    def test_command_not_found_in_cmd(self):
        """cmd /c は、コマンドが見つからなくても終了コード 1 で終わる。最後の出力の文で見分ける。"""
        found = BUILD.command_not_found
        english = ["'tool-xyz' is not recognized as an internal or external command,",
                   "operable program or batch file."]
        japanese = ["'tool-xyz' は、内部コマンドまたは外部コマンド、",
                    "操作可能なプログラムまたはバッチ ファイルとして認識されていません。"]
        for said in (english, japanese):
            self.assertTrue(found("cmd", 1, ["building"] + said))
            self.assertTrue(found("cmd", 9009, said))
            self.assertFalse(found("cmd", 2, said))  # ほかの終了コードは、ビルドの失敗
            self.assertFalse(found("cmd", 1, said + ["make: *** [all] Error 1"]))  # 後に続く出力がある
        self.assertFalse(found("cmd", 1, ["app.c: No such file or directory"]))
        self.assertTrue(found("bash", 127, []))
        self.assertTrue(found("powershell", 127, []))
        self.assertFalse(found("bash", 1, english))
        s = {"shell": "cmd", "build_command": "tool-xyz all", "clean_command": "",
             "timeout_seconds": 60}
        msg = BUILD.failure_message("build", 1, s, [], english)
        self.assertIn("コマンドが見つかりません", msg)
        self.assertIn("tool-xyz", msg)

    def test_elf_arch(self):
        tmp = tempfile.mkdtemp(prefix="cc-elf-")
        self.addCleanup(shutil.rmtree, tmp, True)

        def elf(name, cls, order, machine):
            head = b"\x7fELF" + bytes([cls, order, 1]) + b"\0" * 11
            head += machine.to_bytes(2, "little" if order == 1 else "big")
            path = os.path.join(tmp, name)
            with open(path, "wb") as f:
                f.write(head + b"\0" * 40)
            return path

        self.assertEqual(BUILD.elf_arch(elf("arm", 1, 1, 40)), "ARM")
        self.assertEqual(BUILD.elf_arch(elf("x64", 2, 1, 62)), "x86-64")
        self.assertEqual(BUILD.elf_arch(elf("a64", 2, 1, 183)), "ARM aarch64")
        self.assertEqual(BUILD.elf_arch(elf("ppc", 1, 2, 20)), "PowerPC")
        text = os.path.join(tmp, "text")
        with open(text, "w") as f:
            f.write("hello\n")
        self.assertIsNone(BUILD.elf_arch(text))
        self.assertIsNone(BUILD.elf_arch(os.path.join(tmp, "none")))
        self.assertEqual(BUILD.kind_of(None, "ASCII text, with no line terminators"), "ASCII text")
        self.assertEqual(BUILD.kind_of(None, None), "不明")
        self.assertEqual(BUILD.kind_of("ARM", "ELF 32-bit LSB executable, ARM"), "ARM")

    def test_pc_arch_from_machine_name(self):
        """Python の実行ファイルが ELF でない PC（Windows など）は、CPU の名前から決める。"""
        self.assertEqual(BUILD.pc_arch("AMD64"), "x86-64")
        self.assertEqual(BUILD.pc_arch("x86_64"), "x86-64")
        self.assertEqual(BUILD.pc_arch("ARM64"), "ARM aarch64")
        self.assertIsNone(BUILD.pc_arch("unknown-cpu"))
        self.assertTrue(BUILD.pc_arch())

    def test_first_word(self):
        self.assertEqual(BUILD.first_word("make -j4 all"), "make")
        self.assertEqual(BUILD.first_word("CC=gcc V=1 make"), "make")
        self.assertEqual(BUILD.first_word("(cd sub && make)"), "cd")
        self.assertEqual(BUILD.first_word("./build.sh --release"), "./build.sh")
        with self.assertRaises(ValueError):
            BUILD.first_word("make 'oops")

    def test_first_word_cmd(self):
        fw = lambda c: BUILD.first_word(c, "cmd")  # noqa: E731
        self.assertEqual(fw("make -j4 all"), "make")
        self.assertEqual(fw(r"C:\tools\make.exe -j4"), r"C:\tools\make.exe")
        self.assertEqual(fw(r'"C:\Program Files\make\make.exe" all'), r"C:\Program Files\make\make.exe")
        self.assertEqual(fw("@call build.bat"), "call")
        self.assertEqual(fw("(cd sub&& make)"), "cd")
        self.assertEqual(fw('wsl bash -c "source /opt/sdk/env && make"'), "wsl")
        self.assertEqual(fw(r".\build.bat"), r".\build.bat")
        with self.assertRaises(ValueError):
            fw('"C:\\tools\\make.exe all')

    def test_first_word_powershell(self):
        fw = lambda c: BUILD.first_word(c, "powershell")  # noqa: E731
        self.assertEqual(fw("make -j4"), "make")
        self.assertEqual(fw(r"& 'C:\Program Files\make\make.exe' all"), r"C:\Program Files\make\make.exe")
        self.assertEqual(fw(r'& "C:\tools\make.exe"'), r"C:\tools\make.exe")
        self.assertEqual(fw(r".\build.ps1 -Release"), r".\build.ps1")
        self.assertEqual(fw(r". .\env.ps1; make"), r".\env.ps1")
        self.assertEqual(fw("cd sub; make"), "cd")
        self.assertIsNone(fw("$env:CC = 'gcc'; make"))
        self.assertEqual(fw("Write-Output 'it''s'"), "Write-Output")
        with self.assertRaises(ValueError):
            fw("make 'oops")
        with self.assertRaises(ValueError):
            fw('make "oops')


class ProgramLookupTest(unittest.TestCase):
    """コマンドの実行ファイルの探し方（PATH、PATHEXT、cmd は作業中のフォルダを先に探す）。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cc-lookup-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ("bin1", "bin2", "src"):
            os.makedirs(os.path.join(self.tmp, d))

    def touch(self, *parts, mode=0o755):
        p = os.path.join(self.tmp, *parts)
        with open(p, "w") as f:
            f.write("x")
        os.chmod(p, mode)
        return p

    def lookup(self, word, **kw):
        dirs = [os.path.join(self.tmp, "bin1"), os.path.join(self.tmp, "bin2")]
        return BUILD.lookup_program(word, dirs, os.path.join(self.tmp, "src"), **kw)

    def test_windows_rules(self):
        exts = [".COM", ".EXE", ".BAT", ".CMD"]
        gcc = self.touch("bin2", "gcc.EXE")
        self.assertEqual(self.lookup("gcc", exts=exts), gcc)
        self.assertEqual(self.lookup("gcc.EXE", exts=exts), gcc)
        self.assertIsNone(self.lookup("make", exts=exts))
        # cmd は、作業中のフォルダを PATH より先に探す。PowerShell と bash は探さない
        local = self.touch("src", "build.BAT")
        self.assertEqual(self.lookup("build", exts=exts, search_cwd=True), local)
        self.assertIsNone(self.lookup("build", exts=exts))
        # 区切りのある語は、作業中のフォルダからのパスとして探す
        self.assertEqual(self.lookup(os.path.join(".", "build.BAT"), exts=exts), os.path.join(
            self.tmp, "src", ".", "build.BAT"))
        self.assertEqual(self.lookup(gcc, exts=exts), gcc)

    @unittest.skipIf(IS_WINDOWS, "実行の権限は Windows にありません")
    def test_posix_rules(self):
        cc = self.touch("bin2", "cc")
        self.touch("bin1", "cc", mode=0o644)  # 実行できないものは飛ばす
        self.assertEqual(self.lookup("cc"), cc)
        self.assertIsNone(self.lookup("gcc"))
        self.touch("src", "build.sh")
        self.assertIsNone(self.lookup("build.sh"), "bash は作業中のフォルダを探さない")
        self.assertTrue(self.lookup("./build.sh"))


class StreamAndEncodingTest(unittest.TestCase):

    def test_json_text_keeps_json_valid_in_any_encoding(self):
        data = {"message": "ビルドに成功しました〜 \u301c ✓ \U0001F600 é", "path": "C:\\作業"}
        text = json.dumps(data, ensure_ascii=False, indent=2)
        for encoding in ("ascii", "cp932", "cp1252", "utf-8"):
            out = BUILD.json_text_for(text, encoding)
            out.encode(encoding)  # 書ける文字だけになっている
            self.assertEqual(json.loads(out), data, encoding)
        self.assertIn("ビルドに成功しました", BUILD.json_text_for(text, "cp932"))
        self.assertEqual(BUILD.json_text_for(text, "utf-8"), text)

    def test_decode_output_falls_back_per_line(self):
        data = "UTF-8 の行\r\n".encode("utf-8") + "シフトJIS の行\r\n".encode("cp932") + b"tail"
        self.assertEqual(BUILD.decode_output(data, fallback="cp932"),
                         "UTF-8 の行\r\nシフトJIS の行\r\ntail")

    def test_recode_log_rewrites_only_the_build_output(self):
        tmp = tempfile.mkdtemp(prefix="cc-recode-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "x.log")
        head = "見出し\n".encode("utf-8")
        with open(path, "wb") as f:
            f.write(head + "エラー: 見つかりません\r\n".encode("cp932"))
        original = BUILD.fallback_encoding
        self.addCleanup(setattr, BUILD, "fallback_encoding", original)
        BUILD.fallback_encoding = lambda: "cp932"
        text = BUILD.recode_log(path, len(head))
        self.assertEqual(text, "エラー: 見つかりません\r\n")
        with open(path, "rb") as f:
            self.assertEqual(f.read().decode("utf-8"), "見出し\nエラー: 見つかりません\r\n")

    def test_stdout_in_narrow_encodings_is_still_json(self):
        """コンソールがないときは、標準出力は Python の既定の文字コードで書く。書けない文字は \\uXXXX にする。
        （Windows でコンソールがあるときに、その文字コードで書くことは、ConsoleCodePageTest で確かめる）"""
        p = Project(self)
        for encoding in ("ascii", "cp932", "utf-8"):
            env = {k: v for k, v in os.environ.items() if k.upper() != "CC"}
            env["PYTHONIOENCODING"] = encoding
            env.pop("PYTHONUTF8", None)
            r = subprocess.run([sys.executable, SCRIPT, "config-path", "--workdir", p.dir],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, timeout=60, **NO_CONSOLE)
            data = json.loads(r.stdout.decode(encoding))
            self.assertIn("設定ファイル", data["message"], encoding)
            if encoding == "ascii":
                self.assertTrue(all(b < 0x80 for b in r.stdout))
            if encoding == "cp932":
                self.assertIn("設定ファイル".encode("cp932"), r.stdout)


# ---------------------------------------------------------------------------
# 設定ファイルに足りない項目を足す
# ---------------------------------------------------------------------------

def template_without(*keys):
    """ひな形から、指定した項目（すぐ上のコメントと項目の行）を除いた文。"""
    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read()
    for key, lines in BUILD.template_blocks(text):
        if key in keys:
            text = text.replace("\n".join(lines) + "\n", "")
    return text


class AddMissingKeysTest(unittest.TestCase):

    def old_config(self, p, text):
        os.makedirs(os.path.dirname(p.config), exist_ok=True)
        with open(p.config, "wb") as f:
            f.write(text.encode("utf-8"))
        return text.encode("utf-8")

    def read(self, p):
        with open(p.config, "rb") as f:
            return f.read()

    def template_blocks(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            return BUILD.template_blocks(f.read())

    def test_template_blocks(self):
        blocks = self.template_blocks()
        self.assertEqual([k for k, _ in blocks], TEMPLATE_KEYS)
        for key, lines in blocks:
            self.assertTrue(lines[-1].startswith(key + ":"), key)
            self.assertTrue(all(l.startswith("#") for l in lines[:-1]), key)
            self.assertGreaterEqual(len(lines), 2, key)
        # ファイルの先頭の説明は、どの項目にも入らない
        with open(TEMPLATE, encoding="utf-8") as f:
            first = f.read().splitlines()[0]
        self.assertTrue(first.startswith("# "), first)
        self.assertNotIn(first, blocks[0][1])

    def test_missing_keys_are_appended_with_comments(self):
        p = Project(self)
        text = template_without("clean_command", "timeout_seconds").rstrip("\n") + "\n"
        text = text.replace('outputs: []', 'outputs: ["app"]')
        text = text.replace('build_command: "make"', 'build_command: "make -j2"  # 速くする')
        before = self.old_config(p, text)
        code, data, _ = p.run("config-path")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["added"], ["clean_command", "timeout_seconds"])
        self.assertIn("設定ファイルに、新しい項目 clean_command、timeout_seconds を足しました"
                      "（今の値はひな形のまま）。", data["message"])
        after = self.read(p)
        # もとの所は1バイトも変えず、最後に足す
        self.assertEqual(after[:len(before)], before)
        tail = after[len(before):].decode("utf-8")
        blocks = dict(self.template_blocks())
        self.assertEqual(tail, "\n" + "\n".join(blocks["clean_command"]) + "\n\n"
                         + "\n".join(blocks["timeout_seconds"]) + "\n")
        values = BUILD.parse_yaml(after.decode("utf-8"))
        self.assertEqual(values["build_command"], "make -j2")
        self.assertEqual(values["outputs"], ["app"])
        self.assertEqual(values["clean_command"], "")
        self.assertEqual(values["timeout_seconds"], 600)

        # 2回目は、何も足さない
        code, data, _ = p.run("config-path")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["added"], [])
        self.assertNotIn("新しい項目", data["message"])
        self.assertEqual(self.read(p), after)

    def test_config_from_before_shell_gets_shell(self):
        """shell の項目が無かったころの設定ファイルにも、shell（auto）が足される。"""
        p = Project(self)
        text = template_without("shell").replace('outputs: []', 'outputs: ["app"]')
        before = self.old_config(p, text)
        code, data, _ = p.run("config-path")
        self.assertEqual(data["added"], ["shell"])
        after = self.read(p)
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(BUILD.parse_yaml(after.decode("utf-8"))["shell"], "auto")

    def test_no_extra_blank_line_after_a_blank_line(self):
        p = Project(self)
        text = template_without("timeout_seconds").rstrip("\n") + "\n\n"
        before = self.old_config(p, text)
        p.run("config-path")
        tail = self.read(p)[len(before):]
        self.assertTrue(tail.startswith(b"# "), tail)

    def test_nothing_added_when_complete(self):
        p = Project(self)
        p.configure(outputs=["app"])
        before = self.read(p)
        code, data, _ = p.run("config-path")
        self.assertEqual(data["added"], [])
        self.assertFalse(data["created"])
        self.assertEqual(self.read(p), before)

    def test_new_config_reports_nothing_added(self):
        p = Project(self)
        code, data, _ = p.run("config-path")
        self.assertTrue(data["created"])
        self.assertEqual(data["added"], [])

    def test_no_final_newline_and_crlf(self):
        p = Project(self)
        text = template_without("timeout_seconds").replace('outputs: []', 'outputs: ["app"]')
        text = text.rstrip("\n").replace("\n", "\r\n")
        before = self.old_config(p, text)
        code, data, _ = p.run("config-path")
        self.assertEqual(data["added"], ["timeout_seconds"])
        after = self.read(p)
        self.assertEqual(after[:len(before)], before)
        tail = after[len(before):]
        self.assertTrue(tail.startswith(b"\r\n\r\n#"), tail)
        self.assertTrue(tail.endswith(b"timeout_seconds: 600\r\n"), tail)
        self.assertNotIn(b"\n", tail.replace(b"\r\n", b""))

    def test_required_key_added_is_reported_missing(self):
        p = Project(self)
        before = self.old_config(p, template_without("outputs"))
        code, data, _ = p.run("config-path")
        self.assertEqual(data["added"], ["outputs"])
        self.assertEqual(data["missing"], ["outputs"])
        self.assertEqual(self.read(p)[:len(before)], before)

    def test_check_and_run_do_not_add(self):
        """check と run は、足りない項目を足さずに、notes で知らせる。足すのは config-path だけ。"""
        p = Project(self)
        p.fake()
        text = template_without("clean_command")
        with open(p.config, encoding="utf-8") as f:
            current = f.read()
        build_line = re.search(r"(?m)^build_command: .*$", current).group(0)
        text = re.sub(r"(?m)^build_command: .*$", lambda m: build_line, text)
        text = text.replace('outputs: []', 'outputs: ["app"]')
        text = text.replace('shell: "auto"', 'shell: "{}"'.format(DEFAULT_SHELL))
        before = self.old_config(p, text)
        note = "設定ファイルに、項目 clean_command がありません。config-path で、ひな形から足せます。"
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertNotIn("added", data)
        self.assertIn(note, data["notes"])
        self.assertIn(note, data["message"])
        self.assertEqual(self.read(p), before)
        self.assertEqual(os.listdir(p.path(DATA_DIR)), ["config.yaml"])

        p2 = Project(self)
        before = self.old_config(p2, text)
        code, data, _ = p2.run("run")
        self.assertEqual(code, 0, data)
        self.assertNotIn("added", data)
        self.assertIn(note, data["notes"])
        self.assertEqual(self.read(p2), before)

        # config-path が足す。その後は、check も run も知らせない
        code, data, _ = p2.run("config-path")
        self.assertEqual(data["added"], ["clean_command"])
        self.assertEqual(self.read(p2)[:len(before)], before)
        code, data, _ = p2.run("check")
        self.assertEqual(code, 0, data)
        self.assertNotIn(note, data["notes"])

    def test_check_reports_a_missing_required_key_without_writing(self):
        p = Project(self)
        before = self.old_config(p, template_without("outputs", "timeout_seconds"))
        code, data, _ = p.run("check")
        self.assertEqual(code, 2, data)
        self.assertTrue(any("outputs（ビルドでできるファイル）が空です（必須）" in x
                            for x in data["problems"]), data)
        self.assertIn("設定ファイルに、項目 outputs、timeout_seconds がありません。"
                      "config-path で、ひな形から足せます。", data["notes"])
        self.assertEqual(self.read(p), before)
        self.assertEqual(os.listdir(p.path(DATA_DIR)), ["config.yaml"])

        code, data, _ = p.run("config-path")
        self.assertEqual(data["added"], ["outputs", "timeout_seconds"])
        self.assertEqual(data["missing"], ["outputs"])
        values = BUILD.parse_yaml(self.read(p).decode("utf-8"))
        self.assertEqual(values["outputs"], [])
        self.assertEqual(values["timeout_seconds"], 600)

    def test_broken_file_is_not_touched(self):
        p = Project(self)
        before = self.old_config(p, "outputs: [\"app\"\n")
        code, data, _ = p.run("config-path")
        self.assertEqual(data["added"], [])
        self.assertIn("problems", data)
        self.assertEqual(self.read(p), before)

    def test_unknown_keys_are_kept(self):
        p = Project(self)
        text = "# 自分で書いた設定\nmy_note: \"keep me\"\noutputs:\n  - app\n"
        before = self.old_config(p, text)
        code, data, _ = p.run("config-path")
        self.assertEqual(data["added"], ["source_dir", "shell", "sdk_env", "build_command",
                                         "clean_command", "timeout_seconds"])
        after = self.read(p)
        self.assertEqual(after[:len(before)], before)
        values = BUILD.parse_yaml(after.decode("utf-8"))
        self.assertEqual(values["my_note"], "keep me")
        self.assertEqual(values["outputs"], ["app"])


# ---------------------------------------------------------------------------
# config-path
# ---------------------------------------------------------------------------

class ConfigPathTest(unittest.TestCase):

    def test_creates_from_template_then_reuses(self):
        p = Project(self)
        code, data, _ = p.run("config-path")
        self.assertEqual(code, 0)
        self.assertEqual(data["config"], p.config)
        self.assertTrue(data["created"])
        self.assertEqual(data["missing"], ["outputs"])
        self.assertEqual(data["empty_recommended"], ["sdk_env"])
        self.assertIn("sdk_env", data["message"])
        self.assertIn("機器向け", data["message"])
        self.assertTrue(os.path.isfile(p.config))
        with open(TEMPLATE, encoding="utf-8") as a, open(p.config, encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read())
        self.assertIn("outputs", data["message"])

        code, data, _ = p.run("config-path")
        self.assertEqual(code, 0)
        self.assertFalse(data["created"])
        self.assertEqual(data["missing"], ["outputs"])

        p.configure(outputs=["app"])
        code, data, _ = p.run("config-path")
        self.assertEqual(data["missing"], [])
        self.assertEqual(data["empty_recommended"], ["sdk_env"])

        p.configure(outputs=["app"], sdk_env="/opt/sdk/environment-setup-x")
        code, data, _ = p.run("config-path")
        self.assertEqual(data["empty_recommended"], [])
        self.assertNotIn("機器向け", data["message"])

    def test_new_folder_gets_gitignore(self):
        p = Project(self)
        p.run("config-path")
        with open(p.path(DATA_DIR, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")

    def test_existing_gitignore_is_kept(self):
        p = Project(self)
        p.write(os.path.join(DATA_DIR, ".gitignore"), "logs/\n")
        p.run("config-path")
        with open(p.path(DATA_DIR, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "logs/\n")

    def test_existing_folder_without_gitignore_gets_one(self):
        """利用者が先にフォルダを作っていても、.gitignore が無ければ作る。"""
        p = Project(self)
        os.makedirs(p.path(DATA_DIR))
        p.run("config-path")
        self.assertTrue(os.path.isfile(p.config))
        with open(p.path(DATA_DIR, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")

    def test_gitignore_is_made_again_when_removed(self):
        p = Project(self)
        p.fake()
        gi = p.path(DATA_DIR, ".gitignore")
        for command in ("config-path", "run"):
            if os.path.exists(gi):
                os.unlink(gi)
            p.run(command)
            with open(gi, encoding="utf-8") as f:
                self.assertEqual(f.read(), "*\n", command)
        # check は、ファイルを作らない
        os.unlink(gi)
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertFalse(os.path.exists(gi))

    def test_gitignore_in_work_folder_with_config_elsewhere(self):
        p = Project(self)
        os.makedirs(p.path(DATA_DIR))
        p.run("config-path", "--config", p.path("conf", "my-build.yaml"))
        self.assertTrue(os.path.isfile(p.path(DATA_DIR, ".gitignore")))

    def test_default_workdir_is_current_folder(self):
        p = Project(self)
        code, data, _ = run_script(["config-path"], cwd=p.dir)
        self.assertEqual(code, 0)
        self.assertEqual(os.path.realpath(data["config"]), os.path.realpath(p.config))

    def test_config_option(self):
        p = Project(self)
        target = p.path("conf", "my-build.yaml")
        code, data, _ = p.run("config-path", "--config", target)
        self.assertEqual(code, 0)
        self.assertEqual(data["config"], target)
        self.assertTrue(data["created"])
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(p.config))

    def test_missing_workdir(self):
        p = Project(self)
        missing = p.path("no", "such", "folder")
        code, data, _ = run_script(["config-path", "--workdir", missing])
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertIn(missing, data["message"])

    def test_config_path_is_a_folder(self):
        p = Project(self)
        os.makedirs(p.config)
        code, data, _ = p.run("config-path")
        self.assertEqual(code, 2)
        self.assertEqual(data["config"], p.config)
        self.assertIn("フォルダ", data["message"])

    def test_unknown_argument(self):
        code, data, _ = run_script(["config-path", "--bogus"])
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertIn("--bogus", data["message"])


# ---------------------------------------------------------------------------
# check（シェルによらないもの）
# ---------------------------------------------------------------------------

class CheckTest(unittest.TestCase):

    def test_ok_with_the_default_shell(self):
        p = Project(self)
        p.fake(shell="auto")
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["problems"], [])
        self.assertEqual(data["summary"]["shell"], "auto")
        self.assertTrue(data["summary"]["shell_program"])
        self.assertEqual(data["summary"]["outputs"], ["app"])
        self.assertEqual(os.path.realpath(data["summary"]["source_dir"]),
                         os.path.realpath(p.dir))
        self.assertFalse(os.path.exists(p.path("app")), "check でビルドしてはいけない")

    def test_empty_sdk_env_is_noted_not_failed(self):
        """sdk_env が空でも失敗にはしない。PC に cc があれば、PC 用のプログラムになると、はっきり言う。"""
        p = Project(self)
        p.fake()
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["notes"]), 1)
        note = data["notes"][0]
        for word in ("sdk_env", "SDK を読み込みません"):
            self.assertIn(word, note)
        # 注意があるときは「問題はありません」と言い切らず、「ただし、」で注意を続ける
        self.assertEqual(data["message"], "ビルドの準備はできています。ただし、" + note)
        self.assertNotIn("問題はありません", data["message"])
        self.assertEqual(data["summary"]["compiler"], "cc")
        path = data["summary"]["compiler_path"]
        if path:
            for word in ("PC のコンパイラー", "PC 用", "機器用ではありません", path):
                self.assertIn(word, note)
        else:
            # PC に cc がない（Windows など）。WSL の中でビルドすることもあるので、言い切らない
            self.assertIn("できたファイルの種類で確認", note)
            self.assertNotIn("PC 用", note)
        if not IS_WINDOWS and have("cc"):
            self.assertEqual(path, shutil.which("cc"))

    def test_note_without_a_compiler_does_not_say_pc(self):
        for when in ("plan", "built"):
            note = BUILD.sdk_note(None, when=when)
            self.assertIn("SDK を読み込", note)
            self.assertIn("できたファイルの種類で確認", note)
            self.assertNotIn("PC 用", note)
            self.assertNotIn("cc", note)
        self.assertNotIn("種類", BUILD.sdk_note(None, when="failed"))
        self.assertIn("PC 用", BUILD.sdk_note("/usr/bin/cc", when="built"))

    def test_quoted_compiler_path_with_a_space(self):
        """CC の「"」で囲んだパスは、空白があっても1語として探す（例：C:\\Program Files）。"""
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="cc-quoted-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        folder = os.path.join(tmp, "Program Files", "sdk")
        os.makedirs(folder)
        exe = os.path.join(folder, "my-gcc.exe" if IS_WINDOWS else "my-gcc")
        with open(exe, "w") as f:
            f.write("x")
        os.chmod(exe, 0o755)
        settings = {"source_dir": tmp, "shell": DEFAULT_SHELL}
        for value in ('"{}" -O2'.format(exe), "'{}'".format(exe)):
            cc = BUILD.compiler_of({"PATH": "", "PATHEXT": ".EXE", "CC": value}, settings)
            self.assertEqual(cc["word"], exe, value)
            self.assertEqual(cc["path"], exe, value)
        self.assertEqual(BUILD.cc_word("gcc -O2"), "gcc")
        self.assertEqual(BUILD.cc_word('"open'), '"open')

    def test_cmd_cannot_use_a_folder_starting_with_backslashes(self):
        """cmd は「\\\\」で始まるフォルダ（ネットワークや WSL の中）に移れない。動かす前に止める。"""
        unc = r"\\wsl.localhost\Ubuntu\home\me\proj"
        exists, isdir = os.path.exists, os.path.isdir
        # フォルダはあることにする（ネットワークを見に行かない）
        with mock.patch.object(BUILD, "_abs", lambda workdir, path: unc), \
                mock.patch.object(BUILD, "find_shell", lambda name: "shell"), \
                mock.patch.object(os.path, "exists", lambda p: p == unc or exists(p)), \
                mock.patch.object(os.path, "isdir", lambda p: p == unc or isdir(p)):
            s, problems = BUILD.normalize({"shell": "cmd", "outputs": ["app"]}, "w")
            self.assertFalse(s["runnable"])
            self.assertTrue(any(unc in x and "powershell" in x for x in problems), problems)
            s, problems = BUILD.normalize({"shell": "powershell", "outputs": ["app"]}, "w")
            self.assertTrue(s["runnable"])
            self.assertEqual(problems, [])

    def test_compiler_from_environment_is_named(self):
        p = Project(self)
        p.fake()
        code, data, _ = run_script(["check", "--workdir", p.dir],
                                   env=dict(os.environ, CC="gcc -O2"))
        self.assertEqual(code, 0, data)
        self.assertEqual(data["summary"]["compiler"], "gcc -O2")
        note = data["notes"][0]
        self.assertIn("環境変数 CC", note)
        self.assertNotIn("PC 用", note, "CC が決まっているときは、PC 用とは言い切らない")

    def test_missing_config_is_not_created(self):
        """設定ファイルが無ければ、作らずに「ありません」と返す。作業フォルダには何も増えない。"""
        p = Project(self)
        before = sorted(os.listdir(p.dir))
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertFalse(data["created"])
        self.assertEqual(data["config"], p.config)
        self.assertEqual(data["problems"], ["設定ファイル {} がありません。".format(p.config)])
        self.assertIn("設定ファイル {} がありません。".format(p.config), data["message"])
        self.assertIn("config-path でひな形から作れます。", data["message"])
        self.assertEqual(data["notes"], [])
        self.assertNotIn("added", data)
        self.assertEqual(sorted(os.listdir(p.dir)), before)

    def test_missing_config_in_an_existing_folder(self):
        """設定とログのフォルダがあっても、設定ファイルも .gitignore も作らない。"""
        p = Project(self)
        os.makedirs(p.path(DATA_DIR))
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertEqual(os.listdir(p.path(DATA_DIR)), [])

    def test_missing_config_option_file_is_not_created(self):
        p = Project(self)
        target = p.path("conf", "my-build.yaml")
        code, data, _ = p.run("check", "--config", target)
        self.assertEqual(code, 2)
        self.assertEqual(data["config"], target)
        self.assertIn(target, data["problems"][0])
        self.assertFalse(os.path.exists(p.path("conf")))
        self.assertFalse(os.path.exists(p.path(DATA_DIR)))

    def test_config_path_is_a_folder(self):
        p = Project(self)
        os.makedirs(p.config)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertEqual(data["config"], p.config)
        self.assertIn("フォルダ", data["message"])
        self.assertEqual(os.listdir(p.path(DATA_DIR)), ["config.yaml"])

    def test_existing_config_gets_no_new_files(self):
        """設定ファイルがあるときも、.gitignore やログを作らない。"""
        p = Project(self)
        p.fake()
        top = sorted(os.listdir(p.dir))
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertEqual(sorted(os.listdir(p.dir)), top)
        self.assertEqual(os.listdir(p.path(DATA_DIR)), ["config.yaml"])

    def test_config_elsewhere_makes_no_gitignore(self):
        """--config で別の場所の設定ファイルを使うとき、作業フォルダの設定とログのフォルダに
        .gitignore を作らない。"""
        p = Project(self)
        p.fake()
        target = p.path("conf", "my.yaml")
        os.makedirs(p.path("conf"))
        os.rename(p.config, target)
        self.assertEqual(os.listdir(p.path(DATA_DIR)), [])
        code, data, _ = p.run("check", "--config", target)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["config"], target)
        self.assertEqual(os.listdir(p.path(DATA_DIR)), [])
        self.assertEqual(os.listdir(p.path("conf")), ["my.yaml"])

    def test_empty_required_item_is_a_problem(self):
        p = Project(self)
        p.configure()
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertFalse(data["created"])
        self.assertTrue(any("outputs" in x and "必須" in x for x in data["problems"]),
                        data["problems"])
        self.assertTrue(any("sdk_env" in x for x in data["notes"]), data["notes"])

    def test_unexpected_error_leaves_no_log(self):
        """check で思わぬエラーが起きても、ログを作らない。詳しい中身は標準エラー出力に出す。"""
        p = Project(self)
        p.fake()
        before = sorted(os.listdir(p.path(DATA_DIR)))
        original = BUILD.dispatch
        self.addCleanup(setattr, BUILD, "dispatch", original)

        def boom(argv):
            raise RuntimeError("boom-check")

        BUILD.dispatch = boom
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = BUILD.main(["check", "--workdir", p.dir])
        data = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertIsNone(data["log"])
        self.assertIn("boom-check", data["message"])
        self.assertIn("Traceback", err.getvalue())
        self.assertEqual(sorted(os.listdir(p.path(DATA_DIR))), before)

    def test_missing_sdk_env(self):
        p = Project(self)
        missing = p.path("no", "such", "sdk", "environment-file")
        p.fake(sdk_env=missing)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertTrue(any(missing in x and "SDK" in x for x in data["problems"]),
                        data["problems"])

    def test_missing_source_dir(self):
        p = Project(self)
        p.fake(source_dir="no-such-dir")
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any(p.path("no-such-dir") in x and "source_dir" in x
                            for x in data["problems"]), data["problems"])

    def test_empty_outputs_problem_is_one_sentence_with_a_generic_example(self):
        p = Project(self)
        p.configure(outputs=[])
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        problem = [x for x in data["problems"] if "outputs" in x][0]
        self.assertTrue(problem.endswith("。"), problem)
        self.assertIn("my_app", problem)

    def test_bad_values(self):
        p = Project(self)
        p.fake(outputs="app", timeout_seconds="abc", clean_command=["make"])
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        text = "\n".join(data["problems"])
        self.assertIn("timeout_seconds", text)
        self.assertIn("clean_command", text)
        self.assertNotIn("outputs", text, "1つの文字はファイル1つとして受け付ける")
        self.assertIn("2 件", data["message"])

    def test_timeout_below_one_second(self):
        p = Project(self)
        p.fake(timeout_seconds=0.5)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("timeout_seconds" in x for x in data["problems"]), data["problems"])

    def test_broken_yaml_names_line(self):
        p = Project(self)
        p.write(os.path.join(DATA_DIR, "config.yaml"), "outputs: [\"app\"\n")
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("1 行目" in x for x in data["problems"]), data["problems"])

    def test_unknown_shell(self):
        p = Project(self)
        p.fake(shell="zsh")
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("auto・cmd・powershell・bash" in x and "zsh" in x
                            for x in data["problems"]), data["problems"])

    def test_shell_value_is_not_case_sensitive(self):
        p = Project(self)
        p.fake(shell=DEFAULT_SHELL.upper())
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["summary"]["shell"], DEFAULT_SHELL)

    @unittest.skipIf(IS_WINDOWS, "Windows には cmd がある")
    def test_cmd_outside_windows(self):
        p = Project(self)
        p.fake(shell="cmd", build_command="make")
        code, data, _ = p.run("run")
        self.assertEqual(code, 2)
        self.assertTrue(any("cmd が見つかりません" in x and "Windows" in x
                            for x in data["problems"]), data["problems"])
        self.assertIsNone(data["log"])

    def test_sdk_env_of_the_wrong_kind_for_the_shell(self):
        """cmd と PowerShell に、Linux のシェルスクリプトなどを読み込ませない（動かす前に止める）。"""
        p = Project(self)
        sdk = p.write(os.path.join("sdk", "environment-setup-armv7"), "export CC=gcc\n")
        for shell, kinds in (("cmd", ".bat・.cmd"), ("powershell", ".ps1")):
            p.fake(shell=shell, sdk_env=sdk, build_command="make")
            code, data, _ = p.run("run")
            self.assertEqual(code, 2, data)
            self.assertTrue(any(kinds in x and "wsl" in x for x in data["problems"]),
                            data["problems"])
            self.assertFalse(os.path.exists(p.path("app")))

    def test_windows_path_with_escapes_is_explained(self):
        """「"」で囲んだ Windows のパスの \\n や \\t は、改行やタブになる。どう直すかを言う。"""
        p = Project(self)
        p.fake()
        with open(p.config, encoding="utf-8") as f:
            text = f.read()
        text = text.replace('source_dir: "."', 'source_dir: "C:\\new\\tools"')
        with open(p.config, "w", encoding="utf-8") as f:
            f.write(text)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("タブや改行" in x and "'" in x for x in data["problems"]),
                        data["problems"])


# ---------------------------------------------------------------------------
# シェルごとのテスト（cmd・powershell・bash のうち、この PC で使えるもの）
# ---------------------------------------------------------------------------

class ShellScenarios(object):
    """OS に頼らないビルドのコマンド（fake_build.py）で、シェルごとの振る舞いを確かめる。"""

    SHELL = None
    START = 2  # シェルが動き出すまでの秒数の目安（時間切れのテストで使う）

    def project(self, **values):
        p = Project(self)
        p.fake(shell=self.SHELL, **values)
        return p

    def py(self, *args):
        return py_command(self.SHELL, *args)

    def sdk(self, p, values=None, fail=None):
        """SDK の環境設定ファイルを、このシェルの書き方で作る。"""
        values = values or {}
        if self.SHELL == "cmd":
            lines = ["@echo off"] + ['set "{}={}"'.format(k, v) for k, v in values.items()]
            if fail:
                lines += ["echo {} 1>&2".format(fail), "exit /b 1"]
            name = "sdk.cmd"
        elif self.SHELL == "powershell":
            lines = ["$env:{} = '{}'".format(k, v.replace("'", "''")) for k, v in values.items()]
            if fail:
                lines += ["throw '{}'".format(fail)]
            name = "sdk.ps1"
        else:
            lines = ["export {}={}".format(k, shlex.quote(v)) for k, v in values.items()]
            if fail:
                lines += ['echo "{}" >&2'.format(fail), "return 1"]
            name = "sdk.sh"
        return p.write(os.path.join("sdk dir", name), "\n".join(lines) + "\n")

    def test_check_ok(self):
        p = self.project()
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["summary"]["shell"], self.SHELL)
        self.assertTrue(data["summary"]["shell_program"])
        self.assertFalse(os.path.exists(p.path("app")))

    def test_run_success_and_log(self):
        p = self.project(build_command=self.py("say", "日本語の出力", "+", "write", "app"))
        code, data, err = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["skill"], SKILL_NAME)
        self.assertIn("成功", data["message"])
        self.assertIn(as_written("ビルドを始めます"), err)
        self.assertEqual(data["shell_program"], BUILD.find_shell(self.SHELL))
        out = data["outputs"][0]
        self.assertEqual(out["path"], p.path("app"))
        self.assertTrue(out["exists"] and out["updated"])
        self.assertIn("種類：", data["message"])
        log = read_log(data["log"])
        self.assertIn("シェル：{}".format(self.SHELL), log)
        self.assertIn("==> ビルドのコマンドを実行します", log)
        self.assertIn("wrote app", log)
        self.assertIn("日本語の出力", log)  # ビルドの出力の UTF-8 は、そのままログに残る

    def test_stale_then_clean(self):
        p = self.project(build_command=self.py("once", "app"))
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["outputs"][0]["updated"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["stale_outputs"], ["app"])
        self.assertIn("前のビルドのまま", data["message"])
        p.fake(shell=self.SHELL, build_command=self.py("once", "app"),
               clean_command=self.py("remove", "app"))
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["outputs"][0]["updated"])
        self.assertIn("==> 片付けのコマンドを実行します", read_log(data["log"]))

    def test_build_failure_shows_error_lines(self):
        p = self.project(build_command=self.py(
            "fail", "2", "app.c:2:40: error: expected ';' before 'return'"))
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "build")
        self.assertEqual(data["exit_code"], 2)
        self.assertTrue(any("app.c:2:40: error" in x for x in data["error_lines"]),
                        data["error_lines"])
        self.assertIn("ビルドが失敗しました", data["message"])
        self.assertIn("error", data["message"])
        self.assertTrue(data["tail"])
        self.assertNotIn("できたのは", data["notes"][0])

    def test_command_not_found(self):
        p = self.project(build_command="no-such-build-tool-xyz all")
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("no-such-build-tool-xyz" in x and "見つかりません" in x
                            for x in data["problems"]), data["problems"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("no-such-build-tool-xyz", data["message"])
        self.assertIn("見つかりません", data["message"])

    def test_sdk_env_is_loaded_before_build(self):
        p = self.project(build_command=self.py("env", "GREETING", "+", "write", "app"))
        sdk = self.sdk(p, {"GREETING": "from-the-sdk", "CC": sys.executable})
        p.fake(shell=self.SHELL, sdk_env=sdk,
               build_command=self.py("env", "GREETING", "+", "write", "app"))
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["notes"], [])
        # 注意がないときだけ「問題はありません」と言う
        self.assertEqual(data["message"], "設定に問題はありません。ビルドの準備ができています。")
        self.assertEqual(data["summary"]["compiler"], sys.executable)
        if " " not in sys.executable:
            self.assertTrue(data["summary"]["compiler_path"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        log = read_log(data["log"])
        self.assertIn("greeting=from-the-sdk", log)
        self.assertIn("==> SDK の環境設定ファイルを読み込みます", log)
        self.assertEqual(data["notes"], [])
        self.assertEqual(data["compiler"], sys.executable)
        self.assertNotIn("PC 用", data["message"])

    def test_sdk_that_fails_to_load(self):
        p = self.project()
        sdk = self.sdk(p, fail="Your environment is misconfigured")
        p.fake(shell=self.SHELL, sdk_env=sdk)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("読み込みが失敗" in x and "misconfigured" in x
                            for x in data["problems"]), data["problems"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "sdk_env")
        self.assertIn("misconfigured", data["message"])
        self.assertTrue(any("misconfigured" in x for x in data["tail"]), data["tail"])
        self.assertFalse(os.path.exists(p.path("app")))

    def test_sdk_compiler_missing(self):
        p = self.project()
        sdk = self.sdk(p, {"CC": "no-such-cross-gcc-xyz -O2"})
        p.fake(shell=self.SHELL, sdk_env=sdk)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("no-such-cross-gcc-xyz" in x for x in data["problems"]),
                        data["problems"])

    def test_clean_failure(self):
        p = self.project(clean_command=self.py("fail", "3", "cannot clean"))
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "clean")
        self.assertIn("片付けのコマンド", data["message"])
        self.assertFalse(os.path.exists(p.path("app")))

    def test_failure_in_the_middle_of_a_command_list(self):
        """「cd x && ビルド」の cd の失敗でも、ビルドの失敗として止まる。"""
        p = self.project(build_command="cd no-such-dir && " + self.py("write", "app"))
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "build")
        self.assertNotEqual(data["exit_code"], 0)
        self.assertFalse(os.path.exists(p.path("app")))

    def test_timeout_stops_the_whole_tree(self):
        p = self.project(build_command=self.py("spawn", "pids.txt"), timeout_seconds=self.START)
        t0 = time.time()
        code, data, _ = p.run("run")
        self.assertLess(time.time() - t0, 60)
        self.assertEqual(code, 1, data)
        self.assertTrue(data["timed_out"])
        self.assertIn("{} 秒".format(self.START), data["message"])
        self.assertIn("止めました", data["message"])
        with open(p.path("pids.txt"), encoding="utf-8") as f:
            pids = [int(x) for x in f.read().split()]
        self.assertEqual(len(pids), 2)
        self.assertEqual(wait_until_gone(pids), [], "ビルドの子プロセスが残っています")

    def test_device_binary_without_sdk_is_named(self):
        """sdk_env が空でも（例：WSL の中で SDK を読み込む）、できたファイルの種類を言う。"""
        p = self.project(build_command=self.py("elf-arm", "app"))
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["outputs"][0]["arch"], "ARM")
        self.assertIn("種類：ARM", data["message"])
        self.assertIn("できたファイルの種類は ARM です", data["notes"][0])

    def test_function_defined_by_the_sdk(self):
        """SDK が決めた関数も、コマンドで使える（SDK を、コマンドと同じシェルで読み込む）。
        SDK が自分のフォルダに移っても、コマンドはビルドするフォルダで動く。"""
        if self.SHELL == "cmd":
            self.skipTest("cmd の SDK が決められるのは、環境変数だけです")
        p = self.project()
        body = self.py("write", "app")
        if self.SHELL == "powershell":
            text = "function build_it { " + body + " }\nSet-Location $PSScriptRoot\n"
            name = "sdk.ps1"
        else:
            text = "build_it() { " + body + "; }\ncd \"$(dirname \"${BASH_SOURCE[0]}\")\"\n"
            name = "sdk.sh"
        sdk = p.write(os.path.join("sdk dir", name), text)
        p.fake(shell=self.SHELL, sdk_env=sdk, build_command="build_it")
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(os.path.isfile(p.path("app")))


@unittest.skipUnless(shell_available("bash"),
                     "bash がありません（Windows では、WSL の bash は使いません）")
class BashScenarioTest(ShellScenarios, unittest.TestCase):
    SHELL = "bash"
    START = 8 if IS_WINDOWS else 2

    def test_sdk_that_fails_when_loaded_again(self):
        """コマンドの前に SDK を読み込み直して失敗したら、SDK の読み込みの失敗として伝える。"""
        p = self.project()
        count = quote_for("bash", p.path("count.txt"))
        sdk = p.write(os.path.join("sdk dir", "sdk.sh"), (
            "n=$(cat {0} 2>/dev/null || echo 0)\n"
            "n=$((n + 1))\n"
            "echo \"$n\" > {0}\n"
            "[ \"$n\" -lt 2 ] || return 3\n").format(count))
        p.fake(shell=self.SHELL, sdk_env=sdk)
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "sdk_env")
        self.assertIn("読み込み直すと、失敗しました（終了コード 3）", data["message"])
        self.assertIn("読み込み直す", read_log(data["log"]))
        self.assertFalse(os.path.exists(p.path("app")))


@unittest.skipUnless(shell_available("cmd"), "cmd は Windows だけです")
class CmdScenarioTest(ShellScenarios, unittest.TestCase):
    SHELL = "cmd"

    def test_batch_file_as_build_command_returns(self):
        """ビルドのコマンドがバッチファイルでも、終わった後に結果を受け取れる。"""
        p = self.project()
        p.write("build.bat", "@echo off\n{}\nexit /b %ERRORLEVEL%\n".format(
            self.py("write", "app")))
        p.fake(shell=self.SHELL, build_command="build.bat", clean_command="build.bat")
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(os.path.isfile(p.path("app")))


@unittest.skipUnless(shell_available("powershell"), "PowerShell（pwsh か powershell）がありません")
class PowershellScenarioTest(ShellScenarios, unittest.TestCase):
    SHELL = "powershell"
    START = 8

    def test_cmdlets_count_as_commands(self):
        p = self.project(build_command="Write-Output 'hello'; " + self.py("write", "app"))
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertIn("hello", read_log(data["log"]))


class PortableRunTest(unittest.TestCase):
    """既定のシェル（auto）で、シェルによらない振る舞いを確かめる。"""

    def test_default_command_is_run(self):
        p = Project(self)
        p.fake()
        code, data, _ = run_script([], cwd=p.dir)
        self.assertEqual(code, 0, data)
        self.assertTrue(os.path.isfile(p.path("app")))

    def test_settings_problem_does_not_build(self):
        p = Project(self)
        code, data, _ = p.run("run")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertTrue(data["created"])
        self.assertEqual(data["config"], p.config)
        self.assertTrue(data["problems"])
        self.assertTrue(any("sdk_env" in n for n in data["notes"]), data["notes"])
        self.assertFalse(os.path.exists(p.path("app")))
        with open(p.path(DATA_DIR, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")

    def test_missing_output_is_reported(self):
        p = Project(self)
        p.fake(outputs=["app", "not_built.bin"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "outputs")
        self.assertIn("not_built.bin", data["message"])
        self.assertIn("できていません", data["message"])
        states = {o["name"]: o["exists"] for o in data["outputs"]}
        self.assertEqual(states, {"app": True, "not_built.bin": False})

    def test_source_dir_and_config_option(self):
        p = Project(self)
        sub = os.path.join("fw", "app dir")
        os.makedirs(p.path(sub))
        conf = p.path("elsewhere.yaml")
        shutil.copyfile(TEMPLATE, conf)
        with open(conf, encoding="utf-8") as f:
            text = f.read()
        text = text.replace('source_dir: "."', "source_dir: " + render(sub))
        text = text.replace('build_command: "make"',
                            "build_command: " + render(py_command(DEFAULT_SHELL, "write", "app")))
        text = text.replace("outputs: []", "outputs:\n  - app\n")
        with open(conf, "w", encoding="utf-8") as f:
            f.write(text)
        code, data, _ = p.run("run", "--config", conf)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["outputs"][0]["path"], p.path(sub, "app"))
        self.assertFalse(os.path.exists(p.config))
        # --config で設定ファイルを別の場所にしても、ログは作業フォルダの方に残る
        self.assertEqual(os.path.dirname(data["log"]), p.path(DATA_DIR, "logs"))
        self.assertFalse(os.path.exists(p.path("logs")), "設定ファイルの横には、ログを作らない")
        # ログのために、設定とログのフォルダを作ったときも、.gitignore を作る
        with open(p.path(DATA_DIR, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")

    def test_unexpected_error_is_logged_in_the_work_folder(self):
        p = Project(self)
        original = BUILD.dispatch
        self.addCleanup(setattr, BUILD, "dispatch", original)

        def boom(argv):
            raise RuntimeError("boom-xyz")

        BUILD.dispatch = boom
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = BUILD.main(["run", "--workdir", p.dir])
        data = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertIn("boom-xyz", data["message"])
        self.assertEqual(os.path.dirname(data["log"]), p.path(DATA_DIR, "logs"))
        self.assertIn("Traceback", read_log(data["log"]))

    def test_two_runs_in_the_same_second_get_two_logs(self):
        p = Project(self)
        p.fake()
        _, a, _ = p.run("run")
        _, b, _ = p.run("run")
        self.assertNotEqual(a["log"], b["log"])
        self.assertTrue(os.path.isfile(a["log"]) and os.path.isfile(b["log"]))

    def test_no_temporary_files_are_left(self):
        p = Project(self)
        p.fake(clean_command=py_command(DEFAULT_SHELL, "remove", "app"))
        private = tempfile.mkdtemp(prefix="cc-tmp-")
        self.addCleanup(shutil.rmtree, private, True)
        env = {k: v for k, v in os.environ.items() if k.upper() != "CC"}
        for name in ("TMPDIR", "TEMP", "TMP"):
            env[name] = private
        code, data, _ = run_script(["check", "--workdir", p.dir], env=env)
        self.assertEqual(code, 0, data)
        code, data, _ = run_script(["run", "--workdir", p.dir], env=env)
        self.assertEqual(code, 0, data)
        self.assertEqual(os.listdir(private), [])


# ---------------------------------------------------------------------------
# PC の make と gcc で、実際にビルドする（Windows 以外）
# ---------------------------------------------------------------------------

@needs_build_tools
class RealBuildTest(unittest.TestCase):

    def test_check_finds_make_and_cc(self):
        p = Project(self)
        p.configure(outputs=["app"])
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["summary"]["build_command"], "make")
        self.assertEqual(data["summary"]["shell_program"], shutil.which("bash"))

    def test_success_reports_outputs_and_log(self):
        p = Project(self)
        p.configure(outputs=["app"])
        code, data, err = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        out = data["outputs"][0]
        self.assertEqual(out["path"], p.path("app"))
        # できたファイルの種類（この PC の CPU）を、メッセージにも書く
        self.assertEqual(out["arch"], BUILD.pc_arch())
        self.assertIn("app（", data["message"])
        self.assertIn("種類：" + out["arch"], data["message"])
        # SDK を読み込んでいないので、PC 用のプログラムだと言う（失敗にはしない）
        self.assertTrue(any("PC 用" in n for n in data["notes"]), data["notes"])
        self.assertIn("機器用ではありません", data["message"])
        self.assertTrue(data["compiler"])
        self.assertEqual(out["size"], os.path.getsize(p.path("app")))
        self.assertRegex(out["mtime"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        if have("file"):
            self.assertIn("ELF", out["file_type"])
        log = data["log"]
        self.assertTrue(os.path.isabs(log))
        self.assertEqual(os.path.dirname(log), p.path(DATA_DIR, "logs"))
        self.assertRegex(os.path.basename(log),
                         r"^" + re.escape(SKILL_NAME) + r"-\d{8}-\d{6}(-\d+)?\.log$")
        text = read_log(log)
        self.assertIn("-o app app.c", text)
        self.assertIn("結果：", text)

    def test_compile_error(self):
        p = Project(self, source=BAD_C)
        p.configure(outputs=["app"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        self.assertEqual(data["failed_step"], "build")
        self.assertNotEqual(data["exit_code"], 0)
        self.assertTrue(any("app.c" in x and "error" in x for x in data["error_lines"]),
                        data["error_lines"])
        self.assertLessEqual(len(data["error_lines"]), 20)
        self.assertTrue(data["tail"])
        self.assertLessEqual(len(data["tail"]), 30)
        self.assertIn("ビルドが失敗しました", data["message"])
        self.assertIn("error", data["message"])
        # 失敗したときは「できたのは PC 用」とは言わない
        self.assertTrue(data["notes"])
        self.assertNotIn("できたのは", data["notes"][0])

    def test_stale_output_is_reported(self):
        p = Project(self)
        p.configure(outputs=["app"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        code, data, _ = p.run("run")  # 何も変えずに、もう一度（make は何もしない）
        self.assertEqual(code, 0, data)
        self.assertEqual(data["stale_outputs"], ["app"])
        self.assertFalse(data["outputs"][0]["updated"])
        self.assertIn("前のビルドのまま", data["message"])
        self.assertIn("clean_command", data["message"])

        p.configure(outputs=["app"], clean_command="make clean")
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["outputs"][0]["updated"])

    def test_sdk_env_is_loaded_before_build(self):
        p = Project(self)
        sdk = p.write("sdk/env", "export CC=gcc\nexport GREETING=from-the-sdk\n")
        p.write("Makefile", MAKEFILE.replace(
            "\t$(CC) -Wall -o app app.c\n",
            "\t@echo \"greeting=$$GREETING\"\n\t$(CC) -Wall -o app app.c\n"))
        p.configure(outputs=["app"], sdk_env=sdk)
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertIn("greeting=from-the-sdk", read_log(data["log"]))
        self.assertEqual(data["notes"], [])
        self.assertEqual(data["compiler"], "gcc")
        self.assertEqual(data["compiler_path"], shutil.which("gcc"))

    def test_sdk_that_exits_in_the_middle(self):
        """SDK の中の exit で、読み込みの途中で終わったら、失敗として伝える。"""
        p = Project(self)
        sdk = p.write("sdk/env", "echo half-way\nexit 0\n")
        p.configure(outputs=["app"], sdk_env=sdk)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("途中で終わってしまいます" in x for x in data["problems"]),
                        data["problems"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "sdk_env")
        self.assertIn("途中で終わってしまいます", data["message"])
        self.assertIn("half-way", data["message"])

    def test_build_script_not_executable(self):
        p = Project(self)
        p.write("build.sh", "#!/bin/sh\nmake\n")  # 実行の権限なし
        p.configure(outputs=["app"], build_command="./build.sh")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("実行の権限", data["message"])

    def test_failure_in_the_middle_of_a_command_list(self):
        p = Project(self)
        p.configure(outputs=["app"], build_command="cd no-such-dir && make")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("no-such-dir", data["message"])
        p.configure(outputs=["app"], clean_command="false && true")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "clean")

    def test_pipe_failure_is_a_failure(self):
        p = Project(self)
        p.configure(outputs=["app"], build_command="make no-such-target | cat")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "build")

    def test_timeout_also_stops_children_that_ignore_term(self):
        p = Project(self)
        sleeper = "sleep 27.{}".format(os.getpid())
        p.configure(outputs=["app"], build_command="(trap '' TERM; exec {}) & echo $! > pid.txt; wait"
                    .format(sleeper), timeout_seconds=1)
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertTrue(data["timed_out"])
        with open(p.path("pid.txt"), encoding="utf-8") as f:
            pid = int(f.read().strip())
        self.assertEqual(wait_until_gone([pid]), [], "TERM を無視する子が残っています")


@unittest.skipUnless(SDK_CC and have("make"),
                     "ARM 向けの SDK（{}）がありません".format(SDK_ENV))
class ArmSdkTest(unittest.TestCase):
    """ARM 向けの SDK で、ARM 向けの実行ファイルを作る。
    コンパイラーの名前は SDK によって違うので、SDK が決める CC と比べる。"""

    def test_check_shows_cross_compiler(self):
        p = Project(self)
        p.configure(outputs=["app"], sdk_env=SDK_ENV)
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertEqual((data["summary"].get("compiler") or "").split()[:1], [SDK_CC])
        self.assertTrue(data["summary"]["compiler_path"])
        self.assertEqual(data["notes"], [])

    def test_build_arm_binary(self):
        p = Project(self)
        p.configure(outputs=["app"], sdk_env=SDK_ENV, clean_command="make clean")
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        out = data["outputs"][0]
        self.assertTrue(out["updated"])
        with open(out["path"], "rb") as f:
            head = f.read(20)
        self.assertEqual(head[:4], b"\x7fELF")
        self.assertEqual(head[4], 1, "32 ビットの ELF のはず")
        self.assertEqual(int.from_bytes(head[18:20], "little"), 40, "ARM（EM_ARM=40）のはず")
        if have("file"):
            self.assertIn("ARM", out["file_type"])
        self.assertEqual(out["arch"], "ARM")
        self.assertIn("種類：ARM", data["message"])
        self.assertEqual(data["notes"], [])
        self.assertEqual((data["compiler"] or "").split()[:1], [SDK_CC])
        self.assertIn(SDK_CC, read_log(data["log"]))


# ---------------------------------------------------------------------------
# シェルに渡すスクリプト（Windows がなくても確かめられること）
# ---------------------------------------------------------------------------

class WindowsBashTest(unittest.TestCase):
    """Windows の bash の探し方（WSL を起動する bash.exe は使わない）。Windows がなくても確かめる。"""

    ENV = {"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files"}
    WSL = r"C:\WINDOWS\system32\bash.exe"
    GIT = r"C:\Program Files\Git\bin\bash.exe"

    def find(self, path, files):
        env = dict(self.ENV, PATH=path)
        return BUILD.find_windows_bash(env, isfile=set(files).__contains__)

    def test_wsl_bash_is_skipped_and_git_bash_is_used(self):
        # Git for Windows は、ふつう Git\cmd だけを PATH に入れる
        found = self.find(r"C:\WINDOWS\system32;C:\Program Files\Git\cmd", [self.WSL, self.GIT])
        self.assertEqual(found, (self.GIT, self.WSL))

    def test_bash_on_path_comes_first(self):
        msys = r"C:\msys64\usr\bin\bash.exe"
        found = self.find(r"C:\WINDOWS\system32;C:\msys64\usr\bin", [self.WSL, msys, self.GIT])
        self.assertEqual(found, (msys, self.WSL))

    def test_only_wsl_bash(self):
        apps = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps"
        found = self.find(apps, [apps + r"\bash.exe"])
        self.assertEqual(found, (None, apps + r"\bash.exe"))
        found = self.find(r"C:\WINDOWS\system32", [self.WSL])
        self.assertEqual(found, (None, self.WSL))
        message = BUILD.shell_missing_message("bash", windows=True, wsl=self.WSL)
        for word in (self.WSL, "WSL の bash なので、使えません", "Git for Windows", "wsl で始まる"):
            self.assertIn(word, message)
        message = BUILD.shell_missing_message("bash", windows=True)
        self.assertIn("Git for Windows", message)


class ShellScriptTest(unittest.TestCase):

    def test_auto_is_cmd_on_windows_and_bash_elsewhere(self):
        self.assertEqual(BUILD.resolve_shell("auto", windows=True), "cmd")
        self.assertEqual(BUILD.resolve_shell("auto", windows=False), "bash")
        for name in ("cmd", "powershell", "bash"):
            self.assertEqual(BUILD.resolve_shell(name, windows=True), name)

    def test_cmd_script_is_ascii_and_takes_values_from_the_environment(self):
        BUILD.CMD_LOAD.encode("ascii")
        self.assertIn('call "%BUILD_SKILL_SDK_ENV%"', BUILD.CMD_LOAD)
        self.assertTrue(BUILD.CMD_LOAD.endswith("\r\n"))
        self.assertNotIn("\n", BUILD.CMD_LOAD.replace("\r\n", ""))
        # Python のコードは cmd の「"…"」の中に置く。cmd が特別に扱う文字を使わない
        for c in '"%^&|<>!':
            self.assertNotIn(c, BUILD.DUMP_ENV)

    def test_powershell_scripts(self):
        for text in (BUILD.PS_LOAD, BUILD.PS_RUN):
            text.encode("ascii")
            # -Command の引数で渡すので、コマンドラインで崩れる「"」と「\」を使わない
            self.assertNotIn('"', text)
            self.assertNotIn("\\", text)
        self.assertIn(". $env:BUILD_SKILL_SDK_ENV", BUILD.PS_LOAD)
        # コマンドの前に、同じシェルで SDK を読み込み直す（SDK の関数も使える）
        self.assertIn(". $env:BUILD_SKILL_SDK_ENV", BUILD.PS_RUN)
        self.assertLess(BUILD.PS_RUN.index("BUILD_SKILL_SDK_ENV"),
                        BUILD.PS_RUN.index("Invoke-Expression ($env:BUILD_SKILL_COMMAND"))
        # スクリプトはファイルにしない（実行ポリシーは、-Command の文字にはかからない）
        self.assertEqual(BUILD.PS_OPTIONS[-1], "-Command")
        self.assertNotIn("-File", BUILD.PS_OPTIONS)
        self.assertIn("-ExecutionPolicy", BUILD.PS_OPTIONS)
        s = {"shell": "powershell", "shell_program": "pwsh"}
        self.assertEqual(BUILD.shell_argv(s, ps_script=BUILD.PS_RUN)[-1], BUILD.PS_RUN)

    def test_bash_scripts_load_the_sdk_in_the_same_shell(self):
        self.assertIn('source "$BUILD_SKILL_SDK_ENV"', BUILD.BASH_LOAD)
        self.assertIn("command -v", BUILD.BASH_LOAD)
        self.assertIn('source "$BUILD_SKILL_SDK_ENV"', BUILD.BASH_RUN)
        self.assertTrue(BUILD.BASH_RUN.endswith("set -e\nset -o pipefail\n"))

    def test_cmd_command_line(self):
        s = {"shell": "cmd", "shell_program": r"C:\Windows\system32\cmd.exe"}
        self.assertEqual(BUILD.shell_argv(s, command='"C:\\x y\\make.exe" -j4 && echo ok'),
                         '"C:\\Windows\\system32\\cmd.exe" /d /s /c ""C:\\x y\\make.exe" -j4 && echo ok"')
        self.assertEqual(BUILD.shell_argv(s, r"C:\t\load.cmd"),
                         '"C:\\Windows\\system32\\cmd.exe" /d /s /c ""C:\\t\\load.cmd""')

    def test_dump_env_code_writes_the_environment(self):
        tmp = tempfile.mkdtemp(prefix="cc-dump-")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "env.json")
        env = dict(os.environ, BUILD_SKILL_FOUND="1 make\n0 no-such", MY_VALUE="日本語")
        subprocess.run([sys.executable, "-I", "-c", BUILD.DUMP_ENV, out], env=env, check=True)
        loaded, found = BUILD.read_env_file(out)
        self.assertEqual(loaded["MY_VALUE"], "日本語")
        self.assertNotIn("BUILD_SKILL_FOUND", loaded)
        self.assertEqual(found, {"make": True, "no-such": False})


# ---------------------------------------------------------------------------
# このフォルダだけで使えること・説明の文書
# ---------------------------------------------------------------------------

def text_files(top):
    """top の中のテキストのファイル（UTF-8 で読めるもの）の (場所, 中身)。"""
    for root, dirs, files in os.walk(top):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as f:
                    yield path, f.read()
            except (OSError, UnicodeDecodeError):
                continue


def all_files(top):
    found = []
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        found += [os.path.relpath(os.path.join(root, n), top) for n in files]
    return sorted(found)


def is_stdlib(name):
    """標準ライブラリのモジュールか。Python 3.10 からは sys.stdlib_module_names で、
    それより前は、組み込みか、標準ライブラリのフォルダ（site-packages の外）にあるかで決める。"""
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return name in names
    if name in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    paths = sysconfig.get_paths()
    origin = os.path.normcase(os.path.realpath(spec.origin))
    if "site-packages" in origin.split(os.sep) or "dist-packages" in origin.split(os.sep):
        return False
    for key in ("stdlib", "platstdlib"):
        root = os.path.normcase(os.path.realpath(paths[key]))
        if origin.startswith(root + os.sep):
            return True
    # Windows の拡張モジュール（DLLs のフォルダ）など
    root = os.path.normcase(os.path.realpath(sys.base_prefix))
    return origin.startswith(root + os.sep)


class SelfContainedTest(unittest.TestCase):

    def test_no_path_goes_out_of_the_folder(self):
        """どのファイルも、このフォルダの外を指さない。"""
        ups = (os.pardir + "/", os.pardir + "\\")
        found = []
        for path, text in text_files(SKILL_DIR):
            for n, line in enumerate(text.splitlines(), 1):
                if any(up in line for up in ups):
                    found.append("{}:{}: {}".format(os.path.relpath(path, SKILL_DIR), n,
                                                    line.strip()))
        self.assertEqual(found, [])

    def test_only_standard_library_imports(self):
        for script in (SCRIPT, FAKE):
            with open(script, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, "相対 import は使わない")
                    names.add(node.module.split(".")[0])
            self.assertEqual(sorted(n for n in names if not is_stdlib(n)), [], script)

    def test_stdout_is_written_as_text(self):
        """標準出力にバイト列を直接書かない（日本語の Windows では文字化けする）。"""
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("stdout.buffer", text)
        self.assertIn('reconfigure(errors="backslashreplace")', text)


class CopiedAloneTest(unittest.TestCase):
    """このフォルダだけを空の場所にコピーして使うと、データは「.<コピーしたフォルダの名前>」にできる。"""

    def copy_skill(self, name):
        place = tempfile.mkdtemp(prefix="cc-alone-")
        self.addCleanup(shutil.rmtree, place, True)
        dest = os.path.join(place, name)
        shutil.copytree(SKILL_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
        return place, dest

    def empty_workdir(self):
        work = tempfile.mkdtemp(prefix="cc-work-")
        self.addCleanup(shutil.rmtree, work, True)
        return os.path.realpath(work)

    def check_copy(self, name):
        place, dest = self.copy_skill(name)
        files_before = all_files(dest)
        script = os.path.join(dest, "scripts", "build.py")
        for command in ("config-path", "check"):
            work = self.empty_workdir()
            code, data, _ = run_script([command], cwd=work, script=script)
            self.assertEqual(code, 0 if command == "config-path" else 2, data)
            self.assertEqual(data["skill"], name)
            data_dir = os.path.join(work, "." + name)
            self.assertEqual(os.path.realpath(data["config"]),
                             os.path.join(data_dir, "config.yaml"))
            if command == "check":
                # check は、設定ファイルが無くても作らない。作業フォルダは空のまま
                self.assertFalse(data["created"], data)
                self.assertEqual(os.listdir(work), [], command)
                continue
            self.assertTrue(data["created"], data)
            # 作業フォルダにできるのは「.<コピーしたフォルダの名前>」だけ。ほかの点のフォルダはできない
            self.assertEqual(os.listdir(work), ["." + name], command)
            self.assertEqual(sorted(os.listdir(data_dir)), [".gitignore", "config.yaml"], command)
            with open(os.path.join(data_dir, ".gitignore"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "*\n")
            with open(os.path.join(dest, "config-template.yaml"), encoding="utf-8") as a, \
                    open(data["config"], encoding="utf-8") as b:
                self.assertEqual(a.read(), b.read())
        # コピーしたフォルダと、その置き場所には、何も増えない
        self.assertEqual(os.listdir(place), [name])
        self.assertEqual(all_files(dest), files_before)
        return script

    def test_copied_with_the_same_name(self):
        self.check_copy(SKILL_NAME)

    def test_copied_with_another_name(self):
        self.check_copy("my-build")

    def test_logs_of_a_renamed_copy(self):
        script = self.check_copy("my-build")
        p = Project(self)
        code, data, _ = run_script(["config-path"], cwd=p.dir, script=script)
        config = data["config"]
        with open(config, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("outputs: []", 'outputs: ["app"]')
        text = text.replace('build_command: "make"',
                            "build_command: " + render(py_command(DEFAULT_SHELL, "write", "app")))
        with open(config, "w", encoding="utf-8") as f:
            f.write(text)
        code, data, _ = run_script(["run"], cwd=p.dir, script=script)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["skill"], "my-build")
        self.assertEqual(os.path.dirname(data["log"]), p.path(".my-build", "logs"))
        self.assertRegex(os.path.basename(data["log"]), r"^my-build-\d{8}-\d{6}(-\d+)?\.log$")
        self.assertTrue(read_log(data["log"]).startswith("my-build のログ"))
        self.assertEqual(sorted(n for n in os.listdir(p.dir) if n.startswith(".")), [".my-build"])


class SkillDocTest(unittest.TestCase):
    """SKILL.md の形を確かめる。文言は確かめない（決まった文を固定すると、テストに合わせた指示が増えるため）。"""

    @property
    def text(self):
        with open(SKILL_MD, encoding="utf-8") as f:
            return f.read()

    def test_frontmatter(self):
        m = re.match(r"^---\n(.*?)\n---\n", self.text, re.S)
        self.assertIsNotNone(m, "frontmatter がありません")
        front = m.group(1)
        # Copilot CLI はスキルを name の名前で呼ぶ。フォルダの名前と同じにする
        self.assertRegex(front, r"(?m)^name: " + re.escape(SKILL_NAME) + r"$")
        desc = re.search(r"(?m)^description: (.+)$", front)
        self.assertIsNotNone(desc)
        self.assertLessEqual(len(desc.group(1)), 1024)

    def test_commands_use_own_folder_and_python_placeholder(self):
        self.assertIn("`<python>`", self.text)
        self.assertIn("py -3", self.text)
        for sub in ("run", "config-path", "check"):
            self.assertIn("<python> <このスキルのフォルダ>/scripts/build.py " + sub, self.text)
        self.assertNotIn("python3 <このスキルのフォルダ>", self.text)

    def test_commands_do_not_depend_on_bash(self):
        """Copilot CLI は、Windows では PowerShell でコマンドを動かす。bash だけの書き方を使わない。"""
        for word in ("<<", "source ", "$(", "&&"):
            self.assertNotIn(word, self.text)



class ConsoleCodePageTest(unittest.TestCase):
    """Windows で、出力先がコンソールでないときは、コンソールの文字コードで書く。"""

    def _stream(self, tty):
        class Buffer(io.BytesIO):
            def isatty(self):
                return tty
        return io.TextIOWrapper(Buffer(), encoding="utf-8")

    def test_streams_follow_the_console_on_windows(self):
        m = BUILD
        for console, tty, expected in (("cp932", False, "cp932"), ("cp932", True, "utf-8"),
                                       (None, False, "utf-8")):
            out, err = self._stream(tty), self._stream(tty)
            with mock.patch.object(m, "console_code_page", return_value=console), \
                    mock.patch.object(m.sys, "stdout", out), mock.patch.object(m.sys, "stderr", err):
                m.setup_streams()
            for s in (out, err):
                self.assertEqual(codecs.lookup(s.encoding).name, expected, (console, tty))
                self.assertEqual(s.errors, "backslashreplace")

    def test_no_console_outside_windows(self):
        if os.name == "nt":
            self.skipTest("Windows では、コンソールの文字コードを返す")
        self.assertIsNone(BUILD.console_code_page())


if __name__ == "__main__":
    unittest.main()
