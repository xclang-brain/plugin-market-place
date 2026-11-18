"""MCP server ``coverity-connect`` (stdio): four tools for Coverity Connect.

Each tool takes the repository (``repo_root``) and reads ``<repo_root>/.coverity-triage/config.yaml``; the
credentials come from the environment variables COV_USER and COV_AUTH_KEY. stdout carries the protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, ClientCapabilities, ElicitationCapability, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import tools
from .settings import Failure

INSTRUCTIONS = (
    "Coverity Connect の警告を取得し、トリアージ（Classification・Action・Severity・コメント）を登録します。"
    "どのツールにも repo_root（リポジトリの最上位フォルダーの絶対パス）を指定します。"
    "接続先の URL、ストリーム、取得する条件は <repo_root>/.coverity-triage/config.yaml から、"
    "ユーザー名と認証キーは環境変数 COV_USER と COV_AUTH_KEY から読み取ります。"
    "応答の done が false のときは、同じ引数でもう一度呼び出すと、続きから進みます。")

mcp = FastMCP("coverity-connect", instructions=INSTRUCTIONS, log_level="WARNING")

DECLINED = "利用者が Coverity への登録を承認しなかったため、登録は行っていません"
APPROVED: set[str] = set()  # the entries files (SHA-256) the person has approved since the server started


class Approval(BaseModel):
    """The question of the write tool: the person ticks it to approve."""

    register: bool = Field(title="登録する", description="Coverity に登録するときは選択してください")


RepoRoot = Annotated[str, Field(description="リポジトリの最上位フォルダーの絶対パス（例: C:/work/app、/home/me/app）")]


def _error(exc: Exception | str) -> CallToolResult:
    """A tool error whose text is the Japanese sentence alone."""
    text = exc if isinstance(exc, str) else str(exc) if isinstance(exc, Failure) else (
        f"ファイルまたはフォルダーを使用できませんでした（{exc}）。引数のパスを確認してください")
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


async def _run(func: Callable[..., dict], *args) -> CallToolResult:
    """Run a tool in a worker thread (its requests block). A failure becomes a tool error."""
    try:
        data = await asyncio.to_thread(func, *args)
    except (Failure, OSError) as exc:
        return _error(exc)
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                          structuredContent=data)


@mcp.tool(name="coverity_check_connection", title="Coverity への接続の確認",
          annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True), description=(
              "設定ファイルの URL と認証情報で Coverity Connect に接続し、設定したストリームがあることを確認します。"
              "応答: {\"ok\", \"message\", \"streams\"}。message は、利用者にそのまま伝える文です。"
              "streams は、このユーザーが参照できるストリームの名前（最大 100 件）です。"))
async def coverity_check_connection(repo_root: RepoRoot) -> CallToolResult:
    return await _run(tools.check_connection, repo_root)


@mcp.tool(name="coverity_search_issues", title="Coverity の警告の取得",
          annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
          description=(
              "設定したストリームから、設定ファイルの条件（conditions: チェッカー、Impact、Classification、件数の上限）に"
              "合う警告を取得します。output_file を指定すると、一覧をそのファイルに保存して、件数を返します: "
              "{\"done\", \"count\", \"total_matched\", \"limited\", \"output_file\"}。done が false のときは、"
              "同じ引数でもう一度呼び出します。指定しないときは、一覧（最大 100 件）を応答に含めます: "
              "{\"done\": true, \"stream\", \"count\", \"total_matched\", \"limited\", \"issues\": [{\"cid\", \"checker\", "
              "\"file\", \"line\", \"function\", \"impact\"}]}。total_matched は条件に合う警告の総数（不明のときは null）、"
              "limited は、上限を超える警告があることを表します。"))
async def coverity_search_issues(
    repo_root: RepoRoot,
    output_file: Annotated[str | None, Field(
        description="一覧を保存する JSON ファイルの絶対パス（例: <結果フォルダー>/issues.json）")] = None,
) -> CallToolResult:
    return await _run(tools.search_issues, repo_root, output_file)


@mcp.tool(name="coverity_get_issue_details", title="Coverity の警告の経路の取得",
          annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
          description=(
              "CID ごとに、警告の経路（イベント）とチェッカーの説明を取得します。output_dir を指定すると、"
              "<output_dir>/<CID>/issue.json（{\"cid\", \"fetched_at\", \"events\": [{\"file\", \"line\", \"tag\", "
              "\"description\", \"main\"}], \"checker_description\"}）に保存します: {\"done\", \"saved\", \"failed\", "
              "\"remaining\", \"output_dir\"}。done が false のときは、同じ引数でもう一度呼び出します"
              "（残りの CID だけを取得します）。指定しないときは、10 件までの CID の内容を応答に含めます: "
              "{\"done\": true, \"issues\": [{\"cid\", \"events\", \"checker_description\"}], \"failed\"}。"
              "events は経路の順で、main が true のイベントが、問題の発生する場所です。"
              "failed は、取得できなかった CID とその理由です。"))
async def coverity_get_issue_details(
    repo_root: RepoRoot,
    cids: Annotated[list[int], Field(description="CID の一覧（例: [20001, 20002]）")],
    output_dir: Annotated[str | None, Field(
        description="CID ごとのフォルダーを作成するフォルダーの絶対パス（例: <結果フォルダー>/cid）")] = None,
) -> CallToolResult:
    return await _run(tools.get_issue_details, repo_root, cids, output_dir)


@mcp.tool(name="coverity_update_triage", title="Coverity へのトリアージの登録",
          annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
          description=(
              "entries_file の内容（{\"stream\", \"entries\": [{\"cids\", \"classification\", \"action\", \"severity\", "
              "\"comment\"}]}）を Coverity Connect に登録します。利用者が一覧を確認して OK と答えた後に呼び出します。"
              "登録した CID は、entries_file と同じフォルダーの written.json に記録し、"
              "次の呼び出しでは残りの CID だけを登録します。"
              "応答: {\"done\", \"written\", \"skipped\", \"failed\", \"remaining\", \"written_file\"}。"
              "done が false のときは、同じ引数でもう一度呼び出します。written はこの呼び出しで登録した CID、"
              "skipped は登録済みの CID、failed は登録できなかった CID とその理由です。"))
async def coverity_update_triage(
    repo_root: RepoRoot,
    entries_file: Annotated[str, Field(
        description="登録する内容のファイルの絶対パス（例: <結果フォルダー>/coverity-entries.json）")],
    ctx: Context,
) -> CallToolResult:
    # The OK in the chat goes through the model, and the permission prompt of the client can be switched off
    # for the session. Where the client can show a question of the server (MCP elicitation), the person
    # approves the write here, once per entries file: the client asks the person, not the model.
    try:
        asked = await asyncio.to_thread(tools.confirmation, repo_root, entries_file)
    except (Failure, OSError) as exc:
        return _error(exc)
    can_ask = ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability()))
    if asked and asked[0] not in APPROVED and can_ask:
        try:
            answer = await ctx.elicit(asked[1], Approval)
        except Exception as exc:  # noqa: BLE001 - nothing is written without the answer
            return _error(f"登録の確認を表示できなかったため、Coverity への登録は行っていません（{type(exc).__name__}: {exc}）")
        if answer.action != "accept" or not answer.data.register:
            return _error(DECLINED)
        APPROVED.add(asked[0])
    return await _run(tools.update_triage, repo_root, entries_file)


def main() -> None:
    # A value such as "${COV_USER}" or "${HTTPS_PROXY}" (the client did not expand the env block of the MCP
    # registration) counts as not set; httpx reads the proxy variables itself.
    for name in [name for name, value in os.environ.items() if re.fullmatch(r"\s*\$\{[^}]*\}\s*", value)]:
        del os.environ[name]
    mcp.run("stdio")
