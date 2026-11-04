#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""機器のシリアルコンソールにつないで、コマンドを実行し、出力を記録する。

使い方：
  python3 capture.py [run] [--workdir D] [--config F] [--command C]... [--listen-only 秒]
  python3 capture.py config-path [--workdir D] [--config F]
  python3 capture.py check [--workdir D] [--config F]

結果は、標準出力に JSON を1つだけ出す。進み具合は、標準エラーに出す。
終わりのコード：0 は成功、1 は作業の失敗、2 は設定の問題。
Python 3.8 以降の標準ライブラリだけで動く。
"""

import argparse
import codecs
import datetime
import errno
import fcntl
import glob
import json
import os
import re
import select
import shutil
import struct
import sys
import termios
import time
import traceback

SKILL = "log-capture"
CONFIG_DIR_NAME = ".embedded-debug-loop"
TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config-template.yaml"
)

REQUIRED_KEYS = ("port",)
DEFAULT_PROMPT = r"[#$] $"

WAKE_WAIT = 5.0        # 呼びかけ1回で、返事を待つ秒数
WAKE_TRIES = 2         # 呼びかけの回数
LOGIN_WAIT = 10.0      # ユーザー名・パスワードを送った後に待つ秒数
INTERRUPT_WAIT = 5.0   # Ctrl-C の後、プロンプトが戻るのを待つ秒数
LOGOUT_WAIT = 3.0      # exit の後に待つ秒数
QUIET = 0.15           # プロンプトが出た後、続きが来ないことを確かめる秒数
DRAIN_QUIET = 0.2      # コマンドの前に、残りの出力を読み捨てる静かな時間
DRAIN_MAX = 1.0
WRITE_WAIT = 5.0
RC_WAIT = 5.0          # 終了コードを調べるコマンドの返事を待つ秒数
PROMPT_HEAD_MAX = 3    # 複数の行のプロンプトとして扱う、上の行の数の上限
OUTPUT_LINES = 60
LOG_PARTIAL_MAX = 65536  # ログで、改行が届くまで持っておく文字の数の上限
LAST_LINE_MAX = 200    # 結果の文に引く「最後の行」の長さの上限
MASK = "＊＊＊"

# 各コマンドの後に送る、終了コードを調べるコマンド。
# 返事は「__EDL_RC=数」の1行になる。エコーされた行は「__EDL_RC=$?」のままなので、
# 数が付いているかで、返事とエコーを見分けられる。
# 先頭の空白は、bash の履歴に残さないため（HISTCONTROL が ignorespace のとき）。
# この印は、ログのファイルにも書かない。代わりに EXIT_LINE の行を書く。
RC_TAG = "__EDL_RC="
RC_COMMAND = " echo " + RC_TAG + "$?"
RC_OUT_RE = re.compile(re.escape(RC_TAG) + r"([0-9]+)")
RC_ECHO_DONE_RE = re.compile(re.escape(RC_TAG) + r"\$\?[^\n]*\n")
EXIT_LINE = "（終了コード：%s）\n"   # ログで、各コマンドの出力の後に書く行
EXIT_UNKNOWN = "読めませんでした"
GITIGNORE_TEXT = "*\n"  # 設定（パスワード）やログが、誤って git に入らないようにする

LOGIN_RE = re.compile(r"login:\s*$")
PASSWORD_RE = re.compile(r"[Pp]assword:\s*$")
PASSWORD_ANY_RE = re.compile(r"[Pp]assword:")
INCORRECT_RE = re.compile(r"(?i)login incorrect|authentication fail")

ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"           # CSI（色、カーソル移動など）
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC（窓の題名など）
    r"|\x1b[PX^_][^\x1b]*\x1b\\"          # DCS など
    r"|\x1b[ -/]*[0-~]"                   # そのほかの短いもの
)
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_BAUD_CANDIDATES = (
    50, 75, 110, 134, 150, 200, 300, 600, 1200, 1800, 2400, 4800, 9600,
    19200, 38400, 57600, 115200, 230400, 460800, 500000, 576000, 921600,
    1000000, 1152000, 1500000, 2000000, 2500000, 3000000, 3500000, 4000000,
)
BAUDS = {}
for _n in _BAUD_CANDIDATES:
    _c = getattr(termios, "B%d" % _n, None)
    if _c is not None:
        BAUDS[_n] = _c


# ---------------------------------------------------------------------------
# 出力の道具
# ---------------------------------------------------------------------------

def say(msg):
    """進み具合を標準エラーに出す。"""
    try:
        sys.stderr.buffer.write((msg + "\n").encode("utf-8", "replace"))
        sys.stderr.buffer.flush()
    except Exception:
        pass


def emit(obj):
    data = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.buffer.write(data.encode("utf-8"))
        sys.stdout.buffer.flush()
    except AttributeError:  # テストなどで差し替えられたとき
        sys.stdout.write(data)
        sys.stdout.flush()


class Failure(Exception):
    """作業の失敗（kind="job"）か、設定の問題（kind="settings"）。"""

    def __init__(self, message, kind="job", extra=None):
        Exception.__init__(self, message)
        self.message = message
        self.kind = kind
        self.extra = extra or {}


class Disconnected(Exception):
    pass


_OS_REASONS = {
    errno.EACCES: "権限がありません",
    errno.EPERM: "権限がありません",
    errno.ENOENT: "見つかりません",
    errno.ENOSPC: "ディスクに空きがありません",
    errno.EROFS: "読み出し専用の場所です",
    errno.EISDIR: "フォルダです",
    errno.ENOTDIR: "途中にフォルダでないものがあります",
    errno.EEXIST: "同じ名前のファイルがあります",
}


def os_reason(e):
    """OSError の理由を、短い日本語にする。"""
    return _OS_REASONS.get(getattr(e, "errno", None)) or e.strerror or str(e)


# ---------------------------------------------------------------------------
# YAML のごく一部を読む
# ---------------------------------------------------------------------------

class ConfigSyntaxError(Exception):
    def __init__(self, lineno, reason):
        Exception.__init__(self, reason)
        self.lineno = lineno
        self.reason = reason


_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)\s*:(?:\s+(.*))?$")
_ITEM_RE = re.compile(r"^\s*-(?:\s+(.*))?$")
_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+(?:\.[0-9]*)?[eE][-+]?[0-9]+)$")

_DQ_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}
_DQ_HEX = {"x": 2, "u": 4, "U": 8}


def _dq_string(s, start, lineno):
    """s[start] の " から始まる文字列を読み、(値, 次の位置) を返す。"""
    out = []
    i = start + 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            return "".join(out), i + 1
        if c == "\\":
            if i + 1 >= n:
                break
            e = s[i + 1]
            if e in _DQ_ESCAPES:
                out.append(_DQ_ESCAPES[e])
                i += 2
                continue
            if e in _DQ_HEX:
                width = _DQ_HEX[e]
                digits = s[i + 2:i + 2 + width]
                if len(digits) == width and re.match(r"^[0-9A-Fa-f]+$", digits):
                    out.append(chr(int(digits, 16)))
                    i += 2 + width
                    continue
            raise ConfigSyntaxError(
                lineno,
                "\"…\" の中の \\%s は使えません。正規表現などで \\ を使うときは、"
                "'…'（シングルクォート）で囲むと、そのまま書けます。" % e,
            )
        out.append(c)
        i += 1
    raise ConfigSyntaxError(lineno, "\" で始まった値が、\" で閉じていません。")


