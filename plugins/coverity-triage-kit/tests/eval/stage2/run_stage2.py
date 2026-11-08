#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""EVAL stage 2: the worker agent alone triages one warning (or one group) with ``claude -p``.

For each case of cases.json (next to this file):

1. a fresh git repository is made from the self-test's sample (``assets/sample-target``: C code and a
   fake Coverity under ``.coverity-triage/``); the expected conclusions are its ``assets/expected.yaml``;
2. claude_adapter extracts the worker agent and the parts of its table only (``--parts-from-table``)
   into ``<repo>/.claude/`` (hidden from git status through .git/info/exclude), with the MCP server
   on the fake Coverity;
3. ``claude -p "<request>" --agent <worker>`` runs in the repository (stream-json transcript kept);
4. programmatic checks: the conclusion against expected.yaml, both proposals, the deviation comment
   through the deviation-comment rules script and the result through the result-record script (both
   run from the workspace copies of the skills), evidence file:line, files changed only where allowed,
   the one-line reply of the instruction file, the group fields;
5. optionally (--judge) a model grades the content (no tools) and its JSON is recorded.

Modes: ``user`` (a person in the chat; claude -p has no AskUserQuestion, so the request is complete
and asks not to change files) and ``brief`` (another agent: an instruction file in the run-folder
style asks to write result.json and to edit only a given work copy).

  uv run --native-tls --quiet tests/eval/stage2/run_stage2.py [--only NAME|N]... [--model M]
        [--judge] [--judge-model M] [--timeout S] [--max-turns N] [--out DIR] [--build-only] [--no-sync]

Writes <out>/<case>/ (repo, run, transcript, case.json), <out>/results.json and <out>/report.md.
Prints one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import evalkit as ek  # noqa: E402

TAG = "stage2"
CASES_FILE = HERE / "cases.json"
ROLE_WORKER = "作業役"
ROLE_SELFTEST = "自己診断"
ROLE_COMMENT = "逸脱コメント"
ROLE_REPORT = "結果の記録"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5-5"

LABELS = ["逸脱コメント案", "修正案", "確信度", "結論", "判定", "推奨", "根拠"]
REQUIRED_LABELS = ["判定", "推奨", "根拠", "修正案", "逸脱コメント案"]
JUDGEMENTS = ["true_bug", "false_positive", "intentional", "undetermined"]
JUDGEMENT_WORDS = {"本物": "true_bug", "誤検知": "false_positive", "意図的": "intentional", "判定できない": "undetermined"}
EXPECTED_JUDGEMENTS = {"fix": {"true_bug"}, "deviation": {"false_positive", "intentional"}}
CONF_JA = {"高": "high", "中": "medium", "低": "low"}
SOURCE_EXT = r"(?:c|h|cc|cpp|cxx|hpp|hh|hxx|inc)"
EVIDENCE_RE = re.compile(r"(?P<path>[A-Za-z0-9_./\\-]+\." + SOURCE_EXT + r")\s*[:：]\s*L?(?P<line>\d+)"
                         r"(?:\s*[-–〜~]\s*L?(?P<end>\d+))?")
_LABEL_ALT = "|".join(LABELS)
LABEL_LINE_RE = re.compile(r"^\s*(?:[-*>#]+\s*)?(?:\*\*)?(" + _LABEL_ALT + r")(?:\*\*)?\s*[：:]\s*(?:\*\*)?\s*(.*)$")
LABEL_ROW_RE = re.compile(r"^\s*\|\s*(?:\*\*)?(" + _LABEL_ALT + r")(?:\*\*)?\s*\|\s*(.*?)\s*\|?\s*$")
ONE_LINE_RE = re.compile(r"^(?P<id>[A-Za-z0-9]+)\s*[:：]\s*(?P<rec>修正|逸脱)(?:を推奨)?\s*[（(]\s*確信度\s*(?P<conf>高|中|低)\s*[）)]"
                         r"\s*[—―–\-ー]+\s*(?P<summary>\S.*)$")


# --------------------------------------------------------------------------- reading answers


