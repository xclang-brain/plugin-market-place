"""Behaviour tests of scripts/show_path.py (run through uv, like the skill does).

Run from the skill folder:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "show_path.py"
STAR = "\u2605"


def tearDownModule() -> None:
    shutil.rmtree(HERE / "__pycache__", ignore_errors=True)


def run(args: list[str], cwd: Path, stdin: bytes | None = None, env: dict[str, str] | None = None
        ) -> tuple[int, dict, bytes]:
    full_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    if not env or "PYTHONIOENCODING" not in env:
        full_env.pop("PYTHONIOENCODING", None)
    proc = subprocess.run(
        ["uv", "run", "--native-tls", "--quiet", str(SCRIPT), *args],
        cwd=cwd, input=stdin, capture_output=True, env=full_env, timeout=120,
    )
    encoding = (env or {}).get("PYTHONIOENCODING", "utf-8")
    text = proc.stdout.decode(encoding)
    try:
        data = json.loads(text)
    except ValueError as exc:  # pragma: no cover - shows the raw output on failure
        raise AssertionError(f"stdout is not one JSON object: {text!r} stderr={proc.stderr!r}") from exc
    return proc.returncode, data, proc.stdout


def numbered(n: int, special: dict[int, str] | None = None) -> str:
    lines = [f"/* line {i} */" for i in range(1, n + 1)]
    for k, v in (special or {}).items():
        lines[k - 1] = v
    return "\n".join(lines) + "\n"


def snapshot(root: Path) -> dict[str, float]:
    return {p.relative_to(root).as_posix(): p.stat().st_mtime for p in root.rglob("*")}


class ShowPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="show-path-")).resolve()  # not an 8.3 short path on Windows
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "reader.c").write_text(numbered(80, {
            38: "int read_header(FILE *fp)",
            40: "\tline = fgets(buf, sizeof buf, fp);",
            42: "\tlen = strlen(line);",
        }), encoding="utf-8", newline="\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def issue_file(self, data: dict, name: str = "issue-20001.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def base_issue(self, file: str = "src/reader.c") -> dict:
        return {
            "fetched_at": "2026-11-08T10:00:00",
            "issue": {"cid": 20001, "checker": "NULL_RETURNS", "file": file, "function": "read_header",
                      "line": 42, "impact": "High", "stream": "app-main"},
            "events": [
                {"file": file, "line": 40, "tag": "returned_null", "description": "fgets は NULL を返しうる", "main": False},
                {"file": file, "line": 42, "tag": "dereference", "description": "NULL を参照する", "main": True},
            ],
            "checker_description": "NULL を返しうる関数の戻り値を確かめずに参照している",
        }

    # ------------------------------------------------------------------ issue file

    def test_issue_file_marks_main_event_and_shows_context(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(self.base_issue()))],
                            self.tmp)
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"])
        issue = data["issues"][0]
        self.assertEqual(issue["cid"], 20001)
        self.assertEqual([e["main"] for e in issue["events"]], [False, True])
        text = data["text"]
        self.assertIn("CID 20001 — NULL_RETURNS（Impact High）", text)
        self.assertIn(f"{STAR} 2. src/reader.c:42 dereference — NULL を参照する", text)
        self.assertIn(f"{STAR}2 42 | \tlen = strlen(line);", text)
        # one merged window: 40-20 .. 42+20
        self.assertIn("src/reader.c（20〜62 行。経路 1・★2）", text)
        self.assertNotIn(" 19 | ", text)
        self.assertIn(" 20 | ", text)
        self.assertIn(" 62 | ", text)
        self.assertNotIn(" 63 | ", text)
        self.assertEqual(data["drift_hints"], [])
        self.assertEqual(data["files"][0]["encoding"], "utf-8")
        self.assertEqual(data["files"][0]["newline"], "LF")

    def test_context_option_and_window_clamped_at_file_start(self) -> None:
        issue = self.base_issue()
        issue["events"] = [{"file": "src/reader.c", "line": 2, "tag": "t", "description": "d", "main": True}]
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue)),
                             "--context", "3"], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertIn("src/reader.c（1〜5 行。経路 ★1）", data["text"])

    def test_far_apart_events_give_separate_windows(self) -> None:
        issue = self.base_issue()
        issue["events"][0]["line"] = 5
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue)),
                             "--context", "2"], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertIn("src/reader.c（3〜7 行。経路 1）", data["text"])
        self.assertIn("src/reader.c（40〜44 行。経路 ★2）", data["text"])

    def test_main_defaults_to_last_event_when_not_marked(self) -> None:
        issue = self.base_issue()
        for e in issue["events"]:
            e.pop("main")
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual([e["main"] for e in data["issues"][0]["events"]], [False, True])

    def test_group_with_two_issue_files(self) -> None:
        second = self.base_issue()
        second["issue"]["cid"] = 20002
        code, data, _ = run(["show", "--repo", str(self.repo),
                             "--issue-file", str(self.issue_file(self.base_issue())),
                             "--issue-file", str(self.issue_file(second, "issue-20002.json"))], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual([i["cid"] for i in data["issues"]], [20001, 20002])
        self.assertIn("CID 20002", data["text"])

    def test_issue_without_events_uses_the_warning_location(self) -> None:
        issue = self.base_issue()
        issue["events"] = []
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))], self.tmp)
        self.assertEqual(code, 0, data)
        events = data["issues"][0]["events"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["main"])
        self.assertEqual(events[0]["line"], 42)

    # ------------------------------------------------------------------ path mapping

    def test_strip_prefix_from_project_settings(self) -> None:
        (self.repo / ".coverity-triage").mkdir()
        (self.repo / ".coverity-triage" / "config.yaml").write_text(
            "coverity:\n  url: https://cov.example\n  path_strip_prefixes:\n    - /build/ws/app\n", encoding="utf-8")
        issue = self.base_issue("/build/ws/app/src/reader.c")
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))], self.tmp)
        self.assertEqual(code, 0, data)
        ev = data["issues"][0]["events"][1]
        self.assertEqual(ev["file"], "src/reader.c")
        self.assertEqual(ev["reported_file"], "/build/ws/app/src/reader.c")
        self.assertEqual(data["issues"][0]["file"], "src/reader.c")
        self.assertEqual(data["notes"], [])

    def test_strip_prefix_argument_and_windows_path(self) -> None:
        issue = self.base_issue("C:\\jenkins\\ws\\src\\reader.c")
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue)),
                             "--strip-prefix", "c:\\Jenkins\\ws"], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["issues"][0]["events"][0]["file"], "src/reader.c")

    def test_suffix_match_adds_a_note(self) -> None:
        issue = self.base_issue("/somewhere/else/project/src/reader.c")
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["issues"][0]["events"][0]["file"], "src/reader.c")
        self.assertTrue(any("末尾が合うファイル" in n for n in data["notes"]), data["notes"])
        self.assertIn("注意:", data["text"])

    def test_absolute_path_inside_repo(self) -> None:
        issue = self.base_issue(str(self.repo / "src" / "reader.c"))
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["issues"][0]["events"][0]["file"], "src/reader.c")
        self.assertEqual(data["notes"], [])

    def test_ambiguous_suffix_is_unmapped_with_candidates(self) -> None:
        (self.repo / "lib").mkdir()
        (self.repo / "lib" / "reader.c").write_text("int x;\n", encoding="utf-8", newline="\n")
        issue = self.base_issue("/other/reader.c")
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertTrue(any("候補が複数" in n for n in data["notes"]), data["notes"])
        self.assertTrue(any("ありません" in d for d in data["drift_hints"]), data["drift_hints"])

    # ------------------------------------------------------------------ drift hints

    def test_drift_hints_for_short_file_missing_function_and_missing_file(self) -> None:
        issue = self.base_issue()
        issue["issue"]["function"] = "Reader::parse_header(int)"
        issue["events"].append({"file": "src/reader.c", "line": 500, "tag": "x", "description": "y", "main": False})
        issue["events"].append({"file": "src/gone.c", "line": 3, "tag": "x", "description": "y", "main": False})
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))], self.tmp)
        self.assertEqual(code, 0, data)
        hints = "\n".join(data["drift_hints"])
        self.assertIn("src/reader.c:500 はファイルの行数（80 行）を超えています", hints)
        self.assertIn("関数 Reader::parse_header(int) が src/reader.c に見つかりません", hints)
        self.assertIn("src/gone.c がリポジトリにありません", hints)
        self.assertIn("ずれの手がかり", data["text"])

    # ------------------------------------------------------------------ arguments and stdin

    def test_location_only_from_arguments(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo), "--cid", "31", "--checker", "UNINIT",
                             "--file", "src/reader.c", "--line", "42", "--context", "1"], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertIn("CID 31 — UNINIT", data["text"])
        self.assertIn(f"{STAR}1 42 |", data["text"])

    def test_events_from_arguments_with_main_number(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo),
                             "--event", "src/reader.c:40:returned_null:fgets: NULL もある",
                             "--event", "src/reader.c:42:dereference",
                             "--main", "2", "--context", "0"], self.tmp)
        self.assertEqual(code, 0, data)
        events = data["issues"][0]["events"]
        self.assertEqual(events[0]["description"], "fgets: NULL もある")
        self.assertEqual(events[1]["tag"], "dereference")
        self.assertEqual([e["main"] for e in events], [False, True])

    def test_events_from_arguments_main_by_line(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo), "--line", "40",
                             "--event", "src/reader.c:40:a", "--event", "src/reader.c:42:b"], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual([e["main"] for e in data["issues"][0]["events"]], [True, False])

    def test_windows_drive_path_in_event_spec(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo), "--strip-prefix", "C:/ws",
                             "--event", "C:\\ws\\src\\reader.c:42:deref:説明"], self.tmp)
        self.assertEqual(code, 0, data)
        ev = data["issues"][0]["events"][0]
        self.assertEqual((ev["file"], ev["line"], ev["tag"]), ("src/reader.c", 42, "deref"))

    def test_stdin_json(self) -> None:
        payload = json.dumps({"issues": [self.base_issue()]}, ensure_ascii=False).encode("utf-8")
        code, data, _ = run(["show", "--repo", str(self.repo), "--stdin"], self.tmp, stdin=payload)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["issues"][0]["cid"], 20001)

    # ------------------------------------------------------------------ files and encodings

    def test_cp932_and_crlf_source(self) -> None:
        (self.repo / "src" / "sjis.c").write_bytes("int a;\r\n/* 日本語の注釈 */\r\nint b;\r\n".encode("cp932"))
        code, data, _ = run(["show", "--repo", str(self.repo), "--file", "src/sjis.c", "--line", "2"], self.tmp)
        self.assertEqual(code, 0, data)
        info = data["files"][0]
        self.assertEqual(info["encoding"], "cp932（Shift_JIS）")
        self.assertEqual(info["newline"], "CRLF")
        self.assertIn("/* 日本語の注釈 */", data["text"])
        self.assertNotIn("\r", data["text"])

    def test_utf8_bom_source(self) -> None:
        (self.repo / "src" / "bom.c").write_bytes(b"\xef\xbb\xbfint a;\nint b;\n")
        code, data, _ = run(["show", "--repo", str(self.repo), "--file", "src/bom.c", "--line", "1"], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["files"][0]["encoding"], "utf-8（BOM あり）")
        self.assertIn(" 1 | int a;", data["text"])

    def test_output_in_console_code_page_escapes_unencodable(self) -> None:
        issue = self.base_issue()
        issue["events"][1]["description"] = "参照する \U0001F600"
        code, data, raw = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))],
                              self.tmp, env={"PYTHONIOENCODING": "cp932"})
        self.assertEqual(code, 0, data)
        self.assertIn(b"\\ud83d\\ude00", raw)
        self.assertIn("参照する".encode("cp932"), raw)
        self.assertEqual(data["issues"][0]["events"][1]["description"], "参照する \U0001F600")

    def test_creates_no_files(self) -> None:
        path = self.issue_file(self.base_issue())
        before = snapshot(self.tmp)
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(path)], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual(snapshot(self.tmp), before)

    def test_long_output_narrows_context_and_keeps_every_event(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo), "--event", "src/reader.c:5:a",
                             "--event", "src/reader.c:70:b", "--max-lines", "30"], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["context"], 5)
        self.assertFalse(data["truncated"])
        self.assertIn("src/reader.c（1〜10 行。経路 1）", data["text"])
        self.assertIn("src/reader.c（65〜75 行。経路 ★2）", data["text"])
        self.assertTrue(any("20 行から 5 行に減らしました" in n for n in data["notes"]), data["notes"])

    def test_max_lines_truncates(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(self.base_issue())),
                             "--max-lines", "5"], self.tmp)
        self.assertEqual(code, 0, data)
        self.assertTrue(data["truncated"])
        self.assertIn("--context を小さく", data["text"])
        code_lines = [ln for ln in data["text"].splitlines() if " | " in ln]
        self.assertEqual(len(code_lines), 5)

    # ------------------------------------------------------------------ failures

    def assertFailure(self, code: int, data: dict, needle: str) -> None:
        self.assertEqual(code, 1, data)
        self.assertFalse(data["ok"])
        self.assertIn(needle, data["error"])
        self.assertIsInstance(data["problems"], list)

    # ------------------------------------------------------------------ files written by the Coverity tool

    def test_issue_file_written_by_the_coverity_tool(self) -> None:
        """tests/data/details/issue-20001.json has the exact shape (design 4.4) the tool that fetches warning
        paths writes into its output_dir: Coverity's paths as they are, the keys without a value left out
        (here an event without a line), UTF-8 with an indent of 2."""
        path = HERE / "data" / "details" / "issue-20001.json"
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(path)], self.tmp)
        self.assertEqual(code, 0, data)
        issue = data["issues"][0]
        self.assertEqual((issue["cid"], issue["checker"], issue["stream"], issue["impact"]),
                         (20001, "NULL_RETURNS", "app-main", "High"))
        self.assertEqual((issue["file"], issue["reported_file"]), ("src/reader.c", "/work/build/app/src/reader.c"))
        self.assertEqual([(e["n"], e["line"], e["main"]) for e in issue["events"]],
                         [(1, 40, False), (2, None, False), (3, 42, True)])
        self.assertTrue(all(e["file"] == "src/reader.c" for e in issue["events"]))
        text = data["text"]
        self.assertIn(f"{STAR} 3. src/reader.c:42 dereference", text)
        self.assertIn("2. src/reader.c（行の番号なし） path", text)
        self.assertIn(f"{STAR}3 42 | \tlen = strlen(line);", text)
        self.assertIn("チェッカーの説明: fgets の戻り値を確かめずに使っている。", text)
        self.assertEqual(data["drift_hints"], [])
        self.assertTrue(data["notes"])  # the build path was matched by its end: said so

    def test_failure_file_written_by_the_coverity_tool(self) -> None:
        path = HERE / "data" / "details" / "issue-20002.json"
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(path)], self.tmp)
        self.assertFailure(code, data, "警告経路は取れていません")
        self.assertIn("ストリーム app-main にありません", data["error"])

    def test_failed_fetch_file(self) -> None:
        path = self.issue_file({"cid": 20001, "stream": "app-main", "fetched_at": "x", "error": "SOAP の失敗"})
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(path)], self.tmp)
        self.assertFailure(code, data, "警告経路は取れていません")
        self.assertIn("SOAP の失敗", data["error"])

    def test_no_input(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo)], self.tmp)
        self.assertFailure(code, data, "警告の情報がありません")

    def test_broken_json(self) -> None:
        path = self.tmp / "issue-1.json"
        path.write_text("{not json", encoding="utf-8")
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(path)], self.tmp)
        self.assertFailure(code, data, "JSON として読めません")

    def test_missing_issue_file_and_repo(self) -> None:
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.tmp / "nope.json")], self.tmp)
        self.assertFailure(code, data, "警告経路のファイルがありません")
        code, data, _ = run(["show", "--repo", str(self.tmp / "no-repo"), "--file", "a.c", "--line", "1"], self.tmp)
        self.assertFailure(code, data, "コードを読むフォルダがありません")

    def test_bad_events(self) -> None:
        issue = self.base_issue()
        issue["events"][0]["line"] = "abc"
        code, data, _ = run(["show", "--repo", str(self.repo), "--issue-file", str(self.issue_file(issue))], self.tmp)
        self.assertFailure(code, data, "形が正しくありません")
        self.assertIn("events の 1 番目の line が数ではありません", data["problems"])

    def test_bad_arguments_give_json_not_traceback(self) -> None:
        code, data, _ = run(["show", "--unknown"], self.tmp)
        self.assertFailure(code, data, "引数が正しくありません")
        code, data, _ = run(["show", "--repo", str(self.repo), "--event", "no-line-number"], self.tmp)
        self.assertFailure(code, data, "警告経路の引数が正しくありません")
        code, data, _ = run(["show", "--repo", str(self.repo), "--event", "src/reader.c:1", "--main", "3"], self.tmp)
        self.assertFailure(code, data, "--main 3")
        code, data, _ = run(["show", "--repo", str(self.repo), "--file", "a.c", "--line", "1", "--context", "999"],
                            self.tmp)
        self.assertFailure(code, data, "--context")


if __name__ == "__main__":
    unittest.main()
