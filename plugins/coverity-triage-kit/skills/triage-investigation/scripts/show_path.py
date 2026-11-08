# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Print a Coverity warning path with the code around each event.

A deterministic helper for reading a warning: it takes the warning path from an
``issue-<CID>.json`` file (the shape written by the Coverity fetch step), from a
JSON object on stdin, or from command-line arguments, maps the Coverity file paths
to files in the repository, and prints the code around every event. Main events
(where the problem happens) are marked with a star.

Output: exactly one JSON object on stdout. ``text`` holds the display text.
Failures: ``{"ok": false, "error": "...", "problems": [...]}`` and exit code 1.
The command only reads files; it never writes any.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_CONTEXT = 20  # lines before and after each event (the usual reading window)
MAX_CONTEXT = 200
# About 800 code lines keep the output well inside an agent's tool-output limit
# (roughly 25k tokens); a longer path is cut and the output says how to narrow it.
DEFAULT_MAX_LINES = 800
MAX_LINE_CHARS = 300  # very long (generated or minified) lines are cut for display
SKIP_DIRS = {".git", ".svn", ".hg"}
STAR = "\u2605"  # ★


class Failure(Exception):
    """A problem the caller can act on; printed as {"ok": false, ...}."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise Failure(f"引数が正しくありません（{message}）。使い方は --help で見られます。")


# --------------------------------------------------------------------------- output


def _json_escape_errors(exc: UnicodeError) -> tuple[str, int]:
    """Replace characters the output encoding cannot hold by JSON \\uXXXX escapes."""
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    out = []
    for ch in exc.object[exc.start:exc.end]:
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            out.append("\\u%04x\\u%04x" % (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)))
        else:
            out.append("\\u%04x" % code)
    return "".join(out), exc.end


codecs.register_error("show_path_json_escape", _json_escape_errors)


def _output_encoding() -> str:
    """PYTHONIOENCODING first; on Windows with piped stdout the console code page
    (PowerShell decodes a child's output with it); otherwise UTF-8."""
    env = os.environ.get("PYTHONIOENCODING", "").split(":")[0].strip()
    candidates = [env] if env else []
    if not env and sys.platform == "win32":
        try:
            piped = not sys.stdout.isatty()
        except (AttributeError, ValueError):
            piped = True
        if piped:
            try:
                import ctypes

                page = ctypes.windll.kernel32.GetConsoleOutputCP()  # type: ignore[attr-defined]
                if page:
                    candidates.append(f"cp{page}")
            except (AttributeError, OSError):
                pass
    for name in candidates:
        try:
            return codecs.lookup(name).name
        except LookupError:
            continue
    return "utf-8"


def emit(result: dict[str, Any]) -> int:
    text = json.dumps(result, ensure_ascii=False, indent=1)
    data = text.encode(_output_encoding(), errors="show_path_json_escape") + b"\n"
    out = getattr(sys.stdout, "buffer", None)
    if out is None:
        sys.stdout.write(data.decode(_output_encoding(), errors="replace"))
    else:
        out.write(data)
        out.flush()
    return 0 if result.get("ok") else 1


# --------------------------------------------------------------------------- reading


def _clean(path: str) -> str:
    # PowerShell turns a quoted path that ends with a backslash ("C:\repo\") into C:\repo"
    return path.strip().strip('"').strip()


def read_source(path: Path) -> tuple[list[str], str, str]:
    """Lines (without line ends), encoding name and newline style of a source file."""
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        text, encoding = data[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace"), "utf-8（BOM あり）"
    else:
        for name, label in (("utf-8", "utf-8"), ("cp932", "cp932（Shift_JIS）")):
            try:
                text, encoding = data.decode(name), label
                break
            except UnicodeDecodeError:
                continue
        else:
            text, encoding = data.decode("latin-1"), "不明（latin-1 で読んだ）"
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    newline = "CRLF" if crlf and not lf else "LF" if lf and not crlf else "混在" if crlf and lf else "なし"
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines], encoding, newline


def load_strip_prefixes(repo: Path, notes: list[str]) -> list[str]:
    """coverity.path_strip_prefixes of the project settings, when the file exists."""
    config = repo / ".coverity-triage" / "config.yaml"
    if not config.is_file():
        return []
    try:
        import yaml  # provided by uv (PEP 723 header)

        data = yaml.safe_load(config.read_text(encoding="utf-8-sig")) or {}
    except Exception as exc:  # a broken settings file must not stop the reading
        notes.append(f"設定 .coverity-triage/config.yaml を読めなかったので、パスの頭を外す設定は使っていません（{type(exc).__name__}）")
        return []
    coverity = data.get("coverity") if isinstance(data, dict) else None
    value = coverity.get("path_strip_prefixes") if isinstance(coverity, dict) else None
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value or [] if isinstance(v, (str, int)) and str(v).strip()]


