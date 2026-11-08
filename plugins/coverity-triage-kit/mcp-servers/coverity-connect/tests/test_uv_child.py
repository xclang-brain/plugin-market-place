"""Without the server's packages in this Python (for example ``python3 -m unittest discover -s tests``
with the system Python), run the whole suite again in a temporary uv environment, from the locked
dependencies, and pass or fail with it. The other modules add no tests in that case.

Nothing is left in the server folder: the environment lives in a temporary folder and no bytecode
is written.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import unittest

from support import CHILD_ENV, MISSING, PART

UV = shutil.which("uv")
TIMEOUT = 600  # seconds; the first run downloads the locked packages


def _run(command: list[str], env: dict[str, str]) -> tuple[int | None, str]:
    posix = os.name == "posix"
    proc = subprocess.Popen(command, env=env, cwd=str(PART), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=posix)
    try:
        out, _ = proc.communicate(timeout=TIMEOUT)
        return proc.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            if posix:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass
        out, _ = proc.communicate()
        return None, out.decode("utf-8", "replace")


@unittest.skipUnless(MISSING, "this Python has the server's packages: the tests run directly")
class UvChildTest(unittest.TestCase):

    def test_suite_in_a_uv_environment(self) -> None:
        if os.environ.get(CHILD_ENV):
            self.fail("the uv environment lacks the server's packages: " + ", ".join(MISSING))
        if not UV:
            self.skipTest("uv is not installed, and this Python lacks the server's packages (" + ", ".join(MISSING) + ")")
        with tempfile.TemporaryDirectory(prefix="cov-mcp-uv-") as work:
            env = dict(os.environ, UV_PROJECT_ENVIRONMENT=os.path.join(work, "venv"), PYTHONDONTWRITEBYTECODE="1")
            env[CHILD_ENV] = "1"
            env.pop("VIRTUAL_ENV", None)
            command = [UV, "run", "--native-tls", "--frozen", "--quiet", "--directory", str(PART),
                       "python", "-B", "-m", "unittest", "discover", "-s", "tests"]
            code, output = _run(command, env)
        if code is None:
            self.fail(f"the tests in the uv environment did not end within {TIMEOUT} s: {output[-2000:]}")
        self.assertEqual(code, 0, output[-4000:])
        self.assertRegex(output, r"Ran [1-9]\d* tests?")


if __name__ == "__main__":
    unittest.main()
