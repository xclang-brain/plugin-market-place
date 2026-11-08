"""部品の独立性を確かめる試験（憲章 第25条・第26条3項、仕様 REQ-001〜REQ-003、REQ-006、REQ-007、REQ-101、
REQ-202、REQ-603、設計書 9.1「独立性」）。

この試験には、部品の名前を書かない。名前は、フォルダ・mcp.json・defaults.json・「使う部品」の表から読む
（tests/structure/plugin_parts.py）。そのため、部品を足す・消す・置き換えても、この試験は直さずに済む。

確かめること：
- 1段階目の部品（defaults.json の pluginSkills にないスキルと、MCP サーバー）：フォルダの中に、プラグインの名前（と略）、
  ほかの部品の名前、MCP サーバーの登録名とツールの名前（MCP サーバーの中を除く）、上の段階の言葉、フォルダの外を指すパスがない。
  sys.path でフォルダの外を読まない。フォルダだけを別の場所にコピーして（スキルは名前を変えたコピーも）、その試験が通る。
- エージェント：部品の名前は「## 使う部品」の表にだけ（そのままの名前で）。tools の MCP のツールは、表の MCP サーバーのものだけ。
  表の名前は、ある部品を指す。description に、defaults.json の役割の言葉がある。自分の名前を書かない。
  2段階目の組（作業役とその表）と3段階目の組（統括エージェントの表をたどった部品）が、名前で閉じている。
- プラグイン全体のスキル：部品の名前は、SKILL.md の「## 使う部品」の表にだけ。
- 上の段の文書（プラグインの README、4段階目のガイド、エージェントの README、変更履歴）に、下の段のスクリプトの名前と設定の項目がない。
- エクステンション：スキルとエージェントの名前（とスクリプトの名前）は defaults.json にだけ。
- 元のプラグインなど、同じリポジトリのほかのプラグインと、エージェントと MCP サーバーの名前が重ならない（REQ-007）。
- 一括実行が書く指示ファイル（作業・確かめ・統合）と launch の頼みの文に、部品の名前がない（設計書 4.14）。
  一時フォルダのリポジトリで、取ってある警告の一覧から実行を作り、決まった振る舞いの偽のエージェントで最後まで流して確かめる。
- 名前を探す決まりそのもの（単語として数える、uv.lock を見ない、output_dir の既定の値だけを許す）。

  python3 -m unittest discover -s tests/independence -v
環境変数：SKIP_SLOW_TESTS=1 でコピーして動かす試験を飛ばす。TESTS_JOBS（同時に動かす数）、TESTS_PART_TIMEOUT（秒）。
"""

import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS / "structure"))
import plugin_parts as pp  # noqa: E402


def load_runner():
    """tests/run.py（部品の試験の動かし方の決まりを持つ）"""
    spec = importlib.util.spec_from_file_location("plugin_tests_run", TESTS / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = load_runner()

# 1段階目の部品が書いてはいけない、上の段階の言葉（英語は単語として、大文字と小文字を区別しない）
UPPER_WORDS_JA = ["Web管理画面", "エクステンション", "統括エージェント"]
UPPER_WORDS_EN = ["extension", "extensions", "orchestrator", "orchestrators"]
# 2段階目の作業役が書いてはいけない、3段階目の言葉
STAGE3_WORDS_JA = ["統括", "検証エージェント", "統合エージェント"]
SLOW = os.environ.get("SKIP_SLOW_TESTS") == "1"


def plugin_patterns():
    """プラグインの名前、その _ の形、略（頭文字の大文字）"""
    name = pp.PLUGIN_NAME
    patterns = [(name, re.compile(re.escape(name), re.I)), (name.replace("-", "_"), re.compile(re.escape(name.replace("-", "_")), re.I))]
    initials = "".join(w[0] for w in re.split(r"[-_]", name) if w)
    if len(initials) >= 3:
        patterns.append((initials.upper() + "（プラグインの略）",
                         re.compile(r"(?<![A-Za-z0-9])" + initials.upper() + r"(?![A-Za-z0-9])|(?<![A-Za-z0-9])"
                                    + initials.lower() + r"(?=[_\-])")))
    return patterns


def upper_patterns():
    return [(w, re.compile(re.escape(w))) for w in UPPER_WORDS_JA] + [(w, pp.token(w)) for w in UPPER_WORDS_EN]


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
            ("プラグインの tests/", re.compile(r"(?<![\w.])tests/(independence|contracts|structure|integration|extension)\b"))]