class PathMapper:
    """Coverity file path -> repository-relative path.

    In order: a relative path that exists; an absolute path inside the repository;
    the longest matching prefix of path_strip_prefixes; the unique repository file
    with the longest common path suffix; otherwise the path is kept (unmapped)."""

    def __init__(self, repo: Path, prefixes: list[str]):
        self.repo = repo
        self.prefixes = sorted({_norm(p).rstrip("/") + "/" for p in prefixes}, key=len, reverse=True)
        self._index: dict[str, list[str]] | None = None
        self.cache: dict[str, tuple[str, str, str]] = {}

    def _files(self) -> dict[str, list[str]]:
        if self._index is None:
            self._index = {}
            for dirpath, dirnames, filenames in os.walk(self.repo):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for name in filenames:
                    rel = (Path(dirpath) / name).relative_to(self.repo).as_posix()
                    self._index.setdefault(name.lower(), []).append(rel)
        return self._index

    def map(self, original: str) -> tuple[str, str, str]:
        """(path, method, note). method: as_is / inside / prefix / suffix / unmapped."""
        if original not in self.cache:
            self.cache[original] = self._map(original)
        return self.cache[original]

    def _map(self, original: str) -> tuple[str, str, str]:
        norm = _norm(original)
        absolute = norm.startswith("/") or bool(re.match(r"^[A-Za-z]:", norm))
        if not absolute and norm and (self.repo / norm).is_file():
            return norm.removeprefix("./"), "as_is", ""
        if absolute:
            root = _norm(str(self.repo)).rstrip("/") + "/"
            if norm.lower().startswith(root.lower()) and (self.repo / norm[len(root):]).is_file():
                return norm[len(root):], "inside", ""
        lowered = norm.lower()
        for prefix in self.prefixes:
            if lowered.startswith(prefix.lower()):
                return norm[len(prefix):], "prefix", ""
        parts = [p for p in norm.split("/") if p and p != "." and not p.endswith(":")]
        if parts:
            best: list[str] = []
            best_len = 0
            for candidate in self._files().get(parts[-1].lower(), []):
                cparts = candidate.lower().split("/")
                n = 0
                while n < min(len(cparts), len(parts)) and cparts[-1 - n] == parts[-1 - n].lower():
                    n += 1
                if n > best_len:
                    best, best_len = [candidate], n
                elif n == best_len:
                    best.append(candidate)
            if len(best) == 1:
                return best[0], "suffix", f"パスを末尾が合うファイルに対応づけました: {original} → {best[0]}"
            if len(best) > 1:
                shown = "、".join(best[:5]) + ("ほか" if len(best) > 5 else "")
                return norm, "unmapped", (f"パスの候補が複数あり、決められませんでした: {original}（候補: {shown}）。"
                                          "リポジトリの中のパスを確かめてください")
        return norm, "unmapped", (f"パスをリポジトリの中のファイルに対応づけられませんでした: {original}"
                                  "（設定 coverity.path_strip_prefixes か --strip-prefix で外す頭を指定できます）")


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


# --------------------------------------------------------------------------- input

EVENT_SPEC = re.compile(r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<tag>[^:]*))?(?::(?P<description>.*))?$", re.S)
ALIASES = {
    "file": ("file", "filePathname", "file_path", "path"),
    "line": ("line", "lineNumber", "line_number"),
    "tag": ("tag", "eventTag", "event_tag"),
    "description": ("description", "eventDescription", "event_description"),
    "main": ("main", "mainEvent", "is_main"),
}


