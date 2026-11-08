#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""EVAL stage 3: the orchestrator triages every warning of the sample with ``claude -p``.

For each variant:

1. the self-test skill (a temp copy, run by path) makes its sample repository: git, a push target
   and a fake Coverity (``selftest.py start --sections 2`` and ``sample``). The variant sets
   ``agents.review`` / ``agents.integration`` in its config and commits it; ``--limit N`` asks for
   「N 件だけ」 in the request (the batch-run skill turns it into --limit; 1 for a smoke test);
2. claude_adapter extracts the orchestrator's table, followed through the agents of the table (all
   agents, the skills and the MCP server), into ``<repo>/.claude/`` (hidden through .git/info/exclude);
   the run folder's output_dir (outside the repository) is passed with ``--add-dir``;
3. ``claude -p "<request>" --agent <orchestrator> --max-turns N`` runs (the orchestrator is the main
   agent: a Claude Code subagent cannot call subagents);
4. checks: the self-test's ``check-run`` (2-2〜2-6: run finished, all items done, conclusions against
   expected.yaml, the group, files and the user's working files), summary.html and approvals.json,
   result.json for every item, review.json when review is on, the user's repository unchanged,
   only role agents called, the orchestrator edited no file; plus counts, accuracy, time and cost.

Variants: ``default`` (review and integration off, the design defaults) and ``full`` (both on).

  uv run --native-tls --quiet tests/eval/stage3/run_stage3.py [--variant default|full]... [--limit N]
        [--model M] [--agent-model M] [--timeout S] [--max-turns N] [--max-budget-usd X]
        [--out DIR] [--build-only] [--no-sync]

Writes <out>/<variant>/ (selftest/, transcript.jsonl, variant.json), <out>/results.json, <out>/report.md.
Prints one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import evalkit as ek  # noqa: E402

TAG = "stage3"
ROLE_ORCHESTRATOR = "統括"
ROLE_SELFTEST = "自己診断"
ROLE_RUN = "一括実行"
ROLES = {"worker": "作業役", "reviewer": "検証役", "integrator": "統合役"}
VARIANTS = {"default": {"review": False, "integration": False}, "full": {"review": True, "integration": True}}
DEFAULT_FILTER = "all.yaml"
PROMPT = "Coverity の警告を、条件ファイル {filter} で、すべてトリアージしてください。途中で質問せず、最後まで進めてください。"
# With --limit, the batch-run skill's own phrase for a limit (「N 件だけ」) instead of 「すべて」.
PROMPT_LIMIT = "Coverity の警告を、条件ファイル {filter} で、{limit} 件だけトリアージしてください。途中で質問せず、最後まで進めてください。"
RUN_DIR_RE = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SELFTEST_LEVELS = {"2-2": "must", "2-3": "must", "2-4": "should", "2-5": "should", "2-6": "must"}


# --------------------------------------------------------------------------- config of the sample


def set_agent_options(text: str, options: dict[str, bool]) -> str:
    """Set agents.<key> values in config.yaml text, keeping comments and other lines."""
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if re.match(r"^agents:\s*(#.*)?$", line)), None)
    value = {k: ("true" if v else "false") for k, v in options.items()}
    if start is None:
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines + ["agents:"] + [f"  {k}: {v}" for k, v in value.items()]) + "\n"
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()
                                or lines[end].lstrip().startswith("#")):
        end += 1
    seen = set()
    for i in range(start + 1, end):
        m = re.match(r"^(\s+)([A-Za-z_]+):\s*([^#\s]*)(\s*#.*)?$", lines[i])
        if m and m.group(2) in value:
            lines[i] = f"{m.group(1)}{m.group(2)}: {value[m.group(2)]}{m.group(4) or ''}"
            seen.add(m.group(2))
    lines[start + 1:start + 1] = [f"  {k}: {v}" for k, v in value.items() if k not in seen]
    return "\n".join(lines)


def prompt_for(filter_name: str, limit: int | None) -> str:
    return (PROMPT_LIMIT if limit else PROMPT).replace("{filter}", filter_name).replace("{limit}", str(limit or ""))


