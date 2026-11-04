"""Option: verification of the fixes by a build (and a Coverity re-analysis).

It runs once per run, after every item has been worked on: all fix patches are applied together
to a copy of the latest code, which is built (and analyzed) once. For the re-analysis, the
unmodified latest code is analyzed once as the baseline, so a run costs two analyses however many
items it has. Results are then given to the items: a CID is resolved when its warning is gone;
build errors and new warnings go to the items that changed the files involved.

Only the commands written in the project settings are run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import CtError, build_env, display_text
from .config import VerifyConfig
from .models import Issue

LOG_TAIL = 3000


def safe_relpath(path: str) -> str:
    """A path inside the repository, written relative to it."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path) or ".." in parts:
        raise CtError(f"verify.build_dir はリポジトリからの相対パスで書いてください: {path}")
    return "/".join(parts)


@dataclass
class BatchItem:
    """One work item's fix, as input to a batch verification."""

    item_id: str
    patch_path: str
    files: list[str]
    issues: list[Issue]
    strip: int = 1  # path components to strip: 1 for git patches, 0 for svn patches


@dataclass
class BatchResult:
    mode: str
    build_ok: bool
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    unassigned_new_issues: list[dict[str, Any]] = field(default_factory=list)
    log: str = ""
    seconds: float = 0.0
    error: str = ""


# The builds this process runs, while ``track_builds`` is on (the screen's trial build): each runs in a
# process group of its own, so that ``stop_builds`` ends the shell and everything it started.
_tracked: set[subprocess.Popen[bytes]] | None = None
_stopped = False
_track_lock = threading.Lock()


def track_builds() -> None:
    """Keep the builds of this process, for ``stop_builds`` (``ct.py gui`` calls it once at start)."""
    global _tracked, _stopped
    with _track_lock:
        _tracked, _stopped = set(), False


def stop_builds() -> None:
    """End the builds running now, with their children; no build starts after this."""
    global _stopped
    with _track_lock:
        _stopped = True
        running = list(_tracked or ())
    for proc in running:
        if proc.poll() is None:
            _stop_tree(proc)


def _stop_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _run_shell(command: str, cwd: Path, log: Path, drop: tuple[str, ...] = ()) -> bool:
    with open(log, "ab") as f:
        f.write(f"$ {command}\n".encode("utf-8"))
        f.flush()
        kwargs: dict[str, Any] = {"cwd": cwd, "shell": True, "stdout": f, "stderr": subprocess.STDOUT,
                                  "env": build_env(drop=drop)}
        with _track_lock:
            tracked = _tracked
            if tracked is None:
                proc = None
            elif _stopped:
                return False
            else:
                group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                         else {"start_new_session": True})
                proc = subprocess.Popen(command, **kwargs, **group)
                tracked.add(proc)
        if proc is None:  # ct.py itself: the build stays in the group of the command (Ctrl+C stops it)
            return subprocess.run(command, **kwargs).returncode == 0
        try:
            proc.wait()
        finally:
            with _track_lock:
                tracked.discard(proc)
    return proc.returncode == 0


def _analyzed(json_path: Path, log: Path) -> bool:
    """Whether the analysis output has no error; an error is written to the log (a failed analysis)."""
    error = analysis_error(json_path)
    if error:
        with open(log, "ab") as f:
            f.write(f"cov-format-errors の結果に error があります: {error}\n".encode("utf-8"))
    return not error


def _tail(log: Path) -> str:
    """The end of the log (the build tools of a Japanese Windows write cp932)."""
    return display_text(log.read_bytes()[-LOG_TAIL:]) if log.is_file() else ""


def intermediate_dir(cov_analyze_args: str) -> str:
    """The ``--dir`` given to cov-analyze, which cov-format-errors reads as well."""
    match = re.search(r'(?:^|\s)--dir(?:=|\s+)(?:"([^"]*)"|(\S+))', cov_analyze_args)
    if match:
        return match.group(1) if match.group(1) is not None else match.group(2)
    raise CtError("verify.cov_analyze_args に --dir（中間ディレクトリ）を指定してください")


def no_ticker(args: str) -> str:
    """``--ticker-mode none`` for cov-analyze and cov-commit-defects (unless the settings choose a mode):
    the progress bars they print by default would fill the end of the log that is shown on a failure."""
    return args if re.search(r"(?:^|\s)--ticker-mode\b", args) else f"--ticker-mode none {args}".rstrip()


def analysis_error(json_path: Path) -> str | None:
    """The ``error`` of the outer object of ``cov-format-errors --json-output-v7`` output (null when it
    worked). With an error the issue list is empty, which must not count as every warning being gone."""
    data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    error = data.get("error") if isinstance(data, dict) else None
    if not error:
        return None
    return error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)


