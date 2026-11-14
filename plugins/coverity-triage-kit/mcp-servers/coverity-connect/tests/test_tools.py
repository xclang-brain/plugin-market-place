"""The four tools through the real server process, against the emulator: replies and files as the
contract of the result folder says."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

from support import PLUGIN, ServerCase, warning

NESTED = warning(101, "NULL_RETURNS")
NESTED["events"][0]["events"] = [{"file": "/build/src/alloc.c", "line": 7, "tag": "return_null",
                                  "description": "NULL を返す"}]
# Not in the order of the CIDs: the list in the file is.
WARNINGS = [warning(104, "MISRA C-2012 Rule 10.4", impact="Low"), NESTED,
            warning(102, "RESOURCE_LEAK", impact="Medium"), warning(103, "MISRA C-2012 Rule 10.3", impact="Low"),
            warning(105, "NULL_RETURNS", classification="Bug"), warning(201, "FORWARD_NULL", stream="app-release")]
TIME = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$"


def listed(item: dict) -> dict:
    """A warning as the list holds it."""
    return {"cid": item["cid"], "checker": item["checker"], "file": item["file"], "line": item["line"],
            "function": item["function"], "impact": item["impact"]}


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class ToolsTest(ServerCase):
    WARNINGS = WARNINGS

    def test_the_four_tools(self) -> None:
        tools = {tool["name"]: tool for tool in self.server.wait(self.server.send("tools/list", {}))["tools"]}
        self.assertEqual(sorted(tools), ["coverity_check_connection", "coverity_get_issue_details",
                                         "coverity_search_issues", "coverity_update_triage"])
        arguments = {name: (sorted(tool["inputSchema"]["properties"]), tool["inputSchema"]["required"])
                     for name, tool in tools.items()}
        self.assertEqual(arguments, {
            "coverity_check_connection": (["repo_root"], ["repo_root"]),
            "coverity_search_issues": (["output_file", "repo_root"], ["repo_root"]),
            "coverity_get_issue_details": (["cids", "output_dir", "repo_root"], ["repo_root", "cids"]),
            "coverity_update_triage": (["entries_file", "repo_root"], ["repo_root", "entries_file"])})

    def test_check_connection(self) -> None:
        repo = self.repo()
        expected = {"ok": True, "message": "接続できました。ストリーム app-main を確認しました",
                    "streams": ["app-main", "app-release"]}
        self.assertEqual(self.ok("coverity_check_connection", repo_root=repo), expected)
        # A settings file as a Windows editor saves it: with a BOM and CRLF, and only the two required keys.
        Path(repo, ".coverity-triage", "config.yaml").write_bytes(
            f"\ufeffcoverity:\r\n  url: {self.emu.url}\r\n  stream: app-main\r\n".encode())
        self.assertEqual(self.ok("coverity_check_connection", repo_root=repo), expected)
        self.assertEqual(self.ok("coverity_search_issues", repo_root=repo)["count"], 4)

    def test_search_saves_the_list(self) -> None:
        repo, file = self.repo(), self.run_dir / "issues.json"
        reply = self.ok("coverity_search_issues", repo_root=repo, output_file=file.as_posix())
        self.assertEqual(reply, {"done": True, "count": 4, "total_matched": 4, "limited": False,
                                 "output_file": file.as_posix()})
        saved = read(file)
        self.assertEqual(list(saved), ["stream", "conditions", "fetched_at", "total_matched", "limited", "issues"])
        self.assertRegex(saved.pop("fetched_at"), TIME)
        self.assertEqual(saved, {
            "stream": "app-main", "total_matched": 4, "limited": False,
            "conditions": {"checkers": [], "impacts": [], "classification": ["Unclassified"], "limit": 20},
            "issues": [listed(item) for item in sorted(WARNINGS[:4], key=lambda item: item["cid"])]})
        # Without a file the same list is in the reply.
        inline = self.ok("coverity_search_issues", repo_root=repo)
        self.assertEqual(inline, {"done": True, "stream": "app-main", "count": 4, "total_matched": 4,
                                  "limited": False, "issues": saved["issues"]})
        # A complete file is never replaced, whatever Coverity holds now.
        before, searches = file.read_bytes(), len(self.requests("/issues/search"))
        self.emu.issues.pop(101)
        self.assertEqual(self.ok("coverity_search_issues", repo_root=repo, output_file=file.as_posix()), reply)
        self.assertEqual((file.read_bytes(), len(self.requests("/issues/search"))), (before, searches))

    def test_two_search_calls_at_once_write_the_list_once(self) -> None:
        repo, file = self.repo(), self.run_dir / "issues.json"
        self.emu.delay = {"/issues/search": 1.0}  # the second call arrives while the first one searches
        calls = [self.server.start("coverity_search_issues", repo_root=repo, output_file=file.as_posix())
                 for _ in range(2)]
        replies = [self.server.wait(call)["structuredContent"] for call in calls]
        self.assertEqual(replies[0], replies[1])
        self.assertEqual((replies[0]["done"], replies[0]["count"]), (True, 4))
        self.assertEqual(len(self.requests("/issues/search")), 1)  # the second call answered from the file

    def test_search_applies_the_configured_conditions(self) -> None:
        def cids(**conditions) -> tuple:
            reply = self.ok("coverity_search_issues", repo_root=self.repo(**conditions))
            return [issue["cid"] for issue in reply["issues"]], reply["total_matched"], reply["limited"]

        self.assertEqual(cids(limit=3), ([101, 102, 104], 4, True))  # the first three the server returns
        self.assertEqual(cids(impacts=["Low", "Medium"]), ([102, 103, 104], 3, False))
        self.assertEqual(cids(classification=["Bug"]), ([105], 1, False))
        self.assertEqual(cids(classification="Bug", impacts="High"), ([105], 1, False))  # a scalar is one value
        # A checker without a warning on the server is not a mistake; one that no name is left for finds nothing.
        self.assertEqual(cids(checkers=["NULL_RETURNS", "USE_AFTER_FREE"]), ([101], 1, False))
        self.assertEqual(cids(checkers=["USE_AFTER_FREE"]), ([], 0, False))
        self.assertEqual(cids(checkers=["MISRA C-2012 Rule 10.*", "RESOURCE_*"]), ([102, 103, 104], 3, False))
        self.assertEqual(cids(checkers=["MISRA*"], limit=1), ([104], 2, True))
        self.assertEqual(
            self.refused("coverity_search_issues", repo_root=self.repo(impacts=["Hihg"])),
            "設定ファイルの conditions に、Coverity にない値があります: Hihg。"
            "conditions.impacts と conditions.classification を確認してください")

    def test_search_without_a_matching_warning(self) -> None:
        file = self.run_dir / "issues.json"
        reply = self.ok("coverity_search_issues", repo_root=self.repo(classification=["Pending"]),
                        output_file=file.as_posix())
        self.assertEqual((reply["count"], reply["total_matched"], reply["limited"]), (0, 0, False))
        self.assertEqual(read(file)["issues"], [])

    def test_details_save_one_file_per_cid(self) -> None:
        repo, folder = self.repo(), self.run_dir / "cid"
        reply = self.ok("coverity_get_issue_details", repo_root=repo, cids=[101, 999, 102], output_dir=folder.as_posix())
        self.assertEqual(reply, {"done": True, "saved": [101, 102], "remaining": [], "output_dir": folder.as_posix(),
                                 "failed": {"999": "ストリーム app-main に CID 999 がありません"}})
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["101", "102"])  # no file for 999
        saved = read(folder / "101" / "issue.json")
        self.assertEqual(list(saved), ["cid", "fetched_at", "events", "checker_description"])
        self.assertRegex(saved.pop("fetched_at"), TIME)
        self.assertEqual(saved, {"cid": 101, "checker_description": "NULL_RETURNS の説明です。", "events": [
            {"file": "/build/src/m101.c", "line": 10, "tag": "cond_true", "description": "条件が真の分岐を通る",
             "main": False},
            {"file": "/build/src/alloc.c", "line": 7, "tag": "return_null", "description": "NULL を返す", "main": False},
            {"file": "/build/src/m101.c", "line": 12, "tag": "dereference",
             "description": "NULL の可能性がある値を参照", "main": True}]})
        # The same call again: what is saved is not fetched again, what failed is tried again.
        before = (folder / "101" / "issue.json").read_bytes()
        self.emu.requests.clear()
        again = self.ok("coverity_get_issue_details", repo_root=repo, cids=[101, 999, 102, 103],
                        output_dir=folder.as_posix())
        self.assertEqual((again["saved"], list(again["failed"])), ([101, 102, 103], ["999"]))
        self.assertEqual(len(self.requests("/ws/v9/defectservice")), 2)
        self.assertEqual((folder / "101" / "issue.json").read_bytes(), before)

    def test_details_in_the_reply(self) -> None:
        repo = self.repo()
        reply = self.ok("coverity_get_issue_details", repo_root=repo, cids=[102, 201])
        self.assertEqual(reply, {"done": True, "failed": {"201": "ストリーム app-main に CID 201 がありません"}, "issues": [
            {"cid": 102, "checker_description": "RESOURCE_LEAK の説明です。",
             "events": [{**event, "main": event.get("main", False)} for event in WARNINGS[2]["events"]]}]})
        self.assertEqual(self.refused("coverity_get_issue_details", repo_root=repo, cids=list(range(1, 12))),
                         "output_dir を指定しないときは、cids は 10 件までです。"
                         "output_dir を指定して、もう一度呼び出してください")

    def test_update_triage_registers_and_records(self) -> None:
        repo = self.repo()
        deviation = {"cids": [103, 104], "classification": "Intentional", "action": "Ignore",
                     "severity": "Unspecified", "comment": "意図的。m103.c の変換は仕様で定めた範囲に収まる。"}
        fix = {"cids": [101], "classification": "Bug", "action": "Fix Required", "severity": "Major",
               "comment": "修正をブランチ coverity-fix/20261110-153000 にコミットしました。NULL の確認を追加"}
        wrong = {"cids": [102], "classification": "Not A Value", "action": "Ignore", "severity": "Unspecified",
                 "comment": "誤検知。"}
        unknown = {"cids": [999], "classification": "Bug", "action": "Ignore", "severity": "Minor",
                   "comment": "本物のバグ。"}
        file = self.entries([wrong, deviation, unknown, fix])
        written_file = self.run_dir / "written.json"
        reply = self.ok("coverity_update_triage", repo_root=repo, entries_file=file)
        self.assertEqual(reply, {
            "done": True, "written": [103, 104, 101], "skipped": [], "remaining": [],
            "written_file": written_file.as_posix(),
            "failed": {"102": "Coverity が要求を受け付けませんでした（HTTP 500: Invalid key provided: Not A Value）",
                       "999": "Coverity が要求を受け付けませんでした（HTTP 404: No defect found for CID 999.）"}})
        # Coverity received the two entries, in the triage store of the stream.
        self.assertEqual(self.emu.writes, [
            {"triageStore": "Default Triage Store", **deviation}, {"triageStore": "Default Triage Store", **fix}])
        record = read(written_file)
        self.assertEqual(list(record), ["written"])
        self.assertEqual(sorted(record["written"]), ["101", "103", "104"])  # one key per CID that was written
        self.assertRegex(record["written"]["103"].pop("at"), TIME)
        self.assertEqual(record["written"]["103"], {key: deviation[key] for key in
                                                    ("classification", "action", "severity", "comment")})
        # The same call again writes nothing twice; an entry Coverity refused is tried again.
        again = self.ok("coverity_update_triage", repo_root=repo, entries_file=file)
        self.assertEqual((again["written"], again["skipped"], sorted(again["failed"])),
                         ([], [103, 104, 101], ["102", "999"]))
        self.assertEqual(len(self.emu.writes), 2)
        # What was registered is no longer Unclassified: the next search leaves it out.
        self.assertEqual([issue["cid"] for issue in self.ok("coverity_search_issues", repo_root=repo)["issues"]], [102])

    def test_update_triage_refuses_a_file_changed_after_the_ok(self) -> None:
        repo = self.repo()
        entry = {"cids": [102], "classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                 "comment": "誤検知。呼び出し元が引数を検証してから呼び出す。"}
        sentence = "登録する内容のファイルが、作成された後に変更されています。もう一度、一覧の確認からやり直してください"
        file = Path(self.entries([entry]))
        file.write_text(file.read_text(encoding="utf-8").replace("False Positive", "Intentional"), encoding="utf-8")
        self.assertEqual(self.refused("coverity_update_triage", repo_root=repo, entries_file=file.as_posix()), sentence)
        file = Path(self.entries([entry], name="no-hash.json"))
        Path(f"{file}.sha256").unlink()
        self.assertEqual(self.refused("coverity_update_triage", repo_root=repo, entries_file=file.as_posix()), sentence)
        self.assertEqual((self.emu.writes, (self.run_dir / "written.json").exists()), ([], False))
        # A CID in two entries is not what apply.py writes.
        file = self.entries([entry, {**entry, "comment": "誤検知。別のコメント。"}], name="twice.json")
        self.assertIn("形式が正しくありません", self.refused("coverity_update_triage", repo_root=repo, entries_file=file))
        self.assertEqual(self.emu.writes, [])

    def test_the_sample_of_the_plugin_is_served(self) -> None:
        emu = self.emulator()  # without a file: tests/fixtures/sample/sample-target/.coverity-triage/fake-issues.yaml
        sample = sorted(emu.issues.values(), key=lambda issue: issue["cid"])
        stream = sample[0]["stream"]
        expected = [issue for issue in sample
                    if issue["stream"] == stream and issue["classification"] == "Unclassified"]
        self.assertTrue(expected)
        repo = self.repo(stream=stream, url=emu.url, limit=len(sample))
        reply = self.ok("coverity_search_issues", repo_root=repo)
        self.assertEqual([issue["cid"] for issue in reply["issues"]], [issue["cid"] for issue in expected])
        details = self.ok("coverity_get_issue_details", repo_root=repo, cids=[expected[0]["cid"]])
        self.assertEqual([event["description"] for event in details["issues"][0]["events"]],
                         [event["description"] for event in expected[0]["events"]])

    def test_the_emulator_as_a_child_process(self) -> None:
        """As the end-to-end tests start it: one JSON line with the URL, then the triage values it received."""
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", "coverity_emulator", "--data", str(self.root / "warnings.yaml"), "--port", "0"],
            cwd=PLUGIN / "tests", stdout=subprocess.PIPE, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        for cleanup in (child.stdout.close, child.wait, child.terminate):
            self.addCleanup(cleanup)
        started = json.loads(child.stdout.readline())
        self.assertEqual((sorted(started), started["user"], started["key"]),
                         (["key", "url", "user"], self.emu.user, self.emu.key))
        entry = {"cids": [102], "classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                 "comment": "誤検知。呼び出し元が引数を検証してから呼び出す。"}
        reply = self.ok("coverity_update_triage", repo_root=self.repo(url=started["url"]),
                        entries_file=self.entries([entry]))
        self.assertEqual(reply["written"], [102])
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1: never through a proxy
        with direct.open(started["url"] + "/emulator/writes", timeout=30) as answer:
            self.assertEqual(json.load(answer), {"writes": [{"triageStore": "Default Triage Store", **entry}]})


if __name__ == "__main__":
    unittest.main()