def parse_report(text: str) -> dict[str, str]:
    """The labelled sections of the worker's chat report (結論・判定・…・逸脱コメント案)."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if current:
                sections[current].append(line)
            continue
        m = None if in_fence else (LABEL_LINE_RE.match(line) or LABEL_ROW_RE.match(line))
        if m and m.group(1) not in sections:
            current = m.group(1)
            sections[current] = [m.group(2).strip().rstrip("|").strip()]
            continue
        if current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def judgement_of(text: str) -> str | None:
    low = (text or "").lower()
    hits = [(low.find(j), j) for j in JUDGEMENTS if j in low]
    if hits:
        return min(hits)[1]
    hits = [(text.find(w), j) for w, j in JUDGEMENT_WORDS.items() if w in (text or "")]
    return min(hits)[1] if hits else None


def recommendation_of(text: str) -> str | None:
    text = text or ""
    hits = [(text.find(w), r) for w, r in (("修正", "fix"), ("逸脱", "deviation"), ("fix", "fix"),
                                            ("deviation", "deviation")) if w in text]
    return min(hits)[1] if hits else None


def evidence_in(text: str) -> list[dict]:
    out = []
    for m in EVIDENCE_RE.finditer(text or ""):
        item = {"file": m.group("path").replace("\\", "/"), "line": int(m.group("line"))}
        if m.group("end"):
            item["end"] = int(m.group("end"))
        if item not in out:
            out.append(item)
    return out


def strip_markup(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^\s*>\s?", "", text)
    text = text.replace("**", "").strip()
    for a, b in (("「", "」"), ("『", "』"), ('"', '"'), ("`", "`"), ("“", "”")):
        if text.startswith(a) and text.endswith(b) and len(text) >= 2:
            text = text[1:-1].strip()
    return text.strip("`").strip()


def comment_of(section: str) -> str | None:
    """The deviation comment of a 逸脱コメント案 section (without the Classification part)."""
    for line in (section or "").split("\n"):
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        line = re.split(r"[（(]\s*Classification", line, maxsplit=1)[0]
        line = strip_markup(line)
        if line:
            return line
    return None


def has_fix_proposal(section: str) -> bool:
    section = section or ""
    if re.search(r"最新.{0,20}直って|already_fixed", section):
        return True
    if len(section.strip()) < 15:
        return False
    return "```" in section or "@@" in section or bool(re.search(r"(?m)^\s*\+[^+]", section))


def one_line_reply(text: str) -> dict:
    lines = [ln.strip() for ln in (text or "").strip().split("\n") if ln.strip()]
    first = strip_markup(lines[0]) if lines else ""
    m = ONE_LINE_RE.match(first)
    return {"lines": len(lines), "text": first,
            "match": bool(m), **({"id": m.group("id"), "recommendation": "fix" if m.group("rec") == "修正" else "deviation",
                                  "confidence": CONF_JA[m.group("conf")]} if m else {})}


# --------------------------------------------------------------------------- checks


def check_evidence(repo: Path, evidence: list[dict]) -> dict:
    if not evidence:
        return ek.check("evidence", False, "根拠のファイルと行がありません")
    bad = []
    for e in evidence:
        rel = str(e.get("file") or "").replace("\\", "/")
        path = Path(rel)
        if path.is_absolute():
            if not ek.inside(path, repo):
                bad.append(f"{rel}: リポジトリの外")
                continue
            path = path.resolve()
        else:
            path = repo / (rel[2:] if rel.startswith("./") else rel)
        if not path.is_file():
            bad.append(f"{rel}: ファイルがありません")
            continue
        count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        last = max(int(e.get("line") or 0), int(e.get("end") or 0))
        if not 1 <= int(e.get("line") or 0) <= count or last > count:
            bad.append(f"{rel}:{e.get('line')}: 行がありません（{count} 行まで）")
    shown = "、".join(f"{e.get('file')}:{e.get('line')}" for e in evidence[:8])
    return ek.check("evidence", not bad, "；".join(bad) if bad else f"すべてある（{shown}）")


def script_check(check_id: str, script: Path | None, args: list[str], stdin_obj: dict | None = None,
                 level: str = "must") -> dict:
    if script is None or not script.is_file():
        return ek.check(check_id, None, "確かめのスクリプトがありません（スキルが取り出されていない）", level)
    data, res = ek.run_json(ek.uv_script(script, *args), timeout=180,
                            input_text=json.dumps(stdin_obj, ensure_ascii=False) if stdin_obj is not None else None)
    if res.get("missing"):
        return ek.check(check_id, None, "uv がないので確かめられません", level)
    if data is None:
        return ek.check(check_id, False, f"スクリプトの出力を読めません: {(res['stderr'] or res['stdout'])[-300:]}", level)
    problems = data.get("problems") or []
    detail = "；".join(str(p.get("message") if isinstance(p, dict) else p) for p in problems)[:600]
    return ek.check(check_id, bool(data.get("ok")), detail or ("合う" if data.get("ok") else str(data.get("error"))), level)


def changed_files_of(repo: Path) -> list[str]:
    files = []
    for line in ek.working_changes(repo):
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


def writes_of(summary: dict) -> list[str]:
    out = []
    for use in summary["tool_uses"]:
        if use["name"] in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = use["input"].get("file_path") or use["input"].get("notebook_path")
            if path:
                out.append(str(path))
    return out


def bash_calls(summary: dict) -> list[str]:
    return [str(u["input"].get("command", ""))[:200] for u in summary["tool_uses"] if u["name"] == "Bash"]


def every_time_check(table_skills: list[str], summary: dict) -> dict:
    """The skills the table says to use 毎回 (every time) were loaded (Skill tool or slash command)."""
    if not table_skills:
        return ek.check("table_skills_used", None, "表に「毎回」のスキルがありません", "should")
    missing = [s for s in table_skills if s not in summary["skills_loaded"]]
    return ek.check("table_skills_used", not missing,
                    ("読まなかった「毎回」のスキル: " + "、".join(missing)) if missing
                    else "「毎回」のスキルをすべて読んだ", "should")


def finished_check(run: dict, summary: dict) -> dict:
    problems = []
    if run["timed_out"]:
        problems.append("時間切れ")
    if run["exit_code"] not in (0, None) or not summary["has_result"]:
        problems.append(f"終了コード {run['exit_code']}" + ("、result の知らせなし" if not summary["has_result"] else ""))
    if summary.get("is_error"):
        problems.append(f"エラーで終わった（{summary.get('subtype')}）")
    return ek.check("finished", not problems, "；".join(problems) or f"{summary.get('num_turns')} ターンで終わった")


def user_checks(case: dict, ctx: dict, run: dict, summary: dict) -> tuple[list[dict], dict]:
    text = ek.final_text(summary)
    sections = parse_report(text)
    want = ctx["expected"]
    checks = [finished_check(run, summary)]
    missing = [label for label in REQUIRED_LABELS if label not in sections]
    checks.append(ek.check("report_shape", not missing,
                           f"ない項目: {'、'.join(missing)}" if missing else "決まった形の項目がそろう", "should"))
    rec = recommendation_of(sections.get("推奨", ""))
    judgement = judgement_of(sections.get("判定", "") or sections.get("結論", ""))
    if want.get("recommendation"):
        checks.append(ek.check("verdict", rec == want["recommendation"],
                               f"推奨 {rec}（想定 {want['recommendation']}: {want.get('reason', '')}）"))
        checks.append(ek.check("judgement", judgement in EXPECTED_JUDGEMENTS.get(want["recommendation"], set()),
                               f"判定 {judgement}", "should"))
    comment = comment_of(sections.get("逸脱コメント案", ""))
    fix_ok = has_fix_proposal(sections.get("修正案", ""))
    checks.append(ek.check("both_proposals", bool(comment) and fix_ok,
                           f"修正案 {'あり' if fix_ok else 'なし'}、逸脱コメント案 {'あり' if comment else 'なし'}"))
    if comment:
        checks.append(script_check("comment_rules", ctx["check_comment"], ["--stdin"], {"comment": comment}))
    else:
        checks.append(ek.check("comment_rules", False, "逸脱コメントが見つかりません"))
    checks.append(check_evidence(ctx["repo"], evidence_in(sections.get("根拠", "")) or evidence_in(text)))
    changed = changed_files_of(ctx["repo"])
    moved = ek.head(ctx["repo"]) != ctx["head"]
    checks.append(every_time_check(ctx["every_time"], summary))
    checks.append(ek.check("files_unchanged", not changed and not moved,
                           ("変わったファイル: " + "、".join(changed) if changed else "") + ("HEAD が変わった" if moved else "")
                           or "作業中のファイルは変わっていない"))
    found = {"recommendation": rec, "judgement": judgement, "comment": comment, "sections": sorted(sections)}
    return checks, found


def diff_of(work: Path) -> str:
    return ek.git(work, "diff", "--no-color", check=False)


def touched_lines(diff: str) -> dict[str, set[int]]:
    """Old-side line numbers each file's hunks change (for 'the fix covers every CID')."""
    out: dict[str, set[int]] = {}
    current: str | None = None
    old = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            current = None
        elif line.startswith("--- "):
            continue
        elif line.startswith("+++ "):
            name = line[4:].strip()
            current = name[2:] if name.startswith("b/") else name
            out.setdefault(current, set())
        elif line.startswith("@@"):
            m = re.match(r"^@@ -(\d+)", line)
            old = int(m.group(1)) if m else 0
        elif current is None:
            continue
        elif line.startswith("-"):
            out[current].add(old)
            old += 1
        elif line.startswith("+"):
            out[current].add(old)  # inserted next to this old line
        elif line.startswith(" "):
            old += 1
    return out


