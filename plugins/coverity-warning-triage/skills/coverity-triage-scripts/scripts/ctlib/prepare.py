"""Commands of /coverity-setup: prewarm, doctor, detect, init-config, install-agent, trial-build, set-verify."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .common import CtError, out_text, plugin_path, resolve_path, run_cmd
from .config import (CONFIG_DIR, CONFIG_FILE, DEFAULT_FILTER, DEFAULT_OUTPUT_DIR, DEFAULT_PARALLEL, FILTERS_DIR,
                     KNOWLEDGE_TEMPLATE, FilterSpec, ProjectConfig, VerifyConfig, knowledge_path, load_config, output_dir,
                     read_text)
from . import yamledit
from .envvars import get_env
from .vcs import detect as detect_vcs
from .vcs import make_vcs
from .verify import trial_build as run_trial_build

AGENT_SUFFIX = ".agent.md"
AGENT_PREFIX = "coverity-triage-"  # every agent of the plugin; others in the person's folder are not touched
AGENT_ROLES = {"coverity-triage-collector": "取得", "coverity-triage-worker": "作業",
               "coverity-triage-integrator": "統合", "coverity-triage-reviewer": "検証"}
INITIAL_COMMANDS = ("cov-build", "cov-analyze", "cov-commit-defects")
CONFIG_HEADER = ("# Coverity トリアージの設定（/coverity-setup で作成）。チームで共有するためコミットします。\n"
                 "# 認証情報はここに書きません（環境変数に置きます）。\n")
COMMENTS_LOST = ("このファイルでは、注釈は動いたり消えたりします。注釈を元の場所に残せない書き方（「|」「>」で何行かに"
                 "わたる値、「&」「*」で値を使い回す書き方など）があるため、ファイル全体を書き直します。行の頭の「#」の行は"
                 "ファイルの先頭にまとめ、字下げした「#」の行と行の終わりの注釈は消えます（値は正しく保存されます）。"
                 "注釈を残すときは、保存しないで、メモ帳などでファイルを直してください。")


def _uv() -> str:
    """uv itself; ``uv run`` tells the script where it is."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise CtError("uv が見つかりません（/coverity-setup の手順で入れます）")
    return uv


def server_dir() -> Path:
    """The MCP server of the plugin (its uv project)."""
    return plugin_path("server")


def server_env() -> Path:
    """The Python environment uv makes for the MCP server: the folder in ``UV_PROJECT_ENVIRONMENT`` when it
    is set (uv takes a relative path from the server's folder and does not expand ``~``), ``server/.venv``
    otherwise."""
    server = server_dir()
    return server / (os.environ.get("UV_PROJECT_ENVIRONMENT") or ".venv")


def agents_dir() -> Path:
    """The agent definitions of the plugin."""
    return plugin_path("com.github.copilot", "agents")


def user_agents_dir() -> Path:
    """Folder of the person's own custom agents, read by the Copilot CLI.

    The CLI keeps its settings in ``$COPILOT_HOME`` when the variable is set, ``~/.copilot`` otherwise."""
    home = os.environ.get("COPILOT_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".copilot") / "agents"


def plugin_agents() -> dict[str, Path]:
    """The agent definitions of the plugin (``com.github.copilot/agents/*.agent.md``), by name, in the
    order of /coverity-run (collector, worker, integrator, reviewer)."""
    # Only this plugin's own agents: the folder also holds the orchestrator, worker and verifier of the work screen,
    # which the person's folder must not get.
    found = {p.name[:-len(AGENT_SUFFIX)]: p for p in agents_dir().glob(f"{AGENT_PREFIX}*{AGENT_SUFFIX}")}
    order = list(AGENT_ROLES)
    return dict(sorted(found.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else len(order), kv[0])))


# ---- prewarm / install-agent -----------------------------------------------------------------


def prewarm() -> dict[str, Any]:
    """Create the MCP server's Python environment now, so its first start is quick."""
    started = time.monotonic()
    server = server_dir()
    uv = _uv()
    version = out_text(run_cmd([uv, "--version"]))
    run_cmd([uv, "sync", "--native-tls", "--frozen", "--quiet", "--directory", str(server)])
    return {"ok": True, "uv": version, "environment": str(server_env()),
            "seconds": round(time.monotonic() - started, 1)}


