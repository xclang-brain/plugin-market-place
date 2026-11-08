#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Prepare the Coverity triage settings of a repository (``.coverity-triage/``).

Run: ``uv run --native-tls --quiet "<skill folder>/scripts/setup.py" <command> ...``

Commands (each prints exactly one JSON object on stdout; failures are ``{"ok": false, ...}`` + exit 1):

  detect       repository root, git or svn, base branch, whether settings exist (reads only)
  init         write config.yaml (with comments), filters/untriaged.yaml, filters/all.yaml, knowledge.md
  check        validate the settings, the credential variables (never their values), output_dir, uv
               (reads only, creates no file)
  set          change values of config.yaml and keep the comments people wrote (``--check``: dry run)
  set-verify   the build verification / initial analysis values (same rules as set)
  credentials  the commands that save the user name and the auth key (the key is typed masked, never
               passed to this script)
  trial-build  build the latest base revision once, in a copy outside the repository
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # never leave __pycache__ in the skill folder

import argparse  # noqa: E402
import codecs  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envvars  # noqa: E402

CONFIG_DIR = ".coverity-triage"
CONFIG_FILE = "config.yaml"
FILTERS_DIR = "filters"
KNOWLEDGE_FILE = "knowledge.md"
# The default output folder is next to the repository (os.pardir is ".." everywhere).
DEFAULT_OUTPUT_DIR = os.pardir + "/coverity-triage-out"
TRIAL_DIR = "_trial-build"
LOG_TAIL_BYTES = 4000
NOTICE = ("このファイルでは、注釈は動いたり消えたりします。注釈を元の場所に残せない書き方（「|」「>」で何行かにわたる値、"
          "「&」「*」で値を使い回す書き方など）があるため、ファイル全体を書き直します。行の頭の「#」の行はファイルの先頭に"
          "まとめ、字下げした「#」の行と行の終わりの注釈は消えます（値は正しく保存されます）。注釈を残したいときは、保存しないで、"
          "メモ帳などでファイルを直してください。")

# ---- output ---------------------------------------------------------------------------------------


class SetupError(Exception):
    """A problem the person can act on; printed as {"ok": false, "error": ..., "problems": [...]}."""

    def __init__(self, message: str, problems: list[str] | None = None, **details: Any):
        super().__init__(message)
        self.problems = problems or []
        self.details = details


def _json_escape(error: UnicodeEncodeError) -> tuple[str, int]:
    chunk = error.object[error.start:error.end]
    return json.dumps(chunk, ensure_ascii=True)[1:-1], error.end


codecs.register_error("setup_json_escape", _json_escape)


def _stdout_encoding() -> str:
    """PYTHONIOENCODING when set; the console code page on Windows when stdout is a pipe; else UTF-8."""
    if os.environ.get("PYTHONIOENCODING"):
        return (sys.stdout.encoding or "utf-8") if sys.stdout else "utf-8"
    if sys.platform == "win32" and not (sys.stdout and sys.stdout.isatty()):
        try:
            import ctypes
            page = ctypes.windll.kernel32.GetConsoleOutputCP()
        except (AttributeError, OSError):
            page = 0
        if page == 65001:
            return "utf-8"
        if page:
            try:
                return codecs.lookup(f"cp{page}").name
            except LookupError:
                pass
        import locale
        return locale.getpreferredencoding(False) or "utf-8"
    return "utf-8"


