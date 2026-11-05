"""部品の独立性を確かめるテスト（憲章 第25条、仕様 REQ-001〜REQ-007、REQ-102、REQ-106、REQ-108、REQ-141）。

確かめること：
- 1段階目：4つのスキルは、それぞれのフォルダだけで完結する（利用者は、プラグインをインストールせずに、
  スキルのフォルダだけを自分の作業フォルダの .github/skills/<名前>/ にコピーして使う）。
  - フォルダの中に、プラグインの名前（とその略）・ほかのスキル・エージェント・Web管理画面・
    エクステンションを書いていない。フォルダの外を指していない。互いのファイルを読み込まない。
    同梱した他者のコード（scripts/vendor/）は中身を変えないので、言葉の確認からは外す。
  - 人向けの使い方（README.md）とテスト（tests/）が、フォルダの中にある。
  - フォルダだけを別の場所にコピーしても（名前を変えても）、設定ファイルの用意・設定の確認・
    そのスキルのテストが動き、データは作業フォルダの .<フォルダの名前>/ にだけできる。
- 2段階目：作業エージェントは、スキルの名前を「使うスキル」の表にだけ書く。スキルの中身
  （スクリプトの名前、設定の項目、設定ファイルの置き場所、転送やログ取得の方法）を書いていない。
- 3段階目：統括エージェント・検証エージェント・エクステンションは、機器・ビルド・転送・ログ取得の
  方法やスキルの名前を書いていない。作業エージェントの名前は、設定のデータ（defaults.json）にだけある。
- 文書：上の段の文書（プラグインの README、作業エージェントの README の統括・検証の部分、
  3段階目のガイド、置き換えのガイド）に、スキルの名前と、転送・ログ取得の具体の言葉がない。
- 置き換え：スキルを、別の中身・別の設定の形のスキルに置き換えても（作業エージェントごと置き換えても）、
  setup の skills がそれを見つけ、作業エージェント・統括エージェントの定義を変えずに済む。

  python3 -m unittest discover -s tests/independence -v
"""

import codecs
import json
import locale
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
GUIDE_DIR = ROOT / "docs" / "guide"
WORKER_README = AGENTS_DIR / "README.markdown"
SKILL_NAMES = ["setup", "cross-compile", "deploy", "log-capture"]
WORK_SKILLS = {"cross-compile": "build.py", "deploy": "deploy.py", "log-capture": "capture.py"}
TEXT_SUFFIXES = {".md", ".markdown", ".py", ".yaml", ".yml", ".json", ".mjs", ".js", ".html", ".css", ".txt", ".sh"}
PLUGIN_NAME = ROOT.name  # embedded-debug-loop
# スキルのフォルダに書いてはいけない、プラグインを指す言葉
PLUGIN_WORDS = [
    (PLUGIN_NAME, re.compile(re.escape(PLUGIN_NAME), re.IGNORECASE)),
    (PLUGIN_NAME.replace("-", "_"), re.compile(re.escape(PLUGIN_NAME.replace("-", "_")), re.IGNORECASE)),
    ("EDL（プラグインの略）", re.compile(r"(?<![A-Za-z0-9])EDL(?![a-z0-9])|(?<![A-Za-z0-9])edl(?=[_\-])", re.IGNORECASE)),
]
# 上の段の文書に書いてはいけない、転送・ログ取得・ビルドの具体の言葉（ラベル, 正規表現）
SPECIFIC_WORDS = [
    ("FTP", re.compile(r"(?<![A-Za-z0-9_\-])[ST]?FTP(?![A-Za-z0-9_\-])", re.IGNORECASE)),
    ("シリアル", re.compile(r"シリアル")),
    ("serial", re.compile(r"serial", re.IGNORECASE)),
    ("UART", re.compile(r"(?<![A-Za-z0-9_\-])UART(?![A-Za-z0-9_\-])", re.IGNORECASE)),
    ("COM ポート", re.compile(r"(?<![A-Za-z0-9_\-.])COM\d*(?![A-Za-z0-9_\-])")),
    ("ポート", re.compile(r"(?<![サレンス])ポート")),  # サポート・レポート・インポート・エクスポートは数えない
    ("IP アドレス", re.compile(r"IP\s?アドレス", re.IGNORECASE)),
    ("scp", re.compile(r"(?<![A-Za-z0-9_\-])scp(?![A-Za-z0-9_\-])", re.IGNORECASE)),
    ("ssh", re.compile(r"(?<![A-Za-z0-9_\-])ssh(?![A-Za-z0-9_\-])", re.IGNORECASE)),
    ("telnet", re.compile(r"telnet", re.IGNORECASE)),
    ("ttyUSB など", re.compile(r"tty(USB|S|ACM)", re.IGNORECASE)),
    ("SITE CHMOD", re.compile(r"SITE\s+CHMOD", re.IGNORECASE)),
    ("chmod", re.compile(r"(?<![A-Za-z0-9_\-])chmod(?![A-Za-z0-9_\-])", re.IGNORECASE)),
    ("ボーレート", re.compile(r"ボーレート|baud", re.IGNORECASE)),
    ("SDK", re.compile(r"(?<![A-Za-z0-9_\-])SDK(?![A-Za-z0-9_\-])")),
    ("Yocto", re.compile(r"Yocto", re.IGNORECASE)),
    ("サム値", re.compile(r"サム値|チェックサム|crc32|sum32", re.IGNORECASE)),
    ("設定ファイルの名前", re.compile(r"config\.ya?ml|\.ya?ml(?![A-Za-z0-9])", re.IGNORECASE)),
    ("昇格の確認", re.compile(r"elevated permissions", re.IGNORECASE)),
]


