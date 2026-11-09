# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.18,<5"]
# ///
"""Check a triage result (result.json) and turn it into a Japanese report for the chat.

Usage:
    uv run --native-tls --quiet "<skill>/scripts/check_result.py" check --result "<result.json>" [--item ID]
        [--cids 20001,20002] [--impact High|Medium|Low|Unspecified]
    uv run --native-tls --quiet "<skill>/scripts/check_result.py" check --stdin
        # stdin: {"result": {...}, "item": "...", "cids": [...], "impact": "..."}
    uv run --native-tls --quiet "<skill>/scripts/check_result.py" render --result "<result.json>"

check:  {"ok", "item", "problems": [{"code", "message", "path"?}], "warnings": [...]} (+ "error"); exit 1 when not ok.
render: {"ok": true, "line", "text", "valid", "problems"}.
Nothing is written to disk. The extra rules are the shared contract "result"
(assets/contract/result/cases.json); the comment rules are imported from check_comment.py
(a byte copy of the contract "comment" in this folder).
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

SCRIPTS_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = SCRIPTS_DIR.parent / "assets" / "result.schema.json"

# --- comment rules (contract: comment) -------------------------------------------------------
# check_comment.py next to this script is a byte copy of the shared comment rules; use it, never rewrite it.
# Only this script's own folder goes on sys.path, and the import leaves no __pycache__ in the skill folder.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
_dont_write_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    from check_comment import CATEGORIES, check_comment  # noqa: E402
except Exception as exc:  # noqa: BLE001 - an incomplete folder: one JSON object, never a traceback
    if __name__ != "__main__":
        raise
    _message = ("このスクリプトと同じフォルダの check_comment.py（逸脱コメントの決まり）を読み込めません。"
                f"スキルのフォルダを、まるごと置き直してください（{type(exc).__name__}: {exc}）")
    sys.stdout.write(json.dumps({"ok": False, "error": _message,
                                 "problems": [{"code": "missing_comment_rules", "message": _message}]}) + "\n")
    sys.exit(1)
finally:
    sys.dont_write_bytecode = _dont_write_bytecode

# --- result rules (contract: result) --------------------------------------------------------
CATEGORY_OF_JUDGEMENT = {"false_positive": "誤検知。", "intentional": "意図的。", "true_bug": "本物の不具合。"}
DEVIATION_COMBOS = {  # category of the comment -> (Classification, Action, allowed Severity)
    "誤検知。": ("False Positive", "Ignore", ("Unspecified",)),
    "意図的。": ("Intentional", "Ignore", ("Unspecified",)),
    "本物の不具合。": ("Bug", "Ignore", ("Major", "Moderate", "Minor")),
}
SEVERITY_OF_IMPACT = {"High": "Major", "Medium": "Moderate", "Low": "Minor"}
FIX_SEVERITIES = ("Major", "Moderate", "Minor")
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
        if expected == "\\S":
            return "空や空白だけにできません"
        return "前後に空白や改行を入れません"
    if kind == "minLength":
        return "空にできません"
    if kind == "maxLength":
        return f"{expected} 文字以内にします（今は {len(instance)} 文字）"
    if kind == "minimum":
        return f"{expected} 以上にします（今は {_short(instance)}）"
    if kind == "minItems":
        return f"{expected} 個以上にします（今は {len(instance)} 個）"
    return error.message


def schema_problems(value, schema: dict) -> list[dict]:
    from jsonschema import Draft202012Validator  # imported here so the rules above need only the stdlib

    errors = sorted(Draft202012Validator(schema).iter_errors(value),
                    key=lambda e: (_json_path(e.absolute_path), str(e.validator)))
    return [{"code": "schema", "path": _json_path(e.absolute_path),
             "message": f"{_json_path(e.absolute_path)}: {_describe(e)}"} for e in errors]


def _is_int_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value)


def combo_warnings(result: dict, impact: str | None = None, prefix: str = "") -> list[dict]:
    """Classification / Action / Severity hints (warnings only; teams may use other values)."""
    warnings: list[dict] = []

    def warn(code: str, path: str, message: str) -> None:
        warnings.append({"code": code, "path": prefix + path, "message": message})

    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    deviation = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
    fix = result.get("fix") if isinstance(result.get("fix"), dict) else {}
    comment = deviation.get("comment") if isinstance(deviation.get("comment"), str) else ""
    category = next((c for c in CATEGORIES if comment.strip().startswith(c)), None)
    expected = CATEGORY_OF_JUDGEMENT.get(verdict.get("judgement"))
    if category and expected and category != expected:
        warn("comment_judgement", "deviation.comment",
             f"判定が {verdict.get('judgement')} なのに、逸脱コメントが「{category}」で始まっています（目安は「{expected}」）")
    if category:
        classification, action, severities = DEVIATION_COMBOS[category]
        now = (deviation.get("classification"), deviation.get("action"), deviation.get("severity", "Unspecified"))
        if now[0] != classification or now[1] != action or now[2] not in severities:
            warn("deviation_combo", "deviation",
                 f"「{category}」の逸脱案の目安は Classification {classification}・Action {action}・"
                 f"Severity {' / '.join(severities)} です（今は {now[0]}・{now[1]}・{now[2]}）")
    if fix:
        now = (fix.get("classification"), fix.get("action"), fix.get("severity", "Unspecified"))
        if now[0] != "Bug" or now[1] != "Fix Required":
            warn("fix_combo", "fix", f"修正案の目安は Classification Bug・Action Fix Required です（今は {now[0]}・{now[1]}）")
        wanted = SEVERITY_OF_IMPACT.get(impact or "")
        if wanted and now[2] != wanted:
            warn("fix_severity", "fix.severity", f"Impact {impact} の修正案の Severity の目安は {wanted} です（今は {now[2]}）")
        elif impact is None and now[2] not in FIX_SEVERITIES:
            warn("fix_severity", "fix.severity",
                 f"修正案の Severity は、警告の Impact に合わせて Major / Moderate / Minor から選びます（今は {now[2]}）")
    return warnings


def check_result(result, schema: dict, item: str | None = None, cids: list[int] | None = None,
                 impact: str | None = None) -> dict:
    """Schema + the extra rules of result.json. Returns {"ok", "item", "problems", "warnings"}."""
    problems = schema_problems(result, schema)
    warnings: list[dict] = []
    if not isinstance(result, dict):
        return {"ok": False, "item": None, "problems": problems, "warnings": warnings}
    deviation = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
    comment = deviation.get("comment")
    if isinstance(comment, str):  # the comment rules also cover the schema's length and spaces
        problems = [p for p in problems if p["path"] != "deviation.comment"]
        for p in check_comment(comment)["problems"]:
            problems.append({"code": "comment." + p["code"], "path": "deviation.comment",
                             "message": "deviation.comment: " + p["message"]})
    if item is not None and result.get("item") != item:
        problems.append({"code": "item_mismatch", "path": "item",
                         "message": f"item: 「{item}」にします（今は {_short(result.get('item'))}）"})
    excluded = result.get("group_excluded_cids", [])
    if _is_int_list(excluded):
        known = list(cids) if cids else None
        name = item if item is not None else result.get("item")
        if known is None and isinstance(name, str) and re.fullmatch(r"[0-9]+", name.strip()):
            known = [int(name)]
        if known is None:
            if excluded:
                warnings.append({"code": "group_unchecked", "path": "group_excluded_cids",
                                 "message": "グループの CID が分からないので、group_excluded_cids を確かめていません（--cids で渡せます）"})
        elif len(set(known)) <= 1:
            if excluded:
                problems.append({"code": "group_excluded_not_group", "path": "group_excluded_cids",
                                 "message": "group_excluded_cids: グループでない作業では空にします"})
        else:
            unknown = sorted(set(excluded) - set(known))
            if unknown:
                problems.append({"code": "group_excluded_unknown", "path": "group_excluded_cids",
                                 "message": f"group_excluded_cids: {', '.join(map(str, unknown))} はこのグループの CID ではありません"})
            if set(known) <= set(excluded):
                problems.append({"code": "group_excluded_all", "path": "group_excluded_cids",
                                 "message": "group_excluded_cids: グループの CID をすべて外すことはできません。"
                                            "外すのは、代表と理由が違う CID だけです"})
    warnings += combo_warnings(result, impact)
    return {"ok": not problems, "item": result.get("item"), "problems": problems, "warnings": warnings}
# --- end of result rules ---------------------------------------------------------------------

JUDGEMENT_JA = {"false_positive": "誤検知", "true_bug": "本物のバグ", "intentional": "意図的", "undetermined": "判定できない"}
RECOMMENDATION_JA = {"fix": "修正", "deviation": "逸脱"}
CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}
DRIFT_JA = {"none": "なし", "detected": "あり", "unknown": "不明"}
NONE = "なし"


def _text(value) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else NONE


def _label(value, names: dict) -> str:
    if isinstance(value, str) and value in names:
        return f"{names[value]}（{value}）"
    return _text(value)


def render(result: dict) -> tuple[str, str]:
    """Return (one line, report) in the order a person reads: conclusion first, then grounds and both proposals."""
    def part(key: str) -> dict:
        return result.get(key) if isinstance(result.get(key), dict) else {}

    verdict, deviation, fix, drift = part("verdict"), part("deviation"), part("fix"), part("revision_drift")
    rec, conf = result.get("recommendation"), result.get("confidence")
    line = (f"{_text(result.get('item'))}: {RECOMMENDATION_JA.get(rec, _text(rec))}を推奨"
            f"（確信度 {CONFIDENCE_JA.get(conf, _text(conf))}）— {_text(verdict.get('summary'))}")
    lines = [line, "",
             f"1. 見立て：{_text(verdict.get('summary'))}",
             f"   判定：{_label(verdict.get('judgement'), JUDGEMENT_JA)}",
             f"2. 推奨：{_label(rec, RECOMMENDATION_JA)}",
             f"   確信度：{_label(conf, CONFIDENCE_JA)} — 理由：{_text(result.get('confidence_reason'))}",
             f"3. 調べた経過：{_text(verdict.get('rationale'))}"]
    evidence = [e for e in verdict.get("evidence", []) if isinstance(e, dict)] if isinstance(verdict.get("evidence"), list) else []
    if evidence:
        lines.append("   根拠の場所：")
        for e in evidence:
            place = _text(e.get("file")) + (f":{e['line']}" if isinstance(e.get("line"), int) else "")
            note = e.get("note").strip() if isinstance(e.get("note"), str) else ""
            lines.append(f"   - {place}" + (f" — {note}" if note else ""))
    else:
        lines.append(f"   根拠の場所：{NONE}")
    lines += [f"4. 逸脱案：Classification {_text(deviation.get('classification'))}／Action {_text(deviation.get('action'))}"
              f"／Severity {_text(deviation.get('severity', 'Unspecified'))}",
              f"   逸脱コメント：{_text(deviation.get('comment'))}",
              f"5. 修正案：{_text(fix.get('summary'))}",
              f"   影響：{_text(fix.get('impact'))}"]
    exceeded = [c for c in fix.get("exceeded_constraints", []) if isinstance(c, str) and c.strip()] \
        if isinstance(fix.get("exceeded_constraints"), list) else []
    lines.append(f"   決まりを超えたこと：{'、'.join(exceeded) if exceeded else NONE}")
    if fix:
        lines.append("   最新のリビジョン：" + ("もう直っている（修正は作っていない）" if fix.get("already_fixed_on_latest") is True
                                         else "まだ直っていない"))
        lines.append(f"   Classification {_text(fix.get('classification'))}／Action {_text(fix.get('action'))}"
                     f"／Severity {_text(fix.get('severity', 'Unspecified'))}")
    detail = drift.get("detail").strip() if isinstance(drift.get("detail"), str) else ""
    lines.append(f"6. リビジョンのずれ：{_label(drift.get('status'), DRIFT_JA)}" + (f" — {detail}" if detail else ""))
    excluded = result.get("group_excluded_cids")
    lines.append(f"   外した CID：{', '.join(map(str, excluded)) if _is_int_list(excluded) and excluded else NONE}")
    return line, "\n".join(lines)


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


def _read_json_file(path_text: str):
    path = Path(path_text)
    if not path.is_file():
        raise ValueError(f"ファイルがありません: {path}")
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} を UTF-8 として読めません: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} を JSON として読めません（{exc.lineno} 行 {exc.colno} 文字目: {exc.msg}）") from exc


def _read_stdin_json() -> dict:
    try:
        value = json.loads(read_stdin_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"標準入力を JSON として読めません（{exc.lineno} 行 {exc.colno} 文字目: {exc.msg}）") from exc
    if not isinstance(value, dict) or "result" not in value:
        raise ValueError('標準入力には {"result": {...}} の形の JSON を渡してください')
    return value


def _parse_cids(values: list[str] | None) -> list[int] | None:
    if not values:
        return None
    cids = []
    for value in values:
        for piece in re.split(r"[,\s]+", value.strip()):
            if not piece:
                continue
            if not re.fullmatch(r"[0-9]+", piece):
                raise ValueError(f"--cids は CID の数をカンマで区切って渡します（今は {value}）")
            cids.append(int(piece))
    return cids


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="結果（result.json）の形を確かめる・文にする。結果は JSON。")
    commands = parser.add_subparsers(dest="command", parser_class=_Parser)
    for name, help_text in (("check", "形と決まりを確かめる"), ("render", "チャット向けの文にする")):
        sub = commands.add_parser(name, help=help_text)
        source = sub.add_mutually_exclusive_group()
        source.add_argument("--result", help="result.json のパス")
        source.add_argument("--stdin", action="store_true", help='標準入力から {"result": {...}, ...} を読む')
        if name == "check":
            sub.add_argument("--item", help="作業の ID（CID か G1 など）。result.json の item と同じか確かめる")
            sub.add_argument("--cids", action="append", help="作業の CID（グループならカンマで区切る）")
            sub.add_argument("--impact", choices=["High", "Medium", "Low", "Unspecified"], help="警告の Impact")
    try:
        args = parser.parse_args(argv)
        if args.command is None:
            raise ValueError("コマンド（check か render）を付けてください")
        if args.result:
            payload = {"result": _read_json_file(args.result)}
        elif args.stdin:
            payload = _read_stdin_json()
        else:
            raise ValueError("結果を --result <ファイル> か --stdin で渡してください")
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        result = payload["result"]
        if args.command == "render":
            if not isinstance(result, dict):
                raise ValueError("結果が JSON のオブジェクト（{...}）ではありません")
            checked = check_result(result, schema)
            line, text = render(result)
            out = {"ok": True, "line": line, "text": text, "valid": checked["ok"], "problems": checked["problems"]}
        else:
            item = args.item if args.item is not None else payload.get("item")
            cids = _parse_cids(args.cids) if args.cids else payload.get("cids")
            if cids is not None and not _is_int_list(cids):
                raise ValueError("cids は CID の数の配列にします")
            impact = args.impact if args.impact else payload.get("impact")
            out = check_result(result, schema, item=item, cids=cids, impact=impact)
            if not out["ok"]:
                out = {"ok": False, "error": "結果に直すところがあります", **out}
    except ValueError as exc:
        out = {"ok": False, "error": str(exc), "problems": [{"code": "bad_input", "message": str(exc)}]}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        message = f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}"
        out = {"ok": False, "error": message, "problems": [{"code": "unexpected", "message": message}]}
    emit(out)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
