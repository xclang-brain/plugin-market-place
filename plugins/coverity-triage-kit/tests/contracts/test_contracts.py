"""部品の間の取り決めを確かめる試験（憲章 第25条4項・5項、仕様 REQ-003、REQ-004、REQ-103、REQ-107、設計書 4.17、8.4、9.1「取り決め」）。

- 写し：設計書 4.17 の取り決めのファイルは、持ち主の写しと、ほかの部品の写しが同じバイト列。
  決まった部品（4.17 の「写しを持つ部品」）が写しを持つ。共通の試験のデータ（cases.json）の形。
- 中身：plan.schema.json の中の結果の定義が、result.schema.json の $defs と同じ。評価基準 C-01〜C-28 の分け方（4.6）。
  確認の文字列の試験のデータが、4.10 の決まりで計算した値と合う。
- defaults.json：役割（roles）と統括エージェント（orchestrator）が、統括エージェントの「使う部品」の表と、役割の言葉に合う。
  コマンドの場所とスキルがある。
- 書く部品と読む部品：MCP サーバーの偽の Coverity（api: fake）が書いた issues.json・issue-<CID>.json を、
  一括実行のコマンドが受け付けて作業を始める。MCP サーバーの確認の文字列が、反映の計画のファイルのバイト列から決まる。

部品の名前は書かない。部品は、役割の名前で表に書き、docs/replace.md の「## 今の部品」の表で今の名前に直す。
ないものは、理由を書いて飛ばす。

  python3 -m unittest discover -s tests/contracts -v
環境変数：SKIP_SLOW_TESTS=1 で、部品を動かして確かめる試験を飛ばす。
"""

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS / "structure"))
import plugin_parts as pp  # noqa: E402

# 取り決めのファイル（設計書 4.17）。ここだけに、取り決めのファイルの名前を書く。
# （ファイル, 持ち主の役割, 写しを持つ部品の役割）。役割の名前は docs/replace.md の「今の部品」の表と同じ
OWNERS = [
    ("result.schema.json", "結果の記録", ["一括実行", "結果と設定の画面"]),
    ("review.schema.json", "結果の確かめ", ["一括実行"]),
    ("criteria.json", "結果の確かめ", ["一括実行"]),
    ("plan.schema.json", "同じ原因の統合", ["一括実行"]),
    ("run.schema.json", "一括実行", ["反映", "結果と設定の画面", "困ったとき", "自己診断"]),
    ("approvals.schema.json", "一括実行", ["反映", "結果と設定の画面"]),
    ("contract/comment/cases.json", "逸脱コメント", ["結果の記録", "一括実行"]),
    ("contract/result/cases.json", "結果の記録", ["一括実行"]),
    ("contract/lint/cases.json", "結果の確かめ", ["一括実行"]),
    ("contract/plan/cases.json", "同じ原因の統合", ["一括実行"]),
    ("contract/yaml-edit/cases.json", "準備", ["結果と設定の画面"]),
    ("contract/token/cases.json", "反映", ["Coverity との通信"]),
]
LINT_CODES = {"evidence_missing_file", "evidence_line_out_of_range", "undetermined_high", "drift_high",
              "true_bug_deviation", "not_bug_fix", "no_evidence"}
MIN_CASES = 8
SLOW = os.environ.get("SKIP_SLOW_TESTS") == "1"


# ---------------------------------------------------------------------------
# 写しを探す
# ---------------------------------------------------------------------------

def part_folder(role):
    """役割 → 今の部品のフォルダ（docs/replace.md の表）。表にない・フォルダがないときは None"""
    for row in pp.current_parts():
        if row["role"] == role:
            folder = pp.ROOT / row["place"]
            return folder if folder.is_dir() else None
    return None


def copies_in(folder, contract):
    """部品のフォルダの中の、取り決めのファイルの写し（スキルは assets/ の下、MCP サーバーは tests/ の下）"""
    found = []
    name = Path(contract).name
    for base in (folder / "assets", folder / "tests"):
        if not base.is_dir():
            continue
        for path in base.rglob(name):
            if path.is_file() and not pp.skipped(path, folder) and path.as_posix().endswith(contract):
                found.append(path)
    return sorted(found)


