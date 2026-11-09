# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0", "jsonschema>=4.18"]
# ///
"""Self test of the Coverity triage kit on this PC, written to one results folder.

The skill's procedure drives the test. Checks with fixed rules are judged here; what only the agent
or the person can see (tool and agent lists, replies of the Coverity tools, the slash-command menu)
is recorded with ``record``. Host names, user names and credentials are masked before anything is
written. Source code of in-house repositories is never recorded (only counts and results).

Commands (each prints exactly one JSON object on stdout; progress goes to stderr):

  start [--dir D] [--sections 1,2,3,4] [--repo R] [--client C]   make the results folder
  check-tools --dir D                                             1-7  uv and git
  sample --dir D                                                  2-1  sample repository (fake Coverity)
  check-run --dir D --run RUN                                     2-2..2-6  the batch run on the sample
  check-refused --dir D --refused yes|no [--actual T]             2-7  wrong confirmation token refused
  check-apply --dir D --run RUN                                   2-8  write-back and fix branches
  check-config --dir D                                            3-1  settings of the in-house repository
  check-build --dir D                                             4-1..4-3  trial build in a temporary copy
  record --dir D (--id ID --status S --actual T [--detail X] | --stdin)
  report --dir D                                                  masked report.md
"""

from __future__ import annotations

import argparse
import codecs
import getpass
import json
import ntpath
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"
SAMPLE = ASSETS / "sample-target"
EXPECTED = ASSETS / "expected.yaml"
RUN_SCHEMA = ASSETS / "run.schema.json"  # copies of the batch run's schemas, when present
APPROVALS_SCHEMA = ASSETS / "approvals.schema.json"
STATE_FILE = "selftest.json"
REPORT_FILE = "report.md"
CONFIG_DIR = ".coverity-triage"
CONFIG_FILE = "config.yaml"
WRONG_TOKEN = "000000000000"
TOOL_TIMEOUT = 10
LOG_LINES = 5  # lines of a failed build kept in the report (compiler source excerpts are dropped)

SECTIONS = {"1": "① 道具とエージェント", "2": "② 偽データでの一連の流れ", "3": "③ 社内の Coverity（読むだけ）",
            "4": "④ ビルドでの検証"}
STATUS_JA = {"pass": "成功", "fail": "失敗", "review": "要確認", "skip": "未実施", "info": "記録"}

# (id, title, what is expected, who judges)
CHECKS = [
    ("1-1", "Coverity との通信", "見本のリポジトリで、接続を確かめるツールが ok を返す", "エージェント"),
    ("1-2", "スラッシュコマンド", "`/` の一覧に、表のスキルが出る", "利用者"),
    ("1-3", "作業役", "表の作業役のエージェントが一覧にあり、説明に「作業役」がある", "エージェント"),
    ("1-4", "検証役", "表の検証役のエージェントが一覧にあり、説明に「検証役」がある", "エージェント"),
    ("1-5", "統合役", "表の統合役のエージェントが一覧にあり、説明に「統合役」がある", "エージェント"),
    ("1-6", "統括エージェント", "表の統括エージェントが一覧にある", "エージェント"),
    ("1-7", "道具", "uv と git がある", "スクリプト"),
    ("1-8", "Web管理画面のツール", "画面を開くツールがある（画面を使わないなら要らない）", "エージェント"),
    ("2-1", "見本のリポジトリ", "偽の Coverity の見本のリポジトリを作れる", "スクリプト"),
    ("2-2", "一括実行", "最後まで進み、一覧（summary.html）と承認（approvals.json）ができる", "スクリプト"),
    ("2-3", "作業", "すべての作業が終わる（エラーと未処理がない）", "スクリプト"),
    ("2-4", "AI の結論", "推奨が想定（expected.yaml）と合う（合わなくても要確認）", "スクリプト"),
    ("2-5", "グループ", "CID 20004〜20006 が1つのグループになる", "スクリプト"),
    ("2-6", "成果物と作業中のファイル", "結果・詳細レポート・差分がそろい、見本の作業中のファイルが変わらない", "スクリプト"),
    ("2-7", "書き戻しの安全策", f"間違った確認の文字列 {WRONG_TOKEN} では断られ、何も書き込まれない", "エージェントとスクリプト"),
    ("2-8", "反映", "逸脱が偽の Coverity に書き戻され、修正のブランチが push される", "スクリプト"),
    ("3-1", "設定", "対象のリポジトリの設定・条件ファイル・認証情報がそろう", "スクリプト"),
    ("3-2", "接続と認証", "接続を確かめるツールが 10 秒以内に ok を返す", "エージェント"),
    ("3-3", "警告の検索", "条件ファイルで 1 件の検索ができる", "エージェント"),
    ("3-4", "警告経路", "その 1 件の警告経路を取れる", "エージェント"),
    ("4-1", "試しのビルド", "設定のビルドのコマンドが、リポジトリのコピーで通る", "スクリプト"),
    ("4-2", "解析のコマンド", "cov-build と cov-analyze がある（ビルドでの検証か初回解析を使うとき）", "スクリプト"),
    ("4-3", "登録のコマンド", "cov-commit-defects がある（初回解析を使うとき。登録は試さない）", "スクリプト"),
]
CHECK_IDS = {c[0]: c for c in CHECKS}


