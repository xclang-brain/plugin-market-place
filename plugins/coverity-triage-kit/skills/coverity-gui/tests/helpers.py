"""Helpers of the tests: temporary repositories, fake run folders and a started screen server.

Only the files of this skill's folder are used. Scripts run with ``uv run --native-tls --quiet``.
"""

from __future__ import annotations

import atexit
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SKILL = Path(__file__).resolve().parents[1]
GUI = SKILL / "scripts" / "gui.py"
UV = shutil.which("uv")
sys.path.insert(0, str(SKILL / "scripts"))
sys.dont_write_bytecode = True
# The test modules imported before this one may have been cached: nothing is left in the folder.
atexit.register(shutil.rmtree, SKILL / "tests" / "__pycache__", ignore_errors=True)

from guilib import schema  # noqa: E402  (stdlib only)

CONFIG = """\
# チームの設定（人が書いた注釈）
coverity:
  url: https://coverity.example.com:8443   # 社内のサーバ
  user_env: COV_USER
  key_env: COV_AUTH_KEY
vcs:
  type: git
  base_branch: main
output_dir: {out}
parallel: 2
agents:
  review: {review}
"""
FILTER = """\
name: 分類がまだの警告
project: app
streams: [app-main]
triage:
  classification: [Unclassified]
limit: 20
"""


def env(tmp: Path, **extra: str) -> dict[str, str]:
    out = {k: v for k, v in os.environ.items() if k not in ("GUI_TOKEN", "GUI_IDLE_SECONDS")}
    return {**out, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "GUI_STATE_DIR": str(tmp / "state"), **extra}


def run_gui(args: list[str], environ: dict[str, str], stdin: Any = None, timeout: float = 180) -> tuple[int, dict[str, Any]]:
    if UV is None:
        raise AssertionError("uv が見つかりません。uv を入れて PATH を通してから、試験を動かしてください")
    data = None if stdin is None else (stdin if isinstance(stdin, bytes) else json.dumps(stdin, ensure_ascii=False).encode("utf-8"))
    proc = subprocess.run([UV, "run", "--native-tls", "--quiet", str(GUI), *args], input=data, capture_output=True,
                          env=environ, timeout=timeout, check=False)
    text = proc.stdout.decode(environ.get("PYTHONIOENCODING", "utf-8"))
    try:
        return proc.returncode, json.loads(text)
    except ValueError:
        raise AssertionError(f"not one JSON object: {text!r} {proc.stderr.decode('utf-8', 'replace')}") from None


def make_repo(root: Path, *, review: bool = False, config: bool = True) -> tuple[Path, Path]:
    """A git repository with .coverity-triage/ (config, one filter, knowledge), and its output folder."""
    repo, out = root / "repo", root / "out"
    repo.mkdir(parents=True)
    out.mkdir()
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    else:
        (repo / ".git").mkdir()
    if config:
        folder = repo / ".coverity-triage"
        (folder / "filters").mkdir(parents=True)
        (folder / "config.yaml").write_text(CONFIG.format(out=out.as_posix(), review=str(review).lower()), encoding="utf-8")
        (folder / "filters" / "untriaged.yaml").write_text(FILTER, encoding="utf-8")
        (folder / "knowledge.md").write_text("# 知識\n\n## 調査で使う事実\n", encoding="utf-8")
    return repo, out


def result(item: str, recommendation: str = "fix", confidence: str = "high", **extra: Any) -> dict[str, Any]:
    data = {
        "item": item,
        "verdict": {"judgement": "true_bug" if recommendation == "fix" else "false_positive",
                    "summary": f"{item} の要約", "rationale": "根拠の説明",
                    "evidence": [{"file": "src/reader.c", "line": 42, "note": "fgets が NULL を返す"}]},
        "recommendation": recommendation, "confidence": confidence, "confidence_reason": "呼び出し元を確かめた",
        "deviation": {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                      "comment": "誤検知。呼び出し元で NULL を確かめている（src/main.c:10）。"},
        "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Moderate", "summary": "NULL を確かめる",
                "impact": "なし", "exceeded_constraints": [], "already_fixed_on_latest": False},
        "revision_drift": {"status": "none", "detail": ""},
    }
    data.update(extra)
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def run_item(item_id: str, cids: list[int], status: str = "pending", **fields: Any) -> dict[str, Any]:
    """One run.json item as the batch run writes it (every field of assets/run.schema.json is there)."""
    item = {"id": item_id, "cids": list(cids), "status": status, "merged_into": None, "merged_from": [], "review": None,
            "slot": None, "stage": None, "attempts": 0, "review_rounds": 0, "error": "", "started": None, "seconds": None}
    if status in ("in_progress", "done", "error"):
        item.update(attempts=1, started=time.time() - 3600)
    if status in ("done", "error"):
        item["seconds"] = 120.0
    item.update(fields)
    return item


def _stage(items: list[dict[str, Any]], review: bool, summary: bool, initial: bool) -> str:
    """The stage the batch run would have written last for such a run."""
    if initial and not items:
        return "initial"
    if not items:
        return "done" if summary else "fetch"
    if any(i["status"] in ("pending", "in_progress") for i in items):
        return "items"
    if summary:
        return "done"
    return "review" if review else "verify"


