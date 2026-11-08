"""Instruction files for the agents (design 4.14). Each says everything the job needs: the facts, the
folders and files, what may be changed, and the one-line answer. They name no skill and no agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import issues, rules, schema
from .common import read_json, write_text
from .config import knowledge_path
from .state import Run, is_group

CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}
PLAN_JA = {"fix": "修正", "deviation": "逸脱"}


def prompt(path: Path) -> str:
    return f"指示ファイル {path} を読み、書かれた手順に従ってください。"


def investigate_root(run: Run) -> Path:
    """The code to read: the copy of the analyzed revision, else the person's repository (read only)."""
    return run.work("a") if run.state.get("analyzed_revision") else run.repo


def _table(rows: list[tuple[str, Any]]) -> list[str]:
    return ["| 用途 | 場所 |", "|---|---|", *[f"| {k} | `{v}` |" for k, v in rows], ""]


def _knowledge(run: Run, rows: list[tuple[str, Any]]) -> None:
    path = knowledge_path(run.repo)
    if path.is_file():
        rows.append(("プロジェクトの知識（読む。コードと食い違えばコードが正しい）", path))


def warning_lines(run: Run, item: dict[str, Any], with_code: bool = True) -> list[str]:
    found, events, description, notes = issues.view(run, item)
    root = investigate_root(run)
    lines: list[str] = []
    for issue in found:
        evs = events.get(issue["cid"]) or []
        where = f"`{issue.get('file')}`:{issue.get('line') or '-'}" + (f" {issue['function']}()" if issue.get("function") else "")
        lines += [f"### CID {issue['cid']} — {issue.get('checker')}（Impact {issue.get('impact') or '-'}）", "",
                  f"- 場所: {where}", f"- CWE: {issue.get('cwe') or '-'}"]
        if issue.get("macro"):
            lines.append(f"- マクロ: {issue['macro']}")
        lines += ["- 警告経路（★ は問題の起きる場所）:", ""]
        lines += [f"  {n}. {'★ ' if e['main'] else ''}`{e['file']}`:{e['line'] or '-'} {e['tag']} — {e['description']}"
                  for n, e in enumerate(evs, 1)] or ["  （取れませんでした。警告の場所から調べます）"]
        lines.append("")
        if with_code:
            lines += ["前後のコード（調べるコードから。★ は問題の起きる行）:", "", *issues.excerpt(root, evs, issue)]
    if description:
        lines += ["チェッカーの説明（Coverity）:", "", description, ""]
    if notes:
        lines += ["パスなどの注意:", "", *[f"- {n}" for n in notes], ""]
    return lines


def drift_findings(run: Run, item: dict[str, Any]) -> list[str]:
    """Signs that the local code differs from the analyzed code (only when the analyzed revision is unknown)."""
    found, events, _, _ = issues.view(run, item)
    out = []
    for issue in found:
        files = {issue.get("file")} | {e["file"] for e in events.get(issue["cid"], [])}
        for rel in sorted(f for f in files if f and f != "-"):
            lines = issues.read_lines(run.repo, rel)
            if lines is None:
                out.append(f"CID {issue['cid']}: ファイルがありません: {rel}")
                continue
            wanted = [e["line"] for e in events.get(issue["cid"], []) if e["file"] == rel and e["line"]]
            if wanted and max(wanted) > len(lines):
                out.append(f"CID {issue['cid']}: {rel} は {len(lines)} 行しかなく、警告の {max(wanted)} 行目がありません")
            func = (issue.get("function") or "").split("::")[-1]
            if rel == issue.get("file") and func and func not in "\n".join(lines):
                out.append(f"CID {issue['cid']}: {rel} に関数 {issue['function']} が見つかりません")
    return out