def emit(data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    encoding = _stdout_encoding()
    try:
        raw = text.encode(encoding, errors="setup_json_escape")
    except LookupError:
        raw = text.encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(raw.decode(encoding, errors="replace"))
    else:
        stream.write(raw)
    sys.stdout.flush()


def progress(message: str) -> None:
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def read_stdin_json() -> Any:
    raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        import locale
        text = raw.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
    text = text.strip()
    if not text:
        raise SetupError("標準入力が空です。JSON を渡してください")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SetupError(f"標準入力の JSON を読めません: {exc}") from None


# ---- files and commands ----------------------------------------------------------------------------


def write_bytes(path: Path, data: bytes) -> None:
    """Write via a temporary file and os.replace (waits a little on Windows while another reads it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SetupError(f"{path.name} を読めません（{exc.strerror or exc}）: {path}") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SetupError(f"{path.name} の文字コードが UTF-8 ではないため読めません。メモ帳などで UTF-8 にして保存し直してください: "
                         f"{path}") from None


def absolute(path: str | Path) -> Path:
    """An absolute path; some Windows drives refuse ``resolve()`` although the folder is usable."""
    path = Path(path).expanduser()
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


def run(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
        check: bool = True, timeout: float | None = 600) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, capture_output=True, check=False, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise SetupError(f"コマンドが見つかりません: {args[0]}（入れて、PATH を通してください）") from None
    except subprocess.TimeoutExpired:
        raise SetupError(f"{' '.join(args[:3])} が時間内に終わりませんでした") from None
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise SetupError(f"{' '.join(args[:3])} が失敗しました: {message[:800]}")
    return proc


def out_text(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def git(repo: Path, *args: str, check: bool = True, timeout: float | None = 600) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return run(["git", "-c", "core.longpaths=true", *args], cwd=repo, env=env, check=check, timeout=timeout)


def svn(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return run(["svn", "--non-interactive", *args], cwd=repo, check=check)


def mask_url(url: str) -> str:
    """A remote URL without the user name or token that may be written in it."""
    return re.sub(r"(//)[^/@\s]+@", r"\1****@", url)


# ---- the repository --------------------------------------------------------------------------------


def find_root(start: str | None) -> tuple[Path, str | None]:
    """The top folder of the git / svn working copy that holds ``start`` (cwd when None)."""
    folder = absolute(start or os.getcwd())
    if not folder.exists():
        raise SetupError(f"フォルダがありません: {folder}")
    if folder.is_file():
        folder = folder.parent
    for candidate in (folder, *folder.parents):
        if (candidate / ".git").exists():
            return candidate, "git"
    svn_top = None
    for candidate in (folder, *folder.parents):
        if (candidate / ".svn").is_dir():
            svn_top = candidate  # the highest one is the working copy root
    if svn_top is not None:
        return svn_top, "svn"
    return folder, None


def need_root(start: str | None) -> tuple[Path, str]:
    root, vcs = find_root(start)
    if vcs is None:
        raise SetupError(f"git か svn のリポジトリの中ではありません: {root}。リポジトリの一番上のフォルダ（.git か .svn がある"
                         "フォルダ）を指定してください")
    return root, vcs


def base_branch(repo: Path) -> str:
    proc = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    head = out_text(proc) if proc.returncode == 0 else ""
    if head and "/" in head:
        return head.split("/", 1)[1]
    proc = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return (out_text(proc) if proc.returncode == 0 else "") or "main"


def remote_url(repo: Path) -> str | None:
    proc = git(repo, "remote", "get-url", "origin", check=False)
    return out_text(proc) or None if proc.returncode == 0 else None


def resolve_output(repo: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return absolute(path if path.is_absolute() else repo / path)


def inside(path: Path, repo: Path) -> bool:
    def norm(p: Path) -> str:
        return os.path.normcase(str(p))
    return norm(path) == norm(repo) or norm(path).startswith(norm(repo).rstrip("\\/") + os.sep)


# ---- the settings schema ----------------------------------------------------------------------------

REQUIRED = object()  # no default: always written
AUTO = object()  # decided from the repository when absent

# dotted key -> (kind, default, choices or range)
SCHEMA: dict[str, tuple[str, Any, Any]] = {
    "coverity.url": ("str", REQUIRED, None),
    "coverity.api": ("enum", "auto", ("auto", "fake")),
    "coverity.user_env": ("name", "COV_USER", None),
    "coverity.key_env": ("name", "COV_AUTH_KEY", None),
    "coverity.triage_store": ("str", "Default Triage Store", None),
    "coverity.revision_field": ("str?", "sourceVersion", None),
    "coverity.path_strip_prefixes": ("list", [], None),
    "coverity.ca_file": ("str?", None, None),
    "coverity.fake_data": ("str?", None, None),
    "vcs.type": ("enum", AUTO, ("git", "svn")),
    "vcs.base_branch": ("str", AUTO, None),
    "vcs.branch_prefix": ("str", "coverity-fix/", None),
    "output_dir": ("str", DEFAULT_OUTPUT_DIR, None),
    "max_items": ("int", 100, (1, None)),
    "parallel": ("int", 3, (1, 8)),
    "options.annotation": ("bool", False, None),
    "options.per_run_branch": ("bool", False, None),
    "options.knowledge_suggestions": ("bool", False, None),
    "options.metrics": ("bool", False, None),
    "options.markdown": ("bool", False, None),
    "verify.default": ("enum", "none", ("none", "build", "build+analyze")),
    "verify.initial": ("bool", False, None),
    "verify.setup_command": ("str", "", None),
    "verify.build_dir": ("str", "", None),
    "verify.build_command": ("str", "", None),
    "verify.cov_build_args": ("str", "--dir idir", None),
    "verify.cov_analyze_args": ("str", "--dir idir --all", None),
    "verify.cov_commit_args": ("str", "", None),
    "verify.timeout_minutes": ("int", 180, (1, None)),
    "agents.integration": ("bool", False, None),
    "agents.review": ("bool", False, None),
    "agents.max_rework": ("int", 1, (0, 2)),
}
SECTIONS = ("coverity", "vcs", "options", "verify", "agents")
IMPACTS = ("High", "Medium", "Low", "Unspecified")
CLASSIFICATIONS = ("Unclassified", "Pending", "False Positive", "Intentional", "Bug")
ACTIONS = ("Undecided", "Fix Required", "Fix Submitted", "Modeling Required", "Ignore")
STATUSES = ("New", "Triaged", "Dismissed", "Absent Dismissed", "Fixed")


def lookup(data: dict[str, Any], key: str) -> tuple[bool, Any]:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def assign(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def remove(data: dict[str, Any], key: str) -> bool:
    parts = key.split(".")
    node: Any = data
    trail = []
    for part in parts[:-1]:
        if not isinstance(node, dict) or not isinstance(node.get(part), dict):
            return False
        trail.append((node, part))
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, part in reversed(trail):  # a section left empty goes too
        if parent[part] == {}:
            del parent[part]
    return True


def type_problem(key: str, value: Any) -> str | None:
    """What is wrong with ``value`` for ``key`` (None: it is fine)."""
    kind, _default, extra = SCHEMA[key]
    if kind in ("str", "name"):
        if not isinstance(value, str):
            return f"{key}: 文字列にしてください"
        if key == "coverity.url" and not value.strip():
            return f"{key}: Coverity Connect の URL を書いてください"
        if kind == "name" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            return f"{key}: 環境変数の名前（英字・数字・_）にしてください"
    elif kind == "str?":
        if value is not None and not isinstance(value, str):
            return f"{key}: 文字列にしてください（使わないときは項目を消します）"
    elif kind == "bool":
        if not isinstance(value, bool):
            return f"{key}: true か false にしてください"
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{key}: 整数にしてください"
        low, high = extra
        if value < low or (high is not None and value > high):
            return f"{key}: {low}〜{high} にしてください" if high is not None else f"{key}: {low} 以上にしてください"
    elif kind == "enum":
        if value not in extra:
            return f"{key}: 次のどれかにしてください: {' / '.join(extra)}"
    elif kind == "list":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return f"{key}: 文字列を並べた形（[a, b] か、1行に1つ「- 値」）にしてください"
    return None


def validate_config(data: Any) -> tuple[list[str], list[str]]:
    """(problems, warnings) of a whole config.yaml."""
    if not isinstance(data, dict):
        return ["config.yaml の一番外側は「キー: 値」の形にしてください"], []
    problems: list[str] = []
    warnings: list[str] = []
    for key in SCHEMA:
        present, value = lookup(data, key)
        if not present:
            if SCHEMA[key][1] is REQUIRED:
                problems.append(f"{key}: ありません。書いてください")
            continue
        problem = type_problem(key, value)
        if problem:
            problems.append(problem)
    for section in SECTIONS:
        if section in data and data[section] is not None and not isinstance(data[section], dict):
            problems.append(f"{section}: 「キー: 値」の形にしてください")
    known_top = set(SECTIONS) | {k for k in SCHEMA if "." not in k}
    for name, value in data.items():
        if name not in known_top:
            warnings.append(f"{name}: この項目は使われません（書き間違いかもしれません）")
        elif name in SECTIONS and isinstance(value, dict):
            for sub in value:
                if f"{name}.{sub}" not in SCHEMA:
                    warnings.append(f"{name}.{sub}: この項目は使われません（書き間違いかもしれません）")
    user = lookup(data, "coverity.user_env")[1] or SCHEMA["coverity.user_env"][1]
    key = lookup(data, "coverity.key_env")[1] or SCHEMA["coverity.key_env"][1]
    if user == key:
        problems.append("coverity.user_env と coverity.key_env に同じ名前は使えません")
    mode = lookup(data, "verify.default")[1] or "none"
    initial = lookup(data, "verify.initial")[1] is True
    build = lookup(data, "verify.build_command")[1] or ""
    if (mode != "none" or initial) and isinstance(build, str) and not build.strip():
        problems.append("verify.build_command: ビルドでの検証か初回解析を使うときは、ビルドのコマンドを書いてください")
    analyze = lookup(data, "verify.cov_analyze_args")
    if (mode == "build+analyze" or initial) and analyze[0] and isinstance(analyze[1], str) \
            and not re.search(r"(?:^|\s)--dir(?:=|\s)", analyze[1]):
        problems.append("verify.cov_analyze_args: --dir（中間ディレクトリ）を書いてください")
    return problems, warnings


def validate_filter(data: Any, name: str) -> tuple[list[str], list[str]]:
    if not isinstance(data, dict):
        return [f"{name}: 一番外側は「キー: 値」の形にしてください"], []
    problems: list[str] = []
    warnings: list[str] = []
    known = {"name", "project", "streams", "checkers", "impacts", "triage", "limit"}
    for key in data:
        if key not in known:
            warnings.append(f"{name} の {key}: この項目は使われません")
    streams = data.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], str) or not streams[0].strip():
        problems.append(f"{name} の streams: ストリームをちょうど1つ書いてください（例：streams: [app-main]）")
    for key in ("checkers", "impacts"):
        value = data.get(key)
        if value is not None and (not isinstance(value, list) or not all(isinstance(x, str) for x in value)):
            problems.append(f"{name} の {key}: 文字列を並べた形にしてください")
    for value in data.get("impacts") or []:
        if isinstance(value, str) and value not in IMPACTS:
            problems.append(f"{name} の impacts: {value} は使えません（{' / '.join(IMPACTS)}）")
    triage = data.get("triage")
    if triage is not None:
        if not isinstance(triage, dict):
            problems.append(f"{name} の triage: 「キー: 値」の形にしてください")
        else:
            for key, allowed in (("classification", CLASSIFICATIONS), ("action", ACTIONS), ("status", STATUSES)):
                value = triage.get(key)
                if value is None:
                    continue
                if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                    problems.append(f"{name} の triage.{key}: 文字列を並べた形にしてください")
                    continue
                for item in value:
                    if item not in allowed:
                        warnings.append(f"{name} の triage.{key}: {item} は Coverity の標準の値ではありません")
            for key in triage:
                if key not in ("classification", "action", "status"):
                    warnings.append(f"{name} の triage.{key}: この項目は使われません")
    limit = data.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        problems.append(f"{name} の limit: 1 以上の整数にしてください")
    for key in ("name", "project"):
        if data.get(key) is not None and not isinstance(data.get(key), str):
            problems.append(f"{name} の {key}: 文字列にしてください")
    return problems, warnings


def load_yaml(path: Path) -> Any:
    import yaml
    try:
        return yaml.safe_load(read_text(path))
    except yaml.YAMLError as exc:
        raise SetupError(f"{path.name} の YAML の書き方が正しくありません: {path}", problems=[str(exc)[:400]]) from None


# ---- writing the first settings -------------------------------------------------------------------

CONFIG_LAYOUT: list[tuple[str, str, str]] = [
    ("comment", "", "Coverity の警告のトリアージの設定。チームで共有するため、コミットします。"),
    ("comment", "", "認証情報（ユーザー名と認証キー）は、ここに書きません。環境変数に置きます。"),
    ("blank", "", ""),
    ("section", "coverity", "Coverity Connect との通信"),
    ("key", "coverity.url", "Coverity Connect の URL（ブラウザーで開くアドレス）"),
    ("key", "coverity.api", "auto：社内の Coverity と通信する。fake：偽の警告で試す"),
    ("key", "coverity.user_env", "ユーザー名を入れる環境変数の名前"),
    ("key", "coverity.key_env", "認証キーを入れる環境変数の名前（user_env と別の名前）"),
    ("key", "coverity.triage_store", "ストリームから決められないときのトリアージストア"),
    ("key", "coverity.revision_field", "解析したリビジョンを入れたスナップショットの項目"),
    ("key", "coverity.path_strip_prefixes", "警告のファイルのパスから外す先頭（例：[/build/work/]）"),
    ("hint", "coverity", "社内の証明書のファイルを使うときは、ここに「ca_file: <ファイル>」を足します"),
    ("blank", "", ""),
    ("section", "vcs", "バージョン管理"),
    ("key", "vcs.type", "git か svn"),
    ("key", "vcs.base_branch", "修正の元にするブランチ"),
    ("key", "vcs.branch_prefix", "修正のブランチの名前の頭"),
    ("blank", "", ""),
    ("key", "output_dir", "結果を置くフォルダ（リポジトリの外。リポジトリからの相対か絶対）"),
    ("key", "max_items", "1回の実行で調べる警告の上限"),
    ("key", "parallel", "同時に動かす作業の数（1〜8）"),
    ("blank", "", ""),
    ("section", "options", "オプション（どれも、使わない＝false が既定）"),
    ("key", "options.annotation", "逸脱の案として、コードに注釈を入れる差分も作る"),
    ("key", "options.per_run_branch", "git：承認した修正を1つのブランチにまとめる"),
    ("key", "options.knowledge_suggestions", "反映の後に、knowledge.md に足す知識の案を出す"),
    ("key", "options.metrics", "反映のたびに、推奨が採られた割合などを記録する"),
    ("key", "options.markdown", "HTML のレポートのほかに Markdown も作る"),
    ("blank", "", ""),
    ("section", "verify", "ビルドでの検証と初回解析"),
    ("key", "verify.default", "修正案の確かめ方：none（しない）／build（ビルド）／build+analyze（ビルドと再解析）"),
    ("key", "verify.initial", "最初に、開いているフォルダをビルド・解析して Coverity に登録する"),
    ("key", "verify.setup_command", "ビルドの前に動かす環境設定のコマンド（{root} はビルドするフォルダ）"),
    ("key", "verify.build_dir", "ビルドするフォルダ（リポジトリからの相対。空なら一番上）"),
    ("key", "verify.build_command", "ビルドのコマンド（例：make all）"),
    ("key", "verify.cov_build_args", "cov-build の引数"),
    ("key", "verify.cov_analyze_args", "cov-analyze の引数（--dir が要る）"),
    ("key", "verify.cov_commit_args", "cov-commit-defects に足す引数（--dir・--url・--stream・--user・--version は自動）"),
    ("key", "verify.timeout_minutes", "初回解析の時間の上限（分）"),
    ("blank", "", ""),
    ("section", "agents", "AI の作業の後の段"),
    ("key", "agents.integration", "同じ原因の警告を1つにまとめる"),
    ("key", "agents.review", "結果を別の AI が確かめる"),
    ("key", "agents.max_rework", "確かめで差し戻した警告を作り直す回数の上限（0〜2）"),
]

FILTER_LAYOUT: list[tuple[str, str, str]] = [
    ("key", "name", "表示の名前"),
    ("key", "project", "プロジェクト"),
    ("key", "streams", "ストリーム（ちょうど1つ）"),
    ("key", "checkers", "チェッカー（* ? [ ] のワイルドカード可。空ならすべて）"),
    ("key", "impacts", "High / Medium / Low / Unspecified（空ならすべて）"),
    ("section", "triage", "Coverity のトリアージの状態（同じ項目の中は「どれか」、項目どうしは「すべて」）"),
    ("key", "triage.classification", "Unclassified / Pending / False Positive / Intentional / Bug"),
    ("key", "triage.action", "Undecided / Fix Required / Fix Submitted / Modeling Required / Ignore"),
    ("key", "triage.status", "New / Triaged / Dismissed / Absent Dismissed / Fixed"),
    ("key", "limit", "調べる件数の上限"),
]

KNOWLEDGE_TEMPLATE = """# プロジェクトの知識（Coverity トリアージ）

警告を調べる前に AI が読みます。チームで共有するため、コミットしてください。
1 項目 1 行で、コードで確かめられる事実と、判断の方針を書きます。

## 調査で使う事実

<!-- 例：fatal_error()（src/common/error.c）は戻らない（内部で abort する） -->

## 推奨の方針（修正か逸脱か）

<!-- 例：ハードウェアレジスタへのアクセスのためのポインタ変換（MISRA Rule 11.x）は逸脱にする -->

## 逸脱コメントの書き方

<!-- 例：誤検知の根拠には、呼び出し元の関数名と行番号を必ず書く -->
"""

COMMENT_COLUMN = 36


def width(text: str) -> int:
    """Columns ``text`` takes in a fixed-width font (wide characters take two)."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def render(layout: list[tuple[str, str, str]], values: dict[str, Any], header: list[str]) -> str:
    """A YAML file with a Japanese comment for every item, in the order of ``layout``."""
    import yamledit

    lines = [f"# {text}" for text in header]
    for kind, key, comment in layout:
        if kind == "comment":
            lines.append(f"# {comment}")
        elif kind == "blank":
            lines.append("")
        elif kind == "section":
            if not isinstance(values.get(key), dict) or not values[key]:
                continue
            lines.append(f"# {comment}")
            lines.append(f"{key}:")
        elif kind == "hint":
            if isinstance(values.get(key), dict):
                lines.append(f"  # {comment}")
        else:
            present, value = lookup(values, key)
            if not present:
                continue
            depth = key.count(".")
            text = "  " * depth + key.split(".")[-1] + ": " + yamledit.scalar(value)
            pad = max(COMMENT_COLUMN - width(text), 3)
            lines.append(text + " " * pad + "# " + comment)
    while lines and lines[-1] == "":
        lines.pop()
    text = "\n".join(lines) + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)


