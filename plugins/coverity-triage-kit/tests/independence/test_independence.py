"""段階の独立性を確認するテスト（仕様 REQ-001、REQ-003、REQ-101、REQ-202）。

- 1段階目のコンポーネント（スキルと MCP サーバー）：フォルダの中に、上の段階の言葉、エージェントの名前、
  フォルダの外を指すパスがない。sys.path でフォルダの外を読まない。tests/ と evals/ をフォルダの中に持つ。
- エージェント（Copilot CLI の .agent.md と、Claude Code の agents/<名前>.md の両方）：自分の名前を書かない。
  tools の MCP のツールは、「## 使う部品」の表の MCP サーバーのものだけ。4段階目の言葉を書かない。
  2段階目の作業エージェントは、3段階目の言葉と、統括・検証・統合のエージェントの名前を書かない。
- 一括実行が書く指示ファイル：作業・検証・統合の3つの役割に渡され、実行フォルダかリポジトリの中だけを指す。
  一時フォルダのリポジトリで、取得済みの警告の一覧から実行を作り、決まった動作の偽のエージェントで最後まで流して確認する。

  python3 -m unittest discover -s tests/independence -v
"""

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS / "structure"))
import plugin_parts as pp  # noqa: E402


# 1段階目のコンポーネントが書いてはいけない、上の段階の言葉（英語は単語として、大文字と小文字を区別しない）
UPPER_WORDS_JA = ["Web管理画面", "エクステンション", "統括エージェント"]
UPPER_WORDS_EN = ["extension", "extensions", "orchestrator", "orchestrators"]
# 2段階目の作業役が書いてはいけない、3段階目の言葉
STAGE3_WORDS_JA = ["統括", "検証エージェント", "統合エージェント"]


def upper_patterns():
    """上の段階の言葉と、エージェントの名前"""
    return ([(w, re.compile(re.escape(w))) for w in UPPER_WORDS_JA] + [(w, pp.token(w)) for w in UPPER_WORDS_EN]
            + [(n, pp.name_token(n)) for n in (pp.ORCHESTRATOR, *pp.ROLES.values())])


def outside_patterns():
    """フォルダの外を指すもの。../ は、設定の output_dir の既定の値そのもの（設計書 4.1 の `../coverity-triage-out` など）と、
    パスを調べるための文字列 "../" だけは許す（../coverity-triage-outside や ../other は数える）"""
    allowed = pp.output_dir_default() or ""
    tail = re.escape(allowed[3:]) + r"(?![\w.\-])" if allowed.startswith("../") else None
    parent = r"(?<!\.)\.\.[/\\](?![\"']|\\[\"'])" + (r"(?!" + tail + r")" if tail else "")
    return [("../（フォルダの外）", re.compile(parent)),
            ("プラグインの根の環境変数", re.compile(r"PLUGIN_ROOT|CLAUDE_PLUGIN|COPILOT_PLUGIN|PLUGIN_DATA")),
            ("plugin.json", re.compile(r"plugin\.json")),
            ("com.github.copilot", re.compile(r"com\.github\.copilot")),
            ("プラグインの tests/", re.compile(r"(?<![\w.])tests/(independence|contracts|structure|integration)\b"))]


# 部品の試験が「書いてはいけない言葉」として並べる、言葉だけの文字列（"../"、"extension" など）は数えない
LISTED_WORD = re.compile(r"([\"'])(?:\.\.(?:/|\\\\)?|\$\{PLUGIN_ROOT\}|PLUGIN_ROOT|PLUGIN_DATA|plugin\.json|com\.github\.copilot|"
                         + "|".join(re.escape(w) for w in UPPER_WORDS_JA + UPPER_WORDS_EN) + r")\1", re.I)


def without_listed_words(path, text):
    return LISTED_WORD.sub("''", text) if path.suffix in (".py", ".mjs", ".js") else text


def outside_problems(folder):
    """フォルダの外を指す書き方。試験（tests/）と共通の試験のデータ（assets/contract/）は、外のパスを入力として
    使うことがあるので見ない"""
    problems = []
    for path in pp.text_files(folder):
        parts = path.relative_to(folder).parts
        if parts[0] == "tests" or parts[:2] == ("assets", "contract"):
            continue
        problems += pp.hits_in_text(pp.rel(path), without_listed_words(path, pp.read(path)), outside_patterns())
    return problems + sys_path_problems(folder)


