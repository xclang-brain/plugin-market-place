"""Mock cov-analyze (specification 4.2).

``cov-analyze --dir <idir> [--all] [--security] [--enable C] [--disable C] [--coding-standard-config F]
[--strip-path P] [--ticker-mode none|no-spin|spin] [-j N] ...``

Every emitted translation unit is analyzed by clang --analyze, gcc -fanalyzer and cppcheck (plus the
MISRA addon when a MISRA C:2012 coding standard is configured); the source-shape rules of rules.py
add what Coverity finds across functions. The results are written to <idir>/output: the warning
trees (emulator-issues.json, read by cov-format-errors and cov-commit-defects), summary.txt and
analysis-log.txt. Exit code 0, 2 when the analysis cannot run (no translation units, bad option).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import values as V
from ..state import merge_key
from . import rules, tools, translate
from .common import (EMIT, ISSUES_FILE, OUTPUT, TUS_FILE, banner, build_tree, map_path, read_json, strip_path, ticker,
                     write_json)
from .csource import Source, read_defines, read_function_macros

VALUE_OPTIONS = {"--dir", "--strip-path", "--enable", "--disable", "--coding-standard-config", "--ticker-mode", "-j",
                 "--jobs", "--aggressiveness-level", "--tmpdir", "--config", "--checker-option", "--model-file",
                 "--user-model-file", "--rule", "--max-mem", "--export-summaries", "--enable-default-coding-standard"}
FLAG_OPTIONS = {"--all", "--security", "--concurrency", "--enable-parse-warnings", "--enable-constraint-fpp",
                "--enable-fnptr", "--enable-virtual", "--enable-callgraph-metrics", "--cpp", "--c", "--disable-default",
                "--webapp-security", "--no-banner", "--force", "--distrust-all", "--trust-all", "--android-security",
                "--enable-single-virtual", "--enable-exceptions", "--inherit-taint-from-unions", "--enable-audit-mode"}
# Which analyzer's report is kept when several report the same warning (same mergeKey).
PRIORITY = {"default": ["clang", "gcc", "cppcheck", "rules"], "RESOURCE_LEAK": ["gcc", "cppcheck", "clang", "rules"]}


def parse_args(argv: list[str]) -> dict:
    opts: dict = {"strip": [], "enable": [], "disable": [], "standards": [], "flags": set(), "ticker": "spin",
                  "jobs": os.cpu_count() or 2, "dir": None, "argv": argv}
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, value = a.partition("=")
        if name in VALUE_OPTIONS:
            if not eq:
                if i + 1 >= len(argv):
                    raise ValueError(f"Option {a} needs a value")
                value = argv[i + 1]
                i += 1
            key = {"--strip-path": "strip", "--enable": "enable", "--disable": "disable",
                   "--coding-standard-config": "standards"}.get(name)
            if key:
                opts[key].append(value)
            elif name == "--dir":
                opts["dir"] = value
            elif name == "--ticker-mode":
                if value not in ("none", "no-spin", "spin"):
                    raise ValueError(f"Invalid value for --ticker-mode: {value}")
                opts["ticker"] = value
            elif name in ("-j", "--jobs"):
                opts["jobs"] = os.cpu_count() or 2 if value == "auto" else max(1, int(value))
        elif name in FLAG_OPTIONS:
            opts["flags"].add(name)
        elif re.fullmatch(r"-j\d+", a):
            opts["jobs"] = int(a[2:])
        else:
            raise ValueError(f"Unrecognized option: {a}")
        i += 1
    if not opts["dir"]:
        raise ValueError("Missing required option --dir")
    return opts


def enabled_checkers(opts: dict) -> set[str]:
    flags = opts["flags"]
    enabled = set() if "--disable-default" in flags else set(V.DEFAULT_CHECKERS)
    if "--all" in flags or "--security" in flags:
        enabled |= V.SECURITY_CHECKERS
    if "--all" in flags or "--enable-parse-warnings" in flags:
        enabled |= V.PARSE_WARNING_CHECKERS
    for item in opts["enable"]:
        enabled |= {c.strip().upper() for c in item.split(",")}
    for item in opts["disable"]:
        enabled -= {c.strip().upper() for c in item.split(",")}
    return enabled


def standards(opts: dict) -> set[str]:
    out = set()
    for path in opts["standards"]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        out.add(str(data.get("standard", "")).lower())
    return out


def _run(task: tuple[dict, str, bool]) -> tuple[list[tools.RawFinding], list[str]]:
    tu, tool, misra = task
    log: list[str] = []
    if tool == "clang":
        return tools.run_clang(tu, log), log
    if tool == "gcc":
        return tools.run_gcc(tu, log), log
    return tools.run_cppcheck(tu, log, misra), log


def analyze(opts: dict, out=sys.stdout) -> int:
    started = time.time()
    idir = Path(opts["dir"]).resolve()
    data = read_json(idir / EMIT / TUS_FILE, {}) or {}
    tus = [t for t in data.get("tus", []) if t.get("emitted")]
    out.write(banner("Static Analysis"))
    if not tus:
        out.write("Error: intermediate directory contains no translation units.\n")
        return 2
    try:
        coding = standards(opts)
    except (OSError, ValueError) as exc:
        out.write(f"[ERROR] Cannot read the coding standard configuration: {exc}\n")
        return 2
    misra = "misrac2012" in coding
    cert = bool({"cert-c", "cert-c-recommendation"} & coding)
    enabled = enabled_checkers(opts)
    mapping = [tuple(m) for m in data.get("pathMap", [])]
    t = opts["ticker"]
    out.write(f"Using {opts['jobs']} workers as limited by CPU(s)\nLooking for translation units\n{ticker(t)}")
    out.write(f"[STATUS] Computing links for {len(tus)} translation units\n{ticker(t)}")
    out.write(f"[STATUS] Computing virtual overrides\n{ticker(t)}[STATUS] Computing callgraph\n{ticker(t)}")
    out.write(f"[STATUS] Topologically sorting {len(tus)} functions\n{ticker(t)}")
    out.write(f"[STATUS] Running Sanity checks\n{ticker(t)}[STATUS] Starting analysis run\n")
    out.flush()

    tasks = [(tu, tool, misra) for tu in tus for tool in ("clang", "gcc", "cppcheck")]
    with ThreadPoolExecutor(max_workers=opts["jobs"]) as pool:
        results = list(pool.map(_run, tasks))
    log_lines: list[str] = []
    raw: list[tools.RawFinding] = []
    for findings, log in results:
        raw += findings
        log_lines += log

    include_dirs = sorted({f[2:] for tu in tus for f in tools.flags(tu) if f.startswith("-I")})
    header_paths = sorted({h["path"] for tu in tus for h in tu.get("headers", [])})
    defines = read_defines([Path(p) for p in header_paths] + [Path(tu["source"]) for tu in tus])
    ctx = translate.Context(defines, read_function_macros([Path(p) for p in header_paths]))
    sources = [ctx.src(tu["source"]) for tu in tus]
    headers = [ctx.src(p) for p in header_paths if os.path.isfile(p)]
    del include_dirs
    found: list[translate.Finding] = []
    parse_warnings = bool(V.PARSE_WARNING_CHECKERS & enabled)
    for f in raw:
        try:
            if f.tool == "clang":
                item = translate.from_clang(f, ctx)
            elif f.tool == "gcc":
                item = translate.from_gcc(f, ctx, parse_warnings)
            else:
                item = translate.from_cppcheck(f, ctx, misra)
        except Exception as exc:  # noqa: BLE001 - one odd report must not stop the analysis
            log_lines.append(f"[translate] {f.tool} {f.rule} {f.file}:{f.line}: {type(exc).__name__}: {exc}")
            continue
        if item is not None:
            found.append(item)
    try:
        found += rules.run_all(ctx, sources, headers, cert)
    except Exception as exc:  # noqa: BLE001
        log_lines.append(f"[rules] {type(exc).__name__}: {exc}")

    # Rule 2.5 is a whole-program rule; cppcheck sees one translation unit at a time
    texts = [s.text for s in sources] + [h.text for h in headers]

    def macro_used(f: translate.Finding) -> bool:
        m = re.match(r"\s*#\s*define\s+(\w+)", ctx.src(f.file).line(f.line))
        if not m:
            return False
        return sum(len(re.findall(rf"\b{m.group(1)}\b", t)) for t in texts) > 1
    found = [f for f in found if not (f.checker == "MISRA C-2012 Rule 2.5" and macro_used(f))]

    def allowed(f: translate.Finding) -> bool:
        if f.checker.startswith("MISRA C-2012 "):
            return misra
        if f.checker.startswith("CERT "):
            return cert
        return f.checker in enabled
    found = [f for f in found if allowed(f)]

    issues = build_issues(found, ctx, mapping, opts["strip"], tus)
    output = idir / OUTPUT
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / ISSUES_FILE, {"formatVersion": "emulator-1", "issues": issues})
    files = {tu["remote"]: tu["md5"] for tu in tus}
    for tu in tus:
        for h in tu.get("headers", []):
            files[h["remote"]] = h["md5"]
    seconds = time.time() - started
    write_json(output / "emulator-analysis.json", {
        "analysisVersion": os.environ.get("COV_EMU_VERSION", "2024.12.1"),
        "analysisCommandLine": "cov-analyze " + " ".join(opts["argv"]), "analysisHost": os.uname().nodename,
        "analysisIntermediateDir": str(idir), "analysisTime": int(seconds), "enabledCheckers": sorted(enabled),
        "codingStandards": sorted(coding), "stripPath": opts["strip"], "files": files,
        "buildCommandLine": data.get("buildCommandLine"), "buildHost": data.get("buildHost"),
        "buildSuccessCount": len(tus), "buildFailureCount": len([t for t in data.get("tus", []) if not t.get("emitted")]),
        "buildTime": int(data.get("buildSeconds") or 0)})
    summary = summary_text(opts, tus, sources, headers, issues, seconds)
    (output / "summary.txt").write_text(summary, encoding="utf-8")
    (output / "analysis-log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    out.write(f"[STATUS] Exporting summaries\n{ticker(t)}")
    out.write("\n".join(summary.splitlines()[1:]) + "\n")
    out.flush()
    return 0


def _pick(found: list[translate.Finding]) -> list[translate.Finding]:
    order = PRIORITY.get(found[0].checker, PRIORITY["default"])
    best = min(found, key=lambda f: order.index(f.tool) if f.tool in order else 99).tool
    seen, out = set(), []
    for f in found:
        if f.tool == best and (f.file, f.line) not in seen:
            seen.add((f.file, f.line))
            out.append(f)
    return out


def build_issues(found: list[translate.Finding], ctx: translate.Context, mapping: list, strip: list[str],
                 tus: list[dict]) -> list[dict]:
    lang_of = {tu["source"]: tu["lang"] for tu in tus}
    for tu in tus:
        for h in tu.get("headers", []):
            lang_of.setdefault(h["path"], tu["lang"])
    groups: dict[str, list[tuple[translate.Finding, dict]]] = defaultdict(list)
    for f in found:
        fn = f.function if f.function is not None else ctx.function_at(f.file, f.line)
        if f.checker.startswith("PW."):
            fn = None
        remote = map_path(f.file, mapping)
        display = fn.display if fn else None
        mk = merge_key(f.checker, display, remote, f.extra)
        groups[mk].append((f, {"fn": fn, "remote": remote, "display": display}))
    issues = []
    for mk, items in groups.items():
        kept = _pick([f for f, _ in items])
        info = {id(f): i for f, i in items}
        for f in kept:
            i = info[id(f)]
            fn = i["fn"]
            lang = lang_of.get(f.file, "C++" if f.file.endswith((".cpp", ".hpp", ".cc")) else "C")
            checker = f.checker
            issue = {
                "mergeKey": mk, "occurrenceCountForMK": 1, "occurrenceNumberInMK": 1,
                "referenceOccurrenceCountForMK": None, "checkerName": checker, "subcategory": f.subcategory,
                "type": f.type or re.sub(r"[^a-z0-9]+", "_", checker.lower()).strip("_"),
                "code-language": "c/c++", "extra": f.extra, "domain": "STATIC_C", "language": lang,
                "mainEventFilePathname": i["remote"], "strippedMainEventFilePathname": strip_path(i["remote"], strip),
                "mainEventLineNumber": f.line, "properties": {},
                "functionDisplayName": i["display"], "functionMangledName": fn.mangled if fn else None,
                "localStatus": None, "ordered": True,
                "events": build_tree(f.events, mapping, strip), "stateOnServer": None,
                "checkerProperties": V.checker_properties(checker, f.subcategory),
                "functionMergeName": i["display"],
            }
            if f.has_subtype:
                issue["subtype"] = f.subcategory
            issues.append(issue)
    issues.sort(key=lambda x: (x["mainEventFilePathname"], x["mainEventLineNumber"], x["checkerName"], x["extra"]))
    counts = Counter(x["mergeKey"] for x in issues)
    numbers: Counter = Counter()
    for x in issues:
        numbers[x["mergeKey"]] += 1
        x["occurrenceCountForMK"] = counts[x["mergeKey"]]
        x["occurrenceNumberInMK"] = numbers[x["mergeKey"]]
    return issues


def summary_text(opts: dict, tus: list[dict], sources: list[Source], headers: list[Source], issues: list[dict],
                 seconds: float) -> str:
    by_lang = Counter(tu["lang"] for tu in tus)
    loc = sum(len(s.lines) for s in sources) + sum(len(h.lines) for h in headers)
    functions = sum(len(s.functions) for s in sources) + sum(len(h.functions) for h in headers)
    classes = len({fn.scope[-1] for s in sources + headers for fn in s.functions if fn.scope})
    lines = ["cov-analyze " + " ".join(opts["argv"]) + " ", "Analysis summary report:", "------------------------",
             f"Files analyzed                 : {len(tus)} Total"]
    for lang in sorted(by_lang):
        lines.append(f"    {lang:<27}: {by_lang[lang]}")
    lines.append(f"Total LoC input to cov-analyze : {loc}")
    if functions:
        lines.append(f"Functions analyzed             : {functions}")
    if classes:
        lines.append(f"Classes/structs analyzed       : {classes}")
    lines.append(f"Paths analyzed                 : {functions * 3}")
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    lines.append(f"Time taken by analysis         : {h:02d}:{m:02d}:{s:02d}")
    if issues:
        lines.append(f"Defect occurrences found       : {len(issues)} Total")
        for checker, n in sorted(Counter(x["checkerName"] for x in issues).items()):
            lines.append(f"                                 {n} {checker}")
    else:
        lines.append("Defect occurrences found       : 0")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = parse_args(argv)
    except ValueError as exc:
        sys.stdout.write(banner("Static Analysis") + f"[ERROR] {exc}\n")
        return 2
    try:
        return analyze(opts)
    except Exception as exc:  # noqa: BLE001 - the "unexpected error" exit code of Coverity Analysis
        sys.stdout.write(f"[ERROR] Unexpected error: {type(exc).__name__}: {exc}\n")
        return 4
