"""Rewrite config.yaml and keep the comments people wrote in it (REQ-102).

Only the text of the values that change is replaced. Where a value is in the file comes from
``yaml.compose`` (pyyaml: no other library). ``edit`` gives ``None`` for a file written in a way it
does not handle and for a result that does not pass the check; the caller then writes the file
whole, as before (the comment lines at the top are kept, the other comments are lost).

What is lost on purpose: the comments of a setting or a list value that is removed (the comment at
the end of its line and the comment lines right above it). The check compares the old and the new
text on their own (values read back, and every comment on the same element as before), not by
following the steps of the rewrite."""
from __future__ import annotations

import difflib
from collections import Counter
from typing import Any

import yaml

WIDE = 10 ** 9  # no folding of a value written in place


class Unsupported(Exception):
    """A way of writing YAML that the rewrite does not handle."""


def edit(text: str, new: dict[str, Any]) -> str | None:
    """``text`` with its values changed to ``new``, its comments kept; ``None`` when that cannot be done."""
    try:
        if _unsupported(text):
            return None
        root = yaml.compose(text)
        if not isinstance(root, yaml.MappingNode) or not isinstance(new, dict):
            return None
        out = _Rewrite(text).run(root, new)
        return out if check(text, out, new) else None
    except (Unsupported, yaml.YAMLError, ValueError, TypeError, IndexError, KeyError, RecursionError):
        return None


def has_comments(text: str) -> bool:
    """Whether writing the file whole would lose comments: a comment after the first line with data."""
    try:
        found = _comments(text)
    except (yaml.YAMLError, ValueError):
        return "#" in text
    lines = text.splitlines()
    data = [i for i, line in enumerate(lines)
            if line.strip() and not (i in found and not line[:found[i][0]].strip())]
    if not data:
        return False  # only the lines at the top, which the whole rewrite keeps
    return any(i >= data[0] or not lines[i].startswith("#") for i in found)


# ---- reading -----------------------------------------------------------------------------------------


def _unsupported(text: str) -> bool:
    """「|」「>」の値、「&」「*」、タグ、文書が2つ以上"""
    if any(char in text for char in "\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"):
        return True  # line breaks that yaml and Python count differently
    documents = 0
    for token in yaml.scan(text):
        if isinstance(token, (yaml.AnchorToken, yaml.AliasToken, yaml.TagToken, yaml.DirectiveToken)):
            return True
        if isinstance(token, yaml.ScalarToken) and token.style in ("|", ">"):
            return True
        if isinstance(token, yaml.DocumentStartToken):
            documents += 1
    return documents > 1


def _value(node: yaml.Node) -> Any:
    loader = yaml.SafeLoader("")
    try:
        return loader.construct_object(node, deep=True)
    finally:
        loader.dispose()


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _comments(text: str) -> dict[int, tuple[int, str]]:
    """Line → (column, text) of the comment on it. A ``#`` inside a value (a quoted string) is not one."""
    spans = [(t.start_mark.index, t.end_mark.index) for t in yaml.scan(text) if isinstance(t, yaml.ScalarToken)]
    found: dict[int, tuple[int, str]] = {}
    offset = 0
    for number, line in enumerate(text.splitlines(keepends=True)):
        body = line.rstrip("\r\n")
        for col, char in enumerate(body):
            if char != "#" or (col and body[col - 1] not in " \t"):
                continue
            at = offset + col
            if any(a <= at < b for a, b in spans):
                continue
            found[number] = (col, body[col:].rstrip())
            break
        offset += len(line)
    return found


def _last_line(node: yaml.Node) -> int:
    """The last line that has text of ``node`` (the end mark of a block collection is on the next line)."""
    if isinstance(node, yaml.ScalarNode) or node.flow_style or not node.value:
        mark = node.end_mark
        return mark.line if mark.column else mark.line - 1
    last = node.value[-1]
    return _last_line(last[1] if isinstance(node, yaml.MappingNode) else last)


# ---- rewriting ---------------------------------------------------------------------------------------


