"""フック（hook.py）のテスト。記録、停止フラグ、パスワードを伏せること、大きさの上限（REQ-405、REQ-407、REQ-408）。

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
import server  # noqa: E402  動いているエージェントの求め方を確かめる

WORKER = "my-plugin:worker-agent"
VERIFIER = "my-plugin:debug-verifier"


def pre(tool, args, **extra):
    return dict({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": args}, **extra)


STAMP = "2026-11-09T18:46:36.580Z"


def transcript_line(kind, data, agent=None, stamp="2026-11-09T18:46:30.000Z", **extra):
    """Copilot CLI の会話の記録（session-state/<セッション>/events.jsonl）の1行"""
    event = dict({"type": kind, "id": "e", "timestamp": stamp, "parentId": None, "data": data}, **extra)
    if agent:
        event["agentId"] = agent
    return json.dumps(event, ensure_ascii=False) + "\n"


def copilot_transcript(folder, final="結論：成功。最後の報告です。", stop_line=True):
    """メインのエージェントの発言、サブエージェントの発言、Stop のフックの開始の行を持つ会話の記録を作る"""
    lines = [transcript_line("user.message", {"content": "直して"}),
             transcript_line("assistant.message", {"content": "計画を立てます。", "toolRequests": [{"name": "task"}]}),
             transcript_line("assistant.message", {"content": "サブエージェントの報告"}, agent="a1"),
             transcript_line("assistant.message", {"content": final}),
             transcript_line("assistant.message", {"content": "古い記録の写し"}, checkpoint=True),
             transcript_line("assistant.message", {"content": ""}),
             transcript_line("hook.start", {"hookInvocationId": "h1", "hookType": "agentStop"}, agent="a1", stamp=STAMP)]
    if stop_line:
        lines.append(transcript_line("hook.start", {"hookInvocationId": "h2", "hookType": "agentStop"}, stamp=STAMP))
    lines.append(transcript_line("assistant.message", {"content": "次のターンの発言"}))
    path = folder / "events.jsonl"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def copilot_stop(path):
    """Copilot CLI の Stop の入力（PascalCase のイベント）。最後の報告はない"""
    return {"hook_event_name": "Stop", "session_id": "s1", "timestamp": STAMP, "cwd": "/w",
            "transcript_path": str(path), "stop_reason": "end_turn", "stop_hook_active": False}


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
                   "export API_KEY=key3 DB_PASS='pw 4' && curl -u root:pw9 x && lftp -u root,pw10 h && "
                   "sshpass -p pw11 ssh h && curl ftp://user:pw12@x@host/")
        run_hook(self.project, pre("Bash", {"command": command}))
        run_hook(self.project, {"session_id": "s1", "prompt": 'password: pw5 と "secret": "pw6"\n'
                                "password：pw13 と password＝pw14\nPASSWORD=\"pw15"}, "UserPromptSubmit")
        run_hook(self.project, pre("mcp__x__login", {"user": "root", "password": "pw7", "url": "https://u:pw8@h/"}))
        text = (self.base / "sessions" / "s1" / "events.jsonl").read_text(encoding="utf-8")
        for secret in ("hunter2", "pw1", "tok2", "key3", "pw 4", "pw5", "pw6", "pw7", "pw8", "pw9", "pw10",
                       "pw11", "pw12", "@x@", "pw13", "pw14", "pw15"):
            self.assertNotIn(secret, text)
        self.assertIn("ftp://root:***@192.168.1.5/", text)
        self.assertIn("ftp://user:***@host/", text)
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

    def test_unknown_event_creates_nothing(self):
        run_hook(self.project, {"session_id": "s9", "hook_event_name": "Notification"})
        self.assertFalse((self.base / "sessions").exists())

    def test_session_id_is_sanitized(self):
        run_hook(self.project, {"session_id": "../../evil", "prompt": "x"}, "UserPromptSubmit")
        self.assertEqual(os.listdir(str(self.base / "sessions")), ["______evil"])
        self.assertFalse((self.project.parent / "evil").exists())

    def stop_run(self):
        stop = self.base / "sessions" / "s1" / "stop"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("")
        return stop

    def test_task_notification_is_not_a_prompt(self):
        """バックグラウンドのサブエージェントが終わった知らせは、利用者の依頼ではない"""
        stop = self.stop_run()
        run_hook(self.project, {"session_id": "s1", "prompt": "\n<task-notification>\n<task-id>a1</task-id>\n"},
                 "UserPromptSubmit")
        self.assertTrue(stop.exists(), "サブエージェントの知らせで、止めた状態が解除されました")
        self.assertEqual(read_events(self.project), [])

    def test_stop_waits_for_background_subagents(self):
        stop = {"session_id": "s1", "hook_event_name": "Stop", "last_assistant_message": "待っています"}
        run_hook(self.project, dict(stop, background_tasks=[{"id": "t1", "type": "subagent", "agent_type": WORKER}]))
        run_hook(self.project, dict(stop, backgroundTasks=[{"id": "t1", "type": "subagent"}]))
        self.assertEqual(read_events(self.project), [])
        run_hook(self.project, dict(stop, background_tasks=[{"id": "t2", "type": "shell"}],
                                    last_assistant_message="結論：成功"))
        self.assertEqual([e["text"] for e in read_events(self.project)], ["結論：成功"])

    def test_handback_is_the_report(self):
        """自動モードでは、サブエージェントは SubagentHandback で最後の報告を渡す"""
        report = "変えたこと：…\npassword=pw16\n" + "x" * 3000
        sub = {"agent_id": "../a1", "agent_type": WORKER}
        done = {"session_id": "s1", "hook_event_name": "SubagentStop", "last_assistant_message": "渡しました"}
        run_hook(self.project, pre("Agent", {"subagent_type": WORKER, "prompt": "試行 1"}))
        self.stop_run()
        self.assertEqual(run_hook(self.project, pre("SubagentHandback", {"message": report}, **sub)), (0, ""))
        folder = self.base / "sessions" / "s1"
        self.assertIn("handback-___a1", os.listdir(str(folder)))
        run_hook(self.project, dict(done, **sub))
        self.assertNotIn("handback-___a1", os.listdir(str(folder)))
        run_hook(self.project, dict(done, agent_id="a2", agent_type=WORKER, last_assistant_message="報告 2"))
        events = read_events(self.project)
        self.assertEqual([e["kind"] for e in events], ["request", "report", "report"])
        self.assertEqual((events[1]["agent"], events[1]["sub"]), ("worker-agent", True))
        self.assertEqual(events[1]["text"], report.replace("pw16", "***"))
        self.assertEqual(events[2]["text"], "報告 2")

    def test_reports_only_from_requested_agents(self):
        """CLI が中で動かすエージェント（入力の候補、/btw）の SubagentStop は記録しない"""
        done = {"session_id": "s1", "hook_event_name": "SubagentStop", "agent_id": "x1", "last_assistant_message": "候補"}
        run_hook(self.project, pre("Agent", {"subagent_type": VERIFIER, "prompt": "確かめて"}))
        for agent_type in ("", "my-plugin:debug-orchestrator", WORKER):
            run_hook(self.project, dict(done, agent_type=agent_type))
        run_hook(self.project, dict(done, agent_type=VERIFIER, last_assistant_message="C1：合格。"))
        events = read_events(self.project)
        self.assertEqual([(e["kind"], e["agent"]) for e in events],
                         [("request", "debug-verifier"), ("report", "debug-verifier")])
        self.assertEqual(events[1]["text"], "C1：合格。")


class CopilotTest(unittest.TestCase):
    """Copilot CLI の PascalCase のイベントの入力（Copilot CLI 1.0.108 での実測の形）"""

    def setUp(self):
        self.project = temp_project(self)
        self.base = mark_recording(self.project)

    def stop_run(self):
        stop = self.base / "sessions" / "s1" / "stop"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("")
        return stop

    def test_subagent_tools_go_to_the_main_session(self):
        """サブエージェントの中のフックは、session_id がサブエージェント自身の ID になる"""
        sub = {"session_id": "a1", "agent_id": "a1", "agent_type": WORKER, "parent_agent_id": "s1"}
        run_hook(self.project, pre("Agent", {"name": "t1", "agent_type": WORKER, "prompt": "試行 1"}), COPILOT_CLI="1")
        run_hook(self.project, pre("Bash", {"command": "make", "description": "build"}, **sub), COPILOT_CLI="1")
        self.stop_run()
        code, out = run_hook(self.project, pre("Bash", {"command": "make"}, **sub), COPILOT_CLI="1")
        self.assertEqual((code, json.loads(out)["permissionDecision"]), (0, "deny"))
        events = read_events(self.project)
        self.assertEqual([(e["kind"], e["agent"]) for e in events],
                         [("request", "worker-agent"), ("tool", "worker-agent"), ("tool", "worker-agent")])
        self.assertTrue(events[2]["denied"] and events[2]["sub"])
        self.assertFalse((self.base / "sessions" / "a1").exists())

    def test_subagent_prompt_and_stop_are_not_recorded(self):
        """サブエージェントも、始めに UserPromptSubmit、終わりに Stop を起こす。依頼でも最後の報告でもない"""
        stop = self.stop_run()
        sub = {"session_id": "a1", "agent_id": "a1", "agent_type": WORKER, "parent_agent_id": "s1"}
        run_hook(self.project, dict(sub, hook_event_name="UserPromptSubmit", prompt="試行 1"), COPILOT_CLI="1")
        run_hook(self.project, dict(sub, hook_event_name="Stop", transcript_path="/nowhere", stop_reason="end_turn"),
                 COPILOT_CLI="1")
        self.assertTrue(stop.exists(), "サブエージェントの始まりで、止めた状態が解除されました")
        self.assertEqual(read_events(self.project), [])
        self.assertEqual(os.listdir(str(self.base / "sessions")), ["s1"])

    def test_subagent_report(self):
        run_hook(self.project, pre("Agent", {"agent_type": VERIFIER, "prompt": "確かめて"}), COPILOT_CLI="1")
        run_hook(self.project, {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a2",
                                "agent_type": VERIFIER, "agent_name": VERIFIER, "agent_display_name": "debug-verifier",
                                "parent_agent_id": "s1", "last_assistant_message": "C1：合格。", "stop_reason": "end_turn"},
                 COPILOT_CLI="1")
        self.assertEqual([(e["kind"], e["agent"], e.get("text")) for e in read_events(self.project)],
                         [("request", "debug-verifier", "確かめて"), ("report", "debug-verifier", "C1：合格。")])

    def test_stop_reads_the_last_message_from_the_transcript(self):
        path = copilot_transcript(temp_project(self), final="結論：成功。password=pw1")
        self.assertEqual(run_hook(self.project, copilot_stop(path), COPILOT_CLI="1"), (0, ""))
        events = read_events(self.project)
        self.assertEqual([(e["kind"], e["sub"], e["text"]) for e in events], [("report", False, "結論：成功。password=***")])

    def test_stop_waits_for_the_transcript(self):
        """会話の記録は、Stop のフックが動いてから少し遅れて書かれる"""
        folder = temp_project(self)
        path = folder / "events.jsonl"
        data = json.dumps(copilot_stop(path)).encode("utf-8")
        proc = subprocess.Popen([sys.executable, str(support.SCRIPTS / "hook.py"), "Stop"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, env=support.clean_env(CLAUDE_PROJECT_DIR=str(self.project),
                                                                              COPILOT_CLI="1"))
        proc.stdin.write(data)
        proc.stdin.close()
        time.sleep(0.5)
        copilot_transcript(folder)
        self.assertEqual(proc.wait(30), 0)
        proc.stdout.close()
        self.assertEqual([e["text"] for e in read_events(self.project)], ["結論：成功。最後の報告です。"])

    def test_stop_records_nothing_without_its_line(self):
        """このターンの Stop の行が書かれなければ、前のターンの発言を最後の報告にしない"""
        path = copilot_transcript(temp_project(self), stop_line=False)
        started = time.time()
        self.assertEqual(run_hook(self.project, copilot_stop(path), COPILOT_CLI="1"), (0, ""))
        self.assertLess(time.time() - started, 9, "フックの時間切れ（10 秒）までに終わること")
        self.assertEqual(read_events(self.project), [])
        missing = dict(copilot_stop(path), transcript_path=str(path.parent / "none.jsonl"))
        self.assertEqual(run_hook(self.project, missing, COPILOT_CLI="1"), (0, ""))
        self.assertEqual(read_events(self.project), [])

    def test_stop_ignores_the_previous_turn(self):
        """このターンにメインのエージェントの発言がなければ、前のターンの発言を最後の報告にしない"""
        previous = [transcript_line("user.message", {"content": "1つ目の依頼"}),
                    transcript_line("assistant.message", {"content": "前のターンの最後の報告"}),
                    transcript_line("hook.start", {"hookInvocationId": "h0", "hookType": "agentStop"})]
        this_turn = [transcript_line("assistant.message", {"content": ""}),
                     transcript_line("hook.start", {"hookInvocationId": "h1", "hookType": "agentStop"}, stamp=STAMP)]
        cases = {"依頼あり": previous + [transcript_line("user.message", {"content": "2つ目の依頼"})] + this_turn,
                 "前のターンの終わりだけ": previous + this_turn}
        for name, lines in cases.items():
            with self.subTest(name):
                path = temp_project(self) / "events.jsonl"
                path.write_text("".join(lines), encoding="utf-8")
                self.assertEqual(run_hook(self.project, copilot_stop(path), COPILOT_CLI="1"), (0, ""))
                self.assertEqual(read_events(self.project), [])


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


class RunningTest(unittest.TestCase):
    """動いているエージェントは、依頼と報告の記録から求める（設計書 8.6）"""

    def running(self, *events):
        return server.summarize(list(events))["running"]

    def test_running_agents(self):
        prompt = {"kind": "prompt", "text": "直して"}
        work = {"kind": "request", "agent": "worker-agent"}
        work_done = {"kind": "report", "agent": "worker-agent", "sub": True}
        review = {"kind": "request", "agent": "debug-verifier"}
        review_done = {"kind": "report", "agent": "debug-verifier", "sub": True, "text": "C1：合格。"}
        done = {"kind": "report", "agent": "", "sub": False}
        self.assertEqual(self.running(), [])
        self.assertEqual(self.running(prompt), ["メインの会話"])
        self.assertEqual(self.running(prompt, work), ["worker-agent"])
        self.assertEqual(self.running(prompt, work, work_done), ["メインの会話"])
        self.assertEqual(self.running(prompt, work, work_done, review), ["debug-verifier"])
        self.assertEqual(self.running(prompt, work, work_done, review, review_done), ["メインの会話"])
        self.assertEqual(self.running(prompt, dict(work, denied=True)), ["メインの会話"])
        self.assertEqual(self.running({"kind": "tool", "agent": "worker-agent", "sub": True}), ["worker-agent"])
        self.assertEqual(self.running(prompt, work, done), [])
        # エージェントを指定して起動したセッション（ほかのエージェントに依頼しない）
        tool = {"kind": "tool", "agent": "worker-agent", "sub": False}
        self.assertEqual(self.running(prompt, tool), ["worker-agent"])
        self.assertEqual(self.running(prompt, tool, dict(done, agent="worker-agent")), [])
        self.assertEqual(server.summarize([prompt, work, work_done, review])["requests"], 2)

    def test_last_report_and_verdict(self):
        summary = server.summarize([
            {"kind": "report", "agent": "debug-verifier", "sub": True, "text": "C1：不合格。"},
            {"kind": "report", "agent": "worker-agent", "sub": True, "text": "作業の報告"},
            {"kind": "report", "agent": "debug-orchestrator", "sub": False, "text": "結論：失敗"},
        ])
        self.assertEqual((summary["last_report"], summary["last_verdict"]), ("結論：失敗", "C1：不合格。"))


HOOKS_JSON = support.SKILL_DIR.parents[1] / "com.anthropic.claude-code" / "hooks" / "hooks.json"
BASH = shutil.which("bash")


@unittest.skipUnless(HOOKS_JSON.is_file() and BASH,
                     "プラグインの hooks.json がないか、bash がありません（スキルのフォルダだけをコピーしたとき）")
class HooksJsonTest(unittest.TestCase):
    """プラグインのフックの設定のコマンドを、CLI と同じく bash で動かす（Windows では Git Bash）"""

    PAYLOADS = {
        "UserPromptSubmit": {"session_id": "s1", "prompt": "直して"},
        "PreToolUse": pre("Agent", {"subagent_type": WORKER, "prompt": "試行 1"}),
        "SubagentStop": {"session_id": "s1", "agent_id": "a1", "agent_type": WORKER, "last_assistant_message": "報告"},
        "Stop": {"session_id": "s1", "last_assistant_message": "結論"},
    }

    def run_command(self, project, event, path=None):
        command = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"][event][0]["hooks"][0]["command"]
        env = support.clean_env(CLAUDE_PLUGIN_ROOT=str(HOOKS_JSON.parents[2]), CLAUDE_PROJECT_DIR=str(project))
        if path:
            env["PATH"] = path
        return subprocess.run([BASH, "-c", command], input=json.dumps(self.PAYLOADS[event]).encode("utf-8"),
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_pre_tool_use_goes_ahead_when_the_hook_fails(self):
        """Git Bash のない Windows では、失敗した PreToolUse のフックがツールを止めることがある"""
        hook = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hook.get("onFailure"), "continue")

    def test_bash_is_not_wsl(self):
        self.assertNotIn("system32", BASH.lower(),
                         "WSL の bash が見つかりました。Git for Windows の bash を PATH の前に置いてください")

    def test_without_marker_exits_0_and_records_nothing(self):
        project = temp_project(self)
        for event in self.PAYLOADS:
            proc = self.run_command(project, event)
            self.assertEqual((proc.returncode, proc.stdout), (0, b""), event)
        self.assertFalse((project / DATA).exists())

    def test_records_every_event(self):
        project = temp_project(self)
        mark_recording(project)
        for event in self.PAYLOADS:
            self.assertEqual(self.run_command(project, event).returncode, 0, event)
        self.assertEqual([e["kind"] for e in read_events(project)], ["prompt", "request", "report", "report"])

    def test_with_stop_marker_denies(self):
        project = temp_project(self)
        base = mark_recording(project)
        (base / "sessions" / "s1").mkdir(parents=True)
        (base / "sessions" / "s1" / "stop").write_text("")
        proc = self.run_command(project, "PreToolUse")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_without_uv_exits_0(self):
        folder = os.path.dirname(BASH)
        if any(shutil.which(name, path=folder) for name in ("uv",)):
            own = temp_project(self)
            if not support.can_symlink(own):
                self.skipTest("uv のない PATH を作れません")
            os.symlink(BASH, str(own / "bash"))
            folder = str(own)
        project = temp_project(self)
        mark_recording(project)
        for event in self.PAYLOADS:
            proc = self.run_command(project, event, path=folder)
            self.assertEqual((proc.returncode, proc.stdout), (0, b""), event)
        self.assertEqual(read_events(project), [])


COPILOT_HOOKS_JSON = support.SKILL_DIR.parents[1] / "com.github.copilot" / "hooks" / "hooks.json"
POWERSHELLS = [p for p in (shutil.which("pwsh"), shutil.which("powershell")) if p]
JAPANESE = "直してください。合格の条件：ログに「完了」と出る"


@unittest.skipUnless(COPILOT_HOOKS_JSON.is_file(),
                     "プラグインの Copilot CLI の hooks.json がありません（スキルのフォルダだけをコピーしたとき）")
class CopilotHooksJsonTest(unittest.TestCase):
    """Copilot CLI のフックの設定のコマンドを、bash の欄は bash で、powershell の欄は PowerShell で動かす。
    Copilot CLI は、PLUGIN_ROOT・COPILOT_PROJECT_DIR・COPILOT_CLI を渡し、入力の JSON を標準入力に UTF-8 で書く"""

    def setUp(self):
        self.config = json.loads(COPILOT_HOOKS_JSON.read_text(encoding="utf-8"))
        self.transcript = copilot_transcript(temp_project(self))

    def payload(self, event):
        return {
            "UserPromptSubmit": {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": JAPANESE},
            "PreToolUse": pre("Agent", {"name": "t1", "agent_type": WORKER, "prompt": "試行 1"}),
            "SubagentStop": {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1",
                             "agent_type": WORKER, "parent_agent_id": "s1", "last_assistant_message": "報告"},
            "Stop": copilot_stop(self.transcript),
        }[event]

    def shells(self):
        found = [("bash", BASH, [BASH, "-c"])] if BASH else []
        found += [("powershell", ps, [ps, "-NoProfile", "-NoLogo", "-Command"]) for ps in POWERSHELLS]  # Copilot CLI と同じ
        if not found:
            self.skipTest("bash も PowerShell もありません")
        return found

    def run_command(self, field, prefix, project, event, path=None):
        env = support.clean_env(PLUGIN_ROOT=str(COPILOT_HOOKS_JSON.parents[2]), COPILOT_PROJECT_DIR=str(project),
                                COPILOT_CLI="1")
        env.pop("PSModulePath", None)  # pwsh から受け継いだものを Windows PowerShell 5.1 に渡さない（モジュールを読み違える）
        env.pop("PSMODULEPATH", None)
        env["PSExecutionPolicyPreference"] = "Restricted"  # Windows の初めの設定。.ps1 のファイルを動かせなくても、フックは動く
        if path:
            env["PATH"] = path
        command = self.config["hooks"][event][0][field]
        return subprocess.run(prefix + [command], input=json.dumps(self.payload(event)).encode("utf-8"), env=env,
                              cwd=str(COPILOT_HOOKS_JSON.parents[2]), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)

    def test_every_event_has_both_shells(self):
        self.assertEqual(self.config["version"], 1)
        self.assertEqual(sorted(self.config["hooks"]), sorted(HooksJsonTest.PAYLOADS))
        for event, hooks in self.config["hooks"].items():
            self.assertEqual(len(hooks), 1, event)
            self.assertTrue(hooks[0]["bash"] and hooks[0]["powershell"], event)
            self.assertEqual(hooks[0]["timeoutSec"], 10, event)

    def test_without_marker_exits_0_and_records_nothing(self):
        for field, shell, prefix in self.shells():
            project = temp_project(self)
            for event in self.config["hooks"]:
                with self.subTest(shell=shell, event=event):
                    proc = self.run_command(field, prefix, project, event)
                    self.assertEqual((proc.returncode, proc.stdout), (0, b""), proc.stderr[-2000:])
            self.assertFalse((project / DATA).exists(), shell)

    def test_records_every_event(self):
        for field, shell, prefix in self.shells():
            with self.subTest(shell=shell):
                project = temp_project(self)
                mark_recording(project)
                for event in self.config["hooks"]:
                    proc = self.run_command(field, prefix, project, event)
                    self.assertEqual((proc.returncode, proc.stdout), (0, b""), (event, proc.stderr[-2000:]))
                events = read_events(project)
                self.assertEqual([e["kind"] for e in events], ["prompt", "request", "report", "report"])
                self.assertEqual(events[0]["text"], JAPANESE, "日本語の依頼が化けました")
                self.assertEqual(events[3]["text"], "結論：成功。最後の報告です。")

    def test_with_stop_marker_denies(self):
        for field, shell, prefix in self.shells():
            with self.subTest(shell=shell):
                project = temp_project(self)
                base = mark_recording(project)
                (base / "sessions" / "s1").mkdir(parents=True)
                (base / "sessions" / "s1" / "stop").write_text("")
                proc = self.run_command(field, prefix, project, "PreToolUse")
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
                decision = json.loads(proc.stdout.decode("ascii"))
                self.assertEqual(decision["permissionDecision"], "deny")
                self.assertTrue(read_events(project)[-1]["denied"])

    def test_without_uv_exits_0(self):
        for field, shell, prefix in self.shells():
            with self.subTest(shell=shell):
                folder = temp_project(self)  # PATH を、何もないフォルダだけにする。シェルは絶対パスで動かす
                project = temp_project(self)
                mark_recording(project)
                for event in self.config["hooks"]:
                    proc = self.run_command(field, prefix, project, event, path=str(folder))
                    self.assertEqual((proc.returncode, proc.stdout), (0, b""), (event, proc.stderr[-2000:]))
                self.assertEqual(read_events(project), [])


if __name__ == "__main__":
    unittest.main()
