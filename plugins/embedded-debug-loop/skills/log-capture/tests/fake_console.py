# -*- coding: utf-8 -*-
"""テスト用の、機器のシリアルコンソールのまね。

スクリプトが開く PC 側と、機器のまねの間を、中継のスレッドでつなぐ。

  スクリプト ⇄ [PC 側] ⇄ 中継 ⇄ [ソケットの組] ⇄ 機器のまね

PC 側は2つから選ぶ。
- "tcp"（既定）：127.0.0.1 の TCP。スクリプトの port は socket://127.0.0.1:<番号>。
  どの OS でも動く。
- "pty"（Linux だけ）：疑似端末。スクリプトの port は /dev/pts/N（本物のシリアルポートの道）。

中継を止めると「ケーブルが抜けた（何も届かない）」状態になり、
pull_usb() で PC 側を閉じると「USB が抜けた（読むとエラー）」状態になる。

シェルのまねは、直前のコマンドの終了コード（$?）を覚えていて、
echo の引数の $? を、その数に置き換える。
"""

import os
import select
import socket
import threading
import time

HOST = "myboard"
PLAIN_PROMPT = "root@myboard:~# "
ANSI_PROMPT = "\x1b[?2004h\x1b[01;32mroot@myboard\x1b[00m:\x1b[01;34m~\x1b[00m# "
KERNEL_LINE = "[  123.456789] usb 1-1: new high-speed USB device number 3 using ehci-platform"

# コマンドへの返事。値は出力の文字か、(出力の文字, 終了コード)。文字だけなら終了コードは 0。
DEFAULT_RESPONSES = {
    "start-app /opt/x": "[DBG] VALUE=3",
    "start-app /opt/noconf/my_app": ("start-app: /opt/noconf/my_app: config file not found", 1),
    "start-app /opt/denied/my_app": ("[DBG] start\nstart-app: /opt/denied/my_app: Permission denied", 126),
    "false": ("", 1),
    "start-app /opt/myapp/my_app": "[DBG] start\n[DBG] VALUE=3\nmy_app: done",
    "uname -a": "Linux myboard 5.15.71-yocto-standard #1 SMP PREEMPT armv7l GNU/Linux",
    "echo hi": "hi",
    "true": "",
    "cat /var/log/messages": "line one\nline two\nline three",
    "seq 100": "\n".join(str(i) for i in range(1, 101)),
    "color": "\x1b[1;31m[DBG]\x1b[0m VALUE=\x1b[33m3\x1b[0m\n\x1b[32mOK\x1b[0m",
}

SEND_WAIT = 2.0  # 書き込みで待つ最長の秒数（相手が読まなくなっても、止まったままにしない）


def _sendall(sock, data):
    try:
        sock.sendall(data)
    except OSError:
        pass


class Relay(object):
    """PC 側と機器のまねの間で、バイトを運ぶ。"""

    def __init__(self, mode="tcp"):
        self.mode = mode
        # 機器のまねとの間は、ソケットの組（どの OS でも動く）
        self.dev_peer, self.dev_sock = socket.socketpair()
        for s in (self.dev_peer, self.dev_sock):
            s.settimeout(SEND_WAIT)
        self.listener = None
        self.conn = None        # tcp：スクリプトとの接続
        self.pc_master = None   # pty：PC 側の疑似端末の親
        self.pc_slave = None
        if mode == "pty":
            import tty  # Linux など（pty のテストは Linux のときだけ動かす）
            self.pc_master, self.pc_slave = os.openpty()
            tty.setraw(self.pc_slave)
            os.set_blocking(self.pc_master, False)
            self.port = os.ttyname(self.pc_slave)
        else:
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(4)
            self.port = "socket://127.0.0.1:%d" % self.listener.getsockname()[1]
        self.plugged = True
        self.from_pc = bytearray()   # スクリプトが送ったもの（つながっている間）
        self.sent_while_unplugged = bytearray()
        self.connections = 0         # tcp：スクリプトが接続した回数
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
        """USB の変換器ごと抜けたときのまね：PC 側を閉じる。"""
        with self._lock:
            if self._closed_pc:
                return
            self._closed_pc = True
            if self.mode == "pty":
                try:
                    os.close(self.pc_master)
                except OSError:
                    pass
            elif self.conn is not None:
                # 送る側だけを閉じる。スクリプトは、届いた文字を読み切った後に「切れた」を知る。
                # 閉じるのは、スクリプトが閉じた後に、中継のスレッドがする
                # （読んでいない文字を残して閉じると、RST で届いた文字が消えることがある）
                try:
                    self.conn.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

    def _to_pc(self, data):
        if self.mode == "pty":
            while data:
                try:
                    n = os.write(self.pc_master, data)
                    data = data[n:]
                except BlockingIOError:
                    time.sleep(0.01)
                except OSError:
                    return
        elif self.conn is not None:
            _sendall(self.conn, data)

    def _pc_readable(self):
        """PC 側から読めるもの（なければ None）。"""
        if self._closed_pc:
            return None
        return self.pc_master if self.mode == "pty" else self.conn

    def _read_pc(self, src):
        """PC 側から読む。相手が閉じたら None。"""
        try:
            if self.mode == "pty":
                return os.read(src, 4096)
            data = src.recv(4096)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:
            data = b""
        if not data and self.mode == "tcp":
            # スクリプトが閉じた（または pull_usb の後）
            with self._lock:
                if self.conn is src:
                    self.conn = None
            try:
                src.close()
            except OSError:
                pass
            return None
        return data

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                pc = self._pc_readable()
                if pc is None and self.mode == "tcp" and self.conn is not None:
                    pc = self.conn  # pull_usb の後：閉じたことを読んで片付ける
            fds = [self.dev_peer] + ([pc] if pc is not None else [])
            if self.listener is not None:
                fds.append(self.listener)
            try:
                ready, _, _ = select.select(fds, [], [], 0.05)
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            for src in ready:
                if src is self.listener:
                    try:
                        conn, _ = self.listener.accept()
                    except OSError:
                        continue
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn.settimeout(SEND_WAIT)
                    with self._lock:
                        old, self.conn = self.conn, conn
                        self.connections += 1
                    if old is not None:
                        try:
                            old.close()
                        except OSError:
                            pass
                    continue
                if src is self.dev_peer:
                    try:
                        data = self.dev_peer.recv(4096)
                    except OSError:
                        continue
                    if data and self.plugged and not self._closed_pc:
                        self._to_pc(data)
                    continue
                data = self._read_pc(src)
                if not data:
                    continue
                if self._closed_pc:
                    continue
                if self.plugged:
                    self.from_pc.extend(data)
                    _sendall(self.dev_peer, data)
                else:
                    self.sent_while_unplugged.extend(data)

    def stop(self):
        self._stop.set()
        self.thread.join(2)
        self.pull_usb()
        socks = [self.dev_peer, self.dev_sock, self.listener, self.conn]
        for s in socks:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        if self.pc_slave is not None:
            try:
                os.close(self.pc_slave)
            except OSError:
                pass


