# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""逸脱コメントの文字数と形を確かめる（スキル deviation-comment の「確かめ」）。

使い方:
    uv run --native-tls <このスキルのフォルダ>/scripts/check_comment.py --comment "<逸脱コメント>"
    <逸脱コメント> を標準入力で渡してもよい（--comment を付けない）。

確かめること（どれもスキル deviation-comment の「書き方」と「書かないこと」に合わせる）:
  - 200 字以内か（前後の空白を除いた文字の数）
  - 判定区分（「誤検知。」「意図的。」「本物の不具合。」）で始まるか
  - 書かないことの言い方（条件や但し書き、推奨や人への依頼）がないか
    「推奨」は「を推奨」「推奨します」「推奨する」のような言い方だけを見る（「推奨範囲」のような名詞の一部は見ない）

結果は JSON を1つ出す: {"ok": 合否, "length": 字数, "problems": [直すところ]}。
終了コードは、合格なら 0、不合格（渡し方の誤りや予期しないエラーを含む）なら 1。
"""

from __future__ import annotations

import argparse
import json
import locale
import re
import sys
from typing import List, Optional, Tuple

MAX_LENGTH = 200  # 結果ファイルを受け取るスクリプト（ct.py finish）と同じ上限
CATEGORIES = ("誤検知。", "意図的。", "本物の不具合。")
# 書かないことの言い方。スキルの「書かないこと」の例（「ただし〜の場合に限る」「〜を確認した場合は」「〜に対して」、
# 「修正を推奨」「確認してください」）と、その言い換えを見つける。
CONDITION_WORDS = ("場合に限", "場合のみ", "に限り", "に限って", "に限る", "た場合は", "た場合には", "た場合のみ", "に対して")
REQUEST_WORDS = ("ください", "下さい", "お願い", "てほしい", "要確認", "確認が必要", "確認する必要")
# 「推奨」は、推奨や依頼の言い方だけを見る。「推奨範囲」「推奨値」のように、後ろに漢字やカタカナが続いて
# 名詞の一部になっているものは見ない。
RECOMMEND_PATTERNS = (("を推奨", re.compile(r"を推奨(?![\u3005\u4e00-\u9fff\u30a0-\u30ff])")),
                      ("推奨します", re.compile(r"推奨します")),
                      ("推奨する", re.compile(r"推奨する")))


def check(comment: str) -> dict:
    text = comment.strip()
    problems: List[str] = []
    if not text:
        return {"ok": False, "length": 0, "problems": ["逸脱コメントが空です"]}
    length = len(text)
    if length > MAX_LENGTH:
        problems.append("%d 字以内にしてください（今は %d 字）。問題にならない理由と根拠の場所だけを書きます"
                        % (MAX_LENGTH, length))
    if not text.startswith(CATEGORIES):
        problems.append("判定区分（%s のどれか）で始めてください" % "・".join("「%s」" % c for c in CATEGORIES))
    found = [w for w in CONDITION_WORDS if w in text]
    if found:
        problems.append("条件や但し書きの言い方（%s）があります。どんな状況でも問題にならないことを、条件を付けずに"
                        "言い切ります（言い切れないときは、推奨を修正にします）" % "、".join("「%s」" % w for w in found))
    found = [w for w, pattern in RECOMMEND_PATTERNS if pattern.search(text)] + [w for w in REQUEST_WORDS if w in text]
    if found:
        problems.append("推奨や人への依頼の言い方（%s）があります。逸脱コメントには、問題にならない理由だけを書きます"
                        % "、".join("「%s」" % w for w in found))
    return {"ok": not problems, "length": length, "problems": problems}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # 引数の誤りも、JSON で返す
        raise ValueError("引数が正しくありません: %s" % message)


def _read_stdin() -> Optional[str]:
    if sys.stdin is None or sys.stdin.isatty():
        return None
    data = sys.stdin.buffer.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def run(argv: Optional[List[str]] = None) -> Tuple[dict, int]:
    parser = _Parser(description="逸脱コメントの文字数と形を確かめる。結果は JSON（ok、length、problems）。")
    parser.add_argument("--comment", help="確かめる逸脱コメント。付けなければ、標準入力から読む")
    args = parser.parse_args(argv)
    comment = args.comment if args.comment is not None else _read_stdin()
    if comment is None:
        return {"ok": False, "length": 0, "problems": ["逸脱コメントを --comment か標準入力で渡してください"]}, 1
    result = check(comment)
    return result, 0 if result["ok"] else 1


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # 画面で表せない文字があっても止まらない
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        result, code = run(argv)
    except ValueError as exc:
        result, code = {"ok": False, "length": 0, "problems": [str(exc)]}, 1
    except Exception as exc:  # noqa: BLE001 - JSON を1つ出し、traceback は出さない
        result, code = {"ok": False, "length": 0,
                        "problems": ["予期しないエラーで止まりました: %s: %s" % (type(exc).__name__, exc)]}, 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
