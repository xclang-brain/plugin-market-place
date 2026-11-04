"""通しの試験（test_e2e_flow.py）の道具。

- Template / World：エミュレーターに題材を登録した状態（seed.py）を1回だけ作り、場面ごとに写して使う。
- Emulator：Coverity Connect のエミュレーター（python3 -m coverity_emulator.server）。
- McpClient：本物と同じく `uv run ... coverity-triage-mcp` で起動した MCP サーバーに、stdio の JSON-RPC で話す。
- Ct：本物と同じく `uv run --native-tls <ct.py>` で ct.py を動かす。
- Orchestrator / Actors：モデルの代わりに、決まった手順で統括（/coverity-run の手順）と、取得・作業・統合・検証の
  エージェントの役をする（指示ファイルを読み、決められたファイルだけを書く）。

標準のライブラリだけで書く（依存のない Python から動かすため）。プラグインの中には何も作らない
（子のプロセスには PYTHONDONTWRITEBYTECODE=1、UV_PROJECT_ENVIRONMENT は一時のフォルダ）。
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent  # tests/stage2/task
PLUGIN = HERE.parents[2]
SCRIPTS = PLUGIN / "skills" / "coverity-triage-scripts" / "scripts"
CT = SCRIPTS / "ct.py"
SERVER = PLUGIN / "server"
BIN = HERE / "coverity_emulator" / "bin"
FIXTURES = PLUGIN / "tests" / "fixtures"
CAPTURE_SCRIPT = HERE / "e2e_flow_capture.mjs"
UV = shutil.which("uv")
TOOLS = ("uv", "git", "svn", "svnadmin", "svnversion", "make", "gcc", "g++", "clang", "cppcheck")
USER, KEY = "triager", "emu-triager-key-0001"  # seed.py の利用者（Web サービスを使える、管理者でない）
ADMIN = ("admin", "emu-admin-key-0001")
REMOTE_ROOT = "/build/agent/work/envmon/"
PROTOCOL = "2025-06-18"


def missing_tools() -> List[str]:
    return [t for t in TOOLS if not shutil.which(t)]


# ---- 時間の記録 ------------------------------------------------------------------------------


class Timings:
    """場面ごと・段ごとの所要時間（秒）。試験の終わりに一覧にする。"""

    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, float]] = []

    def add(self, scene: str, stage: str, seconds: float) -> None:
        self.rows.append((scene, stage, round(seconds, 1)))

    def total(self) -> float:
        return round(sum(r[2] for r in self.rows), 1)

    def by_scene(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for scene, _, seconds in self.rows:
            out[scene] = round(out.get(scene, 0.0) + seconds, 1)
        return out

    def text(self) -> str:
        lines = ["通しの試験の所要時間（秒）"]
        for scene, stage, seconds in self.rows:
            lines.append(f"  {scene:<14} {stage:<34} {seconds:>7.1f}")
        for scene, seconds in self.by_scene().items():
            lines.append(f"  {scene:<14} {'（場面の計）':<34} {seconds:>7.1f}")
        lines.append(f"  合計 {self.total():.1f} 秒（{self.total() / 60:.1f} 分）")
        return "\n".join(lines)


TIMINGS = Timings()


class Stage:
    """with Stage(scene, "段の名前"): ... の所要時間を TIMINGS に入れる。"""

    def __init__(self, scene: str, stage: str) -> None:
        self.scene, self.stage = scene, stage

    def __enter__(self) -> "Stage":
        self.started = time.monotonic()
        return self

    def __exit__(self, *exc: Any) -> None:
        TIMINGS.add(self.scene, self.stage, time.monotonic() - self.started)


# ---- 一時のフォルダ ---------------------------------------------------------------------------


class Session:
    """この試験の一時のフォルダ（試験の終わりに消す）と、MCP サーバーの環境（UV_PROJECT_ENVIRONMENT）。"""

    root: Optional[Path] = None
    procs: List[subprocess.Popen] = []

    @classmethod
    def dir(cls) -> Path:
        if cls.root is None:
            cls.root = Path(tempfile.mkdtemp(prefix="cwt-e2e-")).resolve()
        return cls.root

    @classmethod
    def uv_env(cls) -> Path:
        return cls.dir() / "uv-project-env"

    @classmethod
    def track(cls, proc: subprocess.Popen) -> subprocess.Popen:
        cls.procs.append(proc)
        return proc

    @classmethod
    def cleanup(cls) -> None:
        for proc in cls.procs:
            if proc.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except OSError:
                    pass
        cls.procs = []
        if cls.root is not None:
            _kill_strays(cls.root)
            if os.environ.get("CWT_E2E_KEEP") == "1":  # 調べるときだけ：一時のフォルダを残す
                sys.stderr.write(f"一時のフォルダを残しました: {cls.root}\n")
            else:
                shutil.rmtree(cls.root, ignore_errors=True)
            cls.root = None


def _kill_strays(root: Path) -> None:
    """一時のフォルダを使う残ったプロセス（ct.py gui、初回解析）を止める（Linux の /proc だけを見る）。"""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return
    mine = str(root)
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if mine in cmdline:
            try:
                os.kill(int(entry.name), signal.SIGKILL)
            except OSError:
                pass


def base_env(**extra: str) -> Dict[str, str]:
    """子のプロセスの環境。認証情報は呼び出し元が足す。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("COV_USER", "COV_AUTH_KEY", "COVERITY_PASSPHRASE", "VIRTUAL_ENV", "PYTHONPATH")
           and not k.startswith("COV_EMU_")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0",
               UV_PROJECT_ENVIRONMENT=str(Session.uv_env()))
    env.update(extra)
    return env


# ---- 子のプロセス ------------------------------------------------------------------------------


def run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None, check: bool = True,
        timeout: float = 600, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True,
                          timeout=timeout, input=input_text)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} が失敗しました（{proc.returncode}）:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return proc


class CtResult(dict):
    """ct.py の JSON と、終了コード・所要時間・標準エラー。"""

    code: int = 0
    seconds: float = 0.0
    stderr: str = ""
    command: List[str] = []


