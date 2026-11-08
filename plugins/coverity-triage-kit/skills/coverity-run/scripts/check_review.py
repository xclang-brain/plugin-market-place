# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.18,<5"]
# ///
"""Check a review (review.json): the schema, one finding for every item criterion, and the outcome rules.

Usage:
    uv run --native-tls --quiet "<skill>/scripts/check_review.py" --review "<review.json>" [--item ID]
        [--checker "<checker name>"] [--merged]
    uv run --native-tls --quiet "<skill>/scripts/check_review.py" --stdin
        # stdin: {"review": {...}, "item": "...", "checker": "...", "merged": true|false}

Output: {"ok", "item", "outcome", "missing": [ids], "problems": [{"code", "message", "path"?}], "warnings": [...]}
(+ "error"); exit 1 when not ok. Nothing is written. The criteria come from assets/criteria.json.
"""

from __future__ import annotations

import argparse
import codecs
import json
import locale
import os
import re
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SCHEMA_FILE = ASSETS / "review.schema.json"
CRITERIA_FILE = ASSETS / "criteria.json"
NOT_APPLICABLE = "対象外"
_TYPE_JA = {"string": "文字列", "integer": "整数", "number": "数", "boolean": "true か false",
            "array": "配列（[...]）", "object": "オブジェクト（{...}）", "null": "null"}


def _json_path(parts) -> str:
    text = ""
    for part in parts:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text or "（全体）"


