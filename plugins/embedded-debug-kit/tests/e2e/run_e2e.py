"""通しのテスト（E2E）。模擬の機器と `claude -p` で、2段階目と3段階目が不具合を直せるかと、止める目印でツールが断られるかを確かめる。

AI の利用料がかかるので、CI では動かさない。Linux で動く（模擬の機器が疑似端末を使う）。
模擬の機器には pyftpdlib が要る。--python に、それが入った Python を渡す。

  python3 tests/e2e/run_e2e.py --python <pyftpdlib の入った python> [--case worker|orchestrator|stop ...]

結果は、ケースごとの合否を JSON で出す。作業フォルダと CLI の出力は --out のフォルダに残す。
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
FIXTURE = HERE / "fixtures" / "sensor_app"
PREFIX = PLUGIN.name
EXPECTED = "AVG=25"

TASK = (
    "作業フォルダの sensor_app は、12個のセンサーの値の平均を AVG=<値> の形で出すプログラムです。"
    "機器で動かすと、平均が正しくありません（値はどれも 24〜26 なので、平均は 25 のはずです）。"
    "原因を調べて直してください。合格の条件は、機器で動かしたときの出力に AVG=25 の行があることです。"
    "スキルの設定は済んでいます。試行の上限は3回です。デバッグ用の出力を足したときは、消してください。"
)
STOP_TASK = "作業フォルダのファイルの一覧を見て、Makefile の中身を教えてください。"

CONFIGS = {
    "cross-build": """[settings]
source_dir = .
build_command = make
outputs =
    sensor_app
timeout_seconds = 120
""",
    "ftp-deploy": """[settings]
host = 127.0.0.1
port = {ftp}
user = root
password_env = E2E_DEVICE_PASSWORD
files =
    sensor_app
remote_dir = /app
file_mode = 755
timeout_seconds = 20
""",
    "serial-console": """[settings]
