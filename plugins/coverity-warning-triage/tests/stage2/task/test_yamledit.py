"""config.yaml を、人が書いた注釈を残して書き換える部品（ctlib/yamledit.py）の試験（REQ-102）。

ct.py の画面の保存と ct.py set-verify は、この部品で、変えた値の範囲だけを書き換える。
消してよい注釈は、消した設定の項目やリストの値に付いていたもの（その行の終わりの注釈と、すぐ上の注釈の行）だけ。
扱わない書き方（「|」「>」の値、「&」「*」など）のファイルでは None を返し、呼ぶ側が今と同じ書き直しに戻る。

依存は PyYAML だけ（tests/run.py が tests/.venv の Python で動かす）。部品は、別のプロセスでなく、その場で読み込む。
"""
from __future__ import annotations

import copy
import random
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "skills" / "coverity-triage-scripts" / "scripts"

try:
    import yaml
    sys.path.insert(0, str(SCRIPTS))
    from ctlib import yamledit
except ImportError as exc:  # PyYAML がない
    yaml = None
    MISSING = str(exc)
finally:
    if str(SCRIPTS) in sys.path:
        sys.path.remove(str(SCRIPTS))

# ct.py が書く形（リストは字下げしない）に、人が注釈を書き足したファイル
CT = """\
# Coverity トリアージの設定（/coverity-setup で作成）。チームで共有するためコミットします。
# 認証情報はここに書きません（環境変数に置きます）。
coverity:
  url: https://cov.example.com:8443   # 社内の Coverity
  api: auto
  # パスの読み替え（CI と手元）
  path_strip_prefixes:
  - /build/agent/work/envmon/   # CI
  # 手元のビルド
  - C:/build/envmon/
  # 社内 CA の証明書
  ca_file: C:/certs/ca.pem
# --- ここからバージョン管理（チームで決めた） ---
vcs:
  type: git
  base_branch: main   # 既定のブランチ
max_items: 20   # 夜間に 20 件まで
"""


def changed(text: str, change) -> dict:
    data = copy.deepcopy(yaml.safe_load(text))
    change(data)
    return data


def strip_prefixes(data: dict) -> list:
    return data["coverity"]["path_strip_prefixes"]


