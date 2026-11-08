"""The run folders (``<output_dir>/<YYYYmmdd-HHMMSS>[-n]/``): read only, through the fields of the contract.

Read here: ``run.json`` (the fields of ``assets/run.schema.json``: ``format``, ``run_id``, ``created_at``,
``filter_name``, ``review``, ``integration``, ``stage``, ``stopped``, ``halted``, ``halt_reason``,
``items[].{id, cids, status, merged_into, merged_from, review, error}``), ``items/<ID>/result.json`` /
``review.json`` / ``saved.json`` / ``issue-<CID>.json``, ``issues.json``, ``verify.json``, ``approvals.json``,
``apply-code.result.json`` (what was applied: its ``applied`` map), ``summary.html``, ``cid/<ID>.html``,
``patches/`` and ``fixed/``. A run whose ``format`` is not 2 was made by another tool: it is listed, and
shown read only.

The confidence shown to the person is the shared rule of ``scripts/confidence.py`` (a byte copy of the
batch run's file; the contract "confidence").

Reports are never written here: the screen is where the current approvals are seen.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import schema
from .common import ASSETS, GuiError, NotFound, read_json, resolve_path, try_json
from .settings import output_dir

RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
FORMAT = 2
RUNS_LISTED = 50
STATUSES = ("pending", "in_progress", "done", "error", "merged")
OUTCOMES = ("pass", "flag", "rework", "unverified")
DECISIONS = ("fix", "deviation", "reject")
ORDER = {"low": 0, "medium": 1, "high": 2}
TAKEN_REVIEWS = ("pass", "flag", "rework")  # run.json items[].review once the batch run took review.json in
APPLY_RECORD = "apply-code.result.json"
APPLIED_JA = {"fix": "修正", "annotation": "逸脱の注釈", "coverity": "Coverity への逸脱の登録", "reject": "却下"}
FOREIGN_NOTE = ("この実行は、別の形（ほかの道具や前の版）で作られたため、この画面では見るだけです。"
                "承認は選べません。一覧のページがあれば、そこで結果を見られます。")
WAIT_TEXT = ("統合・検証で結果が変わることがあるため、統合・検証が終わって一覧（summary.html）ができてから承認できます"
             "（一括のトリアージが終わるまでお待ちください）")
STAGE_JA = {
    "initial": "初回解析（ビルド・解析・サーバへの登録）",
    "fetch": "Coverity から警告を取っている",
    "details": "警告経路を取っている",
    "items": "警告を調べている",
    "integrate": "同じ原因の警告をまとめている",
    "review": "結果を確かめている",
    "verify": "ビルドで確かめている",
    "summary": "まとめと一覧を作っている",
    "done": "終わりました",
    "stopped": "止めました",
    "foreign": "別の形の実行（見るだけ）",
}
RESULT_SCHEMA = schema.load(ASSETS / "result.schema.json")


def _load_confidence() -> Any:
    """scripts/confidence.py (stdlib only, reads no file): the rule of the confidence shown to people."""
    path = Path(__file__).resolve().parents[1] / "confidence.py"
    spec = importlib.util.spec_from_file_location("gui_confidence_rule", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIDENCE = _load_confidence()


# ---- one run -------------------------------------------------------------------------------


def _cids(value: Any) -> list[int]:
    out = []
    for cid in value if isinstance(value, list) else []:
        if isinstance(cid, int) and not isinstance(cid, bool):
            out.append(cid)
        elif isinstance(cid, str) and cid.isdigit():
            out.append(int(cid))
    return out


def _item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("id") in (None, ""):
        return None
    item = dict(raw)
    item["id"] = str(raw["id"])
    item["cids"] = _cids(raw.get("cids"))
    if not item["cids"] and item["id"].isdigit():
        item["cids"] = [int(item["id"])]
    item["status"] = raw.get("status") if raw.get("status") in STATUSES else "pending"
    return item


class Run:
    """A run folder and its run.json (GuiError when run.json cannot be read)."""

    def __init__(self, folder: Path):
        self.dir = folder
        data = read_json(folder / "run.json")
        if not isinstance(data, dict):
            raise GuiError(f"run.json の形が違います（「キー: 値」の形ではありません）: {folder / 'run.json'}")
        self.data = data
        self.foreign = data.get("format") != FORMAT
        self.items = [] if self.foreign else [i for i in map(_item, data.get("items") or []) if i]
        self._applied: dict[str, Any] | None = None

    @property
    def run_id(self) -> str:
        return self.dir.name

    def item(self, item_id: str) -> dict[str, Any]:
        for item in self.items:
            if item["id"] == item_id:
                return item
        raise NotFound(f"作業がありません: {item_id}")

    def item_dir(self, item_id: str) -> Path:
        return self.dir / "items" / item_id

    @property
    def summary_made(self) -> bool:
        return (self.dir / "summary.html").is_file()

    @property
    def review_on(self) -> bool:
        return bool(self.data.get("review"))

    @property
    def integration_on(self) -> bool:
        return bool(self.data.get("integration"))

    def applied_parts(self, item_id: str) -> list[str]:
        """What the apply skill applied for an item (``applied`` of apply-code.result.json): fix / annotation /
        coverity / reject, in that order."""
        if self._applied is None:
            record = try_json(self.dir / APPLY_RECORD)
            applied = record.get("applied") if isinstance(record, dict) else None
            self._applied = applied if isinstance(applied, dict) else {}
        entry = self._applied.get(item_id)
        return [part for part in APPLIED_JA if isinstance(entry, dict) and entry.get(part)]

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for item in self.items:
            counts[item["status"]] += 1
        return counts

    def approval_wait(self) -> str | None:
        """Why approvals cannot be chosen yet: the integration and the review may still change the items."""
        if (self.integration_on or self.review_on) and not self.summary_made:
            return WAIT_TEXT
        return None


def runs_folder(repo: Path) -> tuple[Path, str | None]:
    return output_dir(repo)


def find_run(repo: Path, run_id: str) -> Run:
    """A run folder directly in the output folder of the settings, by its name only."""
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        raise NotFound(f"実行が見つかりません: {run_id}")
    output, _ = runs_folder(repo)
    folder = output / run_id
    if folder.is_symlink() or not (folder / "run.json").is_file() or resolve_path(folder).parent != resolve_path(output):
        raise NotFound(f"実行が見つかりません: {run_id}（結果を置く場所: {output}）")
    return Run(folder)


# ---- stage ---------------------------------------------------------------------------------


def stage(run: Run) -> tuple[str, str]:
    """The stage of a run, from its files (a stage the run tool wrote in run.json is used first)."""
    data = run.data
    if run.foreign:
        return "foreign", STAGE_JA["foreign"]
    if data.get("stopped"):
        return "stopped", STAGE_JA["stopped"]
    if run.summary_made and not any(i["status"] in ("pending", "in_progress") for i in run.items):
        applied = any(run.applied_parts(i["id"]) for i in run.items)
        return "done", "反映まで終わりました" if applied else "一覧ができました（承認を待っています）"
    if isinstance(data.get("stage"), str) and data["stage"]:
        label = data.get("stage_label") if isinstance(data.get("stage_label"), str) else None
        return data["stage"], label or STAGE_JA.get(data["stage"], data["stage"])
    initial = try_json(run.dir / "analysis" / "status.json")
    if not run.items and isinstance(initial, dict) and initial.get("status") in ("running", "failed"):
        failed = initial.get("status") == "failed"
        return "initial", "初回解析に失敗しました（止まっています）" if failed else STAGE_JA["initial"]
    if not run.items:
        found = try_json(run.dir / "issues.json")
        if not (isinstance(found, dict) and found.get("finished_at")):
            return "fetch", STAGE_JA["fetch"]
    if any(i["status"] in ("pending", "in_progress") for i in run.items):
        return "items", STAGE_JA["items"]
    if run.review_on and any(i["status"] == "done" and review_outcome(run, i) is None for i in run.items):
        return "review", STAGE_JA["review"]
    return "summary", STAGE_JA["summary"]


def unfinished(run: Run) -> bool:
    if run.foreign or run.data.get("stopped"):
        return False
    return not run.summary_made or any(i["status"] in ("pending", "in_progress") for i in run.items)


def run_info(run: Run) -> dict[str, Any]:
    data = run.data
    info: dict[str, Any] = {"run_id": run.run_id, "run_dir": str(run.dir), "foreign": run.foreign,
                            "created_at": data.get("created_at") if isinstance(data.get("created_at"), str) else "",
                            "filter_name": data.get("filter_name") if isinstance(data.get("filter_name"), str) else "",
                            "summary": run.summary_made}
    if run.foreign:
        return {**info, "counts": {}, "total": 0, "unfinished": False, "stopped": False, "applied": 0,
                "stage": "foreign", "stage_label": STAGE_JA["foreign"], "review": False, "integration": False}
    counts = run.counts()
    key, label = stage(run)
    return {**info, "counts": counts, "total": len(run.items) - counts["merged"], "unfinished": unfinished(run),
            "stopped": bool(data.get("stopped")), "applied": sum(1 for i in run.items if run.applied_parts(i["id"])),
            "stage": key, "stage_label": label, "review": run.review_on, "integration": run.integration_on}


def runs_list(repo: Path) -> dict[str, Any]:
    """The runs in the output folder, newest first; runs whose run.json cannot be read are listed apart."""
    output, note = runs_folder(repo)
    found, unreadable = [], []
    try:
        names = sorted((p.name for p in output.iterdir() if RUN_ID.match(p.name)), reverse=True)
    except OSError:
        names = []
    for name in names:
        if len(found) >= RUNS_LISTED:
            break
        folder = output / name
        if folder.is_symlink() or not folder.is_dir() or not (folder / "run.json").exists():
            continue
        try:
            found.append(run_info(Run(folder)))
        except GuiError as exc:
            if len(unreadable) < RUNS_LISTED:
                unreadable.append({"run_id": name, "error": str(exc)})
    out: dict[str, Any] = {"ok": True, "runs": found, "output_dir": str(output), "unreadable": unreadable}
    notes = [n for n in (note,) if n]
    if not found and not unreadable:
        notes.append(f"まだ実行がありません（結果を置く場所: {output}）。一括のトリアージを始めると、ここに出ます")
    if notes:
        out["note"] = "\n".join(notes)
    return out


# ---- the results of one item ---------------------------------------------------------------


def result_of(run: Run, item_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """result.json of an item, checked with the contract's schema; None and why when it cannot be used."""
    path = run.item_dir(item_id) / "result.json"
    try:
        data = read_json(path)
    except GuiError as exc:
        return None, str(exc)
    problems = schema.errors(data, RESULT_SCHEMA) if RESULT_SCHEMA else []
    if not isinstance(data, dict) or problems or not isinstance(data.get("verdict"), dict):
        return None, f"結果ファイルの形が違います: {path}" + (f"（{problems[0]}）" if problems else "")
    return data, None


