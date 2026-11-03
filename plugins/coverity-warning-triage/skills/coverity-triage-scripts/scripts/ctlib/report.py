"""Detailed reports (``cid/<ID>.html``) and the summary (``summary.html``), with Markdown copies.

The worker subagent writes structured data (result.json); this module lays it out, so every report
has the same shape. The reports only show things: approvals and edits of the deviation are entered
in the screen (``ct.py gui``) and kept in ``approvals.json``. The Markdown copies (option
``markdown``) keep the approval column and the deviation section of earlier versions, so the runs
made before approvals.json can still be read back.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .common import CtError, display_text, resolve_path
from .models import Attributes, Event, Issue, TriageResult

JUDGEMENT_JA = {"false_positive": "誤検知", "true_bug": "本物のバグ", "intentional": "意図的",
                "undetermined": "判定できない"}
PLAN_JA = {"fix": "修正", "deviation": "逸脱"}
CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
DRIFT_JA = {"none": "なし", "detected": "あり", "unknown": "不明"}
APPROVALS = {"修正": "fix", "逸脱": "deviation", "却下": "reject"}
DECISION_JA = {v: k for k, v in APPROVALS.items()}
GUI_NOTE = "承認（修正／逸脱／却下）と逸脱コメントの手直しは、画面（ct.py gui）の「結果」で入力します。"

DEV_BEGIN = "<!-- ct:begin deviation -->"
DEV_END = "<!-- ct:end deviation -->"
MAX_DIFF_LINES = 400
SUMMARY_HEADER = "| 承認 | ID | 推奨 | 確信度 | 見立て | 詳細 |"


@dataclass
class ItemReport:
    """Everything needed to render one item."""

    item_id: str
    issues: list[Issue]
    events: dict[int, list[Event]]
    checker_description: str
    result: TriageResult
    saved: dict[str, dict[str, Any]] = field(default_factory=dict)   # "fix" / "annotation" -> saved fix
    verify: dict[str, Any] = field(default_factory=dict)
    analyzed_revision: str | None = None
    latest_revision: str | None = None
    seconds: float | None = None
    notes: list[str] = field(default_factory=list)
    run_dir: Path | None = None
    review: dict[str, Any] = field(default_factory=dict)  # review.json, or {"outcome": "unverified"}
    merged_from: list[str] = field(default_factory=list)  # the items the integration took into this one

    def rel(self, path: str) -> str:
        """Paths inside the run folder, relative to the report (cid/<ID>.html and cid/<ID>.md)."""
        if self.run_dir:
            try:
                return "../" + resolve_path(path).relative_to(resolve_path(self.run_dir)).as_posix()
            except ValueError:
                pass
        return path


ALREADY_FIXED_NOTE = ("最新のリビジョンではもう直っているため、取り込む修正（差分）はありません。"
                      "承認は「反映しない」で構いません。次の解析で警告が消えるか確かめてください")

def item_title(item_id: str, count: int) -> str:
    return f"{item_id}（{count} 件）" if item_id.startswith("G") else item_id


def _attrs(attrs: Attributes) -> list[str]:
    return [f"- Classification: {attrs.classification}", f"- Action: {attrs.action}",
            f"- Severity: {attrs.severity}"]


def shown_deviation(result: TriageResult, decision: dict[str, Any] | None) -> tuple[Attributes, str, bool]:
    """The deviation to show: the person's edit from approvals.json, else the AI's proposal."""
    edit = (decision or {}).get("deviation")
    proposed = result.deviation
    if not isinstance(edit, dict):
        return Attributes(**proposed.model_dump(exclude={"comment"})), proposed.comment, False
    values = {k: str(edit.get(k) or "").strip() or getattr(proposed, k)
              for k in ("classification", "action", "severity")}
    return Attributes(**values), str(edit.get("comment") or "").strip() or proposed.comment, True


def _diff_block(path: str | None) -> list[str]:
    if not path or not Path(path).is_file():
        return []
    lines = display_text(Path(path).read_bytes()).splitlines()
    shown = lines[:MAX_DIFF_LINES]
    # A fence longer than any run of backticks in the diff, so the diff cannot close it.
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", "\n".join(shown))), default=0) + 1)
    out = [fence + "diff", *shown, fence]
    if len(lines) > MAX_DIFF_LINES:
        out.append(f"（差分が長いため先頭 {MAX_DIFF_LINES} 行だけ表示しています。全体は差分ファイルを見てください）")
    return out


def render_item(r: ItemReport, decision: dict[str, Any] | None = None) -> str:
    """Markdown copy of the detailed report. ``decision`` is the item of approvals.json, if any."""
    res, main = r.result, r.issues[0]
    out: list[str] = [f"# {item_title(r.item_id, len(r.issues))} — {main.checker}（{main.impact or '-'}）", ""]

    # 1. conclusion
    notes = [f"リビジョンのずれ {DRIFT_JA[res.revision_drift.status]}"
             + (f"（{res.revision_drift.detail}）" if res.revision_drift.detail else "")]
    if res.fix.exceeded_constraints:
        notes.append("修正の制約を超えた点あり: " + "、".join(res.fix.exceeded_constraints))
    if res.fix.already_fixed_on_latest:
        notes.append("最新のリビジョンでは直っています" if r.saved.get("fix") else ALREADY_FIXED_NOTE)
    notes += r.notes
    out += ["## 1. 結論", "",
            f"- 推奨: **{PLAN_JA[res.recommendation]}** ／ 確信度: **{CONFIDENCE_JA[res.confidence]}**"
            f"（{res.confidence_reason}）",
            f"- 見立て: {JUDGEMENT_JA[res.verdict.judgement]}（{res.verdict.summary}）",
            f"- 注意: {' ／ '.join(notes)}", ""]

    # 2. overview
    out += ["## 2. 警告の概要", "", "| CID | チェッカー | ファイル | 関数 | 行 | Impact | CWE |",
            "|---|---|---|---|---|---|---|"]
    for i in r.issues:
        out.append(f"| {i.cid} | {i.checker} | {i.file} | {i.function or '-'} | {i.line or '-'} | "
                   f"{i.impact or '-'} | {i.cwe or '-'} |")
    if r.checker_description:
        out += ["", f"チェッカーの説明: {r.checker_description}"]
    out.append("")

    # 3. evidence
    out += ["## 3. 真偽の根拠", ""]
    for i in r.issues:
        out += [f"警告経路（CID {i.cid}）:", ""]
        events = r.events.get(i.cid) or []
        out += [f"{n}. {'★ ' if e.main else ''}{e.file}:{e.line or '-'} `{e.tag}` {e.description}"
                for n, e in enumerate(events, 1)] or ["（警告経路を取得できませんでした）"]
        out.append("")
    out += ["調査結果:", "", res.verdict.rationale, ""]
    out += [f"- {ev.file}:{ev.line or '-'} {ev.note}" for ev in res.verdict.evidence]
    out.append("")

    # 4. deviation (the markers let runs made before approvals.json be read back)
    attrs, comment, edited = shown_deviation(res, decision)
    out += ["## 4. 案A: 逸脱", "",
            "「逸脱」で反映すると、この内容が Coverity に登録されます。内容は画面（ct.py gui）の「結果」で直せます"
            "（このファイルを書き換えても反映されません）。", ""]
    if edited:
        out += ["（人が画面で直した内容です）", ""]
    out += [DEV_BEGIN, *_attrs(attrs), "- 逸脱コメント:", "", comment, DEV_END, ""]
    annotation = r.saved.get("annotation")
    if annotation:
        out += ["アノテーション（ソースに入れる注釈）の差分:", "", *_diff_block(annotation.get("patch")), ""]

    # 5. fix (diff inline)
    fix = res.fix
    out += ["## 5. 案B: 修正", "", f"- 概要: {fix.summary}", f"- 影響とリスク: {fix.impact}"]
    saved = r.saved.get("fix")
    if saved:
        if saved.get("branch"):
            out.append(f"- ブランチ: `{saved['branch']}`")
        out += [f"- 差分ファイル: [{Path(saved['patch']).name}]({r.rel(saved['patch'])})", "",
                *_diff_block(saved.get("patch"))]
    else:
        out.append("- 差分: なし（最新のリビジョンでは直っているため）" if fix.already_fixed_on_latest
                   else "- 差分: なし")
    out += ["", "修正した場合の Classification / Action / Severity（参考。「修正」で反映しても Coverity には書き込みません）:",
            "", *_attrs(fix), ""]

    # 6. verification and review (options); the processing section takes the next number
    number = 6
    if r.verify:
        out += [f"## {number}. ビルドでの検証", "", *_verify_lines(r.verify), ""]
        number += 1
    if r.review:
        out += [f"## {number}. 独立した検証", "", *_review_lines(r.review), ""]
        number += 1

    out += [f"## {number}. 処理情報", "",
            f"- 調べたコード: {('解析リビジョン ' + _short(r.analyzed_revision)) if r.analyzed_revision else '手元のコード'}",
            f"- 修正の元にしたリビジョン: {_short(r.latest_revision) if r.latest_revision else '-'}"]
    if r.seconds is not None:
        out.append(f"- 処理時間: {r.seconds:.0f} 秒")
    if res.group_excluded_cids:
        out.append(f"- グループから外して個別の作業に戻した CID: {res.group_excluded_cids}")
    if r.merged_from:
        out.append(f"- 統合した作業: {', '.join(r.merged_from)}")
    return "\n".join(out) + "\n"


REVIEW_JA = {"pass": "合格", "flag": "要確認", "rework": "差し戻し", "unverified": "未検証"}


def _review_lines(review: dict[str, Any]) -> list[str]:
    outcome = review.get("outcome", "unverified")
    lines = [f"- 結果: {REVIEW_JA.get(outcome, outcome)}"]
    if review.get("note"):
        lines.append(f"- 見解: {review['note']}")
    if review.get("confidence_cap"):
        lines.append(f"- 確信度の上限: {CONFIDENCE_JA.get(review['confidence_cap'], review['confidence_cap'])}")
    for f in review.get("findings") or []:
        where = f" `{f['file']}`:{f.get('line') or '-'}" if f.get("file") else ""
        lines.append(f"- {'○' if f.get('ok') else '×'} {f.get('check', '')}{where} — {f.get('detail', '')}")
    if review.get("reason"):
        lines.append(f"- 補足: {review['reason']}")
    return lines


def _review_html(review: dict[str, Any]) -> str:
    outcome = review.get("outcome", "unverified")
    rows = [("結果", _badge(REVIEW_JA.get(outcome, outcome), "review-" + outcome))]
    if review.get("note"):
        rows.append(("見解", _e(review["note"])))
    if review.get("confidence_cap"):
        rows.append(("確信度の上限", _e(CONFIDENCE_JA.get(review["confidence_cap"], review["confidence_cap"]))))
    if review.get("reason"):
        rows.append(("補足", _e(review["reason"])))
    out = _kv(rows)
    findings = review.get("findings") or []
    if findings:
        out += '\n<ul class="evidence">' + "".join(
            f'<li><span class="loc">{"○" if f.get("ok") else "×"} {_e(f.get("check", ""))}'
            + (f' {_e(f["file"])}:{_e(f.get("line") or "-")}' if f.get("file") else "")
            + f'</span>{_e(f.get("detail", ""))}</li>' for f in findings) + "</ul>"
    return out


def _short(revision: str) -> str:
    """A git commit as its first 10 characters; svn revisions stay as they are."""
    return revision[:10] if re.fullmatch(r"[0-9a-f]{40}", revision) else revision


def _verify_lines(verify: dict[str, Any]) -> list[str]:
    mode = {"build": "ビルド", "build+analyze": "ビルドと Coverity の再解析"}.get(verify.get("mode"), "-")
    lines = [f"- 方法: {mode}（この実行の修正案をすべて当てて 1 回で確かめています）",
             f"- 結果: {verify_label(verify)}"]
    lines += [f"- 要確認: {p}" for p in verify.get("problems", [])]
    if verify.get("note"):
        lines.append(f"- 補足: {verify['note']}")
    if "resolved_cids" in verify:
        lines += [f"- 消えた警告: {verify['resolved_cids'] or 'なし'}",
                  f"- 残った警告: {verify['remaining_cids'] or 'なし'}",
                  *([f"- 確かめられない警告（修正前の解析にも出ていない）: {verify['unconfirmed_cids']}"]
                    if verify.get("unconfirmed_cids") else []),
                  f"- 変更したファイルで増えた警告: {verify.get('new_issue_count', 0)} 件"]
    if verify.get("log"):
        lines.append(f"- ログ: `{verify['log']}`")
    if verify.get("log_tail"):
        lines += ["", "```", verify["log_tail"].strip(), "```"]
    return lines


def verify_label(verify: dict[str, Any] | None) -> str:
    if not verify:
        return "-"
    if verify.get("applied") is False:
        return "当てられない"
    if verify.get("build_ok") is None:
        return "確かめられない"
    if not verify["build_ok"]:
        return "ビルド失敗"
    if verify.get("remaining_cids"):
        return "警告が残る"
    if verify.get("new_issue_count"):
        return f"警告が {verify['new_issue_count']} 件増える"
    return "問題なし"


def read_deviation(markdown: str) -> tuple[Attributes, str]:
    """The (possibly edited) deviation section of a detailed report."""
    match = re.search(re.escape(DEV_BEGIN) + r"(.*?)" + re.escape(DEV_END), markdown, re.S)
    if not match:
        raise CtError("逸脱の節の目印（<!-- ct:begin deviation --> と <!-- ct:end deviation -->）が"
                      "見つかりません。目印の行は消さないでください")
    body = match.group(1)
    values = {}
    for key in ("Classification", "Action", "Severity"):
        m = re.search(rf"^\s*-\s*{key}\s*:\s*(.+?)\s*$", body, re.M)
        if not m:
            raise CtError(f"逸脱の節に {key} がありません")
        values[key.lower()] = m.group(1)
    m = re.search(r"^\s*-\s*逸脱コメント\s*:\s*$(.*)", body, re.M | re.S)
    if not m or not m.group(1).strip():
        raise CtError("逸脱コメントが空です")
    return Attributes(**values), m.group(1).strip()


# ---- summary -------------------------------------------------------------------------------


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _by_confidence(reports: list[ItemReport]) -> list[ItemReport]:
    return sorted(reports, key=lambda r: (CONFIDENCE_ORDER[r.result.confidence], r.item_id))


def render_summary(title: str, filter_name: str, reports: list[ItemReport], errors: list[tuple[str, str]],
                   unprocessed: list[str], approvals: dict[str, dict[str, Any]]) -> str:
    """Markdown copy of the summary. ``approvals`` are the items of approvals.json (shown, not read back)."""
    done = _by_confidence(reports)
    total = sum(len(r.issues) for r in reports)
    groups = sum(1 for r in reports if r.item_id.startswith("G"))
    out = [f"# トリアージ結果 {title}", "",
           f"条件: {filter_name} ／ 警告 {total} 件（作業 {len(reports)}、うちグループ {groups}）"
           + (f" ／ エラー {len(errors)}" if errors else "") + (f" ／ 未処理 {len(unprocessed)}" if unprocessed else ""),
           "",
           GUI_NOTE + "承認欄はいまの承認を表示しています（はじめは AI の推奨です）。空欄の行は反映しません。"
           "この表を書き換えても反映されません。",
           "承認を確かめたら `/coverity-apply` で反映します。", "",
           SUMMARY_HEADER, "|---|---|---|---|---|---|"]
    for r in done:
        res = r.result
        approval = DECISION_JA.get((approvals.get(r.item_id) or {}).get("decision") or "", "")
        row = [approval, item_title(r.item_id, len(r.issues)), PLAN_JA[res.recommendation],
               CONFIDENCE_JA[res.confidence], res.verdict.summary, f"[詳細](cid/{r.item_id}.md)"]
        out.append("| " + " | ".join(_cell(c) for c in row) + " |")
    if errors:
        out += ["", "## 処理できなかった作業", "", "| ID | 理由 |", "|---|---|"]
        out += [f"| {_cell(i)} | {_cell(m)} |" for i, m in errors]
    if unprocessed:
        out += ["", "## 未処理の作業", "", "`/coverity-run` で続きから処理できます: " + "、".join(unprocessed)]
    return "\n".join(out) + "\n"


def read_approval_rows(markdown: str) -> tuple[dict[str, str | None], list[str]]:
    """Every row of the approval column (``None`` for an empty cell) and the cells that cannot be read."""
    lines = markdown.splitlines()
    start = next((n for n, line in enumerate(lines) if re.match(r"^\|\s*承認\s*\|\s*ID\s*\|", line)), None)
    if start is None:
        raise CtError("一覧の表（| 承認 | ID | ...）が見つかりません。表の見出しの行は消さないでください")
    rows: dict[str, str | None] = {}
    problems: list[str] = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) < 2:
            continue
        m = re.match(r"^(G\d+|\d+)", cells[1])
        if not m:
            problems.append(f"ID の欄を読めません: {cells[1]}")
            continue
        value = cells[0].strip("*` ")
        if value and value not in APPROVALS:
            problems.append(f"{m.group(1)} の承認欄「{value}」は「修正」「逸脱」「却下」のどれかにしてください")
            continue
        rows[m.group(1)] = APPROVALS[value] if value else None
    return rows, problems


def read_approvals(markdown: str) -> dict[str, str]:
    """``{item id: "fix" | "deviation" | "reject"}`` for rows whose approval column is filled in."""
    rows, problems = read_approval_rows(markdown)
    if problems:
        raise CtError("一覧の承認欄に直すところがあります", problems=problems)
    return {k: v for k, v in rows.items() if v}


# ---- HTML ----------------------------------------------------------------------------------
# Both pages are complete documents with inline CSS and no script, so they work when opened from
# the folder (file://) and inside the screen of ct.py gui. Every text that comes from the AI or
# from Coverity goes through _e().

REPORT_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f5f6f8; --surface: #ffffff; --sunken: #eef0f3; --text: #1c2230; --muted: #5d6676;
  --border: #d8dde5; --link: #1f56c9; --shadow: 0 1px 2px rgba(20, 30, 50, .06);
  --blue: #1f56c9; --blue-bg: #e5edfc; --violet: #6b3fb8; --violet-bg: #efe8fb;
  --green: #1d7044; --green-bg: #e2f3e8; --amber: #8a5800; --amber-bg: #fbf0d6;
  --red: #b02a30; --red-bg: #fbe5e5; --gray: #4f5866; --gray-bg: #e9ecf0;
  --add-bg: #e4f5e8; --add-fg: #14532d; --del-bg: #fce6e6; --del-fg: #8b1d1d;
  --hunk-bg: #e6ecfb; --hunk-fg: #2b4a9b; --meta-fg: #6b7383;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "BIZ UDGothic", "MS Gothic", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", Meiryo, sans-serif;
}
@media screen and (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c; --surface: #1c2027; --sunken: #242a33; --text: #e4e7ec; --muted: #9aa3b2;
    --border: #343c48; --link: #8fb3ff; --shadow: none;
    --blue: #8fb3ff; --blue-bg: #1d2b4a; --violet: #c4a6ff; --violet-bg: #2e2246;
    --green: #7fd6a2; --green-bg: #173526; --amber: #f0c36b; --amber-bg: #3a2d10;
    --red: #ff9a9a; --red-bg: #4a1d20; --gray: #b5bdc9; --gray-bg: #2a3039;
    --add-bg: #16351f; --add-fg: #a6e9b9; --del-bg: #43191b; --del-fg: #ffb3b3;
    --hunk-bg: #1d2a48; --hunk-fg: #a9c1ff; --meta-fg: #8f98a7;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.7 var(--sans); }
.page { max-width: 76rem; margin: 0 auto; padding: 20px 16px 40px; }
a { color: var(--link); }
code, pre, .mono { font-family: var(--mono); font-size: .92em; }
h1, h2, h3, h4 { line-height: 1.35; margin: 0; }
h1 { font-size: 1.55rem; }
h2 { font-size: 1.15rem; margin-bottom: 10px; }
h3 { font-size: 1rem; margin: 16px 0 6px; }
h4 { font-size: .92rem; margin: 12px 0 4px; color: var(--muted); }
p { margin: 6px 0; }
.muted, .sub { color: var(--muted); }
.sub { display: block; font-size: .85em; }
.crumbs { margin: 0 0 8px; font-size: .9rem; }
.head { margin-bottom: 18px; }
.head .where { margin: 4px 0 10px; color: var(--muted); overflow-wrap: anywhere; }
.head .checker, .head .when { color: var(--muted); font-weight: 600; margin-left: .4em; }
.badges { display: flex; flex-wrap: wrap; gap: 6px; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: .82rem; font-weight: 600;
  line-height: 1.6; white-space: nowrap; border: 1px solid transparent; background: var(--gray-bg); color: var(--gray); }
.badge.fix { background: var(--blue-bg); color: var(--blue); }
.badge.deviation { background: var(--violet-bg); color: var(--violet); }
.badge.reject { background: var(--gray-bg); color: var(--gray); }
.badge.none { background: transparent; color: var(--muted); border-color: var(--border); }
.badge.conf-high { background: var(--green-bg); color: var(--green); }
.badge.conf-medium { background: var(--amber-bg); color: var(--amber); }
.badge.conf-low { background: var(--red-bg); color: var(--red); }
.badge.review-pass { background: var(--green-bg); color: var(--green); }
.badge.review-flag, .badge.review-rework { background: var(--amber-bg); color: var(--amber); }
.badge.review-unverified { background: var(--gray-bg); color: var(--gray); }
.badge.impact-high { background: var(--red-bg); color: var(--red); }
.badge.impact-medium { background: var(--amber-bg); color: var(--amber); }
.badge.ok { background: var(--green-bg); color: var(--green); }
.badge.ng { background: var(--red-bg); color: var(--red); }
.badge.edited { background: var(--amber-bg); color: var(--amber); }
.card, section.block { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 18px; margin: 0 0 16px; box-shadow: var(--shadow); }
.lead { font-size: 1.08rem; font-weight: 600; margin: 0 0 10px; overflow-wrap: anywhere; }
.conclusion { border-left: 5px solid var(--blue); }
.conclusion.deviation { border-left-color: var(--violet); }
dl.kv { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 4px 16px; margin: 6px 0; }
dl.kv dt { color: var(--muted); font-weight: 600; }
dl.kv dd { margin: 0; overflow-wrap: anywhere; }
.callout { background: var(--amber-bg); color: var(--text); border-radius: 8px; padding: 8px 12px; margin: 10px 0 0; }
.callout ul { margin: 4px 0 0; padding-left: 1.3em; }
.prose { white-space: pre-wrap; overflow-wrap: anywhere; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .93rem; }
th, td { text-align: left; vertical-align: top; padding: 7px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; background: var(--sunken); white-space: nowrap; }
td { overflow-wrap: anywhere; }
ol.path { margin: 4px 0 8px; padding-left: 2.2em; }
ol.path li { padding: 3px 8px; border-radius: 6px; margin: 2px 0; }
ol.path li.main { background: var(--red-bg); font-weight: 600; }
ol.path li.main::marker { color: var(--red); }
.loc { font-family: var(--mono); font-size: .9em; color: var(--muted); margin-right: .4em; overflow-wrap: anywhere; }
.tag { font-family: var(--mono); font-size: .82em; padding: 0 5px; border-radius: 4px; background: var(--sunken);
  margin-right: .4em; }
ul.evidence { margin: 6px 0; padding-left: 1.3em; }
.plans { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 24rem), 1fr)); gap: 16px; }
.plan { background: var(--surface); border: 1px solid var(--border); border-top: 5px solid var(--gray);
  border-radius: 10px; padding: 14px 16px; min-width: 0; box-shadow: var(--shadow); }
.plan.deviation { border-top-color: var(--violet); }
.plan.fix { border-top-color: var(--blue); }
.plan.recommended { outline: 2px solid var(--border); }
.plan h3 { margin: 0 0 6px; font-size: 1.08rem; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.plan .note { font-size: .88rem; color: var(--muted); }
.comment { background: var(--sunken); border-radius: 8px; padding: 10px 12px; margin: 4px 0; }
details { margin: 10px 0 0; }
summary { cursor: pointer; color: var(--muted); font-size: .9rem; }
ul.files { margin: 0; padding-left: 1.2em; }
.diffs h3 { margin-top: 20px; }
pre.diff, pre.log { margin: 6px 0; padding: 10px 0; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; overflow-x: auto; font-size: .84rem; line-height: 1.5; }
pre.log { padding: 10px 12px; background: var(--sunken); }
pre.diff > span { display: block; padding: 0 12px; min-height: 1.5em; }
pre.diff > .add { background: var(--add-bg); color: var(--add-fg); }
pre.diff > .del { background: var(--del-bg); color: var(--del-fg); }
pre.diff > .hunk { background: var(--hunk-bg); color: var(--hunk-fg); }
pre.diff > .meta { color: var(--meta-fg); font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(8.5rem, 1fr)); gap: 10px; margin: 0 0 16px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px;
  box-shadow: var(--shadow); }
.tile .label { color: var(--muted); font-size: .82rem; font-weight: 600; }
.tile .value { font-size: 1.6rem; font-weight: 700; line-height: 1.3; font-variant-numeric: tabular-nums; }
.tile .sub { font-size: .78rem; }
.tile.warn .value { color: var(--amber); }
.tile.bad .value { color: var(--red); }
.notice { border-left: 5px solid var(--blue); }
table.items td.id a { font-weight: 700; }
table.items .edited-mark { display: block; color: var(--amber); font-size: .8rem; font-weight: 600; }
footer.foot { color: var(--muted); font-size: .85rem; margin-top: 24px; }
@media (max-width: 40rem) {
  dl.kv { grid-template-columns: minmax(0, 1fr); gap: 0; }
  dl.kv dd { margin-bottom: 6px; }
  table.items thead { display: none; }
  table.items, table.items tbody, table.items tr, table.items td { display: block; width: 100%; }
  table.items tr { border: 1px solid var(--border); border-radius: 10px; margin: 0 0 10px; padding: 6px 0;
    background: var(--surface); }
  table.items td { border: 0; padding: 3px 12px; display: grid; grid-template-columns: 5.5em minmax(0, 1fr); gap: 8px; }
  table.items td::before { content: attr(data-label); color: var(--muted); font-size: .82rem; font-weight: 600; }
}
@media print {
  :root { --bg: #fff; --surface: #fff; --sunken: #f3f4f6; --text: #000; --muted: #444; --border: #bbb;
    --shadow: none; --link: #000; }
  body { font-size: 11pt; }
  .page { max-width: none; padding: 0; }
  .crumbs { display: none; }
  .card, section.block, .plan, .tile, pre.diff, ol.path li.main { break-inside: avoid; }
  pre.diff, pre.log { white-space: pre-wrap; overflow-wrap: anywhere; overflow: visible; }
  pre.diff > span, .badge, ol.path li.main { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""

IMPACT_CLASS = {"high": "impact-high", "medium": "impact-medium"}


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _href(path: str) -> str:
    """A link from a report: a relative path inside the run folder, or a file URI outside it."""
    return _e(Path(path).as_uri() if Path(path).is_absolute() else quote(path, safe="/"))


def _badge(text: str, kind: str = "") -> str:
    return f'<span class="{"badge " + kind if kind else "badge"}">{_e(text)}</span>'


def _decision_badge(decision: str | None) -> str:
    return _badge(DECISION_JA[decision], decision) if decision in DECISION_JA else _badge("（空）", "none")


def _kv(rows: list[tuple[str, str]]) -> str:
    """A definition list; values are HTML already."""
    return '<dl class="kv">\n' + "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>\n" for k, v in rows) + "</dl>"


def _page(title: str, body: str) -> str:
    return ('<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{_e(title)}</title>\n<style>{REPORT_CSS}</style>\n</head>\n'
            f'<body>\n<div class="page">\n{body}\n</div>\n</body>\n</html>\n')


def diff_html(text: str, limit: int | None = MAX_DIFF_LINES) -> str:
    """A unified diff (git or svn) as ``<pre class="diff">`` with one span per line."""
    lines = text.splitlines()
    shown = lines if limit is None else lines[:limit]
    out, in_hunk = [], False
    for line in shown:
        if line.startswith(("diff ", "Index: ")):
            kind, in_hunk = "meta", False
        elif line.startswith("@@"):
            kind, in_hunk = "hunk", True
        elif not in_hunk or line.startswith("\\"):
            kind = "meta"  # file headers, and "\ No newline at end of file"
        else:
            kind = {"+": "add", "-": "del"}.get(line[:1], "")
        out.append(f'<span class="{kind}">' if kind else "<span>")
        out.append(f"{_e(line)}\n</span>")
    block = '<pre class="diff">' + "".join(out) + "</pre>"
    if limit is not None and len(lines) > limit:
        block += f'\n<p class="muted">差分が長いため先頭 {limit} 行だけ表示しています。全体は差分ファイルを見てください。</p>'
    return block


def _diff_file(path: str | None) -> str:
    if not path or not Path(path).is_file():
        return ""
    return diff_html(display_text(Path(path).read_bytes()))


def _event_list(events: list[Event]) -> str:
    if not events:
        return '<p class="muted">（警告経路を取得できませんでした）</p>'
    items = []
    for e in events:
        start = '<li class="main">' if e.main else "<li>"
        items.append(f'{start}<span class="loc">{_e(e.file)}:{_e(e.line or "-")}</span>'
                     f'<span class="tag">{_e(e.tag)}</span>{_e(e.description)}'
                     + (' <span class="badge conf-low">警告の場所</span>' if e.main else "") + "</li>")
    return '<ol class="path">\n' + "\n".join(items) + "\n</ol>"


def _saved_links(r: ItemReport, kind: str) -> list[tuple[str, str]]:
    """Rows for the branch, the patch file and the changed files of a saved fix or annotation."""
    saved = r.saved.get(kind)
    if not saved:
        return []
    rows = []
    if saved.get("branch"):
        rows.append(("ブランチ", f"<code>{_e(saved['branch'])}</code>"))
    if saved.get("patch"):
        rows.append(("差分ファイル", f'<a href="{_href(r.rel(saved["patch"]))}">{_e(Path(saved["patch"]).name)}</a>'))
    files = []
    for rel in saved.get("files") or []:
        copy = (r.run_dir / "fixed" / r.item_id / kind / rel) if r.run_dir else None
        if copy is not None and copy.is_file():
            files.append(f'<li><a href="{_href(r.rel(str(copy)))}">{_e(rel)}</a></li>')
        else:
            files.append(f"<li>{_e(rel)}（消したファイル）</li>" if kind == "fix" else f"<li>{_e(rel)}</li>")
    if files:
        rows.append(("修正後のファイル" if kind == "fix" else "注釈を入れたファイル",
                     '<ul class="files">' + "".join(files) + "</ul>"))
    return rows


def _attr_rows(attrs: Attributes) -> list[tuple[str, str]]:
    return [("Classification", _e(attrs.classification)), ("Action", _e(attrs.action)),
            ("Severity", _e(attrs.severity))]


def render_item_html(r: ItemReport, decision: dict[str, Any] | None = None) -> str:
    """The detailed report. ``decision`` is the item of approvals.json (None: nothing approved yet)."""
    res, main = r.result, r.issues[0]
    title = item_title(r.item_id, len(r.issues))
    rec = res.recommendation
    body: list[str] = []

    # header
    where = f"{main.file}:{main.line or '-'}" + (f" {main.function}()" if main.function else "")
    if len(r.issues) > 1:
        where += f" ほか {len(r.issues) - 1} 件"
    badges = [_badge(f"推奨 {PLAN_JA[rec]}", rec), _badge(f"確信度 {CONFIDENCE_JA[res.confidence]}", f"conf-{res.confidence}"),
              _badge(f"Impact {main.impact or '-'}", IMPACT_CLASS.get((main.impact or "").lower(), "")),
              _badge(JUDGEMENT_JA[res.verdict.judgement])]
    if decision is not None:
        badges.append(_badge(f"承認 {DECISION_JA.get(decision.get('decision') or '', '（空）')}",
                             decision.get("decision") or "none"))
        if decision.get("edited"):
            badges.append(_badge("人が変更", "edited"))
    body += ['<header class="head">', '<p class="crumbs"><a href="../summary.html">← 一覧（summary）</a></p>',
             f'<h1>{_e(title)}<span class="checker">{_e(main.checker)}</span></h1>',
             f'<p class="where"><code>{_e(where)}</code></p>', '<div class="badges">' + "".join(badges) + "</div>",
             "</header>"]

    # conclusion
    notes = []
    if res.fix.exceeded_constraints:
        notes.append("修正の制約を超えた点あり: " + "、".join(res.fix.exceeded_constraints))
    if res.fix.already_fixed_on_latest:
        notes.append("最新のリビジョンでは直っています" if r.saved.get("fix") else ALREADY_FIXED_NOTE)
    notes += r.notes
    drift = DRIFT_JA[res.revision_drift.status] + (f"（{res.revision_drift.detail}）" if res.revision_drift.detail else "")
    body += [f'<section class="card conclusion {rec}">', "<h2>結論</h2>",
             f'<p class="lead">{_e(res.verdict.summary)}</p>',
             _kv([("推奨", f"<strong>{_e(PLAN_JA[rec])}</strong>（{'案A' if rec == 'deviation' else '案B'}）"),
                  ("確信度", f"<strong>{_e(CONFIDENCE_JA[res.confidence])}</strong> — {_e(res.confidence_reason)}"),
                  ("見立て", _e(JUDGEMENT_JA[res.verdict.judgement])), ("リビジョンのずれ", _e(drift))])]
    if notes:
        body.append('<div class="callout"><strong>注意</strong><ul>' + "".join(f"<li>{_e(n)}</li>" for n in notes)
                    + "</ul></div>")
    body.append("</section>")

    # overview
    rows = "".join(f"<tr><td>{_e(i.cid)}</td><td>{_e(i.checker)}</td><td class=\"mono\">{_e(i.file)}</td>"
                   f"<td>{_e(i.function or '-')}</td><td>{_e(i.line or '-')}</td><td>{_e(i.impact or '-')}</td>"
                   f"<td>{_e(i.cwe or '-')}</td></tr>\n" for i in r.issues)
    body += ['<section class="block">', "<h2>警告の概要</h2>", '<div class="table-wrap"><table>',
             "<thead><tr><th>CID</th><th>チェッカー</th><th>ファイル</th><th>関数</th><th>行</th><th>Impact</th>"
             "<th>CWE</th></tr></thead>", f"<tbody>\n{rows}</tbody></table></div>"]
    if r.checker_description:
        body.append(f'<p class="muted">チェッカーの説明: {_e(r.checker_description)}</p>')
    body.append("</section>")

    # evidence
    body += ['<section class="block">', "<h2>真偽の根拠</h2>"]
    for i in r.issues:
        body += [f"<h3>警告経路（CID {_e(i.cid)}）</h3>", _event_list(r.events.get(i.cid) or [])]
    body += ["<h3>調査結果</h3>", f'<p class="prose">{_e(res.verdict.rationale)}</p>']
    if res.verdict.evidence:
        body.append('<ul class="evidence">' + "".join(
            f'<li><span class="loc">{_e(ev.file)}:{_e(ev.line or "-")}</span>{_e(ev.note)}</li>'
            for ev in res.verdict.evidence) + "</ul>")
    body.append("</section>")

    # the two proposals, side by side on a wide screen
    attrs, comment, edited = shown_deviation(res, decision)
    recommended = ' <span class="badge ' + rec + '">推奨</span>'
    deviation = ['<article class="plan deviation' + (" recommended" if rec == "deviation" else "") + '">',
                 "<h3>案A: 逸脱" + (recommended if rec == "deviation" else "") + "</h3>",
                 '<p class="note">「逸脱」で反映すると、この内容が Coverity に登録されます。</p>',
                 _kv(_attr_rows(attrs)), "<h4>逸脱コメント</h4>", f'<div class="comment prose">{_e(comment)}</div>']
    if edited:
        proposed = res.deviation
        deviation += ['<p class="note"><span class="badge edited">人が直した内容</span> 画面（ct.py gui）で直した内容です。</p>',
                      "<details><summary>AI の案を見る</summary>", _kv(_attr_rows(proposed)),
                      f'<div class="comment prose">{_e(proposed.comment)}</div></details>']
    deviation += [_kv(_saved_links(r, "annotation")) if r.saved.get("annotation") else "", "</article>"]

    fix = res.fix
    fix_rows = [("概要", _e(fix.summary)), ("影響とリスク", _e(fix.impact)), *_saved_links(r, "fix")]
    nothing_to_take = not r.saved.get("fix") and fix.already_fixed_on_latest
    if not r.saved.get("fix"):
        fix_rows.append(("差分", "なし（最新のリビジョンでは直っているため）" if fix.already_fixed_on_latest else "なし"))
    fixed = ['<article class="plan fix' + (" recommended" if rec == "fix" else "") + '">',
             "<h3>案B: 修正" + (recommended if rec == "fix" else "") + "</h3>",
             '<p class="note">' + (ALREADY_FIXED_NOTE if nothing_to_take else
                                   "「修正」で反映すると、修正をブランチ（svn では差分）で取り込みます。") + "</p>",
             _kv(fix_rows),
             "<details><summary>修正した場合の Classification / Action / Severity（参考。"
             "「修正」で反映しても Coverity には書き込みません）</summary>", _kv(_attr_rows(fix)), "</details>",
             "</article>"]
    body += ['<section class="proposals">', "<h2>対応の案</h2>", '<div class="plans">', *deviation, *fixed, "</div>"]
    diffs = []
    if r.saved.get("fix"):
        diffs += ["<h3>修正の差分（案B）</h3>", _diff_file(r.saved["fix"].get("patch"))]
    if r.saved.get("annotation"):
        diffs += ["<h3>注釈（アノテーション）の差分（案A）</h3>", _diff_file(r.saved["annotation"].get("patch"))]
    if diffs:
        body += ['<div class="diffs">', *diffs, "</div>"]
    body.append("</section>")

    # build verification and the independent review (options)
    if r.verify:
        body += ['<section class="block verify">', "<h2>ビルドでの検証</h2>", _verify_html(r.verify), "</section>"]
    if r.review:
        body += ['<section class="block review">', "<h2>独立した検証</h2>", _review_html(r.review), "</section>"]

    info = [("調べたコード", _e(("解析リビジョン " + _short(r.analyzed_revision)) if r.analyzed_revision else "手元のコード")),
            ("修正の元にしたリビジョン", _e(_short(r.latest_revision) if r.latest_revision else "-"))]
    if r.seconds is not None:
        info.append(("処理時間", f"{r.seconds:.0f} 秒"))
    if res.group_excluded_cids:
        info.append(("グループから外して個別の作業に戻した CID", _e("、".join(map(str, res.group_excluded_cids)))))
    if r.merged_from:
        info.append(("統合した作業", _e("、".join(r.merged_from))))
    body += ['<section class="block info">', "<h2>処理情報</h2>", _kv(info), "</section>",
             f'<footer class="foot">このレポートは表示専用です。{_e(GUI_NOTE)}</footer>']
    return _page(f"{title} {main.checker} — Coverity トリアージ", "\n".join(b for b in body if b))


def _verify_html(verify: dict[str, Any]) -> str:
    mode = {"build": "ビルド", "build+analyze": "ビルドと Coverity の再解析"}.get(verify.get("mode"), "-")
    label = verify_label(verify)
    rows = [("方法", f"{_e(mode)}（この実行の修正案をすべて当てて 1 回で確かめています）"),
            ("結果", _badge(label, "ok" if label == "問題なし" else "ng"))]
    if verify.get("problems"):
        rows.append(("要確認", "<ul>" + "".join(f"<li>{_e(p)}</li>" for p in verify["problems"]) + "</ul>"))
    if verify.get("note"):
        rows.append(("補足", _e(verify["note"])))
    if "resolved_cids" in verify:
        rows += [("消えた警告", _e(verify["resolved_cids"] or "なし")), ("残った警告", _e(verify["remaining_cids"] or "なし")),
                 *([("確かめられない警告", _e(verify["unconfirmed_cids"]) + "（修正前の解析にも出ていない）")]
                   if verify.get("unconfirmed_cids") else []),
                 ("変更したファイルで増えた警告", f"{_e(verify.get('new_issue_count', 0))} 件")]
    if verify.get("log"):
        rows.append(("ログ", f"<code>{_e(verify['log'])}</code>"))
    out = _kv(rows)
    if verify.get("log_tail"):
        out += f'\n<details><summary>ログの最後</summary><pre class="log">{_e(verify["log_tail"].strip())}</pre></details>'
    return out


def render_summary_html(title: str, filter_name: str, reports: list[ItemReport], errors: list[tuple[str, str]],
                        unprocessed: list[str], approvals: dict[str, dict[str, Any]]) -> str:
    """The summary. ``approvals`` are the items of approvals.json; the page only shows them."""
    done = _by_confidence(reports)
    total = sum(len(r.issues) for r in reports)
    groups = sum(1 for r in reports if r.item_id.startswith("G"))
    recommended = {k: sum(1 for r in reports if r.result.recommendation == k) for k in PLAN_JA}
    low = sum(1 for r in reports if r.result.confidence == "low")
    decided = [(approvals.get(r.item_id) or {}) for r in reports]
    tally = {k: sum(1 for d in decided if d.get("decision") == k) for k in DECISION_JA}
    empty = sum(1 for d in decided if d.get("decision") not in DECISION_JA)
    changed = sum(1 for d in decided if d.get("edited"))

    def tile(label: str, value: int, sub: str = "", kind: str = "") -> str:
        return (f'<div class="{"tile " + kind if kind else "tile"}"><div class="label">{_e(label)}</div><div class="value">{value}</div>'
                + (f'<div class="sub">{_e(sub)}</div>' if sub else "") + "</div>")

    body = ['<header class="head">', f'<h1>トリアージ結果<span class="when">{_e(title)}</span></h1>',
            f'<p class="where">条件: {_e(filter_name)}</p>', "</header>",
            '<div class="tiles">', tile("警告", total, "終わった作業の CID"),
            tile("作業", len(reports), f"うちグループ {groups}"), tile("修正の推奨", recommended["fix"]),
            tile("逸脱の推奨", recommended["deviation"]), tile("確信度 低", low, "先に確かめます", "warn" if low else ""),
            tile("エラー", len(errors), "処理できなかった作業", "bad" if errors else ""),
            tile("未処理", len(unprocessed), "", "warn" if unprocessed else ""), "</div>",
            '<section class="card notice">',
            f"<p>{_e(GUI_NOTE)}この一覧は表示専用で、承認欄はいまの承認です（はじめは AI の推奨です）。"
            "（空）の行は反映しません。</p>",
            "<p>承認を確かめたら <code>/coverity-apply</code> で反映します。</p>",
            f'<p class="muted">いまの承認: 修正 {tally["fix"]} ／ 逸脱 {tally["deviation"]} ／ 却下 {tally["reject"]}'
            f" ／ （空） {empty} ／ 推奨から変えたもの {changed}</p>", "</section>",
            '<section class="block">', "<h2>作業の一覧（確信度の低い順）</h2>"]
    rows = []
    for r in done:
        res, d = r.result, approvals.get(r.item_id) or {}
        link = _href(f"cid/{r.item_id}.html")
        mark = '<span class="edited-mark">✎ 推奨から変更</span>' if d.get("edited") else ""
        rows.append(f'<tr><td data-label="承認">{_decision_badge(d.get("decision"))}{mark}</td>'
                    f'<td data-label="ID" class="id"><a href="{link}">{_e(item_title(r.item_id, len(r.issues)))}</a>'
                    f'<span class="sub">{_e(r.issues[0].checker)}</span></td>'
                    f'<td data-label="推奨">{_badge(PLAN_JA[res.recommendation], res.recommendation)}</td>'
                    f'<td data-label="確信度">{_badge(CONFIDENCE_JA[res.confidence], "conf-" + res.confidence)}</td>'
                    f'<td data-label="見立て">{_e(res.verdict.summary)}'
                    f'<span class="sub">{_e(JUDGEMENT_JA[res.verdict.judgement])}</span></td>'
                    f'<td data-label="詳細"><a href="{link}">詳細</a></td></tr>')
    if rows:
        body += ['<div class="table-wrap"><table class="items">',
                 "<thead><tr><th>承認</th><th>ID</th><th>推奨</th><th>確信度</th><th>見立て</th><th>詳細</th></tr></thead>",
                 "<tbody>", *rows, "</tbody></table></div>"]
    else:
        body.append('<p class="muted">終わった作業はまだありません。</p>')
    body.append("</section>")
    if errors:
        body += ['<section class="block">', "<h2>処理できなかった作業</h2>", '<div class="table-wrap"><table>',
                 "<thead><tr><th>ID</th><th>理由</th></tr></thead><tbody>",
                 *[f'<tr><td>{_e(i)}</td><td class="prose">{_e(m)}</td></tr>' for i, m in errors],
                 "</tbody></table></div>", "</section>"]
    if unprocessed:
        body += ['<section class="block">', "<h2>未処理の作業</h2>",
                 f"<p><code>/coverity-run</code> で続きから処理できます: {_e('、'.join(unprocessed))}</p>", "</section>"]
    return _page(f"トリアージ結果 {title} — Coverity トリアージ", "\n".join(body))
