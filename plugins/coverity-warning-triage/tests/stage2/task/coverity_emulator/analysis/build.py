"""Mock cov-build (specification 4.1).

``cov-build --dir <idir> [--return-emit-failures] [--parse-error-threshold N] <build command>``

The build command really runs. Compiler calls by name (gcc, g++, cc, c++, clang, clang++) are caught
by wrapper scripts put first in PATH, which record the arguments and the working folder in the
intermediate directory and then run the real compiler; the build result is not changed. (The real
cov-build also catches compilers called by full path; the wrappers only see calls through PATH.)

Exit code: the build's own exit code. With --return-emit-failures: OR of 1 (build failed), 2 (signal),
4 (nothing emitted), 8 (less than the threshold, 95 % by default, emitted). Bad arguments: 16.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import EMIT, TUS_FILE, banner, map_path, path_map_from_env, read_json, write_json

COMPILERS = ("gcc", "g++", "cc", "c++", "clang", "clang++")
CPP_EXT = (".cpp", ".cc", ".cxx", ".C", ".c++")
SOURCE_EXT = (".c",) + CPP_EXT
PACKAGE_PARENT = Path(__file__).resolve().parents[2]

WRAPPER = """#!{python}
import sys
sys.path.insert(0, {parent!r})
from coverity_emulator.analysis.build import capture
sys.exit(capture({name!r}, {real!r}, {log!r}))
"""


def capture(name: str, real: str, log: str) -> int:
    """Body of a compiler wrapper: run the real compiler, then record the call."""
    args = sys.argv[1:]
    started = time.time()
    code = subprocess.call([real, *args])
    record = {"compiler": name, "real": real, "args": args, "cwd": os.getcwd(), "status": code,
              "seconds": round(time.time() - started, 3)}
    with open(log, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(record) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)
    return code


def _usage(message: str) -> int:
    sys.stdout.write(banner("Build Capture (64-bit)"))
    sys.stdout.write(f"[ERROR] {message}\nUsage: cov-build --dir <intermediate_directory> [OPTIONS] <build command>\n")
    return 16


def _stamp(pid: int) -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1e6):06d}Z|cov-build|{pid}|info|> "


def _sources(args: list[str]) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("-o", "-MF", "-MT", "-MQ", "-include", "-isystem", "-iquote", "-x", "-I", "-D", "-U"):
            skip = True
            continue
        if not a.startswith("-") and a.endswith(SOURCE_EXT):
            out.append(a)
    return out


def _deps(compiler: str, args: list[str], src: str, cwd: str) -> list[str]:
    keep, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ("-I", "-D", "-U", "-include", "-isystem", "-iquote") and i + 1 < len(args):
            keep += [a, args[i + 1]]
            i += 2
            continue
        if a.startswith(("-I", "-D", "-U", "-std=")):
            keep.append(a)
        i += 1
    proc = subprocess.run([compiler, *keep, "-MM", src], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    text = proc.stdout.replace("\\\n", " ")
    parts = text.split(":", 1)[1].split() if ":" in text else []
    return sorted({os.path.normpath(os.path.join(cwd, p)) for p in parts[1:]})


def _md5(path: str) -> str:
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "0" * 32


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    idir = None
    return_emit = False
    threshold = 95
    options: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dir" and i + 1 < len(argv):
            idir, i = argv[i + 1], i + 2
        elif a.startswith("--dir="):
            idir, i = a.split("=", 1)[1], i + 1
        elif a == "--return-emit-failures":
            return_emit, i = True, i + 1
        elif a == "--parse-error-threshold" and i + 1 < len(argv):
            threshold, i = int(argv[i + 1]), i + 2
        elif a in ("--emit-complementary-info", "--no-command-line-tool", "--record-only", "--fs-capture-search"):
            options.append(a)
            i += 1
        elif a in ("--config", "--tmpdir", "--fs-capture-search-exclude-regex") and i + 1 < len(argv):
            options += argv[i:i + 2]
            i += 2
        elif a.startswith("-"):
            return _usage(f"Unrecognized option: {a}")
        else:
            break
    command = argv[i:]
    if not idir:
        return _usage("Missing required option --dir")
    if not command:
        return _usage("No build command was given")
    sys.stdout.write(banner("Build Capture (64-bit)"))
    sys.stdout.flush()
    idir_path = Path(idir).resolve()
    emu = idir_path / "emulator"
    wrappers = emu / "wrappers"
    wrappers.mkdir(parents=True, exist_ok=True)
    log = emu / "captures.jsonl"
    log.write_text("", encoding="utf-8")
    search_path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                                  if Path(p).resolve() != wrappers.resolve())
    for name in COMPILERS:
        real = shutil.which(name, path=search_path)
        if not real:
            continue
        w = wrappers / name
        w.write_text(WRAPPER.format(python=sys.executable, parent=str(PACKAGE_PARENT), name=name, real=real,
                                    log=str(log)), encoding="utf-8")
        w.chmod(0o755)
    env = dict(os.environ, PATH=str(wrappers) + os.pathsep + search_path, COV_EMU_IDIR=str(idir_path))
    started = time.time()
    proc = subprocess.run(command, env=env)
    build_seconds = time.time() - started
    code = proc.returncode
    signaled = code < 0

    mapping = path_map_from_env()
    tus_path = idir_path / EMIT / TUS_FILE
    data = read_json(tus_path, {}) or {}
    tus = {t["source"]: t for t in data.get("tus", [])}
    fail_globs = [g.strip() for g in (os.environ.get("COV_EMU_EMIT_FAIL") or "").split(",") if g.strip()]
    emit_started = time.time()
    attempted = 0
    emitted = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        args = rec["args"]
        if "-E" in args or ("-M" in args or "-MM" in args) and "-c" not in args:
            continue
        for src in _sources(args):
            path = os.path.normpath(os.path.join(rec["cwd"], src))
            lang = "C++" if rec["compiler"] in ("g++", "c++", "clang++") or path.endswith(CPP_EXT) else "C"
            if "-x" in args:
                lang = "C++" if args[args.index("-x") + 1].startswith("c++") else lang
            attempted += 1
            remote = map_path(path, mapping)
            failed = rec["status"] != 0 or any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(remote, g)
                                               or fnmatch.fnmatch(os.path.basename(path), g) for g in fail_globs)
            headers = [] if failed else _deps(rec["real"], args, src, rec["cwd"])
            tus[path] = {"source": path, "remote": remote, "lang": lang, "compiler": rec["compiler"], "args": args,
                         "cwd": rec["cwd"], "md5": _md5(path), "emitted": not failed,
                         "headers": [{"path": h, "remote": map_path(h, mapping), "md5": _md5(h)} for h in headers],
                         "lines": len(Path(path).read_text(encoding="utf-8", errors="replace").splitlines())
                         if Path(path).is_file() else 0}
            emitted += 0 if failed else 1
    emit_seconds = time.time() - emit_started
    write_json(tus_path, {"pathMap": mapping, "buildCommandLine": " ".join(command), "buildHost": os.uname().nodename,
                          "options": options, "buildSeconds": round(build_seconds, 3), "tus": sorted(tus.values(), key=lambda t: t["source"])})

    pid = os.getpid()
    lines = []
    lines.append(f"Build time (cov-build overall): {_hms(build_seconds)}")
    lines.append("")
    if emitted:
        lines.append(f"Build time (C/C++ emits total): {_hms(emit_seconds)}")
        lines.append("")
    warnings = []
    if code != 0:
        warnings.append(f"[WARNING] Build command {command[0]} exited with code {code}. Please verify that the build "
                        "completed successfully.")
    if attempted == 0 or emitted == 0:
        warnings += ["[WARNING] No files were emitted. This may be due to a problem with your configuration",
                     "or because no files were actually compiled by your build command.",
                     "Please make sure you have configured the compilers actually used in the compilation.",
                     " For more details, please look at: ", f"    {idir_path / 'build-log.txt'}"]
    else:
        pct = int(emitted * 100 / attempted)
        lines.append(f"Emitted {emitted} C/C++ compilation units ({pct}%) successfully")
        lines.append("")
        if emitted < attempted:
            lines.append(f"[WARNING] {attempted - emitted} C/C++ compilation units ({100 - pct}%) failed to emit")
        lines.append(f"{emitted} C/C++ compilation units ({pct}%) are ready for analysis")
        if code == 0:
            lines.append("The cov-build utility completed successfully.")
    out_lines = list(lines)
    if warnings:
        out_lines += [""] + warnings
    sys.stdout.write("\n".join(out_lines) + "\n")
    sys.stdout.flush()
    with open(idir_path / "build-log.txt", "a", encoding="utf-8") as f:
        f.write(_stamp(pid) + f"cov-build {' '.join(argv)}\n")
        for line in log.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            f.write(_stamp(pid) + f"COMMAND LINE: {rec['real']} {' '.join(rec['args'])} (in {rec['cwd']})\n")
        for line in out_lines:
            f.write(_stamp(pid) + line + "\n")
    if not return_emit:
        return code if code >= 0 else 128 - code
    flags = 0
    if code != 0:
        flags |= 1
    if signaled:
        flags |= 2
    if emitted == 0:
        flags |= 4
    elif attempted and emitted * 100 < threshold * attempted:
        flags |= 8
    return flags


def _hms(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:09.6f}"
