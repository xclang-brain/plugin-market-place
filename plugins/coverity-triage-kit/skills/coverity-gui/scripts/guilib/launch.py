"""Opening, reusing and stopping the screen's server (one per repository).

The server runs detached from the command that opened it. Where it is (address, token, pid) is kept in
``~/.<this skill's folder name>/<hash of the repository>.json`` (only the user can read it; tests set
``GUI_STATE_DIR``). ``open`` reuses a server that answers for the same repository with the same scripts,
and else starts one and waits for its ready file. A server stops when 「サーバを止める」 is pressed,
or when another server has taken its place in the file. Like the original screen it does not stop on idle; an idle stop
is only used when ``GUI_IDLE_SECONDS`` is set to a positive number of seconds (tests).
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .common import SKILL_DIR, GuiError, file_lock, replace, resolve_path, spawn_detached, write_text
from .runs import ITEM_ID, RUN_ID
from .server import GuiServer, code_version
from .settings import output_dir

VIEWS = ("monitor", "summary", "settings")
TOKEN_ENV = "GUI_TOKEN"
STATE_ENV = "GUI_STATE_DIR"
IDLE_ENV = "GUI_IDLE_SECONDS"
IDLE_SECONDS = 0.0  # no idle stop unless GUI_IDLE_SECONDS asks for one
READY_SECONDS = 30
CALL_SECONDS = 3
WATCH_SECONDS = 3


def state_dir() -> Path:
    configured = os.environ.get(STATE_ENV)
    return Path(configured) if configured else Path.home() / f".{SKILL_DIR.name}"


def _key(repo: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(repo)).encode("utf-8")).hexdigest()[:16]


def server_file(repo: Path) -> Path:
    return state_dir() / f"{_key(repo)}.json"


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not isinstance(data.get("port"), int):
        return None
    return data


def _remember(path: Path, record: dict[str, Any]) -> bool:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it holds the token
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


def _forget(path: Path, token: str) -> None:
    found = _read_record(path)
    if found is not None and found["token"] == token:
        with contextlib.suppress(OSError):
            path.unlink()


def call(port: int, token: str, method: str, path: str) -> dict[str, Any] | None:
    """Ask a server of this PC; None when nothing (or something else) answers."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=CALL_SECONDS)
    headers = {"Host": f"127.0.0.1:{port}", "X-Token": token}
    if method == "POST":
        headers.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
    try:
        conn.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
        res = conn.getresponse()
        data = json.loads(res.read().decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    return data if res.status == HTTPStatus.OK and isinstance(data, dict) and data.get("ok") else None


def _running(repo: Path, path: Path) -> dict[str, Any] | None:
    found = _read_record(path)
    if found is None:
        return None
    answer = call(found["port"], found["token"], "GET", "/api/ping")
    if answer is None or answer.get("repo") != str(repo):
        return None
    if answer.get("version") != code_version():
        call(found["port"], found["token"], "POST", "/api/close")  # started by other scripts: replaced
        return None
    return {"url": f"http://127.0.0.1:{found['port']}/?t={found['token']}", "pid": found.get("pid")}


def _repo(repo_root: str) -> Path:
    repo = resolve_path(repo_root)
    if not repo.is_dir():
        raise GuiError(f"フォルダがありません: {repo}（--repo には、調べるリポジトリの一番上のフォルダを渡します）")
    return repo


def fragment(repo: Path, run: str | None, view: str | None, item: str | None) -> tuple[str, str | None]:
    """``#run=<run id>&view=<view>&item=<ID>`` that the page reads, and a note when the run is not listed."""
    if view is not None and view not in VIEWS:
        raise GuiError(f"--view は {' / '.join(VIEWS)} のどれかです（今は {view}）")
    if item is not None and not ITEM_ID.match(item):
        raise GuiError(f"--item は作業の ID（CID か G1 など）です（今は {item}）")
    run_id, note = None, None
    if run:
        folder = Path(run)
        if RUN_ID.match(run) and not folder.exists():
            run_id = run
            output, _ = output_dir(repo)
            if not (output / run_id / "run.json").is_file():
                note = f"実行 {run_id} は、結果を置く場所（{output}）にありません"
        else:
            folder = resolve_path(folder)
            if not (folder / "run.json").is_file():
                raise GuiError(f"--run には実行フォルダ（run.json があるフォルダ）を渡します: {folder}")
            if not RUN_ID.match(folder.name):
                raise GuiError(f"実行フォルダの名前が「年月日-時分秒」の形ではありません: {folder}")
            run_id = folder.name
            output, _ = output_dir(repo)
            if resolve_path(folder.parent) != output:
                note = (f"この実行フォルダは、設定の「結果を置く場所」（{output}）の中にないため、画面の「結果」と"
                        "「監視」には出ません")
    if item and not run_id:
        raise GuiError("--item は --run と一緒に指定します")
    view = view or ("summary" if item else "monitor" if run_id else "settings")
    query = {"run": run_id, "view": view, "item": item if view == "summary" else None}
    return "#" + urlencode({k: v for k, v in query.items() if v}), note


def _start(repo: Path) -> dict[str, Any]:
    """A new server, detached; returns once it is ready."""
    token = secrets.token_urlsafe(24)
    folder = state_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    ready = folder / f"{_key(repo)}.{os.getpid()}.ready.json"
    log_path = folder / f"{_key(repo)}.log"
    with contextlib.suppress(OSError):
        ready.unlink()
    script = SKILL_DIR / "scripts" / "gui.py"
    args = [sys.executable, str(script), "serve", "--repo", str(repo), "--ready-file", str(ready)]
    env = {**os.environ, TOKEN_ENV: token, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        log = log_path.open("wb")
    except OSError:  # Windows: a server that is stopping may still hold the log of its start
        log_path = folder / f"{_key(repo)}.{os.getpid()}.log"
        log = log_path.open("wb")
    with log:
        proc = spawn_detached(args, env, folder, log)
    deadline = time.monotonic() + READY_SECONDS
    try:
        while not ready.is_file():
            if proc.poll() is not None or time.monotonic() > deadline:
                if proc.poll() is None:
                    proc.kill()
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise GuiError("画面のサーバを起動できませんでした", log=str(log_path), log_tail=tail)
            time.sleep(0.05)
        data = json.loads(ready.read_text(encoding="utf-8"))
    finally:
        with contextlib.suppress(OSError):
            ready.unlink()
    return {"url": data["url"], "pid": proc.pid}


def open_screen(repo_root: str, run: str | None = None, view: str | None = None, item: str | None = None,
                browser: bool = True) -> dict[str, Any]:
    repo = _repo(repo_root)
    frag, note = fragment(repo, run, view, item)
    path = server_file(repo)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock: Any = file_lock(path, timeout=READY_SECONDS + 15)  # two at once start only one server
    except OSError:
        lock = contextlib.nullcontext()
    with lock:
        running = _running(repo, path)
        started = running or _start(repo)
    url = started["url"] + frag
    opened = False
    if browser:
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 - no browser here: the URL is still given
            opened = False
    out = {"ok": True, "url": url, "reused": running is not None, "browser_opened": opened, "pid": started.get("pid")}
    if note:
        out["note"] = note
    return out


def stop_screen(repo_root: str) -> dict[str, Any]:
    repo = _repo(repo_root)
    found = _read_record(server_file(repo))
    if found is None:
        return {"ok": True, "stopped": False, "note": "このリポジトリの画面のサーバは動いていません"}
    # a busy PC can make one call time out: try a few times before saying the server is not running
    for _ in range(3):
        if call(found["port"], found["token"], "GET", "/api/ping") is not None:
            break
    else:
        if _pid_alive(found.get("pid")):
            return {"ok": True, "stopped": False, "note": "画面のサーバが返事をしません（動いているかもしれません）。少し待って、もう一度止めてください"}
        return {"ok": True, "stopped": False, "note": "このリポジトリの画面のサーバは動いていません"}
    for _ in range(3):
        if call(found["port"], found["token"], "POST", "/api/close") is not None:
            return {"ok": True, "stopped": True}
        # the answer can be lost while the server is closing: a server that answered a moment ago and now
        # does not is stopped
        if call(found["port"], found["token"], "GET", "/api/ping") is None:
            return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False, "note": "画面のサーバが止める頼みに返事をしません。少し待って、もう一度止めてください"}


def _pid_alive(pid: Any) -> bool:
    """Whether the recorded server process still exists (best effort; False when unknown)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return False  # no cheap check without extra libraries; the note then says it is not running
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _watch(server: GuiServer, path: Path, stopped: threading.Event) -> None:
    """Stop once another server has taken this repository's file: nothing could find this one any more."""
    while not stopped.wait(WATCH_SECONDS):
        found = _read_record(path)
        if found is not None and found["token"] != server.token:
            server.close_later("replaced")
            return


def serve(repo_root: str, ready_file: str | None, port: int = 0) -> dict[str, Any]:
    """Serve the screen (the detached process ``open`` starts). The token comes in ``GUI_TOKEN``."""
    repo = _repo(repo_root)
    token = os.environ.pop(TOKEN_ENV, "") or secrets.token_urlsafe(24)
    try:
        idle = float(os.environ.get(IDLE_ENV) or IDLE_SECONDS)
    except ValueError:
        idle = IDLE_SECONDS
    server = GuiServer(repo, token, port, idle_seconds=max(1.0, idle) if idle > 0 else 0.0)
    path = server_file(repo)
    record = {"url": server.url, "pid": os.getpid(), "port": server.port, "token": token, "repo": str(repo),
              "version": server.version}
    remembered = _remember(path, record)
    stopped = threading.Event()
    try:
        if ready_file:
            write_text(Path(ready_file), json.dumps({"url": server.url, "pid": os.getpid(), "port": server.port}))
        if server.idle_seconds > 0:
            threading.Thread(target=server.watch_idle, args=(stopped,), daemon=True).start()
        if remembered:
            threading.Thread(target=_watch, args=(server, path, stopped), daemon=True).start()
        server.serve_forever(poll_interval=0.2)
    finally:
        stopped.set()
        server.server_close()
        _forget(path, token)
    return {"ok": True, "url": server.url, "closed_by": server.closed_by}
