# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""The screen of a triage of Coverity warnings: a server on 127.0.0.1 and one page.

    uv run --native-tls --quiet gui.py open --repo <repo>    start the server (or reuse it), open the page
    uv run --native-tls --quiet gui.py stop --repo <repo>    stop the server
    gui.py serve --repo <repo>                               the detached server that open starts

The page shows the newest result folder of the repository and lets the person choose, per CID, between
the fix code and the deviation comment. The server starts no process. It writes decisions.json in the
result folder, and its record and log (gui-<key>.json, gui-<key>.log) beside the result folders.
open and stop print one JSON object (UTF-8); ``"ok": false`` comes with a Japanese ``error``.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

SCRIPT = Path(__file__).resolve()
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css")}
JSON = "application/json"
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
MAX_BODY = 64 * 1024
CHOICES = ("fix", "deviation", "none")
STARTS = ("誤検知。", "意図的。", "本物のバグ。")  # the start of a deviation comment fixes its Classification
COMMENT_MAX = 200  # characters
FORBIDDEN = "この URL では画面を表示できません。チャットで「画面を開いて」と入力し、表示された URL を開いてください"


class Problem(Exception):
    """Something the person can correct: {"ok": false, "error": ...}; over HTTP with this status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise Problem(f"引数が正しくありません（{message}）")


def load(path: Path) -> dict:
    """The object of a JSON file (UTF-8, a BOM is allowed); {} when it is missing or is not one."""
    for _ in range(20):
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except PermissionError:  # Windows: the file is being replaced at this moment
            time.sleep(0.05)
        except (OSError, ValueError):
            break
    return {}


def write_json(path: Path, data: dict, mode: int = 0o666) -> None:
    """Write through a temporary file and a rename (Windows refuses the rename while the file is read)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(tmp, flags, mode), "wb") as out:
        out.write((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    for attempt in range(60):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(0.05)


def settings(repo: Path) -> dict:
    """What the screen uses of <repo>/.coverity-triage/config.yaml: where the result folders are, and
    the conditions to show (with their defaults)."""
    path = repo / ".coverity-triage" / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        data = data if isinstance(data, dict) else {}
        asked = data.get("conditions") or {}

        def items(key: str, default: list) -> list:
            value = asked.get(key) or default
            return value if isinstance(value, list) else [value]

        return {"output_dir": (repo / str(data.get("output_dir") or "../coverity-triage-out")).resolve(),
                "conditions": {"stream": str((data.get("coverity") or {}).get("stream") or ""),
                               "checkers": items("checkers", []), "impacts": items("impacts", []),
                               "classification": items("classification", ["Unclassified"]),
                               "limit": int(asked.get("limit") or 20)}}
    except FileNotFoundError:
        raise Problem(f"設定ファイルがありません: {path.as_posix()}。"
                      "/coverity-triage-kit:coverity-setup <URL> <ストリーム> を実行してください") from None
    except (yaml.YAMLError, UnicodeDecodeError, AttributeError, TypeError, ValueError) as exc:
        raise Problem(f"設定ファイルを読み取れません: {path.as_posix()}（{exc}）") from None


# ---- the result folder ----------------------------------------------------------------------


def status(state: dict) -> str:
    """pending: nobody works on the CID (it has no state.json, or it was sent back)."""
    return state.get("status") if state.get("status") in ("working", "done", "failed") else "pending"


class Run:
    """One reading of the newest result folder of the repository (``dir`` is None: there is none yet)."""

    def __init__(self, repo: Path):
        cfg = settings(repo)
        self.conditions, out = cfg["conditions"], cfg["output_dir"]
        runs = sorted(p for p in (out.iterdir() if out.is_dir() else [])
                      if load(p / "run.json").get("repo") == repo.as_posix())
        self.dir = runs[-1] if runs else None
        read = (lambda name: load(self.dir / name)) if runs else (lambda name: {})
        self.fetch = read("issues.json")
        self.issues = {i["cid"]: i for i in self.fetch.get("issues") or []
                       if isinstance(i, dict) and type(i.get("cid")) is int}
        self.states = {cid: read(f"cid/{cid}/state.json") for cid in self.issues}
        self.review = read("run.json").get("review") is True
        built = read("build.json")
        self.built = (built.get("cids") or {}) if built.get("status") == "done" else None
        self.applied = read("applied.json").get("fixes") or {}
        self.written = read("written.json").get("written") or {}
        self.decisions = read("decisions.json").get("decisions") or {}

    def file(self, cid: int, name: str) -> Path:
        return self.dir / "cid" / str(cid) / name

    def counts(self) -> dict:
        found = {"total": len(self.states), "pending": 0, "working": 0, "done": 0, "failed": 0}
        for state in self.states.values():
            found[status(state)] += 1
        return {**found, "remaining": found["pending"] + found["working"]}

    def chosen(self, cid: int, can_fix: bool) -> dict:
        """The choice made on the screen, while it still fits the item: a fix needs fix.patch, and
        state.json must not have been written after the choice (a new save, review or merge)."""
        found = self.decisions.get(str(cid))
        try:
            at = datetime.fromisoformat(found["at"]).timestamp()
            fits = (found["decision"] in CHOICES and (can_fix or found["decision"] != "fix")
                    and int(self.file(cid, "state.json").stat().st_mtime) <= at)
        except (KeyError, TypeError, ValueError, OSError):
            fits = False
        return found if fits else {}

    def row(self, cid: int) -> dict:
        """The table row of a CID. Only an item (finished, readable, not merged into another CID) has
        the two outputs; only an item that is not taken in or registered yet has a choice."""
        issue, state = self.issues[cid], self.states[cid]
        row = {"cid": cid, "members": [], "checker": issue.get("checker"),
               "place": f"{issue.get('file', '')}:{issue.get('line', '')}", "status": status(state),
               "verdict": None, "recommendation": None, "confidence": None, "review": None, "build": None,
               "files": [], "comment": None, "choice": None, "choice_from": None, "reason": "", "can_fix": False,
               "comment_edited": False, "done": None,
               "error": str(state.get("error") or "") if status(state) == "failed" else ""}
        if row["status"] != "done":
            return row
        result = load(self.file(cid, "result.json"))
        deviation = result.get("deviation")
        if not isinstance(deviation, dict) or not isinstance(deviation.get("comment"), str):
            return {**row, "status": "unreadable"}
        members, can_fix = state.get("members") or [], self.file(cid, "fix.patch").is_file()
        review, build, advice = state.get("review"), (self.built or {}).get(str(cid)), result.get("recommendation")
        row.update(members=members, verdict=result.get("verdict"), recommendation=advice,
                   confidence=state.get("confidence"), review=review, build=build,
                   files=(state.get("files") or []) if can_fix else [], comment=deviation["comment"], can_fix=can_fix)
        if str(cid) in self.applied:
            return {**row, "done": "applied"}
        if all(str(c) in self.written for c in [cid, *members]):  # shown: the comment that was registered
            entry = self.written[str(cid)]
            said = (entry.get("comment") if isinstance(entry, dict) else None) or row["comment"]
            return {**row, "done": "written", "comment": said, "comment_edited": said != row["comment"]}
        chosen = self.chosen(cid, can_fix)
        if chosen:
            return {**row, "choice": chosen["decision"], "choice_from": "screen",
                    "comment": chosen.get("comment") or row["comment"], "comment_edited": bool(chosen.get("comment"))}
        # The default choice: the same rule as in the skill that takes the fixes in. No default where a
        # person has to look first; the first reason that applies is shown.
        reasons = {"確信度が低いため": state.get("confidence") == "low", "検証で要確認のため": review == "flag",
                   "未検証のため": review == "unverified" or (self.review and not review),
                   "ビルドに失敗したため": build == "failed"}
        reason = next((sentence for sentence, applies in reasons.items() if applies), "")
        choice = advice if not reason and (advice == "deviation" or (advice == "fix" and can_fix)) else "none"
        return {**row, "choice": choice, "choice_from": "default", "reason": reason}

    def rows(self) -> list[dict]:
        return [self.row(cid) for cid, state in self.states.items()
                if not (state.get("status") == "done" and state.get("merged_into"))]


def overview(repo: Path) -> dict:
    run = Run(repo)
    rows, fetch = run.rows(), run.fetch
    fetched = {"at": fetch.get("fetched_at"), "count": len(run.issues), "total_matched": fetch.get("total_matched"),
               "limited": fetch.get("limited") is True} if isinstance(fetch.get("issues"), list) else None
    return {"ok": True, "repo": repo.as_posix(), "run": run.dir.name if run.dir else None,
            "run_dir": run.dir.as_posix() if run.dir else None, "conditions": run.conditions, "fetched": fetched,
            "counts": run.counts(), "review": run.review, "build": run.built is not None,
            "working": [{"cid": cid, "claimed_at": s.get("claimed_at")} for cid, s in run.states.items()
                        if s.get("status") == "working"],
            "rows": rows, "choices": {choice: sum(row["choice"] == choice for row in rows) for choice in CHOICES}}


def own_row(run: Run, cid, refusal: int) -> dict:
    """The row of a CID of the list that is not merged into another one; else Problem with this HTTP status."""
    if type(cid) is not int or cid not in run.issues:
        raise Problem("この CID は警告の一覧にありません", refusal)
    if run.states[cid].get("merged_into"):
        raise Problem(f"CID {cid} は CID {run.states[cid]['merged_into']} に統合されています", refusal)
    return run.row(cid)


def detail(repo: Path, cid: int) -> dict:
    run = Run(repo)
    row = own_row(run, cid, 404)
    out = {"ok": True, "cid": cid, "members": row["members"], "status": row["status"],
           "issue": {key: run.issues[cid].get(key) for key in ("checker", "file", "line", "function", "impact")},
           "result": None, "patch": None, "review": None, "build": row["build"], "deviation": None,
           **{key: row[key] for key in ("choice", "choice_from", "can_fix", "done")}}
    if row["status"] != "done":
        return out
    result = load(run.file(cid, "result.json"))
    out["result"] = {"verdict": row["verdict"], "summary": result.get("summary"),
                     "evidence": [{key: e.get(key) for key in ("file", "line", "note")}
                                  for e in result.get("evidence") or [] if isinstance(e, dict)],
                     "recommendation": row["recommendation"], "confidence": row["confidence"],
                     "fix_summary": (result.get("fix") or {}).get("summary"),
                     "no_fix_reason": result.get("no_fix_reason")}
    if row["can_fix"]:
        data = run.file(cid, "fix.patch").read_bytes()
        try:
            out["patch"] = data.decode("utf-8")
        except UnicodeDecodeError:
            out["patch"] = data.decode("cp932", "replace")
    if row["review"]:
        out["review"] = {"outcome": row["review"], "points": load(run.file(cid, "review.json")).get("points") or []}
    out["deviation"] = {"comment": row["comment"], "original_comment": result["deviation"]["comment"],
                        **{key: result["deviation"].get(key) for key in ("classification", "action", "severity")}}
    return out


def decide(repo: Path, body: dict) -> dict:
    """Store what the person chose for one item in decisions.json."""
    run = Run(repo)
    if not run.dir or body.get("run") != run.dir.name:
        raise Problem("結果フォルダーが新しくなっています。画面を読み込み直してください")
    cid, choice, comment = body.get("cid"), body.get("decision"), body.get("comment")
    row = own_row(run, cid, 400)
    if row["status"] != "done":
        raise Problem("この CID は読み取れません" if row["status"] == "unreadable" else f"CID {cid} は処置済みではありません")
    if row["done"]:
        raise Problem("この CID は取り込み済みです" if row["done"] == "applied" else "この CID は登録済みです")
    if choice not in CHOICES:
        raise Problem("選択が正しくありません。fix、deviation、none のいずれかを指定してください")
    if choice == "fix" and not row["can_fix"]:
        raise Problem("この CID には修正コードがないため、「修正を取り込む」は選択できません")
    entry = {"decision": choice}
    original = load(run.file(cid, "result.json"))["deviation"]["comment"]
    comment = comment.strip() if isinstance(comment, str) else comment
    if comment is not None and comment != original:
        start = next((s for s in STARTS if original.startswith(s)), "")
        if not isinstance(comment, str) or not comment:
            raise Problem("逸脱コメントを入力してください")
        if "\n" in comment or "\r" in comment:
            raise Problem("逸脱コメントは、改行を入れずに1行で入力してください")
        if len(comment) > COMMENT_MAX:
            raise Problem(f"逸脱コメントは {COMMENT_MAX} 文字以内で入力してください（現在 {len(comment)} 文字）")
        if not comment.startswith(start):
            raise Problem(f"逸脱コメントは、元のコメントと同じ「{start}」で始めてください")
        entry["comment"] = comment
    entry["at"] = datetime.now().astimezone().isoformat()
    write_json(run.dir / "decisions.json", {"decisions": {**run.decisions, str(cid): entry}})
    return {"ok": True, "row": Run(repo).row(cid)}


# ---- the server -----------------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    daemon_threads, allow_reuse_address = True, False  # Windows: no other process binds the same port


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "coverity-gui", ""

    def log_message(self, *args) -> None:
        pass  # the address of the page holds the token: requests stay out of the log

    def do_GET(self) -> None:  # noqa: N802
        self.answer(b"")

    def do_POST(self) -> None:  # noqa: N802
        length = self.headers.get("Content-Length") or ""
        # Read before any refusal: an answer that is followed by a close with unread data can be lost.
        self.answer(self.rfile.read(min(int(length), 16 * MAX_BODY) if length.isdecimal() else 0))

    def answer(self, raw: bytes) -> None:
        try:
            status, (body, kind) = 200, self.route(raw)
        except Problem as exc:
            status, body, kind = exc.status, {"ok": False, "error": str(exc)}, JSON
        except Exception as exc:  # noqa: BLE001 - the page shows it, the log has the place, the server goes on
            traceback.print_exc()
            status, kind = 500, JSON
            body = {"ok": False, "error": f"予期しないエラーが発生しました: {type(exc).__name__}: {exc}"}
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for name, value in (("Content-Type", f"{kind}; charset=utf-8"), ("Content-Length", str(len(data))),
                            ("Cache-Control", "no-store"), ("Content-Security-Policy", CSP)):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def route(self, raw: bytes) -> tuple:
        server, url = self.server, urlsplit(self.path)
        hosts = [f"{name}:{server.server_port}" for name in ("127.0.0.1", "localhost")]

        def token(value) -> bool:
            return isinstance(value, str) and secrets.compare_digest(value.encode(), server.token.encode())

        if self.headers.get("Host") not in hosts:
            raise Problem(FORBIDDEN, 403)
        if self.command == "GET" and url.path in STATIC:  # the page: fixed names, no data
            if url.path == "/" and not token(parse_qs(url.query).get("t", [""])[0]):
                raise Problem(FORBIDDEN, 403)
            name, kind = STATIC[url.path]
            return (SCRIPT.parent / "static" / name).read_bytes(), kind
        if not url.path.startswith("/api/"):
            raise Problem("ページがありません", 404)
        if not token(self.headers.get("X-Token")):
            raise Problem(FORBIDDEN, 403)
        cid = re.fullmatch(r"/api/cid/(\d{1,18})", url.path)
        if self.command == "GET" and url.path == "/api/state":
            return overview(server.repo), JSON
        if self.command == "GET" and cid:
            return detail(server.repo, int(cid[1])), JSON
        if self.command == "POST" and url.path == "/api/decision":
            if self.headers.get("Origin") not in [f"http://{host}" for host in hosts]:
                raise Problem(FORBIDDEN, 403)
            try:
                kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                body = json.loads(raw.decode("utf-8")) if kind == JSON and len(raw) <= MAX_BODY else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise Problem("送信された内容を読み取れません")
            with server.lock:  # decisions.json has one writer at a time
                return decide(server.repo, body), JSON
        raise Problem("ページがありません", 404)


def record_file(repo: Path) -> Path:
    key = hashlib.sha256(repo.as_posix().encode("utf-8")).hexdigest()[:8]
    return settings(repo)["output_dir"] / f"gui-{key}.json"


def alive(record: dict) -> bool:
    """Whether the server of this record answers (nothing else knows its token)."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(record["port"]), timeout=3)  # never through a proxy
        try:
            conn.request("GET", f"/?t={record['token']}")
            answer = conn.getresponse()
            return answer.status == 200 and (answer.getheader("Server") or "").strip() == Handler.server_version
        finally:
            conn.close()
    except (KeyError, TypeError, ValueError, OSError, http.client.HTTPException):
        return False


def cmd_serve(repo: Path) -> dict:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    record = record_file(repo)
    server = Server(("127.0.0.1", 0), Handler)
    server.repo, server.token, server.lock = repo, secrets.token_urlsafe(24), threading.Lock()
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json(record, {"pid": os.getpid(), "port": server.server_port, "token": server.token,
                        "repo": repo.as_posix(), "started_at": started}, mode=0o600)

    def watch() -> None:  # the record is what keeps the server running: stop removes it, a newer server replaces it
        while load(record).get("token") == server.token:
            time.sleep(0.5)
        server.shutdown()

    threading.Thread(target=watch, daemon=True).start()
    print(f"{started} 画面のサーバーを開始しました（ポート {server.server_port}）", flush=True)
    server.serve_forever()
    return {"ok": True, "message": "画面のサーバーを終了しました"}  # into the log, like the line above


def show(url: str) -> bool:
    """Open the page in the default browser, from a process without the output of this command: what a
    browser prints would stand beside the one JSON object, and a caller that reads the output to its end
    would wait for the browser."""
    code = "import sys, webbrowser; sys.exit(not webbrowser.open(sys.argv[1]))"
    nothing = dict.fromkeys(("stdin", "stdout", "stderr"), subprocess.DEVNULL)
    try:
        return subprocess.run([sys.executable, "-c", code, url], **nothing,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
    except OSError:  # no browser is opened: the URL is still given
        return False


def cmd_open(repo: Path) -> dict:
    record = record_file(repo)
    found = load(record)
    reused = alive(found)
    if not reused:
        record.parent.mkdir(parents=True, exist_ok=True)
        log = record.with_suffix(".log")
        with open(log, "ab") as out:
            common = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT,
                      "cwd": str(record.parent), "close_fds": True}
            command = [sys.executable, str(SCRIPT), "serve", "--repo", str(repo)]
            if os.name != "nt":
                proc = subprocess.Popen(command, start_new_session=True, **common)
            else:  # no window, and outside the terminal's job object: closing the terminal does not end it
                flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                try:
                    proc = subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                                            **common)
                except OSError:
                    proc = subprocess.Popen(command, creationflags=flags, **common)
        deadline = time.monotonic() + 30
        while not alive(found := load(record)):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise Problem(f"画面を起動できませんでした。ログを確認してください: {log.as_posix()}")
            time.sleep(0.1)
    url = f"http://127.0.0.1:{found['port']}/?t={found['token']}"
    run_dir = Run(repo).dir
    return {"ok": True, "url": url, "run_dir": run_dir.as_posix() if run_dir else None, "reused": reused,
            "browser_opened": show(url), "message": "画面を開きました。ブラウザーが開かないときは、次の URL を開いてください"}


def cmd_stop(repo: Path) -> dict:
    record = record_file(repo)
    found = load(record)
    running = alive(found)
    deadline = time.monotonic() + 15
    while record.exists() or alive(found):  # the server ends once its record is gone
        try:
            record.unlink(missing_ok=True)
        except PermissionError:  # Windows: the server reads it at this moment
            pass
        if time.monotonic() > deadline:
            raise Problem("画面を終了できませんでした。少し待ってから、もう一度「画面を閉じて」と入力してください")
        time.sleep(0.1)
    return {"ok": True, "stopped": running, "message": "画面を終了しました" if running else "画面は起動していません"}


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="gui.py")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("open", "stop", "serve"):
        sub.add_parser(name).add_argument("--repo", required=True)
    code = 0
    try:
        args = parser.parse_args(argv)
        out = {"open": cmd_open, "stop": cmd_stop, "serve": cmd_serve}[args.command](Path(args.repo).resolve())
    except Problem as exc:
        out = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        out, code = {"ok": False, "error": f"予期しないエラーが発生しました: {type(exc).__name__}: {exc}"}, 1
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(main())
