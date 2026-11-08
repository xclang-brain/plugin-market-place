"""部品の独立性を確かめるテスト（憲章 第25条、仕様 REQ-001〜REQ-007、REQ-102、REQ-106、REQ-108）。

このテストには、部品（スキルと作業エージェント）の名前も作りも書かない。部品は、次のところから読む。
そのため、スキルを足す・消す・置き換えても、作業エージェントを置き換えても、このテストは直さずに済む。
- スキル：skills/ の下の、SKILL.md があるフォルダ（フォルダの名前がスキルの名前）
- 作業エージェント：defaults.json の worker（「<プラグイン>:<名前>」）。定義は agents/<名前>.agent.md
- 使うスキル：作業エージェントの定義の「使うスキル」の一覧（`名前` を並べたもの）
- スキルの中身：スキルのフォルダの scripts/ の下の .py（同梱した他者のコード scripts/vendor/ を除く）と、
  フォルダの直下の、名前に template を含むファイル（設定のひな形。YAML の「項目:」か INI の「項目 =」の行を項目とする）

確かめること：
- 1段階目：スキルは、それぞれのフォルダだけで完結する（利用者は、プラグインをインストールせずに、
  スキルのフォルダだけを自分の作業フォルダの .github/skills/<名前>/ にコピーして使う）。
  - フォルダの中に、プラグインの名前（とその略）・ほかのスキル・エージェント・Web管理画面・
    エクステンションを書いていない。フォルダの外を指していない。互いのファイルを読み込まない。
    同梱した他者のコード（scripts/vendor/）は中身を変えないので、言葉の確認からは外す。
  - テスト（tests/）が、フォルダの中にある。README.md はフォルダに置かない（Anthropic のスキル作成ガイド）。
    人向けの使い方は docs/guide/1-<スキルの名前>.md にあり、設定のひな形を持つスキルは、
    そこにデータの置き場所 .<フォルダの名前>/ と、ひな形のすべての項目を書く。
  - フォルダだけを別の場所にコピーしても、そのスキルのテストが通る。
    コピーしたフォルダ（名前を変えたコピーも）で設定ファイルの用意と設定の確認が動き、データが作業フォルダの
    .<フォルダの名前>/ にだけできることは、コマンドがスキルごとに違うので、各スキルのテスト（tests/）で確かめる。
- 2段階目：作業エージェントの定義は、使うスキルの名前を「使うスキル」の一覧にだけ書く。一覧のスキルは、
  skills/ の下のスキルと同じ。一覧の外には、スキルの名前も自分の名前も書かない。
  スキルの中身（スクリプトの名前、設定の項目、設定ファイルの置き場所）を書いていない。
  作業エージェントの使い方の文書（docs/guide/2-<名前>.md）は、スキルの名前を書かず、定義の一覧を案内する。
  agents/ には、エージェントの定義のほかのファイルを置かない（.md はエージェントとして読み込まれる）。
- 3段階目：統括エージェント・検証エージェント・エクステンションは、機器・ビルド・転送・ログ取得の
  方法やスキルの名前を書いていない。作業エージェントの名前は、設定のデータ（defaults.json）にだけある。
  定義の例に、具体の値（答えになりうる数字）を書いていない。利用者への質問の例や選択肢にも、
  頼みに書かれていない値を作って入れない。
- 文書：上の段の文書（プラグインの README、3段階目のガイド）に、
  スキルの名前と、転送・ログ取得の具体の言葉がない。部品を変える手順書（docs/replace.md）は、
  直す所が分かるように今の部品の名前を例として書く開発者向けの文書なので、上の段の文書に入れない。
  3段階目のガイドは、作業エージェントが機器のものだと前提にしない。作業エージェントは、
  最初の依頼に名前を書いて選ぶ。部品を変える手順書は開発者向けで、利用者向けのガイドの中にない。
- 置き換え：スキルは、作業エージェントの定義の「使うスキル」の一覧の名前を替えるだけで置き換えられる
  （作業エージェントごと置き換えても）。統括エージェント・検証エージェント・エクステンションと、作業エージェントの
  使い方の文書は、スキルの中身を書いていないので、直さずに済む。
  作業エージェントを別の名前に替えても 3段階目が動くことは、tests/extension/ で確かめる。

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
DEFAULTS = EXT_DIR / "defaults.json"
GUIDE_DIR = ROOT / "docs" / "guide"
REPLACE_DOC = ROOT / "docs" / "replace.md"  # 部品を変える手順書（開発者向け。今の部品の名前を例に書く）
TEXT_SUFFIXES = {".md", ".markdown", ".py", ".yaml", ".yml", ".json", ".mjs", ".js", ".html", ".css", ".txt", ".sh"}
PLUGIN_NAME = ROOT.name  # embedded-debug-loop
# スキルのフォルダに書いてはいけない、プラグインを指す言葉
PLUGIN_WORDS = [
    (PLUGIN_NAME, re.compile(re.escape(PLUGIN_NAME), re.IGNORECASE)),
    (PLUGIN_NAME.replace("-", "_"), re.compile(re.escape(PLUGIN_NAME.replace("-", "_")), re.IGNORECASE)),
    ("EDL（プラグインの略）", re.compile(r"(?<![A-Za-z0-9])EDL(?![a-z0-9])|(?<![A-Za-z0-9])edl(?=[_\-])", re.IGNORECASE)),
]
# 3段階目の部品に書いてはいけない、機器を指す言葉（3段階目は、作業の対象が機器だと知らない）。
# 今のスキルの技術の言葉（転送の方法など）は、ここに書かない。スキルから読む（internal_patterns）
DEVICE_WORDS = ["機器", "device", "board", "ボード"]


# ---------------------------------------------------------------------------
# 部品を読む
# ---------------------------------------------------------------------------

def skill_folders():
    """skills/ の下のフォルダ（点や _ で始まるものを除く）"""
    return sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def skill_names():
    """skills/ の下のスキル（SKILL.md があるフォルダ）の名前"""
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


def defaults():
    return json.loads(DEFAULTS.read_text(encoding="utf-8"))


def agent_name(key):
    """defaults.json のエージェント（「<プラグイン>:<名前>」）の <名前>"""
    return defaults()[key].split(":")[-1]


def agent_path(key):
    """defaults.json のエージェントの定義のファイル"""
    return AGENTS_DIR / (agent_name(key) + ".agent.md")


def worker_path():
    """作業エージェントの定義。defaults.json の worker から決める"""
    return agent_path("worker")


def skill_guide(skill):
    """スキルの人向けの使い方（スキルのフォルダの外に置く）"""
    return GUIDE_DIR / f"1-{skill}.md"


def worker_guide():
    """作業エージェントの使い方の文書。defaults.json の worker から決める"""
    return GUIDE_DIR / f"2-{agent_name('worker')}.md"


def stage3_files():
    """3段階目の部品：統括エージェント・検証エージェントの定義と、エクステンション（defaults.json を除く）"""
    files = [agent_path("orchestrator"), agent_path("verifier")]
    if EXT_DIR.exists():
        files += [p for p in text_files(EXT_DIR) if p.name != "defaults.json"]
    return files


def scripts(skill):
    """スキルのスクリプト（scripts/ の下の .py。同梱した他者のコード scripts/vendor/ を除く）"""
    folder = SKILLS_DIR / skill / "scripts"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob("*.py")
                  if "vendor" not in p.relative_to(folder).parts and "__pycache__" not in p.parts)


def templates(skill):
    """設定のひな形（スキルのフォルダの直下の、名前に template を含むファイル）"""
    return sorted(p for p in (SKILLS_DIR / skill).iterdir() if p.is_file() and "template" in p.name.lower())


def template_keys(skill):
    """設定のひな形の項目の名前（YAML の「項目:」と、INI の「項目 =」の行。「https://…」のような行は数えない）"""
    keys = []
    for template in templates(skill):
        for line in template.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_.\-]*)\s*(?::(?=\s|$)|=)", line)
            if m and m.group(1) not in keys:
                keys.append(m.group(1))
    return keys


