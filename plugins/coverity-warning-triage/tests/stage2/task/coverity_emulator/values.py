"""Fixed values of Coverity Connect and Coverity Analysis (specification section 5).

Strength of each value, as in the specification: "confirmed" values come from the official
documentation, real output or measurement; "likely" values come from public implementations
(for example the checker table of coverity/coverity-sonar-plugin); "guess" values are marked as such.
"""

from __future__ import annotations

# ---- REST: GET /api/v2/issues/columns (official sample, confirmed) ---------------------------
COLUMNS: list[tuple[str, str]] = [
    ("cid", "CID"), ("checker", "Checker"), ("displayImpact", "Impact"), ("displayCategory", "Category"),
    ("displayType", "Type"), ("cwe", "CWE"), ("displayIssueKind", "Issue Kind"), ("status", "Status"),
    ("firstDetected", "First Detected"), ("owner", "Owner"), ("ownerFullName", "Owner Name"),
    ("externalReference", "External Reference"), ("classification", "Classification"),
    ("severity", "Severity"), ("action", "Action"), ("fixTarget", "Fix Target"), ("legacy", "Legacy"),
    ("displayComponent", "Component"), ("displayFile", "File"), ("displayFunction", "Function"),
    ("functionMergeName", "Function Merge Name"), ("mergeExtra", "Merge Extra"), ("mergeKey", "Merge Key"),
    ("fileLanguage", "Language"), ("ruleStrength", "MISRA Category"), ("lastTriaged", "Last Triaged"),
    ("lastTriagedUser", "Last Triaged User"), ("occurrenceCount", "Count"),
    ("displayComparison", "Comparison"), ("firstSnapshotDate", "First Snapshot Date"),
    ("firstSnapshotId", "First Snapshot"), ("firstSnapshotVersion", "First Snapshot Version"),
    ("firstSnapshotTarget", "First Snapshot Target"),
    ("firstSnapshotDescription", "First Snapshot Description"),
    ("firstSnapshotStream", "First Snapshot Stream"), ("lastDetected", "Last Snapshot Date"),
    ("lastDetectedId", "Last Snapshot"), ("lastDetectedVersion", "Last Snapshot Version"),
    ("lastDetectedTarget", "Last Snapshot Target"), ("lastDetectedDescription", "Last Snapshot Description"),
    ("lastDetectedStream", "Last Snapshot Stream"), ("score", "Score"), ("lineNumber", "Line Number"),
    ("lastTriageComment", "Last Triage Comment"), ("column_custom_test", "test"),
    ("column_standard_PCI DSS 2018", "Standard: PCI DSS 2018"),
    ("column_standard_AUTOSAR C++14", "Standard: AUTOSAR C++14"),
    ("column_standard_OWASP Web Top Ten 2017", "Standard: OWASP Web Top Ten 2017"),
    ("column_standard_DISA-STIG V4R10", "Standard: DISA-STIG V4R10"),
    ("column_standard_DISA-STIG V4R3", "Standard: DISA-STIG V4R3"),
    ("column_standard_CERT C", "Standard: CERT C"),
    ("column_standard_DISA-STIG V4R10 Severity", "Standard: DISA-STIG V4R10 Severity"),
    ("column_standard_DISA-STIG V4R3 Severity", "Standard: DISA-STIG V4R3 Severity"),
    ("column_standard_CERT C++", "Standard: CERT C++"),
    ("column_standard_OWASP Mobile Top Ten 2016", "Standard: OWASP Mobile Top Ten 2016"),
    ("column_standard_ISO TS17961 2016", "Standard: ISO TS17961 2016"),
    ("project", "Project"),
]
COLUMN_KEYS = {k for k, _ in COLUMNS}
# Keys that only filter (never listed by GET columns, confirmed).
FILTER_ONLY_KEYS = {"streams"}