def worker(run: Run, item: dict[str, Any], fix_problems: list[str] | None = None) -> Path:
    item_id = item["id"]
    folder = run.item_dir(item_id)
    analyzed = run.state.get("analyzed_revision")
    latest = run.state.get("latest_revision")
    rows = [("調べるコード（読むだけ。変えない）", investigate_root(run)),
            ("直すフォルダ（作業用のコピー。直すのはここだけ）", run.work(f"w{item['slot']}"))]
    if run.settings["options"]["annotation"]:
        rows.append(("注釈を入れるフォルダ（逸脱案の注釈だけを入れる）", run.work(f"n{item['slot']}")))
    rows += [("結果ファイル（書く）", run.result_file(item_id)),
             ("結果ファイルの形（JSON Schema）", run.dir / "schemas" / "result.schema.json"),
             ("進み具合（書く。節目ごとに上書き）", folder / "progress.json")]
    _knowledge(run, rows)
    cids = ", ".join(map(str, item["cids"]))
    example = json.dumps({"item": item_id, "phase": "judge", "note": "呼び出し元を確かめている"}, ensure_ascii=False)
    lines = [f"# 作業の指示: {item_id}", "",
             f"Coverity の警告 {len(item['cids'])} 件（CID {cids}）が本物の不具合かを調べ、**修正案と逸脱コメント案の両方**を"
             "作って、結果ファイルに書きます。どちらを採るかは人が決めます。", "",
             "## フォルダとファイル", "", *_table(rows),
             "## 守ること", "",
             "- コマンド（ターミナル）は使いません。ファイルを読む・探す・直す道具だけを使います。",
             "- 直してよいのは、直すフォルダ（とオプションの注釈のフォルダ）、結果ファイル、進み具合のファイルだけです。"
             "調べるコードと、利用者のリポジトリは変えません。",
             "- パスは、どのフォルダでもリポジトリの一番上からの相対パスです（結果ファイルの evidence も同じ）。",
             (f"- 調べるコードは、Coverity が解析したリビジョン {analyzed} です。" if analyzed else
              "- 解析したリビジョンが分からないため、調べるコードは利用者の手元のコードです。警告の行と内容が合っているか"
              "（ずれ）を確かめ、結果の revision_drift に書きます。"),
             f"- 直すフォルダは、取り込み先の最新のリビジョン {latest or '-'} です。最新でもう直っているときは、何も直さずに "
             "fix.already_fixed_on_latest を true にします。",
             "- 結果ファイルは最後に書きます（書いた後で直すフォルダを変えない）。", "",
             "## 逸脱コメントの書き方（書いた後にスクリプトが確かめます）", "",
             "- 「誤検知。」「意図的。」「本物の不具合。」のどれかで始め、そのあとに問題にならない理由とコードの場所（ファイルと行）を"
             "1〜2 文で書きます。全体で 200 文字以内、前後に空白を入れず、改行と書式（見出し・箇条書き・太字・コードの印）を使いません。",
             "- 条件・但し書き・推量（「ただし」「場合に限る」「おそらく」「かもしれない」など）、調べた過程（「確認できなかった」など）、"
             "推奨や依頼（「を推奨」「ください」「要確認」など）は書きません。条件なしで言い切れないときは、推奨を修正にします。", "",
             "## 進み具合", "",
             f"節目ごとに、進み具合のファイルへ `{example}` の形で上書きします（人が画面で見ます。書けなくても作業は続けます）。"
             "phase は read（警告経路とコードを読む）→ judge（真偽を決める）→ fix（修正を作る）→ deviation（逸脱コメントを書く）"
             "→ result（結果を書く）の順です。note は 60 文字ほどで、時刻は書きません。", "",
             "## 警告", "", *warning_lines(run, item)]
    if not analyzed:
        drift = drift_findings(run, item)
        if drift:
            lines += ["## 機械的に見つけたずれ", "", *[f"- {d}" for d in drift], ""]
    if is_group(item_id) and not item.get("merged_from"):
        lines += ["## グループ", "",
                  "同じ原因と思われる警告をまとめた作業です。すべてが同じ原因かを確かめ、原因の違う CID は "
                  "group_excluded_cids に入れます（その CID は別の作業になります。迷うときは外します。1 つは残します）。"
                  "修正案と逸脱コメント案は、残した CID に対して作ります。", ""]
    lines += _merge_lines(run, item) + _rework_lines(run, item)
    if fix_problems:
        lines += ["## 結果ファイルの直すこと", "",
                  "前に書いた結果ファイルを確かめたところ、次の点が決まりに合いませんでした。結果ファイルを読み、"
                  "これらの点だけを直して書き直します（進み具合の phase は result）。",
                  *[f"- {p}" for p in fix_problems], ""]
    lines += ["## 返す形", "",
              "結果ファイルを書いたら、呼び出し元に次の形の 1 行だけを返します。",
              f"`{item_id}: <推奨（修正 か 逸脱）>（確信度 <高 / 中 / 低>）— <40 文字ほどの要約>`", "",
              "続けられない問題があったときは、結果ファイルを書かずに、理由を 1 行で返します。", ""]
    path = folder / "brief.md"
    write_text(path, "\n".join(lines))
    return path


