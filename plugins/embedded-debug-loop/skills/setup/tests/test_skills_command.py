# -*- coding: utf-8 -*-
"""道具（scripts/config_tool.py）の skills のテスト。

skills は、設定するスキルを探す。どんなスキルでも見つけられなければならない。
そのため、このテストで作った架空のスキルで試す。Windows でも Linux でも動く。

- 3つの場所（作業フォルダの .github/skills/、COPILOT_HOME の skills/、このスキルの隣）を、この順に探す。
- 同じ名前のスキルは、先に見つかったものが使われる（後のものは hidden）。
- このスキル自身は返さない。同じフォルダを2回数えない。
- README がないスキルも返す（readme は null）。
- 何も作らない。

このスキルのフォルダで、次のように動かす。
  python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
OWN_NAME = os.path.basename(SKILL_DIR)

RESULT_KEYS = {"ok", "workdir", "searched", "skills", "unreadable"}
SKILL_KEYS = {"name", "path", "where", "skill_md", "readme", "description", "hidden"}


def snapshot(folder):
    """フォルダの中のすべてのもの（フォルダとファイルの中身）。"""
    out = {}
    for root, dirs, files in os.walk(folder):
        for name in dirs:
            out[os.path.relpath(os.path.join(root, name), folder)] = None
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, folder)] = f.read()
    return out


def same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


class SkillsTestCase(unittest.TestCase):
    """作業フォルダ・利用者のフォルダ（COPILOT_HOME）・プラグインのスキルの置き場所を、一時フォルダに作る。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="setup-test-skills-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.work = os.path.join(self.base, "work")
        self.project = os.path.join(self.work, ".github", "skills")
        self.home = os.path.join(self.base, "copilot-home")
        self.personal = os.path.join(self.home, "skills")
        self.plugin = os.path.join(self.base, "plugin", "skills")
        for folder in (self.work, self.home, self.plugin):
            os.makedirs(folder)
        # このスキルを、プラグインのスキルの置き場所に写す（名前も変える）
        self.own = os.path.join(self.plugin, "my-setup")
        shutil.copytree(SKILL_DIR, self.own, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.tool = os.path.join(self.own, "scripts", "config_tool.py")

    def make_skill(self, place, folder, name=None, description="説明", readme="README.md", raw=None):
        """架空のスキルを作る。raw を渡すと、SKILL.md の中身をそのまま書く（バイト列も可）。"""
        path = os.path.join(place, folder)
        os.makedirs(path)
        if raw is None:
            lines = ["---"]
            if name is not None:
                lines.append("name: " + name)
            if description is not None:
                lines.append("description: " + description)
            lines += ["---", "", "# 本文", ""]
            raw = "\n".join(lines)
        with open(os.path.join(path, "SKILL.md"), "wb") as f:
            f.write(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        if readme:
            with open(os.path.join(path, readme), "w", encoding="utf-8") as f:
                f.write("# {}\n\n設定ファイルは `<作業フォルダ>/.{}/config.json` です。\n".format(folder, folder))
        return path

    def run_skills(self, *args, **kw):
        env = dict(os.environ)
        env.pop("COPILOT_HOME", None)
        env.update(kw.pop("env", {"COPILOT_HOME": self.home}))
        proc = subprocess.run([sys.executable, kw.pop("tool", self.tool), "skills"] + list(args),
                              cwd=kw.pop("cwd", self.work), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=60)
        self.assertEqual(kw, {})
        encoding = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
                                  stdout=subprocess.PIPE, env=env, timeout=60).stdout.decode("ascii").strip()
        data = json.loads(proc.stdout.decode(encoding))
        return proc.returncode, data

    def skills(self, *args, **kw):
        code, data = self.run_skills(*args, **kw)
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"], data)
        self.assertEqual(set(data), RESULT_KEYS)
        return data

    def by_path(self, data):
        return {os.path.normcase(os.path.realpath(s["path"])): s for s in data["skills"]}

    def entry(self, data, path):
        return self.by_path(data)[os.path.normcase(os.path.realpath(path))]


class PlacesTest(SkillsTestCase):
    def test_three_places_in_order(self):
        """作業フォルダ（project）→ 利用者のフォルダ（personal）→ 隣（sibling）の順に返す。"""
        a = self.make_skill(self.project, "build-x", description="ビルドする。設定ファイルを使う。")
        b = self.make_skill(self.personal, "send-y", description="送る")
        c = self.make_skill(self.plugin, "watch-z", description="見る")
        data = self.skills()
        self.assertEqual([(s["name"], s["where"]) for s in data["skills"]],
                         [("build-x", "project"), ("send-y", "personal"), ("watch-z", "sibling")])
        for skill, path in zip(data["skills"], (a, b, c)):
            self.assertEqual(set(skill), SKILL_KEYS)
            self.assertTrue(same(skill["path"], path))
            self.assertTrue(os.path.isabs(skill["path"]))
            self.assertTrue(same(skill["skill_md"], os.path.join(path, "SKILL.md")))
            self.assertTrue(same(skill["readme"], os.path.join(path, "README.md")))
            self.assertIs(skill["hidden"], False)
        self.assertEqual(data["skills"][0]["description"], "ビルドする。設定ファイルを使う。")
        self.assertEqual([(s["where"], s["exists"]) for s in data["searched"]],
                         [("project", True), ("personal", True), ("sibling", True)])
        self.assertTrue(same(data["searched"][0]["dir"], self.project))
        self.assertTrue(same(data["searched"][1]["dir"], self.personal))
        self.assertTrue(same(data["searched"][2]["dir"], self.plugin))
        self.assertTrue(same(data["workdir"], self.work))
        self.assertEqual(data["unreadable"], [])

    def test_sorted_by_folder_name_within_a_place(self):
        for folder in ("zeta", "alpha", "mid"):
            self.make_skill(self.project, folder, name=folder)
        self.assertEqual([s["name"] for s in self.skills()["skills"]], ["alpha", "mid", "zeta"])

    def test_missing_places(self):
        """場所がなくても失敗しない。何も作らない。"""
        shutil.rmtree(self.home)
        data = self.skills()
        self.assertEqual(data["skills"], [])
        self.assertEqual([(s["where"], s["exists"]) for s in data["searched"]],
                         [("project", False), ("personal", False), ("sibling", True)])
        self.assertFalse(os.path.exists(self.project))
        self.assertFalse(os.path.exists(self.home))

    def test_copilot_home_default(self):
        """COPILOT_HOME がなければ、ホームフォルダの .copilot/skills/ を探す。"""
        fake_home = os.path.join(self.base, "user-home")
        skill = self.make_skill(os.path.join(fake_home, ".copilot", "skills"), "home-skill")
        data = self.skills(env={"HOME": fake_home, "USERPROFILE": fake_home})
        self.assertEqual([s["name"] for s in data["skills"]], ["home-skill"])
        self.assertEqual(data["skills"][0]["where"], "personal")
        self.assertTrue(same(data["skills"][0]["path"], skill))
        self.assertTrue(same(data["searched"][1]["dir"], os.path.join(fake_home, ".copilot", "skills")))
        # 空の COPILOT_HOME も、ないのと同じ
        data = self.skills(env={"HOME": fake_home, "USERPROFILE": fake_home, "COPILOT_HOME": ""})
        self.assertEqual([s["name"] for s in data["skills"]], ["home-skill"])

    def test_workdir_option_and_current_folder(self):
        self.make_skill(self.project, "here")
        other = os.path.join(self.base, "other")
        os.makedirs(other)
        for args in (("--workdir", self.work), ("--workdir=" + self.work,)):
            data = self.skills(*args, cwd=other)
            self.assertTrue(same(data["workdir"], self.work))
            self.assertEqual([s["name"] for s in data["skills"]], ["here"])
        self.assertEqual(self.skills(cwd=other)["skills"], [])

    def test_creates_nothing(self):
        self.make_skill(self.project, "a")
        self.make_skill(self.personal, "b")
        before = {folder: snapshot(folder) for folder in (self.work, self.home, os.path.dirname(self.plugin))}
        self.skills()
        for folder, snap in before.items():
            self.assertEqual(snapshot(folder), snap, folder)

    def test_extra_arguments_are_refused(self):
        code, data = self.run_skills("extra-arg")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertIn("--workdir", data["error"])


class HiddenTest(SkillsTestCase):
    def test_project_wins_over_personal_and_sibling(self):
        """同じ名前は、作業フォルダのものが使われる。ほかは hidden で、使われるほうを hidden_by に示す。"""
        p = self.make_skill(self.project, "dup", description="作業フォルダ")
        h = self.make_skill(self.personal, "dup", description="利用者")
        s = self.make_skill(self.plugin, "dup", description="隣")
        data = self.skills()
        self.assertEqual([x["where"] for x in data["skills"]], ["project", "personal", "sibling"])
        self.assertIs(self.entry(data, p)["hidden"], False)
        self.assertNotIn("hidden_by", self.entry(data, p))
        for path in (h, s):
            entry = self.entry(data, path)
            self.assertIs(entry["hidden"], True)
            self.assertTrue(same(entry["hidden_by"], p))
            self.assertEqual(set(entry), SKILL_KEYS | {"hidden_by"})

    def test_personal_wins_over_sibling(self):
        h = self.make_skill(self.personal, "dup2")
        s = self.make_skill(self.plugin, "dup2")
        data = self.skills()
        self.assertIs(self.entry(data, h)["hidden"], False)
        self.assertIs(self.entry(data, s)["hidden"], True)
        self.assertTrue(same(self.entry(data, s)["hidden_by"], h))

    def test_name_comes_from_skill_md_not_folder(self):
        """名前は SKILL.md の name:。フォルダの名前が違っても、同じ名前なら隠れる。"""
        p = self.make_skill(self.project, "replacement-folder", name="same-name")
        s = self.make_skill(self.plugin, "same-name", name="same-name")
        data = self.skills()
        self.assertEqual(self.entry(data, p)["name"], "same-name")
        self.assertIs(self.entry(data, s)["hidden"], True)

    def test_name_falls_back_to_folder_name(self):
        path = self.make_skill(self.project, "no-name-field", name=None, description="名前のないスキル")
        entry = self.entry(self.skills(), path)
        self.assertEqual(entry["name"], "no-name-field")
        self.assertEqual(entry["description"], "名前のないスキル")


class OwnFolderTest(SkillsTestCase):
    def test_own_folder_is_not_returned(self):
        self.make_skill(self.plugin, "neighbor")
        names = [s["name"] for s in self.skills()["skills"]]
        self.assertEqual(names, ["neighbor"])
        self.assertNotIn(OWN_NAME, names)
        self.assertNotIn("my-setup", names)

    def test_own_folder_in_project_is_counted_once(self):
        """このスキルを作業フォルダの .github/skills/ に置くと、隣と作業フォルダは同じ場所。2回数えない。"""
        own = os.path.join(self.project, "my-setup")
        shutil.copytree(SKILL_DIR, own, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.make_skill(self.project, "partner")
        data = self.skills(tool=os.path.join(own, "scripts", "config_tool.py"))
        self.assertEqual([(s["name"], s["where"]) for s in data["skills"]], [("partner", "project")])
        self.assertTrue(same(data["searched"][2]["dir"], self.project))

    def test_same_name_as_self_elsewhere_is_hidden_by_self(self):
        """このスキルと同じ名前のほかのスキルは、このスキルに隠れる（このスキルは返さない）。"""
        own_name = "my-setup-renamed"
        skill_md = os.path.join(self.own, "SKILL.md")
        with open(skill_md, encoding="utf-8") as f:
            text = f.read()
        with open(skill_md, "w", encoding="utf-8") as f:
            f.write(text.replace("name: " + OWN_NAME, "name: " + own_name, 1))
        self.make_skill(self.plugin, "zz-copy", name=own_name)
        data = self.skills()
        self.assertEqual(len(data["skills"]), 1)
        self.assertIs(data["skills"][0]["hidden"], True)
        self.assertTrue(same(data["skills"][0]["hidden_by"], self.own))


class SkillFolderTest(SkillsTestCase):
    def test_skill_without_readme(self):
        path = self.make_skill(self.project, "no-readme", readme=None)
        entry = self.entry(self.skills(), path)
        self.assertIsNone(entry["readme"])
        self.assertTrue(same(entry["skill_md"], os.path.join(path, "SKILL.md")))

    def test_readme_name_is_not_case_sensitive(self):
        path = self.make_skill(self.project, "lower-readme", readme="readme.md")
        entry = self.entry(self.skills(), path)
        self.assertEqual(os.path.basename(entry["readme"]).lower(), "readme.md")
        self.assertTrue(os.path.isfile(entry["readme"]))

    def test_folders_that_are_not_skills(self):
        """SKILL.md のないフォルダ、ファイル、点で始まるフォルダは、スキルとして数えない。"""
        os.makedirs(os.path.join(self.project, "just-a-folder"))
        with open(os.path.join(self.project, "notes.md"), "w", encoding="utf-8") as f:
            f.write("x")
        self.make_skill(self.project, ".hidden-skill")
        nested = os.path.join(self.project, "group")
        os.makedirs(nested)
        self.make_skill(nested, "deeper")  # 1段だけ下を見る
        self.make_skill(self.project, "real")
        data = self.skills()
        self.assertEqual([s["name"] for s in data["skills"]], ["real"])
        self.assertEqual(data["unreadable"], [])

    def test_front_matter_variants(self):
        """SKILL.md の先頭の情報は、引用符・複数行・CRLF・BOM の書き方でも読める。"""
        cases = {
            "quoted": ('---\nname: "quoted"\ndescription: \'it\'\'s 「設定」\'\n---\n', "quoted", "it's 「設定」"),
            "double": ('---\nname: double\ndescription: "a \\"b\\" c"\n---\n', "double", 'a "b" c'),
            "folded": ("---\nname: folded\ndescription: >\n  1行目\n  2行目\n---\n", "folded", "1行目 2行目"),
            "literal": ("---\nname: literal\ndescription: |\n  1行目\n  2行目\n---\n", "literal", "1行目\n2行目"),
            "plain-multi": ("---\nname: plain-multi\ndescription: 前半\n  後半\n---\n", "plain-multi", "前半 後半"),
            "crlf": ("---\r\nname: crlf\r\ndescription: 改行が CRLF\r\n---\r\n", "crlf", "改行が CRLF"),
            "bom": (b"\xef\xbb\xbf---\nname: bom\ndescription: BOM \xe3\x81\x82\xe3\x82\x8a\n---\n", "bom", "BOM あり"),
            "comment": ("---\nname: comment # 注\ndescription: 説明 # 注\nlicense: MIT\n---\n", "comment", "説明"),
            "no-desc": ("---\nname: no-desc\n---\n", "no-desc", ""),
        }
        paths = {}
        for folder, (raw, _, _) in cases.items():
            paths[folder] = self.make_skill(self.project, folder, raw=raw)
        data = self.skills()
        for folder, (_, name, description) in cases.items():
            entry = self.entry(data, paths[folder])
            self.assertEqual(entry["name"], name, folder)
            self.assertEqual(entry["description"], description, folder)

    def test_unreadable_skill_md(self):
        """先頭の情報がない・閉じていない・UTF-8 でない SKILL.md は、unreadable に入れる。"""
        bad = {
            "no-front": "# 本文だけ\n",
            "unclosed": "---\nname: unclosed\ndescription: x\n",
            "blank-first": "\n---\nname: blank-first\n---\n",
            "not-utf8": "---\nname: sjis\ndescription: 設定\n---\n".encode("cp932"),
        }
        for folder, raw in bad.items():
            self.make_skill(self.personal, folder, raw=raw)
        self.make_skill(self.personal, "good")
        data = self.skills()
        self.assertEqual([s["name"] for s in data["skills"]], ["good"])
        unreadable = {os.path.basename(u["path"]): u for u in data["unreadable"]}
        self.assertEqual(sorted(unreadable), sorted(bad))
        for u in unreadable.values():
            self.assertEqual(set(u), {"path", "where", "error"})
            self.assertEqual(u["where"], "personal")
            self.assertTrue(u["error"])
        self.assertIn("UTF-8", unreadable["not-utf8"]["error"])

    def test_japanese_output_on_other_code_pages(self):
        """日本語の説明は、Python の既定の文字コードが cp932 でも ASCII でも、JSON として正しく読める。"""
        self.make_skill(self.project, "jp", description="機器に送る。設定ファイルを使う 😀")
        for encoding in ("cp932", "ascii", "utf-8"):
            env = {"COPILOT_HOME": self.home, "PYTHONIOENCODING": encoding}
            data = self.skills(env=env)
            self.assertEqual(data["skills"][0]["description"], "機器に送る。設定ファイルを使う 😀", encoding)


class ReplaceableTest(SkillsTestCase):
    """どんなスキルでも設定できる：知らない名前・知らない設定の形のスキルも、同じように見つかる。"""

    def test_replaced_skill_is_found_instead(self):
        original = self.make_skill(self.plugin, "transfer", description="元のスキル。YAML の設定ファイル。")
        replacement = self.make_skill(self.project, "transfer-v2", name="transfer",
                                      description="置き換えたスキル。JSON の設定ファイル。")
        with open(os.path.join(replacement, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"target": ""}, f)
        data = self.skills()
        used = [s for s in data["skills"] if s["name"] == "transfer" and not s["hidden"]]
        self.assertEqual(len(used), 1)
        self.assertTrue(same(used[0]["path"], replacement))
        self.assertEqual(used[0]["description"], "置き換えたスキル。JSON の設定ファイル。")
        self.assertTrue(same(self.entry(data, original)["hidden_by"], replacement))


if __name__ == "__main__":
    unittest.main()
