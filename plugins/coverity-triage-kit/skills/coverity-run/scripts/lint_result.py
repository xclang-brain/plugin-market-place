# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Find, by fixed rules, the points of a triage result (result.json) that a reviewer should look at.

Usage:
    uv run --native-tls --quiet "<skill>/scripts/lint_result.py" --result "<result.json>" --repo "<code folder>"
    uv run --native-tls --quiet "<skill>/scripts/lint_result.py" --stdin --repo "<code folder>"   # stdin: result JSON

Output: {"ok": true, "points": [sorted unique codes], "findings": [{"code", "message", "file"?, "line"?}]}.
The points are hints (the reviewer weighs them); "ok" is false only when the input cannot be read.
Nothing is written. The rules are the shared contract "lint" (assets/contract/lint/cases.json).
"""

from __future__ import annotations

import argparse
import codecs
import json
import locale
import os
import posixpath
import re
import sys
from pathlib import Path

# --- lint rules (contract: lint) ------------------------------------------------------------
LINT_CODES = ("evidence_missing_file", "evidence_line_out_of_range", "undetermined_high", "drift_high",
              "true_bug_deviation", "not_bug_fix", "no_evidence")


def resolve_in_repo(repo: Path, file: str) -> Path | None:
    """Repository-relative path (either slash) -> path inside repo; None for absolute or escaping paths."""
    text = file.strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    normal = posixpath.normpath(text)
    if normal == "." or normal.split("/")[0] == "..":
        return None
    return repo.joinpath(*normal.split("/"))


def count_lines(data: bytes) -> int:
    """Lines as an editor shows them (LF, CRLF or CR); a last line without a line end counts."""
    return len(data.splitlines())


def lint(result: dict, repo: Path) -> list[dict]:
    findings: list[dict] = []
    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    evidence = verdict.get("evidence") if isinstance(verdict.get("evidence"), list) else []
    entries = [e for e in evidence if isinstance(e, dict)]
    lines_of: dict[Path, int] = {}
    for entry in entries:
        file = entry.get("file") if isinstance(entry.get("file"), str) else ""
        path = resolve_in_repo(repo, file)
        if path is None or not path.is_file():
            findings.append({"code": "evidence_missing_file", "file": file,
                             "message": f"根拠のファイルが、調べるコードにありません: {file or '（空）'}"})
            continue
        line = entry.get("line")
        if isinstance(line, int) and not isinstance(line, bool):
            if path not in lines_of:
                lines_of[path] = count_lines(path.read_bytes())
            if line < 1 or line > lines_of[path]:
                findings.append({"code": "evidence_line_out_of_range", "file": file, "line": line,
                                 "message": f"根拠の行が範囲の外です: {file}:{line}（{lines_of[path]} 行まで）"})
    if not entries:
        findings.append({"code": "no_evidence", "message": "根拠の場所（verdict.evidence）がありません"})
    judgement = verdict.get("judgement")
    confidence = result.get("confidence")
    recommendation = result.get("recommendation")
    drift = result.get("revision_drift") if isinstance(result.get("revision_drift"), dict) else {}
    if judgement == "undetermined" and confidence == "high":
        findings.append({"code": "undetermined_high", "message": "判定が undetermined（判定できない）なのに、確信度が high です"})
    if drift.get("status") == "detected" and confidence == "high":
        findings.append({"code": "drift_high", "message": "リビジョンのずれがある（detected）のに、確信度が high です"})
    if judgement == "true_bug" and recommendation == "deviation":
        findings.append({"code": "true_bug_deviation",
                         "message": "true_bug（本物のバグ）なのに、逸脱を推奨しています（プロジェクトの方針によることがあります）"})
    if judgement in ("false_positive", "intentional") and recommendation == "fix":
        findings.append({"code": "not_bug_fix",
                         "message": f"{judgement} なのに、修正を推奨しています（小さく安全な修正なら、ありえます）"})
    return findings


def points(findings: list[dict]) -> list[str]:
    return sorted({f["code"] for f in findings})
# --- end of lint rules -----------------------------------------------------------------------


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


def _load_result(args) -> dict:
    if args.result:
        path = Path(args.result)
        if not path.is_file():
            raise ValueError(f"結果のファイルがありません: {path}")
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        where = str(path)
    else:
        text, where = read_stdin_text(), "標準入力"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where} を JSON として読めません（{exc.lineno} 行 {exc.colno} 文字目: {exc.msg}）") from exc
    if isinstance(value, dict) and set(value) == {"result"} and isinstance(value["result"], dict):
        value = value["result"]
    if not isinstance(value, dict):
        raise ValueError("結果が JSON のオブジェクト（{...}）ではありません")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="結果（result.json）の、決まった方法で見つかる点を挙げる。結果は JSON。")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--result", help="result.json のパス")
    source.add_argument("--stdin", action="store_true", help="標準入力から結果の JSON を読む")
    parser.add_argument("--repo", help="調べたコードのフォルダ（根拠のパスの起点）")
    try:
        args = parser.parse_args(argv)
        if not (args.result or args.stdin):
            raise ValueError("結果を --result <ファイル> か --stdin で渡してください")
        if not args.repo:
            raise ValueError("調べたコードのフォルダを --repo で渡してください")
        repo = Path(args.repo)
        if not repo.is_dir():
            raise ValueError(f"--repo のフォルダがありません: {repo}")
        findings = lint(_load_result(args), repo)
        out = {"ok": True, "points": points(findings), "findings": findings}
    except ValueError as exc:
        out = {"ok": False, "error": str(exc), "problems": [{"code": "bad_input", "message": str(exc)}]}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        message = f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}"
        out = {"ok": False, "error": message, "problems": [{"code": "unexpected", "message": message}]}
    emit(out)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
