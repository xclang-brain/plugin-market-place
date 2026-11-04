# -*- coding: utf-8 -*-
"""試験用の、機器のシリアルコンソールのまね。

PC 側の pty（スクリプトが開くシリアルポート）と、機器側の pty を、
中継のスレッドでつなぐ。機器側では、ログインとシェルをまねるスレッドが動く。

  スクリプト ⇄ [PC 側 pty] ⇄ 中継 ⇄ [機器側 pty] ⇄ 機器のまね

中継を止めると「ケーブルが抜けた（何も届かない）」状態になり、
PC 側の親を閉じると「USB が抜けた（読むとエラー）」状態になる。
"""

import os
import select
import threading
import time
import tty

HOST = "sensor-board"
PLAIN_PROMPT = "root@sensor-board:~# "
ANSI_PROMPT = "\x1b[?2004h\x1b[01;32mroot@sensor-board\x1b[00m:\x1b[01;34m~\x1b[00m# "
KERNEL_LINE = "[  123.456789] usb 1-1: new high-speed USB device number 3 using ehci-platform"

DEFAULT_RESPONSES = {
    "run-app /opt/x": "[DBG] AVG=3",
    "run-app /opt/sensor/sensor_app": "[DBG] start\n[DBG] AVG=3\nsensor_app: done",
    "uname -a": "Linux sensor-board 5.15.71-yocto-standard #1 SMP PREEMPT armv7l GNU/Linux",
    "echo hi": "hi",
    "true": "",
    "cat /var/log/messages": "line one\nline two\nline three",
    "seq 100": "\n".join(str(i) for i in range(1, 101)),
    "color": "\x1b[1;31m[DBG]\x1b[0m AVG=\x1b[33m3\x1b[0m\n\x1b[32mOK\x1b[0m",
}


class Relay(object):
    """PC 側の pty と機器側の pty の間で、バイトを運ぶ。"""

    def __init__(self):
        self.pc_master, self.pc_slave = os.openpty()
        self.dev_master, self.dev_slave = os.openpty()
        tty.setraw(self.pc_slave)
        tty.setraw(self.dev_slave)
        self.port = os.ttyname(self.pc_slave)
        for fd in (self.pc_master, self.dev_master):
            os.set_blocking(fd, False)
        self.plugged = True
        self.from_pc = bytearray()   # スクリプトが送ったもの（つながっている間）
        self.sent_while_unplugged = bytearray()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._closed_pc = False
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def unplug(self):
        self.plugged = False

    def plug(self):
        self.plugged = True

    def pull_usb(self):
        """USB の変換器ごと抜けたときのまね：PC 側の親を閉じる。"""
        with self._lock:
            if not self._closed_pc:
                self._closed_pc = True
                try:
                    os.close(self.pc_master)
                except OSError:
                    pass

    def _write(self, fd, data):
        while data:
            try:
                n = os.write(fd, data)
                data = data[n:]
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                return

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                fds = [self.dev_master] + ([] if self._closed_pc else [self.pc_master])
            try:
                ready, _, _ = select.select(fds, [], [], 0.05)
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            for fd in ready:
                try:
                    data = os.read(fd, 4096)
                except (BlockingIOError, OSError):
                    continue
                if not data:
                    continue
                if fd == self.dev_master:
                    if self.plugged and not self._closed_pc:
                        self._write(self.pc_master, data)
                else:
                    if self.plugged:
                        self.from_pc.extend(data)
                        self._write(self.dev_master, data)
                    else:
                        self.sent_while_unplugged.extend(data)

    def stop(self):
        self._stop.set()
        self.thread.join(2)
        self.pull_usb()
        for fd in (self.pc_slave, self.dev_master, self.dev_slave):
            try:
                os.close(fd)
            except OSError:
                pass