def _merge_lines(run: Run, item: dict[str, Any]) -> list[str]:
    merged = read_json(run.dir / "integration" / "merged.json", {}).get(item["id"])
    if not item.get("merged_from") or not merged:
        return []
    what = ("確かめの指摘を直すときの参考に、元の作業の結果と差分を読みます。" if item.get("rework") else
            "それぞれの結果と差分を読み、すべての CID に効く 1 つの修正を、直すフォルダで作り直します"
            "（直すフォルダは最新のリビジョンのままで、まだ何も直していません）。")
    lines = ["## 統合の指示", "",
             f"作業 {', '.join(item['merged_from'])} は同じ原因と判断され、1 つの作業にまとめられました。{what}",
             f"まとめた理由: {merged.get('reason', '')}", "",
             "| 元の作業 | 結果ファイル | 修正の差分 |", "|---|---|---|"]
    for member in item["merged_from"]:
        patch = run.patch_file(member)
        lines.append(f"| {member} | `{run.result_file(member)}` | " + (f"`{patch}`" if patch.is_file() else "なし") + " |")
    lines += ["", f"まとめた見立てと逸脱コメントの案は `{run.dir / 'integration' / 'merged.json'}` の `{item['id']}` にあります。"
              "内容がコードと合っていれば、そのまま使います。", ""]
    return lines


def _rework_lines(run: Run, item: dict[str, Any]) -> list[str]:
    if not item.get("rework"):
        return []
    folder = run.item_dir(item["id"])
    copy = ("直すフォルダは最新のリビジョンのままです（前の修正を当てられなかったため）。前の結果を参考に作り直します。"
            if (folder / "rework-fresh").is_file() else "直すフォルダには、前の修正が当ててあります。")
    lines = ["## 確かめの指摘（直すこと）", "",
             "別の検証役が前の結果を確かめ、次の点を指摘しました。", *[f"- {r}" for r in item["rework"]], "",
             f"前の結果ファイルは `{folder / 'result.previous.json'}` です。{copy}"
             "指摘の点を確かめ、正しければ直します。指摘が当たらないと分かったときは、その根拠を confidence_reason に書きます。"
             "結果ファイルは、前の結果をもとに全体を書き直します。",
             "- group_excluded_cids には、今の作業の CID の中から書きます。", ""]
    if run.settings["options"]["annotation"]:
        lines.insert(-1, "- 注釈を入れるフォルダは最新のリビジョンに戻してあります。注釈も入れ直します。")
    return lines


