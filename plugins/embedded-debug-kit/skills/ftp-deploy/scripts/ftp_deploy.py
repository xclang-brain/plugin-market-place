#!/usr/bin/env python3
"""ファイルを FTP で機器に送る。

  ftp_deploy.py run|check|init [--workdir 作業フォルダ]

設定は <作業フォルダ>/.<スキルの名前>/config.ini、ログは同じフォルダの logs/ に置く。
標準出力には結果の JSON を1つだけ、ASCII で書く。
終了コード：0 成功、1 作業の失敗、2 設定や引数の問題。
Python 3.8 以上の標準ライブラリだけで動く。
"""

import argparse
import configparser
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
TEMPLATE = os.path.join(SKILL_DIR, "config-template.ini")
SECTION = "settings"
KEYS = ("host", "port", "user", "password", "password_env", "passive", "tls",
        "files", "remote_dir", "file_mode", "timeout_seconds")
USAGE = "使い方：ftp_deploy.py run|check|init [--workdir 作業フォルダ]"
MASK = "***"
BLOCK_SIZE = 64 * 1024
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
FTP_ERRORS = ftplib.all_errors  # ftplib.Error、OSError（ssl.SSLError を含む）、EOFError
DROPPED = (EOFError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
# 機器までの経路がないときの errno。Windows では WSA の番号になる。
UNREACHABLE = {getattr(errno, n) for n in (
    "EHOSTUNREACH", "ENETUNREACH", "EHOSTDOWN", "ENETDOWN",
    "WSAEHOSTUNREACH", "WSAENETUNREACH", "WSAEHOSTDOWN", "WSAENETDOWN") if hasattr(errno, n)}

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
        for secret in SECRETS:
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

    def __call__(self, text):
        if self.fh is None:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in mask(str(text)).splitlines() or [""]:
            self.fh.write("%s %s\n" % (stamp, line))
        self.fh.flush()

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None


LOG = Log()


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


def read_config(path):
    """[settings] の項目を辞書で返す。読めなければ Stop（終了コード 2）。"""
    if not os.path.isfile(path):
        raise Stop("設定ファイル %s がありません。init で作ってから、値を書いてください。" % path, 2)
    parser = configparser.ConfigParser(interpolation=None)  # % をそのまま値として扱う
    # 誤りを伝えるときに、行の中身は出さない（パスワードの行かもしれない）
    try:
        with open(path, encoding="utf-8-sig") as fh:  # BOM があっても読める
            parser.read_file(fh)
    except UnicodeDecodeError:
        raise Stop("設定ファイル %s を UTF-8 で読めません。UTF-8 で保存し直してください。" % path, 2)
    except configparser.DuplicateOptionError as e:
        raise Stop("設定ファイル %s の %s 行目：%s が2回書かれています。" % (path, e.lineno, e.option), 2)
    except configparser.DuplicateSectionError as e:
        raise Stop("設定ファイル %s の %s 行目：[%s] が2回書かれています。" % (path, e.lineno, e.section), 2)
    except configparser.MissingSectionHeaderError:
        raise Stop("設定ファイル %s の最初の項目より前に [settings] の行がありません。" % path, 2)
    except configparser.Error as e:
        errors = getattr(e, "errors", None)
        where = "の %s 行目" % errors[0][0] if errors else ""
        raise Stop("設定ファイル %s%sを読めません。「項目 = 値」の形で書いてください。"
                   "2つ目からのリストの値は、行頭に空白を入れてください。" % (path, where), 2)
    if not parser.has_section(SECTION):
        raise Stop("設定ファイル %s に [settings] の行がありません。" % path, 2)
    raw = dict(parser.items(SECTION))
    if raw.get("password"):
        SECRETS.append(raw["password"])
    return raw


def settings_from(raw, workdir, environ):
    """値を確かめる。(settings, problems) を返す。problems は日本語の文のリスト。"""
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

    for key, default, choices in (("passive", "true", ("true", "false")), ("tls", "no", ("no", "yes"))):
        value = get(key, default).lower()
        if value not in choices:
            problems.append("%s は %s か %s にしてください（今の値：%s）。" % ((key,) + choices + (value,)))
        s[key] = value in ("true", "yes")

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

    s["files"] = []
    entries = [line.strip() for line in raw.get("files", "").splitlines() if line.strip()]
    if not entries:
        problems.append("files（送るファイル）が空です。")
    seen = {}
    for entry in entries:
        path = os.path.normpath(os.path.join(workdir, entry))
        name = os.path.basename(path)
        if not os.path.isfile(path):
            problems.append("送るファイル %s がありません（%s）。" % (entry, path))
        elif not safe_name(name):
            problems.append("%s は、名前に \\ や改行などが入っているので送れません。"
                            "名前を変えてください。" % entry)
        elif name in seen:
            problems.append("files に、名前が同じファイルが2つあります（%s と %s）。" % (seen[name], entry))
        else:
            seen[name] = entry
            s["files"].append((path, name))
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
    reply = str(e).strip()
    code = reply[:3] if isinstance(e, ftplib.Error) else ""
    if isinstance(e, socket.gaierror):
        why, check = "ホスト名 %s が見つかりません" % s["host"], "host を確かめてください"
    elif isinstance(e, ConnectionRefusedError) and transfer:
        why, check = "ファイルを送るための接続を断られました", "passive を変えると直ることがあります"
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
    elif isinstance(e, ssl.SSLError):
        why, check = "暗号化の手続きに失敗しました（%s）" % reply, "機器の FTP サーバーが FTPS に対応しているかと、tls を確かめてください"
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


def connect(s):
    """機器につないでログインする。つながらなくても、やり直さない。"""
    ftp = ftplib.FTP_TLS() if s["tls"] else ftplib.FTP()
    ftp.encoding = "utf-8"  # Python 3.8 の既定は latin-1。ファイルの名前を UTF-8 で送る
    host, port = s["host"], s["port"]
    user = s["user"] or "anonymous"
    LOG("機器 %s の %d 番ポートに FTP でつなぎます（tls = %s、ユーザー %s）。"
        % (host, port, "yes" if s["tls"] else "no", user))
    step, login, check = "機器 %s の %d 番ポートに FTP でつながりません" % (host, port), False, None
    try:
        LOG(ftp.connect(host, port, timeout=s["timeout"]))
        if s["tls"]:
            step = "FTPS（tls = yes）を始められません"
            check = "機器の FTP サーバーが FTPS に対応しているかを確かめてください。対応していなければ、tls を no にしてください"
            LOG(ftp.auth())
        step, login, check = "機器 %s に FTP でログインできません" % host, True, None
        LOG(ftp.login(user, s["password"]))
        if s["tls"]:
            step, login = "FTPS でファイルを送る準備ができません", False
            LOG(ftp.prot_p())
    except FTP_ERRORS as e:
        LOG("失敗：%r" % (e,))
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
    """remote_dir に入り、その絶対パスを返す。create なら、ないフォルダを親から順に作る。"""
    remote_dir = s["remote_dir"]
    step = "機器のフォルダ %s に入れません" % remote_dir
    try:
        try:
            ftp.cwd(remote_dir)
        except ftplib.error_perm as e:
            if not create:
                raise Stop("機器 %s にはログインできましたが、フォルダ %s に入れません（%s）。"
                           "フォルダがないなら、run のときに作ります。"
                           "あるなら、user に入る権限があるかを確かめてください。" % (s["host"], remote_dir, e))
            step = "機器に送り先のフォルダ %s を作れません" % remote_dir
            if remote_dir.startswith("/"):
                ftp.cwd("/")
            for part in s["remote_parts"]:
                try:
                    ftp.cwd(part)
                except ftplib.error_perm:
                    ftp.mkd(part)
                    LOG("機器にフォルダ %s を作りました。" % part)
                    ftp.cwd(part)
        try:
            return ftp.pwd()
        except ftplib.Error:
            return remote_dir
    except FTP_ERRORS as e:
        LOG("失敗：%r" % (e,))
        raise Stop(explain(step, e, s))


def send_file(ftp, s, path, name):
    """1つのファイルを name.part に送り、サイズを確かめ、権限を変えてから name に置き換える。
    失敗したら name.part を消して Stop。"""
    part = name + ".part"
    size = os.path.getsize(path)
    try:
        fh = open(path, "rb")
    except OSError as e:
        raise Stop("PC のファイル %s を読めません（%s）。" % (path, e.strerror or e))
    LOG("%s（%d バイト）を %s として送ります。" % (path, size, part))
    step, sending = "%s を送れません" % name, True
    try:
        with fh:
            LOG(ftp.storbinary("STOR " + part, fh, BLOCK_SIZE))
        step, sending = "送った %s のサイズを確かめられません" % name, False
        remote_size = ftp.size(part)
        if remote_size != size:
            raise Stop("送った %s のサイズが合いません（機器：%s バイト、PC：%d バイト）。"
                       "送る途中で壊れたおそれがあります。機器の空き容量とケーブルを確かめてください。"
                       % (name, "?" if remote_size is None else remote_size, size))
        LOG("機器の %s のサイズは PC と同じです（%d バイト）。" % (part, size))
        if s["file_mode"]:
            # 置き換える前に変える。失敗しても、元のファイルはそのまま残る
            step = "%s の権限を %s にできません" % (name, s["file_mode"])
            LOG(ftp.sendcmd("SITE CHMOD %s %s" % (s["file_mode"], part)))
        step = "%s を %s に置き換えられません" % (part, name)
        replace(ftp, part, name)
    except Stop:
        remove_part(ftp, part)
        raise
    except FTP_ERRORS as e:
        LOG("失敗：%r" % (e,))
        message = explain(step, e, s, transfer=sending)
        if not remove_part(ftp, part):
            message += "機器に %s が残っているかもしれません。" % part
        raise Stop(message)
    return size


def replace(ftp, part, name):
    try:
        LOG(ftp.rename(part, name))
    except ftplib.error_perm as first:
        # 今あるファイルへの名前の変更を断るサーバーがある。古いファイルを消して、もう一度試す
        LOG("名前を変えられません（%s）。古い %s を消して、もう一度試します。" % (first, name))
        try:
            LOG(ftp.delete(name))
        except ftplib.error_perm:
            raise first
        LOG(ftp.rename(part, name))


def remove_part(ftp, part):
    """途中までのファイルを消す。消せたか、もともとなければ True。接続が切れていれば False。"""
    try:
        LOG(ftp.delete(part))
        LOG("機器の %s を消しました。" % part)
    except ftplib.error_perm:
        pass
    except FTP_ERRORS:
        return False
    return True


# --- サブコマンド -------------------------------------------------------------

def checked_settings(raw, workdir, result):
    LOG("設定：%s" % logged_settings(raw))
    s, problems = settings_from(raw, workdir, os.environ)
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
    s = checked_settings(read_config(result["config"]), workdir, result)
    ftp = connect(s)
    try:
        result["remote_dir"] = enter_remote_dir(ftp, s, create=False)
    finally:
        close(ftp)
    result.update(ok=True, message="機器 %s に FTP でログインでき、送り先のフォルダ %s があることを確かめました。"
                  % (s["host"], result["remote_dir"]))
    return 0


def cmd_run(workdir, result):
    result.update(files=[], remote_dir=None)
    raw = read_config(result["config"])
    open_log(workdir)
    result["log"] = LOG.path
    LOG("run を始めます。設定ファイル：%s" % result["config"])
    s = checked_settings(raw, workdir, result)
    ftp = connect(s)
    try:
        remote_dir = enter_remote_dir(ftp, s, create=True)
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
        result["config"] = os.path.join(data_dir(workdir), "config.ini")
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
        LOG(traceback.format_exc())
        result["ok"] = False
        result["message"] = "思わぬエラーで止まりました（%s: %s）。" % (type(e).__name__, e)
        if LOG.path:
            result["message"] += "ログ %s を見てください。" % LOG.path
        code = 1
    LOG(result["message"])
    LOG.close()
    sys.stdout.write(json.dumps(mask(result), ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
