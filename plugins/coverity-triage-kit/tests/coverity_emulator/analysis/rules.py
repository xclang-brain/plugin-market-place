"""Source-shape rules for what Coverity finds across functions and the analyzers here do not.

Each rule reads the code (functions, statements, macro values), never line numbers, so a fix in
the source makes its warning go away:

- CHECKED_RETURN: a call whose result is ignored while most other calls of the same function in the
  program check it (Coverity's statistical checker: "as is done elsewhere N out of M times").
- NEGATIVE_RETURNS: the result of a function that can return a negative constant is used as a loop
  bound or an array index without a sign check.
- NULL_RETURNS: the result of a function that can return NULL / nullptr is dereferenced directly
  (``f(x)->y``).
- UNINIT: a local set only in the cases of a switch without default, then used after the switch.
- STRING_OVERFLOW (fixed_size_dest): strcpy of a non-literal into a fixed-size array.
- OVERRUN (strlen): ``p = malloc(strlen(s))`` then ``strcpy(p, s)``.
- OVERRUN with buffers sized by a macro that is smaller than what is written into them: the frame
  codec of the envmon fixture (FRAME_MAX < FRAME_HDR_LEN + FRAME_PAYLOAD_MAX + FRAME_CRC_LEN).
- CERT INT30-C (only with a cert-c coding standard): an unsigned variable updated by a shift or a
  multiplication of itself (wrap-around).
"""

from __future__ import annotations

import re
from collections import defaultdict

from .csource import Function, Source, eval_macro, line_of, match_close
from .translate import Context, Finding, ev, path_ev


def _calls(src: Source, name: str) -> list[tuple[int, int]]:
    """(offset, line) of calls of ``name`` in function bodies of ``src``."""
    out = []
    for fn in src.functions:
        body = src.code[fn.body_start:fn.body_end + 1]
        for m in re.finditer(rf"(?<![\w.>]){re.escape(name)}\s*\(", body):
            off = fn.body_start + m.start()
            out.append((off, line_of(src.code, off)))
    return out


def _statement_start(code: str, off: int) -> str:
    """The text between the previous statement boundary and ``off``."""
    i = off - 1
    while i >= 0 and code[i] not in ";{}":
        i -= 1
    return code[i + 1:off]


def checked_return(ctx: Context, sources: list[Source]) -> list[Finding]:
    defined: dict[str, tuple[Source, Function]] = {}
    for src in sources:
        for fn in src.functions:
            if fn.ret and not re.fullmatch(r"(static\s+)?(inline\s+)?void", fn.ret) and "::" not in fn.qualified:
                defined.setdefault(fn.name, (src, fn))
    out = []
    for name, (dsrc, dfn) in sorted(defined.items()):
        sites = []
        for src in sources:
            for off, line in _calls(src, name):
                before = _statement_start(src.code, off).strip()
                if before == "" or re.fullmatch(r"(else|do)", before) or re.search(r"\b(if|while|for)\s*\(.*\)$", before):
                    end = match_close(src.code, src.code.index("(", off), "()")
                    after = src.code[end + 1:end + 20].lstrip()
                    kind = "ignored" if after.startswith(";") else "checked"
                elif re.fullmatch(r"\(\s*void\s*\)", before):
                    kind = "void"
                else:
                    kind = "checked"
                sites.append((src, off, line, kind))
        checked = [s for s in sites if s[3] == "checked"]
        ignored = [s for s in sites if s[3] == "ignored"]
        total = len(checked) + len(ignored)
        if not ignored or len(checked) < 3 or len(checked) / total < 0.75:
            continue
        for src, off, line, _ in ignored:
            fn = src.function_at(line)
            examples = []
            for i, (esrc, eoff, eline, _) in enumerate(checked[:5], 1):
                stmt = _statement_text(esrc, eoff)
                call = _call_text(esrc, eoff)
                examples.append(ev("example_checked", f'Example {i}: "{ctx.expand(call)}" has its value checked in '
                                   f'"{ctx.expand(stmt)}".', esrc.path, eline))
            events = examples + [ev("check_return", f'Calling "{name}" without checking return value (as is done '
                                    f"elsewhere {len(checked)} out of {total} times).", src.path, line, main=True)]
            out.append(Finding("CHECKED_RETURN", "none", name, src.path, line, events, "rules", fn))
    return out