def _sq_string(s, start, lineno):
    out = []
    i = start + 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "'":
            if i + 1 < n and s[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise ConfigSyntaxError(lineno, "' で始まった値が、' で閉じていません。")


def _rest_ok(rest):
    rest = rest.strip()
    return rest == "" or rest.startswith("#")


def _bare(token):
    t = token.strip()
    if t in ("", "~", "null", "Null", "NULL"):
        return None
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(t):
        return int(t)
    if _FLOAT_RE.match(t):
        return float(t)
    return t


def _flow_list(s, lineno):
    items = []
    i = 1
    n = len(s)
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            raise ConfigSyntaxError(lineno, "[ で始まった並びが、] で閉じていません。")
        if s[i] == "]":
            i += 1
            break
        c = s[i]
        if c == '"':
            value, i = _dq_string(s, i, lineno)
        elif c == "'":
            value, i = _sq_string(s, i, lineno)
        elif c in "[{":
            raise ConfigSyntaxError(lineno, "並びの中に、さらに並びは書けません。")
        else:
            j = i
            while j < n and s[j] not in ",]":
                j += 1
            value = _bare(s[i:j])
            i = j
        items.append(value)
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] == ",":
            i += 1
            continue
        if i < n and s[i] == "]":
            i += 1
            break
        raise ConfigSyntaxError(lineno, "並びの書き方が読めません。[\"a\", \"b\"] の形で書いてください。")
    if not _rest_ok(s[i:]):
        raise ConfigSyntaxError(lineno, "] の後ろに、余分な文字があります。")
    return items


def _scalar(raw, lineno):
    s = raw.strip()
    if s == "":
        return None
    if s[0] == '"':
        value, i = _dq_string(s, 0, lineno)
        if not _rest_ok(s[i:]):
            raise ConfigSyntaxError(lineno, "\"…\" の後ろに、余分な文字があります。")
        return value
    if s[0] == "'":
        value, i = _sq_string(s, 0, lineno)
        if not _rest_ok(s[i:]):
            raise ConfigSyntaxError(lineno, "'…' の後ろに、余分な文字があります。")
        return value
    if s[0] == "[":
        return _flow_list(s, lineno)
    if s[0] in "{&*!|>%@`":
        raise ConfigSyntaxError(lineno, "この書き方には対応していません。値を \"…\" で囲んでください。")
    m = re.search(r"\s#", s)
    if m:
        s = s[:m.start()]
    return _bare(s)


def read_yaml(path):
    """`key: value` だけの YAML を読み、dict を返す。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    data = {}
    block_keys = []
    current_list_key = None
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if stripped == "---" and line.startswith("---"):
            continue
        m_item = _ITEM_RE.match(line)
        if m_item and current_list_key is not None:
            data[current_list_key].append(_scalar(m_item.group(1) or "", idx))
            continue
        if line[0] in " \t":
            raise ConfigSyntaxError(idx, "行の始まりに空白があります。項目は行の始めから書いてください。")
        m = _KEY_RE.match(line)
        if not m:
            raise ConfigSyntaxError(idx, "「項目名: 値」の形になっていません。")
        key, raw = m.group(1), m.group(2)
        if key in data:
            raise ConfigSyntaxError(idx, "項目 %s が2回書かれています。" % key)
        if raw is None or raw.strip() == "" or raw.strip().startswith("#"):
            # 「key:」だけの行。次の行から「  - 値」の並びが続くことがある
            data[key] = []
            block_keys.append(key)
            current_list_key = key
            continue
        current_list_key = None
        data[key] = _scalar(raw, idx)
    # 「key:」の後に並びが続かなかったものは、空（null）とみなす
    for key in block_keys:
        if data[key] == []:
            data[key] = None
    return data


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

class Settings(object):
    pass


def default_config_path(workdir):
    return os.path.join(workdir, CONFIG_DIR_NAME, SKILL + ".yaml")


def _config_dir_of(path):
    """path か、その上にある .embedded-debug-loop フォルダの場所。なければ None。"""
    p = os.path.abspath(path)
    while True:
        if os.path.basename(p) == CONFIG_DIR_NAME:
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def ensure_gitignore(path):
    """path か、その上にある .embedded-debug-loop フォルダに .gitignore がなければ作る
    （中身は * の1行）。すでにあれば、そのままにする。作れなくても止めない。"""
    top = _config_dir_of(path)
    if top is None or not os.path.isdir(top):
        return
    target = os.path.join(top, ".gitignore")
    if os.path.lexists(target):
        return
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:
        return
    try:
        os.write(fd, GITIGNORE_TEXT.encode("utf-8"))
    except OSError:
        pass
    finally:
        os.close(fd)


def make_dirs(path):
    """フォルダを作る。.embedded-debug-loop フォルダに .gitignore がなければ、それも作る。"""
    os.makedirs(path, exist_ok=True)
    ensure_gitignore(path)


def template_blocks(text):
    """ひな形の項目ごとに (名前, 行の並び) を返す。
    行の並びは、項目のすぐ上のコメントと、項目の行（「- 値」の行が続けば、それも）。"""
    lines = text.splitlines()
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0][1:]
    blocks = []
    i = 0
    while i < len(lines):
        m = _KEY_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        end = i + 1
        while end < len(lines) and _ITEM_RE.match(lines[end]):
            end += 1
        blocks.append((m.group(1), lines[start:end]))
        i = end
    return blocks


def add_missing_keys(path):
    """設定ファイルに、ひな形にある項目がなければ、説明のコメントと一緒にファイルの最後に足す。
    すでにある所は1バイトも変えない。足した項目の名前の並びを返す。
    ファイルが読めないときや、書き方の誤りがあるときは、何も足さない。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        have = read_yaml(path)
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            blocks = template_blocks(f.read())
    except (OSError, UnicodeDecodeError, ConfigSyntaxError):
        return []
    missing = [(key, lines) for key, lines in blocks if key not in have]
    if not missing:
        return []
    eol = "\r\n" if b"\r\n" in data else "\n"
    parts = []
    if data and not data.endswith(b"\n"):
        parts.append(eol)
    blank = bool(data.strip()) and not data.endswith(b"\n\n") and not data.endswith(b"\r\n\r\n")
    for n, (key, lines) in enumerate(missing):
        if n > 0 or blank:
            parts.append(eol)
        parts.append(eol.join(lines) + eol)
    try:
        with open(path, "ab") as f:
            f.write("".join(parts).encode("utf-8"))
    except OSError:
        return []
    return [key for key, _lines in missing]


def added_sentence(added):
    return "設定ファイルに、新しい項目 %s を足しました（今の値はひな形のまま）。" % "、".join(added)


def with_added(out, added):
    """結果に、足した項目（added）と、そのことを伝える文を入れる。"""
    out["added"] = list(added)
    if added:
        out["message"] = (out.get("message") or "") + added_sentence(added)
    return out


def open_config(workdir, config):
    """設定ファイルを用意する。なければひな形から作り、項目が足りなければ足す。
    .embedded-debug-loop フォルダに .gitignore がなければ作る。(作ったか, 足した項目) を返す。"""
    ensure_gitignore(os.path.join(workdir, CONFIG_DIR_NAME))
    created = ensure_config(config)
    ensure_gitignore(config)
    added = [] if created else add_missing_keys(config)
    return created, added


def ensure_config(path):
    """設定ファイルがなければ、ひな形から作る。作ったら True。"""
    if os.path.exists(path):
        return False
    if not os.path.isfile(TEMPLATE_PATH):
        raise Failure(
            "設定ファイルのひな形（%s）が見つかりません。スキルを入れ直してください。" % TEMPLATE_PATH,
            kind="settings", extra={"config": path},
        )
    folder = os.path.dirname(path)
    try:
        if folder:
            make_dirs(folder)
        shutil.copyfile(TEMPLATE_PATH, path)
    except OSError as e:
        raise Failure(
            "設定ファイル %s を作れません（%s）。フォルダに書き込めるかを確かめてください。"
            % (path, os_reason(e)),
            kind="settings", extra={"config": path},
        )
    return True


def _is_empty(value):
    return value is None or value == "" or value == []


def missing_keys(cfg):
    return [k for k in REQUIRED_KEYS if _is_empty(cfg.get(k))]


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and (_INT_RE.match(value.strip()) or _FLOAT_RE.match(value.strip())):
        return float(value) if not _INT_RE.match(value.strip()) else int(value)
    return None


