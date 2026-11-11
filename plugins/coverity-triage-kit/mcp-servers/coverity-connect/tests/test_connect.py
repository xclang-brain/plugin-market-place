"""The client in the test process, for what the server process shows on Windows only: the proxy exceptions
that the OS keeps (the registry), and the ending of the authentication sentences."""

from __future__ import annotations

import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import support

if not support.MISSING:
    sys.path.insert(0, str(support.SERVER / "src"))
    from coverity_mcp.connect import Client
    from coverity_mcp.settings import Failure


class Proxy(BaseHTTPRequestHandler):
    """A proxy that notes every request and refuses it."""

    def do_GET(self) -> None:
        self.server.asked.append(self.path)
        self.send_error(403)

    def log_message(self, *args) -> None:
        pass


@unittest.skipIf(support.MISSING, f"run the tests in the environment of the server (see support.py): {support.MISSING}")
class ConnectTest(unittest.TestCase):
    def streams(self, excepted: bool) -> tuple[list[str] | str, list[str]]:
        """What the client reads (the streams, or the sentence of its failure) and the requests that reached
        the proxy, on a PC whose OS has a proxy and answers ``excepted`` when asked whether the host of
        Coverity is one of its exceptions."""
        emu, proxy = support.Emulator(), ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        proxy.asked = []
        threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        # As Windows with "use a proxy server" answers urllib: the proxy, and no list of exceptions.
        env = {name: value for name, value in os.environ.items() if not name.lower().endswith("_proxy")}
        env.update(http_proxy=f"http://127.0.0.1:{proxy.server_port}", COV_USER=emu.user, COV_AUTH_KEY=emu.key)
        with emu, mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.proxy_bypass", return_value=excepted) as asked:
            try:
                with Client(emu.url) as client:
                    read = client.streams()
            except Failure as exc:
                read = str(exc)
        asked.assert_called_once_with("127.0.0.1")
        return read, proxy.asked

    def test_a_host_that_the_os_excepts_from_its_proxy_is_reached_directly(self) -> None:
        self.assertEqual(self.streams(excepted=True), (["Sample-main"], []))

    def test_another_host_is_reached_through_the_proxy_of_the_os(self) -> None:
        read, asked = self.streams(excepted=False)  # the refusal of the proxy is what the client reads
        self.assertEqual((read.split("。")[0], len(asked)), ("認証できませんでした（HTTP 403）", 1))

    def test_on_windows_the_authentication_sentences_say_what_to_do_after_saving_again(self) -> None:
        sentences = {401: "認証できませんでした（HTTP 401）。COV_USER と COV_AUTH_KEY を確認してください",
                     403: "認証できませんでした（HTTP 403）。COV_USER と COV_AUTH_KEY、ユーザーの権限（Access Web services）"
                          "を確認してください"}
        for platform, ending in (("win32", "。保存し直したときは、新しいターミナルで copilot を起動し直してください"), ("linux", "")):
            with mock.patch.object(sys, "platform", platform):
                self.assertEqual({status: str(Client._not_authenticated(status)) for status in sentences},
                                 {status: sentence + ending for status, sentence in sentences.items()}, platform)


if __name__ == "__main__":
    unittest.main()