def prepare_sample(selftest_script: Path, vdir: Path, variant: str) -> dict:
    sdir = vdir / "selftest"
    started, res = ek.run_json(ek.uv_script(selftest_script, "start", "--sections", "2", "--dir", str(sdir),
                                            "--client", "Claude Code（EVAL）"), timeout=600)
    if not started or not started.get("ok"):
        raise ek.EvalError("自己診断のスクリプトの start が失敗しました",
                           [str((started or {}).get("error") or res["stderr"][-800:])])
    sample, res = ek.run_json(ek.uv_script(selftest_script, "sample", "--dir", str(sdir)), timeout=600)
    if not sample or not sample.get("ok"):
        raise ek.EvalError("自己診断のスクリプトの sample が失敗しました",
                           [str((sample or {}).get("error") or res["stderr"][-800:])])
    repo = Path(sample["repo_root"])
    config = repo / ".coverity-triage" / "config.yaml"
    config.write_text(set_agent_options(config.read_text(encoding="utf-8"), VARIANTS[variant]), encoding="utf-8")
    filter_name = sample.get("filter") or DEFAULT_FILTER
    ek.git(repo, "add", "-A")
    ek.git(repo, "commit", "-q", "-m", f"EVAL の設定（{variant}）", check=False)
    return {"selftest_dir": sdir, "repo": repo, "output_dir": Path(sample["output_dir"]), "filter": filter_name}


# --------------------------------------------------------------------------- after the run


def run_dirs(output_dir: Path) -> list[Path]:
    if not Path(output_dir).is_dir():
        return []
    return sorted(p for p in Path(output_dir).iterdir() if p.is_dir() and RUN_DIR_RE.match(p.name))


def find_run_dir(output_dir: Path) -> Path | None:
    runs = run_dirs(output_dir)
    return runs[-1] if runs else None


def accuracy(run_dir: Path, expected: dict) -> dict:
    items = expected.get("items") or {}
    rows = []
    for cid, want in items.items():
        result = ek.read_json(run_dir / "items" / str(cid) / "result.json")
        got = result.get("recommendation") if isinstance(result, dict) else None
        rows.append({"id": str(cid), "expected": want.get("recommendation"), "got": got,
                     "ok": got == want.get("recommendation")})
    return {"matched": sum(1 for r in rows if r["ok"]), "total": len(rows), "items": rows}


def item_checks(run_dir: Path, run_json: dict, review_on: bool, integration_on: bool) -> list[dict]:
    items = [i for i in run_json.get("items") or [] if isinstance(i, dict) and i.get("id") is not None]
    live = [i for i in items if i.get("status") != "merged"]
    no_result = [str(i["id"]) for i in live if not (run_dir / "items" / str(i["id"]) / "result.json").is_file()]
    checks = [ek.check("results_every_item", bool(live) and not no_result,
                       f"result.json のない作業: {no_result}" if no_result else f"{len(live)} 件すべてにある")]
    if review_on:
        done = [str(i["id"]) for i in live if i.get("status") == "done"]
        no_review = [d for d in done if not (run_dir / "items" / d / "review.json").is_file()]
        checks.append(ek.check("reviews_when_on", bool(done) and not no_review,
                               f"review.json のない作業: {no_review}" if no_review else f"{len(done)} 件すべてにある"))
    if integration_on:
        candidates = ek.read_json(run_dir / "integration" / "candidates.json")
        plan = (run_dir / "integration" / "plan.json").is_file()
        has_candidates = bool(candidates.get("candidates") if isinstance(candidates, dict) else candidates)
        checks.append(ek.check("integration_when_on", plan or not has_candidates,
                               "plan.json がある" if plan else ("統合の候補がない" if not has_candidates
                                                                 else "候補があるのに plan.json がない"), "should"))
    return checks


