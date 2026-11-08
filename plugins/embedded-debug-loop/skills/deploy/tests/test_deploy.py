# -*- coding: utf-8 -*-
"""deploy スキル（scripts/deploy.py）の試験。

- サーバーなし：サム値の計算、設定ファイルの読み方と確かめ、サブコマンドの JSON。
- サーバーあり：試験の中で FTP サーバー（pyftpdlib）を立てて、実際に送る。
  pyftpdlib が読み込めないときは、その試験だけ飛ばす。
  pyftpdlib の場所は、環境変数 DEPLOY_TEST_PYLIB（Windows は ;、Linux は : 区切り）で足せる。
- フォルダだけで動くこと：このスキルのフォルダだけを別の場所に写して動かす。
- Windows でも動くこと：標準出力の文字コード、パスの書き方、改行、権限の確かめ方。
  権限（st_mode）は、POSIX のときだけ見る。どの OS でも、SITE CHMOD の中身を見る。

実行：py -3 -m unittest discover -s tests（Linux は python3）
     （このスキルのフォルダで）
"""

import codecs
import io
import contextlib
import errno
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zlib

# パスは、この試験のファイルの場所から求める（tests/ の親が、このスキルのフォルダ）。
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SKILL_NAME = os.path.basename(SKILL_DIR)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "deploy.py")
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")
# 設定と記録を置くフォルダ：作業フォルダの「.<このスキルのフォルダの名前>」
DATA_DIR = "." + SKILL_NAME


def _load_module():
    # スキルのフォルダに __pycache__ を作らないようにする
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("deploy_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    return mod


deploy = _load_module()


IS_WINDOWS = os.name == "nt"
# Windows で、スクリプトをコンソールなしで動かす（スクリプトは Python の既定の文字コードで書く）
NO_CONSOLE = {"creationflags": subprocess.DETACHED_PROCESS} if IS_WINDOWS else {}


def console_code_page():
    """Windows で、この試験のプロセスのコンソールの出力の文字コード（cp932 など）。なければ None。
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


def _child_encoding():
    """スクリプトが、パイプにつないだ標準出力に使う文字コード。
    Windows でコンソールを受け継ぐときは、コンソールの文字コード。ほかは、この環境の Python の既定。"""
    if console_code_page():
        return console_code_page()
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdout.encoding)"],
        stdout=subprocess.PIPE, check=True)
    return proc.stdout.decode("ascii").strip()


CHILD_ENCODING = _child_encoding()


def decode_out(data, encoding=CHILD_ENCODING):
    return data.decode(encoding)


def decode_err(data, encoding=CHILD_ENCODING):
    return data.decode(encoding, errors="replace")


# --- pyftpdlib（試験のときだけ使う。利用者の PC には要らない） ---------------

_PYLIB_CANDIDATES = [p for p in os.environ.get("DEPLOY_TEST_PYLIB", "").split(os.pathsep) if p]


def _import_pyftpdlib():
    for path in _PYLIB_CANDIDATES:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        from pyftpdlib.authorizers import DummyAuthorizer  # noqa: F401
        from pyftpdlib.handlers import FTPHandler  # noqa: F401
        from pyftpdlib.ioloop import IOLoop  # noqa: F401
        from pyftpdlib.servers import FTPServer  # noqa: F401
    except Exception:
        return False
    # pyftpdlib は、記録の出し先がないと自分で標準エラーに出し始める。
    # 出し先を「何もしない」にして、試験の表示を静かにする。
    ftp_log = logging.getLogger("pyftpdlib")
    ftp_log.addHandler(logging.NullHandler())
    ftp_log.setLevel(logging.CRITICAL)
    ftp_log.propagate = False
    return True


HAVE_PYFTPDLIB = _import_pyftpdlib()
needs_server = unittest.skipUnless(
    HAVE_PYFTPDLIB,
    "pyftpdlib が読み込めないので飛ばす（環境変数 DEPLOY_TEST_PYLIB でその場所を指定できる）")

PASSWORD = "s3cret-Pw"


@contextlib.contextmanager
def ftp_server(root, perm="elradfmwMT", password=PASSWORD, no_size=False, chmods=None):
    """root を "/" にした FTP サーバーを、別のスレッドで動かす。ポート番号を返す。
    chmods にリストを渡すと、受けた SITE CHMOD の (PC のパス, 権限の文字) を足していく。"""
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.ioloop import IOLoop
    from pyftpdlib.servers import FTPServer

    authorizer = DummyAuthorizer()
    authorizer.add_user("root", password, root, perm=perm)
    def on_file_received(self, file):
        # 機器と同じく、受け取ったファイルは上書きでも 0644 にする（chmod するまで実行できない）
        os.chmod(file, 0o644)

    attrs = {
        "authorizer": authorizer,
        "auth_failed_timeout": 0.05,
        "banner": "test ftp ready",
        "timeout": 30,
        "on_file_received": on_file_received,
    }
    if chmods is not None:
        def ftp_SITE_CHMOD(self, path, mode):
            chmods.append((path, mode))
            return FTPHandler.ftp_SITE_CHMOD(self, path, mode)
        attrs["ftp_SITE_CHMOD"] = ftp_SITE_CHMOD
    if no_size:
        cmds = dict(FTPHandler.proto_cmds)
        del cmds["SIZE"]
        attrs["proto_cmds"] = cmds
    handler = type("TestHandler", (FTPHandler,), attrs)
    server = FTPServer(("127.0.0.1", 0), handler, ioloop=IOLoop())
    port = server.address[1]
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            server.serve_forever(timeout=0.05, blocking=False, handle_exit=False)
        server.close_all()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(5)


@contextlib.contextmanager
def silent_server():
    """つながるが、何も答えないサーバー（待ち時間切れを起こす）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --- 共通の道具 ---------------------------------------------------------------

def render_yaml(values):
    """試験用の設定ファイルを書く。値は JSON の形で書く（YAML としても正しい）。"""
    lines = ["# 試験用の設定ファイル"]
    for key, value in values.items():
        lines.append("# %s の説明" % key)
        lines.append("%s: %s" % (key, json.dumps(value, ensure_ascii=False)))
    return "\n".join(lines) + "\n"


class Workspace(object):
    """作業フォルダと、送るファイルと、設定ファイルを用意する。"""

    def __init__(self, test):
        self.dir = tempfile.mkdtemp(prefix="deploy-test-")
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.data_dir = os.path.join(self.dir, DATA_DIR)
        self.config = os.path.join(self.data_dir, "config.yaml")

    def file(self, rel, data):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def write_config(self, **values):
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(render_yaml(values))

    def run(self, *args, env=None, timeout=60):
        full_env = dict(os.environ)
        full_env.pop("DEPLOY_TEST_PW", None)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, SCRIPT] + list(args) + ["--workdir", self.dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=full_env, timeout=timeout)
        out = decode_out(proc.stdout)
        err = decode_err(proc.stderr)
        try:
            data = json.loads(out)
        except ValueError:
            raise AssertionError("標準出力が JSON 1つではありません：%r\n標準エラー：%s" % (out, err))
        return proc.returncode, data, out, err


def base_config(port, **over):
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "user": "root",
        "password": PASSWORD,
        "password_env": "",
        "passive": True,
        "files": ["sensor_app"],
        "remote_dir": "/opt/sensor",
        "file_mode": "755",
        "checksum": "crc32",
        "checksum_suffix": ".sum",
        "timeout_seconds": 10,
    }
    cfg.update(over)
    return cfg


def read_file(path):
    with open(path, "rb") as fh:
        return fh.read()


APP = bytes(range(256)) * 300 + b"\x7fELF-tail"


# =============================================================================
# サーバーなし
# =============================================================================

