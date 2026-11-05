#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC にあるファイルを、FTP で機器に送る。

使い方:
  deploy.py [run | config-path | check] [--workdir フォルダ] [--config ファイル]
  （Windows では py -3 deploy.py …、それ以外では python3 deploy.py …）

  run          設定ファイルのとおりに、ファイルを機器に送る（省略したときもこれ）
  config-path  設定ファイルの場所を出す。なければ、ひな形から作る
  check        設定を確かめ、機器に FTP でログインできるかを試す。ファイルは作らない
               （設定ファイルがなければ、作らずに、ないと答える。記録のファイルも作らない）

設定と記録は、作業フォルダの「.<このスキルのフォルダの名前>」フォルダに置く。
  config.yaml  設定ファイル（config-path と run は、なければ、ひな形 config-template.yaml から作る）
  logs/        記録のファイル（run のとき）
  .gitignore   中身は * の1行（設定や記録を git に入れないため。config-path と run が作る）

標準出力には、結果の JSON を1つだけ出す。途中のようすは標準エラーに出す。
どちらも Python の既定の文字コードで書く（書けない文字は \\ の形にする）。
Python 3.8 以上の標準ライブラリだけで動く。Windows でも Linux でも動く。
"""

import argparse
import datetime
import errno
import ftplib
import json
import os
import posixpath
import re
import shutil
import socket
import sys
import time
import traceback
import zlib

# このスキルのフォルダ（scripts/ の親）と、その名前。
# フォルダの名前を変えて写したときは、変えた後の名前になる。
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_NAME = os.path.basename(SKILL_DIR)
# 結果の JSON の skill、記録のファイルの名前の頭、途中のようすの頭に使う名前。
SKILL = SKILL_NAME
# 設定と記録を置くフォルダの名前（作業フォルダの中に作る）。
DATA_DIR_NAME = "." + SKILL_NAME
CONFIG_FILE_NAME = "config.yaml"
LOGS_DIR_NAME = "logs"
TEMPLATE_PATH = os.path.join(SKILL_DIR, "config-template.yaml")

REQUIRED_KEYS = ("host", "files", "remote_dir")
CHECKSUM_KINDS = ("none", "crc32", "sum32")
MASK = "＊＊＊"
CHUNK = 64 * 1024
GITIGNORE_TEXT = "*\n"  # 設定（パスワード）や記録が、誤って git に入らないようにする

# 文字列として扱う項目。数字だけの値（例：file_mode: 0755）も、書かれた文字のまま使う。
STRING_KEYS = (
    "host", "user", "password", "password_env", "remote_dir",
    "file_mode", "checksum", "checksum_suffix",
)

DEFAULTS = {
    "port": 21,
    "user": "root",
    "password": "",
    "password_env": "",
    "passive": True,
    "file_mode": "",
    "checksum": "none",
    "checksum_suffix": ".sum",
    "timeout_seconds": 30,
}

USAGE_JA = (
    "使い方：deploy.py [run | config-path | check] "
    "[--workdir フォルダ] [--config 設定ファイル]"
)

# checksum_suffix に使えない文字（Windows か Linux で、ファイルの名前に使えないもの）
BAD_NAME_CHARS = '/\\:*?"<>|'


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

def console_code_page():
    """Windows で、コンソールの出力の文字コード（cp932 など）。コンソールがなければ None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except Exception:
        return None
    if not cp:
        return None
    name = "cp%d" % cp
    try:
        import codecs
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def prepare_streams():
    """標準出力と標準エラーを、読む側の文字コードに合わせ、書けない文字を \\ の形にする。

    Windows で、出力先がコンソールでない（パイプなど）ときは、コンソールの文字コードで書く。
    PowerShell は、この文字コードで読む。Python 3.15 からの既定（UTF-8 モード）のままだと、
    日本語の Windows では文字化けする。
    """
    encoding = console_code_page()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if encoding and not stream.isatty():
                reconfigure(encoding=encoding, errors="backslashreplace")
            else:
                reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass


def json_for(stream, obj):
    """obj の JSON の文。stream の文字コードで書けない文字は、JSON の \\uXXXX にする
    （JSON として読めるままにする）。"""
    text = json.dumps(obj, ensure_ascii=False)
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        pass
    except LookupError:
        return json.dumps(obj)
    out = []
    for ch in text:
        try:
            ch.encode(encoding)
        except UnicodeEncodeError:
            ch = json.dumps(ch)[1:-1]
        out.append(ch)
    return "".join(out)


def emit(obj):
    """結果の JSON を標準出力に1つだけ出す。"""
    sys.stdout.write(json_for(sys.stdout, obj) + "\n")
    sys.stdout.flush()


def progress(text):
    """途中のようすを標準エラーに出す。"""
    try:
        sys.stderr.write("[%s] %s\n" % (SKILL, text))
        sys.stderr.flush()
    except Exception:
        pass


class Log(object):
    """記録のファイル。パスワードは書かない。"""

    def __init__(self, path):
        self.path = path
        self._fh = None
        if path:
            try:
                self._fh = open(path, "a", encoding="utf-8")
            except OSError:
                self.path = None
                self._fh = None

    def write(self, text):
        if self._fh is None:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in str(text).splitlines() or [""]:
            self._fh.write("%s %s\n" % (stamp, line))
        self._fh.flush()

    def say(self, text):
        self.write(text)
        progress(text)

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _config_dir_of(path):
    """path か、その上にあるデータのフォルダ（DATA_DIR_NAME）の場所。なければ None。"""
    p = os.path.abspath(path)
    while True:
        if os.path.basename(p) == DATA_DIR_NAME:
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def ensure_gitignore(path):
    """path か、その上にあるデータのフォルダに .gitignore がなければ作る
    （中身は * の1行）。すでにあれば、そのままにする。作れなくても止めない。"""
    top = _config_dir_of(path)
    if top is None or not os.path.isdir(top):
        return
    target = os.path.join(top, ".gitignore")
    if os.path.lexists(target):
        return
    # Windows では、O_BINARY を付けないと、改行が \r\n になる
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(target, flags, 0o644)
    except OSError:
        return
    try:
        os.write(fd, GITIGNORE_TEXT.encode("utf-8"))
    except OSError:
        pass
    finally:
        os.close(fd)