def text_files(folder, skip_vendor=False):
    for path in sorted(folder.rglob("*")):
        if not (path.is_file() and path.suffix in TEXT_SUFFIXES and "__pycache__" not in path.parts):
            continue
        if skip_vendor and "vendor" in path.relative_to(folder).parts:
            continue
        yield path


def token(word):
    """英数字とハイフンに挟まれていない、単語としての出現（environment-setup-… の setup は数えない）"""
    return re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(word) + r"(?![A-Za-z0-9_\-])", re.IGNORECASE)


def name_token(word):
    """スキルの名前としての出現。大文字と小文字を分ける（unittest の setUp は数えない）"""
    return re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(word) + r"(?![A-Za-z0-9_\-])")


def hits_in_text(label_path, text, patterns):
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern in patterns:
            if pattern.search(line):
                found.append(f"{label_path}:{lineno}: 「{label}」 … {line.strip()[:120]}")
    return found


def hits(path, patterns):
    return hits_in_text(path.relative_to(ROOT), path.read_text(encoding="utf-8", errors="replace"), patterns)


def template_keys(skill):
    keys = []
    template = SKILLS_DIR / skill / "config-template.yaml"
    if template.exists():
        for line in template.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
            if m:
                keys.append(m.group(1))
    return keys


def all_skill_names():
    """skills/ の下のスキル（SKILL.md があるフォルダ）の名前。上の段の文書に書いてはいけない"""
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


def section(text, heading):
    """「## 見出し」から、次の「## 」の手前まで"""
    m = re.search(r"^## " + re.escape(heading) + r"\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1) if m else None


def table_rows(text):
    """Markdown の表の行（見出しと区切りの行は除く）を、セルの並びで返す"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|[\s:\-|]+\|$", line):
            continue
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows[1:] if rows else rows


def skill_table(text):
    """「使うスキル」の表から、役割 → スキルの名前（`名前` か [`名前`](…)）"""
    body = section(text, "使うスキル")
    if body is None:
        return None
    out = {}
    for role, cell in table_rows(body):
        m = re.search(r"`([^`]+)`", cell)
        if m:
            out[role] = m.group(1)
    return out


def run_tool(script, *args, cwd, env=None, stdin=None):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, timeout=120,
                          cwd=str(cwd), input=stdin,
                          env=env or dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))


def make_skill(folder, name, description, readme=None, files=None):
    """テスト用のスキルのフォルダを作る"""
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: \"{description}\"\n---\n\n# {name}\n",
                                     encoding="utf-8")
    if readme is not None:
        (folder / "README.md").write_text(readme, encoding="utf-8")
    for rel, body in (files or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(body, encoding="utf-8")
    return folder


def list_skills(setup_dir, workdir, copilot_home, home):
    """setup の skills で見つかるスキルの一覧（ホームフォルダも COPILOT_HOME も、テスト用の場所にする）"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COPILOT_HOME=str(copilot_home),
               HOME=str(home), USERPROFILE=str(home))
    done = run_tool(setup_dir / "scripts" / "config_tool.py", "skills", cwd=workdir, env=env)
    if done.returncode != 0:
        raise AssertionError(f"setup の skills が失敗しました：{done.stdout[-500:]}{done.stderr[-500:]}")
    data = json.loads(done.stdout)
    return data["skills"]