class ChecksumTest(unittest.TestCase):

    def test_crc32_known_value(self):
        # CRC-32 の「123456789」の値は cbf43926 として知られている。
        self.assertEqual(deploy.checksum_of_bytes("crc32", b"123456789"), 0xCBF43926)
        self.assertEqual(deploy.checksum_of_bytes("crc32", APP),
                         zlib.crc32(APP) & 0xFFFFFFFF)

    def test_sum32_value(self):
        self.assertEqual(deploy.checksum_of_bytes("sum32", b"\x01\x02\xff"), 0x102)
        self.assertEqual(deploy.checksum_of_bytes("sum32", b""), 0)
        self.assertEqual(deploy.checksum_of_bytes("sum32", APP), sum(APP) & 0xFFFFFFFF)

    def test_sum32_wraps_at_32_bits(self):
        chunk = b"\xff" * 65536
        value = 0
        rounds = 258  # 255 * 65536 * 258 は 2^32 を超える
        for _ in range(rounds):
            value = deploy.checksum_update("sum32", value, chunk)
        self.assertEqual(value, (255 * 65536 * rounds) & 0xFFFFFFFF)
        self.assertLess(value, 2 ** 32)

    def test_text_format(self):
        self.assertEqual(deploy.checksum_text(0xCBF43926), "cbf43926\n")
        self.assertEqual(deploy.checksum_text(0x102), "00000102\n")
        self.assertEqual(deploy.checksum_text(0), "00000000\n")
        self.assertRegex(deploy.checksum_text(0xABCDEF01), r"^[0-9a-f]{8}\n$")

    def test_file_matches_bytes_across_chunks(self):
        ws = Workspace(self)
        data = os.urandom(deploy.CHUNK * 3 + 17)
        path = ws.file("blob.bin", data)
        for kind in ("crc32", "sum32"):
            self.assertEqual(deploy.checksum_of_file(kind, path),
                             deploy.checksum_of_bytes(kind, data))

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            deploy.checksum_update("md5", 0, b"x")


