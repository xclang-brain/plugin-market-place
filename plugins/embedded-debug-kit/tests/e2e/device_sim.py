"""通しのテスト（E2E）のための模擬の機器。この PC の上で、機器の FTP サーバーとシリアルコンソールの代わりを動かす。

- FTP サーバー：127.0.0.1:<--ftp-port>。ユーザー root、パスワード sim。機器のルートは --root のフォルダ。
- コンソール：127.0.0.1:<--shell-port> の TCP。つなぐと、--root のフォルダで本物の bash が動く
  （シリアルの代わりに socket://127.0.0.1:<番号> でつなぐ）。

機器のルートからの相対パスで、FTP の送り先とシェルのパスがそろう（例：送り先 app → シェルの ./app）。
Linux と macOS で動く（疑似端末を使う）。pyftpdlib が要る（pip install pyftpdlib）。

  python3 tests/e2e/device_sim.py --root /tmp/device
"""

import argparse
import os
import pty
import select
import signal
import socket
import sys
import threading

USER = "root"
PASSWORD = "sim"
PROMPT = "root@sim:~# "


def serve_console(conn, root):
    """1つの接続に、疑似端末の上の bash を1つ割り当て、バイトを運ぶ"""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(root)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": root, "PS1": PROMPT, "TERM": "dumb", "LANG": "C.UTF-8"}
        os.execvpe("bash", ["bash", "--norc", "--noprofile", "-i"], env)
    try:
        while True:
            ready, _, _ = select.select([conn, fd], [], [])
            if conn in ready:
                data = conn.recv(4096)
                if not data:
                    break
                os.write(fd, data)
            if fd in ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                conn.sendall(data)
    finally:
        conn.close()
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        os.waitpid(pid, 0)


def console_server(port, root):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen()
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve_console, args=(conn, root), daemon=True).start()


def ftp_server(port, root):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    authorizer = DummyAuthorizer()
    authorizer.add_user(USER, PASSWORD, root, perm="elradfmwMT")  # M は権限の変更（SITE CHMOD）
    handler = FTPHandler
    handler.authorizer = authorizer
    return FTPServer(("127.0.0.1", port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="通しのテスト（E2E）のための模擬の機器（FTP とコンソール）")
    parser.add_argument("--root", required=True, help="機器のルートにするフォルダ（なければ作る）")
    parser.add_argument("--ftp-port", type=int, default=2121)
    parser.add_argument("--shell-port", type=int, default=2323)
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    os.makedirs(root, exist_ok=True)
    ftp = ftp_server(args.ftp_port, root)
    threading.Thread(target=console_server, args=(args.shell_port, root), daemon=True).start()
    print("模擬の機器：ルート {}、FTP 127.0.0.1:{}（{} / {}）、コンソール socket://127.0.0.1:{}".format(
        root, args.ftp_port, USER, PASSWORD, args.shell_port), file=sys.stderr, flush=True)
    try:
        ftp.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