def agent_checks(summary: dict, role_names: dict[str, str]) -> list[dict]:
    allowed = set(role_names.values())
    called = summary["agents_called"]
    stems = [c.split(":")[-1] for c in called]
    others = sorted({c for c, s in zip(called, stems) if s not in allowed})
    workers = stems.count(role_names.get("worker", ""))
    edits = [str(u["input"].get("file_path"))[:160] for u in summary["tool_uses"]
             if u["parent"] is None and u["name"] in EDIT_TOOLS]
    by_role = {role: stems.count(name) for role, name in role_names.items()}
    return [
        ek.check("role_agents_only", not others,
                 f"役のエージェントのほかを呼んだ: {others}" if others else f"呼んだエージェント: {by_role}"),
        ek.check("worker_called", workers > 0, f"作業役を {workers} 回呼んだ"),
        ek.check("orchestrator_no_edits", not edits, f"統括エージェントが書いた: {edits[:5]}" if edits else "書いていない"),
    ]


def selftest_checks(selftest_script: Path, sdir: Path, run_dir: Path, limited: bool = False) -> tuple[list[dict], dict | None]:
    """The self-test's check-run (2-2〜2-6). A limited run cannot meet 2-4 and 2-5 (they need every warning)."""
    data, res = ek.run_json(ek.uv_script(selftest_script, "check-run", "--dir", str(sdir), "--run", str(run_dir)),
                            timeout=600)
    if not data or not data.get("ok"):
        return [ek.check("selftest_check_run", False, str((data or {}).get("error") or res["stderr"][-600:]))], data
    out = []
    for c in data.get("checks") or []:
        level = SELFTEST_LEVELS.get(str(c.get("id")), "should")
        ok = c.get("status") != "fail" if level == "must" else c.get("status") == "pass"
        if limited and str(c.get("id")) in ("2-4", "2-5"):
            ok = None  # skipped: the run was limited to fewer warnings than the sample has
        detail = f"{c.get('status')}: {c.get('actual', '')}" + (f"（{c['detail']}）" if c.get("detail") else "")
        out.append(ek.check(f"selftest-{c.get('id')}", ok, detail[:600], level))
    return out, data


def run_status(run_script: Path | None, run_dir: Path) -> dict | None:
    if run_script is None or not run_script.is_file():
        return None
    data, _ = ek.run_json(ek.uv_script(run_script, "status", "--run", str(run_dir)), timeout=300)
    return data


# --------------------------------------------------------------------------- a variant


def adapter_args(**kw) -> argparse.Namespace:
    base = dict(workspace=None, parts_from_table=None, agent=None, skill=None, mcp=None, plugin_root=None, main=None,
                output_dir=None, fake_data=None, mcp_env=None, model=None, agent_model=None, prompt=None, extra_arg=None,
                claude_bin=None, allow_missing=False, run=False, inspect=False, timeout=120.0)
    base.update(kw)
    return argparse.Namespace(**base)


