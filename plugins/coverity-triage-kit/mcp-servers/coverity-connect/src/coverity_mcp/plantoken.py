"""The confirmation string of a write-back (standard library only, so the file can be copied as it is).

* From a plan file: the first 12 hex characters of SHA-256 of the file's bytes, exactly as they are on
  disk (no decoding, no change of line breaks).
* From entries given without a file: SHA-256 of the UTF-8 of ``{"triage": [entry, ...]}`` written as JSON
  with ``sort_keys``, separators ``(",", ":")`` and ``ensure_ascii=False``, first 12 hex characters.
  Each entry is normalized first: only ``id``, ``cids``, ``stream``, ``classification``, ``action``,
  ``severity`` and ``comment`` are kept; ``stream`` is left out when it is missing or null; ``severity``
  and ``comment`` are ``""`` when missing.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

LENGTH = 12
FIELDS = ("id", "cids", "stream", "classification", "action", "severity", "comment")


def token_for_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:LENGTH]


def normalize(entry: dict[str, Any]) -> dict[str, Any]:
    out = {key: entry[key] for key in FIELDS if key in entry and entry[key] is not None}
    out.setdefault("severity", "")
    out.setdefault("comment", "")
    return out


def token_for_entries(entries: list[dict[str, Any]]) -> str:
    text = json.dumps({"triage": [normalize(e) for e in entries]}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return token_for_bytes(text.encode("utf-8"))


def plan_bytes(plan: dict[str, Any]) -> bytes:
    """How a plan file is written: UTF-8 JSON with an indent of 2, LF line breaks, a final line break."""
    return (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