class CliError(Exception):
    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


# ---- output ----------------------------------------------------------------------------------


def _json_escape(err: UnicodeError) -> tuple[str, int]:
    """Characters the console code page cannot write become JSON escapes (\\uXXXX)."""
    out = []
    for ch in err.object[err.start:err.end]:  # type: ignore[attr-defined]
        cp = ord(ch)
        if cp > 0xFFFF:
            cp -= 0x10000
            out.append("\\u%04x\\u%04x" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)))
        else:
            out.append("\\u%04x" % cp)
    return "".join(out), err.end  # type: ignore[attr-defined]


codecs.register_error("selftest_json_escape", _json_escape)


def stdout_encoding() -> str:
    """PYTHONIOENCODING if set; on Windows with a pipe, the console code page (PowerShell reads it); else UTF-8."""
    name = os.environ.get("PYTHONIOENCODING", "").split(":")[0].strip()
    if not name and sys.platform == "win32" and not sys.stdout.isatty():
        try:
            import ctypes

            page = ctypes.windll.kernel32.GetConsoleOutputCP()  # type: ignore[attr-defined]
            name = f"cp{page}" if page else ""
        except Exception:  # noqa: BLE001 - no console: fall back to UTF-8
            name = ""
    try:
        return codecs.lookup(name or "utf-8").name
    except LookupError:
        return "utf-8"


def emit(obj: dict[str, Any], code: int = 0) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    sys.stdout.buffer.write(text.encode(stdout_encoding(), errors="selftest_json_escape"))
    sys.stdout.flush()
    sys.exit(code)


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise CliError(f"コマンドの引数が正しくありません: {message}", [self.format_usage().strip()])


# ---- files -----------------------------------------------------------------------------------


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(read_text(path))


def load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(read_text(path))


def write_text(path: Path, text: str) -> None:
    """Write through a temporary file and os.replace (Windows: retry while another process holds the file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.2)


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def remove_tree(path: Path, attempts: int = 10, pause: float = 0.3) -> None:
    """Remove a folder. Read-only files (git objects on Windows) are made writable; a file that another
    process still holds (a sharing violation on Windows) is tried again a few times with a pause."""
    def on_error(func: Any, name: str, _exc: Any) -> None:
        for attempt in range(attempts):
            try:
                os.chmod(name, stat.S_IWRITE)
                func(name)
                return
            except FileNotFoundError:
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(pause)

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=on_error)
        else:
            shutil.rmtree(path, onerror=on_error)


def resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def home_relative(path: Path) -> str:
    """A path under the home folder as ``~/...``, so the results folder does not hold the user name."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def run(args: list[str], cwd: Path | None = None, check: bool = True, timeout: float = 120) -> str:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise CliError(f"コマンドが見つかりません: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"コマンドが {timeout:.0f} 秒で終わりませんでした: {' '.join(args[:3])}") from exc
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise CliError(f"コマンドが失敗しました（{proc.returncode}）: {' '.join(args[:4])}", [err[-500:]])
    return out


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-c", "core.longpaths=true", *args], cwd=repo, check=check)


