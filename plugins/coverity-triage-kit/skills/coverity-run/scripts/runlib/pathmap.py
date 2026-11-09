"""Map the file paths Coverity reports to repository-relative paths.

In order: (1) a relative path that exists as it is, (2) the longest ``coverity.path_strip_prefixes``
removed, (3) the one repository file sharing the longest trailing part of the path, else (4) kept as
it is ("unmapped"). (3) and (4) add a note, so the reports can say so.
"""

from __future__ import annotations

import os
from pathlib import Path

SKIP_DIRS = {".git", ".svn"}


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").strip()


class PathMapper:
    def __init__(self, root: str | Path, strip_prefixes: list[str]):
        self.root = Path(root)
        self.prefixes = sorted((_norm(p).rstrip("/") + "/" for p in strip_prefixes if str(p).strip()),
                               key=len, reverse=True)
        self._files: dict[str, list[str]] | None = None

    def _index(self) -> dict[str, list[str]]:
        """Repository files by lower-case base name."""
        if self._files is None:
            self._files = {}
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for name in filenames:
                    rel = (Path(dirpath) / name).relative_to(self.root).as_posix()
                    self._files.setdefault(name.lower(), []).append(rel)
        return self._files

    def map(self, path: str) -> tuple[str, str]:
        """(repository-relative path, method): method is as_is / prefix / suffix / unmapped."""
        norm = _norm(path)
        absolute = norm.startswith("/") or (len(norm) > 1 and norm[1] == ":")
        if not absolute and norm and (self.root / norm).is_file():
            return norm, "as_is"
        lowered = norm.lower()
        for prefix in self.prefixes:
            if lowered.startswith(prefix.lower()):
                return norm[len(prefix):], "prefix"
        parts = [p for p in norm.split("/") if p and not p.endswith(":")]
        if not parts:
            return norm, "unmapped"
        best: list[str] = []
        best_len = 0
        for candidate in self._index().get(parts[-1].lower(), []):
            cparts = candidate.lower().split("/")
            n = 0
            while n < min(len(cparts), len(parts)) and cparts[-1 - n] == parts[-1 - n].lower():
                n += 1
            if n > best_len:
                best, best_len = [candidate], n
            elif n == best_len:
                best.append(candidate)
        if len(best) == 1:
            return best[0], "suffix"
        return norm, "unmapped"

    def mapped(self, path: str, notes: list[str]) -> str:
        rel, method = self.map(path)
        note = None
        if method == "suffix":
            note = f"パスを自動で対応づけました: {path} → {rel}"
        elif method == "unmapped":
            note = f"パスをリポジトリの中に対応づけられませんでした: {path}（設定 coverity.path_strip_prefixes を確かめてください）"
        if note and note not in notes:
            notes.append(note)
        return rel
