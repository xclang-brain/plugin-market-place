# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""The result folder of a triage of Coverity warnings, CID by CID.

    uv run --native-tls --quiet triage.py <command> ...

  start   --repo <repo> [--slots N] [--review] [--new]      create or continue the result folder
  next    --run <dir> [--slot K] [--batch R] [--cid N]      hand out the next CID to work on
  save    --run <dir> --cid N [--slot K]                    check result.json and store the fix as fix.patch
  review  --run <dir> --cid N --outcome pass|flag|rework|unverified [--point "file:line text"].. [--cap high|medium|low]
  merge   --run <dir> --leader N --members A,B [--rework] [--point "text"]..
  status  --run <dir>                                       counts, slots, merge candidates, review batches
  finish  --run <dir>                                       refuse unfinished work, write summary.md

Every command prints one JSON object (UTF-8): ``ok``, ``run_dir`` and, under ``next``, the full command
lines of the next step, so that an agent that lost its context continues from the output alone.
``"ok": false`` comes with a Japanese ``error`` (and ``problems``) that says what to correct.

The folder is the state. A CID of issues.json without cid/<CID>/state.json is pending. ``next`` wins a
CID by the exclusive creation of work/claims/<CID>.<try>; only the winner writes its state.json.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True  # the skill folder may be read-only, and it stays clean
import yaml  # noqa: E402

import checks  # noqa: E402
import job  # noqa: E402
import vcs  # noqa: E402
from job import read_json, write_bytes, write_json  # noqa: E402

SCRIPT = Path(__file__).resolve()
WAIT = 20  # seconds start and finish wait for a background job before they answer "wait"
STALE = 30 * 60  # seconds after which a claim without a save is taken from its slot
BATCH = 5  # CIDs per slot and round (keeps a worker's context small)
VERDICT = {"true_bug": "本物のバグ", "false_positive": "誤検知", "intentional": "意図的", "undetermined": "判定不能"}
RECOMMEND = {"fix": "修正を取り込む", "deviation": "逸脱コメントを登録する"}
LEVEL = {"high": "高", "medium": "中", "low": "低"}
REVIEW = {"pass": "合格", "flag": "要確認", "rework": "差し戻し", "unverified": "未検証"}
BUILD = {"ok": "成功", "failed": "失敗", "not_applied": "適用できず"}
HEADERS = ["CID", "チェッカー", "場所", "判定", "推奨", "確信度", "検証", "ビルド", "修正コード", "逸脱コメント"]


class Problem(Exception):
    """Something the person or the agent can correct. Printed as {"ok": false, "error": ...}."""

    def __init__(self, message: str, **fields):
        super().__init__(message)
        self.fields = fields


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise Problem(f"引数が正しくありません（{message}）")


def command(name: str, run_dir: Path | None, *extra) -> str:
    """The full command line of a step. Paths are written with forward slashes (Windows accepts them)."""
    parts = ["uv run --native-tls --quiet", f'"{SCRIPT.as_posix()}"', name]
    if run_dir is not None:
        parts += ["--run", f'"{run_dir.as_posix()}"']
    return " ".join([*parts, *map(str, extra)])


def start_command(repo: Path, mode: dict) -> str:
    return command("start", None, "--repo", f'"{repo.as_posix()}"', *(["--slots", mode["slots"]] if mode["all"] else []),
                   *(["--review"] if mode["review"] else []))


def reply(run_dir: Path, **fields) -> dict:
    return {"ok": True, "run_dir": run_dir.as_posix(), **fields}


