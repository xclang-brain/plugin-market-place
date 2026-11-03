"""Approvals of a run (``approvals.json``): fix, deviation or reject per item, and edits of the deviation.

The person enters them in the screen (``ct.py gui``). ``ct.py summary`` puts the AI's recommendation
in for items that have none yet, and ``ct.py preview`` reads them. Runs made before approvals.json
kept the approvals in the table of summary.md and the edited deviation in cid/<ID>.md; they are
read from there until approvals.json is written (the first change, or ``ct.py summary`` again).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .common import CtError, file_lock, now, read_json, write_json
from .models import Attributes, TriageResult
from .report import read_approval_rows, read_approvals, read_deviation
from .run import Run

APPROVALS_FILE = "approvals.json"
VERSION = 1
DECISIONS = ("fix", "deviation", "reject")
FIELDS = {"classification": "Classification", "action": "Action", "severity": "Severity", "comment": "逸脱コメント"}


def _path(run: Run) -> Path:
    return run.dir / APPROVALS_FILE


def _result(run: Run, item_id: str) -> TriageResult | None:
    try:
        return TriageResult.model_validate(read_json(run.result_file(item_id)))
    except (CtError, ValidationError):
        return None


def _proposal(result: TriageResult) -> dict[str, str]:
    return {k: getattr(result.deviation, k).strip() for k in FIELDS}


def default_decision(run: Run, item_id: str, result: TriageResult) -> str | None:
    """The AI's recommendation as an approval. A fix with nothing to take in (the latest revision is
    already fixed, so there is no diff) is "not applied": "fix" could not be applied by preview."""
    if (result.recommendation == "fix" and result.fix.already_fixed_on_latest
            and "fix" not in read_json(run.item_dir(item_id) / "saved.json", {})):
        return None
    return result.recommendation


def already_fixed(run: Run, item_id: str) -> bool:
    result = _result(run, item_id)
    return bool(result and result.fix.already_fixed_on_latest)


def _entry(decision: str | None, recommendation: str | None, deviation: dict[str, str] | None = None,
           updated_at: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"decision": decision, "edited": decision != recommendation or deviation is not None,
                             "updated_at": updated_at or now()}
    if deviation is not None:
        entry["deviation"] = deviation
    return entry


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    """An item as read from the file; values that cannot be used are dropped, never applied."""
    out = dict(entry)
    if out.get("decision") not in DECISIONS:
        out["decision"] = None
    deviation = out.get("deviation")
    if deviation is not None and not (isinstance(deviation, dict) and all(
            isinstance(deviation.get(k), str) and deviation[k].strip() for k in FIELDS)):
        out.pop("deviation")
    out["edited"] = bool(out.get("edited"))
    return out


def _legacy(run: Run) -> dict[str, dict[str, Any]]:
    """Approvals of a run made before approvals.json: the table of summary.md and cid/<ID>.md."""
    summary = run.summary_file("md")
    try:
        rows, _ = read_approval_rows(summary.read_text(encoding="utf-8"))
    except CtError:
        rows = {}
    stamp = datetime.fromtimestamp(summary.stat().st_mtime).isoformat(timespec="seconds")
    items = {}
    for item in run.state.items:
        if item.status != "done" or item.id not in rows:
            continue
        result = _result(run, item.id)
        deviation = None
        report = run.report_file(item.id, "md")
        if result and report.is_file():
            try:
                attrs, comment = read_deviation(report.read_text(encoding="utf-8"))
            except CtError:
                pass
            else:
                edit = {**attrs.model_dump(), "comment": comment}
                deviation = None if edit == _proposal(result) else edit
        items[item.id] = _entry(rows[item.id], result.recommendation if result else None, deviation, stamp)
    return items


def summary_made(run: Run) -> bool:
    """``ct.py summary`` has made the summary (summary.html, or summary.md in a run made before 0.4)."""
    return run.summary_file().is_file() or run.summary_file("md").is_file()


def _save(run: Run, items: dict[str, dict[str, Any]]) -> None:
    write_json(_path(run), {"version": VERSION, "updated_at": now(), "items": items})


def load(run: Run) -> dict[str, dict[str, Any]]:
    """``{item id: {"decision", "edited", "updated_at", ["deviation"]}}``; empty when there are none yet."""
    path = _path(run)
    if path.is_file():
        data = read_json(path)
        items = data.get("items") if isinstance(data, dict) else None
        return {str(k): _clean(v) for k, v in (items or {}).items() if isinstance(v, dict)}
    if run.summary_file("md").is_file():
        return _legacy(run)
    return {}


def ensure_defaults(run: Run, results: dict[str, TriageResult]) -> dict[str, dict[str, Any]]:
    """Put the AI's recommendation in for finished items that have no approval yet; keep the others."""
    with file_lock(_path(run)):
        items = load(run)
        added = [item_id for item_id in results if item_id not in items]
        for item_id in added:
            default = default_decision(run, item_id, results[item_id])
            items[item_id] = _entry(default, default)
        if added or not _path(run).is_file():
            _save(run, items)
    return items