def first_config(url: str, vcs: str, base: str | None, output_dir: str, strip: list[str]) -> dict[str, Any]:
    config: dict[str, Any] = {
        "coverity": {"url": url.strip().rstrip("/"), "api": "auto", "user_env": "COV_USER", "key_env": "COV_AUTH_KEY",
                     "triage_store": "Default Triage Store", "revision_field": "sourceVersion",
                     "path_strip_prefixes": strip},
        "vcs": {"type": vcs, "base_branch": base or "main", "branch_prefix": "coverity-fix/"},
        "output_dir": output_dir,
        "max_items": 100,
        "parallel": 3,
        "options": {k.split(".")[1]: False for k in SCHEMA if k.startswith("options.")},
        "verify": {k.split(".")[1]: SCHEMA[k][1] for k in SCHEMA if k.startswith("verify.")},
        "agents": {"integration": False, "review": False, "max_rework": 1},
    }
    if vcs == "svn":
        del config["vcs"]["base_branch"], config["vcs"]["branch_prefix"]
    return config


def first_filters(project: str | None, stream: str) -> dict[str, dict[str, Any]]:
    base: dict[str, Any] = {"name": "", **({"project": project} if project else {}), "streams": [stream],
                            "checkers": [], "impacts": []}
    untriaged = {**base, "name": "分類がまだの警告（20 件まで）",
                 "triage": {"classification": ["Unclassified"], "action": [], "status": []}, "limit": 20}
    everything = {**base, "name": "すべての警告（100 件まで）",
                  "triage": {"classification": [], "action": [], "status": []}, "limit": 100}
    return {"untriaged.yaml": untriaged, "all.yaml": everything}