def tool_version(command: list[str]) -> str | None:
    path = shutil.which(command[0])
    if not path:
        return None
    try:
        proc = subprocess.run([path, *command[1:]], capture_output=True, timeout=TOOL_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return path
    text = (proc.stdout or proc.stderr or b"").decode("utf-8", errors="replace").strip()
    return (text.splitlines() or [path])[0][:200]


# ---- credentials and masking -------------------------------------------------------------------


def usable(value: str | None) -> bool:
    """Empty values and values that arrived unexpanded (``${NAME}``) count as missing."""
    return bool(value) and not (value.startswith("${") and value.endswith("}"))


def user_env(name: str) -> str | None:
    """An environment variable; on Windows also the user's saved variables (HKCU\\Environment)."""
    value = os.environ.get(name)
    if usable(value):
        return value
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                stored, kind = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        stored = str(stored)
        if kind == getattr(winreg, "REG_EXPAND_SZ", 2):
            stored = ntpath.expandvars(stored)
        return stored if usable(stored) else None
    return None


def repo_config(repo: Path | None) -> dict[str, Any]:
    if not repo:
        return {}
    path = repo / CONFIG_DIR / CONFIG_FILE
    try:
        data = load_yaml(path) if path.is_file() else {}
    except Exception:  # noqa: BLE001 - broken settings are reported by check-config
        return {}
    return data if isinstance(data, dict) else {}


URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)(?:[^/\s@'\"<>|]+@)?([^/\s:'\"<>|)]+)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
LOCAL = {"localhost", "127.0.0.1"}


class Masker:
    """Replaces credentials, user names and host names with ****."""

    def __init__(self, repo: Path | None):
        cov = repo_config(repo).get("coverity") or {}
        cov = cov if isinstance(cov, dict) else {}
        key_env = str(cov.get("key_env") or "COV_AUTH_KEY")
        user_name_env = str(cov.get("user_env") or "COV_USER")
        self.secrets = [v for v in (user_env(key_env), user_env("COVERITY_PASSPHRASE")) if v and len(v) >= 4]
        self.users = {u for u in (getpass.getuser(), Path.home().name, user_env(user_name_env)) if u and len(u) >= 2}
        host = urlparse(str(cov.get("url") or "")).hostname
        self.hosts = {host} if host and host not in LOCAL else set()
        self.home = str(Path.home())

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "****")
        for home in {self.home, self.home.replace("\\", "/")}:
            if len(home) > 3:
                value = value.replace(home, "~")
        value = URL.sub(lambda m: m.group(0) if m.group(2).lower() in LOCAL else m.group(1) + "****", value)
        for host in sorted(self.hosts, key=len, reverse=True):
            value = re.sub(re.escape(host), "****", value, flags=re.I)
        value = EMAIL.sub("****@****", value)
        value = IPV4.sub(lambda m: m.group(0) if m.group(0) in LOCAL else "****", value)
        for user in sorted(self.users, key=len, reverse=True):
            value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", "****", value, flags=re.I)
        return value

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {str(k): self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        return value


# ---- results folder --------------------------------------------------------------------------


class Results:
    def __init__(self, folder: str | Path):
        self.dir = resolve(folder)
        if not (self.dir / STATE_FILE).is_file():
            raise CliError(f"自己診断の結果のフォルダではありません: {self.dir}", ["start で作ったフォルダ（dir）を渡します"])
        self.state = read_json(self.dir / STATE_FILE)
        self.repo = Path(self.state["repo_root"]).expanduser() if self.state.get("repo_root") else None
        self.mask = Masker(self.repo)

    @property
    def sample_repo(self) -> Path:
        if not self.state.get("sample_repo"):
            raise CliError("見本のリポジトリがまだありません", ["先に sample を動かします"])
        return Path(self.state["sample_repo"]).expanduser()

    def record(self, check_id: str, status: str, actual: str, detail: str = "") -> dict[str, Any]:
        if check_id not in CHECK_IDS:
            raise CliError(f"確かめる項目 {check_id} はありません", [", ".join(CHECK_IDS)])
        if status not in STATUS_JA:
            raise CliError(f"status は {' / '.join(STATUS_JA)} のどれかです")
        entry = {"status": status, "actual": self.mask(actual).strip(), "detail": self.mask(detail).strip(),
                 "at": datetime.now().isoformat(timespec="seconds")}
        self.state["checks"][check_id] = entry
        return {"id": check_id, "status": status, "actual": entry["actual"], **({"detail": entry["detail"]} if entry["detail"] else {})}

    def add_detail(self, check_id: str, text: str) -> dict[str, Any]:
        """Add a line to the detail of a check already recorded (masked like the rest)."""
        entry = self.state["checks"][check_id]
        entry["detail"] = "\n".join(part for part in (entry.get("detail", ""), self.mask(text).strip()) if part)
        return {"detail": entry["detail"]}

    def save(self) -> None:
        write_json(self.dir / STATE_FILE, self.state)


def need_repo(results: Results) -> Path:
    if not results.repo:
        raise CliError("対象のリポジトリが記録されていません", ["③④ は start に --repo を付けて始めます"])
    return results.repo


# ---- commands: start and ① ---------------------------------------------------------------------


def parse_sections(text: str) -> list[str]:
    for a, b in (("①", "1"), ("②", "2"), ("③", "3"), ("④", "4"), ("、", ","), (" ", ",")):
        text = text.replace(a, b)
    picked = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and len(part) > 1:  # "12" means ① and ②
            picked.update(part)
        else:
            picked.add(part)
    if not picked or any(s not in SECTIONS for s in picked):
        raise CliError("範囲は 1〜4（①〜④）を、カンマで区切って書きます（例 1,2）")
    return sorted(picked)


def cmd_start(folder: str | None, sections: str, repo: str | None, client: str) -> dict[str, Any]:
    picked = parse_sections(sections)
    repo_path = resolve(repo) if repo else None
    if ("3" in picked or "4" in picked) and not repo_path:
        raise CliError("③ と ④ には、対象のリポジトリ（--repo）が要ります")
    if repo_path and not repo_path.is_dir():
        raise CliError(f"フォルダがありません: {repo_path}")
    if folder:
        target = resolve(folder)
        if target.exists() and any(target.iterdir()):
            raise CliError(f"空でないフォルダです: {target}", ["新しいフォルダを渡すか、--dir を省きます"])
    else:
        base = Path.home() / f"{SKILL_DIR.name}-results"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = base / stamp
        n = 1
        while target.exists():
            n += 1
            target = base / f"{stamp}-{n}"
    target.mkdir(parents=True, exist_ok=True)
    state = {
        "version": 1, "started_at": datetime.now().isoformat(timespec="seconds"), "sections": picked,
        "client": client, "repo_root": home_relative(repo_path) if repo_path else None,
        "environment": {"os": platform.platform(), "python": platform.python_version(),
                        "uv": tool_version(["uv", "--version"]) or "見つかりません",
                        "git": tool_version(["git", "--version"]) or "見つかりません",
                        "svn": tool_version(["svn", "--version", "--quiet"]) or "見つかりません"},
        "checks": {},
    }
    write_json(target / STATE_FILE, state)
    results = Results(target)
    for check_id, *_ in CHECKS:
        if check_id[0] not in picked:
            results.record(check_id, "skip", "範囲外")
    results.save()
    return {"ok": True, "dir": str(target), "sections": {s: SECTIONS[s] for s in picked},
            "checks": [{"id": c[0], "title": c[1], "expected": c[2], "who": c[3]} for c in CHECKS if c[0][0] in picked]}


def cmd_check_tools(folder: str) -> dict[str, Any]:
    results = Results(folder)
    env = results.state["environment"]
    uv, gitv = tool_version(["uv", "--version"]), tool_version(["git", "--version"])
    env.update({"uv": uv or "見つかりません", "git": gitv or "見つかりません"})
    missing = [name for name, found in (("uv", uv), ("git", gitv)) if not found]
    out = results.record("1-7", "fail" if missing else "pass",
                         f"uv: {uv or '見つかりません'}、git: {gitv or '見つかりません'}、svn: {env.get('svn')}",
                         f"見つからない: {'、'.join(missing)}。入れて PATH を通し、Copilot CLI を起動し直します" if missing else "")
    results.save()
    return {"ok": True, "checks": [out]}


# ---- commands: ② ------------------------------------------------------------------------------


def writes_file(repo: Path) -> Path | None:
    cov = repo_config(repo).get("coverity") or {}
    fake = cov.get("fake_data") if isinstance(cov, dict) else None
    if not fake:
        return None
    path = Path(str(fake))
    path = path if path.is_absolute() else repo / CONFIG_DIR / path
    return Path(str(path) + ".writes.jsonl")


def read_writes(repo: Path) -> list[dict[str, Any]]:
    path = writes_file(repo)
    if not path or not path.is_file():
        return []
    lines = []
    for line in read_text(path).splitlines():
        if line.strip():
            try:
                lines.append(json.loads(line))
            except ValueError:
                lines.append({"broken": line[:100]})
    return lines


def cmd_sample(folder: str) -> dict[str, Any]:
    results = Results(folder)
    if not SAMPLE.is_dir() or not EXPECTED.is_file():
        raise CliError(f"見本がありません: {SAMPLE}", ["このスキルのフォルダの assets/ を確かめます"])
    repo = results.dir / "sample-target"
    origin = results.dir / "sample-origin.git"
    out = results.dir / "coverity-triage-out"  # the default output_dir (next to the repository)
    for path in (repo, origin, out):
        remove_tree(path)
    progress("見本のリポジトリを作っています")
    shutil.copytree(SAMPLE, repo)
    run(["git", "init", "-q", "--bare", str(origin)])
    git(repo, "init", "-q")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, value in (("user.name", "coverity-selftest"), ("user.email", "selftest@localhost"),
                       ("core.autocrlf", "false"), ("core.longpaths", "true")):
        git(repo, "config", key, value)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "見本")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "-u", "origin", "main")
    results.state["sample_repo"] = home_relative(repo)
    rec = results.record("2-1", "pass", "見本のリポジトリを作りました（git、偽の Coverity、push 先あり）")
    results.save()
    return {"ok": True, "repo_root": str(repo), "filter": "all.yaml", "output_dir": str(out), "checks": [rec]}


