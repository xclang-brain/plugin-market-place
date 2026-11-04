"""The order of /coverity-run: ``ct.py tick`` tells the orchestrating agent what to do next.

The orchestrating agent (the chat AI) never decides the order itself: it starts the agents tick
names (in the background), and when one of them ends it calls tick again with ``--finished`` (or
``--failed``). The steps, each skipped when its option is off:

1. initial analysis (option verify.initial): build, analyze and commit the open folder (``initial.py``)
2. the collector agent searches the warnings; tick makes the work items (``triage.plan``)
3. the collector agent fetches the warning paths of every item
4. the worker agents investigate the items, up to ``parallel`` at a time
5. the integrator agent merges items whose warnings have one cause (option agents.integration)
6. the reviewer agent checks every result, one at a time (option agents.review)
7. the build verification (option verify.default)

At most one collector, one integrator and one reviewer work at a time (``RunState.roles``).
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import initial, triage
from .common import CtError, file_lock, read_json, skills_dir, write_json, write_text
from .config import knowledge_path, load_no_grouping
from .grouping import MAX_GROUP
from .models import IntegrationPlan, ReviewResult, TriageResult
from .run import ItemState, Run, finish_item

AGENTS = {"collector": "coverity-triage-collector", "worker": "coverity-triage-worker",
          "integrator": "coverity-triage-integrator", "reviewer": "coverity-triage-reviewer"}
FIX_ROUNDS = 2  # a worker is asked this many times to fix its result file, then the item fails
FETCH_ROUNDS = 2  # the collector fetches the warning paths at most twice (once more for the ones missing)
ROLE_ROUNDS = 2  # the collector, integrator and reviewer are started at most twice for one task
WAIT_SECONDS = 120  # longest wait of one tick while the initial analysis runs
STAGE_JA = {"initial": "初回解析（ビルド・解析・サーバへの登録）", "search": "警告を検索している",
            "paths": "警告経路を取っている", "items": "警告を調べている", "integrate": "同じ原因の警告をまとめている",
            "review": "結果を検証している", "verify": "ビルドで確かめる", "done": "終わった"}


# ---- helpers ------------------------------------------------------------------------------------


def _log(run: Run, event: str, **fields: Any) -> None:
    triage._log(run, event, **fields)


def _prompt(brief: Path, what: str) -> str:
    return f"{what}。指示ファイル {brief} を読み、書かれた手順に従ってください。"


TASK = re.compile(r"^(?P<base>[^@#]+)(?:@(?P<gen>\d+))?(?:#(?P<round>\d+))?$")


def _task(base: str, gen: int | None = None, round_: int = 1) -> str:
    """The task an agent is started with: ``worker:20001@2#3`` is the 3rd start of the 2nd attempt.

    The marks tell a late report of an earlier start apart from the report of the current one."""
    return base + (f"@{gen}" if gen else "") + (f"#{round_}" if round_ > 1 else "")


def _current(gen: int | None, round_: int | None, current_gen: int, current_round: int) -> bool:
    """Whether a report is about the current start. A task without marks (the orchestrating agent left
    them out) is taken as the current start."""
    if gen is None and round_ is None:
        return True
    if gen is not None and gen != current_gen:
        return False
    return (round_ or 1) == current_round


def _worker_task(item: ItemState) -> str:
    return _task(f"worker:{item.id}", item.attempts, item.fix_rounds + 1)


def _role_task(role: str, lease: dict[str, Any]) -> str:
    if role == "collector":
        return _task(f"collector:{lease.get('task')}", lease.get("rounds", 1))
    if role == "integrator":
        return _task("integrator", 1, lease.get("rounds", 1))
    if role == "reviewer":
        return _task(str(lease.get("task")), lease.get("gen", 1), lease.get("rounds", 1))
    return str(lease.get("task") or role)


def _launch(role: str, task: str, prompt: str, item: str | None = None) -> dict[str, Any]:
    return {"type": "launch", "agent": AGENTS[role], "task": task, "item": item, "prompt": prompt,
            "background": True}


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _start_role(run: Run, role: str, task: str, fresh: bool = False, **extra: Any) -> None:
    """Record that the one agent of the role works on ``task``, counting the starts of the same task
    (``fresh``: a new piece of work, counted from 1 again)."""
    with run.update() as state:
        before = state.roles.get(role, {})
        rounds = before.get("rounds", 0) + 1 if before.get("task") == task and not fresh else 1
        gen = before.get("gen", 0) + 1 if fresh or not before else before.get("gen", 1)
        state.roles[role] = {"task": task, "active": True, "started": time.time(), "rounds": rounds, "gen": gen,
                             "fallback": before.get("fallback", False),
                             "fallback_searches": before.get("fallback_searches", 0), **extra}
    _log(run, "launch", role=role, task=task, item=extra.get("item"), round=rounds)


def _end_role(run: Run, role: str, outcome: str, **fields: Any) -> None:
    with run.update() as state:
        lease = state.roles.setdefault(role, {})
        lease["active"] = False
        lease["ended"] = time.time()
    _log(run, "role-end", role=role, task=lease.get("task"), outcome=outcome, **fields)


def _role_busy(run: Run, role: str) -> bool:
    return bool(run.state.roles.get(role, {}).get("active"))


def _rounds(run: Run, role: str, task: str) -> int:
    lease = run.state.roles.get(role, {})
    return lease.get("rounds", 0) if lease.get("task") == task else 0


def _fallback(run: Run) -> bool:
    """The collector agent could not be used: the orchestrating agent calls the MCP tools itself."""
    return bool(run.state.roles.get("collector", {}).get("fallback"))


# ---- tick ---------------------------------------------------------------------------------------


def tick(run_dir: str, finished: str | None = None, failed: str | None = None, reason: str = "",
         wait: int = WAIT_SECONDS) -> dict[str, Any]:
    """What to do next. ``finished`` / ``failed`` is the task of an agent that just ended."""
    run = Run(run_dir)
    say: list[str] = []
    actions: list[dict[str, Any]] = []
    if finished or failed:
        stop = _ended(run, finished or failed or "", failed is not None, reason, say, actions)
        if stop:
            return stop
        run = Run(run_dir)

    # 1. initial analysis
    if run.state.initial and not run.state.planned:
        status = initial.status(run)
        if status["status"] in ("none", "running"):
            if status["status"] == "none" and not (run.dir / "issues.json").is_file():
                initial.start(run)
                say.append("初回解析（ビルド → 解析 → サーバへの登録）を始めました。終わるまで待ちます")
            status = initial.wait(run, wait)
        if status["status"] == "running":
            return _answer(run, "initial", actions, say, wait=True, extra={"initial": status},
                           next_step="初回解析が終わるまで待つ。少ししてから、もう一度 ct.py tick")
        if status["status"] == "failed":
            say += [f"初回解析に失敗しました（{status.get('step_label', '')}）: {status.get('error', '')}",
                    "トリアージは始めずに止めます。設定を直してから /coverity-run で再開してください"]
            return _answer(run, "initial", [], say, extra={"stop": True, "initial": status,
                                                         "error": status.get("error", "初回解析に失敗しました")},
                           next_step="利用者に say と initial.log_tail の要点を伝えて止める（ほかには何もしない）")

    # 2. search the warnings, then make the work items
    if not run.state.planned:
        found = read_json(run.dir / "issues.json", {}) if (run.dir / "issues.json").is_file() else {}
        if not found.get("finished_at"):
            if _role_busy(run, "collector"):
                return _answer(run, "search", actions, say, wait=True)
            if _fallback(run):
                tries = run.state.roles.get("collector", {}).get("fallback_searches", 0)
                if tries >= ROLE_ROUNDS:
                    say.append("警告を検索できませんでした（Coverity に届かない、認証キーの期限が切れた、条件ファイルの"
                               "ストリームが違う など）。トリアージは始めずに止めます")
                    return _answer(run, "search", [], say, extra={"stop": True, "error": "警告を検索できませんでした"},
                                   next_step="利用者に say を伝えて止める（/coverity-help で接続を確かめられる）")
                with run.update() as state:
                    state.roles.setdefault("collector", {})["fallback_searches"] = tries + 1
                return _answer(run, "search", [_mcp_search(run)], say,
                               next_step="取得エージェントが使えないため、MCP の search_issues を自分で done になるまで"
                                         "呼び、そのあと ct.py tick（ツールがエラーを返したら ct.py tick --failed "
                                         "collector:search --reason \"<エラー>\"）")
            actions.append(_collector_search(run))
            return _answer(run, "search", actions, say)
        planned = triage.plan(str(run.dir))
        run = Run(run_dir)
        say.append(f"警告が {planned['issues']} 件見つかりました" + ("（上限の件数で止めました）" if planned["limited"] else "")
                   + f"。作業は {planned['items']} 件（グループ {len(planned['groups'])} 件）です")
        say += planned["notes"]
        if not planned["items"]:
            return _answer(run, "done", [], say + [planned["next"]], done=True)

    # 3. warning paths of every item
    missing = _missing_paths(run)
    if missing or (run.state.fetch_rounds >= FETCH_ROUNDS and not run.state.paths_checked):
        if _role_busy(run, "collector"):
            return _answer(run, "paths", actions, say, wait=True)
        if missing and run.state.fetch_rounds < FETCH_ROUNDS:
            if _fallback(run):
                with run.update() as state:
                    state.fetch_rounds += 1
                return _answer(run, "paths", [_mcp_paths(run, item_id, cids) for item_id, cids in missing.items()], say,
                               next_step="取得エージェントが使えないため、actions の MCP の get_issues を自分で順に "
                                         "done になるまで呼び、そのあと ct.py tick")
            actions.append(_collector_paths(run, missing))
            return _answer(run, "paths", actions, say)
        # Fetched as often as allowed: the brief says the path could not be fetched, and the work goes on.
        # The first time, the person is told of every path missing, also of those the MCP server could not
        # fetch (it saves a file with the error, so they are not missing files).
        first = not run.state.paths_checked
        _mark_unfetched(run, missing, log=False)
        unfetched = _unfetched(run) if first else missing
        if first:
            with run.update() as state:
                state.paths_checked = True
        if unfetched:
            _log(run, "paths-missing", level="warning", items=sorted(unfetched))
            say.append(f"警告経路を取れなかった警告が {sum(len(c) for c in unfetched.values())} 件あります"
                       "（経路なしで調べます）")

    # 4. the worker agents
    actions += _fill_slots(run, say, keep=bool(actions))
    run = Run(run_dir)
    counts = run.state.counts()
    if counts["pending"] or counts["in_progress"]:
        return _answer(run, "items", actions, say, wait=not actions)

    # 5. integration
    if run.state.integration and not run.state.integrated:
        if _role_busy(run, "integrator"):
            return _answer(run, "integrate", actions, say, wait=True)
        launch = _integrator(run, say)
        if launch:
            return _answer(run, "integrate", [launch], say)
        run = Run(run_dir)

    # 6. review, one item at a time
    if run.state.review:
        if _role_busy(run, "reviewer"):
            return _answer(run, "review", actions, say, wait=True)
        target = next((i for i in run.state.items if i.status == "done" and i.review is None), None)
        if target is not None:
            return _answer(run, "review", [_reviewer(run, target)], say)

    # 7. build verification
    if run.state.verify_mode != "none" and not run.state.verified:
        if run.state.roles.get("verify", {}).get("active"):
            return _answer(run, "verify", [], say, wait=True,
                           next_step="ct.py verify が終わるのを待ち、ct.py tick --finished verify（ok: false で終わった・"
                                     "止まったときは --failed verify --reason ...）")
        _start_role(run, "verify", "verify", fresh=True)
        actions.append({"type": "command", "task": "verify", "command": f'ct.py verify --run "{run.dir}"',
                        "note": "修正案をまとめてビルドで確かめます（時間がかかります）。終わったら ct.py tick --finished verify"
                                "（ok: false で終わったら --failed verify --reason \"<error>\"）"})
        return _answer(run, "verify", actions, say)

    return _answer(run, "done", [], say, done=True)


def _answer(run: Run, stage: str, actions: list[dict[str, Any]], say: list[str], *, wait: bool = False,
            done: bool = False, extra: dict[str, Any] | None = None, next_step: str = "") -> dict[str, Any]:
    state = run.state
    running = [i.id for i in state.items if i.status == "in_progress"]
    roles = {role: lease.get("task") for role, lease in state.roles.items() if lease.get("active")}
    tasks = [_worker_task(i) for i in state.items if i.status == "in_progress"] + \
        [_role_task(role, lease) for role, lease in state.roles.items() if lease.get("active")]
    if done:
        next_step = (f"ct.py summary --run {run.dir} で一覧を作り、ct.py gui --repo {run.repo} --run {run.dir} "
                     "--view summary で画面の「結果」を開いて、利用者に伝える")
    elif not next_step:
        next_step = ("actions を順に行う（launch はそのエージェントを裏で動かす）。どれかが終わったら "
                     "ct.py tick --finished <task>（途中で止まった・起動できなかったら --failed <task> --reason ...）"
                     if actions else "動いているエージェントが終わるのを待ち、終わったら ct.py tick --finished <task>"
                     "（task は running_tasks のもの）")
    out = {"ok": True, "stage": stage, "stage_label": STAGE_JA[stage], "actions": actions, "wait": wait and not actions,
           "done": done, "running_items": running, "running_roles": roles, "running_tasks": tasks,
           "counts": state.counts(), "say": say,
           "next": next_step}
    if extra:
        out.update(extra)
    return out


# ---- an agent ended -----------------------------------------------------------------------------


def _ended(run: Run, task: str, failed: bool, reason: str, say: list[str],
           actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    match = TASK.match(task.strip())
    base = match.group("base") if match else task
    gen = int(match.group("gen")) if match and match.group("gen") else None
    round_ = int(match.group("round")) if match and match.group("round") else None
    role, _, rest = base.partition(":")
    if role == "worker":
        _worker_ended(run, rest, gen, round_, failed, reason, say, actions)
    elif role == "collector":
        _collector_ended(run, rest, gen, round_, failed, reason, say)
    elif role == "integrator":
        _integrator_ended(run, gen, round_, failed, reason, say, actions)
    elif role == "reviewer":
        _reviewer_ended(run, rest, gen, round_, failed, reason, say, actions)
    elif role == "verify":
        _verify_ended(run, failed, reason, say)
    else:
        raise CtError(f"task が分かりません: {task}（worker:<ID>、collector:search、collector:paths、integrator、"
                      "reviewer:<ID>、verify のどれか。actions の task をそのまま渡す）")
    return None


def _fail_item(run: Run, item_id: str, reason: str, say: list[str]) -> None:
    """The item could not be worked on. A merged item gives its members back as they were."""
    triage.fail(str(run.dir), item_id, reason)
    if not run.state.item(item_id).merged_from:
        say.append(f"{item_id}: 処理できませんでした（{reason}）")
        return
    _undo_merge(run, item_id, reason, say)


def _undo_merge(run: Run, item_id: str, reason: str, say: list[str]) -> None:
    item = run.state.item(item_id)
    with run.update() as state:
        for member in item.merged_from:
            current = state.item(member)
            if current.status == "merged" and current.merged_into == item_id:
                current.status, current.merged_into = "done", None
        state.items = [i for i in state.items if i.id != item_id]
        state.notes.append(f"統合した作業 {item_id} を処理できなかったため、元の作業 {', '.join(item.merged_from)} に戻しました"
                           f"（{reason}）")
    _log(run, "integrate-undo", level="warning", item=item_id, members=item.merged_from, reason=reason)
    say.append(f"{item_id}: 処理できなかったため、統合をやめて元の作業 {', '.join(item.merged_from)} に戻しました")


def _worker_ended(run: Run, item_id: str, gen: int | None, round_: int | None, failed: bool, reason: str,
                  say: list[str], actions: list[dict[str, Any]]) -> None:
    try:
        item = run.state.item(item_id)
    except CtError:
        return  # a merged item that was given back: a late report
    if item.status != "in_progress" or not _current(gen, round_, item.attempts, item.fix_rounds + 1):
        return  # told twice, or about a start that was followed by another
    if failed:
        _fail_item(run, item_id, reason or "作業エージェントが途中で止まりました", say)
        return
    try:
        done = triage.finish(str(run.dir), item_id)
    except CtError as exc:
        problems = exc.details.get("problems")
        if not problems:  # not the worker's to fix (saving the fix failed, for example)
            _fail_item(run, item_id, str(exc), say)
            return
        _log(run, "error", level="warning", command="finish", item=item_id, problems=problems)  # for the monitor
        if item.fix_rounds >= FIX_ROUNDS:
            _fail_item(run, item_id, f"結果ファイルを {FIX_ROUNDS} 回直しても通りませんでした: {'；'.join(problems)}", say)
            return
        with run.update() as state:
            state.item(item_id).fix_rounds += 1
        brief = run.item_dir(item_id) / "brief.md"
        actions.append(_launch("worker", _worker_task(run.state.item(item_id)),
                               _prompt(brief, "Coverity の警告を調べます") + " 結果ファイルの次の点を直してください：\n"
                               + "\n".join(f"- {p}" for p in problems), item_id))
        return
    if done.get("split_into"):
        say.append(f"{item_id}: 原因の違う CID {', '.join(done['split_into'])} を個別の作業に戻しました")
    for warning in done.get("warnings") or []:
        say.append(f"{item_id}: {warning}")


def _collector_ended(run: Run, task: str, gen: int | None, round_: int | None, failed: bool, reason: str,
                     say: list[str]) -> None:
    lease = run.state.roles.get("collector", {})
    if failed and task == "search" and lease.get("fallback") and not lease.get("active"):
        with run.update() as state:  # the orchestrating agent's own search failed: the next tick stops
            state.roles["collector"]["fallback_searches"] = ROLE_ROUNDS
        say.append(f"警告の検索がエラーになりました（{reason or '理由不明'}）")
        return None
    if not lease.get("active") or lease.get("task") != task or not _current(gen, round_, lease.get("rounds", 1), 1):
        return None  # told twice, or about a start that was followed by another
    _end_role(run, "collector", "failed" if failed else "done", reason=reason)
    if failed:  # the agent could not be started or stopped: the orchestrating agent calls the tools itself
        with run.update() as state:
            state.roles["collector"]["fallback"] = True
        say.append(f"取得エージェントを使えませんでした（{reason or '理由不明'}）。メインの AI が代わりに取ります")
    if task != "search":
        return None  # paths: the next steps fetch the missing ones once more, or go on without them
    found = read_json(run.dir / "issues.json", {}) if (run.dir / "issues.json").is_file() else {}
    if found.get("finished_at") or (not failed and _rounds(run, "collector", "search") < ROLE_ROUNDS):
        return None  # done, or started once more by the next steps of this tick
    with run.update() as state:
        state.roles["collector"]["fallback"] = True
    return None


def _verify_ended(run: Run, failed: bool, reason: str, say: list[str]) -> None:
    if not run.state.roles.get("verify", {}).get("active"):
        return  # told twice
    if failed:  # ct.py verify stopped or said ok: false: the run goes on without it
        with run.update() as state:
            state.verified = True
            state.roles.pop("verify", None)
            state.notes.append(f"ビルドでの検証はできませんでした（{reason or '理由不明'}）")
        _log(run, "verify-failed", level="warning", reason=reason)
        say.append(f"ビルドでの検証はできませんでした（{reason or '理由不明'}）。確かめずに一覧を作ります")
    elif not run.state.verified:
        with run.update() as state:
            state.roles.pop("verify", None)


def _mcp_search(run: Run) -> dict[str, Any]:
    return {"type": "mcp", "tool": "search_issues", "until_done": True,
            "args": {"repo_root": str(run.repo), "filter_file": str(run.dir / "filter.json"),
                     "output_file": str(run.dir / "issues.json")}}


def _mcp_paths(run: Run, item_id: str, cids: list[int]) -> dict[str, Any]:
    folder = run.item_dir(item_id)
    folder.mkdir(parents=True, exist_ok=True)
    return {"type": "mcp", "tool": "get_issues", "until_done": True,
            "args": {"repo_root": str(run.repo), "stream": run.state.stream, "cids": cids, "output_dir": str(folder)}}


# ---- collector ----------------------------------------------------------------------------------


def _collector_search(run: Run) -> dict[str, Any]:
    brief = run.dir / "agents" / "collector" / "search.md"
    args = _mcp_search(run)["args"]
    write_text(brief, "\n".join([
        "# 取得の指示: 警告の検索", "",
        "Coverity Connect で条件に合う警告を検索し、結果をファイルに保存します。判断や要約はしません。", "",
        "## すること", "",
        "MCP のツール `search_issues` を、次の引数で呼びます。結果の `done` が false の間は、**同じ引数で**呼び直します"
        "（1 回のツールは 20 秒で区切られます。保存済みの分は繰り返しません）。", "",
        "| 引数 | 値 |", "|---|---|",
        *[f"| `{k}` | `{v}` |" for k, v in args.items()], "",
        "`done` が true になったら、呼び出し元に次の 1 行だけを返します。", "",
        "`検索: found=<found> limited=<limited> excluded=<excluded>`", "",
        "ツールがエラーを返したら、そのエラーの文を 1 行で返します（何度も呼び直さない）。", ""]))
    _start_role(run, "collector", "search")
    return _launch("collector", _role_task("collector", run.state.roles["collector"]),
                   _prompt(brief, "Coverity の警告を検索します"))


def _missing_paths(run: Run) -> dict[str, list[int]]:
    """CIDs whose warning path is not there yet, by item (items still to be worked on). Before the last
    round, a file that only says the path could not be fetched is removed, so it is fetched once more."""
    retry = 0 < run.state.fetch_rounds < FETCH_ROUNDS and not _role_busy(run, "collector")
    missing = {}
    for item in run.state.items:
        if item.status != "pending":
            continue
        folder = run.item_dir(item.id)
        cids = []
        for cid in item.cids:
            path = folder / f"issue-{cid}.json"
            if retry and path.is_file():
                data = read_json(path, {})
                if data.get("error") and not data.get("events"):
                    path.unlink()
            if not path.is_file():
                cids.append(cid)
        if cids:
            missing[item.id] = cids
    return missing


def _collector_paths(run: Run, missing: dict[str, list[int]]) -> dict[str, Any]:
    with run.update() as state:
        state.fetch_rounds += 1
    brief = run.dir / "agents" / "collector" / f"paths-{run.state.fetch_rounds}.md"
    rows = []
    for item_id, cids in missing.items():
        folder = run.item_dir(item_id)
        folder.mkdir(parents=True, exist_ok=True)
        rows.append(f"| {item_id} | `{', '.join(map(str, cids))}` | `{folder}` |")
    write_text(brief, "\n".join([
        "# 取得の指示: 警告経路", "",
        "作業ごとに、警告経路を Coverity Connect から取り、ファイルに保存します。判断や要約はしません。", "",
        "## すること", "",
        f"下の表の行ごとに、MCP のツール `get_issues` を `repo_root` = `{run.repo}`、`stream` = `{run.state.stream}`、"
        "`cids` = その行の CID（数の配列）、`output_dir` = その行のフォルダで呼びます。結果の `done` が false の間は、"
        "**同じ引数で**呼び直します（保存済みの分は繰り返しません）。", "",
        "| 作業 | cids | output_dir |", "|---|---|---|", *rows, "",
        "全部の行が終わったら、呼び出し元に次の 1 行だけを返します。", "",
        "`警告経路: 保存 <保存した CID の数> / 取れなかった <取れなかった CID の数>`", "",
        "ツールがエラーを返した行は飛ばして、次の行へ進みます。", ""]))
    _start_role(run, "collector", "paths")
    return _launch("collector", _role_task("collector", run.state.roles["collector"]),
                   _prompt(brief, "Coverity の警告経路を取ります"))


def _mark_unfetched(run: Run, missing: dict[str, list[int]], log: bool = True) -> None:
    for item_id, cids in missing.items():
        for cid in cids:
            path = run.item_dir(item_id) / f"issue-{cid}.json"
            if not path.is_file():
                write_json(path, {"cid": cid, "error": "警告経路を Coverity から取れませんでした（取得を 2 回試しました）"})
    if log and missing:
        _log(run, "paths-missing", level="warning", items=sorted(missing))


def _no_path(path: Path) -> bool:
    """The file of the CID only says that its warning path could not be fetched."""
    try:
        data = read_json(path, {})
    except CtError:  # a damaged file is another problem: the brief of the item says so
        return False
    return isinstance(data, dict) and bool(data.get("error") and not data.get("events"))


def _unfetched(run: Run) -> dict[str, list[int]]:
    """CIDs whose warning path could not be fetched, by item (items still to be worked on)."""
    found = {}
    for item in run.state.items:
        if item.status != "pending":
            continue
        cids = [cid for cid in item.cids if _no_path(run.item_dir(item.id) / f"issue-{cid}.json")]
        if cids:
            found[item.id] = cids
    return found


# ---- workers ------------------------------------------------------------------------------------


def _copy_missing(run: Run) -> bool:
    """The slot the next item gets has no work copy yet (a copy of a large repository takes minutes)."""
    busy = {i.slot for i in run.state.items if i.status == "in_progress"}
    slot = next((s for s in range(1, run.config.parallel + 1) if s not in busy), None)
    if slot is None or not any(i.status == "pending" for i in run.state.items):
        return False
    fix_dir = run.copy_dir("fix", slot)
    return not ((fix_dir / ".git").exists() or (fix_dir / ".svn").exists())


def _fill_slots(run: Run, say: list[str], keep: bool = False) -> list[dict[str, Any]]:
    """Start as many items as there are free slots.

    A slot whose work copy has yet to be made waits for the next tick when there is something to
    give out already, so that no worker waits for another's copy. When a start fails, the starts
    made before it (and, with ``keep``, what the tick has to give out already) are given out; the
    next tick meets the error again if it is not of that item only."""
    actions: list[dict[str, Any]] = []
    while True:
        if (actions or keep) and _copy_missing(Run(run.dir)):
            break  # making that copy can take long: give out the starts first, the next tick makes it
        item_id = None
        try:
            got = triage.next_item(str(run.dir))
            if not got.get("item"):
                break
            item_id = got["item"]
            if got.get("missing_issue_files"):  # an item made by the split or the integration copies them
                _mark_unfetched(run, {item_id: got["missing_issue_files"]})
            brief = triage.brief(str(run.dir), item_id)
        except CtError as exc:
            current = Run(run.dir)
            for item in current.state.items:  # a merged item that could not be started gives its members back
                if item.status == "error" and item.merged_from:
                    _undo_merge(current, item.id, str(exc), say)
            if item_id and current.state.items and any(i.id == item_id and i.status == "in_progress"
                                                       for i in current.state.items):
                triage.fail(str(run.dir), item_id, str(exc))
            if not actions and not keep:
                raise
            say.append(str(exc))
            break
        actions.append(_launch("worker", _worker_task(Run(run.dir).state.item(item_id)), brief["prompt"], item_id))
        if not got.get("can_start_more"):
            break
    return actions


# ---- integration --------------------------------------------------------------------------------


HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? ", re.MULTILINE)


def _changed_ranges(patch: Path) -> dict[str, list[tuple[int, int]]]:
    """Lines of the original file each patch changes, by file (git and svn unified diffs)."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    if not patch.is_file():
        return ranges
    current = None
    for line in patch.read_bytes().splitlines():
        if line.startswith(b"+++ "):
            name = line[4:].split(b"\t")[0].decode("utf-8", "replace").strip()
            current = name[2:] if name.startswith(("a/", "b/")) else name
        elif line.startswith(b"@@ ") and current:
            match = HUNK.match(line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or 1)
                ranges.setdefault(current, []).append((start, start + max(count, 1) - 1))
    return ranges


