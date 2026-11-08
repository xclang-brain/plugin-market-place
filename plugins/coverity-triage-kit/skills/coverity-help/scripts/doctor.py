# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0", "jsonschema>=4.18"]
# ///
"""Read-only checks used to answer questions about Coverity triage.

Commands (each prints exactly one JSON object on stdout; progress goes to stderr):

  check [--repo R]   tools (uv, git, svn, Coverity analysis commands), the settings in
                     R/.coverity-triage/, credential environment variables (values are never shown),
                     the output folder, and a summary of the run folders in it.
  run --run D        the state of one run folder (from its run.json).

Nothing is written: no files are created and no settings are changed.
"""

from __future__ import annotations

import argparse
import codecs
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
RUN_SCHEMA = SKILL_DIR / "assets" / "run.schema.json"  # copy of the batch run's schema, when present
CONFIG_DIR = ".coverity-triage"
CONFIG_FILE = "config.yaml"
FILTERS_DIR = "filters"
RUN_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
RUN_FORMAT = 2
MAX_RUNS = 10  # newest run folders listed by `check`
LONG_OUTPUT_DIR = 100  # run folders add about 60 characters (run id, work/w<n>, items/<ID>/...) before repo paths
VERSION_TIMEOUT = 10

IMPACTS = ["High", "Medium", "Low", "Unspecified"]
CLASSIFICATIONS = ["Unclassified", "Pending", "False Positive", "Intentional", "Bug"]
ACTIONS = ["Undecided", "Fix Required", "Fix Submitted", "Modeling Required", "Ignore"]
STATUSES = ["New", "Triaged", "Dismissed", "Absent Dismissed", "Fixed"]

# Keys of config.yaml (design 4.1): kind and default. Readers ignore keys they do not know.
STR, BOOL, INT, LIST = "str", "bool", "int", "list"
SCHEMA: dict[str, Any] = {
    "coverity": {
        "url": (STR, None), "api": (("auto", "fake"), "auto"), "user_env": (STR, "COV_USER"),
        "key_env": (STR, "COV_AUTH_KEY"), "triage_store": (STR, "Default Triage Store"),
        "revision_field": (STR, "sourceVersion"), "path_strip_prefixes": (LIST, []), "ca_file": (STR, ""),
        "fake_data": (STR, ""),
    },
    "vcs": {"type": (("git", "svn"), None), "base_branch": (STR, None), "branch_prefix": (STR, "coverity-fix/")},
    "output_dir": (STR, "../coverity-triage-out"),
    "max_items": ((INT, 1, None), 100),
    "parallel": ((INT, 1, 8), 3),
    "options": {name: (BOOL, False) for name in
                ("annotation", "per_run_branch", "knowledge_suggestions", "metrics", "markdown")},
    "verify": {
        "default": (("none", "build", "build+analyze"), "none"), "initial": (BOOL, False),
        "setup_command": (STR, ""), "build_dir": (STR, ""), "build_command": (STR, ""),
        "cov_build_args": (STR, "--dir idir"), "cov_analyze_args": (STR, "--dir idir --all"),
        "cov_commit_args": (STR, ""), "timeout_minutes": ((INT, 1, None), 180),
    },
    "agents": {"integration": (BOOL, False), "review": (BOOL, False), "max_rework": ((INT, 0, 2), 1)},
}

STATUS_LABEL = {"ok": "ok", "ng": "ng", "warning": "warning", "info": "info"}


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


codecs.register_error("doctor_json_escape", _json_escape)


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
    data = text.encode(stdout_encoding(), errors="doctor_json_escape")
    sys.stdout.buffer.write(data)
    sys.stdout.flush()
    sys.exit(code)


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise CliError(f"コマンドの引数が正しくありません: {message}", [self.format_usage().strip()])


# ---- small helpers ---------------------------------------------------------------------------


def check(name: str, status: str, detail: str, fix: str = "") -> dict[str, str]:
    entry = {"check": name, "status": STATUS_LABEL[status], "detail": detail}
    if fix:
        entry["fix"] = fix
    return entry


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(read_text(path))