def schema_problems(schema_file: Path, data: Any) -> list[str] | None:
    """Problems of data against a schema copy in assets/; None when the copy is not there."""
    if not schema_file.is_file():
        return None
    from jsonschema.validators import validator_for

    schema = json.loads(read_text(schema_file))
    validator = validator_for(schema)(schema)
    return [f"{'/'.join(map(str, e.absolute_path)) or '(全体)'}: {e.message}" for e in validator.iter_errors(data)][:10]


def run_items(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = read_json(run_dir / "run.json")
    if not isinstance(data, dict):
        raise CliError(f"実行フォルダではありません（run.json がない）: {run_dir}", ["一括実行が作った実行フォルダを渡します"])
    items = [i for i in data.get("items") or [] if isinstance(i, dict) and i.get("id") is not None]
    return data, items


def cmd_check_run(folder: str, run_arg: str) -> dict[str, Any]:
    results = Results(folder)
    repo = results.sample_repo
    run_dir = resolve(run_arg)
    data, items = run_items(run_dir)
    recs = []

    # 2-2 the run reached its end
    missing = [name for name in ("summary.html", "approvals.json") if not (run_dir / name).is_file()]
    problems = [f"{name} がありません" for name in missing]
    problems += [f"run.json の形: {p}" for p in schema_problems(RUN_SCHEMA, data) or []]
    if data.get("halted"):
        problems.append("実行が止まっています（続けられない理由: "
                        f"{str(data.get('halt_reason') or '').strip() or '記録がありません'}）")  # record() masks it
    if data.get("stopped"):
        problems.append("実行が途中で止められています（run.json の stopped が true）")
    elif data.get("finished") is not True and not data.get("halted"):
        problems.append("実行が最後まで進んでいません（run.json の finished が true ではありません）")
    if "approvals.json" not in missing:
        approvals = read_json(run_dir / "approvals.json")
        problems += [f"approvals.json の形: {p}" for p in schema_problems(APPROVALS_SCHEMA, approvals) or []]
    recs.append(results.record("2-2", "fail" if problems else "pass",
                               f"作業 {len(items)} 件" + ("、一覧と承認ができました" if not missing else ""),
                               "\n".join(problems)))

    # 2-3 every item finished
    unfinished = [f"{i['id']}: {i.get('status')}" + (f"（{i['error']}）" if i.get("error") else "")
                  for i in items if i.get("status") not in ("done", "merged")]
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.get("status"))] = counts.get(str(item.get("status")), 0) + 1
    recs.append(results.record("2-3", "fail" if unfinished or not items else "pass",
                               "、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "作業がありません",
                               "\n".join(unfinished)))

    # 2-4 recommendations as expected (AI results vary: a mismatch is only "review")
    expected = load_yaml(EXPECTED) or {}
    mismatches = []
    for cid, want in (expected.get("items") or {}).items():
        result = read_json(run_dir / "items" / str(cid) / "result.json")
        if not isinstance(result, dict):
            mismatches.append(f"{cid}: 結果がありません")
        elif result.get("recommendation") != want.get("recommendation"):
            mismatches.append(f"{cid}: 推奨 {result.get('recommendation')}（想定 {want.get('recommendation')}: {want.get('reason')}）")
    recs.append(results.record("2-4", "review" if mismatches else "pass",
                               "想定どおり" if not mismatches else f"想定と違う結論が {len(mismatches)} 件",
                               "\n".join(mismatches)))

    # 2-5 the group (made by the script's rule: same checker and function)
    group = set((expected.get("group") or {}).get("cids") or [])
    exact = [i for i in items if set(i.get("cids") or []) == group]
    partial = [i for i in items if len(set(i.get("cids") or []) & group) >= 2]
    groups = [f"{i['id']}: {i.get('cids')}" for i in items if len(i.get("cids") or []) > 1]
    recs.append(results.record("2-5", "pass" if exact else ("review" if partial else "fail"),
                               "グループ: " + ("、".join(groups) or "なし"),
                               "" if exact else ("グループの一部が外れています（AI が別の原因と見た）" if partial
                                                 else "CID 20004〜20006 がグループになっていません")))

    # 2-6 files of each finished item, and the sample's working files unchanged
    hard, soft = [], []
    for item in items:
        if item.get("status") != "done":
            continue
        item_id = str(item["id"])
        result = read_json(run_dir / "items" / item_id / "result.json")
        if not isinstance(result, dict) or not {"verdict", "recommendation", "confidence", "deviation", "fix"} <= set(result):
            hard.append(f"{item_id}: result.json がないか、形が足りません")
            continue
        if not (run_dir / "cid" / f"{item_id}.html").is_file():
            hard.append(f"{item_id}: 詳細レポート cid/{item_id}.html がありません")
        already = bool((result.get("fix") or {}).get("already_fixed_on_latest"))
        if not already and not (run_dir / "patches" / f"{item_id}-fix.patch").is_file():
            soft.append(f"{item_id}: 差分 patches/{item_id}-fix.patch がありません")
    changed = git(repo, "status", "--porcelain", check=False)
    if changed:
        hard.append("見本の作業中のファイルが変わりました: " + changed.replace("\n", "、")[:300])
    recs.append(results.record("2-6", "fail" if hard else ("review" if soft else "pass"),
                               "そろっています" if not hard and not soft else f"{len(hard) + len(soft)} 件の問題",
                               "\n".join(hard + soft)))
    results.state["sample_run"] = home_relative(run_dir)
    results.save()
    return {"ok": True, "checks": recs}


