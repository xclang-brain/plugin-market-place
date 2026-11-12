# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""After the person's OK: take the chosen fixes into the repository and prepare the entries for Coverity.

    uv run --native-tls --quiet apply.py <command> ...

  plan   --repo <repo> [--run <dir>] [--set <CID>=fix|deviation|none]..   one choice per CID, the table to agree to
  apply  --repo <repo> [--run <dir>]                                      do what apply-plan.json says

Every command prints one JSON object (UTF-8): ``ok``, ``message`` for the person, ``next`` with the full
command lines to run now, without a word from the person (only ever a plan line), ``after_ok`` with the
apply line, to run once the person has answered OK, ``to_change`` with the plan line for an answer that
changes a choice (a template: one --set per changed CID), ``call`` with the tool call that registers the
entries in Coverity, and ``if_not_called`` with the sentence for the person when that call was not made.
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
CHOICE = {"fix": "修正を取り込む", "deviation": "逸脱コメントを登録する", "none": "保留にする"}
# An item that is neither taken in nor registered with its comment is marked in Coverity: it is not fetched again.
PENDING = {"classification": "Pending", "action": "Undecided", "severity": "Unspecified"}
OWN = "利用者が保留にしたため"  # the reason of 保留 for a choice of the chat or the screen
NOT_ITEM = {"failed": "処置に失敗", "unfinished": "未処置"}
REFUSED = ("取り込み先のコードに適用できませんでした。先に取り込んだ修正と同じ箇所を変更しているときは、"
           "その修正を{}した後に、もう一度処置してください")  # {}: マージ (git), コミット (svn)
STALE = "。作業コピーが最新でないときは、svn update を実行した後に、「修正を取り込んで」と入力してください"  # svn
REST = "\n\n未処置の CID が {} 件あります。続けるときは「次を処置して」と入力してください"  # below the table of a chat run
NOT_CALLED = "Coverity への登録は行われていません。「修正を取り込んで」と入力すると、登録します"  # the write call was refused or failed


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
    for attempt in range(200):  # Windows refuses the rename while another process reads the file
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 199:
                raise
            time.sleep(0.05)
    return raw


def read_json(path: Path) -> dict:
    """A JSON object (UTF-8, a BOM is allowed). {} when the file is missing or is not one. A file that stays
    locked is an error, never {}: a locked written.json must not read as nothing registered."""
    for attempt in range(200):
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except PermissionError:  # Windows: the file is being replaced at this moment
            if attempt == 199:
                raise
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


def stamp(at: str) -> float:
    return datetime.fromisoformat(at).timestamp()


def fits(decision, patch: bool, state_file: Path) -> bool:
    """A choice made on the screen counts while it still fits the item: a fix needs fix.patch, and
    state.json must not have been written after the choice (a new save, review or merge)."""
    try:
        return (decision["decision"] in CHOICE and (patch or decision["decision"] != "fix")
                and int(state_file.stat().st_mtime) <= stamp(decision["at"]))
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
                "reason": reason, "decision": decision.get("decision"), "decided": decision.get("at"),
                "handled": handled, "verdict": result.get("verdict"),
                "summary": str((result.get("fix") or {}).get("summary", "")),
                "finding": re.match(r"[^。]*。?", str(result.get("summary") or ""))[0],  # its first sentence
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


