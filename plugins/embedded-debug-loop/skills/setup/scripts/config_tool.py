#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YAML の設定ファイルを、コメントと項目の並びを残したまま読み書きする道具。

使い方:
  config_tool.py list [--dir フォルダ] [--workdir フォルダ]
  config_tool.py show ファイル
  config_tool.py get ファイル 項目
  config_tool.py set ファイル 項目 値 [--json]
  config_tool.py set ファイル 項目 --stdin [--json]

- 結果は、いつも JSON 1つを標準出力に書く（UTF-8）。
- 終了コード: 0 成功 / 2 ファイルや引数の問題 / 1 思わぬエラー。
- 読める YAML は、次の書き方だけ。
  - 先頭の列に「項目: 値」。入れ子はなし。
  - 行の頭が # のコメント。
  - 値は、文字（"…" か '…' か囲みなし）、数、true / false、null、
    1行のリスト ["a", "b"]、または「項目:」の次の行から「  - 値」と並べるリスト。
- 項目のすぐ上のコメントを、その項目の説明として読む。
  - 「例：…」の行は例、「選べる値：a / b」の行は書ける値の一覧。
  - 説明に「必須」があれば、必須の項目とみなす。
- Python 3.8 以上の標準ライブラリだけで動く。
"""

import errno
import json
import math
import os
import re
import sys
import tempfile
import traceback
import unicodedata

CONFIG_DIR_NAME = ".embedded-debug-loop"
MASK = "＊＊＊"

USAGE = [
    "config_tool.py list [--dir フォルダ] [--workdir フォルダ]",
    "config_tool.py show ファイル",
    "config_tool.py get ファイル 項目",
    "config_tool.py set ファイル 項目 値 [--json]",
    "config_tool.py set ファイル 項目 --stdin [--json]",
]

TYPE_WORDS = {
    "string": "文字",
    "number": "数",
    "boolean": "true か false",
    "list": "リスト",
    "null": "空（null）",
}


class ToolError(Exception):
    """利用者に見せる問題。message は日本語の文。"""

    def __init__(self, message, **extra):
        Exception.__init__(self, message)
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# 行の見分け
# ---------------------------------------------------------------------------

KEY_LINE_RE = re.compile(r"^(\w[\w.\-]*)[ \t]*:(?:[ \t]+(.*?))?[ \t]*$")
ITEM_LINE_RE = re.compile(r"^([ \t]*)-(?:[ \t]+(.*?))?[ \t]*$")
DOC_MARK_RE = re.compile(r"^(?:---|\.\.\.)[ \t]*$")
INT_RE = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$")
FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$")
LOOSE_INT_RE = re.compile(r"^[-+]?[0-9]+$")
LOOSE_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][-+]?[0-9]+)?$")
EXAMPLE_RE = re.compile(r"^\s*例\s*[：:]\s*(.*?)\s*$")
CHOICES_RE = re.compile(r"^\s*選べる値\s*[：:]\s*(.*?)\s*$")
REQUIRED_RE = re.compile(r"必須(?!で(?:は)?(?:ない|なく|ありません))")
SECRET_KEY_RE = re.compile(r"(password|passwd|passphrase|secret|token|api_?key|パスワード|暗証)", re.IGNORECASE)


def _line_for_message(text):
    """エラーの文に添える行の中身。パスワードなどを含みそうな行は、中身を見せない。"""
    text = text.strip()
    if SECRET_KEY_RE.search(text):
        return "。"
    return "：" + text


def _classify(text):
    if text.strip() == "":
        return "blank"
    if text.lstrip(" \t").startswith("#"):
        return "comment"
    if DOC_MARK_RE.match(text):
        return "doc"
    if ITEM_LINE_RE.match(text):
        return "item"
    if text[0] in " \t":
        return "indented"
    if KEY_LINE_RE.match(text):
        return "key"
    return "other"


# ---------------------------------------------------------------------------
# 値の読み取り
# ---------------------------------------------------------------------------

_DQ_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}
_HEX_LEN = {"x": 2, "u": 4, "U": 8}


def _decode_double(body, line_no):
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(body):
            raise ToolError("{} 行目の \"…\" の中の「\\」の使い方が正しくありません。「\\」そのものは「\\\\」と書いてください。".format(line_no))
        nxt = body[i + 1]
        if nxt in _DQ_ESCAPES:
            out.append(_DQ_ESCAPES[nxt])
            i += 2
            continue
        if nxt in _HEX_LEN:
            size = _HEX_LEN[nxt]
            digits = body[i + 2:i + 2 + size]
            if len(digits) != size or not re.match(r"^[0-9A-Fa-f]+$", digits):
                raise ToolError("{} 行目の \"…\" の中の「\\{}」の書き方が正しくありません。".format(line_no, nxt))
            out.append(chr(int(digits, 16)))
            i += 2 + size
            continue
        raise ToolError("{} 行目の \"…\" の中に、読めない「\\{}」があります。「\\」そのものは「\\\\」と書いてください。".format(line_no, nxt))
    text = "".join(out)
    # JSON と同じく、😀 のような組を1文字にまとめる。
    try:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        pass
    return text


def _scan_quoted(text, pos, line_no):
    """text[pos] の引用符から始まる文字を読み、(値, 次の位置) を返す。"""
    quote = text[pos]
    i = pos + 1
    if quote == '"':
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                return _decode_double(text[pos + 1:i], line_no), i + 1
            i += 1
    else:
        parts = []
        while i < len(text):
            if text[i] == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    parts.append("'")
                    i += 2
                    continue
                return "".join(parts), i + 1
            parts.append(text[i])
            i += 1
    raise ToolError("{} 行目の {} が閉じていません。値は1行で書き、最後を {} で閉じてください。".format(
        line_no, quote, quote))


def _resolve_plain(text):
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if INT_RE.match(text):
        return int(text)
    if FLOAT_RE.match(text):
        return float(text)
    return text


def _check_plain(text, line_no):
    first = text[0]
    if first in "&*!":
        raise ToolError("{} 行目の値は「{}」で始まっています。この書き方は読めません。値を \"…\" で囲んでください。".format(line_no, first))
    if first in "|>":
        raise ToolError("{} 行目の「{}」で始まる、複数行の文字には対応していません。値は \"…\" で囲んで1行で書いてください。".format(line_no, first))
    if first in "{":
        raise ToolError("{} 行目の {{…}} の書き方（入れ子）には対応していません。1つの項目には、1つの値か1行のリストを書いてください。".format(line_no))
    if first in "@`%,]}":
        raise ToolError("{} 行目の「{}」で始まる値は、\"…\" で囲んでください。".format(line_no, first))
    if text == "-" or text.startswith("- "):
        raise ToolError("{} 行目の書き方（入れ子のリスト）には対応していません。リストは [\"a\", \"b\"] の形で1行に書いてください。".format(line_no))
    if re.search(r":(?:[ \t]|$)", text):
        raise ToolError("{} 行目の値に「: 」があります。この値は \"…\" で囲んでください。".format(line_no))


def _split_inline_comment(text):
    """囲みのない値の後ろの「 #…」を分ける。"""
    m = re.search(r"[ \t]#", text)
    if not m:
        return text, ""
    body = text[:m.start()].rstrip(" \t")
    return body, text[len(body):]


