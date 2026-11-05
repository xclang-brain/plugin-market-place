# -*- coding: utf-8 -*-
"""テストで使う、OS に頼らない「ビルドのコマンド」。

使い方：<python> fake_build.py 動作 [引数...] [+ 動作 [引数...]]...
  write 名前 [中身]       ファイルを作る（いつも作り直す）
  once 名前               ファイルが無いときだけ作る（make が作り直さないときの代わり）
  remove 名前             ファイルを消す（無くてもよい）
  elf-arm 名前            ARM 向けの ELF の頭だけのファイルを作る
  say 文字                標準出力に1行書く（UTF-8）
  bytes 16進数            標準出力に、そのバイト列をそのまま書く
  env 名前                「名前の小文字=値」を標準出力に書く
  fail 終了コード [文字]  文字を標準エラー出力に書いて、その終了コードで終わる
  spawn 記録ファイル      子のプロセスを1つ起動し、「自分の番号 子の番号」を書いてから、長く待つ
  sleep 秒                待つ
"""

import os
import subprocess
import sys
import time

ARITY = {"write": (1, 2), "once": (1, 1), "remove": (1, 1), "elf-arm": (1, 1), "say": (1, 1),
         "bytes": (1, 1), "env": (1, 1), "fail": (1, 2), "spawn": (1, 1), "sleep": (1, 1)}


def out(data):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def line(text):
    out((text + "\n").encode("utf-8"))


def act(name, args):
    if name in ("write", "once"):
        if name == "once" and os.path.exists(args[0]):
            line("up to date: " + args[0])
            return
        with open(args[0], "w", encoding="utf-8") as f:
            f.write(args[1] if len(args) > 1 else "built\n")
        line("wrote " + args[0])
    elif name == "remove":
        if os.path.exists(args[0]):
            os.remove(args[0])
        line("removed " + args[0])
    elif name == "elf-arm":
        head = b"\x7fELF" + bytes([1, 1, 1]) + b"\0" * 11 + (40).to_bytes(2, "little")
        with open(args[0], "wb") as f:
            f.write(head + b"\0" * 40)
        line("wrote " + args[0])
    elif name == "say":
        line(args[0])
    elif name == "bytes":
        out(bytes.fromhex(args[0]))
    elif name == "env":
        line("{}={}".format(args[0].lower(), os.environ.get(args[0], "")))
    elif name == "fail":
        if len(args) > 1:
            sys.stderr.buffer.write((args[1] + "\n").encode("utf-8"))
            sys.stderr.buffer.flush()
        sys.exit(int(args[0]))
    elif name == "spawn":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(args[0], "w", encoding="utf-8") as f:
            f.write("{} {}\n".format(os.getpid(), child.pid))
        line("spawned {}".format(child.pid))
        time.sleep(120)
    elif name == "sleep":
        time.sleep(float(args[0]))


def main(argv):
    groups = [[]]
    for a in argv:
        if a == "+":
            groups.append([])
        else:
            groups[-1].append(a)
    for group in groups:
        if not group or group[0] not in ARITY:
            sys.stderr.write("fake_build: bad action {!r}\n".format(group))
            return 64
        low, high = ARITY[group[0]]
        if not low <= len(group) - 1 <= high:
            sys.stderr.write("fake_build: bad arguments {!r}\n".format(group))
            return 64
        act(group[0], group[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
