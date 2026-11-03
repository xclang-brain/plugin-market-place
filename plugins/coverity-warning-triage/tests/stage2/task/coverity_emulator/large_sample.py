"""A large warning with the shape of a real one (specification 4.3): 20 events nested 6 levels deep.

Real Coverity warnings of this kind (USE_AFTER_FREE through several calls) carry 20 to 28 events and
reach 12 to 17 KB as JSON. The warning is made up here, on the paths of the test subject envmon, so that
no third-party data is copied into this repository. Only the shape (keys, nesting, event tags, the main
event at the end) follows the specification.
"""

from __future__ import annotations

PATH = "/build/agent/work/envmon/src/app/logger.c"
STRIPPED = "src/app/logger.c"

# (tree position, tag, line, description, children)
TREE = [
    ("1", "path", 196, 'Condition "lg->dir_count > 0", taking true branch.', []),
    ("2", "path", 199, 'Condition "rc != 0", taking false branch.', []),
    ("3", "freed_arg", 205, '"logger_rotate_dir" frees "lg->dir".', [
        ("3.1", "path", 140, 'Condition "lg->dir == NULL", taking false branch.', []),
        ("3.2", "path", 141, 'Condition "lg->mode == LOG_MODE_RING", taking false branch.', []),
        ("3.3", "path", 144, 'Condition "rotate", taking true branch.', []),
        ("3.4", "path", 147, 'Condition "lg->files > LOG_MAX_FILES", taking true branch.', []),
        ("3.5", "path", 148, 'Condition "unlink_oldest(lg) < 0", taking false branch.', []),
        ("3.6", "path", 151, 'Condition "rename_failed", taking false branch.', []),
        ("3.7", "freed_arg", 156, '"logger_close_dir" frees "lg->dir".', [
            ("3.7.1", "path", 120, 'Condition "lg->fp != NULL", taking true branch.', []),
            ("3.7.2", "path", 123, 'Condition "flush_pending(lg)", taking false branch.', []),
            ("3.7.3", "freed_arg", 126, '"release_dir" frees "lg->dir".', [
                ("3.7.3.1", "path", 98, 'Condition "dir->refs > 1", taking false branch.', []),
                ("3.7.3.2", "freed_arg", 101, '"dir_free" frees "dir".', [
                    ("3.7.3.2.1", "freed_arg", 74, '"free" frees "dir->name".', [
                        ("3.7.3.2.1.1", "path", 70, 'Condition "dir->name != NULL", taking true branch.', []),
                        ("3.7.3.2.1.2", "freed_arg", 71, '"free" frees "dir".', []),
                    ]),
                ]),
            ]),
        ]),
    ]),
    ("4", "path", 207, 'Condition "lg->verbose", taking false branch.', []),
    ("5", "deref_after_free", 211, 'Dereferencing freed pointer "lg->dir".', []),
]


def _events(nodes, counter):
    out = []
    for position, tag, line, description, children in nodes:
        counter[0] += 1
        out.append({
            "covLStrEventDescription": "{CovLStrv2{{t{" + description.replace("{", "(").replace("}", ")") + "}}}}",
            "eventDescription": description,
            "eventNumber": counter[0],
            "eventTreePosition": position,
            "eventSet": 0,
            "eventTag": tag,
            "filePathname": PATH,
            "strippedFilePathname": STRIPPED,
            "lineNumber": line,
            "main": tag == "deref_after_free",
            "moreInformationId": None,
            "remediation": False,
            "events": _events(children, counter),
        })
    return out


def issue(merge_key: str = "4f2c7d1e9a8b6c5d4e3f2a1b0c9d8e7f", extra: str = "lg->dir,lg") -> dict:
    """One json-output-v7 issue with the large shape."""
    return {
        "mergeKey": merge_key, "occurrenceCountForMK": 1, "occurrenceNumberInMK": 1,
        "referenceOccurrenceCountForMK": None, "checkerName": "USE_AFTER_FREE",
        "subcategory": "deref_read_after_free", "type": "use_after_free", "subtype": "deref_read_after_free",
        "code-language": "c/c++", "extra": extra, "domain": "STATIC_C", "language": "C",
        "mainEventFilePathname": PATH, "strippedMainEventFilePathname": STRIPPED, "mainEventLineNumber": 211,
        "properties": {}, "functionDisplayName": "logger_switch_dir", "functionMangledName": "logger_switch_dir",
        "localStatus": None, "ordered": True, "events": _events(TREE, [0]), "stateOnServer": None,
        "checkerProperties": {
            "category": "Memory - illegal accesses", "categoryDescription": "Memory - illegal accesses",
            "cweCategory": "416", "issueKinds": ["QUALITY", "SECURITY"], "eventSetCaptions": [],
            "impact": "High", "impactDescription": "High",
            "subcategoryLocalEffect": "Memory corruption or crash.",
            "subcategoryShortDescription": "Read from pointer after free",
            "subcategoryLongDescription": "Reads target of a freed pointer",
        },
    }


def document() -> dict:
    """A whole json-output-v7 document holding the large issue."""
    return {"type": "Coverity issues", "formatVersion": 7, "suppressedIssueCount": 0, "issues": [issue()],
            "desktopAnalysisSettings": None, "error": None, "warnings": []}
