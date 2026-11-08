"""The reports: the summary ``summary.html`` (C-25) and the detailed report ``cid/<ID>.html`` (C-26).

Each is one complete page with inline CSS and no script, so it opens from the folder. Every text that
comes from an agent or from Coverity goes through ``_e`` (HTML-escaped, K-87). The reports only show
things: approvals are chosen on a screen and kept in approvals.json. With the option ``markdown``,
``summary.md`` and ``cid/<ID>.md`` are written too.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import approvals, assess, issues
from .common import display_text, read_json, try_json, write_text
from .state import Run, is_group

JUDGEMENT_JA = {"false_positive": "誤検知", "true_bug": "本物のバグ", "intentional": "意図的", "undetermined": "判定できない"}
PLAN_JA = {"fix": "修正", "deviation": "逸脱"}
CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
DRIFT_JA = {"none": "なし", "detected": "あり", "unknown": "不明"}
DECISION_JA = {"fix": "修正", "deviation": "逸脱", "reject": "却下"}
REVIEW_JA = {"pass": "合格", "flag": "要確認", "rework": "差し戻し", "unverified": "未検証"}
MAX_DIFF_LINES = 400
SCREEN_NOTE = ("承認（修正・逸脱・却下・反映しない）の選択と逸脱コメントの手直しは、結果を見る画面で行い、"
               "反映の手順で反映します。")
ALREADY_FIXED_NOTE = ("最新のリビジョンではもう直っているため、取り込む修正（差分）はありません。承認は「反映しない」で構いません。"
                      "次の解析で警告が消えるか確かめてください")
TILES = ("警告", "作業", "修正の推奨", "逸脱の推奨", "確信度 低", "エラー", "未処理")
ITEM_SECTIONS = ("1. 結論", "2. 警告の概要", "3. 真偽の根拠", "4. 案A: 逸脱", "5. 案B: 修正", "6. 処理情報")
SUMMARY_COLUMNS = ("承認", "ID", "推奨", "確信度", "見立て", "詳細")

CSS = """
:root { color-scheme: light dark;
  --bg: #f5f6f8; --surface: #ffffff; --sunken: #eef0f3; --text: #1c2230; --muted: #5d6676; --border: #d8dde5;
  --link: #1f56c9; --blue: #1f56c9; --blue-bg: #e5edfc; --violet: #6b3fb8; --violet-bg: #efe8fb;
  --green: #1d7044; --green-bg: #e2f3e8; --amber: #8a5800; --amber-bg: #fbf0d6; --red: #b02a30; --red-bg: #fbe5e5;
  --gray: #4f5866; --gray-bg: #e9ecf0; --add-bg: #e4f5e8; --add-fg: #14532d; --del-bg: #fce6e6; --del-fg: #8b1d1d;
  --hunk-bg: #e6ecfb; --hunk-fg: #2b4a9b; --meta-fg: #6b7383;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "BIZ UDGothic", "MS Gothic", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", Meiryo, sans-serif; }
@media screen and (prefers-color-scheme: dark) { :root {
  --bg: #14171c; --surface: #1c2027; --sunken: #242a33; --text: #e4e7ec; --muted: #9aa3b2; --border: #343c48;
  --link: #8fb3ff; --blue: #8fb3ff; --blue-bg: #1d2b4a; --violet: #c4a6ff; --violet-bg: #2e2246;
  --green: #7fd6a2; --green-bg: #173526; --amber: #f0c36b; --amber-bg: #3a2d10; --red: #ff9a9a; --red-bg: #4a1d20;
  --gray: #b5bdc9; --gray-bg: #2a3039; --add-bg: #16351f; --add-fg: #a6e9b9; --del-bg: #43191b; --del-fg: #ffb3b3;
  --hunk-bg: #1d2a48; --hunk-fg: #a9c1ff; --meta-fg: #8f98a7; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.7 var(--sans); }
.page { max-width: 76rem; margin: 0 auto; padding: 20px 16px 40px; }
a { color: var(--link); }
code, pre, .mono { font-family: var(--mono); font-size: .92em; }
h1, h2, h3 { line-height: 1.35; margin: 0; }
h1 { font-size: 1.5rem; } h2 { font-size: 1.15rem; margin-bottom: 10px; } h3 { font-size: 1rem; margin: 14px 0 6px; }
p { margin: 6px 0; } .muted, .sub { color: var(--muted); } .sub { display: block; font-size: .85em; }
.crumbs { margin: 0 0 8px; font-size: .9rem; } .head { margin-bottom: 18px; }
.where { color: var(--muted); overflow-wrap: anywhere; margin: 4px 0 10px; }
.badges { display: flex; flex-wrap: wrap; gap: 6px; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: .82rem; font-weight: 600;
  white-space: nowrap; border: 1px solid transparent; background: var(--gray-bg); color: var(--gray); }
.badge.fix { background: var(--blue-bg); color: var(--blue); } .badge.deviation { background: var(--violet-bg); color: var(--violet); }
.badge.none { background: transparent; color: var(--muted); border-color: var(--border); }
.badge.conf-high, .badge.ok, .badge.review-pass { background: var(--green-bg); color: var(--green); }
.badge.conf-medium, .badge.review-flag, .badge.review-rework, .badge.edited { background: var(--amber-bg); color: var(--amber); }
.badge.conf-low, .badge.ng { background: var(--red-bg); color: var(--red); }
section.block { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin: 0 0 16px; }
.lead { font-size: 1.05rem; font-weight: 600; overflow-wrap: anywhere; }
dl.kv { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 4px 16px; margin: 6px 0; }
dl.kv dt { color: var(--muted); font-weight: 600; } dl.kv dd { margin: 0; overflow-wrap: anywhere; }
.callout { background: var(--amber-bg); border-radius: 8px; padding: 8px 12px; margin: 10px 0 0; }
.prose { white-space: pre-wrap; overflow-wrap: anywhere; } .comment { background: var(--sunken); border-radius: 8px; padding: 10px 12px; }
.table-wrap { overflow-x: auto; } table { border-collapse: collapse; width: 100%; font-size: .93rem; }
th, td { text-align: left; vertical-align: top; padding: 7px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); background: var(--sunken); white-space: nowrap; } td { overflow-wrap: anywhere; }
ol.path li { padding: 2px 6px; border-radius: 6px; } ol.path li.main { background: var(--red-bg); font-weight: 600; }
.loc { font-family: var(--mono); font-size: .9em; color: var(--muted); margin-right: .4em; }
.tag { font-family: var(--mono); font-size: .82em; padding: 0 5px; border-radius: 4px; background: var(--sunken); margin-right: .4em; }
pre.diff, pre.log { margin: 6px 0; padding: 10px 0; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; overflow-x: auto; font-size: .84rem; line-height: 1.5; }
pre.log { padding: 10px 12px; background: var(--sunken); }
pre.diff > span { display: block; padding: 0 12px; min-height: 1.5em; }
pre.diff > .add { background: var(--add-bg); color: var(--add-fg); } pre.diff > .del { background: var(--del-bg); color: var(--del-fg); }
pre.diff > .hunk { background: var(--hunk-bg); color: var(--hunk-fg); } pre.diff > .meta { color: var(--meta-fg); font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(8.5rem, 1fr)); gap: 10px; margin: 0 0 16px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; }
.tile .label { color: var(--muted); font-size: .82rem; font-weight: 600; }
.tile .value { font-size: 1.6rem; font-weight: 700; line-height: 1.3; font-variant-numeric: tabular-nums; }
.tile.warn .value { color: var(--amber); } .tile.bad .value { color: var(--red); }
.edited-mark { display: block; color: var(--amber); font-size: .8rem; font-weight: 600; }
footer.foot { color: var(--muted); font-size: .85rem; margin-top: 24px; }
@media (max-width: 40rem) { dl.kv { grid-template-columns: minmax(0, 1fr); }
  table.items thead { display: none; } table.items, table.items tbody, table.items tr, table.items td { display: block; width: 100%; }
  table.items tr { border: 1px solid var(--border); border-radius: 10px; margin: 0 0 10px; background: var(--surface); }
  table.items td { border: 0; padding: 3px 12px; display: grid; grid-template-columns: 5.5em minmax(0, 1fr); gap: 8px; }
  table.items td::before { content: attr(data-label); color: var(--muted); font-size: .82rem; font-weight: 600; } }
@media print { pre.diff, pre.log { white-space: pre-wrap; } }
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _href(path: str) -> str:
    return _e(quote(path, safe="/"))


def _badge(text: str, kind: str = "") -> str:
    return f'<span class="{"badge " + kind if kind else "badge"}">{_e(text)}</span>'


def _kv(rows: list[tuple[str, str]]) -> str:
    return '<dl class="kv">\n' + "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>\n" for k, v in rows) + "</dl>"