def all_copies(contract):
    folders = [pp.SKILLS_DIR / n for n in pp.skill_names()] + pp.mcp_dirs()
    out = []
    for folder in folders:
        out += copies_in(folder, contract)
    return out


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owner_copy(contract, role):
    folder = part_folder(role)
    if folder is None:
        return None
    paths = copies_in(folder, contract)
    return paths[0] if paths else None


class CopiesTest(unittest.TestCase):
    """設計書 4.17：写しは持ち主と同じバイト列"""

    def test_replace_doc_names_the_owners(self):
        if not pp.REPLACE_DOC.is_file():
            self.skipTest("docs/replace.md がまだありません")
        roles = {row["role"] for row in pp.current_parts()}
        needed = {role for _, owner, holders in OWNERS for role in [owner] + holders}
        self.assertEqual(sorted(needed - roles), [], "docs/replace.md の「今の部品」の表に、取り決めの役割がありません")

    def test_copies_are_identical(self):
        problems, checked = [], 0
        for contract, owner, _ in OWNERS:
            paths = all_copies(contract)
            if len(paths) < 2:
                continue
            canonical = owner_copy(contract, owner) or paths[0]
            for path in paths:
                checked += 1
                if path.read_bytes() != canonical.read_bytes():
                    problems.append(f"{pp.rel(path)} が {pp.rel(canonical)}（持ち主：{owner}）と違います")
        if not checked:
            self.skipTest("写しがまだありません")
        self.assertEqual(problems, [], "取り決めの写しが、持ち主のものと違います（持ち主の写しをそのままコピーしてください）：\n" + "\n".join(problems))

    def test_owners_and_holders_have_the_files(self):
        problems, checked = [], 0
        for contract, owner, holders in OWNERS:
            owner_dir = part_folder(owner)
            if owner_dir is None:
                continue
            checked += 1
            if not copies_in(owner_dir, contract):
                problems.append(f"持ち主（{owner}：{pp.rel(owner_dir)}）に {contract} がありません")
                continue
            for holder in holders:
                folder = part_folder(holder)
                if folder is not None and not copies_in(folder, contract):
                    problems.append(f"{holder}（{pp.rel(folder)}）に {contract} の写しがありません")
        if not checked:
            self.skipTest("取り決めの持ち主の部品がまだありません")
        self.assertEqual(problems, [], "取り決めのファイルが足りません（設計書 4.17）：\n" + "\n".join(problems))