def brief_checks(case: dict, ctx: dict, run: dict, summary: dict) -> tuple[list[dict], dict]:
    item = case["item"]
    want = ctx["expected"]
    result_file: Path = ctx["result_file"]
    work: Path = ctx["work"]
    checks = [finished_check(run, summary)]
    reply = one_line_reply(ek.final_text(summary))
    checks.append(ek.check("one_line_reply", reply["match"] and reply["lines"] == 1 and reply.get("id") == item,
                           f"{reply['lines']} 行: {reply['text'][:160]}"))
    result = ek.read_json(result_file) if result_file.is_file() else None
    if not isinstance(result, dict):
        checks.append(ek.check("result_written", False, f"結果ファイルがないか、JSON ではありません: {result_file}"))
        result = {}
    else:
        cids = ",".join(str(c) for c in case["cids"])
        checks.append(script_check("result_valid", ctx["check_result"],
                                   ["check", "--result", str(result_file), "--item", item, "--cids", cids,
                                    "--impact", ctx["impact"]]))
        comment = (result.get("deviation") or {}).get("comment")
        checks.append(script_check("comment_rules", ctx["check_comment"], ["--stdin"], {"comment": comment or ""}))
        evidence = (result.get("verdict") or {}).get("evidence") or []
        checks.append(check_evidence(ctx["repo"], [e for e in evidence if isinstance(e, dict)]))
        if want.get("recommendation"):
            checks.append(ek.check("verdict", result.get("recommendation") == want["recommendation"],
                                   f"推奨 {result.get('recommendation')}（想定 {want['recommendation']}: {want.get('reason', '')}）"))
            checks.append(ek.check("judgement", (result.get("verdict") or {}).get("judgement")
                                   in EXPECTED_JUDGEMENTS.get(want["recommendation"], set()),
                                   f"判定 {(result.get('verdict') or {}).get('judgement')}", "should"))
    diff = diff_of(work)
    already = bool((result.get("fix") or {}).get("already_fixed_on_latest"))
    work_changed = changed_files_of(work)
    checks.append(ek.check("fix_in_work_copy", bool(diff.strip()) or already,
                           ("直したファイル: " + "、".join(work_changed)) if work_changed
                           else ("最新で直っている" if already else "作業用のコピーに修正がありません")))
    repo_changed = changed_files_of(ctx["repo"])
    moved = ek.head(ctx["repo"]) != ctx["head"]
    checks.append(ek.check("repo_unchanged", not repo_changed and not moved,
                           ("変わったファイル: " + "、".join(repo_changed) if repo_changed else "")
                           + ("HEAD が変わった" if moved else "") or "利用者のリポジトリは変わっていない"))
    allowed = [work, ctx["item_dir"]]
    outside = [w for w in writes_of(summary) if not any(ek.inside(Path(w), a) for a in allowed)]
    checks.append(ek.check("writes_only_allowed", not outside,
                           ("許されていない場所への書き込み: " + "、".join(outside)) if outside else "直すフォルダと結果・進み具合だけ"))
    checks.append(every_time_check(ctx["every_time"], summary))
    commands = bash_calls(summary)
    checks.append(ek.check("no_commands", not commands,
                           ("コマンドを使った: " + "；".join(commands[:3])) if commands else "コマンドを使っていない", "should"))
    progress_data = ek.read_json(ctx["item_dir"] / "progress.json")
    checks.append(ek.check("progress_written", isinstance(progress_data, dict) and progress_data.get("item") == item
                           and bool(progress_data.get("phase")),
                           json.dumps(progress_data, ensure_ascii=False)[:160] if progress_data else "進み具合がありません",
                           "should"))
    if len(case["cids"]) > 1:
        excluded = result.get("group_excluded_cids")
        valid = (isinstance(excluded, list) and set(excluded) <= set(case["cids"])
                 and len(excluded) < len(case["cids"]) and result.get("item") == item)
        checks.append(ek.check("group_fields", valid, f"item {result.get('item')}、group_excluded_cids {excluded}"))
        want_group = set(ctx.get("expected_group") or [])
        if want_group and want_group == set(case["cids"]):
            checks.append(ek.check("group_kept", excluded == [], f"外した CID {excluded}（想定: 同じ原因で外さない）", "should"))
        lines = {c: ctx["lines"].get(c) for c in case["cids"]}
        covered = touched_lines(diff)
        missing = [c for c, (f, ln) in lines.items() if f is None or ln not in covered.get(f, set())]
        checks.append(ek.check("fix_covers_group", not missing,
                               f"修正のない CID: {missing}" if missing else "すべての CID の行を直した", "should"))
    found = {"result": result, "reply": reply, "diff": diff[:6000], "writes": writes_of(summary)}
    return checks, found


