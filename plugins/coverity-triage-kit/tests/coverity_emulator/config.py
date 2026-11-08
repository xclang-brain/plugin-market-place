"""Switches of the emulator (specification 1.5 and the review's additions) and fault injection.

Each switch picks one of the behaviors the specification could not confirm. The defaults follow the
specification's "default" column, corrected by the review (coverity-spec-critique.md):

- auth_failure "split": a request without an Authorization header gets 302 to /login/login.htm,
  wrong Basic credentials get 401 with the body "Authentication failed.\\n" (measured on scan8).
- server_version "2024.12.1" (scan8 runs 2024.12.1A).
- soap_max_cids 100 (official: "up to a limit of 100").

Fault injection rules (``inject``) apply to matching requests, in order, and can be limited to a
number of requests (``count``). Fields of a rule:

    path       regular expression searched in the request path (default: any)
    method     HTTP method (default: any)
    operation  SOAP operation name (default: any)
    count      how many matching requests it applies to (default: unlimited)
    delay      seconds to wait before answering
    status     HTTP status to answer instead of the normal handling (with ``body`` / ``content_type``)
    html       true: answer ``status`` (default 200) with an HTML login page instead of JSON/XML
    soap_fault faultstring of a SOAP Fault to answer (HTTP ``status``, default 500; ``code``
               adds a CoverityFault detail with that errorCode)
    redirect   301 or 302: redirect to the https URL of the same path
    drop       "before_response": close the connection without answering;
               "mid_body": send the headers and half of the body, then close
    invalid_key  answer 500 "Invalid key provided: <value>" in the error_body format
    omit_total_rows  true: leave totalRows out of the search response
    page_overlap     n: a search page with offset > 0 starts n rows earlier
"""

from __future__ import annotations

import copy
import json
from typing import Any

DEFAULTS: dict[str, Any] = {
    # ---- specification 1.5 ----
    "auth_failure": "split",            # split | 401 | 302
    "rest_no_permission": 403,          # 403 | 401
    "columns_names": "en",              # en | ja   (names when the request has no locale)
    "search_default_sort": "cid",       # cid | unstable   (order without sortColumn)
    "search_empty_value": "empty",      # empty ("") | null | omit
    "search_unknown_key": "500",        # 500 | 400 | 200_empty
    "error_body": "json",               # json | text | html
    "unknown_stream": "empty",          # empty | 404
    "rowcount_minus1": "all",           # all | 400
    "old_server_needs_project": False,  # true: a stream filter without a project filter is ignored
    "latency_seconds": 0.0,             # added to every answer (a production server took ~4.6 s)
    "triage_success": "200_empty",      # 200_empty | 200_json | 204
    "triage_max_cids": 0,               # 0: no limit
    "triage_require_comment": False,    # triage.require.comment.on.change
    "triage_test_values_only_for_test": False,
    "server_version": "2024.12.1",      # e.g. 2021.12.0: PUT triage and serverInfo answer 404
    "soap_max_instances_missing": "all",  # all | none | one   (maxDefectInstances missing or 0)
    "soap_cid_not_in_stream": "empty",  # empty | fault
    "soap_max_cids": 100,               # 0: no limit
    "soap_require_must_understand": False,
    "soap_strict_namespace": False,     # true: child elements in the v9 namespace are not read
    "soap_accept_basic": False,         # true: HTTP Basic is accepted instead of WS-Security
    # ---- review additions ----
    "http_to_https_redirect": 0,        # 0 | 301 | 302 (applies to the plain http listener)
    "csrf_on_session_cookie": False,    # true: PUT with the session cookie and no X-XSRF-TOKEN -> 403
    "rest_line_from_occurrence": "first",  # first | last   (lineNumber of a CID with several occurrences)
    # ---- other guesses that tests may want to flip ----
    "test_advisor": False,              # false: Untested / No Test Needed / Tested Elsewhere are unknown keys
    "issue_kind_both": "Various",       # displayIssueKind of a QUALITY+SECURITY issue (unknown)
    "snapshot_order": "id_asc",         # getSnapshotsForStream order: id_asc | id_desc | date_desc
    "triage_wrong_store": "accept",     # accept | error   (store not used by any stream of the CIDs)
    "attribute_values": {},             # extra values per attribute, e.g. {"Action": ["Review"]}
    "omit_total_rows": False,
    "page_overlap": 0,
    "inject": [],
}

CHOICES: dict[str, tuple] = {
    "auth_failure": ("split", "401", "302"),
    "rest_no_permission": (403, 401),
    "columns_names": ("en", "ja"),
    "search_default_sort": ("cid", "unstable"),
    "search_empty_value": ("empty", "null", "omit"),
    "search_unknown_key": ("500", "400", "200_empty"),
    "error_body": ("json", "text", "html"),
    "unknown_stream": ("empty", "404"),
    "rowcount_minus1": ("all", "400"),
    "triage_success": ("200_empty", "200_json", "204"),
    "soap_max_instances_missing": ("all", "none", "one"),
    "soap_cid_not_in_stream": ("empty", "fault"),
    "http_to_https_redirect": (0, 301, 302),
    "rest_line_from_occurrence": ("first", "last"),
    "snapshot_order": ("id_asc", "id_desc", "date_desc"),
    "triage_wrong_store": ("accept", "error"),
}


class ConfigError(ValueError):
    pass


def _coerce(key: str, value: Any) -> Any:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, (list, dict)):
        return json.loads(value) if isinstance(value, str) else value
    return str(value)


def merge(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Validated copy of ``current`` with ``changes``. ``{"reset": true}`` starts from the defaults."""
    out = copy.deepcopy(DEFAULTS if changes.get("reset") else current)
    for key, value in changes.items():
        if key == "reset":
            continue
        if key not in DEFAULTS:
            raise ConfigError(f"unknown switch: {key}")
        value = _coerce(key, value)
        if key in CHOICES and value not in CHOICES[key]:
            raise ConfigError(f"{key} must be one of {CHOICES[key]}: {value!r}")
        if key == "inject":
            if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
                raise ConfigError("inject must be a list of objects")
        out[key] = value
    return out


def version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for p in str(text).split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))
