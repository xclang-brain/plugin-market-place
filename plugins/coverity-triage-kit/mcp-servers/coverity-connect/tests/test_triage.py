"""coverity_preview_triage and coverity_update_triage: token, idempotency, stores, error classes."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from support import MISSING, only_with_packages

load_tests = only_with_packages

ENTRIES = [
    {"id": "10001", "cids": [10001], "classification": "False Positive", "action": "Ignore",
     "comment": "誤検知。ループの前で NULL を確かめている（src/ev.c:40）。"},
    {"id": "G1", "cids": [10002, 10003], "classification": "Bug", "action": "Fix Required", "severity": "Major"},
]


def token_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def entries_token(entries: list) -> str:
    """The recipe of the README: {"triage": entries} normalized, as JSON with sorted keys and no spaces."""
    normal = []
    for e in entries:
        item = {"id": e["id"], "cids": [int(c) for c in e["cids"]], "classification": e["classification"],
                "action": e["action"], "severity": e.get("severity", ""), "comment": e.get("comment", "")}
        if e.get("stream"):
            item["stream"] = e["stream"]
        normal.append(item)
    text = json.dumps({"triage": normal}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return token_of(text.encode("utf-8"))


if not MISSING:
    import support as cov
    from support import BASE, KEY, STREAM, ServerCase

    class PreviewUpdateTest(ServerCase):

        def write_plan(self, entries=ENTRIES, name: str = "apply-plan.json") -> Path:
            path = self.run_dir / name
            path.write_text(json.dumps({"run_id": "r1", "created_at": "2026-11-08T10:15:00+09:00", "triage": entries,
                                        "code": [], "reject": []}, ensure_ascii=False, indent=2), encoding="utf-8")
            return path

        def update(self, token: str, **kwargs):
            return self.tools.update_triage(confirmation_token=token, repo_root=str(self.repo), **kwargs)

        def puts(self) -> int:
            return self.cov.requests.count("PUT /api/v2/issues/triage")

        def test_preview_entries_token_counts_items_and_no_traffic(self) -> None:
            out = self.tools.preview_triage(entries=ENTRIES)
            self.assertEqual(out["confirmation_token"], entries_token(ENTRIES))
            self.assertEqual(out["counts"], {"entries": 2, "cids": 3})
            self.assertEqual(out["items"][1], {"id": "G1", "cids": [10002, 10003], "classification": "Bug",
                                               "action": "Fix Required", "severity": "Major", "comment": ""})
            self.assertIn("coverity_update_triage", out["next"])
            self.assertIn("note", out)  # entries without a stream go to the store of the settings
            self.assertEqual(self.cov.requests, [])

        def test_preview_with_stream_keeps_it_in_the_token(self) -> None:
            entries = [dict(ENTRIES[0], stream=STREAM)]
            out = self.tools.preview_triage(entries=entries)
            self.assertEqual(out["confirmation_token"], entries_token(entries))
            self.assertNotEqual(out["confirmation_token"], entries_token(ENTRIES[:1]))
            self.assertNotIn("note", out)

        def test_preview_plan_file_token_is_file_sha(self) -> None:
            plan = self.write_plan()
            out = self.tools.preview_triage(plan_file=str(plan))
            self.assertEqual(out["confirmation_token"], token_of(plan.read_bytes()))
            self.assertEqual(out["counts"], {"entries": 2, "cids": 3})
            self.assertEqual(self.cov.requests, [])

        def test_plan_entries_may_hold_other_keys_but_entries_may_not(self) -> None:
            plan = self.write_plan([dict(ENTRIES[0], note="ほかの道具の項目")])
            self.assertEqual(self.tools.preview_triage(plan_file=str(plan))["counts"]["entries"], 1)
            message = self.assertFailure(self.tools.preview_triage, entries=[dict(ENTRIES[0], comments="x")])
            self.assertIn("comments", message)

        def test_preview_needs_exactly_one_source(self) -> None:
            self.assertFailure(self.tools.preview_triage)
            self.assertFailure(self.tools.preview_triage, plan_file=str(self.write_plan()), entries=ENTRIES)
            self.assertIn("plan_file", self.assertFailure(self.tools.preview_triage,
                                                         plan_file=str(self.run_dir / "none.json")))

        def test_wrong_token_writes_nothing(self) -> None:
            message = self.assertFailure(self.update, "000000000000", entries=ENTRIES)
            self.assertIn("coverity_preview_triage", message)
            self.assertEqual(self.cov.writes, [])
            self.assertNotIn("PUT /api/v2/issues/triage", self.cov.requests)
            plan = self.write_plan()
            self.assertIn("合いません", self.assertFailure(self.update, "abcdefabcdef", plan_file=str(plan)))
            self.assertFalse((self.run_dir / "apply-plan.result.json").exists())

        def test_entries_written_once(self) -> None:
            token = self.tools.preview_triage(entries=ENTRIES)["confirmation_token"]
            out = self.update(token, entries=ENTRIES)
            self.assertTrue(out["done"])
            self.assertEqual(out["written"], ["10001", "G1"])
            result_file = Path(out["result_file"])
            self.assertEqual(result_file.parent.parent, self.tmp / "systemp")
            self.assertTrue(result_file.parent.name.startswith("coverity-connect-mcp-"))
            self.assertEqual(self.cov.writes[0], {
                "store": "Default Triage Store", "cids": [10001],
                "attributeValuesList": [{"attributeName": "Classification", "attributeValue": "False Positive"},
                                        {"attributeName": "Action", "attributeValue": "Ignore"},
                                        {"attributeName": "Comment", "attributeValue": ENTRIES[0]["comment"]}]})
            again = self.update(token, entries=ENTRIES)
            self.assertEqual((again["written"], again["written_total"]), ([], 2))
            self.assertEqual(len(self.cov.writes), 2)  # never twice

        def test_stream_decides_the_triage_store(self) -> None:
            entries = [dict(ENTRIES[0], stream="libev-release"), dict(ENTRIES[1], stream="no-such-stream")]
            out = self.update(entries_token(entries), entries=entries)
            self.assertEqual(out["written"], ["10001"])
            self.assertEqual(self.cov.writes[0]["store"], "Release Triage Store")
            self.assertIn("GET /api/v2/streams/libev-release", self.cov.requests)
            self.assertEqual([f["id"] for f in out["failed"]], ["G1"])
            self.assertIn("no-such-stream", out["failed"][0]["error"])
            result = json.loads(Path(out["result_file"]).read_text(encoding="utf-8"))
            written = next(iter(result["written"].values()))
            self.assertEqual((written["stream"], written["triage_store"]), ("libev-release", "Release Triage Store"))

        def test_plan_file_keeps_result_file_next_to_it(self) -> None:
            plan = self.write_plan()
            token = token_of(plan.read_bytes())
            out = self.update(token, plan_file=str(plan))
            self.assertEqual(out["result_file"], str(self.run_dir / "apply-plan.result.json"))
            self.assertEqual((out["written"], out["written_total"], out["entries"]), (["10001", "G1"], 2, 2))
            result = json.loads((self.run_dir / "apply-plan.result.json").read_text(encoding="utf-8"))
            self.assertEqual(set(result), {"written", "failed", "requests"})
            self.assertEqual(sorted(v["id"] for v in result["written"].values()), ["10001", "G1"])
            for key, value in result["written"].items():
                self.assertEqual(len(key), 16)
                self.assertEqual(set(value), {"id", "cids", "triage_store", "at"})
            self.assertEqual(self.update(token, plan_file=str(plan))["written"], [])
            self.assertEqual(len(self.cov.writes), 2)

        def test_5xx_and_auth_stop_the_call_without_records(self) -> None:
            token = entries_token(ENTRIES)
            for status in (500, 502, 503, 504):
                with self.subTest(status=status):
                    self.cov.triage_responses[10001] = lambda status=status: cov.html_error(status)
                    before = self.puts()
                    self.assertIn(str(status), self.assertFailure(self.update, token, entries=ENTRIES))
                    self.assertEqual(self.puts() - before, 1)  # G1 is not sent
                    self.assertEqual(self.cov.writes, [])
            self.cov.triage_responses[10001] = cov.rest_401
            self.assertIn("認証に失敗", self.assertFailure(self.update, token, entries=ENTRIES))
            del self.cov.triage_responses[10001]  # the server is back
            out = self.update(token, entries=ENTRIES)
            self.assertEqual((out["written"], out["failed"]), (["10001", "G1"], []))

        def test_plan_500_keeps_earlier_entries_and_writes_rest_later(self) -> None:
            plan = self.write_plan()
            token = token_of(plan.read_bytes())
            self.cov.triage_responses[10002] = lambda: cov.json_response(500, {"code": 1004, "message": "Internal error"})
            self.assertIn("500", self.assertFailure(self.update, token, plan_file=str(plan)))
            result = json.loads((self.run_dir / "apply-plan.result.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(v["id"] for v in result["written"].values()), ["10001"])
            self.assertEqual(result["failed"], {})
            del self.cov.triage_responses[10002]
            again = self.update(token, plan_file=str(plan))
            self.assertEqual((again["written"], again["written_total"], again["failed"]), (["G1"], 2, []))
            self.assertEqual(len(self.cov.writes), 2)

        def test_4xx_refusal_is_not_sent_again_with_same_token(self) -> None:
            self.cov.triage_responses[10001] = lambda: cov.json_response(400, {"code": 1200, "message": "Bad value"})
            token = entries_token(ENTRIES)
            out = self.update(token, entries=ENTRIES)
            self.assertEqual(out["written"], ["G1"])
            self.assertIn("400", out["failed"][0]["error"])
            self.assertIn("failed_note", out)
            before = self.puts()
            again = self.update(token, entries=ENTRIES)
            self.assertEqual((again["written"], [f["id"] for f in again["failed"]]), ([], ["10001"]))
            self.assertEqual(self.puts(), before)

        def test_remaining_when_time_runs_out(self) -> None:
            token = entries_token(ENTRIES)
            with mock.patch.object(self.tools.Deadline, "allows", lambda self, client, requests=1: False):
                out = self.update(token, entries=ENTRIES)
            self.assertEqual((out["done"], out["remaining"], out["written"]), (False, ["10001", "G1"], []))
            self.assertTrue(out["next"])
            self.assertTrue(self.update(token, entries=ENTRIES)["done"])

        def test_entries_result_file_per_user_and_target(self) -> None:
            token = entries_token(ENTRIES)
            with mock.patch("getpass.getuser", return_value="alice"):
                out = self.update(token, entries=ENTRIES)
                result_file = Path(out["result_file"])
                self.assertEqual(result_file.parent, self.tmp / "systemp" / "coverity-connect-mcp-alice")
                if os.name == "posix":
                    self.assertEqual(result_file.parent.stat().st_mode & 0o077, 0)
                saved = json.loads(result_file.read_text(encoding="utf-8"))
                self.assertEqual((saved["url"], saved["triage_store"]), (BASE, "Default Triage Store"))
                reordered = list(reversed(ENTRIES))
                again = self.update(entries_token(reordered), entries=reordered)
                self.assertEqual((again["written"], again["written_total"], again["result_file"]),
                                 ([], 2, out["result_file"]))
                self.write_config(f"  url: {BASE}\n  user_env: COVTEST_USER\n  key_env: COVTEST_KEY\n"
                                  "  triage_store: Store Two\n")
                other = self.update(token, entries=ENTRIES)
                self.assertEqual(other["written"], ["10001", "G1"])
                self.assertNotEqual(other["result_file"], out["result_file"])
                self.assertEqual([w["store"] for w in self.cov.writes[2:]], ["Store Two", "Store Two"])
            with mock.patch("getpass.getuser", return_value="bob"):
                self.assertEqual(Path(self.update(token, entries=ENTRIES)["result_file"]).parent.name,
                                 "coverity-connect-mcp-bob")

        @unittest.skipUnless(hasattr(os, "getuid"), "the owner of a folder is checked on Linux and macOS")
        def test_refuses_a_record_folder_of_another_user(self) -> None:
            (self.tmp / "systemp" / "coverity-connect-mcp-alice").mkdir()
            with mock.patch("getpass.getuser", return_value="alice"), \
                    mock.patch("os.getuid", return_value=os.getuid() + 1):
                message = self.assertFailure(self.update, entries_token(ENTRIES), entries=ENTRIES)
            self.assertIn("ほかのユーザー", message)
            self.assertEqual(self.cov.writes, [])

        def test_records_never_hold_the_key(self) -> None:
            plan = self.write_plan()
            self.cov.triage_responses[10001] = lambda: cov.json_response(400, {"message": f"bad key {KEY}"})
            self.update(token_of(plan.read_bytes()), plan_file=str(plan))
            text = (self.run_dir / "apply-plan.result.json").read_text(encoding="utf-8")
            self.assertNotIn(KEY, text)
            self.assertIn("****", text)


if __name__ == "__main__":
    unittest.main()