# 部品の試験が「書いてはいけない言葉」として並べる、言葉だけの文字列（"../"、"extension" など）は数えない
LISTED_WORD = re.compile(r"([\"'])(?:\.\.(?:/|\\\\)?|\$\{PLUGIN_ROOT\}|PLUGIN_ROOT|PLUGIN_DATA|plugin\.json|com\.github\.copilot|"
                         + "|".join(re.escape(w) for w in UPPER_WORDS_JA + UPPER_WORDS_EN) + r")\1", re.I)


def without_listed_words(path, text):
    return LISTED_WORD.sub("''", text) if path.suffix in (".py", ".mjs", ".js") else text


def outside_problems(folder):
    """フォルダの外を指す書き方。試験（tests/）と共通の試験のデータ（assets/contract/）は、外のパスを入力として
    使うことがあるので見ない（フォルダの外を読まないことは、コピーして動かす試験で確かめる）"""
    problems = []
    for path in pp.text_files(folder):
        parts = path.relative_to(folder).parts
        if parts[0] == "tests" or parts[:2] == ("assets", "contract"):
            continue
        problems += pp.hits_in_text(pp.rel(path), without_listed_words(path, pp.read(path)), outside_patterns())
    return problems + sys_path_problems(folder)


def mcp_entries():
    return pp.mcp_names()


def part_name_patterns(exclude=(), include_inner=True):
    """部品の名前（大文字と小文字を区別し、単語として）。exclude の名前は除く"""
    names = set(pp.skill_names()) | set(pp.agent_names())
    for entry in mcp_entries().values():
        names |= entry["names"]
        if include_inner:
            names |= entry["inner"]
    return [(n, pp.name_token(n)) for n in sorted(names - set(exclude))]


def script_patterns(own=()):
    """ほかのスキルのスクリプトの名前（中身）"""
    own = set(own)
    return [(n, pp.script_token(n)) for n in sorted(pp.script_owners()) if n not in own]


def own_python_files(folder):
    """フォルダの中の .py の名前（同じ名前のファイルを持つなら、ほかのスキルのスクリプトの名前と重なっても数えない）"""
    return {p.name for p in folder.rglob("*.py") if not pp.skipped(p, folder)}


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
    """1段階目の部品のフォルダ：(フォルダ, その部品の名前の集まり, 種類)"""
    parts = [(pp.SKILLS_DIR / n, {n}, "skill") for n in pp.stage1_skill_names()]
    for entry in mcp_entries().values():
        folder = entry["folder"]
        if folder and folder.is_dir():
            parts.append((folder, entry["names"] | entry["inner"], "mcp"))
    return parts


def agent_info(name):
    """エージェントの定義を読む：{path, frontmatter, body, table, outside, error}"""
    path = pp.agent_path(name)
    data, body, problem = pp.frontmatter(path)
    rows, table_error = pp.parts_table(body)
    _, outside_body = pp.split_section_table(body, pp.PARTS_HEADING)
    data = data or {}
    outside = "\n".join([str(data.get("description", "")), " ".join(map(str, data.get("model") or [])), outside_body])
    return {"path": path, "fm": data, "body": body, "rows": rows or [], "outside": outside,
            "error": problem or table_error}


def resolve(name):
    """表の名前が指す部品の種類（skill / agent / mcp）。なければ None"""
    if (pp.SKILLS_DIR / name / "SKILL.md").is_file():
        return "skill"
    if pp.agent_path(name).is_file():
        return "agent"
    for entry in mcp_entries().values():
        if name in entry["names"] and entry["folder"] and entry["folder"].is_dir():
            return "mcp"
    return None