def make_dirs(path):
    """フォルダを作る。データのフォルダに .gitignore がなければ、それも作る。"""
    os.makedirs(path, exist_ok=True)
    ensure_gitignore(path)


def open_log(workdir):
    """<作業フォルダ>/<データのフォルダ>/logs/<スキルの名前>-YYYYmmdd-HHMMSS.log を作る。"""
    logs_dir = os.path.join(workdir, DATA_DIR_NAME, LOGS_DIR_NAME)
    try:
        make_dirs(logs_dir)
    except OSError:
        return Log(None)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(logs_dir, "%s-%s" % (SKILL, stamp))
    path = base + ".log"
    n = 2
    while os.path.exists(path):
        path = "%s-%d.log" % (base, n)
        n += 1
    return Log(path)


# ---------------------------------------------------------------------------
# 設定ファイル（YAML の一部の書き方だけを読む）
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """設定ファイルが読めないときのエラー。メッセージは日本語。"""


_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*))?$")
_ITEM_LINE = re.compile(r"^[ \t]*-(?:[ \t]+(.*))?$")
_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(
    r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][-+]?[0-9]+)?$")


def _quoted_end(text, start):
    """text[start] の引用符が閉じる位置を返す。閉じていなければ -1。"""
    quote = text[start]
    i = start + 1
    while i < len(text):
        c = text[i]
        if quote == '"' and c == "\\":
            i += 2
            continue
        if c == quote:
            if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    return -1


def _strip_comment(text):
    """値の後ろの「 # コメント」を取り除く。"""
    t = text.strip()
    if not t or t.startswith("#"):
        return ""
    if t[0] in "\"'":
        end = _quoted_end(t, 0)
        if end < 0:
            return t
        rest = t[end + 1:].strip()
        if rest == "" or rest.startswith("#"):
            return t[:end + 1]
        return t
    if t[0] == "[":
        i = 1
        while i < len(t):
            c = t[i]
            if c in "\"'":
                end = _quoted_end(t, i)
                if end < 0:
                    return t
                i = end + 1
                continue
            if c == "]":
                rest = t[i + 1:].strip()
                if rest == "" or rest.startswith("#"):
                    return t[:i + 1]
                return t
            i += 1
        return t
    m = re.search(r"[ \t]#", t)
    if m:
        return t[:m.start()].rstrip()
    return t


def _parse_scalar(text, lineno):
    t = text.strip()
    if t == "":
        return None
    c = t[0]
    if c == '"':
        if _quoted_end(t, 0) != len(t) - 1:
            raise ConfigError(
                "%d 行目：\" で始まる値が、\" で閉じていません：%s" % (lineno, t))
        try:
            return json.loads(t)
        except ValueError:
            return t[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if c == "'":
        if _quoted_end(t, 0) != len(t) - 1:
            raise ConfigError(
                "%d 行目：' で始まる値が、' で閉じていません：%s" % (lineno, t))
        return t[1:-1].replace("''", "'")
    if c == "[":
        return _parse_flow_list(t, lineno)
    if c in "{&*!|>%@`":
        raise ConfigError(
            "%d 行目：この書き方には対応していません：%s" % (lineno, t))
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if _INT_RE.match(t):
        return int(t)
    if _FLOAT_RE.match(t):
        return float(t)
    return t


def _split_flow_items(inner):
    items = []
    cur = ""
    i = 0
    while i < len(inner):
        c = inner[i]
        if c in "\"'":
            end = _quoted_end(inner, i)
            if end < 0:
                cur += inner[i:]
                break
            cur += inner[i:end + 1]
            i = end + 1
            continue
        if c == ",":
            items.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    items.append(cur)
    return items


def _parse_flow_list(text, lineno):
    t = text.strip()
    if not t.endswith("]"):
        raise ConfigError(
            "%d 行目：[ で始まる値が、] で閉じていません：%s" % (lineno, t))
    inner = t[1:-1].strip()
    if inner == "":
        return []
    parts = _split_flow_items(inner)
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    result = []
    for part in parts:
        p = part.strip()
        if p == "":
            raise ConfigError("%d 行目：リストに空の項目があります：%s" % (lineno, t))
        if p[0] in "[{":
            raise ConfigError(
                "%d 行目：リストの入れ子には対応していません：%s" % (lineno, t))
        result.append(_parse_scalar(p, lineno))
    return result


def read_config_text(text):
    """設定ファイルの文字を読み、(値の辞書, 書かれたままの文字の辞書) を返す。"""
    values = {}
    raws = {}
    list_key = None
    if text.startswith("\ufeff"):
        text = text[1:]
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.rstrip()
        stripped = s.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("---", "..."):
            continue
        item = _ITEM_LINE.match(s)
        if item:
            if list_key is None:
                raise ConfigError(
                    "%d 行目：「- 」で始まる行の前に、項目の名前がありません。" % lineno)
            if values[list_key] is None:
                values[list_key] = []
            item_text = _strip_comment(item.group(1) or "")
            value = _parse_scalar(item_text, lineno)
            if isinstance(value, list):
                raise ConfigError(
                    "%d 行目：リストの入れ子には対応していません：%s" % (lineno, stripped))
            values[list_key].append(value)
            continue
        if s[0] in " \t":
            raise ConfigError(
                "%d 行目：字下げした行は読めません（入れ子の書き方には対応していません）：%s"
                % (lineno, stripped))
        m = _KEY_LINE.match(s)
        if not m:
            raise ConfigError(
                "%d 行目が読めません：%s（「名前: 値」の形で書いてください）"
                % (lineno, stripped))
        key = m.group(1)
        if key in values:
            raise ConfigError("%d 行目：%s が2回書かれています。" % (lineno, key))
        rest = _strip_comment(m.group(2) or "")
        if rest == "":
            values[key] = None
            raws[key] = ""
            list_key = key
        else:
            values[key] = _parse_scalar(rest, lineno)
            raws[key] = rest
            list_key = None
    return values, raws


def read_config_file(path):
    # メッセージに path を書かない（problems_message が設定ファイルの場所を足す。
    # config-path の結果には config がある）
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        raise ConfigError("設定ファイルが UTF-8 で読めません。UTF-8 で保存し直してください。")
    except OSError as e:
        raise ConfigError("設定ファイルが開けません（%s）。" % _os_reason(e))
    return read_config_text(text)


def ensure_config(path):
    """設定ファイルがなければ、ひな形から作る。作ったら True を返す。"""
    if os.path.isfile(path):
        return False
    if os.path.exists(path):
        raise ConfigError("設定ファイルの場所 %s は、ファイルではありません。" % path)
    if not os.path.isfile(TEMPLATE_PATH):
        raise ConfigError(
            "設定のひな形 %s が見つかりません。スキルのファイルがそろっているかを確かめてください。"
            % TEMPLATE_PATH)
    try:
        make_dirs(os.path.dirname(path))
        shutil.copyfile(TEMPLATE_PATH, path)
    except OSError as e:
        raise ConfigError("設定ファイル %s を作れません（%s）。" % (path, _os_reason(e)))
    return True


def template_blocks(text):
    """ひな形の項目ごとに (名前, 行の並び) を返す。
    行の並びは、項目のすぐ上のコメントと、項目の行（「- 値」の行が続けば、それも）。"""
    lines = text.splitlines()
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0][1:]
    blocks = []
    i = 0
    while i < len(lines):
        m = _KEY_LINE.match(lines[i].rstrip())
        if not m:
            i += 1
            continue
        start = i
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        end = i + 1
        while end < len(lines) and _ITEM_LINE.match(lines[end]):
            end += 1
        blocks.append((m.group(1), lines[start:end]))
        i = end
    return blocks


