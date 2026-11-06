"""部品の独立性を確かめるテスト（憲章 第25条、仕様 REQ-001〜REQ-007、REQ-102、REQ-106、REQ-108、REQ-141）。

このテストには、部品（スキルと作業エージェント）の名前も作りも書かない。部品は、次のところから読む。
そのため、スキルを足す・消す・置き換えても、作業エージェントを置き換えても、このテストは直さずに済む。
- スキル：skills/ の下の、SKILL.md があるフォルダ（フォルダの名前がスキルの名前）
- 作業エージェント：defaults.json の worker（「<プラグイン>:<名前>」）。定義は agents/<名前>.agent.md
- 設定のスキルと作業のスキル：作業エージェントの定義の「使うスキル」の表（役割・スキル・使うとき）。
  「使うとき」が「設定」の行が設定のスキル、ほかの行が作業のスキル
- スキルの中身：スキルのフォルダの scripts/ の下の .py（同梱した他者のコード scripts/vendor/ を除く）と、
  フォルダの直下の、名前に template を含むファイル（設定のひな形。YAML の「項目:」か INI の「項目 =」の行を項目とする）

確かめること：
- 1段階目：スキルは、それぞれのフォルダだけで完結する（利用者は、プラグインをインストールせずに、
  スキルのフォルダだけを自分の作業フォルダの .github/skills/<名前>/ にコピーして使う）。
  - フォルダの中に、プラグインの名前（とその略）・ほかのスキル・エージェント・Web管理画面・
    エクステンションを書いていない。フォルダの外を指していない。互いのファイルを読み込まない。
    同梱した他者のコード（scripts/vendor/）は中身を変えないので、言葉の確認からは外す。
  - 人向けの使い方（README.md）とテスト（tests/）が、フォルダの中にある。設定のひな形を持つスキルは、
    README に、データの置き場所 .<フォルダの名前>/ を書く。
  - フォルダだけを別の場所にコピーしても、そのスキルのテストが通る。
    コピーしたフォルダ（名前を変えたコピーも）で設定ファイルの用意と設定の確認が動き、データが作業フォルダの
    .<フォルダの名前>/ にだけできることは、コマンドがスキルごとに違うので、各スキルのテスト（tests/）で確かめる。
- 2段階目：作業エージェントの定義は、スキルの名前と役割を「使うスキル」の表にだけ書く。表のスキルは、
  skills/ の下のスキルと同じ。表の外には、スキルの名前も、役割の並びも、自分の名前も書かない。
  スキルの中身（スクリプトの名前、設定の項目、設定ファイルの置き場所、転送やログ取得の方法）を書いていない。
  セットアップでは、設定のスキルに渡す名前を、読む前にチャットに書く。
  作業エージェントの README は、スキルの名前を書かず、定義の表を案内する。
- 3段階目：統括エージェント・検証エージェント・エクステンションは、機器・ビルド・転送・ログ取得の
  方法やスキルの名前を書いていない。作業エージェントの名前は、設定のデータ（defaults.json）にだけある。
  定義の例に、具体の値（答えになりうる数字）を書いていない。利用者への質問の例や選択肢にも、
  頼みに書かれていない値を作って入れない。
- 文書：上の段の文書（プラグインの README、作業エージェントの README の統括・検証の部分、3段階目のガイド）に、
  スキルの名前と、転送・ログ取得の具体の言葉がない。部品を変える手順書（docs/replace.md）は、
  直す所が分かるように今の部品の名前を例として書く開発者向けの文書なので、上の段の文書に入れない。
  3段階目のガイドは、作業エージェントが機器のものだと前提にしない。作業エージェントは、
  最初の依頼に名前を書いて選ぶ。部品を変える手順書は開発者向けで、利用者向けのガイドの中にない。
- 置き換え：スキルは、作業エージェントの定義の「使うスキル」の表の行の名前を替えるだけで置き換えられる
  （作業エージェントごと置き換えても）。統括エージェント・検証エージェント・エクステンションと、作業エージェントの
  README は、スキルの中身を書いていないので、直さずに済む。
  置き換えたスキルを設定のスキルが見つけて設定できることは、設定のスキルのテストで確かめる。
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
WORKER_README = AGENTS_DIR / "README.markdown"
SETUP_WHEN = "設定"  # 「使うスキル」の表で、設定のスキルの「使うとき」
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
# 作業エージェントの定義に書いてはいけない、転送・ログ取得・ビルドの具体の言葉
WORKER_SPECIFIC_WORDS = ["FTP", "ftp", "シリアル", "serial", "scp", "Makefile", "SITE CHMOD", "." + PLUGIN_NAME]
# 3段階目の部品に書いてはいけない、機器・作業の中身の言葉
DEVICE_WORDS = ["FTP", "シリアル", "serial", "クロスコンパイル", "Makefile", "make", "IPアドレス", "IP アドレス",
                "デプロイ", "deploy", "転送", "ビルド", "build", "機器", "device", "board", "ボード", "sensor",
                "ログイン", "chmod", "crc32"]


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
    """すべてのスキルの、設定の項目の名前（4文字以上。短い名前は、ふつうの言葉と見分けにくい）"""
    return sorted({k for skill in skill_names() for k in template_keys(skill) if len(k) > 3})


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
# 作業エージェントの定義の「使うスキル」の表
# ---------------------------------------------------------------------------

SKILL_SECTION = re.compile(r"^## 使うスキル\n(.*?)(?=^## |\Z)", re.S | re.M)


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


def is_table_line(line):
    return line.lstrip().startswith("|")


def skill_table_parts(text):
    """作業エージェントの定義を、「使うスキル」の節の表の行と、表の外の文に分ける。節がなければ (None, text)"""
    m = SKILL_SECTION.search(text)
    if m is None:
        return None, text
    lines = m.group(1).splitlines(keepends=True)
    table = "".join(line for line in lines if is_table_line(line))
    rest = "".join(line for line in lines if not is_table_line(line))
    return table, text[:m.start(1)] + rest + text[m.end(1):]


def worker_table(text):
    """「使うスキル」の表の行を、(役割, スキルの名前, 使うとき) の並びで返す。表の形が違えば AssertionError"""
    table, _ = skill_table_parts(text)
    if table is None:
        raise AssertionError("作業エージェントの定義に「## 使うスキル」の節がありません")
    rows = []
    for cells in table_rows(table):
        if len(cells) != 3:
            raise AssertionError(f"「使うスキル」の表の行が、3列（役割・スキル・使うとき）ではありません：{cells}")
        m = re.fullmatch(r"`([^`]+)`", cells[1])
        if m is None:
            raise AssertionError(f"「使うスキル」の表のスキルが、skill ツールに渡す名前（`名前`）の形ではありません：{cells[1]}")
        rows.append((cells[0], m.group(1), cells[2]))
    if not rows:
        raise AssertionError("「使うスキル」の表に、行がありません")
    return rows


def setup_skill(rows):
    """設定のスキル（「使うとき」が「設定」の行）の名前。なければ None（設定のスキルは、なくてもよい）"""
    found = [skill for _, skill, when in rows if when == SETUP_WHEN]
    if len(found) > 1:
        raise AssertionError(f"「使うとき」が「{SETUP_WHEN}」の行（設定のスキル）が、2つ以上あります：{found}")
    return found[0] if found else None


def work_skills(rows):
    """作業のスキル（設定のスキルのほか）の名前。表の順"""
    return [skill for _, skill, when in rows if when != SETUP_WHEN]


def replace_in_table(text, renames):
    """「使うスキル」の表の行の `名前` だけを替える（表の外は変えない）"""
    m = SKILL_SECTION.search(text)
    if m is None:
        raise AssertionError("作業エージェントの定義に「## 使うスキル」の節がありません")
    out = []
    for line in m.group(1).splitlines(keepends=True):
        if is_table_line(line):
            for old, new in renames.items():
                line = line.replace(f"`{old}`", f"`{new}`")
        out.append(line)
    return text[:m.start(1)] + "".join(out) + text[m.end(1):]


def role_word(role):
    """役割の言葉の中心（括弧の中を除き、最後の「の」より後ろ）。「機器への転送」→「転送」"""
    core = re.sub(r"[（(][^）)]*[）)]", "", role).strip()
    core = core.rsplit("の", 1)[-1].strip()
    return core or role


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

    def test_readme_and_tests_inside(self):
        """人向けの使い方とテストが、スキルのフォルダの中にある。設定のひな形を持つスキルは、データの置き場所を書く"""
        for skill in skill_names():
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
                if templates(skill):
                    self.assertIn(f".{skill}", text, f"{skill} の README.md に、データの置き場所 .{skill}/ がありません")

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
        """スキルのフォルダだけをコピーした場所で、README に書いた動かし方で、そのスキルのテストが通る"""
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
    """2段階目：作業エージェントは、スキルの名前と役割を「使うスキル」の表でだけ知る"""

    def setUp(self):
        self.path = worker_path()
        self.assertTrue(self.path.is_file(),
                        f"defaults.json の worker の定義（{self.path.relative_to(ROOT)}）がありません")
        self.text = self.path.read_text(encoding="utf-8")
        self.table, self.outside = skill_table_parts(self.text)
        self.assertIsNotNone(self.table, "作業エージェントの定義に「## 使うスキル」の節がありません")
        self.rows = worker_table(self.text)
        self.label = f"{self.path.relative_to(ROOT)}（「使うスキル」の表の外）"

    def test_table_lists_the_skills(self):
        """表の列は、役割・スキル・使うとき。表のスキルは、skills/ の下のスキルと同じ。設定のスキルは1つ。
        どの行にも役割と使うときがある"""
        header = [c.strip() for c in self.table.splitlines()[0].strip().strip("|").split("|")]
        self.assertEqual(header, ["役割", "スキル", "使うとき"], "「使うスキル」の表の見出しが違います")
        names = [skill for _, skill, _ in self.rows]
        self.assertEqual(len(names), len(set(names)), f"表に、同じスキルが2回あります：{names}")
        self.assertEqual(sorted(names), skill_names(), "表のスキルが、skills/ の下のスキルと違います")
        setup_skill(self.rows)
        for role, skill, when in self.rows:
            self.assertTrue(role, f"表の {skill} の役割が空です")
            self.assertTrue(when, f"表の {skill} の「使うとき」が空です")

    def test_skill_names_only_in_table(self):
        names = sorted(set(skill_names()) | {skill for _, skill, _ in self.rows})
        problems = hits_in_text(self.label, self.outside, [(name, token(name)) for name in names])
        self.assertEqual(problems, [], "表の外に、スキルの名前があります：\n" + "\n".join(problems))

    def test_roles_not_listed_outside_table(self):
        """表の外に、作業のスキルの役割を並べて書かない（1つの文に、2つ以上の役割を書かない）"""
        words = sorted({role_word(role) for role, _, when in self.rows if when != SETUP_WHEN})
        problems = []
        for line in self.outside.splitlines():
            for sentence in line.split("。"):
                named = [w for w in words if w in sentence]
                if len(named) >= 2:
                    problems.append(f"「{'・'.join(named)}」 … {sentence.strip()[:120]}")
        self.assertEqual(problems, [], f"{self.label} に、役割の並びがあります：\n" + "\n".join(problems))

    def test_does_not_name_itself(self):
        """自分の名前を書かない（「この作業エージェント」と書く）。作業エージェントを置き換えても、中を直さずに済む"""
        name = agent_name("worker")
        problems = hits_in_text(self.path.relative_to(ROOT), self.text, [(name, token(name))])
        self.assertEqual(problems, [], "作業エージェントの定義に、自分の名前があります：\n" + "\n".join(problems))

    def test_no_skill_internals(self):
        # 表のスキルの名前の欄は除く（スキルの名前は、そこにだけ書く）
        text = self.outside + "\n" + "\n".join(f"{role} {when}" for role, _, when in self.rows)
        patterns = internal_patterns() + [(w, token(w)) for w in WORKER_SPECIFIC_WORDS]
        found = [label for label, pattern in patterns if pattern.search(text)]
        # 設定ファイルやログの名前・場所は、字の並びとして探す（config.yaml の .yaml も見つける）
        found += [w for w in [".yaml", ".yml", "config.yaml", "logs/"] if w in text]
        self.assertEqual(found, [], f"作業エージェントが、スキルの中身を書いています：{found}")

    def test_setup_gets_skill_names_not_paths(self):
        """セットアップでは、設定のスキルにスキルの名前を渡す。設定ファイルの場所を集めて渡さない"""
        self.assertNotRegex(self.text, r"設定ファイルの場所(の一覧)?を(返|渡して)", "設定ファイルの場所を集めて渡しています")
        self.assertNotRegex(self.text, r"設定ファイルの場所を返す", "スキルに、設定ファイルの場所を返すよう頼んでいます")

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

    def test_worker_contract_matches(self):
        """統括エージェントの頼みの書き出しと、作業エージェントが見分ける書き出しがそろっている（置き換えの約束）"""
        orchestrator = agent_path("orchestrator").read_text(encoding="utf-8")
        worker = worker_path().read_text(encoding="utf-8")
        for phrase in ["これは準備の確認です", "あなたは作業役です"]:
            self.assertRegex(orchestrator, r"(?m)^" + phrase, f"統括エージェントの頼みが「{phrase}」で始まりません")
            self.assertIn(f"「{phrase}」", worker, f"作業エージェントが「{phrase}」を見分けていません")
        for phrase in ["準備：できている", "準備：できていない"]:
            self.assertIn(phrase, orchestrator, f"統括エージェントが「{phrase}」を見ていません")
        self.assertRegex(worker, r"(?m)^準備：<できている／できていない>$", "作業エージェントの準備の確認の返事の形が違います")


class DocumentIndependence(unittest.TestCase):
    """文書：各部品の使い方は、その部品の README にだけ書く。上の段の文書に、下の段の中身を書かない"""

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
        patterns = SPECIFIC_WORDS + [(k, token(k)) for k in all_template_keys()]
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
        self.assertNotIn(WORKER_README.name, text, "3段階目のガイドが、特定の作業エージェントの README を案内しています")

    def test_guide_chooses_worker_in_first_request(self):
        """作業エージェントは、最初の依頼に名前を書いて選ぶ（画面で選ぶ前に、既定の作業エージェントが動かないように）"""
        text = (GUIDE_DIR / "3-orchestrator.md").read_text(encoding="utf-8")
        request = re.search(r"「作業エージェントは <[^>]+> で」", text)
        self.assertIsNotNone(request, "3段階目のガイドに、依頼で作業エージェントを選ぶ書き方がありません")
        # 画面で選ぶのは、2回目からの切り替え。依頼の書き方より後に書く
        screen = re.search(r"でも替えられ", text)
        self.assertIsNotNone(screen, "3段階目のガイドに、Web管理画面で替えられることが書かれていません")
        self.assertLess(request.start(), screen.start(), "3段階目のガイドで、Web管理画面で選ぶ方法が、依頼の書き方より先にあります")

    def test_worker_readme_names_no_skill(self):
        """作業エージェントの README は、スキルの名前も中身も書かない（スキルを足す・消す・替えても、README は直さずに済む）"""
        self.assertTrue(WORKER_README.is_file(), "作業エージェントの README（agents/README.markdown）がありません")
        text = WORKER_README.read_text(encoding="utf-8")
        label = WORKER_README.relative_to(ROOT)
        problems = hits_in_text(label, text, [(name, name_token(name)) for name in skill_names()])
        problems += hits_in_text(label, text, SPECIFIC_WORDS)
        problems += hits_in_text(label, text, [(k, token(k)) for k in all_template_keys()])
        self.assertEqual(problems, [], "作業エージェントの README に、スキルの名前や中身があります：\n" + "\n".join(problems))

    def test_worker_readme_points_to_the_definition_table(self):
        """作業エージェントの README の「使うスキル」は、表を持たず、作業エージェントの定義の表を案内する"""
        text = WORKER_README.read_text(encoding="utf-8")
        part = section(text, "使うスキル")
        self.assertIsNotNone(part, "作業エージェントの README に「## 使うスキル」の節がありません")
        self.assertFalse([line for line in part.splitlines() if is_table_line(line)],
                         "作業エージェントの README の「使うスキル」に、表があります（表は、作業エージェントの定義にだけ置く）")
        definition = worker_path().name
        links = [t.partition("#") for t in re.findall(r"\]\(([^)\s]+)\)", part)]
        self.assertTrue(any(Path(file_part).name == definition and anchor == "使うスキル" for file_part, _, anchor in links),
                        f"作業エージェントの README の「使うスキル」が、{definition}#使うスキル を案内していません：{links}")

    def test_agents_folder_has_only_agent_definitions_as_md(self):
        """agents/ の .md は、Copilot CLI がエージェントの定義として読み込む。README は .markdown にする"""
        for path in AGENTS_DIR.iterdir():
            if path.suffix == ".md":
                self.assertTrue(path.name.endswith(".agent.md"), f"{path.name} はエージェントとして読み込まれます")

    def test_old_guides_are_gone(self):
        # 1・2段階目の使い方は、各部品の README にある。ガイドに置かない
        old = sorted(p.name for p in GUIDE_DIR.iterdir() if re.match(r"[12]-", p.name))
        self.assertEqual(old, [], "docs/guide/ に、1・2段階目のガイドがあります（中身は各部品の README に書く）")
        # 部品の置き換えはプラグインの設計変更。利用者向けのガイドに置かない
        self.assertFalse((GUIDE_DIR / REPLACE_DOC.name).exists(), "部品を変える手順書が、利用者向けのガイドの中にあります")
        self.assertTrue(REPLACE_DOC.is_file(), "部品を変える手順書（docs/replace.md）がありません")

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
    """置き換え：スキルは「使うスキル」の表の行の名前を替えるだけで置き換えられる（作業エージェントごと置き換えても）。
    ほかの部品は、スキルの中身を書いていないので、直さずに済む"""

    def setUp(self):
        self.definition = worker_path().read_text(encoding="utf-8")
        self.rows = worker_table(self.definition)

    def test_skill_is_replaced_by_its_table_row(self):
        """作業のスキルを1つ、別の名前のスキルに替える。作業エージェントの定義で直すのは、表のその行の名前だけ"""
        works = work_skills(self.rows)
        if not works:
            self.skipTest("作業のスキルがありません")
        old = works[0]
        new = f"{old}-alt"
        replaced = replace_in_table(self.definition, {old: new})
        rows = worker_table(replaced)
        self.assertEqual([(role, when) for role, _, when in rows], [(role, when) for role, _, when in self.rows],
                         "役割と使うときは変わりません")
        self.assertEqual([skill for _, skill, _ in rows], [new if skill == old else skill for _, skill, _ in self.rows])
        self.assertEqual(setup_skill(rows), setup_skill(self.rows), "設定のスキルは変わりません")
        self.assertIsNone(token(old).search(replaced), f"表の行を替えても、定義に {old} が残ります（表の外にも書いてあります）")

    def test_replaced_worker_and_skills_need_no_upper_change(self):
        """作業エージェントごと、名前の違うスキルの組に置き換える。作業エージェントの定義で直すのは、表の行の名前だけ。
        統括エージェント・検証エージェント・エクステンションは、どの名前も書いていないので、直さずに済む"""
        old_worker = agent_name("worker")
        new_worker = f"{old_worker}-alt"
        renames = {skill: f"{skill}-alt" for _, skill, _ in self.rows}
        new_definition = replace_in_table(self.definition, renames)
        new_rows = worker_table(new_definition)
        self.assertEqual([(role, when) for role, _, when in new_rows], [(role, when) for role, _, when in self.rows],
                         "役割と使うときは変わりません")
        old_setup = setup_skill(self.rows)
        self.assertEqual(setup_skill(new_rows), renames[old_setup] if old_setup else None,
                         "設定のスキルは、表の「使うとき」で決まります")
        for word in list(renames) + [old_worker]:
            self.assertIsNone(token(word).search(new_definition), f"表の行の名前を替えても、定義に {word} が残ります")
        for path in stage3_files():
            text = path.read_text(encoding="utf-8")
            for word in [old_worker, new_worker] + list(renames) + list(renames.values()):
                self.assertIsNone(token(word).search(text), f"{path.relative_to(ROOT)} に {word} があります")

    def test_other_parts_know_no_skill_internals(self):
        """作業エージェントの README と 3段階目の部品は、スキルの中身（スクリプト・設定のひな形のファイルと項目の名前、
        データの置き場所）を書いていない。スキルを、別の中身・別の設定の形のスキルに置き換えても、直さずに済む"""
        problems = hits(WORKER_README, internal_patterns())
        # 3段階目は、ツールの引数（prompt など）と重なる短い項目の名前を除き、_ を含む項目の名前だけを見る
        compound = internal_patterns(compound_keys_only=True)
        for path in stage3_files():
            problems += hits(path, compound)
        self.assertEqual(problems, [], "スキルの中身を書いています：\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
