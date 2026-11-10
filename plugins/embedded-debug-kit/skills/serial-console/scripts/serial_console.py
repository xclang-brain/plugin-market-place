#!/usr/bin/env python3
"""シリアルで機器のシェルにつなぎ、コマンドを順に動かして、出力と終了コードを返す。

  serial_console.py run [--command コマンド]... [--workdir 作業フォルダ]
  serial_console.py run --stdin [--workdir 作業フォルダ]   （標準入力の1行を1つのコマンドにする）
  serial_console.py check|init [--workdir 作業フォルダ]

設定は <作業フォルダ>/.<スキルの名前>/config.yaml、ログは同じフォルダの logs/ に置く。
標準出力には結果の JSON を1つだけ、ASCII で書く。
終了コード：0 成功、1 作業の失敗、2 設定や引数の問題。
Python 3.8 以上の標準ライブラリと、同梱の pyserial 3.5（vendor/serial、BSD ライセンス）で動く。
"""

import argparse
import codecs
import datetime
import errno
import json
import math
import os
import re
import shutil
import sys
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 同梱の pyserial を、ほかに入っているものより先に読む。スキルのフォルダに __pycache__ を作らない。
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(SCRIPT_DIR, "vendor"))
import serial  # noqa: E402
import serial.tools.list_ports  # noqa: E402

# スキルの名前は、scripts/ の親のフォルダの名前。名前を変えて写せば、その名前になる。
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
SKILL_NAME = os.path.basename(SKILL_DIR)
TEMPLATE = os.path.join(SKILL_DIR, "assets", "config-template.yaml")
KEYS = ("port", "baud", "login_user", "login_password", "login_password_env", "prompt",
        "commands", "command_timeout_seconds")
USAGE = "使い方：serial_console.py run [--command コマンド]... | run --stdin | check | init [--workdir 作業フォルダ]"
DEFAULT_PROMPT = r"[#$] $"
MASK = "***"
IS_WINDOWS = os.name == "nt"

# 待つ秒数。機器はふつう1秒以内に答えるが、遅い機器も考えて長めにする。
WAKE_WAIT = 3.0       # Enter を送ってから、プロンプトかログインの求めを待つ（2回まで送る）
LOGIN_WAIT = 10.0     # ユーザー名やパスワードを送ってから待つ。パスワードが違うと、答えが数秒遅れる
INTERRUPT_WAIT = 5.0  # Ctrl-C を送ってから、プロンプトが戻るのを待つ
RC_WAIT = 5.0         # 終了コードを調べるコマンドの答えを待つ
QUIET = 0.2           # プロンプトに合ったあと、続きが来ないことを確かめる（出力の途中の「# 」と見分ける）
NUDGE_QUIET = 1.0     # プロンプトが機器のメッセージで隠れたとみて Enter を送るまでの、静かな秒数
NUDGES = 2            # 1回の待ちで、隠れたプロンプトのために Enter を送る回数の上限
POLL = 0.05           # 1回の読み取りで文字を待つ秒数
READ_MAX = 4096       # 1回に読むバイト数の上限
WRITE_WAIT = 5.0      # 書き込みを待つ秒数
OUTPUT_LINES = 60     # JSON に入れる、コマンドごとの出力の行数（全部はログにある）
OUTPUT_CHARS = 8000   # 同じく、文字数

RC_COMMAND = ' echo "__rc=$?"'          # 前の空白は、bash の履歴に残さないため
RC_RE = re.compile(r"__rc=([0-9]+)")    # エコーされた行は「__rc=$?」なので合わない
LOGIN_RE = re.compile(r"login:\s*$")
PASSWORD_RE = re.compile(r"[Pp]assword:\s*$")
INCORRECT_RE = re.compile(r"(?i)login incorrect|authentication fail")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"           # 色、カーソルの移動など
                     r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # 端末のタイトルなど
                     r"|\x1b[ -/]*[0-~]")                  # そのほかの短いもの
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Windows の Git Bash は、/ で始まる引数（と「名前=/…」の後ろ）を「C:/Program Files/Git/…」に書き換える
MSYS_PATH_RE = re.compile(r"(?:^|=)[A-Za-z]:[/\\]")

