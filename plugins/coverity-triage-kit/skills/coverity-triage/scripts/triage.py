# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""The result folder of a triage of Coverity warnings, CID by CID.

    uv run --native-tls --quiet triage.py <command> ...

  start   --repo <repo> [--slots N] [--review] [--new]      create or continue the result folder
  next    --run <dir> [--slot K] [--batch R] [--cid N]      hand out the next CID to work on
  save    --run <dir> --cid N [--slot K]                    check result.json and store the fix as fix.patch
  review  --run <dir> --cid N --outcome pass|flag|rework|unverified [--point "file:line text"]..
          [--cap high|medium|low]
  merge   --run <dir> --leader N --members A,B [--rework] [--point "text"]..
  status  --run <dir>                                       counts, merge candidates, review batches
  finish  --run <dir>                                       refuse unfinished work, write summary.md

Every command prints one JSON object (UTF-8): ``ok``, ``run_dir`` and, under ``next``, the full command
lines of the next step, so that an agent that lost its context continues from the output alone. ``fetch``
lists the tools to call before that step. ``message`` is a sentence for the person.
``"ok": false`` comes with a Japanese ``error`` (and ``problems``) that says what to correct.

The folder is the state. A CID of issues.json without cid/<CID>/state.json is pending. ``next`` wins a
CID by the exclusive creation of work/claims/<CID>.<try>; only the winner writes its state.json.
"""

from __future__ import annotations

import argparse
import hashlib
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
WAIT = 20  # seconds start and finish wait for a background job before they answer that it still runs
STALE = 30 * 60  # seconds after which a claim without a save is taken from its slot
OLD_LIST = 10 * 60  # seconds after which start says when the list was fetched, and fetches an empty one again
BATCH = 5  # CIDs per slot and round (keeps a worker's context small)
VERDICT = {"true_bug": "本物のバグ", "false_positive": "誤検知", "intentional": "意図的", "undetermined": "判定不能"}
RECOMMEND = {"fix": "修正を取り込む", "deviation": "逸脱コメントを登録する"}
LEVEL = {"high": "高", "medium": "中", "low": "低"}
REVIEW = {"pass": "合格", "flag": "要確認", "rework": "差し戻し", "unverified": "未検証"}
BUILD = {"ok": "成功", "failed": "失敗", "not_applied": "適用できず", "unknown": "不明"}


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
    return command("start", None, "--repo", f'"{repo.as_posix()}"',
                   *(["--slots", mode["slots"]] if mode["all"] else []), *(["--review"] if mode["review"] else []))


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


def written(path: Path) -> float:
    """When the file was written (0: there is no such file)."""
    return path.stat().st_mtime if path.is_file() else 0


def remove_work(run_dir: Path, repo: Path) -> str:
    """Delete the work copies of a run. This is cleanup: a folder that another program holds (on Windows
    also a shell that sits in it) stays, and the sentence that says so is returned."""
    try:
        if (run_dir / "work").is_dir():
            vcs.open_repo(repo).remove(run_dir / "work")
    except vcs.VcsError as exc:
        return str(exc)
    return ""


def find_run(repo: Path, cfg: dict, kind: str, new: bool) -> Path:
    """The newest result folder of this repository while it is not finished, else a new one. Not
    finished: it has no list yet, or unfinished CIDs, or (a run that processes every CID) finish has not
    written the summary since a CID changed last. A list without warnings is the answer to the request
    while it is fresh: the folder stays the newest one, and a later request fetches again."""
    out = cfg["output_dir"]
    runs = sorted((p for p in out.iterdir() if (read_json(p / "run.json") or {}).get("repo") == repo.as_posix()),
                  reverse=True) if out.is_dir() else []
    if runs and not new:
        latest = Run(runs[0], starting=True)
        if latest.issues:
            changed = max(written(latest.cid_dir(cid) / "state.json") for cid in latest.issues)
            open_end = latest.meta["all"] and written(runs[0] / "summary.md") < changed
        else:
            open_end = time.time() - written(runs[0] / "issues.json") < OLD_LIST
        if not latest.fetch or count(latest.states())["remaining"] or open_end:
            return runs[0]
    for old in runs:
        remove_work(old, repo)  # what cannot be deleted now is tried again with the next new folder
    while True:
        run_dir = out / time.strftime("%Y%m%d-%H%M%S")
        try:
            run_dir.mkdir(parents=True)
            break
        except FileExistsError:
            time.sleep(0.3)
    write_json(run_dir / "run.json", {"repo": repo.as_posix(), "vcs": kind, "base_branch": "", "base_revision": "",
                                      "stream": cfg["stream"], "slots": 1, "review": False, "all": False})
    return run_dir


def need_command(cfg: dict, option: str) -> None:
    if not cfg["command"]:
        raise Problem(f"{option} を使用するには、設定ファイルの build.command にビルドのコマンドを記入してください")


def cmd_start(args) -> dict:
    repo = Path(args.repo).resolve()
    cfg = settings(repo)
    opened = vcs.open_repo(repo)
    run_dir = find_run(repo, cfg, opened.kind, args.new)

    def meta() -> dict:
        """run.json with the mode of this request. The mode of a run only grows: a request in a chat
        does not take the guards (every CID, review) from a run that an agent has started."""
        old = read_json(run_dir / "run.json")
        slots = args.slots or (cfg["parallel"] if args.review else 1)
        return {**old, "slots": max(old["slots"], slots), "review": old["review"] or args.review,
                "all": old["all"] or args.slots is not None or args.review}

    mode = meta()
    again = [start_command(repo, mode)]
    current = Run(run_dir, starting=True)
    listed = bool(current.fetch)
    if listed and not current.issues:  # nothing to work on: no work copy is waited for
        return reply(run_dir, counts=count({}), message="条件に合う警告はありません", next=[])
    deadline = time.monotonic() + WAIT
    jobs = []
    if cfg["initial_analysis"] and not listed:
        need_command(cfg, "build.initial_analysis")
        jobs.append(("analyze", "最初の解析と登録", "最初の解析と登録を実行しています", deadline,
                     {"repo": str(repo), "command": cfg["command"], "url": cfg["url"],
                      "analyze_args": cfg["analyze_args"], "stream": cfg["stream"]}))
    made = job.read(run_dir, "prepare")
    if made and made["status"] == "done" and not all(opened.is_copy(run_dir / "work" / f"w{k}")
                                                     for k in range(1, mode["slots"] + 1)):
        (run_dir / "work" / "prepare.json").unlink(missing_ok=True)  # a copy was lost: the job makes it again
    # The copies are not needed to fetch the list: without the list the job is started and not waited for.
    jobs.append(("prepare", "作業用コピーの準備", "作業用コピーを準備しています", deadline if listed else 0,
                 {"repo": str(repo), "slots": mode["slots"], "base_branch": cfg["base_branch"]}))
    for name, label, running, until, job_args in jobs:
        status = job.ensure(run_dir, name, job_args, until)
        if status["status"] == "failed":
            raise Problem(f"{label}が失敗しました: {status['error']}", run_dir=run_dir.as_posix(), log=status["log"],
                          next=again)
        if status["status"] == "running" and until:
            return reply(run_dir, message=running, log=status["log"], next=again)
    if not listed:
        search = {"tool": "coverity_search_issues",
                  "arguments": {"repo_root": repo.as_posix(), "output_file": (run_dir / "issues.json").as_posix()}}
        return reply(run_dir, fetch=[search], next=again)
    mode = meta()
    if not mode["base_revision"]:
        mode.update(base_branch=status["base_branch"], base_revision=status["base_revision"])
    write_json(run_dir / "run.json", mode)
    run = Run(run_dir)
    states = run.states()
    found = count(states)
    total, matched = found["total"], run.fetch.get("total_matched")
    if found["done"] + found["failed"]:
        message = f"前回の続きから進めます（残り {found['remaining']} 件）"
    elif run.fetch.get("limited") and matched:
        message = f"条件に合う警告は {matched} 件あります。設定の上限（{total} 件）まで取得しました"
    else:
        message = f"条件に合う警告を {total} 件取得しました"
    listed_at = (run_dir / "issues.json").stat().st_mtime
    if time.time() - listed_at > OLD_LIST:
        message += f"（警告の取得: {time.strftime('%Y-%m-%d %H:%M', time.localtime(listed_at))}）"
    if status.get("note"):
        message += f"。{status['note']}"
    # A run that processes every CID fetches the warning paths once, here; a chat fetches them per CID (next).
    unfinished = [cid for cid, s in states.items() if not s or s["status"] in ("working", "rework")]
    return reply(run_dir, fetch=details_call(run, unfinished) if mode["all"] else [], counts=found,
                 message=message, next=run.steps(states))


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
    arguments = {"repo_root": run.repo.as_posix(), "cids": missing, "output_dir": (run.dir / "cid").as_posix()}
    return [{"tool": "coverity_get_issue_details", "arguments": arguments}] if missing else []


def work_copy(run: Run, slot: int):
    """(the repository, the work copy of the slot). start makes a copy that is missing or damaged again."""
    repo, copy = vcs.open_repo(run.repo), run.copy(slot)
    if not repo.is_copy(copy):
        raise Problem(f"作業用コピーがありません: {copy.as_posix()}。next のコマンド（start）を実行してください",
                      next=[start_command(run.repo, run.meta)])
    return repo, copy


def cmd_next(args) -> dict:
    run = Run(args.run)
    slot, batch = args.slot, args.batch
    repo, copy = work_copy(run, slot)
    if args.cid is not None and args.cid not in run.issues:
        raise Problem(f"CID {args.cid} は取得した警告の一覧にありません")
    # Before a CID is claimed: a copy that cannot be made clean must not cost a CID one of its two tries.
    repo.reset(copy, run.meta["base_revision"])
    states = run.states()
    named = states[args.cid] if args.cid is not None else None
    if named and named["status"] == "done" and not named.get("merged_into"):
        # the person asks for a saved CID again: like one that was sent back, without points
        states[args.cid] = {**named, "status": "rework", "rework": []}

    def free(state) -> bool:
        if state is None or state["status"] == "rework":
            return True
        if state["status"] == "failed":
            return args.cid is not None  # the person asks for this CID: once more
        return state["status"] == "working" and (state["slot"] == slot or age(state["claimed_at"]) > STALE)

    def own(cid) -> bool:  # the CID this slot was given and did not save comes first
        return bool(states[cid]) and states[cid]["status"] == "working" and states[cid]["slot"] == slot

    order = [args.cid] if args.cid is not None else sorted(run.issues, key=lambda cid: not own(cid))
    taken = sum(1 for s in states.values() if s and s.get("slot") == slot and s.get("batch") == batch)
    for cid in order if batch is None or taken < BATCH else []:
        if not free(states[cid]):
            continue
        state = claim(run, cid, states[cid], slot, batch)
        if state and state["status"] == "failed" and args.cid is not None:
            state = claim(run, cid, state, slot, batch)  # the person asks for this CID: it is not refused
        if state is None or state["status"] != "working":
            states[cid] = state or states[cid]
            continue
        issue = {**run.issues[cid], "file": map_path(copy, run.issues[cid].get("file", ""))}
        out = reply(run.dir, cid=cid, issue=issue, issue_file=(run.cid_dir(cid) / "issue.json").as_posix(),
                    work_dir=copy.as_posix(), result_file=(run.cid_dir(cid) / "result.json").as_posix(),
                    fetch=details_call(run, [cid]))
        if "rework" in state:  # sent back, or asked for again: the earlier fix is in the copy, to be changed
            patch = run.cid_dir(cid) / "fix.patch"
            applied = patch.is_file() and not repo.apply(copy, patch)
            out["rework"] = {"points": state["rework"], "patch_applied": applied}
        if state["retry"]:
            out["message"] = f"CID {cid} は、前回の結果が保存されていないため、もう一度処置します"
        out["next"] = [command("save", run.dir, "--cid", cid, "--slot", slot)]
        return out
    found = count(states)
    if args.cid is not None:
        done = (read_json(run.cid_dir(args.cid) / "state.json") or {}).get("status") == "done"
        message = f"CID {args.cid} は" + ("処置済みです" if done else "ほかのエージェントが処置中です")
    elif batch is not None:
        message = "この依頼で処置する CID は以上です"
    elif found["remaining"]:
        message = f"ほかのエージェントが処置中の CID が {found['working']} 件あります"
    else:
        message = "未処置の CID はありません"
    return reply(run.dir, cid=None, done=True, counts=found, message=message,
                 next=[] if batch is not None else [command("finish", run.dir)])


# ---- save, review, merge --------------------------------------------------------------------


def cmd_save(args) -> dict:
    run = Run(args.run)
    cid, slot, folder = args.cid, args.slot, run.cid_dir(args.cid)
    state = read_json(folder / "state.json")
    if not state or state["status"] != "working" or state["slot"] != slot:
        raise Problem(f"CID {cid} は処置中ではありません。next のコマンドで CID を受け取ってから保存してください",
                      next=[command("next", run.dir, "--slot", slot)])
    repo, copy = work_copy(run, slot)
    base = run.meta["base_revision"]
    files, patch = [], b""
    try:
        result = json.loads((folder / "result.json").read_bytes().decode("utf-8-sig"))
        problems = checks.check_result(result, cid)
        if not problems:
            files, patch = repo.diff(copy, base)
    except FileNotFoundError:
        problems = [f"result.json がありません: {(folder / 'result.json').as_posix()}"]
    except ValueError as exc:
        problems = [f"result.json を UTF-8 の JSON として読み取れません（{exc}）"]
    except vcs.VcsError as exc:
        problems = [str(exc)]
    if not problems:
        if result.get("fix") and not patch:
            problems.append(f"fix がありますが、work_dir のファイルが変更されていません: {copy.as_posix()}")
        if patch and not result.get("fix"):
            problems.append("work_dir のファイルが変更されていますが、fix がありません。fix.summary を記入するか、変更を元に戻してください")
        for rel in files:
            before = repo.original(copy, base, rel)
            if before is not None and (copy / rel).is_file():
                problems += checks.check_form(rel, before, (copy / rel).read_bytes())
        for n, item in enumerate(result["evidence"]):  # a place in the code before the fix
            rel = item["file"]
            inside = not Path(rel).is_absolute() and ".." not in Path(rel).parts
            before = repo.original(copy, base, rel) if inside else None
            if before is None:
                problems.append(f"evidence[{n}].file: {rel} が work_dir にありません。"
                                "リポジトリの最上位からの相対パス（区切りは /）で記入してください")
            elif item["line"] > len(before.splitlines()):
                problems.append(f"evidence[{n}].line: {rel} は {len(before.splitlines())} 行までです。"
                                "修正前のコードでの行を記入してください")
    if problems:
        return {**reply(run.dir, cid=cid, result_file=(folder / "result.json").as_posix(), work_dir=copy.as_posix(),
                        problems=problems, next=[command("save", run.dir, "--cid", cid, "--slot", slot)]),
                "ok": False, "error": "結果に不備があります。problems の内容を訂正して、もう一度保存してください"}
    result["deviation"].update(checks.coverity_fields(result, run.issues[cid].get("impact")))
    write_json(folder / "result.json", result)
    if patch:
        write_bytes(folder / "fix.patch", patch)
    else:
        (folder / "fix.patch").unlink(missing_ok=True)
    kept = {k: v for k, v in state.items() if k not in ("rework", "retry", "review", "error")}
    run.put(cid, {**kept, "status": "done", "files": files, "confidence": result["confidence"]})
    try:
        repo.reset(copy, base)  # a clean copy for whoever reads it next
    except vcs.VcsError:
        pass  # the result is stored; next makes the copy clean before it hands out a CID
    if not run.meta["all"]:
        summarize(run)
    states = run.states()
    batch = ["--batch", state["batch"]] if state.get("batch") else []
    more = batch or count(states)["remaining"]  # a worker of a round asks next until it answers done
    return reply(run.dir, cid=cid, files=files, patch_file=(folder / "fix.patch").as_posix() if patch else None,
                 counts=count(states),
                 next=[command("next", run.dir, "--slot", slot, *batch)] if more else run.steps(states))


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
    write_json(folder / "review.json", {"outcome": outcome, "points": args.point, "confidence_cap": args.cap})
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
            f"CID {', '.join(map(str, members))} と同じ原因です。"
            "すべての CID の警告を解消する修正と逸脱コメントを1つ作成してください", *args.point])
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
    todo = unreviewed(states) if run.meta["review"] else []
    batches = [{"slot": k + 1, "work_dir": run.copy(k + 1).as_posix(), "cids": todo[k::slots]}
               for k in range(slots) if todo[k::slots]]
    single = [cid for cid, s in states.items()
              if s and s["status"] == "done" and not s.get("merged_into") and not s.get("members")]
    merge = command("merge", run.dir, "--leader", "<CID>", "--members", "<CID,CID>", "[--rework]")
    return reply(run.dir, counts=count(states), merge_candidates=merge_candidates(run, single), merge_command=merge,
                 review_batches=batches, next=run.steps(states))


def summarize(run: Run) -> str:
    """Write summary.md: every fetched CID, and both outputs (fix code, deviation comment) of every
    finished one. The review and the build check are shown when the run has them. Returns the table."""
    states = run.states()
    built = read_json(run.dir / "build.json") or {}
    built = built.get("cids", {}) if built.get("status") == "done" else None
    shown = [name for name, has in (("検証", run.meta["review"]), ("ビルド", built is not None)) if has]
    headers = ["CID", "チェッカー", "場所", "判定", "推奨", "確信度", *shown, "修正コード", "逸脱コメント"]
    rows, sections = [], []
    for cid, issue in run.issues.items():
        state = states[cid]
        place = f"{issue.get('file', '')}:{issue.get('line', '')}"
        lead = [str(cid), str(issue.get("checker", "")), place]
        if state is None or state["status"] in ("working", "rework"):
            rows.append([*lead, "未処置", *["-"] * (len(headers) - 4)])
        elif state["status"] == "failed":
            rows.append([*lead, "失敗", *["-"] * (len(headers) - 4)])
            sections.append(f"## CID {cid}（{lead[1]}、{place}）\n\n処置できませんでした: {state.get('error', '')}\n")
        if state is None or state["status"] != "done" or state.get("merged_into"):
            continue
        result = read_json(run.cid_dir(cid) / "result.json") or {}
        deviation = result.get("deviation") or {}
        patch = run.cid_dir(cid) / "fix.patch"
        points = (read_json(run.cid_dir(cid) / "review.json") or {}).get("points") or []
        if state.get("members"):
            lead[0] += f"（同じ原因: {', '.join(map(str, state['members']))}）"
        verdict, level = VERDICT.get(result.get("verdict"), "-"), LEVEL.get(state.get("confidence"), "-")
        recommend = RECOMMEND.get(result.get("recommendation"), "-")
        checked = {"検証": REVIEW.get(state.get("review"), "-"), "ビルド": BUILD.get((built or {}).get(str(cid)), "-")}
        rows.append([*lead, verdict, recommend, level, *(checked[name] for name in shown),
                     ", ".join(state.get("files") or []) or "なし", str(deviation.get("comment", ""))])
        if patch.is_file():
            fix = [str((result.get("fix") or {}).get("summary", "")), "", "````diff", vcs.text(patch.read_bytes()),
                   "````"]
        else:
            fix = [f"修正コードはありません。理由: {result.get('no_fix_reason', '')}"]
        sections.append("\n".join([
            f"## CID {lead[0]}（{lead[1]}、{place}）", "", f"- 判定: {verdict}（確信度: {level}）",
            f"- 推奨: {recommend}", f"- 概要: {result.get('summary', '')}",
            *[f"- {name}: {checked[name]}" for name in shown], *[f"- 検証の指摘: {p}" for p in points], "", "根拠:",
            *[f"- {e.get('file')}:{e.get('line')} {e.get('note')}" for e in result.get("evidence") or []], "",
            "### 修正コード", "", *fix, "", "### 逸脱コメント", "", str(deviation.get("comment", "")), "",
            f"Classification: {deviation.get('classification')}／Action: {deviation.get('action')}／"
            f"Severity: {deviation.get('severity')}", ""]))
    table = "\n".join("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in row) + " |"
                      for row in [headers, ["---"] * len(headers), *rows])
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
    fixes = []
    for cid, state in states.items():
        patch = run.cid_dir(cid) / "fix.patch"
        if state and state["status"] == "done" and not state.get("merged_into") and patch.is_file():
            digest = hashlib.sha256(patch.read_bytes()).hexdigest()  # a fix that was made again is built again
            fixes.append({"cid": cid, "files": state["files"], "sha256": digest})
    if cfg["check_fixes"] and fixes:
        need_command(cfg, "build.check_fixes")
        status = job.ensure(run.dir, "build", {"repo": str(run.repo), "base_revision": run.meta["base_revision"],
                                               "command": cfg["command"], "fixes": fixes}, time.monotonic() + WAIT)
        if status["status"] == "running":
            return reply(run.dir, message="修正を適用してビルドしています", log=status["log"],
                         next=[command("finish", run.dir)])
        if status["status"] == "failed":
            raise Problem(f"修正のビルド確認を実行できませんでした: {status['error']}", log=status["log"])
    table = summarize(run)
    left = remove_work(run.dir, run.repo) if not found["remaining"] else ""
    return reply(run.dir, counts=found, summary_file=(run.dir / "summary.md").as_posix(), table=table,
                 **({"message": f"作業用コピーが残っています。{left}"} if left else {}), next=[])


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
        place = "repo" if args.command == "start" else "run"
        setattr(args, place, Path(getattr(args, place)).resolve())
        os.chdir(SCRIPT.parent)  # Windows cannot delete a work copy that is the current folder of a process
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
            what = " ".join(f"{k}={getattr(args, k)}" for k in ("slot", "cid", "leader", "outcome")
                            if getattr(args, k, None))
            with open(Path(run_dir, "log.txt"), "a", encoding="utf-8", newline="\n") as log:
                log.write(f"{job.now()} {args.command} {what} -> {'ok' if out['ok'] else out['error']}\n")
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(main())
