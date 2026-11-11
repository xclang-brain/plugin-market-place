#!/usr/bin/env python3
"""EVAL: run stage 1 (skills in a chat), 2 (triage-worker) or 3 (triage-orchestrator) with a real model, then grade.

    python3 tests/eval/run_eval.py --stage 1|2|3 --vcs git|svn [--work <folder>] [--answer-ok] [--copies N] [--model M]

The development form of the plugin (agents/*.md, .claude-plugin/plugin.json, .mcp.json) runs headless with the
claude CLI against tests/coverity_emulator, in a scratch folder outside the repository. The tool that writes to
Coverity is never allowed. Prints one line per check (合格 / 不合格), then one JSON line. Exit code 0: every check
passed; 1: a check failed; 2: the EVAL could not run or was stopped. A developer tool: tests/run.py does not run it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parents[2]
SAMPLE = PLUGIN / "tests" / "fixtures" / "sample"
NAME = "coverity-triage-kit"
PROMPT = "条件に合う警告をすべて処置して"
CID = 20002  # the CID the person names in stage 1
# The agent (stage 1: none, a chat that picks the skills), the minutes one claude run may take
STAGES = {1: (None, 15), 2: ("triage-worker", 30), 3: ("triage-orchestrator", 60)}
SUBAGENTS = ("triage-worker", "triage-reviewer", "triage-integrator")
READ_TOOLS = ("coverity_search_issues", "coverity_get_issue_details")  # coverity_update_triage is never allowed
# expected.yaml gives the recommendation of a CID: the verdicts that fit it
FITS = {"fix": ("true_bug",), "deviation": ("false_positive", "intentional")}
# The last message after the OK says that the registration in Coverity is still to do: 登録 with one of LEFT,
# and no sentence that says it was made
LEFT = ("「修正を取り込んで」", "残", "まだ", "未", "許可", "拒否", "できません", "できなかった", "されていません")
MADE = re.compile(r"登録(?:しました|[がをはも]?完了しました|済みです)")
ASKS = re.compile(r"(?:[?？]|(?:です|ます|でしょう|ません)か。?)[\s*」）)]*\Z")  # a text that ends in a question
SVN_STATUS = re.compile(r"^([ADUGC][ADUGC ]| [ADUGC]) +\S", re.M)  # a status line of svn patch
SQUEEZE = re.compile(r"[\s*`]+")  # white space and Markdown emphasis are not compared


class Stop(Exception):
    """The EVAL cannot run here. The message is for the developer."""


def sh(*args, cwd: Path, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run([str(a) for a in args], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, capture_output=True)
    if check and done.returncode != 0:
        output = (done.stdout + done.stderr).decode("utf-8", "replace")
        raise Stop(f"コマンドが失敗しました: {' '.join(map(str, args))}\n{output}")
    return done


def parse(source: str | Path) -> dict:
    """A JSON object from a text or from a file; {} for anything else."""
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig") if isinstance(source, Path) else source)
    except (OSError, ValueError):
        data = None
    return data if isinstance(data, dict) else {}


class Transcript:
    """The stream-json output of one claude run."""

    def __init__(self, path: Path):
        self.events = list(filter(None, map(parse, path.read_text(encoding="utf-8", errors="replace").splitlines())))
        ends = [e for e in self.events if e.get("type") == "result"]  # several when sub-agents ran in the background
        last = ends[-1] if ends else {}
        self.final = str(last.get("result") or "")  # the last message to the person
        self.session = next((e["session_id"] for e in self.events if e.get("session_id")), None)
        # The text and tool_use blocks of the main conversation, in order: (id of the response, block).
        # The CLI writes one event per block; the blocks of one response have the same id.
        self.blocks = [(e["message"].get("id"), block) for e in self.events
                       if e.get("type") == "assistant" and not e.get("parent_tool_use_id")
                       for block in e["message"].get("content") or []]
        self.uses = [(n, mid, block) for n, (mid, block) in enumerate(self.blocks) if block.get("type") == "tool_use"]
        self.replies = {}  # tool_use id -> the text of its result
        for event in self.events:
            content = event["message"].get("content") if event.get("type") == "user" else None
            for block in content if isinstance(content, list) else []:
                body = block.get("content")
                self.replies[block.get("tool_use_id")] = body if isinstance(body, str) else "".join(
                    part.get("text", "") for part in body or [] if isinstance(part, dict))
        models = list((last.get("modelUsage") or {}).values())  # the CLI's totals of the session, sub-agents included
        self.usage = {"cost_usd": round(last.get("total_cost_usd") or 0, 4),
                      "input_tokens": sum(m.get(key, 0) for m in models for key in (
                          "inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")),
                      "output_tokens": sum(m.get("outputTokens", 0) for m in models)}

    def commands(self, pattern: str) -> list[tuple[int, dict]]:
        """The shell commands of the main conversation that match: (place in blocks, the tool_use block)."""
        return [(n, block) for n, _, block in self.uses
                if re.search(pattern, str((block.get("input") or {}).get("command") or ""))]

    def plan(self) -> tuple[int | None, dict]:
        """Where the main conversation first ran apply.py plan, and the last reply of that command."""
        ran = self.commands(r'apply\.py["\']? +plan\b')
        bodies = [self.replies.get(block.get("id"), "") for _, block in ran]
        replies = [parse(body[body.find("{"):body.rfind("}") + 1]) for body in bodies]
        return (ran[0][0] if ran else None), next(filter(None, reversed(replies)), {})

    def launches(self, agent: str) -> list[tuple[str, bool]]:
        """Per request of the main conversation to this sub-agent: (id of the response, the sub-agent really
        started: the transcript has its task_started event or messages of its own; a refused request has neither)."""
        ran = {e.get("tool_use_id") for e in self.events if e.get("subtype") == "task_started"}
        ran = (ran | {e.get("parent_tool_use_id") for e in self.events}) - {None}
        return [(mid, block.get("id") in ran) for _, mid, block in self.uses
                if block.get("name") in ("Agent", "Task")
                and str((block.get("input") or {}).get("subagent_type") or "").split(":")[-1] == agent]


class Eval:
    """One EVAL: its folders, the files it grades and its checks."""

    def __init__(self, stage: int, vcs: str, work: str | None, copies: int = 1, model: str = "haiku"):
        for tool in ("claude", "uv", *(("git",) if vcs == "git" else ("svn", "svnadmin"))):
            if not shutil.which(tool):
                raise Stop(f"{tool} コマンドが見つかりません。インストールして、PATH を確認してください")
        # The scratch top folder: outside the repository, with a space and a Japanese character in its path.
        self.site = (Path(work).resolve() if work else Path(tempfile.mkdtemp(prefix="coverity-eval-"))) / "評価 作業"
        if self.site.is_relative_to(next((p for p in PLUGIN.parents if (p / ".git").exists()), PLUGIN)):
            raise Stop("--work には、リポジトリの外のフォルダーを指定してください")
        if self.site.exists():
            raise Stop(f"作業フォルダーが既にあります: {self.site}。--work に別のフォルダーを指定してください")
        self.site.mkdir(parents=True)
        self.stage, self.vcs, self.model, self.checks, self.usage = stage, vcs, model, [], {}
        self.repo, self.out = self.site / "repo", self.site / "coverity-triage-out"  # output_dir is the default
        self.snap = self.site / "plugin" / NAME
        self.run, self.items, self.states, self.done = self.out / "none", [], {}, []  # the run folder; see load()
        # The sample, --copies times. Copy k (2, 3, ...) is src/reader<k>.c with the functions <name>_<k>; its CIDs
        # are 1000 * (k - 1) higher. expected.yaml holds for every copy: each has its own group of one cause.
        code = (SAMPLE / "sample-target" / "src" / "reader.c").read_bytes().decode("utf-8")
        data = (SAMPLE / "sample-target" / ".coverity-triage" / "fake-issues.yaml").read_text(encoding="utf-8")
        names = "|".join(re.findall(r"^[a-z][^(\n]*?(\w+)\(", code, re.M))  # the functions the file defines
        code, data = ([text, *(re.sub(rf"\b({names})\b", rf"\1_{k}", text).replace("reader.c", f"reader{k}.c")
                               for k in range(2, copies + 1))] for text in (code, data))
        self.sources = {f"reader{k}.c": text for k, text in enumerate(code[1:], 2)}
        # The warnings the emulator returns: all of them fit the conditions of the settings file.
        self.warnings = [{**warning, "cid": warning["cid"] + 1000 * k} for k, text in enumerate(data)
                         for warning in yaml.safe_load(text)["issues"]]
        (self.site / "warnings.json").write_text(json.dumps({"issues": self.warnings}), encoding="ascii")  # is YAML
        expected = yaml.safe_load((SAMPLE / "expected.yaml").read_text(encoding="utf-8"))
        items, group = expected.get("items") or {}, (expected.get("group") or {}).get("cids") or []
        self.expected = {int(cid) + 1000 * k: want for k in range(copies) for cid, want in items.items()}
        self.groups = [{cid + 1000 * k for cid in group} for k in range(copies)]

    def check(self, name: str, ok, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'合格' if ok else '不合格'}: {name}" + (f"（{detail}）" if detail else ""), flush=True)


def prepare(ev: Eval, emulator: dict) -> None:
    """The plugin snapshot, the sample as a repository, the settings file made by the real setup.py init."""
    shutil.copytree(PLUGIN, ev.snap, ignore=shutil.ignore_patterns(
        "tests", "docs", ".git", ".venv", "__pycache__", ".ruff_cache"))
    # Build the environment of the MCP server now: its first start in the session must not time out.
    sh("uv", "run", "--native-tls", "--frozen", "--quiet", "--directory", ev.snap / "mcp-servers" / "coverity-connect",
       "python", "-c", "import coverity_mcp", cwd=ev.site)
    if ev.vcs == "git":
        sh("git", "init", "-q", "--bare", "-b", "main", ev.site / "origin.git", cwd=ev.site)
        sh("git", "init", "-q", "-b", "main", ev.repo, cwd=ev.site)
        for key, value in (("user.name", "eval"), ("user.email", "eval@example.com"), ("core.autocrlf", "false")):
            sh("git", "config", key, value, cwd=ev.repo)
    else:
        sh("svnadmin", "create", ev.site / "svnrepo", cwd=ev.site)
        sh("svn", "checkout", "-q", (ev.site / "svnrepo").as_uri(), ev.repo, cwd=ev.site)
    # The code only: .coverity-triage of the sample holds the emulator's data, with the answers in its comments.
    shutil.copytree(SAMPLE / "sample-target", ev.repo, ignore=shutil.ignore_patterns(".coverity-triage"),
                    dirs_exist_ok=True)
    for name, code in ev.sources.items():
        (ev.repo / "src" / name).write_bytes(code.encode("utf-8"))
    ev.env = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
    ev.env.update(COV_USER=emulator["user"], COV_AUTH_KEY=emulator["key"])
    for key in ("NO_PROXY", "no_proxy"):  # the emulator is reached without a proxy
        ev.env[key] = ",".join(filter(None, [ev.env.get(key), "127.0.0.1,localhost"]))
    setup = ev.snap / "skills" / "coverity-setup" / "scripts" / "setup.py"
    reply = parse(sh("uv", "run", "--native-tls", "--quiet", setup, "init", "--repo", ev.repo, "--url", emulator["url"],
                     "--stream", ev.warnings[0]["stream"], cwd=ev.repo, env=ev.env).stdout.decode("utf-8"))
    if not reply.get("ok"):
        raise Stop(f"設定ファイルを作成できませんでした: {json.dumps(reply, ensure_ascii=False)}")
    print(reply["message"], flush=True)
    if ev.sources:  # the limit that setup wrote may be lower than the number of warnings: fetch them all
        config = Path(reply["config_file"])
        config.write_text(re.sub(r"(?m)^( *limit:).*", rf"\1 {len(ev.warnings)}", config.read_text(encoding="utf-8")),
                          encoding="utf-8", newline="\n")
    for step in ((("add", "-A"), ("commit", "-q", "-m", "sample"), ("remote", "add", "origin", ev.site / "origin.git"),
                  ("push", "-q", "-u", "origin", "main")) if ev.vcs == "git" else
                 (("add", "-q", "--force", "."), ("commit", "-q", "-m", "sample"), ("update", "-q"))):
        sh(ev.vcs, *step, cwd=ev.repo)
    ev.out.mkdir()


def tree(ev: Eval) -> bytes:
    """What the person has: revision, branches and changed files of the repository; the branches of origin."""
    looks = ((("svn", "info", "--show-item", "revision"), ("svn", "status")) if ev.vcs == "svn" else
             (("git", "rev-parse", "HEAD"), ("git", "branch", "--list"), ("git", "status", "--porcelain"),
              ("git", "ls-remote", "--heads", "origin")))
    return b"".join(sh(*look, cwd=ev.repo).stdout for look in looks)


def writes(ev: Eval) -> list:
    """The triage values the emulator has received."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(ev.url + "/emulator/writes", timeout=10) as reply:
        return json.load(reply)["writes"]


