"""部品の間の取り決めを確かめる試験（憲章 第25条4項・5項、仕様 REQ-003、REQ-004、REQ-103、REQ-107、設計書 4.17、8.4、9.1「取り決め」）。

- 写し（設計書 4.17）：取り決めのファイル（データ・持ち主のスクリプト）は、持ち主のものと、
  ほかの部品の写しが同じバイト列。写しは、部品のフォルダの中を「ファイルの名前」で探す（表に書いた部品のほかに
  同じ名前のファイルを持つ部品も、写しとして確かめる）。表の「写しを持つ部品」に写しがなければ、不合格にする。
  スクリプトの写しは、写す側が読み込んで使い（自分で書き直さない）、要るライブラリを写す側が持ち、
  探すデータ（assets/ のファイル）を写す側も同じ場所に持つ。
- 中身：plan.schema.json の中の結果の定義が、result.schema.json の $defs と同じ。評価基準 C-01〜C-28 の分け方（4.6）。
- エージェントの2つの形（Claude Code の agents/<名前>.md と Copilot CLI の .agent.md）：同じ名前、同じ「使う部品」の表、
  同じ見出しの並び、同じ報告の形。ツールの言葉と tools は読み替えたもの（Skill・Agent・AskUserQuestion・Bash と
  skill・task・ask_user・シェル）。Claude Code の統括エージェントは tools を書かず、Edit・Write と書き戻しのツール
  （2つの名前の形）を disallowedTools で外す。MCP のツールは表のサーバーのものだけ、書き戻しのツールはどのエージェントにもない。
- 書く部品と読む部品：MCP サーバーの偽の Coverity（api: fake）が書いた issues.json・issue-<CID>.json を、
  一括実行のコマンドが受け付けて作業を始める。真偽の調査のスクリプトが issue-<CID>.json を読める。
  MCP サーバーの確認の文字列が、反映の計画のファイルのバイト列から決まる。

コンポーネントは、役割の名前で表に書く。役割とフォルダの対応は、tests/structure/plugin_parts.py の PARTS にある。

  python3 -m unittest discover -s tests/contracts -v
環境変数：SKIP_SLOW_TESTS=1 で、部品を動かして確かめる試験を飛ばす。
"""

import ast
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

# 取り決めのファイル（設計書 4.17 の表）。ここだけに、取り決めのファイルの名前を書く。
# （持ち主のフォルダの中のパス, 持ち主の役割, 写しを持つ部品の役割）。役割の名前は plugin_parts.py の PARTS と同じ。
# 写しの置き場所：データは assets/、スクリプトは scripts/（MCP サーバーはパッケージ src/<パッケージ>/）。
CONTRACTS = [
    # 結果の形・確かめの形と評価基準・統合の計画の形（データ）
    ("assets/result.schema.json", "結果の記録", ["一括実行", "同じ原因の統合", "結果と設定の画面"]),
    ("assets/review.schema.json", "結果の確かめ", ["一括実行"]),
    ("assets/criteria.json", "結果の確かめ", ["一括実行"]),
    ("assets/plan.schema.json", "同じ原因の統合", ["一括実行"]),
    # 逸脱コメントの決まり
    ("scripts/check_comment.py", "逸脱コメント", ["結果の記録", "同じ原因の統合", "一括実行"]),
    # 結果の決まり
    ("scripts/check_result.py", "結果の記録", ["一括実行"]),
    # 確かめの決まり・機械的な確かめ
    ("scripts/check_review.py", "結果の確かめ", ["一括実行"]),
    ("scripts/lint_result.py", "結果の確かめ", ["一括実行"]),
    # 統合の計画の決まり
    ("scripts/check_plan.py", "同じ原因の統合", ["一括実行"]),
    # 人に見せる確信度
    ("scripts/confidence.py", "一括実行", ["反映", "結果と設定の画面"]),
    # 注釈を残す書き換え
    ("scripts/yamledit.py", "準備", ["結果と設定の画面"]),
    # 確認の文字列
    ("scripts/plantoken.py", "反映", ["Coverity との通信"]),
    # 実行フォルダと承認の形
    ("assets/run.schema.json", "一括実行", ["反映", "結果と設定の画面"]),
    ("assets/approvals.schema.json", "一括実行", ["反映", "結果と設定の画面"]),
]
SLOW = os.environ.get("SKIP_SLOW_TESTS") == "1"
# PEP 723 / pyproject.toml の名前と、import の名前が違うライブラリ
IMPORT_NAMES = {"pyyaml": "yaml", "beautifulsoup4": "bs4", "pillow": "PIL"}


