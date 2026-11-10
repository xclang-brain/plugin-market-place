#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""CLI のフックから呼ばれ、依頼・ツールの呼び出し・報告を記録する。停止フラグがあれば、ツールを断る。

使い方：hook.py [イベント] < フックの入力（JSON）
  イベント：UserPromptSubmit、PreToolUse、SubagentStop、Stop。省くと入力の hook_event_name を使う。
作業フォルダの記録中フラグ（.debug-dashboard/recording）が新しくなければ、何もしない（REQ-407、REQ-408）。
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

DATA = ".debug-dashboard"         # フックの設定（hooks.json）も、この名前を使う
FRESH_SECONDS = 120               # サーバーは 30 秒ごとに recording を書き直す。2 分そのままなら止まったとみなす
EVENT_BYTES = 8 * 1024            # 1件の上限
SESSION_BYTES = 20 * 1024 * 1024  # 1つのセッションの記録の上限
KEEP_SESSIONS = 30                # 残すセッションの数
COMMAND_CHARS = 1000              # コマンドは先頭のこれだけを残す
MASK_CHARS = 16 * 1024            # 伏せる前に、文をこの長さまでにする。1件は 8 KB までなので、これより後ろは残らない
TRANSCRIPT_WAIT = 3.0             # Copilot CLI は、Stop のフックを動かしてから会話の記録を書き足す。これだけ待つ
TRANSCRIPT_TAIL = 4 * 1024 * 1024  # 会話の記録は、後ろのこれだけを読む
FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)

SECRET_WORDS = re.compile(r"(?i)pass|token|secret|key")
# 名前=値、名前: 値（"名前": "値"、全角の ＝ と ： も）。名前に pass・token・secret・key を含むもの。
# 閉じていない引用符は、行の終わりまで伏せる。名前の長さに上限を付けて、長い文で正規表現が遅くならないようにする
SECRET_PAIR = re.compile(r"""(?i)([\w.-]{0,64}(?:pass|token|secret|key)[\w.-]{0,64}["']?\s*[=:＝：]\s*)"""
                         r"""("[^"\n]*"?|'[^'\n]*'?|[^\s"',;&|]+)""")
# --password 値 のように、フラグの後に空白で続く値
SECRET_FLAG = re.compile(r"(?i)((?<![\w-])--?[\w-]{0,64}(?:pass|token|secret|key)[\w-]{0,64}\s+)(?!-)(\S+)")
# -u 利用者:パスワード（curl）、-u 利用者,パスワード（lftp）、sshpass -p パスワード
OPTION_PASSWORD = re.compile(r"((?<![\w-])(?:(?:-u|--user)\s+[^\s:,]+[:,]|sshpass\s+-p\s*))(\S+)")
# //利用者:パスワード@ホスト。パスワードに @ があっても、ホストの前の最後の @ までを伏せる
URL_PASSWORD = re.compile(r"(//[^/\s:@]+:)[^\s/]+?(@)(?=[^@\s/]*(?:[/\s]|$))")

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
NOTICE = "<task-notification>"  # バックグラウンドのサブエージェントが終わった知らせ。利用者の依頼ではない
HANDBACK = "subagenthandback"   # 自動モードで、サブエージェントが最後の報告を渡すツール

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
    text = SECRET_FLAG.sub(r"\1***", text)
    return OPTION_PASSWORD.sub(r"\1***", text)


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


