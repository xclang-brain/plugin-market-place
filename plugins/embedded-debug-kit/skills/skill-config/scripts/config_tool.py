#!/usr/bin/env python3
"""ほかのスキルの設定ファイル <作業フォルダ>/.<スキルの名前>/config.ini を読み書きする。

  config_tool.py show <スキルの名前> [--workdir 作業フォルダ]
  config_tool.py set <スキルの名前> <項目> --stdin [--workdir 作業フォルダ]

show は、項目ごとに、値と、すぐ上のコメントから読んだこと（必須・秘密・例・選べる値）を返す。何も作らない。
set は、標準入力の値を1つの項目に書く。その項目の行だけを書き換え、コメントと並びは残す。
標準出力には結果の JSON を1つだけ、ASCII で書く。
終了コード：0 成功、1 書き込みの失敗、2 設定ファイルや引数の問題。
Python 3.8 以上の標準ライブラリだけで動く。
"""

import argparse
import configparser
import datetime
import io
import json
import locale
import os
import re
import stat
import sys
import tempfile
import traceback

# このスキルの名前は、このスクリプトのフォルダの親の名前。名前を変えて写せば、その名前になる。
SKILL_NAME = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECTION = "settings"
MASK = "＊＊＊"
INDENT = "    "
USAGE = ("使い方：config_tool.py show <スキルの名前> [--workdir 作業フォルダ]、"
         "config_tool.py set <スキルの名前> <項目> --stdin [--workdir 作業フォルダ]")
HOW_TO_PASS = ("bash は「--stdin <<'END'」の次の行から値を書いて END の行で閉じ、"
               "PowerShell は @' と '@ の間に値を書いて、パイプで渡してください。")
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUIRED_RE = re.compile(r"[（(]必須[）)]")
SECRET_RE = re.compile(r"[（(]秘密[）)]")
# コメントに（秘密）がなくても、名前がパスワードらしい項目は伏せる
SECRET_NAME_RE = re.compile(r"password|passwd|passphrase|secret|token|api_?key", re.IGNORECASE)
EXAMPLE_RE = re.compile(r"^例\s*[：:]\s*(.*)$")
CHOICES_RE = re.compile(r"^選べる値\s*[：:]\s*(.*)$")
NEWLINE_RE = re.compile(r"(\r\n|\r|\n)")  # configparser がファイルを読むときと同じ区切り
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# 行の見分けは、設定を使うスキルと同じ configparser の決まりにそろえる
SECTCRE = configparser.ConfigParser.SECTCRE
OPTCRE = configparser.ConfigParser.OPTCRE
COMMENT = ("#", ";")


class Stop(Exception):
    """利用者に伝えて終える。code は終了コード。"""

    def __init__(self, message, code=2):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


class Log(object):
    """ログのファイル（UTF-8）。開いていなければ何も書かない（show は何も作らない）。"""

    path = None
    fh = None

    def open(self, workdir):
        folder = os.path.join(workdir, "." + SKILL_NAME)
        try:
            os.makedirs(folder, exist_ok=True)
            put_gitignore(folder)
            logs = os.path.join(folder, "logs")
            os.makedirs(logs, exist_ok=True)
            path = os.path.join(logs, "%s-%s.log" % (SKILL_NAME, datetime.date.today().strftime("%Y%m%d")))
            self.fh = open(path, "a", encoding="utf-8")
            self.path = path
        except OSError as e:
            raise Stop("ログのファイルを作れません（%s）。作業フォルダに書き込めるかを確かめてください。"
                       % (e.strerror or e), 1)

    def __call__(self, text):
        if self.fh is not None:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for line in str(text).splitlines() or [""]:
                self.fh.write("%s %s\n" % (stamp, line))
            self.fh.flush()

    def close(self):
        if self.fh is not None:
            self.fh.close()
        self.fh = self.path = None


LOG = Log()


def put_gitignore(folder):
    """フォルダの中身を Git に入れない .gitignore を置く（あれば、そのまま）。"""
    path = os.path.join(folder, ".gitignore")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("*\n")


# --- 設定ファイルを読む ---------------------------------------------------------