class YamlReaderTest(unittest.TestCase):

    def test_template_values(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            values, _ = deploy.read_config_text(fh.read())
        self.assertEqual(values, {
            "host": "", "port": 21, "user": "root", "password": "",
            "password_env": "", "passive": True, "files": [], "remote_dir": "",
            "file_mode": "", "checksum": "none", "checksum_suffix": ".sum",
            "timeout_seconds": 30,
        })

    def test_template_keys_have_comments(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertTrue(lines[0].startswith("# ") and "スキルの設定ファイルです" in lines[0], lines[0])
        # 名前を変えて写すと食い違うので、ひな形にはスキルの名前を書かない
        self.assertNotIn("deploy", "\n".join(lines))
        for i, line in enumerate(lines):
            if line and not line.startswith("#"):
                key = line.split(":", 1)[0]
                self.assertTrue(lines[i - 1].startswith("#"),
                                "%s の上に説明のコメントがありません" % key)
                # 説明の1行目（例：・選べる値：でない行）を探す
                j = i - 1
                while j > 0 and lines[j - 1].startswith("#"):
                    j -= 1
                first = lines[j]
                self.assertFalse(first.startswith("# 例："), key)
                if key in ("host", "files", "remote_dir"):
                    self.assertIn("（必須）", "\n".join(lines[j:i]), key)
        text = "\n".join(lines)
        self.assertIn("# 選べる値：none / crc32 / sum32", text)

    def test_template_examples_are_generic(self):
        """例が試験で使う名前（sensor_app など）と同じだと、試験が例に頼って通ってしまう。"""
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("sensor", text)
        self.assertIn('# 例：["my_app"]', text)
        self.assertIn("# 例：/opt/myapp", text)

    def test_template_comments_do_not_point_by_position(self):
        """コメントは、項目から離れて（チャットなどで）読まれることがある。「下の」などは、離れて読むと通じない。"""
        with open(TEMPLATE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for i, line in enumerate(lines):
            if not re.match(r"^[A-Za-z_]+:", line):
                continue
            j = i
            while j > 0 and lines[j - 1].startswith("#"):
                j -= 1
            for comment in lines[j:i]:
                for word in ("下の", "上の", "上から", "このスキル", "この項目"):
                    self.assertNotIn(word, comment, comment)

    def test_password_comment_says_it_is_needed_with_user(self):
        """設定を案内する側が「必ず書く」の言い方で、ふだん埋める項目だと分かるように。"""
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        block = re.search(r"((?:^#.*\n)+)password:", text, re.M).group(1)
        self.assertIn("user を書いたら、必ず書く（password_env で渡すときは空）。", block)
        self.assertNotIn("必須", block, "必須の印を付けると、anonymous や環境変数で渡すときに困る")

    def test_file_mode_comment_says_files_are_not_executable(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        block = re.search(r"((?:^#.*\n)+)file_mode:", text, re.M).group(1)
        self.assertIn("実行できない権限", block)
        self.assertNotIn("空なら変えない", block)
        self.assertIn("# 選べる値：true / false", text)

    def test_scalars_and_lists(self):
        text = "\n".join([
            "# head",
            "a: \"x # not a comment\"",
            "b: 'it''s'",
            "c: bare value  # comment",
            "d: 12",
            "e: 1.5",
            "f: false",
            "g: null",
            "h: [\"x\", \"y\"]",
            "i: ['p', q, 3]",
            "j:",
            "  - one",
            "  # comment between",
            "  - \"two\"",
            "k:",
            "l: 0755",
            "m: []  # empty",
            "",
        ])
        values, raws = deploy.read_config_text(text)
        self.assertEqual(values["a"], "x # not a comment")
        self.assertEqual(values["b"], "it's")
        self.assertEqual(values["c"], "bare value")
        self.assertEqual(values["d"], 12)
        self.assertEqual(values["e"], 1.5)
        self.assertIs(values["f"], False)
        self.assertIsNone(values["g"])
        self.assertEqual(values["h"], ["x", "y"])
        self.assertEqual(values["i"], ["p", "q", 3])
        self.assertEqual(values["j"], ["one", "two"])
        self.assertIsNone(values["k"])
        self.assertEqual(raws["l"], "0755")
        self.assertEqual(values["m"], [])

    def test_errors_are_japanese_with_line_numbers(self):
        bad = [
            ("a: 1\n  nested: 2\n", "2 行目"),
            ("just text\n", "1 行目"),
            ("- item\n", "1 行目"),
            ("a: \"open\n", "1 行目"),
            ("a: [1, 2\n", "1 行目"),
            ("a: 1\na: 2\n", "2 回"),
            ("a: {x: 1}\n", "対応していません"),
        ]
        for text, expect in bad:
            with self.assertRaises(deploy.ConfigError, msg=text) as ctx:
                deploy.read_config_text(text)
            self.assertIn(expect.replace(" ", ""), str(ctx.exception).replace(" ", ""))


class ValidateTest(unittest.TestCase):

    def check(self, ws, environ=None, **values):
        raws = {k: json.dumps(v) for k, v in values.items()}
        return deploy.validate(values, raws, ws.dir, environ or {})

    def test_good_settings(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        s, problems, notes, ready = self.check(ws, **base_config(21))
        self.assertEqual(problems, [])
        self.assertTrue(ready)
        self.assertEqual(s["local_files"][0]["path"], os.path.join(ws.dir, "sensor_app"))
        self.assertEqual(s["file_mode"], "755")

    def test_each_problem_is_reported(self):
        ws = Workspace(self)
        cfg = base_config(
            70000, files=["missing.bin"], checksum="md5", file_mode="rwx",
            timeout_seconds=0, passive="yes", remote_dir="")
        _s, problems, _n, ready = self.check(ws, **cfg)
        text = "\n".join(problems)
        self.assertIn("port は 1〜65535", text)
        self.assertIn("missing.bin が見つかりません", text)
        self.assertIn("checksum は none / crc32 / sum32", text)
        self.assertIn("file_mode は 755 のような", text)
        self.assertIn("timeout_seconds", text)
        self.assertIn("passive は true か false", text)
        self.assertIn("remote_dir", text)
        self.assertFalse(ready)

    def test_required_missing(self):
        ws = Workspace(self)
        values = {"host": "", "files": [], "remote_dir": ""}
        self.assertEqual(deploy.missing_keys(values), ["host", "files", "remote_dir"])
        _s, problems, _n, ready = deploy.validate(values, {}, ws.dir, {})
        self.assertEqual(len(problems), 3)
        self.assertFalse(ready)

    def test_single_problem_message_is_punctuated(self):
        """問題の文に「。」がないと、次の「設定ファイルは…」とつながって読めない。"""
        msg = deploy.problems_message(["files に、ファイルの名前でない値があります：3"], "/w/c.yaml", False)
        self.assertEqual(msg, "files に、ファイルの名前でない値があります：3。設定ファイルは /w/c.yaml です。")
        msg = deploy.problems_message(["host（機器の IP アドレスかホスト名）が空です。"], "/w/c.yaml", True)
        self.assertNotIn("。。", msg)

    def test_every_problem_ends_a_sentence(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        cfg = base_config(
            70000, files=["missing.bin", 3, "sensor_app", "sensor_app"], checksum="md5", file_mode="rwx",
            timeout_seconds=0, passive="yes", remote_dir="", user=5, host="a b")
        _s, problems, _n, _ready = self.check(ws, **cfg)
        self.assertGreater(len(problems), 5)
        for p in problems:
            self.assertTrue(p.endswith("。"), p)
        _s, problems, _n, _ready = self.check(ws, files="x", host=["a"], checksum="crc32",
                                              checksum_suffix="")
        for p in problems:
            self.assertTrue(p.endswith("。"), p)

    def test_mode_forms(self):
        self.assertEqual(deploy.normalize_mode("755"), "755")
        self.assertEqual(deploy.normalize_mode("0755"), "755")
        self.assertEqual(deploy.normalize_mode("0o755"), "755")
        self.assertEqual(deploy.normalize_mode(755), "755")
        self.assertEqual(deploy.normalize_mode("4755"), "4755")
        self.assertEqual(deploy.normalize_mode("644"), "644")
        self.assertIsNone(deploy.normalize_mode("799"))
        self.assertIsNone(deploy.normalize_mode("rwx"))

    def test_bare_octal_mode_in_file(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        text = render_yaml(base_config(21)).replace('file_mode: "755"', "file_mode: 0755")
        values, raws = deploy.read_config_text(text)
        s, problems, _n, _r = deploy.validate(values, raws, ws.dir, {})
        self.assertEqual(problems, [])
        self.assertEqual(s["file_mode"], "755")

    def test_password_env_takes_precedence(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        cfg = base_config(21, password="from-file", password_env="DEPLOY_TEST_PW")
        s, problems, _n, _r = self.check(ws, environ={"DEPLOY_TEST_PW": "from-env"}, **cfg)
        self.assertEqual(problems, [])
        self.assertEqual(s["password"], "from-env")
        # 環境変数がなく、password もあれば、password を使う（知らせは出す）
        s, problems, notes, _r = self.check(ws, environ={}, **cfg)
        self.assertEqual(problems, [])
        self.assertEqual(s["password"], "from-file")
        self.assertTrue(any("DEPLOY_TEST_PW" in n for n in notes))
        # どちらもなければ問題
        cfg["password"] = ""
        _s, problems, _n, ready = self.check(ws, environ={}, **cfg)
        self.assertTrue(any("DEPLOY_TEST_PW" in p for p in problems))
        self.assertFalse(ready)

    def test_passive_written_as_text(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        s, problems, _n, _r = self.check(ws, **base_config(21, passive="false"))
        self.assertEqual(problems, [])
        self.assertIs(s["passive"], False)

    def test_host_must_be_one_value(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        _s, problems, _n, ready = self.check(ws, **base_config(21, host=["a", "b"]))
        self.assertTrue(any("1つだけ" in p for p in problems), problems)
        self.assertFalse(ready)

    def test_windows_path_read_as_tab_and_newline(self):
        """「"」で囲んだ C:\\temp\\new_app は、YAML と同じく \\t と \\n がタブと改行になる。それを知らせる。"""
        ws = Workspace(self)
        text = render_yaml(base_config(21)).replace(
            'files: ["sensor_app"]', 'files: ["C:\\temp\\new_app"]')
        values, raws = deploy.read_config_text(text)
        self.assertEqual(values["files"], ["C:" + "\t" + "emp" + "\n" + "ew_app"])
        _s, problems, _n, _r = deploy.validate(values, raws, ws.dir, {})
        found = [p for p in problems if p.startswith("files の")]
        self.assertEqual(len(found), 1, problems)
        self.assertIn("C:\\temp\\new_app", found[0])
        self.assertIn("タブや改行", found[0])
        self.assertIn("/ で区切る", found[0])
        self.assertTrue(found[0].endswith("。"), found[0])

    def test_windows_path_forms_that_work(self):
        """「'」で囲んだ \\ のパスと、/ で区切ったパスは、そのまま読む。"""
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        text = render_yaml(base_config(21)).replace(
            'files: ["sensor_app"]', "files: ['C:\\temp\\new_app']")
        values, _raws = deploy.read_config_text(text)
        self.assertEqual(values["files"], ["C:\\temp\\new_app"])
        absolute = os.path.join(ws.dir, "sensor_app").replace(os.sep, "/")
        s, problems, _n, _r = self.check(ws, **base_config(21, files=[absolute]))
        self.assertEqual(problems, [])
        self.assertEqual(s["local_files"][0]["path"], os.path.join(ws.dir, "sensor_app"))

    def test_checksum_suffix_must_be_usable_in_file_names(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        for suffix in ("/sum", "\\sum", ".sum:x", ".s*m", '.s"m', ".s|m", ".s\tm"):
            with self.subTest(suffix=suffix):
                _s, problems, _n, _r = self.check(ws, **base_config(21, checksum_suffix=suffix))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("ファイルの名前に使えない文字", problems[0])
                self.assertTrue(problems[0].endswith("。"), problems[0])
        _s, problems, _n, _r = self.check(ws, **base_config(21, checksum_suffix=".crc-32_sum"))
        self.assertEqual(problems, [])

    def test_remote_dir_with_backslashes(self):
        """機器のパスを Windows の書き方（\\ で区切る）で書くと、機器に \\ を含む名前のフォルダができる。送る前に止める。"""
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        # 最後は "\tmp\new" と " で囲んだとき（\t と \n がタブと改行になる）
        for value, shown in (("\\opt\\app", "\\opt\\app"), ("opt\\app", "opt\\app"),
                             ("\t" + "mp" + "\n" + "ew", "\\tmp\\new")):
            with self.subTest(value=value):
                s, problems, _n, _r = self.check(ws, **base_config(21, remote_dir=value))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("\\ ではなく / で区切ってください", problems[0])
                self.assertIn("（今の値：%s）" % shown, problems[0])
                self.assertTrue(problems[0].endswith("。"), problems[0])
                self.assertEqual(s["remote_dir"], "")
        s, problems, _n, _r = self.check(ws, **base_config(21, remote_dir="/opt/app/"))
        self.assertEqual(problems, [])
        self.assertEqual(s["remote_dir"], "/opt/app")

    def test_duplicate_basenames(self):
        ws = Workspace(self)
        ws.file("a/app", b"1")
        ws.file("b/app", b"2")
        _s, problems, _n, _r = self.check(ws, **base_config(21, files=["a/app", "b/app"]))
        self.assertTrue(any("同じ名前" in p for p in problems))

    def test_login_hint_names_empty_password_and_where_to_write(self):
        conf = "/w/%s/config.yaml" % DATA_DIR
        hint = deploy.login_hint({"user": "root", "password": "", "password_env": "",
                                  "config_path": conf})
        self.assertIn("password も password_env も空", hint)
        self.assertIn(conf, hint)
        hint = deploy.login_hint({"user": "root", "password": "", "password_env": "MY_PW",
                                  "config_path": conf})
        self.assertIn("環境変数 MY_PW の中身が空", hint)
        hint = deploy.login_hint({"user": "", "password": "", "password_env": "",
                                  "config_path": conf})
        self.assertIn("user が空", hint)
        hint = deploy.login_hint({"user": "root", "password": "x", "password_env": "",
                                  "config_path": conf})
        self.assertEqual(hint, "設定の user と password を確かめてください。")

    def test_summary_masks_password(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        s, _p, _n, _r = self.check(ws, **base_config(21))
        summary = deploy.summary_of(s)
        self.assertEqual(summary["password"], deploy.MASK)
        self.assertNotIn(PASSWORD, json.dumps(summary, ensure_ascii=False))


def template_blocks():
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        return deploy.template_blocks(fh.read())


def template_without(*keys):
    """ひな形から、指定した項目（すぐ上のコメントと項目の行）を除いた文。"""
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        text = fh.read()
    for key, lines in template_blocks():
        if key in keys:
            text = text.replace("\n".join(lines) + "\n", "")
    return text.rstrip("\n") + "\n"


class OutputTest(unittest.TestCase):
    """コンソールがないときは、標準出力は Python の既定の文字コードで書き、書けない文字があっても
    JSON として読めること（Windows でコンソールがあるときは、ConsoleCodePageTest で確かめる）。"""

    def run_script(self, workdir, encoding, *args):
        env = dict(os.environ, PYTHONIOENCODING=encoding)
        proc = subprocess.run(
            [sys.executable, SCRIPT] + list(args) + ["--workdir", workdir],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            timeout=60, **NO_CONSOLE)
        return proc.returncode, proc.stdout, proc.stderr

    def workdir(self):
        # 日本語と、BMP の外の文字（絵文字）を入れたフォルダ
        path = tempfile.mkdtemp(prefix="deploy-出力-\U0001F600-")
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_json_for_escapes_only_what_cannot_be_written(self):
        class Stream(object):
            encoding = "cp1252"
        obj = {"message": "機器 é \U0001F600", "n": 1}
        text = deploy.json_for(Stream, obj)
        text.encode("cp1252")
        self.assertIn("é", text)
        self.assertIn("\\u6a5f\\u5668", text)
        self.assertIn("\\ud83d\\ude00", text)
        self.assertEqual(json.loads(text), obj)
        Stream.encoding = "utf-8"
        self.assertEqual(deploy.json_for(Stream, obj), json.dumps(obj, ensure_ascii=False))

    def test_stdout_in_cp932(self):
        """日本語の Windows の PowerShell と同じ cp932 で書く（UTF-8 のバイト列を直接書かない）。"""
        work = self.workdir()
        code, out, err = self.run_script(work, "cp932")
        self.assertEqual(code, 2)
        self.assertIn("ひな形から作りました".encode("cp932"), out)
        self.assertNotIn("ひな形から作りました".encode("utf-8"), out)
        data = json.loads(out.decode("cp932"))
        self.assertIn("ひな形から作りました", data["message"])
        # cp932 にない絵文字は、JSON の \u の形で届く
        self.assertEqual(data["config"], os.path.join(work, DATA_DIR, "config.yaml"))
        self.assertIn("ひな形から作りました".encode("cp932"), err)

    def test_stdout_when_japanese_cannot_be_written(self):
        """英語の Windows（cp1252 など）のように日本語を書けなくても、JSON として読める。"""
        work = self.workdir()
        code, out, err = self.run_script(work, "ascii", "config-path")
        self.assertEqual(code, 0, out)
        data = json.loads(out.decode("ascii"))
        self.assertEqual(data["config"], os.path.join(work, DATA_DIR, "config.yaml"))
        code, out, err = self.run_script(work, "ascii", "check")
        self.assertEqual(code, 2)
        data = json.loads(out.decode("ascii"))
        self.assertIn("設定に直すところが", data["message"])
        # 標準エラーは、書けない文字を \ の形で書く（止まらない）
        lines = [l for l in err.decode("ascii").splitlines() if l]
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue(line.startswith("[%s] " % SKILL_NAME), line)
        self.assertNotIn("Traceback", err.decode("ascii"))


class AddMissingKeysTest(unittest.TestCase):
    """スキルを新しくして、ひな形に項目が増えたとき、config-path だけが古い設定ファイルに足す。
    check と run は足さずに、notes で知らせる。"""

    def old_config(self, ws, text):
        data = text.encode("utf-8")
        ws.file(os.path.relpath(ws.config, ws.dir), data)
        return data

    def test_template_blocks(self):
        blocks = template_blocks()
        self.assertEqual([k for k, _ in blocks],
                         ["host", "port", "user", "password", "password_env", "passive", "files",
                          "remote_dir", "file_mode", "checksum", "checksum_suffix", "timeout_seconds"])
        for key, lines in blocks:
            self.assertTrue(lines[-1].startswith(key + ":"), key)
            self.assertTrue(all(l.startswith("#") for l in lines[:-1]), key)
        with open(TEMPLATE, "r", encoding="utf-8") as fh:
            head = fh.readline().rstrip("\n")
        self.assertNotIn(head, blocks[0][1])

    def test_missing_keys_are_appended_with_comments(self):
        ws = Workspace(self)
        text = template_without("checksum", "checksum_suffix").replace('host: ""', 'host: "10.0.0.5"')
        before = self.old_config(ws, text)
        code, data, _o, _e = ws.run("config-path")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["added"], ["checksum", "checksum_suffix"])
        self.assertEqual(data["message"],
                         "設定ファイルに、新しい項目 checksum、checksum_suffix を足しました（今の値はひな形のまま）。")
        after = read_file(ws.config)
        self.assertEqual(after[:len(before)], before)
        blocks = dict(template_blocks())
        expected = "\n" + "\n".join(blocks["checksum"]) + "\n\n" + "\n".join(blocks["checksum_suffix"]) + "\n"
        self.assertEqual(after[len(before):].decode("utf-8"), expected)
        values, _raws = deploy.read_config_text(after.decode("utf-8"))
        self.assertEqual(values["host"], "10.0.0.5")
        self.assertEqual(values["checksum"], "none")
        self.assertEqual(values["checksum_suffix"], ".sum")

        code, data, _o, _e = ws.run("config-path")
        self.assertEqual(data["added"], [])
        self.assertNotIn("message", data)
        self.assertEqual(read_file(ws.config), after)

    def test_check_and_run_do_not_add(self):
        ws = Workspace(self)
        before = self.old_config(ws, template_without("timeout_seconds"))
        note = "設定ファイルに、項目 timeout_seconds がありません。config-path で、ひな形から足せます。"
        code, data, _o, _e = ws.run("check")
        self.assertEqual(code, 2, data)
        self.assertNotIn("added", data)
        self.assertIn(note, data["notes"])
        self.assertEqual(read_file(ws.config), before)
        self.assertEqual(os.listdir(ws.data_dir), ["config.yaml"])

        code, data, _o, _e = ws.run("run")
        self.assertEqual(code, 2, data)
        self.assertNotIn("added", data)
        self.assertEqual(data["notes"], [note])
        self.assertEqual(read_file(ws.config), before)

        # config-path が足す。その後は、check も知らせない
        code, data, _o, _e = ws.run("config-path")
        self.assertEqual(data["added"], ["timeout_seconds"])
        self.assertEqual(read_file(ws.config)[:len(before)], before)
        code, data, _o, _e = ws.run("check")
        self.assertNotIn(note, data["notes"])

    def test_check_reports_a_missing_required_key_without_writing(self):
        ws = Workspace(self)
        before = self.old_config(ws, template_without("host"))
        code, data, _o, _e = ws.run("check")
        self.assertEqual(code, 2, data)
        self.assertIn("host（機器の IP アドレスかホスト名）が空です。", data["problems"])
        self.assertIn("設定ファイルに、項目 host がありません。config-path で、ひな形から足せます。",
                      data["notes"])
        self.assertEqual(read_file(ws.config), before)
        self.assertEqual(os.listdir(ws.data_dir), ["config.yaml"])

        code, data, _o, _e = ws.run("config-path")
        self.assertEqual(data["added"], ["host"])
        self.assertIn("host", data["missing"])
        values, _raws = deploy.read_config_file(ws.config)
        self.assertEqual(values["host"], "")

    def test_broken_file_is_not_touched(self):
        ws = Workspace(self)
        before = self.old_config(ws, 'host: "a\n')
        code, data, _o, _e = ws.run("config-path")
        self.assertEqual(data["added"], [])
        self.assertIn("problems", data)
        self.assertEqual(read_file(ws.config), before)

    def test_crlf_file(self):
        ws = Workspace(self)
        text = template_without("passive").replace("\n", "\r\n")
        before = self.old_config(ws, text)
        code, data, _o, _e = ws.run("config-path")
        self.assertEqual(data["added"], ["passive"])
        after = read_file(ws.config)
        self.assertEqual(after[:len(before)], before)
        tail = after[len(before):]
        self.assertTrue(tail.startswith(b"\r\n#"), tail)
        self.assertTrue(tail.endswith(b"passive: true\r\n"), tail)
        self.assertNotIn(b"\n", tail.replace(b"\r\n", b""))

    def test_utf8_file_with_bom(self):
        """BOM 付きの UTF-8（Windows のメモ帳や PowerShell 5.1 の Set-Content -Encoding UTF8）。BOM は残して足す。"""
        ws = Workspace(self)
        before = b"\xef\xbb\xbf" + template_without("passive").replace("\n", "\r\n").encode("utf-8")
        ws.file(os.path.relpath(ws.config, ws.dir), before)
        code, data, _o, _e = ws.run("config-path")
        self.assertEqual(code, 0, data)
        self.assertNotIn("problems", data)
        self.assertEqual(data["added"], ["passive"])
        after = read_file(ws.config)
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(after.count(b"\xef\xbb\xbf"), 1)
        self.assertTrue(after.endswith(b"passive: true\r\n"), after[-40:])
        values, _raws = deploy.read_config_file(ws.config)
        self.assertIs(values["passive"], True)


class CommandWithoutServerTest(unittest.TestCase):

    def test_config_path_creates_from_template(self):
        ws = Workspace(self)
        code, data, _out, _err = ws.run("config-path")
        self.assertEqual(code, 0)
        self.assertEqual(set(data), {"config", "created", "missing", "added"})
        self.assertEqual(data["config"], ws.config)
        self.assertTrue(data["created"])
        self.assertEqual(data["added"], [])
        self.assertEqual(data["missing"], ["host", "files", "remote_dir"])
        self.assertEqual(read_file(ws.config), read_file(TEMPLATE))
        code, data, _out, _err = ws.run("config-path")
        self.assertEqual(code, 0)
        self.assertFalse(data["created"])

    def test_new_folder_gets_gitignore(self):
        ws = Workspace(self)
        ws.run("config-path")
        gi = os.path.join(ws.data_dir, ".gitignore")
        self.assertEqual(read_file(gi), b"*\n")
        # 2回目は、そのまま（書き直さない）
        with open(gi, "wb") as fh:
            fh.write(b"logs/\n")
        ws.run("config-path")
        self.assertEqual(read_file(gi), b"logs/\n")

    def test_existing_folder_without_gitignore_gets_one(self):
        """ほかのものが先にフォルダを作っても、.gitignore がなければ作る。"""
        ws = Workspace(self)
        os.makedirs(ws.data_dir)
        ws.run("config-path")
        self.assertTrue(os.path.isfile(ws.config))
        self.assertEqual(read_file(os.path.join(ws.data_dir, ".gitignore")), b"*\n")

    def test_gitignore_is_made_again_when_removed(self):
        """config-path と run は作り直す。check は作らない（作業フォルダにファイルを作らない）。"""
        ws = Workspace(self)
        ws.run("config-path")
        gi = os.path.join(ws.data_dir, ".gitignore")
        for command in ("config-path", "run"):
            os.unlink(gi)
            ws.run(command)
            self.assertEqual(read_file(gi), b"*\n", command)
        os.unlink(gi)
        ws.run("check")
        self.assertFalse(os.path.lexists(gi))

    def test_gitignore_in_work_folder_with_config_elsewhere(self):
        ws = Workspace(self)
        os.makedirs(ws.data_dir)
        ws.run("config-path", "--config", os.path.join(ws.dir, "conf", "my-deploy.yaml"))
        self.assertEqual(read_file(os.path.join(ws.data_dir, ".gitignore")), b"*\n")

    def test_gitignore_when_only_the_log_folder_is_made(self):
        ws = Workspace(self)
        other = os.path.join(ws.dir, "conf", "my-deploy.yaml")
        ws.run("run", "--config", other)
        self.assertEqual(read_file(os.path.join(ws.data_dir, ".gitignore")), b"*\n")

    def test_check_without_config_makes_no_files(self):
        """check は、設定ファイルがなければ作らずに、ないと答える（終了コード 2）。記録のファイルも作らない。"""
        ws = Workspace(self)
        other = os.path.join(ws.dir, "conf", "my-deploy.yaml")
        for args, config in (((), ws.config), (("--config", other), other)):
            with self.subTest(args=args):
                code, data, _o, err = ws.run("check", *args)
                self.assertEqual(code, 2, data)
                self.assertFalse(data["ok"])
                self.assertFalse(data["created"])
                self.assertEqual(data["config"], config)
                self.assertEqual(data["message"],
                                 "設定ファイルがありません（%s）。config-path で、ひな形から作れます。" % config)
                self.assertEqual(data["problems"], [data["message"]])
                self.assertNotIn("log", data)
                self.assertEqual(os.listdir(ws.dir), [])
                self.assertNotIn("Traceback", err)

    def test_check_with_config_makes_no_files(self):
        """設定ファイルがあっても、.gitignore や記録のファイルを作らない。"""
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        ws.write_config(**base_config(free_port()))
        before = read_file(ws.config)
        code, data, _o, _e = ws.run("check")
        self.assertEqual(code, 2, data)
        self.assertNotIn("log", data)
        self.assertEqual(sorted(os.listdir(ws.dir)), [DATA_DIR, "sensor_app"])
        self.assertEqual(os.listdir(ws.data_dir), ["config.yaml"])
        self.assertEqual(read_file(ws.config), before)

    def test_check_when_config_is_a_folder(self):
        """設定ファイルの場所がフォルダなら、設定の問題として答える（終了コード 2）。何も作らない。"""
        ws = Workspace(self)
        os.makedirs(ws.config)
        code, data, _o, err = ws.run("check")
        self.assertEqual(code, 2, data)
        self.assertFalse(data["ok"])
        self.assertIn("設定ファイルの場所 %s は、ファイルではありません。" % ws.config, data["problems"])
        self.assertNotIn("log", data)
        self.assertEqual(os.listdir(ws.dir), [DATA_DIR])
        self.assertEqual(os.listdir(ws.data_dir), ["config.yaml"])
        self.assertEqual(os.listdir(ws.config), [])
        self.assertNotIn("Traceback", err)

    def test_check_notes_empty_file_mode(self):
        """file_mode が空なら、check は notes で知らせる（設定の問題にはしない）。"""
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        for mode, expected in (("", [deploy.NO_MODE_NOTE]), ("755", [])):
            with self.subTest(file_mode=mode):
                ws.write_config(**base_config(free_port(), file_mode=mode))
                code, data, _o, _e = ws.run("check")
                self.assertEqual(code, 2, data)  # 接続が拒否される
                self.assertEqual(data["notes"], expected)
                self.assertEqual(len(data["problems"]), 1, data)
                self.assertNotIn("file_mode", data["problems"][0])
        # 項目そのものがないとき（古い設定ファイル）も、空と同じ。ないことも知らせる（足さない）
        cfg = base_config(free_port())
        del cfg["file_mode"]
        before = render_yaml(cfg).encode("utf-8")
        ws.file(os.path.relpath(ws.config, ws.dir), before)
        code, data, _o, _e = ws.run("check")
        self.assertNotIn("added", data)
        self.assertEqual(data["notes"], [
            deploy.NO_MODE_NOTE,
            "設定ファイルに、項目 file_mode がありません。config-path で、ひな形から足せます。"])
        self.assertEqual(read_file(ws.config), before)

    def test_check_unexpected_error_makes_no_files(self):
        ws = Workspace(self)
        ws.write_config(**base_config(free_port()))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(deploy, "validate", side_effect=RuntimeError("boom")), \
                mock.patch.object(deploy.sys, "stdout", out), mock.patch.object(deploy.sys, "stderr", err):
            code = deploy.main(["check", "--workdir", ws.dir])
        self.assertEqual(code, 1)
        data = json.loads(out.getvalue())
        self.assertFalse(data["ok"])
        self.assertEqual(data["message"], "思わぬエラーで止まりました（RuntimeError: boom）。")
        self.assertNotIn("log", data)
        self.assertIn("Traceback", err.getvalue())
        self.assertEqual(os.listdir(ws.data_dir), ["config.yaml"])

    def test_config_option(self):
        ws = Workspace(self)
        other = os.path.join(ws.dir, "conf", "my-deploy.yaml")
        code, data, _o, _e = ws.run("config-path", "--config", other)
        self.assertEqual(code, 0)
        self.assertEqual(data["config"], other)
        self.assertTrue(os.path.isfile(other))

    def test_run_with_new_config_reports_problems(self):
        ws = Workspace(self)
        code, data, _out, err = ws.run()  # サブコマンドなし = run
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertTrue(data["created"])
        self.assertEqual(data["config"], ws.config)
        self.assertEqual(len(data["problems"]), 3)
        self.assertIn("ひな形から作りました", data["message"])
        self.assertIn(ws.config, data["message"])
        self.assertIn("コメント", data["message"])
        self.assertTrue(os.path.isfile(data["log"]))
        self.assertTrue(data["log"].startswith(os.path.join(ws.data_dir, "logs", SKILL_NAME + "-")),
                        data["log"])
        self.assertNotIn("Traceback", err)

    def test_check_with_missing_local_file(self):
        ws = Workspace(self)
        ws.write_config(**base_config(free_port(), files=["nothing_here"]))
        code, data, _o, _e = ws.run("check")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertTrue(any("nothing_here" in p for p in data["problems"]))
        self.assertEqual(data["summary"]["password"], deploy.MASK)
        # 接続も拒否されるので、原因が2つとも message に出る
        self.assertIn("拒否", data["message"])
        self.assertIn("nothing_here", data["message"])

    def test_run_with_missing_local_file_names_the_file(self):
        ws = Workspace(self)
        ws.write_config(**base_config(free_port(), files=["not_built_yet"]))
        code, data, _o, _e = ws.run()
        self.assertEqual(code, 2)
        self.assertEqual(len(data["problems"]), 1)
        self.assertIn("not_built_yet が見つかりません", data["message"])
        self.assertIn(ws.config, data["message"])

    def test_broken_yaml(self):
        ws = Workspace(self)
        os.makedirs(os.path.dirname(ws.config))
        with open(ws.config, "w", encoding="utf-8") as fh:
            fh.write("host: \"1.2.3.4\nfiles: [\n")
        for sub in ("run", "check"):
            code, data, _o, _e = ws.run(sub)
            self.assertEqual(code, 2, sub)
            self.assertFalse(data["ok"])
            self.assertIn("1 行目", data["problems"][0])
            self.assertIn("1 行目", data["message"])
        code, data, _o, _e = ws.run("config-path")
        self.assertEqual(code, 0)
        self.assertEqual(data["config"], ws.config)
        self.assertIn("1 行目", data["problems"][0])

    def test_utf8_file_with_bom_and_key_on_first_line(self):
        """BOM のすぐ後ろの項目も読める。CRLF でも読める。"""
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        text = "".join(line + "\r\n" for line in render_yaml(base_config(21)).splitlines()
                       if not line.startswith("#"))
        self.assertTrue(text.startswith("host:"), text)
        data = b"\xef\xbb\xbf" + text.encode("utf-8")
        ws.file(os.path.relpath(ws.config, ws.dir), data)
        values, raws = deploy.read_config_file(ws.config)
        self.assertEqual(values["host"], "127.0.0.1")
        _s, problems, _n, _r = deploy.validate(values, raws, ws.dir, {})
        self.assertEqual(problems, [])
        code, out, _o, _e = ws.run("config-path")
        self.assertEqual(code, 0, out)
        self.assertNotIn("problems", out)
        self.assertEqual(out["missing"], [])
        self.assertEqual(out["added"], [])
        self.assertEqual(read_file(ws.config), data)

    def test_config_not_in_utf8(self):
        """メモ帳で ANSI（日本語の Windows では cp932）にしたときや、PowerShell 5.1 の > で書いたとき（UTF-16）。"""
        for name, data in (("cp932", 'host: "機器"\n'.encode("cp932")),
                           ("utf-16", 'host: "x"\r\n'.encode("utf-16"))):
            with self.subTest(encoding=name):
                ws = Workspace(self)
                ws.file(os.path.relpath(ws.config, ws.dir), data)
                for sub in ("check", "run"):
                    code, out, _o, _e = ws.run(sub)
                    self.assertEqual(code, 2, out)
                    self.assertFalse(out["ok"])
                    self.assertEqual(len(out["problems"]), 1, out)
                    self.assertIn("UTF-8 で読めません", out["message"])
                    # 設定ファイルの場所は、メッセージに1回だけ出る
                    self.assertEqual(out["message"].count(ws.config), 1, out["message"])
                code, out, _o, _e = ws.run("config-path")
                self.assertEqual(code, 0, out)
                self.assertEqual(out["config"], ws.config)
                self.assertIn("UTF-8 で読めません", out["problems"][0])
                self.assertEqual(out["added"], [])
                self.assertEqual(read_file(ws.config), data)

    def test_run_stops_on_windows_style_remote_dir(self):
        """'\\opt\\sensor' と書くと、機器につながずに止まる（送らない）。"""
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        text = render_yaml(base_config(free_port())).replace(
            'remote_dir: "/opt/sensor"', "remote_dir: '\\opt\\sensor'")
        self.assertIn("remote_dir: '\\opt\\sensor'", text)
        ws.file(os.path.relpath(ws.config, ws.dir), text.encode("utf-8"))
        code, data, _o, _e = ws.run()
        self.assertEqual(code, 2, data)
        self.assertEqual(len(data["problems"]), 1, data)
        self.assertIn("（今の値：\\opt\\sensor）", data["message"])
        self.assertIn("/ で区切ってください", data["message"])
        self.assertNotIn("files", data)

    def test_bad_command_answers_json(self):
        ws = Workspace(self)
        for args in (["upload"], ["-h"], ["--help"]):
            code, data, _o, _e = ws.run(*args)
            self.assertEqual(code, 2, args)
            self.assertFalse(data["ok"])
            self.assertIn("使い方", data["message"])

    def test_connection_refused(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        port = free_port()
        ws.write_config(**base_config(port))
        code, data, out, _e = ws.run()
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertIn("127.0.0.1", data["message"])
        self.assertIn("%d 番ポート" % port, data["message"])
        self.assertIn("拒否", data["message"])
        self.assertIn("電源", data["message"])
        self.assertNotIn(PASSWORD, out)

    def test_timeout_when_server_is_silent(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        with silent_server() as port:
            ws.write_config(**base_config(port, timeout_seconds=1))
            start = time.time()
            code, data, _o, _e = ws.run()
            elapsed = time.time() - start
        self.assertEqual(code, 1)
        self.assertIn("1 秒待っても応答がありません", data["message"])
        self.assertIn("%d 番ポート" % port, data["message"])
        self.assertLess(elapsed, 15)

    def test_timeout_to_unroutable_address(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        ws.write_config(**base_config(21, host="10.255.255.1", timeout_seconds=1))
        start = time.time()
        code, data, _o, _e = ws.run()
        elapsed = time.time() - start
        self.assertEqual(code, 1)
        msg = data["message"]
        self.assertIn("機器 10.255.255.1 の 21 番ポートに FTP でつながりません", msg)
        # 環境によって、待ち時間切れか、道がないか、のどちらかになる
        self.assertTrue(
            "1 秒待っても応答がありません" in msg or "経路がありません" in msg
            or "拒否" in msg, msg)
        self.assertLess(elapsed, 15)

    def test_unreachable_errors_on_each_os(self):
        """経路がないときのエラーの番号（Windows では WSA の番号）を、経路がないと伝える。"""
        for name in ("EHOSTUNREACH", "ENETUNREACH", "WSAEHOSTUNREACH", "WSAENETUNREACH"):
            code = getattr(errno, name, None)
            if code is None:
                continue
            with self.subTest(name=name):
                msg = deploy.connect_error_message("10.0.0.9", 21, 5, OSError(code, "x"))
                self.assertIn("機器までの経路がありません", msg)

    def test_unknown_host_name(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        ws.write_config(**base_config(21, host="no-such-board.invalid", timeout_seconds=2))
        code, data, _o, _e = ws.run()
        self.assertEqual(code, 1)
        self.assertIn("no-such-board.invalid", data["message"])


# =============================================================================
# サーバーあり
# =============================================================================

@needs_server
class FtpServerTest(unittest.TestCase):

    def root(self):
        path = tempfile.mkdtemp(prefix="deploy-ftp-root-")
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_upload_chmod_size_and_crc32(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        chmods = []
        with ftp_server(root, chmods=chmods) as port:
            ws.write_config(**base_config(port))
            code, data, out, err = ws.run()
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["skill"], SKILL_NAME)
        self.assertEqual(data["remote_dir"], "/opt/sensor")
        self.assertEqual(data["created_dirs"], ["/opt", "/opt/sensor"])
        on_server = os.path.join(root, "opt", "sensor", "sensor_app")
        self.assertEqual(read_file(on_server), APP)
        # 機器に送った権限の変更（SITE CHMOD）。Windows の FTP サーバーでは、実行の権限は残らない
        self.assertEqual(len(chmods), 1, chmods)
        self.assertTrue(os.path.samefile(chmods[0][0], on_server), chmods)
        self.assertEqual(chmods[0][1], "755")
        if os.name == "posix":
            self.assertEqual(os.stat(on_server).st_mode & 0o777, 0o755)
        expected = "%08x\n" % (zlib.crc32(APP) & 0xFFFFFFFF)
        sum_path = on_server + ".sum"
        self.assertEqual(read_file(sum_path).decode("ascii"), expected)
        entry = data["files"][0]
        self.assertEqual(entry["local"], os.path.join(ws.dir, "sensor_app"))
        self.assertEqual(entry["remote"], "/opt/sensor/sensor_app")
        self.assertEqual(entry["size"], len(APP))
        self.assertEqual(entry["remote_size"], len(APP))
        self.assertEqual(entry["mode"], "755")
        self.assertEqual(entry["checksum"], expected.strip())
        self.assertEqual(entry["checksum_file"], "/opt/sensor/sensor_app.sum")
        # PC 側にも同じサム値のファイルが残る（記録のフォルダの中）
        self.assertEqual(read_file(entry["checksum_local_file"]).decode(), expected)
        self.assertTrue(entry["checksum_local_file"].startswith(
            os.path.join(ws.data_dir, "logs") + os.sep), entry["checksum_local_file"])
        self.assertIn("127.0.0.1", data["message"])
        self.assertIn("755", data["message"])
        self.assertNotIn("権限は付けていません", data["message"])
        # パスワードはどこにも出さない
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(PASSWORD, err)
        with open(data["log"], encoding="utf-8") as fh:
            log_text = fh.read()
        self.assertNotIn(PASSWORD, log_text)
        self.assertIn("sensor_app", log_text)

    def test_sum32_file_content(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, checksum="sum32", checksum_suffix=".chk"))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 0, data)
        sum_path = os.path.join(root, "opt", "sensor", "sensor_app.chk")
        self.assertEqual(read_file(sum_path).decode("ascii"),
                         "%08x\n" % (sum(APP) & 0xFFFFFFFF))
        self.assertEqual(data["checksum_kind"], "sum32")

    def test_no_checksum_no_chmod(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        chmods = []
        with ftp_server(root, chmods=chmods) as port:
            ws.write_config(**base_config(port, checksum="none", file_mode=""))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 0, data)
        on_server = os.path.join(root, "opt", "sensor", "sensor_app")
        self.assertFalse(os.path.exists(on_server + ".sum"))
        self.assertEqual(chmods, [])
        if os.name == "posix":
            self.assertEqual(os.stat(on_server).st_mode & 0o777, 0o644)
        entry = data["files"][0]
        self.assertIsNone(entry["mode"])
        self.assertIsNone(entry["checksum"])
        self.assertIsNone(entry["checksum_file"])
        # 権限を付けていないことと、そのままでは実行できないことを伝える（「変更なし」とは言わない）
        msg = data["message"]
        self.assertIn("権限は付けていません（file_mode が空です）。", msg)
        self.assertIn("ふつうは実行できない権限になります。", msg)
        self.assertIn("file_mode に 755 などを書いてください。", msg)
        self.assertNotIn("変更なし", msg)
        self.assertNotIn("変えていません", msg)

    def test_nested_remote_dir_is_created(self):
        ws = Workspace(self)
        ws.file("build/out/sensor_app", APP)
        ws.file("conf/sensor.conf", b"rate=10\n")
        root = self.root()
        os.makedirs(os.path.join(root, "opt"))  # 親の一部はもうある
        with ftp_server(root) as port:
            ws.write_config(**base_config(
                port, files=["build/out/sensor_app", "conf/sensor.conf"],
                remote_dir="/opt/a/b/c/"))
            code, data, _o, _e = ws.run()
            self.assertEqual(code, 0, data)
            self.assertEqual(data["created_dirs"], ["/opt/a", "/opt/a/b", "/opt/a/b/c"])
            self.assertEqual(read_file(os.path.join(root, "opt/a/b/c/sensor_app")), APP)
            self.assertEqual(read_file(os.path.join(root, "opt/a/b/c/sensor.conf")),
                             b"rate=10\n")
            self.assertEqual(len(data["files"]), 2)
            # 2回目は、もうあるので作らない
            code, data, _o, _e = ws.run()
            self.assertEqual(code, 0, data)
            self.assertEqual(data["created_dirs"], [])

    def test_non_ascii_names(self):
        """日本語の名前のファイルとフォルダも送れる（Python 3.8 でも）。"""
        ws = Workspace(self)
        ws.file("センサー/計測_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, files=["センサー/計測_app"], remote_dir="/opt/計測"))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 0, data)
        self.assertEqual(data["files"][0]["remote"], "/opt/計測/計測_app")
        self.assertEqual(read_file(os.path.join(root, "opt", "計測", "計測_app")), APP)
        self.assertEqual(read_file(os.path.join(root, "opt", "計測", "計測_app.sum")).decode("ascii"),
                         "%08x\n" % (zlib.crc32(APP) & 0xFFFFFFFF))

    def test_relative_remote_dir(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, remote_dir="upload/x"))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 0, data)
        self.assertEqual(data["remote_dir"], "/upload/x")
        self.assertTrue(os.path.isfile(os.path.join(root, "upload", "x", "sensor_app")))

    def test_wrong_password_530(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, password="wrong-one"))
            code, data, out, _e = ws.run()
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertIn("ログインできません", data["message"])
        self.assertIn("パスワード", data["message"])
        self.assertIn("user と password", data["message"])
        self.assertNotIn("wrong-one", out)
        self.assertEqual(os.listdir(root), [])

    def test_empty_password_530_says_where_to_write_it(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, password="", password_env=""))
            code, data, _out, _e = ws.run()
            self.assertEqual(code, 1)
            self.assertIn("ログインできません", data["message"])
            self.assertIn("password も password_env も空", data["message"])
            self.assertIn(ws.config, data["message"])
            code, data, _out, _e = ws.run("check")
            self.assertEqual(code, 2)
            self.assertIn("password も password_env も空", data["message"])
            self.assertEqual(data["summary"]["password"], "")
            # 環境変数はあるが、中身が空のとき
            ws.write_config(**base_config(port, password="", password_env="DEPLOY_TEST_PW"))
            code, data, _out, _e = ws.run(env={"DEPLOY_TEST_PW": ""})
            self.assertEqual(code, 1)
            self.assertIn("環境変数 DEPLOY_TEST_PW の中身が空", data["message"])

    def test_password_from_env(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, password="wrong-one",
                                          password_env="DEPLOY_TEST_PW"))
            code, data, out, _e = ws.run(env={"DEPLOY_TEST_PW": PASSWORD})
        self.assertEqual(code, 0, data)
        self.assertNotIn(PASSWORD, out)

    def test_chmod_rejected(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root, perm="elradfmwT") as port:  # M がない = 権限を変えられない
            ws.write_config(**base_config(port))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertIn("権限を 755 に変えられません", data["message"])
        self.assertIn("実行できません", data["message"])
        # 送れたところまでは files に残る
        self.assertEqual(data["files"][0]["remote"], "/opt/sensor/sensor_app")
        self.assertIsNone(data["files"][0]["mode"])

    def test_size_not_supported_is_noted(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root, no_size=True) as port:
            ws.write_config(**base_config(port))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 0, data)
        self.assertIsNone(data["files"][0]["remote_size"])
        self.assertTrue(any("SIZE" in n for n in data["notes"]))
        self.assertIn("大きさは確かめていません", data["message"])

    def test_write_refused_550(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root, perm="elr") as port:  # 読むだけ
            ws.write_config(**base_config(port))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 1)
        self.assertIn("/opt", data["message"])
        self.assertIn("権限", data["message"])
        self.assertIn("user", data["message"])
        self.assertIn("remote_dir", data["message"])

    def test_active_mode(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, passive=False))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 0, data)
        self.assertEqual(read_file(os.path.join(root, "opt/sensor/sensor_app")), APP)

    def test_check_ok(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port))
            code, data, out, err = ws.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["problems"], [])
        # ログインできても、作業フォルダにファイルを作らない。機器の答えは標準エラーに出す
        self.assertNotIn("log", data)
        self.assertEqual(os.listdir(ws.data_dir), ["config.yaml"])
        self.assertIn("機器の答え：", err)
        self.assertNotIn(PASSWORD, err)
        summary = data["summary"]
        self.assertEqual(summary["host"], "127.0.0.1")
        self.assertEqual(summary["port"], port)
        self.assertEqual(summary["password"], deploy.MASK)
        self.assertEqual(summary["login"], "できた")
        self.assertEqual(summary["remote_home"], "/")
        self.assertNotIn(PASSWORD, out)
        self.assertEqual(data["notes"], [])
        self.assertEqual(data["message"], "設定に問題はありません。機器 127.0.0.1 に FTP でログインできました。")
        # check は何も送らない
        self.assertEqual(os.listdir(root), [])

    def test_check_ok_with_empty_file_mode(self):
        """file_mode が空でも問題にはしない。notes と message で知らせる。"""
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, file_mode=""))
            code, data, _o, _e = ws.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["problems"], [])
        self.assertNotIn("log", data)
        self.assertEqual(os.listdir(ws.data_dir), ["config.yaml"])
        self.assertEqual(data["notes"], [deploy.NO_MODE_NOTE])
        # 注意があるときは「問題はありません」と言い切らず、「ただし、」で注意を続ける
        self.assertEqual(data["message"],
                         "機器 127.0.0.1 に FTP でログインできました。ただし、" + deploy.NO_MODE_NOTE)
        self.assertNotIn("問題はありません", data["message"])

    def test_check_wrong_password(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, password="nope"))
            code, data, _o, err = ws.run("check")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertTrue(any("ログインできません" in p for p in data["problems"]))
        self.assertEqual(data["summary"]["login"], "できない")
        # 失敗の詳しいことは、記録のファイルを作らずに、標準エラーに出す
        self.assertIn("詳しく：", err)
        self.assertNotIn("log", data)
        self.assertEqual(os.listdir(ws.data_dir), ["config.yaml"])

    def test_check_refused(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        port = free_port()
        ws.write_config(**base_config(port))
        code, data, _o, _e = ws.run("check")
        self.assertEqual(code, 2)
        self.assertTrue(any("拒否" in p for p in data["problems"]))
        # 設定ではなく接続の問題なので、message はその原因をそのまま伝える
        self.assertIn("拒否", data["message"])
        self.assertIn("電源", data["message"])
        self.assertNotIn("直すところ", data["message"])


class SelfContainedTest(unittest.TestCase):
    """このスキルのフォルダだけで動き、フォルダの外を指さないこと。"""

    def files(self, top=SKILL_DIR):
        for base, dirs, names in os.walk(top):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in names:
                yield os.path.join(base, name)

    def test_no_reference_outside_the_folder(self):
        """どのテキストにも、親のフォルダへ出る書き方（.. と区切り）がないこと。"""
        patterns = [".." + "/", ".." + "\\"]
        checked = 0
        for path in self.files():
            with open(path, "rb") as fh:
                data = fh.read()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue  # テキストでないファイルは見ない
            checked += 1
            for pattern in patterns:
                self.assertNotIn(pattern, text, "%s に「%s」があります" % (path, pattern))
        self.assertGreaterEqual(checked, 5)

    def copy_skill(self, name):
        """このスキルのフォルダだけを、空の場所に name という名前で写す。"""
        place = tempfile.mkdtemp(prefix="deploy-copy-")
        self.addCleanup(shutil.rmtree, place, True)
        dest = os.path.join(place, name)
        shutil.copytree(SKILL_DIR, dest,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return place, dest

    def run_copy(self, dest, command, workdir):
        """写したスクリプトを、作業フォルダで（--workdir なしで）動かす。(終了コード, JSON, 標準エラー) を返す。"""
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, os.path.join(dest, "scripts", "deploy.py"), command],
            cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=60)
        out = decode_out(proc.stdout)
        err = decode_err(proc.stderr)
        try:
            data = json.loads(out)
        except ValueError:
            raise AssertionError("標準出力が JSON 1つではありません：%r\n標準エラー：%s"
                                 % (out, err))
        return proc.returncode, data, err

    def test_copied_folder_keeps_data_in_its_own_dot_folder(self):
        """写したフォルダの名前のとおりに、作業フォルダの「.<名前>」にだけデータができること。"""
        for name in (SKILL_NAME, "ftp-send-copy"):
            with self.subTest(name=name):
                place, dest = self.copy_skill(name)
                before = sorted(os.path.relpath(p, place) for p in self.files(place))
                workdir = os.path.realpath(tempfile.mkdtemp(prefix="deploy-work-"))
                self.addCleanup(shutil.rmtree, workdir, True)
                data_dir = os.path.join(workdir, "." + name)
                config = os.path.join(data_dir, "config.yaml")

                code, data, _err = self.run_copy(dest, "config-path", workdir)
                self.assertEqual(code, 0, data)
                self.assertEqual(data["config"], config)
                self.assertTrue(data["created"])
                self.assertEqual(data["missing"], ["host", "files", "remote_dir"])

                code, data, err = self.run_copy(dest, "check", workdir)
                self.assertEqual(code, 2, data)
                self.assertFalse(data["ok"])
                self.assertEqual(data["config"], config)
                self.assertFalse(data["created"])
                self.assertNotIn("log", data)
                # check は、作業フォルダにファイルを作らない
                self.assertEqual(sorted(os.listdir(data_dir)), [".gitignore", "config.yaml"])
                # 結果の skill、途中のようすの頭は、写したフォルダの名前
                self.assertEqual(data["skill"], name)
                lines = [l for l in err.splitlines() if l]
                self.assertTrue(lines)
                for line in lines:
                    self.assertTrue(line.startswith("[%s] " % name), line)

                code, data, err = self.run_copy(dest, "run", workdir)
                self.assertEqual(code, 2, data)
                self.assertEqual(data["skill"], name)
                self.assertTrue(data["log"].startswith(os.path.join(data_dir, "logs") + os.sep),
                                data["log"])
                self.assertTrue(os.path.isfile(data["log"]))
                # 記録のファイルの名前の頭も、写したフォルダの名前
                self.assertTrue(os.path.basename(data["log"]).startswith(name + "-"), data["log"])
                for line in [l for l in err.splitlines() if l]:
                    self.assertTrue(line.startswith("[%s] " % name), line)
                with open(data["log"], encoding="utf-8") as fh:
                    self.assertIn("%s run を始めます" % name, fh.readline())

                # 作業フォルダには「.<写したフォルダの名前>」だけができる
                self.assertEqual(os.listdir(workdir), ["." + name])
                self.assertEqual(sorted(os.listdir(data_dir)),
                                 [".gitignore", "config.yaml", "logs"])
                self.assertEqual(read_file(os.path.join(data_dir, ".gitignore")), b"*\n")
                self.assertEqual(read_file(config), read_file(os.path.join(dest, "config-template.yaml")))
                # 写したスキルのフォルダと、写した場所は変わらない
                after = sorted(os.path.relpath(p, place) for p in self.files(place))
                self.assertEqual(after, before)
                self.assertEqual(os.listdir(place), [name])

    def test_script_imports_only_stdlib(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        imports = re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", text, re.M)
        allowed = {"argparse", "codecs", "ctypes", "datetime", "errno", "ftplib", "json", "os", "posixpath",
                   "re", "shutil", "socket", "sys", "time", "traceback", "zlib"}
        self.assertEqual(set(i.split(".")[0] for i in imports) - allowed, set())
        self.assertNotIn("sys.path", text)


class SkillFilesTest(unittest.TestCase):
    """SKILL.md と、スクリプトの文の書き方。"""

    def test_skill_md_frontmatter(self):
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m)
        # 名前を変えて写すときは、フォルダの名前と name: を同じにする
        self.assertIn("name: %s\n" % SKILL_NAME, m.group(1) + "\n")
        self.assertRegex(m.group(1), r"(?m)^description: .+")
        # 名前を変えて写すと食い違うので、description と見出しにはスキルの名前を書かない
        desc = re.search(r"(?m)^description: (.+)$", m.group(1)).group(1)
        self.assertNotRegex(desc, r"/[A-Za-z]")
        self.assertLessEqual(len(desc), 1024)
        heading = re.search(r"(?m)^# (.+)$", text[m.end():]).group(1)
        self.assertNotIn("deploy", heading)
        for sub in ("run", "config-path", "check"):
            self.assertIn("scripts/deploy.py " + sub, text)

    def test_commands_follow_the_python_rule(self):
        """Windows では py -3（なければ python）、それ以外では python3。SKILL.md はそれを <python> と書く。"""
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("`<python>`", text)
        self.assertIn("py -3", text)
        commands = [l for l in text.splitlines() if "scripts/deploy.py" in l]
        self.assertEqual(len(commands), 3, commands)
        for line in commands:
            self.assertIn("<python> <このスキルのフォルダ>/scripts/deploy.py ", line)
        # Windows の PowerShell でも動くように、bash だけの書き方に頼らない
        for word in ("<<", "$(", "source ", "export "):
            self.assertNotIn(word, text)


    def test_messages_that_ask_to_check_name_concrete_things(self):
        """「確かめてください」の文は、何を確かめるかを具体的に言う。"""
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("ようすを確かめて", text)
        self.assertNotIn("状態を確かめて", text)


class ConsoleCodePageTest(unittest.TestCase):
    """Windows で、出力先がコンソールでないときは、コンソールの文字コードで書く。"""

    def _stream(self, tty):
        class Buffer(io.BytesIO):
            def isatty(self):
                return tty
        return io.TextIOWrapper(Buffer(), encoding="utf-8")

    def test_streams_follow_the_console_on_windows(self):
        m = deploy
        for console, tty, expected in (("cp932", False, "cp932"), ("cp932", True, "utf-8"),
                                       (None, False, "utf-8")):
            out, err = self._stream(tty), self._stream(tty)
            with mock.patch.object(m, "console_code_page", return_value=console), \
                    mock.patch.object(m.sys, "stdout", out), mock.patch.object(m.sys, "stderr", err):
                m.prepare_streams()
            for s in (out, err):
                self.assertEqual(codecs.lookup(s.encoding).name, expected, (console, tty))
                self.assertEqual(s.errors, "backslashreplace")

    def test_no_console_outside_windows(self):
        if os.name == "nt":
            self.skipTest("Windows では、コンソールの文字コードを返す")
        self.assertIsNone(deploy.console_code_page())


if __name__ == "__main__":
    unittest.main()