# Agents an earlier version of the plugin placed and the plugin no longer has. Only these are removed:
# another file with the same prefix may be the person's own (a copy to try another model, for example).
RETIRED_AGENTS: tuple[str, ...] = ()


def _stale_agents(folder: Path) -> list[Path]:
    """Agents of an earlier version of the plugin that it no longer has (they could still be started)."""
    if not folder.is_dir():
        return []
    current = {p.name.lower() for p in plugin_agents().values()}
    retired = {f"{name}{AGENT_SUFFIX}".lower() for name in RETIRED_AGENTS}
    return sorted(p for p in folder.glob(f"{AGENT_PREFIX}*{AGENT_SUFFIX}")
                  if p.is_file() and p.name.lower() not in current and p.name.lower() in retired)


def agent_state() -> dict[str, dict[str, str]]:
    """Every agent of the plugin in the person's agents folder: current / outdated / missing.

    Old agents the plugin no longer has are listed too, as ``stale``."""
    folder = user_agents_dir()
    out = {}
    for name, source in plugin_agents().items():
        target = folder / source.name
        if not target.is_file():
            status = "missing"
        else:
            status = "current" if target.read_bytes() == source.read_bytes() else "outdated"
        out[name] = {"status": status, "path": str(target)}
    for path in _stale_agents(folder):
        out[path.name[:-len(AGENT_SUFFIX)]] = {"status": "stale", "path": str(path)}
    return out


def install_agent() -> dict[str, Any]:
    """Copy the plugin's agents to the person's agents folder (where the Copilot CLI finds them), and
    remove the old ones the plugin no longer has. Only what changed is written."""
    folder = user_agents_dir()
    agents = {}
    for name, before in agent_state().items():
        target = Path(before["path"])
        if before["status"] == "stale":
            target.unlink()
        elif before["status"] != "current":
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plugin_agents()[name], target)
        status = {"missing": "installed", "outdated": "updated", "current": "unchanged",
                  "stale": "removed"}[before["status"]]
        agents[name] = {"status": status, "path": str(target)}
    return {"ok": True, "folder": str(folder), "agents": agents,
            "restart_needed": any(a["status"] != "unchanged" for a in agents.values())}


# ---- detect / init-config --------------------------------------------------------------------


def _git_ref(repo: Path, ref: str) -> str | None:
    proc = run_cmd(["git", "symbolic-ref", "--short", ref], repo, check=False)
    if proc.returncode != 0:
        return None
    return out_text(proc) or None


def detect(repo_root: str) -> dict[str, Any]:
    """Values decided from the repository, for the person to confirm."""
    repo = resolve_path(repo_root)
    if not repo.is_dir():
        raise CtError(f"フォルダがありません: {repo}")
    vcs = detect_vcs(repo)
    base = None
    if vcs == "git":
        head = _git_ref(repo, "refs/remotes/origin/HEAD")
        base = head.split("/", 1)[1] if head and "/" in head else (_git_ref(repo, "HEAD") or "main")
    return {"ok": True, "repo_root": str(repo), "vcs": vcs,
            "vcs_note": None if vcs else "git / svn のリポジトリの一番上のフォルダ（.git か .svn があるフォルダ）を開いてください",
            "base_branch": base, "has_config": (repo / CONFIG_DIR / CONFIG_FILE).is_file(),
            "output_dir": DEFAULT_OUTPUT_DIR, "output_dir_resolved": str(resolve_path(repo / DEFAULT_OUTPUT_DIR))}


