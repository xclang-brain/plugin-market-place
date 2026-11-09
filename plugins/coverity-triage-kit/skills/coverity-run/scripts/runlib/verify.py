"""Option verify.default: check the fixes by a build (and a Coverity re-analysis), once per run.

Every fix patch is applied together to a copy of the latest code, which is built (and analyzed) once.
For the re-analysis the unmodified latest code is analyzed once as the baseline. Results are given to
the items: a CID is resolved when its warning is gone; build errors and new warnings go to the items
that changed the files involved. Only the commands written in the settings are run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import jobs
from .common import RunError, build_env, display_text, kill_tree, read_json, remove_tree, write_json
from .state import Run
from .vcs import make_vcs

LOG_TAIL = 3000


def safe_relpath(path: str) -> str:
    """A folder inside the repository, written relative to it."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path) or ".." in parts:
        raise RunError(f"verify.build_dir はリポジトリからの相対パスで書いてください: {path}")
    return "/".join(parts)


def intermediate_dir(cov_analyze_args: str) -> str:
    """The ``--dir`` given to cov-analyze (cov-format-errors and cov-commit-defects read it too)."""
    match = re.search(r'(?:^|\s)--dir(?:=|\s+)(?:"([^"]*)"|(\S+))', cov_analyze_args)
    if match:
        return match.group(1) if match.group(1) is not None else match.group(2)
    raise RunError("verify.cov_analyze_args に --dir（中間ディレクトリ）を書いてください")


def no_ticker(args: str) -> str:
    """``--ticker-mode none`` unless the settings choose a mode: the progress bars would fill the log."""
    return args if re.search(r"(?:^|\s)--ticker-mode\b", args) else f"--ticker-mode none {args}".rstrip()


def tail(log: Path) -> str:
    return display_text(log.read_bytes()[-LOG_TAIL:]) if log.is_file() else ""


def prefix_lines(verify: dict[str, Any], root: Path) -> list[str]:
    """Environment setup, then the move to the build folder (absolute, so a setup that moves still works)."""
    steps = []
    if verify.get("setup_command"):
        setup = verify["setup_command"].replace("{root}", str(root))
        steps.append(("call " if sys.platform == "win32" else "") + setup)
    if verify.get("build_dir"):
        target = root / safe_relpath(verify["build_dir"])
        steps.append(f'cd /d "{target}"' if sys.platform == "win32" else f'cd "{target}"')
    return steps


def build_line(verify: dict[str, Any], root: Path, mode: str, json_out: Path | None = None) -> str:
    if not verify.get("build_command"):
        raise RunError("verify.build_command が設定されていません")
    steps = prefix_lines(verify, root)
    if mode == "build":
        steps.append(verify["build_command"])
    elif mode == "build+analyze":
        idir = intermediate_dir(verify["cov_analyze_args"])
        steps += [f"cov-build {verify['cov_build_args']} {verify['build_command']}",
                  f"cov-analyze {no_ticker(verify['cov_analyze_args'])}",
                  f'cov-format-errors --dir "{idir}" --json-output-v7 "{json_out}"']
    else:
        raise RunError("検証のしかたは build か build+analyze です")
    return " && ".join(steps)


def run_shell(line: str, cwd: Path, log: Path, env: dict[str, str], handle: jobs.Handle | None,
              deadline: float | None = None) -> int | None:
    """Run one shell line into the log in its own process group. None: stopped at the deadline."""
    extra: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                             if sys.platform == "win32" else {"start_new_session": True})
    with open(log, "ab") as f:
        proc = subprocess.Popen(line, cwd=cwd, shell=True, stdout=f, stderr=subprocess.STDOUT, env=env, **extra)
        if handle:
            handle.add_child(proc.pid)
        try:
            while proc.poll() is None:
                if deadline is not None and time.monotonic() > deadline:
                    kill_tree(proc.pid)
                    proc.wait()
                    return None
                time.sleep(0.5)
        except BaseException:
            kill_tree(proc.pid)
            raise
    return proc.returncode


