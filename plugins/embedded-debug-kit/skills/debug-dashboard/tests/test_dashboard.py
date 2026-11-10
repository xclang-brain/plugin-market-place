"""dashboard.py の start・status・stop を通して動かすテスト（REQ-402、REQ-405、REQ-407）。

  python3 -m unittest discover -s tests
"""

import http.server
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import unittest

import support
from support import DATA, temp_project


def dashboard(*args, cwd=None, **env):
    proc = subprocess.run([sys.executable, str(support.SCRIPTS / "dashboard.py")] + list(args), cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=support.clean_env(**env))
    out = proc.stdout.decode("ascii")  # JSON は ASCII だけで書く
    return proc.returncode, json.loads(out)


class OtherServer(http.server.BaseHTTPRequestHandler):
    """前の server.json のポートで動いている、別の HTTP サーバー"""

    def do_GET(self):
        body = b'{"ok": true, "workdir": "/somewhere/else"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.project = temp_project(self)
        self.workdir = str(self.project)

    def write_server_json(self, port):
        base = self.project / DATA
        base.mkdir()
        info = {"pid": 123456, "port": port, "url": "http://127.0.0.1:%d/#k=old" % port, "token": "old"}
        (base / "server.json").write_text(json.dumps(info), encoding="utf-8")
        return info

    def test_start_status_stop(self):
        self.addCleanup(dashboard, "stop", "--workdir", self.workdir)
        code, result = dashboard("start", "--workdir", self.workdir)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ok"] and result["running"])
        self.assertRegex(result["url"], r"^http://127\.0\.0\.1:\d+/#k=[A-Za-z0-9_-]+$")
        self.assertIn(result["url"], result["message"])
        for key in ("problems", "config", "log"):
            self.assertIn(key, result)
        base = self.project / DATA
        self.assertEqual((base / ".gitignore").read_text(encoding="utf-8").strip(), "*")
        self.assertTrue((base / "recording").is_file())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o700)

        code, again = dashboard("start", "--workdir", self.workdir)
        self.assertEqual((code, again["url"]), (0, result["url"]), "動いているサーバーを使い直していません")

        code, status = dashboard("status", "--workdir", self.workdir)
        self.assertEqual((code, status["running"], status["url"]), (0, True, result["url"]))

        code, stopped = dashboard("stop", "--workdir", self.workdir)
        self.assertEqual((code, stopped["ok"]), (0, True), stopped)
        self.assertFalse((base / "recording").exists())
        self.assertFalse(dashboard("status", "--workdir", self.workdir)[1]["running"])

    def test_stop_marks_do_not_outlive_the_server(self):
        """画面を終了すると「実行を止める」も解ける。落ちた画面のフラグは、次の起動で消える"""
        self.addCleanup(dashboard, "stop", "--workdir", self.workdir)
        stop = self.project / DATA / "sessions" / "s1" / "stop"
        self.assertEqual(dashboard("start", "--workdir", self.workdir)[0], 0)
        stop.parent.mkdir(parents=True)
        stop.write_text("")
        self.assertEqual(dashboard("stop", "--workdir", self.workdir)[0], 0)
        self.assertFalse(stop.exists(), "stop が停止フラグを消していません")
        stop.write_text("")
        self.assertEqual(dashboard("start", "--workdir", self.workdir)[0], 0)
        self.assertFalse(stop.exists(), "start が前の停止フラグを消していません")

    def test_workdir_defaults_to_the_project_dir(self):
        """シェルが下のフォルダに移っていても、フックと同じ CLAUDE_PROJECT_DIR を使う"""
        sub = self.project / "build"
        sub.mkdir()
        env = {"CLAUDE_PROJECT_DIR": self.workdir}
        self.addCleanup(dashboard, "stop", cwd=str(sub), **env)
        code, result = dashboard("start", cwd=str(sub), **env)
        self.assertEqual(code, 0, result)
        self.assertTrue((self.project / DATA / "recording").is_file())
        self.assertFalse((sub / DATA).exists())

    def test_copilot_uses_the_repository_root(self):
        """Copilot CLI のフックは、Git のリポジトリのいちばん上のフォルダを作業フォルダとして渡す。シェルにはそれが渡らない"""
        (self.project / ".git").mkdir()
        sub = self.project / "src" / "app"
        sub.mkdir(parents=True)
        self.addCleanup(dashboard, "stop", cwd=str(sub), COPILOT_CLI="1")
        code, result = dashboard("start", cwd=str(sub), COPILOT_CLI="1")
        self.assertEqual(code, 0, result)
        self.assertTrue((self.project / DATA / "recording").is_file())
        self.assertFalse((sub / DATA).exists())
        self.assertEqual(dashboard("status", cwd=str(sub))[1]["running"], False, "Claude Code では今のフォルダを使う")

    def test_unresponsive_server_is_left_alone(self):
        """応答しないサーバーのフラグを消さず、2つ目のサーバーも起動しない"""
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)  # 受け付けるだけで、応答しない
        info = self.write_server_json(listener.getsockname()[1])
        for command in ("status", "stop", "start"):
            code, result = dashboard(command, "--workdir", self.workdir)
            self.assertEqual((code, result["ok"]), (1, False), command)
            self.assertIn("123456", result["message"], command)
        saved = json.loads((self.project / DATA / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, info)

    def test_other_server_on_the_old_port_is_not_taken_for_ours(self):
        other = http.server.HTTPServer(("127.0.0.1", 0), OtherServer)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        port = other.server_address[1]
        self.write_server_json(port)
        self.addCleanup(dashboard, "stop", "--workdir", self.workdir)
        code, result = dashboard("start", "--workdir", self.workdir)
        self.assertEqual(code, 0, result)
        self.assertNotIn(":%d/" % port, result["url"])
        self.assertTrue((self.project / DATA / "recording").is_file())

    def test_status_and_stop_create_nothing(self):
        for command in ("status", "stop"):
            code, result = dashboard(command, "--workdir", self.workdir)
            self.assertEqual((code, result["ok"], result["running"]), (0, True, False))
        self.assertEqual(os.listdir(self.workdir), [])

    def test_missing_workdir(self):
        code, result = dashboard("start", "--workdir", str(self.project / "nothing"))
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertTrue(result["problems"])

    def test_unknown_command(self):
        code, result = dashboard("restart", "--workdir", self.workdir)
        self.assertEqual((code, result["ok"]), (2, False))

    def test_symlinked_folder_is_refused(self):
        if not support.can_symlink(self.project):
            self.skipTest("この環境ではシンボリックリンクを作れません")
        other = temp_project(self)
        os.symlink(str(other), str(self.project / DATA))
        code, result = dashboard("start", "--workdir", self.workdir)
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertEqual(os.listdir(str(other)), [])

    def test_symlinks_inside_the_folder_are_refused(self):
        """logs/ や .gitignore がシンボリックリンクなら、その先に書かない"""
        if not support.can_symlink(self.project):
            self.skipTest("この環境ではシンボリックリンクを作れません")
        outside = temp_project(self)
        base = self.project / DATA
        base.mkdir()
        for name, target in (("logs", outside), (".gitignore", outside / "victim.txt"),
                             ("logs/server.log", outside / "server.log")):
            with self.subTest(name=name):
                if name.startswith("logs/"):
                    (base / "logs").mkdir()
                os.symlink(str(target), str(base / name))
                code, result = dashboard("start", "--workdir", self.workdir)
                self.assertEqual((code, result["ok"]), (2, False), result)
                self.assertEqual(os.listdir(str(outside)), [])
                os.remove(str(base / name))
                if name.startswith("logs/"):
                    (base / "logs").rmdir()


if __name__ == "__main__":
    unittest.main()
