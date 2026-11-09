"""A run on an svn working copy (skipped when svn is not installed)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import READER_C, RunCase, base_env, issue, runner


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn がないため飛ばします")
class SvnRunTest(RunCase):
    def setUp(self) -> None:  # an svn working copy instead of the git repository
        self.cmd = runner()
        self.tmp = Path(tempfile.mkdtemp(prefix="runskill-svn-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = base_env(self.tmp)
        server = self.tmp / "server"
        subprocess.run(["svnadmin", "create", str(server)], check=True, env=self.env)
        url = server.as_uri()
        self.repo = self.tmp / "repo"
        subprocess.run(["svn", "checkout", "--quiet", url, str(self.repo)], check=True, env=self.env)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "reader.c").write_text(READER_C, encoding="utf-8")
        self.write_config()
        config = self.repo / ".coverity-triage" / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8").replace("type: git\n  base_branch: main", "type: svn"),
                          encoding="utf-8")
        subprocess.run(["svn", "add", "--quiet", "src", ".coverity-triage"], cwd=self.repo, check=True, env=self.env)
        subprocess.run(["svn", "commit", "--quiet", "-m", "init", "--username", "t"], cwd=self.repo, check=True, env=self.env)
        subprocess.run(["svn", "update", "--quiet"], cwd=self.repo, check=True, env=self.env)

    def test_svn_run(self) -> None:
        run_dir = self.new_run()["run_dir"]
        out = self.drive(run_dir, [issue(20001, line=6)])
        self.assertTrue(out["done"], out)
        saved = json.loads((Path(run_dir) / "items" / "20001" / "saved.json").read_text(encoding="utf-8"))
        self.assertIsNone(saved["fix"]["branch"])
        self.assertIn("/* fix 20001 */", (Path(run_dir) / "patches" / "20001-fix.patch").read_text(encoding="utf-8"))
        summary = self.run_json("summary", "--run", run_dir)
        checks = {c["id"]: c for c in summary["run_checks"]}
        self.assertTrue(checks["C-24"]["ok"], checks["C-24"])
        self.assertTrue(checks["C-27"]["ok"], checks["C-27"])
        status = subprocess.run(["svn", "status"], cwd=self.repo, capture_output=True, env=self.env).stdout.decode()
        self.assertEqual(status.strip(), "")

    def test_half_removed_copy_is_made_again(self) -> None:
        """svn keeps .svn/pristine read-only; Windows cannot delete read-only files, so a plain rmtree leaves
        a folder that looks like a working copy but is not one."""
        import os
        import stat
        from unittest import mock

        from support import import_runlib, windows_unlink

        import_runlib()
        from runlib.vcs import SvnVcs

        vcs = SvnVcs(self.repo, {"type": "svn"})
        revision = vcs.current_revision()
        dest = self.tmp / "work" / "w1"
        vcs.reset_copy(dest, revision)
        pristine = [p for p in (dest / ".svn" / "pristine").rglob("*") if p.is_file()]
        self.assertTrue(pristine)
        for path in pristine:  # as svn makes them on Windows
            path.chmod(stat.S_IREAD)
        with mock.patch.object(os, "unlink", windows_unlink()):
            vcs.remove_copy(dest)
        self.assertFalse(dest.exists())
        vcs.reset_copy(dest, revision)
        (dest / ".svn" / "wc.db").unlink()  # only part of a working copy left
        self.assertFalse(vcs.usable(dest))
        vcs.reset_copy(dest, revision)
        self.assertTrue(vcs.usable(dest))
        self.assertIn("read_header", (dest / "src" / "reader.c").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
