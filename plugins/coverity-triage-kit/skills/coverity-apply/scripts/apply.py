# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""After the person's OK: take the chosen fixes into the repository and prepare the entries for Coverity.

    uv run --native-tls --quiet apply.py <command> ...

  plan   --repo <repo> [--run <dir>] [--set <CID>=fix|deviation|none]..   one choice per CID, the table to agree to
  apply  --repo <repo> [--run <dir>]                                      do what apply-plan.json says

Every command prints one JSON object (UTF-8): ``ok``, ``message`` for the person, ``next`` with the full
command line of the continuation, ``call`` with the tool call that registers the entries in Coverity.
The result folder is the state. This script writes apply-plan.json, applied.json and coverity-entries.json
(+ .sha256) into it. git: one branch per result folder, one commit per fix, pushed. svn: svn patch.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml

SCRIPT = Path(__file__).resolve()
NETWORK = 120  # seconds a fetch or a push may take
VERDICT = {"true_bug": "本物のバグ", "false_positive": "誤検知", "intentional": "意図的", "undetermined": "判定不能"}
LEVEL = {"high": "高", "medium": "中", "low": "低"}
CHOICE = {"fix": "修正を取り込む", "deviation": "逸脱コメントを登録する", "none": "何もしない"}
NOT_ITEM = {"failed": "処置に失敗", "unfinished": "未処置"}


class Problem(Exception):
    """Something the person or the agent can correct. Printed as {"ok": false, "error": ...}."""

    def __init__(self, message: str, **fields):
        super().__init__(message)
        self.fields = fields


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise Problem(f"引数が正しくありません（{message}）")