class _Rewrite:
    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.offs = [0]
        for line in self.lines:
            self.offs.append(self.offs[-1] + len(line))
        self.edits: list[tuple[int, int, str]] = []  # (start, end, new text); end == start: an insertion
        self.comment_lines = _comments(text)

    def run(self, root: yaml.MappingNode, new: dict[str, Any]) -> str:
        self._map(root, new)
        out = self.text
        # From the end of the text. At the same place, a range is replaced before the insertions, and the
        # insertions go in the order they were made (the first one ends up first).
        order = sorted(enumerate(self.edits), key=lambda e: (e[1][0], e[1][1] > e[1][0], e[0]), reverse=True)
        for _, (start, end, replacement) in order:
            out = out[:start] + replacement + out[end:]
        if out and not out.endswith("\n"):
            out += "\n"
        return out

    def pos(self, mark: yaml.Mark) -> int:
        return self.offs[mark.line] + mark.column

    def line_start(self, line: int) -> int:
        return self.offs[min(line, len(self.lines))]

    def _full_comment(self, line: int) -> bool:
        return 0 <= line < len(self.lines) and line in self.comment_lines and not self.lines[line][:self.comment_lines[line][0]].strip()

    def top(self, line: int, floor: int) -> int:
        """The first of the comment lines right above ``line`` (no blank line between), not above ``floor``."""
        while line - 1 > floor and self._full_comment(line - 1):
            line -= 1
        return line

    # mappings

    def _map(self, node: yaml.MappingNode, new: dict[str, Any], floor: int = -1) -> None:
        if node.flow_style:
            raise Unsupported("1行に書いたマップ")
        present: dict[str, tuple[yaml.Node, yaml.Node]] = {}
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.value in present:
                raise Unsupported("キー")
            present[key.value] = (key, value)
        for name, (key, value) in present.items():
            if name not in new:
                start = self.top(key.start_mark.line, floor)
                self.edits.append((self.line_start(start), self.line_start(_last_line(value) + 1), ""))
                continue
            want = new[name]
            if isinstance(value, yaml.MappingNode):
                if not isinstance(want, dict):
                    raise Unsupported("マップでなくなる値")
                self._map(value, want, key.start_mark.line)
            elif isinstance(value, yaml.SequenceNode):
                if not isinstance(want, list):
                    raise Unsupported("リストでなくなる値")
                self._list(key, value, want)
            else:
                if _same(_value(value), want):
                    continue
                if isinstance(want, (dict, list)):
                    raise Unsupported("マップやリストになる値")
                self._scalar(key, value, want)
        added = [name for name in new if name not in present]
        if added:
            at = self.line_start(_last_line(node) + 1)
            indent = node.value[0][0].start_mark.column
            block = "".join(_block(name, new[name], indent) for name in added)
            if at == len(self.text) and self.text and not self.text.endswith("\n"):
                block = "\n" + block
            self.edits.append((at, at, block))

    def _scalar(self, key: yaml.Node, value: yaml.ScalarNode, want: Any) -> None:
        start, end = self.pos(value.start_mark), self.pos(value.end_mark)
        text = _scalar(want)
        if start == end:  # 「キー:」だけで値がない
            start = end = self.pos(key.end_mark)
            rest = self.text[start:]
            colon = rest.index(":")
            start = end = start + colon + 1
            text = " " + text
        self.edits.append((start, end, text))

    # lists

    def _list(self, key: yaml.Node, seq: yaml.SequenceNode, want: list[Any]) -> None:
        items = seq.value
        if any(not isinstance(item, yaml.ScalarNode) for item in items) or any(isinstance(x, (dict, list)) for x in want):
            raise Unsupported("リストの中のマップやリスト")
        old = [_value(item) for item in items]
        if len(old) == len(want) and all(_same(a, b) for a, b in zip(old, want)):
            return
        if seq.flow_style:
            if items or not want:
                self.edits.append((self.pos(seq.start_mark), self.pos(seq.end_mark), _scalar(want)))
                return
            # 「キー: []」に値を入れる：[] を消し、次の行から「- 値」で書く（キーの行の注釈は残る）
            if seq.start_mark.line != key.start_mark.line:
                raise Unsupported("[] がキーと別の行")
            self.edits.append((self.pos(key.end_mark), self.pos(seq.end_mark), ":"))
            at = self.line_start(key.start_mark.line + 1)
            block = "".join(" " * (key.start_mark.column + 2) + "- " + _scalar(x) + "\n" for x in want)
            if at == len(self.text) and not self.text.endswith("\n"):
                block = "\n" + block
            self.edits.append((at, at, block))
            return
        if not want:
            # 空にする：「キー: []」にして、キーの行の終わりの注釈は残す
            line = key.start_mark.line
            note = self.comment_lines.get(line)
            keep = "   " + note[1] if note and note[0] > key.end_mark.column else ""
            if _last_line(key) != line:
                raise Unsupported("キーが何行か")
            self.edits.append((self.pos(key.end_mark), self.line_start(_last_line(seq) + 1), ": []" + keep + "\n"))
            return
        first_line = self.lines[items[0].start_mark.line]
        dash = first_line.index("-", 0, items[0].start_mark.column)
        gap = items[0].start_mark.column - dash
        if any(self.lines[item.start_mark.line][dash:dash + 1] != "-" for item in items):
            raise Unsupported("リストの字下げがそろっていない")
        floor = key.start_mark.line
        matcher = difflib.SequenceMatcher(a=[repr(x) for x in old], b=[repr(x) for x in want], autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and i2 - i1 == j2 - j1:
                for k in range(i2 - i1):
                    item = items[i1 + k]
                    self.edits.append((self.pos(item.start_mark), self.pos(item.end_mark), _scalar(want[j1 + k])))
                continue
            if tag in ("delete", "replace"):
                for item in items[i1:i2]:
                    start = self.top(item.start_mark.line, floor)
                    self.edits.append((self.line_start(start), self.line_start(_last_line(item) + 1), ""))
            if tag in ("insert", "replace"):
                if i1 > 0:
                    at = self.line_start(_last_line(items[i1 - 1]) + 1)
                else:
                    at = self.line_start(self.top(items[0].start_mark.line, floor))
                block = "".join(" " * dash + "-" + " " * (gap - 1) + _scalar(x) + "\n" for x in want[j1:j2])
                self.edits.append((at, at, block))


def _scalar(value: Any) -> str:
    """One value as written in place (lists in the one-line form)."""
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=WIDE).rstrip("\n")
    if text.endswith("\n..."):
        text = text[:-4]
    if "\n" in text:
        raise Unsupported("何行かになる値")
    return text


