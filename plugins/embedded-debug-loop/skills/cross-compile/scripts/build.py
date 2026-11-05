#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""機器で動かすプログラムを、PC でビルドする（クロスコンパイル）。

使い方：
  <python> build.py [run | config-path | check] [--workdir フォルダ] [--config ファイル]
  （<python> は、Windows では py -3 か python、それ以外では python3）

  run          ビルドする（何も書かないときも run）
  config-path  設定ファイルの場所を出す。無ければひな形から作る
  check        設定を確かめる（ビルドはしない）

結果は、標準出力に JSON を1つだけ出す。途中の様子は標準エラー出力に出す。
どちらも Python の既定の文字コードで書く。その文字コードにない文字は、JSON では \\uXXXX にする。
終了コード：run は 0＝成功、1＝ビルドの失敗、2＝設定の問題。
            check は 0＝問題なし、2＝問題あり。config-path は 0。

コマンドは、設定の shell で選んだシェル（cmd・powershell・bash）で動かす。
  1. sdk_env があれば、そのシェルで読み込む（cmd は call、powershell は .、bash は source）。
     読み込んだ後の環境変数は、Python に JSON で書き出させて受け取る（コンパイラーを調べるため）。
  2. clean_command、build_command の順に、ビルドするフォルダの中で動かす。
     powershell と bash は、同じシェルの中で SDK を読み込み直してから動かす（SDK の関数も使える）。
     cmd は、1 で受け取った環境変数で動かす（cmd の SDK が決められるのは、環境変数だけ）。
時間切れのときは、子のプロセスまで止める（Windows は taskkill /T /F、それ以外はプロセスグループ）。
Windows の bash は、Git for Windows などの bash を使う（WSL を起動する bash.exe は使わない）。

設定とログは、作業フォルダの「.<このスキルのフォルダの名前>」フォルダに置く。
  config.yaml  設定ファイル（無ければ config-template.yaml から作る）
  logs/        ログ（いつも UTF-8。ビルドの出力が UTF-8 でなければ、OS の既定の文字コードとして読む）
  .gitignore   中身は *（設定やログを git に入れないため）
作業フォルダは、今のフォルダ（--workdir で変えられる）。
設定ファイルの場所は、--config で変えられる。

Python 3.8 以降の標準ライブラリだけで動く。Windows でも Linux でも動く。
"""

import datetime
import json
import locale
import ntpath
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
# スキルの名前は、このスキルのフォルダの名前（名前を変えてコピーしたら、その名前）
SKILL = os.path.basename(SKILL_DIR)
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")
DATA_DIR_NAME = "." + SKILL  # 作業フォルダの中の、設定とログのフォルダ
CONFIG_FILE_NAME = "config.yaml"
LOGS_DIR_NAME = "logs"
COMMANDS = ("run", "config-path", "check")
IS_WINDOWS = os.name == "nt"

REQUIRED_KEYS = ("outputs",)
# 必須ではないが、機器向けにビルドするなら書く項目
RECOMMENDED_KEYS = ("sdk_env",)
GITIGNORE_TEXT = "*\n"  # 設定やログが、誤って git に入らないようにする
LABELS = {
    "source_dir": "ビルドするフォルダ",
    "shell": "コマンドを動かすシェル",
    "sdk_env": "SDK の環境設定ファイル",
    "build_command": "ビルドのコマンド",
    "clean_command": "片付けのコマンド",
    "outputs": "ビルドでできるファイル",
    "timeout_seconds": "ビルドを待つ最長の秒数",
}
DEFAULT_BUILD_COMMAND = "make"
DEFAULT_TIMEOUT = 600
MAX_ERROR_LINES = 20
MAX_TAIL_LINES = 30
PROBE_TIMEOUT = 60
MARK = "==> "  # ビルドの出力に、区切りとして書く行の頭

SHELLS = ("auto", "cmd", "powershell", "bash")
# sdk_env のファイルの拡張子（cmd と powershell は、これ以外を読み込めない）
SDK_SUFFIXES = {"cmd": (".bat", ".cmd"), "powershell": (".ps1",)}
# 「コマンドが見つからない」ときの終了コード（powershell は、下の PS_RUN が 127 にする）
NOT_FOUND_CODES = {"bash": (127,), "powershell": (127,), "cmd": (9009,)}
DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
STEP_MARKS = {
    "sdk_env": "SDK の環境設定ファイルを読み込みます",
    "clean": "片付けのコマンドを実行します",
    "build": "ビルドのコマンドを実行します",
}

USAGE = (
    "使い方：<python> build.py [run | config-path | check] "
    "[--workdir 作業フォルダ] [--config 設定ファイル]"
)


def label(key):
    return "{}（{}）".format(key, LABELS.get(key, key))


def show_value(value):
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# YAML の一部だけを読む（名前: 値 の並び、コメント、文字・数・真偽・null・リスト）
# ---------------------------------------------------------------------------

class YamlError(Exception):
    def __init__(self, line_no, reason):
        Exception.__init__(self, "{} 行目：{}".format(line_no, reason))
        self.line_no = line_no
        self.reason = reason


KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]+(.*))?$")
ITEM_RE = re.compile(r"^[ \t]*-(?:[ \t]+(.*))?$")
INT_RE = re.compile(r"^[-+]?[0-9]+$")
FLOAT_RE = re.compile(r"^[-+]?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][-+]?[0-9]+)?$")


def _read_quoted(s, i, line_no):
    """s[i] の引用符から始まる文字を読み、(値, 次の位置) を返す。"""
    quote = s[i]
    if quote == '"':
        j = i + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == '"':
                try:
                    return json.loads(s[i:j + 1]), j + 1
                except ValueError:
                    raise YamlError(
                        line_no, "「\"」で囲んだ文字の書き方が正しくありません。"
                        "「\\」は「\\\\」と書くか、全体を「'」で囲んでください。")
            j += 1
        raise YamlError(line_no, "「\"」が閉じていません。")
    parts = []
    j = i + 1
    while True:
        k = s.find("'", j)
        if k < 0:
            raise YamlError(line_no, "「'」が閉じていません。")
        if s[k + 1:k + 2] == "'":
            parts.append(s[j:k] + "'")
            j = k + 2
            continue
        parts.append(s[j:k])
        return "".join(parts), k + 1


def _plain(text, line_no):
    t = text.strip()
    if t in ("", "~", "null", "Null", "NULL"):
        return None
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if INT_RE.match(t):
        return int(t)
    if FLOAT_RE.match(t):
        return float(t)
    if t[0] in "{&*!|>@`":
        raise YamlError(line_no, "この書き方（{} で始まる値）には対応していません。".format(t[0]))
    return t


def _strip_comment(text):
    """引用符の外の「 #」から後ろをコメントとして消す。"""
    m = re.search(r"(^|[ \t])#", text)
    return text[:m.start()] if m else text


def _check_rest(rest, line_no):
    rest = rest.strip()
    if rest and not rest.startswith("#"):
        raise YamlError(line_no, "値の後ろに、余計な文字があります：{}".format(rest))


def _flow_list(s, line_no):
    """s は「[」で始まる。(リスト, 次の位置) を返す。"""
    items = []
    i = 1
    n = len(s)
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            raise YamlError(line_no, "「[」が閉じていません。")
        if s[i] == "]":
            return items, i + 1
        if s[i] in "[{":
            raise YamlError(line_no, "リストの中のリストには対応していません。")
        if s[i] == ",":
            raise YamlError(line_no, "リストの中に、空の要素があります。")
        if s[i] in "\"'":
            value, i = _read_quoted(s, i, line_no)
        else:
            j = i
            while j < n and s[j] not in ",]":
                j += 1
            value = _plain(s[i:j], line_no)
            i = j
        items.append(value)
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] == ",":
            i += 1
        elif i < n and s[i] == "]":
            continue
        elif i >= n:
            raise YamlError(line_no, "「[」が閉じていません。")
        else:
            raise YamlError(line_no, "リストの区切り「,」がありません。")


def parse_scalar(raw, line_no):
    s = raw.strip()
    if not s:
        return None
    if s[0] in "\"'":
        value, end = _read_quoted(s, 0, line_no)
        _check_rest(s[end:], line_no)
        return value
    if s[0] == "[":
        value, end = _flow_list(s, line_no)
        _check_rest(s[end:], line_no)
        return value
    return _plain(_strip_comment(s), line_no)