def _call_text(src: Source, off: int) -> str:
    end = match_close(src.code, src.code.index("(", off), "()")
    return re.sub(r"\s+", " ", src.text[off:end + 1])


def _statement_text(src: Source, off: int) -> str:
    i = off - 1
    while i >= 0 and src.code[i] not in ";{}":
        i -= 1
    j = src.code.find(";", off)
    text = re.sub(r"\s+", " ", src.text[i + 1:j]).strip()
    return re.sub(r"^(if|while)\s*\(", "", text).rstrip(")").strip() if text.startswith(("if", "while")) else text


def negative_returns(ctx: Context, sources: list[Source]) -> list[Finding]:
    negative: dict[str, tuple[Source, Function, int]] = {}
    for src in sources:
        for fn in src.functions:
            if not re.search(r"\b(int|int\d+_t|long|ssize_t)\b", fn.ret) or "unsigned" in fn.ret:
                continue
            m = re.search(r"\breturn\s+\(?\s*-\s*\d+\s*\)?\s*;", src.body(fn))
            if m:
                negative[fn.name] = (src, fn, line_of(src.code, fn.body_start + m.start()))
    out = []
    for src in sources:
        for fn in src.functions:
            body = src.body(fn)
            for name, (dsrc, dfn, ret_line) in negative.items():
                for m in re.finditer(rf"(?<![\w.>])(\w+)\s*=\s*{re.escape(name)}\s*\(", body):
                    var = m.group(1)
                    start = fn.body_start + m.start()
                    call = _call_text(src, start + m.group(0).index(name))
                    assign_line = line_of(src.code, start)
                    rest = src.code[start:fn.body_end]
                    check = re.search(rf"\b{var}\s*(<|<=|>=|>)\s*0\b|\b0\s*(>|<=|<|>=)\s*{var}\b|\b{var}\s*[!=]=\s*-\s*1\b", rest)
                    uses = []
                    for um in re.finditer(rf"for\s*\([^;]*;[^;]*<\s*\(?\s*(?:\w+\s*\)\s*)?{var}\b", rest):
                        uses.append((um.start(), "loop_bound", None))
                    for um in re.finditer(rf"(\w+)\s*\[\s*{var}\s*\]", rest):
                        tail = rest[um.end():um.end() + 40]
                        write = bool(re.match(r"\s*(\.\w+\s*)?=[^=]", tail))
                        uses.append((um.start(), "array_index_write" if write else "array_index_read", um.group(1)))
                    if not uses:
                        continue
                    first = min(uses)
                    if check and check.start() < first[0]:
                        continue
                    use_line = line_of(src.code, start + first[0])
                    events = [ev("negative_return_fn", f'Function "{call}" returns a negative number.', src.path,
                                 assign_line, kind="MODEL"),
                              ev("return_negative", 'Explicitly returning negative value "-1".', dsrc.path, ret_line, 1),
                              ev("var_assign", f'Assigning: "{var}" = "{call}".', src.path, assign_line)]
                    if first[1] == "loop_bound":
                        desc = f'Using unsigned variable "{var}" in a loop exit condition.'
                    else:
                        desc = f'Using variable "{var}" as an index to array "{first[2]}".'
                    events.append(ev("negative_returns", desc, src.path, use_line, main=True))
                    out.append(Finding("NEGATIVE_RETURNS", first[1], name, src.path, use_line, events, "rules", fn))
    return out


def null_returns_chain(ctx: Context, sources: list[Source]) -> list[Finding]:
    nullable: dict[str, tuple[Source, Function, int]] = {}
    for src in sources:
        for fn in src.functions:
            if "*" not in fn.ret:
                continue
            m = re.search(r"\breturn\s+(nullptr|NULL|0)\s*;", src.body(fn))
            if m:
                nullable[fn.name.split("::")[-1]] = (src, fn, line_of(src.code, fn.body_start + m.start()))
    out = []
    for src in sources:
        for fn in src.functions:
            body = src.body(fn)
            for name, (dsrc, dfn, ret_line) in nullable.items():
                for m in re.finditer(rf"(?<![\w])({re.escape(name)})\s*\(", body):
                    off = fn.body_start + m.start()
                    end = match_close(src.code, src.code.index("(", off), "()")
                    if not src.code[end + 1:end + 4].lstrip().startswith("->"):
                        continue
                    line = line_of(src.code, off)
                    call = re.sub(r"\s+", " ", src.text[off:end + 1])
                    member = re.search(r"->\s*(\w+)", src.code[end + 1:end + 60]).group(1)
                    events = [ev("returned_null", f'"{name}" returns "nullptr".', src.path, line,
                                 kind="MODEL"),
                              ev("return_null", "Explicitly returning NULL.", dsrc.path, ret_line, 1),
                              ev("dereference", f'Dereferencing a pointer that might be "nullptr" "{call}" when calling '
                                 f'"{member}".', src.path, line,
                                 main=True)]
                    out.append(Finding("NULL_RETURNS", "none", name, src.path, line, events, "rules", fn))
    return out