def run_agent(ev: Eval, prompt: str, name: str, label: str, resume: str | None = None) -> Transcript:
    """One headless claude run in the repository; the transcript is <site>/<name>.jsonl."""
    agent, limit = STAGES[ev.stage]
    # A rule for a folder is Read(//<absolute path>/**); on Windows C:\a is //c/a.
    read, edit = (re.sub(r"^([A-Za-z]):", lambda m: "/" + m[1].lower(), p.as_posix()) for p in (ev.snap, ev.out))
    allowed = ["Bash(uv run *)", f"Skill({NAME}:*)", f"Read(/{read}/**)", f"Edit(/{edit}/**)",
               *(f"mcp__plugin_{NAME}_coverity-connect__{tool}" for tool in READ_TOOLS)]
    if ev.stage == 3:  # the orchestrator starts its three sub-agents
        allowed += [f"Agent({NAME}:{sub})" for sub in SUBAGENTS]
    assert not any("update_triage" in tool for tool in allowed), "the tool that writes to Coverity is never allowed"
    # The prompt comes right after -p: the options that take a list follow it.
    who = ["--agent", f"{NAME}:{agent}"] if agent else ["--model", ev.model]  # stage 1: the chat, with no agent
    argv = [shutil.which("claude"), "-p", prompt, *(["--resume", resume] if resume else []),
            "--plugin-dir", str(ev.snap), *who, "--setting-sources", "project",
            "--allowedTools", *allowed, "--add-dir", str(ev.out), "--output-format", "stream-json", "--verbose"]
    record, begin = ev.site / f"{name}.jsonl", time.monotonic()
    who = f"エージェント {agent} " if agent else f"エージェントなしのチャット（モデル {ev.model}）"
    print(f"{who}を実行しています（{label}、上限 {limit} 分）。記録: {record}", flush=True)
    with record.open("wb") as log, record.with_suffix(".err").open("wb") as err:
        proc = subprocess.Popen(argv, cwd=str(ev.repo), env=ev.env, stdin=subprocess.DEVNULL, stdout=log, stderr=err,
                                start_new_session=os.name == "posix")
        try:
            code = proc.wait(timeout=limit * 60)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            if proc.poll() is None:  # the time limit, or the EVAL was interrupted
                os.killpg(proc.pid, signal.SIGKILL) if os.name == "posix" else proc.kill()
                proc.wait()
    transcript, seconds = Transcript(record), round(time.monotonic() - begin)
    transcript.ended = code == 0
    # The CLI counts the whole session: the numbers of a resumed run include the run before it.
    ev.usage[name] = {"seconds": seconds, **transcript.usage}
    ev.check(f"{label}の実行が、時間内に正常に終了している", code == 0,
             f"{seconds} 秒" if code == 0 else f"上限の {limit} 分を超えたため、停止しました" if code is None
             else f"終了コード {code}。{record.with_suffix('.err')} を確認してください")
    return transcript