def cmd_check_refused(folder: str, refused: str, actual: str) -> dict[str, Any]:
    results = Results(folder)
    writes = read_writes(results.sample_repo)
    said_refused = refused == "yes"
    if writes:
        status, text = "fail", f"偽の Coverity に {len(writes)} 件書き込まれました（断られるはずでした）"
    elif not said_refused:
        status, text = "fail", "書き戻しのツールが、間違った確認の文字列を断りませんでした"
    else:
        status, text = "pass", "断られ、何も書き込まれていません"
    rec = results.record("2-7", status, text, actual)
    results.save()
    return {"ok": True, "checks": [rec], "writes": len(writes)}


def cmd_check_apply(folder: str, run_arg: str) -> dict[str, Any]:
    results = Results(folder)
    repo = results.sample_repo
    run_dir = resolve(run_arg)
    plan = read_json(run_dir / "apply-plan.json")
    if not isinstance(plan, dict):
        raise CliError("反映の計画（apply-plan.json）がありません", ["反映のスキルの手順で反映してから呼びます"])
    written: set[int] = set()
    for line in read_writes(repo):
        written.update(int(c) for c in line.get("cids") or [] if isinstance(c, (int, str)) and str(c).isdigit())
    triage = [e for e in plan.get("triage") or [] if isinstance(e, dict)]
    not_written = [str(e.get("id")) for e in triage if not set(e.get("cids") or []) <= written]
    remote = git(repo, "ls-remote", "--heads", "origin", check=False)
    branches = [str(c["branch"]) for c in plan.get("code") or [] if isinstance(c, dict) and c.get("branch")]
    unpushed = [b for b in branches if f"refs/heads/{b}" not in remote]
    failed = (read_json(run_dir / "apply-plan.result.json", {}) or {}).get("failed") or {}
    failed = list(failed.values()) if isinstance(failed, dict) else list(failed)
    if not triage and not branches:
        status = "review"
    else:
        status = "fail" if not_written or unpushed or failed else "pass"
    detail = [f"書き戻されていない: {i}" for i in not_written] + [f"push されていない: {b}" for b in unpushed]
    detail += [f"書き戻しの失敗: {v.get('id')}: {v.get('error')}" for v in failed if isinstance(v, dict)]
    rec = results.record("2-8", status,
                         f"逸脱の書き戻し {len(triage) - len(not_written)}/{len(triage)}、"
                         f"push したブランチ {len(branches) - len(unpushed)}/{len(branches)}"
                         + ("（反映するものがありません）" if status == "review" else ""),
                         "\n".join(detail))
    results.save()
    return {"ok": True, "checks": [rec]}


