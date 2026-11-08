"""Helpers for the tests of this skill: a git repository with a bare origin, and run folders made the way
a triage run leaves them (format 2). Only the standard library; the script runs through uv."""
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
SCRIPT = SKILL / "scripts" / "apply.py"
UV = shutil.which("uv")
GIT = shutil.which("git")
RUN_ID = "20261108-101500"

needs_uv = unittest.skipIf(UV is None, "uv がないので、スクリプトを動かす試験は飛ばします")
needs_git = unittest.skipIf(GIT is None, "git がないので飛ばします")

READER = """#include <stdio.h>

int read_header(const char *path, char *buf, int size)
{
    FILE *fp = fopen(path, "r");
    if (fgets(buf, size, fp) == NULL) {
        return -1;
    }
    fclose(fp);
    return 0;
}
"""
FIXED = READER.replace('    FILE *fp = fopen(path, "r");\n', '    FILE *fp = fopen(path, "r");\n    if (fp == NULL) {\n'
                       '        return -1;\n    }\n')
CONFIG = """# チームの設定
coverity:
  url: https://cov.example.com:8443   # 社内
vcs:
  type: git
  base_branch: main
output_dir: OUT
"""


def child_encoding(env: dict[str, str]) -> str:
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
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "GIT_DIR", "GIT_WORK_TREE")}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    env.update(extra)
    return env


def run_script(*args: str, env: dict[str, str] | None = None, stdin: str | None = None,
               cwd: Path | None = None) -> tuple[int, dict, str]:
    env = env if env is not None else base_env()
    proc = subprocess.run([UV, "run", "--native-tls", "--quiet", str(SCRIPT), *args], cwd=cwd, env=env,
                          input=stdin.encode("utf-8") if stdin is not None else None,
                          capture_output=True, timeout=300, check=False)
    text = proc.stdout.decode(child_encoding(env))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AssertionError(f"標準出力が JSON 1つではない: {text!r}\nstderr: {proc.stderr!r}") from exc
    return proc.returncode, data, proc.stderr.decode("utf-8", "replace")


def git(cwd: Path, *args: str, input_text: str | None = None) -> str:
    env = base_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.com")
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True,
                          input=input_text.encode("utf-8") if input_text is not None else None)
    return proc.stdout.decode("utf-8", "replace").strip()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def result_of(item: str, recommendation: str, confidence: str = "high", already_fixed: bool = False,
              comment: str | None = None) -> dict:
    return {
        "item": item,
        "verdict": {"judgement": "true_bug" if recommendation == "fix" else "false_positive",
                    "summary": "fopen の失敗を確かめていない" if recommendation == "fix" else "呼び出し元で検証済み",
                    "rationale": "fopen が NULL を返すと fgets に NULL が渡る",
                    "evidence": [{"file": "src/reader.c", "line": 5, "note": ""}]},
        "recommendation": recommendation,
        "confidence": confidence,
        "confidence_reason": "経路をたどって確かめた",
        "deviation": {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                      "comment": comment or "誤検知。呼び出し元で path を検証している（src/main.c:10）。"},
        "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Moderate",
                "summary": "fopen の失敗を確かめる", "impact": "read_header の呼び出し元だけ",
                "exceeded_constraints": [], "already_fixed_on_latest": already_fixed},
        "revision_drift": {"status": "none", "detail": ""},
        "group_excluded_cids": [],
    }