def _block(name: str, value: Any, indent: int) -> str:
    """A setting added at the end of its section, written as ct.py writes it."""
    text = yaml.safe_dump({name: value}, allow_unicode=True, sort_keys=False)
    return "".join(" " * indent + line + "\n" for line in text.splitlines())


# ---- the check ---------------------------------------------------------------------------------------


def check(old_text: str, new_text: str, new: dict[str, Any]) -> bool:
    """The new text reads back as ``new``, and each comment is where it was.

    A comment is on an element (a setting or a list value): the comment at the end of a line is on the
    element of that line, a comment line is on the element of the next line with data. Each comment of an
    element that is still there must be on that same element, with nothing between (a list value is
    followed by its position in the new list). The comments of removed elements may go; their comment lines
    may also stay. No other comment may come in."""
    if yaml.safe_load(new_text) != new or _unsupported(new_text):
        return False
    old_root, new_root = yaml.compose(old_text), yaml.compose(new_text)
    moves = _moves(old_root, new)
    expected: Counter = Counter()
    allowed: Counter = Counter()
    for kind, note, where in _notes(old_text, old_root):
        target = _follow(where, moves, new)
        (expected if target is not None else allowed)[(kind, note, target)] += 1
    found = Counter(_notes(new_text, new_root))
    for entry, count in expected.items():
        if found[entry] < count:
            return False
        found[entry] -= count
    # What is left may only be comment lines of removed elements that stayed where they were (a comment line
    # with a blank line between it and the removed setting is not right above it, and is not removed with it).
    # A comment at the end of a removed line cannot stay: it would be on another line.
    left = Counter({(kind, note): n for (kind, note, _), n in found.items() if n > 0})
    gone = Counter()
    for (kind, note, _), n in allowed.items():
        gone[(kind, note)] += n
    return all(kind == "line" and gone[(kind, note)] >= n for (kind, note), n in left.items())