def write(path: Path, data) -> bytes:
    """Write bytes, or anything else as JSON, through a temporary file and a rename (Windows refuses the
    rename while the file is read). Returns the bytes written."""
    raw = data if isinstance(data, bytes) else (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(raw)
    for attempt in range(60):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(0.05)
    return raw


def read_json(path: Path) -> dict:
    """A JSON object (UTF-8, a BOM is allowed). {} when the file is missing or is not one."""
    for _ in range(20):
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except PermissionError:  # Windows: the file is being replaced at this moment
            time.sleep(0.05)
        except (OSError, ValueError):
            break
    return {}


def text(data: bytes) -> str:
    """The output of git or svn, for showing: UTF-8, or cp932 on a Japanese Windows."""
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return data.decode("cp932", "replace").strip()


def cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def execute(args: list[str], cwd: Path, check: bool = True, timeout: float | None = None,
            data: bytes | None = None, env: dict | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run a command without a shell. The output stays bytes. A command that does not end in time has failed."""
    feed = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
    try:
        proc = subprocess.run(args, cwd=str(cwd), env=env, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **feed)
    except FileNotFoundError:
        raise Problem(f"{args[0]} コマンドが見つかりません。インストールして、PATH を設定してください") from None
    except subprocess.TimeoutExpired:
        late = f"{' '.join(args[:4])} が {timeout:.0f} 秒以内に終了しませんでした"
        proc = subprocess.CompletedProcess(args, 1, b"", late.encode())
    if check and proc.returncode != 0:
        raise Problem(f"{' '.join(args[:4])} が失敗しました: {text(proc.stderr or proc.stdout)[:500]}")
    return proc


def git(repo: Path, *args: str, check: bool = False, **options) -> subprocess.CompletedProcess[bytes]:
    """Run git in the repository, or in the worktree ``cwd``. It answers no question on a terminal."""
    place = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")  # git works where it is run
    env = {**{k: v for k, v in os.environ.items() if k not in place}, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    return execute(["git", "-c", "core.longpaths=true", *args], options.pop("cwd", repo), check, env=env, **options)


# ---- the result folder ----------------------------------------------------------------------


def fits(decision, patch: bool, state_file: Path) -> bool:
    """A choice made on the screen counts while it still fits the item: a fix needs fix.patch, and
    state.json must not have been written after the choice (a new save, review or merge)."""
    try:
        at = datetime.fromisoformat(decision["at"]).timestamp()
        return (decision["decision"] in CHOICE and (patch or decision["decision"] != "fix")
                and int(state_file.stat().st_mtime) <= at)
    except (KeyError, TypeError, ValueError, OSError):
        return False


class Run:
    """One result folder. ``rows``: one per item to decide on, failed CID and unfinished CID, in the
    order of issues.json. A CID that was merged into another has no row (``leader`` names its item)."""

    def __init__(self, repo: Path, folder: Path):
        self.repo, self.dir = repo, folder
        self.meta = read_json(folder / "run.json")
        self.issues = {i["cid"]: i for i in read_json(folder / "issues.json").get("issues") or []
                       if isinstance(i, dict) and "cid" in i}
        self.applied = read_json(folder / "applied.json")
        self.fixed = self.applied.get("fixes") or {}  # "<CID>": the fixes that are taken in
        self.written = read_json(folder / "written.json").get("written") or {}
        decisions = read_json(folder / "decisions.json").get("decisions") or {}
        build = read_json(folder / "build.json")
        built = (build.get("cids") or {}) if build.get("status") == "done" else {}
        self.rows, self.leader = [], {}
        for cid in self.issues:
            state, result = read_json(self.file(cid, "state.json")), read_json(self.file(cid, "result.json"))
            status = state.get("status")
            if status == "done" and state.get("merged_into"):
                self.leader[cid] = state["merged_into"]
            elif status == "done" and result:
                self.rows.append(self.item(cid, state, result, decisions.get(str(cid)), built.get(str(cid))))
            else:  # a finished CID whose result cannot be read has no outputs: it is shown as failed
                self.rows.append({"cid": cid, "kind": "failed" if status in ("failed", "done") else "unfinished"})
        self.items = {row["cid"]: row for row in self.rows if row["kind"] == "item"}

    def file(self, cid: int, name: str) -> Path:
        return self.dir / "cid" / str(cid) / name

    def item(self, cid: int, state: dict, result: dict, decision, build) -> dict:
        cids = [cid, *(state.get("members") or [])]  # the members share the fix and the comment
        patch, review = self.file(cid, "fix.patch").is_file(), state.get("review")
        reasons = {"確信度が低いため": state.get("confidence") == "low", "検証で要確認のため": review == "flag",
                   "未検証のため": review == "unverified" or (self.meta.get("review") and not review),
                   "ビルドに失敗したため": build == "failed"}
        reason = next((sentence for sentence, applies in reasons.items() if applies), "")
        advice = result.get("recommendation")
        default = advice if not reason and (advice == "deviation" or advice == "fix" and patch) else "none"
        decision = decision if fits(decision, patch, self.file(cid, "state.json")) else {}
        deviation = result.get("deviation") or {}
        handled = "applied" if str(cid) in self.fixed else "written" if self.registered(cids) else ""
        return {"cid": cid, "kind": "item", "cids": cids, "state": state, "patch": patch, "default": default,
                "reason": reason, "decision": decision.get("decision"), "handled": handled,
                "verdict": result.get("verdict"), "summary": str((result.get("fix") or {}).get("summary", "")),
                "deviation": {**{key: deviation.get(key) for key in ("classification", "action", "severity")},
                              "comment": decision.get("comment") or deviation.get("comment", "")}}

    def registered(self, cids: list[int]) -> bool:
        return all(str(cid) in self.written for cid in cids)

    def waiting(self) -> list[int]:
        """The fixes that are taken in and not yet registered in Coverity as Bug / Fix Required."""
        return [cid for cid, row in self.items.items() if str(cid) in self.fixed and not self.registered(row["cids"])]

    def basis(self) -> str:
        """SHA-256 over what a plan is made from: decisions.json and the files of every item."""
        digest = hashlib.sha256()
        for path in [self.dir / "decisions.json", *(self.file(cid, name) for cid in self.items
                                                    for name in ("state.json", "result.json", "fix.patch"))]:
            data = path.read_bytes() if path.is_file() else None
            digest.update(f"{path.relative_to(self.dir).as_posix()} {-1 if data is None else len(data)}\n".encode())
            digest.update(data or b"")
        return digest.hexdigest()

    def command(self, name: str, *extra: str) -> str:
        """The full command line of a step. Paths are written with forward slashes (Windows accepts them)."""
        return " ".join(["uv run --native-tls --quiet", f'"{SCRIPT.as_posix()}"', name, "--repo",
                         f'"{self.repo.as_posix()}"', "--run", f'"{self.dir.as_posix()}"', *extra])


def open_run(args) -> Run:
    """The result folder of --run, or the newest one of the repository (the last by name)."""
    repo = Path(args.repo).resolve()
    if args.run:
        folder = Path(args.run).resolve()
        if read_json(folder / "run.json").get("repo") != repo.as_posix():
            raise Problem(f"このリポジトリの結果フォルダーではありません: {folder.as_posix()}")
    else:
        path = repo / ".coverity-triage" / "config.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise Problem(f"設定ファイルがありません: {path.as_posix()}。"
                          "/coverity-triage-kit:coverity-setup <URL> <ストリーム> を実行してください") from None
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise Problem(f"設定ファイルを読み取れません: {path.as_posix()}（{exc}）") from None
        out = repo / str((data.get("output_dir") if isinstance(data, dict) else "") or "../coverity-triage-out")
        runs = [p for p in out.iterdir() if read_json(p / "run.json").get("repo") == repo.as_posix()] \
            if out.is_dir() else []
        folder = max(runs).resolve() if runs else None
    run = Run(repo, folder) if folder else None
    if not run or not run.items:
        raise Problem("処置済みの CID がありません")
    args.run_dir = folder.as_posix()  # every reply names the folder
    return run


def cmd_plan(args) -> dict:
    run = open_run(args)
    chosen, problems = {}, []
    for given in args.set:  # what the person said in the chat
        number, _, choice = given.partition("=")
        cid = int(number) if number.isdigit() else None
        row = run.items.get(cid)
        if cid is None or choice not in CHOICE:
            problems.append(f"--set は <CID>=fix|deviation|none の形で指定してください（指定: {given}）")
        elif cid in run.leader:
            problems.append(f"CID {cid} は CID {run.leader[cid]} に統合されています。{run.leader[cid]} を指定してください")
        elif cid not in run.issues:
            problems.append(f"CID {cid} は取得した警告の一覧にありません")
        elif row is None:
            failed = any(r["cid"] == cid and r["kind"] == "failed" for r in run.rows)
            problems.append(f"CID {cid} は{'処置に失敗している' if failed else '未処置の'}ため、選択できません")
        elif row["handled"]:
            problems.append(f"CID {cid} は{'取り込み済み' if row['handled'] == 'applied' else '登録済み'}です")
        elif choice == "fix" and not row["patch"]:
            problems.append(f"CID {cid} には修正コードがないため、fix は指定できません")
        else:
            chosen[cid] = choice
    if problems:
        raise Problem("選択の指定に訂正が必要な項目があります", problems=problems,
                      next=[run.command("plan", *(f"--set {cid}={choice}" for cid, choice in chosen.items()))])
    # What an earlier OK did not complete: a push (the branch has fixes and there is an origin), entries.
    unpushed = bool(run.meta.get("vcs") == "git" and run.fixed and not run.applied.get("pushed")
                    and git(run.repo, "config", "--get", "remote.origin.url").stdout.strip())
    waiting = len(run.waiting())
    n = {**dict.fromkeys(("fix", "deviation", "none", "done", "failed", "unfinished"), 0), "left": unpushed + waiting}
    plan = {"created_at": datetime.now().isoformat(timespec="seconds"), "basis": run.basis(),
            "set": {str(cid): choice for cid, choice in chosen.items()}, "fixes": [], "deviations": [], "none": []}
    table = [["CID", "判定", "確信度", "修正コード", "逸脱コメント", "選択", "備考"], ["---"] * 7]
    for row in run.rows:
        cid = row["cid"]
        if row["kind"] != "item":
            n[row["kind"]] += 1
            table.append([cid, "-", "-", "-", "-", "-", NOT_ITEM[row["kind"]]])
            continue
        deviation, choice = row["deviation"], None
        if row["handled"] == "written":
            remark = "登録済み"
        elif row["handled"]:
            remark = "取り込み済み" + ("（push が残っています）" if unpushed else "" if run.registered(row["cids"])
                                 else "（Coverity への登録が残っています）")
        elif cid in chosen:
            choice, remark = chosen[cid], "チャットで指定"
        elif row["decision"]:
            choice, remark = row["decision"], "画面で選択"
        else:
            choice, remark = row["default"], row["reason"]
        n[choice or "done"] += 1
        if choice == "deviation":
            plan["deviations"].append({"cids": row["cids"], **deviation})
        elif choice:
            plan["fixes" if choice == "fix" else "none"].append(cid)
        same = f"（同じ原因: {', '.join(map(str, row['cids'][1:]))}）" if row["cids"][1:] else ""
        values = f"（{deviation['classification']}／{deviation['action']}／{deviation['severity']}）"
        table.append([f"{cid}{same}", VERDICT.get(row["verdict"], "-"), LEVEL.get(row["state"].get("confidence"), "-"),
                      ", ".join(row["state"].get("files") or []) if row["patch"] else "なし",
                      deviation["comment"] + values, CHOICE.get(choice, "-"), remark])
    write(run.dir / "apply-plan.json", plan)
    out = {"vcs": run.meta.get("vcs"), "plan_file": (run.dir / "apply-plan.json").as_posix(),
           "summary_file": (run.dir / "summary.md").as_posix(),
           "table": "\n".join("| " + " | ".join(map(cell, line)) + " |" for line in table), "counts": n}
    parts = [f"修正の取り込み（{n['fix']} 件）" if n["fix"] else "",
             f"逸脱コメントの登録（{n['deviation']} 件）" if n["deviation"] else "",
             f"ブランチ {run.applied.get('branch')} の push" if unpushed else "",
             f"取り込み済みの修正の Coverity への登録（{waiting} 件）" if waiting else ""]
    if not any(parts):
        return {**out, "message": "取り込む修正と、登録する逸脱コメントはありません。"
                                  "変更するときは「20003 は修正」のように入力してください", "next": []}
    registers = "取り込んだ修正は、Coverity に「Bug／Fix Required」として登録します。" if n["fix"] else ""
    return {**out, "question": f"この内容で、{'、'.join(filter(None, parts))}を行います。{registers}"
                               "よろしければ「OK」と入力してください。"
                               "変更するときは「20003 は逸脱、20007 は何もしない、ほかは OK」のように入力してください",
            "next": [run.command("apply")]}


def record(run: Run, cid: int, **fields) -> None:
    """applied.json, written after each fix that is in."""
    at = datetime.now().isoformat(timespec="seconds")
    run.fixed[str(cid)] = {**fields, "files": run.items[cid]["state"].get("files") or [], "at": at}
    write(run.dir / "applied.json", {**run.applied, "fixes": run.fixed})


def take_git(run: Run, todo: list[int], fixes: dict) -> dict | None:
    """One commit per fix on the one branch of this result folder, made in a temporary worktree outside
    the repository (the person's working tree, index and current branch are not touched); then the push,
    without force, and the text of the pull request (the person creates it)."""
    repo, branch, base = run.repo, f"coverity-fix/{run.dir.name}", str(run.meta.get("base_branch") or "")
    remote = text(git(repo, "config", "--get", "remote.origin.url").stdout)
    if todo:
        if f"branch refs/heads/{branch}" in text(git(repo, "worktree", "list", "--porcelain").stdout).splitlines():
            raise Problem(f"ブランチ {branch} がチェックアウトされています。別のブランチに切り替えてから、"
                          "もう一度「修正を取り込んで」と入力してください")
        if remote:
            git(repo, "fetch", "--quiet", "origin", base, timeout=NETWORK)  # no connection: what was fetched before
        refs = [f"refs/heads/{branch}", *([f"refs/remotes/origin/{base}"] if remote else []), f"refs/heads/{base}"]
        found = (text(git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}").stdout) for ref in refs)
        start = next(filter(None, found), "")  # the branch is continued when it exists
        if not start:
            raise Problem(f"取り込み先のブランチ {base} が見つかりません")
        tmp = Path(tempfile.mkdtemp(prefix="coverity-apply-"))
        tree = tmp / "w"
        try:
            git(repo, "worktree", "add", "--quiet", "--detach", "--force", str(tree), start, check=True)
            for cid in todo:
                row = run.items[cid]
                subject = f"Coverity CID {', '.join(map(str, row['cids']))} の修正（{run.issues[cid].get('checker', '')}）"
                tried = git(repo, "apply", "--index", "--3way", "--whitespace=nowarn", str(run.file(cid, "fix.patch")),
                            cwd=tree)
                if tried.returncode != 0:
                    reason = f"取り込み先のコードが変更されているため、修正を適用できませんでした（{text(tried.stderr)[:300]}）"
                elif git(repo, "diff", "--cached", "--quiet", cwd=tree).returncode == 0:
                    # Nothing to commit. An earlier fix on this branch made the same change: it is taken in.
                    made_here = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").stdout
                    reason = "" if made_here else "取り込み先のブランチに、同じ変更が既にあります"
                else:
                    made = git(repo, "commit", "--quiet", "--no-verify", "-F", "-", cwd=tree,
                               data=f"{subject}\n\n{row['summary']}\n".encode())
                    reason = made.returncode and f"コミットできませんでした（{text(made.stderr or made.stdout)[:300]}）"
                if reason:
                    fixes["failed"][str(cid)] = reason
                    git(repo, "reset", "--quiet", "--hard", cwd=tree, check=True)
                    continue
                commit = text(git(repo, "rev-parse", "HEAD", cwd=tree, check=True).stdout)
                git(repo, "update-ref", f"refs/heads/{branch}", commit, check=True)
                run.applied = {"vcs": "git", "branch": branch, "base_branch": base, "pushed": False}
                record(run, cid, commit=commit)
                fixes["applied"].append(cid)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            git(repo, "worktree", "prune")
    if not run.fixed:
        return None
    pushed, error, pull = bool(run.applied.get("pushed")), None, None
    if remote and not pushed:
        push = git(repo, "push", "--quiet", "origin", branch, timeout=NETWORK)
        if push.returncode == 0:
            run.applied["pushed"] = pushed = True
            write(run.dir / "applied.json", {**run.applied, "fixes": run.fixed})
        else:
            error = text(push.stderr)[-500:] or "git push が失敗しました"
    if pushed:
        stream = run.meta.get("stream", "")
        title = f"Coverity の警告の修正（{stream}、{len(run.fixed)} 件）"
        lines = [[", ".join(map(str, row["cids"])), run.issues[cid].get("checker", ""),
                  f"{run.issues[cid].get('file', '')}:{run.issues[cid].get('line', '')}", row["summary"]]
                 for cid, row in run.items.items() if str(cid) in run.fixed]
        body = "\n".join([f"Coverity Connect のストリーム {stream} で検出された警告の修正です。", "",
                          "| CID | チェッカー | 場所 | 修正の内容 |", "| --- | --- | --- | --- |",
                          *("| " + " | ".join(map(cell, line)) + " |" for line in lines)]) + "\n"
        web = (re.match(r"https?://(?:[^@/]+@)?([^/]+)/([^/]+)/([^/]+?)(?:\.git)?/?$", remote)
               or re.match(r"(?:ssh://)?[^@/]+@([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote))
        url = web and (f"https://{web[1]}/{web[2]}/{web[3]}/compare/{quote(base)}...{quote(branch)}?expand=1"
                       f"&title={quote(title, safe='')}&body={quote(body[:3000], safe='')}")
        pull = {"title": title, "body": body, "compare_url": url}
    return {"branch": branch, "base_branch": base, "pushed": pushed, "push_error": error, "pr": pull}


def svn_patch(repo: Path, patch: Path, files: list[str], *dry: str) -> str:
    """'' when svn patch takes every file of the fix, else what svn printed. svn prints one status line
    per file: with C for a conflict (a rejected hunk), and none for a target it skips. The lines are
    counted, not compared with the file names: svn prints a name the console cannot show as ?\\NNN."""
    proc = execute(["svn", "--non-interactive", "patch", *dry, str(patch), "."], repo, check=False)
    output = text(proc.stdout + proc.stderr)
    status = re.findall(r"^([ADUGC][ADUGC ]| [ADUGC]) +\S", output, re.M)
    taken = proc.returncode == 0 and len(status) >= len(files) and not any("C" in columns for columns in status)
    return "" if taken else output or "svn patch が失敗しました"


def take_svn(run: Run, todo: list[int], fixes: dict) -> dict | None:
    """Patch the fixes into the person's working copy; nothing is committed. svn patch applies what it
    can, so a dry run comes first: a fix with a conflict is left out as a whole."""
    for cid in todo:
        patch, files = run.file(cid, "fix.patch"), run.items[cid]["state"].get("files") or []
        refused = svn_patch(run.repo, patch, files, "--dry-run") or svn_patch(run.repo, patch, files)
        if refused:
            fixes["failed"][str(cid)] = f"作業コピーのコードが変更されているため、修正を適用できませんでした（{refused[:300]}）"
            continue
        run.applied = {"vcs": "svn"}
        record(run, cid)
        fixes["applied"].append(cid)
    return {"files": sorted({name for fix in run.fixed.values() for name in fix["files"]})} if run.fixed else None


def cmd_apply(args) -> dict:
    run = open_run(args)
    plan = read_json(run.dir / "apply-plan.json")
    if plan.get("basis") != run.basis():  # also: there is no plan yet
        sets = (f"--set {cid}={choice}" for cid, choice in (plan.get("set") or {}).items())
        return {"ok": False, "changed": True, "next": [run.command("plan", *sets)],
                "error": "一覧を表示した後に、結果または選択が変更されています。もう一度、一覧を確認してください"}
    kind, pushed_before = run.meta.get("vcs"), bool(run.applied.get("pushed"))
    fixes = {"applied": [], "skipped": [cid for cid in plan["fixes"] if str(cid) in run.fixed], "failed": {}}
    todo = [cid for cid in plan["fixes"] if str(cid) not in run.fixed]
    place = (take_git if kind == "git" else take_svn)(run, todo, fixes)
    # The entries for Coverity, after the fixes: the chosen deviations and every fix that is taken in,
    # each while it is not registered yet (written.json).
    where = f"修正をブランチ {place['branch']} にコミットしました。" if kind == "git" and place \
        else "修正を作業コピーに適用しました。"
    deviations = [entry for entry in plan["deviations"] if not run.registered(entry["cids"])]
    taken = [{"cids": run.items[cid]["cids"], "classification": "Bug", "action": "Fix Required",
              "severity": run.items[cid]["deviation"]["severity"], "comment": (where + run.items[cid]["summary"])[:200]}
             for cid in run.waiting()]
    target, entries = run.dir / "coverity-entries.json", [*deviations, *taken]
    if entries:
        raw = write(target, {"stream": run.meta.get("stream"), "entries": entries})
        write(Path(f"{target}.sha256"), (hashlib.sha256(raw).hexdigest() + "\n").encode("ascii"))
    else:
        for path in (target, Path(f"{target}.sha256")):
            path.unlink(missing_ok=True)
    said, done = [], len(fixes["applied"])
    if kind == "git" and place:
        if done:
            said.append(f"修正 {done} 件をブランチ {place['branch']} にコミット"
                        + ("して push しました" if place["pushed"] else "しました"))
        elif place["pushed"] and not pushed_before:
            said.append(f"ブランチ {place['branch']} を push しました")
        if place["push_error"]:
            said.append("push に失敗しました")
    elif done:
        said.append(f"修正 {done} 件を作業コピーに適用しました。内容を確認して、コミットしてください")
    if fixes["failed"]:
        said.append(f"取り込めなかった修正が {len(fixes['failed'])} 件あります")
    kinds = [f"逸脱コメント {len(deviations)} 件" if deviations else "", f"取り込んだ修正 {len(taken)} 件" if taken else ""]
    if entries:
        said.append(f"続けて、{'と、'.join(filter(None, kinds))}を Coverity に登録します")
    call = {"tool": "coverity_update_triage",
            "arguments": {"repo_root": run.repo.as_posix(), "entries_file": target.as_posix()}}
    return {"fixes": fixes, **({kind: place} if place else {}), "entries_file": target.as_posix() if entries else None,
            "entries": len(entries), "call": [call] if entries else [],
            "message": "。".join(said) or "取り込みと登録は完了しています", "next": []}


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="apply.py")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("plan", "apply"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", required=True)
        cmd.add_argument("--run")
    sub.choices["plan"].add_argument("--set", action="append", default=[])
    args, code = None, 0
    try:
        args = parser.parse_args(argv)
        out = globals()[f"cmd_{args.command}"](args)
    except Problem as exc:
        out = {"ok": False, "error": str(exc), **exc.fields}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        out, code = {"ok": False, "error": f"予期しないエラーが発生しました: {type(exc).__name__}: {exc}"}, 1
    folder = getattr(args, "run_dir", None)
    out = {"ok": True, **({"run_dir": folder} if folder else {}), **out}
    out.setdefault("next", [])
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode())
    return code


if __name__ == "__main__":
    sys.exit(main())