# ---- commands ----------------------------------------------------------------------------------------


def cmd_detect(args: argparse.Namespace) -> dict[str, Any]:
    root, vcs = find_root(args.repo)
    base = None
    remote = None
    if vcs == "git":
        base = base_branch(root)
        url = remote_url(root)
        remote = mask_url(url) if url else None
    elif vcs == "svn" and shutil.which("svn"):
        proc = svn(root, "info", "--show-item", "url", str(root), check=False)
        remote = mask_url(out_text(proc)) if proc.returncode == 0 else None
    out_dir = resolve_output(root, DEFAULT_OUTPUT_DIR)
    config = root / CONFIG_DIR / CONFIG_FILE
    return {"ok": True, "repo_root": str(root), "vcs": vcs,
            "vcs_note": None if vcs else "git か svn のリポジトリの一番上のフォルダ（.git か .svn があるフォルダ）を指定してください",
            "base_branch": base, "remote": remote, "vcs_command": bool(vcs and shutil.which(vcs)),
            "has_config": config.is_file(), "config_file": str(config),
            "output_dir": DEFAULT_OUTPUT_DIR, "output_dir_resolved": str(out_dir),
            "output_dir_outside_repo": not inside(out_dir, root)}


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    import yaml

    root, vcs = need_root(args.repo)
    url = (args.url or "").strip()
    stream = (args.stream or "").strip()
    project = (args.project or "").strip() or None
    problems = []
    if not re.match(r"^https?://[^\s/]+", url):
        problems.append("--url: Coverity Connect の URL を http:// か https:// で始めて書いてください")
    if not stream:
        problems.append("--stream: ストリームの名前を書いてください")
    if problems:
        raise SetupError("値が足りないか、正しくありません", problems=problems)
    base = args.base_branch or (base_branch(root) if vcs == "git" else None)
    out_value = args.output_dir or DEFAULT_OUTPUT_DIR
    out_dir = resolve_output(root, out_value)
    if inside(out_dir, root):
        raise SetupError(f"結果を置くフォルダは、リポジトリの外にしてください（誤ってコミットしないため）: {out_dir}")
    config = first_config(url, vcs, base, out_value, list(args.strip_prefix or []))
    bad, _ = validate_config(config)
    if bad:
        raise SetupError("値が正しくありません", problems=bad)
    target = root / CONFIG_DIR
    config_path = target / CONFIG_FILE
    if config_path.exists() and not args.overwrite:
        raise SetupError("設定ファイルがもうあります。作り直すときだけ --overwrite を付けます（値を変えるときは set を使います）",
                         path=str(config_path))
    files: dict[Path, str] = {config_path: render(CONFIG_LAYOUT, config, [])}
    for name, spec in first_filters(project, stream).items():
        path = target / FILTERS_DIR / name
        if args.overwrite or not path.exists():
            files[path] = render(FILTER_LAYOUT, spec, [spec["name"].split("（")[0] + "を調べる条件"])
    knowledge = target / KNOWLEDGE_FILE
    if not knowledge.exists():  # never overwrite what the team wrote
        files[knowledge] = KNOWLEDGE_TEMPLATE
    for path, text in files.items():  # what is written must read back as the values
        if path.suffix == ".yaml" and not isinstance(yaml.safe_load(text), dict):
            raise SetupError(f"書く内容を読み返せませんでした: {path}")
    table = [
        {"項目": "Coverity Connect の URL", "値": config["coverity"]["url"]},
        {"項目": "プロジェクト", "値": project or "（なし）"},
        {"項目": "ストリーム", "値": stream},
        {"項目": "バージョン管理", "値": vcs},
        *([{"項目": "修正の元にするブランチ", "値": config["vcs"]["base_branch"]}] if vcs == "git" else []),
        {"項目": "結果を置くフォルダ", "値": f"{out_value}（{out_dir}）"},
        {"項目": "最初に調べる警告", "値": "分類がまだの警告を 20 件まで（filters/untriaged.yaml）"},
        {"項目": "認証情報", "値": "環境変数 COV_USER と COV_AUTH_KEY（ファイルには書きません）"},
    ]
    result = {"ok": True, "repo_root": str(root), "table": table, "files": [str(p) for p in files],
              "kept": [str(p) for p in (knowledge,) if p not in files]}
    if args.check:
        return {**result, "check": True, "written": [],
                "next": "表を人に見せて同意を得てから、--check を付けずに同じ引数で init を動かす"}
    for path, text in files.items():
        write_text(path, text)
    return {**result, "written": [str(p) for p in files],
            "next": ".coverity-triage/ をコミットしてチームで共有するよう人に伝える（認証情報は入っていません）"}