# Names used when the switch columns_names is "ja" (guess: no Japanese sample exists).
COLUMN_NAMES_JA = {
    "cid": "CID", "checker": "チェッカー", "displayImpact": "影響度", "displayCategory": "カテゴリ",
    "displayType": "タイプ", "cwe": "CWE", "displayIssueKind": "問題の種類", "status": "ステータス",
    "firstDetected": "最初の検出", "owner": "所有者", "ownerFullName": "所有者名",
    "externalReference": "外部参照", "classification": "分類", "severity": "重大度", "action": "アクション",
    "fixTarget": "修正目標", "legacy": "レガシー", "displayComponent": "コンポーネント",
    "displayFile": "ファイル", "displayFunction": "関数", "functionMergeName": "関数のマージ名",
    "mergeExtra": "マージの追加情報", "mergeKey": "マージキー", "fileLanguage": "言語",
    "ruleStrength": "MISRA カテゴリ", "lastTriaged": "最終トリアージ", "lastTriagedUser": "最終トリアージのユーザー",
    "occurrenceCount": "件数", "lineNumber": "行番号", "lastTriageComment": "最終トリアージのコメント",
    "project": "プロジェクト",
}

# ---- triage attributes (GET /api/v2/triageAttributes, confirmed) ------------------------------
CLASSIFICATIONS = ["Unclassified", "Pending", "False Positive", "Intentional", "Bug",
                   "Untested", "No Test Needed", "Tested Elsewhere"]
TEST_CLASSIFICATIONS = ["Untested", "No Test Needed", "Tested Elsewhere"]
ACTIONS = ["Undecided", "Fix Required", "Fix Submitted", "Modeling Required", "Ignore"]
SEVERITIES = ["Unspecified", "Major", "Moderate", "Minor"]
FIX_TARGETS = ["Untargeted"]
LEGACY = ["False", "True"]
STATUSES = ["New", "Triaged", "Dismissed", "Fixed", "Absent Dismissed"]
IMPACTS = ["High", "Medium", "Low", "Audit"]
ISSUE_KINDS = ["QUALITY", "SECURITY", "TEST"]
ISSUE_KIND_DISPLAY = {"QUALITY": "Quality", "SECURITY": "Security", "TEST": "Test"}

# Default triage of a new CID (REST attribute names as written by PUT /api/v2/issues/triage).
DEFAULT_TRIAGE = {"Classification": "Unclassified", "Action": "Undecided", "Severity": "Unspecified",
                  "Fix Target": "Untargeted", "Legacy": "False", "Owner": "Unassigned",
                  "Ext. Reference": ""}
# PUT attribute name -> the lower camel name of triageHistory (confirmed).
HISTORY_NAMES = {"Classification": "classification", "Action": "action", "Fix Target": "fixTarget",
                 "Severity": "severity", "Legacy": "legacy", "Owner": "owner",
                 "Ext. Reference": "externalReference"}
# PUT attribute name -> SOAP attributeDefinitionId.name (confirmed, melexis tests/defect_soap.xml).
SOAP_ATTRIBUTE_NAMES = {"Classification": "Classification", "Action": "Action", "Severity": "Severity",
                        "Fix Target": "Fix Target", "Legacy": "Legacy", "Owner": "Owner",
                        "Ext. Reference": "Ext. Reference"}
WRITABLE_ATTRIBUTES = ["Classification", "Action", "Severity", "Fix Target", "Legacy", "Ext. Reference",
                       "Owner", "Comment"]