@unittest.skipIf(yaml is None, "PyYAML がない")
class KeepsComments(unittest.TestCase):
    """値を変えた行だけが変わり、注釈は元の場所に残る（書いた中身を、そのまま期待する中身と比べる）"""

    def edit(self, change, text: str = CT) -> str:
        new = changed(text, change)
        out = yamledit.edit(text, new)
        self.assertIsNotNone(out, "扱える形なのに、書き換えられなかった")
        self.assertEqual(yaml.safe_load(out), new, "値が意図どおりでない")
        return out

    def test_value_keeps_the_comment_at_the_end_of_its_line(self):
        out = self.edit(lambda d: d["coverity"].__setitem__("url", "https://cov2.example.com"))
        self.assertEqual(out, CT.replace("url: https://cov.example.com:8443   #", "url: https://cov2.example.com   #"))

    def test_top_level_value(self):
        out = self.edit(lambda d: d.__setitem__("max_items", 50))
        self.assertEqual(out, CT.replace("max_items: 20   #", "max_items: 50   #"))

    def test_list_value_changed_in_place(self):
        out = self.edit(lambda d: strip_prefixes(d).__setitem__(1, "D:/b/"))
        self.assertEqual(out, CT.replace("  - C:/build/envmon/\n", "  - D:/b/\n"))

    def test_list_value_added_at_the_end_goes_before_the_comment_of_the_next_setting(self):
        out = self.edit(lambda d: strip_prefixes(d).append("/opt/b/"))
        self.assertEqual(out, CT.replace("  - C:/build/envmon/\n", "  - C:/build/envmon/\n  - /opt/b/\n"))

    def test_list_value_added_at_the_start(self):
        out = self.edit(lambda d: strip_prefixes(d).insert(0, "/x/"))
        self.assertEqual(out, CT.replace("  path_strip_prefixes:\n", "  path_strip_prefixes:\n  - /x/\n"))

    def test_removed_list_value_takes_its_comment_line(self):
        out = self.edit(lambda d: strip_prefixes(d).pop(1))
        self.assertEqual(out, CT.replace("  # 手元のビルド\n  - C:/build/envmon/\n", ""))

    def test_removed_first_list_value_takes_its_comment_at_the_end(self):
        out = self.edit(lambda d: strip_prefixes(d).pop(0))
        self.assertEqual(out, CT.replace("  - /build/agent/work/envmon/   # CI\n", ""))

    def test_emptied_list(self):
        out = self.edit(lambda d: d["coverity"].__setitem__("path_strip_prefixes", []))
        self.assertEqual(out, CT.replace("  path_strip_prefixes:\n  - /build/agent/work/envmon/   # CI\n"
                                         "  # 手元のビルド\n  - C:/build/envmon/\n", "  path_strip_prefixes: []\n"))

    def test_emptied_list_keeps_the_comment_of_its_key(self):
        text = CT.replace("  path_strip_prefixes:\n", "  path_strip_prefixes:   # CI で変わる\n")
        out = self.edit(lambda d: d["coverity"].__setitem__("path_strip_prefixes", []), text)
        self.assertIn("  path_strip_prefixes: []   # CI で変わる\n", out)
        self.assertNotIn("# CI\n", out)

    def test_removed_setting_takes_the_comment_line_right_above(self):
        out = self.edit(lambda d: d["coverity"].pop("ca_file"))  # CA 証明書の欄を空にしたとき
        self.assertEqual(out, CT.replace("  # 社内 CA の証明書\n  ca_file: C:/certs/ca.pem\n", ""))

    def test_removed_setting_with_its_comment_at_the_end(self):
        out = self.edit(lambda d: (d["vcs"].__setitem__("type", "svn"), d["vcs"].pop("base_branch")))
        self.assertEqual(out, CT.replace("  type: git\n  base_branch: main   # 既定のブランチ\n", "  type: svn\n"))

    def test_added_setting_goes_at_the_end_of_its_section_before_the_next_comment(self):
        out = self.edit(lambda d: d["coverity"].__setitem__("triage_store", "TS"))
        self.assertEqual(out, CT.replace("  ca_file: C:/certs/ca.pem\n", "  ca_file: C:/certs/ca.pem\n  triage_store: TS\n"))

    def test_added_section_goes_at_the_end(self):
        out = self.edit(lambda d: d.__setitem__("agents", {"integration": True, "review": False}))
        self.assertEqual(out, CT + "agents:\n  integration: true\n  review: false\n")

    def test_two_insertions_at_the_same_place_keep_their_order(self):
        """リストの最後の値の後と、節の最後は同じ場所。リストの値が先、足した設定の項目が後になる"""
        text = CT.replace("  # 社内 CA の証明書\n  ca_file: C:/certs/ca.pem\n", "")
        out = self.edit(lambda d: (strip_prefixes(d).append("/opt/b/"), d["coverity"].__setitem__("ca_file", "D:/x.pem")), text)
        self.assertEqual(out, text.replace("  - C:/build/envmon/\n", "  - C:/build/envmon/\n  - /opt/b/\n  ca_file: D:/x.pem\n"))

    def test_insertion_and_removal_at_the_same_place(self):
        out = self.edit(lambda d: (strip_prefixes(d).append("/opt/b/"), d["coverity"].pop("ca_file")))
        self.assertEqual(out, CT.replace("  - C:/build/envmon/\n  # 社内 CA の証明書\n  ca_file: C:/certs/ca.pem\n",
                                         "  - C:/build/envmon/\n  - /opt/b/\n"))

    def test_long_value_folded_by_ct_py(self):
        long = 'call C:\\tools\\env.bat && make -f makefileXX CFLAGS="-O2 -g" all -j8 V=1 PLATFORM=stm32f4 BOARD=em4-rev3'
        folded = CT + yaml.safe_dump({"verify": {"default": "build", "build_command": long}}, allow_unicode=True, sort_keys=False)
        self.assertGreater(len(folded.splitlines()), len(CT.splitlines()) + 3, "pyyaml が長い値を折り返していない")
        out = self.edit(lambda d: d["verify"].__setitem__("build_command", "make all"), folded)
        self.assertEqual(out, CT + "verify:\n  default: build\n  build_command: make all\n")
        out = self.edit(lambda d: d.__setitem__("max_items", 5), folded)
        self.assertEqual(out, folded.replace("max_items: 20   #", "max_items: 5   #"))

    def test_one_line_list_stays_on_one_line(self):
        text = CT.replace("  api: auto\n", "  api: auto\n  streams: [a, b]   # 2つ\n")
        out = yamledit.edit(text, {**yaml.safe_load(text), "coverity": {**yaml.safe_load(text)["coverity"], "streams": ["a"]}})
        self.assertIn("  streams: [a]   # 2つ\n", out)

    def test_empty_one_line_list_filled(self):
        text = CT.replace("  api: auto\n", "  api: auto\n  streams: []   # あとで\n")
        new = changed(text, lambda d: d["coverity"].__setitem__("streams", ["a", "b"]))
        out = yamledit.edit(text, new)
        self.assertIn("  streams:   # あとで\n    - a\n    - b\n  # パスの読み替え", out)
        self.assertEqual(yaml.safe_load(out), new)

    def test_key_without_a_value(self):
        text = CT.replace("ca_file: C:/certs/ca.pem", "ca_file:")
        out = self.edit(lambda d: d["coverity"].__setitem__("ca_file", "D:/x.pem"), text)
        self.assertEqual(out, CT.replace("ca_file: C:/certs/ca.pem", "ca_file: D:/x.pem"))

    def test_hash_inside_a_quoted_value_is_not_a_comment(self):
        text = CT.replace("  api: auto\n", "  api: auto\n  user_env: 'A # B'   # 注釈\n")
        out = self.edit(lambda d: d["coverity"].__setitem__("user_env", "C # D"), text)
        self.assertIn("  user_env: 'C # D'   # 注釈\n", out)

    def test_nothing_changed(self):
        self.assertEqual(yamledit.edit(CT, yaml.safe_load(CT)), CT)