def _overlap(a: dict[str, list[tuple[int, int]]], b: dict[str, list[tuple[int, int]]]) -> bool:
    return any(x0 <= y1 and y0 <= x1 for f in a.keys() & b.keys() for x0, x1 in a[f] for y0, y1 in b[f])


def candidates(run: Run) -> list[dict[str, Any]]:
    """Groups of finished items that may have one cause, found by fixed rules (the integrator decides)."""
    no_grouping = load_no_grouping(run.repo)
    done = [i for i in run.state.items if i.status == "done" and not set(i.cids) & no_grouping]
    results = {i.id: TriageResult.model_validate(read_json(run.result_file(i.id))) for i in done}
    ranges = {i.id: _changed_ranges(run.dir / "patches" / f"{i.id}-fix.patch") for i in done}
    reasons: dict[tuple[str, str], list[str]] = {}

    def link(a: str, b: str, why: str) -> None:
        key = (a, b) if a < b else (b, a)
        if why not in reasons.setdefault(key, []):
            reasons[key].append(why)

    for n, a in enumerate(done):
        for b in done[n + 1:]:
            if a.cluster and a.cluster == b.cluster:
                link(a.id, b.id, "機械的なグループ化で同じまとまりだった（10 件ずつに分けた）")
            if a.split_from and (a.split_from == b.split_from or a.split_from == b.id) or \
                    (b.split_from and b.split_from == a.id):
                link(a.id, b.id, "グループから外された CID と、元のグループ（またはその仲間）")
            if _overlap(ranges[a.id], ranges[b.id]):
                link(a.id, b.id, "修正が同じファイルの同じ箇所を変えている")
            ev_a = {(e.file, e.line) for e in results[a.id].verdict.evidence if e.line}
            ev_b = {(e.file, e.line) for e in results[b.id].verdict.evidence if e.line}
            if ev_a & ev_b:
                link(a.id, b.id, "根拠の場所（ファイルと行）が同じ")
    # connected groups of linked items
    parent = {i.id: i.id for i in done}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in reasons:
        parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for item in done:
        if any(item.id in pair for pair in reasons):
            groups.setdefault(find(item.id), []).append(item.id)
    return [{"items": members, "reasons": {f"{a}-{b}": why for (a, b), why in reasons.items()
                                           if a in members and b in members}}
            for members in groups.values()]


