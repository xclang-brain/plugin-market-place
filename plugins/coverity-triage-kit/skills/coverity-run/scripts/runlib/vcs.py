"""git / svn: work copies outside the repository, and fixes saved as commits, branches and patches.

The person's working files are never changed. Fixes are made in work copies in the run folder
(git: ``git worktree``; svn: ``svn checkout``). Patches keep the bytes git or svn wrote, so the files'
character encoding is never touched.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
import xml.etree.ElementTree as ET  # noqa: N817 - the usual name
from pathlib import Path
from typing import Any

from .common import RunError, out_text, remove_file, remove_tree, run_cmd, sha256

QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "GCM_INTERACTIVE": "never"}
# Looking at the person's repository never takes .git/index.lock (their own git add or commit must not fail).
READ_ONLY_ENV = {"GIT_OPTIONAL_LOCKS": "0"}
FETCH_TIMEOUT = 120


class Vcs:
    kind = ""

    def __init__(self, repo: Path, settings: dict[str, Any]):
        self.repo = repo
        self.settings = settings

    def fingerprint(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


class GitVcs(Vcs):
    kind = "git"

    def git(self, *args: str, cwd: Path | None = None, check: bool = True, input_bytes: bytes | None = None,
            env: dict[str, str] | None = None, timeout: float | None = None):
        full_env = {**os.environ, **QUIET_ENV, **(env or {})}
        return run_cmd(["git", "-c", "core.longpaths=true", *args], cwd=cwd or self.repo, env=full_env,
                       input_bytes=input_bytes, check=check, timeout=timeout)

    def has_revision(self, revision: str) -> bool:
        if not revision:
            return False
        return self.git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", check=False).returncode == 0

    def resolve_latest(self) -> tuple[str, str]:
        """The latest revision of the base branch (fetched from origin when there is one), and a note."""
        base = self.settings.get("base_branch") or "main"
        note = ""
        if "origin" in out_text(self.git("remote")).split():
            try:
                fetched = self.git("fetch", "--quiet", "origin", base, check=False, timeout=FETCH_TIMEOUT)
                ok = fetched.returncode == 0
            except RunError:
                ok = False
            if ok and self.has_revision(f"origin/{base}"):
                return out_text(self.git("rev-parse", f"origin/{base}^{{commit}}")), note
            note = f"origin から {base} を取れなかったため、手元の {base} を使います。"
        if self.has_revision(base):
            return out_text(self.git("rev-parse", f"{base}^{{commit}}")), note
        raise RunError(f"取り込み先のブランチ {base} が見つかりません（設定 vcs.base_branch を確かめてください）")

    def usable(self, dest: Path) -> bool:
        """Whether ``dest`` is a work copy git can use (not only a folder with a .git file in it)."""
        if not (dest / ".git").is_file():
            return False
        return self.git("rev-parse", "--git-dir", cwd=dest, check=False).returncode == 0

    def ensure_copy(self, dest: Path, revision: str) -> None:
        if self.usable(dest):
            return
        if os.path.lexists(dest):
            self.remove_copy(dest)
        self.git("worktree", "prune")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--detach", "--force", str(dest), revision)

    def reset_copy(self, dest: Path, revision: str) -> None:
        self.ensure_copy(dest, revision)
        self.git("checkout", "--quiet", "--force", "--detach", revision, cwd=dest)
        self.git("reset", "--quiet", "--hard", revision, cwd=dest)
        self.git("clean", "-q", "-f", "-d", "-x", cwd=dest)

    def changed_files(self, dest: Path) -> list[str]:
        raw = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=dest).stdout
        files, entries, i = [], raw.split(b"\0"), 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            files.append(entry[3:].decode("utf-8", "surrogateescape"))
            if entry[:1] in (b"R", b"C"):
                i += 1  # the original name of a rename or copy follows
        return sorted(set(files))

    def _identity(self) -> dict[str, str]:
        """Commits need a name and an e-mail; neutral ones when the person has none set."""
        env = {}
        for key, var, default in (("user.name", "GIT_AUTHOR_NAME", "triage-bot"),
                                  ("user.email", "GIT_AUTHOR_EMAIL", "triage-bot@localhost")):
            if not out_text(self.git("config", key, check=False)):
                env[var] = env[var.replace("AUTHOR", "COMMITTER")] = default
        return env

    def save(self, dest: Path, revision: str, message: str, branch: str | None, keep_ref: str,
             patch: Path) -> dict[str, Any]:
        files = self.changed_files(dest)
        self.git("add", "-A", cwd=dest)
        self.git("commit", "-q", "--no-verify", "--allow-empty", "-F", "-", cwd=dest,
                 input_bytes=message.encode("utf-8"), env=self._identity())
        commit = out_text(self.git("rev-parse", "HEAD", cwd=dest))
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_bytes(self.git("diff", "--binary", revision, commit, cwd=dest).stdout)
        self.git("update-ref", keep_ref, commit)  # keeps the commit even without a branch
        if branch:
            branch = self.unique_branch(branch)
            self.git("branch", branch, commit)
        return {"files": files, "patch": str(patch), "branch": branch, "commit": commit}

    def unique_branch(self, name: str) -> str:
        candidate, n = name, 1
        while self.has_revision(f"refs/heads/{candidate}"):
            n += 1
            candidate = f"{name}-{n}"
        return candidate

    def delete_branch(self, branch: str) -> None:
        if branch and self.has_revision(f"refs/heads/{branch}"):
            self.git("branch", "-D", branch, check=False)

    def apply_to_copy(self, dest: Path, patch: Path) -> str | None:
        proc = self.git("apply", "--whitespace=nowarn", str(patch), cwd=dest, check=False)
        return None if proc.returncode == 0 else (proc.stderr.decode("utf-8", "replace").strip() or "git apply に失敗しました")

    def remove_copy(self, dest: Path) -> None:
        if (dest / ".git").is_file():
            self.git("worktree", "remove", "--force", str(dest), check=False)
        remove_tree(dest)
        self.git("worktree", "prune", check=False)

    def export(self, revision: str, dest: Path) -> None:
        data = self.git("archive", "--format=tar", revision).stdout
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            try:
                tar.extractall(dest, filter="data")
            except TypeError:  # Python before 3.10.12 has no filter
                tar.extractall(dest)  # noqa: S202 - our own git archive

    def current_revision(self) -> str:
        return out_text(self.git("rev-parse", "HEAD"))

    def has_local_changes(self) -> bool:
        return bool(out_text(self.git("status", "--porcelain", "--untracked-files=no", env=READ_ONLY_ENV)))

    def fingerprint(self) -> str:
        status = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all", env=READ_ONLY_ENV).stdout
        diff = self.git("diff", "HEAD", "--binary", check=False, env=READ_ONLY_ENV).stdout
        return sha256(status + b"\0" + diff)


class SvnVcs(Vcs):
    kind = "svn"

    def svn(self, *args: str, cwd: Path | None = None, check: bool = True):
        return run_cmd(["svn", "--non-interactive", *args], cwd=cwd or self.repo, check=check)

    def url(self) -> str:
        return out_text(self.svn("info", "--show-item", "url", str(self.repo)))

    def has_revision(self, revision: str) -> bool:
        if not revision or not str(revision).isdigit():
            return False
        return self.svn("info", "-r", str(revision), self.url(), check=False).returncode == 0

    def resolve_latest(self) -> tuple[str, str]:
        return out_text(self.svn("info", "-r", "HEAD", "--show-item", "revision", self.url())), ""

    @staticmethod
    def usable(dest: Path) -> bool:
        """Whether ``dest`` is a working copy (its database is there: a half-removed folder keeps only
        .svn/pristine, whose files svn makes read-only)."""
        return (dest / ".svn" / "wc.db").is_file()

    def ensure_copy(self, dest: Path, revision: str) -> None:
        if self.usable(dest):
            return
        remove_tree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # A checkout of a large repository takes many minutes; a copy of another work copy of the run is
        # much faster, and reset_copy then reverts and updates it.
        source = next((p for p in sorted(dest.parent.iterdir()) if p != dest and self.usable(p)), None)
        if source is not None:
            try:
                shutil.copytree(source, dest, symlinks=True)
                return
            except (OSError, shutil.Error):
                remove_tree(dest)
        self.svn("checkout", "--quiet", "-r", str(revision), self.url(), str(dest))

    # svn status --xml: always UTF-8 (the text form uses the console or ANSI code page on Windows, which
    # garbles non-ASCII names). wc-status/@item → the first column of the text form.
    STATUS_CODES = {"modified": "M", "added": "A", "deleted": "D", "unversioned": "?", "missing": "!",
                    "replaced": "R", "ignored": "I", "conflicted": "C", "external": "X", "obstructed": "~",
                    "incomplete": "!"}

    def _status(self, dest: Path, *extra: str) -> list[tuple[str, str]]:
        proc = self.svn("status", "--xml", *extra, cwd=dest)
        try:
            root = ET.fromstring(proc.stdout)
        except ET.ParseError as e:
            raise RunError(f"svn status の結果を読めませんでした: {e}") from e
        out = []
        for entry in root.iter("entry"):
            status = entry.find("wc-status")
            code = self.STATUS_CODES.get(status.get("item", "") if status is not None else "")
            path = entry.get("path", "")
            if code and path and path != ".":
                out.append((code, path))
        return out

    def reset_copy(self, dest: Path, revision: str) -> None:
        self.ensure_copy(dest, revision)
        self.svn("revert", "--quiet", "-R", ".", cwd=dest)
        for code, rel in self._status(dest, "--no-ignore"):
            if code in ("?", "I"):
                target = dest / rel
                if target.is_dir() and not target.is_symlink():
                    remove_tree(target)
                else:
                    remove_file(target)
        self.svn("update", "--quiet", "-r", str(revision), cwd=dest)

    def changed_files(self, dest: Path) -> list[str]:
        return sorted(rel.replace("\\", "/") for code, rel in self._status(dest) if code in "MAD?!R")

    def save(self, dest: Path, revision: str, message: str, branch: str | None, keep_ref: str,
             patch: Path) -> dict[str, Any]:
        files = self.changed_files(dest)
        self.svn("add", "--quiet", "--force", ".", cwd=dest)
        for code, rel in self._status(dest):
            if code == "!":
                self.svn("delete", "--quiet", rel, cwd=dest)
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_bytes(self.svn("diff", cwd=dest).stdout)
        return {"files": files, "patch": str(patch), "branch": None, "commit": None}

    def delete_branch(self, branch: str) -> None:
        return None

    def apply_to_copy(self, dest: Path, patch: Path) -> str | None:
        proc = self.svn("patch", str(patch), ".", cwd=dest, check=False)
        output = out_text(proc)
        if proc.returncode != 0 or re.search(r"^(C|>\s+rejected)", output, re.MULTILINE):
            return output or "svn patch に失敗しました"
        return None

    def remove_copy(self, dest: Path) -> None:
        remove_tree(dest)

    def export(self, revision: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.svn("export", "--quiet", "--force", "-r", str(revision), self.url(), str(dest))

    def current_revision(self) -> str:
        return out_text(self.svn("info", "--show-item", "revision", str(self.repo)))

    def has_local_changes(self) -> bool:
        """Changed files, or a working copy of mixed revisions (svnversion prints 100:101 or 100M)."""
        if out_text(self.svn("status", "-q")):
            return True
        proc = run_cmd(["svnversion", str(self.repo)], check=False)
        version = out_text(proc)
        return proc.returncode != 0 or not version.isdigit()

    def fingerprint(self) -> str:
        status = self.svn("status").stdout
        diff = self.svn("diff", check=False).stdout
        return sha256(status + b"\0" + diff)


def make_vcs(repo: Path, settings: dict[str, Any]) -> GitVcs | SvnVcs:
    vcs = settings["vcs"]
    if vcs.get("type") == "svn":
        return SvnVcs(repo, vcs)
    return GitVcs(repo, vcs)