def run_variant(variant: str, args: argparse.Namespace, ctx: dict) -> dict:
    vdir = ctx["out"] / variant
    ek.progress(TAG, f"{variant}: 見本のリポジトリを作る")
    sample = prepare_sample(ctx["selftest_script"], vdir, variant)
    repo, output_dir = sample["repo"], sample["output_dir"]
    venv = vdir / "mcp-venv"
    build = ek.ADAPTER.build(adapter_args(
        workspace=str(repo), parts_from_table=str(ctx["orchestrator"]["path"]), plugin_root=str(ctx["root"]),
        output_dir=str(output_dir), fake_data=str(repo / ".coverity-triage" / "fake-issues.yaml"),
        mcp_env=[f"UV_PROJECT_ENVIRONMENT={venv}"], model=ctx["model"], agent_model=args.agent_model,
        claude_bin=args.claude_bin))
    ek.exclude_in_git(repo, ".claude/")
    head = ek.head(repo)
    prompt = prompt_for(sample["filter"], args.limit)
    saved = ek.read_json(Path(build["files"]["command"]))
    argv = list(saved["argv"])
    argv[saved["prompt_index"]] = prompt
    argv += ["--max-turns", str(args.max_turns), "--output-format", "stream-json", "--verbose"]
    if args.max_budget_usd:
        argv += ["--max-budget-usd", str(args.max_budget_usd)]
    record = {"variant": variant, "options": VARIANTS[variant], "filter": sample["filter"], "limit": args.limit,
              "prompt": prompt, "repo": str(repo), "output_dir": str(output_dir), "cwd": saved["cwd"], "command": argv,
              "agents": [{"name": a["name"], "model": a["model"], "skills": a["skills"]} for a in build["agents"]],
              "skills": sorted(build["skills"]), "mcp_servers": sorted(build["mcp_servers"]),
              "build_problems": build.get("problems")}
    if not args.no_sync:
        for name, folder in build["mcp_servers"].items():
            sync = ek.uv_sync(Path(folder), env=ek.claude_env({"UV_PROJECT_ENVIRONMENT": str(venv)}))
            if sync["code"] != 0:
                raise ek.EvalError(f"MCP サーバー {name} の用意（uv sync）が失敗しました", [sync["stderr"][-1500:]])
    if args.build_only:
        record.update({"built": True, "checks": [], "passed": None})
        ek.write_json(vdir / "variant.json", record)
        return record

    ek.progress(TAG, f"{variant}: claude -p --agent {ctx['orchestrator']['name']}（時間の上限 {args.timeout:g} 秒）")
    run = ek.run_claude(argv, Path(saved["cwd"]), args.timeout, vdir / "transcript.jsonl")
    summary = ek.summarize(run["events"], prompt)
    checks = [ek.check("finished", not run["timed_out"] and summary["has_result"] and not summary.get("is_error"),
                       ("時間切れ" if run["timed_out"] else "")
                       + ("" if summary["has_result"] else " result の知らせなし")
                       + (f" エラーで終わった（{summary.get('subtype')}）" if summary.get("is_error") else "")
                       or f"{summary.get('num_turns')} ターンで終わった")]
    run_dir = find_run_dir(output_dir)
    checks.append(ek.check("run_folder", run_dir is not None, str(run_dir) if run_dir else f"実行フォルダがありません: {output_dir}"))
    made = run_dirs(output_dir)
    checks.append(ek.check("one_run_folder", len(made) <= 1,
                           f"実行フォルダを {len(made)} つ作った（{', '.join(p.name for p in made)}）", "should"))
    status, selftest, acc = None, None, None
    if run_dir is not None:
        run_json = ek.read_json(run_dir / "run.json") or {}
        status = run_status(ctx["run_script"](build), run_dir)
        finished = bool((status or run_json).get("finished"))
        checks.append(ek.check("run_finished", finished and not (status or {}).get("halted"),
                               f"finished {finished}、halted {(status or {}).get('halted')}"
                               + (f"（{status.get('halt_reason')}）" if status and status.get("halt_reason") else "")))
        missing = [n for n in ("summary.html", "approvals.json") if not (run_dir / n).is_file()]
        checks.append(ek.check("summary_files", not missing, f"ない: {missing}" if missing else "summary.html と approvals.json がある"))
        checks += item_checks(run_dir, run_json, VARIANTS[variant]["review"], VARIANTS[variant]["integration"])
        st_checks, selftest = selftest_checks(ctx["selftest_script"], sample["selftest_dir"], run_dir, bool(args.limit))
        checks += st_checks
        acc = accuracy(run_dir, ctx["expected"]) if not args.limit else None
    changed = ek.working_changes(repo)
    moved = ek.head(repo) != head
    checks.append(ek.check("repo_unchanged", not changed and not moved,
                           ("変わったファイル: " + "、".join(changed[:10]) if changed else "") + (" HEAD が変わった" if moved else "")
                           or "利用者のリポジトリは変わっていない"))
    checks += agent_checks(summary, ctx["role_names"])
    record.update({
        "checks": checks, "passed": ek.verdict_of(checks), "run_dir": str(run_dir) if run_dir else None,
        "seconds": run["seconds"], "cost_usd": summary.get("cost_usd"), "num_turns": summary.get("num_turns"),
        "usage": summary.get("usage"), "timed_out": run["timed_out"], "exit_code": run["exit_code"],
        "tools": summary["main_tool_counts"], "agents_called": summary["agents_called"],
        "skills_loaded": summary["skills_loaded"], "tool_errors": summary["tool_errors"][:20],
        "permission_denials": summary["permission_denials"][:20], "reply": ek.final_text(summary)[:4000],
        "accuracy": acc, "counts": (status or {}).get("counts"), "criteria_results": (status or {}).get("criteria_results"),
        "errors": (status or {}).get("errors"), "selftest": selftest, "init": summary.get("init"),
        "transcript": run["transcript"]})
    ek.write_json(vdir / "variant.json", record)
    ek.progress(TAG, f"{variant}: {'合格' if record['passed'] else '不合格'}（{run['seconds']:.0f} 秒、{ek.money(summary.get('cost_usd'))}）")
    return record