class Ct:
    """ct.py を、本物と同じく `uv run --native-tls <ct.py>` で動かす。"""

    def __init__(self, env: Dict[str, str], cwd: Path, log: Path) -> None:
        self.env, self.cwd, self.log = env, cwd, log
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, *args: str, env: Optional[Dict[str, str]] = None, timeout: float = 900) -> CtResult:
        cmd = [UV, "run", "--native-tls", "--quiet", str(CT), *args]
        started = time.monotonic()
        proc = subprocess.run(cmd, cwd=str(self.cwd), env={**self.env, **(env or {})}, capture_output=True,
                              text=True, timeout=timeout)
        seconds = time.monotonic() - started
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            data = {"ok": False, "error": f"ct.py の出力が JSON ではありません（終了コード {proc.returncode}）",
                    "stdout": proc.stdout[-3000:]}
        out = CtResult(data)
        out.code, out.seconds, out.stderr, out.command = proc.returncode, seconds, proc.stderr, list(args)
        self.calls.append({"args": list(args), "code": proc.returncode, "seconds": round(seconds, 2)})
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": list(args), "code": proc.returncode, "seconds": round(seconds, 2),
                                "out": data}, ensure_ascii=False)[:20000] + "\n")
        return out


# ---- エミュレーター ------------------------------------------------------------------------------


class Emulator:
    def __init__(self, data: Path, log: Path, *args: str) -> None:
        self.data = data
        env = base_env()
        self.log = open(log, "ab")
        self.proc = Session.track(subprocess.Popen(
            [sys.executable, "-m", "coverity_emulator.server", "--port", "0", "--data", str(data), *args],
            cwd=str(HERE), stdout=subprocess.PIPE, stderr=self.log, env=env, start_new_session=True))
        line = self.proc.stdout.readline().decode("utf-8")
        if not line:
            raise RuntimeError(f"エミュレーターが起動しません（{log}）")
        self.info = json.loads(line)
        self.url = self.info["url"]
        # 管理の窓口と試験からの REST は、平文の窓口で呼ぶ（--tls のときは http_url。自己署名の証明書を避ける）
        self.plain_url = self.info.get("http_url") or self.url

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc.stdout.close()
        self.log.close()

    def _request(self, method: str, path: str, body: Any = None, auth: Optional[Tuple[str, str]] = None,
                 timeout: float = 60) -> Tuple[int, bytes]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.plain_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def admin(self, method: str, name: str, body: Any = None) -> Any:
        status, data = self._request(method, f"/__emulator__/{name}", body)
        if status != 200:
            raise AssertionError(f"エミュレーターの管理の窓口 {name}: {status} {data[:300]!r}")
        return json.loads(data)

    def config(self, **changes: Any) -> Dict[str, Any]:
        return self.admin("POST", "config", changes)

    def inject(self, *rules: Dict[str, Any]) -> None:
        self.config(inject=list(rules))

    def reset(self) -> None:
        self.config(reset=True)

    def writes(self) -> List[Dict[str, Any]]:
        return self.admin("GET", "triage-writes")["writes"]

    def issues(self, stream: str) -> List[Dict[str, Any]]:
        return self.admin("GET", f"issues?stream={stream}")["issues"]

    def state(self) -> Dict[str, Any]:
        return self.admin("GET", "state")

    def requests(self, since: int = 0, path: Optional[str] = None) -> Dict[str, Any]:
        query = f"requests?since={since}" + (f"&path={urllib.request.quote(path)}" if path else "")
        return self.admin("GET", query)

    def rest(self, method: str, path: str, body: Any = None, auth: Tuple[str, str] = ADMIN) -> Tuple[int, Any]:
        status, data = self._request(method, path, body, auth)
        try:
            return status, json.loads(data) if data else None
        except ValueError:
            return status, data

    # ---- エミュレーターの状態から求める、正しい答え ----

    def latest_cids(self, stream: str) -> List[int]:
        """ストリームの最新のスナップショットにある CID（検索の範囲 last() の答え）。"""
        state = self.state()
        snaps = sorted((s for s in state["snapshots"].values() if s["stream"] == stream),
                       key=lambda s: (s["dateCreated"], s["id"]))
        return sorted(int(c) for c in snaps[-1]["occurrences"]) if snaps else []

    def table(self, stream: str) -> Dict[int, Dict[str, Any]]:
        """CID ごとの checker・impact・status・トリアージの値（ストリームのトリアージストアの値）。"""
        state = self.state()
        rows = {}
        for item in self.issues(stream):
            rec = state["cids"][str(item["cid"])]
            rows[item["cid"]] = {"checker": item["checker"], "status": item["status"],
                                 "impact": (rec.get("checkerProperties") or {}).get("impact"),
                                 "classification": item["triage"]["Classification"],
                                 "action": item["triage"]["Action"], "function": item.get("function"),
                                 "file": item["file"], "line": item["line"]}
        return rows


# ---- 題材を登録した状態を1回だけ作る -------------------------------------------------------------


class Template:
    """seed.py で題材を登録したエミュレーターのデータとリポジトリ（場面ごとに写して使う）。"""

    summary: Optional[Dict[str, Any]] = None
    root: Optional[Path] = None

    @classmethod
    def get(cls) -> Dict[str, Any]:
        if cls.summary is None:
            started = time.monotonic()
            root = Session.dir() / "template"
            root.mkdir()
            emu = Emulator(root / "data", root / "emulator.log")
            try:
                proc = run([sys.executable, "-m", "coverity_emulator.seed", "--url", emu.url, "--workdir",
                            str(root / "work"), "--no-large-sample"], cwd=HERE, env=base_env(), timeout=600)
                cls.summary = json.loads(proc.stdout)
            finally:
                emu.stop()
            cls.root = root
            TIMINGS.add("準備", "題材の登録（seed.py）", time.monotonic() - started)
        return cls.summary

    @classmethod
    def truth(cls) -> Dict[int, Dict[str, str]]:
        """CID → 正解の記録（envmon-truth.yaml）の id と verdict。1つの CID が2件に当たるときは先のもの。"""
        out: Dict[int, Dict[str, str]] = {}
        for entry in cls.get()["truth"]["found"]:
            for hit in entry["found"]:
                out.setdefault(hit["cid"], {"id": entry["id"], "verdict": entry["verdict"]})
        return out


# ---- MCP の stdio のクライアント ----------------------------------------------------------------


class ToolResult:
    def __init__(self, ok: bool, data: Any, error: str, seconds: float) -> None:
        self.ok, self.data, self.error, self.seconds = ok, data, error, seconds

    def __getitem__(self, key: str) -> Any:
        if not self.ok:
            raise AssertionError(f"ツールがエラーを返しました: {self.error}")
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default) if self.ok and isinstance(self.data, dict) else default

    def __repr__(self) -> str:
        return f"ToolResult(ok={self.ok}, error={self.error!r}, data={str(self.data)[:300]})"