class FakeDevice(object):
    """機器側の pty で動く、ログインとシェルのまね。"""

    def __init__(self, relay, login=True, user="root", password="root", ansi=False,
                 garbage=False, ticker=None, wrap=0, responses=None, ignore_interrupt=(),
                 left_at_password=False):
        self.relay = relay
        self.fd = relay.dev_slave
        self.need_login = login
        self.user = user
        self.password = password
        self.ansi = ansi
        self.garbage = garbage
        self.ticker = ticker
        self.wrap = wrap
        self.responses = dict(DEFAULT_RESPONSES)
        if responses:
            self.responses.update(responses)
        self.ignore_interrupt = set(ignore_interrupt)
        self.state = "login" if login else "shell"
        self.pending_user = ""
        self.left_at_password = left_at_password
        self.logged_in_by_login = False
        self.line = ""
        self.col = 0
        self.running = None
        self.login_attempts = []   # (ユーザー, 成功したか)
        self.executed = []         # シェルで実行したコマンド
        self.exits = 0
        self.interrupts = 0
        self.scheduled = []        # (時刻, 文字, プロンプトを出すか)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    # --- 出力 ---
    def out(self, text):
        data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
        while data:
            try:
                n = os.write(self.fd, data)
                data = data[n:]
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                return

    def raw(self, data):
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def prompt(self):
        self.col = 0
        self.out(ANSI_PROMPT if self.ansi else PLAIN_PROMPT)

    def login_prompt(self):
        self.out("\n%s login: " % HOST)

    def start(self):
        os.set_blocking(self.fd, False)
        self.out("\nU-Boot 2022.04\nStarting kernel ...\n\nPoky (Yocto Project Reference Distro) 4.0 %s ttyS0\n" % HOST)
        if self.state == "login":
            self.login_prompt()
            if self.left_at_password:
                # 前の人が、ユーザー名だけ入れて離れた状態
                self.pending_user = "admin"
                self.state = "password"
                self.out("admin\nPassword: ")
        else:
            self.prompt()
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        self.thread.join(2)

    # --- 入力 ---
    def _loop(self):
        next_tick = time.monotonic() + self.ticker if self.ticker else None
        while not self._stop.is_set():
            now = time.monotonic()
            if next_tick is not None and now >= next_tick:
                self.out(KERNEL_LINE + "\n")
                next_tick = now + self.ticker
            if self.scheduled and self.scheduled[0][0] <= now:
                _, text, then_prompt = self.scheduled.pop(0)
                self.out(text)
                if then_prompt:
                    self.prompt()
                continue
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.02)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                data = os.read(self.fd, 1024)
            except (BlockingIOError, OSError):
                continue
            for ch in data.decode("utf-8", "replace"):
                self._char(ch)

    def _char(self, ch):
        if self.garbage:
            self.raw(b"\xfe\x80\x9f\x0c\xf3\xa0~\x92")
            return
        if self.state == "login":
            if ch in "\r\n":
                self.out("\n")
                if self.line == "":
                    self.login_prompt()
                else:
                    self.pending_user = self.line
                    self.line = ""
                    self.state = "password"
                    self.out("Password: ")
                return
            if ch >= " ":
                self.line += ch
                self.out(ch)
            return
        if self.state == "password":
            if ch in "\r\n":
                self.out("\n")
                ok = self.pending_user == self.user and self.line == self.password
                self.login_attempts.append((self.pending_user, ok))
                self.line = ""
                if ok:
                    self.state = "shell"
                    self.logged_in_by_login = True
                    self.out("Last login: Tue Nov  4 12:00:00 on ttyS0\n")
                    self.prompt()
                else:
                    self.state = "login"
                    self.out("\nLogin incorrect\n")
                    self.login_prompt()
                return
            if ch >= " ":
                self.line += ch
            return
        # シェル
        if self.running is not None:
            if ch == "\x03":
                self.interrupts += 1
                if self.running in self.ignore_interrupt or self.running.startswith("run-app /opt/stuck"):
                    self.out("^C")
                    return
                self.running = None
                self.out("^C\n")
                self.prompt()
            return
        if ch == "\x03":
            self.interrupts += 1
            self.line = ""
            self.out("^C\n")
            self.prompt()
            return
        if ch in "\r\n":
            self.out("\n")
            if self.ansi:
                self.raw(b"\x1b[?2004l\r")
            cmd = self.line.strip()
            self.line = ""
            self._execute(cmd)
            return
        if ch in "\x7f\b":
            if self.line:
                self.line = self.line[:-1]
                self.raw(b"\b \b")
            return
        if ch >= " ":
            self.line += ch
            self.out(ch)
            self.col += 1
            if self.wrap and self.col % self.wrap == 0:
                # 長い行を折り返すときの bash のまね（空白と \r を入れる）
                self.raw(b" \r")

    def _execute(self, cmd):
        if cmd == "":
            self.prompt()
            return
        self.executed.append(cmd)
        if cmd == "exit":
            self.exits += 1
            if self.logged_in_by_login:
                self.logged_in_by_login = False
                self.state = "login"
                self.out("logout\n")
                self.login_prompt()
            else:
                self.prompt()
            return
        if cmd.startswith("run-app /opt/hang") or cmd.startswith("run-app /opt/stuck"):
            self.out("[DBG] start\n")
            self.running = cmd
            return
        if cmd == "pull-usb":
            self.out("bye\n")
            time.sleep(0.2)
            self.relay.pull_usb()
            return
        if cmd == "slow":
            self.out("first\n")
            self.scheduled.append((time.monotonic() + 0.6, "second\n", True))
            return
        if cmd == "noisy":
            # プロンプトのすぐ後ろに、カーネルのメッセージが出る
            self.out("[DBG] AVG=3\n")
            self.prompt()
            self.scheduled.append((time.monotonic() + 0.05, KERNEL_LINE + "\n", False))
            return
        if cmd == "tricky":
            # 出力の途中に「# 」が来て、少し間が空いてから続きが来る
            self.out("value # ")
            self.scheduled.append((time.monotonic() + 0.05, "done\n", True))
            return
        if cmd in self.responses:
            text = self.responses[cmd]
            if text:
                self.out(text + "\n")
            self.prompt()
            return
        self.out("-sh: %s: not found\n" % cmd.split()[0])
        self.prompt()
