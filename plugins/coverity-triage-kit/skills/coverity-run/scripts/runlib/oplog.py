"""The run's log ``operations.log``: one JSON object per line, credential values masked as ****.

Fields: ``time``, ``event``, ``level`` (info / warning / critical), ``item`` (or null), ``message``
(one line of plain Japanese, shown as the recent activity) and the event's own fields.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_FILE = "operations.log"


def mask(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "****")
        return value
    if isinstance(value, dict):
        return {k: mask(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(v, secrets) for v in value]
    return value


def log(run_dir: str | Path, event: str, message: str, *, level: str = "info", item: str | None = None,
        secrets: list[str] | None = None, **fields: Any) -> None:
    """Append one line. Logging never stops a command."""
    record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, "level": level,
              "item": item, "message": message, **fields}
    try:
        line = json.dumps(mask(record, secrets or []), ensure_ascii=False, default=str)
        with open(Path(run_dir) / LOG_FILE, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read(run_dir: str | Path) -> list[dict[str, Any]]:
    """The records, oldest first. A line that is not a JSON object is skipped (being written, or damaged)."""
    try:
        text = (Path(run_dir) / LOG_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("event"), str):
            records.append(record)
    return records


def recent(run_dir: str | Path, limit: int = 10) -> list[str]:
    """The newest lines as "HH:MM:SS message", newest first."""
    lines = []
    for record in reversed(read(run_dir)[-limit:]):
        clock = str(record.get("time") or "")[11:19] or "--:--:--"
        text = str(record.get("message") or record.get("event"))
        lines.append(f"{clock} {text}")
    return lines