def load(ev: Eval) -> list[int]:
    """Read the newest result folder (ev.run): the CIDs of its issues.json. ev.states gets their state.json, ev.done
    the finished CIDs, ev.new those that were not finished at the last call, ev.items the items to decide on."""
    runs = sorted(p for p in ev.out.iterdir() if (p / "run.json").is_file())
    ev.run = runs[-1] if runs else ev.run
    cids = [issue["cid"] for issue in parse(ev.run / "issues.json").get("issues") or []]
    ev.states = {cid: parse(ev.run / "cid" / str(cid) / "state.json") for cid in cids}
    done = [c for c in cids if ev.states[c].get("status") == "done"]
    ev.new, ev.done = [c for c in done if c not in ev.done], done
    ev.items = [c for c in done if not ev.states[c].get("merged_into")]  # a merged CID shares the result of its leader
    return cids


def lacks(ev: Eval, cid: int) -> list[str]:
    """What the result of a finished CID lacks: fields of result.json, fix.patch."""
    result, patch = parse(ev.run / "cid" / str(cid) / "result.json"), ev.run / "cid" / str(cid) / "fix.patch"
    deviation, fix = (v if isinstance(v, dict) else {} for v in (result.get("deviation"), result.get("fix")))
    evidence = result.get("evidence")
    fields = {"cid": result.get("cid") == cid, "summary": result.get("summary"),
              "verdict": result.get("verdict") in (*FITS["fix"], *FITS["deviation"], "undetermined"),
              "evidence": isinstance(evidence, list) and evidence and all(
                  isinstance(e, dict) and e.get("file") and e.get("line") and e.get("note") for e in evidence),
              "recommendation": result.get("recommendation") in FITS,
              "confidence": result.get("confidence") in ("high", "medium", "low"),
              "fix.summary / no_fix_reason": bool(fix.get("summary")) != bool(result.get("no_fix_reason")),
              "fix（recommendation が fix のとき）": result.get("recommendation") != "fix" or bool(fix),
              "fix.patch": patch.is_file() == bool(fix),
              "deviation.comment": 0 < len(str(deviation.get("comment") or "")) <= 200,
              **{f"deviation.{key}": deviation.get(key) for key in ("classification", "action", "severity")}}
    return [f"{cid}: {name}" for name, fine in fields.items() if not fine]


