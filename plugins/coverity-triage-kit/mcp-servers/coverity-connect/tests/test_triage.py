"""coverity_preview_triage and coverity_update_triage: token, idempotency, stores, error classes."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
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

        def test_another_session_writing_at_the_same_time_is_kept(self) -> None:
            """Each session has its own server process; they share the record of entries."""
            token = entries_token(ENTRIES)
            path, target = self.tools.entries_result_path(self.tools.config_of(str(self.repo)))
            other = {"0123456789abcdef": {"id": "other", "cids": [20001], "triage_store": "Default Triage Store",
                                          "at": "2026-11-09T10:00:00+09:00"}}

            def other_session_writes():
                # While this call sends 10001, another session writes G1 and an entry of its own.
                saved = self.tools._load_result(path, target)
                saved["written"].update(other)
                saved["written"][self.tools.entry_key(self.tools.TriageEntry.model_validate(ENTRIES[1]))] = {
                    "id": "G1", "cids": [10002, 10003], "at": "2026-11-09T10:00:00+09:00"}
                saved["requests"].append({"by": "other"})
                self.tools.write_json(path, saved)
                return cov.httpx.Response(200)

            self.cov.triage_responses[10001] = other_session_writes
            out = self.update(token, entries=ENTRIES)
            self.assertEqual((out["done"], out["written"], out["written_total"]), (True, ["10001"], 2))
            self.assertEqual(self.puts(), 1)  # G1 is not sent again
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(v["id"] for v in result["written"].values()), ["10001", "G1", "other"])
            self.assertIn({"by": "other"}, result["requests"])
            self.assertTrue(any("PUT" in str(r) for r in result["requests"]))
            self.assertFalse(path.with_name(path.name + ".lock").exists())

        def test_busy_record_sends_nothing_and_asks_to_call_again(self) -> None:
            token = entries_token(ENTRIES)
            path, _ = self.tools.entries_result_path(self.tools.config_of(str(self.repo)))
            lock = path.with_name(path.name + ".lock")
            lock.write_text(f"{os.getpid()} now", encoding="ascii")
            self.tools._held_files.add(str(lock))  # held by another call
            with mock.patch.object(self.tools, "LOCK_WAIT", 0.2):
                out = self.update(token, entries=ENTRIES)
            self.assertEqual((out["done"], out["written"], out["remaining"]), (False, [], ["10001", "G1"]))
            self.assertIn("もう一度呼んで", out["busy_note"])
            self.assertEqual(self.puts(), 0)
            self.assertTrue(lock.exists())  # never taken from a live holder
            self.tools._held_files.discard(str(lock))
            lock.unlink()
            again = self.update(token, entries=ENTRIES)
            self.assertEqual((again["done"], again["written"]), (True, ["10001", "G1"]))
            self.assertNotIn("busy_note", again)

        def hold_lock(self, path: Path) -> Path:
            """The lock of the record, held by another call of this process (never taken as left behind)."""
            lock = path.with_name(path.name + ".lock")
            lock.write_text(f"{os.getpid()} now", encoding="ascii")
            self.tools._held_files.add(str(lock))
            return lock

        def free_lock(self, lock: Path) -> None:
            self.tools._held_files.discard(str(lock))
            lock.unlink()

        @contextlib.contextmanager
        def short_budget(self, seconds: float):
            """A call budget of ``seconds``; the fake Coverity answers at once (no first request of 5 s)."""
            from coverity_mcp import connect

            real = self.tools.Deadline
            with mock.patch.object(self.tools, "Deadline", lambda: real(seconds)), \
                    mock.patch.object(connect.ConnectClient, "FIRST_REQUEST_SECONDS", 0.0):
                yield

        def test_busy_record_after_a_write_is_no_error_and_never_sends_it_twice(self) -> None:
            token = entries_token(ENTRIES)
            path, _ = self.tools.entries_result_path(self.tools.config_of(str(self.repo)))
            self.addCleanup(self.tools._unsaved.clear)
            held: list[Path] = []

            def another_session_takes_the_record():
                held.append(self.hold_lock(path))  # while 10001 is being sent
                return cov.httpx.Response(200)

            self.cov.triage_responses[10001] = another_session_takes_the_record
            started = time.monotonic()
            with self.short_budget(1.0), mock.patch.object(self.tools, "SAVE_GRACE", 0.5):
                out = self.update(token, entries=ENTRIES)  # not an error: 10001 is in Coverity now
            self.assertLess(time.monotonic() - started, 4.0)  # not LOCK_WAIT (10 s) twice
            self.assertEqual((out["done"], out["written"], out["remaining"]), (False, ["10001"], ["G1"]))
            self.assertIn("10001 は Coverity に書きましたが", out["busy_note"])
            self.assertEqual(out["next"], self.tools.CALL_AGAIN)
            self.assertEqual(self.puts(), 1)
            del self.cov.triage_responses[10001]

            with mock.patch.object(self.tools, "LOCK_WAIT", 0.2):  # still busy: nothing sent, still named
                still = self.update(token, entries=ENTRIES)
            self.assertEqual((still["done"], still["written"]), (False, []))
            self.assertIn("10001 は Coverity に書きましたが", still["busy_note"])
            self.assertEqual(self.puts(), 1)

            self.free_lock(held[0])
            again = self.update(token, entries=ENTRIES)
            self.assertEqual((again["done"], again["written"], again["written_total"]), (True, ["G1"], 2))
            self.assertNotIn("busy_note", again)
            self.assertEqual(self.puts(), 2)  # only G1: 10001 is not sent again
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(v["id"] for v in saved["written"].values()), ["10001", "G1"])
            self.assertEqual(self.tools._unsaved, {})

        def test_save_made_good_at_the_end_leaves_no_busy_note(self) -> None:
            import threading

            one = ENTRIES[:1]
            token = entries_token(one)
            self.tools._unsaved.clear()
            path, _ = self.tools.entries_result_path(self.tools.config_of(str(self.repo)))
            self.addCleanup(self.tools._unsaved.clear)

            def another_session_holds_the_record_briefly():
                lock = self.hold_lock(path)  # while 10001 is being sent
                timer = threading.Timer(1.2, self.free_lock, args=(lock,))  # freed before the last save gives up
                timer.start()
                self.addCleanup(timer.cancel)
                return cov.httpx.Response(200)

            self.cov.triage_responses[10001] = another_session_holds_the_record_briefly
            with self.short_budget(1.0), mock.patch.object(self.tools, "SAVE_GRACE", 3.0):
                out = self.update(token, entries=one)
            self.assertEqual((out["done"], out["written"], out["remaining"], out["next"]), (True, ["10001"], [], None))
            self.assertNotIn("busy_note", out)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([v["id"] for v in saved["written"].values()], ["10001"])
            self.assertEqual(self.tools._unsaved, {})

        def test_lock_waits_end_within_the_budget(self) -> None:
            token = entries_token(ENTRIES)
            path, _ = self.tools.entries_result_path(self.tools.config_of(str(self.repo)))
            lock = self.hold_lock(path)
            started = time.monotonic()
            with self.short_budget(1.0):  # LOCK_WAIT stays 10 s
                out = self.update(token, entries=ENTRIES)
            self.assertLess(time.monotonic() - started, 4.0)
            self.assertEqual((out["done"], out["remaining"]), (False, ["10001", "G1"]))
            self.assertIn("busy_note", out)
            self.assertEqual(self.puts(), 0)
            self.free_lock(lock)

        def test_no_send_when_the_time_ran_out_while_waiting_for_the_record(self) -> None:
            real = self.tools.Deadline

            class TimeRunsOut(real):  # time is left before the wait for the lock, none after it
                calls = 0

                def allows(self, client, requests=1):
                    TimeRunsOut.calls += 1
                    return TimeRunsOut.calls == 1

            with mock.patch.object(self.tools, "Deadline", TimeRunsOut):
                out = self.update(entries_token(ENTRIES), entries=ENTRIES)
            self.assertEqual((out["done"], out["written"], out["remaining"]), (False, [], ["10001", "G1"]))
            self.assertEqual(self.puts(), 0)

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