def closure(start):
    """エージェントから「使う部品」の表をたどって、使う部品の名前を集める（エージェントは、その表もたどる）"""
    seen, todo, missing = {start}, [start], []
    while todo:
        name = todo.pop()
        if not pp.agent_path(name).is_file():
            missing.append(name)
            continue
        for row in agent_info(name)["rows"]:
            part = row[1]
            if part in seen:
                continue
            seen.add(part)
            kind = resolve(part)
            if kind == "agent":
                todo.append(part)
            elif kind is None:
                missing.append(part)
    return seen, missing


def defaults_or_skip(case):
    data = pp.defaults()
    if data is None:
        case.skipTest("defaults.json がまだありません")
    return data


class Stage1Independence(unittest.TestCase):
    """1段階目：スキルと MCP サーバーは、それぞれのフォルダだけで仕事ができ、ほかの部品を知らない"""

    def setUp(self):
        self.parts = stage1_parts()
        if not self.parts:
            self.skipTest("1段階目の部品がまだありません")

    def test_no_mention_of_other_parts(self):
        problems = []
        for folder, own, kind in self.parts:
            patterns = part_name_patterns(exclude=own) + plugin_patterns() + upper_patterns()
            patterns += script_patterns(own=own_python_files(folder))
            for path in pp.text_files(folder):
                problems += pp.hits_in_text(pp.rel(path), without_listed_words(path, pp.read(path)), patterns)
        self.assertEqual(problems, [], "1段階目の部品が、プラグイン・ほかの部品・上の段階のことを書いています：\n" + "\n".join(problems))

    def test_no_path_outside_the_folder(self):
        problems = []
        for folder, _, _ in self.parts:
            problems += outside_problems(folder)
        self.assertEqual(problems, [], "1段階目の部品が、フォルダの外を指しています：\n" + "\n".join(problems))

    def test_readme_tests_and_evals_inside(self):
        """README.md・tests/・evals/ がフォルダの中にある（仕様 REQ-101）"""
        for folder, _, _ in self.parts:
            with self.subTest(part=pp.rel(folder)):
                self.assertTrue((folder / "README.md").is_file(), "README.md がありません")
                self.assertTrue(RUNNER.has_tests(folder), "tests/ に試験がありません")
                self.assertTrue((folder / "evals").is_dir(), "evals/ がありません")


class CopyAlone(unittest.TestCase):
    """部品のフォルダだけを別の場所にコピーして、その部品の試験が通る（仕様 REQ-002、REQ-603）。
    スキルは、名前を変えたコピーでも通る（SKILL.md の name だけを新しい名前にする）。
    2段階目と3段階目の組は、組の部品がどれもこの試験に通り、組が名前で閉じていること（Stage2And3Sets）で確かめる"""

    def test_each_part_passes_its_tests_alone(self):
        if SLOW:
            self.skipTest("SKIP_SLOW_TESTS=1 のため飛ばしました")
        jobs = []
        for folder in [pp.SKILLS_DIR / n for n in pp.skill_names()] + pp.mcp_dirs():
            if not RUNNER.has_tests(folder):
                continue
            jobs.append((folder, folder.name))
            if folder.parent == pp.SKILLS_DIR:
                jobs.append((folder, ("my-" + folder.name)[:64].rstrip("-")))
        if not jobs:
            self.skipTest("試験を持つ部品がまだありません")
        workers = max(1, int(os.environ.get("TESTS_JOBS") or min(4, os.cpu_count() or 1)))
        timeout = int(os.environ.get("TESTS_PART_TIMEOUT") or 900)
        with tempfile.TemporaryDirectory(prefix="lone-parts-") as tmp:
            def run(job):
                index, (folder, name) = job
                # Windows のパスの長さ（260 文字）に収まるよう、置き場所の名前は短くする
                place = Path(tmp) / f"p{index}" / name
                shutil.copytree(folder, place, ignore=pp.copy_ignore)
                if name != folder.name:
                    skill = place / "SKILL.md"
                    text = pp.read(skill)
                    skill.write_text(re.sub(r"(?m)^name:\s*.*$", f"name: {name}", text, count=1), encoding="utf-8")
                env = RUNNER.test_env({"UV_PROJECT_ENVIRONMENT": str(Path(tmp) / f"v{index}")})
                code, out, _ = RUNNER.run_command(RUNNER.part_test_command(place), place, env, timeout)
                return folder, name, code, out

            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(run, enumerate(jobs)))
        for folder, name, code, out in results:
            label = pp.rel(folder) + ("" if name == folder.name else f"（名前を {name} に変えたコピー）")
            with self.subTest(part=label):
                tail = "\n".join(out.strip().splitlines()[-40:])
                self.assertEqual(code, 0, f"{label} の試験が、フォルダだけでは通りません：\n{tail}")
                self.assertRegex(out, r"(?m)^Ran [1-9]\d* tests?", f"{label} の試験が1つも動きません")