# ---- commands: ③ and ④ -------------------------------------------------------------------------


def cmd_check_config(folder: str) -> dict[str, Any]:
    results = Results(folder)
    repo = need_repo(results)
    problems, notes = [], []
    config_path = repo / CONFIG_DIR / CONFIG_FILE
    try:
        config = load_yaml(config_path) or {} if config_path.is_file() else {}
        if not config_path.is_file():
            problems.append(f"{CONFIG_DIR}/{CONFIG_FILE} がありません（準備のスキルで準備します）")
    except Exception as exc:  # noqa: BLE001
        config = {}
        problems.append(f"設定ファイルの書式が正しくありません: {str(exc).splitlines()[0]}")
    config = config if isinstance(config, dict) else {}
    cov = config.get("coverity") or {}
    cov = cov if isinstance(cov, dict) else {}
    if not cov.get("url"):
        problems.append("coverity.url がありません")
    elif str(cov["url"]).lower().startswith("http://"):
        notes.append("URL が http です（認証キーが暗号化されずに送られます）")
    fake = cov.get("api") == "fake"
    if fake:
        notes.append("偽の Coverity を使う設定です（社内の Coverity は確かめられません）")
    for label, name in (("ユーザー名", str(cov.get("user_env") or "COV_USER")),
                        ("認証キー", str(cov.get("key_env") or "COV_AUTH_KEY"))):
        if not fake and not user_env(name):
            problems.append(f"{label}の環境変数 {name} がありません")
    out = Path(str(config.get("output_dir") or "../coverity-triage-out")).expanduser()
    out = (out if out.is_absolute() else repo / out).resolve()
    if is_inside(out, repo):
        problems.append("結果の置き場所（output_dir）がリポジトリの中です")
    filters = []
    for path in sorted((repo / CONFIG_DIR / "filters").glob("*.yaml")):
        try:
            spec = load_yaml(path) or {}
        except Exception:  # noqa: BLE001
            problems.append(f"条件ファイル {path.name} の書式が正しくありません")
            continue
        if isinstance(spec, dict) and isinstance(spec.get("streams"), list) and len(spec["streams"]) == 1:
            filters.append(str(path))
        else:
            problems.append(f"条件ファイル {path.name} のストリームが 1 つではありません")
    if not filters:
        problems.append("使える条件ファイルがありません")
    status = "fail" if problems else ("review" if notes else "pass")
    rec = results.record("3-1", status, "そろっています" if status == "pass" else f"{len(problems) + len(notes)} 件",
                         "\n".join(problems + notes))
    results.save()
    return {"ok": True, "checks": [rec], "repo_root": str(repo), "filter_files": filters}


SOURCE_EXCERPT = re.compile(r"^\s*(\d+\s*)?\|")  # compiler lines that quote the source ("  12 | x = y;")


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    """Stop a process and its children (taskkill /T on Windows, the process group elsewhere)."""
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def log_tail(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip() and not SOURCE_EXCERPT.match(line)]
    return "\n".join(line[:200] for line in lines[-LOG_LINES:])


def copy_repository(repo: Path, target: Path) -> None:
    if (repo / ".git").exists() and shutil.which("git"):
        run(["git", "-c", "core.longpaths=true", "clone", "-q", "--shared", str(repo), str(target)], timeout=600)
    else:
        shutil.copytree(repo, target, symlinks=True)


