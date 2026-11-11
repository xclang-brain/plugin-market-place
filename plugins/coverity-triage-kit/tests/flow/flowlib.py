"""The parts of the flow test: the person's side, and scripted agents.

The person's side is real: a repository of the sample (git with a bare origin, svn with a working copy), a
Coverity Connect that has its warnings (tests/coverity_emulator), and the MCP server of the plugin, started
as mcp.json registers it. A scripted agent has a shell in the person's repository and the MCP tools. It runs
the lines the replies give under ``next`` (and, only after the person's OK, the line under ``after_ok``),
makes the calls under ``fetch`` and ``call``, edits the work copy and writes result.json. It builds only the
lines the agent and skill texts tell an agent to build.
"""

import atexit
import functools
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parents[2]
SAMPLE = PLUGIN / "tests" / "fixtures" / "sample" / "sample-target"
STREAM = "Sample-main"
sys.dont_write_bytecode = True  # the emulator is imported from the repository
sys.path.insert(0, str(PLUGIN / "tests"))
from coverity_emulator import KEY, USER, Emulator  # noqa: E402

# The warnings Coverity has for the sample (CID -> warning), and its source file as it is committed.
WARNINGS = {w["cid"]: w for w in yaml.safe_load(
    (SAMPLE / ".coverity-triage" / "fake-issues.yaml").read_text(encoding="utf-8"))["issues"]}
CIDS = list(WARNINGS)
CLEAN = (SAMPLE / "src" / "reader.c").read_bytes().replace(b"\r\n", b"\n")  # LF, also on a CRLF checkout
# The fix of the scripted worker per CID: (bytes of the reported file, what it writes in their place).
FIXES = {
    20001: (b"BUF_SIZE, fp) == NULL) {\n", b"BUF_SIZE, fp) == NULL) {\n        fclose(fp);\n"),
    20003: (b"    int c;\n", b"    int c;\n    if (buf == NULL) {\n        return -1;\n    }\n"),
    20004: (b"    unsigned char a = value;\n", b"    unsigned char a = (unsigned char)value;\n"),
    20005: (b"    unsigned char b = value + 1;\n", b"    unsigned char b = (unsigned char)(value + 1);\n"),
    20006: (b"    unsigned char c = value + 2;\n", b"    unsigned char c = (unsigned char)(value + 2);\n"),
}
# What it changes when a CID comes back with its earlier fix: after a review; as the leader of the CIDs with one cause.
REWORKS = {20001: [(b"        fclose(fp);\n", b"        (void)fclose(fp);\n")], 20004: [FIXES[20005], FIXES[20006]]}
GROUP = [20004, 20005, 20006]  # the CIDs of the sample that have one cause
STARTS = {"true_bug": "本物のバグ。", "false_positive": "誤検知。", "intentional": "意図的。", "undetermined": "本物のバグ。"}
# What the person's terminal has: the credentials, and a locale in which svn takes a Japanese folder name.
PERSON = {**os.environ, "COV_USER": USER, "COV_AUTH_KEY": KEY, "PYTHONDONTWRITEBYTECODE": "1"}
if os.name == "posix" and "UTF-8" not in "".join(os.environ.get(k, "") for k in ("LC_ALL", "LC_CTYPE", "LANG")):
    PERSON["LC_ALL"] = "C.UTF-8"
BROWSER = "browser.cmd" if os.name == "nt" else "browser"  # in the test folder: a browser that opens nothing
EMULATORS: dict[Path, Emulator] = {}  # test folder -> the Coverity that make_repo started for it


def line(plugin: Path, skill: str, *words) -> str:
    """A command line as the texts give it: the script of a skill, then the words (a Path is quoted)."""
    script = plugin / "skills" / f"coverity-{skill}" / "scripts" / f"{skill}.py"
    return " ".join(["uv run --native-tls --quiet", f'"{script.as_posix()}"',
                     *(f'"{word.as_posix()}"' if isinstance(word, Path) else str(word) for word in words)])


