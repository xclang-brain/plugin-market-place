# -*- coding: utf-8 -*-
"""cross-compile スキル（scripts/build.py）の試験。

PC の gcc と make で、小さなプロジェクトを実際にビルドして確かめる。
SDK の環境設定ファイル（Yocto を模したもの）があれば、ARM 向けのビルドも試す。

実行：
  cd plugins/embedded-debug-loop
  python3 -m unittest discover -s tests/skills/cross-compile -t . -v
"""

import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SKILL_DIR = os.path.join(PLUGIN, "skills", "cross-compile")
SCRIPT = os.path.join(SKILL_DIR, "scripts", "build.py")
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
SDK_ENV = "/opt/poky/4.0/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi"

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

TEMPLATE_KEYS = ["source_dir", "sdk_env", "build_command", "clean_command",
                 "outputs", "timeout_seconds"]


def load_module():
    spec = importlib.util.spec_from_file_location("cross_compile_build_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BUILD = load_module()


def have(cmd):
    return shutil.which(cmd) is not None


def sdk_compiler_ready():
    if not os.path.isfile(SDK_ENV) or not have("bash"):
        return False
    r = subprocess.run(
        ["bash", "-c", '. "$1" >/dev/null 2>&1 || exit 1; set -- $CC; command -v "$1"',
         "x", SDK_ENV],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.returncode == 0 and r.stdout.strip() != b""


def render(value):
    return json.dumps(value, ensure_ascii=False)


class Project(object):
    """作業フォルダ（一時フォルダ）と、小さな C のプロジェクト。"""

    def __init__(self, testcase, source=GOOD_C):
        self.dir = tempfile.mkdtemp(prefix="cc-test-")
        testcase.addCleanup(shutil.rmtree, self.dir, True)
        self.write("Makefile", MAKEFILE)
        self.write("app.c", source)
        self.config = os.path.join(self.dir, ".embedded-debug-loop", "cross-compile.yaml")

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
        """ひな形を写し、指定した項目の行だけを書き換える（コメントは残す）。"""
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

    def run(self, *args, **kw):
        return run_script(list(args) + ["--workdir", self.dir], **kw)


def run_script(args, cwd=None, timeout=120, env=None):
    if env is None:
        # 試験を動かす端末の CC に左右されないよう、外しておく
        env = {k: v for k, v in os.environ.items() if k != "CC"}
    r = subprocess.run([sys.executable, SCRIPT] + args, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout, env=env)
    out = r.stdout.decode("utf-8")
    try:
        data = json.loads(out)
    except ValueError:
        raise AssertionError("標準出力が JSON 1つになっていません：\n" + out)
    if not isinstance(data, dict):
        raise AssertionError("JSON がオブジェクトではありません：" + out)
    return r.returncode, data, r.stderr.decode("utf-8", "replace")


def process_alive(token):
    """コマンドラインに token を含むプロセスがあるか（/proc を見る）。"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/{}/cmdline".format(pid), "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if token in cmd:
            return True
    return False


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
        self.assertIn("cross-compile", header[0])

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
        self.assertIn("機器向けにビルドするなら、必ず書く", block)
        self.assertIn("PC 用", block)
        self.assertNotIn("必須", block, "必須の印を付けると、空のときにビルドできなくなる")

    def test_clean_command_comment_says_the_old_build_stays(self):
        """空のままだと「前のビルドのまま」になることを、コメントだけで分かるようにする。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        block = re.search(r"((?:^#.*\n)+)clean_command:", text, re.M).group(1)
        self.assertIn("作り直されず", block)
        self.assertIn("前のビルドでできたファイルがそのまま残る", block)
        self.assertIn("make clean", block)

    def test_template_examples_are_generic(self):
        """例が試験のプロジェクトの名前だと、写すだけで埋まってしまう。"""
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("sensor", text)
        self.assertIn('# 例：["my_app"]', text)

    def test_template_comments_do_not_point_by_position(self):
        """コメントは、設定の対話でそのまま質問の文になる。「下の」などはチャットでは通じない。"""
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
            "source_dir": ".", "sdk_env": "", "build_command": "make",
            "clean_command": "", "outputs": [], "timeout_seconds": 600})

    def test_template_lines_are_short_enough(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.startswith("# 例："):
                    continue
                self.assertLessEqual(len(line), 70, line)


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

    def test_first_word(self):
        self.assertEqual(BUILD.first_word("make -j4 all"), "make")
        self.assertEqual(BUILD.first_word("CC=gcc V=1 make"), "make")
        self.assertEqual(BUILD.first_word("(cd sub && make)"), "cd")
        self.assertEqual(BUILD.first_word("./build.sh --release"), "./build.sh")
        with self.assertRaises(ValueError):
            BUILD.first_word("make 'oops")


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
        self.assertNotIn("# cross-compile スキルの設定ファイルです。", blocks[0][1])

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

    def test_check_and_run_also_add(self):
        p = Project(self)
        text = template_without("clean_command").replace('outputs: []', 'outputs: ["app"]')
        self.old_config(p, text)
        code, data, _ = p.run("check")
        self.assertEqual(data["added"], ["clean_command"])
        self.assertIn("新しい項目 clean_command を足しました", data["message"])

        p2 = Project(self)
        self.old_config(p2, text)
        code, data, _ = p2.run("run")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["added"], ["clean_command"])
        self.assertIn("新しい項目 clean_command を足しました", data["message"])
        code, data, _ = p2.run("run")
        self.assertEqual(data["added"], [])

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
        self.assertEqual(data["added"], ["source_dir", "sdk_env", "build_command",
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
        with open(p.path(".embedded-debug-loop", ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")

    def test_existing_gitignore_is_kept(self):
        p = Project(self)
        p.write(".embedded-debug-loop/.gitignore", "logs/\n")
        p.run("config-path")
        with open(p.path(".embedded-debug-loop", ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "logs/\n")

    def test_existing_folder_without_gitignore_gets_one(self):
        """ほかのものが先にフォルダを作っても、.gitignore が無ければ作る。"""
        p = Project(self)
        os.makedirs(p.path(".embedded-debug-loop"))
        p.run("config-path")
        self.assertTrue(os.path.isfile(p.config))
        with open(p.path(".embedded-debug-loop", ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")

    def test_gitignore_is_made_again_when_removed(self):
        p = Project(self)
        p.configure(outputs=["app"])
        for command in ("config-path", "check", "run"):
            gi = p.path(".embedded-debug-loop", ".gitignore")
            if os.path.exists(gi):
                os.unlink(gi)
            p.run(command)
            with open(gi, encoding="utf-8") as f:
                self.assertEqual(f.read(), "*\n", command)

    def test_gitignore_in_work_folder_with_config_elsewhere(self):
        p = Project(self)
        os.makedirs(p.path(".embedded-debug-loop"))
        p.run("config-path", "--config", p.path("conf", "my-build.yaml"))
        self.assertTrue(os.path.isfile(p.path(".embedded-debug-loop", ".gitignore")))

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
        code, data, _ = run_script(["config-path", "--workdir", "/no/such/folder/xyz"])
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertIn("/no/such/folder/xyz", data["message"])

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
# check
# ---------------------------------------------------------------------------

class CheckTest(unittest.TestCase):

    def test_ok(self):
        p = Project(self)
        p.configure(outputs=["app"])
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["problems"], [])
        self.assertEqual(data["summary"]["outputs"], ["app"])
        self.assertEqual(data["summary"]["build_command"], "make")
        self.assertEqual(os.path.realpath(data["summary"]["source_dir"]),
                         os.path.realpath(p.dir))
        self.assertFalse(os.path.exists(p.path("app")), "check でビルドしてはいけない")

    def test_empty_sdk_env_is_noted_not_failed(self):
        """sdk_env が空でも失敗にはしないが、PC 用のプログラムになると、はっきり言う。"""
        p = Project(self)
        p.configure(outputs=["app"])
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["notes"]), 1)
        note = data["notes"][0]
        for word in ("sdk_env", "SDK を読み込みません", "PC のコンパイラー", "PC 用", "機器用ではありません"):
            self.assertIn(word, note)
        self.assertIn(note, data["message"])
        self.assertEqual(data["summary"]["compiler"], "cc")
        if have("cc"):
            self.assertEqual(data["summary"]["compiler_path"], shutil.which("cc"))
            self.assertIn(shutil.which("cc"), note)

    def test_compiler_from_environment_is_named(self):
        p = Project(self)
        p.configure(outputs=["app"])
        code, data, _ = run_script(["check", "--workdir", p.dir],
                                   env=dict(os.environ, CC="gcc -O2"))
        self.assertEqual(code, 0, data)
        self.assertEqual(data["summary"]["compiler"], "gcc -O2")
        note = data["notes"][0]
        self.assertIn("環境変数 CC", note)
        self.assertNotIn("PC 用", note, "CC が決まっているときは、PC 用とは言い切らない")

    def test_sdk_env_set_means_no_note(self):
        p = Project(self)
        sdk = p.write("sdk/env", "export CC=gcc\n")
        p.configure(outputs=["app"], sdk_env=sdk)
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["notes"], [])
        self.assertEqual(data["summary"]["compiler"], "gcc")
        self.assertNotIn("PC 用", data["message"])

    def test_new_config_reports_required_item(self):
        p = Project(self)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertTrue(data["created"])
        self.assertEqual(data["config"], p.config)
        self.assertTrue(any("outputs" in x and "必須" in x for x in data["problems"]),
                        data["problems"])
        self.assertTrue(any("sdk_env" in x for x in data["notes"]), data["notes"])

    def test_missing_sdk_env(self):
        p = Project(self)
        p.configure(outputs=["app"], sdk_env="/no/such/sdk/environment-file")
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertTrue(any("/no/such/sdk/environment-file" in x and "SDK" in x
                            for x in data["problems"]), data["problems"])

    def test_missing_source_dir(self):
        p = Project(self)
        p.configure(outputs=["app"], source_dir="no-such-dir")
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any(p.path("no-such-dir") in x and "source_dir" in x
                            for x in data["problems"]), data["problems"])

    def test_build_command_not_found(self):
        p = Project(self)
        p.configure(outputs=["app"], build_command="no-such-build-tool-xyz all")
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("no-such-build-tool-xyz" in x and "見つかりません" in x
                            for x in data["problems"]), data["problems"])
        # PC の準備の問題なので、メッセージはその中身を言う
        self.assertIn("no-such-build-tool-xyz", data["message"])

    def test_sdk_that_fails_to_load(self):
        p = Project(self)
        sdk = p.write("sdk/env-broken", BROKEN_SDK)
        p.configure(outputs=["app"], sdk_env=sdk)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        # SDK のメッセージは、最初の行（大事な行）を見せる
        self.assertTrue(any("読み込みが失敗" in x and "misconfigured" in x
                            for x in data["problems"]), data["problems"])
        self.assertIn("misconfigured", data["message"])

    def test_sdk_compiler_missing(self):
        p = Project(self)
        sdk = p.write("sdk/env-nocc", 'export CC="no-such-cross-gcc-xyz -O2"\n')
        p.configure(outputs=["app"], sdk_env=sdk)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("no-such-cross-gcc-xyz" in x for x in data["problems"]),
                        data["problems"])

    def test_empty_outputs_problem_is_one_sentence_with_a_generic_example(self):
        p = Project(self)
        p.configure(outputs=[])
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        problem = [x for x in data["problems"] if "outputs" in x][0]
        self.assertTrue(problem.endswith("。"), problem)
        self.assertIn("my_app", problem)
        self.assertNotIn("sensor", problem)

    def test_bad_values(self):
        p = Project(self)
        p.configure(outputs="app", timeout_seconds="abc", build_command=["make"])
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        text = "\n".join(data["problems"])
        self.assertIn("timeout_seconds", text)
        self.assertIn("build_command", text)
        self.assertNotIn("outputs", text, "1つの文字はファイル1つとして受け付ける")
        self.assertIn("2 件", data["message"])

    def test_timeout_below_one_second(self):
        p = Project(self)
        p.configure(outputs=["app"], timeout_seconds=0.5)
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("timeout_seconds" in x for x in data["problems"]), data["problems"])

    def test_broken_yaml_names_line(self):
        p = Project(self)
        p.write(".embedded-debug-loop/cross-compile.yaml", "outputs: [\"app\"\n")
        code, data, _ = p.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("1 行目" in x for x in data["problems"]), data["problems"])


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

