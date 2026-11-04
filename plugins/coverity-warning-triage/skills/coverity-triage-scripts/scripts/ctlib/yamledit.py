"""Rewrite config.yaml and keep the comments people wrote in it (REQ-102).

Only the text of the values that change is replaced. Where a value is in the file comes from
``yaml.compose`` (pyyaml: no other library). ``edit`` gives ``None`` for a file written in a way it
does not handle and for a result that does not pass the check; the caller then writes the file
whole, as before (the comment lines at the top are kept, the other comments are lost).

A comment belongs to an element (a setting or a list value): the comment at the end of a line to the
element of that line, a run of comment lines to the element of the next line with data. What is lost
on purpose: the comments of a setting or a list value that is removed (the comment at the end of its
line, and the run of comment lines right above it with no blank line between). A list value that
moves (the list is put in another order) takes its comments along. The check compares the old and
the new text on their own (values read back, and each comment on the same element, at the same
place, as before), not by following the steps of the rewrite."""
from __future__ import annotations

import difflib
from collections import Counter
from typing import Any, NamedTuple

import yaml

WIDE = 10 ** 9  # no folding of a value written in place
BOM = "﻿"


class Unsupported(Exception):
    """A way of writing YAML that the rewrite does not handle."""


def edit(text: str, new: dict[str, Any]) -> str | None:
    """``text`` with its values changed to ``new``, its comments kept; ``None`` when that cannot be done."""
    bom = BOM if text.startswith(BOM) else ""
    body = text[len(bom):]
    try:
        if _unsupported(body):
            return None
        root = yaml.compose(body)
        if not isinstance(root, yaml.MappingNode) or not isinstance(new, dict):
            return None
        out = _Rewrite(body).run(root, new)
        return bom + out if check(body, out, new) else None
    except (Unsupported, yaml.YAMLError, ValueError, TypeError, IndexError, KeyError, RecursionError):
        return None


def has_comments(text: str) -> bool:
    """Whether writing the file whole would lose comments: a comment after the first line with data."""
    text = text.removeprefix(BOM)
    try:
        found = _comments(text)
    except (yaml.YAMLError, ValueError):
        return "#" in text
    lines = text.splitlines()
    data = [i for i, line in enumerate(lines) if line.strip() and not _is_comment_line(lines, found, i)]
    if not data:
        return False  # only the lines at the top, which the whole rewrite keeps
    return any(i >= data[0] or not lines[i].startswith("#") for i in found)


# ---- reading -----------------------------------------------------------------------------------------


