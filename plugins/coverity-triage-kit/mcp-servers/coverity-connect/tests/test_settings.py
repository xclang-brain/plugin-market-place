"""Settings, credentials and coverity_check_connection."""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from unittest import mock

from support import MISSING, only_with_packages

load_tests = only_with_packages

if not MISSING:
    import support as cov
    from support import BASE, KEY, STREAM, USER, ServerCase

    from coverity_mcp import envvars, settings

    class CheckConnectionTest(ServerCase):

        def check(self, **kwargs):
            return self.tools.check_connection(repo_root=str(self.repo), **kwargs)

        def test_ok_without_the_key_value(self) -> None:
            out = self.check()
            self.assertTrue(out["ok"])
            text = json.dumps(out, ensure_ascii=False)
            self.assertNotIn(KEY, text)
            self.assertNotIn(USER, text)
            self.assertIn(f"設定あり（{len(KEY)} 文字）", out["key"])
            self.assertEqual((out["url"], out["api"], out["triage_store"]), (BASE, "auto", "Default Triage Store"))
            self.assertEqual(out["config"], str(self.repo / ".coverity-triage" / "config.yaml"))
            self.assertNotIn("warning", out)
            self.assertEqual((out["streams"], out["streams_total"]), ([STREAM, "libev-release"], 2))
            self.assertNotIn("streams_note", out)
            self.assertEqual(self.cov.requests, ["GET /api/v2/streams"])  # one request checks and lists

        def test_detailed_streams_have_project_and_triage_store(self) -> None:
            out = self.check(response_format="detailed")
            self.assertEqual(out["streams"], [
                {"name": STREAM, "project": "libev", "triage_store": "Default Triage Store"},
                {"name": "libev-release", "project": "libev", "triage_store": "Release Triage Store"}])

        def test_many_streams_are_capped_with_the_total(self) -> None:
            self.cov.streams = {f"s{n:03}": "Default Triage Store" for n in range(150)}
            out = self.check()
            self.assertEqual((len(out["streams"]), out["streams"][0], out["streams_total"]), (100, "s000", 150))
            self.assertIn("150", out["streams_note"])

        def test_the_stream_list_is_kept_for_the_lookups(self) -> None:
            self.check()
            self.tools.get_issue_details(repo_root=str(self.repo), cids=[10001])
            self.assertEqual(self.cov.requests.count("GET /api/v2/streams"), 1)

        def test_an_unexpected_stream_list_still_checks_the_connection(self) -> None:
            self.cov.rest_override = lambda request: cov.json_response(200, [{"name": "x"}])
            out = self.check()
            self.assertTrue(out["ok"])
            self.assertNotIn("streams", out)
            self.assertIn("想定と違います", out["streams_error"])

        def test_failures_are_reported_not_raised(self) -> None:
            cases = [(cov.rest_401, ["401"]), (cov.redirect_302, [cov.LOGIN]), (cov.html_200, ["JSON"]),
                     (lambda: cov.json_response(403, {"code": 1003, "message": "Access denied"}),
                      ["403", "Access Web services"])]
            for answer, words in cases:
                with self.subTest(words=words):
                    self.cov.rest_override = lambda request, answer=answer: answer()
                    out = self.check()
                    self.assertFalse(out["ok"])
                    for word in words:
                        self.assertIn(word, out["result"])
            self.cov.rest_override = lambda request: cov.rest_401()
            self.assertNotIn("Access Web services", self.check()["result"])

        def test_http_warning_and_unknown_keys(self) -> None:
            self.write_config("  url: http://coverity.example.com:8080\n  user_env: COVTEST_USER\n"
                              "  key_env: COVTEST_KEY\n  triage_stor: typo\n  project: app\n")
            out = self.check()
            self.assertIn("http", out["warning"])
            self.assertEqual(out["unknown_keys"], ["project", "triage_stor"])
            self.assertIn("unknown_keys_note", out)

        def test_settings_errors_say_what_to_fix_without_other_tools(self) -> None:
            (self.repo / ".coverity-triage" / "config.yaml").unlink()
            out = self.check()
            self.assertFalse(out["ok"])
            self.assertIn(str(self.repo / ".coverity-triage" / "config.yaml"), out["result"])
            self.assertIn("coverity.url", out["result"])
            self.assertIn("COVERITY_URL", out["result"])
            message = self.assertFailure(self.tools.search_issues, repo_root=str(self.repo), filter={"streams": [STREAM]})
            self.assertIn("coverity.url", message)
            for word in ("/coverity-", "ct.py", "skill", "スキル"):
                self.assertNotIn(word, out["result"] + message)

        def test_missing_credentials_name_the_variables(self) -> None:
            self.write_config(f"  url: {BASE}\n  user_env: COVTEST_NONE_USER\n  key_env: COVTEST_NONE_KEY\n")
            out = self.check()
            self.assertFalse(out["ok"])
            self.assertIn("COVTEST_NONE_USER", out["result"])
            message = self.assertFailure(self.tools.search_issues, repo_root=str(self.repo), filter={"streams": [STREAM]})
            self.assertIn("COVTEST_NONE_KEY", message)

        def test_unexpanded_values_count_as_unset(self) -> None:
            with mock.patch.dict(os.environ, {"COVTEST_USER": "${COVTEST_USER}", "COVTEST_KEY": ""}):
                out = self.check()
            self.assertFalse(out["ok"])
            self.assertIn("なし", out["user"])
            self.assertIn("なし", out["key"])

        def test_bad_values_are_explained(self) -> None:
            cases = [("  url: coverity.example.com\n", "https://"),
                     (f"  url: {BASE}\n  user_env: SAME\n  key_env: SAME\n", "同じ名前"),
                     (f"  url: {BASE}\n  api: fake\n", "fake_data"),
                     (f"  url: {BASE}\n  api: real\n", "api"),
                     ("  user_env: X\n", "url")]
            for text, word in cases:
                with self.subTest(text=text):
                    self.write_config(text)
                    out = self.check()
                    self.assertFalse(out["ok"])
                    self.assertIn(word, out["result"])

    class EnvModeTest(ServerCase):

        def test_without_repo_root_the_environment_is_read(self) -> None:
            env = {"COVERITY_URL": BASE + "/", "COVERITY_TRIAGE_STORE": "Env Store", "COV_USER": USER, "COV_AUTH_KEY": KEY}
            with mock.patch.dict(os.environ, env):
                out = self.tools.check_connection()
                self.assertTrue(out["ok"], out)
                self.assertEqual((out["config"], out["url"], out["triage_store"]), ("環境変数", BASE, "Env Store"))
                found = self.tools.search_issues(filter={"streams": [STREAM]})
                self.assertEqual(found["count"], 2)

        def test_without_repo_root_and_url_the_message_says_both_ways(self) -> None:
            with mock.patch.dict(os.environ, {"COVERITY_URL": "${COVERITY_URL}"}):
                out = self.tools.check_connection()
            self.assertFalse(out["ok"])
            self.assertIn("repo_root", out["result"])
            self.assertIn("COVERITY_URL", out["result"])

        def test_relative_fake_data_in_the_environment_is_refused(self) -> None:
            with mock.patch.dict(os.environ, {"COVERITY_API": "fake", "COVERITY_FAKE_DATA": "fake.yaml"}):
                out = self.tools.check_connection()
            self.assertFalse(out["ok"])
            self.assertIn("絶対パス", out["result"])

    class EnvVarsTest(unittest.TestCase):

        def test_empty_and_unexpanded_are_unset(self) -> None:
            with mock.patch.dict(os.environ, {"A_SET": "v", "A_EMPTY": "", "A_RAW": "${A_RAW}", "A_SPACE": "  "}):
                self.assertEqual(envvars.get_env("A_SET"), "v")
                for name in ("A_EMPTY", "A_RAW", "A_SPACE", "A_MISSING"):
                    self.assertIsNone(envvars.get_env(name))

        def test_windows_reads_the_user_variables(self) -> None:
            values = {"COV_USER": ("alice", 1), "COV_AUTH_KEY": ("%KEYDIR%-k", 2), "RAW": ("${RAW}", 1)}

            class Key:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            def query(key, name):
                if name not in values:
                    raise FileNotFoundError(name)
                return values[name]

            fake = types.SimpleNamespace(HKEY_CURRENT_USER=object(), REG_EXPAND_SZ=2, OpenKey=lambda root, path: Key(),
                                         QueryValueEx=query, ExpandEnvironmentStrings=lambda s: s.replace("%KEYDIR%", "x"))
            env = {k: v for k, v in os.environ.items() if k not in values}
            env["COV_USER"] = "${COV_USER}"  # not expanded by the client: read the saved one
            with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"winreg": fake}), \
                    mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(envvars.get_env("COV_USER"), "alice")
                self.assertEqual(envvars.get_env("COV_AUTH_KEY"), "x-k")
                self.assertIsNone(envvars.get_env("RAW"))
                self.assertIsNone(envvars.get_env("NOT_THERE"))

        def test_drop_unexpanded_removes_literal_placeholders(self) -> None:
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "${HTTPS_PROXY}", "NO_PROXY": "localhost"}):
                self.assertIn("HTTPS_PROXY", envvars.drop_unexpanded())
                self.assertNotIn("HTTPS_PROXY", os.environ)
                self.assertEqual(os.environ["NO_PROXY"], "localhost")

        def test_masker_hides_both_values(self) -> None:
            with mock.patch.dict(os.environ, {"MU": "someone", "MK": "secret-key-1"}):
                masker = envvars.Masker(["MU", "MK"])
                self.assertEqual(masker.text("someone used secret-key-1"), "**** used ****")
                self.assertEqual(masker.data({"a": ["secret-key-1"]}), {"a": ["****"]})

    class FilterFileTest(unittest.TestCase):

        def test_yaml_filter_of_the_settings_folder(self) -> None:
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "untriaged.yaml"
                path.write_text("name: 分類がまだの警告\nstreams: [app-main]\ncheckers: [NULL_RETURNS, \"MISRA C-2012 Rule 10.*\"]\n"
                                "impacts: [high, Medium]\ntriage:\n  classification: [unclassified]\n  status: [New, Triaged]\n"
                                "limit: 20\n", encoding="utf-8")
                spec, raw, skipped = settings.load_filter(path)
            self.assertEqual(spec.impacts, ["High", "Medium"])
            self.assertEqual(spec.triage.classification, ["Unclassified"])
            self.assertEqual((spec.limit, spec.streams, skipped), (20, ["app-main"], []))
            self.assertTrue(raw.startswith(b"name:"))


if __name__ == "__main__":
    unittest.main()