class FakeDevice(object):
    """ソケットの組の機器側で動く、ログインとシェルのまね。"""

    def __init__(self, relay, login=True, user="root", password="root", ansi=False,
                 garbage=False, ticker=None, wrap=0, wrap_repeat=False, responses=None,
                 ignore_interrupt=(), left_at_password=False, status_prompt=False, dollar_q=True,
                 prompt_text=None):
        self.relay = relay
        self.sock = relay.dev_sock
        self.need_login = login
        self.user = user
        self.password = password
        self.ansi = ansi
        self.garbage = garbage
        self.ticker = ticker
        self.wrap = wrap
        self.wrap_repeat = wrap_repeat
        self.responses = dict(DEFAULT_RESPONSES)
        if responses:
            self.responses.update(responses)
        self.ignore_interrupt = set(ignore_interrupt)
        self.status_prompt = status_prompt  # プロンプトの頭に、直前の終了コードを出す（例：[1] root@…# ）
        self.dollar_q = dollar_q            # False なら $? が使えないシェルのまね
        self.status = 0                     # 直前のコマンドの終了コード（$?）
        self.prompt_text = prompt_text      # プロンプトを差し替える（複数の行のプロンプトなど）
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
        _sendall(self.sock, data)

    def raw(self, data):
        _sendall(self.sock, data)

    def prompt(self):
        self.col = 0
        text = self.prompt_text or (ANSI_PROMPT if self.ansi else PLAIN_PROMPT)
        if self.status_prompt:
            text = "[%d] %s" % (self.status, text)
        self.out(text)

    def login_prompt(self):
        self.out("\n%s login: " % HOST)

    def start(self):
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
                ready, _, _ = select.select([self.sock], [], [], 0.02)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                data = self.sock.recv(1024)
            except OSError:
                continue
            if not data:
                return  # 中継が閉じた
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
                if self.running in self.ignore_interrupt or self.running.startswith("start-app /opt/stuck"):
                    self.out("^C")
                    return
                self.running = None
                self.status = 130
                self.out("^C\n")
                self.prompt()
            return
        if ch == "\x03":
            self.interrupts += 1
            self.line = ""
            self.status = 130
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
            if self.wrap and self.wrap_repeat and self.col > self.wrap and self.col % self.wrap == 1:
                # bash（端末が vt102 など）のまね：次の行の頭の1文字を出し、\r の後にもう一度出す
                self.raw(b"\r" + ch.encode("utf-8"))
            elif self.wrap and not self.wrap_repeat and self.col % self.wrap == 0:
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
        if cmd.startswith("start-app /opt/hang") or cmd.startswith("start-app /opt/stuck"):
            self.out("[DBG] start\n")
            self.running = cmd
            return
        if cmd == "pull-usb":
            self.out("bye\n")
            time.sleep(0.2)
            self.relay.pull_usb()
            return
        if cmd == "slow":
            self.status = 0
            self.out("first\n")
            self.scheduled.append((time.monotonic() + 0.6, "second\n", True))
            return
        if cmd == "noisy":
            # プロンプトのすぐ後ろに、カーネルのメッセージが出る
            self.status = 0
            self.out("[DBG] VALUE=3\n")
            self.prompt()
            self.scheduled.append((time.monotonic() + 0.05, KERNEL_LINE + "\n", False))
            return
        if cmd == "tricky":
            # 出力の途中に「# 」が来て、少し間が空いてから続きが来る
            self.status = 0
            self.out("value # ")
            self.scheduled.append((time.monotonic() + 0.05, "done\n", True))
            return
        if cmd in self.responses:
            text = self.responses[cmd]
            status = 0
            if isinstance(text, tuple):
                text, status = text
            if text:
                self.out(text + "\n")
            self.status = status
            self.prompt()
            return
        if cmd == "echo" or cmd.startswith("echo "):
            self._echo(cmd[4:].strip())
            self.prompt()
            return
        self.out("-sh: %s: not found\n" % cmd.split()[0])
        self.status = 127
        self.prompt()

    def _echo(self, args):
        """echo のまね。$? を直前の終了コードに置き換え、引用符を外す。"""
        if "$?" in args and not self.dollar_q:
            # fish のように $? が使えないシェル
            self.out("fish: $? is not the exit status. In fish, please use $status.\n")
            self.out("echo %s\n" % args)
            self.status = 1
            return
        text = args.replace("$?", str(self.status)).replace('"', "").replace("'", "")
        self.out(text + "\n")
        self.status = 0