command = functools.partial(line, PLUGIN, "triage")  # a line of triage.py


def rows(table: str) -> dict[int, list[str]]:
    """The rows of the one table by CID: [CID, 判定, 確信度, 修正コード, 逸脱コメント, 選択, 備考]. What a
    chat run has below the table, after an empty line, is no row."""
    return {int(row[2:7]): row[2:-2].split(" | ") for row in table.split("\n\n")[0].splitlines()[2:]}


@functools.cache
class Tools:
    """The MCP server of a plugin folder (one per test process): the process mcp.json registers, driven as
    a client drives it (JSON-RPC, one message per line on its pipes)."""

    def __init__(self, plugin: Path):
        registered = json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["coverity-connect"]
        # The client fills in what it has: the root of the plugin, the person's environment (no proxy stands
        # before the emulator). uv puts the environment of the server into ``home``, not into the plugin.
        person = {k: v for k, v in PERSON.items() if not k.upper().endswith("_PROXY")}
        have = {**person, "PLUGIN_ROOT": plugin.as_posix()}
        fill = functools.partial(re.sub, r"\$\{(\w+)\}", lambda found: have.get(found[1], found[0]))
        self.home, self.errors = tempfile.mkdtemp(prefix="flow-mcp-"), tempfile.TemporaryFile()
        env = {**person, **{name: fill(value) for name, value in registered["env"].items()}, "UV_PROJECT_ENVIRONMENT": self.home}
        self.proc = subprocess.Popen([registered["command"], *map(fill, registered["args"])], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)
        self.lock, self.last = threading.Lock(), 0
        atexit.register(self.close)
        self.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "flow", "version": "0"}})
        self.send(method="notifications/initialized")

    def send(self, **message) -> None:
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def ask(self, method: str, params: dict) -> dict:
        """The result of one request. A server that ends, or does not answer in time, fails the test."""
        with self.lock:
            self.last += 1
            guard = threading.Timer(120, self.proc.kill)
            guard.start()
            try:
                self.send(id=self.last, method=method, params=params)
                return next(json.loads(raw)["result"] for raw in self.proc.stdout if json.loads(raw).get("id") == self.last)
            except StopIteration:
                self.errors.seek(0)
                said = self.errors.read().decode("utf-8", "replace")[-2000:]
                raise AssertionError(f"the MCP server gave no answer: {method} {params}\n{said}") from None
            finally:
                guard.cancel()

    def close(self) -> None:
        self.proc.stdin.close()  # the server ends with its input
        self.proc.wait(timeout=30)
        self.proc.stdout.close()
        self.errors.close()
        shutil.rmtree(self.home, ignore_errors=True)