def usable(value: str | None) -> bool:
    """Empty values and values that arrived unexpanded (``${NAME}``) count as missing."""
    return bool(value) and not (value.startswith("${") and value.endswith("}"))


def user_env(name: str) -> tuple[str | None, str | None]:
    """(value, where) of an environment variable; on Windows also the user's saved variables (HKCU\\Environment).

    Values saved with [Environment]::SetEnvironmentVariable(..., 'User') are seen without restarting.
    """
    value = os.environ.get(name)
    if usable(value):
        return value, "process"
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                stored, kind = winreg.QueryValueEx(key, name)
        except OSError:
            return None, None
        stored = str(stored)
        if kind == getattr(winreg, "REG_EXPAND_SZ", 2):
            stored = ntpath.expandvars(stored)  # %NAME% as Windows expands it
        if usable(stored):
            return stored, "user"
    return None, None


def tool_version(command: list[str]) -> str | None:
    """First line of a version command, or None when the command is not there."""
    path = shutil.which(command[0])
    if not path:
        return None
    try:
        proc = subprocess.run([path, *command[1:]], capture_output=True, timeout=VERSION_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return path
    raw = proc.stdout or proc.stderr or b""
    text = raw.decode("utf-8", errors="replace").strip()
    return (text.splitlines() or [path])[0][:200]


def is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


# ---- settings --------------------------------------------------------------------------------


def _check_value(key: str, kind: Any, value: Any) -> str | None:
    """A Japanese problem text, or None when the value is acceptable. Empty values mean the default."""
    if value is None:
        return None
    if isinstance(kind, tuple) and kind and kind[0] == INT:
        _, low, high = kind
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{key} は整数にします（今は {value!r}）"
        if value < low or (high is not None and value > high):
            span = f"{low}〜{high}" if high is not None else f"{low} 以上"
            return f"{key} は {span} にします（今は {value}）"
        return None
    if isinstance(kind, tuple):
        return None if value in kind else f"{key} は {' / '.join(kind)} のどれかにします（今は {value!r}）"
    if kind == BOOL:
        return None if isinstance(value, bool) else f"{key} は true か false にします（今は {value!r}）"
    if kind == STR:
        return None if isinstance(value, (str, int, float)) and not isinstance(value, bool) \
            else f"{key} は文字にします（今は {value!r}）"
    if kind == LIST:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return f"{key} は文字のリストにします（例 [\"C:/build/\"]）"
    return None


def effective_settings(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """(values with defaults, problems, unknown keys)."""
    values: dict[str, Any] = {}
    problems: list[str] = []
    unknown: list[str] = []
    for key, spec in SCHEMA.items():
        given = raw.get(key)
        if isinstance(spec, dict):
            if given is not None and not isinstance(given, dict):
                problems.append(f"{key} は項目の集まり（{key}: の下に字下げして書く）にします")
                given = {}
            given = given or {}
            values[key] = {}
            for sub, (kind, default) in spec.items():
                problem = _check_value(f"{key}.{sub}", kind, given.get(sub))
                if problem:
                    problems.append(problem)
                value = given.get(sub)
                values[key][sub] = default if value in (None, "") and default is not None else value
            unknown += [f"{key}.{k}" for k in given if k not in spec]
        else:
            kind, default = spec
            problem = _check_value(key, kind, given)
            if problem:
                problems.append(problem)
            values[key] = default if given in (None, "") else given
    unknown += [k for k in raw if k not in SCHEMA]
    return values, problems, unknown


def detect_vcs(repo: Path) -> str | None:
    if (repo / ".git").exists():
        return "git"
    if (repo / ".svn").exists():
        return "svn"
    return None


def check_settings(repo: Path) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    checks: list[dict[str, str]] = []
    path = repo / CONFIG_DIR / CONFIG_FILE
    if not path.is_file():
        checks.append(check("設定ファイル", "ng", f"{CONFIG_DIR}/{CONFIG_FILE} がありません（{repo}）",
                            "リポジトリの一番上のフォルダを開いているか確かめます。まだなら、準備のスキルで作ります"))
        return None, checks
    try:
        raw = load_yaml(path)
    except Exception as exc:  # noqa: BLE001 - every YAML error is reported the same way
        mark = getattr(exc, "problem_mark", None)
        where = f"（{mark.line + 1} 行目）" if mark is not None else ""
        checks.append(check("設定ファイル", "ng", f"YAML の書式が正しくありません{where}: {str(exc).splitlines()[0]}",
                            "表示された行を直します（字下げはスペースで、タブを使わない）"))
        return None, checks
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        checks.append(check("設定ファイル", "ng", "設定ファイルの一番上は「項目: 値」の形にします",
                            "準備のスキルで作り直すか、settings の資料の例と見比べて直します"))
        return None, checks
    values, problems, unknown = effective_settings(raw)
    cov = values["coverity"]
    if not cov.get("url"):
        problems.append("coverity.url（Coverity Connect の URL）がありません")
    if cov["user_env"] == cov["key_env"]:
        problems.append("coverity.user_env と coverity.key_env が同じ名前です。別の名前にします")
    checks.append(check("設定ファイル", "ng" if problems else "ok",
                        "、".join(problems) if problems else str(path),
                        "表示された項目を直します（同意を得てから、準備のスキルの手順で）" if problems else ""))
    if unknown:
        checks.append(check("設定の知らない項目", "info", "、".join(unknown),
                            "書き間違いでなければ、そのままでかまいません（使わない部品は無視します）"))
    url = str(cov.get("url") or "")
    if url.lower().startswith("http://"):
        checks.append(check("Coverity の URL", "warning", "http です（認証キーが暗号化されずに送られます）",
                            "https が使えるか、Coverity の管理者に確かめます"))
    if cov["api"] == "fake":
        fake = cov.get("fake_data")
        fake_path = repo / CONFIG_DIR / str(fake) if fake else None
        if not fake_path or not fake_path.is_file():
            checks.append(check("偽の Coverity", "ng", f"偽の警告のファイルがありません（coverity.fake_data: {fake or '空'}）",
                                f"{CONFIG_DIR}/ に偽の警告のファイルを置き、coverity.fake_data に名前を書きます"))
        else:
            checks.append(check("偽の Coverity", "warning", f"偽の警告を使う設定です（{fake}）",
                                "社内の Coverity を使うときは coverity.api を auto にします"))
    if cov.get("ca_file"):
        ca = Path(str(cov["ca_file"])).expanduser()
        ca = ca if ca.is_absolute() else repo / ca
        checks.append(check("証明書のファイル", "ok" if ca.is_file() else "ng", str(ca),
                            "" if ca.is_file() else "coverity.ca_file に、この PC で開ける証明書のファイルを書きます"))
    vcs = detect_vcs(repo)
    want = values["vcs"].get("type")
    if not vcs:
        checks.append(check("リポジトリ", "ng", f".git も .svn もありません（{repo}）",
                            "調べたいリポジトリの一番上のフォルダを開きます"))
    elif want and want != vcs:
        checks.append(check("リポジトリ", "ng", f"設定は {want} ですが、フォルダは {vcs} です",
                            "vcs.type を直します"))
    else:
        checks.append(check("リポジトリ", "ok", f"{vcs}（{repo}）"))
    if vcs == "git" and shutil.which("git"):
        checks += check_git(repo, values["vcs"].get("base_branch"))
    verify = values["verify"]
    if (verify["initial"] or verify["default"] != "none") and not verify.get("build_command"):
        checks.append(check("ビルドのコマンド", "ng", "初回解析かビルドでの検証を使う設定ですが、verify.build_command が空です",
                            "準備のスキルの「ビルドでの検証」で、試しのビルドをしてから設定します"))
    return values, checks


def check_git(repo: Path, base: str | None) -> list[dict[str, str]]:
    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=repo, capture_output=True,
                              timeout=VERSION_TIMEOUT, stdin=subprocess.DEVNULL)

    checks = []
    try:
        if base:
            found = any(git("rev-parse", "--verify", "--quiet", ref).returncode == 0
                        for ref in (base, f"origin/{base}"))
            checks.append(check("取り込み先のブランチ", "ok" if found else "ng", base,
                                "" if found else "vcs.base_branch を、あるブランチの名前にします"))
        remote = git("remote").stdout.decode("utf-8", errors="replace").split()
        checks.append(check("git のリモート", "ok" if "origin" in remote else "warning",
                            "origin があります" if "origin" in remote else "origin がありません",
                            "" if "origin" in remote else "修正のブランチを push できません（反映のときに要ります）"))
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(check("git", "warning", f"git で確かめられませんでした: {exc}"))
    return checks


def check_filters(repo: Path) -> list[dict[str, str]]:
    folder = repo / CONFIG_DIR / FILTERS_DIR
    files = sorted(folder.glob("*.yaml")) if folder.is_dir() else []
    if not files:
        return [check("条件ファイル", "ng", f"{CONFIG_DIR}/{FILTERS_DIR}/ に条件ファイルがありません",
                      "準備のスキルで作ります（既定は untriaged.yaml）")]
    checks = []
    for path in files:
        problems: list[str] = []
        notes: list[str] = []
        try:
            spec = load_yaml(path) or {}
        except Exception as exc:  # noqa: BLE001
            checks.append(check(f"条件ファイル {path.name}", "ng", f"YAML の書式が正しくありません: {str(exc).splitlines()[0]}"))
            continue
        if not isinstance(spec, dict):
            checks.append(check(f"条件ファイル {path.name}", "ng", "「項目: 値」の形にします"))
            continue
        streams = spec.get("streams")
        if not isinstance(streams, list) or len(streams) != 1:
            problems.append("条件にはストリームを 1 つだけ指定してください（streams: [名前]）")
        bad = [v for v in spec.get("impacts") or [] if v not in IMPACTS]
        if bad:
            problems.append(f"impacts に知らない値 {bad}（{' / '.join(IMPACTS)}）")
        triage = spec.get("triage") or {}
        if isinstance(triage, dict):
            for key, allowed in (("classification", CLASSIFICATIONS), ("status", STATUSES), ("action", ACTIONS)):
                bad = [v for v in triage.get(key) or [] if v not in allowed]
                if bad:
                    notes.append(f"triage.{key} に一覧にない値 {bad}（管理者が足した値なら、そのままでよい）")
        limit = spec.get("limit")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            problems.append(f"limit は 1 以上の整数にします（今は {limit!r}）")
        name = spec.get("name") or path.stem
        if problems:
            checks.append(check(f"条件ファイル {path.name}", "ng", "、".join(problems), "表示された項目を直します"))
        else:
            checks.append(check(f"条件ファイル {path.name}", "warning" if notes else "ok",
                                f"{name}" + (f"（{'、'.join(notes)}）" if notes else "")))
    return checks


def check_env(values: dict[str, Any] | None) -> list[dict[str, str]]:
    cov = (values or {}).get("coverity") or {}
    user_name = cov.get("user_env") or "COV_USER"
    key_name = cov.get("key_env") or "COV_AUTH_KEY"
    fake = cov.get("api") == "fake"
    checks = []
    for label, name in (("Coverity のユーザー名", user_name), ("Coverity の認証キー", key_name)):
        value, where = user_env(name)
        if value:
            place = "Windows のユーザーの環境変数" if where == "user" else "環境変数"
            checks.append(check(label, "ok", f"{place} {name}: ****"))
        elif fake:
            checks.append(check(label, "info", f"環境変数 {name}: なし（偽の Coverity なので要りません）"))
        else:
            checks.append(check(label, "ng", f"環境変数 {name}: なし",
                                "準備のスキルの手順で、ターミナルの伏せ字の欄から入れます（チャットには書かない）"))
    proxies = [n for n in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY") if user_env(n)[0] or user_env(n.lower())[0]]
    checks.append(check("プロキシの環境変数", "info", "、".join(proxies) + " があります" if proxies else "ありません"))
    if os.environ.get("PYTHONIOENCODING"):
        checks.append(check("PYTHONIOENCODING", "info", os.environ["PYTHONIOENCODING"]))
    return checks


def check_tools(vcs: str | None, values: dict[str, Any] | None) -> list[dict[str, str]]:
    checks = []
    uv = tool_version(["uv", "--version"])
    checks.append(check("uv", "ok" if uv else "ng", uv or "見つかりません",
                        "" if uv else "準備のスキルの手順で入れ、Copilot CLI を起動し直します"))
    for name, cmd in (("git", ["git", "--version"]), ("svn", ["svn", "--version", "--quiet"])):
        found = tool_version(cmd)
        needed = vcs == name or ((values or {}).get("vcs") or {}).get("type") == name
        status = "ok" if found else ("ng" if needed else "info")
        checks.append(check(name, status, found or "見つかりません",
                            f"{name} を入れて PATH を通します" if needed and not found else ""))
    verify = (values or {}).get("verify") or {}
    needs_cov = bool(verify.get("initial")) or verify.get("default", "none") != "none"
    sets_path = bool(verify.get("setup_command"))
    for name in ("cov-build", "cov-analyze", "cov-commit-defects"):
        found = tool_version([name, "--ident"])
        needed = needs_cov and (name != "cov-commit-defects" or bool(verify.get("initial")))
        if found:
            status = "ok"
        elif not needed:
            status = "info"
        else:
            status = "warning" if sets_path else "ng"
        fix = ""
        if needed and not found:
            fix = ("環境設定のコマンド（verify.setup_command）で PATH を足しているなら、そのままでかまいません"
                   if sets_path else "Coverity の bin フォルダを PATH に足します（社内の手順で）")
        checks.append(check(name, status, found or ("見つかりません" + ("" if needed else "（今の設定では使いません）")), fix))
    return checks


def check_output_dir(repo: Path, values: dict[str, Any] | None) -> tuple[Path | None, list[dict[str, str]]]:
    raw = str((values or {}).get("output_dir") or "../coverity-triage-out")
    try:
        out = Path(raw).expanduser()
        out = (out if out.is_absolute() else repo / out).resolve()
    except (OSError, RuntimeError) as exc:
        return None, [check("結果の置き場所", "ng", f"{raw} を開けません: {exc}",
                            "output_dir に、この PC で開けるフォルダを書きます")]
    checks = []
    if is_inside(out, repo):
        checks.append(check("結果の置き場所", "ng", f"出力先がリポジトリの中です（{out}）",
                            "output_dir をリポジトリの外にします（既定 ../coverity-triage-out）"))
    else:
        checks.append(check("結果の置き場所", "ok", f"{out}" + ("" if out.is_dir() else "（まだありません。最初の実行で作られます）")))
    if len(str(out)) > LONG_OUTPUT_DIR:
        checks.append(check("結果の置き場所の長さ", "warning", f"{len(str(out))} 文字",
                            "Windows の長いパスで失敗しないよう、短い場所（例 C:/cov-out）にします"))
    return out, checks


# ---- run folders -----------------------------------------------------------------------------


def run_schema() -> dict[str, Any] | None:
    if not RUN_SCHEMA.is_file():
        return None
    return json.loads(read_text(RUN_SCHEMA))


def schema_problems(data: Any, schema: dict[str, Any] | None) -> list[str] | None:
    """Problems of run.json against the run schema copy; None when there is no copy."""
    if schema is None:
        return None
    from jsonschema.validators import validator_for

    cls = validator_for(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(全体)'}: {e.message}"
            for e in sorted(cls(schema).iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))][:20]


def summarize_run(run_dir: Path, schema: dict[str, Any] | None) -> dict[str, Any]:
    data = json.loads(read_text(run_dir / "run.json"))
    if not isinstance(data, dict):
        raise CliError(f"run.json の形が正しくありません: {run_dir}")
    items = [i for i in data.get("items") or [] if isinstance(i, dict)]
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(items)
    has_summary = (run_dir / "summary.html").is_file()
    result = {
        "run_id": data.get("run_id") or run_dir.name, "run_dir": str(run_dir),
        "created_at": data.get("created_at"), "filter_name": data.get("filter_name"),
        "format": data.get("format"), "foreign": data.get("format") != RUN_FORMAT,
        "review": data.get("review"), "integration": data.get("integration"),
        "stopped": bool(data.get("stopped")), "has_summary": has_summary,
        "unfinished": not has_summary, "counts": counts,
    }
    problems = schema_problems(data, schema)
    result["schema_checked"] = problems is not None
    if problems:
        result["schema_problems"] = problems
    result["_items"] = items
    return result


def list_runs(out: Path | None) -> tuple[list[dict[str, Any]], int]:
    if out is None or not out.is_dir():
        return [], 0
    schema = run_schema()
    folders = sorted((p for p in out.iterdir() if p.is_dir() and RUN_NAME.match(p.name) and (p / "run.json").is_file()),
                     key=lambda p: p.name, reverse=True)
    runs = []
    for folder in folders[:MAX_RUNS]:
        try:
            run = summarize_run(folder, schema)
            run.pop("_items")
        except (OSError, ValueError, CliError) as exc:
            run = {"run_id": folder.name, "run_dir": str(folder), "error": f"run.json を読めません: {exc}"}
        runs.append(run)
    return runs, len(folders)


# ---- commands --------------------------------------------------------------------------------


def cmd_check(repo_arg: str | None) -> dict[str, Any]:
    repo = resolve(repo_arg or os.getcwd())
    if not repo.is_dir():
        raise CliError(f"フォルダがありません: {repo}")
    progress("設定と道具を確かめています")
    values, checks = check_settings(repo)
    if values is not None:
        checks += check_filters(repo)
    checks += check_env(values)
    checks += check_tools(detect_vcs(repo), values)
    out, out_checks = check_output_dir(repo, values) if values is not None else (None, [])
    checks += out_checks
    knowledge = repo / CONFIG_DIR / "knowledge.md"
    checks.append(check("知識のファイル", "info", "あります" if knowledge.is_file() else "ありません（なくても動きます）"))
    runs, total = list_runs(out)
    result: dict[str, Any] = {
        "ok": True, "ready": all(c["status"] != "ng" for c in checks), "repo_root": str(repo),
        "checks": checks, "output_dir": str(out) if out else None, "runs_total": total, "runs": runs,
    }
    if values is not None:
        result["settings"] = values
    return result


def cmd_run(run_arg: str) -> dict[str, Any]:
    run_dir = resolve(run_arg)
    if not (run_dir / "run.json").is_file():
        raise CliError(f"実行フォルダではありません（run.json がありません）: {run_dir}",
                       ["check の結果の runs[].run_dir を渡します"])
    try:
        run = summarize_run(run_dir, run_schema())
    except ValueError as exc:
        raise CliError(f"run.json を読めません: {exc}") from exc
    items = run.pop("_items")
    keep = ("id", "cids", "status", "merged_into", "review", "applied", "error")
    run["items"] = [{k: i[k] for k in keep if k in i} for i in items]
    run["errors"] = [i for i in run["items"] if i.get("status") == "error"]
    return {"ok": True, **run}


def main(argv: list[str] | None = None) -> None:
    try:
        sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    except AttributeError:
        pass
    parser = Parser(prog="doctor.py", description="Coverity トリアージの点検（読むだけ）")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    p = sub.add_parser("check", help="道具・設定・環境変数・結果の置き場所・実行の一覧")
    p.add_argument("--repo", help="調べたいリポジトリの一番上のフォルダ（既定は今のフォルダ）")
    p = sub.add_parser("run", help="1つの実行フォルダの様子")
    p.add_argument("--run", required=True, help="実行フォルダ")
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        emit({"ok": True, "help": parser.format_help()})
    try:
        args = parser.parse_args(argv)
        result = cmd_check(args.repo) if args.command == "check" else cmd_run(args.run)
    except CliError as exc:
        emit({"ok": False, "error": str(exc), "problems": exc.problems}, 1)
    except Exception as exc:  # noqa: BLE001 - never a traceback
        emit({"ok": False, "error": f"点検に失敗しました: {type(exc).__name__}: {exc}", "problems": []}, 1)
    emit(result)


if __name__ == "__main__":
    main()