def parse_yaml(text):
    """名前と値の dict を返す。読めないときは YamlError。"""
    data = {}
    first_line = {}
    list_key = None
    for line_no, line in enumerate(text.splitlines(), 1):
        if line_no == 1:
            line = line.lstrip("\ufeff")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "---" and not data:
            continue
        m = ITEM_RE.match(line)
        if m and list_key is not None:
            if data[list_key] is None:
                data[list_key] = []
            data[list_key].append(parse_scalar(m.group(1) or "", line_no))
            continue
        if line[0] in " \t":
            raise YamlError(line_no, "字下げした行は、「- 」で始まるリストの行にだけ使えます。")
        m = KEY_RE.match(line)
        if not m:
            raise YamlError(line_no, "「名前: 値」の形になっていません。")
        key = m.group(1)
        raw = m.group(2) or ""
        if key in first_line:
            raise YamlError(
                line_no, "{} が {} 行目にも書かれています。".format(key, first_line[key]))
        first_line[key] = line_no
        value = parse_scalar(raw, line_no)
        data[key] = value
        list_key = key if value is None else None
    return data


# ---------------------------------------------------------------------------
# 設定ファイル
# ---------------------------------------------------------------------------

class UsageError(Exception):
    pass


class SettingsProblem(Exception):
    def __init__(self, problems, config=None):
        Exception.__init__(self, " ".join(problems))
        self.problems = problems
        self.config = config


def problems_message(problems):
    if len(problems) == 1:
        return problems[0]
    return "問題が {} 件あります。1つ目：{}".format(len(problems), problems[0])


def parse_args(argv):
    opts = {"command": None, "workdir": None, "config": None, "help": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            opts["help"] = True
            i += 1
            continue
        name = None
        value = None
        for opt in ("--workdir", "--config"):
            if a == opt:
                if i + 1 >= len(argv):
                    raise UsageError("{} の後ろに、パスを書いてください。".format(opt))
                name, value = opt, argv[i + 1]
                i += 2
                break
            if a.startswith(opt + "="):
                name, value = opt, a[len(opt) + 1:]
                i += 1
                break
        if name:
            if not value:
                raise UsageError("{} の後ろに、パスを書いてください。".format(name))
            opts[name[2:]] = value
            continue
        if a in COMMANDS and opts["command"] is None:
            opts["command"] = a
            i += 1
            continue
        raise UsageError("「{}」は使えません。{}".format(a, USAGE))
    if opts["command"] is None:
        opts["command"] = "run"
    return opts


def resolve_paths(opts):
    workdir = os.path.abspath(os.path.expanduser(opts["workdir"] or os.getcwd()))
    if not os.path.isdir(workdir):
        raise SettingsProblem(["作業フォルダ {} がありません。".format(workdir)])
    if opts["config"]:
        config = os.path.abspath(os.path.expanduser(opts["config"]))
    else:
        config = os.path.join(workdir, DATA_DIR_NAME, CONFIG_FILE_NAME)
    return workdir, config


def _data_dir_of(path):
    """path か、その上にある、設定とログのフォルダの場所。無ければ None。"""
    p = os.path.abspath(path)
    while True:
        if os.path.basename(p) == DATA_DIR_NAME:
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def ensure_gitignore(path):
    """path か、その上にある、設定とログのフォルダに .gitignore が無ければ作る
    （中身は * の1行）。すでにあれば、そのままにする。作れなくても止めない。"""
    top = _data_dir_of(path)
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
    """フォルダを作る。設定とログのフォルダに .gitignore が無ければ、それも作る。"""
    os.makedirs(path, exist_ok=True)
    ensure_gitignore(path)


def template_blocks(text):
    """ひな形の項目ごとに (名前, 行の並び) を返す。
    行の並びは、項目のすぐ上のコメントと、項目の行（「- 値」の行が続けば、それも）。"""
    lines = text.splitlines()
    if lines:
        lines[0] = lines[0].lstrip("\ufeff")
    blocks = []
    i = 0
    while i < len(lines):
        m = KEY_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        end = i + 1
        while end < len(lines) and ITEM_RE.match(lines[end]):
            end += 1
        blocks.append((m.group(1), lines[start:end]))
        i = end
    return blocks


def add_missing_keys(path):
    """設定ファイルに、ひな形にある項目が無ければ、説明のコメントと一緒にファイルの最後に足す。
    すでにある所は1バイトも変えない。足した項目の名前の並びを返す。
    ファイルが読めないときや、書き方の誤りがあるときは、何も足さない。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        have = parse_yaml(data.decode("utf-8"))
        with open(TEMPLATE, encoding="utf-8") as f:
            blocks = template_blocks(f.read())
    except (OSError, UnicodeDecodeError, YamlError):
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
    return [key for key, _ in missing]


def added_sentence(added):
    return "設定ファイルに、新しい項目 {} を足しました（今の値はひな形のまま）。".format("、".join(added))


def open_config(opts, workdir, config):
    """設定ファイルを用意する。無ければひな形から作り、項目が足りなければ足す。
    設定とログのフォルダに .gitignore が無ければ作る。作ったら True。
    足した項目の名前は opts["added"] に入れる。"""
    ensure_gitignore(os.path.join(workdir, DATA_DIR_NAME))
    created = ensure_config(config)
    ensure_gitignore(config)
    opts["added"] = [] if created else add_missing_keys(config)
    return created


def ensure_config(path):
    """設定ファイルが無ければ、ひな形をコピーして作る。作ったら True。"""
    if os.path.exists(path):
        if os.path.isdir(path):
            raise SettingsProblem(["設定ファイルの場所 {} は、フォルダです。".format(path)], path)
        return False
    if not os.path.isfile(TEMPLATE):
        raise SettingsProblem([
            "設定のひな形 {} がありません。スキルのフォルダをコピーし直してください。".format(TEMPLATE)],
            path)
    try:
        make_dirs(os.path.dirname(path))
        shutil.copyfile(TEMPLATE, path)
    except OSError as e:
        raise SettingsProblem([
            "設定ファイル {} を作れません（{}）。フォルダに書き込めるかを確かめてください。".format(
                path, e.strerror or e)], path)
    return True


def read_config(path):
    """(名前と値の dict, 問題のリスト) を返す。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        return {}, ["設定ファイル {} が UTF-8 で書かれていません。".format(path)]
    except OSError as e:
        return {}, ["設定ファイル {} が読めません（{}）。".format(path, e.strerror or e)]
    try:
        return parse_yaml(text), []
    except YamlError as e:
        return {}, ["設定ファイル {} の {} 行目が読めません。{}".format(
            path, e.line_no, e.reason)]


def is_empty(value):
    return value is None or value == "" or value == []


def missing_required(raw):
    return [k for k in REQUIRED_KEYS if is_empty(raw.get(k))]


def empty_recommended(raw):
    return [k for k in RECOMMENDED_KEYS if is_empty(raw.get(k))]


def sdk_note(compiler=None, when="plan", cc_from_env=False, other_arch=None):
    """sdk_env が空のときの注意の文。

    when は "plan"（これからビルドする）、"built"（できた）、"failed"（失敗した）。
    """
    done = when != "plan"
    head = "{}が空なので、SDK を読み込{}。".format(
        label("sdk_env"), "みませんでした" if done else "みません")
    tail = "機器向けにビルドするなら、sdk_env を書いてください。"
    if other_arch:
        # PC とは違う種類のファイルができた（Makefile などでコンパイラーを決めている）
        return head + "できたファイルの種類は {} です。".format(other_arch)
    if not compiler and not cc_from_env:
        # PC に cc がない（Windows など）。WSL の中でビルドすることもあるので、何用かは言い切らない
        if when == "failed":
            return head
        return head + "機器向けかは、できたファイルの種類で確認してください。"
    if cc_from_env:
        cc = "環境変数 CC のコンパイラー（{}）".format(compiler)
    else:
        cc = "PC のコンパイラー" + ("（{}）".format(compiler) if compiler else "")
    if when == "plan":
        body = "{}でビルドします。".format(cc)
        if not cc_from_env:
            body += "できるのは PC 用のプログラムで、機器用ではありません。"
    elif when == "built":
        body = "{}でビルドしました。".format(cc)
        if not cc_from_env:
            body += "できたのは PC 用のプログラムで、機器用ではありません。"
    else:
        body = "{}でビルドしました。".format(cc)
    return head + body + tail


def _text(raw, key, problems):
    value = raw.get(key)
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, list):
        problems.append("{}は文字で書いてください。今の値：{}".format(label(key), show_value(value)))
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return value.strip()


def _has_control(text):
    return any(ord(c) < 0x20 or c == "\x7f" for c in text)


def _control_problem(key, value):
    # "C:\new\app" のように「"」で囲むと、\n が改行として読まれる
    return ("{}に、タブや改行が入っています（今の値：{}）。Windows のパスは、"
            "「/」で区切るか、全体を「'」で囲んでください。".format(label(key), show_value(value)))


def _abs(workdir, path):
    return os.path.normpath(os.path.join(workdir, os.path.expanduser(path)))


def resolve_shell(setting, windows=IS_WINDOWS):
    """設定の shell の値から、使うシェルの名前（cmd・powershell・bash）を決める。"""
    if setting == "auto":
        return "cmd" if windows else "bash"
    return setting


