"""Initial state for the tests: the envmon fixture as repositories, analyzed and committed.

``python3 -m coverity_emulator.seed --url <emulator URL> --workdir <dir> [--no-misra] [--no-cert] [--no-svn]``

1. Users, projects, streams and triage stores in the emulator (``POST /__emulator__/seed``).
2. A git repository <workdir>/repos/git/envmon with two commits: the fixture as it is (the analyzed
   revision) and envmon-latest.patch on top (the latest revision; two issues are fixed there).
   Unless --no-svn: an svn repository <workdir>/repos/svnrepo with a working copy
   <workdir>/repos/svn/envmon, revisions 1 and 2 the same way.
3. A build agent's checkout of the first commit (<workdir>/agent/work/envmon), built and analyzed
   there with the mock cov-build / cov-analyze, reported as /build/agent/work/envmon/... (the path of
   a build agent, to try path_strip_prefixes), and committed with cov-commit-defects to the streams
   envmon-main, envmon-release (another triage store) and envmon-svn (version = svn revision 1).
4. A large warning with the shape of a real one (specification 4.3; USE_AFTER_FREE, 20 events nested 6
   deep; made up on the paths of envmon by large_sample.py, so that no third-party data is copied) with
   two variants (no main event; the last top-level event has children) in the stream large-sample.

Prints a JSON summary (and writes it to <workdir>/seed-summary.json): users and keys, repositories
and revisions, snapshots, CIDs per checker, and which of the 35 problems of envmon-truth.yaml came out.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE / "bin"
PLUGIN = HERE.parents[3]
FIXTURES = PLUGIN / "tests" / "fixtures"
REMOTE_ROOT = "/build/agent/work/envmon"

USERS = [
    {"name": "admin", "password": "coverity", "keys": ["emu-admin-key-0001"], "fullName": "Administrator",
     "email": "admin@example.invalid", "superUser": True},
    {"name": "triager", "password": "triage-pass", "keys": ["emu-triager-key-0001"], "fullName": "Triage Engineer",
     "email": "triager@example.invalid"},
    {"name": "viewer", "password": "viewer-pass", "keys": ["emu-viewer-key-0001"], "fullName": "No Web Services",
     "webservices": False},
    {"name": "retired", "password": "retired-pass", "keys": ["emu-retired-key-0001"], "fullName": "Disabled User",
     "disabled": True},
]
SEED = {
    "users": USERS,
    "triageStores": [{"name": "Release Triage Store", "description": "Triage of the release branch"}],
    "projects": [{"name": "envmon", "description": "EM-4 environment monitor firmware"},
                 {"name": "large-sample", "description": "A large warning with the shape of a real one (specification 4.3)"}],
    "streams": [
        {"name": "envmon-main", "project": "envmon", "description": "main branch (git)"},
        {"name": "envmon-release", "project": "envmon", "triageStore": "Release Triage Store",
         "description": "release branch, another triage store"},
        {"name": "envmon-svn", "project": "envmon", "description": "trunk (svn)"},
        {"name": "large-sample", "project": "large-sample", "description": "large warning with the shape of a real one"},
    ],
}
GIT_ENV = {"GIT_AUTHOR_NAME": "EM-4 Developer", "GIT_AUTHOR_EMAIL": "dev@example.invalid",
           "GIT_COMMITTER_NAME": "EM-4 Developer", "GIT_COMMITTER_EMAIL": "dev@example.invalid"}


def run(cmd: list[str], cwd: Path, env: dict | None = None, check: bool = True, log: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if log is not None:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"$ {' '.join(cmd)}  (in {cwd})\n{proc.stdout}{proc.stderr}[exit {proc.returncode}]\n")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def post(url: str, path: str, data: dict) -> dict:
    req = urllib.request.Request(url + path, data=json.dumps(data).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url: str, path: str) -> dict:
    with urllib.request.urlopen(url + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def make_git(fixture: Path, patch: Path, root: Path) -> dict:
    repo = root / "repos" / "git" / "envmon"
    if repo.exists():
        shutil.rmtree(repo)
    shutil.copytree(fixture, repo, ignore=shutil.ignore_patterns("build", "idir"))
    env = dict(os.environ, **GIT_ENV)
    run(["git", "init", "-q", "-b", "main"], repo, env)
    run(["git", "add", "-A"], repo, env)
    first_env = dict(env, GIT_AUTHOR_DATE="2026-10-01T09:00:00+09:00", GIT_COMMITTER_DATE="2026-10-01T09:00:00+09:00")
    run(["git", "commit", "-q", "-m", "EM-4 firmware 1.4.0"], repo, first_env)
    analyzed = run(["git", "rev-parse", "HEAD"], repo, env).strip()
    run(["git", "apply", str(patch)], repo, env)
    run(["git", "add", "-A"], repo, env)
    second_env = dict(env, GIT_AUTHOR_DATE="2026-10-20T18:30:00+09:00", GIT_COMMITTER_DATE="2026-10-20T18:30:00+09:00")
    run(["git", "commit", "-q", "-m", "config: reject too long device names; flash: close image on bad magic"],
        repo, second_env)
    latest = run(["git", "rev-parse", "HEAD"], repo, env).strip()
    return {"path": str(repo), "analyzed": analyzed, "latest": latest}


def make_svn(fixture: Path, patch: Path, root: Path) -> dict:
    repo = root / "repos" / "svnrepo"
    wc = root / "repos" / "svn" / "envmon"
    for p in (repo, wc):
        if p.exists():
            shutil.rmtree(p)
    repo.parent.mkdir(parents=True, exist_ok=True)
    wc.parent.mkdir(parents=True, exist_ok=True)
    run(["svnadmin", "create", str(repo)], root)
    url = repo.resolve().as_uri()
    run(["svn", "checkout", "-q", url, str(wc)], root)
    for item in fixture.iterdir():
        if item.name in ("build", "idir"):
            continue
        dest = wc / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    run(["svn", "add", "-q", "--force", "."], wc)
    run(["svn", "commit", "-q", "-m", "EM-4 firmware 1.4.0", "--username", "dev"], wc)
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(wc.parent))
    run(["git", "apply", str(patch)], wc, env)
    run(["svn", "commit", "-q", "-m", "config: reject too long device names; flash: close image on bad magic",
         "--username", "dev"], wc)
    run(["svn", "update", "-q"], wc)
    return {"repository": url, "working_copy": str(wc), "analyzed": "1", "latest": "2"}


def analyze(git: dict, root: Path, url: str, misra: bool, cert: bool, log: Path) -> dict:
    """The build agent: a clone at the analyzed commit, cov-build, cov-analyze (paths as /build/agent/...)."""
    work = root / "agent" / "work" / "envmon"
    idir = root / "agent" / "idir"
    for p in (work, idir):
        if p.exists():
            shutil.rmtree(p)
    work.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "-q", git["path"], str(work)], root)
    run(["git", "checkout", "-q", git["analyzed"]], work)
    env = dict(os.environ, PATH=str(BIN) + os.pathsep + os.environ.get("PATH", ""),
               COV_EMU_PATH_MAP=f"{work}={REMOTE_ROOT}")
    env.pop("COVERITY_PASSPHRASE", None)
    run(["cov-build", "--dir", str(idir), "make", "-j4"], work, env, log=log)
    args = ["cov-analyze", "--dir", str(idir), "--all", "--ticker-mode", "none"]
    configs = root / "agent" / "config"
    configs.mkdir(parents=True, exist_ok=True)
    if misra:
        (configs / "misra.config").write_text(json.dumps({"version": "2.0", "standard": "misrac2012",
                                                           "title": "MISRA C 2012 (all rules)"}), encoding="utf-8")
        args += ["--coding-standard-config", str(configs / "misra.config")]
    if cert:
        (configs / "cert-c.config").write_text(json.dumps({"version": "2.0", "standard": "cert-c",
                                                            "title": "CERT C (rules)"}), encoding="utf-8")
        args += ["--coding-standard-config", str(configs / "cert-c.config")]
    run(args, work, env, log=log)
    return {"work": str(work), "idir": str(idir), "env": env}


def commit(info: dict, url: str, stream: str, version: str, log: Path, description: str) -> int:
    env = dict(info["env"], COVERITY_PASSPHRASE=USERS[0]["keys"][0])
    out = run(["cov-commit-defects", "--dir", info["idir"], "--url", url, "--stream", stream, "--user", "admin",
               "--version", version, "--target", "x86_64-linux-host", "--description", description,
               "--ticker-mode", "none"], Path(info["work"]), env, log=log)
    m = re.search(r"New snapshot ID (\d+) added\.", out)
    if not m:
        raise RuntimeError(f"no snapshot ID in the output of cov-commit-defects:\n{out}")
    return int(m.group(1))


def large_sample(url: str) -> dict:
    from .large_sample import document
    base = document()["issues"][0]
    no_main = copy.deepcopy(base)
    no_main["extra"] = "lg->dir,lg,no_main"
    no_main["mergeKey"] = "dd7cea15ef54c17c70b8c3657d0242aa"

    def clear(events: list | None) -> None:
        for e in events or []:
            e["main"] = False
            clear(e["events"])
    clear(no_main["events"])
    nested_last = copy.deepcopy(base)
    nested_last["extra"] = "lg->dir,lg,nested_last"
    nested_last["mergeKey"] = "dd7cea15ef54c17c70b8c3657d0242bb"
    last = nested_last["events"][-1]
    last["events"] = [copy.deepcopy(nested_last["events"][2]["events"][0])]
    last["events"][0]["eventTreePosition"] = f"{last['eventTreePosition']}.1"
    last["events"][0]["main"] = False
    issues = [base, no_main, nested_last]
    result = post(url, "/__emulator__/issues", {"stream": "large-sample", "user": "admin",
                                                "snapshot": {"sourceVersion": "4.33", "target": "x86_64",
                                                             "description": "large warning with the shape of a real one",
                                                             "analysisVersion": "2023.12.0"},
                                                "issues": issues})
    return result


def truth_match(url: str, stream: str, truth_path: Path) -> dict:
    """Which problems of envmon-truth.yaml came out as warnings (same file, a line within 3 lines of the
    recorded line in any event, and a checker the record expects)."""
    state = get(url, "/__emulator__/state")
    snaps = sorted((s for s in state["snapshots"].values() if s["stream"] == stream),
                   key=lambda s: (s["dateCreated"], s["id"]))
    latest = snaps[-1]
    items = parse_truth(truth_path.read_text(encoding="utf-8"))
    found, missing = [], []
    for item in items:
        hits = []
        for cid, occs in latest["occurrences"].items():
            rec = state["cids"][cid]
            if rec["checkerName"] not in item["coverity_expected"]:
                continue
            wanted = [item["line"]] + item.get("related", [])
            function = (rec.get("functionDisplayName") or "").split("(")[0]
            for occ in occs:
                lines = [(e["filePathname"], e["lineNumber"]) for e in _flat(occ["events"])] + [(occ["file"], occ["line"])]
                same_function = occ["file"].endswith("/" + item["file"]) and function == item.get("function")
                if same_function or any(p.endswith("/" + item["file"]) and abs(int(n) - w) <= 3
                                        for p, n in lines for w in wanted):
                    hits.append({"cid": int(cid), "checker": rec["checkerName"], "file": occ["file"], "line": occ["line"]})
                    break
        unique = {h["cid"]: h for h in hits}
        entry = {"id": item["id"], "verdict": item["verdict"], "expected": item["coverity_expected"],
                 "found": sorted(unique.values(), key=lambda h: h["cid"])}
        (found if unique else missing).append(entry)
    return {"found": found, "missing": missing, "found_count": len(found), "total": len(items)}


def _flat(events: list | None):
    for e in events or []:
        yield e
        yield from _flat(e.get("events"))


def parse_truth(text: str) -> list[dict]:
    """The few fields of envmon-truth.yaml that the match needs (no YAML library in the standard library)."""
    items, cur = [], None
    in_issues = False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_issues = line.startswith("issues:")
            cur = None
            continue
        if not in_issues:
            continue
        m = re.match(r"  - id: (\S+)", line)
        if m:
            cur = {"id": m.group(1), "coverity_expected": []}
            items.append(cur)
            continue
        if cur is None:
            continue
        for key in ("verdict", "file", "function"):
            m = re.match(rf"    {key}: (.+)$", line)
            if m:
                cur[key] = m.group(1).strip()
        m = re.match(r"    line: (\d+)", line)
        if m:
            cur["line"] = int(m.group(1))
        m = re.match(r"    related_lines: \{(.*)\}", line)
        if m:
            plain = re.sub(r'"[^"]*"|\[[^\]]*\]', "", m.group(1))
            cur["related"] = [int(x) for x in re.findall(r":\s*(\d+)\b", plain)]
        m = re.match(r"    coverity_expected: \[(.*)\]", line)
        if m:
            cur["coverity_expected"] = [x.strip() for x in m.group(1).split(",") if x.strip()]
    return [i for i in items if "file" in i and "line" in i]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m coverity_emulator.seed")
    parser.add_argument("--url", required=True, help="URL of a running emulator")
    parser.add_argument("--workdir", required=True, help="folder for the repositories and the build agent")
    parser.add_argument("--fixture", default=str(FIXTURES / "envmon"))
    parser.add_argument("--patch", default=str(FIXTURES / "envmon-latest.patch"))
    parser.add_argument("--truth", default=str(FIXTURES / "envmon-truth.yaml"))
    parser.add_argument("--no-misra", action="store_true")
    parser.add_argument("--no-cert", action="store_true")
    parser.add_argument("--no-svn", action="store_true")
    parser.add_argument("--no-large-sample", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    log = root / "seed.log"
    log.write_text("", encoding="utf-8")
    url = args.url.rstrip("/")
    post(url, "/__emulator__/seed", SEED)
    git = make_git(Path(args.fixture), Path(args.patch), root)
    svn = None if args.no_svn or not shutil.which("svnadmin") else make_svn(Path(args.fixture), Path(args.patch), root)
    info = analyze(git, root, url, not args.no_misra, not args.no_cert, log)
    snapshots = {"envmon-main": commit(info, url, "envmon-main", git["analyzed"], log, "nightly build of main"),
                 "envmon-release": commit(info, url, "envmon-release", git["analyzed"], log, "release candidate")}
    if svn:
        snapshots["envmon-svn"] = commit(info, url, "envmon-svn", svn["analyzed"], log, "nightly build of trunk")
    sample = None if args.no_large_sample else large_sample(url)
    issues = get(url, "/__emulator__/issues?stream=envmon-main")["issues"]
    per_checker = Counter(i["checker"] for i in issues)
    occurrences = sum(i["occurrences"] for i in issues)
    summary = {
        "url": url, "users": [{k: u[k] for k in ("name", "password", "keys") if k in u} for u in USERS],
        "git": git, "svn": svn, "agent": {"work": info["work"], "idir": info["idir"], "remote_root": REMOTE_ROOT},
        "snapshots": snapshots, "large_sample": sample,
        "envmon_main": {"cids": len(issues), "occurrences": occurrences,
                        "per_checker": dict(sorted(per_checker.items()))},
        "plugin_config": {"coverity": {"url": url, "user_env": "COV_USER", "key_env": "COV_AUTH_KEY",
                                       "triage_store": "Default Triage Store", "revision_field": "sourceVersion",
                                       "path_strip_prefixes": [REMOTE_ROOT + "/"]}},
        "log": str(log),
    }
    if Path(args.truth).is_file():
        summary["truth"] = truth_match(url, "envmon-main", Path(args.truth))
    (root / "seed-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
