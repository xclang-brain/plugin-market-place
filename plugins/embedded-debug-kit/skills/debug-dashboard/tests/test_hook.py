"""フック（hook.py）のテスト。記録、止める目印、パスワードを伏せること、大きさの上限（REQ-405、REQ-407、REQ-408）。

  python3 -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest

import support
from support import DATA, mark_recording, read_events, run_hook, temp_project

sys.path.insert(0, str(support.SCRIPTS))
import server  # noqa: E402  今の段の求め方を確かめる

WORKER = "my-plugin:worker-agent"
VERIFIER = "my-plugin:debug-verifier"


def pre(tool, args, **extra):
    return dict({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": args}, **extra)


class NotRecordingTest(unittest.TestCase):
    def setUp(self):
        self.project = temp_project(self)

    def assert_nothing(self, keep=()):
        code, out = run_hook(self.project, pre("Bash", {"command": "ls"}))
        self.assertEqual((code, out), (0, ""))
        self.assertFalse((self.project / DATA / "sessions" / "s1" / "events.jsonl").exists())

    def test_no_marker(self):
        self.assert_nothing()
        self.assertFalse((self.project / DATA).exists(), "画面を起動していないフォルダに、何かを作りました")

    def test_stale_marker(self):
        base = mark_recording(self.project, age=300)
        (base / "sessions" / "s1").mkdir(parents=True)
        (base / "sessions" / "s1" / "stop").write_text("")
        self.assert_nothing()

    def test_symlinked_marker(self):
        if not support.can_symlink(self.project):
            self.skipTest("この環境ではシンボリックリンクを作れません")
        other = temp_project(self)
        mark_recording(other)
        (self.project / DATA).mkdir()
        os.symlink(str(other / DATA / "recording"), str(self.project / DATA / "recording"))
        self.assert_nothing()

    def test_symlinked_folder(self):
        if not support.can_symlink(self.project):
            self.skipTest("この環境ではシンボリックリンクを作れません")
        other = temp_project(self)
        mark_recording(other)
        os.symlink(str(other / DATA), str(self.project / DATA))
        self.assert_nothing()
        self.assertFalse((other / DATA / "sessions").exists())

    def test_bad_input(self):
        mark_recording(self.project)
        for raw in (b"{not json", b"", b"[1, 2]", b"\xff\xfe"):
            self.assertEqual(run_hook(self.project, raw, "PreToolUse"), (0, ""), raw)


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.project = temp_project(self)
        self.base = mark_recording(self.project)

    def test_records_a_run_in_order(self):
        run_hook(self.project, {"session_id": "s1", "prompt": "直してください"}, "UserPromptSubmit")
        run_hook(self.project, pre("Agent", {"subagent_type": WORKER, "prompt": "試行 1 です"}))
        run_hook(self.project, pre("Bash", {"command": "make"}, agent_id="a1", agent_type=WORKER))
        run_hook(self.project, {"session_id": "s1", "hook_event_name": "SubagentStop", "agent_id": "a1",
                                "agent_type": WORKER, "last_assistant_message": "変えたこと：…"})
        run_hook(self.project, {"session_id": "s1", "hook_event_name": "Stop",
                                "agent_type": "my-plugin:debug-orchestrator", "last_assistant_message": "結論：成功"})
        events = read_events(self.project)
        self.assertEqual([e["kind"] for e in events], ["prompt", "request", "tool", "report", "report"])
        self.assertEqual(events[1]["agent"], "worker-agent")
        self.assertEqual(events[1]["text"], "試行 1 です")
        self.assertEqual((events[2]["agent"], events[2]["sub"], events[2]["input"]), ("worker-agent", True, "make"))
        self.assertEqual((events[3]["sub"], events[3]["text"]), (True, "変えたこと：…"))
        self.assertEqual((events[4]["agent"], events[4]["sub"]), ("debug-orchestrator", False))
        self.assertTrue(all("time" in e for e in events))

    def test_stop_marker_denies_in_claude_code_format(self):
        (self.base / "sessions" / "s1").mkdir(parents=True)
        (self.base / "sessions" / "s1" / "stop").write_text("")
        code, out = run_hook(self.project, pre("Bash", {"command": "make"}))
        self.assertEqual(code, 0)
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("止め", decision["permissionDecisionReason"])
        self.assertTrue(out.isascii())
        self.assertTrue(read_events(self.project)[-1]["denied"])

    def test_stop_marker_denies_in_copilot_format(self):
        (self.base / "sessions" / "s1").mkdir(parents=True)
        (self.base / "sessions" / "s1" / "stop").write_text("")
        code, out = run_hook(self.project, pre("bash", {"command": "make"}), COPILOT_CLI="1")
        decision = json.loads(out)
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("報告", decision["permissionDecisionReason"])
        self.assertNotIn("hookSpecificOutput", decision)

    def test_next_prompt_clears_the_stop_marker(self):
        stop = self.base / "sessions" / "s1" / "stop"
        stop.parent.mkdir(parents=True)
        stop.write_text("")
        run_hook(self.project, {"session_id": "s1", "prompt": "続けて"}, "UserPromptSubmit")
        self.assertFalse(stop.exists())
        self.assertEqual(run_hook(self.project, pre("Bash", {"command": "make"})), (0, ""))

    def test_other_sessions_are_not_denied(self):
        (self.base / "sessions" / "s2").mkdir(parents=True)
        (self.base / "sessions" / "s2" / "stop").write_text("")
        self.assertEqual(run_hook(self.project, pre("Bash", {"command": "make"})), (0, ""))

    def test_masks_passwords(self):
        command = ("curl ftp://root:hunter2@192.168.1.5/ --password=pw1 --token tok2 && "
                   "export API_KEY=key3 DB_PASS='pw 4'")
        run_hook(self.project, pre("Bash", {"command": command}))
        run_hook(self.project, {"session_id": "s1", "prompt": 'password: pw5 と "secret": "pw6"'},
                 "UserPromptSubmit")
        run_hook(self.project, pre("mcp__x__login", {"user": "root", "password": "pw7", "url": "https://u:pw8@h/"}))
        text = (self.base / "sessions" / "s1" / "events.jsonl").read_text(encoding="utf-8")
        for secret in ("hunter2", "pw1", "tok2", "key3", "pw 4", "pw5", "pw6", "pw7", "pw8"):
            self.assertNotIn(secret, text)
        self.assertIn("ftp://root:***@192.168.1.5/", text)
        self.assertIn("root", text)

    def test_file_tools_record_paths_only(self):
        run_hook(self.project, pre("Write", {"file_path": "/src/a.c", "content": "int body_of_file;"}))
        run_hook(self.project, pre("Edit", {"file_path": "/src/b.c", "old_string": "old_text", "new_string": "new_text"}))
        run_hook(self.project, pre("Grep", {"pattern": "grep_pattern", "path": "/src"}))
        events = read_events(self.project)
        self.assertEqual([e["input"] for e in events], ["/src/a.c", "/src/b.c", "/src"])
        text = json.dumps(events)
        for word in ("body_of_file", "old_text", "new_text", "grep_pattern"):
            self.assertNotIn(word, text)

    def test_questions_record_only_the_question(self):
        run_hook(self.project, pre("AskUserQuestion", {"questions": [
            {"question": "どの機器？", "header": "h", "options": [{"label": "option_text"}]}]}))
        self.assertEqual(read_events(self.project)[0]["input"], "どの機器？")

    def test_copilot_field_names(self):
        run_hook(self.project, {"sessionId": "c1", "prompt": "直して"}, "userPromptSubmitted")
        run_hook(self.project, {"sessionId": "c1", "toolName": "bash", "toolArgs": json.dumps({"command": "make"})},
                 "preToolUse")
        events = read_events(self.project, "c1")
        self.assertEqual([(e["kind"], e.get("input")) for e in events], [("prompt", None), ("tool", "make")])

    def test_session_id_is_sanitized(self):
        run_hook(self.project, {"session_id": "../../evil", "prompt": "x"}, "UserPromptSubmit")
        self.assertEqual(os.listdir(str(self.base / "sessions")), ["______evil"])
        self.assertFalse((self.project.parent / "evil").exists())


class LimitTest(unittest.TestCase):
    def setUp(self):
        self.project = temp_project(self)
        self.base = mark_recording(self.project)

    def test_command_keeps_the_first_1000_chars(self):
        run_hook(self.project, pre("Bash", {"command": "x" * 5000}))
        self.assertEqual(len(read_events(self.project)[0]["input"]), 1000)

    def test_one_event_is_at_most_8kb(self):
        run_hook(self.project, {"session_id": "s1", "prompt": "長い依頼" * 5000}, "UserPromptSubmit")
        line = (self.base / "sessions" / "s1" / "events.jsonl").read_bytes()
        self.assertLessEqual(len(line), 8 * 1024)
        self.assertTrue(json.loads(line)["cut"])

    def test_session_stops_recording_after_20mb(self):
        folder = self.base / "sessions" / "s1"
        folder.mkdir(parents=True)
        with open(str(folder / "events.jsonl"), "wb") as f:
            f.truncate(20 * 1024 * 1024)
        for _ in range(3):
            run_hook(self.project, pre("Bash", {"command": "make"}))
        tail = (folder / "events.jsonl").read_bytes()[20 * 1024 * 1024:].decode("utf-8").splitlines()
        self.assertEqual(len(tail), 1)
        self.assertEqual(json.loads(tail[0])["kind"], "note")

    def test_keeps_the_newest_30_sessions(self):
        sessions = self.base / "sessions"
        for i in range(31):
            folder = sessions / ("old%02d" % i)
            folder.mkdir(parents=True)
            (folder / "events.jsonl").write_text("{}\n")
            stamp = time.time() - 1000 + i
            os.utime(str(folder / "events.jsonl"), (stamp, stamp))
        run_hook(self.project, {"session_id": "new", "prompt": "x"}, "UserPromptSubmit")
        names = sorted(os.listdir(str(sessions)))
        self.assertEqual(len(names), 30)
        self.assertIn("new", names)
        self.assertIn("old30", names)
        self.assertNotIn("old01", names)


class StageTest(unittest.TestCase):
    """今の段は、動いているエージェントの名前で決める（設計書 8.6）"""

    def stage(self, *events):
        return server.summarize(list(events))["stage"]

    def test_stages(self):
        prompt = {"kind": "prompt", "text": "直して"}
        work = {"kind": "request", "agent": "worker-agent"}
        work_done = {"kind": "report", "agent": "worker-agent", "sub": True}
        review = {"kind": "request", "agent": "debug-verifier"}
        review_done = {"kind": "report", "agent": "debug-verifier", "sub": True, "text": "C1：合格。"}
        self.assertEqual(self.stage(prompt), "plan")
        self.assertEqual(self.stage(prompt, work), "work")
        self.assertEqual(self.stage(prompt, work, work_done), "plan")
        self.assertEqual(self.stage(prompt, work, work_done, review), "review")
        self.assertEqual(self.stage(prompt, work, work_done, review, review_done), "plan")
        self.assertEqual(self.stage(prompt, dict(work, denied=True)), "plan")
        self.assertEqual(self.stage({"kind": "tool", "agent": "worker-agent", "sub": True}), "work")
        self.assertEqual(self.stage(prompt, work, {"kind": "report", "agent": "", "sub": False}), "plan")

    def test_last_report_and_verdict(self):
        summary = server.summarize([
            {"kind": "report", "agent": "debug-verifier", "sub": True, "text": "C1：不合格。"},
            {"kind": "report", "agent": "worker-agent", "sub": True, "text": "作業の報告"},
            {"kind": "report", "agent": "debug-orchestrator", "sub": False, "text": "結論：失敗"},
        ])
        self.assertEqual((summary["last_report"], summary["last_verdict"]), ("結論：失敗", "C1：不合格。"))


HOOKS_JSON = support.SKILL_DIR.parents[1] / "com.anthropic.claude-code" / "hooks" / "hooks.json"


@unittest.skipUnless(HOOKS_JSON.is_file() and shutil.which("bash") and os.name != "nt",
                     "プラグインの hooks.json がないか、bash がありません（スキルのフォルダだけをコピーしたとき）")
class HooksJsonTest(unittest.TestCase):
    """プラグインのフックの設定のコマンドを、そのまま動かす"""

    def run_command(self, project, payload):
        command = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        env = support.clean_env(CLAUDE_PLUGIN_ROOT=str(HOOKS_JSON.parents[2]), CLAUDE_PROJECT_DIR=str(project))
        return subprocess.run(command, shell=True, input=json.dumps(payload).encode("utf-8"), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_without_marker_exits_0_and_records_nothing(self):
        project = temp_project(self)
        proc = self.run_command(project, pre("Bash", {"command": "ls"}))
        self.assertEqual((proc.returncode, proc.stdout), (0, b""))
        self.assertFalse((project / DATA).exists())

    def test_with_stop_marker_denies(self):
        project = temp_project(self)
        base = mark_recording(project)
        (base / "sessions" / "s1").mkdir(parents=True)
        (base / "sessions" / "s1" / "stop").write_text("")
        proc = self.run_command(project, pre("Bash", {"command": "ls"}))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