port = socket://127.0.0.1:{shell}
prompt = [#$] $
commands =
    ./app/sensor_app
command_timeout_seconds = 20
""",
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"模擬の機器のポート {port} が開きません")


def make_workdir(base, ftp, shell):
    work = base / "work"
    shutil.copytree(FIXTURE, work)
    for skill, text in CONFIGS.items():
        folder = work / f".{skill}"
        folder.mkdir()
        (folder / ".gitignore").write_text("*\n", encoding="utf-8")
        (folder / "config.ini").write_text(text.format(ftp=ftp, shell=shell), encoding="utf-8")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid", "commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", *args], cwd=work, check=True)
    return work


def claude(work, agent, prompt, out, session=None, budget="3"):
    cmd = ["claude", "-p", "--plugin-dir", str(PLUGIN), "--agent", f"{PREFIX}:{agent}",
           "--output-format", "stream-json", "--verbose", "--max-budget-usd", budget,
           "--allowedTools", "Read,Edit,Write,Grep,Glob,Bash,Skill,Agent", "--setting-sources", "project"]
    if session:
        cmd += ["--session-id", session]
    env = dict(os.environ, E2E_DEVICE_PASSWORD="sim")
    with open(out, "w", encoding="utf-8") as f:
        subprocess.run(cmd + [prompt], cwd=work, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=3600)
    return [json.loads(line) for line in open(out, encoding="utf-8") if line.startswith("{")]


def tool_uses(events, main_only=False):
    """ツールの呼び出し。main_only のときは、メインの会話のものだけ（サブエージェントのものを除く）"""
    for e in events:
        if e.get("type") == "assistant" and not (main_only and e.get("parent_tool_use_id")):
            for c in e["message"].get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    yield c


def tool_results(events):
    for e in events:
        if e.get("type") == "user":
            for c in e["message"].get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    yield c


def device_output(work):
    """直したソースを、この PC でビルドして動かした出力（機器と同じソース）"""
    subprocess.run(["make", "-s", "-B"], cwd=work, capture_output=True)
    run = subprocess.run(["./sensor_app"], cwd=work, capture_output=True, text=True)
    return run.stdout


def final(events):
    return next((e.get("result", "") for e in reversed(events) if e.get("type") == "result"), "")


def case_worker(base, ftp, shell):
    work = make_workdir(base / "worker", ftp, shell)
    events = claude(work, "device-debugger", TASK, base / "worker.jsonl")
    skills = {u["input"].get("skill", "").split(":")[-1] for u in tool_uses(events) if u["name"] == "Skill"}
    out = device_output(work)
    checks = {
        "直った（AVG=25）": EXPECTED in out,
        "スキルを使った（ビルド・転送・実行）": {"cross-build", "ftp-deploy", "serial-console"} <= skills,
        "デバッグ用の出力が残っていない": out.strip().splitlines()[-1:] == [EXPECTED],
    }
    return checks, final(events)


def case_orchestrator(base, ftp, shell):
    work = make_workdir(base / "orchestrator", ftp, shell)
    events = claude(work, "debug-orchestrator", TASK, base / "orchestrator.jsonl", budget="6")
    called = [u["input"].get("subagent_type", "") for u in tool_uses(events, main_only=True) if u["name"] in ("Agent", "Task")]
    direct = [u["name"] for u in tool_uses(events, main_only=True) if u["name"] in ("Edit", "Write", "Bash", "Skill")]
    text = final(events)
    checks = {
        "直った（AVG=25）": EXPECTED in device_output(work),
        "作業エージェントに頼んだ": any(c.endswith("device-debugger") for c in called),
        "検証エージェントに頼んだ": any(c.endswith("debug-verifier") for c in called),
        "ほかのエージェントに頼んでいない": all(c.endswith(("device-debugger", "debug-verifier")) for c in called),
        "統括エージェントが自分でファイルを直していない": not direct,
        "結論と条件ごとの判定を報告した": "C1" in text and ("成功" in text or "失敗" in text or "人の判断" in text),
    }
    return checks, text


def press_stop(url, session, events_file, seconds=120):
    """依頼が記録されたら、画面の「実行を止める」と同じ要求をサーバーに送る"""
    end = time.time() + seconds
    while time.time() < end and not events_file.is_file():
        time.sleep(0.1)
    base, token = url.split("/#k=")
    host = base.split("//", 1)[1]
    req = urllib.request.Request(base + "/api/stop", data=json.dumps({"session": session}).encode("utf-8"), method="POST",
                                 headers={"X-Dashboard-Token": token, "Origin": base, "Host": host,
                                          "Content-Type": "application/json"})
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 にはプロキシを通さない
    with direct.open(req, timeout=10) as res:
        return json.loads(res.read().decode("utf-8")).get("ok") is True


def case_stop(base, ftp, shell):
    work = make_workdir(base / "stop", ftp, shell)
    py = "py" if os.name == "nt" else sys.executable
    dash = PLUGIN / "skills" / "debug-dashboard" / "scripts" / "dashboard.py"
    started = json.loads(subprocess.run([py, str(dash), "start", "--workdir", str(work)], capture_output=True, text=True).stdout)
    session = str(uuid.uuid4())
    folder = work / ".debug-dashboard" / "sessions" / session
    pressed = {}
    presser = threading.Thread(target=lambda: pressed.update(ok=press_stop(started["url"], session, folder / "events.jsonl")))
    presser.start()
    try:
        events = claude(work, "device-debugger", STOP_TASK, base / "stop.jsonl", session=session, budget="1")
    finally:
        presser.join(5)
        subprocess.run([py, str(dash), "stop", "--workdir", str(work)], capture_output=True)
    results = list(tool_results(events))
    checks = {
        "画面を起動できた": bool(started.get("ok")),
        "「実行を止める」が受け付けられた": pressed.get("ok") is True,
        "ツールの呼び出しを断った": bool(results) and all(r.get("is_error") for r in results),
        "記録が残った": (folder / "events.jsonl").is_file(),
    }
    return checks, final(events)


CASES = {"worker": case_worker, "orchestrator": case_orchestrator, "stop": case_stop}


def main():
    parser = argparse.ArgumentParser(description="通しのテスト（E2E）")
    parser.add_argument("--python", required=True, help="pyftpdlib の入った Python（模擬の機器を動かす）")
    parser.add_argument("--case", nargs="*", choices=sorted(CASES), default=sorted(CASES))
    parser.add_argument("--out", help="作業フォルダと CLI の出力を残すフォルダ（既定は一時フォルダ）")
    args = parser.parse_args()
    base = Path(args.out or tempfile.mkdtemp(prefix="kit-e2e-")).resolve()
    base.mkdir(parents=True, exist_ok=True)
    ftp, shell = free_port(), free_port()
    device = base / "device"
    sim = subprocess.Popen([args.python, str(HERE / "device_sim.py"), "--root", str(device),
                            "--ftp-port", str(ftp), "--shell-port", str(shell)],
                           stdout=open(base / "device_sim.log", "w"), stderr=subprocess.STDOUT)
    report = {"out": str(base), "cases": {}}
    try:
        wait_port(ftp)
        wait_port(shell)
        for name in args.case:
            checks, text = CASES[name](base, ftp, shell)
            report["cases"][name] = {"ok": all(checks.values()), "checks": checks, "final": text[-2000:]}
    finally:
        sim.terminate()
    report["ok"] = all(c["ok"] for c in report["cases"].values())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