@unittest.skipUnless(have("make") and have("gcc") and have("bash"), "make・gcc・bash が要ります")
class RunTest(unittest.TestCase):

    def test_success_reports_outputs_and_log(self):
        p = Project(self)
        p.configure(outputs=["app"])
        code, data, err = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["skill"], "cross-compile")
        self.assertIn("成功", data["message"])
        self.assertIn("ビルドを始めます", err)
        out = data["outputs"][0]
        self.assertEqual(out["path"], p.path("app"))
        self.assertTrue(out["exists"])
        self.assertTrue(out["updated"])
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
        self.assertEqual(os.path.dirname(log), p.path(".embedded-debug-loop", "logs"))
        self.assertRegex(os.path.basename(log), r"^cross-compile-\d{8}-\d{6}(-\d+)?\.log$")
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("-o app app.c", text)
        self.assertIn("結果：", text)

    def test_default_command_is_run(self):
        p = Project(self)
        p.configure(outputs=["app"])
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
        with open(p.path(".embedded-debug-loop", ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")

    def test_compile_error(self):
        p = Project(self, source=BAD_C)
        p.configure(outputs=["app"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        self.assertEqual(data["failed_step"], "build")
        self.assertNotEqual(data["exit_code"], 0)
        self.assertTrue(data["error_lines"])
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
        self.assertTrue(data["ok"])
        self.assertEqual(data["stale_outputs"], ["app"])
        self.assertFalse(data["outputs"][0]["updated"])
        self.assertIn("前のビルドのまま", data["message"])
        self.assertIn("clean_command", data["message"])
        self.assertIn("種類：", data["message"])

        p.configure(outputs=["app"], clean_command="make clean")
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["outputs"][0]["updated"])

    def test_missing_output_is_reported(self):
        p = Project(self)
        p.configure(outputs=["app", "not_built.bin"])
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "outputs")
        self.assertIn("not_built.bin", data["message"])
        self.assertIn("できていません", data["message"])
        states = {o["name"]: o["exists"] for o in data["outputs"]}
        self.assertEqual(states, {"app": True, "not_built.bin": False})

    def test_timeout_stops_the_whole_build(self):
        p = Project(self)
        # 同時に走るほかの試験と区別できるよう、秒数に自分のプロセス番号を入れる
        sleeper = "sleep 31.{}".format(os.getpid())
        p.configure(outputs=["app"], build_command=sleeper + " && make", timeout_seconds=1)
        t0 = time.time()
        code, data, _ = p.run("run")
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        self.assertTrue(data["timed_out"])
        self.assertIn("1 秒", data["message"])
        self.assertIn("止めました", data["message"])
        time.sleep(0.3)
        self.assertFalse(process_alive(sleeper), "ビルドの子プロセスが残っています")

    def test_sdk_env_is_loaded_before_build(self):
        p = Project(self)
        sdk = p.write("sdk/env", "export CC=gcc\nexport GREETING=from-the-sdk\n")
        p.write("Makefile", MAKEFILE.replace(
            "\t$(CC) -Wall -o app app.c\n",
            "\t@echo \"greeting=$$GREETING\"\n\t$(CC) -Wall -o app app.c\n"))
        p.configure(outputs=["app"], sdk_env=sdk)
        code, data, _ = p.run("run")
        self.assertEqual(code, 0, data)
        with open(data["log"], encoding="utf-8") as f:
            self.assertIn("greeting=from-the-sdk", f.read())
        self.assertEqual(data["notes"], [])
        self.assertEqual(data["compiler"], "gcc")
        self.assertNotIn("PC 用", data["message"])

    def test_sdk_env_failure(self):
        p = Project(self)
        sdk = p.write("sdk/env-broken", BROKEN_SDK)
        p.configure(outputs=["app"], sdk_env=sdk)
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "sdk_env")
        self.assertIn("misconfigured", data["message"])
        self.assertTrue(any("misconfigured" in x for x in data["tail"]))
        self.assertFalse(os.path.exists(p.path("app")))

    def test_clean_failure(self):
        p = Project(self)
        p.configure(outputs=["app"], clean_command="false")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "clean")
        self.assertIn("片付けのコマンド", data["message"])
        self.assertFalse(os.path.exists(p.path("app")))

    def test_build_command_not_found(self):
        p = Project(self)
        p.configure(outputs=["app"], build_command="no-such-build-tool-xyz")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("no-such-build-tool-xyz", data["message"])
        self.assertIn("見つかりません", data["message"])

    def test_build_script_not_executable(self):
        p = Project(self)
        p.write("build.sh", "#!/bin/sh\nmake\n")  # 実行の権限なし
        p.configure(outputs=["app"], build_command="./build.sh")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertIn("実行の権限", data["message"])

    def test_failure_in_the_middle_of_a_command_list(self):
        """「cd x && make」の cd の失敗でも、ビルドの失敗として止まる。"""
        p = Project(self)
        p.configure(outputs=["app"], build_command="cd no-such-dir && make")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "build")
        self.assertNotEqual(data["exit_code"], 0)
        self.assertIn("no-such-dir", data["message"])
        self.assertFalse(os.path.exists(p.path("app")))

        p.configure(outputs=["app"], clean_command="false && true")
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertEqual(data["failed_step"], "clean")

    def test_timeout_also_stops_children_that_ignore_term(self):
        p = Project(self)
        sleeper = "sleep 27.{}".format(os.getpid())
        p.configure(outputs=["app"], build_command="(trap '' TERM; exec {})".format(sleeper),
                    timeout_seconds=1)
        code, data, _ = p.run("run")
        self.assertEqual(code, 1, data)
        self.assertTrue(data["timed_out"])
        time.sleep(0.3)
        self.assertFalse(process_alive(sleeper), "TERM を無視する子が残っています")

    def test_source_dir_and_config_option(self):
        p = Project(self)
        sub = os.path.join("fw", "app dir")
        p.write(os.path.join(sub, "Makefile"), MAKEFILE)
        p.write(os.path.join(sub, "app.c"), GOOD_C)
        conf = p.path("elsewhere.yaml")
        shutil.copyfile(TEMPLATE, conf)
        with open(conf, encoding="utf-8") as f:
            text = f.read()
        text = text.replace('source_dir: "."', "source_dir: " + render(sub))
        text = text.replace("outputs: []", "outputs:\n  - app\n")
        with open(conf, "w", encoding="utf-8") as f:
            f.write(text)
        code, data, _ = p.run("run", "--config", conf)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["outputs"][0]["path"], p.path(sub, "app"))
        self.assertFalse(os.path.exists(p.config))
        # ログのために .embedded-debug-loop を作ったときも、.gitignore を作る
        with open(p.path(".embedded-debug-loop", ".gitignore"), encoding="utf-8") as f:
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
        self.assertEqual(os.path.dirname(data["log"]), p.path(".embedded-debug-loop", "logs"))
        with open(data["log"], encoding="utf-8") as f:
            self.assertIn("Traceback", f.read())

    def test_two_runs_in_the_same_second_get_two_logs(self):
        p = Project(self)
        p.configure(outputs=["app"], clean_command="make clean")
        _, a, _ = p.run("run")
        _, b, _ = p.run("run")
        self.assertNotEqual(a["log"], b["log"])
        self.assertTrue(os.path.isfile(a["log"]) and os.path.isfile(b["log"]))