# ---------------------------------------------------------------------------
# 写しを探す
# ---------------------------------------------------------------------------

def part_folder(role):
    """役割 → コンポーネントのフォルダ（plugin_parts.py の PARTS）"""
    return pp.ROOT / pp.PARTS[role]


def role_of(folder):
    for role, place in pp.PARTS.items():
        if (pp.ROOT / place).resolve() == Path(folder).resolve():
            return role
    return None


def part_folders():
    """取り決めのファイルを持ちうる部品のフォルダ（スキルと MCP サーバー）"""
    return [pp.SKILLS_DIR / n for n in pp.skill_names()] + pp.mcp_dirs()


def kind_of(contract):
    return "script" if contract.endswith(".py") else "data"


def copies_in(folder, contract):
    """部品のフォルダの中の、取り決めのファイルの写し（ファイルの名前で探す）。
    スクリプトは scripts/ と src/ の下、データは assets/ の下（assets/contract/ を除く）。evals/ と作られたものは見ない"""
    folder = Path(folder)
    kind, name = kind_of(contract), Path(contract).name
    bases = {"script": ("scripts", "src"), "data": ("assets",)}[kind]
    found = []
    for base in bases:
        top = folder / base
        if not top.is_dir():
            continue
        for path in top.rglob(name):
            if not path.is_file() or pp.skipped(path, folder):
                continue
            inner = path.relative_to(folder).parts
            if kind == "data" and "contract" in inner:
                continue
            found.append(path)
    return sorted(found)


def owner_copy(contract, role):
    """持ち主のファイル（持ち主のフォルダの決まった場所）。なければ None"""
    path = part_folder(role) / contract
    return path if path.is_file() else None


def expected_place(folder, contract):
    """写しを置く場所（設計書 4.17）：スキルは持ち主と同じ。MCP サーバーは、スクリプトをパッケージ（src/<パッケージ>/）に置く"""
    if Path(folder).parent.resolve() != pp.MCP_DIR.resolve():
        return contract
    if kind_of(contract) == "script":
        packages = sorted(p.name for p in (Path(folder) / "src").glob("*/__init__.py") for p in [p.parent]) \
            if (Path(folder) / "src").is_dir() else []
        return f"src/{packages[0] if packages else '<パッケージ>'}/{Path(contract).name}"
    return contract


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def declared_libraries(folder):
    """写す側の部品が持つ（uv が用意する）ライブラリの import の名前。
    スキル：scripts/ の直下の .py の PEP 723 の dependencies。MCP サーバー：pyproject.toml の [project] の dependencies"""
    names = set()
    specs = []
    for path in sorted((Path(folder) / "scripts").glob("*.py")) if (Path(folder) / "scripts").is_dir() else []:
        block = re.search(r"(?ms)^# /// script\s*$(.*?)^# ///\s*$", pp.read(path))
        if block:
            body = "\n".join(line[1:].lstrip() if line.startswith("#") else line for line in block.group(1).splitlines())
            m = re.search(r"(?s)dependencies\s*=\s*\[(.*?)\]", body)
            if m:
                specs += re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
    pyproject = Path(folder) / "pyproject.toml"
    if pyproject.is_file():
        m = re.search(r"(?ms)^\[project\].*?^dependencies\s*=\s*\[(.*?)\]", pp.read(pyproject))
        if m:
            specs += re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
    for spec in specs:
        base = re.split(r"[\s<>=!~;\[]", spec.strip(), maxsplit=1)[0].lower()
        names.add(IMPORT_NAMES.get(base, base.replace("-", "_")))
    return names


