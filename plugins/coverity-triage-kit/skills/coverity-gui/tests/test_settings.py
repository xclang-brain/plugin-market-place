"""The 設定 tab: what it shows, the checks before saving, and saving that keeps comments."""

from __future__ import annotations

import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

LOST = "このファイルでは、注釈は動いたり消えたりします"


class SettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="gui-set-"))
        cls.repo, cls.out = helpers.make_repo(cls.tmp)
        cls.folder = cls.repo / ".coverity-triage"
        cls.files = {p: p.read_bytes() for p in cls.folder.rglob("*") if p.is_file()}
        cls.srv = helpers.Server(cls.repo, cls.tmp, COV_USER="triage-bot", COV_AUTH_KEY="k3y-Secret-Value-0123456789",
                                 MY_KEY="other-Secret-Value-9876543210")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        for path in list(self.folder.rglob("*")):
            if path.is_file() and path not in self.files:
                path.unlink()
        for path, data in self.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def state(self) -> dict:
        status, data = self.srv.get("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"], data)
        return data

    def config_text(self) -> str:
        return (self.folder / "config.yaml").read_text(encoding="utf-8")

    def test_the_screen_shows_the_files_and_the_vcs_cannot_change(self) -> None:
        data = self.state()
        self.assertEqual(data["vcs"], "git")
        self.assertEqual(data["config"]["coverity"]["url"], "https://coverity.example.com:8443")
        self.assertEqual(data["config"]["parallel"], 2)
        self.assertEqual(data["config"]["max_items"], 100)  # not in the file: the default
        self.assertEqual(data["filters"][0]["file"], "untriaged.yaml")
        self.assertEqual(data["filters"][0]["data"]["streams"], ["app-main"])
        self.assertIn("調査で使う事実", data["knowledge"]["text"])
        config = copy.deepcopy(data["config"])
        config["vcs"]["type"] = "svn"
        answer = self.srv.post("/api/save", {"config": config})[1]
        self.assertFalse(answer["ok"])
        self.assertTrue(any("git" in p for p in answer["problems"]))

    def test_key_values_never_reach_the_page(self) -> None:
        status, _, body = self.srv.request("GET", "/api/state")
        self.assertNotIn(b"k3y-Secret-Value", body)
        data = self.state()
        self.assertEqual(data["credentials"]["user"], "triage-bot")
        self.assertTrue(data["credentials"]["key_set"])
        for user_env in ("COV_AUTH_KEY", "MY_KEY", "COVERITY_PASSPHRASE"):
            status, _, body = self.srv.request("GET", f"/api/credentials?user_env={user_env}&key_env=MY_KEY")
            self.assertEqual(status, 200)
            self.assertNotIn(b"Secret-Value", body)
        status, credentials = self.srv.get("/api/credentials?user_env=COV_AUTH_KEY&key_env=MY_KEY")
        self.assertTrue(credentials["user_hidden"])

    @unittest.skipIf(sys.platform == "win32", "on Windows the credentials can be saved")
    def test_credentials_cannot_be_saved_off_windows(self) -> None:
        self.assertFalse(self.state()["credentials"]["can_save"])
        answer = self.srv.post("/api/save", {"credentials": [{"kind": "key", "env": "COV_AUTH_KEY", "value": "abc"}]})[1]
        self.assertFalse(answer["ok"])
        self.assertTrue(any("Windows" in p for p in answer["problems"]))

    def test_user_and_key_need_different_variables(self) -> None:
        config = self.state()["config"]
        config["coverity"]["user_env"] = config["coverity"]["key_env"]
        answer = self.srv.post("/api/save", {"config": config})[1]
        self.assertFalse(answer["ok"])
        self.assertTrue(any("別の名前" in p for p in answer["problems"]))
        user = self.srv.post("/api/save", {"credentials": [{"kind": "user", "env": "COV_AUTH_KEY", "value": "me"}]})[1]
        self.assertFalse(user["ok"])

    def test_saving_changes_only_the_changed_lines_and_keeps_comments(self) -> None:
        before = self.config_text()
        config = self.state()["config"]
        config["parallel"] = 4
        config["coverity"]["url"] = "https://new.example.com:8443/"
        answer = self.srv.post("/api/save", {"config": config})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["comments"], "kept")
        after = self.config_text()
        changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(changed, [("  url: https://coverity.example.com:8443   # 社内のサーバ",
                                    "  url: https://new.example.com:8443   # 社内のサーバ"),
                                   ("parallel: 2", "parallel: 4")])
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))  # nothing added: defaults stay out

    def test_a_bom_and_crlf_line_breaks_are_kept(self) -> None:
        path = self.folder / "config.yaml"
        path.write_bytes(b"\xef\xbb\xbf" + self.config_text().replace("\n", "\r\n").encode("utf-8"))
        config = self.state()["config"]
        config["parallel"] = 6
        self.assertTrue(self.srv.post("/api/save", {"config": config})[1]["ok"])
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf# "))
        self.assertIn(b"parallel: 6\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_a_key_not_in_the_file_is_added_only_when_changed_and_unknown_keys_stay(self) -> None:
        path = self.folder / "config.yaml"
        path.write_text(self.config_text() + "team_note: そのまま残す   # 知らない項目\n", encoding="utf-8")
        config = self.state()["config"]
        self.assertIn("team_note", self.state()["config_unknown"])
        config["max_items"] = 50
        self.assertTrue(self.srv.post("/api/save", {"config": config})[1]["ok"])
        text = self.config_text()
        self.assertIn("max_items: 50", text)
        self.assertIn("team_note: そのまま残す   # 知らない項目", text)
        self.assertNotIn("options:", text)

    def test_wrong_values_are_told_and_nothing_is_written(self) -> None:
        before = self.config_text()
        base = self.state()["config"]
        cases = [("coverity", "url", "ftp://x"), (None, "parallel", 20), (None, "max_items", 0),
                 ("agents", "max_rework", 5), (None, "output_dir", str(self.repo / "out")), ("coverity", "user_env", "A-B")]
        for section, key, value in cases:
            config = copy.deepcopy(base)
            (config[section] if section else config)[key] = value
            answer = self.srv.post("/api/save", {"config": config})[1]
            self.assertFalse(answer["ok"], (key, value))
            self.assertTrue(answer["problems"])
        self.assertEqual(self.config_text(), before)

    def test_a_file_that_cannot_keep_comments_is_told_before_saving(self) -> None:
        path = self.folder / "config.yaml"
        path.write_text(self.config_text().replace("parallel: 2\n", "parallel: 2\nnote: |\n  一行目\n  二行目\n"), encoding="utf-8")
        config = self.state()["config"]
        config["parallel"] = 5
        check = self.srv.post("/api/save-check", {"config": config})[1]
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["comments"], "lost")
        self.assertIn(LOST, check["notice"])
        self.assertTrue(self.srv.post("/api/save", {"config": config})[1]["ok"])
        text = self.config_text()
        self.assertIn("parallel: 5", text)
        self.assertIn("# チームの設定（人が書いた注釈）", text)  # the comment lines at the left edge stay, at the top
        plain = self.srv.post("/api/save-check", {"config": self.state()["config"]})[1]
        self.assertEqual(plain["comments"], "kept")

    def test_filters_are_created_changed_and_at_least_one_stays(self) -> None:
        data = {"name": "MISRA", "project": "app", "streams": ["app-main"], "checkers": ["MISRA C-2012 Rule 10.*"],
                "impacts": ["High", "Unspecified"], "triage": {"classification": ["Unclassified"], "action": [], "status": ["New"]},
                "limit": 10}
        answer = self.srv.post("/api/save", {"filters": {"save": [{"file": "misra.yaml", "new": True, "data": data}]}})[1]
        self.assertTrue(answer["ok"], answer)
        written = (self.folder / "filters" / "misra.yaml").read_text(encoding="utf-8")
        self.assertIn("MISRA C-2012 Rule 10.*", written)
        self.assertNotIn("action", written)  # empty conditions are not written
        two = dict(data, streams=["a", "b"])
        self.assertFalse(self.srv.post("/api/save", {"filters": {"save": [{"file": "two.yaml", "new": True, "data": two}]}})[1]["ok"])
        self.assertFalse(self.srv.post("/api/save", {"filters": {"save": [{"file": "a/b.yaml", "new": True, "data": data}]}})[1]["ok"])
        self.assertFalse(self.srv.post("/api/save", {"filters": {"save": [{"file": "misra.yaml", "new": True, "data": data}]}})[1]["ok"])
        everything = {"filters": {"delete": ["untriaged.yaml", "misra.yaml"]}}
        refused = self.srv.post("/api/save", everything)[1]
        self.assertFalse(refused["ok"])
        self.assertTrue(any("1 つも" in p for p in refused["problems"]))
        self.assertTrue(self.srv.post("/api/save", {"filters": {"delete": ["misra.yaml"]}})[1]["ok"])
        self.assertFalse((self.folder / "filters" / "misra.yaml").exists())

    def test_editing_a_filter_keeps_its_comments(self) -> None:
        path = self.folder / "filters" / "untriaged.yaml"
        path.write_text("# 分類がまだのもの\n" + path.read_text(encoding="utf-8").replace("limit: 20", "limit: 20   # 上限"),
                        encoding="utf-8")
        data = self.state()["filters"][0]["data"]
        data["limit"] = 30
        self.assertTrue(self.srv.post("/api/save", {"filters": {"save": [{"file": "untriaged.yaml", "data": data}]}})[1]["ok"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 分類がまだのもの", text)
        self.assertIn("limit: 30   # 上限", text)

    def test_knowledge_and_no_grouping_are_saved(self) -> None:
        answer = self.srv.post("/api/save", {"knowledge": "# 知識\n- 新しい事実\n", "no_grouping": [20005, "20004", 20005]})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((self.folder / "knowledge.md").read_text(encoding="utf-8"), "# 知識\n- 新しい事実\n")
        self.assertEqual(self.state()["no_grouping"], [20004, 20005])
        self.assertFalse(self.srv.post("/api/save", {"no_grouping": ["abc"]})[1]["ok"])
        self.assertEqual(self.state()["no_grouping"], [20004, 20005])

    def test_unreadable_files_are_shown_with_defaults_and_a_hint(self) -> None:
        (self.folder / "config.yaml").write_bytes("coverity:\n  url: https://x\n# 日本語\n".encode("cp932"))
        (self.folder / "knowledge.md").write_bytes("# 知識\n".encode("cp932"))
        (self.folder / "filters" / "broken.yaml").write_text("streams: [a\n", encoding="utf-8")
        (self.folder / "filters" / "typed.yaml").write_text("streams: app-main\nlimit: many\n", encoding="utf-8")
        data = self.state()
        self.assertTrue(data["config_unreadable"])
        self.assertIn("UTF-8", data["config_error"])
        self.assertEqual(data["config"]["max_items"], 100)
        self.assertIn("UTF-8", data["knowledge"]["error"])
        self.assertEqual(data["knowledge"]["text"], "")
        filters = {f["file"]: f for f in data["filters"]}
        self.assertTrue(filters["broken.yaml"]["unreadable"])
        self.assertIn("YAML", filters["broken.yaml"]["error"])
        typed = filters["typed.yaml"]
        self.assertEqual(typed["data"]["streams"], [])
        self.assertEqual(set(typed["reset"]), {"streams", "limit"})
        self.assertIn("typed.yaml の streams", typed["error"])
        self.assertFalse(self.srv.post("/api/save", {"knowledge": "x"})[1]["ok"])  # what cannot be read is not replaced
        config = data["config"]
        config["coverity"]["url"] = "https://fixed.example.com"
        self.assertTrue(self.srv.post("/api/save", {"config": config})[1]["ok"])  # written whole, as the screen said
        self.assertIn("url: https://fixed.example.com", self.config_text())

    def test_a_value_of_the_wrong_kind_is_shown_as_the_default_and_listed(self) -> None:
        path = self.folder / "config.yaml"
        path.write_text(self.config_text().replace("  key_env: COV_AUTH_KEY\n", "  key_env: COV_AUTH_KEY\n  path_strip_prefixes: C:/build\n  user_env: 123\n"),
                        encoding="utf-8")
        data = self.state()
        self.assertEqual(data["config"]["coverity"]["path_strip_prefixes"], [])
        self.assertIn("coverity.path_strip_prefixes", data["config_reset"])
        self.assertIn("config.yaml の coverity.path_strip_prefixes", data["config_error"])

    def test_a_new_repository_gets_its_files_from_the_screen(self) -> None:
        root = helpers.temp_dir(self)
        repo, out = helpers.make_repo(root, config=False)
        srv = helpers.Server(repo, root)
        self.addCleanup(srv.close)
        data = srv.get("/api/state")[1]
        self.assertFalse(data["config_exists"])
        config = data["config"]
        config["coverity"]["url"] = "https://coverity.example.com"
        first = {"file": "untriaged.yaml", "new": True, "data": {"name": "未トリアージ", "project": "app", "streams": ["main"],
                                                              "checkers": [], "impacts": [], "triage": {"classification": ["Unclassified"]}, "limit": 20}}
        answer = srv.post("/api/save", {"config": config, "filters": {"save": [first]}, "knowledge": data["knowledge"]["text"]})[1]
        self.assertTrue(answer["ok"], answer)
        text = (repo / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "))
        self.assertIn("url: https://coverity.example.com", text)
        self.assertTrue((repo / ".coverity-triage" / "knowledge.md").is_file())


if __name__ == "__main__":
    unittest.main()