@unittest.skipUnless(sdk_compiler_ready() and have("make"),
                     "ARM 向けの SDK（{}）がありません".format(SDK_ENV))
class ArmSdkTest(unittest.TestCase):
    """Yocto を模した SDK で、ARM 向けの実行ファイルを作る。"""

    def test_check_shows_cross_compiler(self):
        p = Project(self)
        p.configure(outputs=["app"], sdk_env=SDK_ENV)
        code, data, _ = p.run("check")
        self.assertEqual(code, 0, data)
        self.assertIn("arm-linux-gnueabihf-gcc", data["summary"].get("compiler", ""))
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
        self.assertIn("arm-linux-gnueabihf-gcc", data["compiler"])
        with open(data["log"], encoding="utf-8") as f:
            self.assertIn("arm-linux-gnueabihf-gcc", f.read())


# ---------------------------------------------------------------------------
# 独立していること・説明の文書
# ---------------------------------------------------------------------------

FORBIDDEN = [
    (r"(?<![\w-])setup(?![\w-])", "ほかのスキルの名前 setup"),
    (r"deploy", "ほかのスキルの名前 deploy"),
    (r"デプロイ", "ほかのスキルの役目"),
    (r"log-capture|log_capture", "ほかのスキルの名前 log-capture"),
    (r"device-worker", "エージェントの名前"),
    (r"orchestrator", "エージェントの名前"),
    (r"verifier", "エージェントの名前"),
    (r"エージェント", "エージェント"),
    (r"統括", "統括"),
    (r"Web管理画面", "Web管理画面"),
    (r"extension|エクステンション|拡張機能", "エクステンション"),
]