# --------------------------------------------------------------------------- the sample and the brief


def sample_data(selftest: Path) -> dict:
    sample = selftest / "assets" / "sample-target"
    expected_file = selftest / "assets" / "expected.yaml"
    if not sample.is_dir() or not expected_file.is_file():
        raise ek.EvalError(f"見本がありません: {sample}", ["--sample で見本のフォルダを渡します"])
    expected = ek.load_yaml(expected_file) or {}
    fake = ek.load_yaml(sample / ".coverity-triage" / "fake-issues.yaml") or {}
    issues = {int(i["cid"]): i for i in fake.get("issues") or [] if isinstance(i, dict) and "cid" in i}
    return {"sample": sample, "expected": expected, "issues": issues}


def expected_for(case: dict, expected: dict) -> dict:
    if len(case["cids"]) == 1:
        want = (expected.get("items") or {}).get(str(case["cids"][0])) or {}
        return {"recommendation": want.get("recommendation"), "reason": want.get("reason")}
    return {"recommendation": None, "reason": "同じ原因のグループ（想定のグループ: "
            + str((expected.get("group") or {}).get("cids")) + "）"}


def numbered(path: Path, lines: set[int], main: set[int], pad: int = 3) -> list[str]:
    if not path.is_file() or not lines:
        return ["  （コードを読めません）"]
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    lo, hi = max(1, min(lines) - pad), min(len(text), max(lines) + pad)
    return ["```c"] + [f"{'★' if n in main else ' '}{n:4d}  {text[n - 1]}" for n in range(lo, hi + 1)] + ["```"]