def _rest_is_comment(rest, line_no):
    if rest.strip() == "":
        return ""
    if re.match(r"^[ \t]+#", rest):
        return rest
    raise ToolError("{} 行目の値の後ろに、余分な文字があります。"
                    "囲んだ値の中の ' は ''、\" は \\\" と書いてください。".format(line_no))


def _parse_flow_list(text, line_no):
    """text は "[" で始まる。(リスト, 後ろのコメント) を返す。"""
    values = []
    i = 1
    expect_value = True
    while True:
        while i < len(text) and text[i] in " \t":
            i += 1
        if i >= len(text):
            raise ToolError("{} 行目のリストが「]」で閉じていません。リストは1行で書いてください。".format(line_no))
        ch = text[i]
        if ch == "]":
            return values, _rest_is_comment(text[i + 1:], line_no)
        if ch == ",":
            if expect_value:
                raise ToolError("{} 行目のリストに、空の値があります（「,」が続いています）。".format(line_no))
            expect_value = True
            i += 1
            continue
        if not expect_value:
            raise ToolError("{} 行目のリストの値の区切りには「,」を使ってください。".format(line_no))
        if ch in "[{":
            raise ToolError("{} 行目のリストの中のリストや {{…}}（入れ子）には対応していません。".format(line_no))
        if ch in "\"'":
            value, i = _scan_quoted(text, i, line_no)
            values.append(value)
        else:
            j = i
            while j < len(text) and text[j] not in ",]":
                if text[j] == "#" and text[j - 1] in " \t":
                    raise ToolError("{} 行目のリストが「]」で閉じていません。リストは1行で書いてください。".format(line_no))
                j += 1
            word = text[i:j].strip()
            _check_plain(word, line_no)
            values.append(_resolve_plain(word))
            i = j
        expect_value = False