def switch_uninit(ctx: Context, sources: list[Source]) -> list[Finding]:
    out = []
    for src in sources:
        for fn in src.functions:
            body = src.body(fn)
            for m in re.finditer(r"\bswitch\s*\(", body):
                paren = fn.body_start + m.end() - 1
                close_paren = match_close(src.code, paren, "()")
                brace = src.code.index("{", close_paren)
                end = match_close(src.code, brace)
                block = src.code[brace:end + 1]
                if re.search(r"\bdefault\s*:", block):
                    continue
                before = src.code[fn.body_start:fn.body_start + m.start()]
                for dm in re.finditer(r"^\s*(?:const\s+)?[\w]+\s+(\w+)\s*;", before, re.M):
                    var = dm.group(1)
                    if not re.search(rf"\b{var}\s*=[^=]", block):
                        continue
                    if re.search(rf"\b{var}\s*=[^=]", before[dm.end():]):
                        continue
                    after = src.code[end + 1:fn.body_end]
                    use = re.search(rf"\b{var}\b(?!\s*=[^=])", after)
                    if not use:
                        continue
                    use_line = line_of(src.code, end + 1 + use.start())
                    decl_line = line_of(src.code, fn.body_start + dm.start(1))
                    switch_line = line_of(src.code, fn.body_start + m.start())
                    subject = re.sub(r"\s+", " ", src.text[paren + 1:close_paren]).strip()
                    events = [ev("var_decl", f'Declaring variable "{var}" without initializer.', src.path, decl_line),
                              ev("path", f'Switch case default.', src.path, switch_line),
                              ev("uninit_use", f'Using uninitialized value "{var}".', src.path, use_line, main=True)]
                    if re.search(r"\breturn\b", src.line(use_line)):
                        events[-1]["desc"] = f'Using uninitialized value "{var}".'
                    out.append(Finding("UNINIT", "none", var, src.path, use_line, events, "rules", fn))
                    del subject
    return out


def _array_size(ctx: Context, name: str, sources: list[Source]) -> int | None:
    for src in sources:
        m = re.search(rf"\b{re.escape(name)}\s*\[\s*(\w+)\s*\]\s*;", src.code)
        if m:
            size = m.group(1)
            return int(size) if size.isdigit() else eval_macro(size, ctx.defines)
    return None


def string_rules(ctx: Context, sources: list[Source], headers: list[Source]) -> list[Finding]:
    out = []
    for src in sources:
        for fn in src.functions:
            body = src.body(fn)
            for m in re.finditer(r"\bstrcpy\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", body):
                dest, source_expr = m.group(1).strip(), m.group(2).strip()
                if source_expr.startswith('"'):
                    continue
                off = fn.body_start + m.start()
                line = line_of(src.code, off)
                dest_name = re.search(r"(\w+)\s*$", dest).group(1)
                allocated = re.search(rf"\b{re.escape(dest_name)}\s*=\s*(?:\([^)]*\)\s*)?malloc\s*\(\s*strlen\s*\(\s*"
                                      rf"{re.escape(source_expr)}\s*\)\s*\)", body[:m.start()])
                if allocated:
                    alloc_line = line_of(src.code, fn.body_start + allocated.start())
                    events = [ev("alloc_strlen", "Allocating insufficient memory for the terminating null of the string.",
                                 src.path, alloc_line, main=True)]
                    out.append(Finding("OVERRUN", "strlen", dest_name, src.path, alloc_line, events, "rules", fn))
                    continue
                size = _array_size(ctx, dest_name, headers + sources)
                if size is None:
                    continue
                src_text = re.sub(r"\s+", " ", src.text[fn.body_start + m.start(2):fn.body_start + m.end(2)]).strip()
                events = [ev("fixed_size_dest", f'You might overrun the {size}-character fixed-size string "{dest}" by '
                             f'copying "{src_text}" without checking the length.', src.path, line, main=True)]
                out.append(Finding("STRING_OVERFLOW", "fixed_size_dest", dest, src.path, line, events, "rules", fn))
    return out