def grade_results(ev: Eval) -> None:
    """The result folder: every CID finished, complete results, the expected conclusions, fix code that applies."""
    cids, run, states, done = load(ev), ev.run, ev.states, ev.done
    failed = [c for c in cids if states[c].get("status") == "failed"]
    bad = [c for c in cids if c not in done and not (c in failed and states[c].get("error"))]
    missing = [w["cid"] for w in ev.warnings if w["cid"] not in cids]
    ev.check("条件に合う警告をすべて取得し、どの CID も、処置済みまたは理由のある失敗になっている",
             cids and not bad and not missing,
             "結果フォルダーがありません" if not run.is_dir() else f"処置済み {len(done)} 件、失敗 {len(failed)} 件"
             + (f"、未処置または理由のない失敗: {bad}" if bad else "")
             + (f"、取得していない CID: {missing}" if missing else ""))
    summary = run / "summary.md"
    text = summary.read_text(encoding="utf-8", errors="replace") if summary.is_file() else ""
    absent = [c for c in cids if str(c) not in text]
    ev.check("summary.md に、取得した CID がすべて載っている", summary.is_file() and cids and not absent,
             "summary.md がありません" if not summary.is_file() else f"載っていない CID: {absent}" if absent
             else "" if cids else "取得した CID がありません")
    results = {c: parse(run / "cid" / str(c) / "result.json") for c in ev.items}
    patches = {c: run / "cid" / str(c) / "fix.patch" for c in ev.items}
    gaps = [gap for cid in ev.items for gap in lacks(ev, cid)]
    ev.check("処置済みの CID の result.json に、必要な項目がそろっている", ev.items and not gaps,
             "、".join(gaps) or (f"{len(ev.items)} 件" if ev.items else "処置済みの CID がありません"))
    wrong = []
    for cid, want in ev.expected.items():
        got, fits = results.get(states.get(cid, {}).get("merged_into") or cid), FITS[want["recommendation"]]
        if got is None:
            wrong.append(f"{cid}: 処置済みの結果がありません")
        elif got.get("recommendation") != want["recommendation"] or got.get("verdict") not in fits:
            wrong.append(f"{cid}: 想定は {want['recommendation']}（{' / '.join(fits)}）、"
                         f"結果は {got.get('recommendation')}（{got.get('verdict')}）")
    ev.check("推奨と判定が、expected.yaml の想定に合っている", not wrong, "、".join(wrong))
    # A fresh clone / checkout of the revision the fixes were made for.
    base, fresh, refused = str(parse(run / "run.json").get("base_revision") or ""), ev.site / "fresh", []
    steps = ((("git", "clone", "-q", ev.site / "origin.git", fresh),
              ("git", "-C", fresh, "checkout", "-q", "--detach", base))
             if ev.vcs == "git" else (("svn", "checkout", "-q", "-r", base, (ev.site / "svnrepo").as_uri(), fresh),))
    ready = base and all(sh(*step, cwd=ev.site, check=False).returncode == 0 for step in steps)
    for cid, patch in ((c, p) for c, p in patches.items() if ready and p.is_file()):
        if ev.vcs == "git":
            fine = sh("git", "apply", "--check", patch, cwd=fresh, check=False).returncode == 0
        else:  # svn prints one status line per file, with C for a hunk it rejects
            tried = sh("svn", "--non-interactive", "patch", "--dry-run", patch, cwd=fresh, check=False)
            lines = SVN_STATUS.findall((tried.stdout + tried.stderr).decode("utf-8", "replace"))
            fine = tried.returncode == 0 and lines and not any("C" in line for line in lines)
        refused += [] if fine else [str(cid)]
    ev.check("修正コード（fix.patch）を、処置を開始したリビジョンに適用できる", ready and not refused,
             f"適用できない CID: {', '.join(refused)}" if refused
             else f"{sum(patch.is_file() for patch in patches.values())} 件" if ready
             else f"run.json の base_revision（{base or 'なし'}）のコードを取り出せません")