class Agent:
    """One session of a scripted agent in the person's repository, with the plugin installed in ``plugin``.
    ``opinions``: CID -> (verdict, recommendation, confidence) where a worker does not conclude the usual."""

    def __init__(self, repo: Path, plugin: Path = PLUGIN, opinions: dict | None = None):
        self.repo, self.plugin, self.opinions = repo, plugin, opinions or {}
        self.line = functools.partial(line, plugin)  # a line this agent builds itself, from the folder of a skill
        self.env = {**PERSON, "BROWSER": str(repo.parent / BROWSER)}
        self.replies, self.calls, self.worked = [], [], []  # every reply, the tools called, the CIDs worked on
        self.slips = set()  # CIDs whose first result has a comment that save refuses

    def run(self, line: str) -> dict:
        """Run one command line. The script must exit 0 and print one JSON object (UTF-8) with ok and, under
        next, lines of the plugin that this agent has."""
        proc = subprocess.run(shlex.split(line), cwd=str(self.repo), env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=180)
        try:
            reply = json.loads(proc.stdout.decode("utf-8"))
            good = proc.returncode == 0 and isinstance(reply["ok"], bool) and all(
                isinstance(n, str) and self.plugin.as_posix() in n for n in reply.get("next", []))
        except (ValueError, KeyError, TypeError):
            good = False
        if not good:
            raise AssertionError(f"{line}\nexit {proc.returncode}\n{proc.stdout.decode('utf-8', 'replace')}\n"
                                 f"{proc.stderr.decode('utf-8', 'replace')}")
        self.replies.append(reply)
        return reply

    def follow(self, line: str, stop="") -> dict:
        """Run a command line, do what its reply asks for, go on with the line under next, and so on. Returns
        the reply with no next line, or the first one that has the key ``stop`` (or that the function ``stop``
        accepts); nothing is done for that one yet. An agent ends at ok false without problems: the test fails."""
        for _ in range(80):
            reply = self.run(line)
            if not reply["ok"] and "problems" not in reply:
                raise AssertionError(f"{line}\n{json.dumps(reply, ensure_ascii=False, indent=1)}")
            if not reply["next"] or (stop(reply) if callable(stop) else stop in reply):
                return reply
            line = self.act(reply)
        raise AssertionError(f"the flow does not end: {line}")

    def ready(self, line: str) -> dict:
        """Run a line and go on while the line under next is a start line: by the texts, the list and the
        work copies are ready after that. Returns the first reply with another line (nothing is done for it yet)."""
        return self.follow(line, stop=lambda reply: " start --repo " not in reply["next"][0])

    def act(self, reply: dict) -> str:
        """Do what a reply asks for before its next line: call the tools, work on the CID. Returns that line."""
        for call in reply.get("fetch") or []:
            self.tool(call)
        if reply.get("result_file"):
            self.work(reply)
        return reply["next"][0]

    def tool(self, call: dict) -> dict:
        """Make a call of fetch or call through the MCP server; while it answers done false, once more.
        No scenario expects a tool error: it fails the test with its sentence."""
        for _ in range(20):
            self.calls.append(call["tool"])
            result = Tools(self.plugin).ask("tools/call", {"name": call["tool"], "arguments": call["arguments"]})
            if result.get("isError"):
                raise AssertionError(f"{call}: {result['content'][0]['text']}")
            if result["structuredContent"].get("done") is not False:
                return result["structuredContent"]
        raise AssertionError(f"the tool does not end: {call}")

    def result(self, given: dict) -> dict:
        """The result.json for a CID: a real bug with a fix where the worker has one, else a false positive."""
        cid, issue = given["cid"], given["issue"]
        usual = ("true_bug", "fix", "high") if cid in FIXES else ("false_positive", "deviation", "high")
        verdict, advice, level = self.opinions.get(cid, usual)
        place = f"{issue['file']}:{issue['line']}"
        fix = {"fix": {"summary": f"{issue['function']} が、指摘された経路を通らないようにします"}} if advice == "fix" else {
            "no_fix_reason": "コードに問題がないため"}
        return {"cid": cid, "verdict": verdict, "summary": f"{place} までの経路をコードで確認しました",
                "evidence": [{"file": issue["file"], "line": issue["line"], "note": issue["checker"]}],
                "recommendation": advice, "confidence": level, **fix,
                "deviation": {"comment": f"{STARTS[verdict]}{place} の {issue['function']} で、経路の成立をコードで確認した。"}}

    def edit(self, given: dict) -> None:
        """Make the fix in the work copy. The file must be what the reply says: the committed one, or for a CID
        that came back the one with its earlier fix (not what another try has left in a copy that was not reset)."""
        back = (given.get("rework") or {}).get("patch_applied")
        path = Path(given["work_dir"]) / given["issue"]["file"]
        data = path.read_bytes()
        if data != (CLEAN.replace(*FIXES[given["cid"]]) if back else CLEAN):
            raise AssertionError(f"CID {given['cid']}: {path} is not the expected file")
        for old, new in REWORKS.get(given["cid"], []) if back else [FIXES[given["cid"]]]:
            data = data.replace(old, new)
        path.write_bytes(data)

    def work(self, given: dict) -> None:
        """What a worker does with the CID that next handed out: read the warning, fix, write the result.
        After a refused save (problems) it corrects the result; the fix stays."""
        refused = "problems" in given
        if not refused:
            if not Path(given["issue_file"]).is_file():
                raise AssertionError(f"the warning path was not fetched: {given['issue_file']}")
            self.given = given
            self.worked.append(given["cid"])
            if "fix" in self.result(given):
                self.edit(given)
        result = self.result(self.given)
        if given["cid"] in self.slips and not refused:
            result["deviation"]["comment"] += "ただし、呼び出し元の確認が必要。"
        Path(given["result_file"]).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    def table(self, *changes: str) -> dict:
        """Skill coverity-apply (「修正を取り込んで」, or an answer that changes choices): its plan line with every
        change the person named; its reply has the one table."""
        return self.run(self.line("apply", "plan", "--repo", self.repo, *(f"--set {change}" for change in changes)))

    def ok(self, plan: dict, write: bool = True) -> dict:
        """The person's OK: the line under after_ok of the plan, then the calls of its reply (write False: no call
        is made)."""
        applied = self.run(plan["after_ok"][0])
        for call in applied["call"] if write else []:
            self.tool(call)
        return applied