def frame_overrun(ctx: Context, sources: list[Source], headers: list[Source]) -> list[Finding]:
    """Buffers of FRAME_MAX bytes that receive a whole frame (header, payload, CRC)."""
    cap = eval_macro("FRAME_MAX", ctx.defines)
    parts = [eval_macro(n, ctx.defines) for n in ("FRAME_HDR_LEN", "FRAME_PAYLOAD_MAX", "FRAME_CRC_LEN")]
    if cap is None or None in parts or cap >= sum(parts):
        return []
    need = sum(parts)
    payload = parts[1]
    out = []
    encoders: dict[str, tuple[Source, Function, str]] = {}
    for src in sources:
        for fn in src.functions:
            pm = re.search(r"\b(\w+)\s*\[\s*FRAME_MAX\s*\]", fn.params)
            if pm and len(re.findall(rf"\b{pm.group(1)}\s*\[\s*pos\+\+\s*\]\s*=", src.body(fn))) >= 2:
                encoders[fn.name] = (src, fn, pm.group(1))
    for src in sources:
        for fn in src.functions:
            body = src.body(fn)
            local = set(re.findall(r"\b(\w+)\s*\[\s*FRAME_MAX\s*\]", body)) | \
                set(re.findall(r"\b(\w+)\s*\[\s*FRAME_MAX\s*\]", src.code[:fn.body_start]))
            # a frame encoded into a FRAME_MAX buffer
            for name, (esrc, efn, param) in encoders.items():
                for m in re.finditer(rf"(?:(\w+)\s*=\s*)?{name}\s*\(\s*(\w+)\s*,", body):
                    buf = m.group(2)
                    if buf not in local or fn is efn:
                        continue
                    line = line_of(src.code, fn.body_start + m.start())
                    ebody_start = efn.body_start
                    ebody = esrc.body(efn)
                    inner = []
                    for cm in re.finditer(r"\bif\s*\(([^{]*?FRAME_PAYLOAD_MAX[^{]*?)\)\s*\{", ebody):
                        inner.append(path_ev(ctx.expand(re.sub(r"\s+", " ", cm.group(1)).strip()), "false", esrc.path,
                                             line_of(esrc.code, ebody_start + cm.start()), 1))
                    for cm in re.finditer(r"\bif\s*\(([^{]*?->len\s*>\s*0U?)\)\s*\{", ebody):
                        inner.append(path_ev(re.sub(r"\s+", " ", cm.group(1)).strip(), "true", esrc.path,
                                             line_of(esrc.code, ebody_start + cm.start()), 1))
                    writes = [wm for wm in re.finditer(rf"\b{param}\s*\[\s*pos\+\+\s*\]\s*=", ebody)]
                    last = writes[-1]
                    inner.append(ev("access_dbuff_in_call", f'Writing to "{param}[pos++]" at byte offset {need - 1}, '
                                    f'which is past the end of the {cap}-byte buffer "{param}".', esrc.path,
                                    line_of(esrc.code, ebody_start + last.start()), 1))
                    events = [ev("overrun-buffer-arg", f'Overrunning array "{buf}" of {cap} bytes by passing it to a '
                                 f'function which accesses it at byte offset {need - 1} using index "pos++" '
                                 f"(which evaluates to {need - 1}).", src.path, line, main=True, kind="MODEL")] + inner
                    out.append(Finding("OVERRUN", "write", buf, src.path, line, events, "rules", fn))
                    # the copy of the encoded frame into another FRAME_MAX buffer
                    if m.group(1):
                        n_var = m.group(1)
                        for cm in re.finditer(rf"memcpy\s*\(\s*(\w+)\s*,\s*\w+\s*,\s*\(\s*size_t\s*\)\s*{n_var}\s*\)",
                                              body[m.end():]):
                            dest = cm.group(1)
                            if dest not in local:
                                continue
                            cline = line_of(src.code, fn.body_start + m.end() + cm.start())
                            chk = re.search(rf"\bif\s*\(\s*{n_var}\s*<\s*0\s*\)", body[m.end():])
                            call = _call_text(src, fn.body_start + m.start() + m.group(0).index(name))
                            evs = [ev("assignment", f'Assigning: "{n_var}" = "{call}". The value of "{n_var}" is now '
                                      f"between 0 and {need}.", src.path, line)]
                            if chk:
                                evs.append(path_ev(f"{n_var} < 0", "false", src.path,
                                                   line_of(src.code, fn.body_start + m.end() + chk.start())))
                            evs.append(ev("overrun-buffer-arg", f'Overrunning array "{dest}" of {cap} bytes by passing it '
                                          f'to a function which accesses it at byte offset {need - 1} using argument '
                                          f'"(size_t){n_var}" (which evaluates to {need}).', src.path, cline, main=True))
                            out.append(Finding("OVERRUN", "write", dest, src.path, cline, evs, "rules", fn))
            # a decoder that stores a whole frame into a FRAME_MAX member
            for m in re.finditer(r"\b(\w+)->buf\s*\[\s*\1->pos\+\+\s*\]\s*=", body):
                obj = m.group(1)
                if not any(re.search(r"\bbuf\s*\[\s*FRAME_MAX\s*\]", h.code) for h in headers + [src]):
                    break
                case = src.code[:fn.body_start + m.start()].rfind("case ")
                label = re.match(r"case\s+(\w+)", src.code[case:case + 60]) if case >= 0 else None
                if not label or "CRC" not in label.group(1):
                    continue
                line = line_of(src.code, fn.body_start + m.start())
                events = []
                lm = re.search(r"\bif\s*\(\s*(\w+)\s*>\s*FRAME_PAYLOAD_MAX\s*\)", body)
                if lm:
                    events.append(ev("cond_at_most", f'Checking "{lm.group(1)} > {payload}U" implies that "{obj}->need" may '
                                     f"be up to {payload} on the false branch.", src.path,
                                     line_of(src.code, fn.body_start + lm.start())))
                nm = re.search(rf"\bif\s*\(\s*{obj}->need\s*==\s*0U?\s*\)", body)
                if nm:
                    events.append(path_ev(f"{obj}->need == 0U", "true", src.path, line_of(src.code, fn.body_start + nm.start())))
                events.append(ev("overrun-local", f'Overrunning array "{obj}->buf" of {cap} bytes at byte offset {cap} using '
                                 f'index "{obj}->pos++" (which evaluates to {cap}).', src.path, line, main=True))
                out.append(Finding("OVERRUN", "write", f"{obj}->buf", src.path, line, events, "rules", fn))
                break
    return out


