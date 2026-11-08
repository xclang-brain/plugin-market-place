# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.18,<5"]
# ///
"""Check an integration plan (integration/plan.json): the schema and the merge rules.

Usage:
    uv run --native-tls --quiet "<skill>/scripts/check_plan.py" --plan "<plan.json>"
        [--member 20011:20011:high --member G1:30001,30002:medium ...] [--no-grouping 30002 ...]
    uv run --native-tls --quiet "<skill>/scripts/check_plan.py" --plan "<plan.json>" --items "<items.json>"
    uv run --native-tls --quiet "<skill>/scripts/check_plan.py" --stdin     # stdin: {"plan": {...}, "items": {...}}

items: {"<id>": {"cids": [int], "status": "done", "confidence": "high|medium|low", "no_grouping": bool}}
Output: {"ok", "merges", "conflicts", "problems": [{"code", "message", "path"?}], "warnings": [...]} (+ "error");
exit 1 when not ok. Nothing is written. The rules are the shared contract "plan" (assets/contract/plan/cases.json);
the comment rules are a copy of the contract "comment".
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

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "assets" / "plan.schema.json"

# --- comment rules (contract: comment) -------------------------------------------------------
COMMENT_MAX = 200  # characters (Unicode code points) after strip; same limit as result.schema.json
CATEGORIES = ("誤検知。", "意図的。", "本物の不具合。")
# Conditions and caveats: the comment must say the warning is harmless in every situation.
CONDITION_WORDS = ("ただし", "但し", "場合に限", "場合のみ", "に限り", "に限って", "に限る",
                   "た場合は", "た場合には", "た場合のみ", "に対して", "を前提", "仮に")
# Guesses: the comment states facts read in the code, not likelihoods.
HEDGE_WORDS = ("おそらく", "恐らく", "たぶん", "多分", "かもしれ", "と思われ", "と考えられ")
# How it was investigated, or what could not be checked (belongs to the rationale).
PROCESS_WORDS = ("分からなかった", "確認できなかった", "確かめられなかった", "追えなかった", "追いきれ",
                 "調べきれ", "再解析", "解析したリビジョン")
# Requests to people and recommendations.
REQUEST_WORDS = ("ください", "下さい", "お願い", "てほしい", "て欲しい", "要確認", "確認が必要",
                 "確認する必要", "すべき", "べきである", "が望ましい")
# 「推奨」 only as a recommendation; 「推奨値」「推奨範囲」 (a noun follows) are fine.
RECOMMEND_PATTERNS = (("を推奨", re.compile(r"を推奨(?![々一-鿿゠-ヿ])")),
                      ("推奨します", re.compile(r"推奨します")),
                      ("推奨する", re.compile(r"推奨する")))
_OPENERS = "（(「『【［[｛{〔"
_CLOSERS = "）)」』】］]｝}〕"
_SENTENCE_ENDS = "。！？"
_LIST_LINE = re.compile(r"^\s*(?:#|[-*+>・■●◆□○]|\d+[.)．]\s)")
_BOLD = re.compile(r"\*\*[^*\s](?:[^*]*[^*\s])?\*\*")
_LINK = re.compile(r"\[[^\]\n]+\]\([^)\s]+\)")


def _count_sentences(body: str) -> int:
    """Sentences end with 。！？ outside brackets and quotes; a last sentence may lack the end mark."""
    depth, count, has_text = 0, 0, False
    for ch in body:
        if ch in _OPENERS:
            depth += 1
            has_text = True
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
            has_text = True
        elif ch in _SENTENCE_ENDS and depth == 0:
            if has_text:
                count += 1
            has_text = False
        elif not ch.isspace():
            has_text = True
    return count + (1 if has_text else 0)


def _has_markup(text: str) -> bool:
    if "`" in text or _BOLD.search(text) or _LINK.search(text):
        return True
    return any(_LIST_LINE.match(line) for line in text.splitlines())


def check_comment(comment: str) -> dict:
    """Return {"ok", "length", "problems": [{"code", "message"}]} for one deviation comment."""
    problems: list[dict] = []

    def add(code: str, message: str) -> None:
        problems.append({"code": code, "message": message})

    text = comment.strip()
    length = len(text)
    if not text:
        add("empty", "逸脱コメントが空です。判定区分・問題にならない理由・根拠の場所を1〜2文で書きます")
        return {"ok": False, "length": 0, "problems": problems}
    if text != comment:
        add("surrounding_whitespace", "前後の空白や改行を消してください")
    if length > COMMENT_MAX:
        add("too_long", f"{COMMENT_MAX}文字以内にしてください（今は {length} 文字）。"
                        "問題にならない理由と根拠の場所だけを残します")
    category = next((c for c in CATEGORIES if text.startswith(c)), None)
    if category is None:
        add("no_category", "「誤検知。」「意図的。」「本物の不具合。」のどれかで始めてください")
    body = text[len(category):] if category else text
    sentences = _count_sentences(body)
    if category and sentences == 0:
        add("no_reason", "判定区分のあとに、問題にならない理由と根拠の場所（ファイルと行）を書いてください")
    elif sentences > 2:
        add("too_many_sentences", f"判定区分のあとは1〜2文にしてください（今は {sentences} 文）")
    if len(text.splitlines()) > 1:
        add("line_break", "改行を入れず、続けて書いてください")
    if _has_markup(text):
        add("markdown", "見出し・箇条書き・` や ** などの書式を使わず、ふつうの文で書いてください")
    found = [w for w in CONDITION_WORDS + HEDGE_WORDS if w in text]
    if found:
        add("condition_words", "条件・但し書き・推量の言い方（%s）があります。どんな状況でも問題にならないことを、"
                               "条件を付けずに言い切ります。言い切れないときは、推奨を修正にします"
            % "、".join(f"「{w}」" for w in found))
    found = [w for w in PROCESS_WORDS if w in text]
    if found:
        add("process_words", "調べた過程や、調べきれなかったことの言い方（%s）があります。それは結果の根拠の説明に書き、"
                             "逸脱コメントには問題にならない理由だけを書きます" % "、".join(f"「{w}」" for w in found))
    found = [w for w, pattern in RECOMMEND_PATTERNS if pattern.search(text)]
    found += [w for w in REQUEST_WORDS if w in text]
    if found:
        add("request_words", "推奨や人への依頼の言い方（%s）があります。逸脱コメントには、問題にならない理由だけを書きます"
            % "、".join(f"「{w}」" for w in found))
    return {"ok": not problems, "length": length, "problems": problems}
# --- end of comment rules --------------------------------------------------------------------

# --- plan rules (contract: plan) ------------------------------------------------------------
MAX_CIDS = 10  # CIDs in one merge, the same limit as a group
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
CATEGORY_OF_JUDGEMENT = {"false_positive": "誤検知。", "intentional": "意図的。", "true_bug": "本物の不具合。"}
DEVIATION_COMBOS = {  # category of the comment -> (Classification, Action, allowed Severity)
    "誤検知。": ("False Positive", "Ignore", ("Unspecified",)),
    "意図的。": ("Intentional", "Ignore", ("Unspecified",)),
    "本物の不具合。": ("Bug", "Ignore", ("Major", "Moderate", "Minor")),
}
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
        if "item:" in str(expected):
            return f"item:<まとめる作業の ID>、combine、rework のどれかにします（今は {_short(instance)}）"
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


def combo_warnings(result: dict, prefix: str) -> list[dict]:
    """Classification / Action hints for the merged result (warnings only)."""
    warnings: list[dict] = []
    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    deviation = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
    fix = result.get("fix") if isinstance(result.get("fix"), dict) else {}
    comment = deviation.get("comment") if isinstance(deviation.get("comment"), str) else ""
    category = next((c for c in CATEGORIES if comment.strip().startswith(c)), None)
    expected = CATEGORY_OF_JUDGEMENT.get(verdict.get("judgement"))
    if category and expected and category != expected:
        warnings.append({"code": "comment_judgement", "path": prefix + "deviation.comment",
                         "message": f"判定が {verdict.get('judgement')} なのに、逸脱コメントが「{category}」で始まっています（目安は「{expected}」）"})
    if category:
        classification, action, severities = DEVIATION_COMBOS[category]
        now = (deviation.get("classification"), deviation.get("action"), deviation.get("severity", "Unspecified"))
        if now[0] != classification or now[1] != action or now[2] not in severities:
            warnings.append({"code": "deviation_combo", "path": prefix + "deviation",
                             "message": f"「{category}」の逸脱案の目安は Classification {classification}・Action {action}・"
                                        f"Severity {' / '.join(severities)} です（今は {now[0]}・{now[1]}・{now[2]}）"})
    if fix and (fix.get("classification") != "Bug" or fix.get("action") != "Fix Required"):
        warnings.append({"code": "fix_combo", "path": prefix + "fix",
                         "message": f"修正案の目安は Classification Bug・Action Fix Required です"
                                    f"（今は {fix.get('classification')}・{fix.get('action')}）"})
    return warnings


def _strings(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def check_plan(plan, schema: dict, items: dict | None) -> dict:
    """Schema + merge rules. items describes the candidate work items; None skips the checks that need them."""
    problems = schema_problems(plan, schema)
    warnings: list[dict] = []
    if not isinstance(plan, dict):
        return {"ok": False, "merges": 0, "conflicts": 0, "problems": problems, "warnings": warnings}

    def add(code: str, path: str, message: str) -> None:
        problems.append({"code": code, "path": path, "message": f"{path}: {message}"})

    merges = plan.get("merges") if isinstance(plan.get("merges"), list) else []
    conflicts = plan.get("conflicts") if isinstance(plan.get("conflicts"), list) else []
    used: dict[str, int] = {}
    for n, merge in enumerate(merges):
        if not isinstance(merge, dict):
            continue
        path = f"merges[{n}]"
        members = _strings(merge.get("members"))
        distinct = list(dict.fromkeys(members))
        if len(distinct) != len(members):
            add("member_duplicate", path + ".members", "同じ作業が2回以上あります")
        if len(members) >= 2 and len(distinct) < 2:
            add("members_too_few", path + ".members", "まとめる作業は、別の作業を2つ以上にします")
        for member in distinct:
            if member in used:
                add("member_in_two_merges", path + ".members",
                    f"{member} が merges[{used[member]}] にも入っています。1つの作業は1つのまとまりにだけ入れます")
            else:
                used[member] = n
            if items is None:
                continue
            info = items.get(member)
            if info is None:
                add("member_unknown", path + ".members", f"{member} は、まとめる候補の作業にありません")
                continue
            if info.get("status", "done") != "done":
                add("member_not_done", path + ".members", f"{member} は調べ終わった作業ではありません（{info.get('status')}）")
            if info.get("no_grouping"):
                add("member_no_grouping", path + ".members", f"{member} は、グループにしない CID を含みます")
        source = merge.get("fix_source")
        if isinstance(source, str) and source.startswith("item:") and source[5:].strip() and source[5:] not in distinct:
            add("fix_source_not_member", path + ".fix_source",
                f"{source} の作業は、このまとまりに入っていません。item:<まとめる作業の ID> にします")
        result = merge.get("result") if isinstance(merge.get("result"), dict) else None
        known = [items[m] for m in distinct if items is not None and m in items]
        if items is not None and known:
            cids = {c for info in known for c in info.get("cids", [])}
            if len(cids) > MAX_CIDS:
                add("too_many_cids", path + ".members", f"まとめる CID は合わせて {MAX_CIDS} 件までにします（今は {len(cids)} 件）")
            levels = [CONFIDENCE_ORDER[i["confidence"]] for i in known if i.get("confidence") in CONFIDENCE_ORDER]
            merged = result.get("confidence") if result else None
            if levels and merged in CONFIDENCE_ORDER and CONFIDENCE_ORDER[merged] > min(levels):
                lowest = next(k for k, v in CONFIDENCE_ORDER.items() if v == min(levels))
                add("confidence_too_high", path + ".result.confidence",
                    f"まとめた確信度は、まとめる作業のいちばん低い確信度（{lowest}）以下にします（今は {merged}）")
        if result:
            deviation = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
            comment = deviation.get("comment")
            comment_path = path + ".result.deviation.comment"
            if isinstance(comment, str):  # the comment rules also cover the schema's length and spaces
                problems[:] = [p for p in problems if p["path"] != comment_path]
                for p in check_comment(comment)["problems"]:
                    add("comment." + p["code"], comment_path, p["message"])
            warnings += combo_warnings(result, path + ".result.")
    for n, conflict in enumerate(conflicts):
        if not isinstance(conflict, dict):
            continue
        path = f"conflicts[{n}].items"
        names = _strings(conflict.get("items"))
        if len(set(names)) != len(names):
            add("conflict_duplicate", path, "同じ作業が2回以上あります")
        if items is not None:
            for name in dict.fromkeys(names):
                if name not in items:
                    add("conflict_unknown_item", path, f"{name} は、候補の作業にありません")
    if items is None:
        warnings.append({"code": "items_unchecked", "path": "merges",
                         "message": "候補の作業（CID・状態・確信度）が渡されていないので、作業の有無・CID の数・確信度を確かめていません"})
    return {"ok": not problems, "merges": len(merges), "conflicts": len(conflicts), "problems": problems,
            "warnings": warnings}
# --- end of plan rules -----------------------------------------------------------------------


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


def _read_file(path_text: str):
    path = Path(path_text)
    if not path.is_file():
        raise ValueError(f"ファイルがありません: {path}")
    return _read_json(path.read_bytes().decode("utf-8-sig", errors="replace"), str(path))


def _valid_items(value) -> dict:
    if isinstance(value, dict) and set(value) == {"items"} and isinstance(value["items"], dict):
        value = value["items"]
    if not isinstance(value, dict):
        raise ValueError('候補の作業は {"<ID>": {"cids": [...], "status": "done", "confidence": "high"}} の形にします')
    for key, info in value.items():
        cids = info.get("cids") if isinstance(info, dict) else None
        if not isinstance(cids, list) or not all(isinstance(c, int) and not isinstance(c, bool) for c in cids):
            raise ValueError(f"候補の作業 {key} の cids を、CID の数の配列にします")
    return value


def _parse_members(members: list[str], no_grouping: list[str]) -> dict:
    blocked = set()
    for value in no_grouping:
        for piece in re.split(r"[,\s]+", value.strip()):
            if piece:
                if not re.fullmatch(r"[0-9]+", piece):
                    raise ValueError(f"--no-grouping には CID の数を渡します（今は {value}）")
                blocked.add(int(piece))
    items = {}
    for value in members:
        parts = value.split(":")
        if len(parts) not in (2, 3) or not parts[0].strip() or not re.fullmatch(r"[0-9]+(,[0-9]+)*", parts[1].replace(" ", "")):
            raise ValueError(f"--member は <ID>:<CID をカンマで区切る>[:<確信度>] の形にします（今は {value}）")
        cids = [int(c) for c in parts[1].replace(" ", "").split(",")]
        info = {"cids": cids, "status": "done", "no_grouping": bool(set(cids) & blocked)}
        if len(parts) == 3:
            if parts[2] not in CONFIDENCE_ORDER:
                raise ValueError(f"--member の確信度は high / medium / low のどれかにします（今は {parts[2]}）")
            info["confidence"] = parts[2]
        items[parts[0].strip()] = info
    return items


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="統合の計画（plan.json）の形と決まりを確かめる。結果は JSON。")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--plan", help="plan.json のパス")
    source.add_argument("--stdin", action="store_true", help='標準入力から {"plan": {...}, "items": {...}} を読む')
    parser.add_argument("--items", help="候補の作業の JSON ファイル")
    parser.add_argument("--member", action="append", default=[], help="候補の作業 <ID>:<CID,...>[:<確信度>]（くり返せる）")
    parser.add_argument("--no-grouping", action="append", default=[], help="グループにしない CID（くり返せる）")
    try:
        args = parser.parse_args(argv)
        items = None
        if args.plan:
            plan = _read_file(args.plan)
        elif args.stdin:
            payload = _read_json(read_stdin_text(), "標準入力")
            if not isinstance(payload, dict) or "plan" not in payload:
                raise ValueError('標準入力には {"plan": {...}, "items": {...}} の形の JSON を渡してください')
            plan = payload["plan"]
            if payload.get("items") is not None:
                items = _valid_items(payload["items"])
        else:
            raise ValueError("計画を --plan <ファイル> か --stdin で渡してください")
        if args.items:
            items = _valid_items(_read_file(args.items))
        elif args.member:
            items = _parse_members(args.member, args.no_grouping)
        elif args.no_grouping:
            raise ValueError("--no-grouping は --member と一緒に使います")
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        out = check_plan(plan, schema, items)
        if not out["ok"]:
            out = {"ok": False, "error": "計画に直すところがあります", **out}
    except ValueError as exc:
        out = {"ok": False, "error": str(exc), "problems": [{"code": "bad_input", "message": str(exc)}]}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        message = f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}"
        out = {"ok": False, "error": message, "problems": [{"code": "unexpected", "message": message}]}
    emit(out)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