def _page(title: str, body: str) -> str:
    return ('<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{_e(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<div class=\"page\">\n{body}\n"
            "</div>\n</body>\n</html>\n")


def item_title(item_id: str, count: int) -> str:
    return f"{item_id}（{count} 件）" if is_group(item_id) else item_id


def _short(revision: str | None) -> str:
    if not revision:
        return "-"
    return revision[:10] if re.fullmatch(r"[0-9a-f]{40}", revision) else revision


def diff_html(text: str, limit: int = MAX_DIFF_LINES) -> str:
    lines = text.splitlines()
    out, in_hunk = [], False
    for line in lines[:limit]:
        if line.startswith(("diff ", "Index: ")):
            kind, in_hunk = "meta", False
        elif line.startswith("@@"):
            kind, in_hunk = "hunk", True
        elif not in_hunk or line.startswith("\\"):
            kind = "meta"
        else:
            kind = {"+": "add", "-": "del"}.get(line[:1], "")
        out.append((f'<span class="{kind}">' if kind else "<span>") + f"{_e(line)}\n</span>")
    block = '<pre class="diff">' + "".join(out) + "</pre>"
    if len(lines) > limit:
        block += f'\n<p class="muted">差分が長いため、先頭の {limit} 行だけを出しています。全体は差分ファイルにあります。</p>'
    return block