class AgentIndependence(unittest.TestCase):
    """エージェント：部品の名前は「## 使う部品」の表にだけ書く（仕様 REQ-202、設計書 3.3）"""

    def setUp(self):
        self.names = pp.agent_names()
        if not self.names:
            self.skipTest("エージェントの定義がまだありません")

    def test_parts_table(self):
        for name in self.names:
            info = agent_info(name)
            with self.subTest(agent=name):
                self.assertIsNone(info["error"], f"{name}: {info['error']}")
                parts = [row[1] for row in info["rows"]]
                self.assertEqual(len(parts), len(set(parts)), f"{name}: 表に同じ部品が2回あります")
                for role, part, when in info["rows"]:
                    self.assertTrue(role and when, f"{name}: 表の {part} の役割か使うときが空です")
                    self.assertRegex(part, r"^[a-z0-9][a-z0-9-]*$", f"{name}: 表の部品は、そのままの名前にします（プラグインの名前や : を付けない）：{part}")
                    self.assertIsNotNone(resolve(part), f"{name}: 表の {part} が、ある部品（スキル・エージェント・MCP サーバー）を指していません")

    def test_names_only_in_the_table(self):
        problems = []
        for name in self.names:
            info = agent_info(name)
            patterns = part_name_patterns() + script_patterns() + plugin_patterns()
            problems += pp.hits_in_text(f"{pp.rel(info['path'])}（表の外）", info["outside"], patterns)
        self.assertEqual(problems, [], "エージェントの定義の、表の外に部品の名前（か中身）があります：\n" + "\n".join(problems))

    def test_does_not_name_itself(self):
        for name in self.names:
            with self.subTest(agent=name):
                found = pp.hits(pp.agent_path(name), [(name, pp.name_token(name))])
                self.assertEqual(found, [], f"{name}: 自分の名前を書いています")

    def test_mcp_tools_only_for_servers_in_the_table(self):
        known = pp.all_mcp_tool_names()
        for name in self.names:
            info = agent_info(name)
            table = {row[1] for row in info["rows"]}
            for tool in info["fm"].get("tools") or []:
                if not isinstance(tool, str) or "/" not in tool:
                    continue
                server, tool_name = tool.split("/", 1)
                with self.subTest(agent=name, tool=tool):
                    self.assertIn(server, table, f"{name}: tools の {tool} の MCP サーバーが「使う部品」の表にありません")
                    if known and tool_name != "*":
                        self.assertIn(tool_name, known, f"{name}: {tool} は、MCP サーバーにないツールです")

    def test_no_stage4_words(self):
        """エージェントは、4段階目（エクステンション）を知らない"""
        patterns = upper_patterns()[:2] + [(w, pp.token(w)) for w in ("extension", "extensions")]
        patterns.append((pp.PLUGIN_NAME + "_", re.compile(re.escape(pp.PLUGIN_NAME + "_"))))
        problems = []
        for name in self.names:
            problems += pp.hits(pp.agent_path(name), patterns)
        self.assertEqual(problems, [], "エージェントが、エクステンションのことを書いています：\n" + "\n".join(problems))

    def test_role_words(self):
        """description に、defaults.json の役割の言葉がある。その言葉は、ほかのエージェントの description にない（設計書 4.13）"""
        data = defaults_or_skip(self)
        roles = data.get("roles") or {}
        for role, word in pp.ROLE_WORDS.items():
            if role not in roles:
                continue
            owner = pp.id_stem(roles[role])
            for name in self.names:
                description = str(agent_info(name)["fm"].get("description", ""))
                with self.subTest(role=role, agent=name):
                    if name == owner:
                        self.assertIn(word, description, f"{name} の description に、役割の言葉「{word}」がありません")
                    else:
                        self.assertNotIn(word, description, f"{name} の description に、ほかの役割の言葉「{word}」があります")

    def test_worker_knows_no_stage3(self):
        """2段階目の作業役は、3段階目（統括・検証・統合のエージェント）のことを書かない"""
        data = defaults_or_skip(self)
        worker = pp.id_stem((data.get("roles") or {}).get("worker", ""))
        if not pp.agent_path(worker).is_file():
            self.skipTest("作業役の定義がまだありません")
        patterns = [(w, re.compile(re.escape(w))) for w in STAGE3_WORDS_JA] + [(w, pp.token(w)) for w in UPPER_WORDS_EN[2:]]
        found = pp.hits(pp.agent_path(worker), patterns)
        self.assertEqual(found, [], "作業役が、3段階目のことを書いています：\n" + "\n".join(found))


