"""Claude Code での使い方の文書を確かめる試験（仕様 REQ-009、設計書 2.3・9.1「文書」）。

- 名前：Claude Code のマニフェスト（.claude-plugin/plugin.json）が plugin.json と同じ名前、リポジトリの2つの一覧の
  マーケットプレイスの名前が同じ（版は test_docs.py が確かめる）。
- プラグインの README に「Claude Code で使う」があり、入れ方・段階ごとの呼び方・4段階目は Copilot CLI だけ・
  前もって許可するしかた・認証情報の環境変数がある。
- 前もって許可する指定（--allowedTools の行と、``` で囲んだ写して使う行）に、書き戻すツールも、サーバーの名前だけの許可も、
  * もない。対象は、プラグインの README、エージェントの README（すべて）、使い方の案内（スキルの references/usage.md）。
  そこに書いた mcp__plugin_<プラグイン>_<登録名>__<ツール>（と手で登録したときの mcp__<登録名>__<ツール>）は、
  プラグインの名前・.mcp.json の登録名・MCP サーバーのツール（ソースと、サーバーの README の「## ツール」の見出し）と合う。
  --allowedTools の行は、書き戻すツールのほかのツール（読むツール）をすべて並べる。
- 3段階目の許可（設計書 3.5）：README と統括エージェントの README の起動のコマンドと /permissions の一覧が、同じ8つ
  （uv、スキル、このプラグインのフォルダを読むだけ、実行フォルダにだけ書く Edit(//…/**)、読む4つのツール）を並べる。
  素の Write・Edit と、プラグインのフォルダに書く許可はない。取り出したときの Skill(<スキル>) は、コピーするスキルのうち
  allowed-tools を持つものと同じ。変更履歴の /permissions の文も Skill・Read・Edit を挙げる。
- エージェントの README に「Claude Code で使う」があり、Claude Code の定義（agents/<名前>.md）と、
  .claude/agents/ への取り出し方を書いている。書いた `--agent <プラグイン>:<名前>` と定義のファイルが、あるものを指す。
- 仕様書に Claude Code の要求があり、設計書に「2つの形の同居」の節がある。リポジトリの README の行が Claude Code に触れる。
部品の名前は書かない。フォルダから読む。

  python3 -m unittest discover -s tests/docs -v
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS / "structure"))
import plugin_parts as pp  # noqa: E402

README = pp.ROOT / "README.md"
# Claude Code の利用者向けのガイド（README は入口だけ。くわしい手順はここ。REQ-008、REQ-009）
CLAUDE_GUIDE = pp.ROOT / "docs" / "guide" / "claude-code.md"
CLAUDE_MANIFEST = pp.ROOT / ".claude-plugin" / "plugin.json"
CLAUDE_AGENTS_DIR = pp.ROOT / "agents"
MARKET_ROOT = pp.ROOT.parents[1]
CLAUDE_MARKET = MARKET_ROOT / ".claude-plugin" / "marketplace.json"
COPILOT_MARKET = MARKET_ROOT / ".github" / "plugin" / "marketplace.json"
MARKET_README = MARKET_ROOT / "README.md"
HEADING = "Claude Code で使う"
# 使い方の案内（スキルの references/usage.md。前もって許可するしかたも書く）
USAGE_NAME = Path("references") / "usage.md"
# 文の中の MCP のツールの名前（<…> の置き換える形は、あとで飛ばす）
MCP_NAME = re.compile(r"mcp__[^\s\"'`)（）、。，,]+")
FENCE_LINE = re.compile(r"^\s*```")


def mcp_names(line):
    """行の中の MCP のツールの名前（文の終わりの . : ; は名前に入れない）"""
    return [n.rstrip(".:;") for n in MCP_NAME.findall(line)]


def section(text, heading, level=2):
    """見出しの節の本文（同じか上の段の次の見出しまで）。なければ None"""
    marks = "#" * level
    m = re.search(rf"(?ms)^{marks} {re.escape(heading)}\s*\n(.*?)(?=^#{{1,{level}}} |\Z)", text)
    return m.group(1) if m else None


def section_with_code(text, heading):
    """「## 見出し」の節の本文。``` の中の「# 」の行（コマンドの注釈）では切らない。なければ空"""
    out, inside, fenced = [], False, False
    for line in text.splitlines():
        if FENCE_LINE.match(line):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            inside = line[3:].strip() == heading
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def claude_agent_names():
    if not CLAUDE_AGENTS_DIR.is_dir():
        return []
    return sorted(p.stem for p in CLAUDE_AGENTS_DIR.glob("*.md"))