def all_template_keys():
    """すべてのスキルの、設定の項目の名前のうち、_ を含むもの（sdk_env など）。
    _ のない名前（model、list など）は、ほかの部品のふつうの言葉と重なり、新しいスキルの項目の名前を縛ってしまうので数えない"""
    return sorted({k for skill in skill_names() for k in template_keys(skill) if "_" in k})


def internal_patterns(compound_keys_only=False):
    """スキルの中身を表す言葉（ラベル, 正規表現）：スクリプトとひな形のファイルの名前、ひな形の項目の名前、
    作業フォルダの中のデータの置き場所（.<スキルの名前>）。
    スクリプトの名前は、_ を含むものだけ、拡張子のない形も探す（_ のない名前は、ふつうの言葉と重なることがある）。
    compound_keys_only なら、項目の名前は _ を含むものだけにする（ツールの引数 prompt などと重ならないように）"""
    patterns = []
    for skill in skill_names():
        for path in scripts(skill):
            patterns.append((path.name, token(path.name)))
            if "_" in path.stem:
                patterns.append((path.stem, token(path.stem)))
        for path in templates(skill):
            patterns += [(path.name, token(path.name)), (path.stem, token(path.stem))]
        patterns.append(("." + skill, re.compile(r"(?<![A-Za-z0-9_\-.])\." + re.escape(skill) + r"(?![A-Za-z0-9_\-])")))
    for key in all_template_keys():
        if not compound_keys_only or "_" in key:
            patterns.append((key, token(key)))
    return patterns