def triage_attributes_payload(extra_values: dict[str, list[str]] | None = None,
                              test_advisor: bool = True) -> list[dict]:
    """The attributes list of GET /api/v2/triageAttributes (official sample, confirmed)."""
    extra_values = extra_values or {}

    def values(names: list[str]) -> list[dict]:
        return [{"name": n, "displayValue": n, "deprecated": False} for n in names]

    classifications = [c for c in CLASSIFICATIONS if test_advisor or c not in TEST_CLASSIFICATIONS]
    return [
        {"name": "Classification", "displayName": "Classification", "description": "Classification of defect",
         "displayDescription": "Classification of issue", "attributeType": "LIST_OF_VALUES", "builtIn": True,
         "defaultValueName": "Unclassified", "showInTriage": True,
         "attributeValues": values(classifications + extra_values.get("Classification", []))},
        {"name": "Action", "displayName": "Action", "description": "Action of defect",
         "displayDescription": "Action of issue", "attributeType": "LIST_OF_VALUES", "builtIn": True,
         "defaultValueName": "Undecided", "showInTriage": True,
         "attributeValues": values(ACTIONS + extra_values.get("Action", []))},
        {"name": "Ext. Reference", "displayName": "Ext. Reference", "description": "External Reference",
         "displayDescription": "External Reference", "attributeType": "STRING", "builtIn": True,
         "defaultValueName": None, "showInTriage": True, "attributeValues": []},
        {"name": "Fix Target", "displayName": "Fix Target", "description": "Fix Target of defect",
         "displayDescription": "Fix Target of issue", "attributeType": "LIST_OF_VALUES", "builtIn": True,
         "defaultValueName": "Untargeted", "showInTriage": False,
         "attributeValues": values(FIX_TARGETS + extra_values.get("Fix Target", []))},
        {"name": "Legacy", "displayName": "Legacy", "description": "Legacy defect",
         "displayDescription": "Legacy issue", "attributeType": "LIST_OF_VALUES", "builtIn": True,
         "defaultValueName": "False", "showInTriage": False, "attributeValues": values(LEGACY)},
        {"name": "Severity", "displayName": "Severity", "description": "Severity of defect",
         "displayDescription": "Severity of issue", "attributeType": "LIST_OF_VALUES", "builtIn": True,
         "defaultValueName": "Unspecified", "showInTriage": True,
         "attributeValues": values(SEVERITIES + extra_values.get("Severity", []))},
    ]


def status_of(classification: str, detected: bool, was_detected: bool) -> str | None:
    """Status from the classification and the detection in the latest snapshot ([BD:status])."""
    dismissed = classification in ("False Positive", "Intentional", "No Test Needed", "Tested Elsewhere")
    if detected:
        if dismissed:
            return "Dismissed"
        if classification in ("Pending", "Bug", "Untested"):
            return "Triaged"
        return "New"
    if was_detected:
        return "Absent Dismissed" if dismissed else "Fixed"
    return None


# ---- categories (5.8, confirmed) --------------------------------------------------------------
CATEGORIES = [
    "API usage errors", "Build system issues", "Class hierarchy inconsistencies",
    "Code maintainability issues", "Concurrent data access violations", "Control flow issues",
    "Error handling issues", "Exceptional resource leaks", "High impact security", "Medium impact security",
    "Low impact security", "Audit impact security", "Incorrect expression", "Insecure data handling",
    "Integer handling issues", "Memory - corruptions", "Memory - illegal accesses",
    "Null pointer dereferences", "Parse warnings", "Performance inefficiencies",
    "Possible Control flow issues", "Program hangs", "Resource leaks", "Rule violations",
    "Security best practices violations", "Uninitialized members", "Uninitialized variables",
    "Miscellaneous", "Coding standard violation",
]

# ja_JP display values. Confirmed: 高 and 変数の未初期化. Likely: 中, 低. Others are not translated.
JA_IMPACT = {"High": "高", "Medium": "中", "Low": "低"}
JA_CATEGORY = {"Uninitialized variables": "変数の未初期化"}

# ---- SOAP / REST error codes (1.4, confirmed for the table) ----------------------------------
ERROR_CODES = {
    1001: "Authentication failed", 1003: "Access denied", 1004: "Internal error",
    1006: "Invalid argument passed to service", 1007: "Argument limit exceeded",
    1009: "Unsupported argument", 1010: "Unsupported operation", 1100: "User not found",
    1200: "Attribute name not found", 1207: "Attribute list contains duplicates", 1300: "Stream not found",
    1301: "Stream exists already", 1302: "Project not found", 1303: "Project exists already",
    1304: "Snapshot not found", 1317: "Unable to assign a disabled user as defect owner",
    1400: "Defect not found", 1401: "Defect edit conflict occurred", 1402: "Checker name invalid",
    1404: "Access to stream defect denied", 1408: "Defect edit during commit", 2000: "Triage store not found",
    2001: "Triage store already exists", 2002: "Triage store not deletable", 2003: "Invalid triage store",
}

