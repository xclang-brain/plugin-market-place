"""MCP server ``coverity-triage``: search Coverity Connect issues, read warning paths, write triage.

The server stands alone: it needs the settings file ``<repo_root>/.coverity-triage/config.yaml``
(its ``coverity`` section) and the credentials in environment variables, nothing else.

Each tool returns within about 20 seconds, because MCP clients such as the Copilot runtime stop a
tool call after 30 seconds by default.

Results are passed in one of two ways:

- Files: with ``output_file`` / ``output_dir`` / ``plan_file`` the result is saved in that file, so that
  large data does not pass through the conversation. When work remains the tool returns
  ``done: false`` and is called again with the same arguments; work already saved is not repeated.
- Inline: without them the result is in the tool result, kept small (search: ``next_offset`` to
  continue, at most 100 issues; warning paths: at most 10 CIDs).

Writing triage takes two calls: ``preview_triage`` returns what will be written and a confirmation
token, and ``update_triage`` writes only with that token, after a person agreed.

A call whose folder holds ``run.json`` (a run folder) is also written to that folder's
``operations.log`` (start, end or error).
"""

from __future__ import annotations

import functools
import getpass
import hashlib
import inspect
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.runner import serve_loop
from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from .connect import check_auth
from .coverity import CoverityClient, CoverityError, make_client
from .envvars import get_env
from .models import TriageAttributes, TriageEntry
from .settings import (CoverityConfig, FilterArg, FilterSpec, SettingsError, load_coverity_config, load_filter,
                       parse_filter)

BUDGET = 20.0  # seconds one tool call may use
DEFAULT_LIMIT = 100  # issues saved by a search to a file, when the filter has no limit
INLINE_LIMIT, MAX_INLINE_LIMIT = 20, 100  # issues returned by one search without a file
MAX_CIDS = 50  # warning paths saved by one call to files
MAX_INLINE_CIDS = 10  # warning paths returned by one call without files
MAX_EXCLUDED_SAMPLES = 50  # rows left out after the search, kept in issues.json to find the cause
INLINE_FIELDS = ("cid", "checker", "file", "line", "function", "impact", "category", "cwe",
                 "classification", "action", "severity", "status")
CALL_AGAIN = "続きがあります。同じ引数でもう一度呼んでください"
RESULT_DIR = "coverity-triage-mcp"  # <RESULT_DIR>-<user> in the temporary folder: write-backs passed as entries
RUN_FILE = "run.json"  # marks a run folder
LOG_FILE = "operations.log"  # the run folder's log (one JSON object per line)

mcp = MCPServer(
    name="coverity-triage",
    instructions=(
        "Coverity Connect の警告を検索し、警告経路を取り、トリアージを書き戻すツールです。\n"
        "- 設定：<repo_root>/.coverity-triage/config.yaml の coverity 節を読みます（url は必須）。"
        "ユーザ名と認証キーは、環境変数（既定は COV_USER と COV_AUTH_KEY。名前は coverity.user_env と"
        " coverity.key_env で変えられる）から読みます。パスの引数は、どれも絶対パスで渡します。\n"
        "- 条件（search_issues の filter、または filter_file の JSON）：{\"streams\": [ストリーム名を1つ], "
        "\"checkers\": [...]（* ? [ が使える）, \"impacts\": [...], \"triage\": {\"status\": [...], "
        "\"classification\": [...], \"action\": [...]}, \"limit\": 件数}。同じ項目の値はどれかに合えばよく、"
        "項目どうしはすべてに合う警告を返します。\n"
        "- 結果の受け取り方：output_file（search_issues）や output_dir（get_issues）を渡すと、ファイルに保存します。"
        "結果に done: false があれば、同じ引数でもう一度呼びます（保存済みの続きから進みます）。"
        "渡さなければ、結果をツールの結果で返します。search_issues は next_offset が null でなければ offset に"
        "渡して呼び直し、get_issues は remaining の CID で呼び直します。\n"
        "- 書き戻し：先に preview_triage を呼び、件数と内容を人に見せて同意を得てから、同じ plan_file か entries と、"
        "返った confirmation_token を update_triage に渡します。done: false なら同じ引数でもう一度呼びます"
        "（plan_file でも entries でも）。"
    ),
)


