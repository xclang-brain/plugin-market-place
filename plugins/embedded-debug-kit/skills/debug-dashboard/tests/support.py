"""テストで共通に使うもの。スキルのフォルダの中だけを参照する。"""

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
DATA = ".debug-dashboard"  # 作業フォルダの記録のフォルダ


def clean_env(**extra):
    """CLI から渡る環境変数を除いた環境"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "COPILOT_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def temp_project(test):
    path = Path(tempfile.mkdtemp(prefix="dashboard-test-"))
    test.addCleanup(shutil.rmtree, str(path), True)
    return path


def mark_recording(project, age=0.0):
    """サーバーの代わりに、記録中の目印を置く"""
    base = project / DATA
    base.mkdir(mode=0o700, exist_ok=True)
    (base / "recording").write_text(json.dumps({"pid": 1, "port": 1, "time": time.time() - age}), encoding="utf-8")
    return base


def run_hook(project, payload, event=None, **env):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    args = [sys.executable, str(SCRIPTS / "hook.py")] + ([event] if event else [])
    proc = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                          env=clean_env(CLAUDE_PROJECT_DIR=str(project), **env))
    return proc.returncode, proc.stdout.decode("utf-8")


def read_events(project, session="s1"):
    path = project / DATA / "sessions" / session / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def can_symlink(folder):
    try:
        os.symlink(str(folder), str(folder / "probe-link"))
    except (OSError, NotImplementedError):
        return False
    os.remove(str(folder / "probe-link"))
    return True


def start_server(test, project):
    """server.py を起動し、server.json の中身を返す。テストの後に止める"""
    (project / DATA).mkdir(mode=0o700, exist_ok=True)
    proc = subprocess.Popen([sys.executable, str(SCRIPTS / "server.py"), "--workdir", str(project)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=clean_env())
    test.addCleanup(stop_process, proc)
    info_path = project / DATA / "server.json"
    deadline = time.time() + 10
    while time.time() < deadline:
        if info_path.is_file():
            try:
                return proc, json.loads(info_path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        time.sleep(0.05)
    raise AssertionError("サーバーが起動しませんでした")


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def request(info, method, path, body=None, host=None, token=None, origin=None, ctype="application/json"):
    """ヘッダーを細かく決めて、サーバーに要求を送る。(状態コード, ヘッダー, 本文) を返す"""
    port = info["port"]
    headers = {"Host": host or "127.0.0.1:%d" % port}
    if token is not False:
        headers["X-Dashboard-Token"] = token or info["token"]
    data = None
    if method == "POST":
        headers["Origin"] = origin if origin is not None else "http://127.0.0.1:%d" % port
        if ctype:
            headers["Content-Type"] = ctype
        data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode("utf-8")
    if origin is False:
        headers.pop("Origin", None)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()