class Stage2And3Sets(unittest.TestCase):
    """2段階目と3段階目の組が、名前で閉じている（その組だけを取り出して使える。仕様 REQ-002、REQ-603）"""

    def extract(self, names):
        """組の部品を、利用者が置く形（.github/agents、.github/skills、mcp/）に取り出し、
        取り出した中で表の名前がたどれること、組の外の部品の名前がないことを確かめる"""
        names = set(names)
        for entry in mcp_entries().values():
            if names & entry["names"]:
                names |= entry["names"]
        with tempfile.TemporaryDirectory(prefix="stage-set-") as tmp:
            base = Path(tmp)
            skills, agents, servers = base / ".github" / "skills", base / ".github" / "agents", base / "mcp"
            for folder in (skills, agents, servers):
                folder.mkdir(parents=True)
            for name in sorted(names):
                kind = resolve(name)
                if kind == "skill":
                    shutil.copytree(pp.SKILLS_DIR / name, skills / name, ignore=pp.copy_ignore)
                elif kind == "agent":
                    shutil.copy2(pp.agent_path(name), agents / (name + pp.AGENT_SUFFIX))
                elif kind == "mcp" and not (servers / name).exists():
                    entry = next(e for e in mcp_entries().values() if name in e["names"])
                    if not any((servers / alias).exists() for alias in entry["names"]):
                        shutil.copytree(entry["folder"], servers / name, ignore=pp.copy_ignore)
            available = {p.name for p in skills.iterdir()} | {pp.agent_stem(p) for p in agents.iterdir()}
            for folder in servers.iterdir():
                available |= next((e["names"] for e in mcp_entries().values() if folder.name in e["names"]), {folder.name})
            problems = []
            for agent in sorted(agents.iterdir()):
                _, body, _ = pp.frontmatter(agent)
                rows, _ = pp.parts_table(body)
                for row in rows or []:
                    if row[1] not in available:
                        problems.append(f"{agent.name} の表の {row[1]} が、取り出した組の中にありません")
            patterns = [(n, pp.name_token(n)) for n in sorted(set(pp.all_part_names()) - names)]
            for path in pp.text_files(base):
                problems += pp.hits_in_text(path.relative_to(base).as_posix(), pp.read(path), patterns)
            return problems

    def test_stage2_worker_set(self):
        data = defaults_or_skip(self)
        worker = pp.id_stem((data.get("roles") or {}).get("worker", ""))
        if not pp.agent_path(worker).is_file():
            self.skipTest("作業役の定義がまだありません")
        names, missing = closure(worker)
        self.assertEqual(missing, [], f"作業役の表に、ない部品があります：{missing}")
        plugin = set(pp.plugin_skill_names())
        upper = [n for n in names - {worker} if resolve(n) == "agent" or n in plugin]
        self.assertEqual(upper, [], f"作業役の表に、1段階目でない部品があります（2段階目は1段階目の部品だけで動く）：{upper}")
        self.assertEqual(self.extract(names), [], "2段階目の組を取り出すと、名前がたどれません")

    def test_stage3_orchestrator_set(self):
        data = defaults_or_skip(self)
        orchestrator = pp.id_stem(data.get("orchestrator", ""))
        if not pp.agent_path(orchestrator).is_file():
            self.skipTest("統括エージェントの定義がまだありません")
        names, missing = closure(orchestrator)
        self.assertEqual(missing, [], f"3段階目の組に、ない部品があります：{missing}")
        plugin = set(pp.plugin_skill_names())
        self.assertEqual(sorted(names & plugin), [], "3段階目の組に、4段階目のスキルがあります（3段階目は4段階目を知らない）")
        for role, agent_id in (data.get("roles") or {}).items():
            with self.subTest(role=role):
                self.assertIn(pp.id_stem(agent_id), names, f"defaults.json の {role} のエージェントが、統括エージェントの表にありません")
        self.assertEqual(self.extract(names), [], "3段階目の組を取り出すと、名前がたどれません")


