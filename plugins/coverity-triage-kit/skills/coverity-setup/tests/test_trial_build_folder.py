"""trial-build のフォルダの片付け。前のビルドが残したプロセスがファイルを持っていたり（Windows では消せない）、
読み取り専用のファイル（git のオブジェクトなど）があったりしても、試しのビルドは止まらない。"""
from __future__ import annotations

import argparse
import importlib.util
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

from setup_testlib import SKILL, TempDirCase, git, make_git_repo, needs_git, needs_uv, run_script

try:
    import yaml  # noqa: F401  (the script needs it; it is a PEP 723 dependency)
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def load_setup():
    spec = importlib.util.spec_from_file_location("setup_under_test_trial_folder", SKILL / "scripts" / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def busy_rmtree(busy_times: int, calls: list[str]):
    """rmtree that hands one file to the error handler; deleting it fails ``busy_times`` more times."""
    def unlink(name: str) -> None:
        calls.append(name)
        if len(calls) <= busy_times:
            raise PermissionError(32, "別のプロセスが使用中です", name)
        os.unlink(name)

    def rmtree(path, onexc=None, onerror=None):
        held = Path(path) / "trial-build.log"
        held.write_bytes(b"")
        (onexc or onerror)(unlink, str(held), PermissionError(5, "read-only"))
        os.rmdir(path)

    return rmtree


@unittest.skipUnless(HAVE_YAML, "pyyaml がないので、スクリプトを読み込む試験は飛ばします")
class RemoveTree(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.setup = load_setup()
        self.folder = self.tmp / "_trial-build"
        self.folder.mkdir()

    def test_read_only_file_is_made_writable_and_a_busy_one_tried_again(self) -> None:
        calls: list[str] = []
        chmods: list[tuple[str, int]] = []
        real_chmod = os.chmod
        with mock.patch.object(self.setup.shutil, "rmtree", busy_rmtree(2, calls)), \
                mock.patch.object(self.setup.os, "chmod", lambda n, m: (chmods.append((n, m)), real_chmod(n, m))), \
                mock.patch.object(self.setup.time, "sleep"):
            self.setup.remove_tree(self.folder)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(mode == stat.S_IWRITE for _, mode in chmods) and chmods)
        self.assertFalse(self.folder.exists())

    def test_gives_up_after_the_retries(self) -> None:
        calls: list[str] = []
        with mock.patch.object(self.setup.shutil, "rmtree", busy_rmtree(100, calls)), \
                mock.patch.object(self.setup.time, "sleep"):
            with self.assertRaises(PermissionError):
                self.setup.remove_tree(self.folder)
        self.assertEqual(len(calls), 5)

    def test_real_read_only_files_are_removed(self) -> None:
        obj = self.folder / "src" / ".git" / "objects" / "ab"
        obj.mkdir(parents=True)
        (obj / "cdef").write_bytes(b"x")
        os.chmod(obj / "cdef", stat.S_IREAD)
        self.setup.remove_tree(self.folder)
        self.assertFalse(self.folder.exists())


@unittest.skipUnless(HAVE_YAML, "pyyaml がないので、スクリプトを読み込む試験は飛ばします")
class FreshTrialDir(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.setup = load_setup()
        self.out = self.tmp / "out"
        (self.out / "_trial-build" / "src").mkdir(parents=True)

    def test_the_usual_folder_when_it_can_be_removed(self) -> None:
        old_spare = self.out / "_trial-build-20261101-090000"
        (old_spare / "src").mkdir(parents=True)
        keep = self.out / "_trial-build-notes"
        keep.mkdir()
        work, note = self.setup.fresh_trial_dir(self.out)
        self.assertEqual(work, self.out / "_trial-build")
        self.assertFalse(work.exists())
        self.assertEqual(note, "")
        self.assertFalse(old_spare.exists(), "前の予備のフォルダが残った")
        self.assertTrue(keep.exists(), "決まった形でないフォルダを消した")

    def test_a_new_folder_when_the_old_one_is_still_held(self) -> None:
        older = self.out / "_trial-build-20261101-090000"
        older.mkdir()
        real = self.setup.remove_tree

        def held(path: Path, attempts: int = 5, pause: float = 0.5) -> None:
            if path.name == "_trial-build":
                raise PermissionError(32, "別のプロセスが使用中です", str(path / "trial-build.log"))
            real(path, attempts, pause)

        with mock.patch.object(self.setup, "remove_tree", held):
            work, note = self.setup.fresh_trial_dir(self.out)
        self.assertRegex(work.name, r"^_trial-build-[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
        self.assertFalse(work.exists())
        self.assertIn("消せなかった", note)
        self.assertIn(work.name, note)
        self.assertTrue((self.out / "_trial-build").exists())
        self.assertFalse(older.exists())

    def test_a_spare_folder_still_held_is_left_for_next_time(self) -> None:
        older = self.out / "_trial-build-20261101-090000"
        older.mkdir()

        def held(path: Path, attempts: int = 5, pause: float = 0.5) -> None:
            if path.name != "_trial-build":
                raise PermissionError(32, "別のプロセスが使用中です", str(path))
            os.rmdir(path / "src")
            os.rmdir(path)

        with mock.patch.object(self.setup, "remove_tree", held):
            work, note = self.setup.fresh_trial_dir(self.out)
        self.assertEqual(work, self.out / "_trial-build")
        self.assertEqual(note, "")
        self.assertTrue(older.exists())


@needs_uv
@needs_git
@unittest.skipUnless(HAVE_YAML, "pyyaml がないので、スクリプトを読み込む試験は飛ばします")
class TrialBuildInANewFolder(TempDirCase):
    def test_builds_in_a_new_folder_when_the_old_one_cannot_be_removed(self) -> None:
        repo = make_git_repo(self.tmp)
        code, out, _ = run_script("init", "--repo", str(repo), "--url", "https://cov.example.com:8443/",
                                  "--project", "envmon", "--stream", "envmon-main", "--output-dir", str(self.tmp / "out"))
        self.assertEqual(code, 0, out)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "settings")
        (self.tmp / "out" / "_trial-build").mkdir(parents=True)
        (self.tmp / "out" / "_trial-build" / "trial-build.log").write_bytes(b"old")
        setup = load_setup()
        real = setup.remove_tree

        def held(path: Path, attempts: int = 5, pause: float = 0.5) -> None:
            if path.name == "_trial-build":
                raise PermissionError(32, "別のプロセスが使用中です", str(path / "trial-build.log"))
            real(path, attempts, pause)

        args = argparse.Namespace(repo=str(repo), build_command=f'"{sys.executable}" -c "print(1)"',
                                  setup_command=None, build_dir=None, timeout_minutes=None)
        with mock.patch.object(setup, "remove_tree", held), mock.patch.object(setup, "progress", lambda *a, **k: None):
            result = setup.cmd_trial_build(args)
        self.assertTrue(result["build_ok"], result)
        self.assertRegex(Path(result["built_in"]).parent.name, r"^_trial-build-[0-9]{8}-[0-9]{6}")
        self.assertEqual(Path(result["log"]).parent, Path(result["built_in"]).parent)
        self.assertIn("消せなかった", result["note"])


if __name__ == "__main__":
    unittest.main()