def _result_lines(result: dict[str, Any]) -> list[str]:
    verdict = result.get("verdict") or {}
    deviation = result.get("deviation") or {}
    fix = result.get("fix") or {}
    drift = result.get("revision_drift") or {}
    evidence = "、".join(f"`{e.get('file')}`:{e.get('line') or '-'}（{e.get('note', '')}）"
                        for e in verdict.get("evidence") or []) or "なし"
    lines = [f"- 見立て: {verdict.get('judgement')} — {verdict.get('summary', '')}",
             f"- 推奨: {result.get('recommendation')} ／ 確信度: {result.get('confidence')}（{result.get('confidence_reason', '')}）",
             f"- 根拠の場所: {evidence}",
             f"- 逸脱案: {deviation.get('classification')} / {deviation.get('action')} / {deviation.get('severity', 'Unspecified')}",
             f"  - 逸脱コメント: {deviation.get('comment', '')}",
             f"- 修正案: {fix.get('summary', '')}（影響: {fix.get('impact', '')}）"
             + ("。最新のリビジョンでもう直っている" if fix.get("already_fixed_on_latest") else ""),
             f"- リビジョンのずれ: {drift.get('status')}" + (f"（{drift['detail']}）" if drift.get("detail") else "")]
    if fix.get("exceeded_constraints"):
        lines.append(f"- 修正の決まりを超えた点: {'、'.join(fix['exceeded_constraints'])}")
    if result.get("group_excluded_cids"):
        lines.append(f"- グループから外した CID: {result['group_excluded_cids']}")
    return lines


def lint_for(run: Run, result: dict[str, Any]) -> list[dict[str, Any]]:
    """The points the fixed rules find in a result, against the code the worker read (contract "lint")."""
    return rules.lint_findings(result, investigate_root(run))


def review_context(run: Run, item: dict[str, Any]) -> tuple[str | None, bool]:
    """(checker, merged) of an item: what decides which conditional criteria apply to its review."""
    found = issues.mapped(run)
    checker = next((found[c].get("checker") for c in item["cids"] if c in found and found[c].get("checker")), None)
    return checker, bool(item.get("merged_from"))