def settings(repo: Path) -> dict:
    """The keys of <repo>/.coverity-triage/config.yaml this script uses, with their defaults."""
    path = repo / ".coverity-triage" / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        data = data if isinstance(data, dict) else {}
        coverity, build = data.get("coverity") or {}, data.get("build") or {}
        found = {"url": str(coverity.get("url") or ""), "stream": str(coverity.get("stream") or ""),
                 "output_dir": (repo / str(data.get("output_dir") or "../coverity-triage-out")).resolve(),
                 "parallel": min(8, max(1, int(data.get("parallel") or 3))),
                 "base_branch": str(data.get("base_branch") or ""), "command": str(build.get("command") or ""),
                 "check_fixes": build.get("check_fixes") is True,
                 "initial_analysis": build.get("initial_analysis") is True,
                 "analyze_args": str(build.get("analyze_args") or "--all")}
    except FileNotFoundError:
        raise Problem(f"設定ファイルがありません: {path.as_posix()}。"
                      "/coverity-triage-kit:coverity-setup <URL> <ストリーム> を実行してください") from None
    except (yaml.YAMLError, UnicodeDecodeError, AttributeError, TypeError, ValueError) as exc:
        raise Problem(f"設定ファイルを読み取れません: {path.as_posix()}（{exc}）") from None
    if not found["url"] or not found["stream"]:
        raise Problem(f"設定ファイルに coverity.url と coverity.stream を記入してください: {path.as_posix()}")
    return found


# ---- the run folder -------------------------------------------------------------------------


class Run:
    def __init__(self, folder: Path, starting: bool = False):
        self.dir = folder
        self.meta = read_json(folder / "run.json")
        if not isinstance(self.meta, dict):
            raise Problem(f"結果フォルダーではありません（run.json がありません）: {folder.as_posix()}")
        self.repo = Path(self.meta["repo"])
        fetch = read_json(folder / "issues.json")
        self.fetch = fetch if fetched(fetch) else {}
        if not self.fetch and not starting:
            raise Problem("警告の一覧がまだありません。next のコマンド（start）を実行してください",
                          next=[start_command(self.repo, self.meta)])
        self.issues = {i["cid"]: i for i in self.fetch.get("issues", []) if isinstance(i, dict) and "cid" in i}

    def cid_dir(self, cid: int) -> Path:
        return self.dir / "cid" / str(cid)

    def states(self) -> dict[int, dict | None]:
        return {cid: read_json(self.cid_dir(cid) / "state.json") for cid in self.issues}

    def put(self, cid: int, state: dict) -> None:
        write_json(self.cid_dir(cid) / "state.json", state)

    def copy(self, slot: int) -> Path:
        return self.dir / "work" / f"w{slot}"

    def steps(self, states: dict) -> list[str]:
        """The command lines of what comes next in this run."""
        if count(states)["remaining"]:
            if self.meta["slots"] == 1 and not self.meta["review"]:
                return [command("next", self.dir, "--slot", 1)]
            round_ = 1 + max((s.get("batch") or 0 for s in states.values() if s), default=0)
            return [command("next", self.dir, "--slot", k, "--batch", round_) for k in range(1, self.meta["slots"] + 1)]
        if self.meta["review"] and unreviewed(states):
            return [command("review", self.dir, "--cid", "<CID>", "--outcome", "<pass|flag|rework>",
                            "[--cap <high|medium|low>]", '[--point "<ファイル:行 指摘>"]')]
        return [command("finish", self.dir)]


def fetched(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("issues"), list)


def count(states: dict) -> dict:
    """pending: nobody works on it (never handed out, or sent back). remaining: not finished yet."""
    found = {"total": len(states), "pending": 0, "working": 0, "done": 0, "failed": 0}
    for state in states.values():
        found["pending" if state is None or state["status"] == "rework" else state["status"]] += 1
    return {**found, "remaining": found["pending"] + found["working"]}


def unreviewed(states: dict) -> list[int]:
    return [cid for cid, s in states.items()
            if s and s["status"] == "done" and not s.get("merged_into") and not s.get("review")]