def team(lead: Agent, run: Path, reviews: dict, log: list):
    """The orchestrator after its start (steps 2 to 6 of its text) with the agents it asks: one worker per
    next line, all at the same time; the integrator, once; the reviewers. ``reviews``: CID -> what its first
    reviewer records (not named: pass; None: nobody records it). Yields every reply of status before its
    step is done, then the reply of the plan line that finish gives. ``log`` gets the workers of each round."""
    status_line, asked, merged = lead.line("triage", "status", "--run", run), {}, False
    for _ in range(20):
        status = lead.run(status_line)
        yield status
        step, other = status["next"][0], Agent(lead.repo, lead.plugin)
        if " next --run " in step:
            workers = [Agent(lead.repo, lead.plugin, lead.opinions) for _ in status["next"]]
            with ThreadPoolExecutor(len(workers)) as pool:  # each: next and save, until next answers done
                list(pool.map(Agent.follow, workers, status["next"]))
            log.append(workers)
        elif status["merge_candidates"] and not merged:  # the integrator: one cause where one fix removes all
            merged, asked_for = True, other.run(status_line)
            for group in asked_for["merge_candidates"]:
                if group["cids"] == GROUP:  # no fix of a member covers all: the lowest CID leads and is made again
                    other.run(asked_for["merge_command"].replace("<CID>", str(GROUP[0])).replace(
                        "<CID,CID>", ",".join(map(str, GROUP[1:]))).replace(
                        "[--rework]", '--rework --point "src/reader.c:58 同じ変換が3行にある"'))
        elif " review --run " in step:
            for batch in status["review_batches"]:  # a reviewer: the code before the fix, one record per CID
                if (Path(batch["work_dir"]) / "src" / "reader.c").read_bytes() != CLEAN:
                    raise AssertionError(f"the copy of a reviewer is not clean: {batch['work_dir']}")
                for cid in batch["cids"]:
                    asked[cid] = asked.get(cid, 0) + 1
                    if asked[cid] < 3 and reviews.get(cid, "") is None:
                        continue
                    outcome = reviews.pop(cid, "pass") if asked[cid] < 3 else "unverified"  # the orchestrator's own record
                    other.run(step.split(" [")[0].replace("<CID>", str(cid)).replace("<pass|flag|rework>", outcome))
        else:
            yield lead.follow(step)  # finish, again while its next is the finish line; then its next: the plan line
            return
    raise AssertionError("the orchestrator does not come to finish")