def grade_stage2(ev: Eval, t: Transcript) -> None:
    asked = []
    for _, block in t.blocks[:t.plan()[0]]:  # before the one table (without a table: the whole run)
        if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
            asked.append("質問のツールを使用")
        elif block.get("type") == "text" and ASKS.search(block.get("text") or ""):
            asked.append(f"「…{block['text'].rstrip()[-30:]}」")
    ev.check("最後の一覧より前に、利用者に質問していない", not asked, "、".join(asked))
    grade_table(ev, t)


def grade_table(ev: Eval, t: Transcript, rows: bool = False) -> None:
    """The last message has every finished CID (rows: its row of the table, as apply.py plan gave it) and the
    question of apply.py plan."""
    plan, said = t.plan()[1], SQUEEZE.sub("", t.final)
    table = {cid: row for row, cid in re.findall(r"(?m)^(\| *(\d+).*)$", str(plan.get("table") or ""))}
    question, what = plan.get("question") or "", "処置済みの CID の一覧の行" if rows else "すべての CID"
    lost = [str(c) for c in ev.items if SQUEEZE.sub("", table.get(str(c), "\0") if rows else str(c)) not in said]
    shown = question and SQUEEZE.sub("", question) in said
    ev.check(f"最後のメッセージに、{what}と apply.py plan の質問文がある", ev.items and not lost and shown,
             "処置済みの CID がありません" if not ev.items else "apply.py plan の応答がありません" if not plan
             else f"ない CID: {', '.join(lost)}" if lost else "" if shown
             else "質問文が apply.py plan の応答と異なります" if question else "apply.py plan の応答に質問文なし")


