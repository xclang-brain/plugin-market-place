#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI のフックから呼ばれ、依頼・ツールの呼び出し・報告を記録する。止める目印があれば、ツールを断る。

使い方：hook.py [イベント] < フックの入力（JSON）
  イベント：UserPromptSubmit、PreToolUse、SubagentStop、Stop。省くと入力の hook_event_name を使う。
作業フォルダの .<このスキルのフォルダの名前>/recording が新しくなければ、何もしない（REQ-407、REQ-408）。
どんなときも終了コード 0 で終える。標準出力には、ツールを断るときの JSON だけを出す。
"""

import datetime
import json
import os
import re
import shutil
import sys
import time
import traceback

SKILL = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRESH_SECONDS = 120               # サーバーは 30 秒ごとに recording を書き直す。2 分そのままなら止まったとみなす
EVENT_BYTES = 8 * 1024            # 1件の上限
SESSION_BYTES = 20 * 1024 * 1024  # 1つのセッションの記録の上限
KEEP_SESSIONS = 30                # 残すセッションの数
COMMAND_CHARS = 1000              # コマンドは先頭のこれだけを残す
MASK_CHARS = 16 * 1024            # 伏せる前に、文をこの長さまでにする。1件は 8 KB までなので、これより後ろは残らない
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

SECRET_WORDS = re.compile(r"(?i)pass|token|secret|key")
# 名前=値、名前: 値（"名前": "値" も）。名前に pass・token・secret・key を含むもの。
# 名前の長さに上限を付けて、長い文で正規表現が遅くならないようにする
SECRET_PAIR = re.compile(r"""(?i)([\w.-]{0,64}(?:pass|token|secret|key)[\w.-]{0,64}["']?\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s"',;&|]+)""")
# --password 値 のように、フラグの後に空白で続く値
SECRET_FLAG = re.compile(r"(?i)((?<![\w-])--?[\w-]{0,64}(?:pass|token|secret|key)[\w-]{0,64}\s+)(?!-)(\S+)")
URL_PASSWORD = re.compile(r"(//[^/\s:@]+:)[^@\s/]+(@)")

AGENT_TOOLS = {"agent", "task"}
COMMAND_TOOLS = {"bash", "powershell", "shell"}
ASK_TOOLS = {"askuserquestion", "ask_user"}
FILE_TOOLS = {"read", "write", "edit", "multiedit", "notebookedit", "glob", "grep", "ls",
              "view", "create", "str_replace_editor", "apply_patch"}
PATH_KEYS = ("file_path", "filePath", "path", "notebook_path")
CONTENT_KEYS = {"content", "new_string", "old_string", "new_str", "old_str", "file_text",
                "edits", "new_source", "patch"}
EVENTS = {"userpromptsubmit": "UserPromptSubmit", "userpromptsubmitted": "UserPromptSubmit",
          "pretooluse": "PreToolUse", "subagentstop": "SubagentStop",
          "stop": "Stop", "agentstop": "Stop"}

DENY_REASON = ("利用者が Web管理画面で実行を止めました。これ以上ツールを使わずに作業を終え、"
               "ここまでにしたことを報告してください。")
FULL_NOTE = "記録が 20 MB を超えたので、ここから後は記録していません。"


def pick(data, *names):
    """snake_case（Claude Code）と camelCase（Copilot CLI）のどちらの名前でも値を取る"""
    for name in names:
        if data.get(name) not in (None, ""):
            return data[name]
    return None


def text_of(value):
    return value if isinstance(value, str) else ("" if value is None else str(value))


def mask(text):
    text = URL_PASSWORD.sub(r"\1***\2", text[:MASK_CHARS])
    text = SECRET_PAIR.sub(r"\1***", text)
    return SECRET_FLAG.sub(r"\1***", text)


def mask_data(value):
    if isinstance(value, dict):
        return {k: "***" if SECRET_WORDS.search(str(k)) and not isinstance(v, (dict, list)) else mask_data(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask_data(v) for v in value]
    return mask(value) if isinstance(value, str) else value


def short(name):
    """plugin:agent の形から、エージェントの名前だけを取り出す"""
    return text_of(name).split(":")[-1].strip()


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def tool_record(name, args):
    """ツールの呼び出しを、記録する形にする（設計書 8.4）"""
    if isinstance(args, str):  # Copilot CLI は toolArgs を JSON の文字列で渡すことがある
        try:
            args = json.loads(args)
        except ValueError:
            args = {"command": args}
    args = args if isinstance(args, dict) else {}
    low = name.lower()
    if low in AGENT_TOOLS:
        agent = pick(args, "subagent_type", "agent_type", "agentType") or "general-purpose"
        return {"kind": "request", "agent": short(agent), "text": mask(text_of(pick(args, "prompt")))}
    record = {"kind": "tool", "tool": name}
    if low in COMMAND_TOOLS or (isinstance(args.get("command"), str) and low not in FILE_TOOLS):
        record["input"] = mask(text_of(args.get("command")))[:COMMAND_CHARS]
    elif low in ASK_TOOLS:
        questions = args.get("questions") or [args]
        record["input"] = mask("\n".join(text_of(q.get("question")) for q in questions if isinstance(q, dict)))
    elif low in FILE_TOOLS:
        keys = PATH_KEYS + (("pattern",) if low == "glob" else ())
        record["input"] = mask("\n".join(text_of(args[k]) for k in keys if args.get(k)))
    else:
        rest = {k: v for k, v in args.items() if k not in CONTENT_KEYS}
        record["input"] = json.dumps(mask_data(rest), ensure_ascii=False)[:COMMAND_CHARS]
    return record


def encode(event):
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8", "replace")


def fit(event):
    """1件を 8 KB までにする。長い文は後ろを省く"""
    data = encode(event)
    field = "text" if "text" in event else "input"
    while len(data) > EVENT_BYTES and event.get(field):
        keep = len(event[field]) - (len(data) - EVENT_BYTES) - 64
        event[field] = event[field][:keep] + "…（長いので省略）" if keep > 0 else ""
        event["cut"] = True
        data = encode(event)
    return data


def write_file(path, data, append):
    flags = os.O_WRONLY | os.O_CREAT | NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def make_dir(path):
    if not os.path.isdir(path):
        os.mkdir(path, 0o700)


def session_folder(base, session):
    """記録を置くフォルダ。シンボリックリンクがあれば None"""
    sessions = os.path.join(base, "sessions")
    folder = os.path.join(sessions, session)
    if os.path.islink(sessions) or os.path.islink(folder):
        return None
    if not os.path.isdir(folder):
        make_dir(sessions)
        make_dir(folder)
        prune(sessions, session)
    return folder


def prune(sessions, current):
    """新しい 30 件だけを残す"""
    def updated(name):
        path = os.path.join(sessions, name)
        events = os.path.join(path, "events.jsonl")
        return os.path.getmtime(events if os.path.isfile(events) else path)
    names = [n for n in os.listdir(sessions)
             if n != current and os.path.isdir(os.path.join(sessions, n))
             and not os.path.islink(os.path.join(sessions, n))]
    for name in sorted(names, key=updated)[:max(0, len(names) + 1 - KEEP_SESSIONS)]:
        shutil.rmtree(os.path.join(sessions, name), ignore_errors=True)


def append(folder, event):
    path = os.path.join(folder, "events.jsonl")
    if os.path.islink(path):
        return
    if os.path.isfile(path) and os.path.getsize(path) >= SESSION_BYTES:
        full = os.path.join(folder, "full")
        if os.path.lexists(full):
            return
        write_file(full, b"", False)
        event = {"kind": "note", "text": FULL_NOTE}
    write_file(path, fit(dict(event, time=now())), True)


def is_recording(base):
    recording = os.path.join(base, "recording")
    if os.path.islink(base) or os.path.islink(recording) or not os.path.isfile(recording):
        return False
    with open(recording, encoding="utf-8") as f:
        stamp = float(json.load(f)["time"])
    return abs(time.time() - stamp) < FRESH_SECONDS


def deny():
    if os.environ.get("COPILOT_CLI"):
        out = {"permissionDecision": "deny", "permissionDecisionReason": DENY_REASON}
    else:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": DENY_REASON}}
    sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def handle(args, raw, base):
    data = json.loads(raw.decode("utf-8-sig", "replace"))
    if not isinstance(data, dict):
        return
    event = EVENTS.get(text_of(args[0] if args else pick(data, "hook_event_name", "hookEventName")).lower())
    session = re.sub(r"[^A-Za-z0-9_-]", "_", text_of(pick(data, "session_id", "sessionId")))[:64] or "default"
    folder = session_folder(base, session)
    if folder is None or event is None:
        return
    stop = os.path.join(folder, "stop")
    agent = short(pick(data, "agent_type", "agentType", "agentName"))
    sub = bool(pick(data, "agent_id", "agentId"))
    if event == "UserPromptSubmit":
        if os.path.lexists(stop):
            os.remove(stop)
        append(folder, {"kind": "prompt", "text": mask(text_of(pick(data, "prompt")))})
    elif event == "PreToolUse":
        stopped = os.path.lexists(stop)
        if stopped:
            deny()
        record = tool_record(text_of(pick(data, "tool_name", "toolName")),
                             pick(data, "tool_input", "toolInput", "toolArgs"))
        record.update({"from" if record["kind"] == "request" else "agent": agent, "sub": sub})
        if stopped:
            record["denied"] = True
        append(folder, record)
    else:
        text = mask(text_of(pick(data, "last_assistant_message", "lastAssistantMessage")))
        append(folder, {"kind": "report", "agent": agent, "sub": event == "SubagentStop", "text": text})


def main():
    base = None
    try:
        raw = sys.stdin.buffer.read()
        project = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("COPILOT_PROJECT_DIR") or os.getcwd()
        candidate = os.path.join(project, "." + SKILL)
        if is_recording(candidate):
            base = candidate
            handle(sys.argv[1:], raw, base)
    except BaseException:  # 記録に失敗しても、ツールの呼び出しは通す
        try:
            logs = os.path.join(base, "logs") if base else None
            if logs and not os.path.islink(logs):
                make_dir(logs)
                line = "%s %s\n" % (now(), traceback.format_exc(limit=2).strip().replace("\n", " | "))
                write_file(os.path.join(logs, "hook.log"), line.encode("utf-8", "replace"), True)
        except BaseException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
