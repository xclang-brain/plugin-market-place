"""The run folder and its state ``run.json`` (format 2).

Only this skill writes run.json (design 4.11). Other parts read the fields fixed in
assets/run.schema.json; every other field is this skill's own and may change between versions.
A run made by another version of the scripts (``tool_version``) is read but never changed, and a run
of another format (an older plugin's) is listed as foreign and never read further.
"""

from __future__ import annotations

import hashlib
import re
import time
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from . import envvars, oplog
from .common import SCRIPTS_DIR, RunError, file_lock, now_iso, read_json, resolve_path, write_json

FORMAT = 2
RUN_FILE = "run.json"
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
STATUSES = ("pending", "in_progress", "done", "error", "merged")
SUBFOLDERS = ("items", "cid", "patches", "fixed", "work", "agents", "jobs", "schemas", "details")


@lru_cache(maxsize=None)
def tool_version() -> str:
    """The version of these scripts: a hash of run.py and runlib/*.py (line ends ignored)."""
    digest = hashlib.sha256()
    for path in sorted([SCRIPTS_DIR / "run.py", *(SCRIPTS_DIR / "runlib").glob("*.py")], key=lambda p: p.name):
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


def new_item(item_id: str, cids: list[int], **extra: Any) -> dict[str, Any]:
    item = {"id": item_id, "cids": list(cids), "status": "pending", "merged_into": None, "review": None,
            "applied": {}, "slot": None, "stage": None, "attempts": 0, "stale_retries": 0, "error": "",
            "started": None, "seconds": None, "split_from": None, "cluster": None, "fix_rounds": 0,
            "merged_from": [], "review_rounds": 0, "review_gen": 0, "review_fix_rounds": 0, "rework": [],
            "warnings": [], "repo_fingerprint": None, "result_sha": None}
    item.update(extra)
    return item


def is_group(item_id: str) -> bool:
    return item_id.startswith("G")


def counts(state: dict[str, Any]) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for item in state.get("items") or []:
        out[item.get("status", "pending")] = out.get(item.get("status", "pending"), 0) + 1
    out["total"] = sum(v for k, v in out.items() if k != "merged")
    return out


class Run:
    """One run folder. ``write=True``: the command will change it (refused for another tool_version)."""

    def __init__(self, folder: str | Path, write: bool = False):
        self.dir = resolve_path(folder)
        path = self.dir / RUN_FILE
        if not path.is_file():
            raise RunError(f"実行フォルダではありません（{RUN_FILE} がありません）: {self.dir}")
        self.state = self.load()
        if self.state.get("format") != FORMAT:
            raise RunError("この実行フォルダは、別の形で作られたものです（別の版や元の道具で作った実行）。"
                           "見ることはできますが、続きからの実行や変更はできません", foreign=True)
        if write and self.state.get("tool_version") != tool_version():
            raise RunError("この実行フォルダは、別の版のスクリプトで作られました。この版のスクリプトでは変更しません"
                           "（新しく実行を始めてください）", run_tool_version=self.state.get("tool_version"),
                           tool_version=tool_version())

    # ---- state ----------------------------------------------------------------------------

    @property
    def file(self) -> Path:
        return self.dir / RUN_FILE

    def load(self) -> dict[str, Any]:
        data = read_json(self.dir / RUN_FILE)
        if not isinstance(data, dict):
            raise RunError(f"{RUN_FILE} の形が正しくありません: {self.dir}")
        return data

    @contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        """Read, change and write run.json while holding its lock."""
        with file_lock(self.file):
            self.state = self.load()
            yield self.state
            write_json(self.file, self.state)

    def reload(self) -> dict[str, Any]:
        self.state = self.load()
        return self.state

    @property
    def repo(self) -> Path:
        return Path(self.state["repo_root"])

    @property
    def settings(self) -> dict[str, Any]:
        return self.state["settings"]

    def item(self, item_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        for item in (state or self.state)["items"]:
            if item["id"] == item_id:
                return item
        raise RunError(f"作業がありません: {item_id}", items=[i["id"] for i in (state or self.state)["items"]])

    def find(self, item_id: str) -> dict[str, Any] | None:
        return next((i for i in self.state["items"] if i["id"] == item_id), None)

    def counts(self) -> dict[str, int]:
        return counts(self.state)

    # ---- paths ----------------------------------------------------------------------------

    def item_dir(self, item_id: str) -> Path:
        return self.dir / "items" / item_id

    def result_file(self, item_id: str) -> Path:
        return self.item_dir(item_id) / "result.json"

    def review_file(self, item_id: str) -> Path:
        return self.item_dir(item_id) / "review.json"

    def report_file(self, item_id: str, ext: str = "html") -> Path:
        return self.dir / "cid" / f"{item_id}.{ext}"

    def summary_file(self, ext: str = "html") -> Path:
        return self.dir / f"summary.{ext}"

    def patch_file(self, item_id: str, kind: str = "fix") -> Path:
        return self.dir / "patches" / f"{item_id}-{kind}.patch"

    def work(self, name: str) -> Path:
        """Work copies with short names (Windows paths): ``w<n>`` fix copies, ``n<n>`` annotation copies,
        ``a`` the analyzed revision, ``m`` merged fixes, ``vl``/``vb``/``va`` the build verification."""
        return self.dir / "work" / name

    # ---- log ------------------------------------------------------------------------------

    def secrets(self) -> list[str]:
        cov = (self.state.get("settings") or {}).get("coverity") or {}
        return envvars.secrets(cov.get("user_env", "COV_USER"), cov.get("key_env", "COV_AUTH_KEY"))

    def log(self, event: str, message: str, *, level: str = "info", item: str | None = None, **fields: Any) -> None:
        oplog.log(self.dir, event, message, level=level, item=item, secrets=self.secrets(), **fields)


def create_run(output: Path, state: dict[str, Any]) -> Run:
    """Make ``<output>/<YYYYmmdd-HHMMSS>[-n]/`` with its folders and run.json."""
    output.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id, n = base, 1
    while True:
        try:
            (output / run_id).mkdir()
            break
        except FileExistsError:
            n += 1
            run_id = f"{base}-{n}"
    folder = output / run_id
    for sub in SUBFOLDERS:
        (folder / sub).mkdir(parents=True, exist_ok=True)
    full = {"format": FORMAT, "tool_version": tool_version(), "run_id": run_id, "created_at": now_iso(),
            "stopped": False, "stopped_at": None, "finished": False, "finished_at": None, **state}
    write_json(folder / RUN_FILE, full)
    return Run(folder)


def finish_item(item: dict[str, Any], status: str, error: str = "") -> None:
    item["status"] = status
    item["error"] = error
    item["seconds"] = round(time.time() - item["started"], 1) if item.get("started") else None
    item["slot"] = None
    item["stage"] = None


def list_runs(output: Path) -> list[Path]:
    """Run folders in the output folder, newest first."""
    if not output.is_dir():
        return []
    found = [p for p in output.iterdir() if p.is_dir() and RUN_ID.match(p.name) and (p / RUN_FILE).is_file()]
    return sorted(found, key=lambda p: p.name, reverse=True)


def unfinished(state: dict[str, Any], folder: Path) -> bool:
    """Whether the run can be resumed: work remains, or the summary was not made yet."""
    if state.get("stopped") or not state.get("finished"):
        return True
    return bool(state.get("items")) and not (folder / "summary.html").is_file()
