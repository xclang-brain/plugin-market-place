"""Test helpers: a temporary git repository with settings, the scripts run as the agents run them, and the
files the Coverity tool and the agents would write (design 4.3, 4.4, 4.5, 4.6, 4.7).

Nothing is written inside the skill folder: every repository, run folder and cache lives in a temporary
folder that is removed afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

SKILL = Path(__file__).resolve().parents[1]
RUN = SKILL / "scripts" / "run.py"
SCRIPTS = SKILL / "scripts"
STREAM = "app-main"
TIMEOUT = 120

READER_C = """#include <stdio.h>
#include <stdlib.h>

char *read_header(FILE *fp) {
    char *buf = malloc(64);
    fgets(buf, 64, fp);
    return buf;
}

int open_file(const char *name) {
    FILE *fp = fopen(name, "r");
    if (fp == NULL) {
        return -1;
    }
    return 0;
}

int parse(const char *p) {
    int n = 0;
    while (*p) {
        n += *p++ - '0';
    }
    return n;
}
"""

_RUNNER: list[str] | None = None


def runner() -> list[str]:
    """How the scripts are run: with uv as in real use, else with this Python when pyyaml is there."""
    global _RUNNER
    if _RUNNER is None:
        uv = shutil.which("uv")
        if uv:
            _RUNNER = [uv, "run", "--native-tls", "--quiet", str(RUN)]
        else:
            try:
                import yaml  # noqa: F401
            except ImportError:
                raise unittest.SkipTest("uv も pyyaml もないため、スクリプトを動かせません") from None
            _RUNNER = [sys.executable, str(RUN)]
    return _RUNNER


def import_runlib() -> None:
    """Make ``import runlib`` work in the tests (the package of this skill's scripts)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


def base_env(tmp: Path) -> dict[str, str]:
    gitconfig = tmp / "gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("COV_USER", "COV_AUTH_KEY", "COVERITY_PASSPHRASE", "PYTHONIOENCODING", "VIRTUAL_ENV")}
    env.update(PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0", PYTHONIOENCODING="utf-8")
    return env


def git(repo: Path, *args: str, env: dict[str, str]) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True)
    return proc.stdout.decode("utf-8", "replace").strip()


CONFIG = """# テストの設定
coverity:
  url: https://coverity.example.com/
  api: fake
  user_env: COVTEST_USER
  key_env: COVTEST_KEY
vcs:
  type: git
  base_branch: main
output_dir: ../out
parallel: {parallel}
options:
  markdown: {markdown}
  metrics: {metrics}
verify:
  default: {verify}
  initial: {initial}
  build_command: {build_command}
agents:
  review: {review}
  integration: {integration}
  max_rework: {max_rework}
unknown_key_for_other_parts: 1
"""

FILTER = """name: 分類がまだの警告
project: app
streams: [app-main]
triage:
  classification: [Unclassified]
limit: 20
"""


class RunCase(unittest.TestCase):
    """A temporary repository with settings, and helpers to drive a run as an agent would."""

    settings: dict[str, Any] = {}

    def setUp(self) -> None:
        if not shutil.which("git"):
            raise unittest.SkipTest("git がありません")
        self.cmd = runner()
        self.tmp = Path(tempfile.mkdtemp(prefix="runskill-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = base_env(self.tmp)
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "reader.c").write_text(READER_C, encoding="utf-8")
        self.write_config(**self.settings)
        git(self.repo, "init", "-q", "-b", "main", env=self.env)
        git(self.repo, "add", "-A", env=self.env)
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init", env=self.env)

    def write_config(self, **values: Any) -> None:
        defaults = {"parallel": 2, "markdown": "false", "metrics": "false", "verify": "none", "initial": "false",
                    "build_command": '""', "review": "false", "integration": "false", "max_rework": 1}
        defaults.update(values)
        folder = self.repo / ".coverity-triage"
        (folder / "filters").mkdir(parents=True, exist_ok=True)
        (folder / "config.yaml").write_text(CONFIG.format(**defaults), encoding="utf-8")
        (folder / "filters" / "untriaged.yaml").write_text(FILTER, encoding="utf-8")

    # ---- the scripts ----------------------------------------------------------------------

    def run_json(self, *args: str, ok: bool = True, stdin: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
        proc = subprocess.run([*self.cmd, *args], env=env or self.env, capture_output=True, timeout=TIMEOUT,
                              input=stdin.encode("utf-8") if stdin is not None else None)
        text = proc.stdout.decode("utf-8")
        try:
            data = json.loads(text)
        except ValueError:
            self.fail(f"one JSON object expected: {text!r} {proc.stderr.decode('utf-8', 'replace')}")
        if ok:
            self.assertTrue(data.get("ok"), data)
            self.assertEqual(proc.returncode, 0, data)
        else:
            self.assertFalse(data.get("ok"), data)
            self.assertEqual(proc.returncode, 1, data)
        return data

    def new_run(self, *extra: str) -> dict[str, Any]:
        return self.run_json("new-run", "--repo", str(self.repo), *extra)

    def tick(self, run_dir: str, *extra: str, wait: int = 10) -> dict[str, Any]:
        return self.run_json("tick", "--run", run_dir, "--wait", str(wait), *extra)

    def state(self, run_dir: str) -> dict[str, Any]:
        return json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))

    def edit_state(self, run_dir: str, fn: Any) -> None:
        path = Path(run_dir) / "run.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        fn(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- what the Coverity tool writes ---------------------------------------------------

    def write_issues(self, path: Path, issues: list[dict[str, Any]], finished: bool = True, **extra: Any) -> None:
        data = {"filter_sha": "x", "stream": STREAM, "limit": 20, "started_at": "2026-11-08T10:00:00",
                "finished_at": "2026-11-08T10:00:05" if finished else None, "next_offset": 0, "limited": False,
                "revision_field": "sourceVersion", "revision_checked": True, "analyzed_revision": None,
                "revision_error": None, "issues": issues, "excluded_count": 0, "excluded": [], "excluded_cids": [],
                "requests": [], **extra}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_details(self, folder: Path, issue: dict[str, Any], events: list[dict[str, Any]] | None = None) -> None:
        events = events if events is not None else [
            {"file": issue["file"], "line": issue["line"] - 1, "tag": "alloc", "description": "確保する", "main": False},
            {"file": issue["file"], "line": issue["line"], "tag": issue["checker"].lower(), "description": "ここで問題", "main": True}]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"issue-{issue['cid']}.json").write_text(json.dumps(
            {"fetched_at": "2026-11-08T10:00:06", "issue": issue, "events": events, "checker_description": "説明"},
            ensure_ascii=False), encoding="utf-8")

    def do_fetch(self, action: dict[str, Any], issues: list[dict[str, Any]]) -> None:
        """Do what the Coverity tool does for a fetch action."""
        if action["type"] == "fetch_issues":
            self.write_issues(Path(action["output_file"]), issues)
        elif action["type"] == "fetch_details":
            by_cid = {i["cid"]: i for i in issues}
            for cid in action["cids"]:
                self.write_details(Path(action["output_dir"]), by_cid[cid])

    # ---- what the agents write -------------------------------------------------------------

    @staticmethod
    def brief_path(action: dict[str, Any]) -> Path:
        prompt = action["prompt"]
        return Path(prompt.split("指示ファイル ", 1)[1].split(" を読み", 1)[0])

    @staticmethod
    def brief_value(brief: Path, label: str) -> str:
        for line in brief.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"| {label}"):
                return line.split("`")[1]
        raise AssertionError(f"{label} not in {brief}")

    def work_as_worker(self, action: dict[str, Any], recommendation: str = "fix", change: bool = True,
                       comment: str | None = None, excluded: list[int] | None = None, **result_extra: Any) -> dict[str, Any]:
        brief = self.brief_path(action)
        fix_dir = Path(self.brief_value(brief, "直すフォルダ"))
        result_file = Path(self.brief_value(brief, "結果ファイル（書く）"))
        if change:
            target = fix_dir / "src" / "reader.c"
            text = target.read_text(encoding="utf-8")
            marker = f"/* fix {action['item']} */\n"
            target.write_text(text.replace("#include <stdlib.h>\n", "#include <stdlib.h>\n" + marker, 1), encoding="utf-8")
        result = result_json(action["item"], recommendation, comment=comment, excluded=excluded, **result_extra)
        result_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result

    def review_as_reviewer(self, action: dict[str, Any], outcome: str = "pass", **extra: Any) -> dict[str, Any]:
        brief = self.brief_path(action)
        out = Path(self.brief_value(brief, "確かめの結果（書く）"))
        review = review_json(action["item"], outcome, **extra)
        out.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        return review

    def drive(self, run_dir: str, issues: list[dict[str, Any]], worker=None, reviewer=None, integrator=None,
              max_ticks: int = 60, out: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the tick loop as the agent does, with fake tools and agents, until done or stop."""
        out = out or self.tick(run_dir)
        for _ in range(max_ticks):
            if out.get("done") or out.get("stop"):
                return out
            if not out["actions"]:
                out = self.tick(run_dir)
                continue
            reports = []
            for action in out["actions"]:
                if action["type"] in ("fetch_issues", "fetch_details"):
                    self.do_fetch(action, issues)
                    reports.append(("--finished", action["task"]))
                elif action["type"] == "launch":
                    handler = {"worker": worker or (lambda a: self.work_as_worker(a)),
                               "reviewer": reviewer or (lambda a: self.review_as_reviewer(a)),
                               "integrator": integrator}[action["role"]]
                    verdict = handler(action)
                    reports.append(("--failed", action["task"]) if verdict == "fail" else ("--finished", action["task"]))
            for flag, task in reports:
                extra = ["--reason", "テストで失敗させた"] if flag == "--failed" else []
                out = self.tick(run_dir, flag, task, *extra)
                if out.get("stop"):
                    return out
                if out["actions"]:
                    break  # hand out what the last report gave before the others (as an agent would)
        self.fail(f"the run did not end: {out}")


def issue(cid: int, checker: str = "NULL_RETURNS", file: str = "src/reader.c", function: str = "read_header",
          line: int = 6, **extra: Any) -> dict[str, Any]:
    return {"cid": cid, "checker": checker, "file": file, "function": function, "line": line, "impact": "High",
            "stream": STREAM, "classification": "Unclassified", "action": "Undecided", "status": "New", **extra}


def result_json(item: str, recommendation: str = "fix", comment: str | None = None, excluded: list[int] | None = None,
                **extra: Any) -> dict[str, Any]:
    judgement = "true_bug" if recommendation == "fix" else "false_positive"
    deviation = ({"classification": "Bug", "action": "Ignore", "severity": "Moderate"} if judgement == "true_bug"
                 else {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified"})
    data = {"item": item,
            "verdict": {"judgement": judgement, "summary": "戻り値を確かめずに使っている", "rationale": "呼び出し元でも確かめていない",
                        "evidence": [{"file": "src/reader.c", "line": 6, "note": "ここで使う"}]},
            "recommendation": recommendation, "confidence": "high", "confidence_reason": "経路をすべて読んだ",
            "deviation": {**deviation, "comment": comment if comment is not None else
                          ("本物の不具合。fgets の戻り値を確かめていないが、入力は固定のファイル（src/reader.c:6）だけである。"
                           if judgement == "true_bug" else "誤検知。呼び出し元 main()（src/reader.c:6）で NULL を除いてから渡している。")},
            "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Major", "summary": "NULL を確かめる",
                    "impact": "戻り値が NULL のときだけ動きが変わる", "exceeded_constraints": [], "already_fixed_on_latest": False},
            "revision_drift": {"status": "none", "detail": ""},
            "group_excluded_cids": excluded or []}
    data.update(extra)
    return data


def review_json(item: str, outcome: str = "pass", fail: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    criteria = json.loads((SKILL / "assets" / "criteria.json").read_text(encoding="utf-8"))["criteria"]
    findings = []
    for c in criteria:
        if c["scope"] != "item":
            continue
        ok = c["id"] not in fail
        detail = "対象外" if c.get("applies_when") and ok else ("確かめた" if ok else "根拠の行にそのコードがない")
        findings.append({"check": c["id"], "ok": ok, "detail": detail, "file": "src/reader.c", "line": 6})
    data = {"item": item, "outcome": outcome, "findings": findings,
            "confidence_cap": "medium" if outcome == "flag" else None,
            "rework": ["根拠の行を読み直して直す"] if outcome == "rework" else [], "note": "根拠の行と経路の条件を確かめた"}
    data.update(extra)
    return data


def wait_for(predicate, timeout: float = 30.0, step: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