class ClaudeNamesTest(unittest.TestCase):
    """版の一致は test_docs.VersionTest が確かめる。ここは、README の入れ方の名前が食い違わないこと"""

    def setUp(self):
        if not CLAUDE_MANIFEST.is_file():
            self.skipTest("Claude Code のマニフェストがまだありません")

    def test_manifest_name_matches(self):
        data = json.loads(pp.read(CLAUDE_MANIFEST))
        self.assertEqual(data.get("name"), json.loads(pp.read(pp.ROOT / "plugin.json")).get("name"),
                         ".claude-plugin/plugin.json の name が plugin.json と違います")

    def test_marketplace_names_match(self):
        if not (CLAUDE_MARKET.is_file() and COPILOT_MARKET.is_file()):
            self.skipTest("リポジトリに2つの一覧がありません")
        self.assertEqual(json.loads(pp.read(CLAUDE_MARKET)).get("name"), json.loads(pp.read(COPILOT_MARKET)).get("name"),
                         "2つの一覧のマーケットプレイスの名前が違います（README の入れ方の @<名前> が食い違う）")


class ReadmeClaudeSectionTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(CLAUDE_GUIDE.is_file(), "docs/guide/claude-code.md がありません（REQ-009）")
        self.text = pp.read(README)
        self.body = pp.read(CLAUDE_GUIDE)

    def test_readme_links_to_the_guide(self):
        self.assertIn("(docs/guide/claude-code.md)", self.text, "README から Claude Code ガイドへのリンクがありません")

    def test_mentions_the_essentials(self):
        market = json.loads(pp.read(CLAUDE_MARKET)).get("name") if CLAUDE_MARKET.is_file() else None
        install = f"claude plugin install {pp.PLUGIN_NAME}" + (f"@{market}" if market else "")
        for word in [install, "claude plugin marketplace add", "--agent", "--allowedTools", "Bash(uv run *)",
                     "/add-dir", "--add-dir", "COV_USER", "COV_AUTH_KEY", "Copilot CLI だけ"]:
            self.assertIn(word, self.body, f"README の「{HEADING}」に {word} がありません（REQ-009）")
        for stage in ["1", "2", "3", "4"]:
            self.assertRegex(self.body, rf"{stage}\s*段階", f"README の「{HEADING}」に {stage}段階目の呼び方がありません")

    def test_stage4_is_copilot_only_in_the_stage_table(self):
        rows = [line for line in (self.text + "\n" + self.body).splitlines()
                if pp.is_table_line(line) and re.match(r"\|\s*4(\.|段階目)", line.strip())]
        self.assertTrue(rows, "README に4段階目の行がありません")
        self.assertTrue(any("Copilot CLI だけ" in r for r in rows), "README の段階の表で、4段階目が Copilot CLI だけと分かりません")

    def test_agent_commands_point_to_agents(self):
        names = claude_agent_names()
        for name in re.findall(rf"--agent {re.escape(pp.PLUGIN_NAME)}:([a-z0-9-]+)", self.body):
            self.assertIn(name, names, f"README の --agent {pp.PLUGIN_NAME}:{name} の定義が agents/ にありません")

    def test_permission_lines_are_in_the_section(self):
        self.assertIn("--allowedTools", self.body, f"README の「{HEADING}」に、起動のときに許可する指定がありません")

    def test_skills_and_their_folder_are_allowed(self):
        """スキルは allowed-tools を持つので使う前に確認が出て、入れたプラグインの参照資料はリポジトリの外にある
        （2.1.319 で確認）。裏で動くエージェントは確認を出せないので、2つの許可のしかたの両方に入れる"""
        if not MARKET_NAME:
            self.skipTest("マーケットプレイスの一覧（.claude-plugin/marketplace.json）がありません")
        rules = [f"Skill({pp.PLUGIN_NAME}:*)", plugin_read_rule()]
        starts = [line for line in self.body.splitlines() if line.startswith("claude --add-dir")]
        self.assertTrue(starts, f"README の「{HEADING}」に起動のコマンドがありません")
        for line in starts:
            for rule in rules:
                self.assertIn(f'"{rule}"', line, f"起動のコマンドに {rule} がありません")
        blocks = [b.splitlines() for b in re.findall(r"```text\n(.*?)```", self.body, re.S)]
        self.assertTrue(any(all(rule in lines for rule in rules) for lines in blocks),
                        f"/permissions で許可する一覧に {rules} がありません")
        self.assertNotRegex(self.body, r"(Edit|Write)\(~/\.claude/plugins", "プラグインのフォルダは読むだけ")


