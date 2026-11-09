"""The server answers only this PC, with the token and the right Host; POST only same-origin JSON."""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path

import helpers

RUN = "20261108-101500"


class SecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="gui-sec-"))
        cls.repo, cls.out = helpers.make_repo(cls.tmp)
        cls.run_dir = helpers.make_run(cls.out, RUN)
        (cls.tmp / "secret.txt").write_text("secret", encoding="utf-8")
        cls.srv = helpers.Server(cls.repo, cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_api_needs_the_token_and_the_host(self) -> None:
        self.assertEqual(self.srv.get("/api/runs")[0], 200)
        self.assertEqual(self.srv.get("/api/runs", token=False)[0], 403)
        self.assertEqual(self.srv.get("/api/runs", token=False, headers={"X-Token": "wrong"})[0], 403)
        self.assertEqual(self.srv.get("/api/runs", host="evil.example:80")[0], 403)
        self.assertEqual(self.srv.get("/api/runs", host=f"127.0.0.1.evil.example:{self.srv.port}")[0], 403)
        self.assertEqual(self.srv.get("/api/runs", host=f"localhost:{self.srv.port}")[0], 200)

    def test_page_sets_a_cookie_that_then_works_alone(self) -> None:
        status, headers, body = self.srv.request("GET", f"/?t={self.srv.token}", token=False)
        self.assertEqual(status, 200)
        cookie = headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        pair = cookie.split(";")[0]
        status, data = self.srv.get("/api/runs", token=False, headers={"Cookie": pair})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self.srv.request("GET", "/", token=False, headers={"Cookie": pair})[0], 200)
        self.assertEqual(self.srv.request("GET", "/", token=False)[0], 403)
        self.assertEqual(self.srv.request("GET", "/?t=wrong", token=False)[0], 403)
        self.assertEqual(self.srv.request("GET", f"/?t={self.srv.token}", token=False, host="evil.example")[0], 403)
        self.assertEqual(self.srv.get("/api/runs", token=False, headers={"Cookie": pair.split("=")[0] + "=wrong"})[0], 403)

    def test_page_has_a_strict_policy_and_a_nonce(self) -> None:
        status, headers, body = self.srv.request("GET", f"/?t={self.srv.token}", token=False)
        csp = headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("'unsafe-inline'", csp.split("script-src")[1].split(";")[0])
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'<script nonce="{nonce}">'.encode(), body)
        self.assertNotIn(b"__NONCE__", body)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["referrer-policy"], "no-referrer")

    def test_post_needs_json_from_the_same_origin(self) -> None:
        body = {"item": "20001", "decision": "fix"}
        path = f"/api/runs/{RUN}/approvals"
        self.assertEqual(self.srv.post(path, body)[0], 200)
        self.assertEqual(self.srv.post(path, body, headers={"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.srv.post(path, body, headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 403)
        self.assertEqual(self.srv.post(path, body, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.srv.post(path, body, headers={"Origin": ""})[0], 403)
        self.assertEqual(self.srv.post(path, body, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.srv.post(path, body, token=False)[0], 403)
        self.assertEqual(self.srv.post(path, body, host="evil.example")[0], 403)

    def test_broken_bodies_get_400_and_the_server_keeps_running(self) -> None:
        path = "/api/save-check"
        self.assertEqual(self.srv.request("POST", path, raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.srv.request("POST", path, raw=b"{broken")[0], 400)
        self.assertEqual(self.srv.request("POST", path, raw="{\"a\": \"\udcff\"}".encode("utf-8", "surrogatepass"))[0], 400)
        self.assertEqual(self.srv.request("POST", path, raw=b"{}", headers={"Content-Length": "abc"})[0], 400)
        self.assertEqual(self.srv.request("POST", path, raw=b"{}", headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.srv.get("/api/ping")[0], 200)

    def test_a_refused_body_of_unknown_length_still_gets_its_answer(self) -> None:
        """The server cannot read a body whose length it does not know, and closes the connection. The client must
        still get the 400 when it reads late: closing with unread data would reset the connection (seen on Windows)."""
        with socket.create_connection(("127.0.0.1", self.srv.port), timeout=10) as sock:
            sock.sendall((f"POST /api/save-check HTTP/1.1\r\nHost: {self.srv.host}\r\nX-Token: {self.srv.token}\r\n"
                          f"Origin: {self.srv.origin}\r\nContent-Type: application/json\r\nContent-Length: abc\r\n\r\n"
                          ).encode("ascii"))
            time.sleep(0.2)  # the server has read the head and answered; the body comes after (as with two packets)
            sock.sendall(b'{"a": 1}' * 64)
            time.sleep(0.3)
            # the server still reads what comes: sending to a socket it had closed would be refused (a reset)
            sock.sendall(b" " * 64)
            time.sleep(0.1)
            sock.sendall(b" ")
            answer = b""
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                answer += part
        self.assertTrue(answer.startswith(b"HTTP/1.1 400"), answer[:80])
        self.assertEqual(self.srv.get("/api/ping")[0], 200)

    def test_a_refused_post_does_not_break_the_next_request_on_the_connection(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        try:
            payload = b'{"item": "20001", "decision": "fix"}'
            conn.request("POST", f"/api/runs/{RUN}/approvals", body=payload,
                         headers={"Host": self.srv.host, "X-Token": self.srv.token, "Content-Type": "text/plain",
                                  "Origin": self.srv.origin})
            res = conn.getresponse()
            res.read()
            self.assertEqual(res.status, 403)
            conn.request("GET", "/api/ping", headers={"Host": self.srv.host, "X-Token": self.srv.token})
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            self.assertIn(b'"ok": true', res.read())
        finally:
            conn.close()

    def test_only_the_files_of_a_run_are_served(self) -> None:
        base = f"/files/{RUN}"
        self.assertEqual(self.srv.request("GET", f"{base}/summary.html")[0], 200)
        self.assertEqual(self.srv.request("GET", f"{base}/cid/20001.html")[0], 200)
        self.assertEqual(self.srv.request("GET", f"{base}/patches/20001-fix.patch")[0], 200)
        status, headers, _ = self.srv.request("GET", f"{base}/fixed/20001/fix/src/%E8%AA%AD%E3%82%80%20%E3%83%95%E3%82%A1%E3%82%A4%E3%83%AB.c")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        self.assertIn("sandbox", headers["content-security-policy"])
        up = ".."
        for bad in (f"{base}/run.json", f"{base}/approvals.json", f"{base}/items/20001/result.json",
                    f"{base}/{up}/{up}/secret.txt", f"{base}/cid/%2e%2e/run.json", f"{base}/cid%2f20001.html",
                    f"{base}/cid/{up}%2f{up}%2fsecret.txt", f"/files/{up}/secret.txt", "/files/20261108-999999/summary.html",
                    f"/view/{RUN}/summary.html", f"{base}/fixed/20001/fix/src/missing.c"):
            self.assertEqual(self.srv.request("GET", bad)[0], 404, bad)
        self.assertEqual(self.srv.request("GET", f"{base}/summary.html", token=False)[0], 403)

    @unittest.skipIf(os.name == "nt", "symbolic links need extra rights on Windows")
    def test_a_link_inside_the_run_is_not_followed(self) -> None:
        link = self.run_dir / "cid" / "link.html"
        link.symlink_to(self.tmp / "secret.txt")
        self.addCleanup(link.unlink)
        self.assertEqual(self.srv.request("GET", f"/files/{RUN}/cid/link.html")[0], 404)

    def test_view_pages_escape_and_have_no_script(self) -> None:
        patch = self.run_dir / "patches" / "20001-fix.patch"
        before = patch.read_bytes()
        self.addCleanup(patch.write_bytes, before)
        patch.write_text("@@ -1 +1 @@\n+<script>alert(1)</script>\n", encoding="utf-8")
        status, headers, body = self.srv.request("GET", f"/view/{RUN}/patches/20001-fix.patch")
        self.assertEqual(status, 200)
        self.assertNotIn(b"<script>", body)
        self.assertIn(b"&lt;script&gt;", body)
        self.assertIn("sandbox", headers["content-security-policy"])

    def test_listens_on_127_0_0_1_only(self) -> None:
        address = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("10.255.255.255", 1))
                address = probe.getsockname()[0]
        except OSError:
            pass
        if not address or address.startswith("127."):
            self.skipTest("this PC has no other address to try")
        with self.assertRaises(OSError):
            socket.create_connection((address, self.srv.port), timeout=2).close()


if __name__ == "__main__":
    unittest.main()