def is_wsl_bash(path, system_root):
    """Windows の bash.exe が、WSL を起動するもの（System32 か WindowsApps にある）か。
    WSL の bash には、環境変数も Windows のパスも渡らないので、使わない。"""
    folder = ntpath.normcase(ntpath.dirname(path))
    if folder == ntpath.normcase(ntpath.join(system_root, "System32")):
        return True
    return ntpath.basename(folder) == "windowsapps"


def find_windows_bash(environ=None, isfile=os.path.isfile):
    """Windows で使う bash を探す。(使える bash, 見つかった WSL の bash) を返す。
    PATH の順に探し、無ければ Git for Windows を入れた場所を探す（ふつう、PATH には入らない）。"""
    environ = os.environ if environ is None else environ
    root = environ.get("SystemRoot") or environ.get("windir") or r"C:\Windows"
    places = [d.strip().strip('"') for d in (environ.get("PATH") or "").split(";")]
    candidates = [ntpath.join(d, "bash.exe") for d in places if d]
    for name, sub in (("ProgramW6432", ("Git",)), ("ProgramFiles", ("Git",)),
                      ("LOCALAPPDATA", ("Programs", "Git"))):
        base = environ.get(name)
        if base:
            candidates.append(ntpath.join(base, *(sub + ("bin", "bash.exe"))))
    wsl = None
    for path in candidates:
        if not isfile(path):
            continue
        if is_wsl_bash(path, root):
            wsl = wsl or path
            continue
        return path, wsl
    return None, wsl


def find_shell(name):
    """シェルの実行ファイルの場所。見つからなければ None。"""
    if name == "cmd":
        if not IS_WINDOWS:
            return None
        comspec = os.environ.get("ComSpec") or ""
        if comspec and os.path.isfile(comspec):
            return comspec
        return shutil.which("cmd.exe") or shutil.which("cmd")
    if name == "powershell":
        return shutil.which("pwsh") or shutil.which("powershell")
    if IS_WINDOWS:
        return find_windows_bash()[0]
    return shutil.which("bash")


def shell_missing_message(name, windows=IS_WINDOWS, wsl=None):
    """シェルが見つからないときの文。wsl は、Windows で見つかった WSL の bash の場所。"""
    if name == "cmd":
        return ("PC に cmd が見つかりません。cmd は Windows のシェルです。"
                "{}を auto か bash にしてください。".format(label("shell")))
    if name == "powershell":
        return ("PC に PowerShell（pwsh か powershell）が見つかりません。"
                "PowerShell をインストールするか、{}を別のシェルにしてください。".format(label("shell")))
    if windows and wsl:
        return ("{} は WSL の bash なので、使えません。Git for Windows を入れるか、"
                "{}を cmd か powershell にしてください。"
                "WSL の中でビルドするなら、build_command に wsl で始まるコマンドを書きます。".format(
                    wsl, label("shell")))
    if windows:
        return ("PC に bash（Git for Windows の bash）が見つかりません。Git for Windows を入れるか、"
                "{}を別のシェルにしてください。".format(label("shell")))
    return ("PC に bash が見つかりません。bash をインストールするか、"
            "{}を別のシェルにしてください。".format(label("shell")))


def normalize(raw, workdir):
    """設定の値をそろえ、(設定, 問題のリスト) を返す。ファイルの有無も見る。

    設定の runnable は、シェルを動かしてよいか（フォルダ・SDK のファイル・シェルがそろっている）。
    """
    problems = []
    s = {}
    runnable = True

    src = _text(raw, "source_dir", problems) or "."
    s["source_dir"] = _abs(workdir, src)

    setting = (_text(raw, "shell", problems) or "auto").lower()
    if setting not in SHELLS:
        problems.append("{}は auto・cmd・powershell・bash のどれかで書いてください。今の値：{}".format(
            label("shell"), show_value(raw.get("shell"))))
        setting = "auto"
        runnable = False
    s["shell_setting"] = setting
    s["shell"] = resolve_shell(setting)
    s["shell_program"] = find_shell(s["shell"])
    if not s["shell_program"]:
        wsl = find_windows_bash()[1] if IS_WINDOWS and s["shell"] == "bash" else None
        problems.append(shell_missing_message(s["shell"], wsl=wsl))
        runnable = False

    if _has_control(src):
        problems.append(_control_problem("source_dir", src))
        runnable = False
    elif s["shell"] == "cmd" and s["source_dir"].startswith("\\\\"):
        # cmd は「\\」で始まるフォルダに移れず、Windows のフォルダでコマンドを動かしてしまう
        problems.append("{}の {} は「\\\\」で始まるので、cmd では使えません。"
                        "{}を powershell にしてください。".format(
                            label("source_dir"), s["source_dir"], label("shell")))
        runnable = False
    elif not os.path.exists(s["source_dir"]):
        problems.append("{}の {} がありません。".format(label("source_dir"), s["source_dir"]))
        runnable = False
    elif not os.path.isdir(s["source_dir"]):
        problems.append("{}の {} は、フォルダではありません。".format(
            label("source_dir"), s["source_dir"]))
        runnable = False

    sdk = _text(raw, "sdk_env", problems)
    s["sdk_env"] = _abs(workdir, sdk) if sdk else ""
    if sdk:
        suffixes = SDK_SUFFIXES.get(s["shell"])
        if _has_control(sdk):
            problems.append(_control_problem("sdk_env", sdk))
            runnable = False
        elif not os.path.exists(s["sdk_env"]):
            problems.append(
                "{}の {} がありません。SDK を入れた場所と、ファイルの名前を確かめてください。".format(
                    label("sdk_env"), s["sdk_env"]))
            runnable = False
        elif not os.path.isfile(s["sdk_env"]):
            problems.append("{}の {} は、ファイルではありません。".format(
                label("sdk_env"), s["sdk_env"]))
            runnable = False
        elif suffixes and not s["sdk_env"].lower().endswith(suffixes):
            # cmd や PowerShell に、ほかの種類のファイル（Linux のシェルスクリプトなど）を
            # 読み込ませると、関連付けのアプリが開くことがある。動かす前に止める。
            problems.append(
                "{}が {} のときは、{}を {} のファイルにしてください。今の値：{}。"
                "Linux の SDK を WSL で使うときは、sdk_env を空にして、"
                "build_command に wsl で始まるコマンドを書きます。".format(
                    label("shell"), s["shell"], label("sdk_env"), "・".join(suffixes),
                    s["sdk_env"]))
            runnable = False

    s["build_command"] = _text(raw, "build_command", problems) or DEFAULT_BUILD_COMMAND
    s["clean_command"] = _text(raw, "clean_command", problems)

    outputs = []
    value = raw.get("outputs")
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if value is None:
        value = []
    if isinstance(value, list):
        control = False
        for item in value:
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            if isinstance(item, (bool, list)):
                problems.append("{}の中は、ファイルの名前（文字）で書いてください。今の値：{}".format(
                    label("outputs"), show_value(item)))
                continue
            item = str(item).strip()
            if _has_control(item):
                problems.append(_control_problem("outputs", item))
                control = True
                continue
            outputs.append(item)
        if not outputs and not control:
            problems.append(
                "{}が空です（必須）。ビルドでできるファイルの名前を書いてください"
                "（例：[\"my_app\"]）。".format(label("outputs")))
    else:
        problems.append("{}はリストで書いてください（例：[\"my_app\"]）。今の値：{}".format(
            label("outputs"), show_value(value)))
    s["outputs"] = outputs

    value = raw.get("timeout_seconds")
    timeout = DEFAULT_TIMEOUT
    bad = False
    if is_empty(value):
        timeout = DEFAULT_TIMEOUT
    elif isinstance(value, bool) or isinstance(value, list):
        bad = True
    elif isinstance(value, (int, float)):
        timeout = value
    elif isinstance(value, str) and FLOAT_RE.match(value.strip()):
        timeout = float(value.strip())
    else:
        bad = True
    if bad or timeout < 1:
        problems.append("{}は、1 以上の数で書いてください。今の値：{}".format(
            label("timeout_seconds"), show_value(value)))
        timeout = DEFAULT_TIMEOUT
    if isinstance(timeout, float) and timeout.is_integer():
        timeout = int(timeout)
    s["timeout_seconds"] = timeout
    s["runnable"] = runnable
    return s, problems


def load(opts):
    """作業フォルダ、設定ファイル、作ったか、設定、問題のリスト を返す。"""
    workdir, config = resolve_paths(opts)
    created = open_config(opts, workdir, config)
    raw, problems = read_config(config)
    settings = None
    if not problems:
        settings, problems = normalize(raw, workdir)
    return workdir, config, created, raw, settings, problems