def _boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "on"):
            return True
        if v in ("false", "no", "off"):
            return False
    return None


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def validate(cfg):
    """設定を確かめて (Settings, problems) を返す。"""
    s = Settings()
    problems = []

    port = _text(cfg.get("port"))
    if port is None:
        problems.append("port（シリアルポート）は、1つの文字列で書いてください（例：/dev/ttyUSB0）。")
    elif port.strip() == "":
        problems.append("port（機器につながるシリアルポート）が空です。/dev/ttyUSB0 のように書いてください。")
    s.port = (port or "").strip()

    baud = _number(cfg.get("baud"))
    s.baud = None
    if cfg.get("baud") is None or cfg.get("baud") == "":
        problems.append("baud（シリアルの速さ）が空です。115200 のように、機器と同じ速さを書いてください。")
    elif baud is None or int(baud) != baud or int(baud) not in BAUDS:
        problems.append(
            "baud（シリアルの速さ）の %s は使えません。使える値は、9600、38400、57600、115200、230400、460800、921600 などです。"
            % cfg.get("baud")
        )
    else:
        s.baud = int(baud)

    user = _text(cfg.get("login_user"))
    if user is None:
        problems.append("login_user（ログインするユーザー名）は、文字列で書いてください（例：root）。")
        user = ""
    s.login_user = user.strip()

    password = _text(cfg.get("login_password"))
    if password is None:
        problems.append("login_password（ログインのパスワード）は、文字列で書いてください。")
        password = ""
    env_name = _text(cfg.get("login_password_env"))
    if env_name is None:
        problems.append("login_password_env（パスワードを入れた環境変数の名前）は、文字列で書いてください。")
        env_name = ""
    env_name = env_name.strip()
    s.password_env = env_name
    if env_name:
        if env_name in os.environ:
            password = os.environ[env_name]
        elif s.login_user and not password:
            problems.append(
                "login_password_env に書いた環境変数 %s がありません。"
                "その環境変数にパスワードを入れるか、login_password に直接書いてください。" % env_name
            )
        elif s.login_user:
            say("環境変数 %s がないため、login_password の値を使います。" % env_name)
    s.password = password

    prompt = _text(cfg.get("prompt"))
    if prompt is None:
        problems.append("prompt（シェルのプロンプトに合う正規表現）は、文字列で書いてください（例：[#$] $）。")
        prompt = ""
    if prompt == "":
        prompt = DEFAULT_PROMPT
    s.prompt = prompt
    try:
        s.prompt_re = re.compile(prompt)
    except re.error as e:
        s.prompt_re = None
        problems.append("prompt の正規表現 %s が正しくありません（%s）。[#$] $ のように書いてください。" % (prompt, e))

    commands = cfg.get("commands")
    s.commands = []
    if commands is None:
        commands = []
    if isinstance(commands, str):
        commands = [commands]
    if not isinstance(commands, list):
        problems.append("commands（機器で実行するコマンド）は、[\"コマンド1\", \"コマンド2\"] の形で書いてください。")
        commands = []
    for i, c in enumerate(commands, 1):
        t = _text(c)
        if t is None or t.strip() == "":
            problems.append("commands の %d 番目が空か、読めない値です。" % i)
        else:
            s.commands.append(t.strip())

    raw = cfg.get("command_timeout_seconds")
    t = _number(raw)
    s.command_timeout = 30
    if raw is None or raw == "":
        problems.append("command_timeout_seconds（1つのコマンドの出力を待つ秒数）が空です。30 のように秒数を書いてください。")
    elif t is None or t <= 0:
        problems.append("command_timeout_seconds は、0 より大きい数で書いてください（例：30）。")
    else:
        s.command_timeout = t

    raw = cfg.get("listen_seconds")
    t = _number(raw)
    s.listen_seconds = 0
    if raw is None or raw == "":
        pass
    elif t is None or t < 0:
        problems.append("listen_seconds（さらに出力を記録する秒数）は、0 以上の数で書いてください（例：10）。")
    else:
        s.listen_seconds = t

    raw = cfg.get("logout")
    b = _boolean(raw)
    s.logout = True
    if raw is None or raw == "":
        pass
    elif b is None:
        problems.append("logout は true か false で書いてください。")
    else:
        s.logout = b

    known = ("port", "baud", "login_user", "login_password", "login_password_env", "prompt",
             "commands", "command_timeout_seconds", "listen_seconds", "logout")
    for key in cfg:
        if key not in known:
            say("設定ファイルの %s は、このスキルでは使いません（そのままにします）。" % key)
    return s, problems


def summary_of(cfg):
    """設定の要点。パスワードは伏せる。"""
    out = {}
    for key in ("port", "baud", "login_user", "login_password", "login_password_env", "prompt",
                "commands", "command_timeout_seconds", "listen_seconds", "logout"):
        value = cfg.get(key)
        if key == "login_password":
            value = MASK if not _is_empty(value) else ""
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# 文字の整え方
# ---------------------------------------------------------------------------

def _apply_backspaces(s):
    out = []
    for ch in s:
        if ch == "\b":
            if out and out[-1] != "\n":
                out.pop()
        else:
            out.append(ch)
    return "".join(out)


def clean(text):
    """色などの制御文字と \\r を取り除く。"""
    t = ANSI_RE.sub("", text)
    t = t.replace("\r", "")
    if "\b" in t:
        t = _apply_backspaces(t)
    return CTRL_RE.sub("", t)


def _squash(s):
    return re.sub(r"\s+", "", s)


def _same_but_repeats(text, target, slack):
    """text が target と同じか。ただし、すぐ前と同じ文字の余分は slack 個まで許す。

    シェルは長いコマンドを折り返すとき、次の行の頭の1文字を
    「a\\ra」のように2回出すことがある。\\r を除くと「aa」になる。
    """
    j = 0
    extra = 0
    for ch in text:
        if j < len(target) and ch == target[j]:
            j += 1
        elif j > 0 and ch == target[j - 1] and extra < slack:
            extra += 1
        else:
            return False
    return j == len(target)


def remove_echo(lines, command):
    """出力の先頭にある、送ったコマンドの表示（エコー。折り返しを含む）を取り除く。"""
    target = _squash(command)
    if not target:
        return lines
    slack = 1 + len(target) // 10
    acc = ""
    for i in range(min(len(lines), 6)):
        acc += _squash(lines[i])
        if target in acc or _same_but_repeats(acc, target, slack):
            return lines[i + 1:]
        if len(acc) > len(target) + 80:
            break
    return lines


def last_lines(text, n=OUTPUT_LINES):
    lines = text.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    while lines and lines[0].strip() == "":
        lines.pop(0)
    total = len(lines)
    return "\n".join(lines[-n:]), total, total > n


def drop_marker_lines(lines):
    """終了コードを調べるコマンドのエコーと返事の行を除く。

    ふつうは別に読むので、コマンドの出力には入らない。
    機器が遅く、返事が次のコマンドの出力に紛れ込んだときのため。
    """
    return [line for line in lines if RC_TAG not in line]


def strip_marker(line):
    """ログに書く1行から、終了コードを調べるコマンドの印を除く。(残す文字, 返事があったか) を返す。

    送ったコマンドの行（エコー。前のプロンプトを含む）は、行ごと除く。
    返事の行は、返事の所だけを除き、ほかの文字（機器のメッセージなど）が残れば残す。
    """
    if RC_TAG not in line:
        return line, False
    answer = RC_OUT_RE.search(line) is not None
    rest = RC_OUT_RE.sub("", line)
    if RC_TAG in rest or not clean(rest).strip():
        return "", answer
    return rest, answer


def exit_line(code):
    """ログに書く、終了コードの行。"""
    return EXIT_LINE % (EXIT_UNKNOWN if code is None else code)