@unittest.skipIf(yaml is None, "PyYAML がない")
class Unsupported(unittest.TestCase):
    """扱わない書き方のファイルは None（呼ぶ側が、保存の前に知らせて、今と同じ書き直しに戻る）"""

    def assert_none(self, text: str, change=lambda d: d.__setitem__("max_items", 5)):
        self.assertIsNone(yamledit.edit(text, changed(text, change)))

    def test_block_scalars(self):
        self.assert_none(CT.replace("  api: auto\n", "  user_env: |\n    COV_USER\n"))
        self.assert_none(CT.replace("  api: auto\n", "  user_env: >\n    COV_USER\n"))

    def test_anchors_and_aliases(self):
        text = CT.replace("  api: auto\n", "  api: &a auto\n").replace("  type: git\n", "  type: git\n  branch_prefix: *a\n")
        self.assert_none(text)
        self.assert_none(text, lambda d: d["coverity"].__setitem__("api", "fake"))

    def test_tags_documents_and_line_breaks(self):
        self.assert_none(CT.replace("max_items: 20", "max_items: !!int 20"))
        self.assertIsNone(yamledit.edit("---\n" + CT + "---\nmax_items: 1\n", yaml.safe_load(CT)), "文書が2つ")
        self.assert_none(CT.replace("\n", "\r\n"))

    def test_one_line_map(self):
        self.assert_none(CT + "agents: {integration: true}\n", lambda d: d["agents"].__setitem__("review", True))

    def test_value_that_becomes_a_map(self):
        self.assert_none(CT + "agents:\n", lambda d: d.__setitem__("agents", {"integration": True}))


