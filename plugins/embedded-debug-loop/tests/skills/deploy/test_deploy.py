# -*- coding: utf-8 -*-
"""deploy スキル（scripts/deploy.py）の試験。

- サーバーなし：サム値の計算、設定ファイルの読み方と確かめ、サブコマンドの JSON。
- サーバーあり：試験の中で FTP サーバー（pyftpdlib）を立てて、実際に送る。
  pyftpdlib が読み込めないときは、その試験だけ飛ばす。
  pyftpdlib の場所は、環境変数 EDL_TEST_PYLIB（: 区切り）で足せる。

実行：python3 -m unittest discover -s tests/skills/deploy -t . -v
     （プラグインのフォルダで）
"""

import contextlib
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
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SKILL_DIR = os.path.join(PLUGIN_ROOT, "skills", "deploy")
SCRIPT = os.path.join(SKILL_DIR, "scripts", "deploy.py")
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")


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


# --- pyftpdlib（試験のときだけ使う。利用者の PC には要らない） ---------------

_PYLIB_CANDIDATES = [p for p in os.environ.get("EDL_TEST_PYLIB", "").split(os.pathsep) if p]


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
    "pyftpdlib が読み込めないので飛ばす（環境変数 EDL_TEST_PYLIB でその場所を指定できる）")

PASSWORD = "s3cret-Pw"


@contextlib.contextmanager
def ftp_server(root, perm="elradfmwMT", password=PASSWORD, no_size=False):
    """root を "/" にした FTP サーバーを、別のスレッドで動かす。ポート番号を返す。"""
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
        self.config = os.path.join(self.dir, ".embedded-debug-loop", "deploy.yaml")

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
        out = proc.stdout.decode("utf-8")
        err = proc.stderr.decode("utf-8")
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
        self.assertTrue(lines[0].startswith("# ") and "deploy" in lines[0])
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
        """例が試験のプロジェクトの名前だと、写すだけで埋まってしまう。"""
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("sensor", text)
        self.assertIn('# 例：["my_app"]', text)
        self.assertIn("# 例：/opt/myapp", text)

    def test_template_comments_do_not_point_by_position(self):
        """コメントは、設定の対話でそのまま質問の文になる。「下の」などはチャットでは通じない。"""
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

    def test_duplicate_basenames(self):
        ws = Workspace(self)
        ws.file("a/app", b"1")
        ws.file("b/app", b"2")
        _s, problems, _n, _r = self.check(ws, **base_config(21, files=["a/app", "b/app"]))
        self.assertTrue(any("同じ名前" in p for p in problems))

    def test_login_hint_names_empty_password_and_where_to_write(self):
        conf = "/w/.embedded-debug-loop/deploy.yaml"
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


class CommandWithoutServerTest(unittest.TestCase):

    def test_config_path_creates_from_template(self):
        ws = Workspace(self)
        code, data, _out, _err = ws.run("config-path")
        self.assertEqual(code, 0)
        self.assertEqual(set(data), {"config", "created", "missing"})
        self.assertEqual(data["config"], ws.config)
        self.assertTrue(data["created"])
        self.assertEqual(data["missing"], ["host", "files", "remote_dir"])
        self.assertEqual(read_file(ws.config), read_file(TEMPLATE))
        code, data, _out, _err = ws.run("config-path")
        self.assertEqual(code, 0)
        self.assertFalse(data["created"])

    def test_new_folder_gets_gitignore(self):
        ws = Workspace(self)
        ws.run("config-path")
        gi = os.path.join(ws.dir, ".embedded-debug-loop", ".gitignore")
        self.assertEqual(read_file(gi), b"*\n")
        # 2回目は、そのまま（書き直さない）
        with open(gi, "wb") as fh:
            fh.write(b"logs/\n")
        ws.run("config-path")
        self.assertEqual(read_file(gi), b"logs/\n")

    def test_gitignore_only_when_the_folder_is_made(self):
        ws = Workspace(self)
        os.makedirs(os.path.join(ws.dir, ".embedded-debug-loop"))
        ws.run("config-path")
        self.assertTrue(os.path.isfile(ws.config))
        self.assertFalse(os.path.exists(os.path.join(ws.dir, ".embedded-debug-loop", ".gitignore")))

    def test_gitignore_when_only_the_log_folder_is_made(self):
        ws = Workspace(self)
        other = os.path.join(ws.dir, "conf", "my-deploy.yaml")
        ws.run("check", "--config", other)
        self.assertEqual(read_file(os.path.join(ws.dir, ".embedded-debug-loop", ".gitignore")),
                         b"*\n")

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
        self.assertIn("/.embedded-debug-loop/logs/deploy-", data["log"])
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
        with ftp_server(root) as port:
            ws.write_config(**base_config(port))
            code, data, out, err = ws.run()
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["skill"], "deploy")
        self.assertEqual(data["remote_dir"], "/opt/sensor")
        self.assertEqual(data["created_dirs"], ["/opt", "/opt/sensor"])
        on_server = os.path.join(root, "opt", "sensor", "sensor_app")
        self.assertEqual(read_file(on_server), APP)
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
        self.assertIn("/.embedded-debug-loop/logs/", entry["checksum_local_file"])
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
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, checksum="none", file_mode=""))
            code, data, _o, _e = ws.run()
        self.assertEqual(code, 0, data)
        on_server = os.path.join(root, "opt", "sensor", "sensor_app")
        self.assertFalse(os.path.exists(on_server + ".sum"))
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
            code, data, out, _e = ws.run("check")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["problems"], [])
        summary = data["summary"]
        self.assertEqual(summary["host"], "127.0.0.1")
        self.assertEqual(summary["port"], port)
        self.assertEqual(summary["password"], deploy.MASK)
        self.assertEqual(summary["login"], "できた")
        self.assertEqual(summary["remote_home"], "/")
        self.assertNotIn(PASSWORD, out)
        # check は何も送らない
        self.assertEqual(os.listdir(root), [])

    def test_check_wrong_password(self):
        ws = Workspace(self)
        ws.file("sensor_app", APP)
        root = self.root()
        with ftp_server(root) as port:
            ws.write_config(**base_config(port, password="nope"))
            code, data, _o, _e = ws.run("check")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertTrue(any("ログインできません" in p for p in data["problems"]))
        self.assertEqual(data["summary"]["login"], "できない")

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