def find_exit_code(text):
    """終了コードを調べるコマンドの返事から、数を読む。読めなければ None。

    返事の行は、どこにあってもよい（プロンプトや機器のメッセージが前後に付いても読める）。
    """
    found = RC_OUT_RE.findall(text)
    return int(found[-1]) if found else None


def last_output_line(output):
    """出力の最後の行（空の行は除く）。長すぎる行は切る。"""
    for line in reversed(output.split("\n")):
        line = line.strip()
        if line:
            if len(line) > LAST_LINE_MAX:
                line = line[:LAST_LINE_MAX] + "…"
            return line
    return ""


def exit_code_notes(commands):
    """終了コードが 0 でないコマンドを、人に伝える文にする。"""
    notes = []
    for c in commands:
        code = c.get("exit_code")
        if code is None or code == 0:
            continue
        last = last_output_line(c.get("output") or "")
        if last:
            notes.append("コマンド「%s」は終了コード %d で終わりました（最後の行：%s）。" % (c["command"], code, last))
        else:
            notes.append("コマンド「%s」は終了コード %d で終わりました（出力はありませんでした）。" % (c["command"], code))
    return "".join(notes)


# ---------------------------------------------------------------------------
# シリアルポート
# ---------------------------------------------------------------------------

def find_ports():
    found = []
    for pattern in ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        for p in sorted(glob.glob(pattern)):
            if p not in found:
                found.append(p)
    return found


def _lock_owner(path):
    """minicom などが作るロックファイルから、使っているプロセスの番号を返す。"""
    name = os.path.basename(path)
    for folder in ("/var/lock", "/run/lock"):
        lock = os.path.join(folder, "LCK.." + name)
        try:
            with open(lock, "rb") as f:
                raw = f.read(64)
        except OSError:
            continue
        pid = None
        try:
            pid = int(raw.decode("ascii", "ignore").strip())
        except ValueError:
            if len(raw) == 4:
                pid = struct.unpack("i", raw)[0]
        if not pid or pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            return pid
        except OSError:
            continue
        return pid
    return None


def _busy_message(path, pid=None):
    who = "ほかのプログラム（プロセス番号 %d）" % pid if pid else "ほかのプログラム"
    return (
        "シリアルポート %s は、%sが使っています。"
        "端末ソフト（minicom や screen など）を閉じてから、やり直してください。" % (path, who)
    )


def _not_found_message(path):
    msg = (
        "シリアルポート %s が見つかりません。"
        "USB ケーブルがつながっているかと、設定の port が正しいかを確かめてください。" % path
    )
    if not os.path.isabs(path):
        msg += "port は /dev から書いてください（例：/dev/ttyUSB0）。"
    ports = find_ports()
    if ports:
        msg += "この PC で見つかったシリアルポートは、%s です。" % "、".join(ports)
    return msg


def open_port(path, baud):
    pid = _lock_owner(path)
    if pid:
        raise Failure(_busy_message(path, pid))
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Failure(_not_found_message(path), extra={"available_ports": find_ports()})
    except PermissionError:
        raise Failure(
            "シリアルポート %s を開く権限がありません。"
            "今のユーザーを dialout グループに入れてから"
            "（sudo usermod -aG dialout $USER）、PC にログインし直してください。" % path
        )
    except IsADirectoryError:
        raise Failure("%s はフォルダで、シリアルポートではありません。設定の port を確かめてください。" % path)
    except OSError as e:
        if e.errno == errno.EBUSY:
            raise Failure(_busy_message(path))
        if e.errno in (errno.ENXIO, errno.ENODEV):
            raise Failure(
                "シリアルポート %s を開けません（つながる先の機器がありません）。"
                "USB ケーブルを挿し直してください。" % path
            )
        raise Failure("シリアルポート %s を開けません（%s）。"
                      "USB ケーブルと、設定の port を確かめてください。" % (path, os_reason(e)))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise Failure(_busy_message(path))
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            raise Failure("%s はシリアルポートではありません。設定の port を確かめてください。" % path)
        new = [list(x) if isinstance(x, list) else x for x in old]
        iflag, oflag, cflag, lflag = new[0], new[1], new[2], new[3]
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                   | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
                   | termios.IXOFF | getattr(termios, "IXANY", 0))
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | getattr(termios, "CRTSCTS", 0))
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        new[0], new[1], new[2], new[3] = iflag, oflag, cflag, lflag
        new[4] = BAUDS[baud]
        new[5] = BAUDS[baud]
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, new)
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error as e:
            raise Failure("シリアルポート %s の速さなどを設定できません（%s）。"
                          "設定の port と baud を確かめてください。" % (path, e))
        return fd, old
    except BaseException:
        os.close(fd)
        raise


def close_port(fd, old):
    if fd is None:
        return
    try:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSANOW, old)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


