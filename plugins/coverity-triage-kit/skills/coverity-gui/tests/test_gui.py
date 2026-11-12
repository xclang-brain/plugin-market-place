"""The screen, against a real server process on 127.0.0.1 and result folders written as the triage
scripts write them. The last test drives the page in Chromium (Playwright; skipped without it).

gui.py runs neither git nor svn: the repository is a folder with a settings file, and its name has a
space and Japanese characters. Run with ``python -B`` (tests/run.py does): nothing is left in the skill folder.
"""

import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
GUI = HERE.parent / "scripts" / "gui.py"
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
FIX = "本物のバグ。src/a.c:3 で p を NULL のまま参照する。"
KEEP = "誤検知。src/b.c:2 で長さを確認しており、範囲外は参照しない。"
FILE = "src/日本語.c"
PATHS = f"--- a/{FILE}\n+++ b/{FILE}\n"
HUNK = "@@ -1,4 +1,4 @@\n int a(int *p)\n {\n-    return *p;  // 参照\n+    return p ? *p : 0;  // 確認\n }\n"
PATCH = PATHS.encode("utf-8") + HUNK.encode("cp932")  # the paths as svn writes them, the content as the file is
FORBIDDEN = "この URL では画面を表示できません。チャットで「画面を開いて」と入力し、表示された URL を開いてください"
# The server as open starts it, but with a limit of 2 seconds without a request (30 minutes in real use).
SHORT = """\
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
import sys

sys.path.insert(0, sys.argv[1])
import gui

assert gui.IDLE == 30 * 60, gui.IDLE
gui.IDLE = 2
sys.exit(gui.main(["serve", "--repo", sys.argv[2]]))
"""

# The default choice of an item: the same rule as in skills/coverity-apply (contracts.md, section 3).
# confidence, the run has a review, state.review, build result, recommendation -> choice, reason (備考)
DEFAULTS = [
    ("high", False, None, None, "fix", "fix", ""),
    ("medium", False, None, None, "deviation", "deviation", ""),
    ("low", False, None, None, "fix", "none", "確信度が低いため"),
    ("high", True, "pass", None, "fix", "fix", ""),
    ("high", True, "flag", None, "fix", "none", "検証で要確認のため"),
    ("high", True, "unverified", None, "deviation", "none", "未検証のため"),
    ("high", True, None, None, "fix", "none", "未検証のため"),  # not reviewed yet in a run with review
    ("high", False, None, "ok", "fix", "fix", ""),
    ("high", False, None, "unknown", "fix", "fix", ""),
    ("high", False, None, "not_applied", "fix", "fix", ""),
    ("high", False, None, "failed", "fix", "none", "ビルドに失敗したため"),
    ("medium", True, "pass", "failed", "deviation", "none", "ビルドに失敗したため"),
    ("low", True, "flag", "failed", "fix", "none", "確信度が低いため"),  # the first reason that applies
    ("medium", True, "flag", "failed", "fix", "none", "検証で要確認のため"),
]


def command() -> list:
    """gui.py needs PyYAML: this Python when it has it, else uv as in real use."""
    try:
        import yaml  # noqa: F401
        return [sys.executable, str(GUI)]
    except ImportError:
        if not shutil.which("uv"):
            raise unittest.SkipTest("PyYAML のある Python も uv もないため、gui.py を実行できません") from None
        return [shutil.which("uv"), "run", "--native-tls", "--quiet", str(GUI)]


def gui(*args, env=None) -> dict:
    """Run one command of gui.py: it must exit 0 and print exactly one JSON object in UTF-8."""
    proc = subprocess.run([*command(), *map(str, args)], capture_output=True, env=env or ENV, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"gui.py {args[0]}: exit {proc.returncode}\n{proc.stdout.decode()}{proc.stderr.decode()}")
    return json.loads(proc.stdout.decode("utf-8"))