def same_path(a, b):
    return a is not None and b is not None and Path(a).resolve() == Path(b).resolve()


def default_output_encoding():
    """子の Python が、パイプに書くときの既定の文字コード（PYTHONIOENCODING があればそれ）"""
    for name in (os.environ.get("PYTHONIOENCODING", "").split(":")[0].strip(), locale.getpreferredencoding(False)):
        try:
            if name:
                return codecs.lookup(name).name
        except LookupError:
            pass
    return "utf-8"


def console_code_page():
    """Windows で、コンソールの出力の文字コード（子のプロセスも同じコンソールを使う）。なければ None"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        return codecs.lookup("cp%d" % cp).name if cp else None
    except Exception:
        return None


# Windows でパイプに書くとき、コンソールの文字コードを使うスキル（設計書 3.8）。ほかは Python の既定
CONSOLE_ENCODING_SKILLS = {"log-capture"}


def output_encoding(skill):
    """スキルのスクリプトが、標準出力と標準エラーに使う文字コード"""
    if skill in CONSOLE_ENCODING_SKILLS:
        return console_code_page() or default_output_encoding()
    return default_output_encoding()


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
            for path in text_files(SKILLS_DIR / skill, skip_vendor=True):
                problems += hits(path, patterns)
        self.assertEqual(problems, [], "スキルが、プラグイン・ほかのスキル・エージェントのことを書いています：\n" + "\n".join(problems))

    def test_vendor_is_third_party_with_license(self):
        """同梱した他者のコードは scripts/vendor/ にだけ置き、ライセンスの文書をそろえる"""
        for skill in SKILL_NAMES:
            for vendor in (SKILLS_DIR / skill).rglob("vendor"):
                if not vendor.is_dir() or "__pycache__" in vendor.parts:
                    continue
                with self.subTest(skill=skill, vendor=str(vendor.relative_to(ROOT))):
                    self.assertEqual(vendor.relative_to(SKILLS_DIR / skill).parts, ("scripts", "vendor"),
                                     "同梱のコードは scripts/vendor/ に置きます")
                    for package in (p for p in vendor.iterdir() if p.is_dir() and p.name != "__pycache__"):
                        self.assertTrue(list(package.glob("LICENSE*")), f"{package.relative_to(ROOT)} にライセンスの文書がありません")

    def test_description_names_no_slash_command(self):
        """SKILL.md の description に、スラッシュコマンドの名前を書かない（名前を変えてコピーすると食い違うため）"""
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
        """人向けの使い方とテストが、スキルのフォルダの中にある"""
        for skill in SKILL_NAMES:
            with self.subTest(skill=skill):
                readme = SKILLS_DIR / skill / "README.md"
                self.assertTrue(readme.is_file(), f"スキル {skill} に README.md がありません")
                text = readme.read_text(encoding="utf-8")
                for label, pattern in [(".github/skills/", r"\.github[/\\]skills"),
                                       ("~/.copilot/skills/", r"\.copilot[/\\]skills"),
                                       ("/skills reload", r"/skills reload"),
                                       ("-m unittest discover -s tests", r"-m unittest discover -s tests")]:
                    self.assertRegex(text, pattern, f"{skill} の README.md に「{label}」がありません")
                self.assertTrue(list((SKILLS_DIR / skill / "tests").glob("test_*.py")),
                                f"スキル {skill} のテストが、フォルダの中（tests/）にありません")
                if skill in WORK_SKILLS:
                    self.assertIn(f".{skill}", text, f"{skill} の README.md に、データの置き場所 .{skill}/ がありません")

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
                    problems.append(f"{path.relative_to(ROOT)}: 自分のフォルダの外を sys.path に足しています")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_each_work_skill_runs_alone(self):
        """スキルのフォルダ1つだけを、ほかに何もない場所にコピーして（名前を変えたコピーも）、設定ファイルの用意と確認を動かす"""
        for skill, script in WORK_SKILLS.items():
            for copied_name in (skill, f"my-{skill}"):
                with self.subTest(skill=skill, copied_as=copied_name), tempfile.TemporaryDirectory() as tmp:
                    lone = Path(tmp) / "home" / ".copilot" / "skills" / copied_name
                    shutil.copytree(SKILLS_DIR / skill, lone, ignore=shutil.ignore_patterns("__pycache__"))
                    before = sorted(str(p.relative_to(lone)) for p in lone.rglob("*"))
                    work = Path(tmp) / "project"
                    work.mkdir()
                    encoding = output_encoding(skill)

                    def run(*args):
                        done = subprocess.run([sys.executable, str(lone / "scripts" / script), *args],
                                              capture_output=True, timeout=60, cwd=work,
                                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                        try:
                            done.stdout = done.stdout.decode(encoding)
                        except UnicodeDecodeError as error:
                            self.fail(f"{skill} の標準出力が、{encoding} で読めません（設計書 3.8）：{error}")
                        done.stderr = done.stderr.decode(encoding, errors="replace")
                        return done

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
                            self.assertEqual(out["skill"], copied_name, f"{skill}: 結果の skill が、コピーしたフォルダの名前ではありません")
                    for line in (made.stderr + checked.stderr).splitlines():
                        m = re.match(r"^\[([^\]]+)\] ", line)
                        if m:
                            self.assertEqual(m.group(1), copied_name, f"{skill}: 経過の表示の頭が、コピーしたフォルダの名前ではありません：{line}")
                    logs = work / f".{copied_name}" / "logs"
                    if logs.is_dir():
                        for log in logs.iterdir():
                            self.assertTrue(log.name.startswith(copied_name + "-"), f"{skill}: ログの名前が、コピーしたフォルダの名前で始まりません：{log.name}")
                    if result["missing"]:
                        self.assertFalse(data["ok"], f"{skill}: 必須の項目が空なのに、確認が ok になりました")
                        self.assertTrue(data.get("problems"), f"{skill}: 問題の説明がありません")
                    self.assertEqual(sorted(p.name for p in work.iterdir()), [f".{copied_name}"],
                                     f"{skill}: 作業フォルダに、.{copied_name}/ のほかのものができました")
                    self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["home", "project"],
                                     f"{skill}: 作業フォルダの外に、何かができました")
                    after = sorted(str(p.relative_to(lone)) for p in lone.rglob("*"))
                    self.assertEqual(after, before, f"{skill}: スキルのフォルダの中が変わりました")

    def test_each_skill_tests_pass_alone(self):
        """スキルのフォルダだけをコピーした場所で、README に書いた動かし方で、そのスキルのテストが通る"""
        for skill in SKILL_NAMES:
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as tmp:
                lone = Path(tmp) / "lone" / skill
                shutil.copytree(SKILLS_DIR / skill, lone, ignore=shutil.ignore_patterns("__pycache__"))
                done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                      capture_output=True, text=True, timeout=900, cwd=lone,
                                      env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                self.assertEqual(done.returncode, 0, f"{skill} のテストが、フォルダだけでは通りません：\n{done.stderr[-2000:]}")
                self.assertRegex(done.stderr, r"Ran [1-9]\d* tests?")

    def test_setup_runs_alone_on_unknown_yaml(self):
        """設定のスキルは、どのスキルのものでもない YAML でも動く。作業フォルダに何も作らない"""
        with tempfile.TemporaryDirectory() as tmp:
            lone = Path(tmp) / "skills" / "my-setup"
            shutil.copytree(SKILLS_DIR / "setup", lone, ignore=shutil.ignore_patterns("__pycache__"))
            work = Path(tmp) / "project"
            (work / "conf").mkdir(parents=True)
            cfg = work / "conf" / "kettle.yaml"
            cfg.write_text("# 湯わかし器の設定\n\n# 湯の温度（必須）\n# 例：90\ntemperature: \n\n# 名前\nname: \"台所\"\n", encoding="utf-8")
            tool = lone / "scripts" / "config_tool.py"
            shown = run_tool(tool, "show", str(cfg), cwd=work)
            self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
            keys = [item["key"] for item in json.loads(shown.stdout)["items"]]
            self.assertEqual(keys, ["temperature", "name"])
            done = run_tool(tool, "set", str(cfg), "temperature", "--stdin", "--json", cwd=work, stdin="85\n")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
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

    def test_setup_gets_skill_names_not_paths(self):
        """セットアップでは、設定のスキルにスキルの名前を渡す。設定ファイルの場所を集めて渡さない"""
        a1 = re.search(r"^### A-1\..*?(?=^### )", self.text, re.S | re.M)
        self.assertIsNotNone(a1, "作業エージェントに、セットアップの節（A-1）がありません")
        self.assertIn("名前", a1.group(0))
        self.assertNotRegex(a1.group(0), r"設定ファイルの場所(の一覧)?を(返|渡して)", "設定ファイルの場所を集めて渡しています")
        self.assertNotRegex(self.text, r"設定ファイルの場所を返す", "スキルに、設定ファイルの場所を返すよう頼んでいます")

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

    def test_worker_contract_matches(self):
        """統括エージェントの頼みの書き出しと、作業エージェントが見分ける書き出しがそろっている（置き換えの約束）"""
        orchestrator = (AGENTS_DIR / "orchestrator.agent.md").read_text(encoding="utf-8")
        worker = (AGENTS_DIR / "device-worker.agent.md").read_text(encoding="utf-8")
        for phrase in ["これは準備の確認です", "あなたは作業役です"]:
            self.assertRegex(orchestrator, r"(?m)^" + phrase, f"統括エージェントの頼みが「{phrase}」で始まりません")
            self.assertIn(f"「{phrase}」", worker, f"作業エージェントが「{phrase}」を見分けていません")
        for phrase in ["準備：できている", "準備：できていない"]:
            self.assertIn(phrase, orchestrator, f"統括エージェントが「{phrase}」を見ていません")
        self.assertRegex(worker, r"(?m)^準備：<できている／できていない>$", "作業エージェントの準備の確認の返事の形が違います")


class DocumentIndependence(unittest.TestCase):
    """文書：各部品の使い方は、その部品の README にだけ書く。上の段の文書に、下の段の中身を書かない"""

    def upper_documents(self):
        """（表示の名前, 本文）。上の段の文書"""
        docs = []
        for path in [ROOT / "README.md", GUIDE_DIR / "3-orchestrator.md", GUIDE_DIR / "replace.md"]:
            docs.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
        text = WORKER_README.read_text(encoding="utf-8")
        part = section(text, "統括エージェントと検証エージェント")
        self.assertIsNotNone(part, "作業エージェントの README に「## 統括エージェントと検証エージェント」の節がありません")
        docs.append((f"{WORKER_README.relative_to(ROOT)}（統括エージェントと検証エージェント）", part))
        return docs

    def test_upper_documents_name_no_skill(self):
        patterns = [(name, name_token(name)) for name in all_skill_names()]
        problems = []
        for label, text in self.upper_documents():
            problems += hits_in_text(label, text, patterns)
        self.assertEqual(problems, [], "上の段の文書に、スキルの名前があります：\n" + "\n".join(problems))

    def test_upper_documents_have_no_specifics(self):
        keys = sorted({k for skill in WORK_SKILLS for k in template_keys(skill) if len(k) > 3})
        patterns = SPECIFIC_WORDS + [(k, token(k)) for k in keys]
        problems = []
        for label, text in self.upper_documents():
            problems += hits_in_text(label, text, patterns)
        self.assertEqual(problems, [], "上の段の文書に、転送・ログ取得などの具体があります：\n" + "\n".join(problems))

    def test_worker_readme_knows_only_names_and_roles(self):
        """作業エージェントの README は、作業エージェントの定義と同じく、スキルの名前と役割だけを書く"""
        self.assertTrue(WORKER_README.is_file(), "作業エージェントの README（agents/README.markdown）がありません")
        text = WORKER_README.read_text(encoding="utf-8")
        table = section(text, "使うスキル")
        self.assertIsNotNone(table, "作業エージェントの README に「## 使うスキル」の節がありません")
        outside = text.replace(table, "")
        problems = hits_in_text(WORKER_README.relative_to(ROOT), outside,
                                [(name, name_token(name)) for name in all_skill_names()])
        problems += hits_in_text(WORKER_README.relative_to(ROOT), text, SPECIFIC_WORDS)
        keys = sorted({k for skill in WORK_SKILLS for k in template_keys(skill) if len(k) > 3})
        problems += hits_in_text(WORKER_README.relative_to(ROOT), text, [(k, token(k)) for k in keys])
        self.assertEqual(problems, [], "作業エージェントの README に、スキルの中身があります：\n" + "\n".join(problems))

    def test_worker_readme_table_matches_definition(self):
        definition = skill_table((AGENTS_DIR / "device-worker.agent.md").read_text(encoding="utf-8"))
        readme = skill_table(WORKER_README.read_text(encoding="utf-8"))
        self.assertTrue(definition)
        self.assertEqual(readme, definition, "作業エージェントの README の「使うスキル」の表が、定義の表と違います")

    def test_agents_folder_has_only_agent_definitions_as_md(self):
        """agents/ の .md は、Copilot CLI がエージェントの定義として読み込む。README は .markdown にする"""
        for path in AGENTS_DIR.iterdir():
            if path.suffix == ".md":
                self.assertTrue(path.name.endswith(".agent.md"), f"{path.name} はエージェントとして読み込まれます")

    def test_old_guides_are_gone(self):
        for name in ["1-skills.md", "2-device-worker.md"]:
            self.assertFalse((GUIDE_DIR / name).exists(), f"docs/guide/{name} は消しました（中身は各部品の README にある）")

    def test_links_resolve(self):
        """プラグインの文書の相対リンクが、あるファイルと見出しを指す"""
        docs = [ROOT / "README.md", WORKER_README] + sorted((ROOT / "docs").rglob("*.md"))
        docs += sorted(SKILLS_DIR.glob("*/README.md"))
        problems = []
        for doc in docs:
            text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                    continue  # https: など
                file_part, _, anchor = target.partition("#")
                dest = (doc.parent / file_part).resolve() if file_part else doc
                if not dest.exists():
                    problems.append(f"{doc.relative_to(ROOT)}: {target}（ファイルがありません）")
                    continue
                if anchor and dest.is_file() and anchor not in heading_slugs(dest):
                    problems.append(f"{doc.relative_to(ROOT)}: {target}（見出しがありません）")
        self.assertEqual(problems, [], "\n".join(problems))


def heading_slugs(path):
    """GitHub が見出しに付けるアンカー（小文字にし、記号を消し、空白を - にする）"""
    slugs = set()
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    for m in re.finditer(r"^#{1,6}\s+(.*?)\s*$", text, re.M):
        title = re.sub(r"`", "", m.group(1)).lower()
        slugs.add(re.sub(r"\s", "-", re.sub(r"[^\w\s\-]", "", title)))
    return slugs


class SkillReplacement(unittest.TestCase):
    """置き換え：別の中身・別の設定の形のスキル（や作業エージェント）に置き換えても、上の段を変えずに済む"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        # プラグインに入っているときと同じ形：setup の隣に、ほかのスキルがある
        self.plugin_skills = base / "plugin" / "skills"
        for skill in SKILL_NAMES:
            shutil.copytree(SKILLS_DIR / skill, self.plugin_skills / skill, ignore=shutil.ignore_patterns("__pycache__"))
        self.setup_dir = self.plugin_skills / "setup"
        # 作業フォルダは Git のリポジトリの一番上にする（その上のフォルダのスキルを拾わないため）
        self.project = base / "project"
        (self.project / ".git").mkdir(parents=True)
        (self.project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.copilot_home = base / "copilot-home"
        self.copilot_home.mkdir()
        self.home = base / "home"
        self.home.mkdir()

    def found(self):
        return list_skills(self.setup_dir, self.project, self.copilot_home, self.home)

    def visible(self, entries, name):
        hits_ = [e for e in entries if e["name"] == name and not e["hidden"]]
        self.assertEqual(len(hits_), 1, f"{name} が1つに決まりません：{hits_}")
        return hits_[0]

    def test_plugin_skills_are_found_as_siblings(self):
        entries = self.found()
        names = sorted(e["name"] for e in entries)
        self.assertNotIn("setup", names, "setup が、自分を結果に入れています")
        for skill in WORK_SKILLS:
            entry = self.visible(entries, skill)
            self.assertEqual(entry["where"], "sibling")
            self.assertTrue(same_path(entry["path"], self.plugin_skills / skill))
            self.assertTrue(same_path(entry["readme"], self.plugin_skills / skill / "README.md"))
        self.assertEqual([p.name for p in self.project.iterdir()], [".git"], "setup の skills が、作業フォルダに何かを作りました")

    def test_replaced_skill_with_other_config_format_is_found(self):
        """作業フォルダに同じ名前の別のスキル（INI の設定、別のスクリプト）を置くと、そちらが使われる"""
        replacement = make_skill(
            self.project / ".github" / "skills" / "deploy", "deploy",
            "ビルドしたファイルを、ほかの方法で機器へ送る。送って、と頼まれたときに使う。",
            readme="# 送るスキル\n\n設定ファイルは `.deploy/target.ini` です。`[target]` の `address` に機器のアドレスを書きます。\n",
            files={"scripts/push.py": "print('pushed')\n"})
        personal = make_skill(self.copilot_home / "skills" / "deploy", "deploy", "個人のフォルダに置いた、送るスキル。",
                              readme="# 個人の送るスキル\n")
        entries = self.found()
        chosen = self.visible(entries, "deploy")
        self.assertEqual(chosen["where"], "project")
        self.assertTrue(same_path(chosen["path"], replacement))
        self.assertTrue(same_path(chosen["readme"], replacement / "README.md"),
                        "置き換えたスキルの README が返りません（setup が設定の方法を知るのに使う）")
        hidden = {e["where"]: e for e in entries if e["name"] == "deploy" and e["hidden"]}
        self.assertEqual(sorted(hidden), ["personal", "sibling"], "隠れたスキルの扱いが違います")
        self.assertTrue(same_path(hidden["personal"]["path"], personal))

        # 作業エージェント（定義と README）と統括エージェントは、元のスキルを名前でだけ知る。
        # 元のスキルの中身（スクリプトの名前、設定の項目、データのフォルダ）を書いていなければ、置き換えても直さずに済む
        worker = (AGENTS_DIR / "device-worker.agent.md").read_text(encoding="utf-8")
        table = skill_table(worker)
        self.assertIn("deploy", table.values(), "作業エージェントは、名前でスキルを呼びます")
        original = SKILLS_DIR / "deploy"
        internals = [p.name for p in (original / "scripts").glob("*.py")] + [".deploy"]
        internals += [k for k in template_keys("deploy") if len(k) > 3]
        for path in [AGENTS_DIR / "device-worker.agent.md", WORKER_README, AGENTS_DIR / "orchestrator.agent.md"]:
            text = path.read_text(encoding="utf-8")
            found = [w for w in internals if (w in text if w.startswith(".") or w.endswith(".py") else token(w).search(text))]
            self.assertEqual(found, [], f"{path.relative_to(ROOT)} が、元のスキルの中身を書いています")
        # 表のどの名前も、1つのスキルに決まる（Copilot CLI と同じ順で）
        for name in table.values():
            if name != "setup":
                self.visible(entries, name)

    def test_replaced_setup_needs_the_skills_beside_it(self):
        """設定のスキルを作業フォルダに置き換えたら、設定するスキルも同じ場所に置く（置き換えのガイドの決まり）"""
        project_skills = self.project / ".github" / "skills"
        project_setup = project_skills / "setup"
        shutil.copytree(SKILLS_DIR / "setup", project_setup, ignore=shutil.ignore_patterns("__pycache__"))
        found = lambda: list_skills(project_setup, self.project, self.copilot_home, self.home)
        names = sorted(e["name"] for e in found())
        for skill in WORK_SKILLS:
            self.assertNotIn(skill, names, "プラグインの中のスキルが、プラグインの外の設定のスキルから見つかりました。"
                                           "置き換えのガイドの決まりを見直してください")
        for skill in WORK_SKILLS:
            shutil.copytree(self.plugin_skills / skill, project_skills / skill, ignore=shutil.ignore_patterns("__pycache__"))
        entries = found()
        for skill in WORK_SKILLS:
            entry = self.visible(entries, skill)
            self.assertEqual(entry["where"], "project")
            self.assertTrue(same_path(entry["path"], project_skills / skill))
            self.assertTrue(same_path(entry["readme"], project_skills / skill / "README.md"))

    def test_skill_without_readme_falls_back_to_skill_md(self):
        bare = make_skill(self.copilot_home / "skills" / "kettle", "kettle", "湯をわかす。")
        entry = self.visible(self.found(), "kettle")
        self.assertEqual(entry["where"], "personal")
        self.assertTrue(same_path(entry["path"], bare))
        self.assertIn(entry["readme"], (None, ""), "README がないスキルに、README の場所を返しています")

    def test_replaced_worker_and_skills_need_no_upper_change(self):
        """作業エージェントごと置き換えても（別の名前のスキル）、setup が見つけ、統括エージェントは変えずに済む"""
        definition = (AGENTS_DIR / "device-worker.agent.md").read_text(encoding="utf-8")
        renames = {"cross-compile": "mcu-build", "deploy": "flash", "log-capture": "uart-log"}
        table = skill_table(definition)
        new_definition = definition
        for old, new in renames.items():
            new_definition = new_definition.replace(f"`{old}`", f"`{new}`")
        new_definition = new_definition.replace("device-worker", "alt-worker")
        agents = self.project / ".github" / "agents"
        agents.mkdir(parents=True)
        (agents / "alt-worker.agent.md").write_text(new_definition, encoding="utf-8")
        for old, new in renames.items():
            make_skill(self.project / ".github" / "skills" / new, new, f"マイコンのボードの {new}。",
                       readme=f"# {new}\n\n設定ファイルは `settings.toml` です。\n")
        new_table = skill_table(new_definition)
        self.assertEqual(set(new_table), set(table), "役割は変わりません")
        entries = self.found()
        for role, name in new_table.items():
            if name == "setup":
                continue  # 設定のスキル自身は、skills の結果に入らない
            entry = self.visible(entries, name)
            if name in renames.values():
                self.assertEqual(entry["where"], "project", f"{role} のスキル {name} が、作業フォルダから見つかりません")
        # 統括エージェント・検証エージェント・エクステンションは、作業エージェントの名前もスキルの名前も持たない
        for path in [AGENTS_DIR / "orchestrator.agent.md", AGENTS_DIR / "verifier.agent.md"] + [
                p for p in text_files(EXT_DIR) if p.name != "defaults.json"]:
            text = path.read_text(encoding="utf-8")
            for word in ["device-worker", "alt-worker"] + list(renames) + list(renames.values()):
                self.assertIsNone(token(word).search(text), f"{path.relative_to(ROOT)} に {word} があります")


if __name__ == "__main__":
    unittest.main()