def init_config(repo_root: str, url: str, project: str, stream: str, base_branch: str | None,
                out_dir: str | None, strip_prefixes: list[str], overwrite: bool) -> dict[str, Any]:
    """Write config.yaml, the first filter and knowledge.md, after the person confirmed the values."""
    found = detect(repo_root)
    repo = Path(found["repo_root"])
    if not found["vcs"]:
        raise CtError(found["vcs_note"])
    target = repo / CONFIG_DIR
    config_path = target / CONFIG_FILE
    if config_path.exists() and not overwrite:
        raise CtError("設定ファイルがもうあります。作り直すときは --overwrite を付けます", path=str(config_path))
    config = {
        "coverity": {"url": url.rstrip("/"), "api": "auto", "user_env": "COV_USER", "key_env": "COV_AUTH_KEY",
                     "triage_store": "Default Triage Store", "revision_field": "sourceVersion",
                     "path_strip_prefixes": strip_prefixes},
        "vcs": {"type": found["vcs"], "base_branch": base_branch or found["base_branch"] or "main",
                "branch_prefix": "coverity-fix/"},
        "output_dir": out_dir or DEFAULT_OUTPUT_DIR,
        "max_items": 100,
        "parallel": DEFAULT_PARALLEL,
        "options": {"annotation": False, "per_run_branch": False, "knowledge_suggestions": False, "metrics": False,
                    "markdown": False},
        "verify": VerifyConfig().model_dump(),
    }
    if found["vcs"] == "svn":
        del config["vcs"]["base_branch"], config["vcs"]["branch_prefix"]
    ProjectConfig.model_validate(config)
    out = output_dir(repo, ProjectConfig.model_validate(config))
    if out == repo or repo in out.parents:
        raise CtError(f"出力先はリポジトリの外にしてください: {out}")
    first_filter = {"name": "未トリアージ（20 件まで）", "project": project, "streams": [stream],
                    "triage": {"classification": ["Unclassified"]}, "limit": 20}
    FilterSpec.model_validate(first_filter)
    (target / FILTERS_DIR).mkdir(parents=True, exist_ok=True)
    config_path.write_text(CONFIG_HEADER + yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
    written = [str(config_path)]
    filter_path = target / FILTERS_DIR / DEFAULT_FILTER
    if overwrite or not filter_path.exists():
        filter_path.write_text(yaml.safe_dump(first_filter, allow_unicode=True, sort_keys=False), encoding="utf-8")
        written.append(str(filter_path))
    if not knowledge_path(repo).exists():  # never overwrite what the team wrote
        knowledge_path(repo).write_text(KNOWLEDGE_TEMPLATE, encoding="utf-8")
        written.append(str(knowledge_path(repo)))
    return {"ok": True, "written": written,
            "next": "内容を確かめてコミットし、チームで共有するよう人に伝える（認証情報は入っていません）"}


# ---- doctor ----------------------------------------------------------------------------------


def _check(name: str, status: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail, "how_to_fix": fix}


def doctor(repo_root: str) -> dict[str, Any]:
    """What is still missing on this PC and in this repository. Coverity itself is checked by the MCP
    tool check_connection."""
    repo = resolve_path(repo_root)
    checks = []
    vcs = detect_vcs(repo)
    checks.append(_check("リポジトリ", "ok" if vcs else "ng", f"{vcs or '不明'}（{repo}）",
                         "" if vcs else ".git か .svn があるフォルダを開いてください"))
    if vcs:
        found = shutil.which(vcs)
        checks.append(_check(f"{vcs} コマンド", "ok" if found else "ng", found or "見つかりません",
                             "" if found else f"{vcs} を入れて PATH を通してください"))
    try:  # the plugin's files: a clear "ng" when ct.py is used outside the plugin
        venv_dir = server_env()
        venv = venv_dir.is_dir()
        checks.append(_check("MCP サーバの Python 環境", "ok" if venv else "ng", str(venv_dir),
                             "" if venv else "ct.py prewarm を実行します"))
    except CtError as exc:
        checks.append(_check("MCP サーバの Python 環境", "ng", str(exc)))
    try:
        agents = agent_state()
    except CtError as exc:
        agents = {}
        checks.append(_check("サブエージェント", "ng", str(exc)))
    for name, agent in agents.items():
        if agent["status"] == "stale":
            checks.append(_check(f"古いサブエージェント {name}", "warning",
                                 f"プラグインに無くなった定義が残っています（{agent['path']}）",
                                 "ct.py install-agent で消し、Copilot CLI を再起動します"))
            continue
        role = AGENT_ROLES.get(name)
        checks.append(_check(f"サブエージェント {name}", "ok" if agent["status"] == "current" else "ng",
                             (f"{role}のエージェント。" if role else "")
                             + {"current": "入っています", "missing": "まだ入っていません",
                                "outdated": "プラグインの更新前のものです"}[agent["status"]] + f"（{agent['path']}）",
                             "" if agent["status"] == "current" else "ct.py install-agent を実行し、VS Code（Copilot CLI）を再起動します"))

    config_path = repo / CONFIG_DIR / CONFIG_FILE
    config = None
    if not config_path.is_file():
        checks.append(_check("設定ファイル", "ng", "まだありません", "/coverity-setup で作ります"))
    else:
        try:
            config = load_config(repo)
            checks.append(_check("設定ファイル", "ok", str(config_path)))
        except CtError as exc:
            checks.append(_check("設定ファイル", "ng", str(exc), "表示された項目を直します"))
    if config is not None:
        filters = []
        for path in sorted((repo / CONFIG_DIR / FILTERS_DIR).glob("*.yaml")):
            try:
                spec = FilterSpec.model_validate(yaml.safe_load(read_text(path)) or {})
                filters.append(path.name if len(spec.streams) == 1 else f"{path.name}（ストリームが 1 つでない）")
            except Exception as exc:  # noqa: BLE001 - report every broken file
                filters.append(f"{path.name}（読めません: {str(exc)[:100]}）")
        bad = [f for f in filters if "（" in f]
        checks.append(_check("条件ファイル", "ng" if bad or not filters else "ok", "、".join(filters) or "ありません",
                             "条件ファイルを直します" if bad else ("" if filters else "/coverity-setup で作ります")))
        try:
            out = output_dir(repo, config)
        except CtError as exc:
            checks.append(_check("出力先", "ng", f"{exc}（{exc.details.get('resolve_error', '')}）",
                                 "設定 output_dir に、この PC で開けるフォルダを指定します"))
        else:
            inside = out == repo or repo in out.parents
            checks.append(_check("出力先", "ng" if inside else "ok", str(out),
                                 "リポジトリの外にしてください（誤ってコミットしないため）" if inside else ""))
        if config.coverity.api == "fake":
            checks.append(_check("Coverity", "warning", "偽データを使う設定です（coverity.api: fake）",
                                 "社内の Coverity を使うときは coverity.api を auto にします"))
        else:
            for label, name in (("Coverity のユーザ名", config.coverity.user_env),
                                ("Coverity の認証キー", config.coverity.key_env)):
                ok = bool(get_env(name))
                checks.append(_check(label, "ok" if ok else "ng", f"環境変数 {name}: {'あり' if ok else 'なし'}",
                                     "" if ok else "/coverity-setup の手順で、ターミナルの伏せ字の欄から入れます"))
            if config.coverity.url.lower().startswith("http://"):
                checks.append(_check("Coverity の URL", "warning", "http（認証キーが暗号化されずに送られます）",
                                     "https が使えるか Coverity の管理者に確かめます"))
        if config.vcs.type == "git" and vcs == "git":
            vcs_ops = make_vcs(repo, config.vcs)
            has_base = vcs_ops.has_revision(config.vcs.base_branch) or vcs_ops.has_revision(f"origin/{config.vcs.base_branch}")
            checks.append(_check("取り込み先のブランチ", "ok" if has_base else "ng", config.vcs.base_branch,
                                 "" if has_base else "設定 vcs.base_branch を直します"))
            remote = out_text(run_cmd(["git", "remote", "get-url", "origin"], repo, check=False))
            checks.append(_check("git のリモート origin", "ok" if remote else "warning", remote or "ありません",
                                 "" if remote else "修正を push できません（反映のときに必要）"))
        if config.verify.initial:
            checks += _initial_checks(config)
    ready = all(c["status"] != "ng" for c in checks)
    return {"ok": True, "ready": ready, "checks": checks,
            "next": "Coverity への接続は MCP の check_connection で確かめる" if ready else "status が ng の項目を直す"}


def _initial_checks(config: ProjectConfig) -> list[dict[str, str]]:
    """The initial analysis (option verify.initial): the build command, the Coverity commands and the
    credentials cov-commit-defects uses."""
    verify, coverity = config.verify, config.coverity
    checks = [_check("初回解析のビルドのコマンド", "ok" if verify.build_command else "ng",
                     verify.build_command or "設定 verify.build_command がありません",
                     "" if verify.build_command else "/coverity-setup の「ビルドでの検証」で設定します")]
    fake = coverity.api == "fake"  # nothing is committed with the fake data
    for name in INITIAL_COMMANDS:
        if fake and name == "cov-commit-defects":
            continue
        found = shutil.which(name)
        if found:
            checks.append(_check(f"初回解析の {name}", "ok", found))
        elif verify.setup_command:  # the setup command may add the Coverity tools to PATH
            checks.append(_check(f"初回解析の {name}", "warning",
                                 "PATH にありません（環境設定のコマンドで足されるなら問題ありません）",
                                 "環境設定のコマンドで PATH に足されるか確かめます"))
        else:
            checks.append(_check(f"初回解析の {name}", "ng", "PATH にありません",
                                 "Coverity の解析のツール（bin フォルダ）を PATH に足すか、環境設定のコマンドで足します"))
    if not fake:
        missing = [name for name in (coverity.user_env, coverity.key_env) if not get_env(name)]
        checks.append(_check("初回解析の登録の認証情報", "ng" if missing else "ok",
                             f"環境変数 {'・'.join(missing)} がありません" if missing else
                             f"環境変数 {coverity.user_env} と {coverity.key_env}（認証キーは COVERITY_PASSPHRASE で渡し、"
                             "コマンドラインとログに出しません）",
                             "/coverity-setup の手順で、ターミナルの伏せ字の欄から入れます" if missing else ""))
    return checks


# ---- build verification settings (option) ---------------------------------------------------


def trial_build(repo_root: str, setup_command: str, build_command: str, build_dir: str,
                config: ProjectConfig | None = None) -> dict[str, Any]:
    """Build the latest code once with the given commands, before saving them.

    ``config`` is the settings not saved yet (the settings screen); the saved file otherwise.
    """
    repo = resolve_path(repo_root)
    config = config or load_config(repo)
    settings = config.verify.model_copy(update={"setup_command": setup_command, "build_command": build_command,
                                                "build_dir": build_dir})
    work = output_dir(repo, config) / "_trial-build"
    if work.exists():
        shutil.rmtree(work)
    vcs = make_vcs(repo, config.vcs)
    latest, note = vcs.resolve_latest()
    vcs.export(latest, work / "src")
    result = run_trial_build(settings, work / "src", work / "trial-build.log", (config.coverity.key_env,))
    return {"ok": True, **result, "revision": latest, "note": note, "built_in": str(work / "src")}


def set_verify(repo_root: str, default: str, setup_command: str, build_command: str, build_dir: str,
               cov_build_args: str | None, cov_analyze_args: str | None, initial: bool | None = None,
               cov_commit_args: str | None = None, timeout_minutes: int | None = None,
               check: bool = False) -> dict[str, Any]:
    """Save the build verification and initial analysis settings the person agreed to.

    The values given as None keep what the settings have. Only the values that change are rewritten, and
    the comments in the file stay where they are (yamledit); a file written in a way that cannot be kept
    is written whole, as before ("comments": "lost"). ``check``: write nothing, only tell what would
    become of the comments (asked before the person agrees)."""
    repo = resolve_path(repo_root)
    path = repo / CONFIG_DIR / CONFIG_FILE
    text = read_text(path)
    header = "".join(line + "\n" for line in text.removeprefix("\ufeff").splitlines() if line.startswith("#"))
    data = yaml.safe_load(text) or {}
    current = data.get("verify") if isinstance(data.get("verify"), dict) else {}
    agreed = {"default": default, "setup_command": setup_command, "build_dir": build_dir, "build_command": build_command,
              "cov_build_args": cov_build_args, "cov_analyze_args": cov_analyze_args, "initial": initial,
              "cov_commit_args": cov_commit_args, "timeout_minutes": timeout_minutes}
    defaults = VerifyConfig().model_dump()
    verify = dict(current)
    for key, value in agreed.items():
        # a value not in the file is written only when it differs from the default (REQ-102)
        if value is not None and (key in current or value != defaults.get(key)):
            verify[key] = value
    if verify or "verify" in data:
        data["verify"] = verify
    try:
        ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise CtError("設定の値が正しくありません",
                      problems=[f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]) from exc
    out = yamledit.edit(text, data)
    comments = "kept"
    if out is None:
        out = header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        comments = "lost" if yamledit.has_comments(text) else "kept"
    if check:
        return {"ok": True, "check": True, "comments": comments, "verify": verify,
                **({"notice": COMMENTS_LOST} if comments == "lost" else {})}
    path.write_text(out, encoding="utf-8")
    return {"ok": True, "written": str(path), "verify": verify, "comments": comments}