def add_missing_keys(path):
    """設定ファイルに、ひな形にある項目がなければ、説明のコメントと一緒にファイルの最後に足す。
    すでにある所は1バイトも変えない。足した項目の名前の並びを返す。
    ファイルが読めないときや、書き方の誤りがあるときは、何も足さない。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        have, _raws = read_config_text(data.decode("utf-8"))
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
            blocks = template_blocks(fh.read())
    except (OSError, UnicodeDecodeError, ConfigError):
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
        with open(path, "ab") as fh:
            fh.write("".join(parts).encode("utf-8"))
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
    データのフォルダに .gitignore がなければ作る。(作ったか, 足した項目) を返す。"""
    ensure_gitignore(os.path.join(workdir, DATA_DIR_NAME))
    created = ensure_config(config)
    ensure_gitignore(config)
    added = [] if created else add_missing_keys(config)
    return created, added


# ---------------------------------------------------------------------------
# 設定の確かめ
# ---------------------------------------------------------------------------

def _is_empty(value):
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    return False


def missing_keys(values):
    return [k for k in REQUIRED_KEYS if _is_empty(values.get(k))]


def _as_text(key, values, raws):
    """文字列の項目の値を、文字列にして返す。"""
    value = values.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        raw = raws.get(key, "")
        if raw and raw[0] not in "\"'[":
            return raw
        return str(value)
    return value


def normalize_mode(value):
    """'755'・'0755'・'0o755' を '755' の形にする。だめなら None。"""
    t = str(value).strip().lower()
    if t.startswith("0o"):
        t = t[2:]
    if not re.match(r"^[0-7]{1,4}$", t):
        return None
    t = t.lstrip("0") or "0"
    return t.zfill(3)