def agreed(run: Run) -> dict:
    """The choices of the chat that got their OK: ``set`` of the last plan when it was carried out
    (applied.json is not older than it), else what that plan had taken over itself. One stands while it
    fits its item, as a choice on the screen must, and no choice was made on the screen after it."""
    file, kept = run.dir / "apply-plan.json", {}
    old = read_json(file)
    try:
        carried = (run.dir / "applied.json").stat().st_mtime >= file.stat().st_mtime
    except OSError:
        carried = False
    for cid, choice in (old.get("set" if carried else "agreed") or {}).items():
        row = run.items.get(int(cid)) if cid.isdigit() else None
        made = {"decision": choice, "at": old.get("created_at")}
        if (row and not row["handled"] and fits(made, row["patch"], run.file(row["cid"], "state.json"))
                and not (row["decision"] and stamp(row["decided"]) > stamp(made["at"]))):
            kept[row["cid"]] = choice
    return kept


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
    # For an answer that changes a choice: this line with one --set per changed CID. It keeps what was said before.
    change = run.command("plan", *(f"--set {cid}={choice}" for cid, choice in chosen.items()),
                         "--set <CID>=<fix|deviation|none>")
    earlier = agreed(run)
    chosen = {**earlier, **chosen}  # what the person says now replaces it
    branch, kind = f"coverity-fix/{run.dir.name}", run.meta.get("vcs")
    origin = kind == "git" and git(run.repo, "config", "--get", "remote.origin.url").stdout.strip()
    # What an earlier OK did not complete: a push (the branch has fixes and there is an origin), entries.
    unpushed = bool(origin and run.fixed and not run.applied.get("pushed"))
    waiting = len(run.waiting())
    n = {**dict.fromkeys(("fix", "deviation", "none", "done", "failed", "unfinished"), 0), "left": unpushed + waiting}
    plan = {"created_at": datetime.now().isoformat(), "basis": run.basis(),  # exact: agreed() compares it
            "set": {str(cid): choice for cid, choice in chosen.items()},
            "agreed": {str(cid): choice for cid, choice in earlier.items()}, "fixes": [], "deviations": [], "none": []}
    table = [["CID", "判定", "確信度", "修正コード", "逸脱コメント", "選択", "備考"], ["---"] * 7]
    for row in run.rows:
        cid = row["cid"]
        if row["kind"] != "item":
            n[row["kind"]] += 1
            if row["kind"] == "failed" or run.meta.get("all"):  # a chat run: the unfinished CIDs are counted below
                table.append([cid, "-", "-", "-", "-", "-", NOT_ITEM[row["kind"]]])
            continue
        deviation, choice = row["deviation"], None
        if row["handled"] == "written":
            remark = "登録済み"
        elif row["handled"]:
            remark = "取り込み済み" + ("（push が残っています）" if unpushed else "" if run.registered(row["cids"])
                                 else "（Coverity への登録が残っています）")
        elif cid in chosen:
            choice, remark, why = chosen[cid], "チャットで指定", OWN
        elif row["decision"]:
            choice, remark, why = row["decision"], "画面で選択", OWN
        else:
            choice, remark, why = row["default"], row["reason"], row["reason"]
        n[choice or "done"] += 1
        if choice == "deviation":
            plan["deviations"].append({"cids": row["cids"], **deviation})
        elif choice == "fix":
            plan["fixes"].append(cid)
        elif choice:  # 保留: one line, the reason and what the investigation found
            said = "".join(f"{part}。" for part in ("保留", why) if part) + row["finding"]
            plan["none"].append({"cids": row["cids"], **PENDING, "comment": " ".join(said.split())[:200]})
        same = f"（同じ原因: {', '.join(map(str, row['cids'][1:]))}）" if row["cids"][1:] else ""
        # The values Coverity gets with the comment: only where the comment is what will be registered.
        values = f"（{deviation['classification']}／{deviation['action']}／{deviation['severity']}）" if choice == "deviation" else ""
        table.append([f"{cid}{same}", VERDICT.get(row["verdict"], "-"), LEVEL.get(row["state"].get("confidence"), "-"),
                      ", ".join(row["state"].get("files") or []) if row["patch"] else "なし",
                      deviation["comment"] + values, CHOICE.get(choice, "-"), remark])
    write(run.dir / "apply-plan.json", plan)
    summary = run.dir / "summary.md"
    rest = REST.format(n["unfinished"]) if n["unfinished"] and not run.meta.get("all") else ""
    out = {"vcs": kind, "plan_file": (run.dir / "apply-plan.json").as_posix(),
           "summary_file": summary.as_posix() if summary.is_file() else None,
           "table": "\n".join("| " + " | ".join(map(cell, line)) + " |" for line in table) + rest, "counts": n}
    # What the OK does to the repository. The push of earlier fixes is named alone only when no fix is taken in.
    how = "作業コピーのファイルに適用" if kind != "git" else f"ブランチ {branch} にコミット" + ("して origin に push" if origin else "")
    parts = [f"修正の取り込み（{n['fix']} 件。{how}）" if n["fix"] else "",
             f"逸脱コメントの登録（{n['deviation']} 件）" if n["deviation"] else "",
             f"保留の登録（{n['none']} 件）" if n["none"] else "",
             f"ブランチ {branch} の push" if unpushed and not n["fix"] else "",
             f"取り込み済みの修正の Coverity への登録（{waiting} 件）" if waiting else ""]
    if not any(parts):
        return {**out, "message": "取り込む修正と、Coverity に登録する内容はありません", "to_change": change, "next": []}
    registers = "取り込んだ修正は、Coverity に「Bug／Fix Required」として登録します。" if n["fix"] else ""
    # The apply line is not under next: whoever follows next alone changes nothing without the person's OK.
    return {**out, "question": f"この内容で、{'、'.join(filter(None, parts))}を行います。{registers}"
                               "よろしければ「OK」と入力してください。"
                               "変更するときは「20003 は逸脱、20007 は保留、ほかは OK」のように入力してください",
            "after_ok": [run.command("apply")], "to_change": change, "next": []}