def grade_stage3(ev: Eval, t: Transcript) -> None:
    ev.check("run.json の review が true になっている", parse(ev.run / "run.json").get("review") is True)
    loose = [c for c in ev.items if ev.states[c].get("review") not in ("pass", "flag")]
    ev.check("すべての結果に、検証の記録（合格・要確認）がある", ev.items and not loose,
             f"ない CID: {loose}" if loose else "" if ev.items else "処置済みの CID がありません")
    most = max(Counter(mid for mid, started in t.launches("triage-worker") if started).values(), default=0)
    ev.check("1回の応答で、triage-worker を2つ以上起動している", most >= 2, f"1回の応答での最大: {most}")
    into = {c: state.get("merged_into") or c for c, state in ev.states.items()}  # CID -> the CID whose result it has
    # One item per group of expected.yaml (with --copies: per copy), and no merge that leaves a group.
    apart = [c for group in ev.groups if len(found := {into.get(c, c) for c in group}) != 1 for c in sorted(found)]
    others = sorted(c for c, leader in into.items() if leader != c and not any({c, leader} <= g for g in ev.groups))
    unfinished = sorted(c for group in ev.groups for c in group if ev.states.get(c, {}).get("status") != "done")
    reasons = [reason for reason, found in ((f"統合されていない CID: {apart}", apart),
                                             (f"同じ原因ではない CID との統合: {others}", others),
                                             (f"処置済みでない CID: {unfinished}", unfinished)) if found]
    groups = " ／ ".join(", ".join(map(str, sorted(group))) for group in ev.groups)
    ev.check(f"同じ原因の CID（{groups}）だけが、{'コピーごとに' * (len(ev.groups) > 1)}1つに統合されている",
             not reasons, "、".join(reasons))
    # The orchestrator leaves this work to them: itself it records only 未検証 and never merges.
    for agent, job, command in (("triage-reviewer", "検証の記録", r"review\b.*--outcome +(pass|flag|rework)"),
                                ("triage-integrator", "統合", r"merge\b")):
        ran, own = sum(started for _, started in t.launches(agent)), len(t.commands(rf'triage\.py["\']? +{command}'))
        ev.check(f"{agent} を起動し、{job}を任せている", ran and not own,
                 f"起動 {ran} 回" + (f"、統括エージェントが自分で実行した{job}のコマンド: {own} 件" if own else ""))