def _check(name: str, status: str, detail: str, how_to_fix: str = "") -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail, "how_to_fix": how_to_fix}


def uv_version() -> str | None:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        return None
    try:
        proc = subprocess.run([uv, "--version"], capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    root, vcs = find_root(args.repo)
    checks = [_check("リポジトリ", "ok" if vcs else "ng", f"{vcs or '不明'}（{root}）",
                     "" if vcs else ".git か .svn があるフォルダを指定します")]
    if vcs:
        found = shutil.which(vcs)
        checks.append(_check(f"{vcs} のコマンド", "ok" if found else "ng", found or "見つかりません",
                             "" if found else f"{vcs} を入れて PATH を通します（社内の手順で）"))
    version = uv_version()
    checks.append(_check("uv", "ok" if version else "ng", version or "見つかりません", "" if version else "uv を入れます"))
    config_path = root / CONFIG_DIR / CONFIG_FILE
    data: Any = None
    if not config_path.is_file():
        checks.append(_check("設定ファイル", "ng", f"まだありません: {config_path}", "init で作ります"))
    else:
        try:
            data = load_yaml(config_path)
        except SetupError as exc:
            checks.append(_check("設定ファイル", "ng", str(exc), "YAML の書き方を直します"))
        else:
            problems, warnings = validate_config(data)
            checks.append(_check("設定ファイル", "ng" if problems else "ok",
                                 "、".join(problems) if problems else str(config_path),
                                 "表示された項目を直します（set で直せます）" if problems else ""))
            for warning in warnings:
                checks.append(_check("設定ファイルの知らない項目", "warning", warning, "要らなければ消します"))
            if problems:
                data = data if isinstance(data, dict) else None
    filters_dir = root / CONFIG_DIR / FILTERS_DIR
    names = sorted(p.name for p in filters_dir.glob("*.yaml")) if filters_dir.is_dir() else []
    if config_path.is_file() and not names:
        checks.append(_check("条件ファイル", "ng", f"ありません: {filters_dir}", "init で作ります"))
    for name in names:
        try:
            problems, warnings = validate_filter(load_yaml(filters_dir / name), name)
        except SetupError as exc:
            problems, warnings = [str(exc)], []
        checks.append(_check(f"条件ファイル {name}", "ng" if problems else "ok",
                             "、".join(problems) if problems else "読めます", "表示された項目を直します" if problems else ""))
        for warning in warnings:
            checks.append(_check(f"条件ファイル {name}", "warning", warning))
    if config_path.is_file():
        knowledge = root / CONFIG_DIR / KNOWLEDGE_FILE
        checks.append(_check("知識のファイル", "ok" if knowledge.is_file() else "warning",
                             str(knowledge) if knowledge.is_file() else "ありません（なくても動きます）",
                             "" if knowledge.is_file() else "init で、ひな形ができます"))
    if isinstance(data, dict):
        out_value = lookup(data, "output_dir")[1] if lookup(data, "output_dir")[0] else DEFAULT_OUTPUT_DIR
        if isinstance(out_value, str):
            out_dir = resolve_output(root, out_value)
            bad = inside(out_dir, root)
            checks.append(_check("結果を置くフォルダ", "ng" if bad else "ok", str(out_dir),
                                 "リポジトリの外にします（set で output_dir を変えます）" if bad else ""))
        cov = data.get("coverity") if isinstance(data.get("coverity"), dict) else {}
        url = cov.get("url") if isinstance(cov.get("url"), str) else ""
        if url.lower().startswith("http://"):
            checks.append(_check("Coverity の URL", "warning", "http です（認証キーが暗号化されずに送られます）",
                                 "https が使えるか、Coverity の管理者に確かめます"))
        if cov.get("api") == "fake":
            checks.append(_check("Coverity", "warning", "偽の警告を使う設定です（coverity.api: fake）",
                                 "社内の Coverity を使うときは coverity.api を auto にします"))
        else:
            for label, key in (("Coverity のユーザー名", "user_env"), ("Coverity の認証キー", "key_env")):
                name = cov.get(key) if isinstance(cov.get(key), str) else SCHEMA[f"coverity.{key}"][1]
                found = envvars.where(name)
                detail = f"環境変数 {name}: " + ({"process": "あり", "user": "あり（ユーザーの環境変数）"}.get(found or "", "なし"))
                checks.append(_check(label, "ok" if found else "ng", detail,
                                     "" if found else "credentials のコマンドで保存します（認証キーは伏せ字で入れます）"))
        vcs_conf = data.get("vcs") if isinstance(data.get("vcs"), dict) else {}
        conf_type = vcs_conf.get("type", vcs)
        if vcs and conf_type != vcs:
            checks.append(_check("バージョン管理", "ng", f"設定は {conf_type}、リポジトリは {vcs}", "set で vcs.type を直します"))
        if vcs == "git" and conf_type == "git" and shutil.which("git"):
            base = vcs_conf.get("base_branch") if isinstance(vcs_conf.get("base_branch"), str) else base_branch(root)
            has = any(git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).returncode == 0
                      for ref in (base, f"origin/{base}"))
            checks.append(_check("修正の元にするブランチ", "ok" if has else "ng", base,
                                 "" if has else "set で vcs.base_branch を直します"))
            url_remote = remote_url(root)
            checks.append(_check("git のリモート origin", "ok" if url_remote else "warning",
                                 mask_url(url_remote) if url_remote else "ありません",
                                 "" if url_remote else "修正のブランチを push できません（反映のときに要ります）"))
        verify = data.get("verify") if isinstance(data.get("verify"), dict) else {}
        if verify.get("initial") is True:
            setup_command = verify.get("setup_command") or ""
            for tool in ("cov-build", "cov-analyze") + (() if cov.get("api") == "fake" else ("cov-commit-defects",)):
                found = shutil.which(tool)
                status = "ok" if found else ("warning" if setup_command else "ng")
                checks.append(_check(f"初回解析の {tool}", status,
                                     found or ("PATH にありません（環境設定のコマンドで足されるなら問題ありません）" if setup_command
                                               else "PATH にありません"),
                                     "" if found else "Coverity の解析のツールの bin フォルダを PATH に足します（社内の手順で）"))
    ready = all(c["status"] != "ng" for c in checks)
    return {"ok": True, "ready": ready, "repo_root": str(root), "checks": checks,
            "missing": [c["check"] for c in checks if c["status"] == "ng"],
            "next": "準備はできています。Coverity と通信できるかを確かめます" if ready else "status が ng の項目を、上から1つずつ直す"}