def record(run: Run, cid: int, **fields) -> None:
    """applied.json, written after each fix that is in."""
    at = datetime.now().isoformat(timespec="seconds")
    run.fixed[str(cid)] = {**fields, "files": run.items[cid]["state"].get("files") or [], "at": at}
    write(run.dir / "applied.json", {**run.applied, "fixes": run.fixed})


def take_git(run: Run, todo: list[int], fixes: dict) -> dict | None:
    """One commit per fix on the one branch of this result folder, made in the index of a temporary
    worktree outside the repository, without a checkout (the person's working tree, index and current
    branch are not touched); then the push, without force, and the text of the pull request (the person
    creates it)."""
    repo, branch, base = run.repo, f"coverity-fix/{run.dir.name}", str(run.meta.get("base_branch") or "")
    remote = text(git(repo, "config", "--get", "remote.origin.url").stdout)
    if todo:
        version = re.search(r"(\d+)\.(\d+)(\.\d+)*", text(git(repo, "version").stdout))  # "git version 2.43.0.windows.1"
        if version and (int(version[1]), int(version[2])) < (2, 32):  # apply --cached --3way
            raise Problem(f"git を 2.32 以降に更新してから、もう一度「修正を取り込んで」と入力してください（現在の git は {version[0]} です）")
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
            git(repo, "worktree", "add", "--quiet", "--detach", "--no-checkout", "--force", str(tree), start,
                check=True)
            git(repo, "read-tree", "HEAD", cwd=tree, check=True)  # the index is filled, no file is written
            for cid in todo:
                row = run.items[cid]
                subject = f"Coverity CID {', '.join(map(str, row['cids']))} の修正（{run.issues[cid].get('checker', '')}）"
                tried = git(repo, "apply", "--cached", "--3way", "--whitespace=nowarn", str(run.file(cid, "fix.patch")),
                            cwd=tree)
                if tried.returncode != 0:
                    reason = REFUSED.format("マージ")
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
                    git(repo, "read-tree", "HEAD", cwd=tree, check=True)
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
        else:
            error = text(push.stderr)[-500:] or "git push が失敗しました"
    if pushed:
        title = f"Coverity の警告の修正（{run.meta.get('stream', '')}、{len(run.fixed)} 件）"
        cids = [member for cid, row in run.items.items() if str(cid) in run.fixed for member in row["cids"]]
        body = ("Coverity の警告の修正です。修正の内容は、各コミットのメッセージにあります。\n\n"
                f"CID: {', '.join(map(str, cids))}")  # no table: the URL stays short
        web = (re.match(r"https?://(?:[^@/]+@)?([^/]+)/([^/]+)/([^/]+?)(?:\.git)?/?$", remote)
               or re.match(r"(?:ssh://)?[^@/]+@([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote))
        url = web and (f"https://{web[1]}/{web[2]}/{web[3]}/compare/{quote(base)}...{quote(branch)}?expand=1"
                       f"&title={quote(title, safe='')}&body={quote(body, safe='')}")
        pull = {"title": title, "body": body, "compare_url": url}
    return {"branch": branch, "base_branch": base, "pushed": pushed, "push_error": error, "pr": pull}


def svn_patch(repo: Path, patch: Path, files: list[str], *dry: str) -> bool:
    """Whether svn patch takes every file of the fix. svn prints one status line per file: with C for
    a conflict (a rejected hunk), and none for a target it skips. The lines are counted, not compared
    with the file names: svn prints a name the console cannot show as ?\\NNN."""
    proc = execute(["svn", "--non-interactive", "patch", *dry, str(patch), "."], repo, check=False)
    status = re.findall(r"^([ADUGC][ADUGC ]| [ADUGC]) +\S", text(proc.stdout + proc.stderr), re.M)
    return proc.returncode == 0 and len(status) >= len(files) and not any("C" in columns for columns in status)


def take_svn(run: Run, todo: list[int], fixes: dict) -> dict | None:
    """Patch the fixes into the person's working copy; nothing is committed. svn patch applies what it
    can, so a dry run comes first: a fix with a conflict is left out as a whole."""
    for cid in todo:
        patch, files = run.file(cid, "fix.patch"), run.items[cid]["state"].get("files") or []
        if not (svn_patch(run.repo, patch, files, "--dry-run") and svn_patch(run.repo, patch, files)):
            fixes["failed"][str(cid)] = REFUSED.format("コミット") + STALE
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
    # The entries for Coverity, after the fixes: the chosen deviations, every fix that is taken in and the
    # items of 保留, each while it is not registered yet (written.json). The comment of a fix is one line.
    where = f"修正をブランチ {place['branch']} にコミットしました。" if kind == "git" and place \
        else "修正を作業コピーに適用しました。"
    deviations = [entry for entry in plan["deviations"] if not run.registered(entry["cids"])]
    taken = [{"cids": run.items[cid]["cids"], "classification": "Bug", "action": "Fix Required",
              "severity": run.items[cid]["deviation"]["severity"],
              "comment": " ".join((where + run.items[cid]["summary"]).split())[:200]}
             for cid in run.waiting()]
    pending = [entry for entry in plan["none"] if isinstance(entry, dict) and not run.registered(entry["cids"])]
    target, entries = run.dir / "coverity-entries.json", [*deviations, *taken, *pending]
    if entries:
        raw = write(target, {"stream": run.meta.get("stream"), "entries": entries})
        write(Path(f"{target}.sha256"), (hashlib.sha256(raw).hexdigest() + "\n").encode("ascii"))
    else:
        for path in (target, Path(f"{target}.sha256")):
            path.unlink(missing_ok=True)
    # Written at the end of every run, also without a fix: its time is that of the OK that was carried out.
    write(run.dir / "applied.json", {"vcs": kind, **run.applied, "fixes": run.fixed})
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
    kinds = [f"逸脱コメント {len(deviations)} 件" if deviations else "", f"取り込んだ修正 {len(taken)} 件" if taken else "",
             f"保留 {len(pending)} 件" if pending else ""]
    if entries:
        said.append(f"続けて、{'と、'.join(filter(None, kinds))}を Coverity に登録します")
    call = {"tool": "coverity_update_triage",
            "arguments": {"repo_root": run.repo.as_posix(), "entries_file": target.as_posix()}}
    # In the order an agent acts: what to tell the person, the call to make, what to tell when the call was not made.
    return {"fixes": fixes, **({kind: place} if place else {}), "entries_file": target.as_posix() if entries else None,
            "entries": len(entries), "message": "。".join(said) or "取り込みと登録は完了しています",
            "call": [call] if entries else [], **({"if_not_called": NOT_CALLED} if entries else {}), "next": []}


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
