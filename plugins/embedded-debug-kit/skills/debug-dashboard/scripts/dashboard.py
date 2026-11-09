#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web管理画面を起動・終了し、結果を JSON 1つで標準出力に返す。

使い方：dashboard.py start|stop|status [--workdir 作業フォルダ]
  start   画面のサーバーを起動して URL を返す。もう動いていれば、その URL を返す
  stop    画面のサーバーを終了する。記録も止まる
  status  動いているかを返す（何も作らない）
終了コード：0＝成功、1＝起動や終了の失敗、2＝引数や作業フォルダの問題。
記録とログは <作業フォルダ>/.<このスキルのフォルダの名前>/ に置く。
"""

import argparse
import http.client
import json
import os
import subprocess
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.basename(os.path.dirname(SCRIPTS))  # フォルダの名前を変えてコピーしたら、その名前を使う
IS_WINDOWS = os.name == "nt"
START_SECONDS = 10  # サーバーの起動を待つ秒数。ふつうは 1 秒もかからない
STOP_SECONDS = 5    # 終了を待つ秒数
PING_SECONDS = 2    # 動いているかの問い合わせを待つ秒数


class Paths:
    def __init__(self, workdir):
        self.base = os.path.join(workdir, "." + SKILL)
        self.server = os.path.join(self.base, "server.json")
        self.recording = os.path.join(self.base, "recording")
        self.logs = os.path.join(self.base, "logs")
        self.log = os.path.join(self.logs, "server.log")


def output(code, ok, message, paths=None, url=None, running=False, problems=()):
    result = {"ok": ok, "message": message, "problems": list(problems), "config": None,
              "log": paths.log if paths and os.path.isfile(paths.log) else None,
              "url": url, "running": running}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, indent=2) + "\n")
    return code


def read_info(paths):
    if os.path.islink(paths.base) or os.path.islink(paths.server):
        return None
    try:
        with open(paths.server, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and isinstance(info.get("port"), int) else None


def call(info, method, path, timeout=PING_SECONDS):
    """サーバーに問い合わせる。応答の状態コードを返す。つながらなければ None"""
    port = info["port"]
    headers = {"X-Dashboard-Token": str(info.get("token", ""))}
    body = None
    if method == "POST":
        headers.update({"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port})
        body = b"{}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        status = conn.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    return status


def running_info(paths):
    info = read_info(paths)
    return info if info and call(info, "GET", "/api/state") == 200 else None


def remove_stale(paths):
    for path in (paths.server, paths.recording):
        if os.path.isfile(path) and not os.path.islink(path):
            os.remove(path)


def prepare(paths):
    """記録のフォルダを本人だけが使える権限で作り、何よりも先に .gitignore を置く"""
    if not os.path.isdir(paths.base):
        os.mkdir(paths.base, 0o700)
    elif not IS_WINDOWS:
        os.chmod(paths.base, 0o700)
    gitignore = os.path.join(paths.base, ".gitignore")
    if not os.path.isfile(gitignore):
        with open(gitignore, "w", encoding="utf-8", newline="\n") as f:
            f.write("*\n")
    if not os.path.isdir(paths.logs):
        os.mkdir(paths.logs, 0o700)


def launch(workdir, paths):
    """サーバーを、CLI から切り離して起動する"""
    options = {}
    if IS_WINDOWS:
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(paths.log, "ab") as log:
        return subprocess.Popen([sys.executable, os.path.join(SCRIPTS, "server.py"), "--workdir", workdir],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=workdir,
                                close_fds=True, env=env, **options)


def start(workdir, paths):
    info = running_info(paths)
    if info:
        return output(0, True, "Web管理画面はもう動いています。ブラウザーで %s を開いてください。" % info["url"],
                      paths, info["url"], True)
    prepare(paths)
    remove_stale(paths)
    proc = launch(workdir, paths)
    deadline = time.time() + START_SECONDS
    while time.time() < deadline:
        time.sleep(0.2)
        info = read_info(paths)
        if info and info.get("pid") == proc.pid and call(info, "GET", "/api/state") == 200:
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
    if not info or call(info, "POST", "/api/shutdown") != 200:
        remove_stale(paths)  # 前のサーバーが残した目印があれば消す
        return output(0, True, "Web管理画面は動いていません。", paths)
    deadline = time.time() + STOP_SECONDS
    while time.time() < deadline:
        if not os.path.exists(paths.recording) and call(info, "GET", "/api/state", 0.5) is None:
            return output(0, True, "Web管理画面を終了しました。記録も止まりました。", paths)
        time.sleep(0.2)
    return output(1, False, "Web管理画面に終了を伝えましたが、%d 秒たっても終わりません。ログ（%s）を確かめてください。"
                  "それでも終わらなければ、プロセス番号 %s のプロセスを止めてください。"
                  % (STOP_SECONDS, paths.log, info.get("pid")), paths)


def status(paths):
    info = running_info(paths)
    if info:
        return output(0, True, "Web管理画面は動いています。URL は %s です。" % info["url"], paths, info["url"], True)
    return output(0, True, "Web管理画面は動いていません。", paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Web管理画面を起動・終了する")
    parser.add_argument("command", choices=("start", "stop", "status"))
    parser.add_argument("--workdir", default=os.getcwd())
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
    if os.path.islink(paths.base):
        return output(2, False, "%s がシンボリックリンクなので使えません。消してから、もう一度起動してください。" % paths.base,
                      problems=["シンボリックリンクです：%s" % paths.base])
    try:
        return {"start": lambda: start(workdir, paths), "stop": lambda: stop(paths),
                "status": lambda: status(paths)}[args.command]()
    except OSError as e:
        return output(1, False, "%s のファイルを扱えませんでした（%s）。フォルダの権限と空き容量を確かめてください。"
                      % (paths.base, e), paths)


if __name__ == "__main__":
    sys.exit(main())
