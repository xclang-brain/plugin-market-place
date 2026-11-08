#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Apply the approvals of a triage run: the plan, the confirmation string, branches and patches.

Run: ``uv run --native-tls --quiet "<skill folder>/scripts/apply.py" <command> ...``

  runs                  run folders under output_dir that have a summary (newest first)
  preview               approvals.json + run files -> apply-plan.json and its confirmation string
  apply-code            git: branches pushed (one per fix, or one per run), pull request texts;
                        svn: patches applied to the working copy; rejected groups -> no-grouping.yaml.
                        Reads apply-plan.result.json (written by the write-back to Coverity) to report
                        what Coverity has. The same confirmation string never pushes twice.
  knowledge-candidates  items where the person did not take the AI's proposal as it was
  add-knowledge         append lessons to knowledge.md under 「## 追記（日付、実行 ID）」
  stats                 how often the recommendation was taken (option metrics)

Each command prints exactly one JSON object; a failure is {"ok": false, "error", "problems"} + exit 1.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # never leave __pycache__ in the skill folder

import argparse  # noqa: E402
import codecs  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import date, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from urllib.parse import quote  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import confidence  # noqa: E402  (byte copy of the batch run's rule: the confidence shown to people)
import plantoken  # noqa: E402

RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
FORMAT = 2
CONFIG_DIR = ".coverity-triage"
PLAN_FILE = "apply-plan.json"
WRITE_BACK_RESULT = "apply-plan.result.json"  # written by the tool that writes the triage to Coverity
CODE_RESULT = "apply-code.result.json"  # this skill's record: what was applied, per confirmation string
NO_GROUPING = "no-grouping.yaml"
NO_GROUPING_HEADER = "# 却下されたグループの CID。次の実行からグループにしない（自動で追記）\n"
KNOWLEDGE_FILE = "knowledge.md"
DEFAULT_OUTPUT_DIR = os.pardir + "/coverity-triage-out"  # next to the repository
LABELS = {"fix": "修正", "deviation": "逸脱", "reject": "却下"}
CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}
COMMENT_MAX = 200  # characters of a deviation comment (the rule for the comments written by the AI)
SERVER_ERROR = re.compile(r"(?:HTTP\s*|[（(]\s*)5\d\d(?!\d)|(?<![0-9A-Za-z])5xx(?![0-9A-Za-z])", re.IGNORECASE)
KNOWLEDGE_TEMPLATE = """# プロジェクトの知識（Coverity トリアージ）

警告を調べる前に AI が読みます。チームで共有するため、コミットしてください。

## 調査で使う事実

## 推奨の方針（修正か逸脱か）

## 逸脱コメントの書き方
"""

# ---- output ---------------------------------------------------------------------------------------


class ApplyError(Exception):
    def __init__(self, message: str, problems: list[str] | None = None, **details: Any):
        super().__init__(message)
        self.problems = problems or []
        self.details = details


def _json_escape(error: UnicodeEncodeError) -> tuple[str, int]:
    return json.dumps(error.object[error.start:error.end], ensure_ascii=True)[1:-1], error.end


codecs.register_error("apply_json_escape", _json_escape)


def _stdout_encoding() -> str:
    """PYTHONIOENCODING when set; the console code page on Windows when stdout is a pipe; else UTF-8."""
    if os.environ.get("PYTHONIOENCODING"):
        return (sys.stdout.encoding or "utf-8") if sys.stdout else "utf-8"
    if sys.platform == "win32" and not (sys.stdout and sys.stdout.isatty()):
        try:
            import ctypes
            page = ctypes.windll.kernel32.GetConsoleOutputCP()
        except (AttributeError, OSError):
            page = 0
        if page == 65001:
            return "utf-8"
        if page:
            try:
                return codecs.lookup(f"cp{page}").name
            except LookupError:
                pass
        import locale
        return locale.getpreferredencoding(False) or "utf-8"
    return "utf-8"