# ---- checkers (5.9; values "likely": coverity-sonar-plugin coverity-cov-cpp.xml, csmock cwe-map) ----
# (checker, subcategory) -> properties. impact from the Sonar severity: BLOCKER High, CRITICAL Medium,
# MAJOR Low. localEffect texts other than those seen in real output are guesses.
_LE_NULL = "Subsequent code may dereference a null pointer, causing a crash."
_LE_LEAK = "The system resource will not be reclaimed and reused, reducing the future availability of the resource."
_LE_OVR = "Possible memory corruption or reading of incorrect values when accessing memory outside the buffer."
_LE_UNINIT = "Using an uninitialized value may cause incorrect computations or unpredictable behavior."
CHECKERS: dict[tuple[str, str], dict] = {
    ("NULL_RETURNS", "none"): dict(category="Null pointer dereferences", short="Dereference null return value",
                                   impact="Medium", cwe="476", kinds=["QUALITY"], local=_LE_NULL,
                                   long="Return value of function which returns null is dereferenced without checking"),
    ("FORWARD_NULL", "none"): dict(category="Null pointer dereferences", short="Dereference after null check",
                                   impact="Medium", cwe="476", kinds=["QUALITY"], local=_LE_NULL,
                                   long="Pointer is checked against null but then dereferenced anyway"),
    ("REVERSE_INULL", "none"): dict(category="Null pointer dereferences", short="Dereference before null check",
                                    impact="Medium", cwe="476", kinds=["QUALITY"], local=_LE_NULL,
                                    long="All paths that lead to this null pointer comparison already dereference the pointer earlier"),
    ("RESOURCE_LEAK", "none"): dict(category="Resource leaks", short="Resource leak", impact="High", cwe="404",
                                    kinds=["QUALITY"], local=_LE_LEAK,
                                    long="Leak of memory or pointers to system resources"),
    ("OVERRUN", "read"): dict(category="Memory - illegal accesses", short="Out-of-bounds read", impact="High",
                              cwe="119", kinds=["QUALITY", "SECURITY"], local=_LE_OVR,
                              long="Out-of-bounds read from a buffer"),
    ("OVERRUN", "write"): dict(category="Memory - corruptions", short="Out-of-bounds write", impact="High",
                               cwe="119", kinds=["QUALITY", "SECURITY"], local=_LE_OVR,
                               long="Out-of-bounds write to a buffer"),
    ("OVERRUN", "none"): dict(category="Memory - corruptions", short="Out-of-bounds access", impact="High",
                              cwe="119", kinds=["QUALITY", "SECURITY"], local=_LE_OVR,
                              long="Out-of-bounds access to a buffer"),
    ("OVERRUN", "strlen"): dict(category="Memory - corruptions", short="Allocation size error", impact="High",
                                cwe="119", kinds=["QUALITY", "SECURITY"], local=_LE_OVR,
                                long="Allocation size does not include space for the null-terminator needed for a string"),
    ("UNINIT", "none"): dict(category="Uninitialized variables", short="Uninitialized scalar variable",
                             impact="High", cwe="457", kinds=["QUALITY", "SECURITY"], local=_LE_UNINIT,
                             long="Use of an uninitialized variable"),
    ("UNINIT", "pointer_deref_read"): dict(
        category="Memory - illegal accesses", short="Uninitialized pointer read", impact="High", cwe="457",
        kinds=["QUALITY", "SECURITY"],
        local="Incorrect values could be read from, or even written to, an arbitrary memory location, causing incorrect computations.",
        long="Reads an uninitialized pointer or its target"),
    ("UNINIT_CTOR", "none"): dict(category="Uninitialized members", short="Uninitialized scalar field",
                                  impact="Medium", cwe="456", kinds=["QUALITY"],
                                  local="The field will contain an arbitrary value left over from earlier computations.",
                                  long="A scalar field is not initialized by the constructor"),
    ("CHECKED_RETURN", "none"): dict(category="Error handling issues", short="Unchecked return value",
                                     impact="Medium", cwe="252", kinds=["QUALITY"],
                                     local="The return value of a function may indicate an error that is not handled.",
                                     long="Value returned from a function is not checked for errors before being used"),
    ("CHECKED_RETURN", "library_function"): dict(
        category="Error handling issues", short="Unchecked return value from library", impact="Medium", cwe="252",
        kinds=["QUALITY"], local="The return value of a function may indicate an error that is not handled.",
        long="Value returned from a library function is not checked for errors before being used.  This value may indicate an error condition."),
    ("DEADCODE", "none"): dict(category="Control flow issues", short="Logically dead code", impact="Medium",
                               cwe="561", kinds=["QUALITY"],
                               local="The indicated dead code may have performed some action; that action will never occur.",
                               long="Code can never be reached because of a logical contradiction"),
    ("MISSING_BREAK", "none"): dict(category="Control flow issues", short="Missing break in switch",
                                    impact="Medium", cwe="484", kinds=["QUALITY"],
                                    local="Execution falls through to the next case statement or default; this might not be the intention.",
                                    long="Missing break statement between cases in switch statement"),
    ("USE_AFTER_FREE", "none"): dict(
        category="Memory - illegal accesses", short="Use after free", impact="High", cwe="416",
        kinds=["QUALITY", "SECURITY"],
        local="This could cause an immediate crash or incorrect values might be read subsequently resulting in incorrect computations.",
        long="A pointer to freed memory is  dereferenced, used as a function argument, or otherwise used"),
    ("USE_AFTER_FREE", "deref_read_after_free"): dict(
        category="Memory - illegal accesses", short="Read from pointer after free", impact="High", cwe="416",
        kinds=["QUALITY", "SECURITY"],
        local="Incorrect values may be read from freed memory, causing incorrect computations.",
        long="Reads target of a freed pointer"),
    ("USE_AFTER_FREE", "double_free"): dict(
        category="Memory - corruptions", short="Double free", impact="High", cwe="416", kinds=["QUALITY", "SECURITY"],
        local="The allocator's own data structures may be corrupted.", long="Memory is deallocated more than once"),
    ("STRING_OVERFLOW", "none"): dict(category="Memory - corruptions", short="Buffer overflow", impact="High",
                                      cwe="120", kinds=["QUALITY", "SECURITY"], local=_LE_OVR,
                                      long="Size of destination buffer is smaller than the size of the source buffer or size of the source buffer is unknown"),
    ("STRING_OVERFLOW", "fixed_size_dest"): dict(
        category="Security best practices violations", short="Copy into fixed size buffer", impact="Low",
        cwe="120", kinds=["SECURITY"],
        local="If the source string is longer than the destination buffer, memory after the buffer may be overwritten.",
        long="A source buffer of statically unknown size is copied into a fixed-size destination buffer"),
    ("DIVIDE_BY_ZERO", "none"): dict(category="Integer handling issues", short="Division or modulo by zero",
                                     impact="Medium", cwe="369", kinds=["QUALITY"],
                                     local="Division by zero results in undefined behavior, typically a crash.",
                                     long="Division or modulo by zero results in undefined behavior."),
    ("NEGATIVE_RETURNS", "none"): dict(category="Integer handling issues", short="Improper use of negative value",
                                       impact="Medium", cwe="687", kinds=["QUALITY", "SECURITY"],
                                       local="A negative value is used where only non-negative values are valid.",
                                       long="Negative value can be returned from function is not being checked before being used improperly"),
    ("NEGATIVE_RETURNS", "loop_bound"): dict(category="Program hangs", short="Negative loop bound", impact="Medium",
                                             cwe="394", kinds=["QUALITY", "SECURITY"],
                                             local="The loop may run far more times than intended.",
                                             long="Negative value used as a loop upper bound"),
    ("NEGATIVE_RETURNS", "array_index_write"): dict(
        category="Memory - corruptions", short="Negative array index write", impact="High", cwe="394",
        kinds=["QUALITY", "SECURITY"], local=_LE_OVR,
        long="Negative value used to index an array in a write operation"),
    ("NEGATIVE_RETURNS", "array_index_read"): dict(
        category="Memory - illegal accesses", short="Negative array index read", impact="High", cwe="394",
        kinds=["QUALITY", "SECURITY"], local=_LE_OVR, long="Negative value used to index an array in a read operation"),
    ("UNUSED_VALUE", "none"): dict(category="Code maintainability issues", short="Unused value", impact="Low",
                                   cwe="563", kinds=["QUALITY"],
                                   local="The value assigned is never used; the assignment may be an error.",
                                   long="A value assigned to a variable is never used."),
    ("ALLOC_FREE_MISMATCH", "none"): dict(category="API usage errors", short="Incorrect deallocator used",
                                          impact="Medium", cwe="762", kinds=["QUALITY"],
                                          local="The memory may not be released correctly, or the heap may be corrupted.",
                                          long="A resource is freed using the wrong deallocator"),
    ("NO_EFFECT", "self_assign"): dict(category="Incorrect expression", short="Self assignment", impact="Medium",
                                       cwe="398", kinds=["QUALITY"], local="The assignment has no effect.",
                                       long="Assignment of a variable or expression to itself has no effect"),
    ("NO_EFFECT", "unsigned_compare"): dict(category="Control flow issues", short="Unsigned compared against 0",
                                            impact="Medium", cwe="570", kinds=["QUALITY"],
                                            local="The comparison is always true or always false.",
                                            long="An unsigned value can never be less than 0"),
    ("TAINTED_SCALAR", "none"): dict(category="Insecure data handling", short="Use of untrusted scalar value",
                                     impact="Medium", cwe="20", kinds=["SECURITY"],
                                     local="An attacker may be able to control a sensitive operation.",
                                     long="An unscrutinized value from an untrusted source used in a trusted context"),
}
_PW = dict(category="Parse warnings", short="Parse warning", impact="Low", cwe="398", kinds=["QUALITY"],
           local="A parse warning may reduce maintainability of the code, or it may indicate a serious error.",
           long="A parse warning from the Coverity parser may indicate a bug, or poor coding practice")
