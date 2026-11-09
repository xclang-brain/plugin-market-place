"""The lock between server processes (<file>.lock) and reading a file another process replaces.

Every session starts its own server process, and the record of write-backs passed as entries is one
file shared by them. Windows behaviour (access denied while a file is deleted or replaced) is
simulated with unittest.mock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import MISSING, SRC, only_with_packages

load_tests = only_with_packages

COUNTER = textwrap.dedent("""\
    import json, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from coverity_mcp import tools
    path = Path(sys.argv[2])
    for _ in range(int(sys.argv[3])):
        with tools.file_lock(path, wait=30):
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"n": 0}
            data["n"] += 1
            tools.write_json(path, data)
    """)


if not MISSING:
    from coverity_mcp import tools

    class FileLockTest(unittest.TestCase):

        def setUp(self) -> None:
            self.tmp = Path(tempfile.mkdtemp(prefix="cov-mcp-lock-")).resolve()
            self.addCleanup(self._cleanup)
            self.path = self.tmp / "triage-abc.result.json"
            self.lock = self.tmp / "triage-abc.result.json.lock"

        def _cleanup(self) -> None:
            import shutil

            shutil.rmtree(self.tmp, ignore_errors=True)

        def make_lock(self, text: str, age: float = 0.0) -> None:
            self.lock.write_text(text, encoding="ascii")
            if age:
                when = time.time() - age
                os.utime(self.lock, (when, when))

        def test_holds_the_pid_and_is_removed_after(self) -> None:
            with tools.file_lock(self.path):
                self.assertEqual(self.lock.read_text(encoding="ascii").split()[0], str(os.getpid()))
            self.assertFalse(self.lock.exists())

        def test_removed_after_an_error(self) -> None:
            with self.assertRaises(RuntimeError):
                with tools.file_lock(self.path):
                    raise RuntimeError("x")
            self.assertFalse(self.lock.exists())

        def test_processes_take_turns(self) -> None:
            script = self.tmp / "counter.py"
            script.write_text(COUNTER, encoding="utf-8")
            procs = [subprocess.Popen([sys.executable, str(script), str(SRC), str(self.path), "25"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
            for proc in procs:
                _, err = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
            self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"n": 75})
            self.assertFalse(self.lock.exists())

        def test_waits_for_a_live_holder(self) -> None:
            self.make_lock(f"{os.getpid()} now")
            tools._held_files.add(str(self.lock))  # held by another thread of this process
            self.addCleanup(tools._held_files.discard, str(self.lock))

            def release() -> None:
                time.sleep(0.3)
                tools._held_files.discard(str(self.lock))
                self.lock.unlink()

            thread = threading.Thread(target=release)
            started = time.monotonic()
            thread.start()
            with tools.file_lock(self.path, wait=5):
                waited = time.monotonic() - started
            thread.join()
            self.assertGreaterEqual(waited, 0.25)

        def test_busy_lock_reports_call_again(self) -> None:
            self.make_lock(f"{os.getpid()} now")
            tools._held_files.add(str(self.lock))
            self.addCleanup(tools._held_files.discard, str(self.lock))
            with self.assertRaises(tools.ToolFailure) as caught:
                with tools.file_lock(self.path, wait=0.2):
                    pass
            self.assertIn("もう一度呼んで", str(caught.exception))
            self.assertTrue(self.lock.exists())  # never taken from a live holder

        def test_lock_of_a_dead_process_is_taken(self) -> None:
            self.make_lock("999999 then", age=tools.LOCK_GRACE + 1)
            with mock.patch.object(tools, "pid_alive", return_value=False):
                with tools.file_lock(self.path, wait=1):
                    self.assertEqual(self.lock.read_text(encoding="ascii").split()[0], str(os.getpid()))

        def test_young_lock_is_never_taken_even_without_a_pid(self) -> None:
            self.make_lock("")
            with self.assertRaises(tools.ToolFailure):
                with tools.file_lock(self.path, wait=0.2):
                    pass

        def test_old_lock_is_taken_even_with_a_live_pid(self) -> None:
            self.make_lock(f"{os.getpid()} then", age=tools.LOCK_STALE + 1)
            tools._held_files.add(str(self.lock))
            self.addCleanup(tools._held_files.discard, str(self.lock))
            with tools.file_lock(self.path, wait=1):
                pass

        def test_windows_access_denied_while_the_lock_is_deleted_is_waited(self) -> None:
            real_open = os.open
            calls = {"n": 0}

            def denied_twice(path, flags, mode=0o777):
                if str(path) == str(self.lock) and calls["n"] < 2:
                    calls["n"] += 1
                    self.lock.touch()  # the name is still in the folder (delete pending)
                    raise PermissionError(13, "Access is denied")
                if str(path) == str(self.lock) and self.lock.exists():
                    self.lock.unlink()
                return real_open(path, flags, mode)

            with mock.patch.object(sys, "platform", "win32"), mock.patch("os.open", denied_twice):
                with tools.file_lock(self.path, wait=5):
                    pass
            self.assertEqual(calls["n"], 2)

        def test_windows_access_denied_without_a_lock_is_raised(self) -> None:
            def denied(path, flags, mode=0o777):
                raise PermissionError(13, "Access is denied")

            with mock.patch.object(sys, "platform", "win32"), mock.patch("os.open", denied), \
                    mock.patch.object(tools, "DENIED_GRACE", 0.1):
                with self.assertRaises(PermissionError):
                    with tools.file_lock(self.path, wait=5):
                        pass

        def test_access_denied_is_raised_at_once_off_windows(self) -> None:
            def denied(path, flags, mode=0o777):
                raise PermissionError(13, "Permission denied")

            with mock.patch.object(sys, "platform", "linux"), mock.patch("os.open", denied):
                started = time.monotonic()
                with self.assertRaises(PermissionError):
                    with tools.file_lock(self.path, wait=5):
                        pass
                self.assertLess(time.monotonic() - started, 1.0)

        def test_windows_release_is_retried(self) -> None:
            real_unlink = Path.unlink
            calls = {"n": 0}

            def busy_twice(self_path, missing_ok=False):
                if self_path.name.endswith(".lock") and calls["n"] < 2:
                    calls["n"] += 1
                    raise PermissionError(13, "Access is denied")
                return real_unlink(self_path, missing_ok=missing_ok)

            with mock.patch.object(sys, "platform", "win32"), mock.patch.object(Path, "unlink", busy_twice):
                with tools.file_lock(self.path):
                    pass
            self.assertEqual(calls["n"], 2)
            self.assertFalse(self.lock.exists())

        def test_read_is_retried_on_windows_while_the_file_is_replaced(self) -> None:
            self.path.write_text('{"a": 1}', encoding="utf-8")
            real_read = Path.read_text
            calls = {"n": 0}

            def busy_twice(self_path, *args, **kwargs):
                if calls["n"] < 2:
                    calls["n"] += 1
                    raise PermissionError(13, "Access is denied")
                return real_read(self_path, *args, **kwargs)

            with mock.patch.object(sys, "platform", "win32"), mock.patch.object(Path, "read_text", busy_twice):
                self.assertEqual(tools.read_json(self.path), {"a": 1})
            calls["n"] = 0
            with mock.patch.object(sys, "platform", "linux"), mock.patch.object(Path, "read_text", busy_twice):
                with self.assertRaises(PermissionError):
                    tools.read_json(self.path)


if __name__ == "__main__":
    unittest.main()
