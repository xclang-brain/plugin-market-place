"""Pages for the files of a run shown inside the screen: a patch with colored lines, a fixed file with
line numbers. Everything is escaped here, and the pages have no script."""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote

from .common import display_text

MAX_LINES = 20000
FIXED_JA = {"fix": "修正後のファイル", "annotation": "注釈を入れたファイル"}
CSS = """
:root { color-scheme: light dark; --bg: #f5f6f8; --surface: #ffffff; --text: #1c2230; --muted: #5d6676;
  --border: #d8dde5; --link: #1f56c9; --add-bg: #e4f5e8; --add-fg: #14532d; --del-bg: #fce6e6; --del-fg: #8b1d1d;
  --hunk-bg: #e6ecfb; --hunk-fg: #2b4a9b; --meta-fg: #5d6676;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "BIZ UDGothic", "MS Gothic", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", Meiryo, sans-serif; }
@media screen and (prefers-color-scheme: dark) {
  :root { --bg: #14171c; --surface: #1c2027; --text: #e4e7ec; --muted: #a3acba; --border: #343c48; --link: #8fb3ff;
    --add-bg: #16351f; --add-fg: #a6e9b9; --del-bg: #43191b; --del-fg: #ffb3b3; --hunk-bg: #1d2a48; --hunk-fg: #a9c1ff;
    --meta-fg: #a3acba; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.7 var(--sans); }
.page { max-width: 76rem; margin: 0 auto; padding: 16px 16px 40px; }
a { color: var(--link); }
code, pre { font-family: var(--mono); }
.muted { color: var(--muted); }
.crumbs { margin: 0; font-size: .9rem; color: var(--muted); }
.head h1 { font-size: 1.15rem; margin: 2px 0; overflow-wrap: anywhere; }
.head .actions { margin: 6px 0 0; display: flex; flex-wrap: wrap; gap: 4px 16px; overflow-wrap: anywhere; }
pre.code, pre.diff { margin: 8px 0; padding: 10px 0; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; overflow-x: auto; font-size: .84rem; line-height: 1.5; }
pre.diff > span, pre.code > span { display: block; padding: 0 12px; min-height: 1.5em; }
pre.diff > .add { background: var(--add-bg); color: var(--add-fg); }
pre.diff > .del { background: var(--del-bg); color: var(--del-fg); }
pre.diff > .hunk { background: var(--hunk-bg); color: var(--hunk-fg); }
pre.diff > .meta { color: var(--meta-fg); font-weight: 600; }
pre.code { counter-reset: line; }
pre.code > span { padding-left: 0; }
pre.code > span::before { counter-increment: line; content: counter(line); display: inline-block; width: 4.5em;
  padding-right: .8em; margin-right: .8em; text-align: right; color: var(--meta-fg);
  border-right: 1px solid var(--border); user-select: none; -webkit-user-select: none; }
@media print { pre.code, pre.diff { white-space: pre-wrap; overflow-wrap: anywhere; overflow: visible; } }
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def diff_html(text: str, limit: int = MAX_LINES) -> str:
    """A unified diff (git or svn) as ``<pre class="diff">`` with one span per line."""
    lines = text.splitlines()
    out, in_hunk = [], False
    for line in lines[:limit]:
        if line.startswith(("diff ", "Index: ")):
            kind, in_hunk = "meta", False
        elif line.startswith("@@"):
            kind, in_hunk = "hunk", True
        elif not in_hunk or line.startswith("\\"):
            kind = "meta"
        else:
            kind = {"+": "add", "-": "del"}.get(line[:1], "")
        out.append((f'<span class="{kind}">' if kind else "<span>") + f"{_e(line)}\n</span>")
    block = '<pre class="diff">' + "".join(out) + "</pre>"
    if len(lines) > limit:
        block += f'\n<p class="muted">長いため先頭 {limit} 行だけ表示しています。全体は「元のファイル」で見てください。</p>'
    return block


def numbered(text: str, limit: int = MAX_LINES) -> str:
    lines = text.splitlines()
    if not lines:
        return '<p class="muted">（空のファイルです）</p>'
    block = '<pre class="code">' + "".join(f"<span>{_e(line)}\n</span>" for line in lines[:limit]) + "</pre>"
    if len(lines) > limit:
        block += f'\n<p class="muted">長いため先頭 {limit} 行だけ表示しています。全体は「元のファイル」で見てください。</p>'
    return block


def view_page(run_id: str, parts: list[str], data: bytes) -> str:
    rel = "/".join(parts)
    if parts[0] == "patches":
        label, shown = "差分ファイル", parts[-1]
    else:
        kind = parts[2] if len(parts) > 3 else ""
        label = f"{parts[1]} の{FIXED_JA[kind]}" if kind in FIXED_JA else "修正後のファイル"
        shown = "/".join(parts[3:]) if kind in FIXED_JA else rel
    if b"\0" in data[:8192]:
        body = '<p class="muted">文字のファイルではないため、ここには表示できません。</p>'
    elif parts[0] == "patches":
        body = diff_html(display_text(data))
    else:
        body = numbered(display_text(data))
    raw = f"/files/{run_id}/{quote(rel, safe='/')}"
    header = (f'<header class="head">\n<p class="crumbs">{_e(label)}</p>\n<h1><code>{_e(shown)}</code></h1>\n'
              f'<p class="actions"><span class="muted">実行 {_e(run_id)} の <code>{_e(rel)}</code></span>'
              f'<a href="{_e(raw)}">元のファイル</a></p>\n</header>')
    return ('<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{_e(shown)}</title>\n<style>{CSS}</style>\n</head>\n'
            f'<body>\n<div class="page">\n{header}\n{body}\n</div>\n</body>\n</html>\n')


def charset(data: bytes) -> str:
    """UTF-8, or Shift_JIS for files saved in cp932 (common on Japanese Windows)."""
    for codec, name in (("utf-8", "utf-8"), ("cp932", "shift_jis")):
        try:
            data.decode(codec)
            return name
        except UnicodeDecodeError:
            continue
    return "utf-8"
