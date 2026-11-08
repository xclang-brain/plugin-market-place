"""The criteria of the run scope (C-16..C-19, C-24..C-27), checked by fixed rules when the summary is made.

Each check is ``{id, ok, detail}``; ``ok`` is null when the criterion does not apply to this run
(for example the review criteria when the review is off).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import assess, briefs, report, rules, schema
from .common import RunError, file_sha, read_json, try_json
from .state import RUN_ID, Run
from .vcs import make_vcs

NA = "対象外"


def _check(cid: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "detail": detail}


def _reviewed(run: Run) -> list[dict[str, Any]]:
    return [i for i in run.state["items"] if i["status"] == "done"]


def c16(run: Run) -> dict[str, Any]:
    if not run.state.get("review"):
        return _check("C-16", None, f"{NA}（結果の確かめを使わない設定）")
    done = _reviewed(run)
    unverified = [i["id"] for i in done if i.get("review") in (None, "unverified")]
    if unverified:
        return _check("C-16", False, "検証役が独立に確かめられなかった作業: " + "、".join(unverified))
    return _check("C-16", True, f"終わった作業 {len(done)} 件すべてを、作業役とは別の検証役が確かめた（確かめの間に結果と差分は変わらなかった）")


def c17(run: Run) -> dict[str, Any]:
    if not run.state.get("review"):
        return _check("C-17", None, f"{NA}（結果の確かめを使わない設定）")
    missing = []
    for item in _reviewed(run):
        if item.get("review") in (None, "unverified"):
            continue
        brief = run.dir / "agents" / "reviewer" / f"{item['id']}.md"
        text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
        result = try_json(run.result_file(item["id"])) or {}
        points = briefs.lint_for(run, result)
        if "## スクリプトが決まった方法で見つけた点" not in text or any(p["message"] not in text for p in points):
            missing.append(item["id"])
    if missing:
        return _check("C-17", False, "検証役の指示に、スクリプトが見つけた点が載っていない作業: " + "、".join(missing))
    return _check("C-17", True, "確かめたすべての作業で、スクリプトが見つけた点を検証役の指示に載せた")


def c18(run: Run) -> dict[str, Any]:
    if not run.state.get("review"):
        return _check("C-18", None, f"{NA}（結果の確かめを使わない設定）")
    bad = []
    for item in _reviewed(run):
        if item.get("review") in (None, "unverified"):
            continue
        review = try_json(run.review_file(item["id"]))
        checker, merged = briefs.review_context(run, item)
        if not isinstance(review, dict) or not rules.review_check(review, item["id"], checker, merged)["ok"]:
            bad.append(item["id"])
    if bad:
        return _check("C-18", False, "確かめの結果の形（pass / flag と上限 / rework と直す点）が正しくない作業: " + "、".join(bad))
    return _check("C-18", True, "確かめの結果は、どれも pass / flag（確信度の上限つき）/ rework（直す点つき）の形だった")


def c19(run: Run, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """result.json is not changed after it was taken in, and the summary shows the shared rule's confidence."""
    bad = []
    verify = try_json(run.dir / "verify.json") or {}
    shown = {r["id"]: r.get("confidence") for r in rows}
    for item in _reviewed(run):
        raw = try_json(run.result_file(item["id"])) or {}
        if item.get("result_sha") and file_sha(run.result_file(item["id"])) != item["result_sha"]:
            bad.append(f"{item['id']}（結果ファイルが受け取った後に変わった）")
            continue
        review = assess.shown_review(item, try_json(run.review_file(item["id"])) if item.get("review") else None)
        wanted = rules.effective_confidence(raw, assess.review_for_confidence(review),
                                            verify.get(item["id"]) if isinstance(verify, dict) else None)
        if item["id"] in shown and shown[item["id"]] != wanted["confidence"]:
            bad.append(item["id"])
    if bad:
        return _check("C-19", False, "人に見せる確信度が、いちばん低いものになっていない作業: " + "、".join(bad))
    return _check("C-19", True, "人に見せる確信度は、作業役・確かめ・ビルドでの確かめのいちばん低いもの（結果ファイルは変えていない）")


def c24(run: Run) -> dict[str, Any]:
    start = run.state.get("fingerprint_start")
    try:
        now = make_vcs(run.repo, run.settings).fingerprint()
    except RunError as exc:
        return _check("C-24", None, f"作業中のファイルを確かめられませんでした: {exc}")
    if not start:
        return _check("C-24", None, "始めたときの作業中のファイルの記録がありません")
    if now != start:
        return _check("C-24", False, "実行の間に、利用者の作業中のファイルが変わりました（人か AI が変えた可能性があります）")
    return _check("C-24", True, "実行の間、利用者の作業中のファイルは変わっていない。修正は作業用のコピーで作り、"
                                "Coverity のトリアージには書き込んでいない")