def _owners(root: yaml.Node) -> dict[int, tuple]:
    """Line → the path of the element that line belongs to (a key line: the key; a 「- 値」 line: that value)."""
    owners: dict[int, tuple] = {}

    def mark(line_from: int, line_to: int, path: tuple) -> None:
        for line in range(line_from, line_to + 1):
            owners.setdefault(line, path)

    def walk(node: yaml.Node, path: tuple) -> None:
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                here = path + (("k", key.value),)
                mark(key.start_mark.line, key.start_mark.line, here)
                if isinstance(value, yaml.ScalarNode) or value.flow_style:
                    mark(key.start_mark.line, _last_line(value), here)
                else:
                    walk(value, here)
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                here = path + (("i", index),)
                mark(item.start_mark.line, item.start_mark.line, here)
                if isinstance(item, yaml.ScalarNode) or item.flow_style:
                    mark(item.start_mark.line, _last_line(item), here)
                else:
                    walk(item, here)

    if root is not None:
        walk(root, ())
    return owners


def _notes(text: str, root: yaml.Node) -> list[tuple[str, str, tuple]]:
    """(kind, comment, the element it is on). kind: "end" (end of a line) or "line" (a comment line)."""
    comments = _comments(text)
    lines = text.splitlines()
    owners = _owners(root)
    notes = []
    for line, (col, note) in sorted(comments.items()):
        if lines[line][:col].strip():
            notes.append(("end", note, owners.get(line, ("?", line))))
            continue
        nxt = next((n for n in range(line + 1, len(lines))
                    if lines[n].strip() and not (n in comments and not lines[n][:comments[n][0]].strip())), None)
        notes.append(("line", note, owners.get(nxt, ("end",)) if nxt is not None else ("end",)))
    return notes


def _moves(old_root: yaml.Node, new: Any) -> dict[tuple, tuple | None]:
    """Old path of a list value → its new path (None: removed). Keys keep their names."""
    moves: dict[tuple, tuple | None] = {}

    def walk(node: yaml.Node, want: Any, path: tuple) -> None:
        if isinstance(node, yaml.MappingNode) and isinstance(want, dict):
            for key, value in node.value:
                if key.value in want:
                    walk(value, want[key.value], path + (("k", key.value),))
        elif isinstance(node, yaml.SequenceNode) and isinstance(want, list):
            old = [repr(_value(item)) for item in node.value]
            matcher = difflib.SequenceMatcher(a=old, b=[repr(x) for x in want], autojunk=False)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                same = tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1)
                for k in range(i2 - i1):
                    moves[path + (("i", i1 + k),)] = path + (("i", j1 + k),) if same else None

    walk(old_root, new, ())
    return moves


def _follow(path: tuple, moves: dict[tuple, tuple | None], new: Any) -> tuple | None:
    """The path in the new text of the element at ``path`` in the old one (None: it was removed)."""
    if path == ("end",) or path[0] == "?":
        return path
    out: tuple = ()
    data = new
    for depth, step in enumerate(path):
        if step[0] == "k":
            if not isinstance(data, dict) or step[1] not in data:
                return None
            data = data[step[1]]
            out += (step,)
            continue
        moved = moves.get(path[:depth + 1])
        if moved is None or not isinstance(data, list) or moved[-1][1] >= len(data):
            return None
        data = data[moved[-1][1]]
        out = moved
    return out


__all__ = ["edit", "check", "has_comments", "Unsupported"]
