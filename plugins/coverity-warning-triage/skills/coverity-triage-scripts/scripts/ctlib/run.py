"""One run folder: settings of the run, progress of every item, and the paths of its files.

Layout (``<output_dir>/<run id>/``)::

    run.json  filter.json  issues.json  config.snapshot.yaml
    items/<ID>/   issue-<CID>.json  brief.md  progress.json  result.json  saved.json  review.json
    agents/       brief and progress of the collector, integrator and reviewer agents
    analysis/     initial analysis (status.json, initial.log)
    integration/  candidates.json  plan.json  (option agents.integration)
    work/         fix-<n>/  annotation-<n>/  analyzed/   (work copies; removed by ``summary``)
    cid/<ID>.html  patches/  fixed/<ID>/  summary.html  approvals.json  apply-plan.json  operations.log
    apply-code.result.json   (the result of ``apply-code``, returned again for the same confirmation string)
    cid/<ID>.md  summary.md   (only with the option ``markdown``, and in runs made before approvals.json)
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

from .common import CtError, file_lock, read_json, resolve_path, write_json
from .config import ProjectConfig, load_config
from .models import Issue
from .vcs import Vcs, make_vcs

RUN_FILE = "run.json"
Status = Literal["pending", "in_progress", "done", "error", "merged"]  # merged: taken into another item


class ItemState(BaseModel):
    id: str
    cids: list[int]
    status: Status = "pending"
    slot: int | None = None
    attempts: int = 0
    error: str = ""
    started: float | None = None
    seconds: float | None = None
    split_from: str | None = None
    repo_fingerprint: str | None = None
    warnings: list[str] = Field(default_factory=list)
    applied: dict[str, Any] = Field(default_factory=dict)
    cluster: int | None = None  # items cut from one cluster of the grouping share the number
    fix_rounds: int = 0  # times the worker was asked to fix its result file in this attempt
    merged_from: list[str] = Field(default_factory=list)  # an item the integration made
    merged_into: str | None = None
    review: str | None = None  # pass / flag / rework / unverified (option agents.review)
    review_rounds: int = 0  # times the review sent the item back to the worker
    rework: list[str] = Field(default_factory=list)  # what the worker is to change in the next attempt

    @property
    def is_group(self) -> bool:
        return self.id.startswith("G")


class RunState(BaseModel):
    run_id: str
    repo_root: str
    created_at: str
    filter_name: str
    stream: str
    limit: int
    verify_mode: str
    planned: bool = False
    found: int | None = None
    limited: bool = False
    latest_revision: str | None = None
    analyzed_revision: str | None = None
    revision_notes: list[str] = Field(default_factory=list)
    items: list[ItemState] = Field(default_factory=list)
    # set at new-run from the settings (and its options), so a resumed run keeps its steps
    initial: bool = False
    integration: bool = False
    review: bool = False
    max_rework: int = 1
    # progress of the steps around the work items
    fetch_rounds: int = 0  # times the collector was started to fetch the warning paths
    paths_checked: bool = False  # after the last fetch: the person was told of the paths that could not be fetched
    integrated: bool = False
    verified: bool = False
    roles: dict[str, dict[str, Any]] = Field(default_factory=dict)  # the collector/integrator/reviewer at work
    notes: list[str] = Field(default_factory=list)

    def item(self, item_id: str) -> ItemState:
        for item in self.items:
            if item.id == item_id:
                return item
        raise CtError(f"作業がありません: {item_id}", items=[i.id for i in self.items])

    def counts(self) -> dict[str, int]:
        counts = {"pending": 0, "in_progress": 0, "done": 0, "error": 0}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1  # "merged" only when the integration made one
        return counts


class Run:
    def __init__(self, folder: str | Path):
        self.dir = resolve_path(folder)
        if not (self.dir / RUN_FILE).is_file():
            raise CtError(f"実行フォルダではありません: {self.dir}（ct.py runs で一覧を見られます）")
        self.state = self.load()
        self.repo = Path(self.state.repo_root)
        self._config: ProjectConfig | None = None

    # ---- files ---------------------------------------------------------------------------

    @property
    def run_file(self) -> Path:
        return self.dir / RUN_FILE

    def load(self) -> RunState:
        return RunState.model_validate(read_json(self.dir / RUN_FILE))

    @contextmanager
    def update(self) -> Iterator[RunState]:
        """Read, change and write run.json while holding the lock."""
        with file_lock(self.run_file):
            self.state = self.load()
            yield self.state
            write_json(self.run_file, self.state.model_dump())

    @property
    def config(self) -> ProjectConfig:
        if self._config is None:
            self._config = load_config(self.repo)
        return self._config

    @property
    def vcs(self) -> Vcs:
        return make_vcs(self.repo, self.config.vcs)

    def item_dir(self, item_id: str) -> Path:
        return self.dir / "items" / item_id

    def result_file(self, item_id: str) -> Path:
        return self.item_dir(item_id) / "result.json"

    def report_file(self, item_id: str, ext: str = "html") -> Path:
        return self.dir / "cid" / f"{item_id}.{ext}"

    def summary_file(self, ext: str = "html") -> Path:
        return self.dir / f"summary.{ext}"

    def copy_dir(self, kind: str, slot: int | None = None) -> Path:
        """Work copies: ``fix-<slot>``, ``annotation-<slot>`` and ``analyzed``."""
        return self.dir / "work" / (kind if slot is None else f"{kind}-{slot}")

    def issues(self) -> dict[int, Issue]:
        data = read_json(self.dir / "issues.json")
        return {i["cid"]: Issue.model_validate(i) for i in data.get("issues") or []}

    def mapped_issues(self) -> dict[int, Issue]:
        """Issues with repository-relative paths (``plan`` writes them)."""
        data = read_json(self.dir / "issues.mapped.json")
        return {i["cid"]: Issue.model_validate(i) for i in data}


def create_run(output: Path, *, repo: Path, filter_name: str, stream: str, limit: int,
               verify_mode: str, **steps: Any) -> Run:
    output.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id, n = base, 1
    while (output / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    folder = output / run_id
    for sub in ("items", "cid", "patches", "fixed", "work", "agents"):
        (folder / sub).mkdir(parents=True)
    state = RunState(run_id=run_id, repo_root=str(repo), created_at=datetime.now().isoformat(timespec="seconds"),
                     filter_name=filter_name, stream=stream, limit=limit, verify_mode=verify_mode, **steps)
    write_json(folder / RUN_FILE, state.model_dump())
    return Run(folder)


def finish_item(item: ItemState, status: Status, error: str = "") -> None:
    item.status = status
    item.error = error
    item.seconds = round(time.time() - item.started, 1) if item.started else None
    item.slot = None


def unfinished(state: RunState, folder: Path | None = None) -> bool:
    """Whether /coverity-run still has something to do in the run (it can be resumed).

    With the run folder, a run whose summary was not made yet is unfinished too."""
    counts = state.counts()
    if not state.planned or counts["pending"] + counts["in_progress"] + counts["error"]:
        return True
    if state.integration and not state.integrated:
        return True
    if state.review and any(i.status == "done" and i.review is None for i in state.items):
        return True
    return folder is not None and bool(state.items) and \
        not any((folder / f"summary.{ext}").is_file() for ext in ("html", "md"))


def list_runs(output: Path) -> list[Path]:
    return sorted((p.parent for p in output.glob(f"*/{RUN_FILE}")), reverse=True)