def validate(values, raws, workdir, environ=None):
    """設定を確かめる。(settings, problems, notes, connect_ready) を返す。

    problems は日本語の文のリスト。connect_ready は、機器につなぐのに
    要る項目（host・port・user・password・timeout_seconds）に問題がないとき True。
    """
    env = os.environ if environ is None else environ
    problems = []
    notes = []
    conn_bad = False
    s = {}

    def get(key):
        if key in STRING_KEYS:
            v = _as_text(key, values, raws)
        else:
            v = values.get(key)
        if v is None and key in DEFAULTS:
            return DEFAULTS[key]
        return v

    # host
    host = get("host")
    if _is_empty(host):
        problems.append("host（機器の IP アドレスかホスト名）が空です。")
        conn_bad = True
        host = ""
    elif not isinstance(host, str):
        problems.append(
            "host は IP アドレスかホスト名を1つだけ書いてください（今の値：%s）。" % (host,))
        conn_bad = True
        host = ""
    elif re.search(r"\s", host.strip()):
        problems.append("host に空白が入っています（今の値：%s）。" % host)
        conn_bad = True
    else:
        host = host.strip()
    s["host"] = host

    # port
    port = get("port")
    if isinstance(port, str) and port.strip().isdigit():
        port = int(port.strip())
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        problems.append("port は 1〜65535 の数にしてください（今の値：%s）。" % (port,))
        conn_bad = True
    s["port"] = port

    # user
    user = get("user")
    if user is None:
        user = ""
    if not isinstance(user, str):
        problems.append("user は文字にしてください（今の値：%s）。" % (user,))
        conn_bad = True
        user = ""
    s["user"] = user.strip()

    # password / password_env
    password = get("password")
    if password is None:
        password = ""
    if not isinstance(password, str):
        password = str(password)
    env_name = get("password_env")
    env_name = "" if env_name is None else str(env_name).strip()
    source = "file"
    if env_name:
        if env_name in env:
            password = env[env_name]
            source = "env"
        elif password == "":
            problems.append(
                "password_env の環境変数 %s がありません。"
                "環境変数を設定するか、password に書いてください。" % env_name)
            conn_bad = True
        else:
            notes.append(
                "環境変数 %s がないので、password に書かれた値を使います。" % env_name)
    s["password"] = password
    s["password_env"] = env_name
    s["password_source"] = source

    # passive
    passive = get("passive")
    if isinstance(passive, str) and passive.strip().lower() in ("true", "false"):
        passive = passive.strip().lower() == "true"
    if not isinstance(passive, bool):
        problems.append(
            "passive は true か false にしてください（今の値：%s）。" % (passive,))
        passive = True
    s["passive"] = passive

    # timeout_seconds
    timeout = get("timeout_seconds")
    if isinstance(timeout, str):
        try:
            timeout = float(timeout.strip())
        except ValueError:
            pass
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or timeout <= 0:
        problems.append(
            "timeout_seconds は 0 より大きい数にしてください（今の値：%s）。" % (timeout,))
        conn_bad = True
        timeout = DEFAULTS["timeout_seconds"]
    s["timeout_seconds"] = timeout

    # files
    files = values.get("files")
    if isinstance(files, str) and files.strip():
        files = [files]
    local_files = []
    if _is_empty(files):
        problems.append("files（機器に送るファイル）が空です。")
        files = []
    elif not isinstance(files, list):
        problems.append(
            "files は [\"my_app\"] のようなリストにしてください（今の値：%s）。" % (files,))
        files = []
    else:
        seen = {}
        for item in files:
            if not isinstance(item, str) or item.strip() == "":
                problems.append("files に、ファイルの名前でない値があります（%s）。" % (item,))
                continue
            if re.search(r"[\x00-\x1f]", item):
                # "C:\temp\new" のように " で囲むと、\t と \n がタブと改行として読まれる
                problems.append(
                    "files の %s に、タブや改行が入っています（\" で囲むと、\\t はタブ、"
                    "\\n は改行として読まれます）。パスは / で区切るか、' で囲んでください。"
                    % visible(item))
                continue
            rel = item.strip()
            path = rel if os.path.isabs(rel) else os.path.join(workdir, rel)
            path = os.path.normpath(path)
            if not os.path.exists(path):
                problems.append(
                    "送るファイル %s が見つかりません（%s）。"
                    "ファイルができているかを確かめてください。" % (rel, path))
            elif not os.path.isfile(path):
                problems.append("送るもの %s はファイルではありません（%s）。" % (rel, path))
            name = os.path.basename(path)
            if name in seen:
                problems.append(
                    "files に、同じ名前のファイルが2つあります（%s と %s）。"
                    "送り先で上書きされます。" % (seen[name], rel))
            seen[name] = rel
            local_files.append({"name": rel, "path": path})
    s["files"] = [f["name"] for f in local_files]
    s["local_files"] = local_files

    # remote_dir
    remote_dir = get("remote_dir")
    if _is_empty(remote_dir):
        problems.append("remote_dir（機器の送り先のフォルダ）が空です。")
        remote_dir = ""
    elif not isinstance(remote_dir, str):
        problems.append("remote_dir はフォルダのパスにしてください（今の値：%s）。" % (remote_dir,))
        remote_dir = ""
    elif "\\" in remote_dir or re.search(r"[\x00-\x1f]", remote_dir):
        # Windows の書き方（\ で区切る）。機器では、名前に \ を含むフォルダができてしまう。
        # " で囲むと \t や \n はタブや改行になるので、それも同じに扱う。
        problems.append(
            "remote_dir は機器のパスなので、\\ ではなく / で区切ってください（今の値：%s）。"
            % visible(remote_dir).strip())
        remote_dir = ""
    else:
        remote_dir = remote_dir.strip()
        if len(remote_dir) > 1:
            remote_dir = remote_dir.rstrip("/") or "/"
    s["remote_dir"] = remote_dir

    # file_mode
    mode = get("file_mode")
    if _is_empty(mode):
        mode = None
    else:
        normalized = normalize_mode(mode)
        if normalized is None:
            problems.append(
                "file_mode は 755 のような 8進数の数字にしてください（今の値：%s）。" % (mode,))
        mode = normalized
    s["file_mode"] = mode

    # checksum
    kind = get("checksum")
    kind = "none" if _is_empty(kind) else str(kind).strip().lower()
    if kind not in CHECKSUM_KINDS:
        problems.append(
            "checksum は none / crc32 / sum32 のどれかにしてください（今の値：%s）。" % kind)
        kind = "none"
    s["checksum"] = kind

    suffix = get("checksum_suffix")
    suffix = "" if suffix is None else str(suffix)
    if kind != "none":
        if suffix.strip() == "":
            problems.append(
                "checksum_suffix（サム値のファイルの名前の後ろに付ける文字）が空です。")
        elif any(c in BAD_NAME_CHARS or ord(c) < 32 for c in suffix):
            problems.append(
                "checksum_suffix に、ファイルの名前に使えない文字（%s）は使えません（今の値：%s）。"
                % (" ".join(BAD_NAME_CHARS), visible(suffix)))
    s["checksum_suffix"] = suffix

    return s, problems, notes, not conn_bad


_CONTROL_NAMES = {"\t": "\\t", "\n": "\\n", "\r": "\\r", "\b": "\\b", "\f": "\\f"}


def visible(text):
    """タブや改行などの見えない文字を、\\t・\\n などの形にする（メッセージに出すため）。"""
    return re.sub(r"[\x00-\x1f]",
                  lambda m: _CONTROL_NAMES.get(m.group(0), "\\x%02x" % ord(m.group(0))), text)


def summary_of(settings):
    """人に見せる設定のまとめ。パスワードは伏せる。"""
    return {
        "host": settings.get("host"),
        "port": settings.get("port"),
        "user": settings.get("user"),
        "password": MASK if settings.get("password") else "",
        "password_env": settings.get("password_env"),
        "passive": settings.get("passive"),
        "files": settings.get("files"),
        "remote_dir": settings.get("remote_dir"),
        "file_mode": settings.get("file_mode") or "",
        "checksum": settings.get("checksum"),
        "checksum_suffix": settings.get("checksum_suffix"),
        "timeout_seconds": settings.get("timeout_seconds"),
    }


# ---------------------------------------------------------------------------
# サム値
# ---------------------------------------------------------------------------

def checksum_update(kind, value, chunk):
    """サム値を chunk の分だけ進める。kind は crc32 か sum32。"""
    if kind == "crc32":
        return zlib.crc32(chunk, value) & 0xFFFFFFFF
    if kind == "sum32":
        return (value + sum(bytearray(chunk))) & 0xFFFFFFFF
    raise ValueError("unknown checksum kind: %r" % (kind,))


def checksum_of_bytes(kind, data):
    return checksum_update(kind, 0, data)


def checksum_of_file(kind, path):
    value = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            value = checksum_update(kind, value, chunk)
    return value