def cmd_check_build(folder: str) -> dict[str, Any]:
    results = Results(folder)
    repo = need_repo(results)
    config_path = repo / CONFIG_DIR / CONFIG_FILE
    try:
        config = (load_yaml(config_path) if config_path.is_file() else {}) or {}
    except Exception as exc:  # noqa: BLE001 - every YAML error is reported the same way
        rec = results.record("4-1", "fail", "設定ファイルの書式が正しくありません", str(exc).splitlines()[0])
        results.save()
        return {"ok": True, "checks": [rec]}
    config = config if isinstance(config, dict) else {}
    verify = config.get("verify") or {}
    verify = verify if isinstance(verify, dict) else {}
    build = str(verify.get("build_command") or "").strip()
    if not build:
        recs = [results.record(i, "skip", "ビルドのコマンド（verify.build_command）の設定がありません")
                for i in ("4-1", "4-2", "4-3")]
        results.save()
        return {"ok": True, "checks": recs}
    setup = str(verify.get("setup_command") or "").strip()
    timeout = int(verify.get("timeout_minutes") or 180) * 60
    work = Path(tempfile.mkdtemp(prefix="cst"))
    recs = []
    leftover = ""
    try:
        root = work / "r"
        progress("リポジトリのコピーを作っています")
        copy_repository(repo, root)
        cwd = root / str(verify.get("build_dir") or "")
        command = build if not setup else f"{setup.replace('{root}', str(root))} && {build}"
        cov = config.get("coverity") if isinstance(config.get("coverity"), dict) else {}
        hidden = {"COVERITY_PASSPHRASE", str(cov.get("key_env") or "COV_AUTH_KEY")}  # the build gets no key
        env = {k: v for k, v in os.environ.items() if k not in hidden}
        progress("試しにビルドしています（時間がかかることがあります）")
        started = time.monotonic()
        # The output goes to a file and the build runs in its own process group, so that a build that
        # does not end can be stopped with all its children (a pipe would wait for every grandchild).
        log = work / "build.log"
        group: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW}
                                 if sys.platform == "win32" else {"start_new_session": True})
        try:
            with open(log, "wb") as handle:
                proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env, **group)
                try:
                    code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    kill_tree(proc)
                    proc.wait()
                    code = None
            seconds = time.monotonic() - started
            output = log.read_bytes().decode("utf-8", errors="replace")
            if code is None:
                recs.append(results.record("4-1", "fail", f"{timeout // 60} 分で終わりませんでした（止めました）",
                                           "入力待ちで止まっていないか、verify.timeout_minutes が短すぎないか確かめます"))
            elif code == 0:
                recs.append(results.record("4-1", "pass", f"ビルドが通りました（{seconds:.0f} 秒）"))
            else:
                recs.append(results.record("4-1", "fail", f"ビルドが失敗しました（終了コード {code}、{seconds:.0f} 秒）",
                                           log_tail(output)))
        except OSError as exc:
            recs.append(results.record("4-1", "fail", f"ビルドを始められませんでした: {exc}"))
    except CliError as exc:
        recs.append(results.record("4-1", "fail", f"リポジトリのコピーを作れませんでした: {exc}"))
    finally:
        results.save()  # the build's result is kept even if the cleanup below cannot finish
        try:
            remove_tree(work)
        except OSError as exc:
            # A process the build left behind (for example mspdbsrv.exe) may still hold files in the copy.
            leftover = (f"一時フォルダを消せませんでした（ビルドが残したプロセスが使っているかもしれません）。"
                        f"あとで手で消します: {work} ({exc})")
    if leftover:
        for rec in recs:
            if rec["id"] == "4-1":
                rec.update(results.add_detail("4-1", leftover))
    initial = bool(verify.get("initial"))
    needs_analysis = initial or str(verify.get("default") or "none") != "none"
    lacking = "review" if setup else "fail"  # the setup command may add the tools to PATH
    found = {name: shutil.which(name) for name in ("cov-build", "cov-analyze", "cov-commit-defects")}
    missing = [n for n in ("cov-build", "cov-analyze") if not found[n]]
    if not missing:
        recs.append(results.record("4-2", "pass", "cov-build と cov-analyze があります"))
    else:
        recs.append(results.record("4-2", lacking if needs_analysis else "info", f"見つかりません: {'、'.join(missing)}",
                                   "Coverity の bin フォルダを PATH に足します" if needs_analysis
                                   else "ビルドでの検証も初回解析も使わない設定なので、今は要りません"))
    if found["cov-commit-defects"]:
        recs.append(results.record("4-3", "pass", "cov-commit-defects があります（登録は試していません）"))
    else:
        recs.append(results.record("4-3", lacking if initial else "info", "cov-commit-defects が見つかりません",
                                   "初回解析を使う設定です。Coverity の bin フォルダを PATH に足します" if initial
                                   else "初回解析を使わない設定なので、今は要りません"))
    results.save()
    return {"ok": True, "checks": recs}


# ---- record and report -----------------------------------------------------------------------


def cmd_record(folder: str, check_id: str | None, status: str | None, actual: str | None, detail: str,
               from_stdin: bool) -> dict[str, Any]:
    if from_stdin:
        raw = sys.stdin.buffer.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode(sys.getfilesystemencoding() if sys.platform != "win32" else "mbcs", errors="replace")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise CliError(f"標準入力の JSON が正しくありません: {exc}", ['{"id": "1-3", "status": "pass", "actual": "…"}']) from exc
        if not isinstance(data, dict):
            raise CliError("標準入力は JSON のオブジェクトにします")
        check_id, status = data.get("id"), data.get("status")
        actual, detail = str(data.get("actual") or ""), str(data.get("detail") or "")
    if not check_id or not status or actual is None:
        raise CliError("--id と --status と --actual が要ります（または --stdin）")
    results = Results(folder)
    rec = results.record(str(check_id), str(status), actual, detail)
    results.save()
    return {"ok": True, "checks": [rec]}