def counts_text(counts: dict | None) -> str:
    if not counts:
        return "－"
    text = f"終わった {counts.get('done', 0)} / {counts.get('total', 0)}"
    extra = [f"{k} {counts[k]}" for k in ("error", "pending", "in_progress", "merged") if counts.get(k)]
    return text + (f"（{'、'.join(extra)}）" if extra else "")


def render_report(meta: dict, records: list[dict]) -> str:
    lines = ["# 3段階目（一括のトリアージ）の EVAL の結果", "",
             f"- 日時: {meta['started_at']}", f"- 統括エージェント: `{meta['orchestrator']}`（モデル `{meta['model']}`）",
             f"- エージェントのモデル: {meta['agent_model'] or '各定義の claude-* のモデル'}",
             f"- 部品: {', '.join(meta['parts'])}", f"- 見本: 自己診断の sample-target（期待は expected.yaml）", "",
             "## まとめ", ""]
    lines += ek.md_table(["変種", "合否", "落ちた確かめ", "件数", "推奨の一致", "時間（秒）", "ターン", "費用"],
                         [[r["variant"], ek.mark(r.get("passed")),
                           "、".join(c["id"] for c in r.get("checks", []) if c["ok"] is False) or "－",
                           counts_text(r.get("counts")),
                           (f"{r['accuracy']['matched']}/{r['accuracy']['total']}" if r.get("accuracy") else "－"),
                           r.get("seconds", "－"), r.get("num_turns", "－"), ek.money(r.get("cost_usd"))] for r in records])
    for r in records:
        lines += ["", f"## {r['variant']}（review {r['options']['review']}、integration {r['options']['integration']}、"
                  f"条件ファイル {r['filter']}）", "", f"頼みの文: {r['prompt']}", "", f"実行フォルダ: `{r.get('run_dir')}`", ""]
        lines += ek.md_table(["確かめ", "重さ", "結果", "詳しく"],
                             [[c["id"], c["level"], ek.mark(c["ok"]), c["detail"]] for c in r.get("checks", [])])
        if r.get("accuracy"):
            lines += [""] + ek.md_table(["作業", "想定", "推奨", "一致"],
                                        [[a["id"], a["expected"], a["got"], ek.mark(a["ok"])] for a in r["accuracy"]["items"]])
        if r.get("criteria_results"):
            failed = [c for c in r["criteria_results"] if c.get("failed")]
            lines += ["", "評価基準で不合格のあったもの: " + ("、".join(f"{c['id']}（{c['failed']} 件）" for c in failed) or "なし")]
        if r.get("errors"):
            lines += ["", "処理できなかった作業・エラー: " + "；".join(f"{e.get('item')}: {e.get('detail')}" for e in r["errors"][:10])]
        if r.get("reply"):
            lines += ["", "<details><summary>統括エージェントの最後の返事</summary>", "", "```text", r["reply"][:3000], "```",
                      "", "</details>"]
    return "\n".join(lines) + "\n"


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="3段階目（一括のトリアージ）の EVAL を claude -p で動かす")
    p.add_argument("--plugin-root")
    p.add_argument("--variant", action="append", choices=sorted(VARIANTS),
                   help="default（確かめと統合なし）か full（両方あり）。何度でも。既定は両方")
    p.add_argument("--limit", type=int, help="調べる件数の上限（頼みの文を「N 件だけ」にする。試しは 1）")
    p.add_argument("--model", help="統括エージェント（メイン）のモデル（既定はその定義の claude-* のモデル）")
    p.add_argument("--agent-model", help="作業・検証・統合のエージェントのモデル（既定は各定義の claude-* のモデル）")
    p.add_argument("--timeout", type=float, default=2700.0, help="1つの変種の時間の上限（秒。既定 45 分）")
    p.add_argument("--max-turns", type=int, default=500)
    p.add_argument("--max-budget-usd", type=float, help="1つの変種の費用の上限（claude の --max-budget-usd）")
    p.add_argument("--out", help="結果のフォルダ（既定 $TMPDIR/<プラグイン>-evals/stage3/<日時>）")
    p.add_argument("--build-only", action="store_true", help="見本と作業フォルダとコマンドを作るだけ")
    p.add_argument("--no-sync", action="store_true", help="MCP サーバーの uv sync をしない")
    p.add_argument("--claude-bin", nargs="+", help="claude の代わりに動かすコマンド（試験用）")
    return p


