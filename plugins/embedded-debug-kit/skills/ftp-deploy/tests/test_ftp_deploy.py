"""ftp-deploy のテスト。

このスキルのフォルダで動かす：python3 -B -m unittest discover -s tests（Windows は py -3）
-B を付けると、tests/__pycache__ を作らない。
FTP のテストは、pyftpdlib の FTP サーバーを別のスレッドで動かし、それを機器の代わりにする。
pyftpdlib がなければ FTP のテストを、pyOpenSSL がなければ FTPS のテストを飛ばす
（pip install -r tests/requirements.txt）。
"""

import contextlib
import datetime
import errno
import ftplib
import importlib.util
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SKILL_NAME = os.path.basename(SKILL_DIR)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "ftp_deploy.py")
TEMPLATE = os.path.join(SKILL_DIR, "assets", "config-template.yaml")
USER = "root"
PASSWORD = "Pw%x-secret"  # % があっても、そのまま読めること
ENV_NAME = "FTP_DEPLOY_TEST_PASSWORD"

try:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.ioloop import IOLoop
    from pyftpdlib.servers import FTPServer
    # ログの出し先がないと、pyftpdlib は自分で標準エラーに出し始める
    logging.getLogger("pyftpdlib").addHandler(logging.NullHandler())
    HAVE_FTPD = True
except ImportError:
    HAVE_FTPD = False

try:
    from pyftpdlib.handlers import TLS_FTPHandler  # pyOpenSSL がないと読み込めない
    from cryptography import x509  # pyOpenSSL が使うもの。テスト用の証明書を作る
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    HAVE_TLS = True
except ImportError:
    HAVE_TLS = False

needs_ftpd = unittest.skipUnless(
    HAVE_FTPD, "pyftpdlib がないので、FTP サーバーを使うテストを飛ばす（pip install -r tests/requirements.txt）")
needs_tls = unittest.skipUnless(
    HAVE_TLS, "pyOpenSSL がないので、FTPS のテストを飛ばす（pip install -r tests/requirements.txt）")