def sys_path_problems(folder):
    problems = []
    for path in pp.text_files(folder):
        if path.suffix != ".py":
            continue
        for lineno, line in enumerate(pp.read(path).splitlines(), 1):
            if re.search(r"sys\.path\.(insert|append|extend)\(", line) and (
                    re.search(r"\.\.|parents\[[2-9]\]|\.parent\.parent\.parent", line)):
                problems.append(f"{pp.rel(path)}:{lineno}: sys.path にフォルダの外を足しています … {line.strip()[:120]}")
    return problems


def stage1_parts():
    """1段階目のコンポーネントのフォルダ（スキルと MCP サーバー）"""
    return [pp.SKILLS_DIR / n for n in pp.skill_names()] + pp.mcp_dirs()


KINDS = ("copilot", "claude")


def kind_path(name, kind="copilot"):
    return pp.claude_agent_path(name) if kind == "claude" else pp.agent_path(name)


def kind_names(kind="copilot"):
    return pp.claude_agent_names() if kind == "claude" else pp.agent_names()


def scan_text(path, kind="copilot"):
    """名前を探す文。Claude Code の定義は、frontmatter の name・tools・disallowedTools の行を空にする（行の番号は変えない）"""
    text = pp.read(path)
    return pp.without_frontmatter_keys(text) if kind == "claude" else text


def mcp_tool_problems(name, kind, fm, table, known):
    """frontmatter の tools・disallowedTools の MCP のツールで、表にない MCP サーバーのものと、サーバーにないツール。
    Claude Code は mcp__… の名前（2つの形）、Copilot CLI は <登録名>/<ツール>"""
    problems = []
    for key in ("tools", "disallowedTools"):
        for tool in pp.tool_list(fm.get(key)):
            if kind == "claude":
                parsed = pp.parse_claude_mcp_tool(tool)
                if parsed is None:
                    continue
                server, tool_name, _ = parsed
            elif isinstance(tool, str) and "/" in tool:
                server, tool_name = tool.split("/", 1)
            else:
                continue
            if server not in table:
                problems.append(f"{name}: {key} の {tool} の MCP サーバーが「使う部品」の表にありません")
            elif known and tool_name != "*" and tool_name not in known:
                problems.append(f"{name}: {key} の {tool} は、MCP サーバーにないツールです")
    return problems


class Stage1Independence(unittest.TestCase):
    """1段階目：スキルと MCP サーバーは、それぞれのフォルダだけで動作し、上の段階を知らない"""

    def setUp(self):
        self.parts = stage1_parts()
        self.assertTrue(self.parts, "1段階目のコンポーネント（スキル、MCP サーバー）がありません")

    def test_no_upper_stage_words(self):
        """下の段階は、上の段階のことを書かない"""
        problems = []
        for folder in self.parts:
            for path in pp.text_files(folder):
                problems += pp.hits_in_text(pp.rel(path), without_listed_words(path, pp.read(path)), upper_patterns())
        self.assertEqual(problems, [], "1段階目のコンポーネントが、上の段階のことを書いています：\n" + "\n".join(problems))

    def test_no_path_outside_the_folder(self):
        problems = []
        for folder in self.parts:
            problems += outside_problems(folder)
        self.assertEqual(problems, [], "1段階目の部品が、フォルダの外を指しています：\n" + "\n".join(problems))

    def test_tests_and_evals_inside(self):
        """tests/・evals/ がフォルダの中にある（仕様 REQ-101）"""
        for folder in self.parts:
            with self.subTest(part=pp.rel(folder)):
                self.assertTrue(any((folder / "tests").rglob("test*.py")), "tests/ に試験がありません")
                self.assertTrue((folder / "evals").is_dir(), "evals/ がありません")