def _pick(obj: dict[str, Any], key: str) -> Any:
    for name in ALIASES[key]:
        if name in obj and obj[name] is not None:
            return obj[name]
    return None


def normalize_issue(raw: Any, label: str) -> dict[str, Any]:
    """One warning in the shape of issue-<CID>.json -> {issue fields, events}."""
    if not isinstance(raw, dict):
        raise Failure(f"{label} が JSON のオブジェクトではありません。")
    if "error" in raw and "events" not in raw:
        cid = raw.get("cid", "?")
        raise Failure(f"CID {cid} の警告経路は取れていません（取ったときの答え: {raw.get('error')}）。",
                      ["警告の場所だけで出すときは --file と --line を使います",
                       "警告経路を取り直せるなら、取り直してから使います"])
    issue = raw.get("issue") if isinstance(raw.get("issue"), dict) else {}
    merged = {**{k: v for k, v in raw.items() if k not in ("issue", "events")}, **issue}
    events_raw = raw.get("events")
    if events_raw is None:
        events_raw = []
    if not isinstance(events_raw, list):
        raise Failure(f"{label} の events がリストではありません。")
    problems: list[str] = []
    events: list[dict[str, Any]] = []
    for i, ev in enumerate(events_raw, start=1):
        if not isinstance(ev, dict):
            problems.append(f"events の {i} 番目がオブジェクトではありません")
            continue
        file, line = _pick(ev, "file"), _pick(ev, "line")
        if not isinstance(file, str) or not file.strip():
            problems.append(f"events の {i} 番目に file がありません")
            continue
        if line is not None:  # Coverity gives some events no line: such an event is listed without code
            try:
                line = int(line)
            except (TypeError, ValueError):
                problems.append(f"events の {i} 番目の line が数ではありません")
                continue
            if line < 1:
                problems.append(f"events の {i} 番目の line は 1 以上にします")
                continue
        events.append({"file": file.strip(), "line": line, "tag": str(_pick(ev, "tag") or ""),
                       "description": str(_pick(ev, "description") or ""), "main": bool(_pick(ev, "main"))})
    if problems:
        raise Failure(f"{label} の警告経路の形が正しくありません。", problems)
    if not events:
        file, line = merged.get("file"), merged.get("line")
        if isinstance(file, str) and file.strip() and isinstance(line, int) and line > 0:
            events = [{"file": file.strip(), "line": line, "tag": "", "description": "（警告の場所。経路のイベントはありません）",
                       "main": True}]
        else:
            raise Failure(f"{label} に警告経路（events）も警告の場所（file と line）もありません。")
    if not any(e["main"] for e in events):
        events[-1]["main"] = True  # Coverity puts the main event last when it is not marked
    fields = {k: merged.get(k) for k in ("cid", "checker", "impact", "stream", "file", "line", "function", "macro")}
    fields["checker_description"] = raw.get("checker_description") or merged.get("checker_description")
    return {**{k: v for k, v in fields.items() if v not in (None, "")}, "events": events}


def issues_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    problems: list[str] = []
    for i, spec in enumerate(args.event or [], start=1):
        m = EVENT_SPEC.match(spec.strip())
        if not m:
            problems.append(f"--event の {i} 番目「{spec}」は「ファイル:行[:タグ[:説明]]」の形にします")
            continue
        events.append({"file": _clean(m["file"]), "line": int(m["line"]), "tag": (m["tag"] or "").strip(),
                       "description": (m["description"] or "").strip(), "main": False})
    if problems:
        raise Failure("警告経路の引数が正しくありません。", problems)
    if args.line is not None and args.line < 1:
        raise Failure("--line は 1 以上にします。")
    if not events:
        if not args.file or args.line is None:
            raise Failure("警告の情報がありません。",
                          ["--issue-file で警告経路のファイルを渡すか",
                           "--file と --line で警告の場所を渡すか",
                           "--event で経路のイベントを順に渡すか、--stdin で JSON を渡します"])
        events = [{"file": _clean(args.file), "line": args.line, "tag": args.tag or "",
                   "description": args.note or "（警告の場所）", "main": True}]
    else:
        mains = args.main or []
        for n in mains:
            if not 1 <= n <= len(events):
                raise Failure(f"--main {n} に当たるイベントがありません（イベントは 1〜{len(events)}）。")
            events[n - 1]["main"] = True
        if not mains:
            hits = [e for e in events if args.line is not None and e["line"] == args.line
                    and (not args.file or _norm(e["file"]).endswith(_norm(_clean(args.file))))]
            for e in hits or events[-1:]:
                e["main"] = True
    issue: dict[str, Any] = {"events": events}
    for key in ("cid", "checker", "impact", "function"):
        if getattr(args, key):
            issue[key] = getattr(args, key)
    if args.file:
        issue["file"] = _clean(args.file)
    if args.line is not None:
        issue["line"] = args.line
    if args.checker_description:
        issue["checker_description"] = args.checker_description
    return [issue]


