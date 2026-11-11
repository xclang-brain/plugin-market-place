"""Tests of apply.py: what a person loses when the step after the OK breaks.

The result folder is written here file by file, as the triage commands, the screen and the write tool leave
it. The repositories are real: git with a bare repository as origin, and svn; their folder has a space and
a Japanese name. Run in the skill folder: python -B -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "apply.py"
RUN = "20261110-153000"
BRANCH = f"coverity-fix/{RUN}"
A = b"/* a */\nint a(int *p)\n{\n    int x = 1;\n    int y = 2;\n    (void)x;\n    (void)y;\n    return *p;\n}\n"
JP = "src/日本語 名前.c"  # its content is Shift_JIS with CRLF
SJIS = "// 日本語のコメント\r\nint s(void)\r\n{\r\n    return 0;\r\n}\r\n".encode("cp932")
B = b"int b(void)\n{\n    return 0;\n}\n"
FILES = {"src/a.c": A, "src/b.c": B, "src/c.c": B.replace(b"b", b"c"), JP: SJIS,
         ".coverity-triage/config.yaml": b"coverity:\n  url: https://coverity.example.co.jp:8443\n  stream: app-main\n"}
FIX_A = {"src/a.c": (b"return *p;", b"return p ? *p : 0;")}  # the edits of a fix: {file: (old, new)}
FIX_BC = {"src/b.c": (b"return 0;", b"return 1;"), "src/c.c": (b"return 0;", b"return 1;")}
FIX_S = {JP: (b"return 0;", "return 1; // 修正".encode("cp932"))}
HEAD = "| CID | 判定 | 確信度 | 修正コード | 逸脱コメント | 選択 | 備考 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
ASK = "よろしければ「OK」と入力してください。変更するときは「20003 は逸脱、20007 は何もしない、ほかは OK」のように入力してください"

BUG = "取り込んだ修正は、Coverity に「Bug／Fix Required」として登録します。"
CHANGED = "一覧を表示した後に、結果または選択が変更されています。もう一度、一覧を確認してください"
REFUSED = ("取り込み先のコードに適用できませんでした。先に取り込んだ修正と同じ箇所を変更しているときは、"
           "その修正を{}した後に、もう一度処置してください")


def interpreter() -> list:
    """apply.py needs PyYAML: this Python when it has it, else uv as in real use."""
    try:
        import yaml  # noqa: F401
        return [sys.executable]
    except ImportError:
        if not shutil.which("uv"):
            raise unittest.SkipTest("PyYAML のある Python も uv もないため、apply.py を実行できません") from None
        return [shutil.which("uv"), "run", "--native-tls", "--quiet"]


def remove_tree(path: Path) -> None:
    def writable(func, target, _exc):  # the pristine files of svn are read-only
        os.chmod(target, stat.S_IRWXU if os.path.isdir(target) else stat.S_IREAD | stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, **({"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}))


def row(out: dict, cid) -> str:
    """The table row of a CID, without the CID cell."""
    return next(line.split(" | ", 1)[1] for line in out["table"].splitlines() if line.startswith(f"| {cid}"))


def fix_row(cid, choice: str, remark: str, level: str = "高") -> str:
    return f"本物のバグ | {level} | src/a.c | 本物のバグ。CID {cid} の理由。（Bug／Ignore／Major） | {choice} | {remark} |"


def keep_row(cid, choice: str, remark: str, comment: str = "") -> str:
    comment = comment or f"誤検知。CID {cid} の理由。"
    return f"誤検知 | 高 | なし | {comment}（False Positive／Ignore／Unspecified） | {choice} | {remark} |"


def folder_name(kind: str, japanese: str, plain: str) -> str:
    """svn on Windows reads its arguments in the ANSI code page of the PC; where that cannot hold Japanese
    (a PC that is not set to Japanese), the svn tests use a plain name."""
    if kind == "svn" and os.name == "nt":
        try:
            japanese.encode("mbcs")
        except UnicodeEncodeError:
            return plain
    return japanese


class Case(unittest.TestCase):
    """A repository with one commit and the settings file, and an empty result folder."""

    kind = "git"
    maxDiff = None

    def setUp(self):
        if self.kind == "svn" and not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn または svnadmin コマンドがありません")
        self.top = Path(tempfile.mkdtemp(prefix=folder_name(self.kind, "適用 テスト-", "apply test-"))).resolve()
        self.addCleanup(remove_tree, self.top)
        (self.top / "gitconfig").write_bytes(b"")  # the commits must not depend on the settings of this PC
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": str(self.top / "gitconfig")}
        if os.name == "posix" and "UTF-8" not in "".join(os.environ.get(k, "") for k in ("LC_ALL", "LC_CTYPE", "LANG")):
            self.env["LC_ALL"] = "C.UTF-8"  # svn needs it for the Japanese folder name
        self.repo, self.origin = self.top / "repo", self.top / "origin.git"
        if self.kind == "git":
            self.sh("git", "init", "-q", "--bare", "-b", "main", self.origin, cwd=self.top)
            self.sh("git", "init", "-q", "-b", "main", self.repo, cwd=self.top)
            web = "https://github.example/acme/app.git"  # origin has the URL of a GitHub repository
            for key, value in (("user.name", "試験 太郎"), ("user.email", "taro@example.com"), ("core.autocrlf", "false"),
                               (f"url.{self.origin.as_posix()}.insteadOf", web)):
                self.sh("git", "config", key, value)
            self.sh("git", "remote", "add", "origin", web)
        else:
            self.sh("svnadmin", "create", self.top / "svnrepo", cwd=self.top)
            self.sh("svn", "checkout", "-q", (self.top / "svnrepo").as_uri(), self.repo, cwd=self.top)
        for name, data in FILES.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_bytes(data)
        if self.kind == "git":
            self.sh("git", "add", "-A")
            self.sh("git", "commit", "-q", "-m", "init")
            self.sh("git", "push", "-q", "origin", "main")
            self.sh("git", "fetch", "-q", "origin")
        else:
            self.sh("svn", "add", "-q", "--force", ".")
            self.sh("svn", "commit", "-q", "-m", "init")
            self.sh("svn", "update", "-q")
        self.run_dir = self.top / "coverity-triage-out" / RUN
        self.issues, self.patches = [], {}
        self.meta()

    def sh(self, *args, cwd=None) -> bytes:
        proc = subprocess.run([str(a) for a in args], cwd=str(cwd or self.repo), env=self.env, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout

    def put(self, name: str, data) -> Path:
        path = self.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8"))
        return path

    def get(self, name: str) -> dict:
        return json.loads((self.run_dir / name).read_text(encoding="utf-8"))

    def meta(self, **over) -> None:
        self.put("run.json", {"repo": self.repo.as_posix(), "vcs": self.kind, "base_revision": "", "stream": "app-main",
                              "base_branch": "main" if self.kind == "git" else "", "conditions": {}, "slots": 1,
                              "review": False, "all": True, **over})
        self.put("issues.json", {"stream": "app-main", "issues": self.issues})

    def patch(self, edits: dict) -> bytes:
        """fix.patch as the save command stores it: the diff that git / svn itself makes of the edits."""
        key = repr(edits)
        if key not in self.patches:
            for name, (old, new) in edits.items():
                (self.repo / name).write_bytes((self.repo / name).read_bytes().replace(old, new))
            if self.kind == "git":
                self.sh("git", "add", "-u")
                self.patches[key] = self.sh("git", "diff", "--cached", "--binary", "--full-index", "HEAD")
                self.sh("git", "reset", "-q", "--hard")
            else:
                self.patches[key] = self.sh("svn", "diff", "--internal-diff", "--force")
                self.sh("svn", "revert", "-q", "-R", ".")
        return self.patches[key]

    def add(self, cid: int, edits: dict | None = None, status: str | None = "done", **state) -> None:
        """One CID of the list. A finished one has its result: with a fix (recommended), or without one."""
        self.issues.append({"cid": cid, "checker": "NULL_RETURNS", "file": "src/a.c", "line": 8, "function": "a",
                            "impact": "High"})
        self.put("issues.json", {"stream": "app-main", "issues": self.issues})
        if status is None:  # never handed out
            return
        self.put(f"cid/{cid}/state.json", {"status": status, "files": sorted(edits or {}), "confidence": "high", **state})
        if status != "done":
            return
        result = {"cid": cid, "summary": "概要", "evidence": [{"file": "src/a.c", "line": 8, "note": "根拠"}],
                  "confidence": "high"}
        if edits:
            self.put(f"cid/{cid}/fix.patch", self.patch(edits))
            result.update(verdict="true_bug", recommendation="fix", fix={"summary": f"CID {cid} の修正の概要"},
                          deviation={"comment": f"本物のバグ。CID {cid} の理由。", "classification": "Bug",
                                     "action": "Ignore", "severity": "Major"})
        else:
            result.update(verdict="false_positive", recommendation="deviation", no_fix_reason="理由",
                          deviation={"comment": f"誤検知。CID {cid} の理由。", "classification": "False Positive",
                                     "action": "Ignore", "severity": "Unspecified"})
        self.put(f"cid/{cid}/result.json", result)

    def script(self, *args) -> dict:
        """Run apply.py in the folder above the repository: it must exit 0 and print one JSON object."""
        proc = subprocess.run([*interpreter(), str(SCRIPT), *map(str, args)], capture_output=True, env=self.env,
                              cwd=str(self.top), timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8") + proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))

    def plan(self, *sets: str) -> dict:
        return self.script("plan", "--repo", "repo", *(arg for choice in sets for arg in ("--set", choice)))

    def go(self) -> dict:
        return self.script("apply", "--repo", "repo")

    def line(self, name: str, *extra: str) -> str:
        return " ".join([f'uv run --native-tls --quiet "{SCRIPT.as_posix()}" {name} --repo "{self.repo.as_posix()}"',
                         f'--run "{self.run_dir.as_posix()}"', *extra])

    def follow(self, line: str) -> dict:
        """Run a command line that a reply gave under next."""
        return self.script(*shlex.split(line.split('apply.py" ', 1)[1]))


class PlanTest(Case):
    def test_default_choice_of_every_kind_of_item_and_the_table(self):
        self.put("../20261109-090000/run.json", {"repo": self.repo.as_posix()})  # an older result folder
        self.put("../20261231-000000/run.json", {"repo": "/another/repository"})
        self.assertEqual(self.plan(), {"ok": False, "error": "処置済みの CID がありません", "next": []})
        other = self.script("plan", "--repo", "repo", "--run", "coverity-triage-out/20261231-000000")
        self.assertIn("このリポジトリの結果フォルダーではありません", other["error"])
        self.meta(review=True)
        self.add(20001, FIX_A, review="pass")
        self.add(20002, review="pass")
        self.add(20003, FIX_A, review="pass", confidence="low")
        self.add(20004, FIX_A, review="flag")
        self.add(20005, FIX_A, review="unverified")
        self.add(20006)  # not reviewed yet
        self.add(20007, FIX_A, review="pass")  # the build check failed
        self.add(20008, review="pass", members=[20009])
        self.add(20009, review="pass", merged_into=20008)  # same cause as 20008: no row of its own
        self.add(20010, status="failed", error="処置を2回開始しましたが、どちらも結果が保存されませんでした")
        self.add(20011, status=None)
        self.add(20012, status="rework")
        self.put("build.json", {"status": "done", "exit_code": 1, "cids": {"20001": "ok", "20007": "failed"}})
        out = self.plan()
        none = "何もしない"
        self.assertEqual(out["table"] + "\n", HEAD + "".join(f"| {cid} | {cells}\n" for cid, cells in (
            (20001, fix_row(20001, "修正を取り込む", "")), (20002, keep_row(20002, "逸脱コメントを登録する", "")),
            (20003, fix_row(20003, none, "確信度が低いため", "低")), (20004, fix_row(20004, none, "検証で要確認のため")),
            (20005, fix_row(20005, none, "未検証のため")), (20006, keep_row(20006, none, "未検証のため")),
            (20007, fix_row(20007, none, "ビルドに失敗したため")),
            ("20008（同じ原因: 20009）", keep_row(20008, "逸脱コメントを登録する", "")),
            (20010, "- | - | - | - | - | 処置に失敗 |"), (20011, "- | - | - | - | - | 未処置 |"),
            (20012, "- | - | - | - | - | 未処置 |"))))
        self.assertEqual(out["counts"], {"fix": 1, "deviation": 2, "none": 5, "done": 0, "failed": 1, "unfinished": 2,
                                         "left": 0})
        self.assertEqual(out["question"], "この内容で、修正の取り込み（1 件）、逸脱コメントの登録（2 件）を行います。" + BUG + ASK)
        self.assertEqual((out["ok"], out["vcs"], out["next"]), (True, "git", [self.line("apply")]))
        self.assertEqual((out["run_dir"], out["plan_file"], out["summary_file"]),
                         (self.run_dir.as_posix(), (self.run_dir / "apply-plan.json").as_posix(), None))
        plan = self.get("apply-plan.json")
        keep = {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified"}
        self.assertEqual((plan["set"], plan["fixes"], plan["none"]), ({}, [20001], [20003, 20004, 20005, 20006, 20007]))
        self.assertEqual(plan["deviations"], [{"cids": [20002], **keep, "comment": "誤検知。CID 20002 の理由。"},
                                              {"cids": [20008, 20009], **keep, "comment": "誤検知。CID 20008 の理由。"}])
        summary = self.put("summary.md", b"# Coverity\n")  # finish has written it
        self.assertEqual(self.plan()["summary_file"], summary.as_posix())
        # showing the list takes nothing in and registers nothing
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()),
                         ["apply-plan.json", "build.json", "cid", "issues.json", "run.json", "summary.md"])

    def test_the_sentence_in_the_chat_and_what_it_cannot_change(self):
        self.add(20001, FIX_A)
        self.add(20002)
        self.add(20003, FIX_A, confidence="low")
        self.add(20008, members=[20009])
        self.add(20009, merged_into=20008)
        self.add(20010, status="failed", error="失敗")
        self.add(20011, status="working")
        out = self.plan("20001=none", "20003=fix", "20008=none")
        self.assertEqual([row(out, cid) for cid in (20001, 20003)],
                         [fix_row(20001, "何もしない", "チャットで指定"), fix_row(20003, "修正を取り込む", "チャットで指定", "低")])
        self.assertEqual(out["counts"], {"fix": 1, "deviation": 1, "none": 2, "done": 0, "failed": 1, "unfinished": 1,
                                         "left": 0})
        chosen = {"20001": "none", "20003": "fix", "20008": "none"}
        self.assertEqual(self.get("apply-plan.json")["set"], chosen)
        refused = self.plan("20001=deviation", "20009=fix", "20010=fix", "20011=none", "20002=fix", "30000=none",
                            "20003=修正")
        self.assertEqual((refused["ok"], refused["next"]), (False, [self.line("plan", "--set 20001=deviation")]))
        self.assertEqual(refused["problems"], [
            "CID 20009 は CID 20008 に統合されています。20008 を指定してください",
            "CID 20010 は処置に失敗しているため、選択できません", "CID 20011 は未処置のため、選択できません",
            "CID 20002 には修正コードがないため、fix は指定できません", "CID 30000 は取得した警告の一覧にありません",
            "--set は <CID>=fix|deviation|none の形で指定してください（指定: 20003=修正）"])
        self.assertEqual(self.get("apply-plan.json")["set"], chosen, "a refused request must not replace the plan")
        # what an earlier OK has handled has no choice any more
        self.put("applied.json", {"vcs": "git", "branch": BRANCH, "base_branch": "main", "pushed": True,
                                  "fixes": {"20001": {"commit": "0" * 40, "files": ["src/a.c"], "at": "2026-11-10T16:00:00"}}})
        self.put("written.json", {"written": {"20001": {}, "20002": {}}})
        self.assertEqual(self.plan("20001=none", "20002=none")["problems"],
                         ["CID 20001 は取り込み済みです", "CID 20002 は登録済みです"])
        out = self.plan()
        self.assertEqual([row(out, cid) for cid in (20001, 20002)],
                         [fix_row(20001, "-", "取り込み済み"), keep_row(20002, "-", "登録済み")])
        self.assertEqual((out["counts"]["done"], out["counts"]["left"]), (2, 0))

    def test_a_choice_on_the_screen_counts_while_it_fits_the_result(self):
        self.add(20001, FIX_A)
        self.add(20002)
        self.add(20003, FIX_A)
        self.add(20004)
        self.add(20005, FIX_A, confidence="low")
        at = datetime.now().astimezone().isoformat()  # as the screen writes it
        self.put("decisions.json", {"decisions": {
            "20001": {"decision": "none", "at": at},
            "20002": {"decision": "deviation", "comment": "誤検知。画面で編集した文。", "at": at},
            "20003": {"decision": "deviation", "at": at},  # the result was saved again after this choice
            "20004": {"decision": "fix", "at": at}}})  # there is no fix code to take in
        later = time.time() + 10
        os.utime(self.run_dir / "cid" / "20003" / "state.json", (later, later))
        out = self.plan()
        self.assertEqual([row(out, cid) for cid in (20001, 20002, 20003, 20004)], [
            fix_row(20001, "何もしない", "画面で選択"),
            keep_row(20002, "逸脱コメントを登録する", "画面で選択", "誤検知。画面で編集した文。"),
            fix_row(20003, "修正を取り込む", ""), keep_row(20004, "逸脱コメントを登録する", "")])
        self.assertEqual([entry["comment"] for entry in self.get("apply-plan.json")["deviations"]],
                         ["誤検知。画面で編集した文。", "誤検知。CID 20004 の理由。"])
        # the sentence in the chat wins over the screen
        self.assertEqual(row(self.plan("20001=fix"), 20001), fix_row(20001, "修正を取り込む", "チャットで指定"))
        # nothing chosen: no question
        out = self.plan("20002=none", "20003=none", "20004=none")
        self.assertEqual((out.get("question"), out["next"], out["message"]), (
            None, [], "取り込む修正と、登録する逸脱コメントはありません。変更するときは「20003 は修正」のように入力してください"))


class Flow:
    """The step after the OK, the same on git and svn."""

    def test_fixes_are_taken_in_and_coverity_gets_each_entry_once(self):
        self.add(20001, FIX_A)
        self.add(20002)
        self.add(20003, FIX_BC)  # one of its two files no longer applies: see disturb
        self.add(20004, FIX_S, members=[20005])  # a CRLF file in Shift_JIS
        self.add(20005, merged_into=20004)
        self.disturb()
        self.plan()
        out = self.go()
        self.assertEqual((out["ok"], out["fixes"]),
                         (True, {"applied": [20001, 20004], "skipped": [], "failed": {"20003": self.refused}}))
        self.taken_in(out)
        self.assertEqual(out["message"], self.said + "。取り込めなかった修正が 1 件あります。"
                                         "続けて、逸脱コメント 1 件と、取り込んだ修正 2 件を Coverity に登録します")
        bug = {"classification": "Bug", "action": "Fix Required", "severity": "Major"}
        entries = [{"cids": [20002], "classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
                    "comment": "誤検知。CID 20002 の理由。"},
                   {"cids": [20001], **bug, "comment": self.where + "CID 20001 の修正の概要"},
                   {"cids": [20004, 20005], **bug, "comment": self.where + "CID 20004 の修正の概要"}]
        target = self.run_dir / "coverity-entries.json"

        def check(reply: dict, expected: list) -> None:
            raw = target.read_bytes()
            self.assertEqual(json.loads(raw), {"stream": "app-main", "entries": expected})
            self.assertEqual(Path(f"{target}.sha256").read_bytes().decode("ascii"), hashlib.sha256(raw).hexdigest() + "\n")
            self.assertEqual((reply["entries_file"], reply["entries"]), (target.as_posix(), len(expected)))
            self.assertEqual(reply["call"], [{"tool": "coverity_update_triage", "arguments": {
                "repo_root": self.repo.as_posix(), "entries_file": target.as_posix()}}])

        check(out, entries)
        # the same OK again: no fix is taken in twice
        again = self.go()
        self.assertEqual((again["fixes"]["applied"], again["fixes"]["skipped"]), ([], [20001, 20004]))
        self.taken_in(again)
        check(again, entries)
        # the write tool records what it has registered (here: not 20005): only the rest is left
        self.put("written.json", {"written": {"20001": {}, "20002": {}, "20004": {}}})
        check(self.go(), entries[2:])
        out = self.plan()
        self.assertEqual([row(out, cid).rsplit(" | ", 2)[1:] for cid in (20001, 20002, 20003, 20004)], [
            ["-", "取り込み済み |"], ["-", "登録済み |"], ["修正を取り込む", " |"],
            ["-", "取り込み済み（Coverity への登録が残っています） |"]])
        self.assertEqual(out["counts"], {"fix": 1, "deviation": 0, "none": 0, "done": 3, "failed": 0, "unfinished": 0,
                                         "left": 1})
        self.assertEqual(out["question"], "この内容で、修正の取り込み（1 件）、"
                                          "取り込み済みの修正の Coverity への登録（1 件）を行います。" + BUG + ASK)
        self.put("written.json", {"written": {"20001": {}, "20002": {}, "20004": {}, "20005": {}}})
        out = self.go()
        self.assertEqual((out["entries_file"], out["entries"], out["call"]), (None, 0, []))
        self.assertFalse(target.exists() or Path(f"{target}.sha256").exists())

    def test_a_result_or_choice_changed_after_the_list_was_shown(self):
        self.add(20001, FIX_A)
        self.add(20002)
        self.plan("20002=none")
        self.add(20003)  # one more CID was finished after the list was shown
        out = self.go()
        self.assertEqual(out, {"ok": False, "run_dir": self.run_dir.as_posix(), "changed": True, "error": CHANGED,
                               "next": [self.line("plan", "--set 20002=none")]})
        out = self.follow(out["next"][0])
        self.assertEqual(row(out, 20002), keep_row(20002, "何もしない", "チャットで指定"))
        self.put("decisions.json", {"decisions": {"20001": {"decision": "none", "at": "2026-11-10T16:00:00"}}})
        self.assertTrue(self.go()["changed"], "a choice on the screen after the list")
        (self.run_dir / "apply-plan.json").unlink()
        self.assertEqual(self.go()["next"], [self.line("plan")], "no list was shown yet")
        self.assertFalse((self.run_dir / "applied.json").exists() or (self.run_dir / "coverity-entries.json").exists())
        self.assertEqual(self.sh(*self.status), b"")

    def test_fixes_of_one_line_are_taken_in_one_at_a_time(self):
        self.add(20001, FIX_A)
        self.add(20002, FIX_A)  # the same cause, not merged: its fix is the same change again
        self.add(20003, {"src/a.c": (b"return *p;", b"return *p + 0;")})  # another change of that line
        self.plan()
        out = self.go()
        self.assertEqual((out["fixes"]["applied"], out["fixes"]["failed"]), ([20001, 20002], {"20003": self.refused}))
        self.assertEqual(sorted(self.get("applied.json")["fixes"]), ["20001", "20002"])
        self.assertEqual([entry["cids"] for entry in self.get("coverity-entries.json")["entries"]], [[20001], [20002]])
        self.assertEqual(len(self.sh(*self.changes).splitlines()), 1)  # one commit, one changed file

    def test_the_comment_of_a_fix_is_one_line_of_at_most_200_characters(self):
        self.add(20001, FIX_A)
        result = self.get("cid/20001/result.json")
        result["fix"]["summary"] = "1行目。\r\n\r\n  2行目。\t" + "長い文。" * 60
        self.put("cid/20001/result.json", result)
        self.plan()
        self.go()
        self.assertEqual(self.get("coverity-entries.json")["entries"][0]["comment"],
                         (self.where + "1行目。 2行目。 " + "長い文。" * 60)[:200])

    def test_an_ok_without_a_fix_is_recorded_too(self):
        self.add(20001, FIX_A)
        self.add(20002)
        self.plan("20001=none")
        out = self.go()
        self.assertEqual((out["fixes"], out["entries"], out["message"], self.kind in out), (
            {"applied": [], "skipped": [], "failed": {}}, 1, "続けて、逸脱コメント 1 件を Coverity に登録します", False))
        self.assertEqual(self.get("applied.json"), {"vcs": self.kind, "fixes": {}})
        # that record is no fix: the list has nothing of it
        out = self.plan("20001=none")
        self.assertEqual((out["counts"]["done"], out["counts"]["left"], out["question"]),
                         (0, 0, "この内容で、逸脱コメントの登録（1 件）を行います。" + ASK))
        # the same OK again, once the write tool has registered the comment: the record gets the new time
        self.put("written.json", {"written": {"20002": {}}})
        record, before = self.run_dir / "applied.json", time.time() - 100
        os.utime(record, (before, before))
        out = self.go()
        self.assertEqual((out["entries_file"], out["call"], out["message"]), (None, [], "取り込みと登録は完了しています"))
        self.assertEqual((self.get("applied.json"), record.stat().st_mtime > before + 50),
                         ({"vcs": self.kind, "fixes": {}}, True))


class GitTest(Flow, Case):
    status = ("git", "status", "--porcelain")
    changes = ("git", "log", "--format=%H", f"main..{BRANCH}")
    said = f"修正 2 件をブランチ {BRANCH} にコミットして push しました"
    where = f"修正をブランチ {BRANCH} にコミットしました。"
    refused = REFUSED.format("マージ")

    def remote(self, *args) -> bytes:
        return self.sh("git", "--git-dir", self.origin, *args).strip()

    def colleague(self, branch: str, files: dict) -> None:
        """A colleague pushes a commit with these files to a branch of origin."""
        other = self.top / "colleague"
        self.sh("git", "clone", "-q", self.origin, other, cwd=self.top)
        for name, data in files.items():
            (other / name).write_bytes(data)
        self.sh("git", "-c", "user.name=c", "-c", "user.email=c@example.com", "commit", "-q", "-am", "theirs", cwd=other)
        self.sh("git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=other)

    def disturb(self):
        """main has moved: a change near the fix of 20001 and one on the line of 20003. The person works
        on another branch and has a staged and an unstaged change."""
        self.colleague("main", {"src/a.c": A.replace(b"int y = 2;", b"int y = 3;"), "src/b.c": B.replace(b"0", b"2")})
        self.sh("git", "checkout", "-q", "-b", "work")
        (self.repo / "src/b.c").write_bytes(B.replace(b"0", b"5"))
        self.sh("git", "add", "src/b.c")
        (self.repo / "src/a.c").write_bytes(A + b"/* mine */\n")
        self.before = self.mine()

    def mine(self) -> list:
        """The person's branch, commit, index, working tree and work trees."""
        return [self.sh("git", *args) for args in (("symbolic-ref", "HEAD"), ("rev-parse", "HEAD"), ("status", "--porcelain"),
                                                   ("diff",), ("diff", "--cached"), ("worktree", "list"))]

    def taken_in(self, out: dict) -> None:
        self.assertEqual(self.mine(), self.before)
        self.assertEqual(self.remote("log", "--format=%an|%s|%b", f"main..{BRANCH}").decode("utf-8").split("\n\n"), [
            "試験 太郎|Coverity CID 20004, 20005 の修正（NULL_RETURNS）|CID 20004 の修正の概要",
            "試験 太郎|Coverity CID 20001 の修正（NULL_RETURNS）|CID 20001 の修正の概要"])  # one commit per fix
        self.assertEqual(self.remote("-c", "core.quotepath=false", "diff", "--name-only", "main", BRANCH).decode("utf-8"),
                         f"src/a.c\n{JP}")
        show = ("git", "--git-dir", self.origin, "show")  # the bytes of the files on the branch
        self.assertEqual(self.sh(*show, f"{BRANCH}:{JP}"), SJIS.replace(*FIX_S[JP]))
        self.assertEqual(self.sh(*show, f"{BRANCH}:src/a.c"), A.replace(b"y = 2", b"y = 3").replace(*FIX_A["src/a.c"]))
        applied = self.get("applied.json")
        self.assertEqual({key: applied[key] for key in ("vcs", "branch", "base_branch", "pushed")},
                         {"vcs": "git", "branch": BRANCH, "base_branch": "main", "pushed": True})
        self.assertEqual({cid: (fix["commit"].encode(), fix["files"]) for cid, fix in applied["fixes"].items()},
                         {"20001": (self.remote("rev-parse", f"{BRANCH}~1"), ["src/a.c"]),
                          "20004": (self.remote("rev-parse", BRANCH), [JP])})
        pull = out["git"].pop("pr")
        self.assertEqual(out["git"], {"branch": BRANCH, "base_branch": "main", "pushed": True, "push_error": None})
        self.assertEqual((pull["title"], pull["body"]), ("Coverity の警告の修正（app-main、2 件）", (
            "Coverity の警告の修正です。修正の内容は、各コミットのメッセージにあります。\n\nCID: 20001, 20004, 20005")))
        url = urlsplit(pull["compare_url"])
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "github.example", f"/acme/app/compare/main...{BRANCH}"))
        self.assertEqual(parse_qs(url.query), {"expand": ["1"], "title": [pull["title"]], "body": [pull["body"]]})

    def test_the_url_of_the_pull_request_holds_the_whole_body(self):
        self.add(20001, FIX_A, members=list(range(30001, 30501)))  # one cause of 501 warnings
        self.plan()
        pull = self.go()["git"]["pr"]
        self.assertEqual(pull["body"].split("CID: ")[1], ", ".join(map(str, [20001, *range(30001, 30501)])))
        self.assertEqual(parse_qs(urlsplit(pull["compare_url"]).query)["body"], [pull["body"]])

    def test_a_refused_push_is_made_later_and_one_branch_holds_every_fix(self):
        self.add(20001, FIX_A)
        self.add(20004, FIX_S)
        self.colleague(BRANCH, {"src/b.c": B.replace(b"0", b"2")})  # origin has another branch of this name
        theirs = self.remote("rev-parse", BRANCH)
        self.plan("20004=none")
        out = self.go()
        self.assertEqual((out["fixes"]["applied"], out["git"]["pushed"], out["git"]["pr"]), ([20001], False, None))
        self.assertIn("rejected", out["git"]["push_error"])
        self.assertEqual(self.remote("rev-parse", BRANCH), theirs, "a push must not overwrite what origin has")
        self.assertEqual(out["message"], f"修正 1 件をブランチ {BRANCH} にコミットしました。push に失敗しました。"
                                         "続けて、取り込んだ修正 1 件を Coverity に登録します")
        self.assertFalse(self.get("applied.json")["pushed"])
        out = self.plan("20004=none")
        self.assertEqual(row(out, 20001), fix_row(20001, "-", "取り込み済み（push が残っています）"))
        self.assertEqual(out["counts"]["left"], 2)
        self.assertEqual(out["question"], f"この内容で、ブランチ {BRANCH} の push、"
                                          "取り込み済みの修正の Coverity への登録（1 件）を行います。" + ASK)
        self.remote("update-ref", "-d", f"refs/heads/{BRANCH}")  # the colleague has removed that branch
        out = self.go()
        self.assertEqual((out["fixes"]["applied"], out["git"]["pushed"], out["git"]["push_error"]), ([], True, None))
        self.assertTrue(out["message"].startswith(f"ブランチ {BRANCH} を push しました。"))
        self.assertEqual(self.remote("rev-parse", BRANCH).decode(), self.get("applied.json")["fixes"]["20001"]["commit"])
        # a later OK for another fix of the same result folder: one more commit on the same branch
        self.plan()
        out = self.go()
        self.assertEqual((out["fixes"]["applied"], out["git"]["pr"]["title"]),
                         ([20004], "Coverity の警告の修正（app-main、2 件）"))
        self.assertEqual(self.remote("rev-list", "--count", f"main..{BRANCH}"), b"2")
        self.assertEqual(self.remote("for-each-ref", "--format=%(refname:short)"), f"{BRANCH}\nmain".encode())

    def test_a_change_that_the_base_branch_already_has_is_not_taken_in(self):
        self.add(20001, FIX_A)
        self.colleague("main", {"src/a.c": A.replace(*FIX_A["src/a.c"])})  # a colleague has made the same change
        self.plan()
        self.assertEqual(self.go()["fixes"]["failed"], {"20001": "取り込み先のブランチに、同じ変更が既にあります"})
        self.assertEqual((self.sh("git", "branch", "--list", BRANCH), self.get("applied.json")["fixes"]), (b"", {}))

    def test_without_origin_the_branch_stays_local(self):
        self.add(20001, FIX_A)
        self.sh("git", "remote", "remove", "origin")
        self.plan()
        self.sh("git", "checkout", "-q", "-b", BRANCH)  # the person has checked out the branch of the fixes
        self.assertEqual(self.go(), {"ok": False, "run_dir": self.run_dir.as_posix(), "next": [], "error": (
            f"ブランチ {BRANCH} がチェックアウトされています。別のブランチに切り替えてから、"
            "もう一度「修正を取り込んで」と入力してください")})
        self.assertFalse((self.run_dir / "applied.json").exists())
        self.sh("git", "checkout", "-q", "main")
        self.assertEqual(self.go()["git"],
                         {"branch": BRANCH, "base_branch": "main", "pushed": False, "push_error": None, "pr": None})
        self.assertEqual(self.sh("git", "rev-list", "--count", f"main..{BRANCH}"), b"1\n")
        out = self.plan()
        self.assertEqual((out["counts"]["left"], out["question"]), (
            1, "この内容で、取り込み済みの修正の Coverity への登録（1 件）を行います。" + ASK))