def _integrator(run: Run, say: list[str]) -> dict[str, Any] | None:
    found = candidates(run)
    folder = run.dir / "integration"
    write_json(folder / "candidates.json", found)
    if not found:
        with run.update() as state:
            state.integrated = True
        _log(run, "integrate", candidates=0, merges=[])
        say.append("同じ原因でまとめられる候補はありませんでした（統合はしません）")
        return None
    plan_file = folder / "plan.json"
    plan_file.unlink(missing_ok=True)
    no_grouping = load_no_grouping(run.repo)
    rows, paths = [], []
    for item in run.state.items:
        if item.status != "done" or set(item.cids) & no_grouping:  # not to be merged (no-grouping.yaml)
            continue
        result = TriageResult.model_validate(read_json(run.result_file(item.id)))
        issues, events, _, _ = triage._item_view(run, item)
        main = issues[0]
        where = f"{main.file}:{main.line or '-'} {main.function or ''}".strip()
        patch = run.dir / "patches" / f"{item.id}-fix.patch"
        rows.append(f"| {item.id} | {', '.join(map(str, item.cids))} | {main.checker} | `{where}` | "
                    f"{result.verdict.judgement} | {result.recommendation} | {result.confidence} | "
                    f"{result.verdict.summary} | `{run.result_file(item.id)}` | " + (f"`{patch}`" if patch.is_file() else "なし")
                    + " |")
        for issue in issues:
            stars = [e for e in events.get(issue.cid) or [] if e.main] or (events.get(issue.cid) or [])[-1:]
            paths.append(f"- {item.id} / CID {issue.cid}（{issue.checker}）: "
                         + ("、".join(f"`{e.file}`:{e.line or '-'} {e.tag} — {e.description}" for e in stars)
                            or f"`{issue.file}`:{issue.line or '-'}（警告経路を取れませんでした）"))
    lines = ["# 統合の指示", "",
             "作業エージェントが調べ終わった作業のうち、**同じ原因**のものを見つけ、1 つの作業にまとめる計画を書きます。"
             "コードは変更しません（まとめた修正は、スクリプトか作業エージェントが作ります）。",
             "最初に手順書 `triage-integration` を読み、その手順と計画の形に従います。"
             "手順書は、下の表の「手順書のフォルダ（読む）」の下の `<名前>/SKILL.md` と、そのフォルダの `references/` です。", "",
             "## フォルダとファイル", "", "| 用途 | 場所 |", "|---|---|",
             f"| 計画（書く） | `{plan_file}` |",
             f"| 進み具合（書く。節目ごとに上書き） | `{run.dir / 'agents' / 'integrator' / 'progress.json'}` |",
             f"| 調べるコード（読むだけ） | `{run.copy_dir('analyzed') if run.state.analyzed_revision else run.repo}` |",
             f"| 手順書のフォルダ（読む） | `{skills_dir()}` |"]
    if knowledge_path(run.repo).is_file():
        lines.append(f"| プロジェクトの知識（読む） | `{knowledge_path(run.repo)}` |")
    lines += ["", "- パスは、どれもリポジトリの一番上からの相対パスです。", "",
              "## 候補（決まった方法で見つけたもの）", ""]
    for n, group in enumerate(found, 1):
        lines.append(f"{n}. 作業 {', '.join(group['items'])}")
        lines += [f"   - {pair}: {'、'.join(why)}" for pair, why in group["reasons"].items()]
    lines += ["", "候補に無い組でも、結果と差分を読んで同じ原因だと言えるなら、根拠を書いてまとめてかまいません。", "",
              "## 調べ終わった作業", "",
              "| 作業 | CID | チェッカー | 場所 | 見立て | 推奨 | 確信度 | 1 行の見立て | 結果ファイル | 修正の差分 |",
              "|---|---|---|---|---|---|---|---|---|---|", *rows, "",
              "## 警告経路の ★（問題の起きる場所）", "", *paths, "",
              "## 決まり", "",
              f"- 1 つにまとめる CID は合わせて {MAX_GROUP} 件まで。まとめる作業は 2 つ以上。1 つの作業は 1 つのまとまりにだけ入れる。",
              "- まとめてよいのは、上の「調べ終わった作業」の表にある作業だけ（グループ化しない CID を含む作業は載せていない）。",
              "- 原因が違うのに同じ箇所を変えている作業は、まとめずに conflicts に書く。",
              "- まとめるものが無ければ、merges を空にした計画を書く。", "",
              "計画を書いたら、呼び出し元に 1 行だけ返します。例: 「統合: 1 つにまとめる（20011, 20012）」", ""]
    brief = run.dir / "agents" / "integrator" / "brief.md"
    write_text(brief, "\n".join(lines))
    (run.dir / "agents" / "integrator" / "progress.json").unlink(missing_ok=True)
    _start_role(run, "integrator", "integrator", fresh=True, repo_fingerprint=run.vcs.fingerprint())
    return _launch("integrator", _role_task("integrator", run.state.roles["integrator"]),
                   _prompt(brief, "同じ原因の警告をまとめます"))