MARKET_NAME = json.loads(pp.read(CLAUDE_MARKET)).get("name") if CLAUDE_MARKET.is_file() else None
# 実行フォルダにだけ書く許可（2.1.319 で確認：Write にも効き、ほかの場所に書くときは確認が出る）
EDIT_RULE = "Edit(//<output_dir の絶対パス>/**)"


def plugin_read_rule():
    """GitHub のマーケットプレイスから入れたプラグインのフォルダを読むだけの許可（ほかのプラグインは読まない）"""
    return f"Read(~/.claude/plugins/cache/{MARKET_NAME}/{pp.PLUGIN_NAME}/**)"


def read_tool_names():
    """Coverity から読むツール（書き戻すツールのほか）の、プラグインとして入れたときの名前"""
    write_back = {t for names in pp.write_back_tools().values() for t in names}
    entries = pp.mcp_names()
    names = []
    for server in sorted(claude_servers()):
        entry = entries.get(server) or {}
        tools = set(entry.get("tools") or set())
        listed = readme_tools(entry.get("folder"))
        tools = tools & listed if listed is not None else tools
        names += [pp.claude_mcp_tool_names(server, t)[0] for t in sorted(tools - write_back)]
    return names


def quoted(line):
    """コマンドの行の "…" で囲んだ値"""
    return re.findall(r'"([^"]*)"', line)


