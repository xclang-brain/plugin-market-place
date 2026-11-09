#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web管理画面のサーバー。dashboard.py start が、バックグラウンドで起動する。

使い方：server.py --workdir 作業フォルダ
127.0.0.1 の、OS が選ぶポートで待ち受ける（REQ-402）。起動ごとにトークンを作る。
.<このスキルのフォルダの名前>/server.json に URL とトークンを、recording に記録中の目印を書く。
recording は 30 秒ごとに書き直し、終わるときに消す。
"""

import argparse
import hmac
import http.server
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.basename(os.path.dirname(SCRIPTS))
STATIC_DIR = os.path.join(SCRIPTS, "static")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
REFRESH_SECONDS = 30   # recording を書き直す間隔。フックは 2 分より古いものを、ないものとして扱う
MAX_BODY = 64 * 1024   # POST の本文の上限。画面の設定より大きいものは来ない
TOOLS_SHOWN = 300      # 画面に返すツールの呼び出しの数（新しいほうから）
MAX_TEXT = 4000        # 設定の各欄の上限の文字数
MAX_TRIALS = 20
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VERIFIER = "debug-verifier"
DEFAULT_SETTINGS = {"goal": "", "criteria": "", "max_trials": 5}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_private(path, obj):
    """本人だけが読み書きできるファイルに、置き換えで書く（読む側が書きかけを見ないように）"""
    data = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:  # Windows で、ほかのプロセスが開いているとき
        os.remove(tmp)
        with open(path, "wb") as f:
            f.write(data)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def summarize(events):
    """記録から、今の段・動いているエージェント・最後の報告と判定を求める（設計書 8.6）"""
    running = {}
    result = {"title": "", "started": None, "updated": None, "count": len(events),
              "last_report": None, "last_verdict": None}
    for e in events:
        kind, agent = e.get("kind"), e.get("agent") or ""
        result["started"] = result["started"] or e.get("time")
        result["updated"] = e.get("time") or result["updated"]
        if kind == "prompt":
            running.clear()
            if not result["title"]:
                result["title"] = (e.get("text") or "").strip().split("\n")[0][:60]
        elif kind == "request" and not e.get("denied"):
            running[agent] = running.get(agent, 0) + 1
        elif kind == "report" and e.get("sub"):
            if running.get(agent, 0) > 1:
                running[agent] -= 1
            else:
                running.pop(agent, None)
            if agent.endswith(VERIFIER):
                result["last_verdict"] = e.get("text")
        elif kind == "report":
            running.clear()
            result["last_report"] = e.get("text")
        elif kind == "tool" and e.get("sub") and agent not in running:
            running[agent] = 1
    names = sorted(running)
    result["running"] = names
    result["stage"] = ("review" if any(n.endswith(VERIFIER) for n in names) else "work" if names else "plan")
    return result


class Store:
    """作業フォルダの .debug-dashboard/ を読み書きする"""

    def __init__(self, workdir, base):
        self.workdir, self.base = workdir, base
        self.cache = {}
        self.lock = threading.Lock()

    def sessions_dir(self):
        path = os.path.join(self.base, "sessions")
        return None if os.path.islink(self.base) or os.path.islink(path) else path

    def session_path(self, session):
        sessions = self.sessions_dir()
        if not sessions or not SESSION_RE.match(session or ""):
            return None
        path = os.path.join(sessions, session)
        return path if os.path.isdir(path) and not os.path.islink(path) else None

    def events(self, session):
        folder = self.session_path(session)
        path = os.path.join(folder, "events.jsonl") if folder else None
        if not path or os.path.islink(path) or not os.path.isfile(path):
            return None
        stat = os.stat(path)
        key = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            hit = self.cache.get(path)
            if hit and hit[0] == key:
                return hit[1]
        events = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
        with self.lock:
            self.cache[path] = (key, events)
        return events

    def summary(self, session, events):
        result = summarize(events)
        folder = self.session_path(session)
        result.update(id=session, stopped=bool(folder) and os.path.lexists(os.path.join(folder, "stop")))
        return result

    def state(self):
        sessions = []
        folder = self.sessions_dir()
        names = os.listdir(folder) if folder and os.path.isdir(folder) else []
        for name in names:
            events = self.events(name)
            if events is not None:
                item = self.summary(name, events)
                for key in ("last_report", "last_verdict"):
                    item.pop(key)
                sessions.append(item)
        sessions.sort(key=lambda s: s["updated"] or "", reverse=True)
        return {"ok": True, "workdir": self.workdir, "settings": self.settings(), "sessions": sessions}

    def session(self, session):
        events = self.events(session)
        if events is None:
            return None
        tools = [i for i, e in enumerate(events) if e.get("kind") == "tool"]
        hidden = set(tools[:-TOOLS_SHOWN]) if len(tools) > TOOLS_SHOWN else set()
        shown = [e for i, e in enumerate(events) if i not in hidden]
        return {"ok": True, "session": self.summary(session, events), "events": shown}

    def stop(self, session):
        folder = self.session_path(session)
        if not folder:
            return False
        path = os.path.join(folder, "stop")
        if not os.path.lexists(path):
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
        return True

    def settings(self):
        saved = read_json(os.path.join(self.base, "settings.json"))
        return dict(DEFAULT_SETTINGS, **saved) if isinstance(saved, dict) else dict(DEFAULT_SETTINGS)

    def save_settings(self, body):
        goal, criteria, trials = body.get("goal"), body.get("criteria"), body.get("max_trials")
        if not (isinstance(goal, str) and isinstance(criteria, str) and len(goal) <= MAX_TEXT
                and len(criteria) <= MAX_TEXT):
            return "直したいことと合格の条件は、%d 字までの文で送ってください。" % MAX_TEXT
        if not (isinstance(trials, int) and not isinstance(trials, bool) and 1 <= trials <= MAX_TRIALS):
            return "試行の上限は、1 から %d までの数にしてください。" % MAX_TRIALS
        write_private(os.path.join(self.base, "settings.json"),
                      {"goal": goal, "criteria": criteria, "max_trials": trials})
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "debug-dashboard"
    sys_version = ""

    def end_headers(self):  # エラーの応答も含め、すべての応答に付ける
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def log_request(self, code="-", size="-"):  # 画面は 2 秒ごとに問い合わせるので、失敗だけを残す
        if str(code).isdigit() and int(code) >= 400:
            super().log_request(code, size)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def reply(self, status, obj):
        body = json.dumps(obj, ensure_ascii=True).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.reply(status, {"ok": False, "message": message})

    def own_origins(self):
        port = self.server.server_address[1]
        return ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def route(self, method):
        path = urlsplit(self.path).path
        host = self.headers.get("Host", "")
        if host not in self.own_origins():
            return self.fail(403, "Host が違います。画面は 127.0.0.1 で開いてください。")
        if method == "GET" and path in STATIC:
            return self.static(*STATIC[path])
        if not path.startswith("/api/"):
            return self.fail(404, "ありません。")
        token = self.headers.get("X-Dashboard-Token", "").encode("utf-8", "replace")
        if not hmac.compare_digest(token, self.server.token.encode("ascii")):
            return self.fail(403, "トークンが違います。スキルが返した URL を開き直してください。")
        if method == "GET":
            return self.get_api(path)
        if self.headers.get("Origin") != "http://" + host:
            return self.fail(403, "ほかのページからの送信は受け付けません。")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.fail(415, "JSON で送ってください。")
        body = self.read_body()
        if body is None:
            return self.fail(400, "送られた JSON を読めません。")
        return self.post_api(path, body)

    def static(self, name, ctype):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 < length <= MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def get_api(self, path):
        store = self.server.store
        if path == "/api/state":
            return self.reply(200, store.state())
        if path.startswith("/api/sessions/"):
            found = store.session(unquote(path[len("/api/sessions/"):]))
            return self.reply(200, found) if found else self.fail(404, "その実行の記録はありません。")
        return self.fail(404, "ありません。")

    def post_api(self, path, body):
        store = self.server.store
        if path == "/api/stop":
            if not store.stop(body.get("session")):
                return self.fail(404, "その実行の記録はありません。")
            return self.reply(200, {"ok": True, "message": "止めました。次のツールの呼び出しから断ります。"})
        if path == "/api/settings":
            problem = store.save_settings(body)
            return self.fail(400, problem) if problem else self.reply(200, {"ok": True})
        if path == "/api/shutdown":
            self.reply(200, {"ok": True, "message": "終了します。"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self.fail(404, "ありません。")


def keep_recording(path, info, done):
    """recording の時刻を書き直し続ける"""
    while not done.wait(REFRESH_SECONDS):
        if os.path.islink(os.path.dirname(path)) or os.path.islink(path):
            return
        try:
            write_private(path, dict(info, time=time.time()))
        except OSError as e:
            sys.stderr.write("%s recording を書き直せません：%s\n" % (now_iso(), e))


def remove_own(path, pid):
    """自分が書いた目印だけを消す（後から起動したサーバーのものは残す）"""
    info = read_json(path)
    if isinstance(info, dict) and info.get("pid") == pid and not os.path.islink(path):
        os.remove(path)


def main(argv=None):
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Web管理画面のサーバー")
    parser.add_argument("--workdir", required=True)
    workdir = os.path.abspath(parser.parse_args(argv).workdir)
    base = os.path.join(workdir, "." + SKILL)
    if os.path.islink(base) or not os.path.isdir(base):
        sys.stderr.write("%s がないか、シンボリックリンクです。\n" % base)
        return 2
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.token = secrets.token_urlsafe(32)
    httpd.store = Store(workdir, base)
    pid, port = os.getpid(), httpd.server_address[1]
    url = "http://127.0.0.1:%d/#k=%s" % (port, httpd.token)
    recording, server_json = os.path.join(base, "recording"), os.path.join(base, "server.json")
    info = {"pid": pid, "port": port}
    write_private(recording, dict(info, time=time.time()))
    write_private(server_json, dict(info, url=url, token=httpd.token, started=now_iso()))
    done = threading.Event()
    threading.Thread(target=keep_recording, args=(recording, info, done), daemon=True).start()
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    sys.stderr.write("%s 起動しました。ポート %d\n" % (now_iso(), port))
    sys.stderr.flush()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        done.set()
        for path in (recording, server_json):
            remove_own(path, pid)
        httpd.server_close()
        sys.stderr.write("%s 終了しました。\n" % now_iso())
    return 0


if __name__ == "__main__":
    sys.exit(main())