def grade_before_ok(ev: Eval, baseline: bytes) -> None:
    reasons = [reason for reason, found in (("applied.json があります", (ev.run / "applied.json").is_file()),
                                             ("リポジトリが変更されています", tree(ev) != baseline)) if found]
    ev.check("OK の前に、修正を取り込んでいない", not reasons, "、".join(reasons))
    got = writes(ev)
    ev.check("OK の前に、Coverity に書き込んでいない", not got, f"書き込み {len(got)} 件" if got else "")


def grade_after_ok(ev: Eval, t: Transcript) -> None:
    fixes = parse(ev.run / "applied.json").get("fixes") or {}
    if ev.vcs == "git":  # the branch of the run is on origin, with one commit per fix
        branch = f"coverity-fix/{ev.run.name}"
        count = sh("git", "--git-dir", ev.site / "origin.git", "rev-list", "--count", f"main..{branch}", cwd=ev.site,
                   check=False)
        commits = int(count.stdout) if count.returncode == 0 else None
        fine = fixes and commits == len(fixes)
        where = f"origin のブランチ {branch}: " + ("ありません" if commits is None else f"コミット {commits} 件")
    else:
        changed = re.findall(rb"^M", sh("svn", "status", cwd=ev.repo).stdout, re.M)
        fine, where = fixes and changed, f"作業コピーで変更されたファイル: {len(changed)} 件"
    ev.check("OK の後に、修正を取り込んでいる", fine, f"{where}、applied.json の修正: {len(fixes)} 件")
    got, made = writes(ev), MADE.search(t.final)
    tried = any(str(block.get("name")).endswith("coverity_update_triage") for _, _, block in t.uses)
    final = t.final.replace("未処置", "")  # 未処置 is said of the CIDs that are left (stage 1), not of the registration
    said = next((word for word in LEFT if word in final), None) if "登録" in final and not made else None
    ev.check("Coverity への書き込みは行われず、最後のメッセージが、登録が残っていることを伝えている", not got and said,
             f"書き込み {len(got)} 件、書き込みのツールの呼び出し: {'あり（許可されず）' if tried else 'なし'}、"
             + (f"最後のメッセージに {said} あり" if said else f"最後のメッセージに「{made[0]}」とあります" if made
                else "最後のメッセージに、登録が残っていることの記載なし"))


def turn_list(ev: Eval, t: Transcript) -> None:
    cids = list(ev.states)
    missing = [w["cid"] for w in ev.warnings if w["cid"] not in cids] or [c for c in cids if str(c) not in t.final]
    ev.check("条件に合う警告をすべて取得し、メッセージに CID の一覧がある", cids and not missing,
             f"取得していない、またはメッセージにない CID: {missing}" if missing else "" if cids else "一覧なし")
    begun = [c for c in cids if ev.states[c]]
    ev.check("取得だけを行い、処置は開始していない", not begun, f"開始した CID: {begun}" if begun else "")


def turn_cid(ev: Eval, t: Transcript) -> None:
    gaps, folder, said = lacks(ev, CID), ev.run / "cid" / str(CID), SQUEEZE.sub("", t.final)
    ev.check(f"CID {CID} だけが処置済みになり、result.json に必要な項目がそろっている", ev.new == [CID] and not gaps,
             "、".join(gaps) if ev.new == [CID] else f"処置済みになった CID: {ev.new or 'なし'}")
    patch = (folder / "fix.patch").read_bytes().decode("utf-8", "replace") if (folder / "fix.patch").is_file() else ""
    # the fix code: the longest line the patch adds (no fix: its reason, under the word 修正コード)
    parts = {"修正コード": max(re.findall(r"(?m)^\+(?!\+\+)(.*)", patch), key=len, default="修正コード"),
             "逸脱コメント": str((parse(folder / "result.json").get("deviation") or {}).get("comment") or "\0")}
    lost = [name for name, part in parts.items() if SQUEEZE.sub("", part) not in said]
    ev.check("メッセージに、修正コードと逸脱コメントの両方がある", not lost, "、".join(lost) + "なし" * bool(lost))


def turn_next(ev: Eval, t: Transcript) -> None:
    ev.check("次の CID を、1件だけ処置している", len(ev.new) == 1, f"処置済みになった CID: {ev.new or 'なし'}")


def turn_table(ev: Eval, t: Transcript) -> None:
    grade_table(ev, t, rows=True)
    grade_before_ok(ev, ev.baseline)


# Stage 1: the requests of README.md and docs/guide/1-skill.md, typed into one conversation; the checks after each
TURNS = (("条件に合う警告を取得して", turn_list), (f"CID {CID} を処置して", turn_cid), ("次を処置して", turn_next),
         ("まとめを見せて", turn_table), ("OK", grade_after_ok))