class PluginSkillIndependence(unittest.TestCase):
    """プラグイン全体のスキル（4段階目）：部品の名前は、SKILL.md の「## 使う部品」の表にだけ（仕様 REQ-101）"""

    def setUp(self):
        self.names = [n for n in pp.plugin_skill_names() if (pp.SKILLS_DIR / n / "SKILL.md").is_file()]
        if not self.names:
            self.skipTest("プラグイン全体のスキルがまだありません")

    def test_parts_table(self):
        for name in self.names:
            rows, error = pp.parts_table(pp.frontmatter(pp.SKILLS_DIR / name / "SKILL.md")[1])
            with self.subTest(skill=name):
                self.assertIsNone(error, f"{name}: {error}")
                for _, part, _ in rows:
                    self.assertIsNotNone(resolve(part), f"{name}: 表の {part} が、ある部品を指していません")
                    self.assertNotEqual(part, name, f"{name}: 表に自分の名前があります")

    def test_names_only_in_the_table(self):
        """プラグインの名前は数えない（4段階目のスキルは、プラグインとエクステンションのツールを知ってよい。設計書 2.1）"""
        problems = []
        for name in self.names:
            folder = pp.SKILLS_DIR / name
            patterns = part_name_patterns(exclude={name}) + script_patterns(own=own_python_files(folder))
            for path in pp.text_files(folder):
                text = pp.read(path)
                if path == folder / "SKILL.md":
                    _, text = pp.split_section_table(text, pp.PARTS_HEADING)
                problems += pp.hits_in_text(pp.rel(path) + ("（表の外）" if path.name == "SKILL.md" else ""), text, patterns)
        self.assertEqual(problems, [], "プラグイン全体のスキルの、表の外に部品の名前があります：\n" + "\n".join(problems))

    def test_no_path_outside_the_folder(self):
        problems = []
        for name in self.names:
            problems += outside_problems(pp.SKILLS_DIR / name)
        self.assertEqual(problems, [], "プラグイン全体のスキルが、フォルダの外を指しています：\n" + "\n".join(problems))


class DocumentIndependence(unittest.TestCase):
    """上の段の文書に、下の段の部品の中身（スクリプトの名前、設定の項目）を書かない（仕様 REQ-006）"""

    def upper_documents(self):
        docs = [pp.ROOT / "README.md", pp.ROOT / "CHANGELOG.md"]
        docs += sorted(pp.GUIDE_DIR.glob("*.md")) if pp.GUIDE_DIR.is_dir() else []
        docs += sorted(pp.AGENTS_DIR.glob("*" + pp.AGENT_README_SUFFIX)) if pp.AGENTS_DIR.is_dir() else []
        return [d for d in docs if d.is_file()]

    def internals(self):
        dotted, leaves = pp.config_keys()
        patterns = [(n, pp.script_token(n)) for n in sorted(pp.script_owners())]
        for entry in mcp_entries().values():
            folder = entry["folder"]
            if folder and folder.is_dir():
                patterns += [(n, pp.name_token(n)) for n in sorted(pp.console_scripts(folder))]
        patterns += [(k, pp.token(k)) for k in dotted]
        patterns += [(k, pp.token(k)) for k in leaves if "_" in k]
        return patterns

    def test_upper_documents_have_no_internals(self):
        docs = self.upper_documents()
        if not docs:
            self.skipTest("上の段の文書がまだありません")
        problems = []
        for doc in docs:
            # <output_dir> のような、値を入れる所の印（設計書 3.5 の書き方）は数えない
            text = re.sub(r"<[A-Za-z0-9_.]+>", "<…>", pp.read(doc))
            problems += pp.hits_in_text(pp.rel(doc), text, self.internals())
        self.assertEqual(problems, [], "上の段の文書に、下の段の中身（スクリプトの名前・設定の項目）があります：\n" + "\n".join(problems))

    def test_replace_doc_is_for_developers(self):
        """部品を変える手順は、開発者向けの docs/replace.md にだけ書く（利用者向けのガイドに置かない）"""
        self.assertTrue(pp.REPLACE_DOC.is_file(), "docs/replace.md がありません")
        if pp.GUIDE_DIR.is_dir():
            self.assertFalse((pp.GUIDE_DIR / pp.REPLACE_DOC.name).exists(), "部品を変える手順が、利用者向けのガイドの中にあります")