class ClaudePermissionSetTest(unittest.TestCase):
    """3段階目を夜通し動かすときの許可（README と統括エージェントの README）：起動のコマンドと /permissions の一覧が、
    同じ8つ（uv、スキル、プラグインのフォルダを読むだけ、実行フォルダにだけ書く、読む4つのツール）を並べる。
    素の Write・Edit と、プラグインのフォルダに書く許可は書かない"""

    def setUp(self):
        if not pp.CLAUDE_MCP.is_file():
            self.skipTest("Claude Code の .mcp.json がまだありません")
        orchestrators = [p for p in pp.AGENTS_DIR.glob("*" + pp.AGENT_README_SUFFIX)
                         if re.search(rf"--agent {re.escape(pp.PLUGIN_NAME)}:", section(pp.read(p), HEADING) or "")
                         and "### 前もって許可する" in (section(pp.read(p), HEADING) or "")]
        self.docs = {"docs/guide/claude-code.md": pp.read(CLAUDE_GUIDE) if CLAUDE_GUIDE.is_file() else ""}
        self.docs.update({p.name: section(pp.read(p), HEADING) or "" for p in orchestrators})
        self.assertGreaterEqual(len(self.docs), 2, "統括エージェントの README に「前もって許可する」がありません")

    @staticmethod
    def same(rules):
        """並びは、MCP のツールの前の4つだけ比べる（読むツールの並びは問わない）"""
        return [r for r in rules if not r.startswith("mcp__")], sorted(r for r in rules if r.startswith("mcp__"))

    def test_launch_commands_and_permissions_lists_name_the_same_rules(self):
        if not MARKET_NAME:  # the narrow Read rule names the marketplace
            self.skipTest("マーケットプレイスの一覧（.claude-plugin/marketplace.json）がありません")
        self.rules = ["Bash(uv run *)", f"Skill({pp.PLUGIN_NAME}:*)", plugin_read_rule(), EDIT_RULE, *read_tool_names()]
        self.assertEqual(len(self.rules), 8, self.rules)
        for name, body in self.docs.items():
            with self.subTest(doc=name):
                starts = [line for line in body.splitlines()
                          if line.startswith("claude --add-dir") and "--allowedTools" in line]
                self.assertTrue(starts, f"{name}：起動のコマンドがありません")
                for line in starts:
                    allowed = line.split("--allowedTools", 1)[1].split(" --agent ", 1)[0]
                    self.assertEqual(self.same(quoted(allowed)), self.same(self.rules), f"{name}：起動のコマンドの許可が8つと違います")
                blocks = [self.same([x for x in b.splitlines() if x.strip()]) for b in re.findall(r"```text\n(.*?)```", body, re.S)]
                self.assertIn(self.same(self.rules), blocks,
                              f"{name}：/permissions で許可する一覧が、起動のコマンドと同じ8つではありません")
                self.assertIn("後のセッションにも残ります", body, f"{name}：/permissions の許可が残ることを書いていません")
                self.assertIn("`.claude/settings.local.json`", body, f"{name}：保存先（自分だけの設定）がありません")
                self.assertIn("//c/", body, f"{name}：Windows のパスの書き方（//c/…）がありません")

    def test_no_bare_write_or_edit_and_no_write_to_the_plugin_folder(self):
        for path in permission_docs():
            text = pp.read(path)
            with self.subTest(doc=pp.rel(path)):
                for number, line, allowed in approve_lines(text):
                    if allowed and "claude" in line:
                        for bare in ("Write", "Edit"):
                            self.assertNotIn(bare, quoted(line), f"{pp.rel(path)}:{number}：素の {bare} を前もって許可しています")
                self.assertNotRegex(text, r"(Edit|Write)\((~|//)[^)]*plugins", "プラグインのフォルダは読むだけ")
                self.assertNotRegex(text, r"(Edit|Write)\(//<プラグインのフォルダ>", "プラグインのフォルダは読むだけ")
                self.assertNotIn("Read(~/.claude/plugins/**)", text, "ほかのプラグインまで読める広さです")
        for name in ("CHANGELOG.md",):
            text = pp.read(pp.ROOT / name)
            with self.subTest(doc=name):
                lines = [line for line in text.splitlines() if "`/permissions`" in line]
                self.assertTrue(lines, f"{name}：/permissions の文がありません")
                for line in lines:
                    self.assertNotIn("`Write`", line, f"{name}：素の Write を許可するとあります")
                    for rule in ("Skill(", "Read(", "Edit("):
                        self.assertIn(rule, line, f"{name}：/permissions の文に {rule} がありません")

    def test_extracted_skills_rules_follow_the_copy_lists(self):
        """取り出したときの Skill(<スキル>) は、コピーするスキルのうち allowed-tools を持つものと同じ"""
        found = 0
        for readme in sorted(pp.AGENTS_DIR.glob("*" + pp.AGENT_README_SUFFIX)):
            body = section_with_code(pp.read(readme), HEADING)
            copied = re.findall(r"^for s in ([^;\n]+); do", body, re.M)
            named = set(re.findall(r"`Skill\(([a-z0-9-]+)\)`", body))
            if not copied:
                continue
            found += 1
            want = {n for n in copied[0].split()
                    if re.search(r"(?m)^allowed-tools:", pp.read(pp.SKILLS_DIR / n / "SKILL.md").split("\n---", 1)[0])}
            with self.subTest(readme=readme.name):
                self.assertEqual(named, want, f"{readme.name}：取り出したときの Skill(<スキル>) が、コピーするスキルと合いません")
                self.assertIn("`Read(~/.claude/skills/**)`", body, f"{readme.name}：~/.claude/skills/ に置いたときの Read がありません")
        if not found:
            self.skipTest("コピーの一覧を書いたエージェントの README がありません")