def parse_value(key: str, raw: str) -> Any:
    """A value given on the command line, read with the type of ``key``."""
    import yaml

    kind, _default, _extra = SCHEMA[key]
    if kind in ("str", "name", "enum"):
        return raw
    if kind == "str?":
        return raw if raw != "" else None
    if kind == "bool":
        low = raw.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        raise SetupError(f"{key}: true か false で書いてください（今は {raw}）")
    if kind == "int":
        try:
            return int(raw.strip())
        except ValueError:
            raise SetupError(f"{key}: 整数で書いてください（今は {raw}）") from None
    text = raw.strip()  # list
    if not text:
        return []
    if text.startswith("["):
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError:
            raise SetupError(f"{key}: [a, b] の形で書いてください") from None
        if isinstance(value, list):
            return [str(x) for x in value]
    return [part.strip() for part in text.split(",") if part.strip()]


def known_key(key: str) -> str:
    if key not in SCHEMA:
        raise SetupError(f"知らない項目です: {key}", problems=["使える項目: " + ", ".join(SCHEMA)])
    return key


def apply_changes(root: Path, values: dict[str, Any], unset: list[str], check_only: bool) -> dict[str, Any]:
    """Change config.yaml as REQ-107 says and keep its comments; ``check_only`` writes nothing."""
    import yaml
    import yamledit

    path = root / CONFIG_DIR / CONFIG_FILE
    if not path.is_file():
        raise SetupError(f"設定ファイルがありません: {path}（先に init で作ります）")
    text = read_text(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SetupError("config.yaml の YAML の書き方が正しくありません。先にファイルを直してください",
                         problems=[str(exc)[:400]]) from None
    data = data if data is not None else {}
    if not isinstance(data, dict):
        raise SetupError("config.yaml の一番外側が「キー: 値」の形ではありません。先にファイルを直してください")
    problems = [p for p in (type_problem(k, v) for k, v in values.items()) if p]
    if problems:
        raise SetupError("値が正しくありません", problems=problems)
    changes = []
    order = list(SCHEMA)
    for key, value in sorted(values.items(), key=lambda kv: order.index(kv[0])):
        present, current = lookup(data, key)
        default = SCHEMA[key][1]
        if present:
            if yamledit._same(current, value):
                changes.append({"key": key, "action": "unchanged", "before": current, "after": value})
                continue
            assign(data, key, value)
            changes.append({"key": key, "action": "changed", "before": current, "after": value})
        elif default not in (REQUIRED, AUTO) and yamledit._same(default, value):
            changes.append({"key": key, "action": "skipped_default", "before": None, "after": value,
                            "note": "既定の値と同じなので、ファイルには書きません"})
        else:
            assign(data, key, value)
            changes.append({"key": key, "action": "added", "before": None, "after": value})
    for key in unset:
        present, current = lookup(data, key)
        if present and remove(data, key):
            changes.append({"key": key, "action": "removed", "before": current, "after": None})
        else:
            changes.append({"key": key, "action": "unchanged", "before": None, "after": None,
                            "note": "ファイルにない項目です"})
    problems, warnings = validate_config(data)
    touched = set(values) | set(unset)
    mine = [p for p in problems if p.split(":")[0] in touched or "user_env と coverity.key_env" in p
            or (p.startswith("verify.") and any(k.startswith("verify.") for k in touched))]
    if mine:
        raise SetupError("この値では保存できません", problems=mine)
    new_text, lost = yamledit.rewrite(text, data)
    changed = new_text != text
    result: dict[str, Any] = {"ok": True, "file": str(path), "changes": changes,
                              "comments": "lost" if lost else "kept",
                              "warnings": [p for p in problems if p not in mine] + warnings}
    if lost:
        result["notice"] = NOTICE
    if check_only:
        return {**result, "check": True, "would_change": changed,
                "next": "変わる項目を表で人に見せ、notice があれば伝えてから同意を得る。同意を得たら --check を付けずに同じ引数で動かす"}
    if changed:
        write_bytes(path, new_text.encode("utf-8"))
    return {**result, "written": changed,
            "next": "config.yaml をコミットしてチームで共有するよう人に伝える" if changed else "変わる値はありませんでした"}


def cmd_set(args: argparse.Namespace) -> dict[str, Any]:
    root, _vcs = need_root(args.repo)
    values: dict[str, Any] = {}
    unset: list[str] = []
    if args.stdin:
        payload = read_stdin_json()
        if not isinstance(payload, dict):
            raise SetupError('標準入力は {"values": {...}, "unset": [...]} の形の JSON にしてください')
        for key, value in (payload.get("values") or {}).items():
            values[known_key(key)] = value
        unset += [known_key(k) for k in payload.get("unset") or []]
    for item in args.value or []:
        if "=" not in item:
            raise SetupError(f"--value は 項目=値 の形で書いてください（今は {item}）")
        key, raw = item.split("=", 1)
        key = known_key(key.strip())
        values[key] = parse_value(key, raw)
    unset += [known_key(k.strip()) for k in args.unset or []]
    if not values and not unset:
        raise SetupError("変える項目がありません（--value 項目=値 か --unset 項目 を渡します）")
    if "coverity.url" in unset:
        raise SetupError("coverity.url は消せません")
    return apply_changes(root, values, unset, args.check)


def cmd_set_verify(args: argparse.Namespace) -> dict[str, Any]:
    root, _vcs = need_root(args.repo)
    values: dict[str, Any] = {"verify.build_command": args.build_command, "verify.default": args.default}
    for name in ("setup_command", "build_dir", "cov_build_args", "cov_analyze_args", "cov_commit_args",
                 "timeout_minutes"):
        value = getattr(args, name)
        if value is not None:
            values[f"verify.{name}"] = value
    if args.initial is not None:
        values["verify.initial"] = args.initial
    return apply_changes(root, values, [], args.check)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cmd_credentials(args: argparse.Namespace) -> dict[str, Any]:
    root, _vcs = find_root(args.repo)
    user_env, key_env = "COV_USER", "COV_AUTH_KEY"
    path = root / CONFIG_DIR / CONFIG_FILE
    if path.is_file():
        try:
            data = load_yaml(path)
        except SetupError:
            data = None
        cov = data.get("coverity") if isinstance(data, dict) and isinstance(data.get("coverity"), dict) else {}
        user_env = cov.get("user_env") if isinstance(cov.get("user_env"), str) else user_env
        key_env = cov.get("key_env") if isinstance(cov.get("key_env"), str) else key_env
    for name in (user_env, key_env):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise SetupError(f"環境変数の名前が正しくありません: {name}（config.yaml の coverity.user_env / key_env）")
    if user_env == key_env:
        raise SetupError("ユーザー名と認証キーに同じ環境変数の名前は使えません（config.yaml の coverity.user_env / key_env）")
    user = args.user if args.user is not None else "<ユーザー名>"
    if args.user is not None and (not user.strip() or "\n" in user):
        raise SetupError("ユーザー名が空か、改行が入っています")
    prompt = f"{key_env} を入力してください（表示されません）"
    ps_key = (f"$s = Read-Host {_ps_quote(prompt)} -AsSecureString; "
              "$b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); "
              f"[Environment]::SetEnvironmentVariable({_ps_quote(key_env)}, "
              "[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b), 'User'); "
              "[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b); Remove-Variable s, b; Write-Host '保存しました'")
    ps_user = f"[Environment]::SetEnvironmentVariable({_ps_quote(user_env)}, {_ps_quote(user)}, 'User')"
    shell = "zsh" if Path(os.environ.get("SHELL", "")).name == "zsh" or sys.platform == "darwin" else "bash"
    profile = "~/.zshrc" if shell == "zsh" else "~/.bashrc"
    key_file = f"~/.{key_env.lower()}.env"
    read = (f"read -rs '?{prompt}: ' v" if shell == "zsh" else f"read -rs -p {shlex.quote(prompt + ': ')} v")
    posix_key = (f"{read}; echo; (umask 077; printf 'export {key_env}=%q\\n' \"$v\" > {key_file}); unset v; "
                 f"grep -qsF {shlex.quote(key_file[2:])} {profile} || "
                 f"printf '%s\\n' {shlex.quote(f'[ -f {key_file} ] && . {key_file}')} >> {profile}; echo '保存しました'")
    posix_user = f"printf '%s\\n' {shlex.quote(f'export {user_env}={shlex.quote(user)}')} >> {profile}"
    platform = "windows" if sys.platform == "win32" else ("macos" if sys.platform == "darwin" else "linux")
    status = {name: bool(envvars.get_env(name)) for name in (user_env, key_env)}
    return {
        "ok": True, "platform": platform, "user_env": user_env, "key_env": key_env,
        "user_set": status[user_env], "key_set": status[key_env],
        "windows": {
            "user": ps_user,
            "key": ps_key,
            "key_from_cmd": f'powershell -NoProfile -Command "{ps_key}"',
            "note": ("key は PowerShell の窓に貼って実行します（コマンド プロンプトなら key_from_cmd）。入力は伏せ字で、"
                     "ユーザーの環境変数に保存されます。保存した後に開いたターミナルと、ユーザーの環境変数を直接読む道具で使えます"),
        },
        "posix": {
            "shell": shell, "profile": profile, "key_file": key_file,
            "user": posix_user,
            "key": posix_key,
            "note": (f"ターミナルで実行します。ユーザー名は {profile} に、認証キーは {key_file}（本人だけが読める）に書き、"
                     f"{profile} から読み込みます。新しいターミナルを開いて、そこから Copilot CLI などを起動し直すと使えます"),
        },
        "rules": ["認証キーはチャットに書かない（貼られたら使わず、作り直しを頼む）",
                  "認証キーはこのスクリプトに渡さない。伏せ字の欄にだけ入れる"],
        "next": ("platform に合うほうのコマンドを人に見せ、人が自分のターミナルで実行する。終わったら check で、"
                 "環境変数が「あり」になったか確かめる"),
    }


def _tail(path: Path) -> str:
    if not path.is_file():
        return ""
    data = path.read_bytes()[-LOG_TAIL_BYTES:]
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _safe_rel(value: str) -> str:
    rel = value.replace("\\", "/").strip().strip("/")
    if not rel:
        return ""
    if Path(rel).is_absolute() or re.match(r"^[A-Za-z]:", rel) or any(part == os.pardir for part in rel.split("/")):
        raise SetupError(f"ビルドするフォルダは、リポジトリの中の相対パスにしてください: {value}")
    return rel


def build_line(setup_command: str, build_dir: str, build_command: str, root: Path) -> str:
    steps = []
    if setup_command.strip():
        setup = setup_command.replace("{root}", str(root))
        steps.append(("call " if sys.platform == "win32" else "") + setup)
    rel = _safe_rel(build_dir)
    if rel:
        target = root / rel
        steps.append(f'cd /d "{target}"' if sys.platform == "win32" else f'cd "{target}"')
    steps.append(build_command)
    return " && ".join(steps)


def build_env(drop: tuple[str, ...]) -> dict[str, str]:
    """The environment of the person's build: no credentials, no Python environment of this script."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV", "PYTHONHOME", *drop)}
    scripts = str(Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin"))
    if sys.prefix != sys.base_prefix and env.get("PATH"):
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                      if os.path.normcase(p.rstrip("\\/")) != os.path.normcase(scripts))
    return env


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def _extract(data: bytes, dest: Path) -> None:
    import io
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
            return
        base = os.path.realpath(dest)
        for member in tar.getmembers():  # older Python: refuse paths that leave the folder
            target = os.path.realpath(os.path.join(dest, member.name))
            if not (target == base or target.startswith(base + os.sep)) or member.issym() or member.islnk() \
                    or member.isdev():
                continue
            tar.extract(member, dest)


def cmd_trial_build(args: argparse.Namespace) -> dict[str, Any]:
    root, vcs = need_root(args.repo)
    path = root / CONFIG_DIR / CONFIG_FILE
    if not path.is_file():
        raise SetupError(f"設定ファイルがありません: {path}（先に init で作ります）")
    data = load_yaml(path)
    data = data if isinstance(data, dict) else {}
    verify = data.get("verify") if isinstance(data.get("verify"), dict) else {}
    cov = data.get("coverity") if isinstance(data.get("coverity"), dict) else {}
    vcs_conf = data.get("vcs") if isinstance(data.get("vcs"), dict) else {}
    build_command = args.build_command if args.build_command is not None else verify.get("build_command") or ""
    setup_command = args.setup_command if args.setup_command is not None else verify.get("setup_command") or ""
    build_dir = args.build_dir if args.build_dir is not None else verify.get("build_dir") or ""
    if not str(build_command).strip():
        raise SetupError("ビルドのコマンドがありません（--build-command で渡します）")
    out_value = data.get("output_dir") if isinstance(data.get("output_dir"), str) else DEFAULT_OUTPUT_DIR
    out_dir = resolve_output(root, out_value)
    if inside(out_dir, root):
        raise SetupError(f"結果を置くフォルダがリポジトリの中です: {out_dir}（set で output_dir をリポジトリの外にします）")
    work = out_dir / TRIAL_DIR
    src = work / "src"
    log = work / "trial-build.log"
    if work.exists():
        shutil.rmtree(work)
    note = ""
    if vcs == "git":
        base = vcs_conf.get("base_branch") if isinstance(vcs_conf.get("base_branch"), str) else base_branch(root)
        revision = None
        if remote_url(root):
            progress(f"origin から {base} を取ってきます")
            fetched = git(root, "fetch", "--quiet", "origin", base, check=False, timeout=300)
            if fetched.returncode == 0 and git(root, "rev-parse", "--verify", "--quiet", f"origin/{base}^{{commit}}",
                                               check=False).returncode == 0:
                revision = out_text(git(root, "rev-parse", f"origin/{base}^{{commit}}"))
            else:
                note = f"origin から {base} を取れなかったため、手元の {base} を使いました。"
        if revision is None:
            proc = git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
            if proc.returncode != 0:
                raise SetupError(f"修正の元にするブランチ {base} が見つかりません（set で vcs.base_branch を直します）")
            revision = out_text(proc)
        progress(f"{revision[:12]} を {src} に書き出します")
        _extract(git(root, "archive", "--format=tar", revision).stdout, src)
    else:
        url = out_text(svn(root, "info", "--show-item", "url", str(root)))
        revision = out_text(svn(root, "info", "-r", "HEAD", "--show-item", "revision", url))
        src.parent.mkdir(parents=True, exist_ok=True)
        svn(root, "export", "--quiet", "--force", "-r", revision, url, str(src))
    line = build_line(str(setup_command), str(build_dir), str(build_command), src)
    key_env = cov.get("key_env") if isinstance(cov.get("key_env"), str) else "COV_AUTH_KEY"
    minutes = args.timeout_minutes or (verify.get("timeout_minutes") if isinstance(verify.get("timeout_minutes"), int)
                                       else 180)
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    progress("ビルドしています（終わるまで待ちます）")
    with open(log, "wb") as handle:
        handle.write(f"$ {line}\n".encode("utf-8"))
        handle.flush()
        group: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                                 else {"start_new_session": True})
        proc = subprocess.Popen(line, cwd=src, shell=True, stdout=handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=build_env((key_env, "COVERITY_PASSPHRASE")), **group)
        try:
            code = proc.wait(timeout=minutes * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            proc.wait()
            code = -1
    ok = code == 0 and not timed_out
    return {"ok": True, "build_ok": ok, "timed_out": timed_out, "seconds": round(time.monotonic() - started, 1),
            "revision": revision, "note": note, "built_in": str(src), "log": str(log),
            "log_tail": "" if ok else _tail(log),
            "next": ("ビルドできました。保存する値を表で見せ、set-verify --check の後に同意を得て保存する" if ok else
                     "log_tail を見て、コマンドの誤りか環境の問題かを人に伝え、直してもう一度試す")}


# ---- the command line ---------------------------------------------------------------------------------


class UsageError(Exception):
    pass


def usage_text(message: str) -> str:
    """argparse's message without the values of unknown arguments (a key typed by mistake must not show)."""
    head, sep, rest = message.partition("unrecognized arguments:")
    if not sep:
        return message
    names = [t.split("=", 1)[0] if t.startswith("-") else "****" for t in rest.split()]
    return head + "知らない引数があります: " + " ".join(names)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def parser() -> Parser:
    top = Parser(prog="setup.py", description="Coverity トリアージの設定（.coverity-triage/）を用意する")
    sub = top.add_subparsers(dest="command", required=True, parser_class=Parser)

    p = sub.add_parser("detect", help="リポジトリの根・git か svn・元のブランチ（読むだけ）")
    p.add_argument("--repo", help="調べるリポジトリのフォルダ（省略すると今のフォルダ）")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("init", help="config.yaml・条件ファイル・knowledge.md を作る")
    p.add_argument("--repo")
    p.add_argument("--url", required=True, help="Coverity Connect の URL")
    p.add_argument("--project", default="", help="プロジェクトの名前")
    p.add_argument("--stream", required=True, help="ストリームの名前")
    p.add_argument("--base-branch", help="修正の元にするブランチ（省略すると detect の値）")
    p.add_argument("--output-dir", help="結果を置くフォルダ（リポジトリの外）")
    p.add_argument("--strip-prefix", action="append", help="警告のパスから外す先頭（何度でも）")
    p.add_argument("--overwrite", action="store_true", help="設定ファイルを作り直す（knowledge.md は残す）")
    p.add_argument("--check", action="store_true", help="何も書かず、書く値の表を返す")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("check", help="設定・環境変数（値は出さない）・出力先・uv を確かめる（読むだけ）")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("set", help="config.yaml の値を変える（注釈は残す）")
    p.add_argument("--repo")
    p.add_argument("--value", action="append", help="項目=値（例：agents.review=true）。何度でも")
    p.add_argument("--unset", action="append", help="消す項目（例：coverity.ca_file）。何度でも")
    p.add_argument("--stdin", action="store_true", help='標準入力から {"values": {...}, "unset": [...]} を読む')
    p.add_argument("--check", action="store_true", help="何も書かず、変わる項目と注釈がどうなるかを返す")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("set-verify", help="ビルドでの検証と初回解析の値を保存する（注釈は残す）")
    p.add_argument("--repo")
    p.add_argument("--build-command", required=True)
    p.add_argument("--setup-command")
    p.add_argument("--build-dir")
    p.add_argument("--default", default="build", choices=["none", "build", "build+analyze"])
    p.add_argument("--initial", dest="initial", action="store_true", default=None)
    p.add_argument("--no-initial", dest="initial", action="store_false")
    p.add_argument("--cov-build-args")
    p.add_argument("--cov-analyze-args")
    p.add_argument("--cov-commit-args")
    p.add_argument("--timeout-minutes", type=int)
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=cmd_set_verify)

    p = sub.add_parser("credentials", help="ユーザー名と認証キーを保存するコマンドを返す（キーは受け取らない）")
    p.add_argument("--repo")
    p.add_argument("--user", help="Coverity のユーザー名（コマンドに入れる）")
    p.set_defaults(func=cmd_credentials)

    p = sub.add_parser("trial-build", help="元のブランチの最新を、リポジトリの外のコピーで1回ビルドする")
    p.add_argument("--repo")
    p.add_argument("--build-command")
    p.add_argument("--setup-command")
    p.add_argument("--build-dir")
    p.add_argument("--timeout-minutes", type=int)
    p.set_defaults(func=cmd_trial_build)
    return top


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    try:
        args = parser().parse_args(argv)
        result = args.func(args)
    except SetupError as exc:
        emit({"ok": False, "error": str(exc), "problems": exc.problems, **exc.details})
        return 1
    except UsageError as exc:
        emit({"ok": False, "error": f"コマンドの引数が正しくありません: {usage_text(str(exc))}", "problems": []})
        return 1
    except OSError as exc:
        emit({"ok": False, "error": f"ファイルやコマンドの操作に失敗しました: {exc}", "problems": []})
        return 1
    except Exception as exc:  # noqa: BLE001 - never a traceback on stdout
        emit({"ok": False, "error": f"予期しないエラーで止まりました: {type(exc).__name__}: {exc}", "problems": []})
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
