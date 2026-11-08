"""Light C/C++ source reading: comments and strings blanked out, function definitions located,
#define values evaluated. Enough for the source-shape rules and for naming functions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "catch", "do", "else", "case", "defined",
            "alignof", "_Alignof", "_Static_assert", "static_assert", "decltype", "__attribute__", "typeof"}


def blank(text: str) -> str:
    """Comments, string and character literals replaced by spaces (newlines kept, quotes kept), and
    preprocessor lines blanked, so offsets and lines stay the same."""
    out = list(text)
    i, n = 0, len(text)
    line_start = True
    while i < n:
        c = text[i]
        if line_start and c == "#":
            j = i
            while j < n:
                if text[j] == "\n" and text[j - 1] != "\\":
                    break
                j += 1
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c == "\n":
            line_start = True
            i += 1
            continue
        if not c.isspace():
            line_start = False
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(out)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def match_close(text: str, open_at: int, pair: str = "{}") -> int:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == pair[0]:
            depth += 1
        elif text[i] == pair[1]:
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


@dataclass
class Function:
    name: str            # as written: frame_encode, RunningStats::RunningStats, channel
    qualified: str       # with the enclosing classes and namespaces: TelemetryHub::channel
    params: str          # the parameter list text
    sig_line: int
    open_line: int
    close_line: int
    body_start: int      # offset of "{"
    body_end: int        # offset of "}"
    is_cpp: bool
    const: bool = False
    ret: str = ""
    scope: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        """Coverity's functionDisplayName: C "name", C++ "Class::name(types) const"."""
        if not self.is_cpp:
            return self.name
        return f"{self.qualified}({param_types(self.params)})" + (" const" if self.const else "")

    @property
    def mangled(self) -> str:
        return mangle(self) if self.is_cpp else self.name


def param_types(params: str) -> str:
    params = params.strip()
    if params in ("", "void"):
        return ""
    out = []
    for p in split_top(params, ","):
        p = re.sub(r"=.*$", "", p).strip()
        p = re.sub(r"\[[^\]]*\]", "*", p)
        m = re.match(r"^(.*?[\s*&])(\w+)$", p)
        if m and m.group(2) not in ("int", "char", "long", "short", "unsigned", "signed", "float", "double", "bool"):
            p = m.group(1)
        p = re.sub(r"\s+", " ", p).strip()
        p = re.sub(r"\s*([*&])", r" \1", p).replace("* *", "**")
        out.append(p)
    return ", ".join(out)


_MANGLE = {"void": "v", "bool": "b", "char": "c", "int": "i", "unsigned": "j", "unsigned int": "j", "long": "l",
           "unsigned long": "m", "short": "s", "unsigned short": "t", "float": "f", "double": "d",
           "int32_t": "i", "uint32_t": "j", "int16_t": "s", "uint16_t": "t", "int8_t": "a", "uint8_t": "h",
           "size_t": "m", "int64_t": "l", "uint64_t": "m"}


def mangle(fn: Function) -> str:
    """An Itanium-like mangled name (good enough for display; the plugin does not read it)."""
    parts = fn.qualified.split("::")
    last = parts[-1]
    if len(parts) > 1 and last == parts[-2]:
        last_code = "C2"
    elif last.startswith("~"):
        last_code = "D2"
    else:
        last_code = f"{len(last)}{last}"
    if len(parts) > 1:
        nested = "".join(f"{len(p)}{p}" for p in parts[:-1])
        name = f"N{'K' if fn.const else ''}{nested}{last_code}E"
    else:
        name = last_code
    codes = []
    for t in (param_types(fn.params).split(", ") if param_types(fn.params) else []):
        ptr = t.count("*")
        ref = "&" in t
        const = t.startswith("const ")
        base = t.replace("const ", "").replace("*", "").replace("&", "").strip()
        code = _MANGLE.get(base, f"{len(base)}{base}")
        if const:
            code = "K" + code
        code = "P" * ptr + ("R" if ref else "") + code
        codes.append(code)
    return "_Z" + name + ("".join(codes) or "v")