def c25(run: Run, rows: list[dict[str, Any]]) -> dict[str, Any]:
    path = run.summary_file()
    if not path.is_file():
        return _check("C-25", False, "summary.html がありません")
    text = path.read_text(encoding="utf-8")
    problems = []
    if not re.search(r"<h1>トリアージ結果 \d{4}-\d{2}-\d{2} \d{2}:\d{2}</h1>", text):
        problems.append("見出し「トリアージ結果 <日時>」")
    if not re.search(r"条件: .*? ／ 警告 \d+ 件（作業 \d+、うちグループ \d+）", text):
        problems.append("「条件: … ／ 警告 N 件（作業 M、うちグループ K）」の行")
    labels = re.findall(r'<div class="label">(.*?)</div>', text)
    if labels != list(report.TILES):
        problems.append("件数のタイルの並び")
    if not re.search(r"修正 \d+ ／ 逸脱 \d+ ／ 却下 \d+ ／ （空） \d+ ／ 推奨から変えたもの \d+", text):
        problems.append("いまの承認の内訳")
    if rows:
        header = re.search(r"<thead><tr>(.*?)</tr></thead>", text)
        if not header or re.findall(r"<th>(.*?)</th>", header.group(1)) != list(report.SUMMARY_COLUMNS):
            problems.append("6 列の表の見出し")
        order = re.findall(r'<td data-label="ID" class="id"><a href="[^"]*">(.*?)</a>', text)
        if order != [report._e(r["title"]) for r in rows]:
            problems.append("確信度の低い順の並び")
        if any(not re.fullmatch(r"G\d+（\d+ 件）", r["title"]) for r in rows if r["id"].startswith("G")):
            problems.append("グループの書き方「G1（3 件）」")
        if text.count('<span class="sub">') < 2 * len(rows):
            problems.append("ID 欄のチェッカー名と見立て欄の区分")
    if "<script" in text.lower():
        problems.append("スクリプトを含まない")
    if problems:
        return _check("C-25", False, "summary.html の形が違います: " + "、".join(problems))
    return _check("C-25", True, "summary.html は、見出し・条件の行・件数のタイル・承認の内訳・確信度の低い順の 6 列の表の形だった")


def c26(run: Run) -> dict[str, Any]:
    bad = []
    for item in _reviewed(run):
        path = run.report_file(item["id"])
        if not path.is_file():
            bad.append(f"{item['id']}（ファイルがない）")
            continue
        text = path.read_text(encoding="utf-8")
        heads = re.findall(r"<h2>(\d\. [^<]*?)(?: <span|</h2>)", text)
        title = re.search(r"<h1>(.*?) — (.*?)（(.*?)）</h1>", text)
        diff_lines = max((block.count("<span") for block in re.findall(r'<pre class="diff">(.*?)</pre>', text, re.S)), default=0)
        if heads != list(report.ITEM_SECTIONS) or not title or diff_lines > report.MAX_DIFF_LINES or "<script" in text.lower():
            bad.append(item["id"])
    if bad:
        return _check("C-26", False, "詳細レポートの形が違う作業: " + "、".join(bad))
    return _check("C-26", True, "詳細レポートは、見出し「<ID> — <チェッカー>（<Impact>）」と 1. 結論 〜 6. 処理情報 の順だった")


def _patch_files(patch: Path) -> set[str]:
    files = set()
    for line in patch.read_bytes().decode("utf-8", "replace").splitlines():
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if m:
            files.add(m.group(2))
            continue
        m = re.match(r"^Index: (.+)$", line)
        if m:
            files.add(m.group(1).strip().replace("\\", "/"))
    return files


def c27(run: Run) -> dict[str, Any]:
    problems = []
    if not RUN_ID.match(run.dir.name):
        problems.append(f"実行フォルダの名前 {run.dir.name}")
    if not run.summary_file().is_file():
        problems.append("summary.html")
    markdown = run.settings["options"]["markdown"]
    if markdown and not run.summary_file("md").is_file():
        problems.append("summary.md")
    for item in _reviewed(run):
        item_id = item["id"]
        if not run.report_file(item_id).is_file() or (markdown and not run.report_file(item_id, "md").is_file()):
            problems.append(f"cid/{item_id}")
        saved = read_json(run.item_dir(item_id) / "saved.json", {}).get("fix")
        if not saved:
            continue
        patch = run.patch_file(item_id)
        if not patch.is_file():
            problems.append(f"patches/{item_id}-fix.patch")
        elif _patch_files(patch) - set(saved.get("files") or []):
            problems.append(f"patches/{item_id}-fix.patch にほかの作業のファイルが混ざっている")
        for rel in saved.get("files") or []:
            source = run.dir / "fixed" / item_id / "fix" / rel
            if not source.is_file() and rel in _patch_files(patch) and b"deleted file" not in patch.read_bytes():
                problems.append(f"fixed/{item_id}/fix/{rel}")
    approvals_file = run.dir / "approvals.json"
    if not approvals_file.is_file():
        problems.append("approvals.json")
    elif schema.validate(read_json(approvals_file), "approvals.schema.json"):
        problems.append("approvals.json の形")
    if problems:
        return _check("C-27", False, "ファイルの置き場所が違います: " + "、".join(problems))
    return _check("C-27", True, "summary.html・cid/<ID>.html・patches/<ID>-fix.patch・fixed/<ID>/fix/・approvals.json が決まった場所にあり、"
                                "差分と修正後のファイルは作業ごとに独立している")


def run_checks(run: Run, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c16(run), c17(run), c18(run), c19(run, rows), c24(run), c25(run, rows), c26(run), c27(run)]
