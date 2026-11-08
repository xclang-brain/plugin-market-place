# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Check a deviation comment (逸脱コメント) against the shared writing rules (contract "comment").

Usage:
    uv run --native-tls --quiet "<skill>/scripts/check_comment.py" --comment "<逸脱コメント>"
    uv run --native-tls --quiet "<skill>/scripts/check_comment.py" --stdin   # JSON {"comment": "..."} on stdin

Output: exactly one JSON object on stdout:
    {"ok": true|false, "length": <chars>, "problems": [{"code": "...", "message": "..."}]}
    (+ "error" when ok is false). Exit code 0 when ok, 1 otherwise. Never a traceback.

The rules (function check_comment) are the shared contract "comment"; the test data is
assets/contract/comment/cases.json. Other scripts import this file (a byte copy in their own scripts/
folder) instead of writing the rules again: ``from check_comment import CATEGORIES, check_comment``.
"""

from __future__ import annotations

import argparse
import codecs
import json
import locale
import os
import re
import sys

# --- comment rules (contract: comment) -------------------------------------------------------
COMMENT_MAX = 200  # characters (Unicode code points) after strip; same limit as result.schema.json
CATEGORIES = ("誤検知。", "意図的。", "本物の不具合。")
# Conditions, caveats and preconditions: the comment must say the warning is harmless in every situation.
# 「〜場合は」「〜場合には」 open a condition whatever comes before them (「した場合は」「する場合は」「NULL の場合は」);
# a fact about the code is written without them (「失敗すると -1 を返す」). 「前提」 in any form is a precondition, and
# 「〜の間だけ」「〜の間のみ」 limit the verdict to a time. 「場合の処理」「その間に」 are not caught.
CONDITION_WORDS = ("ただし", "但し", "場合に限", "場合のみ", "に限り", "に限って", "に限る",
                   "場合は", "場合には", "に対して", "前提", "間だけ", "間のみ", "仮に")
# Guesses: the comment states facts read in the code, not likelihoods: adverbs of guessing, the writer's
# 「〜と思う」「〜と考える」 (also 「〜とは思えない」「〜とは考えにくい」), 「〜だろう」「〜でしょう」, 「〜と推測・推定する」.
# Kept narrow on purpose: 「戻ると考えて警告する」「とみなす」「推測しない」 describe what the tool does and are fine.
HEDGE_WORDS = ("おそらく", "恐らく", "たぶん", "多分", "かもしれ", "かも知れ",
               "と思う", "と思い", "と思われ", "と思っ", "と思え", "とは思", "ように思",
               "と考える", "と考えます", "と考えられ", "と考えた", "と考えてい", "とは考え", "考えにく",
               "と推測", "と推定", "と見られ", "とみられ", "だろう", "でしょう")
# How it was investigated, or what could not be checked (belongs to the rationale).
PROCESS_WORDS = ("分からなかった", "確認できなかった", "確かめられなかった", "追えなかった", "追いきれ",
                 "調べきれ", "再解析", "解析したリビジョン")
# Requests to people, recommendations and review instructions (「見直す」「見直し」 in any form).
REQUEST_WORDS = ("ください", "下さい", "お願い", "てほしい", "て欲しい", "要確認", "確認が必要",
                 "確認する必要", "すべき", "べきである", "が望ましい", "見直")
# A sentence that ends in 「〜こと」 is an instruction (「直すこと。」「入れないこと。」). Only sentence ends outside
# brackets count, so a code comment quoted in 「」 or （） is not read as one; 「〜ことはない。」 does not end in こと.
INSTRUCTION_END = "こと"
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


def _sentences(body: str) -> list[str]:
    """Sentences end with 。！？ outside brackets and quotes; a last sentence may lack the end mark.

    Returns the text of each sentence without its end mark."""
    depth, sentences, current, has_text = 0, [], [], False
    for ch in body:
        if ch in _OPENERS:
            depth += 1
            has_text = True
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
            has_text = True
        elif ch in _SENTENCE_ENDS and depth == 0:
            if has_text:
                sentences.append("".join(current).strip())
            current, has_text = [], False
            continue
        elif not ch.isspace():
            has_text = True
        current.append(ch)
    if has_text:
        sentences.append("".join(current).strip())
    return sentences


def _count_sentences(body: str) -> int:
    return len(_sentences(body))


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
    sentence_texts = _sentences(body)
    sentences = len(sentence_texts)
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
        add("condition_words", "条件・前提・但し書き・推量の言い方（%s）があります。どんな状況でも問題にならないことを、"
                               "条件を付けずに言い切ります。言い切れないときは、推奨を修正にします"
            % "、".join(f"「{w}」" for w in found))
    found = [w for w in PROCESS_WORDS if w in text]
    if found:
        add("process_words", "調べた過程や、調べきれなかったことの言い方（%s）があります。それは結果の根拠の説明に書き、"
                             "逸脱コメントには問題にならない理由だけを書きます" % "、".join(f"「{w}」" for w in found))
    found = [w for w, pattern in RECOMMEND_PATTERNS if pattern.search(text)]
    found += [w for w in REQUEST_WORDS if w in text]
    labels = [f"「{w}」" for w in found]
    if any(s.endswith(INSTRUCTION_END) for s in sentence_texts):
        labels.append(f"文の終わりの「〜{INSTRUCTION_END}」")
    if labels:
        add("request_words", "推奨・人への依頼・見直しなどの指示の言い方（%s）があります。逸脱コメントには、"
                             "問題にならない理由だけを書きます" % "、".join(labels))
    return {"ok": not problems, "length": length, "problems": problems}
# --- end of comment rules --------------------------------------------------------------------


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


def _comment_from_stdin() -> str:
    raw = read_stdin_text()
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if isinstance(value, dict):
        if not isinstance(value.get("comment"), str):
            raise ValueError("標準入力の JSON に、文字列の \"comment\" がありません（例 {\"comment\": \"誤検知。…\"}）")
        return value["comment"]
    if isinstance(value, str):
        return value
    # Plain text: drop the one line end that echo or a file adds.
    return raw[:-2] if raw.endswith("\r\n") else raw[:-1] if raw.endswith("\n") else raw


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="逸脱コメントの形を確かめる。結果は JSON（ok、length、problems）。")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--comment", help="確かめる逸脱コメント")
    source.add_argument("--stdin", action="store_true",
                        help="標準入力から読む（JSON の {\"comment\": \"...\"}、または文そのもの）")
    try:
        args = parser.parse_args(argv)
        if args.comment is not None:
            comment = args.comment
        elif args.stdin:
            comment = _comment_from_stdin()
        else:
            raise ValueError("逸脱コメントを --comment か --stdin で渡してください")
        result = check_comment(comment)
        if not result["ok"]:
            result = {"ok": False, "error": "逸脱コメントに直すところがあります", **result}
    except ValueError as exc:
        result = {"ok": False, "error": str(exc), "length": 0,
                  "problems": [{"code": "bad_input", "message": str(exc)}]}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        message = f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}"
        result = {"ok": False, "error": message, "length": 0,
                  "problems": [{"code": "unexpected", "message": message}]}
    emit(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