class CasesTest(unittest.TestCase):
    """共通の試験のデータ（assets/contract/<名前>/cases.json）の形"""

    def cases_files(self):
        found = []
        for contract, owner, _ in OWNERS:
            if contract.startswith("contract/"):
                path = owner_copy(contract, owner) or next(iter(all_copies(contract)), None)
                if path is not None:
                    found.append((contract.split("/")[1], path, path == owner_copy(contract, owner)))
        if not found:
            self.skipTest("共通の試験のデータがまだありません")
        return found

    def check_case(self, kind, case):
        inp, exp = case.get("input"), case.get("expect")
        self.assertIsInstance(inp, dict, "input がありません")
        self.assertIsInstance(exp, dict, "expect がありません")
        if kind == "comment":
            self.assertIsInstance(inp.get("comment"), str)
            self.assertIsInstance(exp.get("ok"), bool)
        elif kind == "result":
            self.assertIn("result", inp)  # 形の崩れた結果（配列など）を確かめる case もある
            self.assertIsInstance(inp.get("item"), str)
            if "cids" in inp:
                self.assertTrue(isinstance(inp["cids"], list) and all(isinstance(c, int) for c in inp["cids"]))
            self.assertIsInstance(exp.get("ok"), bool)
        elif kind == "lint":
            self.assertIn("result", inp)
            self.assertIsInstance(inp.get("files"), dict)
            points = exp.get("points")
            self.assertIsInstance(points, list)
            self.assertEqual(points, sorted(points), "points は並べた形にします")
            self.assertEqual(sorted(set(points) - LINT_CODES), [], "知らない points の値があります")
        elif kind == "plan":
            self.assertIn("plan", inp)
            self.assertIsInstance(inp.get("items"), dict)
            self.assertIsInstance(exp.get("ok"), bool)
        elif kind == "yaml-edit":
            self.assertIsInstance(inp.get("original"), str)
            self.assertIsInstance(inp.get("values"), dict)
            self.assertIsInstance(exp.get("text"), str)
            self.assertIsInstance(exp.get("notice"), bool)
        elif kind == "token":
            self.assertTrue(isinstance(inp.get("plan_text"), str) or isinstance(inp.get("entries"), list),
                            "input は plan_text か entries のどちらかです")
            self.assertRegex(str(exp.get("token")), r"^[0-9a-f]{12}$")

    def test_cases_have_the_contract_shape(self):
        for kind, path, is_owner in self.cases_files():
            data = json.loads(pp.read(path))
            with self.subTest(contract=kind, file=pp.rel(path)):
                self.assertEqual(data.get("version"), 1)
                self.assertEqual(data.get("contract"), kind, "contract がフォルダの名前と違います")
                cases = data.get("cases")
                self.assertIsInstance(cases, list)
                if is_owner:
                    self.assertGreaterEqual(len(cases), MIN_CASES, f"持ち主の cases.json は {MIN_CASES} 件以上にします")
                names = [c.get("name") for c in cases]
                self.assertTrue(all(isinstance(n, str) and n for n in names), "どの case にも name を付けます")
                self.assertEqual(len(names), len(set(names)), "同じ name の case があります")
                for case in cases:
                    with self.subTest(case=case.get("name")):
                        self.check_case(kind, case)

    def test_token_cases_follow_the_rule(self):
        """確認の文字列（設計書 4.10）：ファイルはバイト列の SHA-256 の先頭12文字。entries は {"triage": [...]} を
        sort_keys・区切り (",", ":")・ensure_ascii=False で JSON にした UTF-8 の SHA-256 の先頭12文字"""
        path = owner_copy("contract/token/cases.json", "反映") or next(iter(all_copies("contract/token/cases.json")), None)
        if path is None:
            self.skipTest("確認の文字列の試験のデータがまだありません")
        for case in json.loads(pp.read(path)).get("cases", []):
            with self.subTest(case=case.get("name")):
                self.assertEqual(token_of(case["input"]), case["expect"]["token"])


ENTRY_KEYS = ("id", "cids", "stream", "classification", "action", "severity", "comment")


def token_entry(entry):
    """確認の文字列に入れる項目の形：取り決めの項目だけ。stream がない（null の）ときは鍵を入れない。
    severity と comment がないときは空の文字列（設計書 4.10 を、持ち主の試験のデータのとおりに読んだもの）"""
    out = {k: entry[k] for k in ENTRY_KEYS if k in entry and entry[k] is not None}
    out.setdefault("severity", "")
    out.setdefault("comment", "")
    return out


def token_of(inp):
    if "plan_text" in inp:
        data = inp["plan_text"].encode("utf-8")
    else:
        entries = [token_entry(e) for e in inp["entries"]]
        data = json.dumps({"triage": entries}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:12]