class Item(object):
    """1つの項目。start から end までが、その項目の行（リストの続きの行を含む）。"""

    def __init__(self, written, delimiter, line, comments):
        self.written = written  # ファイルに書かれたとおりの名前
        self.key = written.lower()  # configparser と同じく、大文字と小文字を区別しない
        self.delimiter = delimiter
        self.start = self.end = line
        self.comments = comments  # すぐ上のコメントの行（# を除いた文）
        self.required = any(REQUIRED_RE.search(c) for c in comments)
        # <項目>_env は環境変数の名前を書く項目なので、秘密にしない
        self.secret = not self.key.endswith("_env") and (
            any(SECRET_RE.search(c) for c in comments) or bool(SECRET_NAME_RE.search(self.key)))
        self.examples, self.choices, words = [], [], []
        for c in comments:
            example, choices = EXAMPLE_RE.match(c), CHOICES_RE.match(c)
            if example:
                self.examples.append(example.group(1).strip())
            elif choices:
                self.choices = [v.strip() for v in choices.group(1).split("/") if v.strip()]
            elif c:
                words.append(c)
        self.comment = "\n".join(words)


def scan(lines):
    """[settings] の項目と、そのすぐ上のコメントを、ファイルの順に返す。"""
    items, comments, item, section = [], [], None, None
    for i, line in enumerate(lines):
        text = line.strip()
        indented = line[:1].isspace()
        if item is not None and indented and text:
            item.end = i  # 値の続きの行（字下げしたコメントも、この項目の中にある）
            continue
        item = None
        if text.startswith(COMMENT) and not indented:
            comments.append(text[1:].strip())
            continue
        header = None if indented else SECTCRE.match(text)
        if header:
            section = header.group("header")
        elif section == SECTION and not indented and text:
            option = OPTCRE.match(text)
            if option and option.group("option").strip():
                item = Item(option.group("option").strip(), option.group("vi"), i, comments)
                items.append(item)
        comments = []
    return items


def parse(path, text):
    """configparser で読んだ [settings] の値（文字列）を返す。設定を使うスキルと同じ読み方をする。"""
    parser = configparser.ConfigParser(interpolation=None)  # % をそのまま値として扱う
    # 誤りを伝えるときに、行の中身は出さない（パスワードの行かもしれない）
    try:
        parser.read_file(io.StringIO(text, newline=None), source=path)
    except configparser.DuplicateOptionError as e:
        raise Stop("設定ファイル %s の %s 行目：%s が2回書かれています。どちらかを消してください。"
                   % (path, e.lineno, e.option))
    except configparser.DuplicateSectionError as e:
        raise Stop("設定ファイル %s の %s 行目：[%s] が2回書かれています。" % (path, e.lineno, e.section))
    except configparser.MissingSectionHeaderError:
        raise Stop("設定ファイル %s の最初の項目より前に [settings] の行がありません。" % path)
    except configparser.Error as e:
        errors = getattr(e, "errors", None)
        where = " の %s 行目" % errors[0][0] if errors else ""
        raise Stop("設定ファイル %s%s を読めません。「項目 = 値」の形で書いてください。"
                   "リストの2つ目からは、行頭に空白を入れてください。" % (path, where))
    if not parser.has_section(SECTION):
        raise Stop("設定ファイル %s に [settings] の行がありません。" % path)
    return dict(parser.items(SECTION, raw=True))


class Doc(object):
    """設定ファイルの中身。行と改行を分けて持ち、書くときに元の改行に戻す。"""

    def __init__(self, path, text, bom=False):
        self.path, self.bom = path, bom
        parts = NEWLINE_RE.split(text)
        self.lines, self.ends = parts[0::2], parts[1::2] + [""]
        self.newline = parts[1] if len(parts) > 1 else "\n"
        self.values = parse(path, text)
        self.items = scan(self.lines)
        unread = sorted(set(self.values) - set(i.key for i in self.items))
        if unread:
            raise Stop("設定ファイル %s の項目 %s の行を見つけられません。項目の行は、行頭に空白を入れずに "
                       "[settings] の下に書いてください。" % (path, "、".join(unread)))

    def value(self, item):
        return to_value(self.values[item.key])

    def find(self, key):
        for item in self.items:
            if item.key == key.strip().lower():
                return item
        raise Stop("設定ファイル %s に、項目 %s はありません。ある項目：%s。"
                   % (self.path, key, "、".join(i.key for i in self.items)))


def load(path, skill):
    if not os.path.isfile(path):
        raise Stop("設定ファイル %s がありません。先に %s スキルに、設定ファイルを作るよう頼んでください"
                   "（そのスキルの init で作れます）。" % (path, skill))
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise Stop("設定ファイル %s を読めません（%s）。ファイルの権限を確かめてください。" % (path, e.strerror or e))
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data[3 if bom else 0:].decode("utf-8")
    except UnicodeDecodeError:
        raise Stop("設定ファイル %s を UTF-8 で読めません。UTF-8 で保存し直してください。" % path)
    return Doc(path, text, bom)


