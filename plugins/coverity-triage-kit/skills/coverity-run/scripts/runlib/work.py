"""Job bodies around the work copies: prepare a copy for an item, save an item's fix, build merged fixes.

Every git / svn operation holds ``work/vcs.lock``, so that two jobs never change the repository's
metadata at the same time. These run in detached processes (jobs.py); they never write run.json.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from . import jobs
from .common import RunError, file_lock, read_json, write_json, write_text
from .state import Run, is_group
from .vcs import make_vcs

VCS_LOCK_WAIT = 1800  # creating a work copy of a large repository can take long
VCS_LOCK_STALE = 7200
REWORK_FRESH = "rework-fresh"
PREVIOUS_RESULT = "result.previous.json"


def vcs_lock(run: Run):
    return file_lock(run.dir / "work" / "vcs", timeout=VCS_LOCK_WAIT, stale=VCS_LOCK_STALE)


def has_copy(folder: Path) -> bool:
    return (folder / ".git").exists() or (folder / ".svn").exists()


@jobs.register("copy")
def prepare_copy(run_dir: Path, args: dict[str, Any], handle: jobs.Handle) -> dict[str, Any]:
    """Reset the slot's work copy to the latest revision (and the analyzed copy when needed). For an item the
    review sent back, the earlier fix is applied first, so that the worker changes it."""
    run = Run(run_dir)
    item = run.item(args["item"])
    vcs = make_vcs(run.repo, run.settings)
    latest = run.state.get("latest_revision") or ""
    slot = args["slot"]
    fix_dir = run.work(f"w{slot}")
    created = not has_copy(fix_dir)
    fresh = False
    marker = run.item_dir(item["id"]) / REWORK_FRESH
    with vcs_lock(run):
        vcs.reset_copy(fix_dir, latest)
        marker.unlink(missing_ok=True)
        previous = run.patch_file(item["id"])
        if item.get("rework") and previous.is_file():
            if vcs.apply_to_copy(fix_dir, previous):
                vcs.reset_copy(fix_dir, latest)
                fresh = True
        elif item.get("rework"):
            fresh = True
        if fresh:
            write_text(marker, "前の修正を当てられなかったため、最新のリビジョンから作り直します\n")
        if run.settings["options"]["annotation"]:
            vcs.reset_copy(run.work(f"n{slot}"), latest)
        analyzed = run.state.get("analyzed_revision")
        if analyzed:
            copy, rev_file = run.work("a"), run.work("a.rev")
            current = rev_file.read_text(encoding="utf-8").strip() if rev_file.is_file() else ""
            if args.get("reset_analyzed") or current != analyzed or not has_copy(copy):
                vcs.reset_copy(copy, analyzed)
                write_text(rev_file, analyzed + "\n")
    return {"created": created, "fresh": fresh}


def _copy_changed(src: Path, files: list[str], dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    for rel in files:
        if (src / rel).is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dest / rel)


def _branch(run: Run, item_id: str, kept: list[int], kind: str) -> str | None:
    settings = run.settings
    if settings["options"]["per_run_branch"] or settings["vcs"]["type"] != "git":
        return None
    name = f"cid-{kept[0]}" if not is_group(item_id) else f"{run.state['run_id']}-{item_id}"
    return settings["vcs"]["branch_prefix"] + name + ("-annotation" if kind == "annotation" else "")


@jobs.register("save")
def save_item(run_dir: Path, args: dict[str, Any], handle: jobs.Handle) -> dict[str, Any]:
    """Check the work copy, then save the fix: commit and branch (git), patch, the changed files in fixed/.

    ``problems`` in the result are for the worker to fix (nothing is saved then)."""
    run = Run(run_dir)
    item_id = args["item"]
    item = run.item(item_id)
    settings = run.settings
    vcs = make_vcs(run.repo, settings)
    result = read_json(run.result_file(item_id))
    fix = result.get("fix") or {}
    slot = args["slot"]
    fix_dir, note_dir = run.work(f"w{slot}"), run.work(f"n{slot}")
    annotation = settings["options"]["annotation"]
    with vcs_lock(run):
        changed = vcs.changed_files(fix_dir)
        annotated = vcs.changed_files(note_dir) if annotation else []
        analyzed_dirty = bool(run.state.get("analyzed_revision")) and has_copy(run.work("a")) and \
            bool(vcs.changed_files(run.work("a")))
    problems = []
    if not changed and not fix.get("already_fixed_on_latest"):
        problems.append(f"修正するフォルダ {fix_dir} に変更がありません。そのフォルダのファイルを直してください"
                        "（最新のリビジョンでもう直っているときは、fix.already_fixed_on_latest を true にします）")
    if annotation and not annotated:
        problems.append(f"注釈を入れるフォルダ {note_dir} に注釈が入っていません")
    if problems:
        return {"problems": problems}
    warnings = []
    if changed and fix.get("already_fixed_on_latest"):
        warnings.append("「最新のリビジョンで直っている」とありますが、修正するフォルダに変更があります（変更を差分にしました）")
    if analyzed_dirty:
        warnings.append("調べるコード（解析したリビジョンのコピー）が変わっていました。次の作業の前に元に戻します")
    if item.get("repo_fingerprint") and vcs.fingerprint() != item["repo_fingerprint"]:
        warnings.append("調べている間に、利用者のリポジトリのファイルが変わりました。誤って変えていないか確かめてください")

    excluded = [c for c in result.get("group_excluded_cids") or [] if c in item["cids"]]
    kept = [c for c in item["cids"] if c not in excluded]
    for cid in excluded:
        src = run.item_dir(item_id) / f"issue-{cid}.json"
        if src.is_file():
            target = run.item_dir(str(cid))
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target / src.name)
    earlier = read_json(run.item_dir(item_id) / "saved.json", {})
    saved: dict[str, Any] = {}
    latest = run.state.get("latest_revision") or ""
    with vcs_lock(run):
        for kind in ("fix", "annotation"):  # a fix made again takes the place (and branch) of the earlier one
            branch = (earlier.get(kind) or {}).get("branch")
            if branch:
                vcs.delete_branch(branch)
        for kind, folder, files in (("fix", fix_dir, changed), ("annotation", note_dir, annotated)):
            patch = run.patch_file(item_id, kind)
            if not files:
                patch.unlink(missing_ok=True)
                shutil.rmtree(run.dir / "fixed" / item_id / kind, ignore_errors=True)
                continue
            label = "修正" if kind == "fix" else "逸脱の注釈"
            body = fix.get("summary", "") if kind == "fix" else (result.get("deviation") or {}).get("classification", "")
            message = f"Coverity CID {', '.join(map(str, kept))}: {label}\n\n{body}\n"
            saved[kind] = vcs.save(folder, latest, message, _branch(run, item_id, kept, kind),
                                   f"refs/triage/{run.state['run_id']}/{item_id}-{kind}", patch)
            _copy_changed(folder, files, run.dir / "fixed" / item_id / kind)
    write_json(run.item_dir(item_id) / "saved.json", saved)
    return {"saved": saved, "warnings": warnings, "excluded": excluded, "kept": kept,
            "analyzed_dirty": analyzed_dirty, "files": changed}


@jobs.register("merge")
def build_merges(run_dir: Path, args: dict[str, Any], handle: jobs.Handle) -> dict[str, Any]:
    """Make the fix of each merged item from its members' patches (fix_source item:<ID> or combine).
    An item whose patches do not apply together is left to a worker (``built: false``)."""
    run = Run(run_dir)
    settings = run.settings
    vcs = make_vcs(run.repo, settings)
    latest = run.state.get("latest_revision") or ""
    merged_info = read_json(run.dir / "integration" / "merged.json", {})
    copy = run.work("m")
    out: dict[str, Any] = {}
    for entry in args.get("merges") or []:
        item_id, members, source = entry["item"], entry["members"], entry["source"]
        info = merged_info.get(item_id) or {}
        result = dict(info.get("result") or {})
        chosen = [source[5:]] if source.startswith("item:") else members
        patches = [run.patch_file(m) for m in chosen if run.patch_file(m).is_file()]
        built, reason = True, ""
        with vcs_lock(run):
            vcs.reset_copy(copy, latest)
            for patch in patches:
                problem = vcs.apply_to_copy(copy, patch)
                if problem:
                    built, reason = False, f"修正を合わせられませんでした: {problem[:300]}"
                    break
            changed = vcs.changed_files(copy) if built else []
            if built and not changed and not (result.get("fix") or {}).get("already_fixed_on_latest"):
                built, reason = False, "合わせた修正に変更がありません"
            if not built:
                vcs.reset_copy(copy, latest)
                out[item_id] = {"built": False, "reason": reason}
                continue
            item = run.find(item_id) or {"cids": [c for m in members for c in (run.find(m) or {}).get("cids", [])]}
            write_json(run.result_file(item_id), {**result, "item": item_id, "group_excluded_cids": []})
            saved: dict[str, Any] = {}
            if changed:
                message = (f"Coverity CID {', '.join(map(str, item['cids']))}: 修正（統合）\n\n"
                           f"{(result.get('fix') or {}).get('summary', '')}\n")
                branch = None if settings["options"]["per_run_branch"] or settings["vcs"]["type"] != "git" else \
                    f"{settings['vcs']['branch_prefix']}{run.state['run_id']}-{item_id}"
                saved["fix"] = vcs.save(copy, latest, message, branch,
                                        f"refs/triage/{run.state['run_id']}/{item_id}-fix", run.patch_file(item_id))
                _copy_changed(copy, changed, run.dir / "fixed" / item_id / "fix")
            write_json(run.item_dir(item_id) / "saved.json", saved)
            out[item_id] = {"built": True, "saved": saved, "files": changed}
    return {"items": out}


def check_issue_file(path: Path) -> bool:
    """Whether an issue-<CID>.json holds a warning path (False: missing, broken, or only an error)."""
    try:
        data = read_json(path)
    except RunError:
        return False
    return isinstance(data, dict) and isinstance(data.get("issue"), dict)