def permission_docs():
    """前もって許可するしかたを書く文書：プラグインの README、エージェントの README（すべて）、使い方の案内"""
    docs = [README, CLAUDE_GUIDE] + sorted(pp.AGENTS_DIR.glob("*" + pp.AGENT_README_SUFFIX))
    docs += [d / USAGE_NAME for d in pp.skill_dirs() if (d / USAGE_NAME).is_file()]
    return docs


def claude_servers():
    """.mcp.json（Claude Code）の登録名"""
    data = pp.load_json(pp.CLAUDE_MCP) if pp.CLAUDE_MCP.is_file() else None
    return set((data or {}).get("mcpServers") or {}) if isinstance(data, dict) else set()


def readme_tools(folder):
    """MCP サーバーの README の「## ツール」の「### `<ツール>(…)`」の名前。README か節がなければ None"""
    path = folder / "README.md" if folder else None
    if not (path and path.is_file()):
        return None
    body = pp.section(pp.read(path), "ツール")
    if body is None:
        return None
    return set(re.findall(r"(?m)^### `([A-Za-z0-9_]+)\(", body))


def split_mcp_name(name):
    """Claude Code の MCP の名前を (プラグイン, 登録名, ツール)。手で登録した形はプラグインが None、
    サーバーの名前だけならツールが None"""
    if name.startswith("mcp__plugin_"):
        rest = name[len("mcp__plugin_"):]
        prefix = pp.PLUGIN_NAME + "_"
        if not rest.startswith(prefix):
            return rest, None, None
        server, sep, tool = rest[len(prefix):].partition("__")
        return pp.PLUGIN_NAME, server, (tool if sep else None)
    server, sep, tool = name[len("mcp__"):].partition("__")
    return None, server, (tool if sep else None)


def approve_lines(text):
    """前もって許可する行：--allowedTools のある行と、``` で囲んだ中の行。(行の番号, 行, --allowedTools の行か)"""
    out, fenced, pending = [], False, None
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE_LINE.match(line):
            fenced = not fenced
            continue
        if pending is not None:  # a command continued with \ : one logical line
            pending = (pending[0], pending[1] + " " + line.rstrip("\\ "))
        elif "--allowedTools" in line or fenced:
            pending = (number, line.rstrip("\\ ") if line.rstrip().endswith("\\") else line)
        if pending is not None and not line.rstrip().endswith("\\"):
            out.append((pending[0], pending[1], "--allowedTools" in pending[1]))
            pending = None
    if pending is not None:
        out.append((pending[0], pending[1], "--allowedTools" in pending[1]))
    return out


DENY_ARGS = re.compile(r"""--(?:disallowedTools|deny-tool)(?:\s+(?:"[^"]*"|'[^']*'|[^\s-][^\s]*))+""")


def allowed_part(line):
    """前もって許可する指定だけ（--disallowedTools と --deny-tool の引数は、禁じる指定なので除く）"""
    return DENY_ARGS.sub(" ", line)