class Activity:
    """Lines of one tool call in the run folder's operations.log (shown by monitors of the run).

    Only a call on the files of a run folder (a folder with run.json) is recorded. Recording is best
    effort: a failure to write never changes what the tool does. Values of the credential environment
    variables are masked, as the scripts do.
    """

    def __init__(self, tool: str, run_dir: Path | None, repo_root: Any, fields: dict[str, Any]):
        self.tool = tool
        self.run_dir = run_dir
        self.repo_root = repo_root
        self.fields = fields
        self._secrets: list[str] | None = None

    @classmethod
    def of(cls, func: Callable[..., Any], locate: Callable[[dict[str, Any]], tuple[Path | None, dict[str, Any]]] | None,
           args: tuple[Any, ...], kwargs: dict[str, Any]) -> Activity:
        run_dir, fields, repo_root = None, {}, None
        if locate is not None:
            try:
                arguments = inspect.signature(func).bind_partial(*args, **kwargs).arguments
                repo_root = arguments.get("repo_root")
                folder, fields = locate(arguments)
                if folder is not None and folder.is_absolute() and (folder / RUN_FILE).is_file():
                    run_dir = folder
            except Exception:  # noqa: BLE001 - bad arguments are reported by the tool itself
                run_dir = None
        return cls(func.__name__, run_dir, repo_root, fields)

    def secrets(self) -> list[str]:
        if self._secrets is None:
            names = ["COV_USER", "COV_AUTH_KEY"]
            try:
                config = load_coverity_config(self.repo_root)
                names = [config.user_env, config.key_env]
            except Exception:  # noqa: BLE001 - the settings may be what failed
                pass
            self._secrets = [v for v in (get_env(n) for n in names) if v]
        return self._secrets

    def _mask(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.secrets():
                value = value.replace(secret, "****")
            return value
        if isinstance(value, dict):
            return {k: self._mask(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._mask(v) for v in value]
        return value

    def write(self, phase: str, level: str = "info", **fields: Any) -> None:
        if self.run_dir is None:
            return
        try:
            record = {"time": _now(), "event": f"mcp:{self.tool}", "level": level, "source": "mcp",
                      "phase": phase, **self.fields, **fields}
            line = json.dumps(self._mask(record), ensure_ascii=False, default=str)
            with open(self.run_dir / LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001 - the log never stops the tool
            pass

    def end(self, result: Any, outcome: Callable[[Any], tuple[str, dict[str, Any]]] | None) -> None:
        if self.run_dir is None:
            return
        try:
            level, fields = outcome(result) if outcome else ("info", {})
        except Exception:  # noqa: BLE001 - the log never stops the tool
            level, fields = "info", {}
        self.write("end", level, **fields)


def tool(title: str, locate: Callable[[dict[str, Any]], tuple[Path | None, dict[str, Any]]] | None = None,
         outcome: Callable[[Any], tuple[str, dict[str, Any]]] | None = None, **hints: bool):
    """Register a tool and turn expected errors into messages the agent can act on.

    ``locate`` gives, from the arguments, the folder that may be a run folder (None for a call without
    files) and the fields of every line of the call in its operations.log; ``outcome`` gives the level
    and fields of the end line from the result (a partial failure is a warning). An error is critical."""
    def register(func):
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            activity = Activity.of(func, locate, args, kwargs)
            activity.write("start")
            try:
                result = func(*args, **kwargs)
            except (SettingsError, CoverityError, ValueError, OSError) as exc:
                activity.write("error", "critical", error=str(exc))
                raise ToolError(str(exc)) from exc
            except Exception as exc:
                activity.write("error", "critical", error=f"{type(exc).__name__}: {exc}")
                raise
            activity.end(result, outcome)
            return result
        annotations = ToolAnnotations(title=title, open_world_hint=False, **hints)
        return mcp.tool(title=title, annotations=annotations)(wrapper)
    return register


class Deadline:
    def __init__(self, budget: float = BUDGET):
        self.started = time.monotonic()
        self.budget = budget

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def left(self) -> float:
        return self.budget - self.elapsed()

    def allows(self, client: CoverityClient, requests: int = 1) -> bool:
        """Whether ``requests`` more requests are expected to end within the budget."""
        return self.elapsed() + requests * client.request_seconds() <= self.budget


def _path(value: str, name: str) -> Path:
    # The server runs in its own folder (uv run --directory), so a relative path would point there.
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} は絶対パスで指定してください: {value}")
    return path


def _folder(value: str, name: str) -> Path:
    path = _path(value, name)
    if not path.is_dir():
        raise ValueError(f"{name} のフォルダがありません: {path}")
    return path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"ファイルを読めません: {path}: {exc}") from exc


def _write_json(path: Path, data: Any) -> None:
    """Write via a temporary file so an interrupted call never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Without the items that have no value (None, empty text, empty list)."""
    return {k: v for k, v in data.items() if v is not None and v != "" and v != []}


def _one_of(name_a: str, a: Any, name_b: str, b: Any) -> None:
    if (a is None) == (b is None):
        raise ValueError(f"{name_a} か {name_b} の、どちらか一方を渡してください")


@tool("Coverity 接続の確認", read_only_hint=True)
def check_connection(repo_root: str) -> dict[str, Any]:
    """設定ファイル（<repo_root>/.coverity-triage/config.yaml）の Coverity に、認証情報の環境変数で
    1 回だけ読み取りの問い合わせをし、認証できるかを確かめる（10 秒以内）。認証キーの値は返さない。"""
    config = load_coverity_config(_folder(repo_root, "repo_root"))
    if config.api == "fake":
        make_client(config).close()  # checks that the file exists
        return {"ok": True, "result": "偽データを使う設定です（coverity.api: fake）", "fake_data": config.fake_data}
    out = check_auth(config)
    out["url"] = config.url
    if config.url.lower().startswith("http://"):
        out["warning"] = ("URL が http です。認証キーが暗号化されずに送られます。"
                          "https が使えるか Coverity の管理者に確認してください")
    return out


def _search_log(args: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    return (Path(args["output_file"]).parent if args.get("output_file") else None), {}


def _search_outcome(out: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    # Without the analyzed revision the run goes on with the local code: a partial failure.
    return ("warning" if out["revision_error"] else "info",
            {"done": out["done"], "found": out["found"], "limited": out["limited"], "excluded": out["excluded"],
             "revision_error": out["revision_error"]})


@tool("Coverity の警告の検索", locate=_search_log, outcome=_search_outcome,
      read_only_hint=False, destructive_hint=False, idempotent_hint=True)
def search_issues(repo_root: str, filter_file: str | None = None, output_file: str | None = None,
                  filter: FilterArg | None = None, offset: int = 0) -> dict[str, Any]:
    """Coverity の警告を検索する。条件は filter_file（条件の JSON のファイル）か filter（同じ形の object：
    streams（1 つだけ）、project、checkers、impacts、triage{status, classification, action}、limit）の、
    どちらか一方で渡す。filter にほかの項目があるとエラーにする。project は Coverity Connect の検索の
    絞り込みには使わない（どの警告を探すかはストリームで決まる）。
    output_file を渡すと、結果をそのファイルに保存して件数を返す。条件の limit 件（既定 100）で止め、
    最新スナップショットの解析リビジョンも記録する。done が false なら、同じ引数でもう一度呼ぶ
    （保存済みの続きから検索する）。外した警告と合わなかった項目の値は output_file の excluded に残る。
    output_file を渡さないと、警告を issues で返す（cid, checker, file, line, function, impact, category,
    cwe, classification, action, severity, status のうち値のあるもの）。1 回に返すのは limit 件（既定 20、
    上限 100）まで。next_offset が null でなければ、offset に渡して呼び直すと続きを返す。
    found は保存した（返した）警告の数。coverity_total は Coverity が検索で返した件数（ストリーム・
    チェッカー・Impact・Classification・Action で絞った数）。excluded は、そのうち Status とワイルド
    カードのチェッカーの条件に合わずに外した件数。"""
    deadline = Deadline()
    config = load_coverity_config(_folder(repo_root, "repo_root"))
    _one_of("filter_file", filter_file, "filter", filter)
    if filter_file is not None:
        filter_path = _path(filter_file, "filter_file")
        spec = load_filter(filter_path)
        filter_sha = _sha(filter_path.read_bytes())[:12]
    else:
        spec = parse_filter(filter)
        filter_sha = _sha(spec.model_dump_json().encode("utf-8"))[:12]
    if offset < 0:
        raise ValueError("offset は 0 以上で指定してください")
    if output_file is None:
        return _search_inline(config, spec, offset, deadline)
    if offset:
        raise ValueError("offset は output_file を渡さないときだけ使います（output_file を渡したときは、"
                         "同じ引数で呼び直すと続きから検索します）")
    out = _path(output_file, "output_file")
    _folder(str(out.parent), "output_file の保存先")
    state = _read_json(out) if out.is_file() else None
    if not isinstance(state, dict) or state.get("filter_sha") != filter_sha:
        state = {"filter_sha": filter_sha, "stream": spec.streams[0], "limit": spec.limit or DEFAULT_LIMIT,
                 "started_at": _now(), "finished_at": None, "coverity_total": None, "next_offset": 0,
                 "limited": False, "revision_field": config.revision_field or None,
                 "revision_checked": not config.revision_field, "analyzed_revision": None,
                 "revision_error": None, "issues": [], "excluded_count": 0, "excluded": [],
                 "requests": []}
    limit = state["limit"]
    client = make_client(config)
    try:
        if state["next_offset"] is not None and deadline.allows(client):
            page = client.search(spec, limit - len(state["issues"]), state["next_offset"], deadline.left())
            known = {i["cid"] for i in state["issues"]}
            # A new snapshot between calls can shift the pages; never keep a CID twice.
            found = [i.model_dump(exclude_none=True) for i in page.issues if i.cid not in known]
            state["issues"] += found
            seen = {e["cid"] for e in state.setdefault("excluded", [])}
            new = [e for e in page.excluded if e["cid"] not in seen]
            state["excluded_count"] = state.get("excluded_count", 0) + len(new)
            state["excluded"] += new[:max(0, MAX_EXCLUDED_SAMPLES - len(state["excluded"]))]
            state["coverity_total"] = page.total
            if len(state["issues"]) >= limit:
                state["limited"] = len(state["issues"]) > limit or page.next_offset is not None
                state["issues"] = state["issues"][:limit]
                state["next_offset"] = None
            else:
                state["next_offset"] = page.next_offset
        if state["next_offset"] is None and not state["revision_checked"] and deadline.allows(client, 2):
            try:
                state["analyzed_revision"] = client.snapshot_revision(state["stream"], state["revision_field"])
            except CoverityError as exc:
                if exc.fatal:
                    raise
                state["revision_error"] = str(exc)  # the search is still usable without the revision
            state["revision_checked"] = True
    finally:
        state["requests"] += client.requests
        client.close()
        done = state["next_offset"] is None and state["revision_checked"]
        if done and not state["finished_at"]:
            state["finished_at"] = _now()
        _write_json(out, state)
    excluded = state.get("excluded_count", 0)
    return {"done": done, "found": len(state["issues"]), "limit": limit, "limited": state["limited"],
            "coverity_total": state["coverity_total"], "excluded": excluded,
            "excluded_note": (f"Coverity が返した警告のうち {excluded} 件が、検索のあとの条件（Status など）に"
                              f"合わず外れました。合わなかった項目と値は {out} の excluded にあります")
            if excluded else None, "analyzed_revision": state["analyzed_revision"],
            "revision_error": state["revision_error"], "seconds": round(deadline.elapsed(), 1),
            "output_file": str(out), "next": None if done else CALL_AGAIN}


def _search_inline(config: Any, spec: FilterSpec, offset: int, deadline: Deadline) -> dict[str, Any]:
    """One page of issues in the tool result. ``offset`` counts the rows Coverity returned."""
    limit = spec.limit or INLINE_LIMIT
    if limit > MAX_INLINE_LIMIT:
        raise ValueError(f"結果をツールの結果で返すときの limit は {MAX_INLINE_LIMIT} 件までです（{limit} 件）。"
                         "もっと多く取るときは output_file を渡してください")
    client = make_client(config)
    try:
        page = client.search(spec, limit, offset, deadline.left())
    finally:
        client.close()
    issues, next_offset, excluded = page.issues, page.next_offset, len(page.excluded)
    if len(issues) > limit:
        # Continue just after the last issue returned, so that none is skipped.
        next_offset = page.ends[limit - 1]
        excluded = sum(1 for end in page.excluded_ends if end <= next_offset)
        issues = issues[:limit]
        if page.total is not None and next_offset >= page.total:
            next_offset = None
    found = [_compact({k: getattr(i, k) for k in INLINE_FIELDS}) for i in issues]
    return {"issues": found, "found": len(found),
            "next_offset": next_offset, "coverity_total": page.total, "excluded": excluded,
            "seconds": round(deadline.elapsed(), 1),
            "next": None if next_offset is None else f"続きがあります。offset に {next_offset} を渡して呼び直してください"}


def _issues_log(args: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    # output_dir is <run>/items/<ID>
    if not args.get("output_dir"):
        return None, {}
    folder = Path(args["output_dir"])
    return folder.parent.parent, {"item": folder.name, "cids": list(args.get("cids") or [])[:MAX_CIDS]}


def _issues_outcome(out: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    failed = [int(c) if str(c).isdigit() else c for c in out["failed"]]
    return ("warning" if failed else "info",
            {"done": out["done"], "saved": len(out["saved"]), "already_saved": len(out["already_saved"]),
             "failed": failed, "remaining": len(out["remaining"])})


@tool("Coverity の警告経路の取得", locate=_issues_log, outcome=_issues_outcome,
      read_only_hint=False, destructive_hint=False, idempotent_hint=True)
def get_issues(repo_root: str, stream: str, cids: list[int], output_dir: str | None = None) -> dict[str, Any]:
    """CID ごとに、ストリーム stream での警告経路（イベント）とチェッカーの説明を取る。
    output_dir（あるフォルダ）を渡すと、issue-<CID>.json として保存する（cids は 50 件まで）。取れなかった
    CID は、理由を error に書いた issue-<CID>.json を保存する。保存済みの CID は取り直さない。
    done が false なら、同じ引数でもう一度呼ぶ。
    output_dir を渡さないと、issues で返す（cids は 10 件まで）。CID ごとに {cid, checker, file, line,
    function, events: [{file, line, tag, description, main}], checker_description}（値のない項目は省く）。
    取れなかった CID は failed に理由を返す。remaining が空でなければ、その CID だけで呼び直す。"""
    deadline = Deadline()
    config = load_coverity_config(_folder(repo_root, "repo_root"))
    folder = _folder(output_dir, "output_dir") if output_dir is not None else None
    wanted = list(dict.fromkeys(cids))
    most = MAX_CIDS if folder is not None else MAX_INLINE_CIDS
    if not wanted or len(wanted) > most:
        raise ValueError(f"cids は 1〜{most} 件で指定してください（{len(wanted)} 件）"
                         + ("" if folder is not None else "。もっと多いときは output_dir を渡してください"))
    if folder is None:
        return _issues_inline(config, stream, wanted, deadline)
    saved: list[int] = []
    already: list[int] = []
    failed: dict[str, str] = {}
    remaining: list[int] = []
    client = make_client(config)
    try:
        for cid in wanted:
            file = folder / f"issue-{cid}.json"
            if file.is_file():
                already.append(cid)
            elif not deadline.allows(client):
                remaining.append(cid)
            else:
                try:
                    detail = client.issue_detail(cid, stream)
                except CoverityError as exc:
                    if exc.fatal:
                        raise
                    failed[str(cid)] = str(exc)
                    _write_json(file, {"cid": cid, "stream": stream, "fetched_at": _now(), "error": str(exc)})
                    continue
                _write_json(file, {"fetched_at": _now(), **detail.model_dump(exclude_none=True)})
                saved.append(cid)
    finally:
        client.close()
    done = not remaining
    return {"done": done, "saved": saved, "already_saved": already, "failed": failed,
            "remaining": remaining, "seconds": round(deadline.elapsed(), 1),
            "next": None if done else CALL_AGAIN}


def _issues_inline(config: Any, stream: str, cids: list[int], deadline: Deadline) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    remaining: list[int] = []
    client = make_client(config)
    try:
        for cid in cids:
            if not deadline.allows(client):
                remaining.append(cid)
                continue
            try:
                detail = client.issue_detail(cid, stream)
            except CoverityError as exc:
                if exc.fatal:
                    raise
                failed[str(cid)] = str(exc)
                continue
            issue = detail.issue
            issues.append(_compact({"cid": issue.cid, "checker": issue.checker, "file": issue.file, "line": issue.line,
                                    "function": issue.function,
                                    "events": [_compact(e.model_dump()) for e in detail.events],
                                    "checker_description": detail.checker_description}))
    finally:
        client.close()
    return {"issues": issues, "failed": failed, "remaining": remaining, "seconds": round(deadline.elapsed(), 1),
            "next": "時間が足りず、remaining の CID は取っていません。その CID だけを cids に渡して呼び直してください"
            if remaining else None}


def _triage_log(args: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    return (Path(args["plan_file"]).parent if args.get("plan_file") else None), {}


def _triage_outcome(out: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    failed = [f["id"] for f in out["failed"]]
    return ("warning" if failed else "info",
            {"done": out["done"], "written": out["written"], "written_total": out["written_total"],
             "entries": out["entries"], "failed": failed, "remaining": len(out["remaining"])})


def entries_token(entries: list[TriageEntry]) -> str:
    """Confirmation token of entries: SHA-256 of the normalized {"triage": entries} as compact JSON."""
    text = json.dumps({"triage": [e.model_dump() for e in entries]}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return _sha(text.encode("utf-8"))[:12]


def _entries(entries: list[TriageEntry] | list[dict[str, Any]]) -> list[TriageEntry]:
    try:
        return [e if isinstance(e, TriageEntry) else TriageEntry.model_validate(e) for e in entries]
    except ValidationError as exc:
        raise ValueError(f"entries が正しくありません: {exc}") from exc


def _plan_bytes(plan_file: str) -> tuple[Path, bytes]:
    path = _path(plan_file, "plan_file")
    if not path.is_file():
        raise ValueError(f"plan_file がありません: {path}")
    return path, path.read_bytes()


def _plan_entries(path: Path, raw: bytes) -> list[TriageEntry]:
    try:
        return [TriageEntry.model_validate(e) for e in json.loads(raw.decode("utf-8")).get("triage") or []]
    except (ValueError, AttributeError, ValidationError) as exc:
        raise ValueError(f"書き戻しの内容を読めません: {path}: {exc}") from exc


@tool("Coverity へのトリアージの書き戻しの確認", read_only_hint=True)
def preview_triage(plan_file: str | None = None, entries: list[TriageEntry] | None = None) -> dict[str, Any]:
    """書き戻しの内容を確かめ、update_triage に渡す確認用の文字列を返す。読むだけで、Coverity とは通信しない。
    plan_file（{"triage": [...]} の JSON のファイル）か entries（[{id, cids, classification, action,
    severity?, comment?}]）の、どちらか一方を渡す。
    confirmation_token の作り方：plan_file なら、ファイルのバイト列の SHA-256 の 16 進の先頭 12 文字。
    entries なら、各項目を {id, cids, classification, action, severity, comment}（severity と comment が
    なければ空の文字列）にそろえた {"triage": entries} を json.dumps(ensure_ascii=False, sort_keys=True,
    separators=(",", ":")) で文字列にし、UTF-8 のバイト列にした SHA-256 の 16 進の先頭 12 文字。
    counts（書き戻す項目の数と CID の数）と items（書き戻す内容）を人に見せ、同意を得てから update_triage を呼ぶ。"""
    _one_of("plan_file", plan_file, "entries", entries)
    if plan_file is not None:
        path, raw = _plan_bytes(plan_file)
        items, token = _plan_entries(path, raw), _sha(raw)[:12]
    else:
        items = _entries(entries)
        token = entries_token(items)
    return {"counts": {"entries": len(items), "cids": len({c for e in items for c in e.cids})},
            "items": [e.model_dump() for e in items], "confirmation_token": token,
            "next": ("件数と内容を人に見せ、同意を得てから update_triage に confirmation_token と、"
                     "同じ plan_file か entries を渡してください")}


def _user_name() -> str:
    """The user's name for a folder name; the uid when the name is not known."""
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no name in the environment nor in the user database
        name = str(os.getuid()) if hasattr(os, "getuid") else ""
    return re.sub(r"[^\w.-]", "_", name) or "user"


def _entries_result_path(config: CoverityConfig) -> tuple[Path, str]:
    """The result file of write-backs passed as entries, and the Coverity URL it is for.

    One folder per user in the temporary folder: in a shared /tmp, a folder made by one user cannot
    be written by the others. One file per Coverity Connect and triage store: an item written there
    is known by its content, whatever the order of the entries or the confirmation token."""
    folder = Path(tempfile.gettempdir()) / f"{RESULT_DIR}-{_user_name()}"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if hasattr(os, "getuid") and folder.stat().st_uid != os.getuid():
        raise ValueError(f"書き戻しの結果のフォルダが、ほかのユーザーのものです: {folder}。"
                         "環境変数 TMPDIR に、自分だけが使う一時のフォルダを指定してください")
    url = config.url.rstrip("/")
    name = _sha(json.dumps([url, config.triage_store], ensure_ascii=False).encode("utf-8"))[:12]
    return folder / f"triage-{name}.result.json", url


@tool("Coverity へのトリアージの書き戻し", locate=_triage_log, outcome=_triage_outcome,
      read_only_hint=False, destructive_hint=True, idempotent_hint=True)
def update_triage(repo_root: str, confirmation_token: str, plan_file: str | None = None,
                  entries: list[TriageEntry] | None = None) -> dict[str, Any]:
    """トリアージ（分類・対応・重大度・コメント）を Coverity に書き込む。plan_file か entries の、どちらか
    一方を渡す（preview_triage に渡したものと同じ）。confirmation_token は preview_triage が返した確認用の
    文字列で、人が件数と内容を見て同意した後にだけ渡す。中身と合わなければ何も書かない。
    結果は、plan_file なら同じフォルダの <名前>.result.json に、entries なら一時のフォルダの
    coverity-triage-mcp-<ユーザー名>/ の、Coverity の URL とトリアージストアごとのファイルに残す（result_file）。
    書き込み済みの項目（id・cids・値・コメントが同じもの）は、並べ方や確認用の文字列が変わっても書き直さない。
    Coverity が1つの項目を値の誤り（401・403 以外の 4xx）で断っても（failed）、ほかの項目は書き込む。その項目は、
    同じ確認用の文字列では書き直さない（値を直して preview_triage からやり直す）。認証の失敗（401・403）と
    5xx では、呼び出し全体を止める。止まった項目は記録しないので、サーバーが戻ってから同じ引数で呼ぶと書き込む。
    done が false なら、同じ引数でもう一度呼ぶ。"""
    deadline = Deadline()
    config = load_coverity_config(_folder(repo_root, "repo_root"))
    _one_of("plan_file", plan_file, "entries", entries)
    if plan_file is not None:
        plan_path, raw = _plan_bytes(plan_file)
        token = _sha(raw)[:12]
    else:
        items = _entries(entries)
        token = entries_token(items)
    if confirmation_token.strip() != token:
        raise ValueError("確認用の文字列が書き戻しの内容と一致しません。preview_triage をやり直し、"
                         "件数と内容を人に見せて同意を得てから呼んでください")
    if plan_file is not None:
        items = _plan_entries(plan_path, raw)
        result_path = plan_path.with_name(plan_path.stem + ".result.json")
        result = _read_json(result_path) if result_path.is_file() else {}
    else:
        # Kept in the temporary folder so that a call again resumes and never writes twice.
        result_path, url = _entries_result_path(config)
        result = _read_json(result_path) if result_path.is_file() else {}
        target = {"url": url, "triage_store": config.triage_store}
        if not isinstance(result, dict) or {k: result.get(k) for k in target} != target:
            result = target
    result.setdefault("written", {})
    result.setdefault("failed", {})
    result.setdefault("requests", [])
    written_now: list[str] = []
    remaining: list[str] = []
    client = make_client(config)
    try:
        for entry in items:
            # Keyed by content: an entry the person changed after writing is written again.
            key = _sha(entry.model_dump_json().encode("utf-8"))[:16]
            failure = result["failed"].get(key, {})
            if key in result["written"] or failure.get("token") == token:
                continue
            if not deadline.allows(client):
                remaining.append(entry.id)
                continue
            attributes = TriageAttributes(classification=entry.classification, action=entry.action,
                                          severity=entry.severity)
            try:
                client.write_triage(entry.cids, attributes, entry.comment)
            except CoverityError as exc:
                if exc.fatal:
                    raise
                # A value Coverity refused (4xx) is not sent again with this token: the entry is to be
                # corrected and confirmed again. (5xx is fatal and raised above, so it leaves no record.)
                result["failed"][key] = {"id": entry.id, "cids": entry.cids, "error": str(exc),
                                         "token": token, "at": _now()}
                continue
            result["written"][key] = {"id": entry.id, "cids": entry.cids, "at": _now()}
            result["failed"].pop(key, None)
            written_now.append(entry.id)
            _write_json(result_path, result)
    finally:
        result["requests"] += client.requests
        client.close()
        _write_json(result_path, result)
    keys = {_sha(e.model_dump_json().encode("utf-8"))[:16] for e in items}
    failed = [{"id": v["id"], "cids": v["cids"], "error": v["error"]}
              for k, v in result["failed"].items() if k in keys]
    done = not remaining
    return {"done": done, "written": written_now,
            "written_total": sum(1 for k in result["written"] if k in keys), "entries": len(items),
            "failed": failed, "remaining": remaining, "result_file": str(result_path),
            "seconds": round(deadline.elapsed(), 1), "next": None if done else CALL_AGAIN}


async def serve_stdio() -> None:
    """Serve over stdio with the initialize handshake only (protocol 2025-11-25 and older).

    ``MCPServer.run("stdio")`` lets the client's first request choose the protocol era. A client
    that first probes ``server/discover`` (2026-07-28) and then falls back to ``initialize`` on the
    same connection, as GitHub Copilot does when the first start is slow, is refused with -32022
    once the probe has locked the connection to 2026-07-28. Serving the handshake era only makes
    the probe fail, and the client's fallback ``initialize`` succeeds. Every client this plugin
    supports speaks the handshake era.
    """
    server = mcp._lowlevel_server  # the same object MCPServer.run_stdio_async serves
    async with stdio_server() as (read_stream, write_stream), server.lifespan(server) as state:
        await serve_loop(server, read_stream, write_stream, lifespan_state=state,
                         init_options=server.create_initialization_options())


def main() -> None:
    anyio.run(serve_stdio)


if __name__ == "__main__":
    main()
