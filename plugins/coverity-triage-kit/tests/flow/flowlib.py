"""The parts of the flow test: the sample as a git or svn repository, and a scripted agent.

The agent has what a real agent has. It runs the command lines the script prints under ``next``, plays the
MCP tools a reply asks for from the fake warnings of the sample, edits files in the work copy the script
names and writes result.json. Nothing of the plugin is imported: the command line is the contract.

The two MCP tools are played here, in the file layout the skill reads. The server in mcp-servers/ is not
part of this test.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parents[2]
SCRIPT = PLUGIN / "skills" / "coverity-triage" / "scripts" / "triage.py"
SAMPLE = PLUGIN / "tests" / "fixtures" / "sample"
TARGET = SAMPLE / "sample-target"
STREAM = "Sample-main"
CONFIG = f"coverity:\n  url: https://coverity.example.co.jp:8443\n  stream: {STREAM}\n"
GIT = ["git", "-c", "user.name=flow", "-c", "user.email=flow@example.com", "-c", "core.autocrlf=false"]


def load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# The sample: the warnings Coverity returns for it (CID -> warning), and the conclusions expected for them.
WARNINGS = {w["cid"]: w for w in load(TARGET / ".coverity-triage" / "fake-issues.yaml")["issues"]}
CIDS = list(WARNINGS)
EXPECTED = load(SAMPLE / "expected.yaml")["items"]

# The fix of the scripted agent per CID: (bytes of the reported file, what it writes in their place).
FIXES = {
    20001: (b"BUF_SIZE, fp) == NULL) {\n", b"BUF_SIZE, fp) == NULL) {\n        fclose(fp);\n"),
    20003: (b"    int c;\n", b"    int c;\n    if (buf == NULL) {\n        return -1;\n    }\n"),
    20004: (b"    unsigned char a = value;\n", b"    unsigned char a = (unsigned char)value;\n"),
    20005: (b"    unsigned char b = value + 1;\n", b"    unsigned char b = (unsigned char)(value + 1);\n"),
    20006: (b"    unsigned char c = value + 2;\n", b"    unsigned char c = (unsigned char)(value + 2);\n"),
}
# What it changes when a CID comes back with its earlier fix in the copy: after a review, and as the
# leader of the CIDs with one cause (one fix for all of them).
REWORKS = {20001: [(b"        fclose(fp);\n", b"        (void)fclose(fp);\n")], 20004: [FIXES[20005], FIXES[20006]]}
# recommendation -> verdict, the deviation comment
CONCLUSIONS = {
    "fix": ("true_bug", "本物のバグ。{place} の {function} で、{checker} が指摘する経路を通る。"),
    "deviation": ("false_positive", "誤検知。{place} の {function} は、呼び出し元が引数を検証してから呼び出す。"),
}


def added_lines(cid: int) -> list[bytes]:
    """The lines the fix of a CID adds, with their line ends: each is in the patch after a "+"."""
    old, new = FIXES[cid]
    return new.replace(old, b"").splitlines(keepends=True)


def command(name: str, *args) -> str:
    """A command line of the skill, as SKILL.md gives it to the agent."""
    return " ".join(["uv run --native-tls --quiet", f'"{SCRIPT.as_posix()}"', name, *map(str, args)])


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Agent:
    """One session of the scripted agent in the person's repository."""

    def __init__(self, repo: Path):
        self.repo, self.warnings = repo, WARNINGS
        self.replies, self.calls, self.worked = [], [], []  # every reply, the tools called, the CIDs worked on

    def run(self, line: str) -> dict:
        """Run one command line. The script must exit 0 and print one JSON object (UTF-8) with ok and next."""
        proc = subprocess.run(shlex.split(line), cwd=str(self.repo), stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=120)
        try:
            reply = json.loads(proc.stdout.decode("utf-8"))
            good = proc.returncode == 0 and isinstance(reply["ok"], bool) and all(
                isinstance(n, str) for n in reply["next"])
        except (ValueError, KeyError, TypeError):
            good = False
        if not good:
            raise AssertionError(f"{line}\nexit {proc.returncode}\n{proc.stdout.decode('utf-8', 'replace')}\n"
                                 f"{proc.stderr.decode('utf-8', 'replace')}")
        self.replies.append(reply)
        return reply

    def follow(self, line: str, stop=lambda reply: False) -> dict:
        """Run a command line, do what its reply asks for, go on with the line under next, and so on.
        Returns the reply ``stop`` accepts (nothing is done for it yet) or the one with no next line."""
        for _ in range(60):
            reply = self.run(line)
            if not reply["ok"]:
                raise AssertionError(f"{line}\n{json.dumps(reply, ensure_ascii=False, indent=1)}")
            if stop(reply) or not reply["next"]:
                return reply
            line = self.act(reply)
        raise AssertionError(f"the flow does not end: {line}")

    def act(self, reply: dict) -> str:
        """Do what a reply asks for before its next line: call the tools, work on the CID. Returns that line."""
        for call in reply.get("fetch") or []:
            self.tool(call)
        if reply.get("result_file"):
            self.work(reply)
        return reply["next"][0]

    def tool(self, call: dict) -> None:
        """Play a tool of the MCP server: write what it would write, from the fake warnings."""
        name, args = call["tool"], call["arguments"]
        if args["repo_root"] != self.repo.as_posix():
            raise AssertionError(f"repo_root is not the person's repository: {call}")
        if name == "coverity_search_issues":
            issues = [{key: w[key] for key in ("cid", "checker", "file", "line", "function", "impact")}
                      for w in self.warnings.values()]
            write_json(Path(args["output_file"]), {
                "stream": STREAM, "conditions": {}, "fetched_at": "2026-11-10T09:00:00+09:00",
                "total_matched": len(issues), "limited": False, "issues": issues})
        elif name == "coverity_get_issue_details":
            for cid in args["cids"]:
                w = self.warnings[cid]
                write_json(Path(args["output_dir"]) / str(cid) / "issue.json",
                           {"events": w["events"], "checker_description": w["checker_description"]})
        else:
            raise AssertionError(f"the plugin has no such tool: {call}")
        self.calls.append(name)

    def result(self, given: dict) -> dict:
        """The result.json for a CID: the conclusion expected for the sample; a fix for every other CID."""
        cid, issue = given["cid"], given["issue"]
        expected = EXPECTED.get(str(cid)) or {"recommendation": "fix",
                                              "reason": self.warnings[cid]["checker_description"]}
        verdict, comment = CONCLUSIONS[expected["recommendation"]]
        place = f"{issue['file']}:{issue['line']}"
        other = {"fix": {"summary": "指摘された経路を通らないようにします"}} if verdict == "true_bug" else {
            "no_fix_reason": "コードに問題がないため"}
        return {"cid": cid, "verdict": verdict, "summary": expected["reason"],
                "evidence": [{"file": issue["file"], "line": issue["line"], "note": expected["reason"]}],
                "recommendation": expected["recommendation"], "confidence": "high", **other,
                "deviation": {"comment": comment.format(place=place, **issue)}}

    def edit(self, given: dict) -> None:
        """Make the fix in the work copy. The file must be what the reply says: the committed one, or for a
        CID that was sent back the one with its earlier fix. In a copy that was not reset for this CID the
        fix of another try would still be there."""
        rework = given.get("rework")
        changes = REWORKS[given["cid"]] if rework and rework["patch_applied"] else [FIXES[given["cid"]]]
        path = Path(given["work_dir"]) / given["issue"]["file"]
        data = path.read_bytes()
        for old, new in changes:
            if data.count(old) != 1 or new in data:
                raise AssertionError(f"CID {given['cid']}: {path} is not the expected file")
            data = data.replace(old, new)
        path.write_bytes(data)

    def work(self, given: dict) -> None:
        """What a worker does with the CID that next handed out: read the warning, fix, write the result."""
        if not Path(given["issue_file"]).is_file():
            raise AssertionError(f"the warning path was not fetched: {given['issue_file']}")
        result = self.result(given)
        if "fix" in result:
            self.edit(given)
        write_json(Path(given["result_file"]), result)
        self.worked.append(given["cid"])