class Console(object):
    """シリアルポートとのやりとり。届いた文字は、そのままログにも書く。"""

    def __init__(self, fd, path, log):
        self.fd = fd
        self.path = path
        self.log = log
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.total = 0
        self.known_prompt = None  # 機器のプロンプトの文字（色などを除いたもの）
        self.prompt_head = []     # プロンプトが複数の行のとき、最後の行より上の行
        self.mark()

    def mark(self):
        """ここから先に届いた文字を、1まとまりとして扱う。"""
        self.seg = []
        self.seg_chars = 0
        self.seg_has_nl = False
        self.tail = ""
        self.lines_done = 0          # このまとまりで、改行まで届いた行の数
        self.first_prompt_line = None  # 覚えたプロンプトで始まる最初の行（0 行目は除く）
        self.seen = set()            # 一度でも行の終わりに合ったもの
        self.last_prompt_text = None
        self.nudged = 0
        self.by_evidence = False

    def segment(self):
        return "".join(self.seg)

    def _feed(self, data):
        self.total += len(data)
        text = self.decoder.decode(data)
        if not text:
            return
        if self.log is not None:
            self.log.device(text)
        self.seg.append(text)
        self.seg_chars += len(text)
        pos = text.rfind("\n")
        if pos >= 0:
            self.seg_has_nl = True
            done = (self.tail + text[:pos]).split("\n")
            if self.known_prompt and self.first_prompt_line is None:
                for i, raw_line in enumerate(done):
                    n = self.lines_done + i
                    if n > 0 and clean(raw_line).startswith(self.known_prompt):
                        self.first_prompt_line = n
                        break
            self.lines_done += len(done)
            self.tail = text[pos + 1:][-4096:]
        else:
            self.tail = (self.tail + text)[-4096:]

    def read(self, timeout):
        """届いた文字を読む。読んだバイト数を返す（届かなければ 0）。"""
        try:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        except InterruptedError:
            return 0
        if not ready:
            return 0
        try:
            data = os.read(self.fd, 4096)
        except (BlockingIOError, InterruptedError):
            return 0
        except OSError as e:
            if e.errno in (errno.EIO, errno.ENXIO, errno.ENODEV, errno.EBADF):
                raise Disconnected()
            raise
        if not data:
            raise Disconnected()
        self._feed(data)
        return len(data)

    def read_for(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.read(min(remaining, 0.5))

    def drain(self, quiet=DRAIN_QUIET, limit=DRAIN_MAX):
        """出力が静かになるまで読み捨てる（ログには残る）。"""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self.read(quiet) == 0:
                return

    def send(self, text):
        data = text.encode("utf-8")
        deadline = time.monotonic() + WRITE_WAIT
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Failure(
                    "シリアルポート %s に書き込めません。"
                    "ケーブルと、機器の電源を確かめてください。" % self.path
                )
            try:
                _, ready, _ = select.select([], [self.fd], [], remaining)
            except InterruptedError:
                continue
            if not ready:
                continue
            try:
                n = os.write(self.fd, data)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as e:
                if e.errno in (errno.EIO, errno.ENXIO, errno.ENODEV, errno.EBADF):
                    raise Disconnected()
                raise
            data = data[n:]

    def _match(self, patterns, need_newline):
        if need_newline and not self.seg_has_nl:
            return None
        line = clean(self.tail)
        for name, rx in patterns:
            if rx.search(line):
                self.seen.add(name)
                if name == "prompt":
                    self.last_prompt_text = line
                return name
        return None

    def _search(self, anywhere):
        if not anywhere:
            return None
        text = clean(self.segment())
        for name, rx in anywhere:
            if rx.search(text):
                return name
        return None

    def shell_is_back(self):
        """覚えたプロンプトで始まる行が、コマンドの行の後に出たか。

        機器のメッセージがプロンプトのすぐ後ろに出ると、
        プロンプトが行の終わりに来ないため、これで見分ける。
        """
        if not self.known_prompt:
            return False
        if self.first_prompt_line is not None:
            return True
        return self.lines_done > 0 and clean(self.tail).startswith(self.known_prompt)

    def wait_for(self, patterns, timeout, need_newline=False, anywhere=(),
                 nudge=None, nudge_if=None, evidence=None):
        """届いた文字が合うまで待つ。合った名前か None を返す。

        patterns：最後の行の終わりに合うか（例：プロンプト）。
        anywhere：届いた文字のどこかに合うか（例：Password:）。
        nudge：(静かな秒数, 間の秒数, 回数)。合わないまま止まったら Enter を送る。
               機器のメッセージがプロンプトの後ろに出て、隠れたときのため。
        nudge_if：Enter を送ってよいかを返す関数。
        evidence：Enter を送り切った後と、時間切れのときに呼ぶ関数。
                  名前を返せば、それを結果にする。
        """
        start = time.monotonic()
        deadline = start + timeout
        last_data = start
        last_send = start
        sent = 0
        while True:
            hit = self._match(patterns, need_newline) or self._search(anywhere)
            if hit:
                # 続きが来ないことを確かめる（出力の途中で「# 」が来たときのため）
                if self.read(QUIET) == 0:
                    return hit
                last_data = time.monotonic()
                if last_data > deadline + 1.0:
                    return self._match(patterns, need_newline) or self._search(anywhere)
                continue
            now = time.monotonic()
            if nudge is not None and self.seg_chars > 0:
                quiet, every, limit = nudge
                due = now - last_data >= quiet or now - last_send >= every
                if sent < limit and due and (nudge_if is None or nudge_if()):
                    sent += 1
                    self.nudged += 1
                    self.send("\r")
                    last_send = last_data = now
                    continue
                if sent >= limit and evidence is not None and now - last_send >= every:
                    ev = evidence()
                    if ev:
                        self.by_evidence = True
                        return ev
            if now >= deadline:
                ev = evidence() if evidence is not None else None
                if ev:
                    self.by_evidence = True
                return ev
            n = self.read(min(deadline - now, 0.1))
            if n:
                last_data = time.monotonic()


# ---------------------------------------------------------------------------
# 機器とのやりとりの手順
# ---------------------------------------------------------------------------

def _last_seen(con, n=3):
    text, _, _ = last_lines(clean(con.segment()), n)
    return text


def _no_response(path):
    return "シリアルポート %s から応答がありません。ケーブルと機器の電源を確かめてください。" % path


def _seen(con, *names):
    def check():
        for name in names:
            if name in con.seen:
                return name
        return None
    return check


def _remember_prompt(con):
    if con.last_prompt_text and not con.by_evidence:
        con.known_prompt = con.last_prompt_text


def wake(con, s):
    """Enter を送って、機器の今の状態（login / prompt）を返す。"""
    got_any = False
    attempts = 0
    cancels = 0
    patterns = [("login", LOGIN_RE), ("password", PASSWORD_RE), ("prompt", s.prompt_re)]
    seen = ""
    while attempts < WAKE_TRIES:
        con.mark()
        say("機器に呼びかけます（Enter を送ります）。")
        con.send("\r")
        hit = con.wait_for(patterns, WAKE_WAIT, nudge=(1.0, 1.5, 2),
                           evidence=_seen(con, "login", "prompt"))
        if con.seg_chars > 0:
            got_any = True
            seen = _last_seen(con) or seen
        if hit == "password" and cancels < 2:
            # 前の人がパスワードの入力の途中で離れていた。Enter で取り消す。
            cancels += 1
            continue
        if hit in ("login", "prompt"):
            if hit == "prompt":
                _remember_prompt(con)
            return hit
        attempts += 1
    if not got_any:
        raise Failure(_no_response(s.port))
    msg = (
        "シリアルポート %s から文字は届きますが、ログインの求めも、シェルのプロンプトも出ません。"
        "機器で前のプログラムが動いたままでないかを確かめてください。"
        "また、設定の baud（速さ）と prompt が機器に合っているかも確かめてください。"
        % s.port
    )
    if seen:
        msg += "最後に届いた行は「%s」です。" % seen.replace("\n", " / ")
    raise Failure(msg)


def login(con, s):
    if not s.login_user:
        raise Failure(
            "機器がログインを求めていますが、設定の login_user（ログインするユーザー名）が空です。"
            "ユーザー名を書いてください。",
            kind="settings",
        )
    say("ログインの求めが出ました。%s でログインします。" % s.login_user)
    con.mark()
    con.send(s.login_user + "\r")
    # ここでは Enter を足さない（空のパスワードを送ってしまうため）
    hit = con.wait_for([("password", PASSWORD_RE), ("login", LOGIN_RE), ("prompt", s.prompt_re)],
                       LOGIN_WAIT, anywhere=[("password", PASSWORD_ANY_RE)])
    if hit == "password":
        con.mark()
        con.send(s.password + "\r")
        hit = con.wait_for([("login", LOGIN_RE), ("password", PASSWORD_RE), ("prompt", s.prompt_re)],
                           LOGIN_WAIT, anywhere=[("incorrect", INCORRECT_RE)],
                           nudge=(1.0, 2.0, 3), evidence=_seen(con, "prompt"))
    if hit == "prompt":
        _remember_prompt(con)
        say("ログインしました。")
        return
    if hit in ("login", "password", "incorrect"):
        raise Failure(
            "ユーザー %s でログインできませんでした（ユーザー名かパスワードが違います）。"
            % s.login_user + login_hint(s)
        )
    msg = (
        "ログインの後、シェルのプロンプトが出ません。"
        "設定の prompt が、機器のプロンプトに合っているかを確かめてください。"
    )
    seen = _last_seen(con)
    if seen:
        msg += "最後に届いた行は「%s」です。" % seen.replace("\n", " / ")
    raise Failure(msg)


def login_hint(s):
    """ログインできないときに、確かめることの文。パスワードが空なら、そう言う。"""
    config = getattr(s, "config", None) or "設定ファイル"
    if s.password == "":
        if s.password_env:
            return ("環境変数 %s の中身が空です。その環境変数にパスワードを入れるか、"
                    "%s の login_password に書いてください。" % (s.password_env, config))
        return ("設定の login_password も login_password_env も空です。"
                "機器のパスワードを、%s の login_password に書くか、"
                "login_password_env にパスワードを入れた環境変数の名前を書いてください。" % config)
    where = "login_password "
    if s.password_env and s.password_env in os.environ:
        where = "環境変数 %s に入れたパスワード" % s.password_env
    return "設定の login_user と、%sを確かめてください。" % where


def _shell_back(con):
    return "prompt" if con.shell_is_back() else None


def run_command(con, s, command):
    """コマンドを1つ実行し、(結果, プロンプトが戻ったか, 出力の行) を返す。"""
    con.drain()
    con.mark()
    say("コマンドを実行します：%s" % command)
    con.send(command + "\r")
    patterns = [("prompt", s.prompt_re)]
    waits = dict(need_newline=True, nudge=(0.5, 1.0, 3),
                 nudge_if=con.shell_is_back, evidence=lambda: _shell_back(con))
    hit = con.wait_for(patterns, s.command_timeout, **waits)
    timed_out = hit is None
    back = not timed_out
    if timed_out:
        say("%s 秒たっても終わらないため、Ctrl-C を送ります。" % _num(s.command_timeout))
        con.send("\x03")
        back = con.wait_for(patterns, INTERRUPT_WAIT, **waits) is not None
    lines = clean(con.segment()).split("\n")
    if back and (con.nudged or con.by_evidence) and con.first_prompt_line is not None:
        # プロンプトの後ろに機器のメッセージが出た：最初のプロンプトの行の前までが出力
        lines = lines[:con.first_prompt_line]
    elif back and lines:
        lines = lines[:-1]  # 最後の行はプロンプト
    if back:
        _remember_prompt(con)
    lines = remove_echo(drop_marker_lines(lines), command)
    result = {
        "command": command,
        "output": "",
        "timed_out": timed_out,
        "exit_code": None,  # 終わった後に read_exit_code で読む
        "output_lines": 0,
        "truncated": False,
    }
    set_output(result, lines)
    return result, back, lines


def set_output(result, lines):
    output, total, truncated = last_lines("\n".join(lines))
    result["output"] = output
    result["output_lines"] = total
    result["truncated"] = truncated


def read_exit_code(con, s):
    """終了コードを調べるコマンドを送り、直前のコマンドの終了コードを返す。

    読めなければ None。プロンプトの形に頼らず、返事の行「__EDL_RC=数」を探す。
    この間に届いた行は、ログにはまだ書かない（終わった後に write_exit_line で書く）。
    """
    con.mark()
    if con.log is not None:
        con.log.hold()
    con.send(RC_COMMAND + "\r")
    patterns = [("prompt", s.prompt_re)]
    waits = dict(need_newline=True, nudge=(0.5, 1.0, 3),
                 nudge_if=con.shell_is_back, evidence=lambda: _shell_back(con))
    deadline = time.monotonic() + RC_WAIT
    while True:
        remaining = deadline - time.monotonic()
        hit = con.wait_for(patterns, remaining, **waits) if remaining > 0 else None
        text = clean(con.segment())
        code = find_exit_code(text)
        if hit is None:
            # プロンプトが戻らない。数が届いていれば、それを使う
            break
        if code is not None:
            _remember_prompt(con)
            _remember_prompt_head(con, text)
            break
        if RC_ECHO_DONE_RE.search(text):
            # コマンドは受け付けられたが、数が出ない（$? が使えないシェルなど）
            break
        # 前のプロンプトが遅れて届いただけ。返事を待ち続ける
    if code is None:
        say("終了コードは読み取れませんでした。")
    else:
        say("終了コード：%d" % code)
    return code


def _remember_prompt_head(con, text):
    """終了コードの返事の後ろに出たプロンプトから、最後の行より上の行を覚える。

    複数の行のプロンプトや、端末の幅で折り返した長いプロンプトのとき、
    上の行がコマンドの出力の終わりに混ざるので、それを除くために使う。
    Enter を足したときや、プロンプトを推し量ったときは、まちがえやすいので覚えない。
    """
    if con.by_evidence or con.nudged:
        return
    lines = text.split("\n")
    last = None
    for i, line in enumerate(lines):
        if RC_OUT_RE.search(line):
            last = i
    if last is None:
        return
    head = lines[last + 1:-1]
    if len(head) <= PROMPT_HEAD_MAX:
        con.prompt_head = head


def _prompt_shape(line):
    """プロンプトの行を比べるための形。数の違い（終了コードや時刻を出すプロンプト）は見ない。"""
    return re.sub(r"[0-9]+", "0", line.rstrip())


def trim_prompt_head(lines, head):
    """出力の終わりが、プロンプトの上の行と同じ形なら、それを除く。"""
    if not head or not any(h.strip() for h in head):
        return lines
    n = len(head)
    if len(lines) >= n and [_prompt_shape(x) for x in lines[-n:]] == [_prompt_shape(h) for h in head]:
        return lines[:-n]
    return lines


def listen(con, seconds):
    con.mark()
    say("出力を %s 秒記録します。" % _num(seconds))
    con.read_for(seconds)
    output, total, truncated = last_lines(clean(con.segment()))
    return {"seconds": seconds, "output": output, "output_lines": total, "truncated": truncated}


def partial_command(con, command):
    """途中で切れたコマンドの、そこまでの出力。"""
    lines = remove_echo(drop_marker_lines(clean(con.segment()).split("\n")), command)
    output, total, truncated = last_lines("\n".join(lines))
    return {"command": command, "output": output, "timed_out": False, "exit_code": None,
            "output_lines": total, "truncated": truncated, "interrupted": True}


def partial_listen(con, seconds):
    output, total, truncated = last_lines(clean(con.segment()))
    return {"seconds": seconds, "output": output, "output_lines": total,
            "truncated": truncated, "interrupted": True}


def logout(con, s):
    con.drain()
    con.mark()
    say("ログアウトします（exit を送ります）。")
    con.send("exit\r")
    return con.wait_for([("login", LOGIN_RE)], LOGOUT_WAIT, nudge=(1.0, 1.0, 1),
                        evidence=_seen(con, "login")) is not None


def _num(x):
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


# ---------------------------------------------------------------------------
# ログ
# ---------------------------------------------------------------------------

def open_log(workdir, label):
    folder = os.path.join(workdir, CONFIG_DIR_NAME, "logs")
    try:
        return _open_log(folder, label)
    except OSError as e:
        raise Failure(
            "ログのファイルを %s に作れません（%s）。フォルダに書き込めるかを確かめてください。"
            % (folder, os_reason(e))
        )


def _open_log(folder, label):
    make_dirs(folder)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, "%s-%s.log" % (SKILL, stamp))
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, "%s-%s-%d.log" % (SKILL, stamp, n))
        n += 1
    f = open(path, "w", encoding="utf-8", errors="replace", newline="")
    f.write("# log-capture の記録（%s）\n" % label)
    f.write("# 開始：%s\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return path, LogFile(f)


class LogFile(object):
    """ログのファイル。機器から届いた文字は、そのまま書く。

    ただし、終了コードを調べるコマンドの行（送った行のエコーと、返事の行）は書かない。
    代わりに、各コマンドの出力の後に「（終了コード：0）」の1行を書く。
    行ごとに印を見分けるため、改行が届くまで、その行は書かずに持っておく。
    """

    def __init__(self, f):
        self.f = f
        self.partial = ""  # まだ改行が届いていない、最後の行
        self.held = None   # 終了コードを調べている間に届いた行（終わってから書く）
        self.line_start = True  # 書いた文字が、改行で終わっているか

    def _write(self, text):
        if not text:
            return
        self.line_start = text.endswith("\n")
        try:
            self.f.write(text)
            self.f.flush()
        except Exception:
            pass

    def device(self, text):
        """機器から届いた文字。"""
        self.partial += text
        pos = self.partial.rfind("\n")
        if pos < 0:
            if len(self.partial) > LOG_PARTIAL_MAX and self.held is None and RC_TAG not in self.partial:
                # 改行のない長い出力は、持ちすぎないように先に書く（プロンプトではない）
                self._write(self.partial)
                self.partial = ""
            return
        done = self.partial[:pos].split("\n")
        self.partial = self.partial[pos + 1:]
        out = []
        for line in done:
            line += "\n"
            if self.held is not None:
                self.held.append(line)
            else:
                out.append(strip_marker(line)[0])
        self._write("".join(out))

    def hold(self):
        """ここから、終了コードの行を書くまで、届いた行を持っておく。"""
        if self.held is None:
            self.held = []

    def _release(self):
        """持っていた行を、印を除いて返す。返事の行のすぐ後の位置も返す（なければ None）。"""
        held, self.held = self.held or [], None
        out = []
        at = None
        for line in held:
            text, answer = strip_marker(line)
            if text:
                out.append(text)
            if answer and at is None:
                at = len(out)
        return out, at

    def write_exit_line(self, code, prompt_is_back=True):
        """コマンドの出力の後に、終了コードの行を書く。

        終了コードを調べていたときは、持っていた行を書き、返事の行の所にこの行を置く。
        prompt_is_back が True なら、書きかけの最後の行はプロンプトなので、その前に置く。
        False なら、書きかけの行を先に書いてから置く。
        """
        out, at = self._release()
        if not prompt_is_back and self.partial:
            text = strip_marker(self.partial)[0]
            self.partial = ""
            if text:
                out.append(text + "\n")
        out.insert(len(out) if at is None else at, exit_line(code))
        self._write("".join(out))

    def flush(self):
        """持っている行と、書きかけの行を、印を除いて書く。"""
        out, _ = self._release()
        if self.partial:
            out.append(strip_marker(self.partial)[0])
            self.partial = ""
        self._write("".join(out))

    def note(self, text):
        """スクリプトからの書き込み（「#」の行など）。

        書いた文字が改行で終わっていれば、text の頭の改行は省く（空の行を作らない）。
        """
        self.flush()
        if self.line_start and text.startswith("\n"):
            text = text[1:]
        self._write(text)

    def close(self):
        self.flush()
        self.f.close()


def log_note(f, text):
    if f is None:
        return
    try:
        f.note(text)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------

class ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        m = re.match(r"unrecognized arguments: (.*)", message)
        if m:
            raise ArgError("「%s」は使えない引数です。" % m.group(1))
        m = re.match(r"argument (\S+?)(?:/\S+)?: expected one argument", message)
        if m:
            raise ArgError("%s の後に、値を書いてください。" % m.group(1))
        raise ArgError(as_sentence(message))


SUBCOMMANDS = ("run", "config-path", "check")
_VALUE_OPTIONS = ("--workdir", "--config", "--command", "--listen-only")

USAGE = (
    "python3 capture.py [run] [--workdir フォルダ] [--config ファイル] "
    "[--command \"コマンド\"]... [--listen-only 秒数] / "
    "python3 capture.py config-path [--workdir フォルダ] [--config ファイル] / "
    "python3 capture.py check [--workdir フォルダ] [--config ファイル]"
)


def parse_args(argv):
    sub = "run"
    rest = list(argv)
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in SUBCOMMANDS:
            sub = a
            del rest[i]
            break
        if a in _VALUE_OPTIONS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        raise ArgError("「%s」は使えないサブコマンドです。使えるのは run、config-path、check です。" % a)
    p = _Parser(add_help=False)
    p.add_argument("--workdir")
    p.add_argument("--config")
    p.add_argument("--command", action="append")
    p.add_argument("--listen-only", dest="listen_only")
    p.add_argument("-h", "--help", action="store_true")
    args = p.parse_args(rest)
    args.sub = sub
    if sub != "run" and (args.command or args.listen_only is not None):
        raise ArgError("--command と --listen-only は、run のときだけ使えます。")
    if args.command and args.listen_only is not None:
        raise ArgError("--command と --listen-only は、一緒には使えません。")
    if args.listen_only is not None:
        v = _number(args.listen_only)
        if v is None or v <= 0:
            raise ArgError("--listen-only には、0 より大きい秒数を書いてください（例：--listen-only 10）。")
        args.listen_only = v
    if args.command:
        for c in args.command:
            if c.strip() == "":
                raise ArgError("--command に空のコマンドは書けません。")
        args.command = [c.strip() for c in args.command]
    return args


def _paths(args):
    workdir = os.path.abspath(args.workdir) if args.workdir else os.getcwd()
    config = os.path.abspath(args.config) if args.config else default_config_path(workdir)
    return workdir, config


def _load(config):
    """設定ファイルを読む。(cfg, problems) を返す。"""
    try:
        return read_yaml(config), []
    except ConfigSyntaxError as e:
        return {}, ["設定ファイルの %d 行目が読めません。%s" % (e.lineno, e.reason)]
    except UnicodeDecodeError:
        return {}, ["設定ファイルが UTF-8 で書かれていません。UTF-8 で保存し直してください。"]
    except OSError as e:
        return {}, ["設定ファイル %s を読めません（%s）。" % (config, os_reason(e))]


def cmd_config_path(args):
    workdir, config = _paths(args)
    try:
        created, added = open_config(workdir, config)
    except Failure as f:
        emit({"config": config, "created": False, "missing": list(REQUIRED_KEYS), "problems": [f.message],
              "added": []})
        return 0
    cfg, problems = _load(config)
    out = {"config": config, "created": created, "missing": missing_keys(cfg) if not problems else list(REQUIRED_KEYS)}
    if problems:
        out["problems"] = problems
    emit(with_added(out, added))
    return 0


def as_sentence(text):
    """文の終わりに「。」がなければ足す（次の文とつながって読めなくならないように）。"""
    text = text.rstrip()
    if text and not text.endswith(("。", "！", "？")):
        text += "。"
    return text


def problems_message(problems, config, created):
    """設定の問題を、人に伝える文にする。"""
    head = "設定ファイルがなかったので、ひな形から作りました。" if created else ""
    if len(problems) == 1:
        return "%s%s設定ファイルは %s です。" % (head, as_sentence(problems[0]), config)
    return ("%s設定に直すところが %d 件あります。設定ファイル %s を直してください。"
            "各項目の意味は、項目のすぐ上のコメントに書いてあります。"
            % (head, len(problems), config))


def _settings_problem(config, created, problems, added):
    emit(with_added({
        "ok": False,
        "skill": SKILL,
        "message": problems_message(problems, config, created),
        "problems": problems,
        "config": config,
        "created": created,
    }, added))
    return 2


def cmd_run(args):
    workdir, config = _paths(args)
    created, added = open_config(workdir, config)
    cfg, problems = _load(config)
    if problems:
        return _settings_problem(config, created, problems, added)
    s, problems = validate(cfg)
    if problems:
        return _settings_problem(config, created, problems, added)
    s.config = config

    commands = list(s.commands)
    listen_seconds = s.listen_seconds
    listen_only = False
    if args.listen_only is not None:
        commands = []
        listen_seconds = args.listen_only
        listen_only = True
    elif args.command:
        commands = list(args.command)
    if not commands and not listen_seconds:
        return _settings_problem(config, created, [
            "実行するコマンド（commands）が空で、記録する秒数（listen_seconds）も 0 です。"
            "どちらかを設定してください。"
        ], added)
    if not commands:
        listen_only = True

    log_path, log = open_log(workdir, "run")
    progress = {}
    log_note(log, "# シリアルポート：%s（速さ %d）\n" % (s.port, s.baud))
    for c in commands:
        log_note(log, "# 実行するコマンド：%s\n" % c)
    if commands:
        log_note(log, "# 各コマンドの出力の後に、機器での終了コードを「（終了コード：数）」の行で書く\n")
    if listen_only:
        log_note(log, "# コマンドは実行せず、%s 秒記録する\n" % _num(listen_seconds))
    log_note(log, "# ---- ここから下は、機器から届いた文字そのまま ----\n")

    result = {
        "ok": False,
        "skill": SKILL,
        "message": "",
        "port": s.port,
        "baud": s.baud,
        "logged_in": False,
        "commands": [],
        "listen": None,
        "log": log_path,
        "config": config,
    }
    code = 1
    fd = old = None
    con = None
    try:
        say("シリアルポート %s を開きます（速さ %d）。" % (s.port, s.baud))
        fd, old = open_port(s.port, s.baud)
        con = Console(fd, s.port, log)
        timed_out_cmd = None
        stuck = False
        if commands:
            state = wake(con, s)
            if state == "login":
                login(con, s)
                result["logged_in"] = True
            else:
                say("シェルのプロンプトを確かめました（ログインは不要でした）。")
            for i, c in enumerate(commands):
                progress = {"phase": "command", "command": c, "rest": commands[i + 1:]}
                r, back, lines = run_command(con, s, c)
                result["commands"].append(r)
                if r["timed_out"] and timed_out_cmd is None:
                    timed_out_cmd = c
                if not back:
                    stuck = True
                    result["not_run"] = commands[i + 1:]
                    log.write_exit_line(None, prompt_is_back=False)
                    break
                if not r["timed_out"]:
                    # Ctrl-C で止めたときは、終了コードを調べない（null のまま）
                    progress = {"phase": "exit_code", "command": c, "rest": commands[i + 1:]}
                    r["exit_code"] = read_exit_code(con, s)
                log.write_exit_line(r["exit_code"])
                trimmed = trim_prompt_head(lines, con.prompt_head)
                if trimmed is not lines:
                    set_output(r, trimmed)
        progress = {}
        if listen_seconds and not stuck:
            progress = {"phase": "listen"}
            result["listen"] = listen(con, listen_seconds)
            progress = {}
        if result["logged_in"] and s.logout and not stuck:
            result["logged_out"] = logout(con, s)

        # 結果の文
        if stuck:
            rest = len(result.get("not_run", []))
            msg = (
                "コマンド「%s」が %s 秒で終わらず、Ctrl-C を送ってもプロンプトが戻りませんでした。"
                % (timed_out_cmd, _num(s.command_timeout))
            )
            if rest:
                msg += "残りの %d 個のコマンドは実行していません。" % rest
            msg += ("機器でコマンドが動いたままかもしれません。"
                    "機器のコンソールでプログラムを止めるか、機器の電源を入れ直してください。"
                    "そこまでの出力は記録しました。")
            result["message"] = msg
            code = 1
        elif timed_out_cmd is not None:
            result["message"] = (
                "コマンド「%s」が %s 秒で終わらなかったため、Ctrl-C で止めました。"
                "そこまでの出力は記録しました。" % (timed_out_cmd, _num(s.command_timeout))
            )
            code = 1
        elif listen_only:
            lst = result["listen"]
            if lst["output_lines"]:
                result["message"] = "シリアルポート %s の出力を %s 秒記録しました（%d 行）。" % (
                    s.port, _num(listen_seconds), lst["output_lines"])
            else:
                result["message"] = (
                    "シリアルポート %s を %s 秒見ていましたが、何も出力されませんでした。"
                    "出力があるはずなら、ケーブルと機器の電源を確かめてください。" % (s.port, _num(listen_seconds))
                )
            code = 0
        else:
            msg = "シリアルポート %s で %d 個のコマンドを実行し、出力を記録しました。" % (s.port, len(commands))
            if result["listen"]:
                msg += "そのあと %s 秒、出力を記録しました。" % _num(listen_seconds)
            result["message"] = msg
            code = 0
        # 機器のコマンドが 0 以外の終了コードで終わったとき（取ること自体はできているので ok は変えない）
        result["message"] += exit_code_notes(result["commands"])
        if result.get("logged_out") is False:
            result["message"] += "exit を送りましたが、ログインの求めに戻ったかは確かめられませんでした。"
        result["ok"] = code == 0
    except Failure as f:
        result["message"] = f.message
        result.update(f.extra)
        if f.kind == "settings":
            result["problems"] = [f.message]
            code = 2
        else:
            code = 1
    except Disconnected:
        result["message"] = (
            "シリアルポート %s とのやりとりが、途中で切れました。"
            "USB ケーブルが抜けていないかを確かめてください。" % s.port
        )
        if con is not None and progress.get("phase") in ("command", "exit_code"):
            log.write_exit_line(None, prompt_is_back=False)
            if progress["phase"] == "command":
                result["commands"].append(partial_command(con, progress["command"]))
            if progress["rest"]:
                result["not_run"] = progress["rest"]
                result["message"] += "残りの %d 個のコマンドは実行していません。" % len(progress["rest"])
        elif con is not None and progress.get("phase") == "listen":
            result["listen"] = partial_listen(con, listen_seconds)
        if result["commands"] or result["listen"]:
            result["message"] += "切れるまでの出力は記録しました。"
        result["message"] += exit_code_notes(result["commands"])
        code = 1
    except Exception as e:  # 思わぬエラー：くわしくはログへ
        log_note(log, "\n# 思わぬエラー：\n" + traceback.format_exc())
        result["message"] = "思わぬエラーで止まりました（%s: %s）。くわしくはログを見てください。" % (
            type(e).__name__, e)
        code = 1
    finally:
        close_port(fd, old)
        log_note(log, "\n# ---- ここまで ----\n# 結果：%s\n" % result["message"])
        try:
            log.close()
        except Exception:
            pass
    say("ログ：%s" % log_path)
    emit(with_added(result, added))
    return code


def cmd_check(args):
    workdir, config = _paths(args)
    created, added = open_config(workdir, config)
    cfg, problems = _load(config)
    out = {"ok": False, "skill": SKILL, "problems": [], "summary": summary_of(cfg),
           "config": config, "created": created}
    if not problems:
        s, problems = validate(cfg)
        s.config = config
    if problems:
        out["problems"] = problems
        out["message"] = problems_message(problems, config, created)
        emit(with_added(out, added))
        return 2

    log_path, log = open_log(workdir, "check")
    log_note(log, "# シリアルポート：%s（速さ %d）\n" % (s.port, s.baud))
    log_note(log, "# ---- ここから下は、機器から届いた文字そのまま ----\n")
    out["log"] = log_path
    fd = old = None
    message = ""
    try:
        say("シリアルポート %s を開きます（速さ %d）。" % (s.port, s.baud))
        fd, old = open_port(s.port, s.baud)
        con = Console(fd, s.port, log)
        state = wake(con, s)
        if state == "login":
            login(con, s)
            out_ok = logout(con, s)
            message = (
                "シリアルポート %s で機器につながり、%s でログインできました。" % (s.port, s.login_user)
                + ("ログアウトしました。" if out_ok else "exit を送りましたが、ログインの求めに戻ったかは確かめられませんでした。")
            )
            out["summary"]["console"] = "ログインできた"
        else:
            message = "シリアルポート %s で機器につながり、シェルのプロンプトを確かめました（ログインは不要でした）。" % s.port
            out["summary"]["console"] = "シェルのプロンプトが出た"
        out["ok"] = True
    except Failure as f:
        out["problems"].append(f.message)
        message = f.message
        out.update(f.extra)
    except Disconnected:
        message = (
            "シリアルポート %s とのやりとりが、途中で切れました。"
            "USB ケーブルが抜けていないかを確かめてください。" % s.port
        )
        out["problems"].append(message)
    except Exception as e:
        log_note(log, "\n# 思わぬエラー：\n" + traceback.format_exc())
        message = "思わぬエラーで止まりました（%s: %s）。くわしくはログを見てください。" % (type(e).__name__, e)
        out["problems"].append(message)
    finally:
        close_port(fd, old)
        log_note(log, "\n# ---- ここまで ----\n# 結果：%s\n" % message)
        try:
            log.close()
        except Exception:
            pass
    out["message"] = message
    say("ログ：%s" % log_path)
    emit(with_added(out, added))
    return 0 if out["ok"] else 2


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except ArgError as e:
        emit({"ok": False, "skill": SKILL, "message": "引数が正しくありません。%s" % e,
              "problems": [str(e)], "usage": USAGE})
        return 2
    if args.help:
        emit({"ok": True, "skill": SKILL, "usage": USAGE})
        return 0
    try:
        if args.sub == "config-path":
            return cmd_config_path(args)
        if args.sub == "check":
            return cmd_check(args)
        return cmd_run(args)
    except Failure as f:
        out = {"ok": False, "skill": SKILL, "message": f.message, "problems": [f.message]}
        out.update(f.extra)
        emit(out)
        # check では、問題があれば 2 を返す決まり
        return 2 if f.kind == "settings" or args.sub == "check" else 1
    except Exception as e:
        say(traceback.format_exc())
        emit({"ok": False, "skill": SKILL,
              "message": "思わぬエラーで止まりました（%s: %s）。" % (type(e).__name__, e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