def to_value(raw):
    """configparser の値を、結果の形にする。2行以上ならリスト。"""
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    return lines if len(lines) > 1 else (lines[0] if lines else "")


def shown(item, value):
    """結果とログに出す値。（秘密）の値は、空でなければ伏せる。"""
    return MASK if item.secret and value else value


# --- show ---------------------------------------------------------------------

def cmd_show(path, skill, result):
    result.update(items=[], missing=[])
    doc = load(path, skill)
    keys = set(i.key for i in doc.items)
    for item in doc.items:
        env_key = item.key + "_env" if item.secret and item.key + "_env" in keys else None
        value = doc.value(item)
        result["items"].append({
            "key": item.key, "value": shown(item, value), "required": item.required, "secret": item.secret,
            "examples": item.examples, "choices": item.choices, "comment": item.comment, "env_key": env_key})
        # （秘密）の値を環境変数で渡すなら、元の項目は空でよい
        if item.required and not value and not (env_key and doc.values[env_key].strip()):
            result["missing"].append(item.key)
    if result["missing"]:
        tail = "必須なのに空の項目が %d 個あります：%s。" % (len(result["missing"]), "、".join(result["missing"]))
    else:
        tail = "必須の項目は、すべて埋まっています。"
    result.update(ok=True, message="設定ファイル %s を読みました。%s" % (path, tail))


# --- set ----------------------------------------------------------------------

def local_encodings():
    """UTF-8 で読めないときに試す、OS の既定の文字コード（日本語の Windows なら cp932）。"""
    names = [locale.getpreferredencoding(False)]
    if hasattr(locale, "getencoding"):  # Python 3.11 から。UTF-8 モードでも OS の既定を返す
        names.insert(0, locale.getencoding())
    return [n for n in names if n]


def decode_input(data, encodings=None):
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for name in ["utf-8"] + (local_encodings() if encodings is None else encodings):
        try:
            return data.decode(name)
        except (UnicodeDecodeError, LookupError):
            pass
    raise Stop("標準入力の値を文字として読めません。UTF-8 で渡してください。")


def read_input():
    """標準入力の値を、行のリストにする。前後の空白と空の行は除く。"""
    stream = getattr(sys.stdin, "buffer", None)
    try:
        waiting = stream is None or stream.isatty()
    except (OSError, ValueError):
        waiting = True
    if waiting:  # 渡し忘れると、入力を待ったまま止まる。待たずに知らせる
        raise Stop("値が標準入力で渡されていません。" + HOW_TO_PASS)
    text = decode_input(stream.read())
    return [line.strip() for line in re.split(r"\r\n|\r|\n", text) if line.strip()]


def checked_values(doc, item, values):
    """書く値を確かめて返す。書けない値なら Stop。"""
    if item.secret and values:
        hint = "%s_env に環境変数の名前を書くか、" % item.key if item.key + "_env" in doc.values else ""
        raise Stop("%s は（秘密）の項目なので、値はここでは書けません。%s利用者がファイルを開いて書いてください。"
                   "空にすることはできます。" % (item.key, hint))
    if any(CONTROL_RE.search(v) for v in values):
        raise Stop("値に制御文字が入っています。%s の値を確かめてください。" % item.key)
    if len(values) > 1 and any(v.startswith(COMMENT) for v in values):
        raise Stop("2行以上の値では、# や ; で始まる行を書けません（コメントとして読まれます）。")
    if item.choices:
        match = [c for c in item.choices if len(values) == 1 and c.lower() == values[0].lower()]
        if not match:
            raise Stop("%s には、次のどれか1つを書いてください：%s。" % (item.key, " / ".join(item.choices)))
        values = match[:1]
    return values


def replaced(doc, item, values):
    """その項目の行だけを書き換えた中身を返す。"""
    head = "%s %s" % (item.written, item.delimiter)
    new = [head + " " + values[0]] if len(values) == 1 else [head] + [INDENT + v for v in values]
    # 値の続きの行にあった、字下げしたコメントは残す
    new += [doc.lines[i] for i in range(item.start + 1, item.end + 1) if doc.lines[i].strip().startswith(COMMENT)]
    newline = doc.ends[item.start] or doc.newline
    lines = doc.lines[:item.start] + new + doc.lines[item.end + 1:]
    ends = doc.ends[:item.start] + [newline] * (len(new) - 1) + [doc.ends[item.end]] + doc.ends[item.end + 1:]
    return "".join(line + end for line, end in zip(lines, ends))