# ---- repositories -----------------------------------------------------------------------------


def call(*args, cwd: Path) -> bytes:
    done = subprocess.run([str(a) for a in args], cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True)
    if done.returncode != 0:
        raise AssertionError(f"{' '.join(map(str, args))}\n{(done.stdout + done.stderr).decode('utf-8', 'replace')}")
    return done.stdout


def make_repo(kind: str, top: Path) -> Path:
    """The sample as the person's repository with one commit: <top>/repo (svn: a working copy of the
    repository <top>/svnrepo)."""
    repo = top / "repo"
    if kind == "svn":
        call("svnadmin", "create", top / "svnrepo", cwd=top)
        call("svn", "checkout", "-q", (top / "svnrepo").as_uri(), repo, cwd=top)
    # LF in the repository, also where git checked the sample out with CRLF (Windows)
    files = {path.relative_to(TARGET).as_posix(): path.read_bytes().replace(b"\r\n", b"\n")
             for path in TARGET.rglob("*") if path.is_file()}
    files[".coverity-triage/config.yaml"] = CONFIG.encode("utf-8")
    for name, data in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(data)
    if kind == "git":
        # autocrlf off: the work copies get the bytes that were committed
        for args in (("init", "-q", "-b", "main"), ("config", "core.autocrlf", "false"), ("add", "-A"),
                     ("commit", "-q", "-m", "sample")):
            call(*GIT, *args, cwd=repo)
    else:
        for args in (("add", "-q", "--force", "."), ("commit", "-q", "-m", "sample"), ("update", "-q")):
            call("svn", *args, cwd=repo)
    return repo


def user_tree(kind: str, repo: Path) -> bytes:
    """What the person sees of the repository: the revision, the branches, every changed or new file."""
    if kind == "git":
        return b"".join(call(*GIT, *args, cwd=repo)
                        for args in (("rev-parse", "HEAD"), ("branch", "--list"), ("status", "--porcelain")))
    return call("svn", "info", "--show-item", "revision", cwd=repo) + call("svn", "status", cwd=repo)


def fresh_copy(kind: str, top: Path, patches: list[Path]) -> Path:
    """A new checkout of the committed code with the patches applied, as the person takes fixes in:
    git apply / svn patch."""
    copy = top / "fresh"
    if kind == "git":
        call(*GIT, "clone", "-q", top / "repo", copy, cwd=top)
    else:
        call("svn", "checkout", "-q", (top / "svnrepo").as_uri(), copy, cwd=top)
    for patch in patches:
        call(*(GIT + ["apply"] if kind == "git" else ["svn", "patch"]), patch, cwd=copy)
    return copy


def wait_for_copies(run_dir: str) -> None:
    """Wait until the background job that makes the work copies has ended. A test does it where start has
    answered without waiting for the job, before the folder the job works in is deleted."""
    record = Path(run_dir) / "work" / "prepare.json"
    for _ in range(600):
        try:
            if json.loads(record.read_text(encoding="utf-8"))["status"] != "running":
                return
        except (OSError, ValueError):  # Windows: the file is being replaced at this moment
            pass
        time.sleep(0.1)
    raise AssertionError(f"the job does not end: {record}")


def remove_tree(path: Path) -> None:
    """Delete a temporary folder. git objects and svn pristine files are read-only; Windows refuses those."""
    def writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, **({"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}))