def render_report(state: dict[str, Any], mask: Masker) -> str:
    env = state.get("environment") or {}
    checks = state.get("checks") or {}
    counts: dict[str, int] = {}
    for entry in checks.values():
        label = STATUS_JA.get(entry.get("status"), "?")
        counts[label] = counts.get(label, 0) + 1
    pending = [c[0] for c in CHECKS if c[0] not in checks]
    summary = "、".join(f"{label} {counts.get(label, 0)}" for label in STATUS_JA.values())
    out = ["# 自己診断の報告", "",
           f"- 始めた時刻: {state.get('started_at')}" + (f" ／ 終えた時刻: {state['finished_at']}" if state.get("finished_at") else ""),
           f"- 使っているもの: {state.get('client') or '-'}",
           f"- OS: {env.get('os')} ／ Python: {env.get('python')}",
           f"- uv: {env.get('uv')} ／ git: {env.get('git')} ／ svn: {env.get('svn')}",
           f"- 結果: {summary}、記録なし {len(pending)}", ""]
    for section, title in SECTIONS.items():
        out += [f"## {title}", "", "| 項目 | 確かめること | 結果 | 見たこと |", "| --- | --- | --- | --- |"]
        details = []
        for check_id, name, expected, _who in CHECKS:
            if check_id[0] != section:
                continue
            entry = checks.get(check_id)
            status = STATUS_JA.get(entry["status"], "?") if entry else "（記録なし）"
            actual = (entry or {}).get("actual", "").replace("|", "\\|").replace("\n", " ")
            out.append(f"| {check_id} {name} | {expected} | {status} | {actual} |")
            if entry and entry.get("detail"):
                details.append(f"- {check_id}: " + entry["detail"].replace("\n", "\n  "))
        out += [""] + (details + [""] if details else [])
    out += ["ホスト名・ユーザー名・認証情報は `****` に伏せてあります。社内のソースコードは記録していません。",
            "ストリーム名やファイルのパスは残るので、確かめてから共有してください。", ""]
    return mask("\n".join(out))


def cmd_report(folder: str) -> dict[str, Any]:
    results = Results(folder)
    results.state["finished_at"] = datetime.now().isoformat(timespec="seconds")
    results.save()
    write_text(results.dir / REPORT_FILE, render_report(results.state, results.mask))
    checks = results.state["checks"]
    counts: dict[str, int] = {}
    for entry in checks.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1

    def listed(status: str) -> list[dict[str, str]]:
        return [{"id": c[0], "title": c[1], "actual": checks[c[0]]["actual"]}
                for c in CHECKS if checks.get(c[0], {}).get("status") == status]

    return {"ok": True, "report": str(results.dir / REPORT_FILE), "counts": counts,
            "failed": listed("fail"), "review": listed("review"),
            "not_recorded": [c[0] for c in CHECKS if c[0] not in checks]}


# ---- command line ----------------------------------------------------------------------------


def build_parser() -> Parser:
    parser = Parser(prog="selftest.py", description="Coverity トリアージの自己診断")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    p = sub.add_parser("start", help="結果のフォルダを作る")
    p.add_argument("--dir", help="結果のフォルダ（既定 ~/<このスキルのフォルダの名前>-results/<日時>）")
    p.add_argument("--sections", default="1,2,3,4")
    p.add_argument("--repo", help="③④ で確かめるリポジトリ")
    p.add_argument("--client", default="")
    for name in ("check-tools", "sample", "check-config", "check-build", "report"):
        sub.add_parser(name).add_argument("--dir", required=True)
    for name in ("check-run", "check-apply"):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True)
        p.add_argument("--run", required=True, help="見本の実行フォルダ")
    p = sub.add_parser("check-refused")
    p.add_argument("--dir", required=True)
    p.add_argument("--refused", required=True, choices=["yes", "no"], help="書き戻しのツールが断ったか")
    p.add_argument("--actual", default="", help="ツールの返事の要点")
    p = sub.add_parser("record")
    p.add_argument("--dir", required=True)
    p.add_argument("--id")
    p.add_argument("--status", choices=list(STATUS_JA))
    p.add_argument("--actual")
    p.add_argument("--detail", default="")
    p.add_argument("--stdin", action="store_true", help='標準入力の JSON {"id","status","actual","detail"} で渡す')
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    c = args.command
    if c == "start":
        return cmd_start(args.dir, args.sections, args.repo, args.client)
    if c == "check-tools":
        return cmd_check_tools(args.dir)
    if c == "sample":
        return cmd_sample(args.dir)
    if c == "check-run":
        return cmd_check_run(args.dir, args.run)
    if c == "check-refused":
        return cmd_check_refused(args.dir, args.refused, args.actual)
    if c == "check-apply":
        return cmd_check_apply(args.dir, args.run)
    if c == "check-config":
        return cmd_check_config(args.dir)
    if c == "check-build":
        return cmd_check_build(args.dir)
    if c == "record":
        return cmd_record(args.dir, args.id, args.status, args.actual, args.detail, args.stdin)
    return cmd_report(args.dir)


def main(argv: list[str] | None = None) -> None:
    try:
        sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    except AttributeError:
        pass
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        emit({"ok": True, "help": parser.format_help()})
    try:
        result = dispatch(parser.parse_args(argv))
    except CliError as exc:
        emit({"ok": False, "error": str(exc), "problems": exc.problems}, 1)
    except Exception as exc:  # noqa: BLE001 - never a traceback
        emit({"ok": False, "error": f"自己診断のスクリプトが失敗しました: {type(exc).__name__}: {exc}", "problems": []}, 1)
    emit(result)


if __name__ == "__main__":
    main()