def run(args: argparse.Namespace) -> dict:
    root = Path(args.plugin_root).resolve() if args.plugin_root else ek.PLUGIN_ROOT
    if args.limit is not None and args.limit < 1:
        raise ek.EvalError("--limit は 1 以上です")
    orchestrator = ek.part(ROLE_ORCHESTRATOR, root)
    selftest = ek.part(ROLE_SELFTEST, root)
    run_skill = ek.part(ROLE_RUN, root)["name"]
    role_names = {role: ek.part(word, root)["name"] for role, word in ROLES.items()}
    out = ek.prepare_out(args.out, "stage3", root)
    tools = ek.copy_tree(selftest["path"], out / "tools" / selftest["path"].name)
    selftest_script = tools / "scripts" / "selftest.py"
    if not selftest_script.is_file():
        raise ek.EvalError(f"自己診断のスクリプトがありません: {selftest_script}")
    expected = ek.load_yaml(tools / "assets" / "expected.yaml") or {}
    agent = ek.ADAPTER.load_agent(orchestrator["path"])
    model = args.model or ek.ADAPTER.claude_model(agent["fields"].get("model"))
    model = None if model == "inherit" else model
    collected = ek.ADAPTER.collect_parts(orchestrator["path"], ek.ADAPTER.Layout(root))
    ctx = {"root": root, "out": out, "orchestrator": orchestrator, "selftest_script": selftest_script,
           "expected": expected, "model": model, "role_names": role_names,
           "run_script": lambda build: (Path(build["skills"][run_skill]) / "scripts" / "run.py") if run_skill in build["skills"] else None}
    meta = {"started_at": datetime.now().isoformat(timespec="seconds"), "orchestrator": orchestrator["name"],
            "model": model or "（claude の既定）", "agent_model": args.agent_model,
            "parts": [a["name"] for a in collected["agents"]] + sorted(collected["skills"]) + sorted(collected["mcp"]),
            "out": str(out)}
    records = [run_variant(v, args, ctx) for v in (args.variant or list(VARIANTS))]
    report = ek.write_text(out / "report.md", render_report(meta, records))
    done = [r for r in records if r.get("passed") is not None]
    result = {"ok": True, "kind": "stage3", "out": str(out), "report": str(report), "meta": meta,
              "passed": sum(1 for r in done if r["passed"]), "total": len(done),
              "cost_usd": round(sum(r.get("cost_usd") or 0 for r in records), 6), "variants": records}
    ek.write_json(out / "results.json", result)
    return {**{k: v for k, v in result.items() if k != "variants"},
            "variants": [{"variant": r["variant"], "passed": r.get("passed"),
                          "failed": [c["id"] for c in r.get("checks", []) if c["ok"] is False],
                          "counts": r.get("counts"), "accuracy": (r.get("accuracy") or {}).get("matched"),
                          "seconds": r.get("seconds"), "cost_usd": r.get("cost_usd"),
                          **({"cwd": r["cwd"], "command": r["command"]} if args.build_only else {})} for r in records]}


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
