#!/usr/bin/env python3
"""ほかのスキルの設定ファイル <作業フォルダ>/.<スキルの名前>/config.yaml を読み書きする。

  config_tool.py show <スキルの名前> [--workdir 作業フォルダ]
  config_tool.py set <スキルの名前> <項目> --stdin|--empty [--workdir 作業フォルダ]

show は、項目ごとに、値と、すぐ上のコメントから読んだこと（必須・秘密・例・選べる値）を返す。何も作らない。
set は、標準入力の値を1つの項目に書く（--empty なら空にする）。その項目の行だけを書き換え、コメントと並びは残す。
標準出力には結果の JSON を1つだけ、ASCII で書く。
終了コード：0 成功、1 書き込みの失敗や思わぬエラー、2 設定ファイルや引数の問題。
Python 3.8 以上の標準ライブラリだけで動く。
"""

import argparse
import datetime
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
MASK = "***"  # ほかのスキルと同じ伏せ方
USAGE = ("使い方：config_tool.py show <スキルの名前> [--workdir 作業フォルダ]、"
         "config_tool.py set <スキルの名前> <項目> --stdin|--empty [--workdir 作業フォルダ]")
HOW_TO_PASS = ("bash は「--stdin <<'END'」の次の行から値を書いて END の行で閉じ、"
               "PowerShell は @' と '@ の間に値を書いて、パイプで渡してください。"
               "空にするときは、--stdin の代わりに --empty を付けてください。")
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUIRED_RE = re.compile(r"[（(]必須[）)]")
SECRET_RE = re.compile(r"[（(]秘密[）)]")
# コメントに（秘密）がなくても、名前がパスワードらしい項目は伏せる。_ . - で区切った語で見る（max_tokens は除く）
SECRET_NAME_RE = re.compile(r"(^|[_.-])(pass(word|wd|phrase)?|pwd|secret|token|api_?key|private_?key)([_.-]|$)",
                            re.IGNORECASE)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXAMPLE_RE = re.compile(r"^例\s*[：:]\s*(.*)$")
CHOICES_RE = re.compile(r"^選べる値\s*[：:]\s*(.*)$")
NEWLINE_RE = re.compile(r"(\r\n|\r|\n)")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


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


class Item(object):
    """1つの項目。start から end までが、その項目の行（リストの行を含む）。"""

    def __init__(self, key, value, start, end, comments):
        self.key, self.value = key, to_value(value)
        self.start, self.end = start, end
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
                self.choices = [v.strip() for v in re.split(r"[/／]", choices.group(1)) if v.strip()]
            elif c:
                words.append(c)
        self.comment = "\n".join(words)


def to_value(value):
    """結果の形にする。値が2つ以上ならリスト、1つなら文字列、なければ空の文字列。"""
    if isinstance(value, list):
        return value if len(value) > 1 else (value[0] if value else "")
    return value


class Doc(object):
    """設定ファイルの中身。行と改行を分けて持ち、書くときに元の改行に戻す。"""

    def __init__(self, path, text, bom=False):
        self.path, self.bom = path, bom
        parts = NEWLINE_RE.split(text)
        self.lines, self.ends = parts[0::2], parts[1::2] + [""]
        self.newline = parts[1] if len(parts) > 1 else "\n"
        try:
            entries = read_yaml(self.lines)
        except ValueError as e:
            raise Stop("設定ファイル %s の %s。" % (path, e))
        self.items = []
        for key, value, start, end in entries:
            comments, i = [], start - 1
            while i >= 0 and self.lines[i].startswith("#"):  # 項目のすぐ上に続く、行頭からのコメント
                comments.insert(0, self.lines[i][1:].strip())
                i -= 1
            self.items.append(Item(key, value, start, end, comments))

    def get(self, key):
        return next((i for i in self.items if i.key == key.strip()), None)

    def find(self, key):
        item = self.get(key)
        if item is not None:
            return item
        raise Stop("設定ファイル %s に、項目 %s はありません。ある項目：%s。"
                   % (self.path, key, "、".join(i.key for i in self.items)))