def _review_file(run: Run, item_id: str) -> dict[str, Any] | None:
    data = try_json(run.item_dir(item_id) / "review.json")
    return data if isinstance(data, dict) and data.get("outcome") in OUTCOMES else None


def review_outcome(run: Run, item: dict[str, Any]) -> str | None:
    """pass / flag / rework / unverified of the review (None: not reviewed, or no review in this run)."""
    value = item.get("review")
    if isinstance(value, dict):
        value = value.get("outcome")
    if value in OUTCOMES:
        return value
    found = _review_file(run, item["id"])
    return found["outcome"] if found else None


def effective_confidence(run: Run, item: dict[str, Any], result: dict[str, Any],
                         verified: dict[str, Any]) -> tuple[str, str]:
    """The confidence shown to the person and its reason (the shared rule of scripts/confidence.py).

    The review counts once the batch run took it in (run.json ``items[].review`` is pass / flag / rework; an
    unverified item has none), as in the summary; the build check is the item's entry of verify.json."""
    review = _review_file(run, item["id"]) if item.get("review") in TAKEN_REVIEWS else None
    entry = verified.get(item["id"]) if isinstance(verified, dict) else None
    shown = CONFIDENCE.effective_confidence(result, review, entry if isinstance(entry, dict) else None)
    return shown["confidence"], shown["reason"]