def mcp_name_problems(label, text, servers, tools_of, write_back):
    """文の中の MCP の名前と、前もって許可する行の問題。servers は .mcp.json の登録名、
    tools_of は登録名 → ツールの名前の集まり（ソースと README の両方にあるもの）、write_back は書き戻すツールの名前"""
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        for name in mcp_names(line):
            if "<" in name or name.endswith("*"):
                continue
            plugin, server, tool = split_mcp_name(name)
            where = f"{label}:{number}: {name}"
            if name.startswith("mcp__plugin_") and plugin != pp.PLUGIN_NAME:
                problems.append(f"{where}：プラグインの名前が {pp.PLUGIN_NAME} ではありません")
            elif server not in servers:
                problems.append(f"{where}：登録名 {server} が .mcp.json にありません")
            elif tool is not None and tool not in tools_of.get(server, set()):
                problems.append(f"{where}：{tool} は、MCP サーバーのツール（ソースと README の「## ツール」）にありません")
    for number, line, allowed in approve_lines(text):
        where = f"{label}:{number}"
        line = allowed_part(line)
        names = mcp_names(line)
        for name in names:
            plugin, server, tool = split_mcp_name(name)
            if name.endswith("*"):
                problems.append(f"{where}：{name} は、書き戻すツールまで前もって許可します")
            elif "<" not in name and tool is None and server in servers:
                problems.append(f"{where}：サーバーの名前だけの {name} は、書き戻すツールまで前もって許可します")
        for tool in write_back:
            if re.search(rf"(?:(?<![A-Za-z0-9_])|(?<=__)){re.escape(tool)}(?![A-Za-z0-9_])", line):
                problems.append(f"{where}：前もって許可する指定に、書き戻すツール {tool} があります")
        if allowed:
            concrete = {split_mcp_name(n)[1] for n in names if "<" not in n and split_mcp_name(n)[2]}
            for server in sorted(concrete & servers):
                listed = {split_mcp_name(n)[2] for n in names if "<" not in n and split_mcp_name(n)[1] == server}
                missing = sorted(tools_of.get(server, set()) - set(write_back) - listed)
                if missing:
                    problems.append(f"{where}：--allowedTools に、読むツール {', '.join(missing)} がありません")
    return problems


class PermissionDocsTest(unittest.TestCase):
    """前もって許可するしかた（--allowedTools と /permissions）の名前が、本物と合い、書き戻すツールを許可しない"""

    def setUp(self):
        if not pp.CLAUDE_MCP.is_file():
            self.skipTest("Claude Code の .mcp.json がまだありません")
        self.servers = claude_servers()
        entries = pp.mcp_names()
        self.tools_of = {}
        for server in self.servers:
            entry = entries.get(server) or {}
            tools = set(entry.get("tools") or set())
            listed = readme_tools(entry.get("folder"))
            self.tools_of[server] = tools & listed if listed is not None else tools
        self.write_back = sorted({t for names in pp.write_back_tools().values() for t in names})
        self.assertTrue(self.write_back, "書き戻しのツール（destructiveHint=True）が MCP サーバーに見つかりません")

    def test_docs_name_real_tools_and_never_pre_approve_write_back(self):
        for path in permission_docs():
            with self.subTest(doc=pp.rel(path)):
                self.assertEqual(mcp_name_problems(pp.rel(path), pp.read(path), self.servers, self.tools_of, self.write_back), [])

    def test_the_check_finds_mistakes(self):
        """確かめる道具が、間違いを見つけられること（書き戻すツール、サーバーの名前だけ、*、違う名前、足りない読むツール）"""
        server = sorted(self.servers)[0]
        tools = sorted(self.tools_of[server])
        reads = [t for t in tools if t not in self.write_back]
        ok = [f'"{pp.claude_mcp_tool_names(server, t)[0]}"' for t in reads]
        good = "claude --allowedTools \"Write\" " + " ".join(ok)
        check = lambda text: mcp_name_problems("x", text, self.servers, self.tools_of, self.write_back)  # noqa: E731
        self.assertEqual(check(good), [])
        self.assertEqual(check("```text\n" + "\n".join(n.strip('"') for n in ok) + "\n```\n"), [])
        self.assertEqual(check(f"説明：サーバーの名前だけの `mcp__plugin_{pp.PLUGIN_NAME}_{server}` は使わない。"), [])
        self.assertEqual(check("`mcp__plugin_<プラグイン>_<登録名>__<ツール>` を並べる"), [])
        wb = pp.claude_mcp_tool_names(server, self.write_back[0])[0]
        self.assertTrue(check(good + f' "{wb}"'))
        self.assertTrue(check(f"```text\n{wb}\n```\n"))
        self.assertTrue(check(good + f' "mcp__plugin_{pp.PLUGIN_NAME}_{server}"'))
        self.assertTrue(check(good + f' "mcp__plugin_{pp.PLUGIN_NAME}_{server}__*"'))
        self.assertTrue(check(good.replace(reads[0], reads[0] + "s")))
        self.assertTrue(check(good.replace(f"_{server}__", "_other-server__")))
        self.assertTrue(check(good.replace(f"plugin_{pp.PLUGIN_NAME}_", "plugin_other-plugin_")))
        if len(ok) > 1:
            self.assertTrue(check("claude --allowedTools " + " ".join(ok[1:])))
            # a command continued with \ is read as one line: a missing read tool is still found
            self.assertTrue(check("```text\nclaude --allowedTools \"Write\" \\\n  " + " ".join(ok[1:]) + "\n```\n"))
        # denying the write-back tool is not pre-approving it
        self.assertEqual(check(good + f' --disallowedTools "{wb}"'), [])
        # a name at the end of a sentence
        self.assertEqual(check(f"手で登録したときは {pp.claude_mcp_tool_names(server, reads[0])[-1]}."), [])


