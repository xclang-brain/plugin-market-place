"""Helpers of the unit tests: throwaway git and svn repositories, the settings file, and driving triage.py.

Everything is made below one temporary folder. A test for svn skips only when the svn command is missing.
Run the tests with ``python -B`` (tests/run.py does): nothing is left in the skill folder then.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import job  # noqa: E402
import vcs  # noqa: E402

GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "core.autocrlf=false"]
SJIS = "// 日本語のコメント\r\nint s(void)\r\n{\r\n    return 0;\r\n}\r\n".encode("cp932")
FILES = {"src/a.c": b"int a(int *p)\n{\n    return *p;\n}\n", "src/b.c": b"int b(void)\n{\n    return 0;\n}\n",
         "src/sjis.c": SJIS}
FIX_COMMENT = "本物のバグ。src/a.c:3 で p を NULL のまま参照するが、呼び出し元の main.c:10 は診断の出力だけで使用している。"
KEEP_COMMENT = "誤検知。src/b.c:2 で長さを確認してから src/b.c:3 で返しており、範囲外は参照しない。"


def command() -> list:
    """triage.py needs PyYAML: this Python when it has it, else uv as in real use."""
    try:
        import yaml  # noqa: F401
        return [sys.executable, str(SCRIPTS / "triage.py")]
    except ImportError:
        if not shutil.which("uv"):
            raise unittest.SkipTest("PyYAML のある Python も uv もないため、triage.py を実行できません") from None
        return [shutil.which("uv"), "run", "--native-tls", "--quiet", str(SCRIPTS / "triage.py")]


def triage(*args, env=None) -> dict:
    """Run one command of triage.py: it must exit 0 and print exactly one JSON object in UTF-8."""
    proc = subprocess.run([*command(), *map(str, args)], capture_output=True, env=env, timeout=300)
    if proc.returncode != 0:
        raise AssertionError(f"triage.py {args[0]}: exit {proc.returncode}\n{proc.stdout.decode()}\n"
                             f"{proc.stderr.decode()}")
    return json.loads(proc.stdout.decode("utf-8"))


def call(*args, cwd) -> bytes:
    return subprocess.run([str(a) for a in args], cwd=str(cwd), check=True, capture_output=True).stdout


def write(root: Path, files: dict) -> None:
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)


def make_repo(kind: str, top: Path, files: dict = FILES) -> Path:
    """A repository with one commit at <top>/repo (svn: the repository itself is <top>/svnrepo)."""
    repo = top / "repo"
    if kind == "git":
        write(repo, files)
        call(*GIT, "init", "-q", "-b", "main", cwd=repo)
        call(*GIT, "config", "core.autocrlf", "false", cwd=repo)  # the bytes of the files are what the tests wrote
        call(*GIT, "add", "-A", cwd=repo)
        call(*GIT, "commit", "-q", "-m", "init", cwd=repo)
    else:
        call("svnadmin", "create", top / "svnrepo", cwd=top)
        call("svn", "checkout", "-q", (top / "svnrepo").as_uri(), repo, cwd=top)
        write(repo, files)
        call("svn", "add", "-q", "--force", ".", cwd=repo)
        call("svn", "commit", "-q", "-m", "init", cwd=repo)
        call("svn", "update", "-q", cwd=repo)
    return repo


def fresh_copy(kind: str, top: Path, repo: Path, name: str = "fresh") -> Path:
    """A clean copy of the committed code, to apply a patch on."""
    if kind == "git":
        call(*GIT, "clone", "-q", repo, top / name, cwd=top)
    else:
        call("svn", "checkout", "-q", (top / "svnrepo").as_uri(), top / name, cwd=top)
    return top / name


def apply_patch(kind: str, copy: Path, patch: Path) -> None:
    call(*(GIT + ["apply"] if kind == "git" else ["svn", "patch"]), patch, cwd=copy)


def user_tree(kind: str, repo: Path) -> bytes:
    """What the person would see as changed in their repository."""
    return call(*(GIT + ["status", "--porcelain"] if kind == "git" else ["svn", "status"]), cwd=repo)


def issue(cid: int, file: str = "src/a.c", line: int = 3, checker: str = "NULL_RETURNS", function: str = "") -> dict:
    return {"cid": cid, "checker": checker, "file": file, "line": line, "function": function, "impact": "High"}


def result(cid: int, with_fix: bool = True, **over) -> dict:
    """A result.json as a worker writes it, that passes the checks: with a fix, or with the reason why
    there is none."""
    data = {"cid": cid, "verdict": "true_bug", "summary": "NULL のまま参照する経路があります",
            "evidence": [{"file": "src/a.c", "line": 3, "note": "p を確認せずに参照しています"}],
            "recommendation": "fix", "confidence": "high", "fix": {"summary": "NULL のときは 0 を返します"},
            "deviation": {"comment": FIX_COMMENT}}
    if not with_fix:
        del data["fix"]
        data.update(verdict="false_positive", recommendation="deviation", no_fix_reason="コードに問題がないため",
                    deviation={"comment": KEEP_COMMENT})
    return {**data, **over}


def stub(folder: Path, name: str, source: str) -> None:
    """A tiny command <name> written in Python (Windows starts it through a .cmd wrapper)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.py").write_text(source, encoding="utf-8")
    if os.name == "nt":  # cmd.exe reads a .cmd file in the console code page, also the path of Python in it
        (folder / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n', encoding="oem")
    else:
        (folder / name).write_text(f"#!/bin/sh\nexec '{sys.executable}' '{folder / name}.py' \"$@\"\n",
                                   encoding="utf-8")
        (folder / name).chmod(0o755)


class RunCase(unittest.TestCase):
    """A repository with a settings file. ``kind`` is set by the subclasses (git, svn)."""

    kind = "git"
    settings = ""  # more lines for config.yaml
    env = None

    def setUp(self):
        if self.kind == "svn" and not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn コマンドがありません")
        self.top = Path(tempfile.mkdtemp(prefix="triage-test-")).resolve()
        self.addCleanup(vcs.remove_tree, self.top)
        self.addCleanup(self.settle)
        self.repo = make_repo(self.kind, self.top)
        self.configure(self.settings)

    def configure(self, more: str = "", stream: str = "app-main") -> None:
        text = f"coverity:\n  url: https://coverity.example.co.jp:8443\n  stream: {stream}\n" + more
        write(self.repo, {".coverity-triage/config.yaml": text.encode("utf-8")})

    def settle(self) -> None:
        """Wait for the jobs that make work copies: start does not wait for them while the list is missing,
        and a folder that a job still writes into is not to be deleted."""
        for record in self.top.glob("coverity-triage-out/*/work/prepare.json"):
            while (job.read(record.parents[1], "prepare") or {}).get("status") == "running":
                time.sleep(0.05)

    def run_triage(self, *args) -> dict:
        return triage(*args, env=self.env)

    def follow(self, line: str) -> dict:
        """Run a command line that a reply gave under next."""
        return self.run_triage(*shlex.split(line.split('triage.py" ', 1)[1]))

    def start(self, *flags) -> dict:
        """start, asked again while it answers that a background job runs."""
        while True:
            out = self.run_triage("start", "--repo", self.repo, *flags)
            if not (out["ok"] and "log" in out):
                return out

    def begin(self, issues: list, *flags, **fetch) -> Path:
        """start, put the fetched list where start said, start again. Returns the run folder."""
        out = self.start(*flags)
        self.assertEqual([call["tool"] for call in out["fetch"]], ["coverity_search_issues"], out)
        Path(out["fetch"][0]["arguments"]["output_file"]).write_text(
            json.dumps({"stream": "app-main", "issues": issues, **fetch}), encoding="utf-8")
        self.assertIn("counts", self.start(*flags))
        return Path(out["run_dir"])

    def work(self, given: dict, edits: dict | None = None, with_fix: bool | None = None, **over) -> dict:
        """What a worker does with the reply of next: edit the work copy, write result.json, save.
        ``edits``: {file: (old, new) or the new bytes}; {} changes nothing and writes a result without a fix."""
        if edits is None:
            edits = {"src/a.c": (b"return *p;", b"return p ? *p : 0;")}
        for name, change in edits.items():
            path = Path(given["work_dir"]) / name
            path.write_bytes(path.read_bytes().replace(*change) if isinstance(change, tuple) else change)
        data = result(given["cid"], edits != {} if with_fix is None else with_fix, **over)
        Path(given["result_file"]).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return self.follow(given["next"][0])

    def read(self, run: Path, cid: int, name: str = "state.json") -> dict:
        return json.loads((run / "cid" / str(cid) / name).read_text(encoding="utf-8"))

    state = read
