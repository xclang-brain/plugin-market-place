"""The results and settings screen in a real browser (design 9.1, REQ-423 for the screen of the results).

A run folder is made by the batch run itself (a temporary repository, the warnings from files, the scripted
fake agents of tests/integration/flowlib.py, then summary), so it has the shape the run really writes.
The screen's server is started with its open command (--no-browser, its state in a temporary folder, a short
idle time), gui_screen.mjs drives Chromium through Playwright (390 px and 1280 px, light and dark), and this
test checks the saved files: the approval chosen in the list and the deviation comment edited in the detail
are in approvals.json, which still fits its schema, and the apply step reads them. The server is stopped at
the end with the stop command.

Skipped with the reason when Node.js, Playwright, uv, git or a part is missing.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE.parent / "integration"))
import flowlib as fl  # noqa: E402

pp = fl.pp
NODE = shutil.which("node")
SCRIPT = HERE / "gui_screen.mjs"
COMMENT = "意図的。hal_write は書いた数だけを返し、失敗の値を返さないため、戻り値を使わなくてよい（src/uart.c:1）。"


def playwright_available() -> bool:
    if NODE is None:
        return False
    try:
        done = subprocess.run([NODE, str(SCRIPT), "--check"], capture_output=True, timeout=60, cwd=str(HERE))
        return json.loads(done.stdout.decode("utf-8") or "{}").get("playwright") is True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def skip_reason() -> str | None:
    if os.environ.get("SKIP_SLOW_TESTS") == "1":
        return "SKIP_SLOW_TESTS=1 のため飛ばしました"
    reason = fl.prerequisites()
    if reason:
        return reason
    if fl.command_script("guiCommand") is None:
        return "結果と設定の画面のコマンド（defaults.json の guiCommand）がまだありません"
    if NODE is None:
        return "Node.js がありません"
    if not playwright_available():
        return "Playwright がありません（npm install --prefix tests --no-save --no-package-lock playwright で入れると動きます）"
    return None


SKIP = skip_reason()


def ping(port: int, token: str) -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/api/ping", headers={"Host": f"127.0.0.1:{port}", "X-Token": token})
        return conn.getresponse().status == 200
    except OSError:
        return False
    finally:
        conn.close()


@unittest.skipIf(SKIP, SKIP or "")
class GuiScreenTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="ui-")).resolve()
        cls.env = fl.base_env(cls.tmp)
        cls.env.update(TMPDIR=str(cls.tmp), TEMP=str(cls.tmp), TMP=str(cls.tmp), GUI_STATE_DIR=str(cls.tmp / "gui-state"),
                       GUI_IDLE_SECONDS="300")
        cls.gui_py = fl.command_script("guiCommand")
        cls.started = False
        try:
            cls.make_run()
            cls.open_screen()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def make_run(cls) -> None:
        cls.repo = cls.tmp / "repo"
        cls.repo.mkdir()
        revision = fl.write_repo(cls.repo, cls.env)
        cls.settings_url = "https://coverity.example.invalid:8443"
        fl.write_settings(cls.repo, cls.tmp / "out", cls.settings_url, extra_coverity="  api: fake\n", env=cls.env)
        cls.cids = {w["key"]: 40001 + n for n, w in enumerate(fl.WARNINGS)}
        issues = fl.offline_files(cls.tmp / "fetched", cls.cids, revision)
        run_py = fl.command_script("runCommand")
        created = fl.run_script(run_py, "new-run", "--repo", str(cls.repo), "--issues-file", str(issues), env=cls.env, cwd=cls.repo)
        cls.run_dir = Path(created["run_dir"])
        # one item reviewed as "flag" (its confidence is capped), so the screen shows a review mark
        agents = fl.FakeAgents(cls.cids, review_outcomes={str(cls.cids["C"]): ["flag"]})
        fl.Orchestrator(run_py, cls.run_dir, cls.env, cls.repo, agents).drive(limit_seconds=150)
        cls.summary = fl.run_script(run_py, "summary", "--run", str(cls.run_dir), env=cls.env, cwd=cls.repo)
        cls.items = sorted(json.loads((cls.run_dir / "approvals.json").read_text(encoding="utf-8"))["items"])

    @classmethod
    def open_screen(cls) -> None:
        opened = fl.run_script(cls.gui_py, "open", "--repo", str(cls.repo), "--run", str(cls.run_dir), "--view", "summary",
                               "--no-browser", env=cls.env, cwd=cls.repo, timeout=180)
        cls.started = True
        cls.opened = opened
        split = urlsplit(opened["url"])
        cls.port = split.port
        cls.token = parse_qs(split.query)["t"][0]
        cls.base = f"{split.scheme}://{split.netloc}/"

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "started", False):
            try:
                fl.run_script(cls.gui_py, "stop", "--repo", str(cls.repo), env=cls.env, cwd=cls.repo, ok=None, timeout=60)
            except Exception:  # noqa: BLE001 - cleaning up
                pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def browse(self) -> dict:
        if getattr(GuiScreenTest, "report", None) is not None:
            return GuiScreenTest.report
        group = next(i for i in self.items if i.startswith("G"))
        config = {"base": self.base, "token": self.token, "run": self.run_dir.name, "rows": self.items, "reject": group,
                  "deviation": str(self.cids["C"]), "comment": COMMENT, "settingsUrl": self.settings_url,
                  "out": str(self.tmp / "ui-report.json")}
        path = self.tmp / "ui-config.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        done = subprocess.run([NODE, str(SCRIPT), str(path)], cwd=str(HERE), capture_output=True, timeout=600,
                              env=dict(self.env))
        out = self.tmp / "ui-report.json"
        if not out.is_file():
            self.fail("ブラウザーの試験が結果を書きませんでした：" + (done.stdout + done.stderr).decode("utf-8", "replace")[-2000:])
        GuiScreenTest.report = json.loads(out.read_text(encoding="utf-8"))
        GuiScreenTest.report["reject"] = group
        return GuiScreenTest.report

    # ---- the screen ------------------------------------------------------------------------------

    def test_0_the_page_checks_find_problems(self) -> None:
        """The checks of gui_screen.mjs find a page wider than the screen, low contrast and a light page in dark mode,
        and pass a table that scrolls in its own box and a readable dark page"""
        done = subprocess.run([NODE, str(SCRIPT), "--selftest"], cwd=str(HERE), capture_output=True, timeout=180)
        found = json.loads(done.stdout.decode("utf-8") or "{}")
        self.assertTrue(found.get("overflow"), found)
        self.assertTrue(found.get("lowContrast"), found)
        self.assertTrue(any("背景が明るい" in p for p in found.get("lightInDark") or []), found)
        self.assertEqual(found.get("scrollBoxIsFine"), [], found)
        self.assertEqual(found.get("goodIsFine"), [], found)

    def test_1_open_returns_a_local_url_with_the_run(self) -> None:
        self.assertTrue(self.opened["url"].startswith("http://127.0.0.1:"), self.opened)
        self.assertIn(f"run={self.run_dir.name}", self.opened["url"])
        self.assertFalse(self.opened.get("browser_opened"), self.opened)
        self.assertTrue(ping(self.port, self.token), "画面のサーバが答えません")

    def test_2_the_screen_in_the_browser(self) -> None:
        report = self.browse()
        self.assertIsNone(report.get("fatal"), report.get("fatal"))
        names = [c["name"] for c in report["checks"]]
        for width in ("390px", "1280px"):
            for scheme in ("ライト", "ダーク"):
                self.assertTrue(any(width in n and scheme in n for n in names), f"{width} {scheme} を確かめていません：{names}")
        for c in report["checks"]:
            with self.subTest(check=c["name"]):
                self.assertEqual(c["problems"], [], c["name"])

    def test_3_the_saved_choices_are_in_approvals_json_and_apply_reads_them(self) -> None:
        report = self.browse()
        self.assertIsNone(report.get("fatal"), report.get("fatal"))
        approvals = json.loads((self.run_dir / "approvals.json").read_text(encoding="utf-8"))
        schema = fl.command_script("runCommand").parents[1] / "assets" / "approvals.schema.json"
        if schema.is_file():
            self.assertEqual(pp.schema_errors(approvals, json.loads(schema.read_text(encoding="utf-8"))), [],
                             "画面が書いた approvals.json が approvals.schema.json に合いません")
        group = report["reject"]
        self.assertEqual(approvals["items"][group]["decision"], "reject")
        self.assertTrue(approvals["items"][group]["edited"])
        deviation = approvals["items"][str(self.cids["C"])]
        self.assertEqual(deviation["decision"], "deviation")
        self.assertEqual((deviation.get("deviation") or {}).get("comment"), COMMENT)
        apply_py = fl.skill_script("applySkill")
        if apply_py is None:
            self.skipTest("反映のスクリプトがまだありません")
        preview = fl.run_script(apply_py, "preview", "--run", str(self.run_dir), env=self.env, cwd=self.repo)
        plan = json.loads(Path(preview["plan_file"]).read_text(encoding="utf-8"))
        self.assertEqual([e["comment"] for e in plan["triage"]], [COMMENT], "反映が、画面で直した逸脱コメントを使いません")
        self.assertEqual([e["id"] for e in plan["reject"]], [group], "反映が、画面で選んだ却下を使いません")

    def test_4_stop_stops_the_server(self) -> None:
        self.browse()
        stopped = fl.run_script(self.gui_py, "stop", "--repo", str(self.repo), env=self.env, cwd=self.repo)
        self.assertTrue(stopped.get("stopped"), stopped)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and ping(self.port, self.token):
            time.sleep(0.1)
        self.assertFalse(ping(self.port, self.token), "止めた後も、画面のサーバが答えます")
        GuiScreenTest.started = False


if __name__ == "__main__":
    unittest.main()