class SchemaContentTest(unittest.TestCase):
    """取り決めの中身（設計書 4.5〜4.7）"""

    def load(self, contract, role):
        path = owner_copy(contract, role) or next(iter(all_copies(contract)), None)
        if path is None:
            self.skipTest(f"{contract} がまだありません")
        return json.loads(pp.read(path))

    def test_plan_schema_embeds_the_result_definitions(self):
        result = self.load("result.schema.json", "結果の記録")
        plan = self.load("plan.schema.json", "同じ原因の統合")
        defs = plan.get("$defs", {})
        for key, value in result.get("$defs", {}).items():
            with self.subTest(definition=key):
                self.assertEqual(defs.get(key), value, f"plan.schema.json の $defs.{key} が result.schema.json と違います")
        merged = [v for v in defs.values() if isinstance(v, dict) and v.get("properties", {}).keys() == result.get("properties", {}).keys()]
        self.assertTrue(merged, "plan.schema.json に、result.json と同じ項目の定義（まとめた結果）がありません")
        for value in merged:
            self.assertEqual(value["properties"], result["properties"], "まとめた結果の項目の定義が result.schema.json と違います")
            self.assertEqual(sorted(set(result.get("required", [])) - set(value.get("required", []))), ["item"],
                             "まとめた結果の要る項目は、result.json から item を除いたものです")

    def test_criteria(self):
        """評価基準は C-01〜C-28。scope: run は C-16〜C-19 と C-24〜C-27、ほかの20個は scope: item（設計書 4.6）"""
        data = self.load("criteria.json", "結果の確かめ")
        criteria = data.get("criteria", [])
        ids = [c.get("id") for c in criteria]
        self.assertEqual(ids, [f"C-{n:02d}" for n in range(1, 29)])
        run = {c["id"] for c in criteria if c.get("scope") == "run"}
        self.assertEqual(run, {f"C-{n:02d}" for n in [16, 17, 18, 19, 24, 25, 26, 27]})
        self.assertEqual(sum(1 for c in criteria if c.get("scope") == "item"), 20)
        for c in criteria:
            self.assertTrue(str(c.get("text", "")).strip(), f"{c.get('id')} の文が空です")

    def test_review_schema_checks_are_item_criteria(self):
        review = self.load("review.schema.json", "結果の確かめ")
        criteria = self.load("criteria.json", "結果の確かめ")
        item_ids = sorted(c["id"] for c in criteria["criteria"] if c.get("scope") == "item")
        enums = [v["enum"] for v in _walk(review) if isinstance(v, dict) and "enum" in v and all(
            isinstance(x, str) and x.startswith("C-") for x in v["enum"])]
        for enum in enums:
            self.assertEqual(sorted(enum), item_ids, "review.schema.json の check の値が、scope: item の評価基準と違います")


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


class DefaultsTest(unittest.TestCase):
    """defaults.json（設計書 8.4）と、統括エージェントの表と役割の言葉"""

    def setUp(self):
        self.data = pp.defaults()
        if self.data is None:
            self.skipTest("defaults.json がまだありません")

    def orchestrator_rows(self):
        stem = pp.id_stem(self.data.get("orchestrator", ""))
        path = pp.agent_path(stem)
        if not path.is_file():
            self.skipTest("統括エージェントの定義がまだありません")
        rows, error = pp.parts_table(pp.frontmatter(path)[1])
        self.assertIsNone(error, error)
        return rows

    def test_keys(self):
        for key in ["orchestrator", "roles", "runCommand", "guiCommand", "applySkill", "setupSkill", "pluginSkills", "port", "issueRepo"]:
            self.assertIn(key, self.data, f"defaults.json に {key} がありません")
        self.assertEqual(sorted(self.data["roles"]), sorted(pp.ROLE_WORDS), "roles は worker・reviewer・integrator です")
        for agent_id in [self.data["orchestrator"], *self.data["roles"].values()]:
            with self.subTest(agent=agent_id):
                self.assertTrue(str(agent_id).startswith(pp.PLUGIN_NAME + ":"), f"{agent_id} は「{pp.PLUGIN_NAME}:<名前>」の形にします")
                self.assertTrue(pp.agent_path(pp.id_stem(agent_id)).is_file(), f"{agent_id} の定義がありません")

    def test_commands_and_skills_exist(self):
        for key in ("runCommand", "guiCommand"):
            with self.subTest(key=key):
                path = pp.ROOT / self.data[key]
                self.assertTrue(path.is_file(), f"defaults.json の {key}（{self.data[key]}）がありません")
                self.assertEqual(path.parent.name, "scripts")
                self.assertEqual(path.parent.parent.parent, pp.SKILLS_DIR, f"{key} はスキルのスクリプトを指します")
        for name in [self.data["applySkill"], self.data["setupSkill"], *self.data["pluginSkills"]]:
            with self.subTest(skill=name):
                self.assertTrue((pp.SKILLS_DIR / name / "SKILL.md").is_file(), f"defaults.json のスキル {name} がありません")

    def test_roles_match_the_orchestrator_table(self):
        """roles の名前は、統括エージェントの表のエージェントと同じ。その行の役割に、役割の言葉がある"""
        rows = self.orchestrator_rows()
        agents = {row[1]: row[0] for row in rows if pp.agent_path(row[1]).is_file()}
        roles = {pp.id_stem(v): k for k, v in self.data["roles"].items()}
        self.assertEqual(sorted(agents), sorted(roles), "統括エージェントの表のエージェントと、defaults.json の roles が違います")
        for name, role in roles.items():
            with self.subTest(role=role):
                self.assertIn(pp.ROLE_WORDS[role], agents.get(name, ""), f"統括エージェントの表の {name} の役割に「{pp.ROLE_WORDS[role]}」がありません")

    def test_commands_belong_to_the_orchestrator_table(self):
        """統括エージェントが使うスキル（一括実行・画面・反映・準備）は、その表にある"""
        names = {row[1] for row in self.orchestrator_rows()}
        used = {Path(self.data["runCommand"]).parent.parent.name, Path(self.data["guiCommand"]).parent.parent.name,
                self.data["applySkill"], self.data["setupSkill"]}
        self.assertEqual(sorted(used - names), [], "defaults.json のスキルが、統括エージェントの表にありません")


