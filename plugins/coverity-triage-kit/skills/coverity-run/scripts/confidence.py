# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""The confidence shown to people (人に見せる確信度): the lowest of the worker's confidence, the review's
cap and the build verification. result.json is never changed; this only lowers what is shown.

Usage:
    uv run --native-tls --quiet "<skill>/scripts/confidence.py" --stdin
        # stdin: {"result": {...result.json...}, "review": {...review.json...} | null, "verify": {...} | null}

"verify" is the entry of verify.json for the item ({"build_ok": true|false|null, "problems": [...], ...}).
Output: {"ok": true, "confidence": "high|medium|low", "reason": "...", "lowered_by": ["...", ...]}
(+ "error" when ok is false; exit 1). Nothing is written. Never a traceback.

The rule (function effective_confidence) is the shared contract "confidence"; the test data is
assets/contract/confidence/cases.json. This file uses only the standard library and reads no other
file, so other parts copy it byte for byte and import it.
"""

from __future__ import annotations

import argparse
import codecs
import json
import locale
import os
import sys

# --- confidence rule (contract: confidence) -----------------------------------------------------
ORDER = {"low": 0, "medium": 1, "high": 2}
JA = {"high": "高", "medium": "中", "low": "低"}


def lowest(*levels: str | None) -> str:
    """The lowest of the given levels; values that are not high / medium / low count as low."""
    known = [level if level in ORDER else "low" for level in levels]
    return min(known, key=ORDER.get) if known else "low"


def review_cap(review) -> str | None:
    """The cap a review puts on the confidence: its confidence_cap; a flag or rework without a cap is low."""
    if not isinstance(review, dict):
        return None
    cap = review.get("confidence_cap")
    if cap in ORDER:
        return cap
    if review.get("outcome") in ("flag", "rework"):
        return "low"
    return None


def verify_failed(verify) -> bool:
    """Whether the build verification found problems in the fix (a failed build, warnings left, new warnings)."""
    if not isinstance(verify, dict):
        return False
    problems = verify.get("problems")
    return verify.get("build_ok") is False or (isinstance(problems, list) and len(problems) > 0)


def effective_confidence(result, review=None, verify=None) -> dict:
    """Return {"confidence", "reason", "lowered_by"} for one item.

    - the worker's confidence (result.confidence; a missing or unknown value is low),
    - lowered to the review's cap (review.confidence_cap; flag or rework without a cap is low),
    - lowered to low when the recommendation is fix and the build verification found problems
      (a deviation recommendation keeps its confidence: its fix is only the other proposal).
    reason is result.confidence_reason, followed by why it was lowered in （…） (only why, when there is no reason).
    """
    result = result if isinstance(result, dict) else {}
    worker = result.get("confidence")
    level = worker if worker in ORDER else "low"
    lowered_by: list[str] = []
    if worker not in ORDER:
        lowered_by.append("作業役の確信度がないため「低」")
    cap = review_cap(review)
    if cap is not None and ORDER[cap] < ORDER[level]:
        level = cap
        if review.get("confidence_cap") in ORDER:
            note = str(review.get("note") or "").strip() or "要確認"
            lowered_by.append(f"結果の確かめで「{JA[cap]}」まで（{note}）")
        else:
            lowered_by.append("結果の確かめが要確認か差し戻しで、確信度の上限が書かれていないため「低」")
    if result.get("recommendation") == "fix" and verify_failed(verify) and level != "low":
        level = "low"
        raw = verify.get("problems")
        problems = [str(p) for p in raw if str(p).strip()] if isinstance(raw, list) else []
        lowered_by.append("ビルドでの検証で問題: " + ("、".join(problems) if problems else "ビルドに失敗しました"))
    reason = str(result.get("confidence_reason") or "").strip()
    if lowered_by:
        added = "。".join(lowered_by)
        reason = f"{reason}（{added}）" if reason else added
    return {"confidence": level, "reason": reason, "lowered_by": lowered_by}
# --- end of confidence rule ------------------------------------------------------------------------


def _json_escape(exc: UnicodeError):
    """codecs error handler: write characters the output encoding lacks as JSON \\uXXXX escapes."""
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    parts = []
    for ch in exc.object[exc.start:exc.end]:
        cp = ord(ch)
        if cp > 0xFFFF:
            cp -= 0x10000
            parts.append("\\u%04x\\u%04x" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)))
        else:
            parts.append("\\u%04x" % cp)
    return "".join(parts), exc.end


codecs.register_error("json_u_escape", _json_escape)


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _windows_code_page(kind: str) -> str | None:
    try:
        import ctypes

        cp = getattr(ctypes.windll.kernel32, kind)()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no console or not Windows
        return None
    if not cp:
        return None
    return "utf-8" if cp == 65001 else f"cp{cp}"


def _known(encoding: str | None) -> str | None:
    if not encoding:
        return None
    try:
        return codecs.lookup(encoding).name
    except LookupError:
        return None


def _env_encoding() -> str | None:
    return _known(os.environ.get("PYTHONIOENCODING", "").split(":")[0].strip())


def output_encoding() -> str:
    """PYTHONIOENCODING, else the console code page on Windows when stdout is piped, else UTF-8."""
    encoding = _env_encoding()
    if encoding:
        return encoding
    if os.name == "nt" and not _isatty(sys.stdout):
        encoding = _known(_windows_code_page("GetConsoleOutputCP"))
        if encoding:
            return encoding
    return "utf-8"


def emit(obj: dict) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode(output_encoding(), errors="json_u_escape")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(data.decode("ascii", errors="replace"))
    else:
        buffer.write(data)
    sys.stdout.flush()


def read_stdin_text() -> str:
    data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
    candidates = [_env_encoding(), "utf-8-sig"]
    if os.name == "nt":
        candidates.append(_known(_windows_code_page("GetConsoleCP")))
    for encoding in [c for c in candidates if c]:
        try:
            return data.decode(encoding).removeprefix("﻿")
        except UnicodeDecodeError:
            continue
    return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace").removeprefix("﻿")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # report argument errors as JSON, not as usage text
        raise ValueError(f"引数が正しくありません: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="人に見せる確信度（作業役・確かめ・ビルドでの検証のいちばん低いもの）を出す。結果は JSON。")
    parser.add_argument("--stdin", action="store_true",
                        help='標準入力から {"result": {...}, "review": {...} か null, "verify": {...} か null} を読む')
    try:
        args = parser.parse_args(argv)
        if not args.stdin:
            raise ValueError('入力を --stdin で渡してください（{"result": {...}, "review": ..., "verify": ...}）')
        try:
            payload = json.loads(read_stdin_text())
        except ValueError as exc:
            raise ValueError(f"標準入力の JSON を読めません: {exc}") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise ValueError('標準入力には {"result": {...}, "review": ..., "verify": ...} の形の JSON を渡してください')
        out = {"ok": True, **effective_confidence(payload["result"], payload.get("review"), payload.get("verify"))}
    except ValueError as exc:
        out = {"ok": False, "error": str(exc), "problems": [{"code": "bad_input", "message": str(exc)}]}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        message = f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}"
        out = {"ok": False, "error": message, "problems": [{"code": "unexpected", "message": message}]}
    emit(out)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