for _name in ("PW.DECLARED_BUT_NOT_REFERENCED", "PW.SET_BUT_NOT_USED", "PW.BRANCH_PAST_INITIALIZATION"):
    CHECKERS[(_name, "none")] = dict(_PW)

# Checkers that cov-analyze runs without options, and the ones --all / --security add (guess
# based on [FT:b5WsY0QAGNQHwywzx9dZuA]: --all enables parse warnings and the security checkers).
DEFAULT_CHECKERS = {"NULL_RETURNS", "FORWARD_NULL", "REVERSE_INULL", "RESOURCE_LEAK", "OVERRUN", "UNINIT",
                    "UNINIT_CTOR", "CHECKED_RETURN", "DEADCODE", "MISSING_BREAK", "USE_AFTER_FREE",
                    "DIVIDE_BY_ZERO", "NEGATIVE_RETURNS", "UNUSED_VALUE", "ALLOC_FREE_MISMATCH", "NO_EFFECT",
                    "STRING_OVERFLOW"}
SECURITY_CHECKERS = {"TAINTED_SCALAR"}
PARSE_WARNING_CHECKERS = {"PW.DECLARED_BUT_NOT_REFERENCED", "PW.SET_BUT_NOT_USED", "PW.BRANCH_PAST_INITIALIZATION"}