def required_imports(path):
    """スクリプトが読み込む、標準ライブラリでないモジュール（try … except ImportError の中のものは除く）。
    相対 import は、(None, 文) で返す"""
    tree = ast.parse(pp.read(path))
    optional = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError") or
                isinstance(h.type, ast.Tuple) and any(getattr(e, "id", None) in ("ImportError", "ModuleNotFoundError") for e in h.type.elts)
                for h in node.handlers):
            for inner in node.body:
                optional |= {id(n) for n in ast.walk(inner)}
    out = []
    for node in ast.walk(tree):
        if id(node) in optional:
            continue
        if isinstance(node, ast.Import):
            out += [(a.name.split(".")[0], f"import {a.name}") for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.append((None, f"from {'.' * node.level}{node.module or ''} import …"))
            elif node.module:
                out.append((node.module.split(".")[0], f"from {node.module} import …"))
    return [(name, text) for name, text in out if name is None or name not in sys.stdlib_module_names]


def asset_names(path):
    """スクリプトが自分の部品の assets/ から探すファイルの名前（文の中の "….json" など）"""
    text = pp.read(path)
    if "assets" not in text:
        return set()
    return set(re.findall(r"[\"']([\w.-]+\.(?:json|ya?ml|md|txt))[\"']", text))


class CopiesTest(unittest.TestCase):
    """設計書 4.17：写しは持ち主と同じバイト列。決まった部品が写しを持ち、使う"""

    def test_owners_have_their_files(self):
        problems = []
        for contract, owner, _ in CONTRACTS:
            if owner_copy(contract, owner) is None:
                problems.append(f"持ち主（{owner}：{pp.rel(part_folder(owner))}）に {contract} がありません")
        self.assertEqual(problems, [], "取り決めの持ち主のファイルがありません（設計書 4.17）：\n" + "\n".join(problems))

    def test_every_copy_is_identical(self):
        """部品のフォルダの中の、同じ名前のファイルは、どれも持ち主のものと同じバイト列"""
        problems, checked = [], 0
        for contract, owner, _ in CONTRACTS:
            canonical = owner_copy(contract, owner)
            if canonical is None:
                continue
            for folder in part_folders():
                for path in copies_in(folder, contract):
                    if path.resolve() == canonical.resolve():
                        continue
                    checked += 1
                    if path.read_bytes() != canonical.read_bytes():
                        problems.append(f"{pp.rel(path)} が {pp.rel(canonical)}（持ち主：{owner}）と違います")
        self.assertTrue(checked, "写しがありません")
        self.assertEqual(problems, [], "取り決めの写しが、持ち主のものと違います（持ち主のファイルをそのままコピーしてください。"
                                       "元のファイルを直したら、写しも同じ内容にする）：\n" + "\n".join(problems))

    def test_holders_have_their_copies(self):
        """設計書 4.17 の「写しを持つ部品」が写しを持つ"""
        for contract, owner, holders in CONTRACTS:
            for holder in holders:
                with self.subTest(contract=contract, holder=holder):
                    folder = part_folder(holder)
                    self.assertTrue(copies_in(folder, contract),
                                    f"{holder}（{pp.rel(folder)}）に {expected_place(folder, contract)} の写しがありません"
                                    f"（持ち主：{owner}。持ち主のファイルをそのままコピーしてください）")

    def test_copies_are_listed(self):
        """同じ名前のファイルを持つ部品は、表（設計書 4.17）の持ち主か写しを持つ部品"""
        problems = []
        for contract, owner, holders in CONTRACTS:
            for folder in part_folders():
                if copies_in(folder, contract) and role_of(folder) not in [owner] + holders:
                    problems.append(f"{pp.rel(folder)}（{role_of(folder) or '表にない部品'}）が {contract} の写しを持っていますが、"
                                    "設計書 4.17 の表と、この試験の CONTRACTS にありません")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_copies_are_placed_like_the_owner(self):
        """写しは持ち主と同じ名前で、データは assets/、スクリプトは scripts/（MCP サーバーは src/ の下）に置く。
        自分の部品の assets/ を探すスクリプトは、持ち主と同じ場所（scripts/ の直下）に置き、探すファイルを写す側も持つ"""
        problems = []
        for contract, owner, _ in CONTRACTS:
            canonical = owner_copy(contract, owner)
            if canonical is None or kind_of(contract) != "script":
                continue
            wanted = asset_names(canonical)
            for folder in part_folders():
                for path in copies_in(folder, contract):
                    if path.resolve() == canonical.resolve():
                        continue
                    inner = path.relative_to(folder).as_posix()
                    if wanted and inner != contract:
                        problems.append(f"{pp.rel(path)}：assets/ のファイルを探すスクリプトなので、{contract} に置きます")
                    for name in sorted(wanted):
                        if (canonical.parents[1] / "assets" / name).is_file() and not (path.parents[1] / "assets" / name).is_file():
                            problems.append(f"{pp.rel(path)} は assets/{name} を探しますが、{pp.rel(folder)}/assets/{name} がありません")
        self.assertEqual(problems, [], "写しの置き場所が違います（設計書 4.17）：\n" + "\n".join(problems))

    def test_script_copies_are_used_not_rewritten(self):
        """写す側は、写したスクリプトを読み込んで使う（試験の外のスクリプトに、そのモジュールの名前がある）"""
        problems = []
        for contract, owner, _ in CONTRACTS:
            if kind_of(contract) != "script":
                continue
            stem = Path(contract).stem
            pattern = re.compile(r"(?<![\w.])" + re.escape(stem) + r"(?![\w])")
            for folder in part_folders():
                copies = copies_in(folder, contract)
                if not copies or role_of(folder) == owner:
                    continue
                users = [p for p in sorted(folder.rglob("*.py")) if not pp.skipped(p, folder) and p not in copies
                         and p.relative_to(folder).parts[0] not in ("tests", "evals") and pattern.search(pp.read(p))]
                if not users:
                    problems.append(f"{pp.rel(copies[0])} を、{pp.rel(folder)} のスクリプトが読み込んでいません（写しを使い、自分で書き直さない）")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_script_copies_need_only_what_the_holder_has(self):
        """持ち主のスクリプトは、標準ライブラリか、写す側も持つライブラリだけを使い、ほかのファイルを読み込まない
        （表のほかのスクリプトを読み込むときは、写す側もそれを持つ）"""
        problems = []
        scripts = {Path(c).stem: c for c, _, _ in CONTRACTS if kind_of(c) == "script"}
        for contract, owner, _ in CONTRACTS:
            canonical = owner_copy(contract, owner)
            if canonical is None or kind_of(contract) != "script":
                continue
            for folder in part_folders():
                if not copies_in(folder, contract):
                    continue
                have = declared_libraries(folder)
                for name, text in required_imports(canonical):
                    if name is None:
                        problems.append(f"{pp.rel(canonical)}：相対の読み込み（{text}）があります。写しは1つのファイルだけで動くようにします")
                    elif name in scripts:
                        if not copies_in(folder, scripts[name]):
                            problems.append(f"{pp.rel(folder)}：{Path(contract).name} が読み込む {scripts[name]} の写しがありません")
                    elif name not in have:
                        problems.append(f"{pp.rel(folder)}：{Path(contract).name} が読み込む {name}（{text}）を、"
                                        "この部品の依存（スクリプトの PEP 723 か pyproject.toml）に書いていません")
        self.assertEqual(problems, [], "\n".join(problems))


ENTRY_KEYS = ("id", "cids", "stream", "classification", "action", "severity", "comment")


def token_entry(entry):
    """確認の文字列に入れる項目の形：取り決めの項目だけ。stream がない（null の）ときは鍵を入れない。
    severity と comment がないときは空の文字列（設計書 4.10）"""
    out = {k: entry[k] for k in ENTRY_KEYS if k in entry and entry[k] is not None}
    out.setdefault("severity", "")
    out.setdefault("comment", "")
    return out


def token_of(entries):
    entries = [token_entry(e) for e in entries]
    data = json.dumps({"triage": entries}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:12]


class SchemaContentTest(unittest.TestCase):
    """取り決めの中身（設計書 4.5〜4.7）"""

    def load(self, contract, role):
        path = owner_copy(contract, role)
        if path is None:
            for folder in part_folders():
                found = copies_in(folder, contract)
                if found:
                    path = found[0]
                    break
        self.assertIsNotNone(path, f"{contract} がありません")
        return json.loads(pp.read(path))

    def test_plan_schema_embeds_the_result_definitions(self):
        result = self.load("assets/result.schema.json", "結果の記録")
        plan = self.load("assets/plan.schema.json", "同じ原因の統合")
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
        data = self.load("assets/criteria.json", "結果の確かめ")
        criteria = data.get("criteria", [])
        ids = [c.get("id") for c in criteria]
        self.assertEqual(ids, [f"C-{n:02d}" for n in range(1, 29)])
        run = {c["id"] for c in criteria if c.get("scope") == "run"}
        self.assertEqual(run, {f"C-{n:02d}" for n in [16, 17, 18, 19, 24, 25, 26, 27]})
        self.assertEqual(sum(1 for c in criteria if c.get("scope") == "item"), 20)
        for c in criteria:
            self.assertTrue(str(c.get("text", "")).strip(), f"{c.get('id')} の文が空です")

    def test_review_schema_checks_are_item_criteria(self):
        review = self.load("assets/review.schema.json", "結果の確かめ")
        criteria = self.load("assets/criteria.json", "結果の確かめ")
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


# ---------------------------------------------------------------------------
# Claude Code と Copilot CLI のエージェントの定義
# ---------------------------------------------------------------------------

# 本文のツールの言葉（Claude Code の言葉, Copilot CLI の言葉）。片方の定義に、もう片方の言葉を書かない
TOOL_WORDS = [("Skill ツール", "skill ツール"), ("Agent ツール", "`task`"), ("AskUserQuestion", "ask_user"), ("Bash", "シェル")]
# frontmatter の tools の読み替え（Copilot CLI → Claude Code）
COPILOT_TO_CLAUDE = {
    "read": {"Read", "Grep", "Glob"}, "search": {"Grep", "Glob"}, "edit": {"Edit", "Write"}, "execute": {"Bash"},
    "shell": {"Bash"}, "skill": {"Skill"}, "ask_user": {"AskUserQuestion"}, "agent": {"Agent"}, "task": {"Agent"},
}
FENCE = re.compile(r"(?ms)^[ \t]*```[^\n]*\n.*?^[ \t]*```[ \t]*$")


def word_count(word, text):
    pattern = re.compile(re.escape(word)) if not re.fullmatch(r"\w+", word) else pp.token(word, ignore_case=False)
    return len(pattern.findall(text))


def headings(body):
    return re.findall(r"(?m)^## (.+?)\s*$", pp.strip_code_blocks(body))


class ClaudeCopilotParityTest(unittest.TestCase):
    """Claude Code のエージェント（agents/<名前>.md）と Copilot CLI のエージェント（com.github.copilot/agents/<名前>.agent.md）が、
    同じ仕事を同じ部品で、同じ形の報告で行う。違うのはツールの言葉だけ"""

    def setUp(self):
        self.names = pp.agent_names()
        self.assertTrue(self.names and pp.CLAUDE_AGENTS_DIR.is_dir(), "どちらかのエージェントの定義がありません")

    def pair(self, name):
        claude = pp.frontmatter(pp.claude_agent_path(name))
        copilot = pp.frontmatter(pp.agent_path(name))
        self.assertIsNone(claude[2], f"agents/{name}.md: {claude[2]}")
        self.assertIsNone(copilot[2], f"{name}{pp.AGENT_SUFFIX}: {copilot[2]}")
        return claude, copilot

    def test_same_agent_names(self):
        self.assertEqual(sorted(pp.claude_agent_names()), sorted(self.names), "2つの形のエージェントの名前が違います")

    def test_same_parts_table(self):
        for name in self.names:
            with self.subTest(agent=name):
                (_, a, _), (_, b, _) = self.pair(name)
                table_a, _ = pp.split_section_table(a, pp.PARTS_HEADING)
                table_b, _ = pp.split_section_table(b, pp.PARTS_HEADING)
                self.assertIsNotNone(table_a, f"agents/{name}.md に「## {pp.PARTS_HEADING}」がありません")
                self.assertEqual(table_a, table_b, f"{name}: 「## {pp.PARTS_HEADING}」の表が、2つの形で違います")

    def test_same_headings_in_the_same_order(self):
        for name in self.names:
            with self.subTest(agent=name):
                (_, a, _), (_, b, _) = self.pair(name)
                self.assertEqual(headings(a), headings(b), f"{name}: 「## 」の見出しが、2つの形で違います")

    def test_same_report_templates(self):
        for name in self.names:
            with self.subTest(agent=name):
                (_, a, _), (_, b, _) = self.pair(name)
                self.assertEqual([m.strip() for m in FENCE.findall(a)], [m.strip() for m in FENCE.findall(b)],
                                 f"{name}: 報告の形（``` で囲んだもの）が、2つの形で違います")

    def test_tool_words_are_mapped(self):
        """本文のツールの言葉：Claude Code は Skill・Agent・AskUserQuestion・Bash、Copilot CLI は skill・task・ask_user・シェル。
        同じ数だけ出てきて、もう片方の言葉はない"""
        for name in self.names:
            (_, a, _), (_, b, _) = self.pair(name)
            for claude_word, copilot_word in TOOL_WORDS:
                with self.subTest(agent=name, word=claude_word):
                    self.assertEqual(word_count(copilot_word, a), 0, f"agents/{name}.md に Copilot CLI の言葉「{copilot_word}」があります")
                    self.assertEqual(word_count(claude_word, b), 0, f"{name}{pp.AGENT_SUFFIX} に Claude Code の言葉「{claude_word}」があります")
                    self.assertEqual(word_count(claude_word, a), word_count(copilot_word, b),
                                     f"{name}: 「{claude_word}」と「{copilot_word}」の数が違います")

    def test_tools_are_mapped(self):
        """frontmatter の tools：Copilot CLI のツールを Claude Code の名前に読み替えたものと同じ。
        MCP の <登録名>/<ツール> は、Claude Code の2つの名前の形（プラグインのサーバー、手で登録したサーバー）"""
        for name in self.names:
            (fa, _, _), (fb, _, _) = self.pair(name)
            want = set()
            for tool in pp.tool_list(fb.get("tools")):
                if isinstance(tool, str) and "/" in tool:
                    want |= set(pp.claude_mcp_tool_names(*tool.split("/", 1)))
                else:
                    want |= COPILOT_TO_CLAUDE.get(tool, {f"（読み替えのない {tool}）"})
            with self.subTest(agent=name):
                self.assertEqual("tools" in fa, "tools" in fb, f"{name}: tools の行が、片方の形にだけあります")
                self.assertEqual(sorted(set(pp.tool_list(fa.get("tools")))), sorted(want),
                                 f"{name}: Claude Code の tools が、Copilot CLI の tools を読み替えたものと違います")

    def write_back(self):
        """書き戻しのツールの、Claude Code の2つの名前の形。見つからなければ失敗にする
        （見つけ方が古くなったまま、守りが黙って外れないように）"""
        tools = set()
        for server, names in pp.write_back_tools().items():
            for tool in names:
                tools |= set(pp.claude_mcp_tool_names(server, tool))
        self.assertTrue(tools, "書き戻しのツール（destructiveHint=True）が MCP サーバーに見つかりません。"
                               "ツールの登録の書き方を変えたなら、plugin_parts._destructive_tools も直してください")
        return tools

    def test_orchestrator_cannot_edit_or_write_back(self):
        """Claude Code の統括エージェントは tools を書かず、disallowedTools で Edit・Write と、書き戻しのツール（2つの名前の形）を外す"""
        fm, _, problem = pp.frontmatter(pp.claude_agent_path(pp.ORCHESTRATOR))
        self.assertIsNone(problem, problem)
        self.assertNotIn("tools", fm, "統括エージェントに tools があります（呼んだエージェントのツールが絞られる）")
        disallowed = set(pp.tool_list(fm.get("disallowedTools")))
        must = {"Edit", "Write"} | self.write_back()
        self.assertEqual(sorted(must - disallowed), [], "統括エージェントの disallowedTools に足りないものがあります")

    def test_mcp_tools_follow_the_table(self):
        """Claude Code のエージェントの MCP のツールは、tools でも disallowedTools でも、表の MCP サーバーのものだけ
        （設計書 9.1）。2つの名前の形をどちらも書く。書き戻しのツールは、どのエージェントの tools にも入れない（Copilot CLI の形も）"""
        write_back = self.write_back()
        plain = {t for names in pp.write_back_tools().values() for t in names}
        for name in self.names:
            (fa, a, _), (fb, _, _) = self.pair(name)
            rows, _ = pp.parts_table(a)
            table = {row[1] for row in rows or []}
            for key in ("tools", "disallowedTools"):
                forms = {}
                for tool in pp.tool_list(fa.get(key)):
                    parsed = pp.parse_claude_mcp_tool(tool)
                    if parsed is None:
                        continue
                    server, tool_name, form = parsed
                    forms.setdefault((server, tool_name), set()).add(form)
                    with self.subTest(agent=name, key=key, tool=tool):
                        self.assertIn(server, table, f"{name}: {key} の {tool} の MCP サーバーが「使う部品」の表にありません")
                        if key == "tools":
                            self.assertNotIn(tool, write_back, f"{name}: tools に書き戻しのツール {tool} があります")
                for (server, tool_name), seen in sorted(forms.items()):
                    with self.subTest(agent=name, key=key, tool=f"{server}/{tool_name}"):
                        self.assertEqual(seen, {"plugin", "hand"}, f"{name}: {key} の {tool_name} を2つの名前の形で書いていません")
            for tool in pp.tool_list(fb.get("tools")):
                with self.subTest(agent=name, copilot_tool=tool):
                    self.assertNotIn(str(tool).split("/", 1)[-1] if "/" in str(tool) else None, plain,
                                     f"{name}{pp.AGENT_SUFFIX}: tools に書き戻しのツール {tool} があります")


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
        self.assertTrue(servers, "mcp.json に MCP サーバーがありません")
        name, server = next(iter(servers.items()))
        folder = pp.mcp_registrations().get(name)
        self.assertTrue(folder and (folder / "pyproject.toml").is_file(), "MCP サーバーのフォルダがありません")
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
        """MCP サーバーの確認の文字列は、反映の計画のファイルのバイト列から決まる（設計書 4.10）"""
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
            self.assertEqual(got, token_of([entry]), "entries の確認の文字列が違います")
        finally:
            client.close()

    def test_investigation_reads_the_details_written_by_the_mcp_server(self):
        """MCP サーバーが output_dir に書いた issue-<CID>.json を、真偽の調査のスクリプトが読み、★ の行を出す（設計書 4.4、4.17）"""
        self.need()
        folder = part_folder("真偽の調査")
        scripts = sorted(pp.entry_scripts(folder))
        self.assertEqual(len(scripts), 1, "真偽の調査のスクリプト（scripts/ の直下の .py が1つ）がありません")
        repo = self.make_repo()
        out = Path(tempfile.mkdtemp(prefix="details-", dir=self.tmp))
        client = self.client(repo)
        try:
            client.call("coverity_get_issue_details", {"repo_root": str(repo), "stream": "app-main", "cids": [20001],
                                                       "output_dir": str(out)})
        finally:
            client.close()
        issue_file = out / "issue-20001.json"
        self.check_detail_file(issue_file)
        uv = shutil.which("uv")
        done = subprocess.run([uv, "run", "--native-tls", "--quiet", str(folder / "scripts" / scripts[0]), "show", "--repo", str(repo),
                               "--issue-file", str(issue_file)], cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"), timeout=180)
        try:
            data = json.loads(done.stdout.decode("utf-8"))
        except ValueError:
            self.fail(f"真偽の調査のスクリプトの出力が JSON ではありません：{done.stdout[-800:]!r} {done.stderr[-800:]!r}")
        self.assertTrue(data.get("ok"), f"真偽の調査のスクリプトが、MCP サーバーの書いたファイルを読めません：{json.dumps(data, ensure_ascii=False)[:1200]}")
        text = data.get("text", "")
        self.assertIn("\u2605", text, "★（問題の起きる行）がありません")
        self.assertIn("strlen(line)", text, "警告の行のコードがありません")

    def run_command(self):
        """一括実行のコマンド"""
        self.need()
        if shutil.which("git") is None:
            self.skipTest("git がありません")
        return pp.RUN_SCRIPT

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
            schema = owner_copy("assets/run.schema.json", "一括実行")
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
