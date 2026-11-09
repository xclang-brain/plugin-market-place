# -*- coding: utf-8 -*-
"""テストで使う、偽のビルドのコマンド。OS に頼らずに動く。

使い方：fake_build.py 動作 [引数...] [+ 動作 [引数...]]...
  write 名前 [中身]        ファイルを作る（いつも作り直す）
  once 名前                ファイルがないときだけ作る（make が作り直さないときの代わり）
  elf 名前 番号 [big]      e_machine がその番号の ELF の頭だけのファイルを作る
  say 文字                 標準出力に1行書く（UTF-8）
  bytes 16進数             標準出力に、そのバイト列と改行を書く
  env 名前                 「名前=値」を書く
  cwd                      今のフォルダを書く
  exit 終了コード          その終了コードで終わる
  sleep 秒                 待つ
  spawn 記録ファイル [ignore-term]
                           子のプロセスを起動し、「自分の番号 子の番号」を書いてから長く待つ
  term 記録ファイル        長く待つ。SIGTERM を受けたら、1 秒後に記録ファイルを作って終わる
                           （make が作りかけのファイルを消す代わり）
"""

import os
import signal
import subprocess
import sys
import time

CHILD = ("import signal, sys, time\n"
         "if sys.argv[1:] == ['ignore-term']:\n"
         "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "time.sleep(120)\n")


def say(text):
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def act(name, args):
    if name == "write" or (name == "once" and not os.path.exists(args[0])):
        with open(args[0], "w", encoding="utf-8") as f:
            f.write(args[1] if len(args) > 1 else "built\n")
        say("wrote " + args[0])
    elif name == "elf":
        big = args[2:] == ["big"]
        head = b"\x7fELF" + bytes([1, 2 if big else 1, 1]) + b"\0" * 9  # e_ident は 16 バイト
        head += (2).to_bytes(2, "big" if big else "little")
        head += int(args[1]).to_bytes(2, "big" if big else "little")
        with open(args[0], "wb") as f:
            f.write(head + b"\0" * 44)
        say("wrote " + args[0])
    elif name == "say":
        say(args[0])
    elif name == "bytes":
        sys.stdout.buffer.write(bytes.fromhex(args[0]) + b"\n")
        sys.stdout.buffer.flush()
    elif name == "env":
        say("{}={}".format(args[0], os.environ.get(args[0], "")))
    elif name == "cwd":
        say(os.getcwd())
    elif name == "exit":
        sys.exit(int(args[0]))
    elif name == "sleep":
        time.sleep(float(args[0]))
    elif name == "spawn":
        child = subprocess.Popen([sys.executable, "-c", CHILD] + args[1:])
        with open(args[0], "w", encoding="utf-8") as f:
            f.write("{} {}\n".format(os.getpid(), child.pid))
        say("spawned")
        time.sleep(120)
    elif name == "term":
        def clean_up(signum, frame):
            time.sleep(1)
            with open(args[0], "w", encoding="utf-8") as f:
                f.write("cleaned\n")
            sys.exit(1)
        signal.signal(signal.SIGTERM, clean_up)
        say("waiting")
        time.sleep(120)


def main(argv):
    group = []
    for arg in argv + ["+"]:
        if arg == "+":
            if group:
                act(group[0], group[1:])
            group = []
        else:
            group.append(arg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