@unittest.skipIf(yaml is None, "PyYAML がない")
class Check(unittest.TestCase):
    """書いた中身の確かめは、書き換えの手順とは別に、元と新しい中身を比べる。注釈を動かした中身を通さない"""

    def test_accepts_what_edit_wrote(self):
        new = changed(CT, lambda d: d["coverity"].pop("ca_file"))
        self.assertTrue(yamledit.check(CT, yamledit.edit(CT, new), new))

    def test_refuses_a_comment_line_moved_under_an_added_setting(self):
        # 注釈を残せるライブラリで起きた形：足した項目が、次の節の前の注釈の行の下に入る
        new = changed(CT, lambda d: d["coverity"].__setitem__("triage_store", "TS"))
        out = CT.replace("# --- ここからバージョン管理（チームで決めた） ---\n",
                         "# --- ここからバージョン管理（チームで決めた） ---\n  triage_store: TS\n")
        self.assertEqual(yaml.safe_load(out), new)
        self.assertFalse(yamledit.check(CT, out, new))

    def test_refuses_a_lost_comment_of_a_neighbour(self):
        new = changed(CT, lambda d: strip_prefixes(d).pop(0))
        out = CT.replace("  - /build/agent/work/envmon/   # CI\n  # 手元のビルド\n", "")
        self.assertFalse(yamledit.check(CT, out, new))

    def test_refuses_the_comment_of_a_removed_setting_put_on_the_next_one(self):
        new = changed(CT, lambda d: d["vcs"].pop("base_branch"))
        out = CT.replace("  base_branch: main   # 既定のブランチ\n", "").replace("max_items: 20   # 夜間に 20 件まで",
                                                                         "max_items: 20   # 既定のブランチ")
        self.assertFalse(yamledit.check(CT, out, new))

    def test_refuses_a_comment_at_the_end_moved_to_another_value(self):
        new = changed(CT, lambda d: strip_prefixes(d).__setitem__(1, "D:/b/"))
        out = CT.replace("  - /build/agent/work/envmon/   # CI\n", "  - /build/agent/work/envmon/\n").replace(
            "  - C:/build/envmon/\n", "  - D:/b/   # CI\n")
        self.assertFalse(yamledit.check(CT, out, new))

    def test_refuses_wrong_values(self):
        new = changed(CT, lambda d: d.__setitem__("max_items", 5))
        self.assertFalse(yamledit.check(CT, CT, new))

    def test_has_comments(self):
        self.assertTrue(yamledit.has_comments(CT))
        header_only = "# 見出し\n# 2行目\ncoverity:\n  url: x\n"
        self.assertFalse(yamledit.has_comments(header_only), "先頭の注釈の行は、全体の書き直しでも残る")
        self.assertTrue(yamledit.has_comments(header_only + "  # 下の注釈\nvcs:\n  type: git\n"))
        self.assertFalse(yamledit.has_comments("coverity:\n  user_env: 'A # B'\n"), "引用符の中の # は注釈ではない")


def _norm(line: str | None) -> str | None:
    return re.sub(r"\s+", " ", line.strip()) if line is not None else None


def comments_moved(src: str, out: str) -> list[str]:
    """yamledit とは別のやり方で、注釈が相手（行の終わりの注釈はその行、注釈の行は次の値の行）と離れたものを探す。
    相手の行が書いた後にもそのまま残っているのに、注釈がその相手と一緒でないものだけを返す。"""
    s, o = src.splitlines(), out.splitlines()
    on = [_norm(x) for x in o]
    bad = []
    for i, line in enumerate(s):
        if "#" not in line:
            continue
        if line.lstrip().startswith("#"):
            partner = next((_norm(x) for x in s[i + 1:] if x.strip() and not x.lstrip().startswith("#")), None)
            if partner is None or partner not in on:
                continue
            j = on.index(partner)
            above = [x for x in on[:j] if x]
            k = len(above) - 1
            block = []
            while k >= 0 and above[k].startswith("#"):
                block.append(above[k])
                k -= 1
            if _norm(line) not in block:
                bad.append(line)
        elif _norm(line) not in on:
            body = _norm(line[:line.index("#")])
            if body in [_norm(x.split("#")[0]) for x in o]:
                bad.append(line)
    return bad


