"""The approvals of a run (``approvals.json``), the person's choices entered on the screen.

``{"version": 1, "updated_at": ISO, "items": {<ID>: {"decision", "edited", "updated_at", ["deviation"]}}}``

- decision: fix / deviation / reject / null (do not apply). The default is the recommendation.
- deviation: only when the person changed the deviation (a change back to the AI's proposal removes it).
- Only finished items. In a run with the integration or the review, only once the summary exists.
- The file is written under ``approvals.json.lock`` (the batch run adds the defaults the same way and
  never changes a person's choice). Other items and keys in the file are kept as they are.
- The reports are not written again: the screen is where the current approvals are seen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import GuiError, file_lock, now, read_json, write_json
from .runs import (APPLIED_JA, DECISIONS, DEVIATION_FIELDS, FOREIGN_NOTE, Run, _not_done_text, ai_deviation,
                   default_decision, find_run, result_of)

FILE = "approvals.json"
VERSION = 1
LABELS = {"classification": "Classification", "action": "Action", "severity": "Severity", "comment": "逸脱コメント"}
COMMENT_MAX = 200


def _path(run: Run) -> Path:
    return run.dir / FILE


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    """An item as read from the file; values that cannot be used are dropped (never applied)."""
    out = dict(entry)
    if out.get("decision") not in DECISIONS:
        out["decision"] = None
    deviation = out.get("deviation")
    if deviation is not None and not (isinstance(deviation, dict) and all(
            isinstance(deviation.get(k), str) and deviation[k].strip() for k in DEVIATION_FIELDS)):
        out.pop("deviation")
    out["edited"] = bool(out.get("edited"))
    return out


def _read(run: Run) -> tuple[dict[str, Any] | None, str | None]:
    path = _path(run)
    if not path.is_file():
        return None, None
    try:
        data = read_json(path)
    except GuiError as exc:
        return None, f"承認のファイルを読めません（{exc}）。承認は、ファイルを直すまで保存できません"
    if not isinstance(data, dict) or not isinstance(data.get("items", {}), dict):
        return None, f"承認のファイルの形が違います: {path}。承認は、ファイルを直すまで保存できません"
    return data, None


def load(run: Run) -> tuple[dict[str, dict[str, Any]], str | None]:
    """``{item id: entry}`` (empty when there are none yet), and what is wrong with the file."""
    data, error = _read(run)
    items = (data or {}).get("items") or {}
    return {str(k): _clean(v) for k, v in items.items() if isinstance(v, dict)}, error


def _checked(deviation: Any) -> dict[str, str]:
    if not isinstance(deviation, dict):
        raise GuiError("逸脱の内容は classification / action / severity / comment の形で渡してください")
    values, problems = {}, []
    for key, label in LABELS.items():
        value = deviation.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{label} が空です")
        elif key != "comment" and ("\n" in value.strip() or "\r" in value):
            problems.append(f"{label} に改行は入れられません")
        else:
            values[key] = value.strip()
    if problems:
        raise GuiError("逸脱の内容に直すところがあります", problems=problems)
    return values


def update(repo: Path, run_id: str, body: Any) -> dict[str, Any]:
    """Save the person's approval of one item: ``{item, decision, deviation?}`` (deviation null keeps the
    one saved before)."""
    if not isinstance(body, dict):
        raise GuiError("承認の内容が正しくありません")
    run = find_run(repo, run_id)
    if run.foreign:
        raise GuiError(FOREIGN_NOTE)
    item_id = body.get("item")
    if not isinstance(item_id, str) or not item_id:
        raise GuiError("承認する作業（item）を指定してください")
    item = run.item(item_id)
    if item["status"] != "done":
        raise GuiError(_not_done_text(item))
    wait = run.approval_wait()
    if wait:
        raise GuiError(wait)
    decision = body.get("decision")
    if decision in ("", "null"):
        decision = None
    if decision is not None and decision not in DECISIONS:
        raise GuiError(f"承認は fix / deviation / reject か null（反映しない）のどれかです（今は {decision}）")
    result, error = result_of(run, item_id)
    if result is None:
        raise GuiError(f"{item_id} の結果ファイルを読めないため、承認できません。{error}")
    edit = None if body.get("deviation") is None else _checked(body["deviation"])
    default = default_decision(run, item_id, result)

    with file_lock(_path(run)):
        data, read_error = _read(run)
        if read_error:
            raise GuiError(read_error)
        data = data if data is not None else {"version": VERSION, "items": {}}
        items = dict(data.get("items") or {})
        before = _clean(items[item_id]) if isinstance(items.get(item_id), dict) else {}
        if edit is None:
            kept = before.get("deviation")
        else:
            kept = None if edit == ai_deviation(result) else edit
        entry: dict[str, Any] = {"decision": decision, "edited": decision != default or kept is not None,
                                 "updated_at": now()}
        if kept is not None:
            entry["deviation"] = kept
        items[item_id] = entry
        data = {**data, "version": data.get("version", VERSION), "updated_at": now(), "items": items}
        write_json(_path(run), data)

    out: dict[str, Any] = {"ok": True, "item": item_id, **entry}
    warnings = []
    applied = item.get("applied")
    if applied and (before.get("decision"), before.get("deviation")) != (entry["decision"], entry.get("deviation")):
        parts = "、".join(label for part, label in APPLIED_JA.items() if isinstance(applied, dict) and part in applied)
        warnings.append(f"{item_id} はもう反映しています{f'（{parts}）' if parts else ''}。承認を変えても、反映したものは"
                        "元に戻りません。まだ反映していない部分は、次に反映するときに反映します")
    comment = (kept or {}).get("comment", "")
    if len(comment) > COMMENT_MAX:
        warnings.append(f"逸脱コメントが {len(comment)} 文字あります。Coverity に書き込む前に、{COMMENT_MAX} 文字以内に縮めるのがおすすめです")
    if warnings:
        out["warning"] = "\n".join(warnings)
    return out