def _checked_deviation(deviation: Any) -> dict[str, str]:
    if not isinstance(deviation, dict):
        raise CtError("逸脱の内容は classification / action / severity / comment の形で渡してください")
    values, problems = {}, []
    for key, label in FIELDS.items():
        value = deviation.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{label} が空です")
        elif key != "comment" and "\n" in value.strip():
            problems.append(f"{label} に改行は入れられません")
        else:
            values[key] = value.strip()
    if problems:
        raise CtError("逸脱の内容に直すところがあります", problems=problems)
    return values


def update(run: Run, item_id: str, decision: str | None, deviation: dict[str, Any] | None) -> dict[str, Any]:
    """Save the person's decision for one item and write its report (and the summary, once made) again.

    ``deviation`` None keeps the deviation edited earlier; the same values as the AI's proposal
    remove the edit (the report shows the AI's proposal again). An item can be approved while the
    run goes on; the summary is first written by ``ct.py summary``, which also puts the AI's
    recommendation in for the other items (until then, ``preview`` refuses the run).

    The entry saved is returned; with ``report_error`` when the pages could not be written again."""
    item = run.state.item(item_id)
    if item.status != "done":
        raise CtError(f"{item_id} はまだ終わっていません（状態: {item.status}）。承認できるのは終わった作業だけです")
    if decision in ("", "null"):
        decision = None
    if decision is not None and decision not in DECISIONS:
        raise CtError(f"承認は fix / deviation / reject か null（反映しない）のどれかです（今は {decision}）")
    result = _result(run, item_id)
    if result is None:
        raise CtError(f"{item_id} の結果ファイルを読めません: {run.result_file(item_id)}")
    edit = None if deviation is None else _checked_deviation(deviation)

    with file_lock(_path(run)):
        items = load(run)
        if edit is None:
            kept = (items.get(item_id) or {}).get("deviation")
        else:
            kept = None if edit == _proposal(result) else edit
        entry = _entry(decision, default_decision(run, item_id, result), kept)
        items[item_id] = entry
        _save(run, items)
        # inside the lock, so a later change is never overwritten by an earlier one's pages
        report_error = _write_pages(run, item_id)
    return {**entry, "report_error": report_error} if report_error else entry


def _write_pages(run: Run, item_id: str) -> str | None:
    """Write the item's report and the summary (once made) again; what went wrong, if anything.

    The approval is saved before and stays saved: the reports only show it (K-64), and preview reads
    approvals.json. So a report that cannot be written is told to the person, never an unsaved approval."""
    from . import triage  # triage imports this module

    try:
        triage.write_item_files(run, item_id)
        if summary_made(run):
            triage.write_summary_files(run)
    except CtError as exc:
        return str(exc)
    except OSError as exc:
        return f"ファイルの操作に失敗しました: {exc}"
    except Exception:  # noqa: BLE001 - a file of the run in a form the reports cannot read
        return "実行フォルダのファイルに、形の違うものがあります"
    return None


def decisions(run: Run) -> dict[str, str]:
    """``{item id: "fix" | "deviation" | "reject"}`` of the approved items (preview applies these)."""
    if not _path(run).is_file() and run.summary_file("md").is_file():
        # a run made before approvals.json: a typo in the approval column is listed, as before
        return read_approvals(run.summary_file("md").read_text(encoding="utf-8"))
    return {k: v["decision"] for k, v in load(run).items() if v.get("decision")}


def deviation_for(run: Run, item_id: str) -> tuple[Attributes, str]:
    """The deviation to register: the person's edit if any, else the AI's proposal."""
    if _path(run).is_file():
        edit = (load(run).get(item_id) or {}).get("deviation")
        if edit:
            return Attributes(**{k: edit[k] for k in ("classification", "action", "severity")}), edit["comment"]
    elif run.report_file(item_id, "md").is_file():
        return read_deviation(run.report_file(item_id, "md").read_text(encoding="utf-8"))
    result = TriageResult.model_validate(read_json(run.result_file(item_id)))
    return Attributes(**result.deviation.model_dump(exclude={"comment"})), result.deviation.comment