def checksum_text(value):
    """サム値のファイルの中身：小文字の16進数8桁と改行。"""
    return "%08x\n" % (value & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# FTP
# ---------------------------------------------------------------------------

class DeployError(Exception):
    """送れなかったときのエラー。message は利用者に見せる日本語。"""

    def __init__(self, message, detail=""):
        Exception.__init__(self, message)
        self.message = message
        self.detail = detail


def _os_reason(e):
    return getattr(e, "strerror", None) or str(e) or e.__class__.__name__


def _reply_code(e):
    text = str(e.args[0]) if getattr(e, "args", None) else str(e)
    return text[:3]


def _reply_text(e):
    return str(e.args[0]).strip() if getattr(e, "args", None) else str(e)


def _is_timeout(e):
    return isinstance(e, (socket.timeout, TimeoutError))


# 機器までの経路がないときのエラーの番号。Windows では WSA の番号になる。
_UNREACH = set(
    getattr(errno, n) for n in (
        "EHOSTUNREACH", "ENETUNREACH", "EHOSTDOWN", "ENETDOWN",
        "WSAEHOSTUNREACH", "WSAENETUNREACH", "WSAEHOSTDOWN", "WSAENETDOWN")
    if hasattr(errno, n))


def connect_error_message(host, port, timeout, e):
    where = "機器 %s の %s 番ポートに FTP でつながりません" % (host, port)
    if isinstance(e, socket.gaierror):
        return ("機器のホスト名 %s が見つかりません。"
                "設定の host（機器の IP アドレスかホスト名）を確かめてください。" % host)
    if isinstance(e, ConnectionRefusedError):
        return (where + "（接続が拒否されました）。"
                "機器の電源と、FTP サーバーが動いているかを確かめてください。")
    if _is_timeout(e):
        return (where + "（%s 秒待っても応答がありません）。"
                "機器の電源とケーブル、IP アドレスが正しいかを確かめてください。"
                % _fmt_seconds(timeout))
    if isinstance(e, OSError) and e.errno in _UNREACH:
        return (where + "（機器までの経路がありません）。"
                "ケーブルと IP アドレス、PC のネットワークの設定を確かめてください。")
    if isinstance(e, (EOFError, ConnectionResetError, ConnectionAbortedError)):
        return ("機器 %s の %s 番ポートにつながりましたが、すぐに切られました。"
                "FTP サーバーが動いているかと、設定の port を確かめてください。" % (host, port))
    if isinstance(e, ftplib.Error):
        return (where + "（FTP サーバーの答え：%s）。"
                "機器の FTP サーバーが動いているかと、ほかの PC からつないでいないかを"
                "確かめてください。" % _reply_text(e))
    return (where + "（%s）。機器の電源とケーブル、IP アドレスを確かめてください。"
            % _os_reason(e))


def session_error_message(what, timeout, e, data_transfer=False):
    """ログインの後のやりとりで起きたエラーを、日本語の文にする。"""
    if isinstance(e, ftplib.Error):
        code = _reply_code(e)
        reply = _reply_text(e)
        if code in ("550", "553"):
            return (what + "（機器が断りました：%s）。"
                    "設定の user に書き込む権限があるかと、"
                    "remote_dir（機器の送り先のフォルダ）が正しいかを確かめてください。" % reply)
        if code in ("452", "552"):
            return (what + "（機器の空き容量が足りません：%s）。"
                    "機器の空き容量を確かめてください。" % reply)
        if code == "530":
            return (what + "（ログインが切れました：%s）。"
                    "設定の user と password を確かめてください。" % reply)
        if code == "421":
            return (what + "（機器の FTP サーバーが接続を切りました：%s）。"
                    "機器の電源と、FTP サーバーが動いているかを確かめてください。" % reply)
        return (what + "（機器の FTP サーバーの答え：%s）。"
                "くわしくは記録のファイルを見てください。" % reply)
    if _is_timeout(e):
        hint = ""
        if data_transfer:
            hint = "設定の passive を変えると直ることがあります。"
        return (what + "（%s 秒待っても機器の応答がありません）。"
                "機器の電源とケーブルを確かめてください。%s"
                % (_fmt_seconds(timeout), hint))
    if isinstance(e, (EOFError, ConnectionResetError, ConnectionAbortedError,
                      BrokenPipeError)):
        return (what + "（途中で機器との接続が切れました）。"
                "機器の電源とケーブルを確かめてください。")
    if isinstance(e, ConnectionRefusedError) and data_transfer:
        return (what + "（ファイルを送るための接続が拒否されました）。"
                "設定の passive を変えると直ることがあります。")
    return (what + "（%s）。機器の電源とケーブルを確かめてください。"
            % _os_reason(e))


def _fmt_seconds(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


FTP_ERRORS = ftplib.all_errors + (EOFError,)


def login_hint(settings):
    """ログインできないときに、確かめることの文。パスワードが空なら、そう言う。"""
    config = settings.get("config_path") or "設定ファイル"
    if not settings.get("user"):
        return ("設定の user が空なので、anonymous でログインしようとしました。"
                "機器のユーザー名とパスワードを、%s の user と password に書いてください。"
                % config)
    if settings.get("password") == "":
        env_name = settings.get("password_env")
        if env_name:
            return ("環境変数 %s の中身が空です。その環境変数にパスワードを入れるか、"
                    "%s の password に書いてください。" % (env_name, config))
        return ("設定の password も password_env も空です。"
                "機器のパスワードを、%s の password に書くか、"
                "password_env にパスワードを入れた環境変数の名前を書いてください。" % config)
    return "設定の user と password を確かめてください。"


def ftp_login(settings, log):
    """機器につないでログインする。うまくいけば FTP の接続を返す。"""
    host = settings["host"]
    port = settings["port"]
    timeout = settings["timeout_seconds"]
    ftp = ftplib.FTP()
    # Python 3.8 の既定は latin-1。3.9 以降と同じく、ファイルやフォルダの名前を UTF-8 で送る。
    ftp.encoding = "utf-8"
    log.say("機器 %s の %s 番ポートに FTP でつなぎます（最長 %s 秒待ちます）。"
            % (host, port, _fmt_seconds(timeout)))
    try:
        welcome = ftp.connect(host, port, timeout=timeout)
        log.write("機器の答え：%s" % welcome)
    except FTP_ERRORS as e:
        _close(ftp)
        raise DeployError(connect_error_message(host, port, timeout, e), repr(e))
    user = settings["user"]
    log.say("ユーザー %s でログインします。" % (user or "anonymous"))
    try:
        reply = ftp.login(user, settings["password"])
        log.write("機器の答え：%s" % reply)
    except ftplib.error_perm as e:
        _close(ftp)
        log.write("ログインできませんでした：%s" % _reply_text(e))
        if _reply_code(e) == "530":
            raise DeployError(
                "機器 %s に FTP でログインできません（ユーザー名かパスワードが違います）。"
                % host + login_hint(settings), _reply_text(e))
        raise DeployError(
            "機器 %s に FTP でログインできません（FTP サーバーの答え：%s）。"
            % (host, _reply_text(e)) + login_hint(settings),
            _reply_text(e))
    except FTP_ERRORS as e:
        _close(ftp)
        log.write("ログインの途中で失敗しました：%r" % (e,))
        raise DeployError(
            session_error_message("機器 %s に FTP でログインできません" % host, timeout, e),
            repr(e))
    ftp.set_pasv(bool(settings["passive"]))
    return ftp


def _close(ftp):
    try:
        ftp.close()
    except Exception:
        pass


def _quit(ftp):
    try:
        ftp.quit()
    except Exception:
        pass
    _close(ftp)


def ensure_remote_dir(ftp, remote_dir, timeout, log):
    """送り先のフォルダに入る。なければ親から順に作る。(絶対パス, 作ったフォルダ) を返す。"""
    try:
        home = ftp.pwd()
    except FTP_ERRORS as e:
        raise DeployError(session_error_message(
            "機器の今のフォルダが分かりません", timeout, e), repr(e))
    if remote_dir.startswith("/"):
        target = remote_dir
    else:
        target = posixpath.join(home or "/", remote_dir)
    target = posixpath.normpath(target)
    if target.startswith("//"):
        target = "/" + target.lstrip("/")
    created = []
    cur = "/"
    parts = [p for p in target.split("/") if p]
    for part in parts:
        cur = posixpath.join(cur, part)
        try:
            ftp.cwd(cur)
            continue
        except ftplib.error_perm:
            pass
        except FTP_ERRORS as e:
            raise DeployError(session_error_message(
                "機器のフォルダ %s に入れません" % cur, timeout, e), repr(e))
        try:
            ftp.mkd(cur)
            created.append(cur)
            log.say("機器にフォルダ %s を作りました。" % cur)
        except ftplib.error_perm as e:
            # ほかの人が同時に作った場合などは、そのまま進む。
            try:
                ftp.cwd(cur)
                continue
            except FTP_ERRORS:
                pass
            raise DeployError(session_error_message(
                "機器の送り先のフォルダ %s を作れません" % cur, timeout, e), repr(e))
        except FTP_ERRORS as e:
            raise DeployError(session_error_message(
                "機器の送り先のフォルダ %s を作れません" % cur, timeout, e), repr(e))
    try:
        ftp.cwd(target)
    except FTP_ERRORS as e:
        raise DeployError(session_error_message(
            "機器の送り先のフォルダ %s に入れません" % target, timeout, e), repr(e))
    return target, created


def _store(ftp, local_path, remote_path, timeout, log, what):
    try:
        fh = open(local_path, "rb")
    except OSError as e:
        raise DeployError(
            "PC のファイル %s が読めません（%s）。" % (local_path, _os_reason(e)), repr(e))
    try:
        reply = ftp.storbinary("STOR " + remote_path, fh, blocksize=CHUNK)
        log.write("機器の答え：%s" % reply)
    except FTP_ERRORS as e:
        log.write("送れませんでした：%r" % (e,))
        raise DeployError(session_error_message(what, timeout, e, True), repr(e))
    finally:
        fh.close()


def _make_checksum_dir(log):
    base = log.path[:-len(".log")] if log.path else None
    if base is None:
        return None
    path = base + "-checksums"
    try:
        make_dirs(path)
    except OSError:
        return None
    return path


def deploy(settings, log):
    """ファイルを機器に送る。結果の辞書を返す。失敗したら DeployError。"""
    host = settings["host"]
    timeout = settings["timeout_seconds"]
    kind = settings["checksum"]
    mode = settings["file_mode"]
    suffix = settings["checksum_suffix"]
    results = []
    notes = []
    state = {"results": results, "notes": notes, "remote_dir": settings["remote_dir"],
             "created_dirs": []}

    ftp = ftp_login(settings, log)
    try:
        target, created = ensure_remote_dir(ftp, settings["remote_dir"], timeout, log)
        state["remote_dir"] = target
        state["created_dirs"] = created

        sum_dir = None
        if kind != "none":
            sum_dir = _make_checksum_dir(log)
            if not sum_dir:
                raise DeployError(
                    "サム値のファイルを PC に作れません。"
                    "作業フォルダに書き込めるかを確かめてください。")

        for item in settings["local_files"]:
            local = item["path"]
            name = os.path.basename(local)
            remote = posixpath.join(target, name)
            size = os.path.getsize(local)
            entry = {
                "local": local,
                "remote": remote,
                "size": size,
                "remote_size": None,
                "mode": None,
                "checksum": None,
                "checksum_file": None,
                "checksum_local_file": None,
            }
            results.append(entry)

            log.say("%s（%d バイト）を機器の %s に送ります。" % (item["name"], size, remote))
            _store(ftp, local, remote, timeout, log,
                   "機器の %s にファイルを送れません" % remote)

            if kind != "none":
                value = checksum_of_file(kind, local)
                text = checksum_text(value)
                sum_name = name + suffix
                sum_remote = posixpath.join(target, sum_name)
                sum_local = os.path.join(sum_dir, sum_name)
                try:
                    with open(sum_local, "w", encoding="ascii", newline="\n") as fh:
                        fh.write(text)
                except OSError as e:
                    raise DeployError(
                        "サム値のファイル %s を PC に作れません（%s）。"
                        % (sum_local, _os_reason(e)), repr(e))
                log.say("サム値のファイル（%s：%s）を、機器の %s に送ります。"
                        % (kind, text.strip(), sum_remote))
                _store(ftp, sum_local, sum_remote, timeout, log,
                       "機器の %s にサム値のファイルを送れません" % sum_remote)
                entry["checksum"] = text.strip()
                entry["checksum_file"] = sum_remote
                entry["checksum_local_file"] = sum_local

            if mode:
                log.say("機器の %s の権限を %s にします。" % (remote, mode))
                try:
                    reply = ftp.sendcmd("SITE CHMOD %s %s" % (mode, remote))
                    log.write("機器の答え：%s" % reply)
                except ftplib.Error as e:
                    log.write("権限を変えられませんでした：%s" % _reply_text(e))
                    hint = ""
                    if any(int(d) & 1 for d in mode[-3:]):
                        hint = "このままでは、機器でプログラムを実行できません。"
                    raise DeployError(
                        "機器の %s の権限を %s に変えられません（FTP サーバーの答え：%s）。"
                        "%sFTP サーバーが権限の変更（SITE CHMOD）を受け付けるかを確かめてください。"
                        % (remote, mode, _reply_text(e), hint), _reply_text(e))
                except FTP_ERRORS as e:
                    raise DeployError(session_error_message(
                        "機器の %s の権限を変えられません" % remote, timeout, e), repr(e))
                entry["mode"] = mode

            try:
                ftp.voidcmd("TYPE I")
                remote_size = ftp.size(remote)
            except ftplib.error_perm as e:
                code = _reply_code(e)
                if code in ("500", "502", "504"):
                    remote_size = None
                else:
                    raise DeployError(
                        "送った %s の大きさを、機器から読み取れません（FTP サーバーの答え：%s）。"
                        "機器の %s にファイルがあるかを確かめてください。"
                        % (remote, _reply_text(e), target), _reply_text(e))
            except FTP_ERRORS as e:
                raise DeployError(session_error_message(
                    "送った %s の大きさを確かめられません" % remote, timeout, e), repr(e))
            if remote_size is None:
                note = ("機器の FTP サーバーが大きさの問い合わせ（SIZE）に対応していないので、"
                        "送ったファイルの大きさは確かめていません。")
                if note not in notes:
                    notes.append(note)
                log.say(note)
            else:
                entry["remote_size"] = remote_size
                if remote_size != size:
                    raise DeployError(
                        "機器の %s の大きさ（%d バイト）が、PC のファイル（%d バイト）と合いません。"
                        "送る途中で切れたおそれがあります。機器の空き容量とケーブルを確かめてください。"
                        % (remote, remote_size, size))
                log.say("機器の %s の大きさが、PC のファイルと同じ（%d バイト）ことを確かめました。"
                        % (remote, size))
    except DeployError as e:
        e.state = state
        _quit(ftp)
        raise
    _quit(ftp)
    return state


# file_mode が空のとき、送った後の権限について伝える文。
# 送ったファイルの権限は機器の FTP サーバーが決め、ふつうは実行できない権限（644 など）になる。
NO_MODE_SENTENCE = ("権限は付けていません（file_mode が空です）。"
                    "FTP で送ったファイルは、ふつうは実行できない権限になります。"
                    "機器でプログラムを実行するなら、file_mode に 755 などを書いてください。")


def success_message(settings, state):
    names = [os.path.basename(r["local"]) for r in state["results"]]
    if len(names) > 3:
        shown = "、".join(names[:3]) + " ほか %d 個" % (len(names) - 3)
    else:
        shown = "、".join(names)
    parts = ["機器 %s の %s に、%s を送りました。" % (settings["host"], state["remote_dir"], shown)]
    if settings["file_mode"]:
        parts.append("権限は %s にしました。" % settings["file_mode"])
    else:
        parts.append(NO_MODE_SENTENCE)
    if settings["checksum"] != "none":
        parts.append("サム値（%s）のファイルも一緒に送りました。" % settings["checksum"])
    if state["notes"]:
        parts.append("大きさは確かめていません（FTP サーバーが対応していません）。")
    else:
        parts.append("機器のファイルの大きさが PC と同じことも確かめました。")
    return "".join(parts)


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------

def resolve_paths(args):
    workdir = os.path.abspath(args.workdir) if args.workdir else os.getcwd()
    if args.config:
        config = os.path.abspath(args.config)
    else:
        config = os.path.join(workdir, DATA_DIR_NAME, CONFIG_FILE_NAME)
    return workdir, config


def cmd_config_path(workdir, config):
    created, added = False, []
    try:
        created, added = open_config(workdir, config)
        values, _raws = read_config_file(config)
    except ConfigError as e:
        emit(with_added({"config": config, "created": created, "missing": [], "problems": [str(e)]},
                        added))
        return 0
    emit(with_added({"config": config, "created": created, "missing": missing_keys(values)}, added))
    return 0


def as_sentence(text):
    """文の終わりに「。」がなければ足す（次の文とつながって読めなくならないように）。"""
    text = text.rstrip()
    if text and not text.endswith(("。", "！", "？")):
        text += "。"
    return text


def problems_message(problems, config, created):
    """設定の問題を、人に伝える1つの文にする。"""
    head = ""
    if created:
        head = "設定ファイルがなかったので、ひな形から作りました。"
    if len(problems) == 1:
        return "%s%s設定ファイルは %s です。" % (head, as_sentence(problems[0]), config)
    return ("%s設定に直すところが %d 件あります。設定ファイル %s を直してください。"
            "各項目の意味は、項目のすぐ上のコメントに書いてあります。"
            % (head, len(problems), config))


def _load(workdir, config):
    """(created, added, values, raws, problems) を返す。"""
    created, added = False, []
    try:
        created, added = open_config(workdir, config)
        values, raws = read_config_file(config)
    except ConfigError as e:
        return created, added, None, None, [str(e)]
    return created, added, values, raws, []


def _load_existing(config):
    """check 用。ファイルを作らずに、今ある設定ファイルを読む。(added, values, raws, problems) を返す。
    足りない項目は、ほかのコマンドと同じく、設定ファイルに足す。"""
    if not os.path.isfile(config):
        return [], None, None, ["設定ファイルの場所 %s は、ファイルではありません。" % config]
    added = add_missing_keys(config)
    try:
        values, raws = read_config_file(config)
    except ConfigError as e:
        return added, None, None, [str(e)]
    return added, values, raws, []


# file_mode が空のとき、check で伝える注意（問題ではない）。
NO_MODE_NOTE = ("file_mode が空なので、送った後に権限を付けません。"
                "FTP で送ったファイルは、ふつうは実行できない権限になります。"
                "機器でプログラムを実行するなら、file_mode に 755 などを書いてください。")


def cmd_check(workdir, config):
    """設定を確かめる。作業フォルダには、ファイルを作らない（設定ファイル・.gitignore・記録のファイル）。"""
    log = Log(None)  # 記録のファイルは作らない。途中のようすは標準エラーにだけ出す
    if not os.path.exists(config):
        message = "設定ファイルがありません（%s）。config-path で、ひな形から作れます。" % config
        progress(message)
        emit({"ok": False, "skill": SKILL, "problems": [message], "notes": [], "summary": {},
              "message": message, "config": config, "created": False, "added": []})
        return 2
    added, values, raws, problems = _load_existing(config)
    summary = {}
    notes = []
    login_error = None
    if values is not None:
        settings, problems, notes, connect_ready = validate(values, raws, workdir)
        settings["config_path"] = config
        summary = summary_of(settings)
        if _is_empty(_as_text("file_mode", values, raws)):
            notes.append(NO_MODE_NOTE)
        for n in notes:
            log.say(n)
        if connect_ready:
            try:
                ftp = ftp_login(settings, log)
                try:
                    home = ftp.pwd()
                    summary["login"] = "できた"
                    summary["remote_home"] = home
                    log.say("ログインできました。機器の今のフォルダは %s です。" % home)
                except FTP_ERRORS as e:
                    raise DeployError(session_error_message(
                        "機器の今のフォルダが分かりません", settings["timeout_seconds"], e),
                        repr(e))
                finally:
                    _quit(ftp)
            except DeployError as e:
                log.write("詳しく：%s" % e.detail)
                login_error = e.message
                summary["login"] = "できない"
        else:
            summary["login"] = "試していない（設定の問題を先に直してください）"
    if problems:
        message = problems_message(problems, config, False)
        if login_error:
            message = login_error + "ほかに、" + message
    elif login_error:
        message = login_error
    else:
        message = ("設定に問題はありません。機器 %s に FTP でログインできました。" % summary.get("host")
                   + "".join(notes))
    if login_error:
        problems.append(login_error)
    ok = not problems
    log.say(message)
    emit(with_added({
        "ok": ok,
        "skill": SKILL,
        "problems": problems,
        "notes": notes,
        "summary": summary,
        "message": message,
        "config": config,
        "created": False,
    }, added))
    return 0 if ok else 2


def cmd_run(workdir, config):
    log = open_log(workdir)
    log.write("%s run を始めます。設定ファイル：%s" % (SKILL, config))
    created, added, values, raws, problems = _load(workdir, config)
    settings = None
    if values is not None:
        settings, problems, notes, _ready = validate(values, raws, workdir)
        settings["config_path"] = config
        for n in notes:
            log.say(n)
    if problems:
        for p in problems:
            log.write("問題：%s" % p)
        message = problems_message(problems, config, created)
        log.say(message)
        log.close()
        emit(with_added({
            "ok": False,
            "skill": SKILL,
            "message": message,
            "problems": problems,
            "config": config,
            "created": created,
            "log": log.path,
        }, added))
        return 2

    base = {
        "skill": SKILL,
        "host": settings["host"],
        "port": settings["port"],
        "checksum_kind": settings["checksum"],
        "config": config,
        "log": log.path,
    }
    started = time.time()
    try:
        state = deploy(settings, log)
    except DeployError as e:
        state = getattr(e, "state", None) or {
            "results": [], "notes": [], "remote_dir": settings["remote_dir"],
            "created_dirs": []}
        log.write("詳しく：%s" % e.detail)
        log.say(e.message)
        log.close()
        out = {"ok": False, "message": e.message}
        out.update(base)
        out.update({
            "remote_dir": state["remote_dir"],
            "created_dirs": state["created_dirs"],
            "files": state["results"],
            "notes": state["notes"],
        })
        emit(with_added(out, added))
        return 1
    message = success_message(settings, state)
    log.say(message)
    log.write("かかった時間：%.1f 秒" % (time.time() - started))
    log.close()
    out = {"ok": True, "message": message}
    out.update(base)
    out.update({
        "remote_dir": state["remote_dir"],
        "created_dirs": state["created_dirs"],
        "files": state["results"],
        "notes": state["notes"],
    })
    emit(with_added(out, added))
    return 0


class _Parser(argparse.ArgumentParser):
    """書き方がちがうときも、JSON で答える。"""

    def error(self, message):
        emit({
            "ok": False,
            "skill": SKILL,
            "message": "コマンドの書き方がちがいます。" + USAGE_JA,
            "problems": ["コマンドの書き方がちがいます。" + USAGE_JA],
            "detail": message,
        })
        sys.exit(2)


def main(argv=None):
    prepare_streams()
    parser = _Parser(prog="deploy.py", add_help=False)
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "config-path", "check"])
    parser.add_argument("--workdir", help="作業フォルダ（省略すると今のフォルダ）")
    parser.add_argument(
        "--config",
        help="設定ファイル（省略すると作業フォルダの %s/%s）" % (DATA_DIR_NAME, CONFIG_FILE_NAME))
    args = parser.parse_args(argv)
    workdir, config = resolve_paths(args)
    if not os.path.isdir(workdir):
        message = "作業フォルダ %s がありません。--workdir の値を確かめてください。" % workdir
        emit({"ok": False, "skill": SKILL, "message": message, "problems": [message],
              "config": config})
        return 2
    try:
        if args.command == "config-path":
            return cmd_config_path(workdir, config)
        if args.command == "check":
            return cmd_check(workdir, config)
        return cmd_run(workdir, config)
    except Exception:
        detail = traceback.format_exc()
        if args.command == "check":
            # check は、記録のファイルも作らない。くわしいことは標準エラーに出す
            progress(detail.rstrip())
            message = "思わぬエラーで止まりました（%s）。" % detail.strip().splitlines()[-1]
            emit({"ok": False, "skill": SKILL, "message": message, "config": config})
            return 1
        log = open_log(workdir)
        log.write("思わぬエラー：\n" + detail)
        log.close()
        message = ("思わぬエラーで止まりました。くわしくは記録のファイル %s を見てください。"
                   % (log.path or "（記録を作れませんでした）"))
        progress(message)
        emit({"ok": False, "skill": SKILL, "message": message, "log": log.path,
              "config": config})
        return 1


if __name__ == "__main__":
    sys.exit(main())
