"""部品の独立性を確かめる試験（憲章 第25条、仕様 REQ-001〜REQ-005、REQ-102、REQ-106、REQ-108）。

確かめること：
- 1段階目：4つのスキルは、それぞれのフォルダだけで完結する（利用者は、プラグインを入れずに、
  スキルのフォルダだけを自分の作業フォルダの .github/skills/<名前>/ に写して使う）。
  - フォルダの中に、プラグインの名前（とその略）・ほかのスキル・エージェント・Web管理画面・
    エクステンションを書いていない。フォルダの外を指していない。互いのファイルを読み込まない。
  - 人向けの使い方（README.md）と試験（tests/）が、フォルダの中にある。
  - フォルダだけを別の場所に写しても（名前を変えて写しても）、設定ファイルの用意・設定の確かめ・
    そのスキルの試験が動き、データは作業フォルダの .<フォルダの名前>/ にだけできる。
- 2段階目：作業エージェントは、スキルの名前を「使うスキル」の表にだけ書く。スキルの中身
  （スクリプトの名前、設定の項目、設定ファイルの置き場所、転送やログ取得のやり方）を書いていない。
- 3段階目：統括エージェント・検証エージェント・エクステンションは、機器・ビルド・転送・ログ取得の
  やり方やスキルの名前を書いていない。作業エージェントの名前は、設定のデータ（defaults.json）にだけある。

  python3 -m unittest discover -s tests/independence -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / "skills"
AGENTS_DIR = ROOT / "com.github.copilot" / "agents"
EXT_DIR = ROOT / "com.github.copilot" / "extensions" / "embedded-debug-loop"
SKILL_NAMES = ["setup", "cross-compile", "deploy", "log-capture"]
WORK_SKILLS = {"cross-compile": "build.py", "deploy": "deploy.py", "log-capture": "capture.py"}
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".mjs", ".js", ".html", ".css", ".txt", ".sh"}
PLUGIN_NAME = ROOT.name  # embedded-debug-loop
# スキルのフォルダに書いてはいけない、プラグインを指す言葉
PLUGIN_WORDS = [
    (PLUGIN_NAME, re.compile(re.escape(PLUGIN_NAME), re.IGNORECASE)),
    (PLUGIN_NAME.replace("-", "_"), re.compile(re.escape(PLUGIN_NAME.replace("-", "_")), re.IGNORECASE)),
    ("EDL（プラグインの略）", re.compile(r"(?<![A-Za-z0-9])EDL(?![a-z0-9])|(?<![A-Za-z0-9])edl(?=[_\-])", re.IGNORECASE)),
]


def text_files(folder):
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix in TEXT_SUFFIXES and "__pycache__" not in path.parts:
            yield path


def token(word):
    """英数字とハイフンに挟まれていない、単語としての出現（environment-setup-… の setup は数えない）"""
    return re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(word) + r"(?![A-Za-z0-9_\-])", re.IGNORECASE)


def name_token(word):
    """スキルの名前としての出現。大文字と小文字を分ける（unittest の setUp は数えない）"""
    return re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(word) + r"(?![A-Za-z0-9_\-])")


def hits(path, patterns):
    found = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for label, pattern in patterns:
            if pattern.search(line):
                found.append(f"{path.relative_to(ROOT)}:{lineno}: 「{label}」 … {line.strip()[:120]}")
    return found


def template_keys(skill):
    keys = []
    template = SKILLS_DIR / skill / "config-template.yaml"
    if template.exists():
        for line in template.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
            if m:
                keys.append(m.group(1))
    return keys


class SkillIndependence(unittest.TestCase):
    """1段階目：スキルどうしが互いを知らない"""

    def test_skills_exist(self):
        for skill in SKILL_NAMES:
            self.assertTrue((SKILLS_DIR / skill / "SKILL.md").is_file(), f"スキル {skill} の SKILL.md がありません")

    def test_no_mention_of_other_parts(self):
        problems = []
        for skill in SKILL_NAMES:
            others = [s for s in SKILL_NAMES if s != skill]
            patterns = [(name, name_token(name)) for name in others]
            patterns += [(w, re.compile(re.escape(w))) for w in [
                "device-worker", "orchestrator", "verifier", "作業エージェント", "統括", "検証エージェント",
                "Web管理画面", "管理画面", "エクステンション", "extension"]]
            patterns += PLUGIN_WORDS
            for path in text_files(SKILLS_DIR / skill):
                problems += hits(path, patterns)
        self.assertEqual(problems, [], "スキルが、プラグイン・ほかのスキル・エージェントのことを書いています：\n" + "\n".join(problems))

    def test_description_names_no_slash_command(self):
        """SKILL.md の description に、スラッシュコマンドの名前を書かない（名前を変えて写すと食い違うため）"""
        for skill in SKILL_NAMES:
            text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
            m = re.search(r"^description:\s*(.*)$", text, re.M)
            self.assertIsNotNone(m, f"{skill} の SKILL.md に description がありません")
            found = re.findall(r"(?<![\w./])/[a-z][a-z0-9-]*", m.group(1))
            self.assertEqual(found, [], f"{skill} の description に、スラッシュコマンドの名前があります：{found}")

    def test_no_path_outside_the_folder(self):
        """スキルのフォルダの外を指さない（../ で外へ出る、プラグインの根や tests/ を指す）"""
        patterns = [("../", re.compile(r"(?<!\.)\.\.[/\\]")),  # 「...\n」のような省略の点は数えない
                    ("parents[2] 以上", re.compile(r"parents\[[2-9]\]")),
                    ("プラグインの定義", re.compile(r"plugin\.json|com\.github\.copilot")),
                    ("プラグインの根の環境変数", re.compile(r"PLUGIN_ROOT|CLAUDE_PLUGIN|COPILOT_PLUGIN")),
                    ("プラグインの tests/", re.compile(r"tests/(independence|extension|skills)"))]
        problems = []
        for skill in SKILL_NAMES:
            for path in text_files(SKILLS_DIR / skill):
                problems += hits(path, patterns)
        self.assertEqual(problems, [], "スキルが、フォルダの外を指しています：\n" + "\n".join(problems))

    def test_readme_and_tests_inside(self):
        """人向けの使い方と試験が、スキルのフォルダの中にある"""
        for skill in SKILL_NAMES:
            with self.subTest(skill=skill):
                readme = SKILLS_DIR / skill / "README.md"
                self.assertTrue(readme.is_file(), f"スキル {skill} に README.md がありません")
                text = readme.read_text(encoding="utf-8")
                for must in [".github/skills/", "~/.copilot/skills/", "/skills reload", "python3 -m unittest discover -s tests"]:
                    self.assertIn(must, text, f"{skill} の README.md に「{must}」がありません")
                self.assertTrue(list((SKILLS_DIR / skill / "tests").glob("test_*.py")),
                                f"スキル {skill} の試験が、フォルダの中（tests/）にありません")
                if skill in WORK_SKILLS:
                    self.assertIn(f".{skill}/", text, f"{skill} の README.md に、データの置き場所 .{skill}/ がありません")

    def test_no_shared_code(self):
        problems = []
        for skill in SKILL_NAMES:
            others = [s for s in SKILL_NAMES if s != skill]
            for path in text_files(SKILLS_DIR / skill):
                if path.suffix != ".py":
                    continue
                text = path.read_text(encoding="utf-8")
                for o in others:
                    if re.search(r"skills[/\\]" + re.escape(o), text):
                        problems.append(f"{path.relative_to(ROOT)}: スキル {o} のファイルを参照しています")
                if re.search(r"sys\.path\.(insert|append)\([^)]*\.\.", text):
                    problems.append(f"{path.relative_to(ROOT)}: 自分のフォルダの外を読み込みの道に足しています")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_each_work_skill_runs_alone(self):
        """スキルのフォルダ1つだけを、ほかに何もない場所に写して（名前を変えた写しも）、設定ファイルの用意と確かめを動かす"""
        for skill, script in WORK_SKILLS.items():
            for copied_name in (skill, f"my-{skill}"):
                with self.subTest(skill=skill, copied_as=copied_name), tempfile.TemporaryDirectory() as tmp:
                    lone = Path(tmp) / "home" / ".copilot" / "skills" / copied_name
                    shutil.copytree(SKILLS_DIR / skill, lone, ignore=shutil.ignore_patterns("__pycache__"))
                    before = sorted(str(p.relative_to(lone)) for p in lone.rglob("*"))
                    work = Path(tmp) / "project"
                    work.mkdir()
                    run = lambda *args: subprocess.run(
                        [sys.executable, str(lone / "scripts" / script), *args],
                        capture_output=True, text=True, timeout=60, cwd=work,
                        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                    made = run("config-path")
                    self.assertEqual(made.returncode, 0, f"{skill} の config-path が失敗：{made.stderr[-500:]}")
                    result = json.loads(made.stdout)
                    expected = work / f".{copied_name}" / "config.yaml"
                    self.assertEqual(Path(result["config"]).resolve(), expected.resolve())
                    self.assertTrue(expected.is_file(), f"{skill} が設定ファイルを作っていません")
                    self.assertTrue(result["created"])
                    self.assertIsInstance(result.get("missing"), list)
                    self.assertEqual((work / f".{copied_name}" / ".gitignore").read_text(encoding="utf-8").strip(), "*")
                    checked = run("check")
                    data = json.loads(checked.stdout)
                    self.assertIn("ok", data)
                    for out in (result, data):
                        if "skill" in out:
                            self.assertEqual(out["skill"], copied_name, f"{skill}: 結果の skill が、写したフォルダの名前ではありません")
                    for line in (made.stderr + checked.stderr).splitlines():
                        m = re.match(r"^\[([^\]]+)\] ", line)
                        if m:
                            self.assertEqual(m.group(1), copied_name, f"{skill}: 経過の表示の頭が、写したフォルダの名前ではありません：{line}")
                    logs = work / f".{copied_name}" / "logs"
                    if logs.is_dir():
                        for log in logs.iterdir():
                            self.assertTrue(log.name.startswith(copied_name + "-"), f"{skill}: ログの名前が、写したフォルダの名前で始まりません：{log.name}")
                    if result["missing"]:
                        self.assertFalse(data["ok"], f"{skill}: 必須の項目が空なのに、確かめが ok になりました")
                        self.assertTrue(data.get("problems"), f"{skill}: 問題の説明がありません")
                    self.assertEqual(sorted(p.name for p in work.iterdir()), [f".{copied_name}"],
                                     f"{skill}: 作業フォルダに、.{copied_name}/ のほかのものができました")
                    self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["home", "project"],
                                     f"{skill}: 作業フォルダの外に、何かができました")
                    after = sorted(str(p.relative_to(lone)) for p in lone.rglob("*"))
                    self.assertEqual(after, before, f"{skill}: スキルのフォルダの中が変わりました")

    def test_each_skill_tests_pass_alone(self):
        """スキルのフォルダだけを写した場所で、README に書いた動かし方で、そのスキルの試験が通る"""
        for skill in SKILL_NAMES:
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as tmp:
                lone = Path(tmp) / "lone" / skill
                shutil.copytree(SKILLS_DIR / skill, lone, ignore=shutil.ignore_patterns("__pycache__"))
                done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                      capture_output=True, text=True, timeout=900, cwd=lone,
                                      env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                self.assertEqual(done.returncode, 0, f"{skill} の試験が、フォルダだけでは通りません：\n{done.stderr[-2000:]}")
                self.assertRegex(done.stderr, r"Ran [1-9]\d* tests?")

    def test_setup_runs_alone_on_unknown_yaml(self):
        """設定のスキルは、どのスキルのものでもない YAML でも動く。作業フォルダに何も作らない"""
        with tempfile.TemporaryDirectory() as tmp:
            lone = Path(tmp) / "skills" / "my-setup"
            shutil.copytree(SKILLS_DIR / "setup", lone)
            work = Path(tmp) / "project"
            (work / "conf").mkdir(parents=True)
            cfg = work / "conf" / "kettle.yaml"
            cfg.write_text("# 湯わかし器の設定\n\n# 湯の温度（必須）\n# 例：90\ntemperature: \n\n# 名前\nname: \"台所\"\n", encoding="utf-8")
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            tool = [sys.executable, str(lone / "scripts" / "config_tool.py")]
            listed = json.loads(subprocess.run(tool + ["list"], capture_output=True, text=True, check=True, cwd=work, env=env).stdout)
            self.assertIn("kettle.yaml", json.dumps(listed, ensure_ascii=False),
                          "設定のスキルが、作業フォルダの中の YAML を候補に出しません")
            shown = json.loads(subprocess.run(tool + ["show", str(cfg)], capture_output=True, text=True, check=True, cwd=work, env=env).stdout)
            keys = [item["key"] for item in shown["items"]]
            self.assertEqual(keys, ["temperature", "name"])
            subprocess.run(tool + ["set", str(cfg), "temperature", "--stdin", "--json"], input="85\n",
                           capture_output=True, text=True, check=True, cwd=work, env=env)
            text = cfg.read_text(encoding="utf-8")
            self.assertIn("# 湯の温度（必須）", text)
            self.assertRegex(text, r"(?m)^temperature: 85$")
            self.assertEqual(sorted(p.name for p in work.iterdir()), ["conf"], "設定のスキルが、作業フォルダに何かを作りました")


class WorkerIndependence(unittest.TestCase):
    """2段階目：作業エージェントは、スキルの名前と役割だけを知る"""

    def setUp(self):
        self.path = AGENTS_DIR / "device-worker.agent.md"
        self.text = self.path.read_text(encoding="utf-8")

    def skills_table(self):
        m = re.search(r"^## 使うスキル\n(.*?)(?=^## )", self.text, re.S | re.M)
        self.assertIsNotNone(m, "作業エージェントに「## 使うスキル」の節がありません")
        return m.group(1)

    def test_skill_names_only_in_table(self):
        table = self.skills_table()
        outside = self.text.replace(table, "")
        for skill in SKILL_NAMES:
            self.assertIn(f"`{skill}`", table, f"表に {skill} がありません（skill ツールで使う名前で書く）")
            self.assertIsNone(token(skill).search(outside), f"表の外に、スキルの名前 {skill} があります")

    def test_no_skill_internals(self):
        forbidden = list(WORK_SKILLS.values()) + ["config_tool", "config-template", ".embedded-debug-loop", ".yaml",
                                                  "FTP", "ftp", "シリアル", "serial", "scp", "Makefile", "SITE CHMOD"]
        for skill in WORK_SKILLS:
            forbidden += [k for k in template_keys(skill) if len(k) > 3]
        found = [w for w in forbidden if token(w).search(self.text)]
        # 設定ファイルやログの名前・場所は、字の並びとして探す（config.yaml の .yaml も見つける）
        found += [w for w in [".yaml", ".yml", "config.yaml", "logs/"] if w in self.text]
        self.assertEqual(found, [], f"作業エージェントが、スキルの中身を書いています：{found}")

    def test_no_upper_layer(self):
        found = [w for w in ["Web管理画面", "統括", "検証エージェント", "orchestrator", "verifier", "embedded-debug-loop_"]
                 if w in self.text]
        self.assertEqual(found, [], f"作業エージェントが、3段階目のことを書いています：{found}")


class OrchestrationIndependence(unittest.TestCase):
    """3段階目：統括・検証・エクステンションは、機器のことを知らない"""

    WORDS = ["FTP", "シリアル", "serial", "クロスコンパイル", "Makefile", "make", "IPアドレス", "IP アドレス",
             "デプロイ", "deploy", "転送", "ビルド", "build", "機器", "device", "board", "ボード", "sensor",
             "ログイン", "chmod", "crc32"]

    def files(self):
        yield AGENTS_DIR / "orchestrator.agent.md"
        yield AGENTS_DIR / "verifier.agent.md"
        if EXT_DIR.exists():
            for path in text_files(EXT_DIR):
                if path.name != "defaults.json":
                    yield path

    def test_no_device_words(self):
        patterns = [(w, token(w)) for w in self.WORDS + SKILL_NAMES + ["device-worker"]]
        problems = []
        for path in self.files():
            problems += hits(path, patterns)
        self.assertEqual(problems, [], "3段階目に、機器やスキルのことが書かれています：\n" + "\n".join(problems))

    def test_worker_name_only_in_defaults(self):
        defaults = EXT_DIR / "defaults.json"
        self.assertTrue(defaults.is_file(), "defaults.json がありません")
        data = json.loads(defaults.read_text(encoding="utf-8"))
        self.assertTrue(data.get("worker"), "defaults.json に作業エージェントの名前がありません")
        for path in self.files():
            self.assertNotIn(data["worker"], path.read_text(encoding="utf-8"), f"{path.relative_to(ROOT)} に作業エージェントの名前が直書きされています")


if __name__ == "__main__":
    unittest.main()
