"""git and svn for the work copies: detect, base revision, one copy per slot, reset, diff, apply.

Fixes are made in work copies in the run folder, never in the person's working files. A patch is the
bytes git or svn wrote, so the character encoding and the line endings of the source files survive.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET  # noqa: N817 - the usual name
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
QUIET_GIT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
# Variables that would make git work on another repository than the one of the folder it runs in.
GIT_PLACE = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")
FETCH_TIMEOUT = 120
# The patch must not depend on the person's git settings (prefixes, colours, external or
# text-converting diffs).
GIT_DIFF = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "diff", "--cached", "--binary",
            "--full-index", "--no-color", "--no-ext-diff", "--no-textconv")


class VcsError(Exception):
    """A problem the person can fix. The message is Japanese."""


def text(data: bytes) -> str:
    """Bytes of a tool's output or of a patch, for showing (tools on a Japanese Windows write cp932)."""
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace").strip()


def run(args: list[str], cwd: Path, check: bool = True, timeout: float | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run a command without a shell. The output stays bytes: file contents are never decoded."""
    try:
        proc = subprocess.run(args, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, creationflags=NO_WINDOW)
    except FileNotFoundError:
        if not Path(cwd).is_dir():
            raise VcsError(f"フォルダーがありません: {Path(cwd).as_posix()}") from None
        raise VcsError(f"{args[0]} コマンドが見つかりません。インストールして、PATH を設定してください") from None
    except subprocess.TimeoutExpired:
        raise VcsError(f"{' '.join(args[:3])} が {timeout:.0f} 秒以内に終了しませんでした") from None
    if check and proc.returncode != 0:
        raise VcsError(f"{' '.join(args[:4])} が失敗しました: {text(proc.stderr or proc.stdout)[:500]}")
    return proc


def remove_tree(path: Path) -> None:
    """Delete a folder with everything in it. git objects and svn pristine files are read-only, and
    Windows refuses to delete those; a file another program still holds is tried again for a while."""
    def writable(func, target, _exc):
        os.chmod(target, stat.S_IRWXU if os.path.isdir(target) else stat.S_IREAD | stat.S_IWRITE)
        func(target)

    handler = {"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}
    for _ in range(10):
        if not os.path.lexists(path):
            return
        try:
            shutil.rmtree(path, **handler)
        except OSError:
            time.sleep(0.2)
    if os.path.lexists(path):
        raise VcsError(f"フォルダーを削除できませんでした。ほかのプログラムが使用している可能性があります: {path}")


class Git:
    kind = "git"

    def __init__(self, repo: Path):
        self.repo = repo

    def git(self, *args: str, cwd: Path | None = None, check: bool = True, timeout: float | None = None):
        """Run git in the person's repository, or in the work copy ``cwd``. In a copy git must never
        fall back to a repository above it (the result folder may lie inside the person's repository):
        a copy that lost its .git file is an error, not the person's working tree."""
        env = {k: v for k, v in os.environ.items() if k not in GIT_PLACE}
        if cwd is not None:
            env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
        return run(["git", "-c", "core.longpaths=true", *args], cwd or self.repo, check, timeout,
                   {**env, **QUIET_GIT})

    def _commit(self, ref: str) -> str:
        return text(self.git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).stdout)

    def base(self, branch: str) -> tuple[str, str, str]:
        """(base branch, base revision, note): the branch the fixes start from, fetched from origin."""
        origin = "origin" in text(self.git("remote").stdout).split()
        if not branch and origin:
            head = self.git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
            branch = text(head.stdout).partition("/")[2]
        branch = branch or text(self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout)
        if not branch:
            raise VcsError("取り込み先のブランチを判定できません。設定ファイルの base_branch に指定してください")
        note = ""
        if origin:
            try:
                fetched = self.git("fetch", "--quiet", "origin", branch, check=False, timeout=FETCH_TIMEOUT)
                failed = fetched.returncode != 0
            except VcsError:
                failed = True
            if failed:
                note = f"origin から {branch} を取得できなかったため、取得済みのリビジョンを使用します"
        for ref in (f"origin/{branch}", branch) if origin else (branch,):
            if self._commit(ref):
                return branch, self._commit(ref), note
        raise VcsError(f"ブランチ {branch} が見つかりません。設定ファイルの base_branch を確認してください")

    def is_copy(self, dest: Path) -> bool:
        return (dest / ".git").is_file() and self.git("rev-parse", "--git-dir", cwd=dest, check=False).returncode == 0

    def make_copy(self, dest: Path, revision: str) -> None:
        if self.is_copy(dest):
            return
        self.remove(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--detach", "--force", str(dest), revision)

    def reset(self, dest: Path, revision: str) -> None:
        try:
            self.git("reset", "--quiet", "--hard", revision, cwd=dest)
            self.git("clean", "-q", "-f", "-d", "-x", cwd=dest)
        except VcsError:  # a lock left by a git command that was killed, a lost .git file: a new copy
            self.remove(dest)
            self.make_copy(dest, revision)

    def diff(self, dest: Path, revision: str) -> tuple[list[str], bytes]:
        """(changed files, patch bytes) of everything changed in the copy since ``revision``."""
        self.git("add", "-A", cwd=dest)
        names = self.git("diff", "--cached", "--name-only", "-z", revision, cwd=dest).stdout
        files = sorted(name.decode("utf-8", "replace") for name in names.split(b"\0") if name)
        return files, self.git(*GIT_DIFF, revision, cwd=dest).stdout

    def original(self, dest: Path, revision: str, rel: str) -> bytes | None:
        """The bytes a file has in a clean copy (line ends as git checks them out); None: no such file."""
        proc = self.git("cat-file", "--filters", f"{revision}:{rel}", cwd=dest, check=False)
        return proc.stdout if proc.returncode == 0 else None

    def apply(self, dest: Path, patch: Path) -> str:
        """Apply a patch to the copy. '' when it applied, else the reason (nothing is changed then)."""
        proc = self.git("apply", "--whitespace=nowarn", str(patch), cwd=dest, check=False)
        return "" if proc.returncode == 0 else text(proc.stderr) or "git apply が失敗しました"

    def remove(self, path: Path) -> None:
        """Delete a work copy, or the folder that holds the copies."""
        remove_tree(path)
        self.git("worktree", "prune", check=False)


class Svn:
    kind = "svn"

    def __init__(self, repo: Path):
        self.repo = repo

    def svn(self, *args: str, cwd: Path | None = None, check: bool = True):
        return run(["svn", "--non-interactive", *args], cwd or self.repo, check)

    def base(self, branch: str) -> tuple[str, str, str]:
        """('', the revision of the person's working copy, '')."""
        return "", text(self.svn("info", "--show-item", "revision", ".").stdout), ""

    @staticmethod
    def is_copy(dest: Path) -> bool:
        return (dest / ".svn" / "wc.db").is_file()

    def make_copy(self, dest: Path, revision: str) -> None:
        if self.is_copy(dest):
            return
        remove_tree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # A checkout of a large repository takes many minutes; a copy of another slot's folder does not.
        source = next((p for p in sorted(dest.parent.iterdir()) if self.is_copy(p)), None)
        if source is not None:
            shutil.copytree(source, dest, symlinks=True)
            self.reset(dest, revision)
            return
        url = text(self.svn("info", "--show-item", "url", ".").stdout)
        self.svn("checkout", "--quiet", "-r", revision, url, str(dest))

    def _status(self, dest: Path, *extra: str) -> list[tuple[str, str]]:
        """[(state, path with /)] from ``svn status --xml`` (the text form garbles non-ASCII names on
        Windows). The files of an svn:externals folder are listed too, after the folder ("external")."""
        root = ET.fromstring(self.svn("status", "--xml", *extra, cwd=dest).stdout)
        found = []
        for entry in root.iter("entry"):
            state = entry.find("wc-status")
            if state is not None and entry.get("path") not in (None, "", "."):
                found.append((state.get("item", ""), entry.get("path").replace("\\", "/")))
        return found

    def reset(self, dest: Path, revision: str) -> None:
        revert = ("revert", "--quiet", "-R")  # a path ends with @: a name with an @ is not read as a revision
        if self.svn(*revert, ".", cwd=dest, check=False).returncode != 0:
            self.svn("cleanup", cwd=dest)  # a lock left by an svn command that was killed
            self.svn(*revert, ".", cwd=dest)
        status = self._status(dest, "--no-ignore")
        external = [rel for state, rel in status if state == "external"]
        for rel in external:  # revert does not go into them
            self.svn(*revert, rel + "@", cwd=dest)
        for state, rel in self._status(dest, "--no-ignore") if external else status:
            if state in ("unversioned", "ignored"):
                target = dest / rel
                if target.is_dir() and not target.is_symlink():
                    remove_tree(target)
                else:
                    target.unlink(missing_ok=True)

    def diff(self, dest: Path, revision: str) -> tuple[list[str], bytes]:
        status = self._status(dest)
        external = tuple(rel + "/" for state, rel in status if state == "external")
        foreign = [rel for _state, rel in status if rel.startswith(external)] if external else []
        if foreign:  # svn diff leaves them out: such a change would silently be no part of the fix
            raise VcsError(f"{'、'.join(foreign)} は svn:externals で取り込んだ別の場所のファイルのため、"
                           "修正コードに含められません。このファイルの変更を元に戻してください")
        self.svn("add", "--quiet", "--force", ".", cwd=dest)
        for state, rel in status:
            if state == "missing":
                self.svn("delete", "--quiet", rel + "@", cwd=dest)
        files = sorted(rel for state, rel in self._status(dest)
                       if state in ("modified", "added", "deleted", "replaced") and not (dest / rel).is_dir())
        # --force: a Shift_JIS source that svn took for a binary file is still written as a text diff
        return files, self.svn("diff", "--internal-diff", "--force", cwd=dest).stdout if files else b""

    def original(self, dest: Path, revision: str, rel: str) -> bytes | None:
        """The bytes a file has in a clean copy (line ends as svn checks them out); None: no such file."""
        proc = self.svn("cat", rel + "@BASE", cwd=dest, check=False)
        return proc.stdout if proc.returncode == 0 else None

    def apply(self, dest: Path, patch: Path) -> str:
        for dry in (("--dry-run",), ()):  # svn patch applies what it can: look first, so that nothing is half applied
            proc = self.svn("patch", *dry, str(patch), ".", cwd=dest, check=False)
            output = text(proc.stdout + proc.stderr)
            if proc.returncode != 0 or re.search(r"^(C\s|>\s+rejected)", output, re.MULTILINE):
                return output or "svn patch が失敗しました"
        return ""

    def remove(self, path: Path) -> None:
        remove_tree(path)


def open_repo(repo: Path) -> Git | Svn:
    if (repo / ".git").exists():
        return Git(repo)
    if (repo / ".svn").is_dir():
        return Svn(repo)
    raise VcsError(f"git または svn のリポジトリではありません: {repo}")