class IndependenceTest(unittest.TestCase):

    def skill_files(self):
        for root, _, files in os.walk(SKILL_DIR):
            if "__pycache__" in root:
                continue
            for name in files:
                yield os.path.join(root, name)

    def test_no_mention_of_other_parts(self):
        found = []
        for path in self.skill_files():
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    for pattern, what in FORBIDDEN:
                        if re.search(pattern, line, re.IGNORECASE):
                            found.append("{}:{}: {}（{}）".format(
                                os.path.relpath(path, SKILL_DIR), n, line.strip(), what))
        self.assertEqual(found, [])

    def test_only_standard_library_imports(self):
        with open(SCRIPT, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "相対 import は使わない")
                names.add(node.module.split(".")[0])
        stdlib = getattr(sys, "stdlib_module_names", None)
        if stdlib is None:
            stdlib = {"datetime", "json", "os", "re", "shlex", "shutil", "signal",
                      "subprocess", "sys", "tempfile", "time", "traceback"}
        self.assertEqual(sorted(n for n in names if n not in stdlib), [])

    def test_works_when_copied_alone(self):
        """スキルのフォルダだけを別の場所に写しても動く。"""
        tmp = tempfile.mkdtemp(prefix="cc-alone-")
        self.addCleanup(shutil.rmtree, tmp, True)
        alone = os.path.join(tmp, "only-this-skill")
        shutil.copytree(SKILL_DIR, alone, ignore=shutil.ignore_patterns("__pycache__"))
        p = Project(self)
        r = subprocess.run(
            [sys.executable, os.path.join(alone, "scripts", "build.py"), "config-path",
             "--workdir", p.dir], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout.decode("utf-8"))["created"])