def parse_value_text(text, line_no):
    """「項目:」の後ろの文字を読む。(値, 形, 後ろのコメント) を返す。

    形は "empty"（何もない）/ "scalar" / "flow"（1行のリスト）。
    """
    text = text.strip(" \t")
    if text == "" or text.startswith("#"):
        return None, "empty", (" " + text if text else "")
    first = text[0]
    if first in "\"'":
        value, end = _scan_quoted(text, 0, line_no)
        return value, "scalar", _rest_is_comment(text[end:], line_no)
    if first == "[":
        values, comment = _parse_flow_list(text, line_no)
        return values, "flow", comment
    body, comment = _split_inline_comment(text)
    _check_plain(body, line_no)
    return _resolve_plain(body), "scalar", comment


# ---------------------------------------------------------------------------
# 値の書き出し
# ---------------------------------------------------------------------------

def _render_string(value):
    text = json.dumps(value, ensure_ascii=False)

    def esc(m):
        return "\\u{:04x}".format(ord(m.group(0)))

    # YAML で書けない文字（制御文字など）や、YAML が改行とみなす文字は \uXXXX にする。
    return re.sub("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff\ud800-\udfff]", esc, text)


def _render_float(value):
    if not math.isfinite(value):
        raise ToolError("無限大や NaN は書けません。ふつうの数で書いてください。")
    text = repr(value)
    if "e" in text or "E" in text:
        mant, exp = re.split(r"[eE]", text, maxsplit=1)
        if "." not in mant:
            mant += ".0"
        if exp[0] not in "+-":
            exp = "+" + exp
        text = mant + "e" + exp
    elif "." not in text:
        text += ".0"
    return text