def verify_results(run: Run) -> tuple[dict[str, Any], str | None]:
    path = run.dir / "verify.json"
    if not path.is_file():
        return {}, None
    data = try_json(path)
    if not isinstance(data, dict):
        return {}, (f"verify.json を読めないため（壊れているか、形が違います）、ビルドでの検証の結果を"
                    f"確信度に入れていません: {path}")
    return data, None


def _issue_of(run: Run, item: dict[str, Any], issues: dict[int, dict[str, Any]]) -> dict[str, Any]:
    if not item["cids"]:
        return {}
    cid = item["cids"][0]
    found = try_json(run.item_dir(item["id"]) / f"issue-{cid}.json")
    if isinstance(found, dict) and isinstance(found.get("issue"), dict):
        return found["issue"]
    return issues.get(cid, {})


def issues_by_cid(run: Run) -> dict[int, dict[str, Any]]:
    data = try_json(run.dir / "issues.json")
    out: dict[int, dict[str, Any]] = {}
    for issue in (data.get("issues") if isinstance(data, dict) else None) or []:
        if isinstance(issue, dict) and isinstance(issue.get("cid"), int):
            out[issue["cid"]] = issue
    return out


def has_fix(run: Run, item_id: str) -> bool:
    saved = try_json(run.item_dir(item_id) / "saved.json")
    return (isinstance(saved, dict) and isinstance(saved.get("fix"), dict)) or \
        (run.dir / "patches" / f"{item_id}-fix.patch").is_file()