# ---------------------------------------------------------------------------
# 書く部品と読む部品（部品を動かす）
# ---------------------------------------------------------------------------

class McpClient:
    """MCP サーバーと stdio で話す小さなクライアント（JSON-RPC 2.0、1行に1つ）"""

    def __init__(self, command, env, cwd):
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.log, **kwargs)
        self.lines = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in iter(self.proc.stdout.readline, b""):
            self.lines.put(line)
        self.lines.put(None)

    def stderr(self):
        self.log.seek(0)
        return self.log.read().decode("utf-8", errors="replace")[-3000:]

    def send(self, message):
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method, params, timeout=120):
        self.next_id += 1
        ident = self.next_id
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"MCP サーバーが {timeout} 秒たっても {method} に答えません：\n{self.stderr()}")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(f"MCP サーバーが終わりました：\n{self.stderr()}")
            try:
                message = json.loads(line)
            except ValueError:
                raise AssertionError(f"MCP サーバーの標準出力に JSON でない行があります：{line[:200]!r}")
            if message.get("id") == ident:
                if "error" in message:
                    raise AssertionError(f"{method} が失敗しました：{message['error']}")
                return message.get("result")

    def start(self):
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "contract-tests", "version": "1"}}, timeout=300)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, tool, arguments, timeout=120):
        """ツールを呼び、結果の JSON を返す。done が false なら、同じ引数で呼び直す"""
        for _ in range(20):
            result = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout)
            data = result.get("structuredContent")
            if not isinstance(data, dict):
                texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
                try:
                    data = json.loads(texts[0]) if texts else {}
                except ValueError:
                    data = {"text": "\n".join(texts)}
            if isinstance(data, dict) and isinstance(data.get("result"), dict) and len(data) == 1:
                data = data["result"]
            if result.get("isError"):
                raise AssertionError(f"{tool} が失敗しました：{json.dumps(data, ensure_ascii=False)[:800]}")
            if not (isinstance(data, dict) and data.get("done") is False):
                return data
        raise AssertionError(f"{tool} が 20 回呼んでも終わりません")

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                try:
                    os.killpg(self.proc.pid, 9)
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for stream in (self.proc.stdout, self.log):
            try:
                stream.close()
            except OSError:
                pass