def phases(ev: Eval, began: datetime) -> dict:
    """Seconds per phase, from log.txt of the result folder (triage.py writes a line when a command ends): the time
    up to a line counts for its command, that of a status line for the command after it. start begins with the
    first request; work after a merge or a review is rework. The table at the end is not in the log."""
    log, spent, later = ev.run / "log.txt", {}, False
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    for at, command in re.findall(r"(?m)^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d) (\w+) ", text):
        at, later = datetime.fromisoformat(at), later or command in ("merge", "review")
        if command != "status":
            phase = command if command not in ("next", "save") else "rework" if later else "work"
            spent[phase], began = spent.get(phase, 0) + round((at - began).total_seconds()), at
    return spent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="1〜3段階目の EVAL です。実際のモデルで実行して、採点します。")
    parser.add_argument("--stage", type=int, choices=(1, 2, 3), required=True, help="実行する段階")
    parser.add_argument("--vcs", choices=("git", "svn"), required=True)
    parser.add_argument("--work", help="作業フォルダーを作成する場所（リポジトリの外）。既定は、一時フォルダー")
    parser.add_argument("--answer-ok", action="store_true", help="2・3段階目で、一覧の質問に「OK」と回答します")
    parser.add_argument("--copies", type=int, choices=range(1, 10), default=1, metavar="N",
                        help="見本のコードと警告を N 倍（1〜9）にします。時間の測定に使用します")
    parser.add_argument("--model", default="haiku", help="1段階目のチャットのモデル（既定は haiku）")
    args = parser.parse_args(argv)
    for name in ("SIGTERM", "SIGHUP"):  # a stopped EVAL stops the agent and the emulator too: the finally blocks run
        signal.signal(getattr(signal, name, signal.SIGTERM), lambda *_: sys.exit(2))
    emulator = None
    try:
        ev = Eval(args.stage, args.vcs, args.work, args.copies, args.model)
        print(f"作業フォルダー: {ev.site}", flush=True)
        emulator = subprocess.Popen([sys.executable, "-B", "-m", "coverity_emulator", "--port", "0", "--data",
                                     str(ev.site / "warnings.json")], cwd=str(PLUGIN / "tests"),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        started = parse(emulator.stdout.readline().decode("utf-8"))  # one JSON line: url, user, key
        if not started:
            raise Stop("エミュレーター（tests/coverity_emulator）を起動できませんでした")
        ev.url = started["url"]
        print(f"エミュレーターを起動しました: {ev.url}", flush=True)
        prepare(ev, started)
        ev.baseline, began, session = tree(ev), datetime.now().replace(microsecond=0), None
        for number, (request, grade) in enumerate(TURNS if ev.stage == 1 else (), 1):
            turn = run_agent(ev, request, f"transcript-{number}", f"「{request}」", resume=session)
            session = turn.session
            if not (turn.ended and session):  # the conversation cannot go on: the later requests are not typed
                break
            load(ev)
            grade(ev, turn)
        if ev.stage != 1:
            first = run_agent(ev, PROMPT, "transcript", "最初の依頼")
            grade_results(ev)
            (grade_stage2 if ev.stage == 2 else grade_stage3)(ev, first)
            grade_before_ok(ev, ev.baseline)
            if args.answer_ok and first.ended and first.session:
                grade_after_ok(ev, run_agent(ev, "OK", "transcript-ok", "「OK」の回答", resume=first.session))
            elif args.answer_ok:
                ev.check("OK の後に、修正を取り込んでいる", False,
                         "最初の依頼が正常に終了しなかったため、回答していません")
    except Stop as stop:
        print(f"EVAL を実行できませんでした。{stop}", file=sys.stderr)
        return 2
    finally:
        if emulator:
            emulator.kill()
            emulator.wait()
    failed = [check["name"] for check in ev.checks if not check["ok"]]
    print(f"合格 {len(ev.checks) - len(failed)} 件、不合格 {len(failed)} 件です。", flush=True)
    print(json.dumps({"ok": not failed, "stage": ev.stage, "vcs": ev.vcs, "passed": len(ev.checks) - len(failed),
                      "failed": failed, "work": ev.site.as_posix(), "run_dir": ev.run.as_posix(),
                      "warnings": len(ev.warnings), "seconds": next(iter(ev.usage.values()), {}).get("seconds"),
                      "phases": phases(ev, began), "usage": ev.usage, "checks": ev.checks}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