def clean(value):
    """ファイルの名前に使えるように、英数字・_・- のほかを _ に替える"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", text_of(value))[:64]


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
    flags = os.O_WRONLY | os.O_CREAT | FLAGS | (os.O_APPEND if append else os.O_TRUNC)
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


def requested(folder, agent):
    """このセッションで、そのエージェントへの依頼（Agent の呼び出し）を記録したか。
    記録は encode() で書いた形なので、行の始まりで見分ける"""
    path = os.path.join(folder, "events.jsonl")
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    head = '{"kind": "request", "agent": %s,' % json.dumps(agent, ensure_ascii=False)
    with open(path, encoding="utf-8", errors="replace") as f:
        return any(line.startswith(head) for line in f)


def waiting(data):
    """バックグラウンドで動いているサブエージェントがあるか（Stop の入力の background_tasks）"""
    tasks = pick(data, "background_tasks", "backgroundTasks")
    return isinstance(tasks, list) and any(isinstance(t, dict) and t.get("type") == "subagent" for t in tasks)


def session_of(data):
    """記録を置くセッション。Copilot CLI のサブエージェントの中のフックは、session_id がサブエージェント自身の ID
    （agent_id と同じ）なので、親の parent_agent_id にまとめる。統括エージェントが直接依頼したエージェントなら、親はメインの会話"""
    session = pick(data, "session_id", "sessionId")
    agent_id = pick(data, "agent_id", "agentId")
    parent = pick(data, "parent_agent_id", "parentAgentId")
    if agent_id and parent and session == agent_id:
        session = parent
    return clean(session) or "default"


def transcript_lines(path):
    """会話の記録（JSONL）の後ろの部分を、行ごとに返す。最初の行は途中から始まることがあるが、JSON として読めないので飛ばされる"""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TRANSCRIPT_TAIL))
        return f.read().decode("utf-8", "replace").splitlines()


def copilot_last_message(data):
    """Copilot CLI の Stop の入力には、最後の報告がない。会話の記録（transcript_path の events.jsonl）から、
    このターンのメインのエージェント（agentId がない）の最後の発言（assistant.message）を返す。
    このターンの終わりは、Stop のフックの開始（hook.start、agentStop、入力と同じ timestamp）の行。
    始まりは、その前のメインの依頼（user.message）か、前のターンの Stop のフックの開始の行。
    このターンに発言がなければ None（前のターンの発言を使わない）。
    記録は少し遅れて書かれるので、終わりの行が書かれるまで待つ。待っても書かれなければ None（記録しない）"""
    path, stamp = text_of(pick(data, "transcript_path", "transcriptPath")), text_of(pick(data, "timestamp"))
    deadline = time.time() + TRANSCRIPT_WAIT
    while path and stamp:
        last = None
        for line in (transcript_lines(path) if os.path.isfile(path) else []):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("agentId") or event.get("checkpoint"):
                continue
            body = event.get("data") if isinstance(event.get("data"), dict) else {}
            kind = event.get("type")
            if kind == "user.message":
                last = None  # ターンの始まり
            elif kind == "assistant.message" and text_of(body.get("content")).strip():
                last = text_of(body.get("content"))
            elif kind == "hook.start" and body.get("hookType") == "agentStop":
                if event.get("timestamp") == stamp:
                    return last
                last = None  # 前のターンの終わり
        if time.time() >= deadline:
            break
        time.sleep(0.1)
    return None


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
    if event is None:
        return
    agent_id = clean(pick(data, "agent_id", "agentId"))
    if agent_id and event in ("UserPromptSubmit", "Stop"):
        return  # Copilot CLI は、サブエージェントの始めと終わりにも、これらを起こす。依頼と報告は、Agent と SubagentStop で記録する
    folder = session_folder(base, session_of(data))
    if folder is None:
        return
    stop = os.path.join(folder, "stop")
    agent = short(pick(data, "agent_type", "agentType", "agentName"))
    handback = os.path.join(folder, "handback-" + agent_id)  # このサブエージェントの報告は記録した
    if event == "UserPromptSubmit":
        prompt = text_of(pick(data, "prompt"))
        if prompt.strip().startswith(NOTICE):
            return
        if os.path.lexists(stop):
            os.remove(stop)
        append(folder, {"kind": "prompt", "text": mask(prompt)})
    elif event == "PreToolUse":
        tool = text_of(pick(data, "tool_name", "toolName"))
        tool_input = pick(data, "tool_input", "toolInput", "toolArgs")
        if tool.lower() == HANDBACK:  # 断ると報告が届かないので、止めていても断らない
            message = tool_input.get("message") if isinstance(tool_input, dict) else tool_input
            append(folder, {"kind": "report", "agent": agent, "sub": True, "text": mask(text_of(message))})
            if agent_id:
                write_file(handback, b"", False)
            return
        stopped = os.path.lexists(stop)
        if stopped:
            deny()
        record = tool_record(tool, tool_input)
        record.update({"from" if record["kind"] == "request" else "agent": agent, "sub": bool(agent_id)})
        if stopped:
            record["denied"] = True
        append(folder, record)
    else:
        text = pick(data, "last_assistant_message", "lastAssistantMessage")
        if event == "SubagentStop":
            if agent_id and os.path.lexists(handback):
                os.remove(handback)
            elif agent and requested(folder, agent):  # CLI が中で動かすエージェントは記録しない
                append(folder, {"kind": "report", "agent": agent, "sub": True, "text": mask(text_of(text))})
        elif not waiting(data):  # サブエージェントが残っていれば、まだ最後の報告ではない
            if text is None and os.environ.get("COPILOT_CLI"):
                text = copilot_last_message(data)
                if text is None:
                    return
            append(folder, {"kind": "report", "agent": agent, "sub": False, "text": mask(text_of(text))})


def main():
    base = None
    try:
        raw = sys.stdin.buffer.read()
        project = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("COPILOT_PROJECT_DIR") or os.getcwd()
        candidate = os.path.join(project, DATA)
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