SECRETS = []  # 結果とログで伏せる文字列（パスワード）


class Stop(Exception):
    """利用者に伝えて終える。code は終了コード。"""

    def __init__(self, message, code=1):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


def mask(value):
    """文字列と、辞書・リストの中の文字列から、パスワードを伏せる。"""
    if isinstance(value, str):
        for secret in sorted(set(SECRETS), key=len, reverse=True):  # 長いものから。短いものが一部なら、残りが出てしまう
            value = value.replace(secret, MASK)
        return value
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


class Log(object):
    """ログのファイル（UTF-8）。開いていなければ何も書かない（check は何も作らない）。"""

    def __init__(self):
        self.path = None
        self.fh = None

    def open(self, path):
        self.fh = open(path, "x", encoding="utf-8")
        self.path = path

    def __call__(self, text, prefix=""):
        if self.fh is None:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in mask(str(text)).splitlines() or [""]:
            self.fh.write("%s %s%s\n" % (stamp, prefix, line))
        self.fh.flush()

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None


LOG = Log()


def clean(text):
    """色などの制御文字と \\r を除き、バックスペース（\\b）で消された文字を消す。"""
    text = ANSI_RE.sub("", text).replace("\r", "")
    if "\b" in text:
        out = []
        for ch in text:
            if ch != "\b":
                out.append(ch)
            elif out and out[-1] != "\n":
                out.pop()
        text = "".join(out)
    return CTRL_RE.sub("", text)


def echoed(seen, command):
    """seen（空白を除いた行）が、送ったコマンドのエコーか。
    端末の幅で折り返すと、1文字が2回出ることがある（「a\\ra」から \\r を除くと「aa」）。"""
    if command in seen:
        return True
    j = extra = 0
    for ch in seen:
        if j < len(command) and ch == command[j]:
            j += 1
        elif j and ch == command[j - 1] and extra <= len(command) // 10:
            extra += 1
        else:
            return False
    return j == len(command)


def drop_echo(lines, command):
    """出力の頭にある、送ったコマンドのエコーを除く。長いコマンドは、折り返して数行になる。"""
    target = re.sub(r"\s+", "", command)
    seen = ""
    for i, line in enumerate(lines[:5]):
        seen += re.sub(r"\s+", "", line)
        if echoed(seen, target):
            return lines[i + 1:]
    return lines


# --- 設定 -------------------------------------------------------------------

def data_dir(workdir):
    return os.path.join(workdir, "." + SKILL_NAME)