class McpClient:
    """本物と同じコマンド（mcp.json）で起動した MCP サーバーに、改行で区切った JSON-RPC で話す。"""

    def __init__(self, env: Dict[str, str], log: Path) -> None:
        cmd = [UV, "run", "--native-tls", "--frozen", "--quiet", "--directory", str(SERVER), "coverity-triage-mcp"]
        self.log = open(log, "ab")
        self.proc = Session.track(subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                   stderr=self.log, env=env, cwd=str(Session.dir()),
                                                   start_new_session=True))
        self.messages: "queue.Queue[Any]" = queue.Queue()
        self.calls: List[Dict[str, Any]] = []
        self._id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.init = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                "clientInfo": {"name": "e2e-test", "version": "1"}}, timeout=300)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                self.messages.put(json.loads(raw.decode("utf-8")))
            except ValueError:
                continue
        self.messages.put(None)

    def _send(self, message: Dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: Dict[str, Any], timeout: float = 120) -> Dict[str, Any]:
        self._id += 1
        wanted = self._id
        self._send({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"MCP サーバーが {timeout} 秒で {method} に答えません")
            try:
                message = self.messages.get(timeout=left)
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError("MCP サーバーが終わりました")
            if message.get("id") == wanted and ("result" in message or "error" in message):
                if "error" in message:
                    raise RuntimeError(f"MCP の誤り: {message['error']}")
                return message["result"]
            if "method" in message and "id" in message:  # サーバーからの頼み（使わない）
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "not supported"}})

    def call(self, name: str, arguments: Dict[str, Any], timeout: float = 120) -> ToolResult:
        started = time.monotonic()
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout)
        seconds = time.monotonic() - started
        text = "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
        if result.get("isError"):
            out = ToolResult(False, None, text, seconds)
        else:
            data = result.get("structuredContent")
            if data is None:
                try:
                    data = json.loads(text)
                except ValueError:
                    data = text
            out = ToolResult(True, data, "", seconds)
        self.calls.append({"tool": name, "arguments": arguments, "ok": out.ok, "error": out.error,
                           "seconds": round(seconds, 2),
                           "done": out.get("done") if out.ok else None})
        return out

    def tools(self) -> List[Dict[str, Any]]:
        return self.request("tools/list", {})["tools"]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                self.proc.kill()
            self.proc.wait(5)
        self.log.close()


# ---- 場面（エミュレーター、リポジトリ、ct.py、MCP サーバー） --------------------------------------