def reviewer(run: Run, item: dict[str, Any], fix_problems: list[str] | None = None) -> Path:
    item_id = item["id"]
    result = read_json(run.result_file(item_id))
    saved = read_json(run.item_dir(item_id) / "saved.json", {})
    patch = (saved.get("fix") or {}).get("patch")
    rows = [("調べるコード（読むだけ）", investigate_root(run)),
            ("確かめる結果（読むだけ）", run.result_file(item_id))]
    if patch:
        rows += [("修正の差分（読むだけ）", patch), ("修正後のファイル（読むだけ）", run.dir / "fixed" / item_id / "fix")]
    rows += [("確かめの結果（書く）", run.review_file(item_id)),
             ("確かめの結果の形（JSON Schema）", run.dir / "schemas" / "review.schema.json"),
             ("評価基準（scope が item のもの）", run.dir / "schemas" / "criteria.json"),
             ("進み具合（書く。節目ごとに上書き）", run.dir / "agents" / "reviewer" / "progress.json")]
    _knowledge(run, rows)
    example = json.dumps({"item": item_id, "phase": "check", "note": "根拠の行を読み直している"}, ensure_ascii=False)
    lines = [f"# 確かめの指示: {item_id}", "",
             "別の作業役が調べた結果を、**独立に**確かめます。最初に下の「警告」と前後のコードを読み、★ の場所と経路の条件を"
             "自分で確かめて判断してから、「確かめる結果」を読み比べます。結果と差分は直しません。", "",
             "## フォルダとファイル", "", *_table(rows),
             "## 守ること", "",
             "- コマンド（ターミナル）は使いません。書いてよいのは、確かめの結果と進み具合のファイルだけです。",
             "- 結果ファイル・差分・コードは変えません（変わっていたら、この確かめは使われません）。", "",
             "## 進み具合", "", f"`{example}` の形で上書きします。phase は read → check → write の順です。", "",
             "## 警告", "", *warning_lines(run, item),
             "## 確かめる結果", "", *_result_lines(result)]
    if item.get("merged_from"):
        lines.append(f"- この作業は、作業 {', '.join(item['merged_from'])} を同じ原因としてまとめたものです。"
                     "すべての CID が同じ原因か、修正がすべての警告に効くかも確かめます。")
    elif is_group(item_id):
        lines.append("- この作業は、同じ原因と思われる警告のグループです。すべての CID が同じ原因かも確かめます。")
    criteria = rules.item_criteria()
    na = rules.not_applicable_word()
    applies = rules.applicability(*review_context(run, item))
    lines += ["", "## 評価基準（警告ごと）", "",
              f"findings に、次の {len(criteria)} 個の基準を、すべて 1 回ずつ書きます（check に id、ok に合否、detail に理由、"
              f"分かれば file と line）。この警告に当てはまらない基準も省かず、ok を true にし、detail に「{na}」と書きます。"
              f"当てはまる基準の detail は「{na}」で始めません。", ""]
    for c in criteria:
        when = ""
        if c.get("applies_when"):
            fits = applies.get(c["id"])
            mark = ("この警告には当てはまる" if fits else
                    f"この警告には当てはまらない。ok を true、detail を「{na}」にする" if fits is False else
                    "当てはまるかを確かめる")
            when = f"（{c['applies_when']}。{mark}）"
        lines.append(f"- {c['id']}{when}: {c['text']}")
    points = lint_for(run, result)
    lines += ["", "## スクリプトが決まった方法で見つけた点（重みは自分で決める）", ""]
    lines += [f"- {p['message']}" for p in points] or ["- なし"]
    lines += ["", "## 確かめの結果の書き方", "",
              "- outcome は pass（合格）／ flag（要確認。confidence_cap に high・medium・low のどれかを書く）／ "
              "rework（差し戻し。rework に、作業役が直す点を 1 点ずつ書く）。note に人が読む 1〜2 文を書きます。",
              "- ok が false の基準があるときは、pass にしません（flag か rework にします）。rework のときは、差し戻す理由の基準を "
              "ok: false にします。pass と flag のときは、rework を空の配列 [] にします。", ""]
    if fix_problems:
        lines += ["## 確かめの結果の直すこと", "",
                  "前に書いた確かめの結果を確かめたところ、次の点が決まりに合いませんでした。これらの点だけを直して書き直します。",
                  *[f"- {p}" for p in fix_problems], ""]
    lines += ["## 返す形", "", "確かめの結果を書いたら、呼び出し元に次の形の 1 行だけを返します。",
              f"`{item_id}: <合格 / 要確認 / 差し戻し> — <理由を短く>`", ""]
    path = run.dir / "agents" / "reviewer" / f"{item_id}.md"
    write_text(path, "\n".join(lines))
    return path