def summary_of(settings):
    return {
        "source_dir": settings["source_dir"],
        "shell": settings["shell_setting"],
        "sdk_env": settings["sdk_env"],
        "build_command": settings["build_command"],
        "clean_command": settings["clean_command"],
        "outputs": settings["outputs"],
        "timeout_seconds": settings["timeout_seconds"],
        "shell_program": settings["shell_program"],
    }


# ---------------------------------------------------------------------------
# 出力と記録
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


def setup_streams():
    """標準出力と標準エラー出力を、読む側の文字コードに合わせ、書けない文字で止まらないようにする。

    Windows で、出力先がコンソールでない（パイプなど）ときは、コンソールの文字コードで書く。
    PowerShell は、この文字コードで読む。Python 3.15 からの既定（UTF-8 モード）のままだと、
    日本語の Windows では文字化けする。
    """
    encoding = console_code_page()
    for stream in (sys.stdout, sys.stderr):
        try:
            if encoding and not stream.isatty():
                stream.reconfigure(encoding=encoding, errors="backslashreplace")
            else:
                stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def json_text_for(text, encoding):
    """JSON の文字のうち、encoding で書けない文字を \\uXXXX にする（JSON として正しいまま）。"""
    if not encoding:
        return text
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        pass
    except LookupError:
        return text
    out = []
    for ch in text:
        try:
            ch.encode(encoding)
            out.append(ch)
        except UnicodeEncodeError:
            # JSON の中で ASCII でない文字は、文字列の中にしか出ない
            out.append(json.dumps(ch)[1:-1])
    return "".join(out)


def progress(text):
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def new_log_path(workdir):
    logs = os.path.join(workdir, DATA_DIR_NAME, LOGS_DIR_NAME)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        make_dirs(logs)
        n = 1
        while True:
            name = "{}-{}{}.log".format(SKILL, stamp, "" if n == 1 else "-{}".format(n))
            path = os.path.join(logs, name)
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                return path
            except FileExistsError:
                n += 1
    except OSError as e:
        raise SettingsProblem([
            "ログのフォルダ {} に書き込めません（{}）。作業フォルダの権限を確かめてください。".format(
                logs, e.strerror or e)])