class ExtensionIndependence(unittest.TestCase):
    """エクステンション：スキルとエージェントの名前とコマンドの場所は、defaults.json にだけ（設計書 8.4）"""

    def test_names_only_in_defaults(self):
        if not pp.EXT_DIR.is_dir():
            self.skipTest("エクステンションがまだありません")
        patterns = [(n, pp.name_token(n)) for n in sorted(set(pp.skill_names()) | set(pp.agent_names()))]
        patterns += [(n, pp.script_token(n)) for n in sorted(pp.script_owners())]
        problems = []
        for path in pp.text_files(pp.EXT_DIR):
            if path == pp.DEFAULTS:
                continue
            problems += pp.hits(path, patterns)
        self.assertEqual(problems, [], "エクステンションの defaults.json の外に、部品の名前があります：\n" + "\n".join(problems))


class NoNameCollision(unittest.TestCase):
    """同じリポジトリのほかのプラグイン（元のプラグインなど）と、エージェント・MCP サーバー・エクステンションの名前が重ならない（仕様 REQ-007）"""

    def test_names_differ_from_sibling_plugins(self):
        siblings = [p for p in pp.ROOT.parent.iterdir() if p.is_dir() and p != pp.ROOT and (p / "plugin.json").is_file()] \
            if pp.ROOT.parent.is_dir() else []
        if not siblings:
            self.skipTest("ほかのプラグインがありません")
        ours_agents = set(pp.agent_names())
        ours_servers = set(pp.mcp_config().get("mcpServers") or {})
        ours_ext = {p.name for p in pp.EXTENSIONS_DIR.iterdir() if p.is_dir()} if pp.EXTENSIONS_DIR.is_dir() else set()
        for other in siblings:
            agents = {pp.agent_stem(p) for p in (other / "com.github.copilot" / "agents").glob("*" + pp.AGENT_SUFFIX)}
            data = pp.load_json(other / "mcp.json") or {}
            servers = set(data.get("mcpServers") or {}) if isinstance(data, dict) else set()
            ext_dir = other / "com.github.copilot" / "extensions"
            exts = {p.name for p in ext_dir.iterdir() if p.is_dir()} if ext_dir.is_dir() else set()
            with self.subTest(plugin=other.name):
                self.assertEqual(sorted(ours_agents & agents), [], f"{other.name} と同じ名前のエージェントがあります")
                self.assertEqual(sorted(ours_servers & servers), [], f"{other.name} と同じ登録名の MCP サーバーがあります")
                self.assertEqual(sorted(ours_ext & exts), [], f"{other.name} と同じ名前のエクステンションがあります")