class AgentIndependence(unittest.TestCase):
    """エージェントの定義（Copilot CLI の定義と Claude Code の定義の両方）"""

    def setUp(self):
        self.agents = [(kind, name) for kind in KINDS for name in kind_names(kind)]
        self.assertTrue(self.agents, "エージェントの定義がありません")

    def test_does_not_name_itself(self):
        for kind, name in self.agents:
            path = kind_path(name, kind)
            with self.subTest(agent=pp.rel(path)):
                found = pp.hits_in_text(pp.rel(path), scan_text(path, kind), [(name, pp.name_token(name))])
                self.assertEqual(found, [], f"{name}: 自分の名前を書いています")

    def test_mcp_tools_only_for_servers_in_the_table(self):
        """tools でも disallowedTools でも、MCP のツールは「使う部品」の表の MCP サーバーのもの（設計書 9.1）"""
        known = pp.all_mcp_tool_names()
        for kind, name in self.agents:
            path = kind_path(name, kind)
            fm, body, _ = pp.frontmatter(path)
            rows, _ = pp.parts_table(body)
            with self.subTest(agent=pp.rel(path)):
                self.assertEqual(mcp_tool_problems(name, kind, fm or {}, {row[1] for row in rows or []}, known), [])

    def test_the_mcp_tool_check_reads_disallowed_tools(self):
        """表に MCP サーバーがないエージェントの disallowedTools に、そのサーバーのツールを書くと、見つける（両方の形）"""
        server = sorted(pp.mcp_registrations())[0]
        claude = {"disallowedTools": ", ".join(pp.claude_mcp_tool_names(server, "any_tool"))}
        self.assertEqual(len(mcp_tool_problems("x", "claude", claude, set(), set())), 2)
        copilot = {"disallowedTools": [f"{server}/any_tool"]}
        self.assertEqual(len(mcp_tool_problems("x", "copilot", copilot, set(), set())), 1)
        self.assertEqual(mcp_tool_problems("x", "claude", claude, {server}, set()), [])

    def test_worker_knows_no_stage3(self):
        """2段階目の作業役は、3段階目（統括・検証・統合のエージェント）のことを書かない"""
        paths = [(kind, kind_path(pp.ROLES["worker"], kind)) for kind in KINDS]
        patterns = [(w, re.compile(re.escape(w))) for w in STAGE3_WORDS_JA] + [(w, pp.token(w)) for w in UPPER_WORDS_EN[2:]]
        patterns += [(n, pp.name_token(n)) for n in (pp.ORCHESTRATOR, pp.ROLES["reviewer"], pp.ROLES["integrator"])]
        found = []
        for kind, path in paths:
            found += pp.hits_in_text(pp.rel(path), scan_text(path, kind), patterns)
        self.assertEqual(found, [], "作業役が、3段階目のことを書いています：\n" + "\n".join(found))


class GeneratedBriefs(unittest.TestCase):
    """一括実行が書く指示ファイル（設計書 4.14）"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(TESTS / "integration"))
        import flowlib  # noqa: E402  (the shared harness of the plugin-level flow tests)

        cls.fl = flowlib
        reason = flowlib.prerequisites()
        if reason:
            raise unittest.SkipTest(reason)
        cls.tmp = Path(tempfile.mkdtemp(prefix="briefs-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        fl = flowlib
        env = fl.base_env(cls.tmp)
        env.update(TMPDIR=str(cls.tmp), TEMP=str(cls.tmp), TMP=str(cls.tmp))
        repo = cls.tmp / "repo"
        repo.mkdir()
        revision = fl.write_repo(repo, env)
        fl.write_settings(repo, cls.tmp / "out", "https://coverity.example.invalid", extra_coverity="  api: fake\n", env=env)
        cids = {w["key"]: 30001 + n for n, w in enumerate(fl.WARNINGS)}
        issues = fl.offline_files(cls.tmp / "fetched", cids, revision)
        run_py = pp.RUN_SCRIPT
        created = fl.run_script(run_py, "new-run", "--repo", str(repo), "--issues-file", str(issues), "--review", "--integration",
                                env=env, cwd=repo)
        run_dir = Path(created["run_dir"])
        cls.agents = fl.FakeAgents(cids, review_outcomes={str(cids["C"]): ["rework", "pass"]})
        cls.orchestrator = fl.Orchestrator(run_py, run_dir, env, repo, cls.agents)
        try:
            cls.orchestrator.drive(limit_seconds=150)
        finally:
            fl.run_script(run_py, "stop", "--run", str(run_dir), env=env, cwd=repo, ok=None, timeout=60)

    def test_every_role_got_an_instruction_file(self):
        roles = {a.get("role") for a in self.orchestrator.launches()}
        self.assertEqual(roles, {"worker", "reviewer", "integrator"}, "作業・確かめ・統合の指示ファイルが揃いません")
        self.assertTrue(any("## 確かめの指摘" in p.read_text(encoding="utf-8") for p in self.agents.briefs),
                        "作り直しの指示ファイルがありません")

    def test_briefs_point_inside_the_run_folder_or_the_repository(self):
        """指示ファイルの表のファイルは、実行フォルダか、リポジトリ（とその設定）の中だけ。部品のフォルダを指さない"""
        problems = []
        for path in sorted(set(self.agents.briefs)):
            brief = self.fl.Brief(path)
            for label, value in brief.rows.items():
                target = Path(value)
                if target.is_absolute() and pp.ROOT.resolve() in [target.resolve(), *target.resolve().parents]:
                    problems.append(f"{path.name}: 「{label}」がプラグインの部品のフォルダを指しています：{value}")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()