def load_analysis(json_path: Path) -> list[dict[str, Any]]:
    """Read ``cov-format-errors --json-output-v7`` output into simple records."""
    data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    records = []
    for item in data.get("issues", []):
        records.append({
            "merge_key": item.get("mergeKey"),
            "checker": item.get("checkerName", ""),
            "file": (item.get("mainEventFilePathname") or item.get("strippedMainEventFilePathname") or "").replace("\\", "/"),
            "function": item.get("functionDisplayName") or item.get("functionName"),
            "line": item.get("mainEventLineNumber"),
        })
    return records


def matches(issue: Issue, record: dict[str, Any]) -> bool:
    if issue.merge_key and record.get("merge_key"):
        return issue.merge_key == record["merge_key"]
    file = issue.file.replace("\\", "/").lstrip("/")
    return (record["checker"] == issue.checker
            and record["file"].endswith(file)
            and (issue.function is None or record.get("function") == issue.function))


def _key(record: dict[str, Any]) -> tuple:
    return record.get("merge_key") or (record["checker"], record["file"], record.get("function"))


def _touches(record_file: str, files: list[str]) -> bool:
    return any(record_file.lower().endswith(f.replace("\\", "/").lower()) for f in files)


def build_command_line(config: VerifyConfig, root: Path, mode: str, json_out: Path | None = None) -> str:
    """One shell line: environment setup, move to the build folder, then build (and analysis)."""
    if not config.build_command:
        raise CtError("verify.build_command が設定されていません（/coverity-setup の「ビルドでの検証」で設定します）")
    steps = []
    if config.setup_command:
        setup = config.setup_command.replace("{root}", str(root))
        steps.append(("call " if sys.platform == "win32" else "") + setup)
    if config.build_dir:
        # An absolute path, so it works even if setup_command changed the current folder;
        # /d also changes the drive on Windows.
        target = root / safe_relpath(config.build_dir)
        steps.append(f'cd /d "{target}"' if sys.platform == "win32" else f'cd "{target}"')
    if mode == "build":
        steps.append(config.build_command)
    elif mode == "build+analyze":
        idir = intermediate_dir(config.cov_analyze_args)
        steps += [f"cov-build {config.cov_build_args} {config.build_command}",
                  f"cov-analyze {no_ticker(config.cov_analyze_args)}",
                  f'cov-format-errors --dir "{idir}" --json-output-v7 "{json_out}"']
    else:
        raise CtError("検証方法は build か build+analyze です")
    return " && ".join(steps)


def apply_patch(root: Path, patch: str, strip: int) -> str | None:
    """Apply one patch to a plain directory. Returns an error message, or None on success."""
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(root.parent))  # never treat it as a repository
    proc = subprocess.run(["git", "apply", f"-p{strip}", "--whitespace=nowarn", patch],
                          cwd=root, env=env, capture_output=True, check=False)
    if proc.returncode != 0:
        return proc.stderr.decode("utf-8", "replace").strip() or "差分を適用できませんでした"
    return None


class Verifier:
    def __init__(self, config: VerifyConfig, run_dir: str | Path, secret_envs: tuple[str, ...] = ()):
        self.config = config
        # environment variables the build never gets (its tools may print their environment into the log)
        self.drop = (*secret_envs, "COVERITY_PASSPHRASE")
        self.dir = Path(run_dir) / "work" / "verify"
        self.log_dir = Path(run_dir) / "verify"

    def _own_dirs(self, name: str) -> VerifyConfig:
        """Settings whose absolute --dir is made one per copy (before and after must not share it)."""
        config = self.config
        idir = intermediate_dir(config.cov_analyze_args)
        if not Path(idir).is_absolute():
            return config  # relative: inside each copy already
        own = f"{idir.rstrip('/').rstrip(chr(92))}-{name}"
        swap = (lambda args: args.replace(f'"{idir}"', f'"{own}"').replace(idir, own))
        return config.model_copy(update={"cov_build_args": swap(config.cov_build_args),
                                         "cov_analyze_args": swap(config.cov_analyze_args)})

    def _copy(self, source: Path, name: str) -> Path:
        dest = self.dir / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        return dest

    def run_batch(self, mode: str, latest_root: Path, items: list[BatchItem]) -> BatchResult:
        if mode not in ("build", "build+analyze"):
            raise CtError("検証方法は build か build+analyze です")
        started = time.time()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_dir / "after.log"
        log.unlink(missing_ok=True)
        result = BatchResult(mode=mode, build_ok=False, log=str(log))

        baseline: list[dict[str, Any]] = []
        if mode == "build+analyze":
            baseline = self._analyze_baseline(latest_root, items, result)
            if result.error:
                result.seconds = time.time() - started
                return result

        after = self._copy(latest_root, "after")
        applied = self._apply_all(after, items, result)
        after_json = self.dir / "after.json"
        result.build_ok = _run_shell(build_command_line(self._own_dirs("after"), after, mode, after_json), after, log,
                                     self.drop)
        if result.build_ok and mode == "build+analyze":
            result.build_ok = _analyzed(after_json, log)
        result.seconds = time.time() - started

        if not result.build_ok:
            _assign_build_failure(result, applied, log)
        elif mode == "build":
            for item in applied:
                result.items[item.item_id] = {"mode": mode, "batch": True, "applied": True,
                                              "build_ok": True, "problems": [], "log": str(log)}
        else:
            _assign_analysis(result, applied, baseline, load_analysis(after_json), log)
        return result

    def _analyze_baseline(self, latest_root: Path, items: list[BatchItem],
                          result: BatchResult) -> list[dict[str, Any]]:
        """Build and analyze the unmodified latest code once; on failure every item gets the error."""
        before = self._copy(latest_root, "before")
        before_log = self.log_dir / "before.log"
        before_log.unlink(missing_ok=True)
        before_json = self.dir / "before.json"
        if _run_shell(build_command_line(self._own_dirs("before"), before, result.mode, before_json), before, before_log,
                      self.drop) and _analyzed(before_json, before_log):
            return load_analysis(before_json)
        # Not a problem of any fix: the unmodified code itself could not be built or analyzed.
        result.error = "修正前のコードのビルド・解析に失敗しました（設定のコマンドを確認してください）"
        result.log = str(before_log)
        for item in items:
            result.items[item.item_id] = {"mode": result.mode, "batch": True, "build_ok": None, "problems": [],
                                          "note": result.error + "。修正案は確かめられませんでした",
                                          "log": str(before_log), "log_tail": _tail(before_log)}
        return []

    @staticmethod
    def _apply_all(root: Path, items: list[BatchItem], result: BatchResult) -> list[BatchItem]:
        """Apply every fix to one copy; fixes that clash with an earlier one are left out."""
        applied = []
        for item in items:
            error = apply_patch(root, item.patch_path, item.strip)
            if error:
                # Not a problem of this fix: it only could not be checked together with the others.
                result.items[item.item_id] = {
                    "mode": result.mode, "batch": True, "applied": False, "build_ok": None, "problems": [],
                    "note": "ほかの修正案と同じ箇所を変えているため、まとめた検証に入れられませんでした",
                    "detail": error[:500]}
            else:
                applied.append(item)
        return applied