class IndependenceTest(unittest.TestCase):
    """このスキルのフォルダが、ほかの部品に触れていないこと。"""

    FORBIDDEN = [
        "setup", "セットアップ", "cross-compile", "log-capture",
        "device-worker", "orchestrator", "verifier",
        "作業エージェント", "統括", "検証エージェント", "Web管理画面",
        "extension", "エクステンション",
    ]

    def files(self):
        for base, _dirs, names in os.walk(SKILL_DIR):
            if "__pycache__" in base:
                continue
            for name in names:
                yield os.path.join(base, name)

    def test_no_forbidden_words(self):
        for path in self.files():
            with open(path, encoding="utf-8") as fh:
                text = fh.read().lower()
            for word in self.FORBIDDEN:
                self.assertNotIn(word.lower(), text, "%s に「%s」があります" % (path, word))

    def test_script_imports_only_stdlib(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        imports = re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", text, re.M)
        allowed = {"argparse", "datetime", "errno", "ftplib", "json", "os", "posixpath",
                   "re", "shutil", "socket", "sys", "time", "traceback", "zlib"}
        self.assertEqual(set(i.split(".")[0] for i in imports) - allowed, set())
        self.assertNotIn("sys.path", text)

    def test_skill_md_frontmatter(self):
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m)
        self.assertIn("name: deploy\n", m.group(1) + "\n")
        self.assertRegex(m.group(1), r"(?m)^description: .+")
        for sub in ("run", "config-path", "check"):
            self.assertIn("scripts/deploy.py " + sub, text)

    def test_skill_md_does_not_show_exit_codes(self):
        """終了コードで場合分けを書くと、モデルが利用者に見せてしまう。"""
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        for line in text.splitlines():
            if "終了コード" in line:
                self.assertIn("伝えません", line, line)
        self.assertNotRegex(text, r"[0-2] ?[がは]成功")
        self.assertIn("`.gitignore`", text)

    def test_skill_md_hides_json_names(self):
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        section = text.split("## 結果の伝え方", 1)[1].split("## 守ること", 1)[0]
        self.assertIn("JSON の項目名（`ok`、`mode`", section)
        self.assertIn("利用者に見せません", section)
        self.assertIn("設定ファイルの項目名（host、file_mode など）は、見せてかまいません", section)

    def test_skill_md_does_not_say_unchanged_permissions(self):
        """file_mode が空のとき「変更なし」と書くと、実行できるままだと思われる。"""
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("権限の欄に「付けていない」と書きます", text)
        self.assertIn("「変更なし」とは書きません", text)

    def test_messages_that_ask_to_check_name_concrete_things(self):
        """「確かめてください」の文は、何を確かめるかを具体的に言う。"""
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("ようすを確かめて", text)
        self.assertNotIn("状態を確かめて", text)


if __name__ == "__main__":
    unittest.main()