def _integrator_ended(run: Run, gen: int | None, round_: int | None, failed: bool, reason: str, say: list[str],
                      actions: list[dict[str, Any]]) -> None:
    lease = run.state.roles.get("integrator", {})
    rounds = lease.get("rounds", 1)
    if not lease.get("active") or not _current(gen, round_, 1, rounds):
        return  # told twice, or about a start that was followed by another
    if lease.get("repo_fingerprint") and run.vcs.fingerprint() != lease["repo_fingerprint"]:
        say.append("統合の間に、利用者のリポジトリのファイルが変わりました。統合エージェントが誤って変更していないか確かめてください")
    problems: list[str] = []
    if not failed:
        try:
            merged = integrate(run)
        except CtError as exc:
            problems = exc.details.get("problems") or [str(exc)]
        else:
            _end_role(run, "integrator", "done", merges=merged)
            say.append(f"同じ原因の作業を {len(merged)} つにまとめました" if merged else "まとめる作業はありませんでした")
            return
    if not failed and rounds < ROLE_ROUNDS:
        brief = run.dir / "agents" / "integrator" / "brief.md"
        _start_role(run, "integrator", "integrator", repo_fingerprint=lease.get("repo_fingerprint"))
        actions.append(_launch("integrator", _role_task("integrator", run.state.roles["integrator"]),
                               _prompt(brief, "同じ原因の警告をまとめます")
                               + " 計画の次の点を直してください：\n" + "\n".join(f"- {p}" for p in problems)))
        return
    _end_role(run, "integrator", "failed", reason=reason or "；".join(problems))
    with run.update() as state:
        state.integrated = True
        state.notes.append(f"統合はできませんでした（{reason or '；'.join(problems)}）。作業はまとめずに進めました")
    say.append("統合はできませんでした。まとめずに進めます")