def load_script():
    """スクリプトを読み込む。スキルのフォルダに __pycache__ を作らない。"""
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("ftp_deploy", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


ftp_deploy = load_script()


def quoted(value):
    """YAML の '…' の形にする。値に記号や「: 」があっても、そのまま読める。"""
    return "'" + str(value).replace("'", "''") + "'"


class FakeDevice(object):
    """機器の代わりの FTP サーバー。root が機器の / になる。attrs で FTP のコマンドの動きや設定を替える。"""

    def __init__(self, test, perm="elradfmwMT", anonymous=False, password=PASSWORD, handler=None, **attrs):
        self.root = tempfile.mkdtemp(prefix="ftp-deploy-device-")
        test.addCleanup(shutil.rmtree, self.root, True)
        self.chmods = []
        base = handler or FTPHandler
        self.authorizer = authorizer = DummyAuthorizer()
        authorizer.add_user(USER, password, self.root, perm=perm)
        if anonymous:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # 書き込める anonymous への警告を出さない
                authorizer.add_anonymous(self.root, perm=perm)
        chmods = self.chmods

        def ftp_SITE_CHMOD(handler, path, mode):
            chmods.append((os.path.basename(path), mode))
            return base.ftp_SITE_CHMOD(handler, path, mode)

        attrs = dict({"authorizer": authorizer, "auth_failed_timeout": 0, "ftp_SITE_CHMOD": ftp_SITE_CHMOD}, **attrs)
        server = FTPServer(("127.0.0.1", 0), type("FakeHandler", (base,), attrs), ioloop=IOLoop())
        self.port = server.address[1]
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                server.serve_forever(timeout=0.05, blocking=False, handle_exit=False)
            server.close_all()

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        test.addCleanup(thread.join, 5)
        test.addCleanup(stop.set)

    def path(self, remote):
        return os.path.join(self.root, *remote.strip("/").split("/"))

    def read(self, remote):
        with open(self.path(remote), "rb") as fh:
            return fh.read()

    def listing(self):
        return sorted(os.path.relpath(os.path.join(d, n), self.root).replace(os.sep, "/")
                      for d, dirs, files in os.walk(self.root) for n in dirs + files)


def tls_device(test, **attrs):
    """FTPS の機器。テスト用の自己署名の証明書を、その場で作る。"""
    folder = tempfile.mkdtemp(prefix="ftp-deploy-cert-")
    test.addCleanup(shutil.rmtree, folder, True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    pem = os.path.join(folder, "device.pem")
    with open(pem, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return FakeDevice(test, handler=TLS_FTPHandler, certfile=pem, tls_control_required=True,
                      tls_data_required=True, **attrs)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Case(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="ftp-deploy-work-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.data = os.path.join(self.work, "." + SKILL_NAME)
        self.config_path = os.path.join(self.data, "config.yaml")
        self.app = self.write("build/app", b"\x7fELF app v2\n")

    def write(self, rel, data):
        path = os.path.join(self.work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return os.path.normpath(path)  # スクリプトが返す local と同じ形にする（Windows では \\ で区切る）

    def config(self, port=21, **values):
        settings = {"host": "127.0.0.1", "port": port, "user": USER, "password": PASSWORD,
                    "files": ["build/app"], "remote_dir": "/opt/app", "timeout_seconds": 10}
        settings.update(values)
        lines = ["# テストの設定"]
        for key, value in settings.items():
            if isinstance(value, list):
                lines += ["%s:" % key] + ["  - " + quoted(item) for item in value]
            elif value is not None:
                lines.append("%s: %s" % (key, quoted(value)))
        os.makedirs(self.data, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def run_script(self, command, env=None, secret=PASSWORD):
        full_env = dict(os.environ)
        full_env.pop(ENV_NAME, None)
        full_env.update(env or {})
        full_env["PYTHONIOENCODING"] = "utf-8"  # スクリプトより前に Python が出す文も、UTF-8 で読めるようにする
        proc = subprocess.run([sys.executable, SCRIPT, command, "--workdir", self.work],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env, timeout=60)
        out = proc.stdout.decode("ascii")  # 結果は ASCII だけで書く
        self.assertEqual(len(out.splitlines()), 1, out)
        data = json.loads(out)
        for key in ("ok", "message", "problems", "config"):
            self.assertIn(key, data)
        for key in ("log", "created"):
            self.assertNotIn(key, data)
        self.stderr = proc.stderr.decode("utf-8")  # 進み具合と機器の答え
        if secret:
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, self.stderr)
        return proc.returncode, data

    def local_listing(self):
        return sorted(os.path.relpath(os.path.join(d, n), self.work)
                      for d, dirs, files in os.walk(self.work) for n in dirs + files)


class ConfigTest(Case):
    def test_check_and_run_without_config_create_nothing(self):
        before = self.local_listing()
        for command in ("check", "run"):
            code, data = self.run_script(command)
            self.assertEqual((code, data["ok"]), (2, False))
            self.assertEqual(data["message"], "設定ファイル %s がありません。設定のスキルで準備してください。手で作るときは、スキルのフォルダの "
                             "assets/config-template.yaml を写して、値を書いてください。" % self.config_path)
            self.assertEqual(data["problems"], [data["message"]])
            self.assertEqual(self.local_listing(), before)

    def test_template_comments(self):
        with open(TEMPLATE, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(tuple(key for key, _, _, _ in ftp_deploy.read_yaml(lines)), ftp_deploy.KEYS)
        for key in ftp_deploy.KEYS:
            index = next(i for i, line in enumerate(lines) if line.startswith(key + ":"))
            comments = []
            for line in reversed(lines[:index]):
                if not line.startswith("#"):
                    break
                comments.append(line)
            self.assertTrue(comments, key)
            joined = "\n".join(comments)
            self.assertEqual("（必須）" in joined, key in ("host", "files", "remote_dir"), key)
            self.assertEqual("（秘密）" in joined, key == "password", key)
            self.assertEqual("選べる値：" in joined, key in ("passive", "tls"), key)

    def test_template_values_pass_except_required(self):
        os.makedirs(self.data)
        shutil.copyfile(TEMPLATE, self.config_path)
        code, data = self.run_script("check")
        self.assertEqual(code, 2)
        self.assertEqual(len(data["problems"]), 3, data["problems"])
        for key in ("host", "files", "remote_dir"):
            self.assertTrue(any(p.startswith(key) for p in data["problems"]), key)

    def test_bom_percent_and_duplicate_key(self):
        os.makedirs(self.data)
        with open(self.config_path, "w", encoding="utf-8-sig") as fh:
            fh.write("password: %s\nfiles:\n  - build/app\nremote_dir: app\n" % PASSWORD)
        code, data = self.run_script("check")
        self.assertEqual(data["problems"], ["host（機器のアドレス）が空です。"])
        with open(self.config_path, "a", encoding="utf-8") as fh:
            fh.write("remote_dir: again\n")
        code, data = self.run_script("check")
        self.assertEqual(code, 2)
        self.assertIn("2回", data["message"])

    def test_each_problem_is_reported(self):
        self.config(port="99999", passive="maybe", tls="2", file_mode="9x", timeout_seconds="0",
                    remote_dir="/opt/../etc", files=["build/app", "build/missing", "build"], hots="x")
        code, data = self.run_script("run")
        self.assertEqual((code, data["ok"]), (2, False))
        text = "\n".join(data["problems"])
        for word in ("port", "passive", "tls", "file_mode", "timeout_seconds", "..", "build/missing",
                     "フォルダです", "hots"):
            self.assertIn(word, text)
        self.assertEqual(len(data["problems"]), 9, text)
        self.assertIn('"password": "***"', self.stderr)  # 設定を標準エラー出力に書くときも、パスワードは伏せる

    def test_windows_style_remote_dir(self):
        self.config(remote_dir="C:\\opt\\app")
        code, data = self.run_script("check")
        self.assertEqual(code, 2)
        self.assertIn("/ で区切って", data["message"])

    def test_names_that_leave_remote_dir(self):
        for name in ("", ".", "..", "a\\..\\b", "a/b", "a\nDELE x", "a\x00"):
            self.assertFalse(ftp_deploy.safe_name(name), repr(name))
        self.assertTrue(ftp_deploy.safe_name("my app-1.0.bin"))
        if os.name != "nt":  # Windows では、\ の入ったファイルを作れない
            self.write("..\\..\\escape.bin", b"x")
            self.config(files=["..\\..\\escape.bin"])
            code, data = self.run_script("run")
            self.assertEqual(code, 2)
            self.assertIn("..\\..\\escape.bin", data["problems"][0])

    def test_copied_folder_uses_its_own_name(self):
        copy_root = tempfile.mkdtemp(prefix="ftp-deploy-copy-")
        self.addCleanup(shutil.rmtree, copy_root, True)
        copy = os.path.join(copy_root, "my-ftp")
        shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        proc = subprocess.run([sys.executable, os.path.join(copy, "scripts", "ftp_deploy.py"), "check",
                               "--workdir", self.work], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 2)  # 設定ファイルがない
        self.assertEqual(json.loads(proc.stdout.decode("ascii"))["config"],
                         os.path.join(self.work, ".my-ftp", "config.yaml"))

    def test_bad_arguments_answer_json(self):
        code, data = self.run_script("deploy")
        self.assertEqual((code, data["ok"]), (2, False))
        self.assertIn("使い方", data["message"])


class MessageTest(unittest.TestCase):
    """接続の失敗を、平易な文にすること。"""

    settings = {"host": "device.local", "port": 21, "user": USER, "timeout": 5.0}

    def explain(self, e, **kw):
        return ftp_deploy.explain("つながりません", e, self.settings, **kw)

    def test_each_kind(self):
        cases = [
            (socket.gaierror(-2, "Name or service not known"), "device.local が見つかりません"),
            (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), "FTP サーバーが動いていない"),
            (socket.timeout("timed out"), "5 秒待っても"),
            (OSError(errno.ENETUNREACH, "Network is unreachable"), "経路がありません"),
            (EOFError(), "接続が切れました"),
            (ConnectionResetError(errno.ECONNRESET, "reset"), "接続が切れました"),
            (ftplib.error_temp("421 Timeout."), "接続を切りました"),
            (ftplib.error_perm("550 Permission denied."), "権限がないか"),
            (ftplib.error_temp("452 Insufficient storage space."), "空き容量が足りません"),
            (ftplib.error_perm("552 Exceeded storage allocation."), "空き容量が足りません"),
        ]
        for e, words in cases:
            self.assertIn(words, self.explain(e), repr(e))
        self.assertIn("パスワードが違います", self.explain(ftplib.error_perm("530 Login incorrect."), login=True))
        self.assertIn("passive", self.explain(socket.timeout("timed out"), transfer=True))

    def test_anonymous_login_refused(self):
        settings = dict(self.settings, user="")
        message = ftp_deploy.explain("ログインできません", ftplib.error_perm("530 No anonymous."), settings, login=True)
        self.assertIn("anonymous", message)

    def test_mask(self):
        ftp_deploy.SECRETS[:] = ["pw1"]
        try:
            self.assertEqual(ftp_deploy.mask(ftplib.error_perm("530 x pw1 y")), "530 x *** y")
        finally:
            ftp_deploy.SECRETS[:] = []

    def test_reply_that_cannot_be_read(self):
        self.assertIn("UTF-8 で読めません", self.explain(UnicodeDecodeError("utf-8", b"\x82", 0, 1, "x")))
        self.assertIn("答えの形が違います", self.explain(ValueError("invalid literal for int(): 'abc'")))
        self.assertIn("暗号化", self.explain(ftplib.error_perm("522 SSL connection failed: session reuse required")))


class NoServerTest(Case):
    def test_refused_port_stops_at_once(self):
        self.config(port=free_port())
        started = time.time()
        code, data = self.run_script("run")
        self.assertEqual((code, data["ok"]), (1, False))
        self.assertIn("FTP サーバーが動いていない", data["message"])
        self.assertLess(time.time() - started, 10)
        self.assertEqual(data["files"], [])

    def test_timeout_when_device_does_not_answer(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)  # つながるが、何も答えない
        self.addCleanup(sock.close)
        self.config(port=sock.getsockname()[1], timeout_seconds=1)
        code, data = self.run_script("check")
        self.assertEqual(code, 1)
        self.assertIn("1 秒待っても機器が応答しません", data["message"])


@needs_ftpd
class DeviceTest(Case):
    def test_run_sends_files(self):
        device = FakeDevice(self)
        conf = self.write("conf/app.conf", b"rate=10\n")
        self.config(device.port, files=["build/app", conf], remote_dir="/opt/app")
        code, data = self.run_script("run")
        self.assertEqual((code, data["ok"]), (0, True), data["message"])
        self.assertEqual(data["remote_dir"], "/opt/app")
        self.assertEqual(data["files"], [
            {"local": self.app, "remote": "/opt/app/app", "size": 12, "mode": None},
            {"local": conf, "remote": "/opt/app/app.conf", "size": 8, "mode": None},
        ])
        self.assertEqual(device.read("/opt/app/app"), b"\x7fELF app v2\n")
        self.assertEqual(device.listing(), ["opt", "opt/app", "opt/app/app", "opt/app/app.conf"])
        self.assertEqual(device.chmods, [])

    def test_run_writes_progress_to_stderr_and_no_files(self):
        device = FakeDevice(self)
        self.config(device.port)
        before = self.local_listing()
        code, data = self.run_script("run")  # run_script は、結果と標準エラー出力にパスワードがないことを確かめる
        self.assertEqual(code, 0, data["message"])
        lines = self.stderr.splitlines()
        self.assertIn('"password": "***"', self.stderr)
        for words in ("run を始めます", "番ポートに FTP でつなぎます", "230 ", "として送ります", "226 ",
                      "/opt/app/app を送りました。"):
            self.assertTrue(any(words in line for line in lines), words)  # 機器の答え（230、226）も出る
        self.assertEqual(lines[-1], data["message"])
        self.assertEqual(self.local_listing(), before)  # logs も .gitignore も作らない
        self.assertFalse(os.path.exists(os.path.join(self.data, "logs")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "logs")))

    def test_file_mode(self):
        device = FakeDevice(self)
        self.config(device.port, file_mode="755")
        code, data = self.run_script("run")
        self.assertEqual(code, 0, data["message"])
        self.assertEqual(device.chmods, [("app.part", "755")])  # 置き換える前に変える
        self.assertEqual(data["files"][0]["mode"], "755")
        if os.name == "posix":
            self.assertEqual(os.stat(device.path("/opt/app/app")).st_mode & 0o777, 0o755)

    def device_with_old_file(self, **attrs):
        device = FakeDevice(self, **attrs)
        os.makedirs(device.path("/opt/app"))
        with open(device.path("/opt/app/app"), "wb") as fh:
            fh.write(b"old")
        return device

    def assert_failed_cleanly(self, device, code, data, words):
        self.assertEqual((code, data["ok"]), (1, False))
        self.assertIn(words, data["message"])
        self.assertEqual(device.read("/opt/app/app"), b"old")  # 元のファイルはそのまま
        self.assertEqual(device.listing(), ["opt", "opt/app", "opt/app/app"])  # .part は残さない

    def test_replaces_old_file_when_rename_over_it_is_refused(self):
        def ftp_RNTO(handler, path):
            if os.path.exists(path):  # Windows の FTP サーバーのように、上書きの名前の変更を断る
                return handler.respond("550 File exists.")
            return FTPHandler.ftp_RNTO(handler, path)

        device = self.device_with_old_file(ftp_RNTO=ftp_RNTO)
        self.config(device.port)
        code, data = self.run_script("run")
        self.assertEqual(code, 0, data["message"])
        self.assertEqual(device.read("/opt/app/app"), b"\x7fELF app v2\n")
        self.assertEqual(device.listing(), ["opt", "opt/app", "opt/app/app"])

    def test_rename_refused_keeps_old_file(self):
        device = self.device_with_old_file(perm="elradmwMT")  # 名前を変える権限（f）がない
        self.config(device.port)
        code, data = self.run_script("run")
        self.assert_failed_cleanly(device, code, data, "置き換えられません")

    def assert_kept_part(self, code, data, device, words):
        self.assertEqual(code, 1)
        self.assertIn(words, data["message"])
        self.assertIn("古い app は消しました", data["message"])
        self.assertEqual(device.listing(), ["opt", "opt/app", "opt/app/app.part"])
        self.assertEqual(device.read("/opt/app/app.part"), b"\x7fELF app v2\n")

    def test_old_file_removed_but_rename_refused_keeps_part(self):
        device = self.device_with_old_file(ftp_RNTO=lambda handler, path: handler.respond("550 Refused."))
        self.config(device.port)
        code, data = self.run_script("run")
        self.assert_kept_part(code, data, device, "置き換えられません")

    def test_old_file_removed_then_connection_lost_keeps_part(self):
        def ftp_RNTO(handler, path):
            if os.path.exists(path):
                return handler.respond("550 File exists.")
            handler.close()  # 古いファイルを消したあとで切れる

        device = self.device_with_old_file(ftp_RNTO=ftp_RNTO)
        self.config(device.port)
        code, data = self.run_script("run")
        self.assert_kept_part(code, data, device, "途中で接続が切れました")

    def test_size_mismatch_removes_part_file(self):
        device = self.device_with_old_file(ftp_SIZE=lambda handler, path: handler.respond("213 1"))
        self.config(device.port)
        code, data = self.run_script("run")
        self.assert_failed_cleanly(device, code, data, "サイズが合いません")

    def test_malformed_size_reply_removes_part_file(self):
        device = self.device_with_old_file(ftp_SIZE=lambda handler, path: handler.respond("213 abc"))
        self.config(device.port)
        code, data = self.run_script("run")
        self.assert_failed_cleanly(device, code, data, "答えの形が違います")

    def test_part_file_that_cannot_be_removed_is_reported(self):
        device = self.device_with_old_file(perm="elrafmwMT",  # 消す権限（d）がない
                                           ftp_SIZE=lambda handler, path: handler.respond("213 1"))
        self.config(device.port)
        code, data = self.run_script("run")
        self.assertEqual(code, 1)
        self.assertIn("機器に app.part が残っているかもしれません", data["message"])
        self.assertEqual(device.read("/opt/app/app"), b"old")

    def test_chmod_refused_removes_part_file(self):
        device = self.device_with_old_file(
            ftp_SITE_CHMOD=lambda handler, path, mode: handler.respond("550 Not allowed."))
        self.config(device.port, file_mode="755")
        code, data = self.run_script("run")
        self.assert_failed_cleanly(device, code, data, "権限を 755 にできません")

    def test_disk_full(self):
        def ftp_STOR(handler, file, mode="w"):
            with open(file, "wb") as fh:  # 途中まで書いて止まった
                fh.write(b"\x7f")
            handler.respond("452 Insufficient storage space.")

        device = self.device_with_old_file(ftp_STOR=ftp_STOR)
        self.config(device.port)
        code, data = self.run_script("run")
        self.assert_failed_cleanly(device, code, data, "空き容量が足りません")

    def test_connection_lost_while_sending(self):
        def ftp_STOR(handler, file, mode="w"):
            with open(file, "wb") as fh:
                fh.write(b"\x7f")
            handler.close()

        device = self.device_with_old_file(ftp_STOR=ftp_STOR)
        self.config(device.port)
        code, data = self.run_script("run")
        self.assertEqual(code, 1)
        self.assertIn("途中で接続が切れました", data["message"])
        self.assertIn("機器に app.part が残っているかもしれません", data["message"])

    def test_stops_at_first_failure_and_lists_files_already_sent(self):
        def ftp_SIZE(handler, path):
            if path.endswith("second.part"):
                return handler.respond("213 99")
            return FTPHandler.ftp_SIZE(handler, path)

        device = self.device_with_old_file(ftp_SIZE=ftp_SIZE)
        self.write("build/second", b"2")
        self.write("build/third", b"3")
        self.config(device.port, files=["build/app", "build/second", "build/third"])
        code, data = self.run_script("run")
        self.assertEqual(code, 1)
        self.assertEqual([f["remote"] for f in data["files"]], ["/opt/app/app"])  # 置き換え済み
        self.assertEqual(device.listing(), ["opt", "opt/app", "opt/app/app"])

    def test_active_mode(self):
        device = FakeDevice(self)
        self.config(device.port, passive="false")
        code, data = self.run_script("run")
        self.assertEqual(code, 0, data["message"])
        self.assertEqual(device.read("/opt/app/app"), b"\x7fELF app v2\n")

    def test_permission_denied(self):
        device = FakeDevice(self, perm="elr")
        self.config(device.port)
        code, data = self.run_script("run")
        self.assertEqual(code, 1)
        self.assertIn("権限", data["message"])
        self.assertEqual(device.listing(), [])

    def test_anonymous_and_relative_remote_dir(self):
        device = FakeDevice(self, anonymous=True)
        self.config(device.port, user="", password=None, remote_dir="app")
        code, data = self.run_script("run")
        self.assertEqual(code, 0, data["message"])
        self.assertEqual(data["files"][0]["remote"], "/app/app")
        self.assertEqual(device.read("/app/app"), b"\x7fELF app v2\n")

    def test_wrong_password(self):
        device = FakeDevice(self)
        self.config(device.port, password="wrong")
        code, data = self.run_script("run")
        self.assertEqual((code, data["ok"]), (1, False))
        self.assertIn("ユーザー名かパスワードが違います", data["message"])
        self.assertEqual(device.listing(), [])

    def test_password_is_masked_even_if_the_device_echoes_it(self):
        device = FakeDevice(self, ftp_PASS=lambda handler, line: handler.respond("530 Bad password %s." % line))
        self.config(device.port)
        code, data = self.run_script("run")  # run_script は、結果と標準エラー出力にパスワードがないことを確かめる
        self.assertEqual(code, 1)
        self.assertIn("Bad password ***", data["message"])
        self.assertIn("Bad password ***", self.stderr)

    def test_short_password_does_not_change_paths(self):
        device = FakeDevice(self, password="root")
        self.config(device.port, password="root", remote_dir="/root/bin")
        code, data = self.run_script("run", secret=None)
        self.assertEqual(code, 0, data["message"])
        self.assertEqual((data["remote_dir"], data["files"][0]["remote"]), ("/root/bin", "/root/bin/app"))
        self.assertIn("ユーザー root", self.stderr)
        self.assertNotIn('"password": "root"', self.stderr)

    def test_reply_not_in_utf8(self):
        device = FakeDevice(self, banner="ようこそ", encoding="shift_jis")
        self.config(device.port)
        code, data = self.run_script("check")
        self.assertEqual(code, 1)
        self.assertIn("UTF-8 で読めません", data["message"])

    def test_password_env_wins(self):
        device = FakeDevice(self)
        self.config(device.port, password="wrong", password_env=ENV_NAME)
        code, data = self.run_script("run", env={ENV_NAME: PASSWORD})
        self.assertEqual(code, 0, data["message"])
        code, data = self.run_script("check")  # 環境変数がなければ、password の値を使う
        self.assertEqual(code, 1)
        self.config(device.port, password=None, password_env=ENV_NAME)
        code, data = self.run_script("check")
        self.assertEqual(code, 2)
        self.assertIn(ENV_NAME, data["message"])

    def test_check_creates_nothing(self):
        device = FakeDevice(self)
        os.makedirs(device.path("/opt/app"))
        self.config(device.port)
        before = self.local_listing()
        code, data = self.run_script("check")
        self.assertEqual((code, data["ok"]), (0, True), data["message"])
        self.assertEqual((data["remote_dir"], data["notes"]), ("/opt/app", []))
        self.assertEqual(self.local_listing(), before)
        self.assertEqual(device.listing(), ["opt", "opt/app"])

    def test_check_before_build_and_before_remote_dir(self):
        device = FakeDevice(self)
        os.makedirs(device.path("/opt"))
        os.remove(self.app)
        self.config(device.port)
        code, data = self.run_script("check")
        self.assertEqual((code, data["ok"]), (0, True), data["message"])
        self.assertEqual(len(data["notes"]), 2, data["notes"])
        self.assertIn("build/app は、まだありません", data["message"])
        self.assertIn("/opt/app は、まだありません。run のときに作ります", data["message"])
        self.assertIsNone(data["remote_dir"])
        self.assertEqual(device.listing(), ["opt"])
        code, data = self.run_script("run")  # run では、ないファイルは設定の問題
        self.assertEqual(code, 2)
        self.assertIn("build/app がありません", data["message"])

    def test_check_remote_dir_without_permission(self):
        device = FakeDevice(self)
        os.makedirs(device.path("/opt/app"))
        device.authorizer.override_perm(USER, device.path("/opt/app"), "lr")  # 入る権限（e）がない
        self.config(device.port)
        code, data = self.run_script("check")
        self.assertEqual((code, data["ok"]), (1, False))
        self.assertIn("/opt/app に入れません", data["message"])
        self.assertIn("権限", data["message"])

    def test_tls_on_a_server_without_ftps(self):
        device = FakeDevice(self)
        self.config(device.port, tls="true")
        code, data = self.run_script("check")
        self.assertEqual(code, 1)
        self.assertIn("FTPS", data["message"])
        self.assertIn("tls を false", data["message"])


@needs_ftpd
@needs_tls
class FTPSTest(Case):
    def test_run_and_check(self):
        device = tls_device(self)
        self.config(device.port, tls="true", file_mode="755")
        code, data = self.run_script("run")
        self.assertEqual(code, 0, data["message"])
        self.assertEqual(device.read("/opt/app/app"), b"\x7fELF app v2\n")
        code, data = self.run_script("check")  # check も、ファイルを送るときと同じ接続を開く
        self.assertEqual(code, 0, data["message"])

    def test_reuses_tls_session_for_transfers(self):
        # vsftpd などは、ファイルを送る接続で TLS セッションを使い回さないと断る
        device = tls_device(self)
        with contextlib.redirect_stderr(io.StringIO()):  # 進み具合を、テストの出力に混ぜない
            ftp = ftp_deploy.connect({"host": "127.0.0.1", "port": device.port, "user": USER, "password": PASSWORD,
                                      "tls": True, "passive": True, "timeout": 10.0})
        try:
            conn, _ = ftp.ntransfercmd("NLST")
            self.assertTrue(conn.session_reused)
            conn.close()  # 読み終える前に閉じるので、機器は 426 を返すことがある。close は、それを気にしない
        finally:
            ftp_deploy.close(ftp)


if __name__ == "__main__":
    unittest.main()