def make_run(out: Path, run_id: str = "20261108-101500", items: list[dict[str, Any]] | None = None, *,
             review: bool = False, integration: bool = False, summary: bool = True, fmt: Any = 2,
             extra: dict[str, Any] | None = None) -> Path:
    """A run folder in the shape the batch run writes (design 4.11, assets/run.schema.json). ``items``: the
    fields that differ from a fresh item (see run_item); for each done item a result.json, an issue file, a
    report, a patch and a fixed file are written."""
    folder = out / run_id
    folder.mkdir(parents=True)
    given = items if items is not None else [
        {"id": "20001", "cids": [20001], "status": "done"},
        {"id": "20002", "cids": [20002], "status": "done"},
    ]
    items = [run_item(i["id"], i["cids"], i.get("status", "pending"),
                      **{k: v for k, v in i.items() if k not in ("id", "cids", "status")}) for i in given]
    extra = dict(extra or {})
    finished = summary and not any(i["status"] in ("pending", "in_progress") for i in items)
    data = {"format": fmt, "tool_version": "1", "run_id": run_id, "repo_root": str(out.parent / "repo"),
            "created_at": "2026-11-08T10:15:00+09:00", "filter_name": "untriaged", "stream": "app-main", "limit": 20,
            "initial": False, "review": review, "integration": integration, "max_rework": 1, "parallel": 2,
            "stage": _stage(items, review, summary, bool(extra.get("initial"))),
            "stopped": False, "stopped_at": None, "finished": finished,
            "finished_at": "2026-11-08T11:00:00+09:00" if finished else None, "halted": False, "halt_reason": None,
            "items": items, **extra}
    write_json(folder / "run.json", data)
    for n, item in enumerate(items):
        cid = (item.get("cids") or [0])[0]
        write_json(folder / "items" / item["id"] / f"issue-{cid}.json",
                   {"fetched_at": "2026-11-08T10:16:00+09:00",
                    "issue": {"cid": cid, "checker": "NULL_RETURNS", "file": "src/reader.c", "function": "read_header",
                              "line": 40 + n, "impact": "High", "stream": "app-main"},
                    "events": [], "checker_description": "NULL を返す関数"})
        if item.get("status") != "done":
            continue
        rec = "fix" if n % 2 == 0 else "deviation"
        write_json(folder / "items" / item["id"] / "result.json", result(item["id"], rec, "high" if n % 2 == 0 else "medium"))
        (folder / "cid").mkdir(exist_ok=True)
        (folder / "cid" / f"{item['id']}.html").write_text(f"<!doctype html><p>{item['id']} の詳細</p>", encoding="utf-8")
        if rec == "fix":
            (folder / "patches").mkdir(exist_ok=True)
            (folder / "patches" / f"{item['id']}-fix.patch").write_text(
                "diff --git a/src/reader.c b/src/reader.c\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
            fixed = folder / "fixed" / item["id"] / "fix" / "src" / "読む ファイル.c"
            fixed.parent.mkdir(parents=True, exist_ok=True)
            fixed.write_text("int main(void) { return 0; }\n", encoding="utf-8")
            write_json(folder / "items" / item["id"] / "saved.json",
                       {"fix": {"files": ["src/読む ファイル.c"], "patch": str(folder / "patches" / f"{item['id']}-fix.patch"),
                                "branch": f"coverity-fix/cid-{cid}", "commit": "abc123"}})
    if summary:
        (folder / "summary.html").write_text("<!doctype html><p>一覧</p>", encoding="utf-8")
        write_json(folder / "approvals.json", {"version": 1, "updated_at": "2026-11-08T11:00:00+09:00", "items": {
            item["id"]: {"decision": "fix" if n % 2 == 0 else "deviation", "edited": False,
                         "updated_at": "2026-11-08T11:00:00+09:00"}
            for n, item in enumerate(items) if item.get("status") == "done"}})
    return folder


class Server:
    """The screen's server started for a repository (``gui.py serve``), and requests to it."""

    def __init__(self, repo: Path, tmp: Path, **environ: str):
        self.token = "test-token-0123456789abcdef"
        if UV is None:
            raise AssertionError("uv が見つかりません。uv を入れて PATH を通してから、試験を動かしてください")
        ready = tmp / f"ready-{time.monotonic_ns()}.json"
        self.log = (tmp / "server.log").open("wb")
        self.proc = subprocess.Popen([UV, "run", "--native-tls", "--quiet", str(GUI), "serve", "--repo", str(repo),
                                      "--ready-file", str(ready)],
                                     env=env(tmp, GUI_TOKEN=self.token, **environ), stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        while not ready.is_file():
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.close()
                raise AssertionError("the server did not start: " + (tmp / "server.log").read_text(errors="replace"))
            time.sleep(0.05)
        while True:  # on Windows the file can be in the middle of being replaced for a moment
            try:
                self.port = json.loads(ready.read_text(encoding="utf-8"))["port"]
                break
            except (PermissionError, ValueError, KeyError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"

    def request(self, method: str, path: str, body: Any = None, *, token: bool = True, host: str | None = None,
                headers: dict[str, str] | None = None, raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        send = {"Host": host or self.host}
        if token:
            send["X-Token"] = self.token
        if method == "POST":
            send.update({"Content-Type": "application/json", "Origin": self.origin})
        send.update(headers or {})
        data = raw if raw is not None else (None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"))
        try:
            conn.request(method, path, body=data, headers=send)
            res = conn.getresponse()
            return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read()
        finally:
            conn.close()

    def get(self, path: str, **kw: Any) -> tuple[int, Any]:
        status, _, data = self.request("GET", path, **kw)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data

    def post(self, path: str, body: Any, **kw: Any) -> tuple[int, Any]:
        status, _, data = self.request("POST", path, body, **kw)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.post("/api/close", {})
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.log.close()


def temp_dir(case: Any) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="gui-test-"))
    case.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    return folder


def contract_schema(name: str) -> dict[str, Any] | None:
    return schema.load(SKILL / "assets" / name)