class SkillDocTest(unittest.TestCase):

    def setUp(self):
        with open(SKILL_MD, encoding="utf-8") as f:
            self.text = f.read()

    def test_frontmatter(self):
        m = re.match(r"^---\n(.*?)\n---\n", self.text, re.S)
        self.assertIsNotNone(m, "frontmatter がありません")
        front = m.group(1)
        self.assertRegex(front, r"(?m)^name: cross-compile$")
        desc = re.search(r"(?m)^description: (.+)$", front)
        self.assertIsNotNone(desc)
        self.assertIn("スラッシュコマンド", desc.group(1))

    def test_commands_use_own_folder(self):
        for sub in ("run", "config-path", "check"):
            self.assertIn("python3 <このスキルのフォルダ>/scripts/build.py " + sub, self.text)

    def test_sections(self):
        for heading in ("## 何をするスキルか", "## 頼まれ方と、すること", "## 結果の伝え方",
                        "## 守ること"):
            self.assertIn(heading, self.text)

    def test_exit_codes_are_not_shown_to_the_user(self):
        """見出しや場合分けに終了コードを書くと、モデルが利用者に見せてしまう。"""
        for line in self.text.splitlines():
            if "終了コード" in line:
                self.assertIn("伝えません", line, line)
        self.assertNotRegex(self.text, r"[0-2] ?[がは]成功")

    def test_json_names_are_not_shown(self):
        section = self.text.split("## 結果の伝え方", 1)[1].split("## 守ること", 1)[0]
        self.assertIn("JSON の項目名", section)
        self.assertIn("利用者に見せません", section)
        self.assertIn("設定ファイルの項目名", section)

    def test_check_summary_is_a_table_of_config_names(self):
        """表の見出しに、項目名と日本語の言い換えが混ざった（S1-r3）。項目名と値の表で見せる。"""
        section = self.text.split("### 設定を確かめて、問題が無かったとき", 1)[1].split("### ビルドに成功したとき", 1)[0]
        for word in ("設定ファイルの項目名と値の表で見せます。",
                     "列は「項目」と「値」です。",
                     "日本語に言い換えません。",
                     "「説明」の列を別に足します。",
                     "表の外に文で伝えます。"):
            self.assertIn(word, section, word)
        self.assertNotIn("ビルドするフォルダ、コマンド、できるファイルを短く伝えます", self.text)

    def test_mentions_notes_compiler_and_gitignore(self):
        for word in ("`notes`", "`empty_recommended`", "`compiler`", "`.gitignore`",
                     "`stale_outputs`", "`added`"):
            self.assertIn(word, self.text)

    def test_gitignore_and_added_keys_are_explained(self):
        self.assertNotIn("フォルダを新しく作るときは", self.text)
        self.assertIn("`.gitignore` が無ければ", self.text)
        self.assertIn("足りない項目", self.text)
        self.assertIn("ほかの所は変えません", self.text)


if __name__ == "__main__":
    unittest.main()