class SvnTest(Flow, Case):
    kind = "svn"
    status = ("svn", "status")
    changes = status
    said = "修正 2 件を作業コピーに適用しました。内容を確認して、コミットしてください"
    where = "修正を作業コピーに適用しました。"
    refused = REFUSED.format("コミット")

    def disturb(self):
        """The person has changed the line of 20003 in one of its two files."""
        (self.repo / "src/b.c").write_bytes(B.replace(b"0", b"2"))

    def taken_in(self, out: dict) -> None:
        self.assertEqual((self.repo / "src/a.c").read_bytes(), A.replace(*FIX_A["src/a.c"]))
        self.assertEqual((self.repo / JP).read_bytes(), SJIS.replace(*FIX_S[JP]))
        # the fix with a conflict is left out as a whole: nothing of it in the working copy
        self.assertEqual([line[:1] for line in self.sh("svn", "status").splitlines()], [b"M"] * 3)  # a.c, b.c, JP
        self.assertEqual(((self.repo / "src/b.c").read_bytes(), list(self.repo.rglob("*.rej"))), (B.replace(b"0", b"2"), []))
        self.assertEqual(out["svn"], {"files": ["src/a.c", JP]})
        applied = self.get("applied.json")
        self.assertEqual((applied["vcs"], {cid: fix["files"] for cid, fix in applied["fixes"].items()}),
                         ("svn", {"20001": ["src/a.c"], "20004": [JP]}))

    def test_a_fix_with_a_file_that_svn_skips_is_left_out_as_a_whole(self):
        self.add(20001, FIX_A)
        new = b"Index: src/new.c\n===\n--- src/new.c\t(nonexistent)\n+++ src/new.c\t(working copy)\n@@ -0,0 +1 @@\n+int n;\n"
        self.put("cid/20001/fix.patch", self.patch(FIX_A) + new)  # the fix also adds a file
        self.put("cid/20001/state.json", {"status": "done", "files": ["src/a.c", "src/new.c"], "confidence": "high"})
        (self.repo / "src/new.c").write_bytes(b"mine\n")  # the person has a file of this name: svn skips the target
        self.plan()
        self.assertEqual(list(self.go()["fixes"]["failed"]), ["20001"])
        self.assertEqual(((self.repo / "src/a.c").read_bytes(), self.get("applied.json")["fixes"]), (A, {}))

    @unittest.skipUnless(os.name == "posix", "svn の出力を置き換えるシェルスクリプトを使用するため、POSIX だけで実行します")
    def test_a_file_name_that_svn_cannot_print_is_taken_in(self):
        """On a Windows whose code page lacks the characters of a name, svn prints ?\\NNN for them."""
        self.add(20004, FIX_S)
        tool = self.top / "bin" / "svn"
        tool.parent.mkdir()
        tool.write_text(f"#!/bin/sh\n'{shutil.which('svn')}' \"$@\" | LC_ALL=C tr -c '\\000-\\177' '?'\n")
        tool.chmod(0o755)
        self.plan()
        self.env["PATH"] = f"{tool.parent}{os.pathsep}{self.env['PATH']}"
        self.assertEqual(self.go()["fixes"], {"applied": [20004], "skipped": [], "failed": {}})
        self.assertEqual((self.repo / JP).read_bytes(), SJIS.replace(*FIX_S[JP]))


if __name__ == "__main__":
    unittest.main()
