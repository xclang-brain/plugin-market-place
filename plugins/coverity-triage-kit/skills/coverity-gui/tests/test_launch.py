"""gui.py open / stop: a detached server per repository, reused, stopped by the button, the command or idleness."""

from __future__ import annotations

import http.client
import json
import re
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import helpers

RUN = "20261108-101500"


def ping(port: int, token: str) -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/api/ping", headers={"Host": f"127.0.0.1:{port}", "X-Token": token})
        return conn.getresponse().status == 200
    except OSError:
        return False
    finally:
        conn.close()


def parts(url: str) -> tuple[int, str, dict[str, str]]:
    split = urlsplit(url)
    token = parse_qs(split.query)["t"][0]
    return split.port, token, {k: v[0] for k, v in parse_qs(split.fragment).items()}


class LaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = helpers.temp_dir(self)
        self.repo, self.out = helpers.make_repo(self.tmp)
        self.run_dir = helpers.make_run(self.out, RUN)
        self.env = helpers.env(self.tmp)

    def open(self, *args: str, environ: dict[str, str] | None = None) -> dict:
        code, data = helpers.run_gui(["open", "--repo", str(self.repo), "--no-browser", *args], environ or self.env)
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"], data)
        return data

    def stop(self) -> dict:
        return helpers.run_gui(["stop", "--repo", str(self.repo)], self.env)[1]

    def test_open_starts_a_server_returns_the_url_and_reuses_it(self) -> None:
        first = self.open("--run", str(self.run_dir), "--item", "20001")
        self.addCleanup(self.stop)
        self.assertRegex(first["url"], r"^http://127\.0\.0\.1:\d+/\?t=[A-Za-z0-9_-]{20,}#")
        port, token, fragment = parts(first["url"])
        self.assertEqual(fragment, {"run": RUN, "view": "summary", "item": "20001"})
        self.assertFalse(first["reused"])
        self.assertFalse(first["browser_opened"])
        self.assertTrue(ping(port, token))
        second = self.open("--view", "settings")
        self.assertTrue(second["reused"])
        self.assertEqual(parts(second["url"])[:2], (port, token))
        self.assertEqual(parts(second["url"])[2], {"view": "settings"})
        monitor = self.open("--run", RUN)
        self.assertEqual(parts(monitor["url"])[2], {"run": RUN, "view": "monitor"})
        record = next((self.tmp / "state").glob("*.json"))
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["port"], port)
        stopped = self.stop()
        self.assertTrue(stopped["stopped"])
        for _ in range(100):
            if not ping(port, token):
                break
            time.sleep(0.05)
        self.assertFalse(ping(port, token))
        self.assertFalse(self.stop()["stopped"])

    def test_a_server_quiet_for_the_idle_time_stops_by_itself(self) -> None:
        environ = {**self.env, "GUI_IDLE_SECONDS": "1"}
        data = self.open(environ=environ)
        port, token, _ = parts(data["url"])
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and ping(port, token):
            time.sleep(0.2)
        self.assertFalse(ping(port, token))
        for _ in range(50):  # the record of the server goes with it
            if not list((self.tmp / "state").glob("*.json")):
                break
            time.sleep(0.1)
        self.assertEqual(list((self.tmp / "state").glob("*.json")), [])

    def test_wrong_arguments_are_told_as_one_json_object(self) -> None:
        for args in (["open", "--repo", str(self.tmp / "missing"), "--no-browser"],
                     ["open", "--repo", str(self.repo), "--item", "20001", "--no-browser"],
                     ["open", "--repo", str(self.repo), "--run", str(self.tmp), "--no-browser"],
                     ["open", "--repo", str(self.repo), "--view", "other"],
                     ["open"]):
            code, data = helpers.run_gui(args, self.env)
            self.assertEqual(code, 1, args)
            self.assertFalse(data["ok"])
            self.assertTrue(re.search(r"[ぁ-んァ-ン一-龥]", data["error"]), data["error"])
        self.assertFalse((self.tmp / "state").exists() and list((self.tmp / "state").glob("*.json")))

    def test_a_run_outside_the_output_folder_gets_a_note(self) -> None:
        other = helpers.make_run(self.tmp / "elsewhere", RUN)
        data = self.open("--run", str(other))
        self.addCleanup(self.stop)
        self.assertIn("結果を置く場所", data["note"])


if __name__ == "__main__":
    unittest.main()
