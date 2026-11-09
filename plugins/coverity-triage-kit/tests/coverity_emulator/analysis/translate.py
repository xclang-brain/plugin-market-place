"""Findings of clang, gcc and cppcheck turned into Coverity warnings (checker, subcategory, extra,
events). The checker table follows specification 5.9 - 5.11, the event tags and the wording follow
the real output collected in 5.10 (eventDescription quotes code in double quotes, in English).

A :class:`Finding` keeps the events flat with a nesting depth; ``analyze.py`` builds the tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .csource import Function, Source, condition_at, eval_macro
from .tools import RawEvent, RawFinding

Q = "[‘’'\"]"  # gcc writes ‘x’, clang 'x'


@dataclass
class Finding:
    checker: str
    subcategory: str
    extra: str
    file: str
    line: int
    events: list[dict]
    tool: str
    function: Function | None = None
    has_subtype: bool = True
    type: str | None = None


class Context:
    """Sources and macros shared by the translators."""

    def __init__(self, defines: dict[str, str] | None = None, fmacros: dict[str, str] | None = None):
        self.sources: dict[str, Source] = {}
        self.defines = defines or {}
        self.fmacros = fmacros or {}

    def callee(self, text: str, near: int | None = None) -> str | None:
        name = _callee(text, near)
        return self.fmacros.get(name, name) if name else name

    def null_cond(self, path: str, line: int, name: str) -> str | None:
        """The comparison of ``name`` (a variable or a field) with NULL written on the line."""
        text = self.src(path).line(line)
        target = rf"(?:[\w.]+->|[\w.]+\.)?{re.escape(name)}" if "->" not in name else re.escape(name)
        m = re.search(rf"({target})\s*(==|!=)\s*(NULL|nullptr|0)\b", text) or \
            re.search(rf"(NULL|nullptr)\s*(==|!=)\s*({target})\b", text)
        if m:
            return m.group(0)
        m = re.search(rf"!\s*({target})\b", text)
        return m.group(0) if m else None

    def src(self, path: str) -> Source:
        if path not in self.sources:
            try:
                self.sources[path] = Source(path)
            except OSError:
                self.sources[path] = Source(path, text="")
        return self.sources[path]

    def function_at(self, path: str, line: int) -> Function | None:
        return self.src(path).function_at(line)

    def expand(self, text: str) -> str:
        """Object-like macros with a literal value written as that value, as Coverity shows conditions."""
        def repl(m: re.Match) -> str:
            name = m.group(0)
            value = self.defines.get(name)
            if value is None:
                return name
            lit = re.fullmatch(r"\(?\s*(-?(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*)\s*\)?", value)
            if lit:
                return lit.group(1)
            number = eval_macro(name, self.defines)
            return f"{number}U" if number is not None and value.count("U") else (str(number) if number is not None else name)
        return re.sub(r"\b[A-Z][A-Z0-9_]{2,}\b", repl, text)

    def cond(self, path: str, line: int, col: int | None = None) -> str:
        c = condition_at(self.src(path), line, col)
        return self.expand(c) if c else "..."


def ev(tag: str, desc: str, file: str, line: int, depth: int = 0, main: bool = False, kind: str | None = None) -> dict:
    return {"tag": tag, "desc": desc, "file": file, "line": line, "depth": depth, "main": main,
            "kind": kind or ("PATH" if tag == "path" else "NORMAL")}


def path_ev(cond: str, branch: str, file: str, line: int, depth: int = 0) -> dict:
    return ev("path", f'Condition "{cond}", taking {branch} branch.', file, line, depth)


_REL = {"<=": "<=", ">=": ">=", "<": "<", ">": ">", "equal to": "==", "not equal to": "!="}
_NEG = {"<": ">=", ">=": "<", ">": "<=", "<=": ">", "==": "!=", "!=": "=="}


def branch_of(assumption: str, cond: str) -> str:
    """Which branch an analyzer assumption takes for a condition text."""
    c = re.sub(r"\s+", "", cond)
    m = re.search(r"is (not )?(equal to|<=|>=|<|>) ?(null|NULL)?", assumption)
    null = re.search(r"\bis (not )?(equal to )?(null|NULL)\b|is (non-)?null", assumption)
    if null:
        assumed_null = not (null.group(1) or null.group(4))
        cond_is_null = bool(re.search(r"==NULL|==nullptr|^!", c))
        cond_is_nonnull = bool(re.search(r"!=NULL|!=nullptr", c)) or re.fullmatch(r"[\w.>\-\[\]]+", c) is not None
        if cond_is_null:
            return "true" if assumed_null else "false"
        if cond_is_nonnull:
            return "false" if assumed_null else "true"
        return "true"
    if re.search(r"is (true|false)$", assumption) or "the condition is" in assumption:
        val = assumption.rstrip(".").split()[-1]
        if c.startswith("!") and "the condition is" not in assumption:
            return "false" if val == "true" else "true"
        return val
    if m:
        rel = _REL[("not " if m.group(1) else "") + m.group(2) if m.group(2) == "equal to" else m.group(2)]
        found = re.search(r"(<=|>=|==|!=|<|>)", c)
        if found:
            op = found.group(1)
            if op == rel:
                return "true"
            if _NEG.get(op) == rel:
                return "false"
    return "true"


def _callee(line_text: str, near: int | None = None, skip: tuple[str, ...] = ()) -> str | None:
    calls = [(m.start(), m.group(1)) for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", line_text)
             if m.group(1) not in ("if", "while", "for", "switch", "return", "sizeof", "void", "char", "int",
                                   "uint8_t", "uint16_t", "uint32_t", "int32_t", "size_t", "unsigned") + skip]
    if not calls:
        return None
    if near is not None:
        before = [c for c in calls if c[0] <= near]
        if before:
            return before[-1][1]
    return calls[0][1]


def _rhs(line_text: str, var: str) -> str | None:
    m = re.search(rf"(?:^|[^=!<>]){re.escape(var)}\s*=\s*([^=;][^;]*);", line_text)
    return m.group(1).strip() if m else None


def _strip_cast(expr: str) -> str:
    return re.sub(r"^\((?:const\s+)?\w+\s*\*?\)\s*", "", expr.strip())


def _quote_var(msg: str) -> str | None:
    m = re.search(rf"{Q}([^‘’'\"]+){Q}", msg)
    return m.group(1) if m else None


# ---- clang ---------------------------------------------------------------------------------------

def _trim(events: list[RawEvent]) -> list[RawEvent]:
    """Keep the part of the path in the function where the problem is (Coverity starts there);
    deeper events (callees) stay, with depths made relative."""
    if not events:
        return []
    final = events[-1].depth
    start = 0
    for i, e in enumerate(events):
        if e.depth < final:
            start = i + 1
    kept = [e for e in events[start:] if e.depth >= final]
    for e in kept:
        e.depth -= final
    return kept


def from_clang(f: RawFinding, ctx: Context) -> Finding | None:
    rule, msg = f.rule, f.message
    src = ctx.src(f.file)
    text = src.line(f.line)
    events = _trim(list(f.path))
    checker, sub, extra = None, "none", ""
    if rule in ("core.NullDereference", "core.NonNullParamChecker"):
        var = re.search(r"loaded from (?:variable|field) '([^']+)'", msg)
        via = re.search(r"via field '([^']+)'", msg)
        null_check = [e for e in events if re.search(r"Assuming .*(equal to NULL|equal to null|is null)", e.message)
                      and "not equal" not in e.message]
        returning = [e for e in events if e.message.startswith("Returning null pointer")]
        assigned = [e for e in events if re.match(r"(Value assigned to|'.+' initialized to a null)", e.message)]
        zero = [e for e in events if re.search(r"'(.+)' initialized to a null pointer value|Null pointer value stored to '(.+)'",
                                                  e.message)]
        if rule == "core.NonNullParamChecker" and zero:
            mz = re.search(r"'([^']+)'", zero[-1].message)
            extra = mz.group(1)
        elif via and null_check:
            fld = re.search(r"field '([^']+)'", null_check[-1].message)
            obj = re.search(r"(\w+)\s*->\s*" + re.escape(via.group(1)), text)
            extra = f"{obj.group(1)}->{via.group(1)}" if obj else via.group(1)
        elif var:
            extra = var.group(1)
        elif null_check:
            extra = re.search(r"'([^']+)'", null_check[-1].message).group(1) if "'" in null_check[-1].message else ""
        elif assigned:
            extra = re.search(r"'([^']+)'", assigned[-1].message).group(1)
        if returning and not null_check:
            checker = "NULL_RETURNS"
            callee = None
            for e in events:
                if e.message.startswith("Calling '"):
                    callee = re.search(r"Calling '([^']+)'", e.message).group(1)
            extra = callee or extra
        else:
            checker = "FORWARD_NULL"
    elif rule == "core.DivideZero":
        checker = "DIVIDE_BY_ZERO"
        m = re.search(r"([\w\->.\[\]]+)\s*[/%]\s*([\w\->.\[\]]+)", text)
        extra = m.group(2) if m else ""
    elif rule.startswith("core.uninitialized.") or (rule in ("core.UndefinedBinaryOperatorResult", "core.CallAndMessage")
                                                    and "garbage" in msg or "uninitialized" in msg):
        decl = [e for e in events if "declared without an initial value" in e.message]
        if decl:
            checker = "UNINIT"
            extra = re.search(r"'([^']+)'", decl[0].message).group(1)
        else:
            return None  # a field of a struct seen byte by byte (see envmon-truth incidental): no Coverity equivalent
    elif rule == "unix.Malloc" or rule.startswith("cplusplus.NewDelete"):
        if "leak" in msg.lower():
            checker = "RESOURCE_LEAK"
            m = re.search(r"pointed to by '([^']+)'", msg)
            extra = m.group(1) if m else ""
        elif "after it is freed" in msg or "released" in msg:
            checker, sub = "USE_AFTER_FREE", "none"
            freed = [e for e in events if e.message.startswith("Memory is released")]
            if freed:
                m = re.search(r"(?:free|delete)\s*\(?\s*([\w\->.]+)", ctx.src(freed[-1].file).line(freed[-1].line))
                extra = m.group(1) if m else ""
            if "Attempt to free released memory" in msg:
                sub = "double_free"
        else:
            return None
    elif rule == "unix.MismatchedDeallocator":
        checker = "ALLOC_FREE_MISMATCH"
    elif rule == "deadcode.DeadStores":
        checker = "UNUSED_VALUE"
        m = re.search(r"'([^']+)'", msg)
        extra = m.group(1) if m else ""
    else:
        return None
    out = clang_events(events, ctx, checker, extra)
    return Finding(checker, sub, extra, f.file, f.line, out, "clang")


def clang_events(events: list[RawEvent], ctx: Context, checker: str, extra: str) -> list[dict]:
    out: list[dict] = []
    compared = False
    for idx, e in enumerate(events):
        msg, file, line, depth = e.message, e.file, e.line, e.depth
        last = idx == len(events) - 1
        text = ctx.src(file).line(line)
        if last:
            out.append(main_event(checker, extra, msg, file, line, depth, ctx, e.col))
            continue
        if msg.startswith(("Entered call from", "Returning from", "The value ", "Execution continues")):
            continue
        m = re.match(r"Calling '([^']+)'", msg)
        if m:
            out.append(ev("call", f'Calling "{m.group(1)}".', file, line, depth, kind="MODEL"))
            continue
        if msg.startswith("Returning null pointer"):
            out.append(ev("return_null", "Explicitly returning NULL.", file, line, depth))
            continue
        m = re.match(r"'([^']+)' declared without an initial value", msg)
        if m:
            out.append(ev("var_decl", f'Declaring variable "{m.group(1)}" without initializer.', file, line, depth))
            continue
        m = re.match(r"'([^']+)' initialized to a null pointer value|Null pointer value stored to '([^']+)'", msg)
        if m:
            name = m.group(1) or m.group(2)
            out.append(ev("assign_zero", f'Assigning: "{name}" = "NULL".', file, line, depth))
            continue
        m = re.match(r"Value assigned to '([^']+)'", msg)
        if m:
            rhs = _rhs(text, m.group(1).split("->")[-1]) or "..."
            out.append(ev("assignment", f'Assigning: "{m.group(1)}" = "{ctx.expand(rhs)}".', file, line, depth))
            continue
        if msg.startswith("Memory is allocated"):
            fn = _callee(text, e.col) or "malloc"
            out.append(ev("alloc_fn", f'Storage is returned from allocation function "{fn}".', file, line, depth))
            var = re.match(r"\s*([\w\->.]+)\s*=", text)
            if var:
                call = re.search(rf"{fn}\s*\([^;]*\)", text)
                expr = ctx.expand(call.group(0)) if call else fn
                out.append(ev("var_assign", f'Assigning: "{var.group(1)}" = storage returned from "{expr}".', file,
                              line, depth))
            continue
        if msg.startswith("Memory is released"):
            m2 = re.search(r"\b(free|delete)\b\s*\(?\s*([\w\->.]+)", text)
            if m2:
                out.append(ev("freed_arg", f'"{m2.group(1)}" frees "{m2.group(2)}".', file, line, depth))
            continue
        name_m = re.search(r"'([^']+)'", msg)
        name = name_m.group(1) if name_m else ""
        matches_var = bool(name) and (name == extra or extra.endswith("->" + name) or extra.endswith("." + name))
        if checker == "FORWARD_NULL" and not compared and matches_var and \
                re.search(r"Assuming .*(equal to NULL|equal to null|is null)", msg) and "not equal" not in msg:
            out.append(ev("var_compare_op", f'Comparing "{extra}" to null implies that "{extra}" might be null.', file,
                          line, depth))
            compared = True
            continue
        if msg.startswith("Looping back"):
            out.append(ev("path", "Jumping back to the beginning of the loop.", file, line, depth))
            continue
        if msg.startswith(("Entering loop body", "Loop condition is true")):
            out.append(path_ev(ctx.cond(file, line), "true", file, line, depth))
            continue
        if msg.startswith(("Loop condition is false", "Loop body executed 0 times")):
            out.append(path_ev(ctx.cond(file, line), "false", file, line, depth))
            continue
        m = re.match(r"Taking (true|false) branch", msg)
        if m:
            out.append(path_ev(ctx.cond(file, line), m.group(1), file, line, depth))
            continue
        if msg.startswith("Assuming"):
            cond = None
            if name and re.search(r"(NULL|null)\b", msg):
                cond = ctx.null_cond(file, line, name)
                cond = ctx.expand(cond) if cond else None
            cond = cond or ctx.cond(file, line, e.col)
            out.append(path_ev(cond, branch_of(msg, cond), file, line, depth))
            continue
    return _dedupe(_prune_calls(out, checker, extra))


def _dedupe(events: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in events:
        if out and all(out[-1][k] == e[k] for k in ("tag", "desc", "line", "depth")):
            continue
        out.append(e)
    return out


RELEVANT = {"NULL_RETURNS": {"return_null"}, "USE_AFTER_FREE": {"freed_arg"}, "RESOURCE_LEAK": {"alloc_fn"},
            "FORWARD_NULL": {"assign_zero", "var_compare_op"}, "UNINIT": {"var_decl"}}


def _prune_calls(events: list[dict], checker: str = "", extra: str = "") -> list[dict]:
    """Calls whose callee events hold nothing that matters for the checker are left out (Coverity
    shows a called function only when something happens in it); a kept call takes the tag of what
    happens. For NULL_RETURNS only the call of the function that returns NULL is kept."""
    out: list[dict] = []
    i = 0
    wanted = RELEVANT.get(checker)
    while i < len(events):
        e = events[i]
        if e["tag"] == "call":
            j = i + 1
            while j < len(events) and events[j]["depth"] > e["depth"]:
                j += 1
            inner = events[i + 1:j]
            call_name = re.search(r'"([^"]+)"', e["desc"]).group(1)
            key = [x for x in inner if x["tag"] not in ("path", "call") and (wanted is None or x["tag"] in wanted)]
            if not key or (checker == "NULL_RETURNS" and call_name != extra):
                i = j
                continue
            name = re.search(r'"([^"]+)"', e["desc"]).group(1)
            first = key[0]
            if first["tag"] == "return_null":
                e = dict(e, tag="returned_null", desc=f'"{name}" returns "NULL".')
            elif first["tag"] == "freed_arg":
                obj = re.search(r'frees "([^"]+)"', first["desc"])
                e = dict(e, tag="freed_arg", desc=f'"{name}" frees "{obj.group(1) if obj else ""}".')
            elif first["tag"] == "alloc_fn":
                e = dict(e, tag="alloc_fn", desc=f'Storage is returned from allocation function "{name}".')
            else:
                e = dict(e, tag=first["tag"])
            out.append(e)
            out.extend(_prune_calls(inner, checker, extra))
            i = j
            continue
        out.append(e)
        i += 1
    return out


def main_event(checker: str, extra: str, msg: str, file: str, line: int, depth: int, ctx: Context,
               col: int | None = None, var: str | None = None) -> dict:
    text = ctx.src(file).line(line)
    call = ctx.callee(text, col)
    if checker == "NULL_RETURNS":
        if var is None:
            found = re.search(r"([\w.]+(?:->[\w.]+)*)\s*(?:->|\[)", text)
            var = found.group(1) if found else extra
        raw_call = _callee(text, col)
        tail = (f' when calling "{call}"' if raw_call and
                re.search(rf"{re.escape(raw_call)}\s*\([^;]*\b{re.escape(var.split('->')[0].split('.')[0])}\b", text) else "")
        return ev("dereference", f'Dereferencing a pointer that might be "NULL" "{var}"{tail}.', file, line, depth, True)
    if checker == "FORWARD_NULL":
        if call and "nonnull" in msg:
            arg = re.search(rf"{re.escape(call)}\s*\(\s*([\w\->.\[\]]+)", text)
            return ev("var_deref_model", f'Passing null pointer "{arg.group(1) if arg else extra}" to "{call}", which '
                      "dereferences it.", file, line, depth, True)
        return ev("var_deref_op", f'Dereferencing null pointer "{extra}".', file, line, depth, True)
    if checker == "RESOURCE_LEAK":
        return ev("leaked_storage", f'Variable "{extra}" going out of scope leaks the storage it points to.', file, line,
                  depth, True)
    if checker == "USE_AFTER_FREE":
        if re.search(rf"\w+\s*\([^;]*\b{re.escape(extra)}\b", text) and not re.search(rf"\b{re.escape(extra)}\s*(->|\[)", text):
            name = ctx.callee(text) or "function"
            return ev("pass_freed_arg", f'Passing freed pointer "{extra}" as an argument to "{name}".', file, line, depth, True)
        return ev("use_after_free", f'Using freed pointer "{extra}".', file, line, depth, True)
    if checker == "DIVIDE_BY_ZERO":
        m = re.search(r"([\w\->.\[\]]+\s*[/%]\s*[\w\->.\[\]]+)", text)
        expr = ctx.expand(m.group(1)) if m else "..."
        return ev("divide_by_zero", f'In expression "{expr}", division by expression "{extra}" which may be zero has '
                  "undefined behavior.", file, line, depth, True)
    if checker == "UNINIT":
        if call and re.search(rf"{re.escape(call)}\s*\([^;]*\b{re.escape(extra)}\b", text):
            return ev("uninit_use_in_call", f'Using uninitialized value "{extra}" when calling "{call}".', file, line,
                      depth, True)
        return ev("uninit_use", f'Using uninitialized value "{extra}".', file, line, depth, True)
    if checker == "UNUSED_VALUE":
        rhs = _rhs(text, extra) or "..."
        fn = ctx.function_at(file, line)
        rest = "\n".join(ctx.src(file).lines[line:fn.close_line]) if fn else ""
        how = ("overwritten before it can be used" if re.search(rf"\b{re.escape(extra)}\s*=[^=]", rest)
               else "not used before it goes out of scope")
        return ev("assigned_value", f'Assigning value from "{rhs}" to "{extra}" here, but that stored value is {how}.',
                  file, line, depth, True)
    if checker == "REVERSE_INULL":
        return ev("check_after_deref", f'Null-checking "{extra}" suggests that it may be null, but it has already been '
                  "dereferenced on all paths leading to the check.", file, line, depth, True)
    return ev(checker.lower(), msg, file, line, depth, True)


# ---- gcc -----------------------------------------------------------------------------------------

def from_gcc(f: RawFinding, ctx: Context, parse_warnings: bool) -> Finding | None:
    rule, msg = f.rule, f.message
    src = ctx.src(f.file)
    if rule == "-Wimplicit-fallthrough=":
        fn = src.function_at(f.line)
        nxt = next((n for n in range(f.line + 1, min(f.line + 12, len(src.lines) + 1))
                    if re.match(r"\s*(case\b|default\s*:)", src.line(n))), f.line + 1)
        label = next((n for n in range(f.line, max(0, f.line - 30), -1) if re.match(r"\s*case\b", src.line(n))), f.line)
        value = re.match(r"\s*case\s+([^:]+):", src.line(label))
        events = [ev("unterminated_case", f'The case for value "{value.group(1).strip() if value else "?"}" is not '
                     'terminated by a "break" statement.', f.file, label),
                  ev("fallthrough", "The above case falls through to this one.", f.file, nxt, main=True)]
        return Finding("MISSING_BREAK", "none", "", f.file, nxt, events, "gcc", fn)
    if rule in ("-Wunused-but-set-variable", "-Wunused-variable", "-Wunused-function"):
        if not parse_warnings:
            return None
        name = _quote_var(msg) or ""
        if rule == "-Wunused-but-set-variable":
            checker, tag, desc = "PW.SET_BUT_NOT_USED", "set_but_not_used", f'variable "{name}" was set but never used'
        else:
            what = "function" if rule == "-Wunused-function" else "variable"
            checker, tag, desc = ("PW.DECLARED_BUT_NOT_REFERENCED", "declared_but_not_referenced",
                                  f'{what} "{name}" was declared but never referenced')
        line_text = src.line(f.line) + "\n"
        events = [ev(tag, desc, f.file, f.line, main=True),
                  ev("caretline", " " * max(0, f.col - 1) + "^", f.file, f.line)]
        out = Finding(checker, "none", line_text, f.file, f.line, events, "gcc", None, has_subtype=False,
                      type="unclassified_parse_warning")
        return out
    if not rule.startswith("-Wanalyzer-"):
        return None
    events = [e for e in _trim(list(f.path)) if e.line > 0]
    var = _quote_var(msg) or ""
    returns_null = [e for e in events if re.search(r"could return NULL|returns NULL", e.message)
                    and "non-NULL" not in e.message]
    return_of = [e for e in events if re.match(rf"return of NULL to {Q}\w+{Q} from {Q}\w+{Q}", e.message)]
    if rule in ("-Wanalyzer-null-dereference", "-Wanalyzer-possible-null-dereference",
                "-Wanalyzer-possible-null-argument", "-Wanalyzer-null-argument"):
        if return_of:
            checker = "NULL_RETURNS"
            extra = re.findall(rf"{Q}(\w+){Q}", return_of[-1].message)[-1]
        elif returns_null:
            checker = "NULL_RETURNS"
            e0 = returns_null[0]
            name = re.search(rf"{Q}(\w+){Q} returns NULL", e0.message)
            extra = name.group(1) if name else (_callee(ctx.src(e0.file).line(e0.line), e0.col) or "")
        else:
            checker, extra = "FORWARD_NULL", var
    elif rule in ("-Wanalyzer-malloc-leak", "-Wanalyzer-file-leak", "-Wanalyzer-fd-leak"):
        checker, extra = "RESOURCE_LEAK", var
    elif rule in ("-Wanalyzer-use-after-free", "-Wanalyzer-double-free", "-Wanalyzer-double-fclose"):
        checker, extra = "USE_AFTER_FREE", var
    elif rule == "-Wanalyzer-use-of-uninitialized-value":
        checker, extra = "UNINIT", var
    elif rule == "-Wanalyzer-deref-before-check":
        checker, extra = "REVERSE_INULL", var
    elif rule == "-Wanalyzer-out-of-bounds":
        checker, extra = "OVERRUN", var
    elif rule == "-Wanalyzer-mismatching-deallocation":
        checker, extra = "ALLOC_FREE_MISMATCH", var
    elif rule == "-Wanalyzer-tainted-array-index":
        checker, extra = "TAINTED_SCALAR", var
    else:
        return None
    sub = "double_free" if rule == "-Wanalyzer-double-free" else "none"
    out: list[dict] = []
    for idx, e in enumerate(events):
        m, file, line, depth = e.message, e.file, e.line, e.depth
        text = ctx.src(file).line(line)
        if idx == len(events) - 1:
            out.append(main_event(checker, extra, msg, file, line, depth, ctx, e.col, var=var or None))
            continue
        if m.startswith(("entry to", "...to here", "returning to", "capacity", "inlined call")):
            continue
        if re.fullmatch(rf"{Q}0{Q} is NULL", m):
            out.append(ev("return_null", "Explicitly returning NULL.", file, line, depth))
            continue
        mm = re.match(rf"return of NULL to {Q}\w+{Q} from {Q}(\w+){Q}", m)
        if mm:
            target = re.match(r"\s*([\w\->.]+)\s*=", text)
            if target:
                out.append(ev("var_assigned", f'Assigning: "{target.group(1)}" = "NULL" return value from '
                              f'"{mm.group(1)}".', file, line, depth))
            continue
        mm = re.match(rf"calling {Q}(\w+){Q}", m)
        if mm:
            out.append(ev("call", f'Calling "{mm.group(1)}".', file, line, depth, kind="MODEL"))
            continue
        mm = re.match(rf"following {Q}(true|false){Q} branch(?: \(when {Q}(.+){Q}\))?", m)
        if mm:
            cond = ctx.expand(mm.group(2)) if mm.group(2) else ctx.cond(file, line, e.col)
            if mm.group(2) and re.search(r"is (non-)?NULL$", mm.group(2)):
                cond = ctx.cond(file, line, e.col)
            out.append(path_ev(cond, mm.group(1), file, line, depth))
            continue
        if (re.search(r"could return NULL", m) or re.match(rf"when {Q}\w+{Q} returns NULL", m)) and checker != "NULL_RETURNS":
            continue
        if re.search(r"could return NULL", m) or re.match(rf"when {Q}\w+{Q} returns NULL", m):
            name = re.search(rf"{Q}(\w+){Q}", m)
            fn = name.group(1) if name and "when" in m else (_callee(text, e.col) or "")
            out.append(ev("returned_null", f'"{fn}" returns "NULL".', file, line, depth))
            target = re.match(r"\s*([\w\->.]+)\s*=", text)
            if target:
                out.append(ev("var_assigned", f'Assigning: "{target.group(1)}" = "NULL" return value from "{fn}".', file,
                              line, depth))
            continue
        if m in ("allocated here", "opened here"):
            fn = _callee(text, e.col) or ("fopen" if m == "opened here" else "malloc")
            out.append(ev("alloc_fn", f'Storage is returned from allocation function "{fn}".', file, line, depth))
            target = re.match(r"\s*([\w\->.]+)\s*=", text)
            if target:
                call = re.search(rf"{fn}\s*\([^;]*\)", text)
                out.append(ev("var_assign", f'Assigning: "{target.group(1)}" = storage returned from '
                              f'"{ctx.expand(call.group(0)) if call else fn}".', file, line, depth))
            continue
        if m.startswith("freed here"):
            m2 = re.search(r"\b(free|delete)\b\s*\(?\s*([\w\->.]+)", text)
            out.append(ev("freed_arg", f'"{m2.group(1) if m2 else "free"}" frees "{m2.group(2) if m2 else extra}".', file,
                          line, depth))
            continue
        if m.startswith("region created on stack here") and checker == "UNINIT" and re.search(rf"\b{re.escape(extra)}\b", text):
            out.append(ev("var_decl", f'Declaring variable "{extra}" without initializer.', file, line, depth))
            continue
        mm = re.match(rf"pointer {Q}(.+){Q} is dereferenced here", m)
        if mm:
            out.append(ev("deref_ptr", f'Directly dereferencing pointer "{mm.group(1)}".', file, line, depth))
            continue
        mm = re.match(rf"assuming {Q}(.+){Q} is NULL", m)
        if mm and checker == "FORWARD_NULL":
            out.append(ev("var_compare_op", f'Comparing "{mm.group(1)}" to null implies that "{mm.group(1)}" might be null.',
                          file, line, depth))
            continue
    if checker == "REVERSE_INULL" and not any(e["tag"] == "deref_ptr" for e in out):
        deref = next((n for n in range(f.line - 1, max(0, f.line - 20), -1)
                      if re.search(rf"\b{re.escape(extra)}\s*->", src.line(n))), None)
        if deref:
            out.insert(0, ev("deref_ptr", f'Directly dereferencing pointer "{extra}".', f.file, deref))
    return Finding(checker, sub, extra, f.file, f.line, _dedupe(_prune_calls(out, checker, extra)), "gcc")


# ---- cppcheck ------------------------------------------------------------------------------------

def _decl_line(src: Source, fn: Function | None, var: str, before: int) -> int | None:
    start = fn.open_line if fn else 1
    for n in range(start, before + 1):
        if re.search(rf"^\s*(?:static\s+|const\s+|volatile\s+)*[\w:]+[\s*]+{re.escape(var)}\s*(;|\[)", src.line(n)):
            return n
    return None


def from_cppcheck(f: RawFinding, ctx: Context, misra: bool) -> Finding | None:
    rule, msg = f.rule, f.message
    src = ctx.src(f.file)
    fn = src.function_at(f.line)
    text = src.line(f.line)
    locs = f.locations
    m = re.fullmatch(r"misra-c2012-(dir-)?(\d+)\.(\d+)", rule)
    if m:
        if not misra:
            return None
        rule_id = ("D" if m.group(1) else "") + f"{m.group(2)}.{m.group(3)}"
        from .. import values as V
        checker = V.misra_checker(rule_id)
        snippet = text.strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        desc = f'"{snippet}" violates {checker}.' if snippet else f"Violation of {checker}."
        events = [ev(V.misra_event_tag(checker), desc, f.file, f.line, main=True)]
        return Finding(checker, "none", "", f.file, f.line, events, "cppcheck", fn)
    if rule == "nullPointerRedundantCheck" and len(locs) >= 2:
        var = f.symbol or ""
        deref_line, cond_line = locs[0][1], locs[1][1]
        if deref_line < cond_line:
            events = [ev("deref_ptr", f'Directly dereferencing pointer "{var}".', f.file, deref_line),
                      main_event("REVERSE_INULL", var, msg, f.file, cond_line, 0, ctx)]
            return Finding("REVERSE_INULL", "none", var, f.file, cond_line, events, "cppcheck", src.function_at(cond_line))
        events = [ev("var_compare_op", f'Comparing "{var}" to null implies that "{var}" might be null.', f.file, cond_line),
                  ev("var_deref_op", f'Dereferencing null pointer "{var}".', f.file, deref_line, main=True)]
        return Finding("FORWARD_NULL", "none", var, f.file, deref_line, events, "cppcheck", fn)
    if rule in ("nullPointer", "nullPointerArithmetic"):
        var = f.symbol or ""
        return Finding("FORWARD_NULL", "none", var, f.file, f.line,
                       [ev("var_deref_op", f'Dereferencing null pointer "{var}".', f.file, f.line, main=True)], "cppcheck", fn)
    if rule in ("resourceLeak", "memleak", "leakReturnValNotUsed", "memleakOnRealloc"):
        var = f.symbol or (re.search(r": (\w+)$", msg).group(1) if re.search(r": (\w+)$", msg) else "")
        events = []
        if fn:
            for n in range(fn.open_line, f.line):
                mm = re.search(rf"\b{re.escape(var)}\s*=\s*(?:\([^)]*\)\s*)?(\w+)\s*\(([^;]*)\)\s*;", src.line(n))
                if mm:
                    events.append(ev("alloc_fn", f'Storage is returned from allocation function "{mm.group(1)}".', f.file, n))
                    events.append(ev("var_assign", f'Assigning: "{var}" = storage returned from "{mm.group(1)}({mm.group(2)})".',
                                     f.file, n))
                    break
        events.append(main_event("RESOURCE_LEAK", var, msg, f.file, f.line, 0, ctx))
        return Finding("RESOURCE_LEAK", "none", var, f.file, f.line, events, "cppcheck", fn)
    if rule in ("arrayIndexOutOfBounds", "arrayIndexOutOfBoundsCond", "arrayIndexThenCheck", "bufferAccessOutOfBounds",
                "negativeIndex", "outOfBounds"):
        arr = re.search(r"Array '(\w+)\[(\d+)\]' accessed at index (-?\d+)", msg) or \
            re.search(r"the array '(\w+)\[(\d+)\]' is accessed at index (-?\d+)", msg)
        idx_var = f.symbol
        if arr is None:
            mm = re.search(rf"(\w+)\s*\[\s*{re.escape(idx_var or '')}\s*\]", text) if idx_var else None
            name = mm.group(1) if mm else ""
            size = None
            for n in range(1, len(src.lines) + 1):
                dm = re.search(rf"\b{re.escape(name)}\s*\[\s*(\w+)\s*\]\s*[;=]", src.line(n)) if name else None
                if dm:
                    size = eval_macro(dm.group(1), ctx.defines) if not dm.group(1).isdigit() else int(dm.group(1))
                    break
            index = size if size is not None else "?"
        else:
            name, size, index = arr.group(1), int(arr.group(2)), int(arr.group(3))
        use = re.search(rf"\b{re.escape(name)}\s*\[\s*([^\]]+)\]", text)
        index_expr = use.group(1).strip() if use else (idx_var or "?")
        write = bool(re.search(rf"\b{re.escape(name)}\s*\[[^\]]+\](?:\.\w+)?\s*(=[^=]|\+\+|--|[-+*/|&]=)", text))
        events = []
        for loc in locs[1:]:
            cond = ctx.cond(loc[0], loc[1], loc[2])
            events.append(ev("cond_at_most", f'Checking "{cond}" implies that "{index_expr}" may be up to {index} on the '
                             f'{"true" if "<=" in cond else "false"} branch.', loc[0], loc[1]))
        if rule == "arrayIndexThenCheck":
            later = next((n for n in range(f.line, min(f.line + 3, len(src.lines) + 1))
                          if re.search(rf"\b{re.escape(index_expr)}\s*<", src.line(n))), f.line)
            at = re.search(rf"\b{re.escape(index_expr)}\s*<", src.line(later))
            check = ctx.cond(f.file, later, at.start() + 1 if at else None)
            events.append(ev("cond_at_least", f'Checking "{check}" implies that "{index_expr}" may be {index} or more on '
                             "the false branch.", f.file, later))
        events.append(ev("overrun-local", f'Overrunning array "{name}" of {size} elements at element index {index} using '
                         f'index "{index_expr}" (which evaluates to {index}).', f.file, f.line, main=True))
        return Finding("OVERRUN", "write" if write else "read", name, f.file, f.line, events, "cppcheck", fn)
    if rule in ("uninitvar", "uninitdata", "uninitStructMember"):
        var = f.symbol or (re.search(r": (\w+)$", msg).group(1) if re.search(r": (\w+)$", msg) else "")
        events = []
        decl = _decl_line(src, fn, var, f.line)
        if decl:
            events.append(ev("var_decl", f'Declaring variable "{var}" without initializer.', f.file, decl))
        for loc in locs[1:]:
            events.append(path_ev(ctx.cond(loc[0], loc[1], loc[2]), "false", loc[0], loc[1]))
        events.append(main_event("UNINIT", var, msg, f.file, f.line, 0, ctx))
        return Finding("UNINIT", "none", var, f.file, f.line, events, "cppcheck", fn)
    if rule in ("uninitMemberVar", "uninitMemberVarPrivate"):
        parts = (f.symbol or "").split("::")
        member, cls = parts[-1], (parts[-2].strip() if len(parts) > 1 else "")
        events = []
        for path, s in list(ctx.sources.items()):
            cm = re.search(rf"\b(?:class|struct)\s+{re.escape(cls)}\b[^;{{]*\{{", s.code) if cls else None
            if not cm:
                continue
            from .csource import match_close
            close = match_close(s.code, cm.end() - 1)
            dm = re.search(rf"(?m)^[ \t]*(?:[\w:<>]+[ \t]+)+[*&]*[ \t]*{re.escape(member)}\s*(?:\[[^\]]*\])?\s*;",
                           s.code[cm.end():close])
            if dm:
                n = s.code.count("\n", 0, cm.end() + dm.start()) + 1
                events.append(ev("member_decl", f'Class member declaration for "{member}".', path, n))
                break
        events.append(ev("uninit_member", f'Non-static class member "{member}" is not initialized in this constructor nor '
                         "in any functions that it calls.", f.file, fn.close_line if fn else f.line, main=True))
        return Finding("UNINIT_CTOR", "none", member, f.file, fn.close_line if fn else f.line, events, "cppcheck", fn)
    if rule in ("zerodiv", "zerodivcond"):
        mm = re.search(r"([\w\->.\[\]]+)\s*[/%]\s*([\w\->.\[\]]+)", text)
        divisor = mm.group(2) if mm else ""
        events = []
        for loc in locs[1:]:
            events.append(path_ev(ctx.cond(loc[0], loc[1], loc[2]), "true", loc[0], loc[1]))
        events.append(main_event("DIVIDE_BY_ZERO", divisor, msg, f.file, f.line, 0, ctx))
        return Finding("DIVIDE_BY_ZERO", "none", divisor, f.file, f.line, events, "cppcheck", fn)
    if rule == "mismatchAllocDealloc":
        member = (f.symbol or "").split("::")[-1].strip()
        events = []
        for n in range(1, len(src.lines) + 1):
            mm = re.search(rf"\b{re.escape(member)}\s*(?:=\s*|\(\s*)(new\b[^;]*?\[[^\]]*\])", src.line(n))
            if mm:
                events.append(ev("alloc_fn", 'Storage is returned from allocation function "operator new[]".', f.file, n))
                events.append(ev("var_assign", f'Assigning: "this->{member}" = storage returned from '
                                 f'"{mm.group(1)}".', f.file, n))
                break
        events.append(ev("free", f'Calling "operator delete" frees "this->{member}" using "operator delete" but it should '
                         'have been freed using "operator delete[]".', f.file, f.line, main=True))
        return Finding("ALLOC_FREE_MISMATCH", "none", member, f.file, f.line, events, "cppcheck", fn)
    if rule in ("doubleFree", "deallocuse"):
        var = f.symbol or ""
        return Finding("USE_AFTER_FREE", "double_free" if rule == "doubleFree" else "none", var, f.file, f.line,
                       [main_event("USE_AFTER_FREE", var, msg, f.file, f.line, 0, ctx)], "cppcheck", fn)
    if rule == "selfAssignment":
        return Finding("NO_EFFECT", "self_assign", "", f.file, f.line,
                       [ev("self_assign", f'Assignment operation "{text.strip().rstrip(";")}" has no effect.', f.file, f.line,
                           main=True)], "cppcheck", fn)
    if rule in ("unsignedLessThanZero", "unsignedPositive"):
        return Finding("NO_EFFECT", "unsigned_compare", "", f.file, f.line,
                       [ev("unsigned_compare", f'This less-than-zero comparison of an unsigned value is never true. '
                           f'"{ctx.cond(f.file, f.line)}".', f.file, f.line, main=True)], "cppcheck", fn)
    if rule == "knownConditionTrueFalse":
        cond = ctx.cond(f.file, f.line, f.col)
        return Finding("DEADCODE", "none", "", f.file, f.line,
                       [ev("dead_error_condition", f'The condition "{cond}" cannot be true.', f.file, f.line),
                        ev("dead_error_begin", "Execution cannot reach this statement.", f.file, f.line + 1, main=True)],
                       "cppcheck", fn)
    return None
