"""テスト用の機器のまね。ログインと、小さなシェルをまねる。

TCP で待ち受ける。スクリプトの port には socket://127.0.0.1:<番号> を書く。
pty=True なら、疑似端末（Linux の /dev/pts/N）で待ち受ける。

シェルが分かるコマンド：
  echo 文字   文字を出す。$? は直前の終了コードに替え、" と ' は除く
  true・false 終了コード 0・1
  seq N       1 から N までを1行ずつ出す
  color       色の付いた文字を出す
  kmsg        ok を出したあと、プロンプトの後ろに機器のメッセージを出す（プロンプトが隠れる）
  hang        Ctrl-C が来るまで終わらない
  bye         接続を切る（ケーブルが抜けたまね）
  そのほか    not found を出し、終了コード 127
"""

import os
import select
import socket
import threading
import time

PROMPT = "root@board:~# "
COLOR_PROMPT = "\x1b[?2004h\x1b[01;32mroot@board\x1b[00m:\x1b[01;34m~\x1b[00m# "
KMSG = "[  12.345678] usb 1-1: new high-speed USB device number 3"


class FakeDevice(object):
    def __init__(self, user="root", password="secret", prompt=PROMPT, state="login", typed="",
                 mute=False, pty=False):
        self.user, self.password, self.prompt = user, password, prompt
        self.state = state  # login・password・shell
        self.line = typed   # 打ちかけの文字
        self.mute = mute    # 何も返さない（電源が入っていない機器のまね）
        self.rc = 0
        self.busy = False
        self.dropped = False
        self.typed_user = None
        self.received = bytearray()
        self.stop = threading.Event()
        if pty:
            import tty
            self.master, self.slave = os.openpty()
            tty.setraw(self.slave)
            self.port = os.ttyname(self.slave)
            target = self._serve_pty
        else:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.bind(("127.0.0.1", 0))
            self.server.listen(1)
            self.server.settimeout(0.2)
            self.port = "socket://127.0.0.1:%d" % self.server.getsockname()[1]
            target = self._serve_tcp
        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(2)
        if hasattr(self, "server"):
            self.server.close()
        else:
            os.close(self.master)
            os.close(self.slave)

    # --- つなぎ方 ---

    def _serve_tcp(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(0.2)
            self._write = conn.sendall
            self.dropped = False
            with conn:
                while not self.stop.is_set() and not self.dropped:
                    try:
                        data = conn.recv(1024)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    self._input(data)

    def _serve_pty(self):
        self._write = lambda data: os.write(self.master, data)
        while not self.stop.is_set():
            if select.select([self.master], [], [], 0.2)[0]:
                try:
                    self._input(os.read(self.master, 1024))
                except OSError:
                    time.sleep(0.05)

    def _out(self, text):
        try:
            self._write(text.encode("utf-8"))
        except OSError:
            pass

    # --- 機器のふるまい ---

    def _input(self, data):
        self.received += data
        if self.mute:
            return
        for ch in data.decode("utf-8", "replace"):
            if self.busy:
                if ch == "\x03":
                    self.busy = False
                    self.rc = 130
                    self._out("^C\r\n" + self.prompt)
            elif ch == "\r":
                self._out("\r\n")
                line, self.line = self.line, ""
                self._enter(line)
            elif ch == "\x03":
                self.line = ""
                self._out("^C\r\n")
                self._show_prompt()
            elif ch != "\n":
                self.line += ch
                if self.state != "password":
                    self._out(ch)

    def _show_prompt(self):
        self._out({"login": "board login: ", "password": "Password: "}.get(self.state, self.prompt))

    def _enter(self, line):
        if self.state == "login":
            if line:
                self.typed_user = line
                self.state = "password"
        elif self.state == "password":
            if self.typed_user == self.user and line == self.password:
                self.state = "shell"
                self._out("Welcome to board\r\n")
            else:
                time.sleep(0.3)
                self._out("Login incorrect\r\n")
                self.state = "login"
        else:
            self._run(line.strip())
            return
        self._show_prompt()

    def _run(self, command):
        word, _, arg = command.partition(" ")
        out = None
        if word == "echo":
            out = arg.replace("$?", str(self.rc)).replace('"', "").replace("'", "")
            self.rc = 0
        elif word in ("true", "false"):
            self.rc = 0 if word == "true" else 1
        elif word == "seq":
            out = "\r\n".join(str(i) for i in range(1, int(arg) + 1))
            self.rc = 0
        elif word == "color":
            out = "\x1b[1;31mred\x1b[0m and \x1b[32mgreen\x1b[0m"
            self.rc = 0
        elif word == "kmsg":
            self.rc = 0
            self._out("ok\r\n" + self.prompt + KMSG + "\r\n")
            return
        elif word == "hang":
            self.busy = True
            return
        elif word == "bye":
            self.dropped = True
            return
        elif word:
            out = "sh: %s: not found" % word
            self.rc = 127
        if out is not None:
            self._out(out + "\r\n")
        self._out(self.prompt)
