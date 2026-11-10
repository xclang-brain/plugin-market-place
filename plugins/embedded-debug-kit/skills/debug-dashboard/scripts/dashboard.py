#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web管理画面を起動・終了し、結果を JSON 1つで標準出力に返す。

使い方：dashboard.py start|stop|status [--workdir 作業フォルダ]
  start   画面のサーバーを起動して URL を返す。もう動いていれば、その URL を返す
  stop    画面のサーバーを終了する。記録も「実行を止める」も止まる
  status  動いているかを返す（何も作らない）
作業フォルダの既定は、CLI が渡す CLAUDE_PROJECT_DIR（なければ COPILOT_PROJECT_DIR、今のフォルダ）。
Copilot CLI から動かしたときは、今のフォルダが Git のリポジトリの中なら、そのいちばん上のフォルダ（フックと同じ）。
終了コード：0＝成功、1＝起動や終了の失敗、2＝引数や作業フォルダの問題。
"""

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
DATA = ".debug-dashboard"  # フックの設定（hooks.json）も、この名前を使う
IS_WINDOWS = os.name == "nt"
FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
START_SECONDS = 10   # サーバーの起動を待つ秒数。ふつうは 1 秒もかからない
STOP_SECONDS = 10    # 終了を待つ秒数
PING_SECONDS = 2     # 問い合わせの応答を待つ秒数
CONNECT_SECONDS = 5  # Windows では、待ち受けていないポートに断られるまで 1〜2 秒かかる
BUSY = "busy"


class Paths:
    def __init__(self, workdir):
        self.workdir = workdir
        self.base = os.path.join(workdir, DATA)
        self.server = os.path.join(self.base, "server.json")
        self.recording = os.path.join(self.base, "recording")
        self.gitignore = os.path.join(self.base, ".gitignore")
        self.sessions = os.path.join(self.base, "sessions")
        self.logs = os.path.join(self.base, "logs")
        self.log = os.path.join(self.logs, "server.log")


def output(code, ok, message, paths=None, url=None, running=False, problems=()):
    result = {"ok": ok, "message": message, "problems": list(problems), "config": None,
              "log": paths.log if paths and os.path.isfile(paths.log) else None,
              "url": url, "running": running}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, indent=2) + "\n")
    return code


def busy(paths, info):
    return output(1, False, "Web管理画面のサーバー（プロセス番号 %s）が応答しません。PC が混んでいるかもしれません。"
                  "しばらく待ってから、もう一度動かしてください。続くときは、そのプロセスを止めてください。"
                  % info.get("pid"), paths, running=True)


def read_info(paths):
    if os.path.islink(paths.server):
        return None
    try:
        with open(paths.server, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and isinstance(info.get("port"), int) else None


def call(info, method, path, timeout=PING_SECONDS):
    """サーバーに要求を送り、(状態コード, 本文) を返す。待ち受けていなければ None、応答がなければ BUSY"""
    port = info["port"]
    headers = {"X-Dashboard-Token": str(info.get("token", ""))}
    body = None
    if method == "POST":
        headers.update({"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port})
        body = b"{}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=CONNECT_SECONDS)
    try:
        try:
            conn.connect()
        except OSError:
            return None
        conn.sock.settimeout(timeout)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    except socket.timeout:
        return BUSY
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def probe(paths, info):
    """server.json のサーバーが、この作業フォルダの画面として動いていれば True。応答がなければ BUSY"""
    reply = call(info, "GET", "/api/state")
    if reply is None or reply == BUSY:
        return reply or False
    try:
        state = json.loads(reply[1].decode("utf-8"))
    except ValueError:
        return False
    return (reply[0] == 200 and isinstance(state, dict) and state.get("ok") is True
            and os.path.normcase(str(state.get("workdir"))) == os.path.normcase(paths.workdir))


def clear(paths):
    """前のサーバーの記録中フラグと、停止フラグを消す。シンボリックリンクは触らない"""
    targets = [paths.server, paths.recording]
    if os.path.isdir(paths.sessions):
        for name in os.listdir(paths.sessions):
            folder = os.path.join(paths.sessions, name)
            if not os.path.islink(folder):
                targets.append(os.path.join(folder, "stop"))
    for path in targets:
        if os.path.isfile(path) and not os.path.islink(path):
            os.remove(path)


def prepare(paths):
    """記録のフォルダを本人だけが使える権限で作り、何よりも先に .gitignore を置く"""
    if not os.path.isdir(paths.base):
        os.mkdir(paths.base, 0o700)
    elif not IS_WINDOWS:
        os.chmod(paths.base, 0o700)
    if not os.path.lexists(paths.gitignore):
        fd = os.open(paths.gitignore, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FLAGS, 0o600)
        try:
            os.write(fd, b"*\n")
        finally:
            os.close(fd)
    if not os.path.isdir(paths.logs):
        os.mkdir(paths.logs, 0o700)


def launch(paths):
    """サーバーを、CLI から切り離して起動する"""
    options = {}
    if IS_WINDOWS:
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    log = os.open(paths.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | FLAGS, 0o600)
    try:
        os.lseek(log, 0, os.SEEK_END)  # Windows でも、ログの後ろに足す
        return subprocess.Popen([sys.executable, os.path.join(SCRIPTS, "server.py"), "--workdir", paths.workdir],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=paths.workdir,
                                close_fds=True, env=env, **options)
    finally:
        os.close(log)


def start(paths):
    info = read_info(paths)
    found = probe(paths, info) if info else False
    if found == BUSY:
        return busy(paths, info)
    if found:
        return output(0, True, "Web管理画面はもう動いています。ブラウザーで %s を開いてください。" % info["url"],
                      paths, info["url"], True)
    prepare(paths)
    clear(paths)
    proc = launch(paths)
    deadline = time.time() + START_SECONDS
    while time.time() < deadline:
        time.sleep(0.2)
        info = read_info(paths)
        if info and info.get("pid") == proc.pid and probe(paths, info) is True:
            return output(0, True, "Web管理画面を起動しました。ブラウザーで %s を開いてください。" % info["url"],
                          paths, info["url"], True)
        if proc.poll() is not None:
            return output(1, False, "Web管理画面のサーバーが、起動してすぐに終わりました。ログ（%s）を確かめてください。"
                          % paths.log, paths)
    proc.kill()
    return output(1, False, "Web管理画面のサーバーが、%d 秒たっても応答しません。ログ（%s）を確かめてください。"
                  % (START_SECONDS, paths.log), paths)


def stop(paths):
    info = read_info(paths)
    found = probe(paths, info) if info else False
    if found == BUSY or (found and call(info, "POST", "/api/shutdown") == BUSY):
        return busy(paths, info)
    if not found:
        clear(paths)  # 前のサーバーが残したフラグがあれば消す
        return output(0, True, "Web管理画面は動いていません。", paths)
    deadline = time.time() + STOP_SECONDS
    while time.time() < deadline:
        if not os.path.exists(paths.recording) and call(info, "GET", "/api/state", 0.5) is None:
            clear(paths)
            return output(0, True, "Web管理画面を終了しました。記録も「実行を止める」も止まりました。", paths)
        time.sleep(0.2)
    return output(1, False, "Web管理画面に終了を伝えましたが、%d 秒たっても終わりません。ログ（%s）を確かめてください。"
                  "それでも終わらなければ、プロセス番号 %s のプロセスを止めてください。"
                  % (STOP_SECONDS, paths.log, info.get("pid")), paths)


def status(paths):
    info = read_info(paths)
    found = probe(paths, info) if info else False
    if found == BUSY:
        return busy(paths, info)
    if found:
        return output(0, True, "Web管理画面は動いています。URL は %s です。" % info["url"], paths, info["url"], True)
    return output(0, True, "Web管理画面は動いていません。", paths)


def default_workdir():
    """フックと同じ作業フォルダ。CLI が渡す CLAUDE_PROJECT_DIR か COPILOT_PROJECT_DIR、なければ今のフォルダ。
    Copilot CLI のフックは、Git のリポジトリの中では、いちばん上のフォルダを作業フォルダとして渡す。
    Copilot CLI から動かしたとき（COPILOT_CLI がある）は、それに合わせる"""
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("COPILOT_PROJECT_DIR")
    if project:
        return project
    here = folder = os.getcwd()
    while os.environ.get("COPILOT_CLI"):
        if os.path.exists(os.path.join(folder, ".git")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return here


def main(argv=None):
    parser = argparse.ArgumentParser(description="Web管理画面を起動・終了する")
    parser.add_argument("command", choices=("start", "stop", "status"))
    parser.add_argument("--workdir", default=default_workdir())
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        if e.code == 0:  # --help
            return 0
        return output(2, False, "使い方：dashboard.py start|stop|status [--workdir 作業フォルダ]",
                      problems=["コマンドは start・stop・status のどれかにしてください。"])
    workdir = os.path.abspath(args.workdir)
    if not os.path.isdir(workdir):
        return output(2, False, "作業フォルダ %s がありません。--workdir を確かめてください。" % workdir,
                      problems=["作業フォルダがありません：%s" % workdir])
    paths = Paths(workdir)
    links = [p for p in (paths.base, paths.gitignore, paths.sessions, paths.logs, paths.log) if os.path.islink(p)]
    if links:
        return output(2, False, "%s がシンボリックリンクなので使えません。消してから、もう一度動かしてください。"
                      % "、".join(links), problems=["シンボリックリンクです：%s" % p for p in links])
    try:
        return {"start": start, "stop": stop, "status": status}[args.command](paths)
    except OSError as e:
        return output(1, False, "%s のファイルを扱えませんでした（%s）。フォルダの権限と空き容量を確かめてください。"
                      % (paths.base, e), paths)


if __name__ == "__main__":
    sys.exit(main())