def load(path, skill):
    if not os.path.isfile(path):
        raise Stop("設定ファイル %s がありません。先に %s スキルに、設定ファイルを作るよう依頼してください"
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


def shown(item, value):
    """結果とログに出す値。（秘密）の値は、空でなければ伏せる。"""
    return MASK if item.secret and value else value


# --- show ---------------------------------------------------------------------

def cmd_show(path, skill, result):
    result.update(items=[], missing=[])
    doc = load(path, skill)
    for item in doc.items:
        pair = doc.get(item.key + "_env") if item.secret else None  # （秘密）の値を入れた環境変数の名前を書く項目
        result["items"].append({
            "key": item.key, "value": shown(item, item.value), "required": item.required, "secret": item.secret,
            "examples": item.examples, "choices": item.choices, "comment": item.comment,
            "env_key": pair.key if pair else None})
        # （秘密）の値を環境変数で渡すなら、元の項目は空でよい
        if item.required and not item.value and not (pair and pair.value):
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
    """標準入力の値を、行のリストにする。前後の空白と空の行は除く。値の行がなければ Stop。"""
    stream = getattr(sys.stdin, "buffer", None)
    try:
        waiting = stream is None or stream.isatty()  # 端末なら、入力を待ったまま止まる。待たずに知らせる
    except (OSError, ValueError):
        waiting = True
    text = "" if waiting else decode_input(stream.read())
    lines = [line.strip() for line in re.split(r"\r\n|\r|\n", text) if line.strip()]
    if not lines:  # 渡し忘れを、空にする依頼と取り違えない
        raise Stop("値が標準入力で渡されていません。" + HOW_TO_PASS)
    return lines


def checked_values(doc, item, values):
    """書く値を確かめて返す。書けない値なら Stop。"""
    if item.secret and values:
        hint = "%s_env に環境変数の名前を書くか、" % item.key if doc.get(item.key + "_env") else ""
        raise Stop("%s は（秘密）の項目なので、値はここでは書けません。%s利用者がファイルを開いて書いてください。"
                   "空にするだけなら、--empty でできます。" % (item.key, hint))
    pair = doc.get(item.key[:-4]) if item.key.endswith("_env") else None  # （秘密）の項目と対の <項目>_env
    if pair is not None and pair.secret and values and not (len(values) == 1 and ENV_NAME_RE.match(values[0])):
        # パスワードそのものを書かれたかもしれないので、値は出さない
        raise Stop("%s には、環境変数の名前を1つだけ書いてください（英数字と _。数字では始めない）。"
                   "パスワードそのものは書けません。" % item.key)
    if any(CONTROL_RE.search(v) for v in values):
        raise Stop("値に制御文字が入っています。%s の値を確かめてください。" % item.key)
    if item.choices:
        match = [c for c in item.choices if len(values) == 1 and c.lower() == values[0].lower()]
        if not match:
            raise Stop("%s には、次のどれか1つを書いてください：%s。" % (item.key, " / ".join(item.choices)))
        values = match[:1]
    return values


def yaml_text(value):
    """値を、YAML に書く形にする。そのまま書くと別の意味になる値は、'…' で囲む。"""
    try:
        plain = yaml_scalar(value) == value
    except ValueError:
        plain = False
    return value if plain else "'%s'" % value.replace("'", "''")


def replaced(doc, item, values):
    """その項目の行だけを書き換えた中身を返す。値が1つなら「項目: 値」、2つ以上なら「- 値」のリスト。"""
    head = item.key + ":"
    if len(values) == 1:
        new = [head + " " + yaml_text(values[0])]
    else:
        new = [head] + ["  - " + yaml_text(v) for v in values]
    # リストの中にあったコメントの行は残す
    new += [doc.lines[i] for i in range(item.start + 1, item.end + 1) if doc.lines[i].lstrip().startswith("#")]
    newline = doc.ends[item.start] or doc.newline
    lines = doc.lines[:item.start] + new + doc.lines[item.end + 1:]
    ends = doc.ends[:item.start] + [newline] * (len(new) - 1) + [doc.ends[item.end]] + doc.ends[item.end + 1:]
    return "".join(line + end for line, end in zip(lines, ends))


def verify(doc, item, value, text):
    """書き換えた中身を読み直し、その項目のほかは、値もコメントも並びも変わらないことを確かめる。"""
    want = [(i.key, value if i is item else i.value, i.comments) for i in doc.items]
    if [(i.key, i.value, i.comments) for i in Doc(doc.path, text).items] != want:
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


def cmd_set(path, skill, key, empty, workdir, result):
    LOG.open(workdir)
    result["log"] = LOG.path
    LOG("set を始めます。設定ファイル：%s、項目：%s" % (path, key))
    doc = load(path, skill)
    item = doc.find(key)
    values = checked_values(doc, item, [] if empty else read_input())
    before, value = item.value, to_value(values)
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
    parser.add_argument("--empty", action="store_true")
    parser.add_argument("--workdir")
    args = parser.parse_intermixed_args(argv)
    if (args.command == "show") == bool(args.key):
        raise Stop("コマンドの書き方が違います。" + USAGE)
    if args.command == "set" and args.stdin == args.empty:
        raise Stop("set には、--stdin か --empty のどちらか1つだけを付けてください。" + HOW_TO_PASS)
    if not SKILL_NAME_RE.match(args.skill):
        raise Stop("スキルの名前 %s は使えません。スキルのフォルダの名前（英数字とハイフンなど）で指定してください。"
                   % args.skill)
    return args


def main(argv=None):
    result = {"ok": False, "message": "", "problems": [], "config": None, "log": None}
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        workdir = os.path.abspath(args.workdir or os.getcwd())
        if not os.path.isdir(workdir):
            raise Stop("作業フォルダ %s がありません。--workdir を確かめてください。" % workdir)
        path = os.path.join(workdir, "." + args.skill, "config.yaml")
        result["config"] = path
        if args.command == "show":
            cmd_show(path, args.skill, result)
        else:
            cmd_set(path, args.skill, args.key, args.empty, workdir, result)
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