def warning_lines(issues: dict, cids: list[int], repo: Path) -> list[str]:
    out = []
    for cid in cids:
        issue = issues.get(cid) or {}
        events = [e for e in issue.get("events") or [] if isinstance(e, dict)]
        out += [f"### CID {cid} — {issue.get('checker')}（Impact {issue.get('impact') or '-'}）", "",
                f"- 場所: `{issue.get('file')}`:{issue.get('line')} {issue.get('function')}()",
                f"- CWE: {issue.get('cwe') or '-'}", "- 警告経路（★ は問題の起きる場所）:", ""]
        out += [f"  {n}. {'★ ' if e.get('main') else ''}`{e.get('file')}`:{e.get('line')} {e.get('tag')} — {e.get('description')}"
                for n, e in enumerate(events, 1)] or ["  （取れませんでした。警告の場所から調べます）"]
        same = [e for e in events if e.get("file") == issue.get("file") and e.get("line")]
        wanted = {int(e["line"]) for e in same} | ({int(issue["line"])} if issue.get("line") else set())
        main = {int(e["line"]) for e in same if e.get("main")}
        out += ["", "前後のコード（調べるコードから。★ は問題の起きる行）:", ""]
        out += numbered(repo / str(issue.get("file")), wanted, main)
        out.append("")
        if issue.get("checker_description"):
            out += ["チェッカーの説明（Coverity）:", "", str(issue["checker_description"]), ""]
    return out


def render_brief(case: dict, issues: dict, repo: Path, work: Path, item_dir: Path, schema: Path, latest: str) -> str:
    item = case["item"]
    cids = case["cids"]
    rows = [("調べるコード（読むだけ。変えない）", repo), ("直すフォルダ（作業用のコピー。直すのはここだけ）", work),
            ("結果ファイル（書く）", item_dir / "result.json"), ("結果ファイルの形（JSON Schema）", schema),
            ("進み具合（書く。節目ごとに上書き）", item_dir / "progress.json")]
    example = json.dumps({"item": item, "phase": "judge", "note": "呼び出し元を確かめている"}, ensure_ascii=False)
    lines = [f"# 作業の指示: {item}", "",
             f"Coverity の警告 {len(cids)} 件（CID {', '.join(map(str, cids))}）が本物の不具合かを調べ、**修正案と逸脱コメント案の両方**を"
             "作って、結果ファイルに書きます。どちらを採るかは人が決めます。", "",
             "## フォルダとファイル", "", "| 用途 | 場所 |", "|---|---|", *[f"| {k} | `{v}` |" for k, v in rows], "",
             "## 守ること", "",
             "- コマンド（ターミナル）は使いません。ファイルを読む・探す・直す道具だけを使います。",
             "- 直してよいのは、直すフォルダ、結果ファイル、進み具合のファイルだけです。調べるコードと、利用者のリポジトリは変えません。",
             "- パスは、どのフォルダでもリポジトリの一番上からの相対パスです（結果ファイルの evidence も同じ）。",
             "- 解析したリビジョンが分からないため、調べるコードは利用者の手元のコードです。警告の行と内容が合っているか"
             "（ずれ）を確かめ、結果の revision_drift に書きます。",
             f"- 直すフォルダは、取り込み先の最新のリビジョン {latest} です。最新でもう直っているときは、何も直さずに "
             "fix.already_fixed_on_latest を true にします。",
             "- 結果ファイルは最後に書きます（書いた後で直すフォルダを変えない）。", "",
             "## 進み具合", "",
             f"節目ごとに、進み具合のファイルへ `{example}` の形で上書きします（人が画面で見ます。書けなくても作業は続けます）。"
             "phase は read（警告経路とコードを読む）→ judge（真偽を決める）→ fix（修正を作る）→ deviation（逸脱コメントを書く）"
             "→ result（結果を書く）の順です。note は 60 文字ほどで、時刻は書きません。", "",
             "## 警告", "", *warning_lines(issues, cids, repo)]
    if len(cids) > 1:
        lines += ["## グループ", "",
                  "同じ原因と思われる警告をまとめた作業です。すべてが同じ原因かを確かめ、原因の違う CID は "
                  "group_excluded_cids に入れます（その CID は別の作業になります。迷うときは外します。1 つは残します）。"
                  "修正案と逸脱コメント案は、残した CID に対して作ります。", ""]
    lines += ["## 返す形", "", "結果ファイルを書いたら、呼び出し元に次の形の 1 行だけを返します。",
              f"`{item}: <推奨（修正 か 逸脱）>（確信度 <高 / 中 / 低>）— <40 文字ほどの要約>`", "",
              "続けられない問題があったときは、結果ファイルを書かずに、理由を 1 行で返します。", ""]
    return "\n".join(lines)


