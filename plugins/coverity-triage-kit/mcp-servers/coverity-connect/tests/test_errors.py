"""What the person reads when something is wrong: one or two Japanese sentences as a tool error (the
connection check answers ``ok: false`` instead), and never a credential value."""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

from support import ServerCase, warning

WARNINGS = [warning(101, "NULL_RETURNS"), warning(102, "RESOURCE_LEAK", impact="Medium")]
ENTRY = {"cids": [101], "classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
         "comment": "誤検知。呼び出し元が引数を検証してから呼び出す。"}
# On Windows the variables are saved for the user: a copilot that is running still has the old values.
AGAIN = "。保存し直したときは、新しいターミナルで copilot を起動し直してください" if sys.platform == "win32" else ""


class ErrorsTest(ServerCase):
    WARNINGS = WARNINGS

    def every_tool(self, repo: str, stream: str = "app-main", **more) -> dict[str, str]:
        """The sentence each tool answers with (the message of the check, the tool error of the others)."""
        check = self.ok("coverity_check_connection", repo_root=repo, **more)
        self.assertFalse(check["ok"])
        return {
            "check": check["message"],
            "search": self.refused("coverity_search_issues", repo_root=repo, **more),
            "details": self.refused("coverity_get_issue_details", repo_root=repo, cids=[101],
                                    output_dir=(self.run_dir / "cid").as_posix(), **more),
            "write": self.refused("coverity_update_triage", repo_root=repo,
                                  entries_file=self.entries([ENTRY], stream=stream), **more)}

    def assertNothingDone(self) -> None:
        self.assertEqual((self.emu.writes, sorted(path.name for path in self.run_dir.iterdir())),
                         ([], ["coverity-entries.json", "coverity-entries.json.sha256"]))

    def test_no_settings_file(self) -> None:
        repo = (self.dir / "設定のない リポジトリ").as_posix()
        Path(repo).mkdir()
        sentence = (f"設定ファイルがありません: {repo}/.coverity-triage/config.yaml。"
                    "/coverity-triage-kit:coverity-setup <URL> <ストリーム> を実行してください")
        self.assertEqual(set(self.every_tool(repo).values()), {sentence})
        self.assertEqual(self.ok("coverity_check_connection", repo_root=repo)["streams"], [])

    def test_settings_file_without_url_or_with_a_wrong_limit(self) -> None:
        repo = self.repo(limit=0)
        config = f"{repo}/.coverity-triage/config.yaml"
        self.assertEqual(self.refused("coverity_search_issues", repo_root=repo),
                         f"設定ファイルの conditions.limit は 1 以上の整数で指定してください: {config}")
        Path(config).write_text("coverity:\n  stream: app-main\n", encoding="utf-8")
        self.assertEqual(self.refused("coverity_search_issues", repo_root=repo),
                         f"設定ファイルに coverity.url と coverity.stream を記入してください: {config}")
        Path(config).write_text("coverity: [url\n", encoding="utf-8")
        self.assertIn(f"設定ファイルを読み取れません: {config}（", self.refused("coverity_search_issues", repo_root=repo))

    def test_no_credentials(self) -> None:
        server = self.start_server()  # the variables as mcp.json passes them: "${COV_USER}", not expanded
        self.assertEqual(set(self.every_tool(self.repo(), server=server).values()),
                         {("環境変数 COV_USER と COV_AUTH_KEY を設定してください。"
                           "手順は /coverity-triage-kit:coverity-setup と入力すると表示されます")})
        self.assertNothingDone()

    def test_authentication_fails(self) -> None:
        self.emu.key = "another-key"
        self.assertEqual(set(self.every_tool(self.repo()).values()),
                         {"認証できませんでした（HTTP 401）。COV_USER と COV_AUTH_KEY を確認してください" + AGAIN})
        self.assertNothingDone()
        self.emu.fail = {"/api/v2/": 403}
        self.assertEqual(self.ok("coverity_check_connection", repo_root=self.repo())["message"],
                         "認証できませんでした（HTTP 403）。COV_USER と COV_AUTH_KEY、ユーザーの権限（Access Web services）"
                         "を確認してください" + AGAIN)

    def test_stream_not_found(self) -> None:
        repo = self.repo(stream="no-such-stream")
        self.assertEqual(set(self.every_tool(repo, stream="no-such-stream").values()),
                         {"ストリーム no-such-stream が見つかりません。設定ファイルの coverity.stream を確認してください"})
        self.assertEqual(self.ok("coverity_check_connection", repo_root=repo)["streams"], ["app-main"])
        self.assertNothingDone()

    def test_no_connection(self) -> None:
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{free.getsockname()[1]}"  # nothing listens here once the socket is closed
        for url in (closed, "http://127.0.0.1:port"):
            for sentence in self.every_tool(self.repo(url=url)).values():
                self.assertTrue(sentence.startswith(f"Coverity に接続できませんでした（{url}: "), sentence)
                self.assertTrue(sentence.endswith("）。設定ファイルの coverity.url とネットワークを確認してください"),
                                sentence)

    def test_relative_paths(self) -> None:
        repo = self.repo()
        self.assertEqual(self.refused("coverity_search_issues", repo_root="リポジトリ1"),
                         "repo_root は絶対パスで指定してください: リポジトリ1")
        self.assertEqual(self.refused("coverity_search_issues", repo_root=repo, output_file="out/issues.json"),
                         "output_file は絶対パスで指定してください: out/issues.json")
        self.assertEqual(self.refused("coverity_get_issue_details", repo_root=repo, cids=[101], output_dir="cid"),
                         "output_dir は絶対パスで指定してください: cid")
        self.assertEqual(self.refused("coverity_update_triage", repo_root=repo, entries_file="coverity-entries.json"),
                         "entries_file は絶対パスで指定してください: coverity-entries.json")
        self.assertEqual(self.emu.requests, [])

    def test_a_path_that_cannot_be_used(self) -> None:
        file = self.run_dir / "a file"
        file.write_text("", encoding="utf-8")
        sentence = self.refused("coverity_get_issue_details", repo_root=self.repo(), cids=[101],
                                output_dir=file.as_posix())
        self.assertTrue(sentence.startswith("ファイルまたはフォルダーを使用できませんでした（"), sentence)
        self.assertTrue(sentence.endswith("）。引数のパスを確認してください"), sentence)

    def test_a_server_error_stops_the_write_and_the_same_call_registers_later(self) -> None:
        repo, file = self.repo(), self.entries([ENTRY, {**ENTRY, "cids": [102]}])
        self.emu.fail = {"/issues/triage": 503}
        self.assertEqual(self.refused("coverity_update_triage", repo_root=repo, entries_file=file),
                         "Coverity がエラーを返しました（HTTP 503: emulated failure）。"
                         "しばらくしてから、もう一度実行してください")
        self.assertNothingDone()
        self.emu.fail = {}
        self.assertEqual(self.ok("coverity_update_triage", repo_root=repo, entries_file=file)["written"], [101, 102])

    def test_no_credential_value_in_a_reply_or_a_file(self) -> None:
        repo, folder = self.repo(), self.run_dir / "cid"
        self.ok("coverity_check_connection", repo_root=repo)
        self.ok("coverity_search_issues", repo_root=repo, output_file=(self.run_dir / "issues.json").as_posix())
        self.ok("coverity_get_issue_details", repo_root=repo, cids=[101, 999], output_dir=folder.as_posix())
        self.ok("coverity_update_triage", repo_root=repo, entries_file=self.entries([ENTRY, {**ENTRY, "cids": [999]}]))
        self.emu.user = "another-user"  # Coverity refuses the credentials: the sentences name the variables only
        self.assertFalse(self.ok("coverity_check_connection", repo_root=repo)["ok"])
        self.refused("coverity_search_issues", repo_root=repo)
        self.refused("coverity_get_issue_details", repo_root=repo, cids=[102])
        files = [path for path in self.dir.rglob("*") if path.is_file()]
        self.assertLessEqual({"issues.json", "issue.json", "written.json"}, {path.name for path in files})
        texts = [path.read_text(encoding="utf-8") for path in files] + self.server.received
        for secret in ("emulator-user", "emulator-key-5b1f9c2e77"):
            self.assertEqual([text for text in texts if secret in text], [], secret)


if __name__ == "__main__":
    unittest.main()