def _unsupported(text: str) -> bool:
    """「|」「>」の値、「&」「*」、タグ、文書が2つ以上、yaml と Python で数え方の違う改行"""
    if any(char in text for char in "\r\x0b\x0c\x1c\x1d\x1e\x85  "):
        return True
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
    """Equal, and of the same types all the way down (True is not 1, 1.0 is not 1)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


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


def _is_comment_line(lines: list[str], comments: dict[int, tuple[int, str]], line: int) -> bool:
    return line in comments and not lines[line][:comments[line][0]].strip()


def _last_line(node: yaml.Node) -> int:
    """The last line that has text of ``node`` (the end mark of a block collection is on the next line)."""
    if isinstance(node, yaml.ScalarNode) or node.flow_style or not node.value:
        mark = node.end_mark
        return mark.line if mark.column else mark.line - 1
    last = node.value[-1]
    return _last_line(last[1] if isinstance(node, yaml.MappingNode) else last)


def _pairs(old: list[Any], new: list[Any]) -> dict[int, int]:
    """Which old list value each new one is (new index → old index); the others are new or removed.

    Values that stay in order first, then the same value somewhere else (it moved), then, in a part that
    was replaced, the values in their order (a value that was changed)."""
    keys_old, keys_new = [repr(x) for x in old], [repr(x) for x in new]
    blocks = difflib.SequenceMatcher(a=keys_old, b=keys_new, autojunk=False).get_opcodes()
    pairs: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in blocks:
        if tag == "equal":
            pairs.update({j1 + k: i1 + k for k in range(i2 - i1)})
    taken = set(pairs.values())
    for j, key in enumerate(keys_new):
        if j in pairs:
            continue
        i = next((i for i, other in enumerate(keys_old) if other == key and i not in taken), None)
        if i is not None:
            pairs[j] = i
            taken.add(i)
    for tag, i1, i2, j1, j2 in blocks:
        if tag == "replace":
            olds = [i for i in range(i1, i2) if i not in taken]
            news = [j for j in range(j1, j2) if j not in pairs]
            for i, j in zip(olds, news):
                pairs[j] = i
                taken.add(i)
    return pairs


# ---- rewriting ---------------------------------------------------------------------------------------


class _Block(NamedTuple):
    """The lines of one list value: [start, adjacent) blank lines and comment runs with a blank line after
    them; [adjacent, end] the comment lines right above the value, and the value."""
    start: int
    adjacent: int
    end: int


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

    def lines_text(self, first: int, last: int) -> str:
        """Lines ``first`` to ``last``, ending with a line break (the last line of the file may have none)."""
        text = self.text[self.line_start(first):self.line_start(last + 1)]
        return text if not text or text.endswith("\n") else text + "\n"

    def top(self, line: int, floor: int) -> int:
        """The first of the comment lines right above ``line`` (no blank line between), not above ``floor``."""
        while line - 1 > floor and _is_comment_line(self.lines, self.comment_lines, line - 1):
            line -= 1
        return line

    # mappings

    def _map(self, node: yaml.MappingNode, new: dict[str, Any], floor: int = -1) -> None:
        if node.flow_style:
            if _same(_value(node), new):
                return
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
            after = self.pos(key.end_mark)
            start = end = after + self.text[after:].index(":") + 1
            text = " " + text
        self.edits.append((start, end, text))

    # lists

    def _list(self, key: yaml.Node, seq: yaml.SequenceNode, want: list[Any]) -> None:
        items = seq.value
        if any(not isinstance(item, yaml.ScalarNode) for item in items) or any(isinstance(x, (dict, list)) for x in want):
            raise Unsupported("リストの中のマップやリスト")
        old = [_value(item) for item in items]
        if _same(old, want):
            return
        if seq.flow_style:
            self._flow_list(key, seq, want)
            return
        if _last_line(key) != key.start_mark.line or seq.start_mark.line == key.start_mark.line:
            raise Unsupported("キーとリストの書き方")
        first_line = self.lines[items[0].start_mark.line]
        dash = first_line.index("-", 0, items[0].start_mark.column)
        gap = items[0].start_mark.column - dash
        if any(self.lines[item.start_mark.line][dash:dash + 1] != "-" for item in items):
            raise Unsupported("リストの字下げがそろっていない")
        blocks, previous = [], key.start_mark.line
        for item in items:
            blocks.append(_Block(previous + 1, self.top(item.start_mark.line, previous), _last_line(item)))
            previous = _last_line(item)
        start, end = self.line_start(blocks[0].start), self.line_start(blocks[-1].end + 1)
        pairs = _pairs(old, want)
        kept = set(pairs.values())
        # The comment runs with a blank line after them, of values that are removed, stay where they were:
        # right before the next value that stays (or at the end of the list).
        stays: dict[int | None, list[str]] = {}
        for i, block in enumerate(blocks):
            if i in kept or block.adjacent == block.start:
                continue
            loose = self.lines_text(block.start, block.adjacent - 1)
            if loose.strip():
                following = next((k for k in range(i + 1, len(items)) if k in kept), None)
                stays.setdefault(following, []).append(loose)
        if not want:
            # 空にする：「キー: []」にして、キーの行の終わりの注釈は残す
            note = self.comment_lines.get(key.start_mark.line)
            keep = "   " + note[1] if note and note[0] > key.end_mark.column else ""
            self.edits.append((self.pos(key.end_mark), end, ": []" + keep + "\n" + "".join(stays.get(None, []))))
            return
        pieces: list[str] = []
        for j, value in enumerate(want):
            i = pairs.get(j)
            if i is None:
                pieces.append(" " * dash + "-" + " " * (gap - 1) + _scalar(value) + "\n")
                continue
            pieces.extend(stays.get(i, []))
            block = self.lines_text(blocks[i].start, blocks[i].end)
            if not _same(old[i], value):
                base = self.line_start(blocks[i].start)
                a, b = self.pos(items[i].start_mark) - base, self.pos(items[i].end_mark) - base
                block = block[:a] + _scalar(value) + block[b:]
            pieces.append(block)
        pieces.extend(stays.get(None, []))
        self.edits.append((start, end, "".join(pieces)))

    def _flow_list(self, key: yaml.Node, seq: yaml.SequenceNode, want: list[Any]) -> None:
        if seq.value or not want:
            self.edits.append((self.pos(seq.start_mark), self.pos(seq.end_mark), _scalar(want)))
            return
        # 「キー: []」に値を入れる：[] を消し、次の行から「- 値」で書く（キーの行の注釈は残る）
        if seq.start_mark.line != key.start_mark.line or seq.end_mark.line != key.start_mark.line:
            raise Unsupported("[] がキーと別の行")
        self.edits.append((self.pos(key.end_mark), self.pos(seq.end_mark), ":"))
        at = self.line_start(key.start_mark.line + 1)
        block = "".join(" " * (key.start_mark.column + 2) + "- " + _scalar(x) + "\n" for x in want)
        if at == len(self.text) and not self.text.endswith("\n"):
            block = "\n" + block
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
    """The new text reads back as ``new`` (same types, no key twice), and each comment is where it was.

    A comment at the end of a line must be at the end of the line of the same element. A comment line must
    be above the same element, at the same place in its run of comment lines, with or without a blank
    line before that element as before. The comments of a removed element go: the one at the end of its
    line, and the run right above it. A run with a blank line after it stays, above the next element that
    stays. A list value is followed to its place in the new list. Nothing else may change."""
    old_text, new_text = old_text.removeprefix(BOM), new_text.removeprefix(BOM)
    if _unsupported(new_text) or not _same(yaml.safe_load(new_text), new):
        return False
    old_root, new_root = yaml.compose(old_text), yaml.compose(new_text)
    if _twice(new_root):
        return False
    moves = _moves(old_root, new)
    expected: Counter = Counter()
    for note in _notes(old_text, old_root):
        if note.kind == "end":
            target = _follow(note.owners[0], moves, new)
            if target is not None:
                expected[("end", note.text, target, False, 0)] += 1
            continue
        target = _follow(note.owners[0], moves, new) if note.owners else ("end",)
        if target is None:
            if note.adjacent:
                continue  # the run right above a removed element goes with it
            target = next((t for t in (_follow(o, moves, new) for o in note.owners[1:]) if t is not None), ("end",))
        expected[("line", note.text, target, note.adjacent, note.depth)] += 1
    found = Counter((note.kind, note.text, note.owners[0] if note.owners else ("end",),
                     note.adjacent if note.kind == "line" else False, note.depth)
                    for note in _notes(new_text, new_root))
    return found == expected


class _Note(NamedTuple):
    kind: str  # "end": at the end of a line; "line": a comment line
    text: str
    owners: tuple  # the element of the line ("end"), or of each line with data after it, in order ("line")
    adjacent: bool  # a comment line: no blank line between its run and the next line with data
    depth: int  # a comment line: how many comment lines of its run are below it


def _notes(text: str, root: yaml.Node) -> list[_Note]:
    comments = _comments(text)
    lines = text.splitlines()
    owners = _owners(root)
    data = [n for n in range(len(lines)) if lines[n].strip() and not _is_comment_line(lines, comments, n)]
    notes = []
    for line, (_, note) in sorted(comments.items()):
        if not _is_comment_line(lines, comments, line):
            notes.append(_Note("end", note, (owners.get(line, ("?", line)),), False, 0))
            continue
        below = line
        while _is_comment_line(lines, comments, below + 1):
            below += 1
        after = []
        for n in data:
            if n > below:
                owner = owners.get(n, ("?", n))
                if not after or after[-1] != owner:
                    after.append(owner)
        adjacent = below + 1 < len(lines) and bool(lines[below + 1].strip())
        notes.append(_Note("line", note, tuple(after), adjacent, below - line))
    return notes


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


def _twice(root: yaml.Node) -> bool:
    """A key written twice in one map (the reader takes the last; a person reads the first)."""
    if isinstance(root, yaml.MappingNode):
        names = [key.value for key, _ in root.value]
        return len(names) != len(set(names)) or any(_twice(value) for _, value in root.value)
    if isinstance(root, yaml.SequenceNode):
        return any(_twice(item) for item in root.value)
    return False


def _moves(old_root: yaml.Node, new: Any) -> dict[tuple, tuple | None]:
    """Old path of a list value → its new path (None: removed). Keys keep their names."""
    moves: dict[tuple, tuple | None] = {}

    def walk(node: yaml.Node, want: Any, path: tuple) -> None:
        if isinstance(node, yaml.MappingNode) and isinstance(want, dict):
            for key, value in node.value:
                if key.value in want:
                    walk(value, want[key.value], path + (("k", key.value),))
        elif isinstance(node, yaml.SequenceNode) and isinstance(want, list):
            back = {i: j for j, i in _pairs([_value(item) for item in node.value], want).items()}
            for i in range(len(node.value)):
                moves[path + (("i", i),)] = path + (("i", back[i]),) if i in back else None

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
