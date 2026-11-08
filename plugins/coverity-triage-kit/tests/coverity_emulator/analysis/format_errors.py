"""Mock cov-format-errors (specification 4.3).

``cov-format-errors --dir <idir> --json-output-v7 <file> [--strip-path P] [--lang en] [--checker-regex R]
[--file-regex R] [--merge-key-regex R] [--impact-regex R] [--include-files R] [--exclude-files R]``

Writes JSON v7 (outer keys, 23 issue keys and 13 event keys in the confirmed order). The default triage
filters only act on stateOnServer, which is null without --preview-report, so nothing is left out.
Exit code 0; with COV_EMU_FORMAT_ERRORS_ZERO_EXIT=1, 1 when there are no issues (unknown in reality).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .common import ISSUES_FILE, OUTPUT, read_json, to_v7_issue

REGEX_OPTIONS = {"--checker-regex": "checkerName", "--file-regex": "mainEventFilePathname",
                 "--merge-key-regex": "mergeKey", "--include-files": "mainEventFilePathname"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    idir = out_file = None
    strip: list[str] = []
    regex: list[tuple[str, str, bool]] = []
    impact = None
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, value = a.partition("=")
        takes = name in ("--dir", "--json-output-v7", "--strip-path", "--lang", "--impact-regex", "--exclude-files",
                         "--preview-report", "--tmpdir", "--cid", "--MISRA-category-regex", "--occurrences",
                         "--component-not-regex", "--triage-attribute-not-regex", "--security-file") or name in REGEX_OPTIONS
        if takes and not eq:
            if i + 1 >= len(argv):
                sys.stdout.write(f"[ERROR] Option {a} needs a value\n")
                return 2
            value = argv[i + 1]
            i += 1
        if name == "--triage-attribute-not-regex":
            i += 1  # attribute name and regex
        if name == "--dir":
            idir = value
        elif name == "--json-output-v7":
            out_file = value
        elif re.fullmatch(r"--json-output-v\d+", name):
            sys.stdout.write(f"[ERROR] This emulator writes only --json-output-v7 (got {name})\n")
            return 2
        elif name == "--strip-path":
            strip.append(value)
        elif name in REGEX_OPTIONS:
            regex.append((REGEX_OPTIONS[name], value, True))
        elif name == "--exclude-files":
            regex.append(("mainEventFilePathname", value, False))
        elif name == "--impact-regex":
            impact = value
        elif name in ("--lang", "--preview-report", "--tmpdir", "--cid", "--MISRA-category-regex", "--occurrences",
                      "--component-not-regex", "--triage-attribute-not-regex", "--security-file", "--no-banner",
                      "--no-default-triage-filters"):
            pass
        else:
            sys.stdout.write(f"[ERROR] Unrecognized option: {a}\n")
            return 2
        i += 1
    if not idir:
        sys.stdout.write("[ERROR] Missing required option --dir\n")
        return 2
    if not out_file:
        sys.stdout.write("[ERROR] No output format was given (use --json-output-v7 <file>)\n")
        return 2
    output = Path(idir) / OUTPUT
    data = read_json(output / ISSUES_FILE)
    if data is None:
        sys.stdout.write(f"[ERROR] No analysis results in the intermediate directory {idir}. Run cov-analyze first.\n")
        return 2
    analysis = read_json(output / "emulator-analysis.json", {}) or {}
    analyzed_strip = analysis.get("stripPath") or []
    extra_strip = [p for p in strip if p not in analyzed_strip]
    if any(p in analyzed_strip for p in strip):
        sys.stdout.write("[WARNING] For compatibility with previous releases, cov-format-errors\n"
                         "will accept --strip-path options that are identical to the one passed to\n"
                         "cov-analyze. It is recommended to only pass the --strip-path option to\n"
                         "cov-analyze.\n\n")
    issues = []
    for issue in data.get("issues", []):
        if any(bool(re.search(rx, str(issue.get(key) or ""))) != keep for key, rx, keep in regex):
            continue
        if impact and not re.search(impact, issue["checkerProperties"]["impact"]):
            continue
        issues.append(to_v7_issue(issue, extra_strip))
    doc = {"type": "Coverity issues", "formatVersion": 7, "suppressedIssueCount": 0, "issues": issues,
           "desktopAnalysisSettings": None, "error": None, "warnings": []}
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    Path(out_file).write_text(json.dumps(doc, ensure_ascii=False, indent=2, separators=(",", " : ")) + "\n",
                              encoding="utf-8")
    if not issues and os.environ.get("COV_EMU_FORMAT_ERRORS_ZERO_EXIT") == "1":
        return 1
    return 0