def judge_prompt(case: dict, ctx: dict, found: dict, text: str) -> str:
    code = (ctx["repo"] / "src" / "reader.c")
    code_lines = code.read_text(encoding="utf-8", errors="replace").splitlines() if code.is_file() else []
    want = ctx["expected"]
    answer = text if case["mode"] == "user" else json.dumps(found.get("result"), ensure_ascii=False, indent=2) + \
        "\n\n修正の差分:\n" + (found.get("diff") or "（なし）")
    return "\n".join([
        "あなたは Coverity の警告のトリアージの結果を採点する人です。次の警告について、作業エージェントの答えを採点してください。",
        "", "## 警告", "", *warning_lines(ctx["issues"], case["cids"], ctx["repo"]),
        "## コード（src/reader.c、行番号つき）", "", "```c", *[f"{n:4d}  {t}" for n, t in enumerate(code_lines, 1)], "```", "",
        "## 想定（人が決めた答え）", "",
        f"- 推奨: {want.get('recommendation') or '（決めていない）'} — {want.get('reason') or ''}", "",
        "## 作業エージェントの答え", "", answer[:12000], "",
        "## 採点", "",
        "次の JSON だけを返してください（ほかの文は書かない）。点は 1（悪い）〜5（よい）。",
        '{"verdict_correct": true, "reasoning": 5, "evidence": 5, "fix": 5, "deviation_comment": 5, "overall": 5, "notes": "<日本語で100文字ほど>"}',
        "- verdict_correct: 判定と推奨が想定と合い、根拠がコードと合っているか（想定がないときは、コードから見て正しいか）",
        "- reasoning: 警告経路を仮説として、呼び出し元や定義まで確かめたか。「たぶん」「ふつうは」で決めていないか",
        "- evidence: 根拠の場所（リポジトリからのパスと行）が正しいか",
        "- fix: 修正が最小で、外から見える仕様を変えず、新しい MISRA・CERT の違反を作らず、周りの書き方に合うか",
        "- deviation_comment: 「誤検知。」「意図的。」「本物の不具合。」で始まる1〜2文、理由 → 場所の順、200文字以内、"
        "条件つきの言い方・調べた過程・依頼の言い方がないか",
    ])


# --------------------------------------------------------------------------- running a case


def adapter_args(**kw) -> argparse.Namespace:
    base = dict(workspace=None, parts_from_table=None, agent=None, skill=None, mcp=None, plugin_root=None, main=None,
                output_dir=None, fake_data=None, mcp_env=None, model=None, agent_model=None, prompt=None, extra_arg=None,
                claude_bin=None, allow_missing=False, run=False, inspect=False, timeout=120.0)
    base.update(kw)
    return argparse.Namespace(**base)


def skill_script(ws: Path, name: str | None, script: str) -> Path | None:
    if name:
        path = ws / ".claude" / "skills" / name / "scripts" / script
        if path.is_file():
            return path
    found = sorted((ws / ".claude" / "skills").glob(f"*/scripts/{script}"))
    return found[0] if found else None


def role_name(role: str, root: Path) -> str | None:
    try:
        return ek.part(role, root)["name"]
    except ek.EvalError:
        return None


def prepare_case(case: dict, args: argparse.Namespace, data: dict, root: Path, out: Path, worker: Path,
                 model: str | None) -> dict:
    case_dir = out / case["name"]
    repo = case_dir / "repo"
    head = ek.make_repo(data["sample"], repo)
    run_dir = case_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    venv = case_dir / "mcp-venv"
    build = ek.ADAPTER.build(adapter_args(
        workspace=str(repo), parts_from_table=str(worker), plugin_root=str(root), output_dir=str(run_dir),
        fake_data=str(repo / ".coverity-triage" / "fake-issues.yaml"), mcp_env=[f"UV_PROJECT_ENVIRONMENT={venv}"],
        model=model, agent_model=args.agent_model or model, claude_bin=args.claude_bin))
    ek.exclude_in_git(repo, ".claude/")
    ws = Path(build["workspace"])
    comment_name, report_name = role_name(ROLE_COMMENT, root), role_name(ROLE_REPORT, root)
    ctx = {"case_dir": case_dir, "repo": repo, "head": head, "run_dir": run_dir, "build": build, "venv": venv,
           "issues": data["issues"], "expected": expected_for(case, data["expected"]),
           "expected_group": (data["expected"].get("group") or {}).get("cids"),
           "check_comment": skill_script(ws, comment_name, "check_comment.py"),
           "check_result": skill_script(ws, report_name, "check_result.py"),
           "impact": str((data["issues"].get(case["cids"][0]) or {}).get("impact") or "Unspecified"),
           "every_time": [r["part"] for r in ek.ADAPTER.load_agent(worker)["table"]
                          if r["when"] == "毎回" and r["part"] in build["skills"]],
           "lines": {c: (str((data["issues"].get(c) or {}).get("file")), int((data["issues"].get(c) or {}).get("line") or 0))
                     for c in case["cids"]}}
    prompt = case["prompt"].replace("{repo}", str(repo))
    if case["mode"] == "brief":
        work = run_dir / "work" / "w1"
        work.parent.mkdir(parents=True, exist_ok=True)
        ek.git(repo, "worktree", "add", "-q", "--detach", str(work), "HEAD")
        item_dir = run_dir / "items" / case["item"]
        item_dir.mkdir(parents=True, exist_ok=True)
        schema_src = (ws / ".claude" / "skills" / report_name / "assets" / "result.schema.json") if report_name else None
        schema = run_dir / "schemas" / "result.schema.json"
        if schema_src and schema_src.is_file():
            ek.write_text(schema, schema_src.read_text(encoding="utf-8"))
        brief = ek.write_text(item_dir / "brief.md",
                              render_brief(case, data["issues"], repo, work, item_dir, schema, head))
        prompt = prompt.replace("{brief}", str(brief))
        ctx.update({"work": work, "item_dir": item_dir, "result_file": item_dir / "result.json", "brief": brief})
    ctx["prompt"] = prompt
    saved = ek.read_json(ws / ".claude" / "eval" / "command.json")
    argv = list(saved["argv"])
    argv[saved["prompt_index"]] = prompt
    argv += ["--max-turns", str(args.max_turns), "--output-format", "stream-json", "--verbose"]
    if args.max_budget_usd:
        argv += ["--max-budget-usd", str(args.max_budget_usd)]
    ctx["argv"] = argv
    ctx["cwd"] = Path(saved["cwd"])
    return ctx


