#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""機器で動かすプログラムを、PC でビルドする（クロスコンパイル）。

使い方：
  python3 build.py [run | config-path | check] [--workdir フォルダ] [--config ファイル]

  run          ビルドする（何も書かないときも run）
  config-path  設定ファイルの場所を出す。無ければひな形から作る
  check        設定を確かめる（ビルドはしない）

結果は、標準出力に JSON を1つだけ出す。途中の様子は標準エラー出力に出す。
終了コード：run は 0＝成功、1＝ビルドの失敗、2＝設定の問題。
            check は 0＝問題なし、2＝問題あり。config-path は 0。

Python 3.8 以降の標準ライブラリだけで動く。
"""

import datetime
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

SKILL = "cross-compile"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
TEMPLATE = os.path.join(SKILL_DIR, "config-template.yaml")
CONFIG_DIR_NAME = ".embedded-debug-loop"
COMMANDS = ("run", "config-path", "check")

REQUIRED_KEYS = ("outputs",)
# 必須ではないが、機器向けにビルドするなら書く項目
RECOMMENDED_KEYS = ("sdk_env",)
GITIGNORE_TEXT = "*\n"  # 設定やログが、誤って git に入らないようにする
LABELS = {
    "source_dir": "ビルドするフォルダ",
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

USAGE = (
    "使い方：python3 build.py [run | config-path | check] "
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
                    raise YamlError(line_no, "「\"」で囲んだ文字の書き方が正しくありません。")
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
            line = line.lstrip("﻿")
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
        config = os.path.join(workdir, CONFIG_DIR_NAME, SKILL + ".yaml")
    return workdir, config


def _config_dir_of(path):
    """path か、その上にある .embedded-debug-loop フォルダの場所。無ければ None。"""
    p = os.path.abspath(path)
    while True:
        if os.path.basename(p) == CONFIG_DIR_NAME:
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def ensure_gitignore(path):
    """path か、その上にある .embedded-debug-loop フォルダに .gitignore が無ければ作る
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
    """フォルダを作る。.embedded-debug-loop フォルダに .gitignore が無ければ、それも作る。"""
    os.makedirs(path, exist_ok=True)
    ensure_gitignore(path)


