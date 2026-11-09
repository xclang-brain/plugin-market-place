"""Running the real analyzers on one translation unit and reading their reports.

- clang --analyze with ``-analyzer-output=plist-multi-file`` (path events with call depth).
- gcc -fanalyzer with ``-fdiagnostics-format=json-stderr`` (path events with depth and function), and the
  front-end warnings of -Wall -Wextra (for MISSING_BREAK and the PW.* parse warnings).
- cppcheck ``--xml`` (with ``--addon=misra`` when a MISRA C:2012 coding standard is enabled).
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RawEvent:
    message: str
    file: str
    line: int
    col: int
    depth: int
    function: str | None = None


@dataclass
class RawFinding:
    tool: str
    rule: str
    message: str
    file: str
    line: int
    col: int
    function: str | None = None
    path: list[RawEvent] = field(default_factory=list)
    locations: list[tuple[str, int, int, str]] = field(default_factory=list)
    symbol: str | None = None
    cwe: str | None = None


def available(tool: str) -> bool:
    wanted = os.environ.get("COV_EMU_TOOLS")
    if wanted and tool not in [t.strip() for t in wanted.split(",")]:
        return False
    return shutil.which(tool) is not None


def _abs(path: str, cwd: str) -> str:
    return os.path.normpath(os.path.join(cwd, path))


def flags(tu: dict) -> list[str]:
    """The preprocessor and language flags of a captured compile command (paths made absolute)."""
    out = []
    args = tu["args"]
    i = 0
    while i < len(args):
        a = args[i]
        for opt in ("-I", "-isystem", "-iquote", "-include"):
            if a == opt and i + 1 < len(args):
                out += [opt, _abs(args[i + 1], tu["cwd"])]
                i += 1
                break
            if a.startswith(opt) and a != opt and opt == "-I":
                out.append("-I" + _abs(a[2:], tu["cwd"]))
                break
        else:
            if a.startswith(("-D", "-U", "-std=")):
                out.append(a)
                if a in ("-D", "-U") and i + 1 < len(args):
                    out.append(args[i + 1])
                    i += 1
        i += 1
    return out


def _std(tu: dict) -> str | None:
    for a in tu["args"]:
        if a.startswith("-std="):
            return a[5:]
    return None


def run_clang(tu: dict, log: list[str]) -> list[RawFinding]:
    cpp = tu["lang"] == "C++"
    exe = "clang++" if cpp else "clang"
    if not available("clang"):
        return []
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.plist"
        cmd = [exe, "--analyze", "-Xanalyzer", "-analyzer-output=plist-multi-file", "-o", str(out),
               *flags(tu), "-x", "c++" if cpp else "c", tu["source"]]
        proc = subprocess.run(cmd, cwd=tu["cwd"], capture_output=True, text=True)
        log.append(f"[clang] {' '.join(cmd)} -> {proc.returncode}")
        if not out.is_file():
            return []
        data = plistlib.loads(out.read_bytes())
    files = [_abs(f, tu["cwd"]) for f in data.get("files", [])]
    result = []
    for d in data.get("diagnostics", []):
        loc = d["location"]
        events = []
        for p in d.get("path", []):
            if p.get("kind") != "event":
                continue
            ploc = p["location"]
            events.append(RawEvent(p.get("message", ""), files[ploc["file"]], ploc["line"], ploc["col"],
                                   int(p.get("depth", 0))))
        result.append(RawFinding("clang", d.get("check_name", ""), d.get("description", ""), files[loc["file"]],
                                 loc["line"], loc["col"], function=d.get("issue_context"), path=events))
    return result


def run_gcc(tu: dict, log: list[str]) -> list[RawFinding]:
    cpp = tu["lang"] == "C++"
    exe = "g++" if cpp else "gcc"
    if not available("gcc"):
        return []
    cmd = [exe, "-fanalyzer", "-Wall", "-Wextra", "-fdiagnostics-format=json-stderr", *flags(tu),
           "-x", "c++" if cpp else "c", "-c", tu["source"], "-o", os.devnull]
    proc = subprocess.run(cmd, cwd=tu["cwd"], capture_output=True, text=True)
    log.append(f"[gcc] {' '.join(cmd)} -> {proc.returncode}")
    text = proc.stderr.strip()
    items = []
    for chunk in text.splitlines():
        chunk = chunk.strip()
        if chunk.startswith("["):
            try:
                items += json.loads(chunk)
            except ValueError:
                pass
    if not items and text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            items = []
    result = []
    for d in items:
        if d.get("kind") not in ("warning", "error"):
            continue
        loc = (d.get("locations") or [{}])[0].get("caret") or {}
        events = []
        for e in d.get("path") or []:
            eloc = e.get("location") or {}
            events.append(RawEvent(e.get("description", ""), _abs(eloc.get("file", ""), tu["cwd"]),
                                   int(eloc.get("line", 0)), int(eloc.get("column", 0)), int(e.get("depth", 0)),
                                   e.get("function")))
        result.append(RawFinding("gcc", d.get("option", ""), d.get("message", ""), _abs(loc.get("file", ""), tu["cwd"]),
                                 int(loc.get("line", 0)), int(loc.get("column", 0)),
                                 function=events[-1].function if events else None, path=events))
    return result


def run_cppcheck(tu: dict, log: list[str], misra: bool) -> list[RawFinding]:
    if not available("cppcheck"):
        return []
    cpp = tu["lang"] == "C++"
    std = _std(tu)
    cmd = ["cppcheck", "--xml", "--enable=warning,style,performance,portability", "--inconclusive", "-q",
           "--suppress=missingIncludeSystem", f"--language={'c++' if cpp else 'c'}"]
    if std:
        std = std.replace("gnu", "c")
        cmd.append(f"--std={std}")
    for f in flags(tu):
        if f.startswith(("-I", "-D", "-U")) and f not in ("-I", "-D", "-U"):
            cmd.append(f)
    with tempfile.TemporaryDirectory() as build_dir:
        cmd.append(f"--cppcheck-build-dir={build_dir}")
        if misra and not cpp:
            cmd.append("--addon=misra")
        cmd.append(tu["source"])
        proc = subprocess.run(cmd, cwd=tu["cwd"], capture_output=True, text=True)
    log.append(f"[cppcheck] {' '.join(cmd)} -> {proc.returncode}")
    try:
        root = ET.fromstring(proc.stderr)
    except ET.ParseError:
        return []
    result = []
    for err in root.iter("error"):
        locs = [(_abs(l.get("file", ""), tu["cwd"]), int(l.get("line", 0)), int(l.get("column", 0)), l.get("info", ""))
                for l in err.findall("location")]
        if not locs:
            continue
        sym = err.findtext("symbol")
        result.append(RawFinding("cppcheck", err.get("id", ""), err.get("msg", ""), locs[0][0], locs[0][1], locs[0][2],
                                 locations=locs, symbol=sym, cwe=err.get("cwe")))
    return result