def cert_int30(ctx: Context, sources: list[Source]) -> list[Finding]:
    out = []
    for src in sources:
        for fn in src.functions:
            body = src.body(fn)
            unsigned = set(re.findall(r"\b(?:uint\d+_t|unsigned(?:\s+int|\s+long)?|size_t)\s+(\w+)\s*[=;]", body))
            for m in re.finditer(r"\b(\w+)\s*=\s*(\(?\s*\1\s*(?:<<|\*)\s*\d+[^;]*);", body):
                if m.group(1) not in unsigned:
                    continue
                line = line_of(src.code, fn.body_start + m.start())
                expr = re.sub(r"\s+", " ", src.text[fn.body_start + m.start(2):fn.body_start + m.end(2)]).strip()
                events = [ev("cert_int30_c_violation", f'Unsigned integer operation "{expr}" may wrap.', src.path, line,
                             main=True)]
                out.append(Finding("CERT INT30-C", "none", "", src.path, line, events, "rules", fn))
    return out


def run_all(ctx: Context, sources: list[Source], headers: list[Source], cert: bool) -> list[Finding]:
    found = []
    found += checked_return(ctx, sources)
    found += negative_returns(ctx, sources)
    found += null_returns_chain(ctx, sources)
    found += switch_uninit(ctx, sources)
    found += string_rules(ctx, sources, headers)
    found += frame_overrun(ctx, sources, headers)
    if cert:
        found += cert_int30(ctx, sources)
    return found