def default_decision(run: Run, item_id: str, result: dict[str, Any]) -> str | None:
    """The AI's recommendation as an approval; a fix with nothing to take in (fixed on the latest) is null."""
    fix = result.get("fix") if isinstance(result.get("fix"), dict) else {}
    if result.get("recommendation") == "fix" and fix.get("already_fixed_on_latest") and not has_fix(run, item_id):
        return None
    return result.get("recommendation") if result.get("recommendation") in DECISIONS else None


def title(item: dict[str, Any]) -> str:
    return f"{item['id']}（{len(item['cids'])} 件）" if item["id"].startswith("G") else item["id"]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def item_row(run: Run, item: dict[str, Any], issues: dict[int, dict[str, Any]], decided: dict[str, dict[str, Any]],
             verified: dict[str, Any]) -> dict[str, Any]:
    result, error = result_of(run, item["id"]) if item["status"] == "done" else (None, None)
    issue = _issue_of(run, item, issues)
    row: dict[str, Any] = {
        "id": item["id"], "title": title(item), "cids": item["cids"], "status": item["status"],
        "checker": _text(issue.get("checker")), "file": _text(issue.get("file")),
        "line": issue.get("line") if isinstance(issue.get("line"), int) else None, "impact": _text(issue.get("impact")),
        "recommendation": None, "confidence": None, "confidence_reason": "", "judgement": None, "summary": "",
        "default_decision": None, "already_fixed": False, "decision": None, "edited": False, "saved": False,
        "review": review_outcome(run, item) if item["status"] == "done" else None,
        "applied": bool(run.applied_parts(item["id"])),
        "applied_parts": [APPLIED_JA[p] for p in run.applied_parts(item["id"])],
        "merged_from": [str(x) for x in item.get("merged_from") or []] if isinstance(item.get("merged_from"), list) else [],
        "report": f"cid/{item['id']}.html" if (run.dir / "cid" / f"{item['id']}.html").is_file() else None,
        "result_error": error,
    }
    if result is not None:
        confidence, reason = effective_confidence(run, item, result, verified)
        default = default_decision(run, item["id"], result)
        row.update({"recommendation": result["recommendation"], "confidence": confidence, "confidence_reason": reason,
                    "judgement": result["verdict"].get("judgement"), "summary": _text(result["verdict"].get("summary")),
                    "default_decision": default,
                    "already_fixed": result["recommendation"] == "fix" and default is None})
        entry = decided.get(item["id"])
        # Until the summary puts the recommendations in, an item without an approval shows its recommendation.
        row["decision"] = entry["decision"] if entry else (None if run.summary_made else default)
        row["edited"] = bool(entry and entry.get("edited"))
        row["saved"] = entry is not None
    return row


