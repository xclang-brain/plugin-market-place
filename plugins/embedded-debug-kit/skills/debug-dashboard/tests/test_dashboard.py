"""dashboard.py の start・status・stop を通して動かすテスト（REQ-402、REQ-407）。

  python3 -m unittest discover -s tests
"""

import json
import os
import stat
import subprocess
import sys
import unittest

import support
from support import DATA, temp_project


def dashboard(*args):
    proc = subprocess.run([sys.executable, str(support.SCRIPTS / "dashboard.py")] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=support.clean_env())
    out = proc.stdout.decode("ascii")  # JSON は ASCII だけで書く
    return proc.returncode, json.loads(out)


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.project = temp_project(self)

    def test_start_status_stop(self):
        workdir = str(self.project)
        self.addCleanup(dashboard, "stop", "--workdir", workdir)
        code, result = dashboard("start", "--workdir", workdir)
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

        code, again = dashboard("start", "--workdir", workdir)
        self.assertEqual((code, again["url"]), (0, result["url"]), "動いているサーバーを使い直していません")

        code, status = dashboard("status", "--workdir", workdir)
        self.assertEqual((code, status["running"], status["url"]), (0, True, result["url"]))

        code, stopped = dashboard("stop", "--workdir", workdir)
        self.assertEqual((code, stopped["ok"]), (0, True), stopped)
        self.assertFalse((base / "recording").exists())
        self.assertFalse(dashboard("status", "--workdir", workdir)[1]["running"])

    def test_status_and_stop_create_nothing(self):
        for command in ("status", "stop"):
            code, result = dashboard(command, "--workdir", str(self.project))
            self.assertEqual((code, result["ok"], result["running"]), (0, True, False))
        self.assertEqual(os.listdir(str(self.project)), [])

    def test_missing_workdir(self):
        code, result = dashboard("start", "--workdir", str(self.project / "nothing"))
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertTrue(result["problems"])

    def test_unknown_command(self):
        code, result = dashboard("restart", "--workdir", str(self.project))
        self.assertEqual((code, result["ok"]), (2, False))

    def test_symlinked_folder_is_refused(self):
        if not support.can_symlink(self.project):
            self.skipTest("この環境ではシンボリックリンクを作れません")
        other = temp_project(self)
        os.symlink(str(other), str(self.project / DATA))
        code, result = dashboard("start", "--workdir", str(self.project))
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertEqual(os.listdir(str(other)), [])


if __name__ == "__main__":
    unittest.main()
