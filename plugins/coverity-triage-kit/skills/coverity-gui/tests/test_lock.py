"""file_lock: a lock left by a process that is gone is taken over; Windows access denied is not waited on for ever."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from guilib import common


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


class StaleLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = helpers.temp_dir(self)
        self.target = self.tmp / "approvals.json"
        self.lock = self.tmp / "approvals.json.lock"

    def take(self, timeout: float = 30.0, stale: float = 60.0) -> float:
        start = time.monotonic()
        with common.file_lock(self.target, timeout=timeout, stale=stale):
            self.assertEqual(self.lock.read_text(), str(os.getpid()))
        self.assertFalse(self.lock.exists())
        return time.monotonic() - start

    def test_a_dead_holder_is_taken_over_at_once(self) -> None:
        self.lock.write_text(str(dead_pid()))
        age(self.lock, common.LOCK_GRACE + 5)
        self.assertLess(self.take(), 2.0)

    def test_an_empty_lock_past_the_grace_is_taken_over(self) -> None:
        self.lock.write_bytes(b"")
        age(self.lock, common.LOCK_GRACE + 5)
        self.assertLess(self.take(), 2.0)

    def test_a_young_lock_is_waited_on_even_with_a_dead_pid(self) -> None:
        self.lock.write_text(str(dead_pid()))
        with self.assertRaises(common.GuiError):
            self.take(timeout=0.3)
        self.assertTrue(self.lock.exists())

    def test_a_living_holder_is_waited_on(self) -> None:
        self.lock.write_text(str(os.getpid()))  # this process: alive, and the lock is not re-entrant
        age(self.lock, common.LOCK_GRACE + 5)
        with self.assertRaises(common.GuiError):
            self.take(timeout=0.3)
        self.assertEqual(self.lock.read_text(), str(os.getpid()))

    def test_text_that_is_not_a_pid_waits_for_the_stale_age(self) -> None:
        self.lock.write_text("??")
        age(self.lock, common.LOCK_GRACE + 5)
        with self.assertRaises(common.GuiError):
            self.take(timeout=0.3)
        age(self.lock, 120)
        self.assertLess(self.take(timeout=30), 2.0)

    def test_a_lock_taken_again_meanwhile_is_not_removed(self) -> None:
        self.lock.write_text(str(dead_pid()))
        age(self.lock, common.LOCK_GRACE + 5)

        def other_waiter_takes_it(pid: object) -> bool:  # between the check and the removal
            self.lock.unlink()
            self.lock.write_text(str(os.getpid()))
            return False

        with mock.patch.object(common, "pid_alive", side_effect=other_waiter_takes_it):
            self.assertFalse(common._remove_stale_lock(self.lock, 30.0))
        self.assertEqual(self.lock.read_text(), str(os.getpid()))


class WindowsAccessDeniedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = helpers.temp_dir(self)
        self.target = self.tmp / "approvals.json"
        self.lock = self.tmp / "approvals.json.lock"

    @staticmethod
    def denied(path, flags, *args):
        raise PermissionError(13, "Access is denied", str(path))

    def test_denied_with_no_lock_there_is_raised_after_a_short_grace(self) -> None:
        self.assertLessEqual(common.DENIED_GRACE, 3.0)
        start = time.monotonic()
        with mock.patch.object(common.os, "open", self.denied), mock.patch.object(common.sys, "platform", "win32"), \
                mock.patch.object(common, "DENIED_GRACE", 0.2):
            with self.assertRaises(PermissionError):
                with common.file_lock(self.target, timeout=30):
                    self.fail("not taken")
        self.assertLess(time.monotonic() - start, 2.0)

    def test_denied_while_the_lock_name_exists_waits_like_a_busy_lock(self) -> None:
        self.lock.write_text(str(os.getpid()))  # delete pending: the name is still in the folder
        with mock.patch.object(common.os, "open", self.denied), mock.patch.object(common.sys, "platform", "win32"), \
                mock.patch.object(common, "DENIED_GRACE", 0.1):
            with self.assertRaises(common.GuiError):
                with common.file_lock(self.target, timeout=0.5):
                    self.fail("not taken")

    def test_denied_elsewhere_is_raised_at_once(self) -> None:
        start = time.monotonic()
        with mock.patch.object(common.os, "open", self.denied), mock.patch.object(common.sys, "platform", "linux"):
            with self.assertRaises(PermissionError):
                with common.file_lock(self.target, timeout=30):
                    self.fail("not taken")
        self.assertLess(time.monotonic() - start, 1.0)


class ReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = helpers.temp_dir(self)
        self.target = self.tmp / "approvals.json"
        self.lock = self.tmp / "approvals.json.lock"

    def flaky_unlink(self, failures: int):
        real = os.unlink
        calls = []

        def unlink(path, *args, **kwargs):
            calls.append(path)
            if len(calls) <= failures:
                raise PermissionError(13, "The process cannot access the file", str(path))
            return real(path, *args, **kwargs)

        return unlink, calls

    def test_release_on_windows_tries_again_while_a_waiter_reads_the_pid(self) -> None:
        unlink, calls = self.flaky_unlink(3)
        with mock.patch.object(common.os, "unlink", unlink), mock.patch.object(common.sys, "platform", "win32"):
            with common.file_lock(self.target, timeout=5):
                self.assertTrue(self.lock.exists())
        self.assertFalse(self.lock.exists())
        self.assertEqual(len(calls), 4)

    def test_release_elsewhere_does_not_loop(self) -> None:
        unlink, calls = self.flaky_unlink(1)
        with mock.patch.object(common.os, "unlink", unlink), mock.patch.object(common.sys, "platform", "linux"):
            common._release_lock(self.lock)
        self.assertEqual(len(calls), 1)


class PidAliveTest(unittest.TestCase):
    def test_this_process_and_a_finished_one(self) -> None:
        self.assertTrue(common.pid_alive(os.getpid()))
        self.assertFalse(common.pid_alive(dead_pid()))
        for value in (0, -1, None, True, "123"):
            self.assertFalse(common.pid_alive(value), value)

    def fake_kernel32(self, handle: int, exit_code: int = 259, ok: int = 1):
        class Func:
            def __init__(self, fn):
                self.fn = fn

            def __call__(self, *args):
                return self.fn(*args)

        def get_exit_code(_handle, ref):
            ref._obj.value = exit_code
            return ok

        kernel = mock.Mock()
        kernel.OpenProcess = Func(lambda access, inherit, pid: handle)
        kernel.GetExitCodeProcess = Func(get_exit_code)
        kernel.CloseHandle = Func(lambda h: 1)
        return kernel

    def windows(self, kernel, last_error: int = 0) -> bool:
        with mock.patch.object(common.sys, "platform", "win32"), \
                mock.patch.object(ctypes, "WinDLL", create=True, return_value=kernel), \
                mock.patch.object(ctypes, "get_last_error", create=True, return_value=last_error):
            return common.pid_alive(4321)

    def test_windows_open_process(self) -> None:
        self.assertTrue(self.windows(self.fake_kernel32(handle=8, exit_code=259)))  # STILL_ACTIVE
        self.assertFalse(self.windows(self.fake_kernel32(handle=8, exit_code=0)))
        self.assertFalse(self.windows(self.fake_kernel32(handle=0), last_error=87))  # no such process
        self.assertTrue(self.windows(self.fake_kernel32(handle=0), last_error=5))  # someone else's process

    def test_windows_without_kernel32_says_alive(self) -> None:
        with mock.patch.object(common.sys, "platform", "win32"), \
                mock.patch.object(ctypes, "WinDLL", create=True, side_effect=OSError("no kernel32")):
            self.assertTrue(common.pid_alive(4321))


if __name__ == "__main__":
    unittest.main()