class Fixture:
    """A repository (with origin) and an output folder with run folders."""

    def __init__(self, root: Path, settings: str = CONFIG):
        self.root = root
        self.repo = root / "repo"
        self.origin = root / "origin.git"
        self.out = root / "out"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "reader.c").write_bytes(READER.encode("utf-8"))
        (self.repo / "src" / "other.c").write_bytes(b"int other(void) { return 1; }\n")
        (self.repo / ".coverity-triage").mkdir()
        (self.repo / ".coverity-triage" / "config.yaml").write_bytes(
            settings.replace("OUT", self.out.as_posix()).encode("utf-8"))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "origin", "main")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def remote_branches(self) -> dict[str, str]:
        out = git(self.root, "--git-dir", str(self.origin), "for-each-ref", "--format=%(refname:short) %(objectname)",
                  "refs/heads")
        return dict(line.split(" ", 1) for line in out.splitlines() if line)

    def make_fix(self, run_dir: Path, item: str, cids: list[int], files: dict[str, str], branch: str | None,
                 commit: bool = True) -> dict:
        """The fix of one item as the run leaves it: a commit in a work copy, a local branch, the patch."""
        work = Path(tempfile.mkdtemp(prefix="fixture-wt-")) / "w"
        git(self.repo, "worktree", "add", "-q", "--detach", str(work), self.base)
        try:
            for rel, text in files.items():
                (work / rel).write_bytes(text.encode("utf-8"))
            git(work, "add", "-A")
            git(work, "commit", "-q", "-F", "-", input_text=f"Coverity CID {', '.join(map(str, cids))}: 修正\n\n要約\n")
            sha = git(work, "rev-parse", "HEAD")
            patch = run_dir / "patches" / f"{item}-fix.patch"
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_bytes(subprocess.run(["git", "diff", "--binary", self.base, sha], cwd=work, capture_output=True,
                                             check=True).stdout)
        finally:
            git(self.repo, "worktree", "remove", "--force", str(work))
            shutil.rmtree(work.parent, ignore_errors=True)
        if commit and branch:
            git(self.repo, "branch", branch, sha)
        return {"files": sorted(files), "patch": str(patch), "branch": branch if commit else None,
                "commit": sha if commit else None}

    def make_run(self, items: list[dict], run_id: str = RUN_ID, summary: bool = True, approvals: bool = True,
                 run_format: int = 2) -> Path:
        """``items``: {id, cids, recommendation, decision?, edit?, fix: "commit"|"patch"|None, status?,
        already_fixed?, confidence?, review?}"""
        run_dir = self.out / run_id
        run_dir.mkdir(parents=True)
        state_items, decisions = [], {}
        for spec in items:
            item_id = spec["id"]
            result = result_of(item_id, spec["recommendation"], spec.get("confidence", "high"),
                               spec.get("already_fixed", False))
            write_json(run_dir / "items" / item_id / "result.json", result)
            saved = {}
            if spec.get("fix"):
                first = spec["cids"][0]
                branch = spec.get("branch", f"coverity-fix/cid-{first}" if not item_id.startswith("G")
                                  else f"coverity-fix/{run_id}-{item_id}")
                saved["fix"] = self.make_fix(run_dir, item_id, spec["cids"], spec.get("files", {"src/reader.c": FIXED}),
                                             branch, commit=spec["fix"] == "commit")
            write_json(run_dir / "items" / item_id / "saved.json", saved)
            if spec.get("review"):
                write_json(run_dir / "items" / item_id / "review.json", spec["review"])
            state_items.append({"id": item_id, "cids": spec["cids"], "status": spec.get("status", "done"),
                                "merged_into": None, "review": None, "applied": {}, "seconds": 120.0})
            entry = {"decision": spec.get("decision", spec["recommendation"]), "edited": False,
                     "updated_at": "2026-11-08T11:00:00"}
            if spec.get("edit"):
                entry["deviation"] = spec["edit"]
                entry["edited"] = True
            decisions[item_id] = entry
        state = {"format": run_format, "tool_version": "test", "run_id": run_id, "repo_root": str(self.repo),
                 "created_at": "2026-11-08T10:15:00", "filter_name": "untriaged.yaml", "stream": "app-main",
                 "review": False, "integration": False, "items": state_items, "stopped": False}
        write_json(run_dir / "run.json", state)
        if summary:
            (run_dir / "summary.html").write_text("<!doctype html><title>一覧</title>", encoding="utf-8")
        if approvals:
            write_json(run_dir / "approvals.json", {"version": 1, "updated_at": "2026-11-08T11:00:00",
                                                    "items": decisions})
        return run_dir


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="apply-test-")
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()