# ---- MISRA C:2012 (5.11) --------------------------------------------------------------------
# Section titles as Coverity shows them in displayType (likely: Sonar rules). Rule headline texts
# are not reproduced (they belong to MISRA); the description says which rule is violated.
MISRA_C2012_SECTIONS = {
    1: "Standard C Environment", 2: "Unused Code", 3: "Comments", 4: "Character Sets", 5: "Identifiers",
    6: "Types", 7: "Literals and Constants", 8: "Declarations and Definitions", 9: "Initializers",
    10: "The Essential Type Model", 11: "Pointer Type Conversions", 12: "Expressions", 13: "Side Effects",
    14: "Control Flow Expressions", 15: "Control Flow", 16: "Switch Statements", 17: "Functions",
    18: "Pointers and Arrays", 19: "Overlapping Storage", 20: "Preprocessing Directives",
    21: "Standard Libraries", 22: "Resources",
}
_M, _R, _A = "Mandatory", "Required", "Advisory"
MISRA_C2012_CATEGORY = {
    "1.1": _R, "1.2": _A, "1.3": _R, "1.4": _R, "1.5": _R,
    "2.1": _R, "2.2": _R, "2.3": _A, "2.4": _A, "2.5": _A, "2.6": _A, "2.7": _A, "2.8": _A,
    "3.1": _R, "3.2": _R, "4.1": _R, "4.2": _A,
    "5.1": _R, "5.2": _R, "5.3": _R, "5.4": _R, "5.5": _R, "5.6": _R, "5.7": _R, "5.8": _R, "5.9": _A,
    "6.1": _R, "6.2": _R, "6.3": _R,
    "7.1": _R, "7.2": _R, "7.3": _R, "7.4": _R, "7.5": _M, "7.6": _R,
    "8.1": _R, "8.2": _R, "8.3": _R, "8.4": _R, "8.5": _R, "8.6": _R, "8.7": _A, "8.8": _R, "8.9": _A,
    "8.10": _R, "8.11": _A, "8.12": _R, "8.13": _A, "8.14": _R, "8.15": _R, "8.16": _A, "8.17": _A,
    "9.1": _M, "9.2": _R, "9.3": _R, "9.4": _R, "9.5": _R, "9.6": _R, "9.7": _M,
    "10.1": _R, "10.2": _R, "10.3": _R, "10.4": _R, "10.5": _A, "10.6": _R, "10.7": _R, "10.8": _R,
    "11.1": _R, "11.2": _R, "11.3": _R, "11.4": _A, "11.5": _A, "11.6": _R, "11.7": _R, "11.8": _R,
    "11.9": _R, "11.10": _R,
    "12.1": _A, "12.2": _R, "12.3": _A, "12.4": _A, "12.5": _M, "12.6": _R,
    "13.1": _R, "13.2": _R, "13.3": _A, "13.4": _A, "13.5": _R, "13.6": _R,
    "14.1": _R, "14.2": _R, "14.3": _R, "14.4": _R,
    "15.1": _A, "15.2": _R, "15.3": _R, "15.4": _A, "15.5": _A, "15.6": _R, "15.7": _R,
    "16.1": _R, "16.2": _R, "16.3": _R, "16.4": _R, "16.5": _R, "16.6": _R, "16.7": _R,
    "17.1": _R, "17.2": _R, "17.3": _M, "17.4": _M, "17.5": _A, "17.6": _M, "17.7": _R, "17.8": _A,
    "17.9": _M, "17.10": _R, "17.11": _A, "17.12": _A, "17.13": _R,
    "18.1": _R, "18.2": _R, "18.3": _R, "18.4": _A, "18.5": _A, "18.6": _R, "18.7": _R, "18.8": _R,
    "18.9": _R, "18.10": _M,
    "19.1": _M, "19.2": _A,
    "20.1": _A, "20.2": _R, "20.3": _R, "20.4": _R, "20.5": _A, "20.6": _R, "20.7": _R, "20.8": _R,
    "20.9": _R, "20.10": _A, "20.11": _R, "20.12": _R, "20.13": _R, "20.14": _R,
    "21.1": _R, "21.2": _R, "21.3": _R, "21.4": _R, "21.5": _R, "21.6": _R, "21.7": _R, "21.8": _R,
    "21.9": _R, "21.10": _R, "21.11": _R, "21.12": _A, "21.13": _M, "21.14": _R, "21.15": _R,
    "21.16": _R, "21.17": _M, "21.18": _M, "21.19": _M, "21.20": _M, "21.21": _R,
    "22.1": _R, "22.2": _M, "22.3": _R, "22.4": _M, "22.5": _M, "22.6": _M, "22.7": _R, "22.8": _R,
    "22.9": _R, "22.10": _R,
    "D4.1": _R, "D4.2": _A, "D4.3": _R, "D4.4": _A, "D4.5": _A, "D4.6": _A, "D4.7": _R, "D4.8": _A,
    "D4.9": _A, "D4.10": _R, "D4.11": _R, "D4.12": _R, "D4.13": _A, "D4.14": _R,
}
# Impact of coding standard checkers (5.11: Low in the real MISRA SOAP data; likely).
CODING_STANDARD_IMPACT = "Low"