class World:
    """1つの場面：題材を登録した状態の写し、利用者のリポジトリ、push 先の bare のリモート。"""

    def __init__(self, name: str, vcs: str = "git", tls: bool = False) -> None:
        summary = Template.get()
        self.name = name
        self.root = Session.dir() / name
        self.root.mkdir()
        self.logs = self.root / "logs"
        self.logs.mkdir()
        with Stage(name, "場面の用意（写し・起動）"):
            shutil.copytree(Template.root / "data", self.root / "data")
            shutil.copytree(Template.root / "work" / "repos", self.root / "repos", symlinks=True)
            self.vcs = vcs
            if vcs == "git":
                self.repo = self.root / "repos" / "git" / "envmon"
                self.analyzed, self.latest = summary["git"]["analyzed"], summary["git"]["latest"]
            else:
                self.repo = self.root / "repos" / "svn" / "envmon"
                url = (self.root / "repos" / "svnrepo").resolve().as_uri()
                run(["svn", "relocate", url], cwd=self.repo)
                self.analyzed, self.latest = summary["svn"]["analyzed"], summary["svn"]["latest"]
            args = ["--tls"] if tls else []
            self.emu = Emulator(self.root / "data", self.logs / "emulator.log", *args)
        self.home = self.root / "home"
        (self.home / "copilot").mkdir(parents=True)
        (self.home / "tmp").mkdir()
        (self.home / "gitconfig").write_text("[user]\n\tname = Triage Tester\n\temail = tester@example.invalid\n",
                                            encoding="utf-8")
        self.cov_log = self.root / "cov-calls.jsonl"
        self.wrappers = self._wrappers()
        self.env = base_env(PATH=os.pathsep.join([str(self.wrappers), str(BIN), os.environ.get("PATH", "")]),
                            COV_USER=USER, COV_AUTH_KEY=KEY, COPILOT_HOME=str(self.home / "copilot"),
                            CT_GUI_STATE_DIR=str(self.home / "gui"), TMPDIR=str(self.home / "tmp"),
                            GIT_CONFIG_GLOBAL=str(self.home / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                            CWT_COV_LOG=str(self.cov_log))
        self.ct = Ct(self.env, self.repo, self.logs / "ct.jsonl")
        self._mcp: Optional[McpClient] = None
        self._mcp_env: Dict[str, str] = {}
        self.remote: Optional[Path] = None
        self.push_log = self.root / "pushes.log"

    def _wrappers(self) -> Path:
        """cov-* の前に置く記録係：引数と、認証キーが環境にあるかだけを書いてから、模擬の解析コマンドを動かす。"""
        folder = self.root / "cov-wrappers"
        folder.mkdir()
        for name in ("cov-build", "cov-analyze", "cov-commit-defects", "cov-format-errors"):
            script = folder / name
            script.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "with open(os.environ['CWT_COV_LOG'], 'a', encoding='utf-8') as f:\n"
                f"    f.write(json.dumps({{'tool': {name!r}, 'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
                f"                        'passphrase_is_key': os.environ.get('COVERITY_PASSPHRASE') == {KEY!r},\n"
                "                        'key_env': 'COV_AUTH_KEY' in os.environ}) + '\\n')\n"
                f"os.execv({str(BIN / name)!r}, [{str(BIN / name)!r}] + sys.argv[1:])\n", encoding="utf-8")
            script.chmod(0o755)
        return folder

    # ---- MCP サーバー ----

    def credentials(self, **env_changes: str) -> None:
        """MCP サーバーに渡す環境（認証情報など）を変える。次の mcp() で起動し直す。何も渡さなければ元に戻す。"""
        self.overrides = dict(env_changes)

    def mcp(self) -> McpClient:
        """MCP サーバー（環境が変わったら起動し直す。利用者が環境変数を直して Copilot CLI を起動し直すのと同じ）。"""
        wanted = {**self.env, **getattr(self, "overrides", {})}
        if self._mcp is not None and wanted != self._mcp_env:
            self._mcp.close()
            self._mcp = None
        if self._mcp is None:
            self._mcp = McpClient(wanted, self.logs / "mcp-server.log")
            self._mcp_env = wanted
        return self._mcp

    def close(self) -> None:
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None
        self.emu.stop()

    # ---- 準備（/coverity-setup） ----

    def add_remote(self) -> Path:
        """git：push 先の bare のリモート（push のたびに pushes.log に1行書く）。"""
        remote = self.root / "remote.git"
        run(["git", "clone", "-q", "--bare", str(self.repo), str(remote)], env=self.env)
        hook = remote / "hooks" / "post-receive"
        hook.write_text("#!/bin/sh\nwhile read old new ref; do echo \"$ref $new\" >> \"%s\"; done\n" % self.push_log,
                        encoding="utf-8")
        hook.chmod(0o755)
        run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, env=self.env)
        run(["git", "fetch", "-q", "origin"], cwd=self.repo, env=self.env)
        run(["git", "branch", "-q", "--set-upstream-to=origin/main", "main"], cwd=self.repo, env=self.env)
        self.remote = remote
        return remote

    def setup(self, stream: str, verify_default: str = "build+analyze", initial: bool = True,
              agents: Optional[Dict[str, Any]] = None, url: Optional[str] = None) -> Dict[str, CtResult]:
        out: Dict[str, CtResult] = {}
        with Stage(self.name, "準備（init-config〜doctor）"):
            if self.vcs == "git":
                self.add_remote()
            out["init-config"] = self.ct("init-config", "--repo", str(self.repo), "--url", url or self.emu.url,
                                         "--project", "envmon", "--stream", stream, "--strip-prefix", REMOTE_ROOT)
            out["set-verify"] = self.ct("set-verify", "--repo", str(self.repo), "--build-command", "make",
                                        "--default", verify_default, "--initial" if initial else "--no-initial")
            if agents:
                self.set_agents(**agents)
            out["install-agent"] = self.ct("install-agent")
            out["prewarm"] = self.ct("prewarm")
            out["doctor"] = self.ct("doctor", "--repo", str(self.repo))
        return out

    @property
    def config_file(self) -> Path:
        return self.repo / ".coverity-triage" / "config.yaml"

    def set_agents(self, integration: bool = False, review: bool = False, max_rework: int = 1) -> None:
        """config.yaml の agents（画面の設定で選ぶ項目。ct.py にはこれを書くコマンドがない）。"""
        text = self.config_file.read_text(encoding="utf-8")
        text = re.sub(r"(?ms)^agents:\n(?:  .*\n)*", "", text)
        text += (f"agents:\n  integration: {str(integration).lower()}\n  review: {str(review).lower()}\n"
                 f"  max_rework: {max_rework}\n")
        self.config_file.write_text(text, encoding="utf-8")

    def write_filter(self, name: str, text: str) -> Path:
        path = self.repo / ".coverity-triage" / "filters" / name
        path.write_text(text, encoding="utf-8")
        return path

    # ---- 利用者の作業中のファイル ----

    def fingerprint(self) -> str:
        if self.vcs == "git":
            status = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=self.repo, env=self.env)
            diff = run(["git", "diff", "HEAD", "--binary"], cwd=self.repo, env=self.env)
            return status.stdout + "\0" + diff.stdout
        status = run(["svn", "status"], cwd=self.repo, env=self.env)
        diff = run(["svn", "diff"], cwd=self.repo, env=self.env)
        return status.stdout + "\0" + diff.stdout

    def tracked_changes(self) -> str:
        if self.vcs == "git":
            return run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=self.repo, env=self.env).stdout
        return "".join(l + "\n" for l in run(["svn", "status", "-q"], cwd=self.repo, env=self.env).stdout.splitlines())

    def pushes(self) -> List[str]:
        return self.push_log.read_text(encoding="utf-8").splitlines() if self.push_log.is_file() else []

    def cov_calls(self) -> List[Dict[str, Any]]:
        if not self.cov_log.is_file():
            return []
        return [json.loads(l) for l in self.cov_log.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---- 画面（ct.py gui）と写真 ------------------------------------------------------------------


def gui_call(url: str, method: str, path: str, body: Any = None) -> Tuple[int, Any]:
    """ct.py gui のサーバーへの頼み（画面と同じく X-CT-Token を付ける）。"""
    match = re.match(r"^(http://127\.0\.0\.1:\d+)/\?t=([^#&]+)", url)
    if not match:
        raise AssertionError(f"画面の URL の形が想定と違います: {url}")
    base, token = match.groups()
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("X-CT-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def capture_dir() -> Optional[Path]:
    value = os.environ.get("CWT_CAPTURE_DIR")
    return Path(value) if value else None


def capture(shots: List[Dict[str, Any]], env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """写真を撮る（環境変数 CWT_CAPTURE_DIR があるときだけ）。shots: [{url, name, width?, height?, full_page?}]。"""
    folder = capture_dir()
    if folder is None or not shots:
        return {"skipped": True, "shots": []}
    folder.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    if not node:
        return {"error": "node がありません", "shots": []}
    spec = Session.dir() / f"shots-{int(time.time() * 1000)}.json"
    spec.write_text(json.dumps({"dir": str(folder), "shots": shots}, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([node, str(CAPTURE_SCRIPT), str(spec)], capture_output=True, text=True, timeout=300,
                          cwd=str(PLUGIN), env={**os.environ, **(env or {})})
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"写真を撮れませんでした（終了コード {proc.returncode}）: {proc.stderr[-1500:]}", "shots": []}


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


# ---- 修正（作業エージェントが修正用のコピーで直すこと） ---------------------------------------------
#
# どれも最新のリビジョン（envmon-latest.patch を当てた後）のコードに当てる。修正は同じものを2回当てても
# 変わらない（差し戻しで前の修正が当ててあるときのため）。

FIXES: Dict[str, List[Tuple[str, str, str]]] = {
    "bmp_div0": [("src/drivers/bmp280.c", "    if (dev->hist_n == 0U) {\n        rc = EM4_ERR_EMPTY;\n    }\n",
                  "    if (dev->hist_n == 0U) {\n        *press_pa = 0U;\n        return EM4_ERR_EMPTY;\n    }\n")],
    "cmd_null": [("src/proto/cmd_parser.c", "    buf = (char *)malloc((size_t)want + 1U);\n    (void)memset",
                  "    buf = (char *)malloc((size_t)want + 1U);\n    if (buf == NULL) {\n        return EM4_ERR_NOMEM;\n"
                  "    }\n    (void)memset")],
    "frame_max": [("include/em4_config.h", "#define FRAME_MAX               (FRAME_HDR_LEN + FRAME_PAYLOAD_MAX)\n",
                   "#define FRAME_MAX               (FRAME_HDR_LEN + FRAME_PAYLOAD_MAX + FRAME_CRC_LEN)\n")],
    "rotate_null": [("src/app/logger.c", "    s_log.fp = fopen(s_log.path, \"w\");\n    (void)fprintf",
                     "    s_log.fp = fopen(s_log.path, \"w\");\n    if (s_log.fp == NULL) {\n        return EM4_ERR_IO;\n"
                     "    }\n    (void)fprintf")],
    # わざとビルドを壊す修正（セミコロンがない）
    "rotate_broken": [("src/app/logger.c", "    s_log.fp = fopen(s_log.path, \"w\");\n    (void)fprintf",
                       "    s_log.fp = fopen(s_log.path, \"w\");\n    if (s_log.fp == NULL) {\n        return EM4_ERR_IO\n"
                       "    }\n    (void)fprintf")],
    "apply_null": [("src/app/config_ini.c", "    sec = ini_find_section(doc, \"sensor\");\n    for",
                    "    sec = ini_find_section(doc, \"sensor\");\n    if (sec == NULL) {\n        return EM4_ERR_FORMAT;\n"
                    "    }\n    for")],
    "level_overrun": [("src/app/logger.c", "    for (i = 0U; i <= LOG_LEVEL_COUNT; i++) {",
                       "    for (i = 0U; i < LOG_LEVEL_COUNT; i++) {")],
    "gpio_set_overrun": [("src/hal/gpio.c", "em4_err_t gpio_set(uint8_t pin, bool level)\n{\n    if (pin > GPIO_PIN_COUNT) {",
                          "em4_err_t gpio_set(uint8_t pin, bool level)\n{\n    if (pin >= GPIO_PIN_COUNT) {")],
    "irq_guard": [("src/hal/gpio.c", "void gpio_irq_dispatch(uint8_t pin)\n{\n    s_irq_count[pin]++;\n",
                   "void gpio_irq_dispatch(uint8_t pin)\n{\n    if (pin >= GPIO_PIN_COUNT) {\n        return;\n    }\n"
                   "    s_irq_count[pin]++;\n")],
    "ini_leak": [("src/app/config_ini.c", "            LOG_E(TAG, \"line %u is too long\", lineno);\n            return EM4_ERR_RANGE;\n",
                  "            LOG_E(TAG, \"line %u is too long\", lineno);\n            rc = EM4_ERR_RANGE;\n            goto out;\n")],
    "ini_eq_null": [("src/app/config_ini.c", "        eq  = strchr(p, '=');\n        *eq",
                     "        eq  = strchr(p, '=');\n        if (eq == NULL) {\n            rc = EM4_ERR_FORMAT;\n"
                     "            goto out;\n        }\n        *eq")],
    "mean_null": [("src/telemetry/telemetry.cpp", "    return g_hub->channel(ch)->window_mean();\n",
                   "    ChannelStats* c = g_hub->channel(ch);\n    if (c == nullptr) {\n        return 0;\n    }\n"
                   "    return c->window_mean();\n")],
    "sr_void": [("src/hal/sim_bus.c", "    tmp = r->SR2;\n    i2c_hw", "    tmp = r->SR2;\n    (void)tmp;\n    i2c_hw")],
    # 足りない修正（警告の原因を直していない。ビルドでの再解析で警告が残る）
    "setdir_comment": [("src/app/logger.c", "    nd = (char *)malloc(strlen(dir));\n",
                        "    /* 確保する大きさは LOG_DIR_MAX で確かめている */\n    nd = (char *)malloc(strlen(dir));\n")],
}


def apply_fix(folder: Path, name: str) -> List[str]:
    """修正を当て、変えたファイルを返す。もう当ててあれば何もしない。"""
    changed = []
    for rel, old, new in FIXES[name]:
        path = folder / rel
        text = path.read_text(encoding="utf-8")
        if new in text:
            continue
        if text.count(old) != 1:
            raise AssertionError(f"{name}: {rel} に直す箇所が見つかりません")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        changed.append(rel)
    return changed


# ---- 作業ごとの方針（正解の記録 envmon-truth.yaml の id ごと） ------------------------------------

DEVIATION_ATTRS = {"false_positive": ("False Positive", "Ignore", "Unspecified"),
                   "intentional": ("Intentional", "Ignore", "Unspecified"),
                   "true_bug": ("Bug", "Ignore", "Major")}
PLANS: Dict[str, Dict[str, Any]] = {
    "TB-01": {"fix": "cmd_null", "summary": "malloc の戻り値を確かめずに memset に渡している",
              "fix_summary": "malloc が NULL を返したら EM4_ERR_NOMEM で戻る",
              "deviation": "本物の不具合。LOG DUMP は保守のときだけ使うコマンドで、通常の運用では呼ばれない（src/proto/cmd_parser.c:233）。"},
    "TB-02": {"fix": "rotate_null", "summary": "fopen の戻り値を確かめずに fprintf に渡している",
              "fix_summary": "fopen が NULL を返したら EM4_ERR_IO で戻る",
              "deviation": "本物の不具合。ログの切り替えは 16 KB を超えたときだけ起きる（src/app/logger.c:135）。"},
    "TB-03": {"fix": "apply_null", "summary": "[sensor] がないと NULL のセクションを参照する",
              "fix_summary": "[sensor] がなければ EM4_ERR_FORMAT で戻る",
              "deviation": "本物の不具合。出荷時の設定ファイルには [sensor] が必ずある（src/app/config_ini.c:327）。"},
    "TB-04": {"already": True, "summary": "不正なマジックのとき fp を閉じずに戻る（最新で修正済み）",
              "deviation": "本物の不具合。最新のリビジョンで fclose を足して直っている（src/drivers/flash_store.c:356）。"},
    "TB-05": {"fix": "ini_leak", "summary": "長すぎる行で line を解放せずに戻る",
              "fix_summary": "長すぎる行でも out: で line を解放してから戻る",
              "deviation": "本物の不具合。長すぎる行は設定の誤りのときだけ現れる（src/app/config_ini.c:103）。"},
    "TB-06": {"fix": "level_overrun", "summary": "ループが k_level_name の要素数を 1 つ超える",
              "fix_summary": "ループの条件を i < LOG_LEVEL_COUNT にする",
              "deviation": "本物の不具合。一致しない名前のときだけ配列の外を読む（src/app/logger.c:239）。"},
    "TB-07": {"fix": "gpio_set_overrun", "summary": "pin == GPIO_PIN_COUNT のとき配列の外に書く",
              "fix_summary": "範囲の確かめを pin >= GPIO_PIN_COUNT にする",
              "deviation": "本物の不具合。呼び出し元は定数のピン番号だけを渡す（src/hal/gpio.c:61）。"},
    "TB-08": {"fix": "frame_max", "summary": "FRAME_MAX に CRC の 2 バイトが入っていない",
              "fix_summary": "FRAME_MAX に FRAME_CRC_LEN を足す",
              "deviation": "本物の不具合。ペイロードが 63 バイト以上のときだけあふれる（include/em4_config.h:40）。"},
    "TB-13": {"already": True, "summary": "長い名前を strcpy で写す（最新で修正済み）",
              "deviation": "本物の不具合。最新のリビジョンで長さを確かめるように直っている（src/app/config_ini.c:303）。"},
    "TB-14": {"fix": "setdir_comment", "summary": "strlen(dir) バイトしか確保せず終端の 0 があふれる",
              "fix_summary": "確保の大きさの前提をコメントに書く",
              "deviation": "本物の不具合。ディレクトリ名は設定ファイルからだけ渡る（src/app/logger.c:198）。"},
    "TB-15": {"fix": "bmp_div0", "summary": "hist_n が 0 のとき 0 で割る",
              "fix_summary": "hist_n が 0 なら割り算をせずに EM4_ERR_EMPTY で戻る",
              "deviation": "本物の不具合。起動直後の測定前に呼ばれたときだけ起きる（src/drivers/bmp280.c:286）。"},
    "FP-06": {"fix": "ini_eq_null", "summary": "str_is_kv_line() が = があることを確かめている",
              "fix_summary": "strchr の戻り値も確かめる（念のため）",
              "deviation": "誤検知。str_is_kv_line() が = のある行だけを通すため、strchr() は NULL を返さない（src/app/config_ini.c:132）。"},
    "FP-07": {"fix": "irq_guard", "summary": "呼び出し元が範囲内のピンだけを渡している",
              "fix_summary": "関数の始めでもピンの範囲を確かめる",
              "deviation": "誤検知。timer_tick_isr() は保留のビットが立った範囲内のピンだけを渡すため、範囲の外を参照しない（src/hal/gpio.c:117）。"},
    "FP-09": {"fix": "mean_null", "summary": "channel() は範囲内の番号で NULL を返さない",
              "fix_summary": "channel() の戻り値も確かめる（念のため）",
              "deviation": "誤検知。呼び出し元は範囲内のチャネルだけを渡し、channel() は NULL を返さない（src/telemetry/telemetry.cpp:194）。"},
    "IN-03": {"fix": "sr_void", "summary": "SR1・SR2 を読むこと自体が目的",
              "fix_summary": "読んだ値を (void) で捨てることを明示する",
              "deviation": "意図的。ADDR フラグは SR1 と SR2 を順に読むと消える仕様で、読んだ値は使わない（src/hal/sim_bus.c:336）。"},
}
PLANS["TB-09"] = PLANS["TB-10"] = PLANS["TB-08"]
FRAME_MAX_CIDS = {10063, 10064, 10237}


def code_line(root: Path, rel: str, line: Optional[int]) -> str:
    path = root / rel
    if not line or not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[line - 1].strip()[:60] if 0 < line <= len(lines) else ""


# ---- 指示ファイルを読む --------------------------------------------------------------------------

BRIEF_PATH = re.compile(r"指示ファイル (\S+?) を読み")


def table_value(text: str, label: str) -> Optional[str]:
    match = re.search(r"^\| " + re.escape(label) + r" \| `([^`]*)` \|", text, re.MULTILINE)
    return match.group(1) if match else None


class Brief:
    """作業エージェントへの指示ファイル（brief.md）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        self.item = re.search(r"^# 調査の指示: (\S+)", self.text, re.MULTILINE).group(1)
        self.investigate = Path(table_value(self.text, "調べるコード（読むだけ。変更しない）"))
        self.fix_dir = Path(table_value(self.text, "修正するコード（ここだけを変更する）"))
        self.result = Path(table_value(self.text, "結果ファイル（書く）"))
        progress = table_value(self.text, "進み具合（書く。節目ごとに上書き）")
        self.progress = Path(progress) if progress else None
        self.warnings: List[Dict[str, Any]] = []
        for match in re.finditer(r"^### CID (\d+) — (.+?)（Impact [^）]*）\n\n- 場所: `([^`]*)`:(\S+)", self.text,
                                 re.MULTILINE):
            line = match.group(4)
            self.warnings.append({"cid": int(match.group(1)), "checker": match.group(2), "file": match.group(3),
                                  "line": int(line) if line.isdigit() else None})
        self.rework = "## 検証からの指摘（直すこと）" in self.text
        self.group = "## グループ" in self.text
        self.unknown_revision = "Coverity が解析したリビジョンが分からないため" in self.text
        self.no_paths = "（取れませんでした）" in self.text


# ---- 役（モデルの代わり） ---------------------------------------------------------------------------


class Actors:
    """取得・作業・統合・検証のエージェントの役。どれも指示ファイルを読み、決められたファイルだけを書く。

    run() は (返事の1行, 失敗の理由 or None) を返す。失敗の理由があれば、統括は --failed で知らせる。"""

    def __init__(self, world: World, fixes: Optional[Dict[str, str]] = None,
                 first_without_fix: Tuple[int, ...] = (), reviews: Optional[Callable[[str, int], Dict[str, Any]]] = None,
                 merge_frame_max: bool = True, collector_unavailable: bool = False,
                 on_search_call: Optional[Callable[[int, ToolResult], None]] = None) -> None:
        self.world = world
        self.truth = Template.truth()
        self.fixes = fixes or {}  # truth id -> 修正の名前（PLANS の fix を置き換える）
        self.first_without_fix = set(first_without_fix)
        self.reviews = reviews
        self.merge_frame_max = merge_frame_max
        self.collector_unavailable = collector_unavailable
        self.on_search_call = on_search_call  # 検索の1回ごとに呼ぶ（エミュレーターの切り替えを変えるため）
        self.log: List[Dict[str, Any]] = []
        self.review_rounds: Dict[str, int] = {}
        self.work_rounds: Dict[str, int] = {}
        self.search_calls: List[ToolResult] = []
        self.path_calls: List[ToolResult] = []

    def run(self, action: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        agent = action["agent"]
        brief = Path(BRIEF_PATH.search(action["prompt"]).group(1))
        if agent == "coverity-triage-collector":
            if self.collector_unavailable:
                return "", "カスタムエージェント coverity-triage-collector が見つかりません"
            out = self.collector(brief)
        elif agent == "coverity-triage-worker":
            out = self.worker(brief, action["prompt"])
        elif agent == "coverity-triage-integrator":
            out = self.integrator(brief)
        elif agent == "coverity-triage-reviewer":
            out = self.reviewer(brief)
        else:
            out = ("", f"知らないエージェント: {agent}")
        self.log.append({"agent": agent, "task": action["task"], "reply": out[0], "failed": out[1]})
        return out

    # ---- 取得 ----

    def collector(self, brief: Path) -> Tuple[str, Optional[str]]:
        text = brief.read_text(encoding="utf-8")
        mcp = self.world.mcp()
        if text.startswith("# 取得の指示: 警告の検索"):
            args = dict(re.findall(r"^\| `(\w+)` \| `([^`]*)` \|", text, re.MULTILINE))
            for _ in range(10):
                result = mcp.call("search_issues", args)
                self.search_calls.append(result)
                if self.on_search_call:
                    self.on_search_call(len(self.search_calls), result)
                if not result.ok:
                    return result.error, result.error  # エラーの文を1行で返す（呼び直さない）
                if result["done"]:
                    return (f"検索: found={result['found']} limited={str(result['limited']).lower()} "
                            f"excluded={result['excluded']}"), None
            return "", "search_issues が 10 回呼んでも done になりません"
        repo = re.search(r"`repo_root` = `([^`]*)`", text).group(1)
        stream = re.search(r"`stream` = `([^`]*)`", text).group(1)
        saved = failed = 0
        for item, cids, folder in re.findall(r"^\| (\S+) \| `([\d, ]+)` \| `([^`]*)` \|", text, re.MULTILINE):
            numbers = [int(c) for c in cids.split(",")]
            for _ in range(10):
                result = mcp.call("get_issues", {"repo_root": repo, "stream": stream, "cids": numbers,
                                                 "output_dir": folder})
                self.path_calls.append(result)
                if not result.ok:
                    failed += len(numbers)
                    break  # エラーの行は飛ばして次へ
                if result["done"]:
                    saved += len(result["saved"]) + len(result["already_saved"]) - len(result["failed"])
                    failed += len(result["failed"])
                    break
        return f"警告経路: 保存 {saved} / 取れなかった {failed}", None

    # ---- 作業 ----

    def plan_for(self, cids: List[int]) -> Tuple[str, str, Dict[str, Any]]:
        info = next((self.truth[c] for c in cids if c in self.truth), {"id": "", "verdict": "undetermined"})
        plan = dict(PLANS.get(info["id"], {}))
        if info["id"] in self.fixes:
            plan["fix"] = self.fixes[info["id"]]
        return info["id"], info["verdict"], plan

    def worker(self, path: Path, prompt: str) -> Tuple[str, Optional[str]]:
        brief = Brief(path)
        rounds = self.work_rounds[brief.item] = self.work_rounds.get(brief.item, 0) + 1
        cids = [w["cid"] for w in brief.warnings]
        truth_id, verdict, plan = self.plan_for(cids)
        if brief.progress:
            brief.progress.write_text(json.dumps({"item": brief.item, "phase": "read", "note": "警告経路を読んでいる"},
                                                 ensure_ascii=False), encoding="utf-8")
        judgement = verdict if verdict in ("true_bug", "false_positive", "intentional") else "undetermined"
        recommendation = "fix" if judgement in ("true_bug", "undetermined") else "deviation"
        already = bool(plan.get("already"))
        fix_requested = "結果ファイルの次の点を直してください" in prompt
        skip_fix = (set(cids) & self.first_without_fix) and not fix_requested and not brief.rework
        if not already and not skip_fix:
            if plan.get("fix"):
                apply_fix(brief.fix_dir, plan["fix"])
            else:  # 方針のない警告：警告の行の上に、直し方のメモを足す（足りない修正の案）
                w = brief.warnings[0]
                target = brief.fix_dir / w["file"]
                lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
                index = max(0, min(len(lines), (w["line"] or 1) - 1))
                marker = f"/* CID {w['cid']}: 直し方を確かめる */\n"
                if marker not in lines:
                    lines.insert(index, marker)
                    target.write_text("".join(lines), encoding="utf-8")
        evidence = [{"file": w["file"], "line": w["line"],
                     "note": "警告の場所: " + (code_line(brief.investigate, w["file"], w["line"]) or w["checker"])}
                    for w in brief.warnings]
        classification, action, severity = DEVIATION_ATTRS.get(judgement, ("Pending", "Undecided", "Unspecified"))
        summary = plan.get("summary") or f"{brief.warnings[0]['checker']} の警告を確かめた"
        result = {
            "item": brief.item,
            "verdict": {"judgement": judgement, "summary": summary,
                        "rationale": f"警告経路を順に確かめた（正解の記録 {truth_id or 'なし'}）。" + summary + "。",
                        "evidence": evidence},
            "recommendation": recommendation,
            "confidence": "high" if truth_id else "low",
            "confidence_reason": "警告経路をコードで最後まで追えた" if truth_id else "方針のない警告で、確かめきれていない",
            "deviation": {"classification": classification, "action": action, "severity": severity,
                          "comment": plan.get("deviation") or "誤検知。警告の経路はコードで成り立たない。"},
            "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Moderate",
                    "summary": plan.get("fix_summary") or ("最新のリビジョンで直っている" if already else "直し方のメモを足す"),
                    "impact": "警告の経路を通るときだけ動きが変わる" + ("（検証の指摘を確かめた）" if brief.rework else ""),
                    "exceeded_constraints": [], "already_fixed_on_latest": already},
            "revision_drift": {"status": "unknown" if brief.unknown_revision else "none", "detail": ""},
            "group_excluded_cids": [],
        }
        brief.result.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        if brief.progress:
            brief.progress.write_text(json.dumps({"item": brief.item, "phase": "result", "note": "結果を書いた"},
                                                 ensure_ascii=False), encoding="utf-8")
        label = "修正" if recommendation == "fix" else "逸脱"
        return f"{brief.item}: {label}を推奨（確信度 {'高' if truth_id else '低'}）— {summary}", None

    # ---- 統合 ----

    def integrator(self, path: Path) -> Tuple[str, Optional[str]]:
        text = path.read_text(encoding="utf-8")
        plan_file = Path(table_value(text, "計画（書く）"))
        rows = re.findall(r"^\| (\S+) \| ([\d, ]+) \| [^|]+ \| `[^`]*` \| \w+ \| \w+ \| \w+ \|", text, re.MULTILINE)
        items = {item: [int(c) for c in cids.split(",")] for item, cids in rows}
        members = [i for i, cids in items.items() if set(cids) <= FRAME_MAX_CIDS] if self.merge_frame_max else []
        merges = []
        if len(members) >= 2:
            members.sort(key=lambda i: (not i.startswith("G"), i))
            evidence = [{"file": "include/em4_config.h", "line": 40, "note": "FRAME_MAX の定義に CRC がない"}]
            merges.append({
                "members": members,
                "reason": "どれも FRAME_MAX（include/em4_config.h:40）に CRC の 2 バイトが入っていないことが原因",
                "fix_source": f"item:{members[0]}",
                "result": {
                    "verdict": {"judgement": "true_bug", "summary": "FRAME_MAX に CRC の 2 バイトが入っていない",
                                "rationale": "3 つの警告の配列はどれも FRAME_MAX の大きさで、フレーム全体（最大 70 バイト）が入らない。",
                                "evidence": evidence},
                    "recommendation": "fix", "confidence": "high",
                    "confidence_reason": "定義と 3 か所の使い方をコードで確かめた",
                    "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Major",
                                  "comment": "本物の不具合。ペイロードが 63 バイト以上のときだけあふれる（include/em4_config.h:40）。"},
                    "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Major",
                            "summary": "FRAME_MAX に FRAME_CRC_LEN を足す", "impact": "送受信のバッファが 2 バイト大きくなる",
                            "exceeded_constraints": [], "already_fixed_on_latest": False},
                    "revision_drift": {"status": "none", "detail": ""}}})
        plan_file.write_text(json.dumps({"merges": merges, "conflicts": []}, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        return ("統合: 1 つにまとめる（" + ", ".join(members) + "）") if merges else "統合: まとめるものはない", None

    # ---- 検証 ----

    def reviewer(self, path: Path) -> Tuple[str, Optional[str]]:
        text = path.read_text(encoding="utf-8")
        item = re.search(r"^# 検証の指示: (\S+)", text, re.MULTILINE).group(1)
        out = Path(table_value(text, "検証の結果（書く）"))
        rounds = self.review_rounds[item] = self.review_rounds.get(item, 0) + 1
        decided = (self.reviews(item, rounds) if self.reviews else None) or {"outcome": "pass"}
        evidence = re.search(r"^- 根拠の場所: `([^`]*)`:(\d+)", text, re.MULTILINE)
        findings = [{"check": "根拠の場所", "ok": True, "detail": "根拠の行に書かれたとおりのコードがある",
                     "file": evidence.group(1) if evidence else None,
                     "line": int(evidence.group(2)) if evidence else None}]
        review = {"item": item, "outcome": decided["outcome"], "findings": findings,
                  "confidence_cap": decided.get("cap"), "rework": decided.get("rework", []),
                  "note": decided.get("note", "根拠と経路の条件を確かめ、判定と修正は正しい")}
        out.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
        return f"{item}: {decided['outcome']} — {review['note']}", None


# ---- 統括（/coverity-run の手順の 2） ----------------------------------------------------------


class Orchestrator:
    """skills/coverity-run/SKILL.md の「2. ct.py tick に従って進める」のとおりに動く。"""

    def __init__(self, world: World, run_dir: str, actors: Actors, scene: str) -> None:
        self.world, self.run_dir, self.actors, self.scene = world, run_dir, actors, scene
        self.ticks: List[CtResult] = []
        self.commands: List[CtResult] = []
        self.mcp_calls: List[ToolResult] = []

    def tick(self, *extra: str, env: Optional[Dict[str, str]] = None) -> CtResult:
        result = self.world.ct("tick", "--run", self.run_dir, "--wait", "60", *extra, env=env)
        result.at = time.monotonic()  # type: ignore[attr-defined]
        self.ticks.append(result)
        if not result.get("ok", False):
            raise AssertionError(f"ct.py tick が ok: false: {result.get('error')}")
        return result

    def says(self) -> List[str]:
        return [s for t in self.ticks for s in t.get("say") or []]

    def stages(self) -> List[str]:
        return [t.get("stage") for t in self.ticks]

    def stage_seconds(self) -> List[Tuple[str, float]]:
        """tick の stage が変わるまでの秒数（段ごとの所要時間）。"""
        out: List[Tuple[str, float]] = []
        start = getattr(self, "started", None)
        for tick in self.ticks:
            stage = tick.get("stage")
            if not out or out[-1][0] != stage:
                out.append((stage, 0.0))
        spans: Dict[str, float] = {}
        previous_at, previous_stage = start, None
        for tick in self.ticks:
            at = getattr(tick, "at", None)
            if previous_at is not None and at is not None:
                stage = previous_stage or tick.get("stage")
                spans[stage] = spans.get(stage, 0.0) + (at - previous_at)
            previous_at, previous_stage = at, tick.get("stage")
        return [(stage, round(spans.get(stage, 0.0), 1)) for stage, _ in out]

    def go(self, first: Optional[CtResult] = None, max_ticks: int = 400,
           env: Optional[Dict[str, str]] = None,
           until: Optional[Callable[[CtResult], bool]] = None) -> CtResult:
        if not hasattr(self, "started"):
            self.started = time.monotonic()
        result = first or self.tick(env=env)
        running: List[Tuple[str, Optional[str]]] = []  # 裏で動いているエージェント（終わった順に知らせる）
        while len(self.ticks) < max_ticks:
            if result.get("stop") or (until is not None and until(result)):
                return result
            mcp_error: Optional[Tuple[str, str]] = None
            mcp_done = False
            for action in result.get("actions") or []:
                if action["type"] == "launch":
                    _, failure = self.actors.run(action)
                    running.append((action["task"], failure))
                elif action["type"] == "command":
                    args = re.findall(r'"([^"]*)"|(\S+)', action["command"])
                    words = [a or b for a, b in args][1:]  # 先頭の ct.py を除く
                    out = self.world.ct(*words)
                    self.commands.append(out)
                    running.append((action["task"], None if out.get("ok") else str(out.get("error"))))
                elif action["type"] == "mcp":
                    task = "collector:search" if action["tool"] == "search_issues" else "collector:paths"
                    for _ in range(10):
                        call = self.world.mcp().call(action["tool"], action["args"])
                        self.mcp_calls.append(call)
                        if not call.ok:
                            mcp_error = (task, call.error)
                            break
                        if not action.get("until_done") or call["done"]:
                            break
                    if mcp_error:
                        break
                    mcp_done = True
            if result.get("done"):
                return result
            if mcp_error:
                result = self.tick("--failed", mcp_error[0], "--reason", mcp_error[1][:500], env=env)
                continue
            if running:
                task, failure = running.pop(0)
                result = (self.tick("--failed", task, "--reason", failure, env=env) if failure
                          else self.tick("--finished", task, env=env))
                continue
            if mcp_done or result.get("wait") or result.get("stage") == "initial":
                result = self.tick(env=env)
                continue
            raise AssertionError(f"tick が、することも待つことも返しませんでした: {dict(result)}")
        raise AssertionError(f"tick を {max_ticks} 回呼んでも終わりません")