def prepare_data_dir(workdir):
    """設定とログのフォルダを作る。中に何かを書く前に、中身を Git に入れない .gitignore を置く。"""
    path = data_dir(workdir)
    os.makedirs(path, exist_ok=True)
    ignore = os.path.join(path, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("*\n")
    return path


# --- 設定ファイル（YAML）を読む。設定を持つスキルは、どれも同じ中身を持つ ------------------------

YAML_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(?:\s+(.*))?$")
YAML_ITEM = re.compile(r"^\s*-(?:\s+(.*))?$")
# 囲まないと、YAML として別の意味になる値（記号で始まる、「: 」を含む、「:」で終わる）
YAML_NEEDS_QUOTES = re.compile(r"""^[\[\]{},&*!|>%@`'"#]|^[-?:](\s|$)|:\s|:$""")


def yaml_scalar(text):
    """YAML の1つの値を、文字列にする。数や true も、文字のまま返す。読めない書き方は ValueError。"""
    text = (text or "").strip()
    if text.startswith("#"):
        return ""
    quoted = re.match(r"^'((?:[^']|'')*)'(?:\s+#.*)?$", text)
    if quoted:
        return quoted.group(1).replace("''", "'")
    quoted = re.match(r'^"((?:[^"\\]|\\["\\])*)"(?:\s+#.*)?$', text)
    if quoted:
        return re.sub(r"\\(.)", r"\1", quoted.group(1))
    text = re.sub(r"\s+#.*$", "", text)  # 空白の後の # からは、コメント
    if YAML_NEEDS_QUOTES.search(text):
        raise ValueError("値を '…' で囲んでください（値の中の ' は '' と書きます）")
    return text


def read_yaml(lines):
    """設定ファイルの行を読み、項目ごとの [名前, 値, 最初の行, 最後の行] を返す（行は 0 から数える）。
    読むのは、YAML のうち「項目: 値」と、項目の下の「- 値」のリストだけ。値は、文字列か、文字列のリスト。
    読めない行は ValueError。何行目かを伝え、行の中身は出さない（パスワードの行かもしれない）。"""
    entries = []
    for n, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, item = YAML_KEY.match(line), YAML_ITEM.match(line)
        try:
            if key:
                if any(e[0] == key.group(1) for e in entries):
                    raise ValueError("%s が2回書かれています" % key.group(1))
                entries.append([key.group(1), yaml_scalar(key.group(2)), n, n])
            elif item and entries and (entries[-1][1] == "" or isinstance(entries[-1][1], list)):
                value = yaml_scalar(item.group(1))
                entries[-1][1] = (entries[-1][1] or []) + ([value] if value else [])
                entries[-1][3] = n
            else:
                raise ValueError("「項目: 値」か、項目の次の行からの「- 値」の形で書いてください")
        except ValueError as e:
            raise ValueError("%d 行目：%s" % (n + 1, e))
    return entries


# --- 設定ファイル（YAML）を読む：ここまで ---------------------------------------------------------


def read_config(path):
    """設定の項目を辞書で返す。リストの値は、1行に1つずつ並べた文字列にする。読めなければ Stop（終了コード 2）。"""
    if not os.path.isfile(path):
        raise Stop("設定ファイル %s がありません。init で作ってから、値を書いてください。" % path, 2)
    try:
        with open(path, encoding="utf-8-sig") as fh:  # BOM があっても読める
            entries = read_yaml(fh.read().split("\n"))
    except UnicodeDecodeError:
        raise Stop("設定ファイル %s を UTF-8 で読めません。UTF-8 で保存し直してください。" % path, 2)
    except ValueError as e:
        raise Stop("設定ファイル %s の %s。" % (path, e), 2)
    raw = {key: "\n".join(value) if isinstance(value, list) else value for key, value, _, _ in entries}
    if raw.get("login_password"):
        SECRETS.append(raw["login_password"])
    return raw


def settings_from(raw, environ, commands_arg):
    """値を確かめる。(settings, problems) を返す。problems は日本語の文のリスト。"""
    problems = []
    for key in raw:
        if key not in KEYS:
            problems.append("項目 %s は使えません。名前を確かめてください。" % key)
        elif key != "commands" and "\n" in raw[key].strip():
            problems.append("%s には値を1つだけ書いてください。" % key)

    def get(key, default=""):
        return raw.get(key, "").strip() or default

    s = {"port": get("port"), "login_user": raw.get("login_user", "root").strip(),
         "password": raw.get("login_password", ""), "password_env": get("login_password_env")}
    if not s["port"]:
        problems.append("port（シリアルポート）が空です。")

    baud = get("baud", "115200")
    s["baud"] = int(baud) if re.match(r"^[0-9]{1,8}$", baud) else 0
    if s["baud"] <= 0:
        problems.append("baud は 115200 のような正の整数にしてください（今の値：%s）。" % baud)

    if s["password_env"] in environ:
        s["password"] = environ[s["password_env"]]
        if s["password"]:
            SECRETS.append(s["password"])
    # 環境変数がないことは、機器がパスワードを求めたときに伝える（もうログインしていれば不要）
    s["env_missing"] = bool(s["password_env"]) and s["password_env"] not in environ and not s["password"]

    prompt = get("prompt", DEFAULT_PROMPT)
    try:
        s["prompt"] = re.compile(prompt)
        if s["prompt"].search(""):
            problems.append("prompt が、空の行にも合います。プロンプトの終わりの文字を入れてください（今の値：%s）。"
                            % prompt)
    except re.error as e:
        problems.append("prompt が正しい正規表現ではありません（今の値：%s。%s）。" % (prompt, e))

    timeout = get("command_timeout_seconds", "30")
    try:
        s["timeout"] = float(timeout)
    except ValueError:
        s["timeout"] = 0.0
    if not (s["timeout"] > 0 and math.isfinite(s["timeout"])):
        problems.append("command_timeout_seconds は 0 より大きい数にしてください（今の値：%s）。" % timeout)

    if commands_arg is not None:
        s["commands"] = [c.strip() for c in commands_arg if c.strip()]
    else:
        s["commands"] = [line.strip() for line in raw.get("commands", "").splitlines() if line.strip()]
    return s, problems


def logged_settings(raw):
    shown = dict(raw)
    if shown.get("login_password"):
        shown["login_password"] = MASK
    return json.dumps(shown, ensure_ascii=False)


# --- シリアルポート -----------------------------------------------------------

def list_ports():
    """この PC のシリアルポートの名前。COM3 が COM10 より先に来るように並べる。"""
    try:
        names = set(p.device for p in serial.tools.list_ports.comports())
    except Exception:
        return []
    return sorted(names, key=lambda n: [int(t) if t.isdigit() else t for t in re.split(r"([0-9]+)", n)])


def open_error(path, e, result):
    """開けなかった理由を、確かめることを添えた文にする。
    Linux などでは e.errno に番号が入る。Windows では番号がなく、文に FileNotFoundError(…) などが入る。"""
    text = str(e)
    num = getattr(e, "errno", None)
    if "://" in path:
        return "%s につながりません（%s）。アドレスとポート番号、相手が動いているかを確かめてください。" % (path, text)
    if num == errno.ENOENT or "FileNotFoundError" in text:
        result["ports"] = ports = list_ports()
        found = ("この PC で見つかったシリアルポート：%s。" % "、".join(ports)) if ports \
            else "この PC では、シリアルポートが1つも見つかりません。"
        return "シリアルポート %s が見つかりません。USB ケーブルと port を確かめてください。%s" % (path, found)
    if num in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK) or (IS_WINDOWS and "PermissionError" in text):
        # Windows では、ほかのプログラムが開いているポートは「アクセスが拒否されました」になる
        apps = "Tera Term や PuTTY など" if IS_WINDOWS else "minicom や screen など"
        return "シリアルポート %s は、ほかのプログラムが使っています。端末ソフト（%s）を閉じてから、" \
               "やり直してください。前に動かしたこのスクリプトが、まだ動いていないかも確かめてください。" % (path, apps)
    if num in (errno.EACCES, errno.EPERM):
        return "シリアルポート %s を開く権限がありません。今のユーザーを dialout グループに入れて" \
               "（sudo usermod -aG dialout $USER）、ログインし直してください。" % path
    return "シリアルポート %s を開けません（%s）。USB ケーブルと port、baud を確かめてください。" % (path, text)