def append_log(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def fallback_encoding():
    """ビルドの出力が UTF-8 でないときに使う、OS の既定の文字コード。"""
    try:
        enc = locale.getpreferredencoding(False) or "utf-8"
        "".encode(enc)
        return enc
    except (LookupError, ValueError):
        return "utf-8"


def decode_output(data, fallback=None):
    """ビルドの出力（バイト列）を、行ごとに UTF-8 で読む。読めない行は、OS の既定の文字コードで読む。"""
    fallback = fallback or fallback_encoding()
    parts = []
    for chunk in data.splitlines(True):
        try:
            parts.append(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            parts.append(chunk.decode(fallback, "replace"))
    return "".join(parts)


def recode_log(log_path, start):
    """ログの start から後ろ（子のプロセスが書いた所）を UTF-8 にそろえ、その文字列を返す。"""
    try:
        with open(log_path, "r+b") as f:
            f.seek(start)
            data = f.read()
            text = decode_output(data)
            new = text.encode("utf-8")
            if new != data:
                f.seek(start)
                f.truncate()
                f.write(new)
        return text
    except OSError:
        return ""


ERROR_FLAG_RE = re.compile(r"(?<!\S)-[A-Za-z][\w,=+.-]*error[\w,=+.-]*", re.IGNORECASE)
# リンクのエラー（undefined reference など）は、行に error が無いので足す
ERROR_RE = re.compile(r"error|エラー|undefined reference|multiple definition", re.IGNORECASE)
WARNING_RE = re.compile(r"warning|警告", re.IGNORECASE)
WARNING_FLAG_RE = re.compile(r"(?<!\S)-[A-Za-z][\w,=+.-]*warning[\w,=+.-]*", re.IGNORECASE)


def pick_lines(lines, pattern, flag_pattern, limit):
    picked = []
    for line in lines:
        if line.startswith(MARK):
            continue
        if pattern.search(flag_pattern.sub("", line)):
            picked.append(line.strip())
            if len(picked) >= limit:
                break
    return picked


def error_lines_of(lines):
    return pick_lines(lines, ERROR_RE, ERROR_FLAG_RE, MAX_ERROR_LINES)


def warning_lines_of(lines):
    return pick_lines(lines, WARNING_RE, WARNING_FLAG_RE, MAX_ERROR_LINES)


def describe_file(path):
    exe = shutil.which("file")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-b", path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=10)
        text = r.stdout.decode("utf-8", "replace").strip()
        return text or None
    except Exception:
        return None


# ELF の e_machine の番号と、その呼び名（file コマンドと同じ呼び名）
ELF_MACHINES = {
    2: "SPARC", 3: "Intel 80386", 8: "MIPS", 20: "PowerPC", 21: "64-bit PowerPC",
    22: "IBM S/390", 40: "ARM", 42: "Renesas SH", 43: "SPARC V9", 62: "x86-64",
    183: "ARM aarch64", 243: "UCB RISC-V", 258: "LoongArch",
}
# platform.machine() の値と、ELF の呼び名（Python の実行ファイルが ELF でない PC のため）
MACHINE_NAMES = {
    "amd64": "x86-64", "x86_64": "x86-64", "x64": "x86-64",
    "arm64": "ARM aarch64", "aarch64": "ARM aarch64",
    "x86": "Intel 80386", "i386": "Intel 80386", "i686": "Intel 80386",
}


def elf_arch(path):
    """ELF のファイルなら、どの CPU 向けか（例：ARM、x86-64）。ELF でなければ None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[:4] != b"\x7fELF" or head[5] not in (1, 2):
        return None
    order = "little" if head[5] == 1 else "big"
    machine = int.from_bytes(head[18:20], order)
    return ELF_MACHINES.get(machine, "CPU の番号 {}".format(machine))


def kind_of(arch, file_type):
    """メッセージに書く、ファイルの種類の短い呼び名。"""
    if arch:
        return arch
    if file_type:
        return short(file_type.split(",")[0], 40)
    return "不明"


def pc_arch(machine=None):
    """この PC の CPU の呼び名。Python の実行ファイルが ELF ならそこから、
    そうでなければ（Windows など）platform.machine() から調べる。"""
    if machine is None:
        arch = elf_arch(os.path.realpath(sys.executable))
        if arch:
            return arch
        machine = platform.machine()
    return MACHINE_NAMES.get((machine or "").lower())


def short(text, limit=160):
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------------------
# シェルでコマンドを動かす
# ---------------------------------------------------------------------------

ENV_PREFIX = "BUILD_SKILL_"  # シェルに値を渡す環境変数の頭（受け取った環境変数からは外す）

# 環境変数を JSON のファイルに書く Python のコード。cmd の「"…"」の中に置くので、
# 「"」「%」「^」「&」「|」「<」「>」「!」を使わない。
DUMP_ENV = ("import json,os,sys;f=open(sys.argv[1],'w',encoding='utf-8');"
            "json.dump(dict(os.environ),f);f.close()")

# bash：SDK を読み込み、BUILD_SKILL_WORDS（1行に1つ）のコマンドの有無を command -v で調べて
# BUILD_SKILL_FOUND に書き、環境変数を書き出す。読み込みが失敗したら、その終了コードで終わる。
# SDK が決めた関数も見つかるよう、読み込んだのと同じシェルで調べる。
BASH_LOAD = (
    'if [ -n "${BUILD_SKILL_SDK_ENV-}" ]; then\n'
    '  source "$BUILD_SKILL_SDK_ENV" || exit $?\n'
    '  cd "$BUILD_SKILL_DIR" || exit $?\n'
    'fi\n'
    'BUILD_SKILL_FOUND=$(printf \'%s\\n\' "${BUILD_SKILL_WORDS-}" | while IFS= read -r w; do\n'
    '  [ -n "$w" ] || continue\n'
    '  if command -v -- "$w" >/dev/null 2>&1; then echo "1 $w"; else echo "0 $w"; fi\n'
    'done)\n'
    'export BUILD_SKILL_FOUND\n'
    'exec "$BUILD_SKILL_PYTHON" -I -c "$BUILD_SKILL_DUMP" "$BUILD_SKILL_ENV_OUT"\n'
)

# bash：コマンドの前に、同じシェルで SDK を読み込み直す（出力は捨てる）。
# 読み込めたら BUILD_SKILL_LOADED のファイルを作る。無ければ、読み込み直しの失敗とみなす。
# 「cd sub && make | tee x」の途中の失敗も、失敗として返す。
BASH_RUN = (
    'if [ -n "${BUILD_SKILL_SDK_ENV-}" ]; then\n'
    '  source "$BUILD_SKILL_SDK_ENV" >/dev/null 2>&1 || exit $?\n'
    '  cd "$BUILD_SKILL_DIR" || exit $?\n'
    '  : > "$BUILD_SKILL_LOADED"\n'
    'fi\n'
    'set -e\n'
    'set -o pipefail\n'
)

# cmd のバッチファイルは、コードページの違いで文字化けしないよう、ASCII だけで書く。
# 値はすべて環境変数で渡す。
CMD_LOAD = "\r\n".join([
    "@echo off",
    "if not defined BUILD_SKILL_SDK_ENV goto dump",
    'call "%BUILD_SKILL_SDK_ENV%"',
    'if not "%ERRORLEVEL%"=="0" exit /b %ERRORLEVEL%',
    ":dump",
    '"%BUILD_SKILL_PYTHON%" -I -c "%BUILD_SKILL_DUMP%" "%BUILD_SKILL_ENV_OUT%"',
    "exit /b %ERRORLEVEL%",
    "",
])

# PowerShell のスクリプトは、ファイルにせず -Command で渡す（実行ポリシーがかからない）。
# コマンドラインで崩れないよう、「"」と「\」を使わない。

# PowerShell：SDK を「.」で読み込む。例外か、0 でない $LASTEXITCODE は失敗。
# BUILD_SKILL_WORDS（1行に1つ）のコマンドの有無を Get-Command で調べ、BUILD_SKILL_FOUND に書く。
PS_LOAD = "\n".join([
    "$ErrorActionPreference = 'Continue'",
    "if ($env:BUILD_SKILL_SDK_ENV) {",
    "    $global:LASTEXITCODE = 0",
    "    try {",
    "        . $env:BUILD_SKILL_SDK_ENV",
    "    } catch {",
    "        Write-Output $_.Exception.Message",
    "        exit 1",
    "    }",
    "    if ($global:LASTEXITCODE) { exit $global:LASTEXITCODE }",
    "    Set-Location -LiteralPath $env:BUILD_SKILL_DIR",
    "}",
    "$found = @()",
    "foreach ($w in ([string]$env:BUILD_SKILL_WORDS -split [char]10)) {",
    "    if (-not $w) { continue }",
    "    if (Get-Command -Name $w -ErrorAction SilentlyContinue) {",
    "        $found += '1 ' + $w",
    "    } else {",
    "        $found += '0 ' + $w",
    "    }",
    "}",
    "$env:BUILD_SKILL_FOUND = $found -join [char]10",
    "& $env:BUILD_SKILL_PYTHON -I -c $env:BUILD_SKILL_DUMP $env:BUILD_SKILL_ENV_OUT",
    "exit $LASTEXITCODE",
])

# PowerShell：同じシェルで SDK を読み込み直してから（出力は捨てる。読み込めたら
# BUILD_SKILL_LOADED のファイルを作る）、BUILD_SKILL_COMMAND を、打ち込んだときと同じように動かす。
# 0 でない $LASTEXITCODE はその値で、コマンドが見つからないときは 127 で、ほかの失敗は 1 で終わる。
# 成功したかは、コマンドの最後の文の直後の $? で見る（コマンドの後ろに、$? を受け取る文を足して動かす）。
# Invoke-Expression の後の $? は、中の「cd x && ビルド」の cd が失敗しても真のままになる。
# Windows PowerShell 5.1 は、ネイティブのコマンドの標準エラー出力を NativeCommandError として
# 記録し、$? を偽にすることがある。それだけなら失敗にしない。
PS_RUN = "\n".join([
    "$ErrorActionPreference = 'Continue'",
    "if ($env:BUILD_SKILL_SDK_ENV) {",
    "    $global:LASTEXITCODE = 0",
    "    try {",
    "        . $env:BUILD_SKILL_SDK_ENV *> $null",
    "    } catch {",
    "        exit 1",
    "    }",
    "    if ($global:LASTEXITCODE) { exit $global:LASTEXITCODE }",
    "    Set-Location -LiteralPath $env:BUILD_SKILL_DIR",
    "    [IO.File]::WriteAllText($env:BUILD_SKILL_LOADED, '')",
    "}",
    "$global:LASTEXITCODE = 0",
    "$Error.Clear()",
    "$ok = $true",
    "$global:BuildSkillLastOk = $true",
    "try {",
    "    Invoke-Expression ($env:BUILD_SKILL_COMMAND + [char]10 + '$global:BuildSkillLastOk = $?')",
    "    $ok = $? -and $global:BuildSkillLastOk",
    "} catch {",
    "    Write-Output $_.Exception.Message",
    "    $ok = $false",
    "}",
    "if ($global:LASTEXITCODE) { exit $global:LASTEXITCODE }",
    "if ($ok) { exit 0 }",
    "$errors = @($Error | Where-Object { ([string]$_.FullyQualifiedErrorId) -notlike "
    "'NativeCommandError*' })",
    "if ($errors.Count -eq 0) { exit 0 }",
    "if (([string]$errors[0].FullyQualifiedErrorId) -like 'CommandNotFoundException*') { exit 127 }",
    "exit 1",
])

# -ExecutionPolicy Bypass は、利用者の SDK（.ps1）を「.」で読み込むため
PS_OPTIONS = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]

CMD_BUILTINS = {
    "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date", "del", "dir",
    "dpath", "echo", "endlocal", "erase", "exit", "for", "ftype", "goto", "if", "keys", "md",
    "mkdir", "mklink", "move", "path", "pause", "popd", "prompt", "pushd", "rd", "rem", "ren",
    "rename", "rmdir", "set", "setlocal", "shift", "start", "time", "title", "type", "ver",
    "verify", "vol",
}


def shell_path(settings, path):
    """シェルに渡すパス。Windows の bash には「/」区切りで渡す。"""
    if IS_WINDOWS and settings["shell"] == "bash":
        return path.replace("\\", "/")
    return path


def write_script(tmp, name, text):
    """シェルに読ませるスクリプトのファイルを作る。cmd のバッチファイルは ASCII、ほかは UTF-8。"""
    path = os.path.join(tmp, name)
    encoding = "ascii" if name.endswith(".cmd") else "utf-8"
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)
    return path


def shell_argv(settings, script_path=None, ps_script=None, command=None):
    """シェルを起動する引数。cmd は1つの文字列（Windows のコマンドライン）で返す。
    bash はスクリプトのファイルを、PowerShell はスクリプトの文字を受け取る。"""
    program = settings["shell_program"]
    shell = settings["shell"]
    if shell == "bash":
        return [program, shell_path(settings, script_path)]
    if shell == "powershell":
        return [program] + PS_OPTIONS + [ps_script]
    # cmd：/s で、/c の後ろの最初と最後の「"」だけを外し、残りを打ち込んだとおりに動かす
    body = '"{}"'.format(script_path) if command is None else command
    return '"{}" /d /s /c "{}"'.format(program, body)


def start_step(settings, out, env, tmp, command=None, env_out=None, words=(), loaded_mark=None):
    """command があれば、それをシェルで動かす。なければ、SDK を読み込んで（sdk_env があれば）
    環境変数を env_out に書き出す。出力（標準出力と標準エラー出力）は out に書く。
    loaded_mark があれば、command の前に同じシェルで SDK を読み込み直し、読み込めたらそのファイルを作る。
    ビルドするフォルダの中で動かす。Popen を返す。"""
    shell = settings["shell"]
    env = dict(env)
    for key in [k for k in env if k.upper().startswith(ENV_PREFIX)]:
        del env[key]
    sdk = settings["sdk_env"]
    if command is None or loaded_mark:
        if sdk:
            env[ENV_PREFIX + "SDK_ENV"] = shell_path(settings, sdk)
            env[ENV_PREFIX + "DIR"] = shell_path(settings, settings["source_dir"])
    if command is None:
        env[ENV_PREFIX + "PYTHON"] = shell_path(settings, sys.executable)
        env[ENV_PREFIX + "DUMP"] = DUMP_ENV
        env[ENV_PREFIX + "ENV_OUT"] = shell_path(settings, env_out)
        if words:
            env[ENV_PREFIX + "WORDS"] = "\n".join(words)
        if shell == "cmd":
            argv = shell_argv(settings, write_script(tmp, "load.cmd", CMD_LOAD))
        elif shell == "powershell":
            argv = shell_argv(settings, ps_script=PS_LOAD)
        else:
            argv = shell_argv(settings, write_script(tmp, "load.sh", BASH_LOAD))
    else:
        if loaded_mark:
            env[ENV_PREFIX + "LOADED"] = shell_path(settings, loaded_mark)
        if shell == "cmd":
            argv = shell_argv(settings, command=command)
        elif shell == "powershell":
            env[ENV_PREFIX + "COMMAND"] = command
            argv = shell_argv(settings, ps_script=PS_RUN)
        else:
            argv = shell_argv(settings, write_script(tmp, "run.sh", BASH_RUN + command + "\n"))
    kwargs = {}
    if not IS_WINDOWS:
        kwargs["start_new_session"] = True  # 時間切れのとき、子のプロセスごと止めるため
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                            cwd=settings["source_dir"], env=env, **kwargs)


def taskkill_program():
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    path = os.path.join(root, "System32", "taskkill.exe")
    return path if os.path.isfile(path) else "taskkill"


def kill_tree(proc):
    """プロセスと、その子のプロセスをすべて止める。"""
    if IS_WINDOWS:
        try:
            subprocess.run([taskkill_program(), "/T", "/F", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # シェルが先に終わっても、TERM を無視した子が残ることがある
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def wait_step(proc, timeout):
    """(終了コード, 時間切れか) を返す。時間切れなら、子のプロセスまで止める。"""
    try:
        return proc.wait(timeout=max(timeout, 0.01)), False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        return proc.returncode, True
    except KeyboardInterrupt:
        kill_tree(proc)
        raise


def read_env_file(path):
    """書き出された環境変数を読む。(環境変数, シェルが調べたコマンドの有無) を返す。
    読めなければ None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    env = {}
    found = {}
    for key, value in data.items():
        key, value = str(key), str(value)
        if key.upper() == ENV_PREFIX + "FOUND":
            for line in value.splitlines():
                flag, _, word = line.partition(" ")
                if word:
                    found[word] = flag == "1"
        elif not key.upper().startswith(ENV_PREFIX):
            env[key] = value
    return env, found


def env_get(env, name):
    if name in env:
        return env[name]
    if IS_WINDOWS:
        for key, value in env.items():
            if key.upper() == name.upper():
                return value
    return None


def lookup_program(word, dirs, cwd, exts=None, search_cwd=False):
    """コマンドの実行ファイルを探す。exts（PATHEXT の拡張子）があれば Windows の決まりで探す。
    search_cwd なら、cwd を PATH より先に探す（cmd の決まり）。見つからなければ None。"""
    if not word:
        return None
    seps = "/\\" if exts is not None else "/"
    if any(c in word for c in seps) or (exts is not None and ":" in word):
        candidates = [os.path.join(cwd, word)]
    else:
        places = [cwd] if search_cwd else []
        for d in dirs:
            d = d.strip().strip('"')
            if d:
                places.append(os.path.join(cwd, d))
        candidates = [os.path.join(p, word) for p in places]
    for c in candidates:
        if exts is None:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
            continue
        tries = [c] if os.path.splitext(c)[1] else []
        tries += [c + e for e in exts]
        for t in tries:
            if os.path.isfile(t):
                return t
    return None


def find_program(word, env, settings):
    path = env_get(env, "PATH") or ""
    exts = None
    if IS_WINDOWS:
        exts = [e.strip() for e in (env_get(env, "PATHEXT") or DEFAULT_PATHEXT).split(";")
                if e.strip()]
    return lookup_program(word, path.split(os.pathsep), settings["source_dir"], exts,
                          settings["shell"] == "cmd")


def command_exists(word, env, settings, found):
    """コマンドがあるか。bash と PowerShell は、SDK を読み込んだシェルが調べた結果
    （SDK の関数も見つかる）。cmd は、書き出された環境変数の PATH で探す。"""
    if settings["shell"] != "cmd":
        return bool(found.get(word))
    if word.lower() in CMD_BUILTINS:
        return True
    return find_program(word, env, settings) is not None


def cc_word(command):
    """CC の値の最初の語（コンパイラー）。「"」か「'」で囲んだパスは、空白があっても1語にする。"""
    command = command.strip()
    if command[:1] in ("\"", "'"):
        end = command.find(command[0], 1)
        if end > 0:
            return command[1:end]
    words = command.split()
    return words[0] if words else ""


def compiler_of(env, settings):
    """make が使うコンパイラー（環境変数 CC、無ければ cc）と、その場所。"""
    cc = (env_get(env, "CC") or "").strip()
    command = cc or "cc"
    word = cc_word(command)
    path = find_program(word, env, settings) if word else None
    return {"command": command, "word": word, "path": path, "from_env": bool(cc)}


def cc_shown(cc):
    """注意の文に書くコンパイラー。CC が無く、cc も見つからなければ None。"""
    if cc is None:
        return None
    if cc["from_env"]:
        return cc["path"] or cc["command"]
    return cc["path"]


# ---------------------------------------------------------------------------
# コマンドの最初の語
# ---------------------------------------------------------------------------

def _first_word_posix(command):
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    tokens = list(lex)  # 全部を読み、引用符の閉じ忘れを見つける
    for token in tokens:
        if not token or all(c in "();<>|&" for c in token):
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return token
    return None


def _check_quotes(command, shell):
    """引用符が閉じていなければ ValueError。"""
    if shell == "cmd":
        if command.count('"') % 2:
            raise ValueError("unclosed quote")
        return
    state = None
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if state is None:
            if c == "`":
                i += 2
                continue
            if c in "'\"":
                state = c
        elif state == "'":
            if c == "'":
                if command[i + 1:i + 2] == "'":
                    i += 2
                    continue
                state = None
        else:
            if c == "`":
                i += 2
                continue
            if c == '"':
                if command[i + 1:i + 2] == '"':
                    i += 2
                    continue
                state = None
        i += 1
    if state is not None:
        raise ValueError("unclosed quote")


def _first_word_windows(command, shell):
    _check_quotes(command, shell)
    quotes = '"' if shell == "cmd" else "\"'"
    ops = "&|<>()" if shell == "cmd" else "&|<>(){};,"
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c.isspace() or c in ops or (shell == "cmd" and c == "@"):
            i += 1
            continue
        if shell == "powershell" and c == "." and (i + 1 >= n or command[i + 1].isspace()):
            i += 1  # 「. ファイル」の「.」
            continue
        if c in quotes:
            j = command.find(c, i + 1)
            return command[i + 1:j] or None
        j = i
        while j < n and not command[j].isspace() and command[j] not in ops \
                and command[j] not in quotes:
            j += 1
        word = command[i:j]
        if shell == "powershell" and word.startswith("$"):
            return None  # 変数や式で始まるコマンドは、調べられない
        return word
    return None


def first_word(command, shell="bash"):
    """コマンドの最初の語（bash では VAR=値 と記号を飛ばす）。読めないときは ValueError。"""
    if shell == "bash":
        return _first_word_posix(command)
    return _first_word_windows(command, shell)


# ---------------------------------------------------------------------------
# config-path
# ---------------------------------------------------------------------------

def cmd_config_path(opts):
    workdir, config = resolve_paths(opts)
    created = open_config(opts, workdir, config)
    raw, problems = read_config(config)
    missing = missing_required(raw) if not problems else []
    recommended = empty_recommended(raw) if not problems else []
    result = {
        "ok": not problems,
        "skill": SKILL,
        "config": config,
        "created": created,
        "missing": missing,
        "empty_recommended": recommended,
    }
    if created:
        msg = "設定ファイルをひな形から作りました：{}。".format(config)
    else:
        msg = "設定ファイルはここにあります：{}。".format(config)
    if missing:
        msg += "まだ空の必須の項目があります：{}。".format("、".join(label(k) for k in missing))
    if recommended:
        msg += "機器向けにビルドするなら書く項目が、まだ空です：{}。".format(
            "、".join(label(k) for k in recommended))
    if problems:
        result["problems"] = problems
        msg += "ただし、ファイルに読めない所があります。"
    result["message"] = msg
    return 0, result


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def cmd_check(opts):
    workdir, config, created, raw, settings, problems = load(opts)
    result = {"ok": False, "skill": SKILL, "config": config, "created": created,
              "problems": problems, "summary": {}}
    if settings is not None:
        result["summary"] = summary_of(settings)

    words = []
    if settings is not None:
        for key in ("clean_command", "build_command"):
            cmd = settings[key]
            if not cmd:
                continue
            try:
                w = first_word(cmd, settings["shell"])
            except ValueError:
                problems.append("{}の書き方が正しくありません（引用符が閉じていません）。".format(
                    label(key)))
                continue
            if w:
                words.append((key, w))

    cc = None
    if settings is not None and settings["runnable"]:
        progress("設定を確かめています（SDK の読み込みと、コマンドの有無）…")
        probe_problems, cc = probe(settings, words)
        problems.extend(probe_problems)
        if cc:
            result["summary"]["compiler"] = cc["command"]
            result["summary"]["compiler_path"] = cc["path"]

    notes = []
    if settings is not None and not settings["sdk_env"]:
        notes.append(sdk_note(cc_shown(cc), cc_from_env=bool(cc and cc["from_env"])))
    result["notes"] = notes

    result["ok"] = not problems
    if problems:
        if created:
            result["message"] = "設定ファイルを新しく作りました。空の必須の項目を書いてください。"
        else:
            result["message"] = problems_message(problems)
        return 2, result
    result["message"] = "設定に問題はありません。ビルドの準備ができています。" + "".join(notes)
    return 0, result


def sdk_said(text):
    """SDK が出したメッセージの最初の行（多くは、いちばん大事な行）。"""
    said = [l.strip() for l in text.splitlines() if l.strip()]
    return "SDK のメッセージ：「{}」。".format(short(said[0])) if said else ""


def cd_message(settings, error=None):
    return "{}の {} に移れません{}。".format(
        label("source_dir"), settings["source_dir"],
        "（{}）".format(error.strerror or error) if error is not None else "")


def start_message(settings, error):
    return "シェル {} を起動できません（{}）。".format(
        settings["shell_program"], getattr(error, "strerror", None) or error)


def load_failure(settings, rc, said, timeout, timed_out):
    """SDK の読み込み（sdk_env が空なら、シェルの起動）が失敗したときの文。"""
    if settings["sdk_env"]:
        if timed_out:
            return ("{}の読み込みが、{} 秒たっても終わりません。"
                    "ファイルの中で、入力を待っていないかを確かめてください。".format(
                        label("sdk_env"), timeout))
        if rc:
            msg = "{}の {} の読み込みが失敗しました（終了コード {}）。".format(
                label("sdk_env"), settings["sdk_env"], rc)
        else:
            msg = "{}の {} を読み込むと、途中で終わってしまいます。".format(
                label("sdk_env"), settings["sdk_env"])
        return msg + sdk_said(said) + "SDK が正しく入っているかを確かめてください。"
    if timed_out:
        return "シェル {} が、{} 秒たっても終わりません。".format(settings["shell_program"], timeout)
    lines = [l.strip() for l in said.splitlines() if l.strip()]
    return "シェル {} で、環境変数を調べられませんでした（終了コード {}）。{}".format(
        settings["shell_program"], rc, "メッセージ：「{}」。".format(short(lines[0])) if lines else "")


def probe(settings, words):
    """SDK を読み込んで、コマンドとコンパイラーがあるかを調べる。(問題のリスト, コンパイラー) を返す。"""
    tmp = tempfile.mkdtemp(prefix="build-check-")
    try:
        out_path = os.path.join(tmp, "output.log")
        env_out = os.path.join(tmp, "env.json")
        with open(out_path, "wb") as out:
            try:
                proc = start_step(settings, out, os.environ, tmp, env_out=env_out,
                                  words=[w for _, w in words])
            except OSError as e:
                if not os.path.isdir(settings["source_dir"]):
                    return [cd_message(settings, e)], None
                return [start_message(settings, e)], None
            rc, timed_out = wait_step(proc, PROBE_TIMEOUT)
        with open(out_path, "rb") as f:
            said = decode_output(f.read())
        loaded = None if timed_out else read_env_file(env_out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if timed_out or rc != 0 or loaded is None:
        return [load_failure(settings, rc, said, PROBE_TIMEOUT, timed_out)], None
    env, found = loaded

    problems = []
    cc = compiler_of(env, settings)
    if settings["sdk_env"] and cc["from_env"] and not cc["path"]:
        problems.append(
            "SDK を読み込んだ後のコンパイラー {} が見つかりません。"
            "SDK が正しく入っているかを確かめてください。".format(cc["word"]))
    after = "（SDK を読み込んだ後で探しました）" if settings["sdk_env"] else ""
    for key, w in words:
        if not command_exists(w, env, settings, found):
            problems.append("{}の {} が見つかりません{}。PC に入っているかを確かめてください。".format(
                label(key), w, after))
    return problems, cc


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def snapshot(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_ino, st.st_size)
    except OSError:
        return None


def iso_time(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def write_mark(logf, step):
    """ログに、段階の区切りの行を書く。その行（改行つき）を返す。"""
    line = MARK + STEP_MARKS[step]
    logf.write((line + os.linesep).encode("utf-8"))
    logf.flush()
    return line + "\n"


def reload_failure(s, rc):
    """コマンドの前に SDK を読み込み直して、失敗したときの文。"""
    return ("{}の {} を読み込み直すと、失敗しました（{}）。"
            "SDK が正しく入っているかを確かめてください。".format(
                label("sdk_env"), s["sdk_env"],
                "終了コード {}".format(rc) if rc else "途中で終わりました"))


def run_steps(s, log_path, deadline):
    """SDK の読み込み、片付け、ビルドを順に動かし、出力をログに書く。
    {failed_step, rc, timed_out, text, env, message} を返す（env は、SDK を読み込んだ後の環境変数）。"""
    state = {"failed_step": None, "rc": 0, "timed_out": False, "env": None, "message": None}
    texts = []
    steps = []
    if s["sdk_env"]:
        steps.append(("sdk_env", None))
    if s["clean_command"]:
        steps.append(("clean", s["clean_command"]))
    steps.append(("build", s["build_command"]))
    # bash と PowerShell は、コマンドごとに同じシェルで SDK を読み込み直す（SDK の関数も使えるように）。
    # cmd は、SDK を読み込んだ後の環境変数でコマンドを動かす。
    reload = bool(s["sdk_env"]) and s["shell"] != "cmd"
    env = dict(os.environ)  # コマンドを動かす環境変数
    loaded_env = env        # SDK を読み込んだ後の環境変数（コンパイラーを調べる）
    tmp = tempfile.mkdtemp(prefix="build-")
    try:
        with open(log_path, "ab") as logf:
            for step, command in steps:
                texts.append(write_mark(logf, step))
                start = logf.tell()
                env_out = os.path.join(tmp, "env.json")
                mark = os.path.join(tmp, "loaded") if reload and command is not None else None
                if mark and os.path.exists(mark):
                    os.remove(mark)
                try:
                    proc = start_step(s, logf, env, tmp, command=command, env_out=env_out,
                                      loaded_mark=mark)
                except OSError as e:
                    if not os.path.isdir(s["source_dir"]):
                        state["failed_step"] = "cd"
                        state["message"] = cd_message(s, e)
                    else:
                        state["failed_step"] = step
                        state["message"] = start_message(s, e)
                    state["rc"] = 1
                    texts.append(state["message"] + "\n")
                    logf.write((state["message"] + os.linesep).encode("utf-8"))
                    break
                rc, timed_out = wait_step(proc, deadline - time.time())
                texts.append(recode_log(log_path, start))
                state["rc"] = rc
                if timed_out:
                    state["failed_step"] = step
                    state["timed_out"] = True
                    break
                if step == "sdk_env":
                    loaded = read_env_file(env_out)
                    if rc != 0 or loaded is None:
                        state["failed_step"] = "sdk_env"
                        break
                    loaded_env = loaded[0]
                    if not reload:
                        env = loaded_env
                elif mark and not os.path.exists(mark):
                    state["failed_step"] = "sdk_env"
                    state["message"] = reload_failure(s, rc)
                    texts.append(state["message"] + "\n")
                    logf.write((state["message"] + os.linesep).encode("utf-8"))
                    break
                elif rc != 0:
                    state["failed_step"] = step
                    break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    state["text"] = "".join(texts)
    if state["failed_step"] != "sdk_env":
        state["env"] = loaded_env
    return state


def cmd_run(opts):
    workdir, config, created, raw, settings, problems = load(opts)
    if problems:
        if created:
            msg = "設定ファイルを新しく作りました。空の必須の項目を書いてから、もう一度ビルドしてください。"
        else:
            msg = "設定に問題があるため、ビルドしませんでした。" + problems_message(problems)
        notes = [sdk_note()] if settings is not None and not settings["sdk_env"] else []
        return 2, {"ok": False, "skill": SKILL, "message": msg, "problems": problems,
                   "notes": notes, "config": config, "created": created, "log": None}

    s = settings
    log_path = new_log_path(workdir)
    outputs = [(name, os.path.normpath(os.path.join(s["source_dir"], os.path.expanduser(name))))
               for name in s["outputs"]]
    before = {path: snapshot(path) for _, path in outputs}

    started = datetime.datetime.now()
    header = [
        "{} のログ".format(SKILL),
        "開始：{}".format(started.strftime("%Y-%m-%d %H:%M:%S")),
        "作業フォルダ：{}".format(workdir),
        "設定ファイル：{}".format(config),
        "ビルドするフォルダ：{}".format(s["source_dir"]),
        "シェル：{}（{}）".format(s["shell"], s["shell_program"]),
        "SDK の環境設定ファイル：{}".format(s["sdk_env"] or "（読み込まない）"),
        "片付けのコマンド：{}".format(s["clean_command"] or "（しない）"),
        "ビルドのコマンド：{}".format(s["build_command"]),
        "ビルドでできるファイル：{}".format("、".join(s["outputs"])),
        "待つ最長の秒数：{}".format(s["timeout_seconds"]),
        "",
        "==== ビルドの出力（ここから） ====",
        "",
    ]
    append_log(log_path, "\n".join(header))

    progress("ビルドを始めます：{}（フォルダ：{}）".format(s["build_command"], s["source_dir"]))
    start = time.time()
    state = run_steps(s, log_path, start + s["timeout_seconds"])
    duration = round(time.time() - start, 1)
    rc = state["rc"]
    timed_out = state["timed_out"]
    cc = compiler_of(state["env"], s) if state["env"] is not None else None
    compiler = cc["command"] if cc else None
    compiler_path = cc["path"] if cc else None
    cc_from_env = bool(cc and cc["from_env"])

    lines = state["text"].splitlines()
    errors = error_lines_of(lines)
    warnings = warning_lines_of(lines)
    tail = lines[-MAX_TAIL_LINES:]

    result = {
        "ok": False,
        "skill": SKILL,
        "message": "",
        "log": log_path,
        "config": config,
        "source_dir": s["source_dir"],
        "shell_program": s["shell_program"],
        "exit_code": rc,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "compiler": compiler,
        "compiler_path": compiler_path,
        "outputs": [],
        "error_lines": errors,
        "warning_lines": warnings,
        "notes": [],
    }

    failed_step = state["failed_step"]
    if timed_out:
        msg = ("ビルドが {} 秒たっても終わらないため、止めました。"
               "ビルドが入力を待っていないか、{}が短すぎないかを確かめてください。").format(
                   s["timeout_seconds"], label("timeout_seconds"))
    elif state["message"]:
        msg = state["message"]
    elif failed_step:
        msg = failure_message(failed_step, rc, s, errors, lines)
    else:
        msg = None

    if failed_step is None:
        missing, stale = [], []
        for name, path in outputs:
            info = {"name": name, "path": path, "exists": False, "updated": False,
                    "size": None, "mtime": None, "file_type": None, "arch": None}
            try:
                st = os.stat(path)
            except OSError:
                missing.append(name)
                result["outputs"].append(info)
                continue
            old = before.get(path)
            now = (st.st_mtime_ns, st.st_ino, st.st_size)
            updated = old is None or now != old
            info.update({"exists": True, "updated": updated, "size": st.st_size,
                         "mtime": iso_time(st.st_mtime), "file_type": describe_file(path),
                         "arch": elf_arch(path)})
            if not updated:
                stale.append(name)
            result["outputs"].append(info)
        if missing:
            failed_step = "outputs"
            msg = ("ビルドは終わりましたが、{} ができていません。"
                   "{}の名前と、ビルドのコマンドを確かめてください。").format(
                       "、".join(missing), label("outputs"))
        else:
            parts = []
            for o in result["outputs"]:
                parts.append("{}（{:,} バイト、種類：{}）".format(
                    o["name"], o["size"], kind_of(o["arch"], o["file_type"])))
            msg = "ビルドに成功しました（{} 秒）。できたファイル：{}。".format(
                duration, "、".join(parts))
            if stale:
                # ビルドのコマンドは成功したが、ファイルは作り直されなかった（ソースが変わっていないなど）。
                # 失敗にはしないが、前のビルドのままであることをはっきり伝える。
                result["stale_outputs"] = stale
                msg = ("ビルドのコマンドは成功しましたが、{} は作り直されず、前のビルドのままです。"
                       "ソースが前のビルドから変わっていないためと考えられます。"
                       "毎回作り直すには、{}に make clean などを書いてください。"
                       "今あるファイル：{}。").format(
                           "、".join(stale), label("clean_command"), "、".join(parts))
            result["ok"] = True

    if not s["sdk_env"]:
        here = pc_arch()
        other = [o["arch"] for o in result["outputs"]
                 if o["arch"] and here and o["arch"] != here]
        note = sdk_note(cc_shown(cc), when="built" if result["ok"] else "failed",
                        cc_from_env=cc_from_env,
                        other_arch="、".join(sorted(set(other))) if other else None)
        result["notes"].append(note)
        if result["ok"]:
            msg += note
    result["message"] = msg
    if not result["ok"]:
        result["failed_step"] = failed_step
        result["tail"] = tail

    footer = [
        "",
        "==== ビルドの出力（ここまで） ====",
        "終了コード：{}（{} 秒）{}".format(rc, duration, "　時間切れで止めました" if timed_out else ""),
        "コンパイラー：{}{}".format(compiler or "（分かりません）",
                                 "（{}）".format(compiler_path) if compiler_path else ""),
    ]
    for o in result["outputs"]:
        if o["exists"]:
            footer.append("できたファイル：{}　{} バイト　{}　{}".format(
                o["path"], o["size"], o["mtime"], o["file_type"] or o["arch"] or ""))
        else:
            footer.append("できていないファイル：{}".format(o["path"]))
    footer.append("結果：{}".format(msg))
    footer.append("")
    append_log(log_path, "\n".join(footer))
    progress(msg)
    return (0 if result["ok"] else 1), result


def last_line_with(lines, words):
    for line in reversed(lines):
        if any(w in line for w in words):
            return line.strip()
    return None


NOT_FOUND_WORDS = ("not found", "見つかりません", "No such file", "is not recognized",
                   "認識されていません", "認識されません")


def failure_message(step, rc, s, errors, lines):
    said = [l.strip() for l in lines if l.strip() and not l.startswith(MARK)]
    if step == "sdk_env":
        return load_failure(s, rc, "\n".join(said), s["timeout_seconds"], False)
    what = "片付けのコマンド" if step == "clean" else "ビルドのコマンド"
    command = short(s["clean_command" if step == "clean" else "build_command"], 80)
    if rc in NOT_FOUND_CODES.get(s["shell"], ()):
        line = last_line_with(lines, NOT_FOUND_WORDS)
        return ("{}（{}）の中で、コマンドが見つかりません。「{}」。"
                "PC に入っているか、SDK を読み込めているかを確かめてください。").format(
                    what, command, short(line or "終了コード {}".format(rc)))
    if rc == 126 and s["shell"] == "bash":
        line = last_line_with(lines, ("Permission denied", "許可がありません"))
        return ("{}（{}）の中で、実行できないファイルがあります。「{}」。"
                "ファイルに実行の権限があるかを確かめてください。").format(
                    what, command, short(line or "終了コード 126"))
    if step == "clean":
        msg = "片付けのコマンド（{}）が失敗しました（終了コード {}）。".format(command, rc)
    else:
        msg = "ビルドが失敗しました（終了コード {}）。".format(rc)
    if errors:
        return msg + "最初のエラーは「{}」です。".format(short(errors[0]))
    if said:
        return msg + "最後の出力は「{}」です。くわしくはログを見てください。".format(short(said[-1]))
    return msg + "ログを確かめてください。"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def dispatch(argv):
    try:
        opts = parse_args(argv)
    except UsageError as e:
        return 2, {"ok": False, "skill": SKILL, "message": str(e), "problems": [str(e)]}
    if opts["help"]:
        progress(USAGE)
        return 0, {"ok": True, "skill": SKILL, "message": USAGE}
    try:
        if opts["command"] == "config-path":
            code, result = cmd_config_path(opts)
        elif opts["command"] == "check":
            code, result = cmd_check(opts)
        else:
            code, result = cmd_run(opts)
    except SettingsProblem as e:
        code = 2
        result = {"ok": False, "skill": SKILL, "message": problems_message(e.problems),
                  "problems": e.problems}
        if e.config:
            result["config"] = e.config
    if "added" in opts:
        # 設定ファイルを開いた後は、足した項目を必ず知らせる
        result["added"] = opts["added"]
        if opts["added"]:
            result["message"] = (result.get("message") or "") + added_sentence(opts["added"])
    return code, result


def crash_log(argv, detail):
    """思わぬエラーの詳しい中身を、作業フォルダのログに残す。残せなければ None。"""
    try:
        workdir = os.path.abspath(os.path.expanduser(parse_args(argv)["workdir"] or os.getcwd()))
        if not os.path.isdir(workdir):
            return None
        path = new_log_path(workdir)
        append_log(path, "{} のログ（思わぬエラー）\n{}\n".format(SKILL, detail))
        return path
    except Exception:
        return None


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    setup_streams()
    try:
        code, result = dispatch(argv)
    except KeyboardInterrupt:
        code, result = 1, {"ok": False, "skill": SKILL,
                           "message": "途中で止められました。ビルドは終わっていません。"}
    except Exception:
        detail = traceback.format_exc()
        log = crash_log(argv, detail)
        last = detail.strip().splitlines()[-1]
        progress(last)
        msg = "スクリプトの中で思わぬエラーが起きました（{}）。".format(short(last, 120))
        msg += "くわしくはログを見てください。" if log else ""
        code, result = 1, {"ok": False, "skill": SKILL, "log": log, "message": msg}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    sys.stdout.write(json_text_for(text, getattr(sys.stdout, "encoding", None)) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
