"""MCP server ``coverity_mcp`` (stdio): search Coverity Connect issues, read warning paths, write triage.

The server stands alone. It needs the Coverity settings (``<repo_root>/.coverity-triage/config.yaml``
or environment variables) and the credentials in environment variables, nothing else. Logs go to
stderr only; stdout carries the protocol.
"""

import functools
import inspect
import logging
import os
import sys
import time
from typing import Annotated, Any, Callable, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field

from . import __version__, tools
from .envvars import drop_unexpanded
from .models import TriageEntry
from .settings import FilterArg

log = logging.getLogger("coverity_mcp")

INSTRUCTIONS = """\
Coverity Connect の警告を検索し、警告経路を取り、トリアージ（分類・対応・重大度・コメント）を書き戻すためのツールです。
- 使えるツールだけを使います。クライアントの設定によっては、読むツール（接続の確認・警告の検索・警告経路の取得）だけが\
使え、書き戻しのツールは使えません。そのときは、書き戻していないことを伝えます（ほかの方法で Coverity に書き込もうとしません）。
- 設定：repo_root（リポジトリの絶対パス）を渡すと <repo_root>/.coverity-triage/config.yaml の coverity の項目を読みます。\
渡さないときは環境変数 COVERITY_URL などを読みます。ユーザ名と認証キーは環境変数（既定 COV_USER と COV_AUTH_KEY）から読みます。
- パスの引数は、どれも絶対パスで渡します。
- 読むとき：coverity_check_connection で接続を確かめる。coverity_search_issues で警告を探す（各警告に cid と stream がある）。\
coverity_get_issue_details で警告経路を取る（stream が要る）。
- 書き戻すとき（coverity_preview_triage と coverity_update_triage が使えるときだけ）：coverity_preview_triage で\
書き戻す件数・内容と確認の文字列を出し、人に見せて同意を得てから、coverity_update_triage に同じ内容と確認の文字列を渡します。
- 続き：has_more が true なら offset に next_offset を渡して呼び直します。done が false なら同じ引数でもう一度呼びます。\
remaining があれば、その CID だけで呼び直します。
- 書き戻しは、人が件数と内容を見て同意したときだけ行います。confirmation_token は自分で作りません。"""

mcp = FastMCP("coverity_mcp", instructions=INSTRUCTIONS, log_level="WARNING")
mcp._mcp_server.version = __version__  # serverInfo.version: this server's version, not the SDK's

# ---- argument types (shared by the tools) -------------------------------------------------------

RepoRoot = Annotated[str | None, Field(
    description="調べるリポジトリの絶対パス（例: \"C:/work/app\"、\"/home/me/app\"）。<repo_root>/.coverity-triage/config.yaml の"
                " coverity の項目を読む。省くと環境変数（COVERITY_URL など）の設定を使う")]
Format = Annotated[Literal["concise", "detailed"], Field(
    description="会話に返す項目の量。concise（既定）は要る項目だけ、detailed はすべての項目")]
PlanFile = Annotated[str | None, Field(
    description="書き戻しの計画のファイル（{\"triage\": [項目…]} の JSON。ほかの鍵は読まない）の絶対パス"
                "（例: \"/work/out/20261108-101500/apply-plan.json\"）。entries と、どちらか一方")]
Entries = Annotated[list[TriageEntry] | None, Field(
    description="書き戻す項目の並び（例: [{\"id\": \"20001\", \"cids\": [20001], \"stream\": \"app-main\", "
                "\"classification\": \"False Positive\", \"action\": \"Ignore\", \"severity\": \"Unspecified\", "
                "\"comment\": \"誤検知。…\"}]）。plan_file と、どちらか一方")]


def _strict(name: str) -> None:
    """Refuse arguments the tool does not have, instead of dropping them silently (a misspelt
    argument would otherwise widen a search or skip a value)."""
    tool = mcp._tool_manager.get_tool(name)
    base = tool.fn_metadata.arg_model

    class Strict(base):  # type: ignore[misc, valid-type]
        model_config = ConfigDict(extra="forbid", title=base.__name__)

    tool.fn_metadata.arg_model = Strict
    tool.parameters = Strict.model_json_schema(by_alias=True)


def _register(name: str, title: str, **hints: bool) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        mcp.tool(name=name, title=title, description=inspect.getdoc(func),
                 annotations=ToolAnnotations(title=title, **hints))(func)
        _strict(name)
        return func
    return decorator