def emit(data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    encoding = _stdout_encoding()
    try:
        raw = text.encode(encoding, errors="apply_json_escape")
    except LookupError:
        raw = text.encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(raw.decode(encoding, errors="replace"))
    else:
        stream.write(raw)
    sys.stdout.flush()


def progress(message: str) -> None:
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def read_stdin_json() -> Any:
    raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        import locale
        text = raw.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
    if not text.strip():
        raise ApplyError("標準入力が空です。JSON を渡してください")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ApplyError(f"標準入力の JSON を読めません: {exc}") from None


# ---- files and commands ----------------------------------------------------------------------------


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_bytes(path: Path, data: bytes) -> None:
    """Write via a temporary file and os.replace (waits a little on Windows while another reads it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def write_json(path: Path, data: Any) -> None:
    write_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, OSError):
        return default


def absolute(path: str | Path) -> Path:
    path = Path(path).expanduser()
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


def run(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True,
        input_bytes: bytes | None = None, timeout: float | None = 900) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, input=input_bytes, capture_output=True, check=False,
                              timeout=timeout, stdin=None if input_bytes is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        raise ApplyError(f"コマンドが見つかりません: {args[0]}（入れて、PATH を通してください）") from None
    except subprocess.TimeoutExpired:
        raise ApplyError(f"{' '.join(args[:4])} が時間内に終わりませんでした") from None
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise ApplyError(f"{' '.join(args[3:6] if args[:1] == ['git'] else args[:3])} が失敗しました: {message[:800]}")
    return proc


def out_text(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


IDENTITY = {"GIT_AUTHOR_NAME": "Coverity トリアージ", "GIT_AUTHOR_EMAIL": "triage@localhost",
            "GIT_COMMITTER_NAME": "Coverity トリアージ", "GIT_COMMITTER_EMAIL": "triage@localhost"}


class Git:
    def __init__(self, repo: Path):
        self.repo = repo
        self._bases: dict[str, str] = {}

    def __call__(self, *args: str, cwd: Path | None = None, check: bool = True, input_bytes: bytes | None = None,
                 env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        full = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
        return run(["git", "-c", "core.longpaths=true", *args], cwd=cwd or self.repo, env=full, check=check,
                   input_bytes=input_bytes)

    def rev(self, ref: str) -> str | None:
        proc = self("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return out_text(proc) if proc.returncode == 0 and out_text(proc) else None

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def identity(self) -> dict[str, str]:
        """A name and e-mail for commits when the person has not set them."""
        if out_text(self("config", "user.name", check=False)) and out_text(self("config", "user.email", check=False)):
            return {}
        return IDENTITY

    def remote(self) -> str | None:
        proc = self("remote", "get-url", "origin", check=False)
        return out_text(proc) or None if proc.returncode == 0 else None

    def checked_out(self, branch: str) -> bool:
        listing = out_text(self("worktree", "list", "--porcelain", check=False))
        return f"branch refs/heads/{branch}" in listing.splitlines()

    def unique_branch(self, name: str) -> str:
        candidate, n = name, 1
        while self.rev(f"refs/heads/{candidate}"):
            n += 1
            candidate = f"{name}-{n}"
        return candidate

    def base(self, branch: str) -> str:
        """The latest revision of the branch fixes are made on: origin's when it can be fetched."""
        if branch not in self._bases:
            self._bases[branch] = self._fetch_base(branch)
        return self._bases[branch]

    def _fetch_base(self, branch: str) -> str:
        if self.remote():
            fetched = self("fetch", "--quiet", "origin", branch, check=False)
            if fetched.returncode == 0:
                found = self.rev(f"origin/{branch}")
                if found:
                    return found
        found = self.rev(branch)
        if not found:
            raise ApplyError(f"修正の元にするブランチ {branch} が見つかりません（config.yaml の vcs.base_branch を確かめてください）")
        return found


def github_web_url(remote: str | None) -> str | None:
    """``https://<host>/<owner>/<repo>`` for an https or ssh remote of GitHub (or GitHub Enterprise)."""
    if not remote:
        return None
    match = (re.match(r"^https?://(?:[^@/]+@)?([^/]+)/([^/]+)/([^/]+?)(?:\.git)?/?$", remote)
             or re.match(r"^(?:ssh://)?[^@/]+@([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote))
    if not match:
        return None
    host, owner, repo = match.groups()
    return f"https://{host}/{owner}/{repo}"


# ---- settings and the run folder ---------------------------------------------------------------------


def find_root(start: str | None) -> Path:
    folder = absolute(start or os.getcwd())
    if folder.is_file():
        folder = folder.parent
    for candidate in (folder, *folder.parents):
        if (candidate / CONFIG_DIR / "config.yaml").is_file() or (candidate / ".git").exists():
            return candidate
    for candidate in reversed((folder, *folder.parents)):
        if (candidate / ".svn").is_dir():
            return candidate
    return folder


def load_settings(repo: Path) -> dict[str, Any]:
    """The values of config.yaml this skill uses, with their defaults."""
    import yaml

    path = repo / CONFIG_DIR / "config.yaml"
    if not path.is_file():
        raise ApplyError(f"設定ファイルがありません: {path}。先に、Coverity のトリアージの準備をしてください")
    try:
        data = yaml.safe_load(path.read_bytes().decode("utf-8-sig")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ApplyError(f"config.yaml を読めません: {path}", problems=[str(exc)[:300]]) from None
    if not isinstance(data, dict):
        raise ApplyError(f"config.yaml の一番外側が「キー: 値」の形ではありません: {path}")

    def section(name: str) -> dict[str, Any]:
        value = data.get(name)
        return value if isinstance(value, dict) else {}

    vcs, options = section("vcs"), section("options")
    kind = vcs.get("type") if vcs.get("type") in ("git", "svn") else ("git" if (repo / ".git").exists() else "svn")
    return {
        "output_dir": data.get("output_dir") if isinstance(data.get("output_dir"), str) else DEFAULT_OUTPUT_DIR,
        "vcs": {"type": kind,
                "base_branch": vcs.get("base_branch") if isinstance(vcs.get("base_branch"), str) else None,
                "branch_prefix": vcs.get("branch_prefix") if isinstance(vcs.get("branch_prefix"), str) else "coverity-fix/"},
        "options": {k: options.get(k) is True for k in ("annotation", "per_run_branch", "knowledge_suggestions",
                                                        "metrics")},
    }


def base_branch(git: Git, settings: dict[str, Any]) -> str:
    if settings["vcs"]["base_branch"]:
        return settings["vcs"]["base_branch"]
    proc = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    head = out_text(proc)
    if proc.returncode == 0 and "/" in head:
        return head.split("/", 1)[1]
    proc = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return (out_text(proc) if proc.returncode == 0 else "") or "main"


def output_dir(repo: Path, settings: dict[str, Any]) -> Path:
    path = Path(settings["output_dir"]).expanduser()
    return absolute(path if path.is_absolute() else repo / path)


class RunFolder:
    def __init__(self, folder: str | Path):
        self.dir = absolute(folder)
        state = read_json(self.dir / "run.json")
        if not isinstance(state, dict):
            raise ApplyError(f"実行フォルダではありません（run.json がないか、読めません）: {self.dir}")
        if state.get("format") != FORMAT:
            raise ApplyError("この実行フォルダは、別の形で作られたもの（元の道具で作った実行など）なので、反映できません。"
                             "作った道具で反映するか、新しく実行してください", foreign=True, run_dir=str(self.dir))
        self.state = state
        self.run_id = str(state.get("run_id") or self.dir.name)
        root = state.get("repo_root")
        if not isinstance(root, str) or not root:
            raise ApplyError(f"run.json に repo_root がありません: {self.dir}")
        self.repo = absolute(root)
        self.items = [i for i in state.get("items") or [] if isinstance(i, dict) and isinstance(i.get("id"), str)]

    def item(self, item_id: str) -> dict[str, Any]:
        return next((i for i in self.items if i["id"] == item_id), {"id": item_id, "cids": []})

    def item_dir(self, item_id: str) -> Path:
        return self.dir / "items" / item_id

    def result(self, item_id: str) -> dict[str, Any] | None:
        data = read_json(self.item_dir(item_id) / "result.json")
        return data if isinstance(data, dict) else None

    def saved(self, item_id: str) -> dict[str, Any]:
        data = read_json(self.item_dir(item_id) / "saved.json", {})
        return data if isinstance(data, dict) else {}

    def has_summary(self) -> bool:
        return (self.dir / "summary.html").is_file()

    @property
    def stream(self) -> str | None:
        """The stream of the run's warnings (run.json ``stream``; one stream per run)."""
        value = self.state.get("stream")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def seconds(self) -> dict[str, float]:
        """How long each finished item took (run.json ``items[].seconds``: a number, or null when not known)."""
        out = {}
        for item in self.items:
            value = item.get("seconds")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                out[item["id"]] = float(value)
        return out


def is_group(item_id: str) -> bool:
    return item_id.startswith("G")


def load_record(run_dir: Path) -> dict[str, Any]:
    data = read_json(run_dir / CODE_RESULT, {})
    data = data if isinstance(data, dict) else {}
    for key in ("applied", "results", "made", "metrics"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    data["version"] = 1
    return data


def written_ids(run_dir: Path) -> tuple[set[str], dict[str, Any]]:
    """The ids Coverity has (``written`` of apply-plan.result.json), and the whole result."""
    data = read_json(run_dir / WRITE_BACK_RESULT, {})
    data = data if isinstance(data, dict) else {}
    written = data.get("written") if isinstance(data.get("written"), dict) else {}
    return {str(v["id"]) for v in written.values() if isinstance(v, dict) and "id" in v}, data


def applied_parts(record: dict[str, Any], written: set[str], item: dict[str, Any]) -> set[str]:
    """What is applied already: this skill's record (apply-code.result.json), and what Coverity has."""
    entry = record["applied"].get(item["id"])
    parts = {k for k, v in entry.items() if v and k != "at"} if isinstance(entry, dict) else set()
    if item["id"] in written:
        parts.add("coverity")
    return parts


def patch_of(run: RunFolder, item_id: str, kind: str, saved: dict[str, Any]) -> Path | None:
    entry = saved.get(kind) if isinstance(saved.get(kind), dict) else {}
    for candidate in (entry.get("patch"), run.dir / "patches" / f"{item_id}-{kind}.patch"):
        if candidate and Path(candidate).is_file() and Path(candidate).stat().st_size > 0:
            return absolute(candidate)
    return None


def default_branch(run: RunFolder, settings: dict[str, Any], item: dict[str, Any], result: dict[str, Any],
                   kind: str) -> str:
    excluded = set(result.get("group_excluded_cids") or [])
    kept = [c for c in item.get("cids") or [] if c not in excluded] or list(item.get("cids") or [])
    name = f"{run.run_id}-{item['id']}" if is_group(item["id"]) else f"cid-{kept[0] if kept else item['id']}"
    return settings["vcs"]["branch_prefix"] + name + ("-annotation" if kind == "annotation" else "")


# ---- runs -------------------------------------------------------------------------------------------


def cmd_runs(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_root(args.repo)
    settings = load_settings(repo)
    out_dir = output_dir(repo, settings)
    runs, without = [], 0
    if out_dir.is_dir():
        for folder in sorted((p for p in out_dir.iterdir() if p.is_dir() and RUN_ID.match(p.name)),
                             key=lambda p: p.name, reverse=True):
            state = read_json(folder / "run.json")
            summary = (folder / "summary.html").is_file() or (folder / "summary.md").is_file()
            if not summary:
                without += 1
                continue
            foreign = not isinstance(state, dict) or state.get("format") != FORMAT
            items = (state or {}).get("items") if isinstance(state, dict) else None
            counts: dict[str, int] = {}
            for item in items or []:
                if isinstance(item, dict):
                    counts[str(item.get("status"))] = counts.get(str(item.get("status")), 0) + 1
            runs.append({"run_id": folder.name, "run_dir": str(folder),
                         "created_at": (state or {}).get("created_at") if isinstance(state, dict) else None,
                         "filter_name": (state or {}).get("filter_name") if isinstance(state, dict) else None,
                         "counts": counts, "has_approvals": (folder / "approvals.json").is_file(),
                         "applied_before": (folder / CODE_RESULT).is_file(), "foreign": foreign})
    latest = next((r["run_dir"] for r in runs if not r["foreign"] and r["has_approvals"]), None)
    return {"ok": True, "repo_root": str(repo), "output_dir": str(out_dir), "runs": runs, "latest": latest,
            "without_summary": without,
            "next": ("latest の実行で preview を動かす" if latest else
                     "一覧（summary.html）のある実行がありません。トリアージの実行が終わってから反映します")}


# ---- preview ------------------------------------------------------------------------------------------


def deviation_values(entry: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
    """The person's edit when it is complete, else the AI's proposal."""
    edit = entry.get("deviation")
    keys = ("classification", "action", "severity", "comment")
    if isinstance(edit, dict) and all(isinstance(edit.get(k), str) and edit[k].strip() for k in keys):
        return {k: edit[k].strip() for k in keys}
    proposal = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
    return {k: str(proposal.get(k) or "").strip() for k in keys}


def cmd_preview(args: argparse.Namespace) -> dict[str, Any]:
    run = RunFolder(args.run)
    if not run.has_summary():
        raise ApplyError("一覧（summary.html）がまだないので、反映できません。トリアージの実行が終わって一覧ができてから使います")
    approvals = read_json(run.dir / "approvals.json")
    if not isinstance(approvals, dict) or not isinstance(approvals.get("items"), dict):
        raise ApplyError("承認のファイル（approvals.json）がないか、読めません。一覧を作るときにできるファイルです。"
                         "古い形の実行（承認を summary.md に書いたもの）は、このスキルでは反映できません",
                         path=str(run.dir / "approvals.json"))
    settings = load_settings(run.repo)
    vcs = settings["vcs"]["type"]
    record = load_record(run.dir)
    written, _ = written_ids(run.dir)
    triage, code, reject, problems = [], [], [], []
    already, already_fixed, undecided, long_comments = [], [], [], []
    for item in run.items:
        if item.get("status") != "done" or item.get("merged_into"):
            continue
        entry = approvals["items"].get(item["id"])
        decision = entry.get("decision") if isinstance(entry, dict) else None
        if decision not in LABELS:
            undecided.append(item["id"])
            continue
        result = run.result(item["id"])
        if result is None:
            problems.append(f"{item['id']}: 結果のファイル（items/{item['id']}/result.json）を読めません")
            continue
        saved = run.saved(item["id"])
        done = applied_parts(record, written, item)
        cids = [int(c) for c in item.get("cids") or []]
        if decision == "fix":
            if "fix" in done:
                already.append(item["id"])
                continue
            patch = patch_of(run, item["id"], "fix", saved)
            if patch is None:
                if (result.get("fix") or {}).get("already_fixed_on_latest"):
                    already_fixed.append(item["id"])
                else:
                    problems.append(f"{item['id']}: 修正の差分がないので「修正」は反映できません（「逸脱」か「却下」を選んでください）")
                continue
            fix = saved.get("fix") if isinstance(saved.get("fix"), dict) else {}
            git_vcs = vcs == "git"
            code.append({"id": item["id"], "cids": cids, "kind": "fix", "files": list(fix.get("files") or []),
                         "patch": str(patch),
                         "branch": (fix.get("branch") or (None if settings["options"]["per_run_branch"] else
                                                          default_branch(run, settings, item, result, "fix")))
                         if git_vcs else None,
                         "commit": fix.get("commit") if git_vcs else None})
        elif decision == "deviation":
            todo = False
            if "coverity" not in done:
                values = deviation_values(entry, result)
                missing = [k for k in ("classification", "action", "comment") if not values[k]]
                if missing:
                    problems.append(f"{item['id']}: 逸脱の {'・'.join(missing)} が空です（承認の画面で直してください）")
                    continue
                stream = run.stream
                triage.append({"id": item["id"], "cids": cids, **({"stream": stream} if stream else {}), **values})
                if len(values["comment"]) > COMMENT_MAX:
                    long_comments.append(f"{item['id']}（{len(values['comment'])} 文字）")
                todo = True
            annotation = saved.get("annotation") if isinstance(saved.get("annotation"), dict) else None
            if annotation and "annotation" not in done:
                patch = patch_of(run, item["id"], "annotation", saved)
                if patch is not None:
                    code.append({"id": item["id"], "cids": cids, "kind": "annotation",
                                 "files": list(annotation.get("files") or []), "patch": str(patch),
                                 "branch": (annotation.get("branch") or (None if settings["options"]["per_run_branch"]
                                            else default_branch(run, settings, item, result, "annotation")))
                                 if vcs == "git" else None,
                                 "commit": annotation.get("commit") if vcs == "git" else None})
                    todo = True
            if not todo:
                already.append(item["id"])
        else:
            if "reject" in done:
                already.append(item["id"])
                continue
            reject.append({"id": item["id"], "cids": cids, "group": is_group(item["id"]) or len(cids) > 1})
    if problems:
        raise ApplyError("反映の前に直すところがあります", problems=problems)
    counts = {"fix": sum(1 for c in code if c["kind"] == "fix"), "deviation": len(triage), "reject": len(reject)}
    items = ([{"id": c["id"], "decision": "fix", "label": "修正", "cids": c["cids"]} for c in code if c["kind"] == "fix"]
             + [{"id": e["id"], "decision": "deviation", "label": "逸脱", "cids": e["cids"]} for e in triage]
             + [{"id": r["id"], "decision": "reject", "label": "却下", "cids": r["cids"]} for r in reject])
    base = {"ok": True, "run_id": run.run_id, "run_dir": str(run.dir), "repo_root": str(run.repo), "vcs": vcs,
            "counts": counts, "items": items, "already_applied": already, "already_fixed_on_latest": already_fixed,
            "not_approved": undecided,
            "options": {k: settings["options"][k] for k in ("knowledge_suggestions", "metrics", "per_run_branch")}}
    if not (triage or code or reject):
        return {**base, "nothing_to_apply": True, "plan_file": None, "confirmation_token": None, "effects": [],
                "next": ("反映するものはありません（承認が1件もないか、すべて反映済みです）。承認を選んでから、もう一度"
                         "反映を頼むよう人に伝える")}
    plan = {"run_id": run.run_id, "created_at": now(), "triage": triage, "code": code, "reject": reject}
    data = plantoken.plan_bytes(plan)
    path = run.dir / PLAN_FILE
    write_bytes(path, data)
    effects = []
    if triage:
        effects.append(f"逸脱 {len(triage)} 件：Coverity の分類・対応・重大度とコメントが書き換わります"
                       f"（{', '.join(e['id'] for e in triage)}）")
    fixes = [c for c in code if c["kind"] == "fix"]
    if fixes:
        if vcs == "git":
            names = [c["branch"] for c in fixes if c["branch"]]
            effects.append(f"修正 {len(fixes)} 件：ブランチを origin に push します"
                           + (f"（{', '.join(names)}）" if names else "（1つのブランチにまとめます）")
                           + "。Coverity には書きません")
        else:
            effects.append(f"修正 {len(fixes)} 件：svn の作業コピーのファイルに差分を当てます（コミットはご自身で）")
    notes = [c for c in code if c["kind"] == "annotation"]
    if notes:
        effects.append(f"注釈 {len(notes)} 件：コードに注釈を入れる差分を、修正と同じように反映します")
    if reject:
        groups = [r["id"] for r in reject if r["group"]]
        effects.append(f"却下 {len(reject)} 件：何も書き込みません" + (
            f"。グループ {', '.join(groups)} の CID は、次の実行からグループにしません（no-grouping.yaml に足します）"
            if groups else ""))
    if long_comments:
        effects.append(f"逸脱コメントが {COMMENT_MAX} 文字を超えています（{', '.join(long_comments)}）。このまま書き戻すと、"
                       f"その文のまま Coverity に入ります。縮めるときは、承認の画面で直してから、もう一度反映を頼んでください")
    if already_fixed:
        effects.append(f"{', '.join(already_fixed)}：最新のリビジョンでもう直っているため、「修正」でも取り込むものはありません")
    return {**base, "nothing_to_apply": False, "plan_file": str(path),
            "confirmation_token": plantoken.token_for_bytes(data), "effects": effects,
            "next": ("件数・ID・effects を人に見せて「この内容で反映しますか？」と聞く。同意を得たら、逸脱が1件以上あれば、"
                     "Coverity にトリアージを書き戻せる道具に plan_file と confirmation_token を渡し（done: false なら"
                     "同じ引数で呼び直す）、そのあと apply-code --token")}


# ---- apply-code ----------------------------------------------------------------------------------------


def shown_confidence(run: RunFolder, item_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """The confidence shown to people (the shared rule of scripts/confidence.py, as in the summary):
    {"confidence", "reason", "lowered_by"}.

    The files go to the rule as they are read: review.json, the item's entry of run.json (the rule decides
    whether the review counts) and the item's entry of verify.json (the build check)."""
    review = read_json(run.item_dir(item_id) / "review.json", None)
    verify = read_json(run.dir / "verify.json", {})
    check = verify.get(item_id) if isinstance(verify, dict) else None
    return confidence.effective_confidence(result, review, check if isinstance(check, dict) else None,
                                           run.item(item_id))


def pr_text(run: RunFolder, entry: dict[str, Any]) -> tuple[str, str]:
    result = run.result(entry["id"]) or {}
    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    fix = result.get("fix") if isinstance(result.get("fix"), dict) else {}
    deviation = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
    cids = entry["cids"]
    head = f"Coverity CID {', '.join(map(str, cids[:5]))}{' ほか' if len(cids) > 5 else ''}"
    title = f"{head}: {fix.get('summary') or '修正'}" if entry["kind"] == "fix" else f"{head}: 逸脱の注釈"
    level = shown_confidence(run, entry["id"], result)["confidence"]
    body = "\n".join([
        "## 見立て", "", str(verdict.get("summary") or ""), "", str(verdict.get("rationale") or ""), "",
        "## 変更の内容", "", str(fix.get("summary") if entry["kind"] == "fix" else deviation.get("comment") or ""), "",
        "## 影響とリスク", "", str(fix.get("impact") or ""), "",
        f"確信度: {CONFIDENCE_JA[level]}（Coverity のトリアージで AI が作り、人が承認した変更）", ""])
    return title[:200], body


def compare_url(git: Git, base: str, branch: str, title: str, body: str) -> str | None:
    web = github_web_url(git.remote())
    if not web:
        return None
    return (f"{web}/compare/{quote(base)}...{quote(branch)}?expand=1&title={quote(title)}"
            f"&body={quote(body[:3000])}")


def commit_message(run: RunFolder, entry: dict[str, Any]) -> str:
    result = run.result(entry["id"]) or {}
    excluded = set(result.get("group_excluded_cids") or [])
    cids = [c for c in entry["cids"] if c not in excluded] or entry["cids"]
    if entry["kind"] == "fix":
        return f"Coverity CID {', '.join(map(str, cids))}: 修正\n\n{(result.get('fix') or {}).get('summary', '')}\n"
    return (f"Coverity CID {', '.join(map(str, cids))}: 逸脱の注釈\n\n"
            f"{(result.get('deviation') or {}).get('classification', '')}\n")


class Worktree:
    """A temporary work copy outside the repository (git worktree), removed afterwards."""

    def __init__(self, git: Git, revision: str):
        self.git = git
        self.path = Path(tempfile.mkdtemp(prefix="cov-apply-")) / "w"
        git("worktree", "add", "--quiet", "--detach", "--force", str(self.path), revision)

    def apply_patch(self, patch: str) -> None:
        proc = self.git("apply", "--index", "--whitespace=nowarn", patch, cwd=self.path, check=False)
        if proc.returncode != 0:
            raise ApplyError("差分を、今の修正の元のブランチに当てられませんでした（ほかの変更と同じ所を変えています）: "
                             + proc.stderr.decode("utf-8", "replace").strip()[:400])
        if not out_text(self.git("diff", "--cached", "--name-only", cwd=self.path)):
            raise ApplyError("差分を当てても、変わるファイルがありません（もう取り込まれているかもしれません）")

    def commit(self, message: str) -> str:
        self.git("commit", "-q", "--no-verify", "-F", "-", cwd=self.path, input_bytes=message.encode("utf-8"),
                 env=self.git.identity())
        return out_text(self.git("rev-parse", "HEAD", cwd=self.path))

    def close(self) -> None:
        self.git("worktree", "remove", "--force", str(self.path), check=False)
        shutil.rmtree(self.path.parent, ignore_errors=True)
        self.git("worktree", "prune", check=False)


def ensure_branch(git: Git, run: RunFolder, record: dict[str, Any], entry: dict[str, Any], base_name: str) -> tuple[str, str]:
    """The branch of a fix at its commit: made from the run's commit, or from the patch on the latest base."""
    key = f"{entry['id']}:{entry['kind']}"
    branch = entry["branch"]
    commit = entry.get("commit") if entry.get("commit") and git.rev(entry["commit"]) else None
    made = record["made"].get(key) if isinstance(record["made"].get(key), dict) else None
    if commit is None and made and git.rev(made.get("commit") or ""):
        commit = made["commit"]
    current = git.rev(f"refs/heads/{branch}")
    if commit is None:
        if current:
            raise ApplyError(f"ブランチ {branch} がもうありますが、この実行の修正のコミットが見つかりません。"
                             "ブランチを消すか名前を変えてから、もう一度反映してください")
        progress(f"{entry['id']}: 差分から {branch} を作ります")
        tree = Worktree(git, git.base(base_name))
        try:
            tree.apply_patch(entry["patch"])
            commit = tree.commit(commit_message(run, entry))
        finally:
            tree.close()
        git("branch", branch, commit)
        record["made"][key] = {"branch": branch, "commit": commit, "at": now()}
        return branch, commit
    if current is None:
        git("branch", branch, commit)
    elif current != commit:
        if git.is_ancestor(current, commit):
            if git.checked_out(branch):
                raise ApplyError(f"ブランチ {branch} が作業中のフォルダで使われているので、動かせません")
            git("branch", "-f", branch, commit)
        elif git.is_ancestor(commit, current):
            commit = current  # the person added commits on top: push them too
        else:
            raise ApplyError(f"ブランチ {branch} は、この修正とは別の変更を指しています。ブランチを消すか名前を変えてから、"
                             "もう一度反映してください")
    return branch, commit


def push(git: Git, branch: str) -> None:
    if not git.remote():
        raise ApplyError("git のリモート origin がないので、push できません（ブランチは手元にできています）")
    git("push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}")


def apply_git(run: RunFolder, settings: dict[str, Any], record: dict[str, Any], entries: list[dict[str, Any]],
              out: dict[str, Any]) -> None:
    git = Git(run.repo)
    base_name = base_branch(git, settings)
    todo = []
    for entry in entries:
        done = (record["applied"].get(entry["id"]) or {}).get(entry["kind"])
        if isinstance(done, dict) and done.get("pushed"):
            out["pushed_before"].append({"id": entry["id"], "kind": entry["kind"], "branch": done.get("branch"),
                                         **({"pull_request": done["pull_request"]} if done.get("pull_request") else {})})
        else:
            todo.append(entry)
    if not todo:
        return
    if settings["options"]["per_run_branch"]:
        apply_combined(git, run, settings, record, todo, base_name, out)
        return
    for entry in todo:
        try:
            branch, commit = ensure_branch(git, run, record, entry, base_name)
            push(git, branch)
        except ApplyError as exc:
            out["errors"].append(f"{entry['id']}（{LABELS.get(entry['kind'], '注釈')}）: {exc}")
            continue
        title, body = pr_text(run, entry)
        pull = {"ids": [entry["id"]], "title": title, "body": body, "branch": branch, "base": base_name,
                "compare_url": compare_url(git, base_name, branch, title, body)}
        out["pull_requests"].append(pull)
        record["applied"].setdefault(entry["id"], {})[entry["kind"]] = {
            "branch": branch, "commit": commit, "pushed": True, "at": now(),
            "pull_request": {k: pull[k] for k in ("title", "compare_url")}}


def apply_combined(git: Git, run: RunFolder, settings: dict[str, Any], record: dict[str, Any],
                   entries: list[dict[str, Any]], base_name: str, out: dict[str, Any]) -> None:
    """Option per_run_branch: one branch with every approved change, on the latest base."""
    tree = Worktree(git, git.base(base_name))
    included: list[dict[str, Any]] = []
    try:
        for entry in entries:
            commit = entry.get("commit") if entry.get("commit") and git.rev(entry["commit"]) else None
            if commit:
                picked = git("cherry-pick", "--allow-empty", commit, cwd=tree.path, check=False, env=git.identity())
                if picked.returncode != 0:
                    git("cherry-pick", "--abort", cwd=tree.path, check=False)
                    out["errors"].append(f"{entry['id']}: ほかの修正と同じ所を変えているため、まとめたブランチに入れられませんでした")
                    continue
            else:
                try:
                    tree.apply_patch(entry["patch"])
                    tree.commit(commit_message(run, entry))
                except ApplyError as exc:
                    git("reset", "--quiet", "--hard", cwd=tree.path, check=False)
                    out["errors"].append(f"{entry['id']}: {exc}")
                    continue
            included.append(entry)
        if not included:
            return
        head = out_text(git("rev-parse", "HEAD", cwd=tree.path))
    finally:
        tree.close()
    branch = git.unique_branch(f"{settings['vcs']['branch_prefix']}run-{run.run_id}")
    git("branch", branch, head)
    try:
        push(git, branch)
    except ApplyError as exc:
        out["errors"].append(f"{branch}: {exc}")
        return
    title = f"Coverity トリアージ {run.run_id}（{len(included)} 件）"
    body = "\n".join(f"- {pr_text(run, e)[0]}" for e in included) + "\n"
    pull = {"ids": [e["id"] for e in included], "title": title, "body": body, "branch": branch, "base": base_name,
            "compare_url": compare_url(git, base_name, branch, title, body)}
    out["pull_requests"].append(pull)
    for entry in included:
        record["applied"].setdefault(entry["id"], {})[entry["kind"]] = {
            "branch": branch, "commit": head, "pushed": True, "at": now(),
            "pull_request": {k: pull[k] for k in ("title", "compare_url")}}


def apply_svn(run: RunFolder, record: dict[str, Any], entries: list[dict[str, Any]], out: dict[str, Any]) -> None:
    for entry in entries:
        done = (record["applied"].get(entry["id"]) or {}).get(entry["kind"])
        if isinstance(done, dict) and done.get("svn_patch"):
            out["svn_applied_before"].append(entry["id"])
            continue
        try:
            proc = run_svn_patch(run.repo, entry["patch"])
        except ApplyError as exc:
            out["errors"].append(f"{entry['id']}: {exc}")
            continue
        record["applied"].setdefault(entry["id"], {})[entry["kind"]] = {"svn_patch": True, "at": now()}
        out["svn_applied"].append(entry["id"])
        if proc:
            out["errors"].append(f"{entry['id']}: {proc}")


def run_svn_patch(repo: Path, patch: str) -> str | None:
    proc = run(["svn", "--non-interactive", "patch", patch, str(repo)], cwd=repo, check=False)
    output = out_text(proc)
    if proc.returncode != 0:
        raise ApplyError(f"svn patch が失敗しました: {output or proc.stderr.decode('utf-8', 'replace')[:400]}")
    if re.search(r"^(C|>\s+rejected)", output, re.MULTILINE):
        return f"svn patch で、当てられない所がありました。作業コピーを確かめてください:\n{output[:800]}"
    return None


def add_no_grouping(repo: Path, cids: list[int]) -> tuple[list[int], bool]:
    """Append CIDs to no-grouping.yaml and keep the comments in it. Returns (added CIDs, comments kept)."""
    import yaml

    path = repo / CONFIG_DIR / NO_GROUPING
    if not path.is_file():
        new = sorted(set(cids))
        write_bytes(path, (NO_GROUPING_HEADER + "cids:\n" + "".join(f"- {c}\n" for c in new)).encode("utf-8"))
        return new, True
    raw = path.read_bytes().decode("utf-8-sig")
    bom = "﻿" if path.read_bytes().startswith(b"\xef\xbb\xbf") else ""
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    data = yaml.safe_load(text) or {}
    data = data if isinstance(data, dict) else {}
    existing = [int(c) for c in data.get("cids") or [] if str(c).strip().lstrip("-").isdigit()]
    new = sorted(set(cids) - set(existing))
    if not new:
        return [], True
    want = existing + new
    out = _append_cids(text, new, want)
    kept = out is not None
    if out is None:
        header = "".join(line + "\n" for line in text.splitlines() if line.startswith("#")) or NO_GROUPING_HEADER
        out = header + yaml.safe_dump({**data, "cids": want}, allow_unicode=True, sort_keys=False)
    if crlf:
        out = out.replace("\n", "\r\n")
    write_bytes(path, (bom + out).encode("utf-8"))
    return new, kept


def _append_cids(text: str, new: list[int], want: list[int]) -> str | None:
    """The minimal text change that adds ``new`` to the ``cids`` list; None when the file is unusual."""
    import yaml

    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    lines = text.splitlines(keepends=True)
    offs = [0]
    for line in lines:
        offs.append(offs[-1] + len(line))

    def pos(mark: yaml.Mark) -> int:
        return offs[mark.line] + mark.column

    if root is None:
        out = text + ("" if not text or text.endswith("\n") else "\n") + "cids:\n" + "".join(f"- {c}\n" for c in new)
    elif not isinstance(root, yaml.MappingNode):
        return None
    else:
        pair = next(((k, v) for k, v in root.value if isinstance(k, yaml.ScalarNode) and k.value == "cids"), None)
        if pair is None:
            out = text + ("" if not text or text.endswith("\n") else "\n") + "cids:\n" + "".join(f"- {c}\n" for c in new)
        else:
            key, value = pair
            if isinstance(value, yaml.SequenceNode) and value.flow_style:
                flow = "[" + ", ".join(str(c) for c in want) + "]"
                out = text[:pos(value.start_mark)] + flow + text[pos(value.end_mark):]
            elif isinstance(value, yaml.SequenceNode) and value.value:
                last = value.value[-1]
                line = lines[last.start_mark.line]
                dash = line.index("-")
                gap = last.start_mark.column - dash
                at = offs[last.end_mark.line + (1 if last.end_mark.column else 0)]
                if last.end_mark.line == len(lines) - 1 and not text.endswith("\n"):
                    text, at = text + "\n", len(text) + 1
                block = "".join(" " * dash + "-" + " " * max(gap - 1, 1) + str(c) + "\n" for c in new)
                out = text[:at] + block + text[at:]
            elif isinstance(value, yaml.ScalarNode) and value.tag == "tag:yaml.org,2002:null":
                flow = "[" + ", ".join(str(c) for c in want) + "]"
                if value.value:  # 「cids: ~」「cids: null」
                    out = text[:pos(value.start_mark)] + flow + text[pos(value.end_mark):]
                else:  # 「cids:」だけ
                    after = pos(key.end_mark)
                    colon = after + text[after:].index(":") + 1
                    out = text[:colon] + " " + flow + text[colon:]
            else:
                return None
    try:
        check = yaml.safe_load(out) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(check, dict) or [int(c) for c in check.get("cids") or []] != want:
        return None
    return out


def write_back_failures(result: dict[str, Any], token: str, not_written: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    failed = result.get("failed") if isinstance(result.get("failed"), dict) else {}
    for value in failed.values():
        if isinstance(value, dict) and str(value.get("id")) in not_written and value.get("token", token) == token:
            out[str(value["id"])] = str(value.get("error") or "理由は記録されていません")
    return out


def not_written_note(not_written: list[str], failures: dict[str, str]) -> str:
    server = [i for i in not_written if i in failures and SERVER_ERROR.search(failures[i])]
    refused = [i for i in not_written if i in failures and i not in server]
    unsent = [i for i in not_written if i not in failures]
    note = ""
    if unsent:
        note += (f"逸脱 {len(unsent)} 件（{', '.join(unsent)}）は、まだ Coverity に書き戻していません。書き戻すときは、"
                 "Coverity にトリアージを書き戻せる道具に plan_file と同じ確認の文字列を渡して done まで呼び、そのあと"
                 "同じ確認の文字列で apply-code をもう一度動かす（push はし直しません）。")
    if refused:
        note += (f"逸脱 {len(refused)} 件は、Coverity が書き戻しを断りました（"
                 + "、".join(f"{i}: {failures[i]}" for i in refused) + "）。値を直して preview からやり直す。")
    if server:
        note += (f"逸脱 {len(server)} 件は、Coverity の一時的な失敗で書き戻せませんでした（"
                 + "、".join(f"{i}: {failures[i]}" for i in server) + "）。少し待って、同じ確認の文字列で書き戻しを呼び直し、"
                 "そのあと apply-code をもう一度動かす。")
    return note


def metrics_records(run: RunFolder, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    entries = ([("deviation", e) for e in plan.get("triage") or []]
               + [("fix", e) for e in plan.get("code") or [] if e.get("kind") == "fix"]
               + [("reject", e) for e in plan.get("reject") or []])
    for approval, entry in entries:
        result = run.result(entry["id"]) or {}
        record = {"item": entry["id"], "cids": entry["cids"], "recommendation": result.get("recommendation"),
                  "confidence": result.get("confidence"),
                  "shown_confidence": shown_confidence(run, entry["id"], result)["confidence"],
                  "approval": approval, "at": now()}
        if approval == "deviation":
            proposal = result.get("deviation") if isinstance(result.get("deviation"), dict) else {}
            record["comment_edited"] = str(entry.get("comment", "")).strip() != str(proposal.get("comment", "")).strip()
            record["attributes_edited"] = any(entry.get(k) != proposal.get(k)
                                              for k in ("classification", "action", "severity"))
        out[entry["id"]] = record
    return out


def cmd_apply_code(args: argparse.Namespace) -> dict[str, Any]:
    run = RunFolder(args.run)
    path = run.dir / PLAN_FILE
    token = (args.token or "").strip()
    if not path.is_file() or plantoken.token_for_bytes(path.read_bytes()) != token:
        raise ApplyError("確認の文字列が、反映の内容（apply-plan.json）と合いません。何もしていません。preview からやり直してください")
    plan = json.loads(path.read_bytes().decode("utf-8"))
    settings = load_settings(run.repo)
    record = load_record(run.dir)
    written, write_back = written_ids(run.dir)
    triage_ids = [str(e["id"]) for e in plan.get("triage") or []]
    registered = [i for i in triage_ids if i in written]
    not_registered = [i for i in triage_ids if i not in written]
    for item_id in registered:
        record["applied"].setdefault(item_id, {}).setdefault("coverity", {"at": now()})
    repeated = token in record["results"]
    out: dict[str, Any] = {"ok": True, "run_id": run.run_id, "vcs": settings["vcs"]["type"], "repeated": repeated,
                           "pull_requests": [], "pushed_before": [], "svn_applied": [], "svn_applied_before": [],
                           "coverity": {"registered": registered, "not_registered": not_registered},
                           "rejected": [], "no_grouping_added": [], "errors": []}
    code = [e for e in plan.get("code") or [] if isinstance(e, dict)]
    try:
        if code:
            if settings["vcs"]["type"] == "git":
                apply_git(run, settings, record, code, out)
            else:
                apply_svn(run, record, code, out)
    except Exception:
        write_json(run.dir / CODE_RESULT, record)  # keep what was pushed before the failure
        raise
    rejects = [e for e in plan.get("reject") or [] if isinstance(e, dict)]
    groups = [int(c) for e in rejects if e.get("group") for c in e.get("cids") or []]
    grouping_ok = True
    if groups:
        try:
            added, kept = add_no_grouping(run.repo, groups)
            out["no_grouping_added"] = added
            if not kept:
                out["errors"].append("no-grouping.yaml の注釈を残せなかったので、ファイル全体を書き直しました（値は正しく入っています）")
        except Exception as exc:  # noqa: BLE001 - a broken file must not lose the record of what was pushed
            grouping_ok = False  # recorded as not applied, so the next apply tries again
            out["errors"].append(f"no-grouping.yaml に CID を足せませんでした: {exc}")
    for entry in rejects:
        if entry.get("group") and not grouping_ok:
            continue
        record["applied"].setdefault(entry["id"], {}).setdefault("reject", {"at": now()})
        out["rejected"].append(entry["id"])
    if settings["options"]["metrics"]:
        record["metrics"].update(metrics_records(run, plan))
    failures = write_back_failures(write_back, token, not_registered)
    if failures:
        out["coverity"]["failures"] = failures
    record["results"][token] = {"at": now(), "pull_requests": [p["branch"] for p in out["pull_requests"]],
                                "registered": registered, "errors": out["errors"]}
    write_json(run.dir / CODE_RESULT, record)
    steps = []
    if out["errors"]:
        steps.append("errors をそのまま人に伝える（反映できなかったものは、もう一度反映を頼むと、残りだけを反映する）。")
    steps.append(not_written_note(not_registered, failures))
    if out["pull_requests"]:
        steps.append("pull_requests の各項目で、branch から base へのプルリクエストを title と body で作る（使える GitHub の道具が"
                     "あればそれで。なければ compare_url を示し、null ならブランチの名前を伝える）。GitHub のトークンは聞かない。")
    if out["svn_applied"]:
        steps.append("svn の作業コピーに差分を当てた。内容を確かめてから、ご自身でコミットするよう人に伝える。")
    if repeated and not (out["pull_requests"] or out["svn_applied"]):
        steps.append("この確認の文字列の反映は前に済んでいます。push も差分の当てもし直していません。プルリクエストは作り直さない。")
    out["next"] = "".join(s for s in steps if s) or "反映は終わりました"
    return out


# ---- knowledge ---------------------------------------------------------------------------------------------


def issue_info(run: RunFolder, item_id: str, cids: list[int]) -> dict[str, Any]:
    for cid in cids:
        detail = read_json(run.item_dir(item_id) / f"issue-{cid}.json", {})
        issue = detail.get("issue") if isinstance(detail, dict) else None
        if isinstance(issue, dict):
            return issue
    mapped = read_json(run.dir / "issues.mapped.json") or read_json(run.dir / "issues.json") or {}
    issues = mapped.get("issues") if isinstance(mapped, dict) else mapped
    for issue in issues if isinstance(issues, list) else []:
        if isinstance(issue, dict) and issue.get("cid") in cids:
            return issue
    return {}


def cmd_knowledge_candidates(args: argparse.Namespace) -> dict[str, Any]:
    run = RunFolder(args.run)
    plan = read_json(run.dir / PLAN_FILE)
    if not isinstance(plan, dict):
        raise ApplyError("反映の内容（apply-plan.json）がありません。反映の後に使います")
    approvals = read_json(run.dir / "approvals.json", {})
    entries = approvals.get("items") if isinstance(approvals, dict) and isinstance(approvals.get("items"), dict) else {}
    found = []
    for item_id, record in metrics_records(run, plan).items():
        changed = record["approval"] != record["recommendation"]
        edited = record.get("comment_edited") or record.get("attributes_edited")
        if not (changed or edited or record["approval"] == "reject"):
            continue
        result = run.result(item_id) or {}
        issue = issue_info(run, item_id, record["cids"])
        entry = {"item": item_id, "cids": record["cids"], "checker": issue.get("checker"), "file": issue.get("file"),
                 "function": issue.get("function"), "ai_summary": (result.get("verdict") or {}).get("summary"),
                 "ai_recommendation": LABELS.get(record["recommendation"], record["recommendation"]),
                 "person_decision": LABELS[record["approval"]]}
        if record.get("comment_edited"):
            edit = (entries.get(item_id) or {}).get("deviation") or {}
            entry["ai_comment"] = (result.get("deviation") or {}).get("comment")
            entry["person_comment"] = edit.get("comment")
        found.append(entry)
    path = run.repo / CONFIG_DIR / KNOWLEDGE_FILE
    return {"ok": True, "candidates": found, "knowledge_file": str(path),
            "current_knowledge": path.read_text(encoding="utf-8") if path.is_file() else "",
            "next": ("候補から、ほかの警告にも使える短い知識（1項目1行）を考え、current_knowledge と重ならないものを人に見せ、"
                     "人が選んだものだけを add-knowledge で足す" if found else "候補はありません")}


def cmd_add_knowledge(args: argparse.Namespace) -> dict[str, Any]:
    run = RunFolder(args.run)
    entries = list(args.entry or [])
    if args.stdin:
        payload = read_stdin_json()
        values = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ApplyError('標準入力は {"entries": ["知識", ...]} の形の JSON にしてください')
        entries += values
    lines = [" ".join(e.split()) for e in entries if e.strip()]
    if not lines:
        raise ApplyError("足す知識がありません（--entry で1項目ずつ渡します）")
    path = run.repo / CONFIG_DIR / KNOWLEDGE_FILE
    current = path.read_bytes().decode("utf-8-sig") if path.is_file() else KNOWLEDGE_TEMPLATE
    block = f"\n## 追記（{date.today().isoformat()}、実行 {run.run_id}）\n\n" + "".join(f"- {line}\n" for line in lines)
    write_bytes(path, (current.rstrip("\n") + "\n" + block).encode("utf-8"))
    return {"ok": True, "file": str(path), "added": len(lines),
            "next": "knowledge.md をコミットしてチームで共有するよう人に伝える"}


# ---- stats -----------------------------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def cmd_stats(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_root(args.repo)
    settings = load_settings(repo)
    out_dir = output_dir(repo, settings)
    records: list[dict[str, Any]] = []
    runs = 0
    seconds: list[float] = []
    for folder in sorted(p for p in out_dir.glob("*") if p.is_dir() and RUN_ID.match(p.name)) if out_dir.is_dir() else []:
        record = read_json(folder / CODE_RESULT, {})
        metrics = record.get("metrics") if isinstance(record, dict) and isinstance(record.get("metrics"), dict) else {}
        if not metrics:
            continue
        runs += 1
        try:
            times = RunFolder(folder).seconds()
        except ApplyError:  # run.json cannot be read: the records count, the times are not known
            times = {}
        for item_id, value in metrics.items():
            if isinstance(value, dict) and value.get("approval") in LABELS:
                records.append(value)
                if item_id in times:
                    seconds.append(times[item_id])
    approvals = {k: 0 for k in LABELS}
    by_confidence: dict[str, list[int]] = {}
    taken = edited = deviations = 0
    for r in records:
        approvals[r["approval"]] += 1
        # the confidence the person saw (records made before it was kept: the worker's)
        stats = by_confidence.setdefault(str(r.get("shown_confidence") or r.get("confidence")), [0, 0])
        stats[0] += 1
        if r["approval"] == r.get("recommendation"):
            taken += 1
            stats[1] += 1
        if r["approval"] == "deviation":
            deviations += 1
            edited += bool(r.get("comment_edited") or r.get("attributes_edited"))
    return {"ok": True, "metrics_option": settings["options"]["metrics"], "runs": runs, "items": len(records),
            "cids": sum(len(r.get("cids") or []) for r in records), "approvals": approvals,
            "recommendation_taken_rate": _rate(taken, len(records)),
            "taken_rate_by_confidence": {k: _rate(v[1], v[0]) for k, v in by_confidence.items()},
            "deviation_edited_rate": _rate(edited, deviations),
            "average_seconds_per_item": round(sum(seconds) / len(seconds), 1) if seconds else None,
            "next": ("集計を人に伝える（割合は 0〜1）" if records else
                     "記録がありません。config.yaml の options.metrics を true にすると、反映のたびに記録します")}


# ---- the command line ---------------------------------------------------------------------------------


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def usage_text(message: str) -> str:
    head, sep, rest = message.partition("unrecognized arguments:")
    if not sep:
        return message
    return head + "知らない引数があります: " + " ".join(t.split("=", 1)[0] if t.startswith("-") else "****"
                                                        for t in rest.split())


def parser() -> Parser:
    top = Parser(prog="apply.py", description="承認したトリアージの結果を反映する")
    sub = top.add_subparsers(dest="command", required=True, parser_class=Parser)
    p = sub.add_parser("runs", help="一覧のある実行フォルダ（新しい順）")
    p.add_argument("--repo", help="リポジトリのフォルダ（省略すると今のフォルダ）")
    p.set_defaults(func=cmd_runs)
    p = sub.add_parser("preview", help="反映の内容（apply-plan.json）と確認の文字列を作る")
    p.add_argument("--run", required=True, help="実行フォルダ")
    p.set_defaults(func=cmd_preview)
    p = sub.add_parser("apply-code", help="修正を反映する（git：ブランチの push、svn：作業コピーに差分を当てる）")
    p.add_argument("--run", required=True)
    p.add_argument("--token", required=True, help="preview が返した確認の文字列")
    p.set_defaults(func=cmd_apply_code)
    p = sub.add_parser("knowledge-candidates", help="知識に足す案の候補（人が AI の案を変えた作業）")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_knowledge_candidates)
    p = sub.add_parser("add-knowledge", help="knowledge.md に知識を足す")
    p.add_argument("--run", required=True)
    p.add_argument("--entry", action="append", help="足す知識（1項目ずつ。何度でも）")
    p.add_argument("--stdin", action="store_true", help='標準入力から {"entries": [...]} を読む')
    p.set_defaults(func=cmd_add_knowledge)
    p = sub.add_parser("stats", help="推奨が採られた割合などの集計")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_stats)
    return top


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    try:
        args = parser().parse_args(argv)
        result = args.func(args)
    except ApplyError as exc:
        emit({"ok": False, "error": str(exc), "problems": exc.problems, **exc.details})
        return 1
    except UsageError as exc:
        emit({"ok": False, "error": f"コマンドの引数が正しくありません: {usage_text(str(exc))}", "problems": []})
        return 1
    except OSError as exc:
        emit({"ok": False, "error": f"ファイルやコマンドの操作に失敗しました: {exc}", "problems": []})
        return 1
    except Exception as exc:  # noqa: BLE001 - never a traceback on stdout
        emit({"ok": False, "error": f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}", "problems": []})
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