def run_case(case: dict, args: argparse.Namespace, data: dict, root: Path, out: Path, worker: Path,
             model: str | None) -> dict:
    ek.progress(TAG, f"{case['name']}: 用意")
    ctx = prepare_case(case, args, data, root, out, worker, model)
    record = {"name": case["name"], "mode": case["mode"], "cids": case["cids"], "item": case.get("item"),
              "prompt": ctx["prompt"], "expected": ctx["expected"], "repo": str(ctx["repo"]),
              "command": ctx["argv"], "cwd": str(ctx["cwd"]), "build_problems": ctx["build"].get("problems")}
    if not args.no_sync:
        for name, folder in ctx["build"]["mcp_servers"].items():
            sync = ek.uv_sync(Path(folder), env=ek.claude_env({"UV_PROJECT_ENVIRONMENT": str(ctx["venv"])}))
            if sync["code"] != 0:
                raise ek.EvalError(f"MCP サーバー {name} の用意（uv sync）が失敗しました", [sync["stderr"][-1500:]])
    if args.build_only:
        record.update({"built": True, "checks": [], "passed": None})
        return record
    ek.progress(TAG, f"{case['name']}: claude -p（{case['mode']}）")
    run = ek.run_claude(ctx["argv"], ctx["cwd"], args.timeout, ctx["case_dir"] / "transcript.jsonl")
    summary = ek.summarize(run["events"])
    checks, found = (user_checks if case["mode"] == "user" else brief_checks)(case, ctx, run, summary)
    record.update({
        "checks": checks, "passed": ek.verdict_of(checks), "found": found,
        "reply": ek.final_text(summary)[:6000], "seconds": run["seconds"], "cost_usd": summary.get("cost_usd"),
        "num_turns": summary.get("num_turns"), "tools": summary["main_tool_counts"],
        "skills_loaded": summary["skills_loaded"], "tool_errors": summary["tool_errors"][:10],
        "init": summary.get("init"), "transcript": run["transcript"], "timed_out": run["timed_out"],
        "exit_code": run["exit_code"]})
    if args.judge:
        ek.progress(TAG, f"{case['name']}: 採点（{args.judge_model}）")
        record["judge"] = ek.llm_judge(judge_prompt(case, ctx, found, record["reply"]), args.judge_model,
                                       args.claude_bin or ["claude"], ctx["case_dir"],
                                       ctx["case_dir"] / "judge.jsonl")
    ek.write_json(ctx["case_dir"] / "case.json", record)
    ek.progress(TAG, f"{case['name']}: {'合格' if record['passed'] else '不合格'}")
    return record


def select_cases(cases: list[dict], only: list[str] | None) -> list[dict]:
    if not only:
        return cases
    picked = []
    for word in only:
        for token in [w.strip() for w in word.split(",") if w.strip()]:
            hit = [c for i, c in enumerate(cases, 1) if c["name"] == token or str(i) == token]
            if not hit:
                raise ek.EvalError(f"課題 {token} がありません", [f"{i}. {c['name']}" for i, c in enumerate(cases, 1)])
            picked += [c for c in hit if c not in picked]
    return picked