def analysis_error(json_path: Path) -> str | None:
    """The ``error`` of cov-format-errors --json-output-v7 (with an error the list is empty, which must not
    count as every warning being gone)."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        return f"解析の結果を読めません: {exc}"
    error = data.get("error") if isinstance(data, dict) else None
    if not error:
        return None
    return error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)


def load_analysis(json_path: Path) -> list[dict[str, Any]]:
    data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    return [{"merge_key": i.get("mergeKey"), "checker": i.get("checkerName", ""),
             "file": (i.get("mainEventFilePathname") or i.get("strippedMainEventFilePathname") or "").replace("\\", "/"),
             "function": i.get("functionDisplayName") or i.get("functionName"), "line": i.get("mainEventLineNumber")}
            for i in data.get("issues", [])]


def matches(issue: dict[str, Any], record: dict[str, Any]) -> bool:
    if issue.get("merge_key") and record.get("merge_key"):
        return issue["merge_key"] == record["merge_key"]
    file = str(issue.get("file", "")).replace("\\", "/").lstrip("/")
    return (record["checker"] == issue.get("checker") and record["file"].endswith(file)
            and (issue.get("function") is None or record.get("function") == issue.get("function")))


def _key(record: dict[str, Any]) -> Any:
    return record.get("merge_key") or (record["checker"], record["file"], record.get("function"))


def _touches(record_file: str, files: list[str]) -> bool:
    return any(record_file.lower().endswith(f.replace("\\", "/").lower()) for f in files)


def apply_patch(root: Path, patch: str, strip: int) -> str | None:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(root.parent))  # never treat the copy as a repository
    proc = subprocess.run(["git", "apply", f"-p{strip}", "--whitespace=nowarn", patch], cwd=root, env=env,
                          capture_output=True, check=False)
    return None if proc.returncode == 0 else (proc.stderr.decode("utf-8", "replace").strip() or "差分を当てられませんでした")


def _own_dirs(verify: dict[str, Any], name: str) -> dict[str, Any]:
    """An absolute --dir is made one per copy (before and after must not share it)."""
    idir = intermediate_dir(verify["cov_analyze_args"])
    if not Path(idir).is_absolute():
        return verify
    own = f"{idir.rstrip('/').rstrip(chr(92))}-{name}"
    swap = (lambda args: args.replace(f'"{idir}"', f'"{own}"').replace(idir, own))
    return {**verify, "cov_build_args": swap(verify["cov_build_args"]), "cov_analyze_args": swap(verify["cov_analyze_args"])}


@jobs.register("verify")
def run_verify(run_dir: Path, args: dict[str, Any], handle: jobs.Handle) -> dict[str, Any]:
    run = Run(run_dir)
    settings = run.settings
    verify = settings["verify"]
    mode = run.state.get("verify_mode", "none")
    mapped = {i["cid"]: i for i in read_json(run.dir / "issues.mapped.json", [])}
    vcs = make_vcs(run.repo, settings)
    strip = 1 if vcs.kind == "git" else 0
    batch = []
    for item in run.state["items"]:
        if item["status"] != "done":
            continue
        saved = read_json(run.item_dir(item["id"]) / "saved.json", {})
        if saved.get("fix"):
            batch.append({"item": item["id"], "patch": saved["fix"]["patch"], "files": saved["fix"]["files"],
                          "issues": [mapped[c] for c in item["cids"] if c in mapped], "strip": strip})
    if not batch:
        write_json(run.dir / "verify.json", {})
        return {"build_ok": None, "items": [], "message": "確かめる修正案がありません"}
    drop = (settings["coverity"]["key_env"], "COVERITY_PASSPHRASE")
    env = build_env(drop=drop)
    started = time.time()
    latest = run.work("vl")
    remove_tree(latest)  # a folder left behind (a file a build server still holds) would mix into the export
    vcs.export(run.state.get("latest_revision") or "", latest)
    logs = run.dir / "jobs"
    results: dict[str, dict[str, Any]] = {}
    error = ""
    baseline: list[dict[str, Any]] = []
    if mode == "build+analyze":
        before = run.work("vb")
        remove_tree(before)
        shutil.copytree(latest, before)
        before_log, before_json = logs / "verify-before.log", run.work("vb.json")
        before_log.unlink(missing_ok=True)
        code = run_shell(build_line(_own_dirs(verify, "before"), before, mode, before_json), before, before_log, env, handle)
        problem = analysis_error(before_json) if code == 0 else "ビルドか解析に失敗しました"
        if problem:
            error = "修正前のコードのビルド・解析に失敗しました（設定のコマンドを確かめてください）"
            for b in batch:
                results[b["item"]] = {"mode": mode, "batch": True, "build_ok": None, "problems": [],
                                      "note": error + "。修正案は確かめられませんでした", "log": str(before_log),
                                      "log_tail": tail(before_log)}
            write_json(run.dir / "verify.json", results)
            remove_tree(latest, check=False)
            return {"build_ok": None, "items": sorted(results), "error": error, "log": str(before_log),
                    "seconds": round(time.time() - started)}
        baseline = load_analysis(before_json)
    after = run.work("va")
    remove_tree(after)
    shutil.copytree(latest, after)
    applied = []
    for b in batch:
        problem = apply_patch(after, b["patch"], b["strip"])
        if problem:
            results[b["item"]] = {"mode": mode, "batch": True, "applied": False, "build_ok": None, "problems": [],
                                  "note": "ほかの修正案と同じ箇所を変えているため、まとめた確かめに入れられませんでした",
                                  "detail": problem[:500]}
        else:
            applied.append(b)
    log, after_json = logs / "verify-after.log", run.work("va.json")
    log.unlink(missing_ok=True)
    code = run_shell(build_line(_own_dirs(verify, "after"), after, mode, after_json), after, log, env, handle)
    build_ok = code == 0
    if build_ok and mode == "build+analyze":
        problem = analysis_error(after_json)
        if problem:
            with open(log, "ab") as f:
                f.write(f"cov-format-errors の結果に error があります: {problem}\n".encode("utf-8"))
            build_ok = False
    if not build_ok:
        _blame(results, applied, log, mode)
    elif mode == "build":
        for b in applied:
            results[b["item"]] = {"mode": mode, "batch": True, "applied": True, "build_ok": True, "problems": [],
                                  "log": str(log)}
    else:
        _assign(results, applied, baseline, load_analysis(after_json), log, mode)
    write_json(run.dir / "verify.json", results)
    remove_tree(latest, check=False)
    return {"build_ok": build_ok, "items": sorted(results), "error": error, "log": str(log),
            "seconds": round(time.time() - started)}


def _blame(results: dict[str, Any], applied: list[dict[str, Any]], log: Path, mode: str) -> None:
    """A failed build: the fixes whose files appear in the error lines are blamed."""
    text = log.read_text(encoding="utf-8", errors="replace").lower() if log.is_file() else ""
    lines = [line for line in text.splitlines() if "error" in line]
    blamed = [b for b in applied if any(Path(f).name.lower() in line for f in b["files"] for line in lines)]
    for b in applied:
        entry: dict[str, Any] = {"mode": mode, "batch": True, "applied": True, "log": str(log)}
        if blamed and b not in blamed:
            entry.update(build_ok=None, problems=[], note="ほかの修正案のビルドエラーのため、この修正案は確かめられませんでした")
        elif blamed:
            entry.update(build_ok=False, log_tail=tail(log), problems=["ビルドエラーが、この修正案の変えたファイルで出ています"])
        else:
            entry.update(build_ok=False, log_tail=tail(log), problems=["ビルドに失敗しました（原因の修正案は分かりません）"])
        results[b["item"]] = entry


def _assign(results: dict[str, Any], applied: list[dict[str, Any]], baseline: list[dict[str, Any]],
            records: list[dict[str, Any]], log: Path, mode: str) -> None:
    known = {_key(r) for r in baseline}
    new = [r for r in records if _key(r) not in known]
    for b in applied:
        remaining = [i["cid"] for i in b["issues"] if any(matches(i, r) for r in records)]
        unconfirmed = [i["cid"] for i in b["issues"] if i["cid"] not in remaining and not any(matches(i, r) for r in baseline)]
        own = [r for r in new if _touches(r["file"], b["files"])]
        problems = []
        if remaining:
            problems.append(f"警告が残っています: CID {remaining}")
        if own:
            problems.append(f"この修正案が変えたファイルで、新しい警告が {len(own)} 件出ています")
        results[b["item"]] = {"mode": mode, "batch": True, "applied": True, "build_ok": True,
                              "resolved_cids": [i["cid"] for i in b["issues"] if i["cid"] not in remaining + unconfirmed],
                              "remaining_cids": remaining, "unconfirmed_cids": unconfirmed, "new_issues": own[:20],
                              "new_issue_count": len(own), "problems": problems, "log": str(log)}