def _not_done_text(item: dict[str, Any]) -> str:
    if item["status"] == "error":
        reason = str(item.get("error") or "").strip() or "理由の記録がありません"
        return f"{item['id']} は処理できませんでした（結果がないため、承認できません）。理由: {reason}"
    if item["status"] == "merged":
        into = f"{item.get('merged_into')} " if item.get("merged_into") else "ほかの作業"
        return f"{item['id']} は、統合で {into}にまとめました。承認は、まとめた作業で入れます"
    if item["status"] == "in_progress":
        return f"{item['id']} は、まだ調べている途中です。調べ終わると、ここで承認できます"
    return f"{item['id']} は、まだ調べていません。調べ終わると、ここで承認できます"


def run_summary(repo: Path, run_id: str) -> dict[str, Any]:
    """The results table: the finished items with their approvals, low confidence first."""
    from . import approvals  # approvals uses this module

    run = find_run(repo, run_id)
    out: dict[str, Any] = {"ok": True, "run": run_info(run), "foreign": run.foreign, "items": [], "errors": [],
                           "unprocessed": [], "summary_html": run.summary_made, "approval_wait": None, "notes": []}
    if run.foreign:
        out["notes"].append(FOREIGN_NOTE)
        return out
    decided, approvals_error = approvals.load(run)
    if approvals_error:
        out["notes"].append(approvals_error)
    issues = issues_by_cid(run)
    verified, verify_note = verify_results(run)
    if verify_note:
        out["notes"].append(verify_note)
    for item in run.items:
        if item["status"] == "done":
            out["items"].append(item_row(run, item, issues, decided, verified))
        elif item["status"] == "error":
            out["errors"].append({"id": item["id"], "cids": item["cids"],
                                  "reason": str(item.get("error") or "").strip() or "理由の記録がありません"})
        elif item["status"] != "merged":
            out["unprocessed"].append(item["id"])
    out["items"].sort(key=lambda r: (ORDER.get(r["confidence"], -1), r["id"]))
    unusable = [r["id"] for r in out["items"] if r["result_error"]]
    if unusable:
        out["notes"].append("結果ファイルを読めない作業があります（承認できません）: " + "、".join(unusable))
    out["approval_wait"] = run.approval_wait()
    return out


def _url(kind: str, run_id: str, rel: str) -> str:
    return f"/{kind}/{run_id}/{quote(rel, safe='/')}"