@unittest.skipIf(yaml is None, "PyYAML がない")
class Random(unittest.TestCase):
    """ct.py が書く形に注釈をでたらめに足し、画面で変えそうな変え方を組み合わせる（いつも同じ乱数の種）"""

    LONG = 'call C:\\tools\\env.bat && make -f makefileXX CFLAGS="-O2 -g" all -j8 V=1 PLATFORM=stm32f4 BOARD=em4-rev3'

    def base(self, rnd: random.Random) -> str:
        coverity = {"url": "https://cov.example.com:8443", "api": "auto", "user_env": "COV_USER", "key_env": "COV_AUTH_KEY",
                    "triage_store": "Default Triage Store", "revision_field": "sourceVersion",
                    "path_strip_prefixes": rnd.sample(["/a/", "/b/", "C:/c/", "/d/"], rnd.randint(0, 3))}
        if rnd.random() < .5:
            coverity["ca_file"] = "C:/certs/ca.pem"
        vcs = {"type": "git", "base_branch": "main", "branch_prefix": "coverity-fix/"} if rnd.random() < .7 else {"type": "svn"}
        data = {"coverity": coverity, "vcs": vcs, "output_dir": "../coverity-triage-out", "max_items": 100, "parallel": 4,
                "options": {"annotation": False, "markdown": False},
                "verify": {"default": "build", "build_command": self.LONG if rnd.random() < .5 else "make all"},
                "agents": {"integration": rnd.random() < .5, "review": False, "max_rework": 1}}
        return "# 見出し\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    def comment(self, rnd: random.Random, text: str) -> str:
        out, n = [], 0
        lines = text.splitlines()
        cont = lambda x: x.strip() and not x.lstrip().startswith(("-", "#")) and ":" not in x  # 折り返した値の続きの行
        for i, line in enumerate(lines):
            if line.startswith("#") or cont(line):
                out.append(line)
                continue
            if rnd.random() < .3:
                n += 1
                out.append(" " * (len(line) - len(line.lstrip())) + f"# 上の注釈{n}")
            following = i + 1 < len(lines) and cont(lines[i + 1])
            if rnd.random() < .3 and not line.rstrip().endswith(":") and not following:
                n += 1
                line += f"   # 行末{n}"
            out.append(line)
        return "\n".join(out) + "\n"

    def change(self, rnd: random.Random, d: dict) -> None:
        r = rnd.random
        if r() < .3:
            d["coverity"]["url"] = "https://cov2.example.com"
        if r() < .3:
            if d["coverity"].get("ca_file"):
                del d["coverity"]["ca_file"]
            else:
                d["coverity"]["ca_file"] = "D:/x.pem"
        if r() < .5:
            values = d["coverity"]["path_strip_prefixes"]
            kind = rnd.choice(["add", "insert", "del", "chg", "empty"])
            if kind == "add":
                values.append("/new/")
            elif kind == "insert":
                values.insert(0, "/first/")
            elif kind == "del" and values:
                values.pop(rnd.randrange(len(values)))
            elif kind == "chg" and values:
                values[rnd.randrange(len(values))] = "/chg/"
            elif kind == "empty":
                values.clear()
        if r() < .2 and d["vcs"]["type"] == "git":
            d["vcs"] = {"type": "svn"}
        if r() < .3:
            d["verify"]["build_command"] = self.LONG + " X=1"
        if r() < .3:
            d["agents"]["review"] = not d["agents"]["review"]
        if r() < .2:
            d["max_items"] = 7
        if r() < .2:
            d["options"]["metrics"] = True

    def test_random_changes_keep_every_comment_with_its_line(self):
        rnd = random.Random(20261104)
        for round_ in range(300):
            with self.subTest(round=round_):
                text = self.comment(rnd, self.base(rnd))
                new = copy.deepcopy(yaml.safe_load(text))
                self.change(rnd, new)
                out = yamledit.edit(text, new)
                self.assertIsNotNone(out, f"ct.py が書く形なのに、書き換えられなかった:\n{text}\n→ {new}")
                self.assertEqual(yaml.safe_load(out), new)
                self.assertEqual(comments_moved(text, out), [], f"注釈が動いた:\n{text}\n---\n{out}")


if __name__ == "__main__":
    unittest.main()