# ---------------------------------------------------------------------------
# 作業エージェントの定義の「使うスキル」の一覧
# ---------------------------------------------------------------------------

SKILL_SECTION = re.compile(r"^## 使うスキル\n(.*?)(?=^## |\Z)", re.S | re.M)
SKILL_NAME = re.compile(r"`([^`]+)`")


def section(text, heading):
    """「## 見出し」から、次の「## 」の手前まで"""
    m = re.search(r"^## " + re.escape(heading) + r"\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1) if m else None


def is_table_line(line):
    return line.lstrip().startswith("|")


def skill_list_parts(text):
    """作業エージェントの定義を、「使うスキル」の一覧の行と、それ以外の文に分ける。節がなければ (None, text)"""
    m = SKILL_SECTION.search(text)
    if m is None:
        return None, text
    lines = m.group(1).splitlines(keepends=True)
    listed = "".join(line for line in lines if SKILL_NAME.search(line))
    rest = "".join(line for line in lines if not SKILL_NAME.search(line))
    return listed, text[:m.start(1)] + rest + text[m.end(1):]


def worker_skills(text):
    """「使うスキル」の一覧のスキルの名前を、書かれた順で返す。一覧がなければ AssertionError"""
    listed, _ = skill_list_parts(text)
    if listed is None:
        raise AssertionError("作業エージェントの定義に「## 使うスキル」の節がありません")
    names = SKILL_NAME.findall(listed)
    if not names:
        raise AssertionError("「使うスキル」の節に、スキルの名前（`名前`）がありません")
    return names


def replace_in_list(text, renames):
    """「使うスキル」の一覧の `名前` だけを替える（一覧の外は変えない）"""
    m = SKILL_SECTION.search(text)
    if m is None:
        raise AssertionError("作業エージェントの定義に「## 使うスキル」の節がありません")
    out = []
    for line in m.group(1).splitlines(keepends=True):
        if SKILL_NAME.search(line):
            for old, new in renames.items():
                line = line.replace(f"`{old}`", f"`{new}`")
        out.append(line)
    return text[:m.start(1)] + "".join(out) + text[m.end(1):]


# ---------------------------------------------------------------------------
# 言葉を探す
# ---------------------------------------------------------------------------

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
    """名前としての出現。大文字と小文字を分ける（unittest の setUp は数えない）"""
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


def default_output_encoding():
    """子の Python が、パイプに書くときの既定の文字コード（PYTHONIOENCODING があればそれ）"""
    for name in (os.environ.get("PYTHONIOENCODING", "").split(":")[0].strip(), locale.getpreferredencoding(False)):
        try:
            if name:
                return codecs.lookup(name).name
        except LookupError:
            pass
    return "utf-8"


class SkillIndependence(unittest.TestCase):
    """1段階目：スキルどうしが互いを知らない"""

    def test_skill_folders_are_skills(self):
        """skills/ の下のフォルダは、どれもスキル（SKILL.md がある）。スキルが1つ以上ある"""
        folders = skill_folders()
        self.assertTrue(folders, "skills/ の下に、スキルがありません")
        for folder in folders:
            self.assertTrue((folder / "SKILL.md").is_file(), f"skills/{folder.name}/ に SKILL.md がありません")

    def test_no_mention_of_other_parts(self):
        names = skill_names()
        agents = sorted({agent_name(key) for key in ("worker", "orchestrator", "verifier")})
        words = ["orchestrator", "verifier", "作業エージェント", "統括", "検証エージェント",
                 "Web管理画面", "管理画面", "エクステンション", "extension"]
        problems = []
        for skill in names:
            patterns = [(name, name_token(name)) for name in names if name != skill]
            patterns += [(name, name_token(name)) for name in agents]
            patterns += [(w, re.compile(re.escape(w))) for w in words]
            patterns += PLUGIN_WORDS
            for path in text_files(SKILLS_DIR / skill, skip_vendor=True):
                problems += hits(path, patterns)
        self.assertEqual(problems, [], "スキルが、プラグイン・ほかのスキル・エージェントのことを書いています：\n" + "\n".join(problems))

    def test_vendor_is_third_party_with_license(self):
        """同梱した他者のコードは scripts/vendor/ にだけ置き、ライセンスの文書をそろえる"""
        for skill in skill_names():
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
        for skill in skill_names():
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
        for skill in skill_names():
            for path in text_files(SKILLS_DIR / skill):
                problems += hits(path, patterns)
        self.assertEqual(problems, [], "スキルが、フォルダの外を指しています：\n" + "\n".join(problems))

    def test_tests_inside_and_guide_outside(self):
        """テストはスキルのフォルダの中にあり、README.md は置かない。人向けの使い方は docs/guide/1-<名前>.md にある。
        設定のひな形を持つスキルのガイドには、データの置き場所と、ひな形のすべての項目を書く"""
        for skill in skill_names():
            with self.subTest(skill=skill):
                self.assertTrue(list((SKILLS_DIR / skill / "tests").glob("test_*.py")),
                                f"スキル {skill} のテストが、フォルダの中（tests/）にありません")
                self.assertFalse((SKILLS_DIR / skill / "README.md").exists(),
                                 f"スキル {skill} のフォルダに README.md があります（人向けの使い方は docs/guide/ に置く）")
                guide = skill_guide(skill)
                self.assertTrue(guide.is_file(), f"スキル {skill} の使い方（{guide.relative_to(ROOT)}）がありません")
                text = guide.read_text(encoding="utf-8")
                if templates(skill):
                    self.assertIn(f".{skill}", text, f"{guide.name} に、データの置き場所 .{skill}/ がありません")
                    for key in template_keys(skill):
                        self.assertIn(f"`{key}`", text, f"{guide.name} に、設定の項目 {key} がありません")

    def test_no_shared_code(self):
        names = skill_names()
        problems = []
        for skill in names:
            others = [s for s in names if s != skill]
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

    def test_each_skill_tests_pass_alone(self):
        """スキルのフォルダだけをコピーした場所で、使い方の文書に書いた動かし方で、そのスキルのテストが通る"""
        for skill in skill_names():
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as tmp:
                lone = Path(tmp) / "lone" / skill
                shutil.copytree(SKILLS_DIR / skill, lone, ignore=shutil.ignore_patterns("__pycache__"))
                done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                      capture_output=True, text=True, encoding=default_output_encoding(),
                                      errors="replace", timeout=900, cwd=lone,
                                      env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                self.assertEqual(done.returncode, 0, f"{skill} のテストが、フォルダだけでは通りません：\n{done.stderr[-2000:]}")
                self.assertRegex(done.stderr, r"Ran [1-9]\d* tests?")


class WorkerIndependence(unittest.TestCase):
    """2段階目：作業エージェントは、スキルを「使うスキル」の一覧の名前でだけ知る"""

    def setUp(self):
        self.path = worker_path()
        self.assertTrue(self.path.is_file(),
                        f"defaults.json の worker の定義（{self.path.relative_to(ROOT)}）がありません")
        self.text = self.path.read_text(encoding="utf-8")
        self.listed, self.outside = skill_list_parts(self.text)
        self.assertIsNotNone(self.listed, "作業エージェントの定義に「## 使うスキル」の節がありません")
        self.names = worker_skills(self.text)
        self.label = f"{self.path.relative_to(ROOT)}（「使うスキル」の一覧の外）"

    def test_list_names_the_skills(self):
        """一覧のスキルは、skills/ の下のスキルと同じ"""
        self.assertEqual(len(self.names), len(set(self.names)), f"一覧に、同じスキルが2回あります：{self.names}")
        self.assertEqual(sorted(self.names), skill_names(), "一覧のスキルが、skills/ の下のスキルと違います")

    def test_skill_names_only_in_list(self):
        names = sorted(set(skill_names()) | set(self.names))
        problems = hits_in_text(self.label, self.outside, [(name, token(name)) for name in names])
        self.assertEqual(problems, [], "一覧の外に、スキルの名前があります：\n" + "\n".join(problems))

    def test_does_not_name_itself(self):
        """自分の名前を書かない。作業エージェントを置き換えても、中を直さずに済む"""
        name = agent_name("worker")
        problems = hits_in_text(self.path.relative_to(ROOT), self.text, [(name, token(name))])
        self.assertEqual(problems, [], "作業エージェントの定義に、自分の名前があります：\n" + "\n".join(problems))

    def test_no_skill_internals(self):
        # 一覧のスキルの名前は除く（スキルの名前は、そこにだけ書く）
        found = [label for label, pattern in internal_patterns() if pattern.search(self.outside)]
        self.assertEqual(found, [], f"作業エージェントが、スキルの中身を書いています：{found}")

    def test_no_upper_layer(self):
        words = ["Web管理画面", "統括", "検証エージェント", "orchestrator", "verifier", PLUGIN_NAME + "_"]
        found = [w for w in words if w in self.text]
        found += [name for name in (agent_name("orchestrator"), agent_name("verifier")) if name_token(name).search(self.text)]
        self.assertEqual(found, [], f"作業エージェントが、3段階目のことを書いています：{found}")


class OrchestrationIndependence(unittest.TestCase):
    """3段階目：統括・検証・エクステンションは、機器のことを知らない"""

    def test_no_device_words(self):
        words = DEVICE_WORDS + skill_names() + [agent_name("worker")]
        patterns = [(w, token(w)) for w in words]
        problems = []
        for path in stage3_files():
            problems += hits(path, patterns)
        self.assertEqual(problems, [], "3段階目に、機器やスキルのことが書かれています：\n" + "\n".join(problems))

    def test_examples_hold_no_answer_values(self):
        """定義の例（「例：「…」」）に具体の値（数字）を書かない。統括エージェントが、例の値を利用者への質問に持ち込むため"""
        problems = []
        for path in [agent_path("orchestrator"), agent_path("verifier")]:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for example in re.findall(r"例：([^）\n]*)", line):
                    for quoted in re.findall(r"「([^」]*)」", example):
                        if re.search(r"[0-9０-９]", quoted):
                            problems.append(f"{path.relative_to(ROOT)}:{lineno}: 「{quoted}」")
        self.assertEqual(problems, [], "定義の例に、具体の値があります：\n" + "\n".join(problems))

    def test_questions_hold_no_invented_values(self):
        """利用者への質問の例や選択肢に、頼みに書かれていない値を作って入れない（例の欄に、答えの値を作って書いたため）"""
        text = agent_path("orchestrator").read_text(encoding="utf-8")
        self.assertRegex(text, r"質問の例や選択肢に、頼みに書かれていない値[^\n]*作って入れない",
                         "統括エージェントに、質問の例や選択肢に値を作って入れない決まりがありません")

    def test_worker_name_only_in_defaults(self):
        self.assertTrue(DEFAULTS.is_file(), "defaults.json がありません")
        data = defaults()
        self.assertTrue(data.get("worker"), "defaults.json に作業エージェントの名前がありません")
        for path in stage3_files():
            self.assertNotIn(data["worker"], path.read_text(encoding="utf-8"), f"{path.relative_to(ROOT)} に作業エージェントの名前が直書きされています")

    def test_orchestrator_requests_define_the_replies(self):
        """返事の形を決めるのは、統括エージェントの頼みの文だけ。作業エージェントは、頼みの文が求める形で返す。
        作業エージェントの定義に、統括エージェントとの決まり（頼みの書き出し、返事の形）を書かない"""
        orchestrator = agent_path("orchestrator").read_text(encoding="utf-8")
        self.assertIn("返事の1行目に「準備：できている」か「準備：できていない」と書いてください", orchestrator)
        self.assertIn("手順ごとの結果", orchestrator)
        worker = worker_path().read_text(encoding="utf-8")
        for phrase in ["これは準備の確認です", "あなたは作業役です", "準備：できている", "準備：できていない"]:
            self.assertNotIn(phrase, worker, f"作業エージェントの定義に、統括エージェントとの決まり「{phrase}」があります")


class DocumentIndependence(unittest.TestCase):
    """文書：各部品の使い方は、その部品の使い方の文書にだけ書く。上の段の文書に、下の段の中身を書かない"""

    def upper_documents(self):
        """（表示の名前, 本文）。上の段の文書（部品を変える手順書は、今の部品の名前を例に書くので入れない）"""
        docs = []
        for path in [ROOT / "README.md", GUIDE_DIR / "3-orchestrator.md"]:
            docs.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
        return docs

    def test_upper_documents_name_no_skill(self):
        patterns = [(name, name_token(name)) for name in skill_names()]
        problems = []
        for label, text in self.upper_documents():
            problems += hits_in_text(label, text, patterns)
        self.assertEqual(problems, [], "上の段の文書に、スキルの名前があります：\n" + "\n".join(problems))

    def test_upper_documents_have_no_specifics(self):
        patterns = [(k, token(k)) for k in all_template_keys()]
        problems = []
        for label, text in self.upper_documents():
            problems += hits_in_text(label, text, patterns)
        self.assertEqual(problems, [], "上の段の文書に、転送・ログ取得などの具体があります：\n" + "\n".join(problems))

    def test_stage3_guide_is_worker_agnostic(self):
        """3段階目のガイドは、作業エージェントが機器のものだと前提にしない。既定の名前は1回だけ（設定の表）"""
        text = (GUIDE_DIR / "3-orchestrator.md").read_text(encoding="utf-8")
        default = agent_name("worker")
        self.assertLessEqual(len(token(default).findall(text)), 1, f"3段階目のガイドに、{default} が2回以上あります")
        self.assertNotIn("機器", text, "3段階目のガイドが、機器を前提にしています")
        self.assertNotIn(worker_guide().name, text, "3段階目のガイドが、特定の作業エージェントの使い方の文書を案内しています")

    def test_guide_chooses_worker_in_first_request(self):
        """作業エージェントは、最初の依頼に名前を書いて選ぶ（画面で選ぶ前に、既定の作業エージェントが動かないように）"""
        text = (GUIDE_DIR / "3-orchestrator.md").read_text(encoding="utf-8")
        request = re.search(r"「作業エージェントは <[^>]+> で」", text)
        self.assertIsNotNone(request, "3段階目のガイドに、依頼で作業エージェントを選ぶ書き方がありません")
        # 画面で選ぶのは、2回目からの切り替え。依頼の書き方より後に書く
        screen = re.search(r"でも替えられ", text)
        self.assertIsNotNone(screen, "3段階目のガイドに、Web管理画面で替えられることが書かれていません")
        self.assertLess(request.start(), screen.start(), "3段階目のガイドで、Web管理画面で選ぶ方法が、依頼の書き方より先にあります")

    def test_worker_guide_names_no_skill(self):
        """作業エージェントの使い方の文書は、スキルの名前も中身も書かない（スキルを足す・消す・替えても、直さずに済む）"""
        guide = worker_guide()
        self.assertTrue(guide.is_file(), f"作業エージェントの使い方の文書（{guide.relative_to(ROOT)}）がありません")
        text = guide.read_text(encoding="utf-8")
        label = guide.relative_to(ROOT)
        problems = hits_in_text(label, text, [(name, name_token(name)) for name in skill_names()])
        problems += hits_in_text(label, text, [(k, token(k)) for k in all_template_keys()])
        self.assertEqual(problems, [], "作業エージェントの使い方の文書に、スキルの名前や中身があります：\n" + "\n".join(problems))

    def test_worker_guide_points_to_the_definition_list(self):
        """作業エージェントの使い方の文書の「使うスキル」は、一覧を持たず、作業エージェントの定義の一覧を案内する"""
        text = worker_guide().read_text(encoding="utf-8")
        part = section(text, "使うスキル")
        self.assertIsNotNone(part, "作業エージェントの使い方の文書に「## 使うスキル」の節がありません")
        self.assertFalse([line for line in part.splitlines() if is_table_line(line)],
                         "作業エージェントの使い方の文書の「使うスキル」に、表があります（一覧は、作業エージェントの定義にだけ置く）")
        definition = worker_path().name
        links = [t.partition("#") for t in re.findall(r"\]\(([^)\s]+)\)", part)]
        self.assertTrue(any(Path(file_part).name == definition and anchor == "使うスキル" for file_part, _, anchor in links),
                        f"作業エージェントの使い方の文書の「使うスキル」が、{definition}#使うスキル を案内していません：{links}")

    def test_agents_folder_holds_only_agent_definitions(self):
        """agents/ の .md は、Copilot CLI がエージェントの定義として読み込む。定義のほかのファイルを置かない"""
        for path in AGENTS_DIR.iterdir():
            self.assertTrue(path.name.endswith(".agent.md"), f"agents/ に、エージェントの定義でない {path.name} があります")

    def test_guides_match_the_parts(self):
        # 1段階目のガイドは skills/ の下のスキルのものだけ、2段階目のガイドは既定の作業エージェントのものだけ
        guides = sorted(p.name for p in GUIDE_DIR.iterdir() if re.match(r"[12]-", p.name))
        expected = sorted([skill_guide(s).name for s in skill_names()] + [worker_guide().name])
        self.assertEqual(guides, expected, "docs/guide/ の 1・2段階目のガイドが、今の部品と合いません")
        # 部品の置き換えはプラグインの設計変更。利用者向けのガイドに置かない
        self.assertFalse((GUIDE_DIR / REPLACE_DOC.name).exists(), "部品を変える手順書が、利用者向けのガイドの中にあります")
        self.assertTrue(REPLACE_DOC.is_file(), "部品を変える手順書（docs/replace.md）がありません")

    def test_links_resolve(self):
        """プラグインの文書の相対リンクが、あるファイルと見出しを指す"""
        docs = [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))
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
    """置き換え：スキルは「使うスキル」の一覧の名前を替えるだけで置き換えられる（作業エージェントごと置き換えても）。
    ほかの部品は、スキルの中身を書いていないので、直さずに済む"""

    def setUp(self):
        self.definition = worker_path().read_text(encoding="utf-8")
        self.names = worker_skills(self.definition)

    def test_skill_is_replaced_by_its_name_in_the_list(self):
        """スキルを1つ、別の名前のスキルに替える。作業エージェントの定義で直すのは、一覧のその名前だけ"""
        old = self.names[0]
        new = f"{old}-alt"
        replaced = replace_in_list(self.definition, {old: new})
        self.assertEqual(worker_skills(replaced), [new if name == old else name for name in self.names])
        self.assertIsNone(token(old).search(replaced), f"一覧の名前を替えても、定義に {old} が残ります（一覧の外にも書いてあります）")

    def test_replaced_worker_and_skills_need_no_upper_change(self):
        """作業エージェントごと、名前の違うスキルの組に置き換える。作業エージェントの定義で直すのは、一覧の名前だけ。
        統括エージェント・検証エージェント・エクステンションは、どの名前も書いていないので、直さずに済む"""
        old_worker = agent_name("worker")
        new_worker = f"{old_worker}-alt"
        renames = {name: f"{name}-alt" for name in self.names}
        new_definition = replace_in_list(self.definition, renames)
        for word in list(renames) + [old_worker]:
            self.assertIsNone(token(word).search(new_definition), f"一覧の名前を替えても、定義に {word} が残ります")
        for path in stage3_files():
            text = path.read_text(encoding="utf-8")
            for word in [old_worker, new_worker] + list(renames) + list(renames.values()):
                self.assertIsNone(token(word).search(text), f"{path.relative_to(ROOT)} に {word} があります")

    def test_other_parts_know_no_skill_internals(self):
        """作業エージェントの使い方の文書と 3段階目の部品は、スキルの中身（スクリプト・設定のひな形のファイルと項目の名前、
        データの置き場所）を書いていない。スキルを、別の中身・別の設定の形のスキルに置き換えても、直さずに済む"""
        problems = hits(worker_guide(), internal_patterns())
        # 3段階目は、ツールの引数（prompt など）と重なる短い項目の名前を除き、_ を含む項目の名前だけを見る
        compound = internal_patterns(compound_keys_only=True)
        for path in stage3_files():
            problems += hits(path, compound)
        self.assertEqual(problems, [], "スキルの中身を書いています：\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