def _short(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 40 else text[:37] + "..."


def _describe(error) -> str:
    kind, expected, instance = error.validator, error.validator_value, error.instance
    if kind == "required":
        missing = [k for k in expected if isinstance(instance, dict) and k not in instance]
        return f"{'、'.join(missing)} がありません"
    if kind == "additionalProperties":
        allowed = set(error.schema.get("properties", {}))
        extra = sorted(k for k in instance if k not in allowed) if isinstance(instance, dict) else []
        return f"決まった項目のほかの {'、'.join(extra)} は書けません"
    if kind == "enum":
        return f"{' / '.join(_short(v) for v in expected)} のどれかにします（今は {_short(instance)}）"
    if kind == "type":
        names = expected if isinstance(expected, list) else [expected]
        return f"{' か '.join(_TYPE_JA.get(n, n) for n in names)}にします（今は {_short(instance)}）"
    if kind == "pattern":
        return "空や空白だけにできません"
    if kind == "minItems":
        return f"{expected} 個以上にします（今は {len(instance)} 個）"
    return error.message


def schema_problems(value, schema: dict) -> list[dict]:
    from jsonschema import Draft202012Validator  # imported here so the rest needs only the stdlib

    errors = sorted(Draft202012Validator(schema).iter_errors(value),
                    key=lambda e: (_json_path(e.absolute_path), str(e.validator)))
    problems = []
    for e in errors:
        path = _json_path(e.absolute_path)
        if e.validator == "minItems" and path == "rework":
            message = "rework: 差し戻しのときは、直すことを1点ずつ書きます"
        else:
            message = f"{path}: {_describe(e)}"
        problems.append({"code": "schema", "path": path, "message": message})
    return problems


def checker_kind(checker: str) -> str:
    name = checker.strip().upper()
    if name.startswith("MISRA"):
        return "misra"
    if name.startswith("CERT"):
        return "cert"
    return "standard"


def applicability(criteria: list[dict], checker: str | None, merged: bool | None) -> dict[str, bool | None]:
    """id -> True (applies) / False (does not) / None (cannot tell) for the scope:item criteria."""
    kind = checker_kind(checker) if checker else None
    result: dict[str, bool | None] = {}
    for criterion in criteria:
        when = criterion.get("applies_when") or ""
        if not when:
            result[criterion["id"]] = True
        elif "統合" in when:
            result[criterion["id"]] = merged
        elif "MISRA" in when:
            result[criterion["id"]] = None if kind is None else kind == "misra"
        elif "CERT" in when:
            result[criterion["id"]] = None if kind is None else kind == "cert"
        elif "標準" in when:
            result[criterion["id"]] = None if kind is None else kind == "standard"
        else:
            result[criterion["id"]] = None
    return result


def check_review(review, schema: dict, criteria_doc: dict, item: str | None = None, checker: str | None = None,
                 merged: bool | None = None) -> dict:
    problems = schema_problems(review, schema)
    warnings: list[dict] = []
    criteria = [c for c in criteria_doc.get("criteria", []) if c.get("scope") == "item"]
    ids = [c["id"] for c in criteria]
    if not isinstance(review, dict):
        return {"ok": False, "item": None, "outcome": None, "missing": ids, "problems": problems, "warnings": warnings}
    if item is not None and review.get("item") != item:
        problems.append({"code": "item_mismatch", "path": "item",
                         "message": f"item: 「{item}」にします（今は {_short(review.get('item'))}）"})
    applies = applicability(criteria, checker, merged)
    findings = review.get("findings") if isinstance(review.get("findings"), list) else []
    seen: dict[str, int] = {}
    failed = False
    for n, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        check, ok, detail = finding.get("check"), finding.get("ok"), finding.get("detail")
        path = f"findings[{n}]"
        failed = failed or ok is False
        if check not in ids:
            problems.append({"code": "unknown_check", "path": path + ".check",
                             "message": f"{path}.check: 評価基準の id（{ids[0]}〜{ids[-1]} の scope が item のもの）"
                                        f"を書きます（今は {_short(check)}）"})
            continue
        seen[check] = seen.get(check, 0) + 1
        if seen[check] == 2:
            problems.append({"code": "duplicate_check", "path": path + ".check",
                             "message": f"{path}.check: {check} が2回以上あります。1つの基準は1回だけ書きます"})
        detail_text = detail if isinstance(detail, str) else ""
        if applies.get(check) is False:
            if ok is False:
                problems.append({"code": "not_applicable_failed", "path": path,
                                 "message": f"{path}: {check} はこの警告に当てはまらないので、ok を true にし、"
                                            f"detail に「{NOT_APPLICABLE}」と書きます"})
            elif NOT_APPLICABLE not in detail_text:
                warnings.append({"code": "not_applicable_detail", "path": path + ".detail",
                                 "message": f"{path}.detail: {check} は当てはまらないので、「{NOT_APPLICABLE}」と書きます"})
        elif applies.get(check) is True and detail_text.strip().startswith(NOT_APPLICABLE):
            problems.append({"code": "applicable_marked_not_applicable", "path": path + ".detail",
                             "message": f"{path}.detail: {check} はこの警告に当てはまります。確かめた結果を書きます"})
    missing = [i for i in ids if i not in seen]
    if missing:
        problems.append({"code": "missing_check", "path": "findings",
                         "message": f"findings: {', '.join(missing)} がありません。scope が item の評価基準を、"
                                    f"すべて1回ずつ書きます（当てはまらないものは ok: true、detail に「{NOT_APPLICABLE}」）"})
    outcome = review.get("outcome")
    rework = review.get("rework") if isinstance(review.get("rework"), list) else []
    if outcome == "flag" and review.get("confidence_cap") is None:
        problems.append({"code": "flag_without_cap", "path": "confidence_cap",
                         "message": "confidence_cap: 要確認（flag）のときは、妥当と考える確信度の上限（high / medium / low）を書きます"})
    if outcome in ("pass", "flag") and rework:
        problems.append({"code": "rework_not_empty", "path": "rework",
                         "message": "rework: 差し戻し（rework）でないときは、空の配列にします"})
    if outcome == "pass" and failed:
        problems.append({"code": "pass_with_failed_check", "path": "outcome",
                         "message": "outcome: ok が false の基準があるのに合格（pass）です。要確認か差し戻しにするか、"
                                    "直さなくてよい点なら ok を true にして detail に書きます"})
    if outcome == "rework" and findings and not failed:
        problems.append({"code": "rework_without_failed_check", "path": "outcome",
                         "message": "outcome: 差し戻し（rework）なのに、ok が false の基準がありません。"
                                    "差し戻す理由になった基準を ok: false にします"})
    return {"ok": not problems, "item": review.get("item"), "outcome": outcome, "missing": missing,
            "problems": problems, "warnings": warnings}


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
            return data.decode(encoding).removeprefix("\ufeff")
        except UnicodeDecodeError:
            continue
    return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace").removeprefix("\ufeff")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # report argument errors as JSON, not as usage text
        raise ValueError(f"引数が正しくありません: {message}")


def _read_json(text: str, where: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where} を JSON として読めません（{exc.lineno} 行 {exc.colno} 文字目: {exc.msg}）") from exc


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="確かめの結果（review.json）の形と決まりを確かめる。結果は JSON。")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--review", help="review.json のパス")
    source.add_argument("--stdin", action="store_true", help='標準入力から {"review": {...}, ...} を読む')
    parser.add_argument("--item", help="作業の ID。review.json の item と同じか確かめる")
    parser.add_argument("--checker", help="警告のチェッカーの名前（当てはまらない基準を見分ける）")
    parser.add_argument("--merged", action="store_true", default=None, help="統合した作業のとき")
    try:
        args = parser.parse_args(argv)
        payload: dict = {}
        if args.review:
            path = Path(args.review)
            if not path.is_file():
                raise ValueError(f"ファイルがありません: {path}")
            payload["review"] = _read_json(path.read_bytes().decode("utf-8-sig", errors="replace"), str(path))
        elif args.stdin:
            payload = _read_json(read_stdin_text(), "標準入力")
            if not isinstance(payload, dict) or "review" not in payload:
                raise ValueError('標準入力には {"review": {...}} の形の JSON を渡してください')
        else:
            raise ValueError("確かめの結果を --review <ファイル> か --stdin で渡してください")
        item = args.item if args.item is not None else payload.get("item")
        checker = args.checker if args.checker else payload.get("checker")
        merged = args.merged if args.merged is not None else payload.get("merged")
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        criteria = json.loads(CRITERIA_FILE.read_text(encoding="utf-8"))
        out = check_review(payload["review"], schema, criteria, item=item, checker=checker, merged=merged)
        if not out["ok"]:
            out = {"ok": False, "error": "確かめの結果に直すところがあります", **out}
    except ValueError as exc:
        out = {"ok": False, "error": str(exc), "problems": [{"code": "bad_input", "message": str(exc)}]}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        message = f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}"
        out = {"ok": False, "error": message, "problems": [{"code": "unexpected", "message": message}]}
    emit(out)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