def put(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8"))


def item(run: Path, cid: int, fix: bool = True, recommendation: str = "", **state) -> None:
    """A finished CID as save leaves it: with a fix (fix.patch), or with the reason why there is none."""
    folder = run / "cid" / str(cid)
    outputs = {"fix": {"summary": "NULL のときは 0 を返します"}} if fix else {"no_fix_reason": "コードに問題がないため"}
    put(folder / "result.json", {
        "cid": cid, "verdict": "true_bug" if fix else "false_positive", "summary": "確認せずに参照する経路があります",
        "evidence": [{"file": "src/a.c", "line": 3, "note": "p を確認せずに参照しています"}],
        "recommendation": recommendation or ("fix" if fix else "deviation"), "confidence": "high", **outputs,
        "deviation": {"comment": FIX if fix else KEEP, "classification": "Bug" if fix else "False Positive",
                      "action": "Ignore", "severity": "Major" if fix else "Unspecified"}})
    if fix:
        put(folder / "fix.patch", PATCH)
    put(folder / "state.json", {"status": "done", "slot": 1, "tries": 1, "files": [FILE] if fix else [],
                                "confidence": "high", **state})


def decisions(run: Path) -> dict:
    """decisions.json without the times (ISO 8601, read by the skill that takes the fixes in)."""
    saved = json.loads((run / "decisions.json").read_bytes().decode("utf-8"))["decisions"]
    assert all(datetime.fromisoformat(entry["at"]).tzinfo for entry in saved.values()), saved
    return {cid: {key: value for key, value in entry.items() if key != "at"} for cid, entry in saved.items()}


class ScreenCase(unittest.TestCase):
    """A repository with a settings file and a running screen. The browser is a command that notes the URL."""

    def setUp(self):
        self.top = Path(tempfile.mkdtemp(prefix="gui-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.top, ignore_errors=True)
        self.repo, self.out, self.runs = self.top / "日本語 リポジトリ", self.top / "結果 フォルダー", 0
        put(self.repo / ".coverity-triage" / "config.yaml",
            'coverity:\n  url: "https://coverity.example.co.jp:8443"\n  stream: "app-main"\n'
            'conditions:\n  impacts: High\noutput_dir: "../結果 フォルダー"\n'.encode("utf-8"))
        browser = self.top / ("browser.cmd" if os.name == "nt" else "browser")
        browser.write_text('@echo %*>> "%~dp0opened.txt"\r\n' if os.name == "nt"
                           else '#!/bin/sh\necho "$1" >> "$(dirname "$0")/opened.txt"\n', encoding="ascii")
        browser.chmod(0o755)
        self.env = {**ENV, "BROWSER": str(browser)}
        self.addCleanup(gui, "stop", "--repo", self.repo)
        self.opened = gui("open", "--repo", self.repo, env=self.env)
        self.port, self.token = urlsplit(self.opened["url"]).port, parse_qs(urlsplit(self.opened["url"]).query)["t"][0]

    def run_folder(self, cids=(), review: bool = False, repo: str = "", **more) -> Path:
        """A new result folder, the newest one, with these CIDs in its list."""
        self.runs += 1
        run = self.out / f"20261110-{self.runs:06d}"
        put(run / "run.json", {"repo": repo or self.repo.as_posix(), "vcs": "svn", "base_branch": "",
                               "base_revision": "2", "stream": "app-main", "conditions": {}, "slots": 1,
                               "review": review, "all": review})
        put(run / "issues.json", {
            "stream": "app-main", "conditions": {}, "fetched_at": "2026-11-10T15:30:05+09:00",
            "total_matched": len(cids), "limited": False, **more,
            "issues": [{"cid": cid, "checker": "NULL_RETURNS", "file": "src/a.c", "line": 3, "function": "a",
                        "impact": "High"} for cid in cids]})
        return run

    def http(self, method: str, path: str, body=None, **headers):
        """(status, the JSON object or the text) of one request, sent as the page sends it unless told otherwise."""
        send = {"X-Token": self.token}
        if method == "POST":
            send.update({"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"})
        send.update({name.replace("_", "-"): value for name, value in headers.items()})
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=data, headers={k: v for k, v in send.items() if v is not None})
            answer = conn.getresponse()
            raw, kind = answer.read().decode("utf-8"), answer.getheader("Content-Type", "")
            return answer.status, json.loads(raw) if kind.startswith("application/json") else raw
        finally:
            conn.close()

    def rows(self) -> dict:
        return {row["cid"]: row for row in self.http("GET", "/api/state")[1]["rows"]}

    def decide(self, run: Path, cid, decision, comment=None, name: str = ""):
        body = {"run": name or run.name, "cid": cid, "decision": decision, "comment": comment}
        return self.http("POST", "/api/decision", body)


class ApiTest(ScreenCase):
    def test_state_and_one_cid(self):
        run = self.run_folder(range(20001, 20010), review=True, total_matched=137, limited=True)
        item(run, 20001, review="pass")
        item(run, 20002, fix=False, review="pass")
        item(run, 20003, review="flag", members=[20004])
        put(run / "cid/20003/review.json", {"outcome": "flag", "points": ["src/a.c:3 呼び出し元を確認できません"]})
        item(run, 20004, merged_into=20003)
        put(run / "cid/20005/state.json", {"status": "failed", "error": "処置を2回開始しましたが、どちらも結果が保存されませんでした"})
        put(run / "cid/20006/state.json", {"status": "working", "slot": 1, "claimed_at": "2026-11-10T15:42:10"})
        put(run / "cid/20008/state.json", {"status": "rework", "rework": []})  # sent back: pending again
        put(run / "cid/20009/state.json", {"status": "done", "confidence": "high"})
        put(run / "cid/20009/result.json", b"{broken")
        put(run / "build.json", {"status": "done", "exit_code": 0, "cids": {"20001": "ok", "20003": "unknown"}})
        status, state = self.http("GET", "/api/state")
        rows = {row["cid"]: row for row in state.pop("rows")}
        self.assertEqual((status, state), (200, {
            "ok": True, "repo": self.repo.as_posix(), "run": run.name, "run_dir": run.as_posix(),
            "conditions": {"stream": "app-main", "checkers": [], "impacts": ["High"],
                           "classification": ["Unclassified"], "limit": 20},  # the defaults are filled in
            "fetched": {"at": "2026-11-10T15:30:05+09:00", "count": 9, "total_matched": 137, "limited": True},
            "counts": {"total": 9, "pending": 2, "working": 1, "done": 5, "failed": 1, "remaining": 3},
            "review": True, "build": True, "working": [{"cid": 20006, "claimed_at": "2026-11-10T15:42:10"}],
            "choices": {"fix": 1, "deviation": 1, "none": 1}}))
        self.assertEqual(list(rows), [20001, 20002, 20003, 20005, 20006, 20007, 20008, 20009])  # 20004 is merged
        self.assertEqual(rows[20001], {
            "cid": 20001, "members": [], "checker": "NULL_RETURNS", "place": "src/a.c:3", "status": "done",
            "verdict": "true_bug", "recommendation": "fix", "confidence": "high", "review": "pass", "build": "ok",
            "files": [FILE], "comment": FIX, "choice": "fix", "choice_from": "default", "reason": "",
            "can_fix": True, "comment_edited": False, "done": None, "error": ""})
        for cid, want in {20002: {"files": [], "comment": KEEP, "choice": "deviation", "can_fix": False, "build": None},
                          20003: {"members": [20004], "review": "flag", "choice": "none", "reason": "検証で要確認のため"},
                          20005: {"status": "failed", "choice": None, "error": "処置を2回開始しましたが、どちらも結果が保存されませんでした"},
                          20006: {"status": "working", "comment": None}, 20007: {"status": "pending"},
                          20008: {"status": "pending"}, 20009: {"status": "unreadable", "choice": None}}.items():
            self.assertEqual({key: rows[cid][key] for key in want}, want, cid)

        status, one = self.http("GET", "/api/cid/20003")
        self.assertEqual((status, one), (200, {
            "ok": True, "cid": 20003, "members": [20004], "status": "done",
            "issue": {"checker": "NULL_RETURNS", "file": "src/a.c", "line": 3, "function": "a", "impact": "High"},
            "result": {"verdict": "true_bug", "summary": "確認せずに参照する経路があります",
                       "evidence": [{"file": "src/a.c", "line": 3, "note": "p を確認せずに参照しています"}],
                       "recommendation": "fix", "confidence": "high", "fix_summary": "NULL のときは 0 を返します",
                       "no_fix_reason": None},
            "patch": PATHS + HUNK, "review": {"outcome": "flag", "points": ["src/a.c:3 呼び出し元を確認できません"]},
            "build": "unknown", "deviation": {"comment": FIX, "original_comment": FIX, "classification": "Bug",
                                              "action": "Ignore", "severity": "Major"},
            "choice": "none", "choice_from": "default", "can_fix": True, "done": None}))
        no_fix = self.http("GET", "/api/cid/20002")[1]
        self.assertEqual((no_fix["patch"], no_fix["result"]["no_fix_reason"]), (None, "コードに問題がないため"))
        self.assertEqual(self.http("GET", "/api/cid/20007")[1]["result"], None)  # not finished: the warning only
        for path in ("/api/cid/20004", "/api/cid/1", "/api/cid/x", "/api/other"):
            self.assertEqual(self.http("GET", path)[0], 404, path)

    def test_default_choice(self):
        for n, (confidence, review, reviewed, build, recommendation, choice, reason) in enumerate(DEFAULTS):
            run = self.run_folder([20001], review=review)
            item(run, 20001, recommendation=recommendation, confidence=confidence,
                 **({"review": reviewed} if reviewed else {}))
            if build:
                put(run / "build.json", {"status": "done", "cids": {"20001": build}})
            row = self.rows()[20001]
            self.assertEqual((row["choice"], row["choice_from"], row["reason"]), (choice, "default", reason),
                             DEFAULTS[n])

    def test_decision_is_saved_or_refused(self):
        run = self.run_folder(range(20001, 20009))
        for cid, fix in ((20001, True), (20002, False), (20003, True), (20004, True), (20005, True), (20006, False)):
            item(run, cid, fix=fix, **({"merged_into": 20003} if cid == 20004 else {}))
        put(run / "cid/20007/state.json", {"status": "failed", "error": "失敗"})
        put(run / "applied.json", {"vcs": "svn", "fixes": {"20005": {"files": ["src/a.c"], "at": "2026-11-10T16:00"}}})
        put(run / "written.json", {"written": {"20006": {"comment": KEEP, "at": "2026-11-10T16:00:00"}}})
        self.assertEqual([self.rows()[cid]["done"] for cid in (20005, 20006)], ["applied", "written"])
        for cid, decision, comment, name, error in (
                (20001, "fix", None, "20261110-000000", "結果フォルダーが新しくなっています。画面を読み込み直してください"),
                (1, "fix", None, "", "この CID は警告の一覧にありません"),
                ("20001", "fix", None, "", "この CID は警告の一覧にありません"),
                (20004, "fix", None, "", "CID 20004 は CID 20003 に統合されています"),
                (20007, "none", None, "", "CID 20007 は処置済みではありません"),
                (20008, "none", None, "", "CID 20008 は処置済みではありません"),
                (20005, "none", None, "", "この CID は取り込み済みです"),
                (20006, "none", None, "", "この CID は登録済みです"),
                (20001, "apply", None, "", "選択が正しくありません。fix、deviation、none のいずれかを指定してください"),
                (20002, "fix", None, "", "この CID には修正コードがないため、「修正を取り込む」は選択できません"),
                (20001, "deviation", "  ", "", "逸脱コメントを入力してください"),
                (20001, "deviation", FIX + "\n次の行", "", "逸脱コメントは、改行を入れずに1行で入力してください"),
                (20001, "deviation", "本物のバグ。" + "あ" * 195, "", "逸脱コメントは 200 文字以内で入力してください（現在 201 文字）"),
                (20001, "deviation", "誤検知。" + FIX, "", "逸脱コメントは、元のコメントと同じ「本物のバグ。」で始めてください")):
            self.assertEqual(self.decide(run, cid, decision, comment, name), (400, {"ok": False, "error": error}))
        put(run / "cid/20002/result.json", b"[]")
        self.assertEqual(self.decide(run, 20002, "none"), (400, {"ok": False, "error": "この CID は読み取れません"}))
        self.assertFalse((run / "decisions.json").exists())

        edited = "本物のバグ。" + "あ" * 194
        status, reply = self.decide(run, 20001, "deviation", f" {edited} ")
        self.assertEqual((status, reply["ok"]), (200, True))
        want = {"choice": "deviation", "choice_from": "screen", "comment": edited, "comment_edited": True, "reason": ""}
        self.assertEqual({key: reply["row"][key] for key in want}, want)
        self.assertEqual(self.decide(run, 20003, "none", FIX)[1]["row"]["choice"], "none")  # FIX: not an edit
        self.assertEqual(decisions(run), {"20001": {"decision": "deviation", "comment": edited},
                                          "20003": {"decision": "none"}})
        state = self.http("GET", "/api/state")[1]
        self.assertEqual(state["choices"], {"fix": 0, "deviation": 1, "none": 1})
        self.assertEqual(self.http("GET", "/api/cid/20001")[1]["deviation"]["comment"], edited)
        self.assertEqual(self.decide(run, 20001, "fix")[1]["row"]["comment"], FIX)  # no comment: the result's again

    def test_registered_item_shows_the_comment_that_was_registered(self):
        run = self.run_folder([20001, 20002])
        item(run, 20001, fix=False)
        item(run, 20002, confidence="low")
        edited = "誤検知。src/b.c:2 で長さを確認している。"
        self.assertEqual(self.decide(run, 20001, "deviation", edited)[0], 200)
        held = "保留。確信度が低いため。呼び出し元がリポジトリにない。"  # 20002: the comment of 保留, which nobody edited
        put(run / "written.json", {"written": {"20001": {  # the OK was given: Coverity has the edited comment
            "classification": "False Positive", "action": "Ignore", "severity": "Unspecified", "comment": edited,
            "at": "2026-11-10T16:00:00+09:00"}, "20002": {
            "classification": "Pending", "action": "Undecided", "severity": "Unspecified", "comment": held,
            "at": "2026-11-10T16:00:00+09:00"}}})
        rows = self.rows()
        self.assertEqual([[rows[cid][key] for key in ("done", "choice", "comment", "comment_edited")] for cid in (20001, 20002)],
                         [["written", None, edited, True], ["written", None, held, False]])
        self.assertEqual(self.http("GET", "/api/cid/20001")[1]["deviation"]["comment"], edited)

    def test_decision_that_no_longer_fits_is_ignored(self):
        run = self.run_folder([20001, 20002])
        item(run, 20001)
        item(run, 20002)
        self.decide(run, 20001, "none")
        self.decide(run, 20002, "fix")
        self.assertEqual([self.rows()[cid]["choice_from"] for cid in (20001, 20002)], ["screen", "screen"])
        later = time.time() + 5  # the result was saved, reviewed or merged again after the choice
        os.utime(run / "cid/20001/state.json", (later, later))
        (run / "cid/20002/fix.patch").unlink()
        rows = self.rows()
        self.assertEqual([(rows[cid]["choice"], rows[cid]["choice_from"]) for cid in (20001, 20002)],
                         [("fix", "default"), ("none", "default")])

    def test_access_is_checked(self):
        run = self.run_folder([20001])
        item(run, 20001)
        body, here = {"run": run.name, "cid": 20001, "decision": "none", "comment": None}, f"localhost:{self.port}"
        page = self.http("GET", f"/?t={self.token}", Host=here)
        self.assertEqual((page[0], "<title>Coverity の警告の処置</title>" in page[1]), (200, True))
        for path in ("/app.js", "/style.css"):  # no data in them: no token
            self.assertEqual(self.http("GET", path, X_Token=None)[0], 200)
        self.assertEqual(self.http("POST", "/api/decision", body, Origin=f"http://{here}", Host=here)[0], 200)
        refused = (403, {"ok": False, "error": FORBIDDEN})
        for method, path, headers in (
                ("GET", "/", {}), ("GET", "/?t=wrong", {}), ("GET", f"/?t={self.token}", {"Host": "example.com"}),
                ("GET", "/api/state", {"X_Token": None}), ("GET", "/api/state", {"X_Token": "wrong"}),
                ("GET", "/api/state", {"Host": f"example.com:{self.port}"}),
                ("GET", "/api/cid/20001", {"X_Token": "wrong"}), ("POST", "/api/decision", {"X_Token": "wrong"}),
                ("POST", "/api/decision", {"Origin": None}), ("POST", "/api/decision", {"Origin": "null"}),
                ("POST", "/api/decision", {"Origin": "http://example.com"}),
                ("POST", "/api/decision", {"Host": f"example.com:{self.port}"})):
            sent = body if method == "POST" else None
            self.assertEqual(self.http(method, path, sent, **headers), refused, (path, headers))
        unreadable = (400, {"ok": False, "error": "送信された内容を読み取れません"})
        self.assertEqual(self.http("POST", "/api/decision", body, Content_Type="text/plain"), unreadable)
        for sent in (b"{", b"[]", json.dumps({**body, "pad": "x" * 65536}).encode()):  # the last: over 64 KiB
            self.assertEqual(self.http("POST", "/api/decision", sent), unreadable)
        for method, path in (("GET", "/gui.py"), ("GET", "/static/app.js"), ("GET", "/../gui.py"),
                             ("POST", "/api/state"), ("POST", "/")):
            self.assertEqual(self.http(method, path, body if method == "POST" else None)[0], 404, path)
        self.assertEqual(decisions(run), {"20001": {"decision": "none"}})  # the one request that was allowed


class LifecycleTest(ScreenCase):
    def test_open_shows_the_newest_folder_reuses_the_server_and_stop_ends_it(self):
        self.assertEqual({key: self.opened[key] for key in ("ok", "run_dir", "reused", "browser_opened", "message")},
                         {"ok": True, "run_dir": None, "reused": False, "browser_opened": True,
                          "message": "画面を開きました。ブラウザーが開かないときは、次の URL を開いてください"})
        state = self.http("GET", "/api/state")[1]  # no result folder yet: the conditions only
        self.assertEqual([state[key] for key in ("run", "run_dir", "fetched", "rows")], [None, None, None, []])
        self.assertEqual(state["conditions"]["stream"], "app-main")
        first = self.run_folder([20001])
        self.assertEqual(self.http("GET", "/api/state")[1]["run"], first.name)
        second = self.run_folder([20002, 20003])
        self.run_folder([20009], repo="/another/repository")  # newer, of another repository: not shown
        state = self.http("GET", "/api/state")[1]
        self.assertEqual((state["run"], [row["cid"] for row in state["rows"]]), (second.name, [20002, 20003]))
        self.assertEqual(self.decide(first, 20001, "none")[0], 400)  # the page still shows the older folder

        record = next(self.out.glob("gui-*.json"))
        saved = json.loads(record.read_bytes().decode("utf-8"))
        self.assertEqual((sorted(saved), saved["port"], saved["token"], saved["repo"]),
                         (["pid", "port", "repo", "started_at", "token"], self.port, self.token, self.repo.as_posix()))
        self.assertRegex(record.name, r"^gui-[0-9a-f]{8}\.json$")
        self.assertTrue(record.with_suffix(".log").is_file())
        if os.name != "nt":
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)  # it holds the token

        again = gui("open", "--repo", self.repo, env=self.env)
        self.assertEqual((again["url"], again["reused"], again["run_dir"]),
                         (self.opened["url"], True, second.as_posix()))
        self.assertEqual((self.top / "opened.txt").read_text(encoding="ascii").split(), [self.opened["url"]] * 2)

        self.assertEqual(gui("stop", "--repo", self.repo), {"ok": True, "stopped": True, "message": "画面を終了しました"})
        self.assertFalse(record.exists())
        with self.assertRaises(OSError):
            self.http("GET", "/api/state")
        self.assertEqual(gui("stop", "--repo", self.repo), {"ok": True, "stopped": False, "message": "画面は起動していません"})

    def test_the_server_ends_by_itself_when_no_page_asks_any_more(self):
        script, log = self.top / "serve.py", self.top / "serve.log"
        script.write_text(SHORT, encoding="utf-8")
        with open(log, "wb") as out:  # this server takes the place of the one that setUp opened
            proc = subprocess.Popen([*command()[:-1], str(script), str(GUI.parent), str(self.repo)], env=ENV,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        record, saved, deadline = next(self.out.glob("gui-*.json")), {}, time.monotonic() + 60
        while saved.get("token") in (None, self.token):
            self.assertTrue(proc.poll() is None and time.monotonic() < deadline, log.read_text("utf-8", "replace"))
            time.sleep(0.1)
            try:
                saved = json.loads(record.read_bytes().decode("utf-8"))
            except (OSError, ValueError):  # Windows: the server replaces the file at this moment
                pass
        self.port, self.token = saved["port"], saved["token"]
        for _ in range(6):  # 3 seconds: a page that asks keeps the server running beyond the limit
            self.assertEqual(self.http("GET", "/api/state")[0], 200)
            time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual(proc.wait(timeout=30), 0)  # no page asks any more: the server ends
        again = gui("open", "--repo", self.repo, env=self.env)
        self.assertEqual((again["reused"], again["url"] == self.opened["url"]), (False, False))

    @unittest.skipIf(os.name == "nt", "Windows は、コマンドの出力を引き継がずにブラウザーを開きます")
    def test_browser_gets_no_output_of_the_command(self):
        """A desktop browser prints, and stays open after the command: the reply is one JSON object, at once."""
        browser = self.top / "desktop-browser"
        browser.write_text('#!/bin/sh\necho "Opening in existing browser session."\necho warning >&2\n'
                           'sleep 60 &\necho $! > "$0.pid"\n', encoding="ascii")
        browser.chmod(0o755)
        self.addCleanup(lambda: os.kill(int(Path(f"{browser}.pid").read_text()), signal.SIGTERM))
        proc = subprocess.run([*command(), "open", "--repo", str(self.repo)], capture_output=True,
                              env={**ENV, "BROWSER": str(browser)}, timeout=30)  # no end of output: it fails here
        self.assertEqual((json.loads(proc.stdout.decode("utf-8"))["browser_opened"], proc.stderr), (True, b""))

    def test_open_without_a_settings_file_says_what_to_run(self):
        empty = self.top / "empty"
        empty.mkdir()
        config = (empty / ".coverity-triage" / "config.yaml").as_posix()
        self.assertEqual(gui("open", "--repo", empty, env=self.env), {"ok": False, "error": (
            f"設定ファイルがありません: {config}。/coverity-triage-kit:coverity-setup <URL> <ストリーム> を実行してください")})


class PageTest(ScreenCase):
    def test_the_page_in_a_browser(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js がありません")
        if subprocess.run([node, "-e", "require('playwright')"], capture_output=True, cwd=HERE).returncode != 0:
            self.skipTest("Playwright がありません")
        run = self.run_folder([20001, 20002, 20003], total_matched=None, limited=True)  # more match: not counted
        item(run, 20001)
        item(run, 20002, fix=False)
        edited, shot = "誤検知。src/b.c:2 で長さを確認している。", self.top / "page.png"
        proc = subprocess.run([node, str(HERE / "page.js"), self.opened["url"], str(shot), edited],
                              capture_output=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        seen = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(seen["errors"], [])
        self.assertEqual(seen["table"], [  # both outputs of every item, and the choice
            {"cid": "20001", "fix": FILE, "deviation": FIX, "choice": "fix"},
            {"cid": "20002", "fix": "なし", "deviation": KEEP, "choice": "deviation"}])
        self.assertEqual((seen["diff"], seen["counter"]), (True, f"{len(edited)} / 200 文字"))
        self.assertEqual(seen["foot"], "修正を取り込む 0 件、逸脱コメントを登録する 1 件、保留にする 1 件")
        self.assertEqual(seen["limited"], "条件に合う警告が、設定の上限（3 件）より多くあります。上限まで取得しました")
        self.assertLessEqual(seen["narrow"]["page"], seen["narrow"]["window"])  # no sideways scrolling at 390 px
        self.assertEqual(decisions(run), {"20001": {"decision": "none"},
                                          "20002": {"decision": "deviation", "comment": edited}})
        self.assertGreater(shot.stat().st_size, 10000)


if __name__ == "__main__":
    unittest.main()