def template_blocks(text):
    """ひな形の項目ごとに (名前, 行の並び) を返す。
    行の並びは、項目のすぐ上のコメントと、項目の行（「- 値」の行が続けば、それも）。"""
    lines = text.splitlines()
    if lines:
        lines[0] = lines[0].lstrip("﻿")
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
    .embedded-debug-loop フォルダに .gitignore が無ければ作る。作ったら True。
    足した項目の名前は opts["added"] に入れる。"""
    ensure_gitignore(os.path.join(workdir, CONFIG_DIR_NAME))
    created = ensure_config(config)
    ensure_gitignore(config)
    opts["added"] = [] if created else add_missing_keys(config)
    return created


def ensure_config(path):
    """設定ファイルが無ければ、ひな形を写して作る。作ったら True。"""
    if os.path.exists(path):
        if os.path.isdir(path):
            raise SettingsProblem(["設定ファイルの場所 {} は、フォルダです。".format(path)], path)
        return False
    if not os.path.isfile(TEMPLATE):
        raise SettingsProblem([
            "設定のひな形 {} がありません。スキルを入れ直してください。".format(TEMPLATE)], path)
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


def _abs(workdir, path):
    return os.path.normpath(os.path.join(workdir, os.path.expanduser(path)))


def normalize(raw, workdir):
    """設定の値をそろえ、(設定, 問題のリスト) を返す。ファイルの有無も見る。"""
    problems = []
    s = {}

    src = _text(raw, "source_dir", problems) or "."
    s["source_dir"] = _abs(workdir, src)
    if not os.path.exists(s["source_dir"]):
        problems.append("{}の {} がありません。".format(label("source_dir"), s["source_dir"]))
    elif not os.path.isdir(s["source_dir"]):
        problems.append("{}の {} は、フォルダではありません。".format(
            label("source_dir"), s["source_dir"]))

    sdk = _text(raw, "sdk_env", problems)
    s["sdk_env"] = _abs(workdir, sdk) if sdk else ""
    if sdk:
        if not os.path.exists(s["sdk_env"]):
            problems.append(
                "{}の {} がありません。SDK を入れた場所と、ファイルの名前を確かめてください。".format(
                    label("sdk_env"), s["sdk_env"]))
        elif not os.path.isfile(s["sdk_env"]):
            problems.append("{}の {} は、ファイルではありません。".format(
                label("sdk_env"), s["sdk_env"]))

    s["build_command"] = _text(raw, "build_command", problems) or DEFAULT_BUILD_COMMAND
    s["clean_command"] = _text(raw, "clean_command", problems)

    outputs = []
    value = raw.get("outputs")
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if value is None:
        value = []
    if isinstance(value, list):
        for item in value:
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            if isinstance(item, (bool, list)):
                problems.append("{}の中は、ファイルの名前（文字）で書いてください。今の値：{}".format(
                    label("outputs"), show_value(item)))
                continue
            outputs.append(str(item).strip())
        if not outputs:
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
        "sdk_env": settings["sdk_env"],
        "build_command": settings["build_command"],
        "clean_command": settings["clean_command"],
        "outputs": settings["outputs"],
        "timeout_seconds": settings["timeout_seconds"],
    }


# ---------------------------------------------------------------------------
# 出力と記録
# ---------------------------------------------------------------------------

def progress(text):
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def new_log_path(workdir):
    logs = os.path.join(workdir, CONFIG_DIR_NAME, "logs")
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
        r = subprocess.run([exe, "-b", path], stdout=subprocess.PIPE,
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


def pc_arch():
    """この PC の CPU の呼び名（Python の実行ファイルから調べる）。"""
    return elf_arch(os.path.realpath(sys.executable))


def short(text, limit=160):
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


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

PROBE_SCRIPT = r'''
sdk_env=$1
source_dir=$2
shift 2
words=("$@")
set --
if [ -n "$sdk_env" ]; then
  . "$sdk_env" 1>&2
  rc=$?
  if [ "$rc" -ne 0 ]; then printf 'sdk_failed\t%s\n' "$rc"; exit 0; fi
fi
if ! cd "$source_dir" 1>&2; then printf 'cd_failed\n'; exit 0; fi
for w in "${words[@]}"; do
  if command -v "$w" >/dev/null 2>&1; then printf 'found\t%s\n' "$w"; else printf 'missing\t%s\n' "$w"; fi
done
if [ -n "${CC-}" ]; then printf 'cc_set\n'; fi
cc_cmd=${CC:-cc}
read -r ccw _rest <<< "$cc_cmd"
if ccp=$(command -v "$ccw" 2>/dev/null); then printf 'cc_path\t%s\n' "$ccp"; else printf 'cc_missing\t%s\n' "$ccw"; fi
printf 'cc\t%s\n' "$cc_cmd"
printf 'end\n'
'''


def first_word(command):
    """コマンドの最初の語（VAR=値 と記号は飛ばす）。読めないときは ValueError。"""
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
                w = first_word(cmd)
            except ValueError:
                problems.append("{}の書き方が正しくありません（引用符が閉じていません）。".format(
                    label(key)))
                continue
            if w:
                words.append((key, w))

    src_ok = settings is not None and os.path.isdir(settings["source_dir"])
    sdk_ok = settings is not None and (
        not settings["sdk_env"] or os.path.isfile(settings["sdk_env"]))
    cc = None
    if src_ok and sdk_ok:
        progress("設定を確かめています（SDK の読み込みと、コマンドの有無）…")
        probe_problems, cc = probe(settings, words)
        problems.extend(probe_problems)
        if cc:
            result["summary"]["compiler"] = cc["command"]
            result["summary"]["compiler_path"] = cc["path"]

    notes = []
    if settings is not None and not settings["sdk_env"]:
        if cc:
            notes.append(sdk_note(cc["path"] or cc["command"], cc_from_env=cc["from_env"]))
        else:
            notes.append(sdk_note())
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


def sdk_said(err):
    """SDK が出したメッセージの最初の行（多くは、いちばん大事な行）。"""
    said = [l.strip() for l in err.splitlines() if l.strip()]
    return "SDK のメッセージ：「{}」。".format(short(said[0])) if said else ""


def probe(settings, words):
    problems = []
    compiler = None
    argv = ["bash", "-c", PROBE_SCRIPT, "probe", settings["sdk_env"], settings["source_dir"]]
    argv += [w for _, w in words]
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=PROBE_TIMEOUT,
                           cwd=settings["source_dir"], start_new_session=True)
    except FileNotFoundError:
        return ["PC に bash が見つかりません。ビルドには bash が要ります。"], None
    except subprocess.TimeoutExpired:
        return ["{}の読み込みが、{} 秒たっても終わりません。"
                "ファイルの中で、入力を待っていないかを確かめてください。".format(
                    label("sdk_env"), PROBE_TIMEOUT)], None
    out = r.stdout.decode("utf-8", "replace").splitlines()
    err = r.stderr.decode("utf-8", "replace").strip()
    found = {}
    ended = False
    cc = {"command": None, "path": None, "from_env": False}
    cc_missing = None
    for line in out:
        parts = line.split("\t", 1)
        tag = parts[0]
        val = parts[1] if len(parts) > 1 else ""
        if tag == "sdk_failed":
            msg = "{}の {} の読み込みが失敗しました（終了コード {}）。".format(
                label("sdk_env"), settings["sdk_env"], val)
            return [msg + sdk_said(err) + "SDK が正しく入っているかを確かめてください。"], None
        if tag == "cd_failed":
            return ["{}の {} に移れません。".format(label("source_dir"), settings["source_dir"])], None
        if tag in ("found", "missing"):
            found[val] = tag == "found"
        elif tag == "cc_set":
            cc["from_env"] = True
        elif tag == "cc_path":
            cc["path"] = val
        elif tag == "cc_missing":
            cc_missing = val
        elif tag == "cc":
            cc["command"] = val
        elif tag == "end":
            ended = True
    if not ended:
        msg = "{}の {} を読み込むと、途中で終わってしまいます。".format(
            label("sdk_env"), settings["sdk_env"])
        return [msg + sdk_said(err) + "SDK が正しく入っているかを確かめてください。"], None
    if cc_missing and settings["sdk_env"] and cc["from_env"]:
        problems.append(
            "SDK を読み込んだ後のコンパイラー {} が見つかりません。"
            "SDK が正しく入っているかを確かめてください。".format(cc_missing))
    if cc["command"]:
        compiler = cc
    after = "（SDK を読み込んだ後で探しました）" if settings["sdk_env"] else ""
    for key, w in words:
        if not found.get(w, False):
            problems.append("{}の {} が見つかりません{}。PC に入っているかを確かめてください。".format(
                label(key), w, after))
    return problems, compiler


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

# set -e は「cd sub && make」の cd の失敗では止まらない。各コマンドの後で終了コードも見る。
STOP_ON_FAILURE = 'st=$?; if [ "$st" -ne 0 ]; then exit "$st"; fi'


def build_script(clean_command, build_command):
    lines = [
        'phase_file=$1',
        'sdk_env=$2',
        'source_dir=$3',
        'cc_file=$4',
        'set --',
        "printf 'sdk_env' > \"$phase_file\"",
        'if [ -n "$sdk_env" ]; then',
        '  echo "' + MARK + 'SDK の環境設定ファイルを読み込みます"',
        '  . "$sdk_env" || exit $?',
        'fi',
        "printf 'cd' > \"$phase_file\"",
        'cd "$source_dir" || exit $?',
        # make が使うコンパイラー（CC が無ければ cc）と、その場所を書き残す
        '{ [ -n "${CC-}" ] && echo env || echo default; echo "${CC:-cc}"; '
        'read -r _cc_word _cc_rest <<< "${CC:-cc}"; '
        'command -v "$_cc_word" 2>/dev/null; } > "$cc_file"',
        'set -e',
        'set -o pipefail',
    ]
    if clean_command:
        lines += [
            "printf 'clean' > \"$phase_file\"",
            'echo "' + MARK + '片付けのコマンドを実行します"',
            clean_command,
            STOP_ON_FAILURE,
        ]
    lines += [
        "printf 'build' > \"$phase_file\"",
        'echo "' + MARK + 'ビルドのコマンドを実行します"',
        build_command,
        STOP_ON_FAILURE,
        "printf 'done' > \"$phase_file\"",
    ]
    return "\n".join(lines) + "\n"


def kill_group(proc):
    """ビルドと、その子のプロセスをすべて止める。"""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # bash が先に終わっても、TERM を無視した子が残ることがある
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def snapshot(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_ino, st.st_size)
    except OSError:
        return None


def iso_time(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


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

    fd, phase_file = tempfile.mkstemp(prefix="build-phase-")
    os.close(fd)
    fd, cc_file = tempfile.mkstemp(prefix="build-cc-")
    os.close(fd)
    script = build_script(s["clean_command"], s["build_command"])
    progress("ビルドを始めます：{}（フォルダ：{}）".format(s["build_command"], s["source_dir"]))
    start = time.time()
    timed_out = False
    try:
        with open(log_path, "ab") as logf:
            offset = logf.tell()
            try:
                proc = subprocess.Popen(
                    ["bash", "-c", script, "build", phase_file, s["sdk_env"], s["source_dir"],
                     cc_file],
                    stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                    cwd=workdir, start_new_session=True)
            except FileNotFoundError:
                append_log(log_path, "bash が見つかりません。\n")
                return 1, {"ok": False, "skill": SKILL, "log": log_path, "config": config,
                           "message": "PC に bash が見つかりません。ビルドには bash が要ります。"}
            try:
                rc = proc.wait(timeout=s["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                progress("{} 秒たっても終わらないので、ビルドを止めます…".format(s["timeout_seconds"]))
                kill_group(proc)
                rc = proc.returncode
            except KeyboardInterrupt:
                kill_group(proc)
                raise
        duration = round(time.time() - start, 1)
        try:
            with open(phase_file, encoding="utf-8") as f:
                phase = f.read().strip()
        except OSError:
            phase = ""
        try:
            with open(cc_file, encoding="utf-8", errors="replace") as f:
                cc_lines = f.read().splitlines()
        except OSError:
            cc_lines = []
    finally:
        for path in (phase_file, cc_file):
            try:
                os.unlink(path)
            except OSError:
                pass
    cc_from_env = bool(cc_lines) and cc_lines[0] == "env"
    compiler = cc_lines[1].strip() if len(cc_lines) > 1 and cc_lines[1].strip() else None
    compiler_path = cc_lines[2].strip() if len(cc_lines) > 2 and cc_lines[2].strip() else None

    with open(log_path, "rb") as f:
        f.seek(offset)
        out_text = f.read().decode("utf-8", "replace")
    lines = out_text.splitlines()
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

    failed_step = None
    if timed_out:
        failed_step = phase or "build"
        msg = ("ビルドが {} 秒たっても終わらないため、止めました。"
               "ビルドが入力を待っていないか、{}が短すぎないかを確かめてください。").format(
                   s["timeout_seconds"], label("timeout_seconds"))
    elif rc != 0:
        failed_step = phase or "build"
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
        note = sdk_note(compiler_path or compiler, when="built" if result["ok"] else "failed",
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


def failure_message(step, rc, s, errors, lines):
    said = [l.strip() for l in lines if l.strip() and not l.startswith(MARK)]
    if step == "sdk_env":
        msg = "{}の {} の読み込みが失敗しました（終了コード {}）。".format(
            label("sdk_env"), s["sdk_env"], rc)
        if said:
            msg += "SDK のメッセージ：「{}」。".format(short(said[0]))
        return msg + "SDK が正しく入っているかを確かめてください。"
    if step == "cd":
        return "{}の {} に移れませんでした。フォルダがあるかを確かめてください。".format(
            label("source_dir"), s["source_dir"])
    what = "片付けのコマンド" if step == "clean" else "ビルドのコマンド"
    command = short(s["clean_command" if step == "clean" else "build_command"], 80)
    if rc == 127:
        line = last_line_with(lines, ("not found", "見つかりません", "No such file"))
        return ("{}（{}）の中で、コマンドが見つかりません。「{}」。"
                "PC に入っているか、SDK を読み込めているかを確かめてください。").format(
                    what, command, short(line or "終了コード 127"))
    if rc == 126:
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
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