def find_token(data):
    """結果の中の、確認の文字列（12桁の16進）"""
    if isinstance(data, dict):
        for key, value in data.items():
            if "token" in key and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value):
                return value
        for value in data.values():
            found = find_token(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_token(value)
            if found:
                return found
    return None


def git(*args, cwd):
    return subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=str(cwd), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


SAMPLE_SOURCE = """#include <stdio.h>
#include <string.h>

int read_header(FILE *fp, char *buf, size_t size)
{
    char *line = fgets(buf, (int)size, fp);
    return (int)strlen(line);
}

int parse_count(const char *text)
{
    int n = 0;
    while (*text >= '0' && *text <= '9') {
        n = n * 10 + (*text - '0');
        text++;
    }
    return n;
}
"""

FAKE_ISSUES = """snapshot:
  sourceVersion: "{revision}"
issues:
  - cid: 20001
    project: app
    stream: app-main
    checker: NULL_RETURNS
    file: src/reader.c
    function: read_header
    line: 7
    impact: High
    cwe: 476
    status: New
    classification: Unclassified
    action: Undecided
    severity: Unspecified
    checker_description: "fgets の戻り値が NULL かを確かめずに使っている"
    events:
      - {{file: src/reader.c, line: 6, tag: returned_null, description: "fgets は NULL を返すことがある", main: false}}
      - {{file: src/reader.c, line: 7, tag: dereference, description: "line を NULL かを確かめずに使う", main: true}}
  - cid: 20002
    project: app
    stream: app-main
    checker: INTEGER_OVERFLOW
    file: src/reader.c
    function: parse_count
    line: 14
    impact: Medium
    cwe: 190
    status: New
    classification: Unclassified
    action: Undecided
    severity: Unspecified
    checker_description: "掛け算があふれることがある"
    events:
      - {{file: src/reader.c, line: 14, tag: overflow, description: "n * 10 があふれる", main: true}}
"""


class WriterReaderTest(unittest.TestCase):
    """書く部品の出力を、読む部品が受け付ける（仕様 REQ-004、設計書 4.3・4.4・4.10・4.17）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="contracts-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def need(self):
        if SLOW:
            self.skipTest("SKIP_SLOW_TESTS=1 のため飛ばしました")
        if shutil.which("uv") is None:
            self.skipTest("uv がありません")
        servers = pp.mcp_config().get("mcpServers") or {}
        if not servers:
            self.skipTest("mcp.json に MCP サーバーがありません")
        name, server = next(iter(servers.items()))
        folder = pp.mcp_registrations().get(name)
        if folder is None or not (folder / "pyproject.toml").is_file():
            self.skipTest("MCP サーバーのフォルダがまだありません")
        return name, server

    def client(self, home):
        name, server = self.need()
        command = [shutil.which(server["command"]) or server["command"]] + [pp.expand_plugin_root(a) for a in server.get("args") or []]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", UV_PROJECT_ENVIRONMENT=str(Path(self.tmp) / "mcp-venv"),
                   COV_USER="contract-tester", COV_AUTH_KEY="contract-test-key")
        for key, value in (server.get("env") or {}).items():
            env[key] = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), value)
        return McpClient(command, env, home).start()

    def make_repo(self):
        repo = Path(tempfile.mkdtemp(prefix="repo-", dir=self.tmp))
        (repo / "src").mkdir()
        (repo / "src" / "reader.c").write_text(SAMPLE_SOURCE, encoding="utf-8")
        git("init", "-q", cwd=repo)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A", cwd=repo)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init", cwd=repo)
        revision = git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip()
        settings = repo / ".coverity-triage"
        (settings / "filters").mkdir(parents=True)
        out = Path(self.tmp) / ("out-" + repo.name)
        (settings / "config.yaml").write_text(
            "coverity:\n  url: https://coverity.example.invalid\n  api: fake\n  fake_data: fake-issues.yaml\n"
            f"output_dir: {json.dumps(out.as_posix())}\n", encoding="utf-8")
        (settings / "filters" / "all.yaml").write_text("name: すべて\nstreams: [app-main]\nlimit: 20\n", encoding="utf-8")
        (settings / "fake-issues.yaml").write_text(FAKE_ISSUES.format(revision=revision), encoding="utf-8")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A", cwd=repo)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "settings", cwd=repo)
        return repo

    def check_issues_file(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("filter_sha", "stream", "limit", "started_at", "finished_at", "issues"):
            self.assertIn(key, data, f"issues.json に {key} がありません（設計書 4.3）")
        self.assertIsNotNone(data["finished_at"], "issues.json の finished_at が空です（途中のまま）")
        self.assertEqual(sorted(i["cid"] for i in data["issues"]), [20001, 20002])
        for issue in data["issues"]:
            for key in ("cid", "checker", "file", "line", "stream"):
                self.assertIn(key, issue, f"issues.json の警告に {key} がありません")
        return data

    def check_detail_file(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("fetched_at", "issue", "events"):
            self.assertIn(key, data, f"{path.name} に {key} がありません（設計書 4.4）")
        for event in data["events"]:
            for key in ("file", "line", "tag", "description", "main"):
                self.assertIn(key, event, f"{path.name} の events に {key} がありません")

    def test_token_from_the_plan_bytes(self):
        """MCP サーバーの確認の文字列は、反映の計画のファイルのバイト列と、entries の決まりから決まる（設計書 4.10）"""
        home = Path(tempfile.mkdtemp(prefix="token-", dir=self.tmp))
        plan = home / "apply-plan.json"
        entry = {"id": "20002", "cids": [20002], "stream": "app-main", "classification": "False Positive",
                 "action": "Ignore", "severity": "Unspecified", "comment": "誤検知。呼び出し元で長さを確かめている（src/reader.c:12）。"}
        plan.write_bytes(json.dumps({"run_id": "20261108-101500", "created_at": "2026-11-08T10:15:00+09:00",
                                     "triage": [entry], "code": [], "reject": []}, ensure_ascii=False, indent=2).encode("utf-8"))
        client = self.client(home)
        try:
            got = find_token(client.call("coverity_preview_triage", {"plan_file": str(plan)}))
            self.assertEqual(got, hashlib.sha256(plan.read_bytes()).hexdigest()[:12], "計画のファイルの確認の文字列が違います")
            got = find_token(client.call("coverity_preview_triage", {"entries": [entry]}))
            self.assertEqual(got, token_of({"entries": [entry]}), "entries の確認の文字列が違います")
            cases = owner_copy("contract/token/cases.json", "反映")
            for case in (json.loads(pp.read(cases))["cases"] if cases else []):
                with self.subTest(case=case["name"]):
                    inp = case["input"]
                    if "plan_text" in inp:
                        path = home / f"case-{abs(hash(case['name']))}.json"
                        path.write_bytes(inp["plan_text"].encode("utf-8"))
                        got = find_token(client.call("coverity_preview_triage", {"plan_file": str(path)}))
                    else:
                        got = find_token(client.call("coverity_preview_triage", {"entries": inp["entries"]}))
                    self.assertEqual(got, case["expect"]["token"])
        finally:
            client.close()

    def run_command(self):
        """一括実行のコマンド（defaults.json の runCommand）。なければ飛ばす"""
        self.need()
        data = pp.defaults()
        if data is None or not (pp.ROOT / data.get("runCommand", "")).is_file():
            self.skipTest("一括実行のコマンド（defaults.json の runCommand）がまだありません")
        if shutil.which("git") is None:
            self.skipTest("git がありません")
        return pp.ROOT / data["runCommand"]

    def flow(self, offline):
        """MCP サーバー（偽の Coverity）と一括実行のコマンドで、作業（launch）が始まるところまで進める。
        offline：MCP サーバーが先に書いた issues.json と issue-<CID>.json を new-run --issues-file に渡す。
        そうでなければ：tick の fetch_issues・fetch_details の指示のとおりに MCP サーバーを呼ぶ（統括エージェントの代わり）"""
        run_py = self.run_command()
        repo = self.make_repo()
        fetched = Path(tempfile.mkdtemp(prefix="fetched-", dir=self.tmp))
        uv = shutil.which("uv")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")

        def run(*args, timeout=180):
            done = subprocess.run([uv, "run", "--native-tls", "--quiet", str(run_py), *args], cwd=str(repo), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            try:
                out = json.loads(done.stdout.decode("utf-8"))
            except ValueError:
                self.fail(f"一括実行の {args[0]} の出力が JSON ではありません：{done.stdout[-800:]!r} {done.stderr[-800:]!r}")
            self.assertTrue(out.get("ok"), f"一括実行の {args[0]} が失敗しました：{json.dumps(out, ensure_ascii=False)[:1200]}")
            return out

        client = self.client(repo)
        run_dir, seen = None, {"fetch_issues": [], "fetch_details": []}
        try:
            filter_file = repo / ".coverity-triage" / "filters" / "all.yaml"
            options = ["--no-initial", "--no-review", "--no-integration", "--verify", "none"]
            if offline:
                client.call("coverity_search_issues", {"repo_root": str(repo), "filter_file": str(filter_file),
                                                       "output_file": str(fetched / "issues.json")})
                self.check_issues_file(fetched / "issues.json")
                client.call("coverity_get_issue_details", {"repo_root": str(repo), "stream": "app-main", "cids": [20001, 20002],
                                                           "output_dir": str(fetched)})
                for cid in (20001, 20002):
                    self.check_detail_file(fetched / f"issue-{cid}.json")
                options += ["--issues-file", str(fetched / "issues.json")]
            created = run("new-run", "--repo", str(repo), "--filter", str(filter_file.name), *options)
            run_dir = Path(created["run_dir"])
            schema = owner_copy("run.schema.json", "一括実行")
            if schema is not None:
                errors = pp.schema_errors(json.loads((run_dir / "run.json").read_text(encoding="utf-8")), json.loads(pp.read(schema)))
                self.assertEqual(errors, [], "new-run が書いた run.json が run.schema.json に合いません")
            pending, launched = [], None
            for _ in range(40):
                args = ["tick", "--run", str(run_dir), "--wait", "2"]
                if pending:
                    args += ["--finished", pending.pop(0)]
                tick = run(*args, timeout=120)
                self.assertFalse(tick.get("stop"), f"tick が止まりました：{tick.get('say')} {tick.get('error')}")
                for action in tick.get("actions", []):
                    kind = action.get("type")
                    if kind == "fetch_issues":
                        client.call("coverity_search_issues", {"repo_root": action["repo_root"], "filter_file": action["filter_file"],
                                                               "output_file": action["output_file"]})
                        seen[kind].append(Path(action["output_file"]))
                    elif kind == "fetch_details":
                        client.call("coverity_get_issue_details", {"repo_root": action["repo_root"], "stream": action["stream"],
                                                                   "cids": action["cids"], "output_dir": action["output_dir"]})
                        seen[kind] += [Path(action["output_dir"]) / f"issue-{cid}.json" for cid in action["cids"]]
                    elif kind == "command":
                        subprocess.run(action["command"], shell=True, cwd=str(repo), env=env, timeout=60,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    elif kind == "launch":
                        launched = action
                        break
                    if action.get("task"):
                        pending.append(action["task"])
                if launched or (tick.get("done") and not pending):
                    break
            self.assertIsNotNone(launched, "MCP サーバーが書いたファイルで、作業（launch）が始まりません")
            self.assertEqual(launched.get("role"), "worker")
            for key in ("task", "role", "item", "description", "prompt"):
                self.assertIn(key, launched, f"launch に {key} がありません（設計書 4.12）")
            # 作業に渡る警告経路が、MCP サーバーの書いたもの（取れなかった印ではない）
            copies = sorted(run_dir.glob("items/*/issue-*.json"))
            self.assertTrue(copies, "実行フォルダの items/ に警告経路のファイルがありません")
            for path in copies:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("error", data, f"{path.name} が、警告経路を取れなかった印になっています：{data.get('error')}")
                self.check_detail_file(path)
            return seen
        finally:
            client.close()
            if run_dir is not None:
                subprocess.run([uv, "run", "--native-tls", "--quiet", str(run_py), "stop", "--run", str(run_dir)], cwd=str(repo),
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)

    def test_offline_run_reads_files_written_by_the_mcp_server(self):
        """MCP サーバーが書いた issues.json と issue-<CID>.json を、new-run --issues-file が受け付け、作業を始める（設計書 6章の一括実行）"""
        self.flow(offline=True)

    def test_run_follows_the_mcp_server_through_tick(self):
        """tick の fetch_issues・fetch_details の指示で MCP サーバーが書いたファイルを、一括実行が読み、作業を始める（設計書 4.3、4.4、4.12）"""
        seen = self.flow(offline=False)
        self.assertTrue(seen["fetch_issues"], "tick が警告の取得（fetch_issues）を頼みませんでした")
        self.assertTrue(seen["fetch_details"], "tick が警告経路の取得（fetch_details）を頼みませんでした")
        for path in seen["fetch_issues"]:
            self.check_issues_file(path)
        for path in seen["fetch_details"]:
            self.check_detail_file(path)

if __name__ == "__main__":
    unittest.main()
