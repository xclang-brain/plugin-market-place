"""画面のサーバー（server.py）と画面のファイルのテスト。アクセスの制限と、記録の HTML が動かないこと（REQ-402〜REQ-407）。

  python3 -m unittest discover -s tests
"""

import json
import re
import time
import unittest

import support
from support import DATA, request, start_server, temp_project

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
HTML_IN_RECORD = '<img src=x onerror="alert(1)"><script>alert(2)</script>'


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.project = temp_project(self)
        folder = self.project / DATA / "sessions" / "s1"
        folder.mkdir(parents=True)
        events = [{"kind": "prompt", "text": HTML_IN_RECORD, "time": "2026-11-09T10:00:00+09:00"},
                  {"kind": "request", "agent": "debug-verifier", "text": "判定して", "time": "2026-11-09T10:01:00+09:00"}]
        (folder / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        self.proc, self.info = start_server(self, self.project)
        self.port = self.info["port"]

    def test_url_has_a_token_after_hash(self):
        self.assertRegex(self.info["url"], r"^http://127\.0\.0\.1:%d/#k=[A-Za-z0-9_-]{30,}$" % self.port)

    def test_security_headers_on_every_response(self):
        for path in ("/", "/app.js", "/style.css", "/nothing", "/api/state"):
            status, headers, _ = request(self.info, "GET", path, token=False)
            self.assertEqual(headers.get("Content-Security-Policy"), CSP, path)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(headers.get("Referrer-Policy"), "no-referrer", path)
            self.assertEqual(headers.get("Cache-Control"), "no-store", path)
            self.assertNotIn("Set-Cookie", headers)

    def test_static_files_only_from_the_list(self):
        self.assertEqual(request(self.info, "GET", "/", token=False)[0], 200)
        self.assertIn("javascript", request(self.info, "GET", "/app.js", token=False)[1]["Content-Type"])
        for path in ("/server.py", "/static/app.js", "/../hook.py", "/%2e%2e/hook.py", "/favicon.ico", "/index.htm"):
            self.assertEqual(request(self.info, "GET", path, token=False)[0], 404, path)

    def test_api_needs_the_token(self):
        self.assertEqual(request(self.info, "GET", "/api/state", token=False)[0], 403)
        self.assertEqual(request(self.info, "GET", "/api/state", token="wrong")[0], 403)
        self.assertEqual(request(self.info, "POST", "/api/stop", {"session": "s1"}, token="wrong")[0], 403)
        self.assertEqual(request(self.info, "GET", "/api/state")[0], 200)

    def test_wrong_host_is_rejected(self):
        for host in ("evil.example:%d" % self.port, "127.0.0.1:1", "127.0.0.1", "[::1]:%d" % self.port):
            self.assertEqual(request(self.info, "GET", "/", host=host)[0], 403, host)
            self.assertEqual(request(self.info, "GET", "/api/state", host=host)[0], 403, host)
        self.assertEqual(request(self.info, "GET", "/api/state", host="localhost:%d" % self.port)[0], 200)

    def test_post_from_another_origin_is_rejected(self):
        stop = self.project / DATA / "sessions" / "s1" / "stop"
        for origin in ("http://evil.example", "null", False, "http://localhost:%d" % self.port):
            self.assertEqual(request(self.info, "POST", "/api/stop", {"session": "s1"}, origin=origin)[0], 403, origin)
        self.assertFalse(stop.exists())

    def test_post_must_be_json(self):
        stop = self.project / DATA / "sessions" / "s1" / "stop"
        for ctype in ("text/plain", "application/x-www-form-urlencoded", None):
            self.assertEqual(request(self.info, "POST", "/api/stop", {"session": "s1"}, ctype=ctype)[0], 415, ctype)
        self.assertEqual(request(self.info, "POST", "/api/stop", b"session=s1")[0], 400)
        self.assertFalse(stop.exists())

    def test_stop_creates_the_marker(self):
        status, _, body = request(self.info, "POST", "/api/stop", {"session": "s1"})
        self.assertEqual(status, 200, body)
        self.assertTrue((self.project / DATA / "sessions" / "s1" / "stop").is_file())
        for session in ("../s1", "nothing", "", None):
            self.assertEqual(request(self.info, "POST", "/api/stop", {"session": session})[0], 404, session)

    def test_state_and_session(self):
        status, headers, body = request(self.info, "GET", "/api/state")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        state = json.loads(body)
        self.assertEqual([s["id"] for s in state["sessions"]], ["s1"])
        self.assertEqual((state["sessions"][0]["stage"], state["sessions"][0]["stopped"]), ("review", False))
        status, headers, body = request(self.info, "GET", "/api/sessions/s1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(json.loads(body)["events"][0]["text"], HTML_IN_RECORD)
        self.assertEqual(request(self.info, "GET", "/api/sessions/..%2Fs1")[0], 404)

    def test_settings_are_saved(self):
        form = {"goal": "平均値が違う", "criteria": "C の1\nC の2", "max_trials": 3}
        self.assertEqual(request(self.info, "POST", "/api/settings", form)[0], 200)
        self.assertEqual(json.loads(request(self.info, "GET", "/api/state")[2])["settings"], form)
        for bad in (dict(form, max_trials=0), dict(form, max_trials="3"), dict(form, goal=None)):
            self.assertEqual(request(self.info, "POST", "/api/settings", bad)[0], 400, bad)

    def test_recording_marker_is_fresh(self):
        recording = json.loads((self.project / DATA / "recording").read_text(encoding="utf-8"))
        self.assertEqual((recording["pid"], recording["port"]), (self.proc.pid, self.port))
        self.assertLess(abs(time.time() - recording["time"]), 60)

    def test_shutdown_removes_the_recording_marker(self):
        self.assertEqual(request(self.info, "POST", "/api/shutdown")[0], 200)
        self.proc.wait(10)
        self.assertFalse((self.project / DATA / "recording").exists())
        self.assertFalse((self.project / DATA / "server.json").exists())


class PageTest(unittest.TestCase):
    """記録の中の HTML が動かないこと（設計書 8.5）"""

    STATIC = support.SCRIPTS / "static"

    def test_app_js_never_parses_html(self):
        code = (self.STATIC / "app.js").read_text(encoding="utf-8")
        for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                     "DOMParser", "createContextualFragment"):
            self.assertNotIn(word, code)
        self.assertIn("textContent", code)

    def test_index_html_has_no_inline_script_or_style(self):
        page = (self.STATIC / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", page), [])
        self.assertNotRegex(page, r"<style|\sstyle=|\son[a-z]+=|javascript:")

    def test_static_files_are_listed(self):
        names = sorted(p.name for p in self.STATIC.iterdir())
        self.assertEqual(names, ["app.js", "index.html", "style.css"])


if __name__ == "__main__":
    unittest.main()