def call(*args, cwd: Path) -> bytes:
    done = subprocess.run([str(a) for a in args], cwd=str(cwd), env=PERSON, stdin=subprocess.DEVNULL, capture_output=True)
    if done.returncode != 0:
        raise AssertionError(f"{' '.join(map(str, args))}\n{(done.stdout + done.stderr).decode('utf-8', 'replace')}")
    return done.stdout


def make_repo(kind: str, top: Path, settings: str | None = "", extra: int = 0) -> Path:
    """The person's repository <top>/repo with the code of the sample in one commit (git: pushed to the
    bare origin <top>/origin.git; svn: a working copy of <top>/svnrepo), and a Coverity that has the
    warnings of the sample and ``extra`` more like CID 20002. ``settings``: what the settings file holds
    after the URL and the stream (None: there is no settings file yet)."""
    repo = top / "repo"
    more = [{**WARNINGS[20002], "cid": CIDS[-1] + n} for n in range(1, extra + 1)]
    (top / "warnings.yaml").write_text(json.dumps({"issues": [*WARNINGS.values(), *more]}), encoding="ascii")  # JSON is YAML
    EMULATORS[top] = emulator = Emulator(top / "warnings.yaml").__enter__()
    (top / BROWSER).write_bytes(b"@exit /b 0\r\n" if os.name == "nt" else b"#!/bin/sh\n")
    (top / BROWSER).chmod(0o755)
    if kind == "svn":
        call("svnadmin", "create", top / "svnrepo", cwd=top)
        call("svn", "checkout", "-q", (top / "svnrepo").as_uri(), repo, cwd=top)
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "reader.c").write_bytes(CLEAN)
    if kind == "git":  # autocrlf off: the work copies get the bytes that were committed
        for args in (("init", "-q", "-b", "main"), ("config", "user.name", "flow"), ("config", "user.email", "flow@example.com"),
                     ("config", "core.autocrlf", "false"), ("add", "-A"), ("commit", "-q", "-m", "sample"),
                     ("clone", "-q", "--bare", ".", top / "origin.git"), ("remote", "add", "origin", top / "origin.git")):
            call("git", *args, cwd=repo)
    else:
        for args in (("add", "-q", "src"), ("commit", "-q", "-m", "sample"), ("update", "-q")):
            call("svn", *args, cwd=repo)
    if settings is not None:
        (repo / ".coverity-triage").mkdir()
        (repo / ".coverity-triage" / "config.yaml").write_text(
            f"coverity:\n  url: {emulator.url}\n  stream: {STREAM}\n{settings}", encoding="utf-8")
    return repo


def user_tree(kind: str, repo: Path) -> bytes:
    """What the person has: the revision, the current branch, every changed or new file (git: and the index)."""
    if kind == "git":
        return b"".join(call("git", *args, cwd=repo)
                        for args in (("rev-parse", "HEAD"), ("symbolic-ref", "HEAD"), ("status", "--porcelain")))
    status = call("svn", "status", cwd=repo).replace(b"\r\n", b"\n").replace(b"\\", b"/")  # as on Linux
    return call("svn", "info", "--show-item", "revision", cwd=repo) + status


@functools.cache
def installed(*absent: str) -> Path:
    """A copy of the plugin as a person has it installed, without the files and folders of these names
    (the stages the person does not use). One per test process; its path has a space and Japanese."""
    top = Path(tempfile.mkdtemp(prefix="flow インストール ")).resolve()
    atexit.register(remove_tree, top)
    skip = {".git", ".venv", ".ruff_cache", "__pycache__", "tests", "docs", *absent}
    return Path(shutil.copytree(PLUGIN, top / "coverity-triage-kit", ignore=lambda _folder, names: skip & set(names)))


def remove_tree(path: Path) -> None:
    """Delete a test folder; the Coverity of the folder ends. git objects and svn pristine files are
    read-only; Windows refuses to delete those."""
    if path in EMULATORS:
        EMULATORS.pop(path).__exit__()

    def writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, **({"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}))