async def _run(name: str, func: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Run a tool in a worker thread (the requests block), so that calls at the same time do not
    wait for each other, and turn a failure into a tool error the agent reads."""
    started = time.monotonic()
    log.info("%s: 開始", name)
    try:
        result = await anyio.to_thread.run_sync(functools.partial(func, **kwargs))
    except tools.ToolFailure as exc:
        log.warning("%s: 失敗（%.1f 秒）: %s", name, time.monotonic() - started, exc)
        raise ToolError(str(exc)) from None
    log.info("%s: 終わり（%.1f 秒）", name, time.monotonic() - started)
    return result


# ---- tools --------------------------------------------------------------------------------------

@_register("coverity_check_connection", "Coverity 接続の確認",
           readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
async def coverity_check_connection(repo_root: RepoRoot = None) -> dict[str, Any]:
    """Coverity Connect への接続と認証を確かめる（読み取りの問い合わせを1回、10 秒まで）。書き込みはしない。

    使うとき：初めて使う前、認証の失敗や接続のエラーが出たとき、設定を変えたあと。
    使わないとき：警告を探す・取るとき（それぞれのツールを使う）。

    引数:
      - repo_root: リポジトリの絶対パス（例: "/home/me/app"）。省くと環境変数の設定。

    返す値（JSON）:
      {"ok": true/false, "result": "認証できました（0.4 秒）" などの説明,
       "config": 設定のファイルのパスか "環境変数", "api": "auto"|"fake", "url": "https://…", "triage_store": "…",
       "user": "環境変数 COV_USER: 設定あり|なし", "key": "環境変数 COV_AUTH_KEY: 設定あり（N 文字）|なし",
       "seconds": 0.4, "warning": http のときの注意, "unknown_keys": [coverity の中の知らない項目],
       "fake_data": 偽の Coverity のファイル（api が fake のとき）}
      認証キーの値は返さない。

    エラー: 設定のファイルがない・値が正しくないときは ok: false と、何を直すかを result に返す。
      認証の失敗は HTTP 401（ユーザ名と認証キー）か 403（それに加えて Web サービスを使う権限 Access Web services）。
    """
    return await _run("coverity_check_connection", tools.check_connection, repo_root=repo_root)


@_register("coverity_search_issues", "Coverity の警告の検索",
           readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
async def coverity_search_issues(
    repo_root: RepoRoot = None,
    filter: Annotated[FilterArg | None, Field(  # noqa: A002
        description="条件（例: {\"streams\": [\"app-main\"], \"impacts\": [\"High\"], \"checkers\": [\"NULL_RETURNS\"], "
                    "\"triage\": {\"classification\": [\"Unclassified\"], \"status\": [\"New\", \"Triaged\"]}}）。"
                    "同じ項目の値はどれか、項目どうしはすべてに合う警告。filter_file と、どちらか一方")] = None,
    filter_file: Annotated[str | None, Field(
        description="条件のファイル（YAML か JSON。filter と同じ形。知らない項目は読み飛ばす）の絶対パス"
                    "（例: \"/home/me/app/.coverity-triage/filters/untriaged.yaml\"）。filter と、どちらか一方")] = None,
    offset: Annotated[int, Field(
        ge=0,
        description="会話に返すときの続きの位置。最初は 0、続きは前の結果の next_offset（Coverity の行の位置）")] = 0,
    limit: Annotated[int, Field(
        ge=1, le=100,
        description="会話に返すときの1ページの件数（1〜100、既定 20）。output_file のときは使わない（条件の limit、既定 100）")] = 20,
    response_format: Format = "concise",
    output_file: Annotated[str | None, Field(
        description="結果を書く JSON のファイルの絶対パス（例: \"/work/out/run1/issues.json\"）。渡すと会話には件数だけ返す")] = None,
) -> dict[str, Any]:
    """1つのストリームの、条件に合う Coverity の警告を探す。最新のスナップショットの警告を探す
    （triage.status に Fixed か Absent Dismissed があるときは、前のスナップショットの警告も探す）。

    使うとき：「ストリーム app-main の、Impact が High の警告を一覧にして」「未分類の NULL_RETURNS は何件？」など。
    使わないとき：CID ごとの警告経路やチェッカーの説明が要るとき（coverity_get_issue_details を使う）。

    引数:
      - filter か filter_file のどちらか一方（必須）。streams はちょうど1つ。checkers は * ? [ ] のワイルドカード可。
        impacts: High / Medium / Low / Unspecified。triage.classification: Unclassified / Pending / False Positive /
        Intentional / Bug。triage.action: Undecided / Fix Required / Fix Submitted / Modeling Required / Ignore。
        triage.status: New / Triaged / Dismissed / Absent Dismissed / Fixed。filter にほかの項目があればエラー。
      - offset, limit: 会話に返すときのページ（例: offset=0, limit=20 → 次は offset=next_offset）。
      - response_format: concise（cid, checker, file, line, function, impact, classification, action, status, stream）
        か detailed（それに category, cwe, severity, merge_key も）。
      - output_file: 渡すと、条件の limit 件（既定 100）までをファイルに書き、解析したリビジョンも記録する。

    返す値（会話のとき）:
      {"stream": "app-main", "count": 20, "offset": 0, "total": Coverity が返した件数（Status とワイルドカードの
       チェッカーで絞る前。分からなければ null）, "has_more": true, "next_offset": 20, "excluded": 検索のあとの条件で
       外した件数, "issues": [{"cid": 20001, "checker": "NULL_RETURNS", "file": "src/reader.c", "line": 42, …}],
       "ignored_values": [{"filter": "triage.classification", "value": "…"}]（Coverity にない値で、外して探したもの）}
    返す値（output_file のとき）:
      {"done": true/false, "found": 件数, "limit": 上限, "limited": 上限で止めたか, "coverity_total": …, "excluded": …,
       "analyzed_revision": "a1b2c3d", "revision_error": null, "output_file": "…", "next": 続きの案内}
      done が false なら、同じ引数でもう一度呼ぶ（保存済みの続きから探す）。

    エラー: 認証の失敗（401/403）、リダイレクト（3xx）、5xx、JSON でない応答、接続の失敗は、何を確かめるかを返して止まる。
    """
    return await _run("coverity_search_issues", tools.search_issues, repo_root=repo_root, filter=filter,
                      filter_file=filter_file, offset=offset, limit=limit, response_format=response_format,
                      output_file=output_file)


@_register("coverity_get_issue_details", "Coverity の警告経路の取得",
           readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
async def coverity_get_issue_details(
    repo_root: RepoRoot = None,
    stream: Annotated[str | None, Field(
        description="必須。警告のあるストリームの名前（例: \"app-main\"）。警告の検索の結果の各警告に stream がある")] = None,
    cids: Annotated[list[int] | None, Field(
        description="CID の並び（例: [20001, 20002]）。会話に返すときは 10 件まで、output_dir のときは 50 件まで")] = None,
    response_format: Format = "concise",
    output_dir: Annotated[str | None, Field(
        description="CID ごとに issue-<CID>.json を書くフォルダの絶対パス（例: \"/work/out/run1/items/20001\"）。"
                    "なければ作る（親のフォルダは要る）")] = None,
) -> dict[str, Any]:
    """CID ごとに、ストリームでの警告経路（イベント）とチェッカーの説明を取る。

    使うとき：「CID 20001 の警告経路を見せて」「この警告が本物か調べたいので、経路と説明を取って」など。
    使わないとき：条件に合う警告を一覧にするとき（coverity_search_issues を使う）。

    引数:
      - stream（必須）と cids（例: stream="app-main", cids=[20001]）。
      - response_format: concise はイベントの file, line, tag, description と main（問題の起きる場所なら true）。
        イベントが 40 件を超えると間を省く。detailed は stream, impact, category, cwe と、すべてのイベント。
      - output_dir: 渡すと issue-<CID>.json（{"fetched_at", "issue", "events", "checker_description"}）を書く。
        取れなかった CID は {"cid", "stream", "fetched_at", "error"} を書く。保存済みの CID は取り直さない。

    返す値（会話のとき）:
      {"stream": "app-main", "issues": [{"cid": 20001, "checker": "NULL_RETURNS", "file": "src/reader.c", "line": 42,
       "function": "read_header", "events": [{"file": "src/reader.c", "line": 40, "tag": "returned_null",
       "description": "…"}, …], "checker_description": "…"}], "failed": {"20009": "理由"}, "remaining": [取っていない CID],
       "next": 続きの案内}
      remaining が空でなければ、その CID だけで呼び直す。
    返す値（output_dir のとき）:
      {"done": true/false, "saved": [...], "already_saved": [...], "failed": {...}, "remaining": [...], "output_dir": "…"}
      done が false なら、同じ引数でもう一度呼ぶ。

    エラー: stream がないとき「stream を渡してください。警告の検索の結果の各警告に stream があります」。
      ストリームにない CID は、その CID だけ failed になる。認証の失敗などは、何も書かずに止まる。
    """
    return await _run("coverity_get_issue_details", tools.get_issue_details, repo_root=repo_root, stream=stream,
                      cids=cids, response_format=response_format, output_dir=output_dir)


@_register("coverity_preview_triage", "Coverity へのトリアージの書き戻しの確認",
           readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
async def coverity_preview_triage(plan_file: PlanFile = None, entries: Entries = None) -> dict[str, Any]:
    """書き戻す件数と内容と、coverity_update_triage に渡す確認の文字列（confirmation_token）を返す。
    読むだけで、Coverity とは通信しない。何も書き込まない。

    使うとき：トリアージを書き戻す前に、必ず先に呼ぶ。返った counts と items を人に見せ、同意を得る。
    使わないとき：警告を探す・取るとき。

    引数（どちらか一方）:
      - plan_file: {"triage": [項目…]} の JSON のファイルの絶対パス。
      - entries: 項目の並び。項目は {"id": "20001", "cids": [20001], "stream": "app-main"（省略可）,
        "classification": "False Positive", "action": "Ignore", "severity": "Unspecified"（省略可）,
        "comment": "誤検知。…"（省略可）}。stream を書くと、そのストリームが使うトリアージストアに書き戻す。

    返す値:
      {"counts": {"entries": 2, "cids": 3}, "items": [書き戻す内容], "confirmation_token": "1a2b3c4d5e6f",
       "next": 次にすること, "note": stream のない項目の書き先}
      confirmation_token は自分で作らない。内容を変えたら、もう一度このツールを呼ぶ。

    エラー: plan_file がない・JSON でない、entries の項目に足りない鍵や知らない鍵があるときは、何を直すかを返す。
    """
    return await _run("coverity_preview_triage", tools.preview_triage, plan_file=plan_file, entries=entries)


@_register("coverity_update_triage", "Coverity へのトリアージの書き戻し",
           readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)
async def coverity_update_triage(
    confirmation_token: Annotated[str, Field(
        min_length=1,
        description="coverity_preview_triage が返した confirmation_token（例: \"1a2b3c4d5e6f\"）。"
                    "人が件数と内容を見て同意したあとにだけ渡す")],
    repo_root: RepoRoot = None,
    plan_file: PlanFile = None,
    entries: Entries = None,
) -> dict[str, Any]:
    """トリアージ（Classification・Action・Severity・コメント）を Coverity に書き込む。

    使うとき：coverity_preview_triage の結果を人に見せ、同意を得たあと。preview に渡したのと同じ plan_file か
    entries と、返った confirmation_token を渡す。
    使わないとき：同意を得ていないとき。確認の文字列が内容と合わなければ、何も書き込まない。

    ふるまい:
      - 項目に stream があれば、そのストリームが使うトリアージストアに書く（Coverity に問い合わせて決める。
        ストリームがなければ、その項目だけ失敗）。stream がなければ、設定の coverity.triage_store に書く。
      - 書き込み済みの項目（id・cids・stream・値・コメントが同じもの）は、並べ方や確認の文字列が変わっても書き直さない。
        記録は、plan_file なら同じフォルダの <名前>.result.json、entries なら一時のフォルダの
        coverity-connect-mcp-<ユーザー名>/（本人だけが読める）。
      - Coverity が値の誤り（401・403 のほかの 4xx）で1項目を断っても、ほかの項目は書く。断られた項目は、
        同じ確認の文字列では送り直さない（値を直して preview からやり直す）。
      - 認証の失敗（401・403）、5xx、接続の失敗では、呼び出し全体を止める。止まった項目は記録しないので、
        直ってから同じ引数で呼ぶと書き込む。

    返す値:
      {"done": true/false, "written": [この呼び出しで書いた id], "written_total": 書き込み済みの数, "entries": 項目の数,
       "failed": [{"id", "cids", "error"}], "remaining": [時間が足りず残した id], "result_file": "記録のパス", "next": …}
      done が false なら、同じ引数でもう一度呼ぶ。
    """
    return await _run("coverity_update_triage", tools.update_triage, confirmation_token=confirmation_token,
                      repo_root=repo_root, plan_file=plan_file, entries=entries)


class _MaskingFilter(logging.Filter):
    """Credential values never reach a log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is logged as is
            return True
        masked = tools.masker_for(None).text(message)
        if masked != message:
            record.msg, record.args = masked, None
        return True


def setup_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    for handler in root.handlers:
        handler.addFilter(_MaskingFilter())
    log.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    drop_unexpanded()  # values such as "${HTTPS_PROXY}" that the client did not expand count as not set
    if not os.environ.get("PYTHONIOENCODING"):
        try:  # clients read the log as UTF-8; a Windows console code page cannot hold every character
            sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    setup_logging()
    log.info("coverity_mcp %s: 起動（stdio）", __version__)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