def load_inputs(args: argparse.Namespace) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for raw_path in args.issue_file or []:
        path = Path(_clean(raw_path))
        if not path.is_file():
            raise Failure(f"警告経路のファイルがありません: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise Failure(f"警告経路のファイルを JSON として読めません: {path}（{exc}）") from exc
        issues.append(normalize_issue(data, path.name))
    if args.stdin:
        raw = sys.stdin.buffer.read()
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise Failure(f"標準入力を JSON として読めません（{exc}）。") from exc
        items = data.get("issues") if isinstance(data, dict) and isinstance(data.get("issues"), list) else data
        items = items if isinstance(items, list) else [items]
        issues.extend(normalize_issue(item, f"標準入力の {i} 番目") for i, item in enumerate(items, start=1))
    # With a file or stdin, --file/--line add nothing (the warning location is in the input);
    # --event always adds one more warning made from the arguments.
    if not issues or args.event:
        issues.extend(issues_from_args(args))
    return issues


# --------------------------------------------------------------------------- display


def _label(n: int, main: bool) -> str:
    return f"{STAR}{n}" if main else str(n)


def plan_blocks(events: list[dict[str, Any]], sources: dict[str, list[str]], context: int
                ) -> list[tuple[str, int, int, dict[int, list[str]]]]:
    """Code windows of one warning, merged per file in the order the path first reaches each file."""
    windows: dict[str, list[list[Any]]] = {}
    for ev in events:
        lines = sources.get(ev["file"])
        if not lines or ev["line"] is None:
            continue
        lo, hi = max(1, ev["line"] - context), min(len(lines), ev["line"] + context)
        if lo > hi:
            continue
        windows.setdefault(ev["file"], []).append([lo, hi, {ev["line"]: [_label(ev["n"], ev["main"])]}])
    blocks: list[tuple[str, int, int, dict[int, list[str]]]] = []
    for rel, spans in windows.items():
        spans.sort(key=lambda s: s[0])
        merged: list[list[Any]] = []
        for lo, hi, marks in spans:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
                for ln, labels in marks.items():
                    merged[-1][2].setdefault(ln, []).extend(labels)
            else:
                merged.append([lo, hi, dict(marks)])
        blocks.extend((rel, lo, hi, marks) for lo, hi, marks in merged)
    return blocks


def _size(plans: list[list[tuple[str, int, int, dict[int, list[str]]]]]) -> int:
    return sum(hi - lo + 1 for blocks in plans for _rel, lo, hi, _marks in blocks)


def build(repo: Path, issues: list[dict[str, Any]], context: int, max_lines: int,
          mapper: PathMapper, notes: list[str]) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    sources: dict[str, list[str]] = {}
    drift: list[str] = []
    out_issues: list[dict[str, Any]] = []

    def source(rel: str) -> list[str] | None:
        if rel not in files:
            path = repo / rel
            if path.is_file():
                try:
                    lines, encoding, newline = read_source(path)
                except OSError as exc:
                    files[rel] = {"path": rel, "exists": True, "readable": False}
                    drift.append(f"{rel} を読めません（{exc.strerror or exc}）")
                    return None
                sources[rel] = lines
                files[rel] = {"path": rel, "exists": True, "lines": len(lines), "encoding": encoding, "newline": newline}
            else:
                files[rel] = {"path": rel, "exists": False}
                drift.append(f"{rel} がリポジトリにありません（解析したコードと違うか、パスの対応づけが違います）")
        return sources.get(rel)

    def mapped(original: str) -> str:
        rel, _method, note = mapper.map(original)
        if note and note not in notes:
            notes.append(note)
        return rel

    # 1. map the paths, read the files and collect the mechanical drift hints
    for issue in issues:
        events = []
        for n, ev in enumerate(issue["events"], start=1):
            rel = mapped(ev["file"])
            item = {"n": n, "file": rel, "line": ev["line"], "tag": ev["tag"], "description": ev["description"],
                    "main": ev["main"]}
            if rel != ev["file"]:
                item["reported_file"] = ev["file"]
            lines = source(rel)
            if lines is not None and ev["line"] is not None and ev["line"] > len(lines):
                hint = f"{rel}:{ev['line']} はファイルの行数（{len(lines)} 行）を超えています（コードがずれています）"
                if hint not in drift:
                    drift.append(hint)
            events.append(item)
        info = {k: v for k, v in issue.items() if k != "events"}
        if isinstance(info.get("file"), str):
            info["reported_file"], info["file"] = info["file"], mapped(info["file"])
            if info["reported_file"] == info["file"]:
                del info["reported_file"]
        function = info.get("function")
        if isinstance(function, str) and function.strip() and isinstance(info.get("file"), str):
            lines = source(info["file"])
            name = re.split(r"[\s(]", function.strip().split("::")[-1])[0]
            if lines is not None and name and not any(name in line for line in lines):
                drift.append(f"関数 {function} が {info['file']} に見つかりません（コードがずれています）")
        out_issues.append({**info, "events": events})

    # 2. fit the code into max_lines: first narrow the windows so that every event stays visible,
    #    and cut the end only when even narrow windows do not fit
    used = context
    plans = [plan_blocks(i["events"], sources, used) for i in out_issues]
    while _size(plans) > max_lines and used > 2:
        used = max(2, used - 5 if used > 7 else used - 1)
        plans = [plan_blocks(i["events"], sources, used) for i in out_issues]
    if used != context:
        notes.append(f"コードが長いので、前後の行数を {context} 行から {used} 行に減らしました（--max-lines で変えられます）")
    truncated = False
    budget = max_lines
    for blocks in plans:
        kept = []
        for rel, lo, hi, marks in blocks:
            if budget <= 0:
                truncated = True
                break
            if hi - lo + 1 > budget:
                hi, truncated = lo + budget - 1, True
            kept.append((rel, lo, hi, marks))
            budget -= hi - lo + 1
        blocks[:] = kept

    # 3. the display text
    text: list[str] = []
    for info, blocks in zip(out_issues, plans):
        events = info["events"]
        head = f"CID {info.get('cid', '?')} — {info.get('checker', 'チェッカー不明')}"
        if info.get("impact"):
            head += f"（Impact {info['impact']}）"
        text.append(head)
        if info.get("file"):
            loc = f"{info['file']}:{info['line']}" if info.get("line") else str(info["file"])
            text.append(f"場所: {loc}" + (f" {info['function']}()" if info.get("function") else ""))
        if info.get("stream"):
            text.append(f"ストリーム: {info['stream']}")
        if info.get("macro"):
            text.append(f"マクロ: {info['macro']}")
        if info.get("checker_description"):
            text.append(f"チェッカーの説明: {info['checker_description']}")
        text.append(f"警告経路（{STAR} は問題の起きる場所）:")
        width = len(str(len(events)))
        for ev in events:
            mark = STAR if ev["main"] else " "
            desc = " — ".join(x for x in (ev["tag"], ev["description"]) if x)
            where = f"{ev['file']}:{ev['line']}" if ev["line"] is not None else f"{ev['file']}（行の番号なし）"
            text.append(f" {mark} {ev['n']:>{width}}. {where}" + (f" {desc}" if desc else ""))
        text.append("")
        for rel, lo, hi, marks in blocks:
            marks = {ln: labels for ln, labels in marks.items() if lo <= ln <= hi}  # a cut block keeps its own marks
            numbers = sorted({lbl for labels in marks.values() for lbl in labels},
                             key=lambda s: int(s.lstrip(STAR)))
            text.append(f"--- {rel}（{lo}〜{hi} 行" + (f"。経路 {'・'.join(numbers)}）" if numbers else "）"))
            lines = sources[rel]
            gutter = max((len(",".join(v)) for v in marks.values()), default=0)
            lw = len(str(hi))
            for ln in range(lo, hi + 1):
                code = lines[ln - 1]
                if len(code) > MAX_LINE_CHARS:
                    code = code[:MAX_LINE_CHARS] + "…"
                text.append(f"{','.join(marks.get(ln, [])):>{gutter}} {ln:>{lw}} | {code}")
            text.append("")

    if truncated:
        notes.append(f"コードの抜き出しを {max_lines} 行で切りました。--max-lines を大きくするか、--context を小さくしてください")
    if notes:
        text.append("注意:")
        text.extend(f"- {n}" for n in notes)
    if drift:
        text.append("ずれの手がかり（機械的に見つけたもの）:")
        text.extend(f"- {d}" for d in drift)
    return {"ok": True, "repo": str(repo), "context": used, "issues": out_issues,
            "files": list(files.values()), "notes": notes, "drift_hints": drift, "truncated": truncated,
            "text": "\n".join(text).rstrip() + "\n"}


# --------------------------------------------------------------------------- command line


def parser() -> Parser:
    p = Parser(prog="show_path.py", description="Coverity の警告経路を、各イベントの前後のコードと一緒に出す（読むだけ）")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    show = sub.add_parser("show", help="警告経路と前後のコードを出す")
    show.add_argument("--repo", default=".", help="コードを読むフォルダ（リポジトリの一番上）。既定は今のフォルダ")
    show.add_argument("--issue-file", action="append", help="警告経路のファイル issue-<CID>.json（グループなら何度でも）")
    show.add_argument("--stdin", action="store_true", help="同じ形の JSON を標準入力で渡す")
    show.add_argument("--cid", type=int)
    show.add_argument("--checker")
    show.add_argument("--impact")
    show.add_argument("--function")
    show.add_argument("--checker-description")
    show.add_argument("--file", help="警告の場所のファイル（--event がなければ、ここが ★）")
    show.add_argument("--line", type=int, help="警告の場所の行")
    show.add_argument("--tag", help="--file と --line だけのときのイベントのタグ")
    show.add_argument("--note", help="--file と --line だけのときのイベントの説明")
    show.add_argument("--event", action="append", help="経路のイベント「ファイル:行[:タグ[:説明]]」。経路の順に何度でも")
    show.add_argument("--main", action="append", type=int, help="★ のイベントの番号（1 から）。省くと --line の行か最後のイベント")
    show.add_argument("--context", type=int, default=DEFAULT_CONTEXT, help=f"前後に出す行数（既定 {DEFAULT_CONTEXT}）")
    show.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES, help=f"コードを出す行数の上限（既定 {DEFAULT_MAX_LINES}）")
    show.add_argument("--strip-prefix", action="append", default=[], help="Coverity のパスから外す頭（設定の path_strip_prefixes に足す）")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if not 0 <= args.context <= MAX_CONTEXT:
            raise Failure(f"--context は 0〜{MAX_CONTEXT} にします。")
        if args.max_lines < 1:
            raise Failure("--max-lines は 1 以上にします。")
        repo = Path(_clean(args.repo))
        if not repo.is_dir():
            raise Failure(f"コードを読むフォルダがありません: {repo}", ["--repo にリポジトリの一番上のフォルダを渡します"])
        try:
            repo = repo.resolve()
        except OSError:  # some Windows drives refuse to resolve; an absolute path is enough
            repo = Path(os.path.abspath(repo))
        notes: list[str] = []
        issues = load_inputs(args)
        mapper = PathMapper(repo, load_strip_prefixes(repo, notes) + [_clean(p) for p in args.strip_prefix])
        return emit(build(repo, issues, args.context, args.max_lines, mapper, notes))
    except Failure as exc:
        return emit({"ok": False, "error": str(exc), "problems": exc.problems})
    except Exception as exc:  # never a traceback: say what happened in one JSON object
        return emit({"ok": False, "error": f"思わぬ問題で止まりました: {type(exc).__name__}: {exc}", "problems": []})


if __name__ == "__main__":
    sys.exit(main())
