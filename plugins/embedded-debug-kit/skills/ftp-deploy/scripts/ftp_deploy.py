#!/usr/bin/env python3
"""ファイルを FTP で機器に送る。

  ftp_deploy.py run|check|init [--workdir 作業フォルダ]

設定は <作業フォルダ>/.<スキルの名前>/config.yaml、ログは同じフォルダの logs/ に置く。
標準出力には結果の JSON を1つだけ、ASCII で書く。
終了コード：0 成功、1 作業の失敗、2 設定や引数の問題。
Python 3.8 以上の標準ライブラリだけで動く。
"""

import argparse
import datetime
import errno
import ftplib
import json
import math
import os
import re
import shutil
import socket
import ssl
import sys
import traceback

# スキルの名前は、このスクリプトのフォルダの親の名前。名前を変えて写せば、その名前になる。
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_NAME = os.path.basename(SKILL_DIR)
TEMPLATE = os.path.join(SKILL_DIR, "assets", "config-template.yaml")
KEYS = ("host", "port", "user", "password", "password_env", "passive", "tls",
        "files", "remote_dir", "file_mode", "timeout_seconds")
USAGE = "使い方：ftp_deploy.py run|check|init [--workdir 作業フォルダ]"
MASK = "***"
BLOCK_SIZE = 64 * 1024
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
BOOLEANS = {"true": True, "yes": True, "on": True, "1": True, "false": False, "no": False, "off": False, "0": False}
# ftplib.Error、OSError（ssl.SSLError を含む）、EOFError と、機器の答えを読めないときの ValueError
FTP_ERRORS = ftplib.all_errors + (ValueError,)
DROPPED = (EOFError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
# 機器までの経路がないときの errno。Windows では WSA の番号になる。
UNREACHABLE = {getattr(errno, n) for n in (
    "EHOSTUNREACH", "ENETUNREACH", "EHOSTDOWN", "ENETDOWN",
    "WSAEHOSTUNREACH", "WSAENETUNREACH", "WSAEHOSTDOWN", "WSAENETDOWN") if hasattr(errno, n)}

SECRETS = []  # 機器の答えや例外の文から伏せる文字列（パスワード）


class Stop(Exception):
    """利用者に伝えて終える。code は終了コード。"""

    def __init__(self, message, code=1):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


class OldRemoved(Exception):
    """古いファイルを消したあとに、名前を変えられなかった。error は元の例外。"""

    def __init__(self, error):
        Exception.__init__(self, error)
        self.error = error


def mask(text):
    """機器の答えや例外の文から、パスワードを伏せる。
    設定から作るパスや名前は伏せない（root などの短いパスワードで、パスが壊れないように）。"""
    text = str(text)
    for secret in SECRETS:
        text = text.replace(secret, MASK)
    return text


class Log(object):
    """ログのファイル（UTF-8）。開いていなければ何も書かない（check は何も作らない）。"""

    def __init__(self):
        self.path = None
        self.fh = None

    def open(self, path):
        self.fh = open(path, "x", encoding="utf-8")
        self.path = path

    def __call__(self, text):
        if self.fh is None:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in str(text).splitlines() or [""]:
            self.fh.write("%s %s\n" % (stamp, line))
        self.fh.flush()

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None


LOG = Log()


def log_failure(e):
    LOG("失敗：" + mask(repr(e)))


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
    if raw.get("password"):
        SECRETS.append(raw["password"])
    return raw


def settings_from(raw, workdir, environ):
    """値を確かめる。(settings, problems) を返す。problems は日本語の文のリスト。
    PC にまだない送るファイルは、problems ではなく settings["missing"] に入れる。"""
    problems = []
    for key in raw:
        if key not in KEYS:
            problems.append("項目 %s は使えません。名前を確かめてください。" % key)
        elif key != "files" and "\n" in raw[key]:
            problems.append("%s には値を1つだけ書いてください。" % key)

    def get(key, default=""):
        return raw.get(key, "").strip() or default

    s = {"host": get("host"), "user": raw.get("user", "root").strip(),
         "password_env": get("password_env"), "file_mode": get("file_mode") or None}
    if not s["host"]:
        problems.append("host（機器のアドレス）が空です。")
    elif re.search(r"\s", s["host"]):
        problems.append("host に空白が入っています（今の値：%s）。" % s["host"])

    port = get("port", "21")
    s["port"] = int(port) if re.match(r"^[0-9]{1,5}$", port) and 1 <= int(port) <= 65535 else 0
    if not s["port"]:
        problems.append("port は 1〜65535 の整数にしてください（今の値：%s）。" % port)

    s["password"] = raw.get("password", "")
    if s["password_env"] in environ:
        s["password"] = environ[s["password_env"]]
        if s["password"]:
            SECRETS.append(s["password"])
    elif s["password_env"] and not s["password"]:
        problems.append("password_env の環境変数 %s がありません。"
                        "環境変数を設定するか、password に書いてください。" % s["password_env"])

    for key, default in (("passive", "true"), ("tls", "false")):
        value = get(key, default).lower()
        if value not in BOOLEANS:
            problems.append("%s は true か false にしてください（今の値：%s）。" % (key, value))
        s[key] = BOOLEANS.get(value, False)

    timeout = get("timeout_seconds", "30")
    try:
        s["timeout"] = float(timeout)
    except ValueError:
        s["timeout"] = 0.0
    if not (s["timeout"] > 0 and math.isfinite(s["timeout"])):
        problems.append("timeout_seconds は 0 より大きい数にしてください（今の値：%s）。" % timeout)

    if s["file_mode"] and not re.match(r"^[0-7]{3,4}$", s["file_mode"]):
        problems.append("file_mode は 755 のような 8 進数の 3〜4 桁にしてください（今の値：%s）。"
                        % s["file_mode"])

    remote_dir = get("remote_dir")
    s["remote_dir"] = remote_dir
    s["remote_parts"] = [p for p in remote_dir.split("/") if p not in ("", ".")]
    if not remote_dir:
        problems.append("remote_dir（機器の送り先のフォルダ）が空です。")
    elif "\\" in remote_dir:
        problems.append("remote_dir は / で区切ってください（今の値：%s）。" % remote_dir)
    elif ".." in s["remote_parts"]:
        problems.append("remote_dir に .. は使えません（今の値：%s）。" % remote_dir)

    s["files"], s["missing"] = [], []
    entries = [line.strip() for line in raw.get("files", "").splitlines() if line.strip()]
    if not entries:
        problems.append("files（送るファイル）が空です。")
    seen = {}
    for entry in entries:
        path = os.path.normpath(os.path.join(workdir, entry))
        name = os.path.basename(path)
        if not safe_name(name):
            problems.append("%s は、名前に \\ や改行などが入っているので送れません。"
                            "名前を変えてください。" % entry)
        elif name in seen:
            problems.append("files に、名前が同じファイルが2つあります（%s と %s）。" % (seen[name], entry))
        elif os.path.isdir(path):
            problems.append("files の %s はフォルダです。送るファイルを1つずつ書いてください。" % entry)
        else:
            seen[name] = entry
            if os.path.isfile(path):
                s["files"].append((path, name))
            else:
                s["missing"].append((entry, path))
    return s, problems


def safe_name(name):
    """機器で remote_dir の外を指さず、FTP のコマンドを壊さない名前か。"""
    return name not in ("", ".", "..") and "/" not in name and "\\" not in name \
        and not CONTROL.search(name)


def logged_settings(raw):
    shown = dict(raw)
    if shown.get("password"):
        shown["password"] = MASK
    return json.dumps(shown, ensure_ascii=False)


# --- FTP --------------------------------------------------------------------

def explain(what, e, s, login=False, transfer=False, hint=None):
    """例外を「何ができないか。なぜか。何を確かめるか。」の文にする。hint を渡せば、それを確かめることにする。"""
    reply = mask(str(e).strip())
    code = reply[:3] if isinstance(e, ftplib.Error) else ""
    if isinstance(e, socket.gaierror):
        why, check = "ホスト名 %s が見つかりません" % s["host"], "host を確かめてください"
    elif isinstance(e, ConnectionRefusedError) and transfer:
        why, check = "ファイルを送るときに使う接続を断られました", "passive を変えると直ることがあります"
    elif isinstance(e, ConnectionRefusedError):
        why = "接続を断られました。機器の FTP サーバーが動いていないようです"
        check = "機器で FTP サーバーが動いているかと、port を確かめてください"
    elif isinstance(e, (socket.timeout, TimeoutError)):
        why = "%g 秒待っても機器が応答しません" % s["timeout"]
        check = "機器の電源とケーブル、host を確かめてください"
        if transfer:
            check += "。passive を変えると直ることがあります"
    elif isinstance(e, OSError) and e.errno in UNREACHABLE:
        why, check = "機器までの経路がありません", "ケーブルと host、PC のネットワークの設定を確かめてください"
    elif isinstance(e, DROPPED):
        why, check = "途中で接続が切れました", "機器の電源とケーブルを確かめてください"
    elif code == "421":
        why, check = "機器の FTP サーバーが接続を切りました（%s）" % reply, "機器の FTP サーバーのようすを確かめてください"
    elif isinstance(e, ssl.SSLError) or code == "522":
        why, check = "暗号化の手続きに失敗しました（%s）" % reply, "機器の FTP サーバーが FTPS に対応しているかと、tls を確かめてください"
    elif isinstance(e, UnicodeDecodeError):
        why, check = "機器の FTP サーバーの答えを UTF-8 で読めません", "機器の FTP サーバーの文字コードの設定を確かめてください"
    elif isinstance(e, ValueError):
        why, check = "機器の FTP サーバーの答えの形が違います（%s）" % reply, "機器の FTP サーバーの設定を確かめてください"
    elif login and code == "530" and s["user"]:
        why = "ユーザー名かパスワードが違います（%s）" % reply
        check = "user と password（password_env を使うときは、その環境変数）を確かめてください"
    elif login and code == "530":
        why, check = "anonymous でのログインを断られました（%s）" % reply, "user と password を書いてください"
    elif code in ("452", "552"):
        why, check = "機器の空き容量が足りません（%s）" % reply, "機器の空き容量を確かめてください"
    elif code in ("450", "530", "532", "550", "553"):
        why = "機器に断られました。権限がないか、場所がありません（%s）" % reply
        check = "user に書き込む権限があるかと、remote_dir を確かめてください"
    elif code in ("500", "502", "504"):
        why, check = "機器の FTP サーバーが、このコマンドに対応していません（%s）" % reply, "機器の FTP サーバーの設定を確かめてください"
    elif isinstance(e, ftplib.Error):
        why, check = "機器の FTP サーバーの答え：%s" % reply, "機器の FTP サーバーの設定を確かめてください"
    else:
        why = "%s: %s" % (type(e).__name__, reply)
        check = "機器の電源とケーブル、host を確かめてください"
    return "%s。%s。%s。" % (what, why, hint or check)


class FTPS(ftplib.FTP_TLS):
    """ファイルを送る接続でも、ログインした接続の TLS セッションを使い回す。
    ftplib はそうしないので、vsftpd（既定の require_ssl_reuse=YES）などに断られる。"""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def connect(s):
    """機器につないでログインする。つながらなくても、やり直さない。"""
    ftp = FTPS() if s["tls"] else ftplib.FTP()
    ftp.encoding = "utf-8"  # Python 3.8 の既定は latin-1。ファイルの名前を UTF-8 で送る
    host, port = s["host"], s["port"]
    user = s["user"] or "anonymous"
    LOG("機器 %s の %d 番ポートに FTP でつなぎます（tls = %s、ユーザー %s）。"
        % (host, port, "true" if s["tls"] else "false", user))
    step, login, check = "機器 %s の %d 番ポートに FTP でつながりません" % (host, port), False, None
    try:
        LOG(mask(ftp.connect(host, port, timeout=s["timeout"])))
        if s["tls"]:
            step = "FTPS（tls = true）を始められません"
            check = "機器の FTP サーバーが FTPS に対応しているかを確かめてください。対応していなければ、tls を false にしてください"
            LOG(mask(ftp.auth()))
        step, login, check = "機器 %s に FTP でログインできません" % host, True, None
        LOG(mask(ftp.login(user, s["password"])))
        if s["tls"]:
            step, login = "FTPS でファイルを送る準備ができません", False
            LOG(mask(ftp.prot_p()))
    except FTP_ERRORS as e:
        log_failure(e)
        close(ftp)
        raise Stop(explain(step, e, s, login=login, hint=check))
    ftp.set_pasv(s["passive"])
    return ftp


def close(ftp):
    """QUIT を送って閉じる。つながる前や、切れた後でも止まらない。"""
    for action in (ftp.quit, ftp.close):
        try:
            action()
        except Exception:
            pass


def enter_remote_dir(ftp, s, create):
    """remote_dir に入る。入れなければ、親から順に入り、入れないフォルダを create なら作る。
    create でなければ作らずに、入れなかったフォルダの名前を返す（入れたら None）。"""
    remote_dir = s["remote_dir"]
    try:
        ftp.cwd(remote_dir)
        return None
    except ftplib.error_perm:
        pass
    if remote_dir.startswith("/"):
        ftp.cwd("/")
    for part in s["remote_parts"]:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            if not create:
                return part
            ftp.mkd(part)
            LOG("機器にフォルダ %s を作りました。" % part)
            ftp.cwd(part)
    return None


def current_dir(ftp, default):
    try:
        return ftp.pwd()
    except ftplib.Error:
        return default


def names_here(ftp):
    """今のフォルダの中の名前。ファイルを送るときと同じ接続を開くので、passive や FTPS の問題もわかる。"""
    try:
        return [n.rstrip("/").rsplit("/", 1)[-1] for n in ftp.nlst()]
    except (ftplib.error_perm, ftplib.error_temp) as e:
        if str(e)[:3] not in ("450", "550"):  # 空のフォルダで 450 か 550 を返す FTP サーバーがある
            raise
        return []


def remote_size(ftp, name):
    """機器のファイルのサイズ。なければ None。"""
    try:
        return ftp.size(name)
    except ftplib.error_perm:
        return None


def send_file(ftp, s, path, name):
    """1つのファイルを name.part に送り、サイズを確かめ、権限を変えてから name に置き換える。
    失敗したら name.part を消して Stop。古い name を消したあとの失敗なら、name.part は残す。"""
    part = name + ".part"
    try:
        size = os.path.getsize(path)
        fh = open(path, "rb")
    except OSError as e:
        raise Stop("PC のファイル %s を読めません（%s）。" % (path, e.strerror or e))
    LOG("%s（%d バイト）を %s として送ります。" % (path, size, part))
    step, sending = "%s を送れません" % name, True
    try:
        with fh:
            LOG(mask(ftp.storbinary("STOR " + part, fh, BLOCK_SIZE)))
        step, sending = "送った %s のサイズを確かめられません" % name, False
        sent = ftp.size(part)
        if sent != size:
            raise Stop("送った %s のサイズが合いません（機器：%s バイト、PC：%d バイト）。"
                       "送る途中で壊れたおそれがあります。機器の空き容量とケーブルを確かめてください。"
                       % (name, "?" if sent is None else sent, size))
        LOG("機器の %s のサイズは PC と同じです（%d バイト）。" % (part, size))
        if s["file_mode"]:
            # 置き換える前に変える。失敗しても、元のファイルはそのまま残る
            step = "%s の権限を %s にできません" % (name, s["file_mode"])
            LOG(mask(ftp.sendcmd("SITE CHMOD %s %s" % (s["file_mode"], part))))
        step = "%s を %s に置き換えられません" % (part, name)
        replace(ftp, part, name)
        return size
    except OldRemoved as e:
        log_failure(e.error)
        raise Stop(explain(step, e.error, s) + "古い %s は消しました。送ったファイルは %s として機器にあります。"
                   "もう一度送るか、機器で名前を %s に変えてください。" % (name, part, name))
    except Stop as e:
        message = e.message
    except FTP_ERRORS as e:
        log_failure(e)
        message = explain(step, e, s, transfer=sending)
    if not remove_part(ftp, part):
        message += "機器に %s が残っているかもしれません。" % part
    raise Stop(message)


def replace(ftp, part, name):
    """part の名前を name に変える。上書きの名前の変更を断る機器では、古い name を消してから変える。
    古い name を消したあとに変えられなければ OldRemoved。"""
    reply = ftp.sendcmd("RNFR " + part)  # 断られたら、何も消さずに失敗にする
    if reply[:1] != "3":
        raise ftplib.error_reply(reply)
    try:
        LOG(mask(ftp.voidcmd("RNTO " + name)))
        return
    except ftplib.error_perm as e:
        if remote_size(ftp, name) is None:  # 古い name がなければ、ほかの理由で断られた
            raise
        LOG("上書きの名前の変更を断られました（%s）。古い %s を消して、もう一度試します。" % (mask(e), name))
    LOG(mask(ftp.delete(name)))
    try:
        LOG(mask(ftp.rename(part, name)))
    except FTP_ERRORS as e:
        raise OldRemoved(e)


def remove_part(ftp, part):
    """途中までのファイルを消す。消せたか、もともとなければ True。残っているかもしれなければ False。"""
    try:
        LOG(mask(ftp.delete(part)))
        LOG("機器の %s を消しました。" % part)
        return True
    except ftplib.error_perm:
        try:
            return remote_size(ftp, part) is None  # 消すのを断られたのか、もともとないのか
        except FTP_ERRORS:
            return False
    except FTP_ERRORS:
        return False


# --- 最初の引数ごとの処理 -----------------------------------------------------

def checked_settings(raw, workdir, result, need_files):
    LOG("設定：%s" % logged_settings(raw))
    s, problems = settings_from(raw, workdir, os.environ)
    if need_files:
        problems += ["送るファイル %s がありません（%s）。ビルドしたかと、files の書き方を確かめてください。" % m
                     for m in s["missing"]]
    if problems:
        result["problems"] = problems
        raise Stop("".join(problems) + "設定ファイル %s を確かめてください。" % result["config"], 2)
    return s


def open_log(workdir):
    try:
        logs = os.path.join(prepare_data_dir(workdir), "logs")
        os.makedirs(logs, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        for n in range(1, 100):  # 同じ秒に何度も動かしたときは、名前に -2、-3… を付ける
            try:
                LOG.open(os.path.join(logs, "%s-%s%s.log" % (SKILL_NAME, stamp, "-%d" % n if n > 1 else "")))
                return
            except FileExistsError:
                pass
    except OSError as e:
        raise Stop("ログのファイルを作れません（%s）。作業フォルダに書き込めるかを確かめてください。"
                   % (e.strerror or e))


def cmd_init(workdir, result):
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


def cmd_check(workdir, result):
    result.update(notes=[], remote_dir=None)
    s = checked_settings(read_config(result["config"]), workdir, result, need_files=False)
    notes = result["notes"]
    notes += ["送るファイル %s は、まだありません。ビルドの前なら、それでかまいません。" % entry
              for entry, _ in s["missing"]]
    ftp = connect(s)
    remote_dir = s["remote_dir"]
    step = "機器の送り先のフォルダ %s に入れません" % remote_dir
    try:
        missing = enter_remote_dir(ftp, s, create=False)
        here = current_dir(ftp, remote_dir)
        step = "機器のフォルダ %s の一覧を取れません" % here
        names = names_here(ftp)
    except FTP_ERRORS as e:
        log_failure(e)
        raise Stop(explain(step, e, s, transfer=True))
    finally:
        close(ftp)
    if missing in names:
        raise Stop("機器 %s にはログインできましたが、送り先のフォルダ %s に入れません。"
                   "ファイルではなくフォルダかと、user に入る権限があるかを確かめてください。" % (s["host"], remote_dir))
    if missing:
        notes.append("送り先のフォルダ %s は、まだありません。run のときに作ります。" % remote_dir)
        message = "機器 %s に FTP でログインできました。" % s["host"]
    else:
        result["remote_dir"] = here
        message = "機器 %s に FTP でログインし、送り先のフォルダ %s の一覧を取れました。" % (s["host"], here)
    result.update(ok=True, message=message + "".join(notes))
    return 0


def cmd_run(workdir, result):
    result.update(files=[], remote_dir=None)
    raw = read_config(result["config"])
    open_log(workdir)
    result["log"] = LOG.path
    LOG("run を始めます。設定ファイル：%s" % result["config"])
    s = checked_settings(raw, workdir, result, need_files=True)
    ftp = connect(s)
    try:
        try:
            enter_remote_dir(ftp, s, create=True)
        except FTP_ERRORS as e:
            log_failure(e)
            raise Stop(explain("機器の送り先のフォルダ %s に入れないか、作れません" % s["remote_dir"], e, s))
        remote_dir = current_dir(ftp, s["remote_dir"])
        result["remote_dir"] = remote_dir
        LOG("送り先：%s" % remote_dir)
        for path, name in s["files"]:
            size = send_file(ftp, s, path, name)
            remote = remote_dir.rstrip("/") + "/" + name
            result["files"].append({"local": path, "remote": remote, "size": size, "mode": s["file_mode"]})
            LOG("%s を送りました。" % remote)
    finally:
        close(ftp)
    mode = ("権限は %s にしました。" % s["file_mode"]) if s["file_mode"] else "file_mode が空なので、権限は変えていません。"
    result.update(ok=True, message="機器 %s の %s に、%d 個のファイルを送りました。%s"
                  % (s["host"], remote_dir, len(result["files"]), mode))
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Stop("コマンドの書き方が違います。" + USAGE, 2)


def main(argv=None):
    parser = Parser(prog="ftp_deploy.py", add_help=False)
    parser.add_argument("command", choices=("run", "check", "init"))
    parser.add_argument("--workdir")
    result = {"ok": False, "message": "", "problems": [], "config": None, "log": None}
    try:
        args = parser.parse_args(argv)
        workdir = os.path.abspath(args.workdir or os.getcwd())
        result["config"] = os.path.join(data_dir(workdir), "config.yaml")
        if not os.path.isdir(workdir):
            raise Stop("作業フォルダ %s がありません。--workdir を確かめてください。" % workdir, 2)
        code = {"run": cmd_run, "check": cmd_check, "init": cmd_init}[args.command](workdir, result)
    except Stop as e:
        result["ok"] = False
        result["message"] = e.message
        if e.code == 2 and not result["problems"]:
            result["problems"] = [e.message]
        code = e.code
    except Exception as e:
        LOG(mask(traceback.format_exc()))
        result["ok"] = False
        result["message"] = "思わぬエラーで止まりました（%s: %s）。" % (type(e).__name__, mask(e))
        if LOG.path:
            result["message"] += "ログ %s を見てください。" % LOG.path
        code = 1
    LOG(result["message"])
    LOG.close()
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