def misra_checker(rule: str) -> str:
    """'10.4' -> 'MISRA C-2012 Rule 10.4'; 'D4.6' -> 'MISRA C-2012 Directive 4.6' (confirmed form)."""
    if rule.startswith("D"):
        return f"MISRA C-2012 Directive {rule[1:]}"
    return f"MISRA C-2012 Rule {rule}"


def misra_event_tag(checker: str) -> str:
    """'MISRA C-2012 Rule 10.4' -> 'misra_c_2012_rule_10_4_violation' (confirmed form)."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", checker.lower()).strip("_") + "_violation"


def misra_properties(rule: str) -> dict:
    section = int(rule.lstrip("D").split(".")[0])
    group = "Code Design" if rule.startswith("D") else MISRA_C2012_SECTIONS.get(section, "Rules")
    checker = misra_checker(rule)
    return dict(category="Coding standard violation", short=f"MISRA C-2012 {group}",
                impact=CODING_STANDARD_IMPACT, cwe="none", kinds=["QUALITY"],
                local="The code does not comply with the MISRA C:2012 guideline; it may be less portable or less safe.",
                long=f"Violation of {checker}", misra=MISRA_C2012_CATEGORY.get(rule, _R))


CERT_C_GROUPS = {"INT": "CERT-C Integers", "EXP": "CERT-C Expression", "ARR": "CERT-C Arrays",
                 "STR": "CERT-C Characters and Strings", "MEM": "CERT-C Memory Management",
                 "FIO": "CERT-C Input Output", "ERR": "CERT-C Error Handling"}


def cert_properties(rule: str) -> dict:
    """rule like 'INT30-C' (checker name 'CERT INT30-C', confirmed form)."""
    return dict(category="Coding standard violation", short=CERT_C_GROUPS.get(rule[:3], "CERT-C"),
                impact=CODING_STANDARD_IMPACT, cwe="none", kinds=["QUALITY"],
                local="The code does not comply with the SEI CERT C Coding Standard.",
                long=f"Violation of CERT {rule}")


def checker_properties(checker: str, subcategory: str) -> dict:
    """checkerProperties of JSON v7 (key order confirmed)."""
    if checker.startswith("MISRA C-2012 "):
        rule = checker.split()[-1]
        props = misra_properties(("D" + rule) if " Directive " in checker else rule)
    elif checker.startswith("CERT "):
        props = cert_properties(checker.split()[1])
    else:
        props = CHECKERS.get((checker, subcategory)) or CHECKERS.get((checker, "none"))
        if props is None:
            props = dict(category="Miscellaneous", short=checker, impact="Low", cwe="none", kinds=["QUALITY"],
                         local="", long="")
    out = {"category": props["category"], "categoryDescription": props["category"],
           "cweCategory": props["cwe"], "issueKinds": sorted(props["kinds"]), "eventSetCaptions": [],
           "impact": props["impact"], "impactDescription": props["impact"],
           "subcategoryLocalEffect": props["local"], "subcategoryShortDescription": props["short"],
           "subcategoryLongDescription": props["long"]}
    if props.get("misra"):
        out["MISRACategory"] = props["misra"]
    return out