# ---- the data of one item -------------------------------------------------------------------


def item_data(run: Run, item_id: str, decided: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Everything a report shows for one finished item (the confidence is the shown one, C-19)."""
    item = run.item(item_id)
    found, events, description, notes = issues.view(run, item)
    raw = read_json(run.result_file(item_id), {})
    review = assess.shown_review(item, try_json(run.review_file(item_id)) if item.get("review") else None)
    verify = (try_json(run.dir / "verify.json") or {}).get(item_id) if (run.dir / "verify.json").is_file() else None
    result = assess.effective(raw, assess.review_for_confidence(review), verify)
    decided = approvals.load(run) if decided is None else decided
    return {"id": item_id, "item": item, "issues": found, "events": events, "description": description,
            "notes": notes + list(item.get("warnings") or []), "result": result, "raw": raw,
            "saved": read_json(run.item_dir(item_id) / "saved.json", {}), "verify": verify, "review": review,
            "decision": decided.get(item_id)}


def shown_result(run: Run, item: dict[str, Any], verify: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """result.json with the confidence shown to the person (without reading the warning paths)."""
    raw = try_json(run.result_file(item["id"]))
    if not isinstance(raw, dict):
        return None
    review = assess.shown_review(item, try_json(run.review_file(item["id"])) if item.get("review") else None)
    return assess.effective(raw, assess.review_for_confidence(review), (verify or {}).get(item["id"]))


def _deviation_shown(result: dict[str, Any], decision: dict[str, Any] | None) -> tuple[dict[str, str], bool]:
    proposed = result.get("deviation") or {}
    edit = (decision or {}).get("deviation")
    if not isinstance(edit, dict):
        return proposed, False
    shown = {k: str(edit.get(k) or "").strip() or str(proposed.get(k) or "")
             for k in ("classification", "action", "severity", "comment")}
    return shown, True


def _verify_label(verify: dict[str, Any] | None) -> str:
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


def _event_list(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<p class="muted">（警告経路を取れませんでした）</p>'
    rows = []
    for e in events:
        star = "★ " if e["main"] else ""
        rows.append(('<li class="main">' if e["main"] else "<li>") + f'{_e(star)}<span class="loc">{_e(e["file"])}:'
                    f'{_e(e["line"] or "-")}</span><span class="tag">{_e(e["tag"])}</span>{_e(e["description"])}</li>')
    return '<ol class="path">\n' + "\n".join(rows) + "\n</ol>"


def _rel(run: Run, path: str) -> str:
    """A link from cid/<ID>.html to a file of the run folder (relative, so the folder can be moved)."""
    try:
        target = Path(path).resolve()
        target.relative_to(run.dir.resolve())
        return Path(os.path.relpath(target, (run.dir / "cid").resolve())).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()


def render_item_html(run: Run, d: dict[str, Any]) -> str:
    res, main, item_id = d["result"], d["issues"][0], d["id"]
    rec = res.get("recommendation", "fix")
    verdict = res.get("verdict") or {}
    fix = res.get("fix") or {}
    drift = res.get("revision_drift") or {}
    title = f"{item_title(item_id, len(d['issues']))} — {main.get('checker')}（{main.get('impact') or '-'}）"
    decision = d["decision"]
    badges = [_badge(f"推奨 {PLAN_JA.get(rec, rec)}", rec),
              _badge(f"確信度 {CONFIDENCE_JA.get(res.get('confidence'), '-')}", f"conf-{res.get('confidence')}"),
              _badge(JUDGEMENT_JA.get(verdict.get("judgement"), "-"))]
    if decision is not None:
        badges.append(_badge(f"承認 {DECISION_JA.get(decision.get('decision') or '', '（空）')}", decision.get("decision") or "none"))
        if decision.get("edited"):
            badges.append(_badge("人が変更", "edited"))
    where = f"{main.get('file')}:{main.get('line') or '-'}" + (f" {main['function']}()" if main.get("function") else "")
    body = ['<header class="head">', f'<p class="crumbs"><a href="{_href(_rel(run, str(run.summary_file())))}">← 一覧</a></p>',
            f"<h1>{_e(title)}</h1>", f'<p class="where"><code>{_e(where)}</code></p>',
            '<div class="badges">' + "".join(badges) + "</div>", "</header>"]
    notes = []
    if fix.get("exceeded_constraints"):
        notes.append("修正の決まりを超えた点あり: " + "、".join(map(str, fix["exceeded_constraints"])))
    if fix.get("already_fixed_on_latest"):
        notes.append("最新のリビジョンでは直っています" if d["saved"].get("fix") else ALREADY_FIXED_NOTE)
    notes += d["notes"]
    drift_text = DRIFT_JA.get(drift.get("status"), "-") + (f"（{drift['detail']}）" if drift.get("detail") else "")
    body += ['<section class="block conclusion">', f"<h2>{ITEM_SECTIONS[0]}</h2>",
             f'<p class="lead">{_e(verdict.get("summary", ""))}</p>',
             _kv([("推奨", f"<strong>{_e(PLAN_JA.get(rec, rec))}</strong>"),
                  ("確信度", f"<strong>{_e(CONFIDENCE_JA.get(res.get('confidence'), '-'))}</strong> — "
                             f"{_e(res.get('confidence_reason', ''))}"),
                  ("見立て", _e(JUDGEMENT_JA.get(verdict.get("judgement"), "-"))),
                  ("注意（リビジョンのずれ）", _e(drift_text))])]
    if notes:
        body.append('<div class="callout"><strong>注意</strong><ul>' + "".join(f"<li>{_e(n)}</li>" for n in notes) + "</ul></div>")
    body.append("</section>")

    rows = "".join(f"<tr><td>{_e(i['cid'])}</td><td>{_e(i.get('checker'))}</td><td class=\"mono\">{_e(i.get('file'))}</td>"
                   f"<td>{_e(i.get('function') or '-')}</td><td>{_e(i.get('line') or '-')}</td>"
                   f"<td>{_e(i.get('impact') or '-')}</td><td>{_e(i.get('cwe') or '-')}</td></tr>\n" for i in d["issues"])
    body += ['<section class="block overview">', f"<h2>{ITEM_SECTIONS[1]}</h2>", '<div class="table-wrap"><table>',
             "<thead><tr><th>CID</th><th>チェッカー</th><th>ファイル</th><th>関数</th><th>行</th><th>Impact</th><th>CWE</th>"
             f"</tr></thead><tbody>\n{rows}</tbody></table></div>"]
    if d["description"]:
        body.append(f'<p class="muted">チェッカーの説明: {_e(d["description"])}</p>')
    body.append("</section>")

    body += ['<section class="block evidence">', f"<h2>{ITEM_SECTIONS[2]}</h2>"]
    for i in d["issues"]:
        body += [f"<h3>警告経路（CID {_e(i['cid'])}）</h3>", _event_list(d["events"].get(i["cid"]) or [])]
    body += ["<h3>調査結果</h3>", f'<p class="prose">{_e(verdict.get("rationale", ""))}</p>', "<h3>根拠の箇所</h3>"]
    evidence = verdict.get("evidence") or []
    body.append("<ul>" + "".join(f'<li><span class="loc">{_e(ev.get("file"))}:{_e(ev.get("line") or "-")}</span>'
                                 f'{_e(ev.get("note", ""))}</li>' for ev in evidence) + "</ul>" if evidence
                else '<p class="muted">（根拠の場所がありません）</p>')
    body.append("</section>")

    shown, edited = _deviation_shown(res, decision)
    body += ['<section class="block deviation">', f"<h2>{ITEM_SECTIONS[3]}" + (" " + _badge("推奨", "deviation") if rec == "deviation" else "")
             + "</h2>", '<p class="muted">「逸脱」で反映すると、この内容が Coverity に登録されます。</p>',
             _kv([("Classification", _e(shown.get("classification", ""))), ("Action", _e(shown.get("action", ""))),
                  ("Severity", _e(shown.get("severity", "Unspecified")))]),
             "<h3>逸脱コメント</h3>", f'<div class="comment prose">{_e(shown.get("comment", ""))}</div>']
    if edited:
        proposed = res.get("deviation") or {}
        body += ['<p class="muted">' + _badge("人が直した内容", "edited") + " 画面で直した内容です。</p>",
                 f'<details><summary>AI の案</summary><div class="comment prose">{_e(proposed.get("comment", ""))}</div></details>']
    annotation = d["saved"].get("annotation")
    if annotation:
        body += ["<h3>注釈（アノテーション）の差分</h3>", _diff_file(annotation.get("patch"))]
    body.append("</section>")

    saved_fix = d["saved"].get("fix")
    fix_rows = [("概要", _e(fix.get("summary", ""))), ("影響とリスク", _e(fix.get("impact", "")))]
    if saved_fix:
        fix_rows.append(("ブランチ", f"<code>{_e(saved_fix['branch'])}</code>" if saved_fix.get("branch") else "（なし）"))
    body += ['<section class="block fix">', f"<h2>{ITEM_SECTIONS[4]}" + (" " + _badge("推奨", "fix") if rec == "fix" else "")
             + "</h2>", _kv(fix_rows)]
    if saved_fix:
        body += ["<h3>差分</h3>", _diff_file(saved_fix.get("patch")),
                 _kv([("差分ファイル", f'<a href="{_href(_rel(run, saved_fix["patch"]))}">{_e(Path(saved_fix["patch"]).name)}</a>')])]
        files = []
        for rel in saved_fix.get("files") or []:
            copy = run.dir / "fixed" / item_id / "fix" / rel
            files.append(f'<li><a href="{_href(_rel(run, str(copy)))}">{_e(rel)}</a></li>' if copy.is_file()
                         else f"<li>{_e(rel)}（消したファイル）</li>")
        body.append("<h3>修正後のファイル</h3><ul>" + "".join(files) + "</ul>")
    else:
        body.append(f'<p class="muted">{_e(ALREADY_FIXED_NOTE if fix.get("already_fixed_on_latest") else "差分はありません")}</p>')
    body.append(f'<p class="muted">修正した場合の Classification / Action / Severity（参考。「修正」で反映しても Coverity には書き込みません）: '
                f'{_e(fix.get("classification", ""))} / {_e(fix.get("action", ""))} / {_e(fix.get("severity", "Unspecified"))}</p>')
    body.append("</section>")

    if d["verify"]:
        v = d["verify"]
        rows = [("結果", _badge(_verify_label(v), "ok" if _verify_label(v) == "問題なし" else "ng"))]
        rows += [("要確認", _e(p)) for p in v.get("problems") or []]
        if v.get("note"):
            rows.append(("補足", _e(v["note"])))
        body += ['<section class="block verify">', "<h2>ビルドでの検証</h2>", _kv(rows)]
        if v.get("log_tail"):
            body.append(f'<details><summary>ログの最後</summary><pre class="log">{_e(v["log_tail"].strip())}</pre></details>')
        body.append("</section>")
    if d["review"]:
        rv = d["review"]
        outcome = rv.get("outcome", "unverified")
        rows = [("結果", _badge(REVIEW_JA.get(outcome, outcome), f"review-{outcome}"))]
        for key, label in (("note", "見解"), ("reason", "補足")):
            if rv.get(key):
                rows.append((label, _e(rv[key])))
        if rv.get("confidence_cap"):
            rows.append(("確信度の上限", _e(CONFIDENCE_JA.get(rv["confidence_cap"], rv["confidence_cap"]))))
        body += ['<section class="block review">', "<h2>独立した検証</h2>", _kv(rows)]
        findings = rv.get("findings") or []
        if findings:
            body.append("<ul>" + "".join(f"<li>{'○' if f.get('ok') else '×'} {_e(f.get('check', ''))} — {_e(f.get('detail', ''))}</li>"
                                         for f in findings) + "</ul>")
        body.append("</section>")

    item = d["item"]
    info = [("調べたコード", _e(("解析したリビジョン " + _short(run.state.get("analyzed_revision")))
                            if run.state.get("analyzed_revision") else "手元のコード")),
            ("修正の元にしたリビジョン", _e(_short(run.state.get("latest_revision"))))]
    if item.get("seconds") is not None:
        info.append(("処理時間", f"{float(item['seconds']):.0f} 秒"))
    if (d["raw"].get("group_excluded_cids")):
        info.append(("グループから外して別の作業にした CID", _e("、".join(map(str, d["raw"]["group_excluded_cids"])))))
    if item.get("merged_from"):
        info.append(("まとめた作業", _e("、".join(item["merged_from"]))))
    body += ['<section class="block info">', f"<h2>{ITEM_SECTIONS[5]}</h2>", _kv(info), "</section>",
             f'<footer class="foot">このレポートは表示専用です。{_e(SCREEN_NOTE)}</footer>']
    return _page(f"{title} — Coverity トリアージ", "\n".join(body))


def _diff_file(path: str | None) -> str:
    if not path or not Path(path).is_file():
        return '<p class="muted">（差分ファイルがありません）</p>'
    return diff_html(display_text(Path(path).read_bytes()))


def render_item_md(run: Run, d: dict[str, Any]) -> str:
    res, main, item_id = d["result"], d["issues"][0], d["id"]
    verdict, fix = res.get("verdict") or {}, res.get("fix") or {}
    shown, edited = _deviation_shown(res, d["decision"])
    out = [f"# {item_title(item_id, len(d['issues']))} — {main.get('checker')}（{main.get('impact') or '-'}）", "",
           f"## {ITEM_SECTIONS[0]}", "",
           f"- 推奨: **{PLAN_JA.get(res.get('recommendation'), '-')}** ／ 確信度: **{CONFIDENCE_JA.get(res.get('confidence'), '-')}**"
           f"（{res.get('confidence_reason', '')}）",
           f"- 見立て: {JUDGEMENT_JA.get(verdict.get('judgement'), '-')}（{verdict.get('summary', '')}）", "",
           f"## {ITEM_SECTIONS[1]}", "", "| CID | チェッカー | ファイル | 関数 | 行 | Impact | CWE |", "|---|---|---|---|---|---|---|"]
    out += [f"| {i['cid']} | {i.get('checker')} | {i.get('file')} | {i.get('function') or '-'} | {i.get('line') or '-'} | "
            f"{i.get('impact') or '-'} | {i.get('cwe') or '-'} |" for i in d["issues"]]
    out += ["", f"## {ITEM_SECTIONS[2]}", ""]
    for i in d["issues"]:
        out += [f"警告経路（CID {i['cid']}）:", ""]
        out += [f"{n}. {'★ ' if e['main'] else ''}{e['file']}:{e['line'] or '-'} `{e['tag']}` {e['description']}"
                for n, e in enumerate(d["events"].get(i["cid"]) or [], 1)] or ["（警告経路を取れませんでした）"]
        out.append("")
    out += [verdict.get("rationale", ""), "", *[f"- {ev.get('file')}:{ev.get('line') or '-'} {ev.get('note', '')}"
                                                for ev in verdict.get("evidence") or []], "",
            f"## {ITEM_SECTIONS[3]}", "", *(["（人が画面で直した内容です）", ""] if edited else []),
            f"- Classification: {shown.get('classification')}", f"- Action: {shown.get('action')}",
            f"- Severity: {shown.get('severity', 'Unspecified')}", "- 逸脱コメント:", "", str(shown.get("comment", "")), "",
            f"## {ITEM_SECTIONS[4]}", "", f"- 概要: {fix.get('summary', '')}", f"- 影響とリスク: {fix.get('impact', '')}"]
    saved_fix = d["saved"].get("fix")
    if saved_fix:
        if saved_fix.get("branch"):
            out.append(f"- ブランチ: `{saved_fix['branch']}`")
        out.append(f"- 差分ファイル: [{Path(saved_fix['patch']).name}]({_rel(run, saved_fix['patch'])})")
    out += ["", f"## {ITEM_SECTIONS[5]}", "",
            f"- 調べたコード: {('解析したリビジョン ' + _short(run.state.get('analyzed_revision'))) if run.state.get('analyzed_revision') else '手元のコード'}",
            f"- 修正の元にしたリビジョン: {_short(run.state.get('latest_revision'))}", ""]
    return "\n".join(out)


def write_item(run: Run, item_id: str, decided: dict[str, dict[str, Any]] | None = None) -> None:
    """Write cid/<ID>.html (and .md with the option markdown) for a finished item."""
    run.reload()
    item = run.find(item_id)
    if item is None or item["status"] != "done" or not run.result_file(item_id).is_file():
        return
    d = item_data(run, item_id, decided)
    write_text(run.report_file(item_id), render_item_html(run, d))
    if run.settings["options"]["markdown"]:
        write_text(run.report_file(item_id, "md"), render_item_md(run, d))


# ---- the summary ------------------------------------------------------------------------------


def summary_rows(run: Run, decided: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The finished items as the summary lists them: lowest shown confidence first."""
    rows = []
    for item in run.state["items"]:
        if item["status"] != "done" or not run.result_file(item["id"]).is_file():
            continue
        d = item_data(run, item["id"], decided)
        res, main = d["result"], d["issues"][0]
        decision = d["decision"] or {}
        rows.append({"id": item["id"], "title": item_title(item["id"], len(item["cids"])), "cids": item["cids"],
                     "checker": main.get("checker"), "approval": decision.get("decision"),
                     "edited": bool(decision.get("edited")), "recommendation": res.get("recommendation"),
                     "confidence": res.get("confidence"), "judgement": (res.get("verdict") or {}).get("judgement"),
                     "summary": (res.get("verdict") or {}).get("summary", ""), "report": f"cid/{item['id']}.html",
                     "review": (d["review"] or {}).get("outcome"), "already_fixed": bool((res.get("fix") or {}).get("already_fixed_on_latest"))})
    return sorted(rows, key=lambda r: (CONFIDENCE_ORDER.get(r["confidence"], 0), r["id"]))


def unprocessed(run: Run) -> list[dict[str, str]]:
    out = []
    for item in run.state["items"]:
        if item["status"] == "error":
            out.append({"id": item["id"], "reason": item.get("error") or "処理できませんでした"})
        elif item["status"] in ("pending", "in_progress"):
            out.append({"id": item["id"], "reason": "まだ処理していません（再開すると続きから処理します）"})
    return out


def summary_counts(rows: list[dict[str, Any]], left: list[dict[str, str]]) -> dict[str, int]:
    return {"warnings": sum(len(r["cids"]) for r in rows), "items": len(rows),
            "groups": sum(1 for r in rows if is_group(r["id"])),
            "fix": sum(1 for r in rows if r["recommendation"] == "fix"),
            "deviation": sum(1 for r in rows if r["recommendation"] == "deviation"),
            "low": sum(1 for r in rows if r["confidence"] == "low"),
            "errors": sum(1 for u in left if not u["reason"].startswith("まだ")),
            "unprocessed": sum(1 for u in left if u["reason"].startswith("まだ"))}


def render_summary_html(run: Run, rows: list[dict[str, Any]], left: list[dict[str, str]],
                        decided: dict[str, dict[str, Any]]) -> str:
    when = str(run.state.get("created_at", "")).replace("T", " ")[:16]
    c = summary_counts(rows, left)
    tally = approvals.tally([decided.get(r["id"]) or {} for r in rows])

    def tile(label: str, value: int, sub: str = "", kind: str = "") -> str:
        return (f'<div class="{"tile " + kind if kind else "tile"}"><div class="label">{_e(label)}</div>'
                f'<div class="value">{value}</div>' + (f'<div class="sub">{_e(sub)}</div>' if sub else "") + "</div>")

    values = [c["warnings"], c["items"], c["fix"], c["deviation"], c["low"], c["errors"], c["unprocessed"]]
    subs = ["終わった作業の CID", f"うちグループ {c['groups']}", "", "", "先に確かめます", "処理できなかった作業", ""]
    kinds = ["", "", "", "", "warn" if c["low"] else "", "bad" if c["errors"] else "", "warn" if c["unprocessed"] else ""]
    body = ['<header class="head">', f"<h1>トリアージ結果 {_e(when)}</h1>",
            f'<p class="where">条件: {_e(run.state.get("filter_name", "-"))} ／ 警告 {c["warnings"]} 件'
            f'（作業 {c["items"]}、うちグループ {c["groups"]}）</p>', "</header>",
            '<div class="tiles">', *[tile(TILES[n], values[n], subs[n], kinds[n]) for n in range(len(TILES))], "</div>",
            '<section class="block notice">',
            f"<p>{_e(SCREEN_NOTE)}この一覧は表示専用で、承認の欄は一覧を作ったときの値です（はじめは AI の推奨）。"
            "（空）の行は反映しません。</p>",
            f'<p class="tally">いまの承認の内訳: 修正 {tally["fix"]} ／ 逸脱 {tally["deviation"]} ／ 却下 {tally["reject"]}'
            f' ／ （空） {tally["none"]} ／ 推奨から変えたもの {tally["changed"]}</p>', "</section>",
            '<section class="block">', "<h2>作業の一覧（確信度の低い順）</h2>"]
    if rows:
        lines = []
        for r in rows:
            link = _href(r["report"])
            approval = (_badge(DECISION_JA[r["approval"]], r["approval"]) if r["approval"] in DECISION_JA
                        else _badge("（空）", "none")) + ('<span class="edited-mark">推奨から変更</span>' if r["edited"] else "")
            lines.append(f'<tr><td data-label="承認">{approval}</td>'
                         f'<td data-label="ID" class="id"><a href="{link}">{_e(r["title"])}</a><span class="sub">{_e(r["checker"])}</span></td>'
                         f'<td data-label="推奨">{_badge(PLAN_JA.get(r["recommendation"], "-"), r["recommendation"] or "")}</td>'
                         f'<td data-label="確信度">{_badge(CONFIDENCE_JA.get(r["confidence"], "-"), "conf-" + str(r["confidence"]))}</td>'
                         f'<td data-label="見立て">{_e(r["summary"])}<span class="sub">{_e(JUDGEMENT_JA.get(r["judgement"], "-"))}</span></td>'
                         f'<td data-label="詳細"><a href="{link}">詳細</a></td></tr>')
        body += ['<div class="table-wrap"><table class="items">',
                 "<thead><tr>" + "".join(f"<th>{h}</th>" for h in SUMMARY_COLUMNS) + "</tr></thead>",
                 "<tbody>", *lines, "</tbody></table></div>"]
    else:
        body.append('<p class="muted">終わった作業はありません。' + ("条件に合う警告はありませんでした。" if not run.state["items"] else "") + "</p>")
    body.append("</section>")
    errors = [u for u in left if not u["reason"].startswith("まだ")]
    waiting = [u for u in left if u["reason"].startswith("まだ")]
    if errors:
        body += ['<section class="block">', "<h2>処理できなかった作業</h2>", '<div class="table-wrap"><table>',
                 "<thead><tr><th>ID</th><th>理由</th></tr></thead><tbody>",
                 *[f'<tr><td>{_e(u["id"])}</td><td class="prose">{_e(u["reason"])}</td></tr>' for u in errors],
                 "</tbody></table></div>", "</section>"]
    if waiting:
        body += ['<section class="block">', "<h2>未処理の作業</h2>",
                 f"<p>実行を再開すると、続きから処理します: {_e('、'.join(u['id'] for u in waiting))}</p>", "</section>"]
    return _page(f"トリアージ結果 {when} — Coverity トリアージ", "\n".join(body))


def render_summary_md(run: Run, rows: list[dict[str, Any]], left: list[dict[str, str]],
                      decided: dict[str, dict[str, Any]]) -> str:
    when = str(run.state.get("created_at", "")).replace("T", " ")[:16]
    c = summary_counts(rows, left)
    tally = approvals.tally([decided.get(r["id"]) or {} for r in rows])

    def cell(text: Any) -> str:
        return str(text).replace("|", "\\|").replace("\n", " ")

    out = [f"# トリアージ結果 {when}", "",
           f"条件: {run.state.get('filter_name', '-')} ／ 警告 {c['warnings']} 件（作業 {c['items']}、うちグループ {c['groups']}）", "",
           " ／ ".join(f"{TILES[n]} {v}" for n, v in enumerate([c["warnings"], c["items"], c["fix"], c["deviation"], c["low"],
                                                               c["errors"], c["unprocessed"]])), "",
           f"いまの承認の内訳: 修正 {tally['fix']} ／ 逸脱 {tally['deviation']} ／ 却下 {tally['reject']} ／ （空） {tally['none']}"
           f" ／ 推奨から変えたもの {tally['changed']}", "", SCREEN_NOTE, "",
           "| " + " | ".join(SUMMARY_COLUMNS) + " |", "|---|---|---|---|---|---|"]
    for r in rows:
        out.append("| " + " | ".join(cell(x) for x in (DECISION_JA.get(r["approval"] or "", "（空）"), f"{r['title']}（{r['checker']}）",
                                                       PLAN_JA.get(r["recommendation"], "-"), CONFIDENCE_JA.get(r["confidence"], "-"),
                                                       f"{r['summary']}（{JUDGEMENT_JA.get(r['judgement'], '-')}）",
                                                       f"[詳細](cid/{r['id']}.md)")) + " |")
    if left:
        out += ["", "## 処理できなかった作業・未処理の作業", "", "| ID | 理由 |", "|---|---|",
                *[f"| {cell(u['id'])} | {cell(u['reason'])} |" for u in left]]
    return "\n".join(out) + "\n"


def write_summary(run: Run) -> dict[str, Any]:
    """Write every detailed report and the summary from the current approvals."""
    run.reload()
    decided = approvals.load(run)
    for item in run.state["items"]:
        if item["status"] == "done":
            write_item(run, item["id"], decided)
    rows = summary_rows(run, decided)
    left = unprocessed(run)
    write_text(run.summary_file(), render_summary_html(run, rows, left, decided))
    if run.settings["options"]["markdown"]:
        write_text(run.summary_file("md"), render_summary_md(run, rows, left, decided))
    return {"rows": rows, "unprocessed": left, "counts": summary_counts(rows, left),
            "approvals_counts": approvals.tally([decided.get(r["id"]) or {} for r in rows])}