def verify(doc, item, value, text):
    """書き換えた中身を読み直し、その項目のほかは、値もコメントも並びも変わらないことを確かめる。"""
    after = Doc(doc.path, text)
    want = {k: value if k == item.key else to_value(v) for k, v in doc.values.items()}
    got = {k: to_value(v) for k, v in after.values.items()}
    if got != want or [(i.key, i.comments) for i in after.items] != [(i.key, i.comments) for i in doc.items]:
        raise Stop("設定ファイル %s は、%s の行だけを書き換えることができない書き方になっているため、"
                   "書き換えませんでした。ファイルは元のままです。ファイルを開いて、直接直してください。"
                   % (doc.path, item.key))


def write_atomic(path, data):
    """一時ファイルに書いてから置き換える。途中で失敗しても、元のファイルは残る。"""
    target = os.path.realpath(path)
    mode = stat.S_IMODE(os.stat(target).st_mode)
    if not mode & 0o222:  # Windows は置き換えられず、Linux は置き換えてしまう。どちらでも書かない
        raise Stop("設定ファイル %s は読み取り専用なので、書き換えませんでした。ファイルの権限を確かめてください。"
                   % path, 1)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(target) + ".", suffix=".tmp",
                               dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_set(path, skill, key, workdir, result):
    LOG.open(workdir)
    result["log"] = LOG.path
    LOG("set を始めます。設定ファイル：%s、項目：%s" % (path, key))
    doc = load(path, skill)
    item = doc.find(key)
    values = checked_values(doc, item, read_input())
    before, value = doc.value(item), to_value("\n".join(values))
    result.update(key=item.key, value=shown(item, value), changed=False)
    if value == before:
        result.update(ok=True, message="設定ファイル %s の %s は、今と同じ値なので、書き換えていません。"
                      % (path, item.key))
        return
    text = replaced(doc, item, values)
    verify(doc, item, value, text)
    try:
        put_gitignore(os.path.dirname(path))
        write_atomic(path, (b"\xef\xbb\xbf" if doc.bom else b"") + text.encode("utf-8"))
    except OSError as e:
        raise Stop("設定ファイル %s に書き込めません（%s）。ほかのアプリで開いていないか、フォルダの権限を"
                   "確かめてください。ファイルは元のままです。" % (path, e.strerror or e), 1)
    LOG("%s：%s → %s" % (item.key, json.dumps(shown(item, before), ensure_ascii=False),
                        json.dumps(shown(item, value), ensure_ascii=False)))
    result.update(ok=True, changed=True, message="設定ファイル %s の %s を書き換えました。" % (path, item.key))


# --- 入口 ---------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Stop("コマンドの書き方が違います。" + USAGE)


def parse_args(argv):
    parser = Parser(prog="config_tool.py", add_help=False)
    parser.add_argument("command", choices=("show", "set"))
    parser.add_argument("skill")
    parser.add_argument("key", nargs="?")
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--workdir")
    args = parser.parse_intermixed_args(argv)
    if (args.command == "show") == bool(args.key):
        raise Stop("コマンドの書き方が違います。" + USAGE)
    if args.command == "set" and not args.stdin:
        raise Stop("値は、--stdin を付けて標準入力で渡してください。" + HOW_TO_PASS)
    if not SKILL_NAME_RE.match(args.skill):
        raise Stop("スキルの名前 %s は使えません。スキルのフォルダの名前（英数字とハイフンなど）で指定してください。"
                   % args.skill)
    return args


def main(argv=None):
    result = {"ok": False, "message": "", "problems": [], "config": None, "file": None, "log": None}
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        workdir = os.path.abspath(args.workdir or os.getcwd())
        if not os.path.isdir(workdir):
            raise Stop("作業フォルダ %s がありません。--workdir を確かめてください。" % workdir)
        path = os.path.join(workdir, "." + args.skill, "config.ini")
        result["config"] = result["file"] = path
        if args.command == "show":
            cmd_show(path, args.skill, result)
        else:
            cmd_set(path, args.skill, args.key, workdir, result)
        code = 0
    except Stop as e:
        result.update(ok=False, message=e.message)
        if e.code == 2:
            result["problems"] = [e.message]
        code = e.code
    except Exception as e:
        LOG(traceback.format_exc())
        result.update(ok=False, message="思わぬエラーで止まりました（%s: %s）。" % (type(e).__name__, e))
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