def _assign_build_failure(result: BatchResult, applied: list[BatchItem], log: Path) -> None:
    """Blame the fixes whose files appear in the error lines."""
    tail = _tail(log)
    text = log.read_text(encoding="utf-8", errors="replace").lower()
    error_lines = [line for line in text.splitlines() if "error" in line]
    blamed = [i for i in applied if any(Path(f).name.lower() in line for f in i.files for line in error_lines)]
    for item in applied:
        entry = {"mode": result.mode, "batch": True, "applied": True, "log": str(log)}
        if blamed and item not in blamed:
            entry.update(build_ok=None, problems=[],
                         note="他の修正案のビルドエラーのため、この修正案は確認できませんでした")
        elif blamed:
            entry.update(build_ok=False, log_tail=tail,
                         problems=["ビルドエラーがこの修正案の変更したファイルで出ています"])
        else:
            entry.update(build_ok=False, log_tail=tail,
                         problems=["ビルドに失敗しました（原因の修正案を特定できません）"])
        result.items[item.item_id] = entry


def _assign_analysis(result: BatchResult, applied: list[BatchItem], baseline: list[dict[str, Any]],
                     records: list[dict[str, Any]], log: Path) -> None:
    """Per fix: which warnings are gone, and which new warnings are in files it changed."""
    known = {_key(r) for r in baseline}
    new = [r for r in records if _key(r) not in known]
    assigned: set[int] = set()
    for item in applied:
        remaining = [i.cid for i in item.issues if any(matches(i, r) for r in records)]
        # a warning the unmodified code did not show either cannot be said to be gone
        unconfirmed = [i.cid for i in item.issues
                       if i.cid not in remaining and not any(matches(i, r) for r in baseline)]
        own = [n for n, r in enumerate(new) if _touches(r["file"], item.files)]
        assigned |= set(own)
        problems = []
        if remaining:
            problems.append(f"警告が残っています: CID {remaining}")
        if own:
            problems.append(f"この修正案が変更したファイルで新しい警告が {len(own)} 件出ています")
        result.items[item.item_id] = {
            "mode": result.mode, "batch": True, "applied": True, "build_ok": True,
            "resolved_cids": [i.cid for i in item.issues if i.cid not in remaining and i.cid not in unconfirmed],
            "remaining_cids": remaining, "unconfirmed_cids": unconfirmed, "new_issues": [new[n] for n in own][:20],
            "new_issue_count": len(own), "problems": problems, "log": str(log)}
    result.unassigned_new_issues = [r for n, r in enumerate(new) if n not in assigned][:50]


def trial_build(config: VerifyConfig, root: Path, log: Path, secret_envs: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build unmodified code once to confirm the settings work."""
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    started = time.time()
    ok = _run_shell(build_command_line(config, root, "build"), root, log, (*secret_envs, "COVERITY_PASSPHRASE"))
    return {"build_ok": ok, "seconds": round(time.time() - started, 1), "log": str(log),
            "log_tail": "" if ok else _tail(log)}