def age(stamp: str) -> float:
    return (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()


def remove_work(run_dir: Path, repo: Path) -> None:
    if (run_dir / "work").is_dir():
        vcs.open_repo(repo).remove(run_dir / "work")


def find_run(repo: Path, cfg: dict, kind: str, new: bool) -> Path:
    """The newest result folder of this repository while it has unfinished CIDs, else a new one."""
    out = cfg["output_dir"]
    runs = sorted((p for p in out.iterdir() if (read_json(p / "run.json") or {}).get("repo") == repo.as_posix()),
                  reverse=True) if out.is_dir() else []
    if runs and not new:
        latest = Run(runs[0], starting=True)
        if not latest.fetch or count(latest.states())["remaining"]:
            return runs[0]
    for old in runs:
        remove_work(old, repo)
    while True:
        run_dir = out / time.strftime("%Y%m%d-%H%M%S")
        try:
            run_dir.mkdir(parents=True)
            break
        except FileExistsError:
            time.sleep(0.3)
    write_json(run_dir / "run.json", {"repo": repo.as_posix(), "vcs": kind, "base_branch": "", "base_revision": "",
                                      "stream": cfg["stream"], "slots": 1, "review": False, "all": False,
                                      "created_at": job.now()})
    return run_dir


def need_command(cfg: dict, option: str) -> None:
    if not cfg["command"]:
        raise Problem(f"{option} を使用するには、設定ファイルの build.command にビルドのコマンドを記入してください")


def cmd_start(args) -> dict:
    repo = Path(args.repo).resolve()
    cfg = settings(repo)
    kind = vcs.open_repo(repo).kind
    slots = args.slots or (cfg["parallel"] if args.review else 1)
    mode = {"slots": slots, "review": args.review, "all": args.slots is not None or args.review}
    again = [start_command(repo, mode)]
    run_dir = find_run(repo, cfg, kind, args.new)
    meta = {**read_json(run_dir / "run.json"), **mode}
    write_json(run_dir / "run.json", meta)
    deadline = time.monotonic() + WAIT
    jobs = []
    if cfg["initial_analysis"] and not fetched(read_json(run_dir / "issues.json")):
        need_command(cfg, "build.initial_analysis")
        jobs.append(("analyze", "最初の解析と登録", {"repo": str(repo), "command": cfg["command"], "url": cfg["url"],
                                             "analyze_args": cfg["analyze_args"], "stream": cfg["stream"]}))
    jobs.append(("prepare", "作業用コピーの準備", {"repo": str(repo), "slots": slots, "base_branch": cfg["base_branch"]}))
    for name, label, job_args in jobs:
        status = job.ensure(run_dir, name, job_args, deadline)
        if status["status"] == "running":
            return reply(run_dir, wait=True, message=f"{label}を実行しています。next のコマンドをもう一度実行してください",
                         log=status["log"], next=again)
        if status["status"] == "failed":
            raise Problem(f"{label}が失敗しました: {status['error']}", run_dir=run_dir.as_posix(), log=status["log"],
                          next=again)
    if not meta["base_revision"]:
        meta.update(base_branch=status["base_branch"], base_revision=status["base_revision"])
        write_json(run_dir / "run.json", meta)
    run = Run(run_dir, starting=True)
    if not run.fetch:
        fetch = {"tool": "coverity_search_issues",
                 "arguments": {"repo_root": repo.as_posix(), "output_file": (run_dir / "issues.json").as_posix()}}
        return reply(run_dir, wait=False, need_fetch=True, fetch=[fetch], next=again,
                     message="警告の一覧がまだありません。fetch のツールを呼び出して取得してから、next のコマンドを実行してください")
    states = run.states()
    found = count(states)
    total, matched = found["total"], run.fetch.get("total_matched")
    message = f"{total} 件の警告を処置します" if total else "条件に合う警告はありません"
    if found["done"] + found["failed"]:
        message = f"前回の続きから進めます（残り {found['remaining']} 件）"
    if run.fetch.get("limited") and matched:
        message += f"。条件に合う警告は {matched} 件あります。設定の上限（{total} 件）まで取得しました"
    fetch = details_call(run, [cid for cid, s in states.items() if not s or s["status"] in ("working", "rework")])
    if fetch:
        message += "。fetch のツールを呼び出して警告の経路を取得してから、next のコマンドを実行してください"
    return reply(run_dir, wait=False, need_fetch=False, fetch=fetch, counts=found, slots=slots,
                 note=status.get("note", ""), message=message, next=run.steps(states) if total else [])


# ---- next: hand out one CID -----------------------------------------------------------------


def claim(run: Run, cid: int, state: dict | None, slot: int, batch: int | None) -> dict | None:
    """Take the CID for the slot. None: another process took it. The winner of the exclusive creation of
    the claim file writes the state. A CID handed out twice without a save becomes failed."""
    tries = (state or {}).get("tries", 0)
    while True:
        tries += 1
        token = run.dir / "work" / "claims" / f"{cid}.{tries}"
        token.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(token, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            taken = (read_json(run.cid_dir(cid) / "state.json") or {}).get("tries", 0) >= tries
            if taken or time.time() - token.stat().st_mtime < 60:
                return None
            # its creator ended before it wrote the state: that try is lost, take the next one
    lost = state is not None and state["status"] == "working"
    new = {**(state or {}), "status": "working", "slot": slot, "batch": batch, "tries": tries, "retry": lost,
           "claimed_at": job.now()}
    if lost and state.get("retry"):
        new.update(status="failed", error="処置を2回開始しましたが、どちらも結果が保存されませんでした")
    run.put(cid, new)
    return new


def map_path(copy: Path, path: str) -> str:
    """The path Coverity reports, as a path in the work copy: as it is when the file is there, else the
    one file with the longest matching end of the path."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p and not p.endswith(":")]
    if not parts or (copy / "/".join(parts)).is_file():
        return "/".join(parts)
    best, score = [], 0
    for folder, dirs, files in os.walk(copy):
        dirs[:] = [d for d in dirs if d not in (".git", ".svn")]
        for name in files:
            if name.lower() == parts[-1].lower():
                have = Path(folder, name).relative_to(copy).parts
                n = 0
                while n < min(len(have), len(parts)) and have[-1 - n].lower() == parts[-1 - n].lower():
                    n += 1
                if n > score:
                    best, score = [have], n
                elif n == score:
                    best.append(have)
    return "/".join(best[0]) if len(best) == 1 else str(path)


def details_call(run: Run, cids: list[int]) -> list[dict]:
    """The tool call that fetches the warning paths (cid/<CID>/issue.json) that are not in the folder yet."""
    missing = [cid for cid in cids if not (run.cid_dir(cid) / "issue.json").is_file()]
    return [{"tool": "coverity_get_issue_details", "arguments": {
        "repo_root": run.repo.as_posix(), "cids": missing, "output_dir": (run.dir / "cid").as_posix()}}] if missing else []


def cmd_next(args) -> dict:
    run = Run(args.run)
    slot, batch, copy = args.slot, args.batch, run.copy(args.slot)
    repo = vcs.open_repo(run.repo)
    if not repo.is_copy(copy):
        raise Problem(f"スロット {slot} の作業用コピーがありません。next のコマンド（start）を実行してください",
                      next=[start_command(run.repo, run.meta)])
    if args.cid is not None and args.cid not in run.issues:
        raise Problem(f"CID {args.cid} は取得した警告の一覧にありません")
    states = run.states()

    def free(state) -> bool:
        if state is None or state["status"] == "rework":
            return True
        return state["status"] == "working" and (state["slot"] == slot or state["slot"] > run.meta["slots"]
                                                 or age(state["claimed_at"]) > STALE)

    def own(cid) -> bool:  # the CID this slot was given and did not save comes first
        return bool(states[cid]) and states[cid]["status"] == "working" and states[cid]["slot"] == slot

    order = [args.cid] if args.cid is not None else sorted(run.issues, key=lambda cid: not own(cid))
    taken = sum(1 for s in states.values() if s and s.get("slot") == slot and s.get("batch") == batch)
    for cid in order if batch is None or taken < BATCH else []:
        if not free(states[cid]):
            continue
        state = claim(run, cid, states[cid], slot, batch)
        if state is None or state["status"] != "working":
            states[cid] = state or states[cid]
            continue
        base = run.meta["base_revision"]
        repo.reset(copy, base)
        issue = {**run.issues[cid], "file": map_path(copy, run.issues[cid].get("file", ""))}
        out = reply(run.dir, cid=cid, slot=slot, repo=run.repo.as_posix(), issue=issue,
                    issue_file=(run.cid_dir(cid) / "issue.json").as_posix(), work_dir=copy.as_posix(),
                    result_file=(run.cid_dir(cid) / "result.json").as_posix(), fetch=details_call(run, [cid]))
        if state.get("rework"):  # sent back: the earlier fix is in the copy again, to be changed
            patch = run.cid_dir(cid) / "fix.patch"
            applied = patch.is_file() and not repo.apply(copy, patch)
            out["rework"] = {"points": state["rework"], "patch_applied": applied}
        out["message"] = (f"CID {cid} を処置してください。work_dir のファイルを修正し、result_file に結果を書いてから、"
                          "next のコマンドを実行してください")
        if state["retry"]:
            out["message"] += "（前回は結果が保存されなかったため、もう一度処置します）"
        out["next"] = [command("save", run.dir, "--cid", cid, "--slot", slot)]
        return out
    found = count(states)
    if args.cid is not None:
        state = read_json(run.cid_dir(args.cid) / "state.json") or {}
        message = f"CID {args.cid} は" + {"done": "処置済みです", "failed": f"処置できませんでした: {state.get('error')}"}.get(
            state.get("status"), "ほかのスロットが処置中です")
    elif batch is not None:
        message = "この依頼で処置する CID は以上です。結果を報告してください"
    else:
        message = f"ほかのスロットが処置中の CID が {found['working']} 件あります" if found["remaining"] else "未処置の CID はありません"
    return reply(run.dir, cid=None, done=True, counts=found, message=message,
                 next=[] if batch is not None else [command("finish", run.dir)])


# ---- save, review, merge --------------------------------------------------------------------


def cmd_save(args) -> dict:
    run = Run(args.run)
    cid, slot = args.cid, args.slot
    state = read_json(run.cid_dir(cid) / "state.json")
    if not state or state["status"] != "working" or state["slot"] != slot:
        raise Problem(f"CID {cid} は、スロット {slot} が処置中の CID ではありません。next で受け取った CID を保存してください",
                      next=[command("next", run.dir, "--slot", slot)])
    folder, copy, base = run.cid_dir(cid), run.copy(slot), run.meta["base_revision"]
    repo = vcs.open_repo(run.repo)
    files, patch = [], b""
    try:
        result = json.loads((folder / "result.json").read_bytes().decode("utf-8-sig"))
        problems = checks.check_result(result, cid)
    except FileNotFoundError:
        problems = [f"result.json がありません: {(folder / 'result.json').as_posix()}"]
    except ValueError as exc:
        problems = [f"result.json を UTF-8 の JSON として読み取れません（{exc}）"]
    if not problems:
        files, patch = repo.diff(copy, base)
        if result.get("fix") and not patch:
            problems.append(f"fix がありますが、work_dir のファイルが変更されていません: {copy.as_posix()}")
        if patch and not result.get("fix"):
            problems.append("work_dir のファイルが変更されていますが、fix がありません。fix.summary を記入するか、変更を元に戻してください")
        for n, item in enumerate(result["evidence"]):
            rel = item["file"]
            if Path(rel).is_absolute() or not ((copy / rel).is_file() or rel in files):
                problems.append(f"evidence[{n}].file: {rel} が work_dir にありません。リポジトリからの相対パスで記入してください")
    if problems:
        return {**reply(run.dir, cid=cid, result_file=(folder / "result.json").as_posix(), work_dir=copy.as_posix(),
                        problems=problems, next=[command("save", run.dir, "--cid", cid, "--slot", slot)]),
                "ok": False, "error": "結果に修正が必要な項目があります。problems を修正してから、next のコマンドを実行してください"}
    if patch:
        write_bytes(folder / "fix.patch", patch)
    else:
        (folder / "fix.patch").unlink(missing_ok=True)
    kept = {k: v for k, v in state.items() if k not in ("rework", "retry", "review", "error")}
    run.put(cid, {**kept, "status": "done", "files": files, "confidence": result["confidence"], "saved_at": job.now()})
    repo.reset(copy, base)
    if not run.meta["all"]:
        summarize(run)
    states = run.states()
    more = [command("next", run.dir, "--slot", slot, *(["--batch", state["batch"]] if state.get("batch") else []))]
    return reply(run.dir, cid=cid, files=files, patch_file=(folder / "fix.patch").as_posix() if patch else None,
                 counts=count(states), next=more if state.get("batch") or count(states)["remaining"] else run.steps(states))


def cmd_review(args) -> dict:
    run = Run(args.run)
    cid, folder = args.cid, run.cid_dir(args.cid)
    state = read_json(folder / "state.json")
    if not state or state["status"] != "done" or state.get("merged_into"):
        raise Problem(f"CID {cid} は検証の対象ではありません。処置済みの CID（統合された CID は代表の CID）を指定してください")
    problems = checks.check_review(args.outcome, args.point, args.cap)
    if problems:
        raise Problem("検証の記録に修正が必要な項目があります", problems=problems)
    # one rework only: a second one is stored as "flag" (要確認)
    outcome = "flag" if args.outcome == "rework" and state.get("reworked") else args.outcome
    round_ = (read_json(folder / "review.json") or {}).get("round", 0) + 1
    write_json(folder / "review.json", {"outcome": outcome, "points": args.point, "confidence_cap": args.cap,
                                        "round": round_})
    worker = (read_json(folder / "result.json") or {}).get("confidence")
    state.update(review=outcome, confidence=checks.lower_confidence(worker, args.cap))
    if outcome == "rework":
        state.update(status="rework", reworked=True, rework=args.point)
    run.put(cid, state)
    return reply(run.dir, cid=cid, outcome=outcome, confidence=state["confidence"],
                 next=[command("status", run.dir)])


def cmd_merge(args) -> dict:
    run = Run(args.run)
    states = run.states()
    try:
        members = [int(m) for m in args.members.split(",") if m.strip()]
    except ValueError:
        raise Problem("members: CID をカンマで区切って指定してください（例: 20004,20007）") from None
    cids = [args.leader, *members]
    bad = [c for c in cids if not states.get(c) or states[c]["status"] != "done" or states[c].get("merged_into")
           or states[c].get("members")]
    if bad or not members or len(set(cids)) != len(cids):
        raise Problem("統合できるのは、処置済みで、まだ統合されていない CID です" + (f"（対象外: {bad}）" if bad else ""))
    for cid in members:
        run.put(cid, {**states[cid], "merged_into": args.leader})
    leader = {**states[args.leader], "members": members}
    if args.rework:  # no member's fix covers all: one worker makes one fix and one comment for all
        leader.pop("review", None)
        leader.update(status="rework", rework=[
            f"CID {', '.join(map(str, members))} と同じ原因です。すべての CID を解消する修正と逸脱コメントを1つ作成してください", *args.point])
    run.put(args.leader, leader)
    return reply(run.dir, leader=args.leader, members=members, rework=args.rework, next=[command("status", run.dir)])


# ---- status, finish -------------------------------------------------------------------------


def patch_spans(patch: Path) -> list[tuple[str, int, int]]:
    """(file, first line, last line) of every hunk, in the lines of the code before the fix."""
    spans, name = [], ""
    for line in (patch.read_bytes() if patch.is_file() else b"").split(b"\n"):
        if line.startswith(b"+++ "):
            name = line[4:].split(b"\t")[0].strip().decode("utf-8", "replace")
            name = name[2:] if name.startswith("b/") else name
        hunk = re.match(rb"@@ -(\d+)(?:,(\d+))? ", line)
        if hunk and name:
            spans.append((name, int(hunk[1]), int(hunk[1]) + max(int(hunk[2] or 1), 1) - 1))
    return spans


def merge_candidates(run: Run, cids: list[int]) -> list[dict]:
    """CIDs that may have one cause: the same checker and function, or fixes that change the same lines."""
    same, spans = {}, []
    for cid in cids:
        issue = run.issues[cid]
        if issue.get("function"):
            same.setdefault((issue.get("checker"), issue.get("file"), issue["function"]), []).append(cid)
        spans += [(*span, cid) for span in patch_spans(run.cid_dir(cid) / "fix.patch")]
    found = [{"cids": group, "reason": "同じチェッカーと関数"} for group in same.values() if len(group) > 1]
    pairs = {tuple(sorted((a[3], b[3]))) for a, b in itertools.combinations(sorted(spans), 2)
             if a[0] == b[0] and a[3] != b[3] and b[1] <= a[2]}
    return found + [{"cids": list(pair), "reason": "修正が同じ行を変更"} for pair in sorted(pairs)
                    if not any(set(pair) <= set(group["cids"]) for group in found)]


def cmd_status(args) -> dict:
    run = Run(args.run)
    states = run.states()
    slots = run.meta["slots"]
    in_progress = [{"slot": s["slot"], "cid": cid, "minutes": int(age(s["claimed_at"]) // 60)}
                   for cid, s in states.items() if s and s["status"] == "working"]
    todo = unreviewed(states) if run.meta["review"] else []
    batches = [{"slot": k + 1, "work_dir": run.copy(k + 1).as_posix(), "cids": todo[k::slots]}
               for k in range(slots) if todo[k::slots]]
    single = [cid for cid, s in states.items()
              if s and s["status"] == "done" and not s.get("merged_into") and not s.get("members")]
    merge = command("merge", run.dir, "--leader", "<CID>", "--members", "<CID,CID>", "[--rework]")
    return reply(run.dir, counts=count(states), in_progress=in_progress, cid_dir=(run.dir / "cid").as_posix(),
                 merge_candidates=merge_candidates(run, single), merge_command=merge, review_batches=batches,
                 next=run.steps(states))


def summarize(run: Run) -> str:
    """Write summary.md: every fetched CID, and both outputs (fix code, deviation comment) of every
    finished one. Returns the table."""
    states = run.states()
    built = read_json(run.dir / "build.json") or {}
    built = built.get("cids", {}) if built.get("status") == "done" else {}
    rows, sections = [], []
    for cid, issue in run.issues.items():
        state = states[cid]
        place = f"{issue.get('file', '')}:{issue.get('line', '')}"
        lead = [str(cid), str(issue.get("checker", "")), place]
        if state is None or state["status"] in ("working", "rework"):
            rows.append([*lead, "未処置", *["-"] * 6])
        elif state["status"] == "failed":
            rows.append([*lead, "失敗", *["-"] * 6])
            sections.append(f"## CID {cid}（{lead[1]}、{place}）\n\n処置できませんでした: {state.get('error', '')}\n")
        if state is None or state["status"] != "done" or state.get("merged_into"):
            continue
        result = read_json(run.cid_dir(cid) / "result.json") or {}
        deviation = result.get("deviation") or {}
        patch = run.cid_dir(cid) / "fix.patch"
        points = (read_json(run.cid_dir(cid) / "review.json") or {}).get("points") or []
        if state.get("members"):
            lead[0] += f"（同じ原因: {', '.join(map(str, state['members']))}）"
        labels = [VERDICT.get(result.get("verdict"), "-"), RECOMMEND.get(result.get("recommendation"), "-"),
                  LEVEL.get(state.get("confidence"), "-"), REVIEW.get(state.get("review"), "-"),
                  BUILD.get(built.get(str(cid)), "-")]
        rows.append([*lead, *labels, ", ".join(state.get("files") or []) or "なし", str(deviation.get("comment", ""))])
        if patch.is_file():
            fix = [str((result.get("fix") or {}).get("summary", "")), "", "````diff", vcs.text(patch.read_bytes()), "````"]
        else:
            fix = [f"修正コードはありません。理由: {result.get('no_fix_reason', '')}"]
        sections.append("\n".join([
            f"## CID {lead[0]}（{lead[1]}、{place}）", "", f"- 判定: {labels[0]}（確信度: {labels[2]}）",
            f"- 推奨: {labels[1]}", f"- 概要: {result.get('summary', '')}", f"- 検証: {labels[3]}",
            *[f"  - {p}" for p in points], f"- ビルド: {labels[4]}", "", "根拠:",
            *[f"- {e.get('file')}:{e.get('line')} {e.get('note')}" for e in result.get("evidence") or []], "",
            "### 修正コード", "", *fix, "", "### 逸脱コメント", "", str(deviation.get("comment", "")), "",
            f"Classification: {deviation.get('classification')}／Action: {deviation.get('action')}／"
            f"Severity: {deviation.get('severity')}", ""]))
    table = "\n".join("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in row) + " |"
                      for row in [HEADERS, ["---"] * len(HEADERS), *rows])
    found = count(states)
    head = ["# Coverity の警告の処置結果", "", f"- リポジトリ: {run.meta['repo']}", f"- ストリーム: {run.meta['stream']}",
            f"- 警告: {found['total']} 件（処置済み {found['done']} 件、失敗 {found['failed']} 件、未処置 {found['remaining']} 件）"]
    if run.fetch.get("limited") and run.fetch.get("total_matched"):
        head.append(f"- 条件に合う警告は {run.fetch['total_matched']} 件あります。設定の上限（{found['total']} 件）まで取得しました")
    write_bytes(run.dir / "summary.md", "\n".join([*head, "", table, "", *sections]).encode("utf-8"))
    return table


def cmd_finish(args) -> dict:
    run = Run(args.run)
    cfg = settings(run.repo)
    states = run.states()
    found = count(states)
    if run.meta["all"] and found["remaining"]:
        left = [cid for cid, s in states.items() if s is None or s["status"] in ("working", "rework")]
        raise Problem(f"未処置の CID が {len(left)} 件あります。すべて処置してから finish を実行してください",
                      pending=left, next=run.steps(states))
    if run.meta["review"] and unreviewed(states):
        raise Problem(f"検証されていない CID が {len(unreviewed(states))} 件あります。検証してから finish を実行してください",
                      unreviewed=unreviewed(states), next=[command("status", run.dir)])
    fixes = [{"cid": cid, "files": s["files"]} for cid, s in states.items()
             if s and s["status"] == "done" and not s.get("merged_into") and (run.cid_dir(cid) / "fix.patch").is_file()]
    if cfg["check_fixes"] and fixes:
        need_command(cfg, "build.check_fixes")
        status = job.ensure(run.dir, "build", {"repo": str(run.repo), "base_revision": run.meta["base_revision"],
                                               "command": cfg["command"], "fixes": fixes}, time.monotonic() + WAIT)
        if status["status"] == "running":
            return reply(run.dir, wait=True, message="修正を適用してビルドしています。next のコマンドをもう一度実行してください",
                         log=status["log"], next=[command("finish", run.dir)])
        if status["status"] == "failed":
            raise Problem(f"ビルドでの確認を実行できませんでした: {status['error']}", log=status["log"])
    table = summarize(run)
    if not found["remaining"]:
        remove_work(run.dir, run.repo)
    return reply(run.dir, wait=False, counts=found, summary_file=(run.dir / "summary.md").as_posix(), table=table, next=[])


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="triage.py")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    start = sub.add_parser("start")
    start.add_argument("--repo", required=True)
    start.add_argument("--slots", type=int, choices=range(1, 9))
    start.add_argument("--review", action="store_true")
    start.add_argument("--new", action="store_true")
    others = {name: sub.add_parser(name) for name in ("next", "save", "review", "merge", "status", "finish")}
    for name, cmd in others.items():
        cmd.add_argument("--run", required=True)
        if name in ("next", "save"):
            cmd.add_argument("--slot", type=int, default=1)
        if name in ("next", "save", "review"):
            cmd.add_argument("--cid", type=int, required=name != "next")
        if name in ("review", "merge"):
            cmd.add_argument("--point", action="append", default=[])
    others["next"].add_argument("--batch", type=int)
    others["review"].add_argument("--outcome", required=True)
    others["review"].add_argument("--cap")
    others["merge"].add_argument("--leader", type=int, required=True)
    others["merge"].add_argument("--members", required=True)
    others["merge"].add_argument("--rework", action="store_true")
    args, code = None, 0
    try:
        args = parser.parse_args(argv)
        if args.command != "start":
            args.run = Path(args.run).resolve()
        out = globals()[f"cmd_{args.command}"](args)
    except Problem as exc:
        out = {"ok": False, "error": str(exc), **exc.fields}
    except vcs.VcsError as exc:
        out = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        out, code = {"ok": False, "error": f"予期しないエラーが発生しました: {type(exc).__name__}: {exc}"}, 1
    out.setdefault("next", [])
    run_dir = out.get("run_dir") or getattr(args, "run", None)
    if run_dir:
        out = {"ok": out["ok"], "run_dir": Path(run_dir).as_posix(), **out}
        if Path(run_dir, "run.json").is_file():  # log.txt: one line per command
            what = " ".join(f"{k}={getattr(args, k)}" for k in ("slot", "cid", "leader", "outcome") if getattr(args, k, None))
            with open(Path(run_dir, "log.txt"), "a", encoding="utf-8", newline="\n") as log:
                log.write(f"{job.now()} {args.command} {what} -> {'ok' if out['ok'] else out['error']}\n")
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(main())