def split_top(text: str, sep: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


class Source:
    """One source file: original text, blanked text, functions."""

    def __init__(self, path: str | Path, text: str | None = None, is_cpp: bool | None = None):
        self.path = str(path)
        self.text = text if text is not None else Path(path).read_text(encoding="utf-8", errors="replace")
        self.lines = self.text.splitlines()
        self.code = blank(self.text)
        self.is_cpp = is_cpp if is_cpp is not None else self.path.endswith((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".C"))
        self.functions = find_functions(self.code, self.is_cpp)

    def line(self, n: int) -> str:
        return self.lines[n - 1] if 0 < n <= len(self.lines) else ""

    def function_at(self, line: int) -> Function | None:
        best = None
        for f in self.functions:
            if f.sig_line <= line <= f.close_line and (best is None or f.sig_line >= best.sig_line):
                best = f
        return best

    def body(self, fn: Function) -> str:
        return self.code[fn.body_start:fn.body_end + 1]

    def offset_of_line(self, n: int) -> int:
        off = 0
        for _ in range(n - 1):
            off = self.text.index("\n", off) + 1
        return off


def find_functions(code: str, is_cpp: bool) -> list[Function]:
    """Function definitions at file, namespace or class scope."""
    out: list[Function] = []
    _scan(code, 0, len(code), [], is_cpp, out)
    return out


def _scan(code: str, start: int, end: int, scope: list[str], is_cpp: bool, out: list[Function]) -> None:
    i = start
    header_start = start
    while i < end:
        c = code[i]
        if c in ";}":
            header_start = i + 1
        elif c == "{":
            header = code[header_start:i]
            close = match_close(code, i)
            kind, name = _classify(header)
            if kind == "function":
                fn = _function(code, header, header_start, i, close, scope, is_cpp)
                if fn:
                    out.append(fn)
            elif kind in ("namespace", "class", "extern"):
                _scan(code, i + 1, close, scope + ([name] if kind == "class" and name else []), is_cpp, out)
            i = close
            header_start = close + 1
        i += 1


def _classify(header: str) -> tuple[str, str]:
    h = header.strip()
    if not h:
        return "block", ""
    if re.search(r"\bnamespace\b", h):
        return "namespace", ""
    if re.match(r'^extern\s*$', re.sub(r'"[^"]*"', "", h).strip()) or re.search(r'extern\s*"C"\s*$', header):
        return "extern", ""
    if h.endswith("=") or re.search(r"=\s*$", h):
        return "init", ""
    m = re.search(r"\b(class|struct|union|enum)\s+(?:\w+\s+)*?(\w+)\s*(?:final\s*)?(?::[^()]*)?$", h)
    if m and "(" not in h[m.start():]:
        return ("class" if m.group(1) in ("class", "struct") else "aggregate"), m.group(2)
    if re.search(r"\b(class|struct|union|enum)\s*$", h):
        return "aggregate", ""
    if ")" in h:
        return "function", ""
    return "block", ""


def _function(code: str, header: str, header_start: int, open_at: int, close: int, scope: list[str],
              is_cpp: bool) -> Function | None:
    # the first "(" at depth 0 preceded by a name that is not a keyword
    depth = 0
    for j, ch in enumerate(header):
        if ch == "(" and depth == 0:
            before = header[:j].rstrip()
            m = re.search(r"((?:~?\w+\s*::\s*)*~?\w+|operator\s*\S+)$", before)
            if m and m.group(1).split("::")[-1].strip() not in KEYWORDS:
                params_end = j + match_close(header[j:], 0, "()")
                params = header[j + 1:params_end]
                rest = header[params_end + 1:]
                name = re.sub(r"\s+", "", m.group(1))
                if not is_cpp and "::" in name:
                    return None
                ret = re.sub(r"\b(public|private|protected)\s*:|\btemplate\s*<[^>]*>|\b(explicit|inline|virtual)\b",
                             " ", before[:m.start()])
                ret = re.sub(r"\s+", " ", ret).strip()
                if not ret and "::" not in name and not scope and not is_cpp:
                    return None
                qualified = "::".join(scope + [name]) if scope else name
                sig_line = line_of(code, header_start + m.start())
                return Function(name=name.split("::")[-1] if not scope else name, qualified=qualified, params=params,
                                sig_line=sig_line, open_line=line_of(code, open_at), close_line=line_of(code, close),
                                body_start=open_at, body_end=close, is_cpp=is_cpp,
                                const=bool(re.match(r"^\s*const\b", rest)), ret=ret, scope=list(scope))
            break
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
    return None


# ---- macros ------------------------------------------------------------------------------------

def read_function_macros(paths: list[Path]) -> dict[str, str]:
    """Function-like macros whose body calls a function: name -> that function (LOG_I -> log_printf)."""
    out: dict[str, str] = {}
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)\([^)]*\)[ \t]+\(?\s*(\w+)\s*\(", text, re.M):
            out.setdefault(m.group(1), m.group(2))
    return out


def read_defines(paths: list[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)(?![\w(])[ \t]*(.*?)(?://.*|/\*.*?\*/[ \t]*)?$", text, re.M):
            out.setdefault(m.group(1), m.group(2).strip())
    return out


def eval_macro(name: str, defines: dict[str, str], depth: int = 0) -> int | None:
    """Integer value of an object-like macro made of numbers, other macros and + - * / ( )."""
    if depth > 20 or name not in defines:
        return None
    expr = defines[name]
    tokens = re.findall(r"0[xX][0-9a-fA-F]+[uUlL]*|\d+[uUlL]*|\w+|[()+\-*/]", expr)
    out = []
    for t in tokens:
        if re.match(r"0[xX]", t):
            out.append(str(int(t.rstrip("uUlL"), 16)))
        elif t[0].isdigit():
            out.append(t.rstrip("uUlL"))
        elif re.match(r"\w", t):
            v = eval_macro(t, defines, depth + 1)
            if v is None:
                return None
            out.append(str(v))
        else:
            out.append("//" if t == "/" else t)
    try:
        return int(eval("".join(out), {"__builtins__": {}}, {}))  # only digits and operators remain
    except Exception:  # noqa: BLE001 - not a constant expression
        return None


def condition_at(src: Source, line: int, col: int | None = None) -> str | None:
    """The condition of the if/while/for/switch on ``line``; with ``col`` inside it, the operand of
    && / || that holds the column (Coverity reports each operand of a compound condition)."""
    text = src.line(line)
    code = blank(text + "\n")[:-1]
    span = None
    m = re.search(r"\b(if|while|switch)\s*\(", code)
    if m:
        open_at = m.end() - 1
        span = (open_at + 1, match_close(code, open_at, "()"))
    else:
        m = re.search(r"\bfor\s*\(", code)
        if m:
            open_at = m.end() - 1
            close = match_close(code, open_at, "()")
            semis = [k for k in range(open_at + 1, close) if code[k] == ";" and
                     code[open_at + 1:k].count("(") == code[open_at + 1:k].count(")")]
            if len(semis) >= 2:
                span = (semis[0] + 1, semis[1])
    if span is None:
        if not col or not (0 <= col - 1 < len(code)):
            return None
        i = col - 1
        if code[i] == "(":
            return _strip_parens(text[i:match_close(code, i, "()") + 1])
        j, depth = i, 0
        while j < len(code):
            ch = code[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and (code.startswith("&&", j) or code.startswith("||", j) or ch in ";?,{"):
                break
            j += 1
        piece = text[i:j].strip()
        return _strip_parens(piece) if piece else None
    start, end = span
    if col and start <= col - 1 < end:
        pieces, depth, last = [], 0, start
        k = start
        while k < end:
            ch = code[k]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth <= 1 and (code.startswith("&&", k) or code.startswith("||", k)):
                pieces.append((last, k))
                last = k + 2
                k += 2
                continue
            k += 1
        pieces.append((last, end))
        if len(pieces) > 1:
            # operands at the top level, or inside the single outer pair of parentheses
            for a, b in pieces:
                if a <= col - 1 < b:
                    return _strip_parens(text[a:b])
    return _strip_parens(text[start:end])


def _strip_parens(s: str) -> str:
    s = s.strip()
    while s.startswith("(") and match_close(s, 0, "()") == len(s) - 1:
        s = s[1:-1].strip()
    return s
