"""Helpers for the tests of this skill (only the standard library; the script runs through uv)."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Never leave __pycache__ in the skill folder: no bytecode from here on, and the folders this test process
# made before that (the first test module is compiled before this line runs) are removed at exit.
sys.dont_write_bytecode = True
_STARTED = time.time() - 120


def _remove_new_bytecode() -> None:
    for folder in (Path(__file__).resolve().parent / "__pycache__", Path(__file__).resolve().parents[1] / "scripts" / "__pycache__"):
        if not folder.is_dir():
            continue
        for pyc in folder.glob("*.pyc"):
            try:
                if pyc.stat().st_mtime >= _STARTED:
                    pyc.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()  # only when empty
        except OSError:
            pass


atexit.register(_remove_new_bytecode)

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "setup.py"
UV = shutil.which("uv")
GIT = shutil.which("git")
SECRET_NAMES = ("COV_USER", "COV_AUTH_KEY", "COVERITY_PASSPHRASE")

needs_uv = unittest.skipIf(UV is None, "uv がないので、スクリプトを動かす試験は飛ばします")
needs_git = unittest.skipIf(GIT is None, "git がないので飛ばします")


def child_encoding(env: dict[str, str]) -> str:
    """The encoding the script writes in: PYTHONIOENCODING, the console code page on Windows, else UTF-8."""
    if env.get("PYTHONIOENCODING"):
        return env["PYTHONIOENCODING"].split(":")[0]
    if sys.platform == "win32":
        import ctypes
        page = ctypes.windll.kernel32.GetConsoleOutputCP()
        if page and page != 65001:
            return f"cp{page}"
        if not page:
            import locale
            return locale.getpreferredencoding(False)
    return "utf-8"


def base_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_NAMES and k != "PYTHONIOENCODING"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.update(extra)
    return env


def run_script(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None,
               stdin: str | None = None, timeout: float = 300) -> tuple[int, dict, str]:
    """Run setup.py through uv; returns (exit code, the JSON object, stderr)."""
    env = env if env is not None else base_env()
    proc = subprocess.run([UV, "run", "--native-tls", "--quiet", str(SCRIPT), *args], cwd=cwd, env=env,
                          input=stdin.encode("utf-8") if stdin is not None else None,
                          capture_output=True, timeout=timeout, check=False)
    text = proc.stdout.decode(child_encoding(env))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AssertionError(f"標準出力が JSON 1つではない: {text!r}\nstderr: {proc.stderr!r}") from exc
    return proc.returncode, data, proc.stderr.decode("utf-8", "replace")


def git(cwd: Path, *args: str) -> str:
    env = base_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.com")
    # no background maintenance (it changes .git while a test compares the folder)
    proc = subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args], cwd=cwd, env=env,
                          capture_output=True, check=True)
    return proc.stdout.decode("utf-8", "replace").strip()


def make_git_repo(root: Path, name: str = "repo", with_origin: bool = True) -> Path:
    """A repository with one commit on main, and (optionally) a bare origin that has it."""
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    if with_origin:
        origin = root / f"{name}-origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(origin))
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        git(repo, "remote", "set-head", "origin", "main")
    return repo


def tree(folder: Path) -> list[str]:
    """Every file and folder under ``folder`` (relative, sorted), with sizes and times of files."""
    out = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_file():
            stat = path.stat()
            out.append(f"{rel} {stat.st_size} {stat.st_mtime_ns}")
        else:
            out.append(rel + "/")
    return out


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="setup-test-")
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()