def render_report(meta: dict, records: list[dict]) -> str:
    done = [r for r in records if r.get("passed") is not None]
    passed = sum(1 for r in done if r["passed"])
    cost = sum(r.get("cost_usd") or 0 for r in records) + sum((r.get("judge") or {}).get("cost_usd") or 0 for r in records)
    lines = ["# 2段階目（作業エージェント）の EVAL の結果", "",
             f"- 日時: {meta['started_at']}", f"- モデル: `{meta['model']}`（採点: {meta['judge'] or 'しない'}）",
             f"- 作業エージェント: `{meta['worker']}` と、その表の部品（{', '.join(meta['parts'])}）",
             f"- 見本: `{meta['sample']}`", "",
             "## まとめ", "", f"- **合格**: {passed}/{len(done)}（「must」の確かめがすべて通った課題）",
             f"- **時間の合計**: {sum(r.get('seconds') or 0 for r in records):.0f} 秒",
             f"- **費用の合計**: {ek.money(cost)}", ""]
    lines += ek.md_table(["課題", "形", "合否", "落ちた確かめ", "時間（秒）", "ターン", "費用"],
                         [[r["name"], r["mode"], ek.mark(r.get("passed")),
                           "、".join(c["id"] for c in r.get("checks", []) if c["ok"] is False) or "－",
                           r.get("seconds", "－"), r.get("num_turns", "－"), ek.money(r.get("cost_usd"))] for r in records])
    for r in records:
        lines += ["", f"## {r['name']}（{r['mode']}、CID {', '.join(map(str, r['cids']))}）", "",
                  f"頼みの文: {r['prompt']}", ""]
        lines += ek.md_table(["確かめ", "重さ", "結果", "詳しく"],
                             [[c["id"], c["level"], ek.mark(c["ok"]), c["detail"]] for c in r.get("checks", [])])
        if r.get("judge"):
            g = r["judge"].get("grade") or {}
            lines += ["", f"採点（{r['judge'].get('model')}）: " + (json.dumps(g, ensure_ascii=False) if g else r["judge"].get("error", ""))]
        if r.get("reply"):
            lines += ["", "<details><summary>返事</summary>", "", "```text", r["reply"][:4000], "```", "", "</details>"]
    return "\n".join(lines) + "\n"


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="2段階目（作業エージェントだけ）の EVAL を claude -p で動かす")
    p.add_argument("--plugin-root")
    p.add_argument("--cases", default=str(CASES_FILE), help="課題のファイル（既定は隣の cases.json）")
    p.add_argument("--only", action="append", help="動かす課題の名前か番号（何度でも。カンマ区切りも可）")
    p.add_argument("--sample", help="見本のフォルダ（assets/sample-target と assets/expected.yaml のあるフォルダ）")
    p.add_argument("--model", help="モデル（既定は作業エージェントの定義の claude-* のモデル）")
    p.add_argument("--agent-model", help="変換したエージェントの model（既定は --model）")
    p.add_argument("--judge", action="store_true", help="モデルに中身を採点させる（ツールなし）")
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    p.add_argument("--timeout", type=float, default=900.0, help="1課題の時間の上限（秒）")
    p.add_argument("--max-turns", type=int, default=80)
    p.add_argument("--max-budget-usd", type=float, help="1課題の費用の上限（claude の --max-budget-usd）")
    p.add_argument("--out", help="結果のフォルダ（既定 $TMPDIR/<プラグイン>-evals/stage2/<日時>）")
    p.add_argument("--build-only", action="store_true", help="作業フォルダとコマンドを作るだけ（claude を動かさない）")
    p.add_argument("--no-sync", action="store_true", help="MCP サーバーの uv sync をしない")
    p.add_argument("--claude-bin", nargs="+", help="claude の代わりに動かすコマンド（試験用）")
    return p


def run(args: argparse.Namespace) -> dict:
    root = Path(args.plugin_root).resolve() if args.plugin_root else ek.PLUGIN_ROOT
    worker_part = ek.part(ROLE_WORKER, root)
    selftest = Path(args.sample).resolve() if args.sample else ek.part(ROLE_SELFTEST, root)["path"]
    cases = select_cases((ek.read_json(Path(args.cases)) or {}).get("cases") or [], args.only)
    if not cases:
        raise ek.EvalError(f"課題がありません: {args.cases}")
    data = sample_data(selftest)
    out = ek.prepare_out(args.out, "stage2", root)
    agent = ek.ADAPTER.load_agent(worker_part["path"])
    model = args.model or ek.ADAPTER.claude_model(agent["fields"].get("model"))
    model = None if model == "inherit" else model
    collected = ek.ADAPTER.collect_parts(worker_part["path"], ek.ADAPTER.Layout(root))
    meta = {"started_at": datetime.now().isoformat(timespec="seconds"), "model": model or "（claude の既定）",
            "judge": args.judge_model if args.judge else None, "worker": worker_part["name"],
            "parts": sorted(collected["skills"]) + sorted(collected["mcp"]), "sample": str(selftest), "out": str(out)}
    records = [run_case(c, args, data, root, out, worker_part["path"], model) for c in cases]
    report = ek.write_text(out / "report.md", render_report(meta, records))
    done = [r for r in records if r.get("passed") is not None]
    result = {"ok": True, "kind": "stage2", "out": str(out), "report": str(report), "meta": meta,
              "passed": sum(1 for r in done if r["passed"]), "total": len(done),
              "cost_usd": round(sum(r.get("cost_usd") or 0 for r in records), 6), "cases": records}
    ek.write_json(out / "results.json", result)
    return {**{k: v for k, v in result.items() if k != "cases"},
            "cases": [{"name": r["name"], "passed": r.get("passed"),
                       "failed": [c["id"] for c in r.get("checks", []) if c["ok"] is False],
                       "seconds": r.get("seconds"), "cost_usd": r.get("cost_usd"),
                       **({"command": r["command"]} if args.build_only else {})} for r in records]}


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        ek.emit({"ok": False, "error": "引数が正しくありません（--help で使い方を見られます）", "problems": []})
        return 1
    try:
        result = run(args)
    except (ek.EvalError, ek.ADAPTER.AdapterError) as exc:
        ek.emit({"ok": False, "error": str(exc), "problems": getattr(exc, "problems", [])})
        return 1
    except Exception as exc:  # noqa: BLE001 - never print a traceback
        ek.emit({"ok": False, "error": f"思わぬエラーが起きました: {type(exc).__name__}: {exc}", "problems": []})
        return 1
    ek.emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