def integrator(run: Run, found: list[dict[str, Any]], fix_problems: list[str] | None = None) -> Path:
    from .config import load_no_grouping

    plan_file = run.dir / "integration" / "plan.json"
    rows = [("計画（書く）", plan_file), ("計画の形（JSON Schema）", run.dir / "schemas" / "plan.schema.json"),
            ("進み具合（書く。節目ごとに上書き）", run.dir / "agents" / "integrator" / "progress.json"),
            ("調べるコード（読むだけ）", investigate_root(run))]
    _knowledge(run, rows)
    no_grouping = load_no_grouping(run.repo)
    table, paths = [], []
    for item in run.state["items"]:
        if item["status"] != "done" or set(item["cids"]) & no_grouping:
            continue
        result = read_json(run.result_file(item["id"]), {})
        found_issues, events, _, _ = issues.view(run, item)
        main = found_issues[0]
        where = f"{main.get('file')}:{main.get('line') or '-'} {main.get('function') or ''}".strip()
        patch = run.patch_file(item["id"])
        verdict = result.get("verdict") or {}
        table.append(f"| {item['id']} | {', '.join(map(str, item['cids']))} | {main.get('checker')} | `{where}` | "
                     f"{verdict.get('judgement')} | {result.get('recommendation')} | {result.get('confidence')} | "
                     f"{verdict.get('summary', '')} | `{run.result_file(item['id'])}` | "
                     + (f"`{patch}`" if patch.is_file() else "なし") + " |")
        for issue in found_issues:
            evs = events.get(issue["cid"]) or []
            stars = [e for e in evs if e["main"]] or evs[-1:]
            paths.append(f"- {item['id']} / CID {issue['cid']}（{issue.get('checker')}）: "
                         + ("、".join(f"`{e['file']}`:{e['line'] or '-'} {e['tag']} — {e['description']}" for e in stars)
                            or f"`{issue.get('file')}`:{issue.get('line') or '-'}（警告経路なし）"))
    lines = ["# 統合の指示", "",
             "調べ終わった作業のうち、**同じ原因**のものを見つけ、1 つの作業にまとめる計画を書きます。コードは変えません。"
             "まとめるのは、原因が 1 つで、1 か所を直せば（または 1 つの理由で）すべての警告が片付くものだけです。迷うときはまとめません。", "",
             "## フォルダとファイル", "", *_table(rows),
             "## 守ること", "",
             "- コマンド（ターミナル）は使いません。書いてよいのは、計画と進み具合のファイルだけです。",
             "- パスは、どれもリポジトリの一番上からの相対パスです。", "",
             "## 進み具合", "", "phase は read → compare → write の順で、`{\"item\": \"integration\", \"phase\": \"compare\", "
             "\"note\": \"…\"}` の形で上書きします。", "",
             "## 候補（決まった方法で見つけた組）", ""]
    for n, group in enumerate(found, 1):
        lines.append(f"{n}. 作業 {', '.join(group['items'])}")
        lines += [f"   - {pair}: {'、'.join(why)}" for pair, why in group["reasons"].items()]
    lines += ["", "候補にない組でも、結果と差分を読んで同じ原因だと言えるなら、理由を書いてまとめてかまいません。", "",
              "## 調べ終わった作業", "",
              "| 作業 | CID | チェッカー | 場所 | 見立て | 推奨 | 確信度 | 要約 | 結果ファイル | 修正の差分 |",
              "|---|---|---|---|---|---|---|---|---|---|", *table, "",
              "## 警告経路の ★（問題の起きる場所）", "", *paths, "",
              "## 計画の決まり", "",
              f"- 1 つにまとめる作業は 2 つ以上、CID は合わせて {rules.max_cids()} 件まで。1 つの作業は 1 つのまとまりにだけ入れます。",
              "- まとめてよいのは、上の表にある作業だけです（グループにしない CID を含む作業は載せていません）。",
              "- result には、まとめた結果（結果ファイルと同じ形）を書きます。confidence は、まとめる作業のいちばん低い確信度以下にします。"
              "逸脱コメントは、すべての CID に当てはまるように書きます。",
              "- fix_source は item:<作業の ID>（その作業の修正ですべてが片付く）／ combine（修正を合わせる）／ "
              "rework（作り直す）のどれかです。",
              "- 原因が違うのに同じ箇所を変えている作業は、まとめずに conflicts に書きます。",
              "- まとめるものがなければ、merges を空にした計画を書きます。", ""]
    if fix_problems:
        lines += ["## 計画の直すこと", "", "前に書いた計画を確かめたところ、次の点が決まりに合いませんでした。これらの点を直して書き直します。",
                  *[f"- {p}" for p in fix_problems], ""]
    lines += ["## 返す形", "", "計画を書いたら、呼び出し元に次の形の 1 行だけを返します。",
              "`統合: <まとめた数> つにまとめる（<作業の ID>）` か `統合: まとめるものはない`", ""]
    path = run.dir / "agents" / "integrator" / "brief.md"
    write_text(path, "\n".join(lines))
    return path


def copy_schemas(run_dir: Path) -> None:
    """The contract files the briefs point to (the agents never read this skill's own folder)."""
    for name in ("result.schema.json", "review.schema.json", "plan.schema.json", "criteria.json"):
        data = (schema.ASSETS_DIR / name).read_bytes()
        target = run_dir / "schemas" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