def integrate(run: Run) -> list[str]:
    """Check integration/plan.json and make one item of each merge. Returns the new item ids.

    Nothing is changed unless the whole plan is right. The new items and the merged members are
    recorded in one update; the fixes are made after it (a fix that cannot be made by joining the
    members' patches is left to a worker)."""
    path = run.dir / "integration" / "plan.json"
    if not path.is_file():
        raise CtError("計画がありません", problems=[f"{path} を書いてください（形はスキル triage-integration）"])
    try:
        plan = IntegrationPlan.model_validate(read_json(path))
    except (CtError, ValidationError) as exc:
        problems = ([f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
                    if isinstance(exc, ValidationError) else [str(exc)])
        raise CtError("計画に直すところがあります", problems=problems) from exc
    no_grouping = load_no_grouping(run.repo)
    items = {i.id: i for i in run.state.items}
    problems, used = [], set()
    for n, merge in enumerate(plan.merges, 1):
        if len(set(merge.members)) < 2:
            problems.append(f"merges[{n}]: まとめる作業は 2 つ以上にします")
        for member in merge.members:
            item = items.get(member)
            if item is None or item.status != "done":
                problems.append(f"merges[{n}]: {member} は調べ終わった作業ではありません")
            elif set(item.cids) & no_grouping:
                problems.append(f"merges[{n}]: {member} はグループ化しない CID を含みます")
            if member in used:
                problems.append(f"merges[{n}]: {member} が 2 つのまとまりに入っています")
            used.add(member)
        if sum(len(items[m].cids) for m in merge.members if m in items) > MAX_GROUP:
            problems.append(f"merges[{n}]: まとめる CID は合わせて {MAX_GROUP} 件までにします")
        comment = merge.result.deviation.comment.strip()
        if len(comment) > triage.DEVIATION_COMMENT_MAX:
            problems.append(f"merges[{n}]: result.deviation.comment は {triage.DEVIATION_COMMENT_MAX} 字以内にしてください"
                            f"（今は {len(comment)} 字）。問題にならない理由と根拠の場所だけを書きます（スキル deviation-comment）")
        source = merge.fix_source
        if source not in ("combine", "rework") and not (source.startswith("item:") and source[5:] in merge.members):
            problems.append(f"merges[{n}]: fix_source は combine、rework、item:<まとめる作業の ID> のどれかにします")
    if problems:
        raise CtError("計画に直すところがあります", problems=problems)

    order = {"low": 0, "medium": 1, "high": 2}
    merged_file = run.dir / "integration" / "merged.json"
    merged_info = read_json(merged_file, {})
    made: list[tuple[str, Any, Any]] = []
    numbers = [int(i.id[1:]) for i in run.state.items if re.fullmatch(r"G\d+", i.id)]
    for n, merge in enumerate(plan.merges, 1):
        new_id = f"G{max(numbers, default=0) + n}"
        folder = run.item_dir(new_id)
        folder.mkdir(parents=True, exist_ok=True)
        for member in merge.members:
            for cid in items[member].cids:
                src = run.item_dir(member) / f"issue-{cid}.json"
                if src.is_file():
                    (folder / src.name).write_bytes(src.read_bytes())
        lowest = min((TriageResult.model_validate(read_json(run.result_file(m))).confidence for m in merge.members),
                     key=order.__getitem__)
        result = merge.result.model_copy(update={"confidence": min((merge.result.confidence, lowest),
                                                                   key=order.__getitem__)})
        merged_info[new_id] = {"members": merge.members, "reason": merge.reason, "fix_source": merge.fix_source,
                               "result": result.model_dump(exclude={"item", "group_excluded_cids"})}
        made.append((new_id, merge, result))
    write_json(merged_file, merged_info)
    with run.update() as state:
        for new_id, merge, _ in made:
            state.items.append(ItemState(id=new_id, cids=[c for m in merge.members for c in items[m].cids],
                                         merged_from=list(merge.members)))
            for member in merge.members:
                current = state.item(member)
                current.status, current.merged_into = "merged", new_id
        for conflict in plan.conflicts:
            for item_id in conflict.items:
                if any(i.id == item_id for i in state.items):
                    state.item(item_id).warnings.append(
                        f"修正が作業 {', '.join(x for x in conflict.items if x != item_id)} と同じ箇所を変えています"
                        f"（原因は別）: {conflict.note}")
        state.integrated = True
    _log(run, "integrate", merges=[m[0] for m in made], conflicts=[c.items for c in plan.conflicts])
    for new_id, merge, result in made:
        if merge.fix_source == "rework" or run.config.options.annotation:
            continue  # a worker writes the fix (and the annotation, which must say the merged comment)
        try:
            _build_merged(run, new_id, merge.members, merge.fix_source, result)  # False: stays pending for a worker
        except CtError as exc:  # making the copy or saving failed: a worker makes the fix instead
            _log(run, "integrate-build", level="warning", item=new_id, error=str(exc))
    for conflict in plan.conflicts:
        for item_id in conflict.items:
            if any(i.id == item_id and i.status == "done" for i in run.state.items):
                triage.write_item_files(run, item_id)
    return [m[0] for m in made]


def _build_merged(run: Run, item_id: str, members: list[str], source: str, result: Any) -> bool:
    """Make the fix of a merged item from the members' patches. False: a worker has to make it."""
    patches = [run.dir / "patches" / f"{m}-fix.patch" for m in ([source[5:]] if source.startswith("item:") else members)]
    patches = [p for p in patches if p.is_file()]
    vcs, config = run.vcs, run.config
    fix_dir = run.copy_dir("fix", 1)
    latest = run.state.latest_revision or ""
    with file_lock(run.dir / "work" / "vcs", timeout=triage.VCS_LOCK_SECONDS):
        vcs.reset_copy(fix_dir, latest)
        for patch in patches:
            if vcs.apply_to_copy(fix_dir, patch):
                vcs.reset_copy(fix_dir, latest)
                return False
    changed = vcs.changed_files(fix_dir)
    if not changed and not result.fix.already_fixed_on_latest:
        return False
    data = {**result.model_dump(), "item": item_id, "group_excluded_cids": []}
    write_json(run.result_file(item_id), data)
    saved: dict[str, Any] = {}
    if changed:
        item = run.state.item(item_id)
        message = f"Coverity CID {', '.join(map(str, item.cids))}: 修正（統合）\n\n{result.fix.summary}\n"
        branch = None if config.options.per_run_branch else f"{config.vcs.branch_prefix}{run.state.run_id}-{item_id}"
        with file_lock(run.dir / "work" / "vcs", timeout=triage.VCS_LOCK_SECONDS):
            fix = vcs.save(fix_dir, latest, message, branch, f"refs/coverity-triage/{run.state.run_id}/{item_id}-fix",
                           run.dir / "patches" / f"{item_id}-fix.patch")
        triage._copy_changed(fix_dir, changed, run.dir / "fixed" / item_id / "fix")
        saved["fix"] = fix.__dict__
    write_json(run.item_dir(item_id) / "saved.json", saved)
    with run.update() as state:
        current = state.item(item_id)
        current.started = time.time()
        finish_item(current, "done")
        current.seconds = 0.0
    triage.write_item_files(run, item_id)
    return True


# ---- review -------------------------------------------------------------------------------------


def _lint(run: Run, item: ItemState, result: TriageResult) -> list[str]:
    """Things the scripts notice by fixed rules; the reviewer weighs them."""
    root = run.copy_dir("analyzed") if run.state.analyzed_revision else run.repo
    found = []
    for ev in result.verdict.evidence:
        path = root / ev.file
        if not path.is_file():
            found.append(f"根拠のファイルが調べるコードにありません: {ev.file}")
        elif ev.line:
            lines = path.read_bytes().count(b"\n") + 1
            if ev.line > lines:
                found.append(f"根拠の行が範囲の外です: {ev.file}:{ev.line}（{lines} 行まで）")
    judgement, rec, conf = result.verdict.judgement, result.recommendation, result.confidence
    if judgement == "undetermined" and conf == "high":
        found.append("判定が「分からない」なのに、確信度が「高」です")
    if result.revision_drift.status == "detected" and conf == "high":
        found.append("リビジョンのずれがあるのに、確信度が「高」です")
    if judgement == "true_bug" and rec == "deviation":
        found.append("本物のバグなのに、逸脱を推奨しています（プロジェクトの方針によることがあります）")
    if judgement in ("false_positive", "intentional") and rec == "fix":
        found.append("誤検知（または意図どおり）なのに、修正を推奨しています")
    if not result.verdict.evidence:
        found.append("根拠の場所（evidence）がありません")
    return found


def _reviewer(run: Run, item: ItemState) -> dict[str, Any]:
    result = TriageResult.model_validate(read_json(run.result_file(item.id)))
    issues, events, description, notes = triage._item_view(run, item)
    saved = read_json(run.item_dir(item.id) / "saved.json", {})
    patch = (saved.get("fix") or {}).get("patch")
    out = run.item_dir(item.id) / "review.json"
    out.unlink(missing_ok=True)
    progress = run.dir / "agents" / "reviewer" / "progress.json"
    progress.unlink(missing_ok=True)
    investigate = run.copy_dir("analyzed") if run.state.analyzed_revision else run.repo
    lines = [f"# 検証の指示: {item.id}", "",
             "別のエージェントが調べた結果を、**独立に**確かめます。結果を作った者の考えた過程は渡しません。"
             "警告経路とコードを自分で読み、下の「確かめる結果」が正しいかを判断します。",
             "最初に手順書 `triage-review` を読み、その手順と結果ファイルの形に従います。"
             "チェッカー別の観点は手順書 `checker-knowledge` にあります。"
             "手順書は、下の表の「手順書のフォルダ（読む）」の下の `<名前>/SKILL.md` と、そのフォルダの `references/` です。", "",
             "## フォルダとファイル", "", "| 用途 | 場所 |", "|---|---|",
             f"| 調べるコード（読むだけ） | `{investigate}` |",
             f"| 検証の結果（書く） | `{out}` |", f"| 進み具合（書く。節目ごとに上書き） | `{progress}` |",
             f"| 手順書のフォルダ（読む） | `{skills_dir()}` |"]
    if patch:
        lines.append(f"| 修正の差分（読むだけ） | `{patch}` |")
        lines.append(f"| 修正後のファイル（読むだけ） | `{run.dir / 'fixed' / item.id / 'fix'}` |")
    if knowledge_path(run.repo).is_file():
        lines.append(f"| プロジェクトの知識（読む） | `{knowledge_path(run.repo)}` |")
    lines += ["", "- 上の表で「書く」とあるファイルのほかは、何も変更しません。", "", "## 警告", ""]
    for issue in issues:
        lines += [f"### CID {issue.cid} — {issue.checker}（Impact {issue.impact or '-'}）", "",
                  f"- 場所: `{issue.file}`:{issue.line or '-'}" + (f" {issue.function}()" if issue.function else ""),
                  "- 警告経路:", ""]
        lines += [f"  {n}. {'★ ' if e.main else ''}`{e.file}`:{e.line or '-'} {e.tag} — {e.description}"
                  for n, e in enumerate(events.get(issue.cid) or [], 1)] or ["  （取れませんでした）"]
        lines.append("")
    if description:
        lines += ["チェッカーの説明（Coverity）:", "", description, ""]
    if notes:
        lines += ["パスなどの注意:", "", *[f"- {n}" for n in notes], ""]
    verdict = result.verdict
    lines += ["## 確かめる結果", "",
              f"- 見立て: {verdict.judgement} — {verdict.summary}",
              f"- 推奨: {result.recommendation} ／ 確信度: {result.confidence}",
              "- 根拠の場所: " + ("、".join(f"`{e.file}`:{e.line or '-'}（{e.note}）" for e in verdict.evidence) or "なし"),
              f"- 逸脱案: {result.deviation.classification} / {result.deviation.action} / {result.deviation.severity}",
              f"  - 逸脱コメント: {result.deviation.comment}",
              f"- 修正案: {result.fix.summary}（影響: {result.fix.impact}）"
              + ("、最新のリビジョンで既に直っている" if result.fix.already_fixed_on_latest else ""),
              f"- リビジョンのずれ: {result.revision_drift.status}" + (f"（{result.revision_drift.detail}）"
                                                                        if result.revision_drift.detail else "")]
    if result.fix.exceeded_constraints:
        lines.append(f"- 修正の制約を超えた点: {'、'.join(result.fix.exceeded_constraints)}")
    if item.merged_from:
        lines.append(f"- この作業は、作業 {', '.join(item.merged_from)} を同じ原因としてまとめたものです。"
                     "すべての CID が同じ原因か、修正がすべての警告に効くかも確かめます。")
    elif item.is_group:
        lines.append("- この作業は、同じ原因と思われる警告のグループです。すべての CID が同じ原因かも確かめます。")
    lint = _lint(run, item, result)
    if lint:
        lines += ["", "## スクリプトが決まった方法で気づいたこと（重みは自分で判断する）", "", *[f"- {x}" for x in lint]]
    lines += ["", "## 終わったら", "",
              f"検証の結果 `{out}` を書いたら、呼び出し元に 1 行だけ返します。例: 「{item.id}: 合格 — 根拠の行と経路の条件を確かめた」", ""]
    brief = run.dir / "agents" / "reviewer" / f"{item.id}.md"
    write_text(brief, "\n".join(lines))
    _start_role(run, "reviewer", f"reviewer:{item.id}", fresh=True, item=item.id, repo_fingerprint=run.vcs.fingerprint(),
                result_sha=_sha(run.result_file(item.id)), patch_sha=_sha(Path(patch)) if patch else None)
    return _launch("reviewer", _role_task("reviewer", run.state.roles["reviewer"]),
                   _prompt(brief, "調べた結果を独立に確かめます"), item.id)


def _reviewer_ended(run: Run, item_id: str, gen: int | None, round_: int | None, failed: bool, reason: str,
                    say: list[str], actions: list[dict[str, Any]]) -> None:
    lease = run.state.roles.get("reviewer", {})
    if lease.get("task") != f"reviewer:{item_id}" or not lease.get("active") or \
            not _current(gen, round_, lease.get("gen", 1), lease.get("rounds", 1)):
        return  # told twice, or about a start that was followed by another
    item = run.state.item(item_id)
    problems: list[str] = []
    review = None
    if not failed:
        path = run.item_dir(item_id) / "review.json"
        try:
            review = ReviewResult.model_validate(read_json(path))
        except (CtError, ValidationError) as exc:
            problems = ([f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
                        if isinstance(exc, ValidationError) else [f"{path} を書いてください（形はスキル triage-review）"])
        else:
            if review.item != item_id:
                problems.append(f"item は {item_id} にしてください（今は {review.item}）")
            if review.outcome == "rework" and not review.rework:
                problems.append("outcome が rework のときは、直すことを rework に書きます")
        if not problems:
            saved = read_json(run.item_dir(item_id) / "saved.json", {})
            patch = (saved.get("fix") or {}).get("patch")
            if _sha(run.result_file(item_id)) != lease.get("result_sha") or \
                    (patch and _sha(Path(patch)) != lease.get("patch_sha")):
                reason = "検証の間に、結果ファイルか修正の差分が変わりました（検証の結果は使いません）"
                failed = True
            elif lease.get("repo_fingerprint") and run.vcs.fingerprint() != lease["repo_fingerprint"]:
                say.append(f"{item_id}: 検証の間に、利用者のリポジトリのファイルが変わりました。"
                           "検証エージェントが誤って変更していないか確かめてください")
    if problems and _rounds(run, "reviewer", f"reviewer:{item_id}") < ROLE_ROUNDS:
        brief = run.dir / "agents" / "reviewer" / f"{item_id}.md"
        _start_role(run, "reviewer", f"reviewer:{item_id}", item=item_id, result_sha=lease.get("result_sha"),
                    patch_sha=lease.get("patch_sha"), repo_fingerprint=lease.get("repo_fingerprint"))
        actions.append(_launch("reviewer", _role_task("reviewer", run.state.roles["reviewer"]),
                               _prompt(brief, "調べた結果を独立に確かめます")
                               + " 検証の結果ファイルの次の点を直してください：\n" + "\n".join(f"- {p}" for p in problems),
                               item_id))
        return
    _end_role(run, "reviewer", "failed" if failed or problems else review.outcome, item=item_id,
              reason=reason or "；".join(problems))
    with run.update() as state:
        current = state.item(item_id)
        if failed or problems or review is None:
            current.review = "unverified"
            say.append(f"{item_id}: 検証できませんでした（{reason or '；'.join(problems)}）")
        elif review.outcome == "rework" and current.review_rounds < state.max_rework:
            current.review, current.review_rounds = "rework", current.review_rounds + 1
            current.rework = review.rework
            current.status, current.slot = "pending", None
            say.append(f"{item_id}: 検証で差し戻しました（{review.note}）")
        else:
            current.review = "flag" if review.outcome == "rework" else review.outcome
            if review.outcome != "pass":
                say.append(f"{item_id}: 検証で要確認になりました（{review.note}）")
    if run.state.item(item_id).status == "done":
        triage.write_item_files(run, item_id)