def render_scalar(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _render_float(value)
    if isinstance(value, str):
        return _render_string(value)
    raise ToolError("この値は書けません。")


def render_value(value):
    if isinstance(value, list):
        return "[" + ", ".join(render_scalar(v) for v in value) + "]"
    return render_scalar(value)


# ---------------------------------------------------------------------------
# 種類・表示
# ---------------------------------------------------------------------------

def type_of(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    return "string"


def is_empty(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def display_scalar(value):
    if isinstance(value, str):
        return value
    return render_scalar(value)


def same_value(a, b):
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(b, ensure_ascii=False, sort_keys=True) \
        and type_of(a) == type_of(b)


def is_secret_key(key):
    return bool(SECRET_KEY_RE.search(key)) and not key.lower().endswith("_env")


# ---------------------------------------------------------------------------
# 設定ファイル
# ---------------------------------------------------------------------------

class Item(object):
    def __init__(self, key, value, style, start, end, comments, inline):
        self.key = key
        self.value = value
        self.style = style          # empty / scalar / flow / block
        self.start = start          # 項目の行（0 から数える）
        self.end = end              # 項目の最後の行（ブロックのリストの最後の行）
        self.comments = comments    # 項目のすぐ上のコメント（# を外した文）
        self.inline = inline        # 「項目: 値」の行の後ろのコメント
        self.description = []
        self.examples_text = []
        self.choices = []
        for line in comments:
            m = EXAMPLE_RE.match(line)
            if m:
                self.examples_text.append(m.group(1))
                continue
            m = CHOICES_RE.match(line)
            if m:
                self.choices.extend(parse_choices(m.group(1)))
                continue
            self.description.append(line)

    @property
    def type(self):
        return type_of(self.value)

    @property
    def required(self):
        return any(REQUIRED_RE.search(line) for line in self.comments)

    @property
    def secret(self):
        return is_secret_key(self.key)

    def examples(self):
        out = []
        for text in self.examples_text:
            try:
                value, style, _ = parse_value_text(text, 0)
            except ToolError:
                value, style = text, "scalar"
            if style == "empty":
                continue
            if self.type == "string" and not isinstance(value, str):
                value = text
            elif self.type == "list" and not isinstance(value, list):
                value = [value]
            out.append(value)
        return out


def parse_choices(text):
    text = text.strip().rstrip("。")
    if " / " in text:
        parts = text.split(" / ")
    else:
        parts = re.split(r"\s*[/／、,|｜]\s*", text)
    out = []
    for part in parts:
        value = re.split(r"\s*[（(]", part.strip(), maxsplit=1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value and value not in out:
            out.append(value)
    return out


def _comment_text(line):
    text = line.rstrip("\r").lstrip(" \t")[1:]
    if text.startswith(" "):
        text = text[1:]
    return text.rstrip()


class Document(object):
    def __init__(self, path, data):
        self.path = path
        self.bom = data.startswith(b"\xef\xbb\xbf")
        if self.bom:
            data = data[3:]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError("ファイルを UTF-8 の文字として読めません。UTF-8 で保存し直してください：{}".format(path))
        self.final_newline = text.endswith("\n")
        if text == "":
            self.lines = []
        else:
            self.lines = text.split("\n")
            if self.final_newline:
                self.lines.pop()
        self.items = []
        self.by_key = {}
        self.header = []
        self._parse()

    def text_of(self, index):
        return self.lines[index].rstrip("\r")

    def _parse(self):
        lines = [self.text_of(i) for i in range(len(self.lines))]
        kinds = [_classify(t) for t in lines]
        n = len(lines)

        # 先頭のコメント（ファイルの説明）。項目に直接つながるものは除く。
        i = 0
        while i < n and kinds[i] in ("blank", "doc"):
            i += 1
        j = i
        while j < n and lines[j].startswith("#"):
            j += 1
        if j > i and (j >= n or kinds[j] != "key"):
            self.header = [_comment_text(lines[k]) for k in range(i, j)]

        i = 0
        while i < n:
            kind = kinds[i]
            line_no = i + 1
            if kind in ("blank", "comment", "doc"):
                i += 1
                continue
            if kind == "item":
                raise ToolError("{} 行目のリストの行（- …）が、どの項目にも属していません。"
                                "すぐ上に「項目:」の行を置いてください。".format(line_no))
            if kind == "indented":
                raise ToolError("{} 行目は読めません。行の頭に空白があります。入れ子や、複数行に分けた値には対応していません{}".format(
                    line_no, _line_for_message(lines[i])))
            if kind == "other":
                raise ToolError("{} 行目は読めません。「項目: 値」の形で書いてください{}".format(
                    line_no, _line_for_message(lines[i])))

            m = KEY_LINE_RE.match(lines[i])
            key = m.group(1)
            if key in self.by_key:
                raise ToolError("項目「{}」が2回書かれています（{} 行目と {} 行目）。1つにしてください。".format(
                    key, self.by_key[key].start + 1, line_no))
            try:
                value, style, inline, end = self._parse_value(i, m.group(2) or "", lines, kinds)
            except ToolError:
                if not is_secret_key(key):
                    raise
                # パスワードなどの値は、エラーの文にも出さない。
                raise ToolError("{} 行目の「{}」の値の書き方が正しくありません（値は見せません）。"
                                "値は \"…\" で囲んで、1行で書いてください。".format(line_no, key))

            # 項目のすぐ上のコメント
            k = i - 1
            comments = []
            while k >= 0 and lines[k].startswith("#"):
                comments.insert(0, _comment_text(lines[k]))
                k -= 1

            item = Item(key, value, style, i, end, comments, inline)
            self.items.append(item)
            self.by_key[key] = item
            i = end + 1

    def _parse_value(self, i, text, lines, kinds):
        """i 行目の項目の値を読む。(値, 形, 後ろのコメント, 最後の行) を返す。"""
        value, style, inline = parse_value_text(text, i + 1)
        n = len(lines)
        j = i + 1
        if style != "empty":
            while j < n and kinds[j] in ("blank", "comment"):
                j += 1
            if j < n and kinds[j] in ("item", "indented"):
                raise ToolError("{} 行目は読めません。値を複数行に分ける書き方には対応していません{}".format(
                    j + 1, _line_for_message(lines[j])))
            return value, style, inline, i
        # 「項目:」の次の行から「  - 値」と並べたリスト
        values = []
        last = None
        indent = None
        while j < n:
            if kinds[j] in ("blank", "comment"):
                j += 1
                continue
            if kinds[j] == "indented":
                raise ToolError("{} 行目は読めません。入れ子の書き方には対応していません{}".format(
                    j + 1, _line_for_message(lines[j])))
            if kinds[j] != "item":
                break
            im = ITEM_LINE_RE.match(lines[j])
            if indent is None:
                indent = im.group(1)
            elif im.group(1) != indent:
                raise ToolError("{} 行目のリストの字下げが、上の行とそろっていません。".format(j + 1))
            v, s, _ = parse_value_text(im.group(2) or "", j + 1)
            if s == "flow":
                raise ToolError("{} 行目のリストの中のリスト（入れ子）には対応していません。".format(j + 1))
            values.append(v)
            last = j
            j += 1
        if last is None:
            return value, style, inline, i
        return values, "block", inline, last

    def get(self, key):
        item = self.by_key.get(key)
        if item is None:
            raise ToolError(
                "このファイルに「{}」という項目はありません。ある項目：{}".format(
                    key, "、".join(it.key for it in self.items) or "（なし）"),
                file=self.path, keys=[it.key for it in self.items])
        return item

    def to_bytes(self, lines):
        text = "\n".join(lines)
        if self.final_newline and lines:
            text += "\n"
        data = text.encode("utf-8")
        if self.bom:
            data = b"\xef\xbb\xbf" + data
        return data


def resolve_path(path, workdir):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(workdir or os.getcwd(), path)
    return os.path.abspath(path)


def load(path):
    if not os.path.exists(path):
        raise ToolError("ファイルがありません：{}".format(path), file=path)
    if os.path.isdir(path):
        raise ToolError("フォルダが指定されました。設定ファイルを指定してください：{}".format(path), file=path)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ToolError("ファイルを読めません（{}）：{}".format(_os_reason(e), path), file=path)
    return Document(path, data)


def _os_reason(err):
    if err.errno in (errno.EACCES, errno.EPERM):
        return "権限がありません"
    if err.errno == errno.ENOSPC:
        return "ディスクがいっぱいです"
    if err.errno == errno.EROFS:
        return "読み取り専用の場所です"
    if err.errno in (errno.ENOENT, errno.ENOTDIR):
        return "場所が見つかりません"
    return err.strerror or type(err).__name__


def write_atomic(path, data):
    target = os.path.realpath(path)
    folder = os.path.dirname(target)
    try:
        mode = os.stat(target).st_mode & 0o7777
    except OSError:
        mode = None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(target) + ".", suffix=".tmp", dir=folder)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
        tmp = None
    except OSError as e:
        raise ToolError("ファイルに書き込めません（{}）。ファイルとフォルダの権限を確かめてください：{}".format(
            _os_reason(e), path), file=path)
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 結果の形
# ---------------------------------------------------------------------------

EMPTY_WORD = "（空）"


def masked(item, value):
    """secret の項目の値は、空でなければ ＊＊＊ にする。空の値は、空のまま返す。"""
    if item.secret and not is_empty(value):
        return MASK
    return value


def shown_value(item, value):
    """文の中に書く値。空なら（空）、secret なら ＊＊＊。"""
    if is_empty(value):
        return EMPTY_WORD
    if item.secret:
        return MASK
    return render_value(value)


def item_info(doc, item):
    info = {
        "key": item.key,
        "comment": "\n".join(item.description).strip(),
        "value": masked(item, item.value),
        "type": item.type,
        "required": item.required,
        "empty": is_empty(item.value),
        "secret": item.secret,
        "examples": [masked(item, v) for v in item.examples()],
        "choices": list(item.choices),
        "line": item.start + 1,
    }
    env_key = item.key + "_env"
    if env_key in doc.by_key:
        info["env_alternative"] = env_key
    if item.key.lower().endswith("_env"):
        base = item.key[:-4]
        if base in doc.by_key:
            info["env_for"] = base
        if isinstance(item.value, str) and item.value.strip():
            info["env_defined"] = bool(os.environ.get(item.value.strip()))
    return info


# ---------------------------------------------------------------------------
# 値の受け取り
# ---------------------------------------------------------------------------

def _check_structure(value, key):
    """JSON で受け取った値が、この道具で書ける形かを確かめる。"""
    if isinstance(value, dict):
        raise ToolError("「{}」に {{…}}（名前付きの値の組）は書けません。".format(key))
    for v in (value if isinstance(value, list) else [value]):
        if isinstance(v, (list, dict)):
            raise ToolError("「{}」に、リストの中のリストや {{…}}（入れ子）は書けません。".format(key))
        if isinstance(v, float) and not math.isfinite(v):
            raise ToolError("「{}」に、無限大や NaN は書けません。ふつうの数で書いてください。".format(key))


def _type_error(item, new_type):
    key = item.key
    want = item.type
    if new_type == "null":
        hint = {"string": "空にするときは、--json を付けずに空の文字（\"\"）を書いてください。",
                "list": "空にするときは、--json を付けて [] を書いてください。"}.get(want, "")
        return ToolError("「{}」は{}の項目なので、null にはできません。{}".format(
            key, TYPE_WORDS.get(want, want), hint), key=key, expected=want, given=new_type)
    if want == "list":
        return ToolError("「{}」はリストの項目です。[\"a\", \"b\"] の形で書いてください。".format(key),
                         key=key, expected=want, given=new_type)
    if want == "string":
        return ToolError("「{}」は文字の項目です。--json を付けずに、そのまま書いてください。".format(key),
                         key=key, expected=want, given=new_type)
    if want == "number":
        return ToolError("「{}」は数の項目です。数だけを書いてください（今の値：{}）。".format(
            key, shown_value(item, item.value)), key=key, expected=want, given=new_type)
    if want == "boolean":
        return ToolError("「{}」は true か false の項目です。どちらかを書いてください。".format(key),
                         key=key, expected=want, given=new_type)
    return ToolError("「{}」は{}の項目です。{}は書けません。".format(
        key, TYPE_WORDS.get(want, want), TYPE_WORDS.get(new_type, new_type)), key=key, expected=want, given=new_type)


def _guess_type(item):
    """値が空（null）の項目は、例の種類から、書く値の種類を見当づける。"""
    kinds = set(type_of(v) for v in item.examples()) - {"null"}
    return kinds.pop() if len(kinds) == 1 else "string"


def convert_input(item, raw, use_json):
    key = item.key
    want = item.type
    shown = (EMPTY_WORD if raw == "" else MASK) if item.secret else raw
    if want == "null" and not use_json:
        guess = _guess_type(item)
        if guess != "string":
            try:
                fake = Item(key, {"number": 0, "boolean": False, "list": []}[guess], "scalar", 0, 0, item.comments, "")
                return convert_input(fake, raw, False)
            except ToolError:
                pass
    if use_json:
        try:
            value = json.loads(raw)
        except ValueError:
            raise ToolError("「{}」の値を JSON として読めません：{}。文字を書くときは、--json を外してください。".format(
                key, shown), key=key)
    elif want == "number":
        text = unicodedata.normalize("NFKC", raw).strip()
        if LOOSE_INT_RE.match(text):
            value = int(text)
        elif LOOSE_FLOAT_RE.match(text):
            value = float(text)
        else:
            raise _type_error(item, "string")
    elif want == "boolean":
        text = unicodedata.normalize("NFKC", raw).strip().lower()
        if text not in ("true", "false"):
            raise _type_error(item, "string")
        value = text == "true"
    elif want == "list":
        text = raw.strip()
        if not text.startswith("["):
            raise _type_error(item, "string")
        try:
            value = json.loads(text)
        except ValueError:
            try:
                value, _, comment = parse_value_text(text, 0)
            except ToolError:
                comment = "x"
            if comment.strip():
                raise ToolError("「{}」の値をリストとして読めません：{}。[\"a\", \"b\"] の形で書いてください。".format(
                    key, shown), key=key)
    else:
        value = raw
    _check_structure(value, key)

    new_type = type_of(value)
    if want != "null" and new_type != want:
        raise _type_error(item, new_type)

    if item.choices and not is_empty(value):
        given = value if isinstance(value, list) else [value]
        bad = [display_scalar(v) for v in given if display_scalar(v) not in item.choices]
        if bad:
            raise ToolError("「{}」に書ける値は、{} のどれかです（書こうとした値：{}）。".format(
                key, " / ".join(item.choices), MASK if item.secret else "、".join(bad)),
                key=key, choices=list(item.choices))
    return value


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------

def cmd_list(opts, pos):
    if pos:
        raise ToolError("list には、ファイルの名前を付けません。フォルダを指定するときは --dir を使います。")
    workdir = os.path.abspath(os.path.expanduser(opts["workdir"])) if opts["workdir"] else os.getcwd()
    if opts["dir"]:
        folder = resolve_path(opts["dir"], workdir)
    else:
        folder = os.path.join(workdir, CONFIG_DIR_NAME)
    if not os.path.isdir(folder):
        return {"ok": True, "dir": folder, "exists": False, "files": [], "details": []}
    try:
        names = sorted(os.listdir(folder))
    except OSError as e:
        raise ToolError("フォルダの中を見られません（{}）：{}".format(_os_reason(e), folder))
    files = []
    details = []
    for name in names:
        path = os.path.join(folder, name)
        if not name.lower().endswith((".yaml", ".yml")) or not os.path.isfile(path):
            continue
        files.append(path)
        try:
            doc = load(path)
            details.append({
                "file": path,
                "name": name,
                "header": doc.header[0] if doc.header else "",
                "items": len(doc.items),
                "missing": [it.key for it in doc.items if it.required and is_empty(it.value)],
            })
        except ToolError as e:
            details.append({"file": path, "name": name, "error": e.message})
    return {"ok": True, "dir": folder, "exists": True, "files": files, "details": details}


def _need(pos, count, usage):
    if len(pos) != count:
        raise ToolError("引数の数が合いません。使い方：{}".format(usage), usage=USAGE)


def cmd_show(opts, pos):
    _need(pos, 1, USAGE[1])
    path = resolve_path(pos[0], opts["workdir"])
    doc = load(path)
    items = [item_info(doc, it) for it in doc.items]
    return {
        "ok": True,
        "file": path,
        "header": "\n".join(doc.header).strip(),
        "items": items,
        "missing": [it["key"] for it in items if it["required"] and it["empty"]],
    }


def cmd_get(opts, pos):
    _need(pos, 2, USAGE[2])
    path = resolve_path(pos[0], opts["workdir"])
    doc = load(path)
    item = doc.get(pos[1])
    return {
        "ok": True,
        "file": path,
        "key": item.key,
        "value": masked(item, item.value),
        "type": item.type,
        "empty": is_empty(item.value),
        "secret": item.secret,
    }


def _read_stdin():
    data = sys.stdin.buffer.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError("標準入力を UTF-8 の文字として読めません。")
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return text


def cmd_set(opts, pos):
    if opts["stdin"]:
        _need(pos, 2, USAGE[4])
        raw = _read_stdin()
    else:
        _need(pos, 3, USAGE[3])
        raw = pos[2]
    path = resolve_path(pos[0], opts["workdir"])
    doc = load(path)
    item = doc.get(pos[1])
    value = convert_input(item, raw, opts["json"])
    result = {
        "ok": True,
        "file": path,
        "key": item.key,
        "value": masked(item, value),
        "previous": masked(item, item.value),
        "type": type_of(value),
        "changed": False,
    }
    if same_value(value, item.value):
        return result

    eol = "\r" if doc.lines[item.start].endswith("\r") else ""
    new_lines = ["{}: {}{}{}".format(item.key, render_value(value), item.inline, eol)]
    # 「項目:」の下に並べたリストを1行に書き換えるときも、中のコメントは消さずに残す。
    # 次の項目の説明に混ざらないよう、字下げして項目の下に置く。
    for k in range(item.start + 1, item.end + 1):
        text = doc.text_of(k)
        kind = _classify(text)
        k_eol = "\r" if doc.lines[k].endswith("\r") else ""
        if kind == "comment":
            new_lines.append(("  " + text if text.startswith("#") else text) + k_eol)
        elif kind == "item":
            m = ITEM_LINE_RE.match(text)
            _, _, inline = parse_value_text(m.group(2) or "", k + 1)
            if inline.strip():
                new_lines.append((m.group(1) or "  ") + inline.strip() + k_eol)
    lines = doc.lines[:item.start] + new_lines + doc.lines[item.end + 1:]
    data = doc.to_bytes(lines)

    # 書く前に読み直して、ほかの項目と説明が変わらないことを確かめる。
    check = Document(path, data)
    if [it.key for it in check.items] != [it.key for it in doc.items] or check.header != doc.header:
        raise ToolError("書き込むと項目の並びや説明が変わるため、書き込みをやめました。"
                        "ファイルは元のままです。この項目は、ファイルを直接書き換えてください。", key=item.key)
    for it in check.items:
        before = doc.by_key[it.key]
        expected = value if it.key == item.key else before.value
        if not same_value(it.value, expected) or it.comments != before.comments:
            raise ToolError("書き込むと項目「{}」の値か説明が変わるため、書き込みをやめました。"
                            "ファイルは元のままです。この項目は、ファイルを直接書き換えてください。".format(it.key),
                            key=item.key)
    write_atomic(path, data)
    result["changed"] = True
    return result


def cmd_help(opts, pos):
    return {"ok": True, "usage": USAGE}


COMMANDS = {
    "list": cmd_list,
    "show": cmd_show,
    "get": cmd_get,
    "set": cmd_set,
    "help": cmd_help,
}


def parse_args(argv):
    opts = {"json": False, "stdin": False, "dir": None, "workdir": None}
    pos = []
    only_pos = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if only_pos:
            pos.append(a)
        elif a == "--":
            only_pos = True
        elif a == "--json":
            opts["json"] = True
        elif a == "--stdin":
            opts["stdin"] = True
        elif a in ("--dir", "--workdir"):
            if i + 1 >= len(argv):
                raise ToolError("{} の後ろに、フォルダを書いてください。".format(a))
            opts[a[2:]] = argv[i + 1]
            i += 1
        elif a.startswith("--dir=") or a.startswith("--workdir="):
            name, val = a[2:].split("=", 1)
            opts[name] = val
        else:
            pos.append(a)
        i += 1
    return opts, pos


def run(argv):
    if not argv or argv[0] in ("-h", "--help"):
        return cmd_help(None, None)
    cmd = argv[0]
    if cmd not in COMMANDS:
        raise ToolError("知らないサブコマンドです：{}。使えるのは list / show / get / set です。".format(cmd), usage=USAGE)
    opts, pos = parse_args(argv[1:])
    return COMMANDS[cmd](opts, pos)


def emit(obj):
    data = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8", "backslashreplace")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        result = run(argv)
        code = 0
    except ToolError as e:
        result = {"ok": False, "error": e.message}
        for k, v in e.extra.items():
            result.setdefault(k, v)
        code = 2
    except Exception as e:  # 思わぬエラーでも、標準出力には JSON だけを書く。
        traceback.print_exc(file=sys.stderr)
        result = {"ok": False, "error": "思わぬエラーが起きました（{}: {}）。設定ファイルは書き換えていません。".format(
            type(e).__name__, e)}
        code = 1
    emit(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