class Console(object):
    """シリアルポートとのやりとり。届いた文字は、行ごとにログにも書く。"""

    def __init__(self, port, name):
        self.port = port
        self.name = name
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.got_any = False
        self.logging = True
        self.unlogged = ""
        self.prompt_line = None  # 最後に合ったプロンプトの行（色などを除いたもの）
        self.mark()

    def mark(self):
        """ここから届く文字を、新しいまとまりとして集める。"""
        if self.unlogged:
            LOG(clean(self.unlogged), "| ")
            self.unlogged = ""
        self.chunks = []
        self.tail = ""     # 最後の改行より後の文字
        self.lines = 0     # 改行まで届いた行の数
        self.back = False  # プロンプトで始まる行が、1行目（エコー）より後に出たか

    def text(self):
        return "".join(self.chunks)

    def _feed(self, data):
        text = self.decoder.decode(data)
        if not text:
            return
        self.got_any = True
        self.chunks.append(text)
        if self.logging:
            self.unlogged += text
            while "\n" in self.unlogged or len(self.unlogged) > READ_MAX:
                line, sep, rest = self.unlogged.partition("\n")
                if not sep:
                    # 改行のない長い行は分けて書く。先に伏せ、パスワードの書きかけかもしれない終わりは次に回す
                    line = mask(line)
                    keep = min(max([len(x) for x in SECRETS] + [1]) - 1, READ_MAX // 2)
                    cut = max(len(line) - keep, 0)
                    line, rest = line[:cut], line[cut:]
                LOG(clean(line), "| ")
                self.unlogged = rest
        parts = (self.tail + text).split("\n")
        # 「# 」のような短いプロンプトは、出力の行（「# Subtest」など）と見分けられないので見ない
        if self.prompt_line and len(self.prompt_line.strip()) >= 3:
            for i, line in enumerate(parts[:-1]):
                if self.lines + i > 0 and clean(line).startswith(self.prompt_line):
                    self.back = True
        self.lines += len(parts) - 1
        self.tail = parts[-1][-READ_MAX:]

    def read(self, timeout):
        """最長 timeout 秒、文字を待って読む。読めたら True。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                data = self.port.read(READ_MAX)
            except (serial.SerialException, OSError) as e:
                raise Stop("シリアルポート %s との接続が途中で切れました（%s）。USB ケーブルと機器の電源を確かめてください。"
                           % (self.name, e))
            if data:
                self._feed(data)
                return True
            if time.monotonic() >= deadline:
                return False

    def send(self, text):
        try:
            self.port.write(text.encode("utf-8"))
        except serial.SerialTimeoutException:
            raise Stop("シリアルポート %s に書き込めません（%g 秒待っても送れません）。"
                       "ケーブルと機器の電源を確かめてください。" % (self.name, WRITE_WAIT))
        except (serial.SerialException, OSError) as e:
            raise Stop("シリアルポート %s との接続が途中で切れました（%s）。USB ケーブルと機器の電源を確かめてください。"
                       % (self.name, e))

    def match(self, patterns, need_newline):
        if need_newline and not self.lines:
            return None  # まだ送ったコマンドのエコーの途中
        line = clean(self.tail)
        for name, rx in patterns:
            if rx.search(line):
                if name == "prompt":
                    self.prompt_line = line
                return name
        return None

    def wait_for(self, patterns, timeout, need_newline=False, nudge=False):
        """最後の行が patterns（(名前, 正規表現) の並び）のどれかに合い、続きが来なくなるまで待つ。
        合った名前を返す。時間切れなら None。
        nudge なら、プロンプトの後ろに機器のメッセージが出てプロンプトが隠れたときに、Enter を送る。"""
        deadline = time.monotonic() + timeout
        quiet_since = time.monotonic()
        nudges = 0
        while True:
            hit = self.match(patterns, need_newline)
            got = self.read(QUIET if hit else min(max(deadline - time.monotonic(), 0.0), 0.2))
            if hit and not got:
                return hit
            now = time.monotonic()
            if got:
                quiet_since = now
            if now >= deadline:
                return None
            if nudge and self.back and nudges < NUDGES and now - quiet_since >= NUDGE_QUIET:
                LOG("プロンプトが機器のメッセージで隠れたようなので、Enter を送ります。")
                nudges += 1
                quiet_since = now
                self.send("\r")

    def last_lines(self, n=3):
        lines = [line for line in clean(self.text()).split("\n") if line.strip()]
        return " / ".join(lines[-n:])[-300:]

    def close(self):
        self.mark()
        try:
            self.port.close()
        except Exception:
            pass


def open_console(s, result):
    path = s["port"]
    LOG("シリアルポート %s を %d bps で開きます。" % (path, s["baud"]))
    try:
        port = serial.serial_for_url(
            path, baudrate=s["baud"], bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=POLL, write_timeout=WRITE_WAIT,
            exclusive=True)  # Linux などで、ほかのプログラムと同時に使わない（Windows はいつも1つだけ）
    except (serial.SerialException, OSError, ValueError) as e:
        LOG("失敗：%r" % (e,))
        raise Stop(open_error(path, e, result))
    return Console(port, path)


# --- 機器とのやりとり ---------------------------------------------------------

def no_prompt(con, s, after_login):
    if not con.got_any:
        msg = "シリアルポート %s から何も届きません。機器の電源とケーブル、baud（今の値：%d）を確かめてください。" \
              % (s["port"], s["baud"])
    elif after_login:
        msg = "ログインのあと、シェルのプロンプトが出ません。prompt（今の値：%s）が機器のプロンプトに" \
              "合っているかを確かめてください。" % s["prompt"].pattern
    else:
        msg = "シリアルポート %s から文字は届きますが、シェルのプロンプトもログインの求めも出ません。" \
              "baud（今の値：%d）と prompt（今の値：%s）が機器に合っているかと、機器で前のプログラムが" \
              "動いたままでないかを確かめてください。" % (s["port"], s["baud"], s["prompt"].pattern)
    seen = con.last_lines()
    return Stop(msg + ("最後に届いた行：「%s」。" % seen if seen else ""))


def login_failed(s):
    env = s["password_env"]
    if env in os.environ:
        check = ("環境変数 %s が空です。" % env) if not s["password"] \
            else "login_user と、環境変数 %s のパスワードを確かめてください。" % env
    elif not s["password"]:
        check = "login_password が空です。パスワードを書くか、login_password_env を使ってください。"
    else:
        check = "login_user と login_password を確かめてください。"
    return Stop("ユーザー %s でログインできません（ユーザー名かパスワードが違います）。%s" % (s["login_user"], check))


def reach_shell(con, s):
    """Enter を送り、シェルのプロンプトが出るまで進める。ログインを求められたらログインする。
    ログインしたら True を返す。"""
    patterns = (("login", LOGIN_RE), ("password", PASSWORD_RE), ("prompt", s["prompt"]))
    user_sent = password_sent = False
    wakes = cancels = 0
    LOG("Ctrl-U と Enter を送り、プロンプトを待ちます。")
    con.send("\x15\r")  # Ctrl-U で打ちかけの文字を消してから Enter を送る（打ちかけのコマンドを動かさない）
    while True:
        hit = con.wait_for(patterns, LOGIN_WAIT if user_sent else WAKE_WAIT)
        if hit == "prompt":
            return user_sent
        if hit == "login" and not user_sent:
            if not s["login_user"]:
                raise Stop("機器がログインを求めていますが、login_user が空です。ユーザー名を書いてください。", 2)
            con.mark()
            LOG("ユーザー %s でログインします。" % s["login_user"])
            con.send(s["login_user"] + "\r")
            user_sent = True
        elif hit == "password" and user_sent and not password_sent:
            if s["env_missing"]:
                raise Stop("機器がパスワードを求めていますが、login_password_env の環境変数 %s がありません。"
                           "CLI を起動する前に設定するか、login_password に書いてください。" % s["password_env"], 2)
            con.mark()
            con.send(s["password"] + "\r")
            password_sent = True
        elif hit == "password" and not user_sent and cancels < 2:
            # 前に誰かがパスワードを入力しかけていた。Enter で取り消すと、ログインの求めに戻る
            con.mark()
            LOG("パスワードの入力の途中だったので、Enter で取り消します。")
            cancels += 1
            con.send("\r")
        elif hit or (password_sent and INCORRECT_RE.search(clean(con.text()))):
            raise login_failed(s)
        elif wakes < 1 and (password_sent or not user_sent):
            # 返事がないか、プロンプトが機器のメッセージで隠れた。もう一度だけ Enter を送る
            # （パスワードの求めを待つ間は送らない。空のパスワードを送ることになるため）
            wakes += 1
            con.send("\r")
        else:
            raise no_prompt(con, s, user_sent)


def read_exit_code(con, s):
    """終了コードを調べるコマンドを送り、直前のコマンドの終了コードを返す。読めなければ None。
    このやりとりは、ログに書かない。"""
    con.mark()
    con.logging = False
    try:
        con.send(RC_COMMAND + "\r")
        con.wait_for((("prompt", s["prompt"]),), RC_WAIT, need_newline=True, nudge=True)
        found = RC_RE.findall(clean(con.text()))
    finally:
        con.logging = True
    code = int(found[-1]) if found else None
    LOG("終了コード：%s" % ("読めません" if code is None else code))
    return code


def run_command(con, s, item):
    """コマンドを1つ動かし、item に出力と終了コードを入れる。プロンプトが戻ったら True。"""
    command = item["command"]
    prompt = (("prompt", s["prompt"]),)
    before = con.prompt_line  # 送る前のプロンプト
    back = None
    con.mark()
    LOG("コマンドを送ります：%s" % command)
    try:
        con.send(command + "\r")
        back = con.wait_for(prompt, s["timeout"], need_newline=True, nudge=True)
        if not back:
            item["timed_out"] = True
            LOG("%g 秒たっても終わらないので、Ctrl-C を送ります。" % s["timeout"])
            con.send("\x03")
            back = con.wait_for(prompt, INTERRUPT_WAIT, need_newline=True, nudge=True)
    finally:
        # 途中で切れても、そこまでの出力を返す
        lines = clean(con.text()).split("\n")
        if back:
            last = lines.pop()  # 最後の行はプロンプト
            # 出力が改行で終わらないと、プロンプトと同じ行に出る（printf など）。プロンプトだけを除く
            if before and last.endswith(before) and last[:-len(before)].strip():
                lines.append(last[:-len(before)])
        lines = drop_echo(lines, command)
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        text = "\n".join(lines[-OUTPUT_LINES:])
        item["output"] = text[-OUTPUT_CHARS:]
        item["truncated"] = len(lines) > OUTPUT_LINES or len(text) > OUTPUT_CHARS
    if back and not item["timed_out"]:
        item["exit_code"] = read_exit_code(con, s)
    return bool(back)


# --- サブコマンド -------------------------------------------------------------

def checked_settings(raw, result, commands_arg=None, need_commands=False):
    LOG("設定：%s" % logged_settings(raw))
    s, problems = settings_from(raw, os.environ, commands_arg)
    if need_commands and not s["commands"]:
        problems.append("動かすコマンドがありません。設定の commands に書くか、--command か --stdin で渡してください。")
    if problems:
        result["problems"] = problems
        raise Stop("".join(problems) + "設定ファイル %s を直してください。" % result["config"], 2)
    return s


def open_log(workdir):
    try:
        logs = os.path.join(prepare_data_dir(workdir), "logs")
        os.makedirs(logs, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        for n in range(1, 100):  # 同じ秒に何度も動かしたときは、名前に -2、-3… を付ける
            try:
                LOG.open(os.path.join(logs, "%s%s.log" % (stamp, "-%d" % n if n > 1 else "")))
                return
            except FileExistsError:
                pass
    except OSError as e:
        raise Stop("ログのファイルを作れません（%s）。作業フォルダに書き込めるかを確かめてください。"
                   % (e.strerror or e))


def cmd_init(workdir, result, commands_arg):
    config = result["config"]
    result["created"] = False
    if os.path.exists(config):
        result.update(ok=True, message="設定ファイル %s はもうあるので、そのままにしました。" % config)
        return 0
    if not os.path.isfile(TEMPLATE):
        raise Stop("ひな形 %s がありません。スキルのフォルダがそろっているかを確かめてください。" % TEMPLATE)
    try:
        prepare_data_dir(workdir)
        shutil.copyfile(TEMPLATE, config)
    except OSError as e:
        raise Stop("設定ファイル %s を作れません（%s）。作業フォルダに書き込めるかを確かめてください。"
                   % (config, e.strerror or e))
    result.update(ok=True, created=True,
                  message="設定ファイル %s を作りました。各項目のすぐ上のコメントを読んで、値を書いてください。" % config)
    return 0


def logged_in_text(s, logged_in):
    return "ユーザー %s でログインしました。" % s["login_user"] if logged_in else "ログインは求められませんでした。"


def cmd_check(workdir, result, commands_arg):
    s = checked_settings(read_config(result["config"]), result)
    con = open_console(s, result)
    try:
        logged_in = reach_shell(con, s)
    finally:
        con.close()
    message = "シリアルポート %s で、機器のシェルのプロンプトまで進めました。%s" % (s["port"], logged_in_text(s, logged_in))
    if not s["commands"]:
        message += "設定の commands が空なので、run では --command か --stdin でコマンドを渡してください。"
    result.update(ok=True, message=message)
    return 0


def cmd_run(workdir, result, commands_arg):
    result.update(commands=[], not_run=[])
    raw = read_config(result["config"])
    open_log(workdir)
    result["log"] = LOG.path
    LOG("run を始めます。設定ファイル：%s" % result["config"])
    s = checked_settings(raw, result, commands_arg, need_commands=True)
    commands = s["commands"]
    result["not_run"] = list(commands)
    con = open_console(s, result)
    try:
        logged_in = reach_shell(con, s)
        for i, command in enumerate(commands):
            result["not_run"] = commands[i + 1:]
            item = {"command": command, "output": "", "exit_code": None, "timed_out": False, "truncated": False}
            result["commands"].append(item)
            back = run_command(con, s, item)
            if item["timed_out"]:
                msg = "コマンド「%s」が %g 秒で終わらないので、Ctrl-C で止めました。" % (command, s["timeout"])
                if not back:
                    msg += "Ctrl-C のあとも、シェルのプロンプトが戻りません。機器のようすを確かめてください。"
                if result["not_run"]:
                    msg += "残りの %d 個のコマンドは動かしていません。" % len(result["not_run"])
                raise Stop(msg + "長くかかるコマンドなら、command_timeout_seconds を増やしてください。")
    finally:
        con.close()
    message = "機器のシェルで、%d 個のコマンドを動かしました。%s" % (len(commands), logged_in_text(s, logged_in))
    failed = ["「%s」（%d）" % (c["command"], c["exit_code"]) for c in result["commands"] if c["exit_code"]]
    unknown = ["「%s」" % c["command"] for c in result["commands"] if c["exit_code"] is None]
    if failed:
        message += "終了コードが 0 でないコマンド：%s。" % "、".join(failed)
    if unknown:
        message += "終了コードを読めなかったコマンド：%s。" % "、".join(unknown)
    result.update(ok=True, message=message)
    return 0


def stdin_commands():
    """標準入力（UTF-8）の空でない行を、コマンドのリストにする。"""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None or stream.isatty():  # 端末なら、入力を待ったまま止まってしまう
        return []
    try:
        text = stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Stop("標準入力を UTF-8 で読めません。PowerShell では、先に "
                   "$OutputEncoding = [Text.UTF8Encoding]::new($false) を動かしてください。", 2)
    return [line.strip() for line in text.splitlines() if line.strip()]


def check_command_args(commands):
    for c in commands or []:
        if "\n" in c or "\r" in c:
            raise Stop("--command の値に改行は入れられません。コマンドごとに --command を付けるか、--stdin で渡してください。", 2)
        if IS_WINDOWS and MSYS_PATH_RE.search(c):
            raise Stop("--command の値「%s」が、Windows のパスに書き換わっています（Git Bash は、/ で始まる値などを"
                       "書き換えます）。このコマンドは --stdin で渡してください。" % c, 2)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Stop("コマンドの書き方が違います。" + USAGE, 2)


def main(argv=None):
    parser = Parser(prog="serial_console.py", add_help=False)
    parser.add_argument("subcommand", choices=("run", "check", "init"))
    parser.add_argument("--workdir")
    parser.add_argument("--command", action="append", dest="commands")
    parser.add_argument("--stdin", action="store_true")
    result = {"ok": False, "message": "", "problems": [], "config": None, "log": None}
    try:
        args = parser.parse_args(argv)
        workdir = os.path.abspath(args.workdir or os.getcwd())
        result["config"] = os.path.join(data_dir(workdir), "config.yaml")
        if (args.commands is not None or args.stdin) and args.subcommand != "run":
            raise Stop("--command と --stdin は run でだけ使えます。" + USAGE, 2)
        if args.commands is not None and args.stdin:
            raise Stop("--command と --stdin は、どちらか一方だけを使ってください。" + USAGE, 2)
        check_command_args(args.commands)
        if not os.path.isdir(workdir):
            raise Stop("作業フォルダ %s がありません。--workdir を確かめてください。" % workdir, 2)
        run = {"run": cmd_run, "check": cmd_check, "init": cmd_init}[args.subcommand]
        code = run(workdir, result, stdin_commands() if args.stdin else args.commands)
    except Stop as e:
        result["ok"] = False
        result["message"] = e.message
        if e.code == 2 and not result["problems"]:
            result["problems"] = [e.message]
        code = e.code
    except Exception as e:
        LOG(traceback.format_exc())
        result["ok"] = False
        result["message"] = "思わぬエラーで止まりました（%s: %s）。" % (type(e).__name__, e)
        if LOG.path:
            result["message"] += "ログ %s を見てください。" % LOG.path
        code = 1
    LOG(result["message"])
    LOG.close()
    shown = mask(result)
    shown.update(config=result["config"], log=result["log"])  # 手元のパスは伏せない（パスワードと同じ文字を含むことがある）
    sys.stdout.write(json.dumps(shown, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