class ScannerRules(unittest.TestCase):
    """この試験の、言葉を探す決まりそのもの（数えすぎも、見落としもしない）"""

    def count(self, patterns, text):
        return [w for w, pattern in patterns if pattern.search(text)]

    def test_english_upper_words_count_as_words_only(self):
        patterns = upper_patterns()
        for text in ("typing-extensions==4.12", "typing_extensions", "my-extension-lib", "extension_id = 1"):
            with self.subTest(text=text):
                self.assertEqual(self.count(patterns, text), [], "単語の一部は数えません")
        for text in ("an extension of the screen", "Extensions", "the ORCHESTRATOR"):
            with self.subTest(text=text):
                self.assertTrue(self.count(patterns, text), "単語としての出現は数えます")

    def test_generated_and_vendored_files_are_not_read(self):
        with tempfile.TemporaryDirectory(prefix="scanner-") as tmp:
            folder = Path(tmp)
            (folder / "uv.lock").write_text('name = "typing-extensions"\nextension\n', encoding="utf-8")
            (folder / "__pycache__").mkdir()
            (folder / "__pycache__" / "x.py").write_text("extension\n", encoding="utf-8")
            (folder / "README.md").write_text("x\n", encoding="utf-8")
            self.assertEqual([p.name for p in pp.text_files(folder)], ["README.md"])

    def test_only_the_default_output_dir_may_point_outside(self):
        default = pp.output_dir_default()
        if not default or not default.startswith("../"):
            self.skipTest("設計書 4.1 の output_dir の既定が ../ で始まりません")
        name = default[3:]
        patterns = outside_patterns()[:1]
        for text in (f"output_dir: {default}", f"既定は `{default}`。", f'DEFAULT = "{default}"', f"{default}/<日時>/",
                     'if part == "../":'):
            with self.subTest(text=text):
                self.assertEqual(self.count(patterns, text), [], "既定の値と、言葉だけの文字列は許します")
        for text in (f"../{name}side/x", "../other/file.md", "see ../README.md", f"output_dir: ../{name}2"):
            with self.subTest(text=text):
                self.assertTrue(self.count(patterns, text), "ほかの ../ は数えます")


class GeneratedBriefs(unittest.TestCase):
    """一括実行が書く指示ファイルと、launch の頼みの文に、部品の名前（スキル・エージェント・MCP サーバーの登録名・ツールの名前・
    プラグインの名前）がない（設計書 4.14：「スキルやエージェントの名前は書かない」）。名前は木から読む"""

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
        run_py = fl.command_script("runCommand")
        created = fl.run_script(run_py, "new-run", "--repo", str(repo), "--issues-file", str(issues), "--review", "--integration",
                                env=env, cwd=repo)
        run_dir = Path(created["run_dir"])
        cls.agents = fl.FakeAgents(cids, review_outcomes={str(cids["C"]): ["rework", "pass"]})
        cls.orchestrator = fl.Orchestrator(run_py, run_dir, env, repo, cls.agents)
        try:
            cls.orchestrator.drive(limit_seconds=150)
        finally:
            fl.run_script(run_py, "stop", "--run", str(run_dir), env=env, cwd=repo, ok=None, timeout=60)
        cls.run_dir = run_dir

    def patterns(self):
        names = set(pp.all_part_names())
        for entry in mcp_entries().values():
            names |= entry["inner"]
        return [(n, pp.name_token(n)) for n in sorted(names)] + plugin_patterns()

    def test_every_role_got_an_instruction_file(self):
        roles = {a.get("role") for a in self.orchestrator.launches()}
        self.assertEqual(roles, {"worker", "reviewer", "integrator"}, "作業・確かめ・統合の指示ファイルが揃いません")
        self.assertTrue(any("## 確かめの指摘" in p.read_text(encoding="utf-8") for p in self.agents.briefs),
                        "作り直しの指示ファイルがありません")

    def test_briefs_and_prompts_name_no_part(self):
        files = sorted(set(self.agents.briefs) | set(self.run_dir.glob("items/*/brief.md"))
                       | set(self.run_dir.glob("agents/*/*.md")))
        self.assertTrue(files, "指示ファイルがありません")
        problems = []
        for path in files:
            problems += pp.hits_in_text(path.relative_to(self.run_dir).as_posix(), pp.read(path), self.patterns())
        for action in self.orchestrator.launches():
            text = "\n".join(str(action.get(k, "")) for k in ("prompt", "description"))
            problems += pp.hits_in_text(f"launch {action.get('task')}", text, self.patterns())
        self.assertEqual(problems, [], "指示ファイルか頼みの文に、部品の名前があります（役割を、ふつうの言葉で書きます）：\n"
                         + "\n".join(problems))

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