class AgentReadmeClaudeSectionTest(unittest.TestCase):
    def setUp(self):
        self.names = claude_agent_names()
        if not self.names:
            self.skipTest("Claude Code のエージェントの定義がまだありません")

    def test_every_agent_readme_has_the_section(self):
        for name in self.names:
            readme = pp.AGENTS_DIR / (name + pp.AGENT_README_SUFFIX)
            with self.subTest(agent=name):
                self.assertTrue(readme.is_file(), f"{readme.name} がありません（2つの形の使い方はここに書く）")
                body = section(pp.read(readme), HEADING)
                self.assertIsNotNone(body, f"{readme.name} に「## {HEADING}」がありません")
                self.assertIn(f"agents/{name}.md", body, f"{readme.name}：Claude Code の定義の場所がありません")
                self.assertIn(".claude/agents/", body, f"{readme.name}：取り出し方（.claude/agents/）がありません")
                self.assertIn(".claude/skills/", body, f"{readme.name}：取り出し方（.claude/skills/）がありません")

    def test_named_files_and_agents_exist(self):
        for readme in sorted(pp.AGENTS_DIR.glob("*" + pp.AGENT_README_SUFFIX)):
            body = section(pp.read(readme), HEADING) or ""
            with self.subTest(readme=readme.name):
                for name in re.findall(rf"--agent {re.escape(pp.PLUGIN_NAME)}:([a-z0-9-]+)", body):
                    self.assertIn(name, self.names, f"{readme.name}：--agent の {name} の定義が agents/ にありません")
                for name in re.findall(r"`agents/([a-z0-9-]+)\.md`", body):
                    self.assertIn(name, self.names, f"{readme.name}：agents/{name}.md がありません")


class SpecAndDesignTest(unittest.TestCase):
    def test_spec_has_the_claude_code_requirement(self):
        reqs = re.findall(r"(?m)^- (REQ-\d{3})：([^\n]*)", pp.read(pp.SPEC))
        hits = [r for r, line in reqs if "Claude Code" in line and "Copilot CLI だけ" in line]
        self.assertTrue(hits, "仕様書に、1〜3段階目は Claude Code でも使え、4段階目は Copilot CLI だけ、の要求がありません")

    def test_design_has_the_coexistence_section(self):
        text = pp.read(pp.DESIGN)
        self.assertRegex(text, r"(?m)^### \d+\.\d+ 2つの形の同居", "設計書に「2つの形の同居」の節がありません")
        self.assertIn("$schema", text, "設計書に、plugin.json の $schema の守りがありません")

    def test_repository_readme_mentions_claude_code(self):
        if not MARKET_README.is_file():
            self.skipTest("リポジトリの README がありません")
        rows = [line for line in pp.read(MARKET_README).splitlines() if line.startswith(f"| [{pp.PLUGIN_NAME}]")]
        self.assertEqual(len(rows), 1, "リポジトリの README の一覧に、このプラグインの行がありません")
        self.assertIn("Claude Code", rows[0], "リポジトリの README の行に、Claude Code でも使えることがありません")


if __name__ == "__main__":
    unittest.main()
