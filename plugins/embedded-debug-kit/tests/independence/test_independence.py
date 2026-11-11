"""部品の独立性と、定義の書き方を確かめるテスト。

- 1段階目のスキルは、そのフォルダだけで完結する（REQ-108）。
- 上の段階は、下の段階の中身を持たない（REQ-002、REQ-003、REQ-006、REQ-202、REQ-304、REQ-409）。
- 両方の CLI のエージェントの本文が同じ。Copilot CLI の frontmatter が Claude Code 版と合う（REQ-004）。
- Copilot CLI の統括エージェントは、作業・検証エージェントのほかに依頼できない（REQ-303）。
- 定義と使い方の文書は、最後の節が「してはいけないこと」（REQ-007）。
- 名前は、ほかとぶつかりにくい形（REQ-008）。

部品の名前は書かない。作業エージェントの定義の skills と、統括エージェントの定義の tools から読む。

  python3 -m unittest discover -s tests/independence -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
CLAUDE_AGENTS = ROOT / "com.anthropic.claude-code" / "agents"
COPILOT_AGENTS = ROOT / "com.github.copilot" / "agents"
GUIDES = ROOT / "docs" / "guide"
TEXT = {".md", ".py", ".yaml", ".txt", ".json", ".js", ".css", ".html"}
LAST = "## してはいけないこと"
DASHBOARD_WORDS = ("Web管理画面", "管理画面", "ダッシュボード", "dashboard")
DEVICE_WORDS = ("FTP", "シリアル", "serial", "クロス", "cross", "ftp")


def read(path):
    return path.read_text(encoding="utf-8")


def split_front(path):
    """先頭の --- で囲まれた部分を、キーと値の辞書（値は文字列か、文字列のリスト）と本文に分ける"""
    text = read(path)
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m:
        return {}, text
    front, key = {}, None
    for line in m.group(1).splitlines():
        item = re.match(r"\s+-\s+(.*)", line)
        if item and key:
            front[key] = (front[key] or []) + [item.group(1).strip()]
            continue
        kv = re.match(r"([A-Za-z_-]+):\s*(.*)", line)
        if kv:
            key = kv.group(1)
            front[key] = kv.group(2).strip() or None
    return front, m.group(2)


def text_files(folder, skip=()):
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_file() and path.suffix in TEXT and "__pycache__" not in rel and not rel.startswith(skip):
            yield path


def short(name):
    """プラグインの名前を前に付けた名前（plugin:agent）から、エージェントの名前を取り出す"""
    return name.split(":")[-1].strip()


def find_parts():
    """エージェントの定義から、各部品の名前を読む"""
    agents = {p.stem: split_front(p) for p in sorted(CLAUDE_AGENTS.glob("*.md"))}
    worker = next(n for n, (f, _) in agents.items() if f.get("skills"))
    orchestrator = next(n for n, (f, _) in agents.items() if "Agent(" in (f.get("tools") or ""))
    called = [short(n) for n in re.search(r"Agent\(([^)]*)\)", agents[orchestrator][0]["tools"]).group(1).split(",")]
    verifier = next(n for n in called if n != worker)
    stage1 = list(agents[worker][0]["skills"])
    others = sorted(p.name for p in SKILLS.iterdir() if p.is_dir() and p.name not in stage1)
    return type("Parts", (), dict(agents=agents, worker=worker, orchestrator=orchestrator,
                                  verifier=verifier, stage1=stage1, others=others))


Parts = find_parts()


class StageOneTest(unittest.TestCase):
    def test_worker_skills_exist(self):
        for name in Parts.stage1:
            self.assertTrue((SKILLS / name / "SKILL.md").is_file(), name)

    def test_skill_files_name_no_other_part(self):
        names = set(Parts.stage1) | set(Parts.others) | set(Parts.agents) | {ROOT.name}
        problems = []
        for skill in Parts.stage1:
            words = (names - {skill}) | {"CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"}
            for path in text_files(SKILLS / skill, skip=("scripts/vendor/",)):
                text = read(path)
                problems += [f"{path.relative_to(ROOT)}：{w}" for w in sorted(words) if w in text]
        self.assertEqual(problems, [], "1段階目のスキルが、ほかの部品の名前を書いています")

    def test_skill_tests_pass_from_a_lone_copy(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for skill in Parts.stage1:
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / skill
                shutil.copytree(SKILLS / skill, copy, ignore=shutil.ignore_patterns("__pycache__"))
                run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                     cwd=str(copy), env=env, capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(run.returncode, 0, run.stderr[-3000:])


class SettingsTest(unittest.TestCase):
    """設定ファイル（YAML）の読み取りは、スキルどうしが互いを読まないので、スキルごとに同じ中身を持つ"""

    BLOCK = re.compile(r"(?ms)^# --- 設定ファイル（YAML）を読む。.*?^# --- 設定ファイル（YAML）を読む：ここまで")

    def blocks(self):
        found = {}
        for path in sorted(SKILLS.glob("*/scripts/*.py")):
            m = self.BLOCK.search(read(path))
            if m:
                found[path.parent.parent.name] = m.group(0)
        return found

    def test_every_skill_reads_settings_with_the_same_code(self):
        blocks = self.blocks()
        for template in sorted(SKILLS.glob("*/assets/config-template.yaml")):
            self.assertIn(template.parent.parent.name, blocks, "設定を持つスキルに、読み取りがありません")
        self.assertGreater(len(blocks), 1)
        self.assertEqual(len(set(blocks.values())), 1, f"読み取りの中身が、スキルで違います：{sorted(blocks)}")

    def test_templates_read_the_same_with_pyyaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML がありません（skills/skill-config/tests/requirements.txt）")
        space = {"re": re}
        exec(next(iter(self.blocks().values())), space)
        templates = sorted(SKILLS.glob("*/assets/config-template.yaml"))
        self.assertTrue(templates)
        for template in templates:
            text = read(template)
            ours = {key: value for key, value, _, _ in space["read_yaml"](text.split("\n"))}
            self.assertEqual(ours, yaml.load(text, Loader=yaml.BaseLoader), template.relative_to(ROOT))


class UpperStageTest(unittest.TestCase):
    def test_worker_holds_skill_names_only(self):
        front, body = Parts.agents[Parts.worker]
        for word in (".py", "config.yaml", "scripts/", "--workdir"):
            self.assertNotIn(word, body, f"作業エージェントの定義が、スキルの中身（{word}）を書いています")
        for name in Parts.stage1:
            self.assertNotIn(f".{name}/", body)

    def test_orchestrator_and_verifier_hold_nothing_of_devices(self):
        for name in (Parts.orchestrator, Parts.verifier):
            text = read(CLAUDE_AGENTS / f"{name}.md")
            found = [w for w in list(Parts.stage1) + list(DEVICE_WORDS) if w in text]
            self.assertEqual(found, [], f"{name} が、機器やスキルのことを書いています")

    def test_verifier_does_not_name_other_agents(self):
        text = read(CLAUDE_AGENTS / f"{Parts.verifier}.md")
        self.assertNotIn(Parts.worker, text)
        self.assertNotIn(Parts.orchestrator, text)

    def test_agents_do_not_know_the_dashboard(self):
        for name in Parts.agents:
            text = read(CLAUDE_AGENTS / f"{name}.md")
            found = [w for w in DASHBOARD_WORDS + tuple(Parts.others) if w in text]
            self.assertEqual(found, [], f"{name} が Web管理画面のことを書いています")

    def test_dashboard_holds_no_lower_parts(self):
        # 設定専用のスキルだけは、Web管理画面が使ってよい（憲章 第25条4項）
        words = [w for w in list(Parts.stage1) + [Parts.worker] if w != "skill-config"]
        for skill in Parts.others:
            for path in text_files(SKILLS / skill):
                found = [w for w in words if w in read(path)]
                self.assertEqual(found, [], f"{path.relative_to(ROOT)} が下の段階の部品の名前を書いています")

    def test_guides_of_upper_stages_hold_no_skill_contents(self):
        for path in sorted(GUIDES.glob("[234]-*.md")):
            text = read(path)
            for word in (".py", "config.yaml", "--workdir"):
                self.assertNotIn(word, text, f"{path.name} がスキルの中身（{word}）を書いています")
            if not path.name.startswith("2-"):
                found = [w for w in Parts.stage1 if w in text]
                self.assertEqual(found, [], f"{path.name} が1段階目のスキルの名前を書いています")


def copilot_front(name):
    """Copilot CLI のエージェントの frontmatter（tools は JSON の配列として読む）"""
    front = split_front(COPILOT_AGENTS / f"{name}.agent.md")[0]
    return dict(front, tools=json.loads(front.get("tools") or "[]"))


def block_values(path):
    """frontmatter の「<キー>: |」の複数行の値を、キーごとに字下げを除いて返す"""
    lines = re.match(r"---\n(.*?)\n---\n", read(path), re.S).group(1).splitlines()
    found, i = {}, 0
    while i < len(lines):
        m = re.match(r"(\s*)([A-Za-z_-]+): \|$", lines[i])
        i += 1
        if m:
            start = i
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > len(m.group(1))):
                i += 1
            found[m.group(2)] = textwrap.dedent("\n".join(lines[start:i])) + "\n"
    return found


def hook_shells():
    """Copilot CLI がフックを動かすのと同じ形のシェル。bash の欄は bash、powershell の欄は PowerShell"""
    bash = shutil.which("bash")
    found = [("bash", [bash, "--norc", "--noprofile", "-c"])] if bash and "system32" not in bash.lower() else []
    found += [("powershell", [ps, "-NoProfile", "-NoLogo", "-Command"])
              for ps in (shutil.which("pwsh"), shutil.which("powershell")) if ps]
    return found


class BothCliTest(unittest.TestCase):
    # Copilot CLI の tools に書けるもの。知らない名前は何も言わずに無視されるので、書き違いをここで見つける
    COPILOT_TOOLS = {"agent", "read", "search", "execute", "edit", "skill", "ask_user"}

    def test_copilot_agents_have_the_same_body(self):
        for name, (_, body) in Parts.agents.items():
            copilot = COPILOT_AGENTS / f"{name}.agent.md"
            self.assertTrue(copilot.is_file(), f"{copilot.relative_to(ROOT)} がありません")
            self.assertEqual(split_front(copilot)[1], body, f"{name} の本文が、Claude Code と Copilot CLI で違います")

    def test_copilot_frontmatter(self):
        for name in Parts.agents:
            front = copilot_front(name)
            self.assertEqual(front.get("name"), name)
            self.assertLessEqual(set(front["tools"]), self.COPILOT_TOOLS, f"{name} の tools に、知らない名前があります")
        self.assertEqual(copilot_front(Parts.worker).get("skills"), Parts.agents[Parts.worker][0]["skills"],
                         "作業エージェントの skills が、Claude Code と Copilot CLI で違います")
        orchestrator = copilot_front(Parts.orchestrator)
        self.assertIn("agent", orchestrator["tools"])
        self.assertFalse({"execute", "edit"} & set(orchestrator["tools"]), "統括エージェントが、コマンドかファイルの編集を使えます")
        self.assertEqual(orchestrator.get("disable-model-invocation"), "true")
        verifier = copilot_front(Parts.verifier)
        self.assertEqual(set(verifier["tools"]), {"read", "search"})
        self.assertEqual(verifier.get("user-invocable"), "false")


class CopilotOrchestratorHookTest(unittest.TestCase):
    """Copilot CLI の統括エージェントの定義のフックは、作業エージェントと検証エージェントのほかへの依頼を断る（REQ-303）。
    Copilot CLI は、入力を空白のない JSON で標準入力に渡し、PreToolUse のフックが 0 以外で終わると呼び出しを断る"""

    PATH = COPILOT_AGENTS / f"{Parts.orchestrator}.agent.md"
    PLUGIN = json.loads(read(ROOT / "plugin.json"))["name"]

    def run_hook(self, field, prefix, agent_type, prompt="試行 1 です。"):
        payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "timestamp": "2026-11-09T19:48:50.572Z",
                   "cwd": str(ROOT), "tool_use_id": "call_1", "tool_name": "Agent",
                   "tool_input": {"name": "t1", "description": "d", "agent_type": agent_type, "prompt": prompt, "mode": "sync"}}
        env = dict(os.environ)
        env.pop("PSModulePath", None)  # pwsh から受け継いだものを Windows PowerShell 5.1 に渡さない
        env.pop("PSMODULEPATH", None)
        return subprocess.run(prefix + [block_values(self.PATH)[field]], cwd=str(ROOT), env=env, timeout=60,
                              input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_hook_is_declared_for_agent_calls(self):
        front = re.match(r"---\n(.*?)\n---\n", read(self.PATH), re.S).group(1)
        self.assertRegex(front, r"(?m)^hooks:\n  PreToolUse:\n    - type: command\n      matcher: Agent\n")
        self.assertEqual(sorted(block_values(self.PATH)), ["bash", "powershell"])

    def test_allows_only_the_worker_and_the_verifier(self):
        shells = hook_shells()
        if not shells:
            self.skipTest("bash も PowerShell もありません")
        allowed = [f"{self.PLUGIN}:{Parts.worker}", f"{self.PLUGIN}:{Parts.verifier}"]
        hidden = '"agent_type":"%s"' % allowed[0]  # 依頼文の中に書かれた名前では通さない
        denied = [("general-purpose", "試行 1"), ("task", "試行 1"), (Parts.worker, "試行 1"), ("general-purpose", hidden)]
        for field, prefix in shells:
            for agent_type in allowed:
                with self.subTest(shell=prefix[0], agent_type=agent_type):
                    run = self.run_hook(field, prefix, agent_type, "直してください。ログに「完了」と出ること")
                    self.assertEqual((run.returncode, run.stdout.strip()), (0, b""), run.stderr[-2000:])
            for agent_type, prompt in denied:
                with self.subTest(shell=prefix[0], agent_type=agent_type, prompt=prompt):
                    run = self.run_hook(field, prefix, agent_type, prompt)
                    self.assertEqual(run.returncode, 0, run.stderr[-2000:])
                    decision = json.loads(run.stdout.decode("ascii"))
                    self.assertEqual(decision["permissionDecision"], "deny")
                    for name in allowed:
                        self.assertIn(name, decision["permissionDecisionReason"])


class WritingTest(unittest.TestCase):
    def files(self):
        yield from sorted(CLAUDE_AGENTS.glob("*.md"))
        yield from sorted(COPILOT_AGENTS.glob("*.agent.md"))
        yield from sorted(SKILLS.glob("*/SKILL.md"))
        yield from sorted(GUIDES.glob("*.md"))

    def test_last_section_is_prohibitions(self):
        problems = []
        for path in self.files():
            heads = re.findall(r"(?m)^## .*$", re.sub(r"(?ms)^```.*?^```", "", read(path)))
            if not heads or heads[-1].strip() != LAST or heads.count(LAST) != 1:
                problems.append(str(path.relative_to(ROOT)))
        self.assertEqual(problems, [], f"最後の節が「{LAST[3:]}」でないファイルがあります")

    def test_reference_files_keep_prohibitions_last(self):
        for path in sorted(SKILLS.glob("*/reference/*.md")):
            heads = re.findall(r"(?m)^## .*$", read(path))
            if LAST in heads:
                self.assertEqual(heads[-1], LAST, path.relative_to(ROOT))

    def test_names_do_not_collide_easily(self):
        names = list(Parts.agents) + [p.name for p in SKILLS.iterdir() if p.is_dir()]
        for name in names:
            self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)+$", "名前は、2語以上をハイフンでつなぐ")
        for path in SKILLS.glob("*/SKILL.md"):
            self.assertEqual(split_front(path)[0].get("name"), path.parent.name, path.relative_to(ROOT))
        for name, (front, _) in Parts.agents.items():
            self.assertEqual(front.get("name"), name)


if __name__ == "__main__":
    unittest.main()