def _files(run: Run, item_id: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    patches, fixed = [], []
    for kind in ("fix", "annotation"):
        name = f"{item_id}-{kind}.patch"
        if (run.dir / "patches" / name).is_file():
            patches.append({"kind": kind, "name": name, "url": _url("view", run.run_id, f"patches/{name}"),
                            "raw_url": _url("files", run.run_id, f"patches/{name}")})
        base = run.dir / "fixed" / item_id / kind
        if not base.is_dir() or base.is_symlink():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not (Path(root) / d).is_symlink())
            for file in sorted(files):
                path = Path(root) / file
                if path.is_symlink():
                    continue
                rel = path.relative_to(base).as_posix()
                full = f"fixed/{item_id}/{kind}/{rel}"
                fixed.append({"kind": kind, "path": rel, "url": _url("view", run.run_id, full),
                              "raw_url": _url("files", run.run_id, full)})
                if len(fixed) >= 200:
                    return patches, fixed
    return patches, fixed


DEVIATION_FIELDS = ("classification", "action", "severity", "comment")


def ai_deviation(result: dict[str, Any]) -> dict[str, str]:
    deviation = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
    out = {k: str(deviation.get(k) or "").strip() for k in DEVIATION_FIELDS}
    out["severity"] = out["severity"] or "Unspecified"
    return out


def item_detail(repo: Path, run_id: str, item_id: str) -> dict[str, Any]:
    """One finished item for the approval panel: the deviation to register, and links to its files."""
    from . import approvals

    run = find_run(repo, run_id)
    if run.foreign:
        raise GuiError(FOREIGN_NOTE)
    item = run.item(item_id)
    if item["status"] != "done":
        raise GuiError(_not_done_text(item))
    result, error = result_of(run, item_id)
    if result is None:
        raise GuiError(f"{item_id} の結果ファイルを読めないため、承認できません。{error}")
    decided, approvals_error = approvals.load(run)
    verified, _ = verify_results(run)
    ai = ai_deviation(result)
    edit = (decided.get(item_id) or {}).get("deviation")
    patches, fixed = _files(run, item_id)
    out = {"ok": True, "item": item_row(run, item, issues_by_cid(run), decided, verified),
           "approval_wait": run.approval_wait(), "deviation": dict(edit) if edit else ai, "ai_deviation": ai,
           "deviation_edited": bool(edit), "patches": patches, "fixed_files": fixed,
           "report_url": _url("files", run.run_id, f"cid/{item_id}.html") if (run.dir / "cid" / f"{item_id}.html").is_file() else None}
    if approvals_error:
        out["deviation_note"] = approvals_error
    return out


# ---- files of a run (/files and /view) -------------------------------------------------------


def _plain(name: str) -> bool:
    return name not in ("", ".", "..") and not any(c in name for c in "/\\:\0")


def _shown(parts: list[str], view: bool) -> bool:
    """Only the reports, the patches and the fixed files; /view only the patches and the fixed files."""
    head, name = parts[0], parts[-1]
    if head == "patches":
        return len(parts) == 2 and name.endswith(".patch")
    if head == "fixed":
        return len(parts) >= 4 and ITEM_ID.match(parts[1]) is not None
    if view:
        return False
    return parts == ["summary.html"] or (head == "cid" and len(parts) == 2 and name.endswith(".html"))


def _linked(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction = getattr(path, "is_junction", None)
    return bool(junction and junction())


def run_file(repo: Path, run_id: str, rel: str, view: bool = False) -> tuple[Path, list[str]]:
    """The file ``rel`` (as in the URL) of a run, and its path parts; NotFound for anything else.

    No ``..``, no encoded separators and no links, which could point anywhere on this PC."""
    try:
        parts = [unquote(p, errors="strict") for p in rel.split("/")]
    except UnicodeDecodeError:
        raise NotFound(rel) from None
    if not parts or not all(_plain(p) for p in parts) or not _shown(parts, view):
        raise NotFound(rel)
    run = find_run(repo, run_id)
    path = run.dir
    for name in parts:
        path = path / name
        if _linked(path):
            raise NotFound(rel)
    if not path.is_file() or resolve_path(run.dir) not in resolve_path(path).parents:
        raise NotFound(rel)
    return path, parts


