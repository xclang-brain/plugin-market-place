# -*- coding: utf-8 -*-
"""このスキルの道具（scripts/config_tool.py）の試験。

道具は、どんな設定ファイルでも扱えなければならない。
そのため、この試験のための見本（コーヒーメーカーとネットワークカメラ）で試す。
標準ライブラリだけで動く。PyYAML があれば、書き込んだファイルが
ふつうの YAML として読めるかも確かめる。

このスキルのフォルダで、次のように動かす。
  python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
TOOL = os.path.join(SKILL_DIR, "scripts", "config_tool.py")
FIXTURES = os.path.join(HERE, "fixtures")
COFFEE = "coffee-machine.yaml"
CAMERA = "network-camera.yaml"
MASK = "＊＊＊"

try:
    import yaml  # noqa: F401  （ない環境では、その試験だけ飛ばす）
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def run_tool(args, cwd, stdin=None, env=None, tool=TOOL):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, tool] + list(args),
        cwd=cwd,
        input=stdin.encode("utf-8") if isinstance(stdin, str) else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=full_env,
        timeout=60,
    )
    out = proc.stdout.decode("utf-8")
    try:
        data = json.loads(out)
    except ValueError:
        raise AssertionError("標準出力が JSON ではありません: {!r}\nstderr: {}".format(
            out, proc.stderr.decode("utf-8", "replace")))
    return proc.returncode, data, out


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="setup-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def copy(self, name, dest_dir=None, dest_name=None):
        folder = dest_dir or self.tmp
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, dest_name or name)
        shutil.copyfile(os.path.join(FIXTURES, name), path)
        return path

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def write(self, name, text, binary=False):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(text if binary else text.encode("utf-8"))
        return path

    def tool(self, *args, **kw):
        return run_tool(args, kw.pop("cwd", self.tmp), **kw)

    def ok(self, *args, **kw):
        code, data, _ = self.tool(*args, **kw)
        self.assertEqual(code, 0, data)
        self.assertTrue(data["ok"], data)
        return data

    def fail_(self, *args, **kw):
        code, data, out = self.tool(*args, **kw)
        self.assertEqual(code, 2, data)
        self.assertFalse(data["ok"], data)
        self.assertIsInstance(data["error"], str)
        self.assertTrue(data["error"])
        return data, out

    def items(self, path):
        return {it["key"]: it for it in self.ok("show", path)["items"]}

    def value(self, path, key):
        return self.ok("get", path, key)["value"]

    def assert_only_lines_changed(self, before, after, changed):
        """changed（1 から数える行の番号）以外の行が、バイト単位で同じことを確かめる。"""
        b = before.split(b"\n")
        a = after.split(b"\n")
        self.assertEqual(len(a), len(b))
        for i, (x, y) in enumerate(zip(b, a), 1):
            if i in changed:
                self.assertNotEqual(x, y, "{} 行目が変わっていません".format(i))
            else:
                self.assertEqual(x, y, "{} 行目が変わりました".format(i))


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

# 項目のすぐ上に # の説明がある形（候補になる）
DESCRIBED = "# 見本\n\n# 名前（必須）。\nname: \"\"\n"
# 説明のない形（候補にならない）
PLAIN = "name: \"\"\ncount: 1\n"
HEADER_ONLY = "# 見本のファイル。\n\nname: \"\"\n"
BARE_HASH = "#\nname: \"\"\n"
# 中を見ないフォルダ
SKIPPED = (".git", "node_modules", "__pycache__", ".venv", "venv", os.path.join("a", "node_modules"))


def tree(folder):
    """フォルダの中のすべてのもの（作業フォルダからの場所。区切りは /）。"""
    out = []
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            out.append(os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/"))
    return sorted(out)


def snapshot(folder):
    """フォルダの中のファイルと、その中身。"""
    out = {}
    for root, dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, folder)] = f.read()
    return out


class ListTest(ToolTestCase):
    """list：作業フォルダの中から、項目のすぐ上に # の説明があるファイルを探す。"""

    EXPECTED = [COFFEE, ".myapp/extra.yml", ".myapp/" + CAMERA, "a/b/c/deep.yaml"]

    def setUp(self):
        ToolTestCase.setUp(self)
        self.copy(COFFEE)
        self.copy(CAMERA, os.path.join(self.tmp, ".myapp"))     # 点で始まるフォルダも見る
        self.put(".myapp/extra.yml", DESCRIBED)                   # .yml も探す
        self.put("a/b/c/deep.yaml", DESCRIBED)                    # 3 段下までは探す
        self.put("a/b/c/d/too-deep.yaml", DESCRIBED)              # 4 段下は探さない
        for folder in SKIPPED:
            self.put(os.path.join(folder, "x.yaml"), DESCRIBED)
        self.put("plain.yaml", PLAIN)
        self.put("header-only.yaml", HEADER_ONLY)
        self.put("bare-hash.yaml", BARE_HASH)
        self.put("notes.txt", DESCRIBED)
        os.makedirs(os.path.join(self.tmp, "folder.yaml"))        # フォルダは数えない

    def put(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def names(self, data):
        return [d["name"] for d in data["details"]]

    def test_search_shape(self):
        data = self.ok("list")
        self.assertEqual(set(data), {"ok", "dir", "exists", "max_depth", "files", "details", "unreadable"})
        self.assertEqual(os.path.realpath(data["dir"]), os.path.realpath(self.tmp))
        self.assertTrue(data["exists"])
        self.assertEqual(data["max_depth"], 3)
        self.assertEqual(self.names(data), self.EXPECTED)
        self.assertEqual(data["files"], [os.path.join(data["dir"], *n.split("/")) for n in self.EXPECTED])
        for p in data["files"]:
            self.assertTrue(os.path.isabs(p))
        self.assertEqual(data["unreadable"], [])
        details = {d["name"]: d for d in data["details"]}
        self.assertEqual(set(details[COFFEE]), {"file", "name", "header", "items", "missing"})
        self.assertEqual(details[COFFEE]["header"], "コーヒーメーカーの設定ファイルです（試験用の見本）。")
        self.assertEqual(details[COFFEE]["items"], 11)
        self.assertEqual(details[COFFEE]["missing"], ["machine_name"])
        self.assertEqual(details[".myapp/" + CAMERA]["missing"], ["camera_ip"])
        self.assertEqual(details["a/b/c/deep.yaml"]["header"], "見本")

    def test_depth_limit(self):
        names = self.names(self.ok("list"))
        self.assertIn("a/b/c/deep.yaml", names)
        self.assertNotIn("a/b/c/d/too-deep.yaml", names)

    def test_skipped_folders(self):
        names = self.names(self.ok("list"))
        for folder in SKIPPED:
            self.assertNotIn(folder.replace(os.sep, "/") + "/x.yaml", names)

    def test_dot_folders_are_searched(self):
        names = self.names(self.ok("list"))
        self.assertIn(".myapp/extra.yml", names)
        self.assertIn(".myapp/" + CAMERA, names)

    def test_only_files_with_described_keys(self):
        self.put("one-described.yaml", "count: 1\n# 色。\ncolor: \"red\"\n")
        names = self.names(self.ok("list"))
        self.assertIn("one-described.yaml", names)      # 説明のある項目が1つでもあれば候補
        for name in ("plain.yaml", "header-only.yaml", "bare-hash.yaml", "notes.txt", "folder.yaml"):
            self.assertNotIn(name, names)

    def test_broken_file_reported_without_failing(self):
        self.put("conf/broken.yaml", "# 名前\nname: \"x\"\n# 組\nnested:\n  a: 1\n")
        self.put("conf/broken-plain.yaml", "a:\n  b: 1\n")                       # 説明がない
        self.put("conf/sjis.yaml", "# 説明\nname: 名前\n".encode("cp932"))
        data = self.ok("list")
        self.assertEqual(self.names(data), self.EXPECTED)                       # 候補にはしない
        unreadable = {d["name"]: d for d in data["unreadable"]}
        self.assertEqual(sorted(unreadable), ["conf/broken.yaml", "conf/sjis.yaml"])
        self.assertEqual(set(unreadable["conf/broken.yaml"]), {"file", "name", "error"})
        self.assertIn("入れ子", unreadable["conf/broken.yaml"]["error"])
        self.assertIn("UTF-8", unreadable["conf/sjis.yaml"]["error"])
        self.assertEqual(unreadable["conf/broken.yaml"]["file"], os.path.join(data["dir"], "conf", "broken.yaml"))

    def test_workdir_option(self):
        other = tempfile.mkdtemp(prefix="setup-test-cwd-")
        self.addCleanup(shutil.rmtree, other, True)
        for args in (("list", "--workdir", self.tmp), ("list", "--workdir=" + self.tmp)):
            data = self.ok(*args, cwd=other)
            self.assertEqual(data["dir"], os.path.abspath(self.tmp))
            self.assertEqual(self.names(data), self.EXPECTED)
        self.assertEqual(self.ok("list", cwd=other)["files"], [])

    def test_dir_option_is_unchanged(self):
        """--dir は、そのフォルダの中（下のフォルダは見ない）の YAML を、説明がなくても全部返す。"""
        self.put(".myapp/plain.yaml", PLAIN)
        self.put(".myapp/broken.yaml", "a:\n  b: 1\n")
        self.put(".myapp/sub/x.yaml", DESCRIBED)
        self.put(".myapp/notes.txt", DESCRIBED)
        other = tempfile.mkdtemp(prefix="setup-test-cwd-")
        self.addCleanup(shutil.rmtree, other, True)
        folder = os.path.join(self.tmp, ".myapp")
        for args, cwd in ((("list", "--dir", ".myapp"), self.tmp),
                          (("list", "--dir", folder), other),
                          (("list", "--dir=" + folder), other),
                          (("list", "--dir", ".myapp", "--workdir", self.tmp), other)):
            data = self.ok(*args, cwd=cwd)
            self.assertEqual(set(data), {"ok", "dir", "exists", "files", "details"})
            self.assertEqual(os.path.realpath(data["dir"]), os.path.realpath(folder))
            self.assertTrue(data["exists"])
            names = ["broken.yaml", "extra.yml", CAMERA, "plain.yaml"]
            self.assertEqual([os.path.basename(p) for p in data["files"]], names)
            self.assertEqual(self.names(data), names)
            broken = [d for d in data["details"] if d["name"] == "broken.yaml"][0]
            self.assertIn("入れ子", broken["error"])

    def test_dir_option_missing_folder(self):
        data = self.ok("list", "--dir", "none")
        self.assertFalse(data["exists"])
        self.assertEqual(data["files"], [])
        self.assertEqual(data["dir"], os.path.join(os.path.realpath(self.tmp), "none"))

    def test_empty_workdir_creates_nothing(self):
        empty = tempfile.mkdtemp(prefix="setup-test-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        data = self.ok("list", cwd=empty)
        self.assertTrue(data["exists"])
        self.assertEqual((data["files"], data["details"], data["unreadable"]), ([], [], []))
        self.assertEqual(os.listdir(empty), [])

    def test_missing_workdir(self):
        missing = os.path.join(self.tmp, "none")
        data = self.ok("list", "--workdir", missing)
        self.assertFalse(data["exists"])
        self.assertEqual(data["files"], [])
        self.assertFalse(os.path.exists(missing))

    def test_own_folder_is_not_searched(self):
        """このスキルのフォルダが作業フォルダの中にあっても、中の試験の見本は候補に出さない。"""
        work = tempfile.mkdtemp(prefix="setup-test-work-")
        self.addCleanup(shutil.rmtree, work, True)
        kit = os.path.join(work, "kit")
        shutil.copytree(SKILL_DIR, kit, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with open(os.path.join(work, "app.yaml"), "w", encoding="utf-8") as f:
            f.write(DESCRIBED)
        code, data, _ = run_tool(["list"], work, tool=os.path.join(kit, "scripts", "config_tool.py"))
        self.assertEqual(code, 0, data)
        self.assertEqual(self.names(data), ["app.yaml"])
        # ほかの場所の道具から見れば、kit/ はふつうのフォルダ（3 段下の見本も見つかる）
        names = self.names(self.ok("list", cwd=work))
        self.assertIn("kit/tests/fixtures/" + COFFEE, names)


class StandaloneTest(unittest.TestCase):
    """このフォルダだけを空の場所に写しても動くこと（名前を変えた写しも）。
    作業フォルダには、点で始まるフォルダもファイルも作らないこと。"""

    def test_copied_folder_works_in_empty_workdir(self):
        for folder_name in ("setup", "yaml-filler"):
            with self.subTest(folder=folder_name):
                base = tempfile.mkdtemp(prefix="setup-test-copy-")
                self.addCleanup(shutil.rmtree, base, True)
                copy = os.path.join(base, "place", folder_name)
                shutil.copytree(SKILL_DIR, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                tool = os.path.join(copy, "scripts", "config_tool.py")
                skill_before = snapshot(copy)
                work = os.path.join(base, "work")
                os.makedirs(os.path.join(work, "conf"))
                coffee = os.path.join(work, "coffee.yaml")
                shutil.copyfile(os.path.join(FIXTURES, COFFEE), coffee)
                shutil.copyfile(os.path.join(FIXTURES, CAMERA), os.path.join(work, "conf", "camera.yaml"))

                def call(*args, **kw):
                    code, data, _ = run_tool(args, work, tool=tool, **kw)
                    self.assertEqual(code, 0, data)
                    self.assertTrue(data["ok"], data)
                    return data

                data = call("list")
                self.assertEqual([d["name"] for d in data["details"]], ["coffee.yaml", "conf/camera.yaml"])
                self.assertEqual(call("show", "coffee.yaml")["missing"], ["machine_name"])
                self.assertTrue(call("set", "coffee.yaml", "machine_name", "--stdin", stdin="1号機\n")["changed"])
                self.assertTrue(call("set", "conf/camera.yaml", "rtsp_port", "--stdin", "--json",
                                     stdin="8554\n")["changed"])
                self.assertEqual(call("get", coffee, "machine_name")["value"], "1号機")
                self.assertEqual(call("show", "coffee.yaml")["missing"], [])
                # 作業フォルダには、点で始まるものも、ほかのものも、何もできない
                self.assertEqual(tree(work), ["coffee.yaml", "conf", "conf/camera.yaml"])
                # 写したスキルのフォルダも、書き換えない
                self.assertEqual(snapshot(copy), skill_before)


# ---------------------------------------------------------------------------
# show / get
# ---------------------------------------------------------------------------

class ShowTest(ToolTestCase):
    def test_show_coffee(self):
        path = self.copy(COFFEE)
        data = self.ok("show", path)
        self.assertEqual(data["file"], path)
        self.assertEqual(data["header"], "コーヒーメーカーの設定ファイルです（試験用の見本）。\nいれ方と、お手入れの日を書きます。")
        keys = [it["key"] for it in data["items"]]
        self.assertEqual(keys, ["machine_name", "bean_type", "cup_size_ml", "water_temp_c", "use_milk",
                                "favorite_menu", "cleaning_days", "maintenance_password",
                                "maintenance_password_env", "last_service", "greeting"])
        it = {i["key"]: i for i in data["items"]}
        self.assertEqual(it["machine_name"]["comment"], "機械の名前。画面に出ます（必須）。")
        self.assertTrue(it["machine_name"]["required"])
        self.assertTrue(it["machine_name"]["empty"])
        self.assertEqual(it["machine_name"]["examples"], ["給湯室の1号機"])
        self.assertEqual(it["bean_type"]["choices"], ["arabica", "robusta", "blend"])
        self.assertEqual(it["bean_type"]["value"], "blend")
        self.assertEqual(it["cup_size_ml"]["type"], "number")
        self.assertEqual(it["cup_size_ml"]["examples"], [180, 240])
        self.assertEqual(it["water_temp_c"]["value"], 92.5)
        self.assertEqual(it["use_milk"]["type"], "boolean")
        self.assertEqual(it["use_milk"]["choices"], ["true", "false"])
        self.assertEqual(it["favorite_menu"]["value"], ["espresso", "latte"])
        self.assertEqual(it["favorite_menu"]["examples"], [["espresso", "latte"]])
        self.assertEqual(it["cleaning_days"]["value"], ["mon", "thu"])  # ブロックのリスト
        self.assertEqual(it["cleaning_days"]["type"], "list")
        self.assertTrue(it["cleaning_days"]["required"])
        self.assertFalse(it["cleaning_days"]["empty"])
        self.assertTrue(it["maintenance_password"]["secret"])
        # 空のパスワードは ＊＊＊ にせず、空と分かるように返す
        self.assertEqual(it["maintenance_password"]["value"], "")
        self.assertTrue(it["maintenance_password"]["empty"])
        self.assertEqual(it["maintenance_password"]["env_alternative"], "maintenance_password_env")
        self.assertFalse(it["maintenance_password_env"]["secret"])
        self.assertEqual(it["maintenance_password_env"]["env_for"], "maintenance_password")
        self.assertIsNone(it["last_service"]["value"])
        self.assertEqual(it["last_service"]["type"], "null")
        self.assertTrue(it["last_service"]["empty"])
        self.assertEqual(it["greeting"]["value"], "いらっしゃいませ")  # '…' の文字
        self.assertEqual(data["missing"], ["machine_name"])
        self.assertEqual(it["machine_name"]["line"], 6)

    def test_show_camera(self):
        path = self.copy(CAMERA)
        data = self.ok("show", path)
        it = {i["key"]: i for i in data["items"]}
        self.assertEqual(len(data["header"].split("\n")), 3)
        self.assertEqual(it["camera_ip"]["examples"], ["192.168.0.20"])
        self.assertEqual(it["rtsp_port"]["value"], 554)
        self.assertEqual(it["resolution"]["choices"], ["720p", "1080p", "4k"])
        self.assertEqual(it["admin_user"]["value"], "admin")
        self.assertTrue(it["api_token"]["secret"])
        self.assertEqual(it["api_token"]["value"], MASK)
        self.assertEqual(it["api_token"]["env_alternative"], "api_token_env")
        self.assertIs(it["night_mode"]["value"], True)
        self.assertEqual(it["recording_dirs"]["value"], ["/mnt/rec1", "/mnt/録画 2"])  # 字下げなしのブロック
        self.assertEqual(it["recording_dirs"]["examples"], [["/mnt/rec1", "/mnt/rec2"]])
        self.assertEqual(it["motion_zones"]["value"], [])
        self.assertTrue(it["motion_zones"]["empty"])
        self.assertEqual(it["snapshot_interval_seconds"]["value"], 0.5)
        self.assertEqual(data["missing"], ["camera_ip"])

    def test_show_table_for_the_final_report(self):
        """終わりの報告にそのまま貼る表。空は（空）、パスワードは ＊＊＊、長い値は真ん中を省く。"""
        path = self.copy(CAMERA)
        self.ok("set", path, "admin_user", "a|b")
        self.ok("set", path, "camera_ip", "x" * 40 + "y" * 40)
        data = self.ok("show", path)
        lines = data["table"].split("\n")
        self.assertEqual(lines[:2], ["| 項目 | 値 |", "| --- | --- |"])
        self.assertEqual(len(lines), 2 + len(data["items"]))
        rows = {}
        for line in lines[2:]:
            self.assertTrue(line.startswith("| ") and line.endswith(" |"), line)
            key, _, cell = line[2:-2].partition(" | ")
            rows[key] = cell
        self.assertEqual(list(rows), [it["key"] for it in data["items"]])
        self.assertEqual(rows["api_token"], MASK)
        self.assertEqual(rows["motion_zones"], "（空）")
        self.assertEqual(rows["rtsp_port"], "`554`")
        self.assertEqual(rows["night_mode"], "`true`")
        self.assertEqual(rows["admin_user"], "`a\\|b`")
        self.assertEqual(rows["camera_ip"], "`" + "x" * 29 + "…" + "y" * 30 + "`")
        self.assertEqual(rows["recording_dirs"], '`["/mnt/rec1", "/mnt/録画 2"]`')
        self.assertNotIn("tk-0123456789abcdef", data["table"])

    def test_show_table_keeps_long_paths_readable(self):
        """60 文字までは切らない。長いパスは真ん中を省き、頭と終わり（ファイルの名前）を残す（S2-r3）。"""
        path = self.copy(CAMERA)
        sixty = "/opt/" + "a" * 55
        self.ok("set", path, "camera_ip", "--stdin", stdin=sixty + "\n")
        self.assertIn("| camera_ip | `" + sixty + "` |", self.ok("show", path)["table"])
        long_path = "/opt/poky/4.0/sysroots/x86_64-pokysdk-linux/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi"
        self.ok("set", path, "camera_ip", "--stdin", stdin=long_path + "\n")
        table = self.ok("show", path)["table"]
        row = [line for line in table.split("\n") if line.startswith("| camera_ip |")][0]
        cell = row[len("| camera_ip | `"):-len("` |")]
        self.assertEqual(len(cell), 60)
        self.assertEqual(cell.count("…"), 1)
        head, tail = cell.split("…")
        self.assertTrue(long_path.startswith(head), cell)
        self.assertTrue(long_path.endswith(tail), cell)
        self.assertTrue(cell.endswith("poky-linux-gnueabi"), cell)

    def test_show_table_empty_secret(self):
        path = self.copy(COFFEE)
        table = self.ok("show", path)["table"]
        self.assertIn("| maintenance_password | （空） |", table)
        self.assertIn("| machine_name | （空） |", table)
        self.assertIn("| greeting | `いらっしゃいませ` |", table)

    def test_fill_recommended(self):
        """必須ではないが、コメントに「必ず書く」とある項目。「必ずしも」は数えない。"""
        path = self.write("r.yaml",
                          "# 見本\n\n"
                          "# 読み込むファイル。ふだん使うなら、必ず書く。\n# 例：/etc/a.conf\nenv_file: \"\"\n\n"
                          "# 名前（必須）。必ず書く。\nname: \"\"\n\n"
                          "# メモ。必ずしも要らない。\nmemo: \"\"\n\n"
                          "# 色。\n# 例：必ず赤\ncolor: \"\"\n")
        it = self.items(path)
        self.assertTrue(it["env_file"]["fill_recommended"])
        self.assertFalse(it["name"]["fill_recommended"])  # 必須の項目は required で扱う
        self.assertFalse(it["memo"]["fill_recommended"])
        self.assertFalse(it["color"]["fill_recommended"])  # 例の行は説明ではない
        coffee = self.items(self.copy(COFFEE))
        self.assertFalse(any(v["fill_recommended"] for v in coffee.values()))

    def test_can_be_empty(self):
        """空にしてよいか。必須の項目と、空を選べない「選べる値」の項目は空にできない（S1-r3）。"""
        coffee = self.items(self.copy(COFFEE))
        self.assertFalse(coffee["machine_name"]["can_be_empty"])    # 必須
        self.assertFalse(coffee["bean_type"]["can_be_empty"])       # 選べる値に空がない
        self.assertFalse(coffee["cleaning_days"]["can_be_empty"])   # 必須で、選べる値がある
        self.assertTrue(coffee["greeting"]["can_be_empty"])
        self.assertTrue(coffee["favorite_menu"]["can_be_empty"])
        self.assertTrue(coffee["last_service"]["can_be_empty"])
        path = self.write("c.yaml", "# 見本\n\n# 色。空なら色なし。\n# 選べる値：\"\" / red / blue\ncolor: \"red\"\n")
        it = self.items(path)
        self.assertEqual(it["color"]["choices"], ["", "red", "blue"])
        self.assertTrue(it["color"]["can_be_empty"])

    def test_ask_alone(self):
        """パスワードを環境変数で渡すための項目は、単独では聞かない（S2-r3）。"""
        coffee = self.items(self.copy(COFFEE))
        self.assertFalse(coffee["maintenance_password_env"]["ask_alone"])
        self.assertTrue(coffee["maintenance_password"]["ask_alone"])
        self.assertTrue(coffee["machine_name"]["ask_alone"])
        camera = self.items(self.copy(CAMERA))
        self.assertFalse(camera["api_token_env"]["ask_alone"])
        # もとの項目がパスワードでなければ、ふつうの項目として聞く
        path = self.write("e.yaml", "# 見本\n\n# 言葉\nlang: \"ja\"\n\n# 言葉を入れた環境変数の名前\nlang_env: \"\"\n")
        it = self.items(path)
        self.assertEqual(it["lang_env"]["env_for"], "lang")
        self.assertTrue(it["lang_env"]["ask_alone"])

    def test_value_digest(self):
        """パスワードなどは、値を見せずに、書き換わったかが分かる指紋を返す（S2-r3）。"""
        path = self.copy(CAMERA)
        first = self.items(path)
        digest = first["api_token"]["value_digest"]
        self.assertRegex(digest, r"^[0-9a-f]{8}$")
        self.assertNotIn("value_digest", first["admin_user"])          # パスワードでない項目にはない
        self.assertNotIn("value_digest", first["api_token_env"])
        # 同じ値なら同じ指紋、違う値なら違う指紋
        self.assertEqual(self.items(path)["api_token"]["value_digest"], digest)
        self.assertEqual(self.ok("get", path, "api_token")["value_digest"], digest)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("tk-0123456789abcdef", "tk-0123456789abcdeg"))
        changed = self.items(path)["api_token"]["value_digest"]
        self.assertRegex(changed, r"^[0-9a-f]{8}$")
        self.assertNotEqual(changed, digest)
        # 手で同じ値に戻せば、指紋も戻る
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.assertEqual(self.items(path)["api_token"]["value_digest"], digest)
        # 値そのものや、ただの sha256 は出さない
        import hashlib
        code, data, out = self.tool("show", path)
        self.assertNotIn("tk-0123456789abcdef", out)
        for raw in ("tk-0123456789abcdef", '"tk-0123456789abcdef"'):
            self.assertNotIn(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8], out)
        # 空の値は、空の指紋
        coffee = self.items(self.copy(COFFEE))
        self.assertEqual(coffee["maintenance_password"]["value_digest"], "")

    def test_secret_never_printed(self):
        path = self.copy(CAMERA)
        for args in (("show", path), ("get", path, "api_token"), ("list", "--dir", self.tmp), ("list",)):
            code, data, out = self.tool(*args)
            self.assertEqual(code, 0)
            self.assertNotIn("tk-0123456789abcdef", out)
        self.assertEqual(self.value(path, "api_token"), MASK)

    def test_env_defined(self):
        path = self.copy(CAMERA)
        self.ok("set", path, "api_token_env", "SETUP_TEST_TOKEN")
        it = {i["key"]: i for i in self.tool("show", path, env={"SETUP_TEST_TOKEN": ""})[1]["items"]}
        self.assertFalse(it["api_token_env"]["env_defined"])
        it = {i["key"]: i for i in self.tool("show", path, env={"SETUP_TEST_TOKEN": "x"})[1]["items"]}
        self.assertTrue(it["api_token_env"]["env_defined"])
        self.assertNotIn("env_defined", it["rtsp_port"])

    def test_get(self):
        path = self.copy(COFFEE)
        data = self.ok("get", path, "cup_size_ml")
        self.assertEqual(data["value"], 180)
        self.assertEqual(data["type"], "number")
        self.assertEqual(self.value(path, "cleaning_days"), ["mon", "thu"])
        data, _ = self.fail_("get", path, "no_such_key")
        self.assertIn("no_such_key", data["error"])

    def test_list_example_written_as_scalar(self):
        path = self.write("e.yaml", "# 見本\n\n# 送るもの（必須）\n# 例：app.bin\nfiles: []\n")
        it = self.items(path)
        self.assertEqual(it["files"]["examples"], [["app.bin"]])
        self.ok("set", path, "files", json.dumps(it["files"]["examples"][0]), "--json")
        self.assertEqual(self.value(path, "files"), ["app.bin"])

    def test_relative_path_and_workdir(self):
        self.copy(COFFEE)
        data = self.ok("show", COFFEE)
        self.assertEqual(data["file"], os.path.join(self.tmp, COFFEE))
        other = tempfile.mkdtemp(prefix="setup-test-cwd-")
        self.addCleanup(shutil.rmtree, other, True)
        data = self.ok("show", COFFEE, "--workdir", self.tmp, cwd=other)
        self.assertEqual(data["file"], os.path.join(self.tmp, COFFEE))


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------

class SetTest(ToolTestCase):
    def test_set_string_japanese_round_trip(self):
        path = self.copy(COFFEE)
        before = self.read(path)
        text = "給湯室の「1号機」 #2 \"引用\" と \\ と タブ\tも"
        data = self.ok("set", path, "machine_name", text)
        self.assertTrue(data["changed"])
        self.assertEqual(data["previous"], "")
        self.assertEqual(self.value(path, "machine_name"), text)
        after = self.read(path)
        self.assert_only_lines_changed(before, after, {6})
        line = after.split(b"\n")[5].decode("utf-8")
        self.assertEqual(line, "machine_name: " + json.dumps(text, ensure_ascii=False))
        self.assertEqual(self.items(path)["machine_name"]["comment"], "機械の名前。画面に出ます（必須）。")

    def test_set_keeps_all_comments_and_order(self):
        path = self.copy(CAMERA)
        before = self.read(path)
        self.ok("set", path, "camera_ip", "192.168.0.20")
        self.ok("set", path, "rtsp_port", "8554")
        self.ok("set", path, "night_mode", "false")
        self.ok("set", path, "admin_user", "管理者")
        self.ok("set", path, "snapshot_interval_seconds", "2.5")
        after = self.read(path)
        # 7=camera_ip 11=rtsp_port 18=admin_user 28=night_mode 40=snapshot_interval_seconds
        self.assert_only_lines_changed(before, after, {7, 11, 18, 28, 40})
        comments_before = [l for l in before.split(b"\n") if l.startswith(b"#")]
        comments_after = [l for l in after.split(b"\n") if l.startswith(b"#")]
        self.assertEqual(comments_before, comments_after)
        it = self.items(path)
        self.assertEqual(it["rtsp_port"]["value"], 8554)
        self.assertIs(it["night_mode"]["value"], False)
        self.assertEqual(it["snapshot_interval_seconds"]["value"], 2.5)
        self.assertEqual(it["admin_user"]["value"], "管理者")

    def test_set_number_boolean_with_json(self):
        path = self.copy(COFFEE)
        self.ok("set", path, "cup_size_ml", "240", "--json")
        self.assertEqual(self.value(path, "cup_size_ml"), 240)
        self.ok("set", path, "water_temp_c", "90", "--json")  # 小数の項目に整数も書ける
        self.assertEqual(self.value(path, "water_temp_c"), 90)
        self.ok("set", path, "use_milk", "true", "--json")
        self.assertIs(self.value(path, "use_milk"), True)
        self.ok("set", path, "cup_size_ml", "２００")  # 全角の数字も数として受け取る
        self.assertEqual(self.value(path, "cup_size_ml"), 200)
        self.ok("set", path, "cup_size_ml", "-5")  # 「-」で始まる値も、値として受け取る
        self.assertEqual(self.value(path, "cup_size_ml"), -5)
        self.ok("set", path, "water_temp_c", "1e-05", "--json")
        self.assertEqual(self.value(path, "water_temp_c"), 1e-05)
        self.assertIn(b"water_temp_c: 1.0e-05\n", self.read(path))

    def test_set_flow_list(self):
        path = self.copy(COFFEE)
        before = self.read(path)
        self.ok("set", path, "favorite_menu", '["カフェラテ", "espresso", "moka \\"pot\\""]', "--json")
        self.assertEqual(self.value(path, "favorite_menu"), ["カフェラテ", "espresso", 'moka "pot"'])
        self.assert_only_lines_changed(before, self.read(path), {27})
        self.ok("set", path, "favorite_menu", "[]", "--json")
        self.assertEqual(self.value(path, "favorite_menu"), [])
        self.assertIn(b"favorite_menu: []\n", self.read(path))
        # --json がなくても、[ で始まればリストとして受け取る（YAML の書き方も可）
        self.ok("set", path, "favorite_menu", "[latte, 'mocha']")
        self.assertEqual(self.value(path, "favorite_menu"), ["latte", "mocha"])

    def test_set_block_list(self):
        path = self.copy(COFFEE)
        self.ok("set", path, "cleaning_days", '["tue", "fri", "sun"]', "--json")
        self.assertEqual(self.value(path, "cleaning_days"), ["tue", "fri", "sun"])
        text = self.read(path).decode("utf-8")
        self.assertIn('cleaning_days: ["tue", "fri", "sun"]\n', text)
        self.assertNotIn("  - mon", text)
        # ブロックの中のコメントは消さない。次の項目の説明にも混ぜない。
        self.assertIn("  # 木曜は午後に洗う\n", text)
        it = self.items(path)
        self.assertEqual(it["maintenance_password"]["comment"], "お手入れ用の画面のパスワード。")
        self.assertEqual(it["cleaning_days"]["comment"], "お手入れをする曜日（必須）。")
        self.assertEqual(it["cleaning_days"]["choices"], ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
        self.assertEqual([i["key"] for i in self.ok("show", path)["items"]][6:8],
                         ["cleaning_days", "maintenance_password"])

    def test_set_block_list_without_indent_and_col0_comment(self):
        path = self.copy(CAMERA)
        self.ok("set", path, "recording_dirs", '["/data/録画"]', "--json")
        self.assertEqual(self.value(path, "recording_dirs"), ["/data/録画"])
        text = self.read(path).decode("utf-8")
        self.assertIn('recording_dirs: ["/data/録画"]\n\n# 動きを見張る場所の名前。', text)
        # 先頭の列のコメントがブロックの中にあっても、次の項目の説明にならない
        p2 = self.write("c.yaml", "# 説明A\nitems:\n# 中のコメント\n  - a  # aの注\n# 説明B\nnext: 1\n")
        self.ok("set", p2, "items", '["x", "y"]', "--json")
        self.assertEqual(self.read(p2).decode("utf-8"),
                         '# 説明A\nitems: ["x", "y"]\n  # 中のコメント\n  # aの注\n# 説明B\nnext: 1\n')
        self.assertEqual(self.items(p2)["next"]["comment"], "説明B")

    def test_set_null_item_any_type(self):
        path = self.copy(COFFEE)
        self.ok("set", path, "last_service", "2026-10-01")
        self.assertEqual(self.value(path, "last_service"), "2026-10-01")
        path = self.copy(COFFEE, dest_name="b.yaml")
        self.ok("set", path, "last_service", '["a"]', "--json")
        self.assertEqual(self.value(path, "last_service"), ["a"])

    def test_set_null_item_follows_examples(self):
        # 値が空（null）の項目は、--json がなくても、例の種類に合わせて書く
        path = self.write("kettle.yaml", "# 湯わかし器\n\n# 湯の温度（必須）\n# 例：90\ntemperature:\n\n"
                                         "# 置き場所\n# 例：[\"台所\"]\nplaces: ~\n\n# 名前\nname: null\n")
        self.ok("set", path, "temperature", "85")
        self.assertEqual(self.value(path, "temperature"), 85)
        self.ok("set", path, "places", '["台所", "居間"]')
        self.assertEqual(self.value(path, "places"), ["台所", "居間"])
        self.ok("set", path, "name", "85")
        self.assertEqual(self.value(path, "name"), "85")  # 例がなければ文字
        self.assertEqual(self.read(path).decode("utf-8"),
                         "# 湯わかし器\n\n# 湯の温度（必須）\n# 例：90\ntemperature: 85\n\n"
                         "# 置き場所\n# 例：[\"台所\"]\nplaces: [\"台所\", \"居間\"]\n\n# 名前\nname: \"85\"\n")

    def test_set_same_value_does_not_write(self):
        path = self.copy(COFFEE)
        before = self.read(path)
        mtime = os.stat(path).st_mtime_ns
        data = self.ok("set", path, "bean_type", "blend")
        self.assertFalse(data["changed"])
        self.assertEqual(self.read(path), before)
        self.assertEqual(os.stat(path).st_mtime_ns, mtime)

    def test_set_with_stdin(self):
        path = self.copy(COFFEE)
        value = "it's \"quoted\" --json -x 'single'"
        self.ok("set", path, "machine_name", "--stdin", stdin=value + "\n")
        self.assertEqual(self.value(path, "machine_name"), value)
        self.ok("set", path, "favorite_menu", "--stdin", "--json", stdin='["a", "b"]\n')
        self.assertEqual(self.value(path, "favorite_menu"), ["a", "b"])

    def test_set_with_stdin_every_type(self):
        """値はいつも --stdin で渡す。どの種類の項目も書けること。"""
        path = self.copy(COFFEE)
        cases = [
            # (項目, 標準入力, --json, 読み直した値)
            ("machine_name", "/opt/x/env $HOME `x` \"q\" 'a' \\n\n", False,
             "/opt/x/env $HOME `x` \"q\" 'a' \\n"),
            ("machine_name", "  前後の空白  \n", False, "  前後の空白  "),
            ("machine_name", "/dev/ttyUSB0\r\n", False, "/dev/ttyUSB0"),
            ("machine_name", "1行目\n2行目\n", False, "1行目\n2行目"),
            ("cup_size_ml", "200\n", True, 200),
            ("cup_size_ml", "250\n", False, 250),
            ("water_temp_c", "93.5\n", True, 93.5),
            ("use_milk", "true\n", True, True),
            ("use_milk", "false\n", False, False),
            ("favorite_menu", '["run-app /opt/x/y", "dmesg | tail -n 20"]\n', True,
             ["run-app /opt/x/y", "dmesg | tail -n 20"]),
            ("favorite_menu", "[]\n", True, []),
            ("cleaning_days", '["tue"]\n', True, ["tue"]),
            ("last_service", "2026-11-04\n", False, "2026-11-04"),
            ("greeting", "", False, ""),
        ]
        for key, stdin, use_json, expected in cases:
            args = ["set", path, key, "--stdin"] + (["--json"] if use_json else [])
            data = self.ok(*args, stdin=stdin)
            self.assertEqual(data["key"], key)
            got = self.value(path, key)
            self.assertEqual(got, expected, key)
            self.assertEqual(type(got), type(expected), key)
        if HAVE_YAML:
            with open(path, encoding="utf-8") as f:
                self.assertEqual(yaml.safe_load(f)["machine_name"], "1行目\n2行目")

    def test_set_with_stdin_heredoc(self):
        """SKILL.md に書いた形（<<'VALUE_END'）のとおりに、bash で渡す。"""
        path = self.copy(COFFEE)
        script = ("{} {} set {} cup_size_ml --stdin --json <<'VALUE_END'\n300\nVALUE_END\n"
                  "{} {} set {} machine_name --stdin <<'VALUE_END'\n/opt/a b/$PATH\nVALUE_END\n"
                  "{} {} set {} greeting --stdin <<'VALUE_END'\nVALUE_END\n").format(
                      sys.executable, TOOL, path, sys.executable, TOOL, path, sys.executable, TOOL, path)
        proc = subprocess.run(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.value(path, "cup_size_ml"), 300)
        self.assertEqual(self.value(path, "machine_name"), "/opt/a b/$PATH")
        self.assertEqual(self.value(path, "greeting"), "")

    def test_set_with_stdin_type_errors(self):
        path = self.copy(COFFEE)
        data, _ = self.fail_("set", path, "cup_size_ml", "--stdin", stdin="600秒\n")
        self.assertIn("数", data["error"])
        data, _ = self.fail_("set", path, "use_milk", "--stdin", stdin="はい\n")
        self.assertIn("true か false", data["error"])
        data, _ = self.fail_("set", path, "favorite_menu", "--stdin", stdin="a、b\n")
        self.assertIn("リスト", data["error"])
        data, _ = self.fail_("set", path, "machine_name", "--stdin", "--json", stdin="[1]\n")
        self.assertIn("文字", data["error"])
        # 値を引数にも書いたときは、引数の数が合わない
        data, _ = self.fail_("set", path, "machine_name", "x", "--stdin", stdin="y\n")
        self.assertIn("引数の数", data["error"])

    def test_set_with_stdin_from_terminal_does_not_wait(self):
        """値を渡し忘れても、入力を待ったまま止まらない。"""
        import pty
        path = self.copy(COFFEE)
        before = self.read(path)
        master, slave = pty.openpty()
        try:
            proc = subprocess.run([sys.executable, TOOL, "set", path, "greeting", "--stdin"],
                                  stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(proc.returncode, 2)
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("VALUE_END", data["error"])
        self.assertEqual(self.read(path), before)

    def test_set_value_after_double_dash(self):
        path = self.copy(COFFEE)
        self.ok("set", path, "machine_name", "--", "--json")
        self.assertEqual(self.value(path, "machine_name"), "--json")

    def test_inline_comment_kept(self):
        path = self.write("i.yaml", "# 説明\nport: 21  # ふつうは 21\nname: 'a'   # 名前\n")
        self.ok("set", path, "port", "2121")
        self.ok("set", path, "name", "b")
        self.assertEqual(self.read(path).decode("utf-8"),
                         '# 説明\nport: 2121  # ふつうは 21\nname: "b"   # 名前\n')

    def test_crlf_bom_and_no_final_newline_kept(self):
        raw = "\ufeff# 見本\r\n\r\n# 名前\r\nname: \"\"\r\n# リスト\r\nlist:\r\n  - a\r\nlast: 1".encode("utf-8")
        path = self.write("crlf.yaml", raw, binary=True)
        self.ok("set", path, "name", "名前")
        self.ok("set", path, "list", '["b"]', "--json")
        self.ok("set", path, "last", "2")
        expected = "\ufeff# 見本\r\n\r\n# 名前\r\nname: \"名前\"\r\n# リスト\r\nlist: [\"b\"]\r\nlast: 2".encode("utf-8")
        self.assertEqual(self.read(path), expected)
        self.assertEqual(self.items(path)["name"]["comment"], "名前")

    def test_file_mode_kept(self):
        path = self.copy(COFFEE)
        os.chmod(path, 0o600)
        self.ok("set", path, "machine_name", "x")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_symlink_target_is_updated(self):
        real = self.copy(COFFEE)
        link = os.path.join(self.tmp, "link.yaml")
        os.symlink(real, link)
        self.ok("set", link, "machine_name", "via link")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(self.value(real, "machine_name"), "via link")


# ---------------------------------------------------------------------------
# 断る場合
# ---------------------------------------------------------------------------

class RefuseTest(ToolTestCase):
    def assert_unchanged(self, path, before):
        self.assertEqual(self.read(path), before)

    def test_unknown_key(self):
        path = self.copy(COFFEE)
        before = self.read(path)
        data, _ = self.fail_("set", path, "sugar", "2")
        self.assertIn("「sugar」という項目はありません", data["error"])
        self.assertIn("machine_name", data["keys"])
        self.assert_unchanged(path, before)

    def test_type_string_to_list_and_back(self):
        path = self.copy(COFFEE)
        before = self.read(path)
        data, _ = self.fail_("set", path, "machine_name", '["a"]', "--json")
        self.assertIn("文字の項目", data["error"])
        data, _ = self.fail_("set", path, "favorite_menu", "latte")
        self.assertIn("リストの項目", data["error"])
        data, _ = self.fail_("set", path, "favorite_menu", '"latte"', "--json")
        self.assertIn("リストの項目", data["error"])
        self.assert_unchanged(path, before)

    def test_type_number_and_boolean(self):
        path = self.copy(CAMERA)
        before = self.read(path)
        data, _ = self.fail_("set", path, "rtsp_port", "abc")
        self.assertIn("数の項目", data["error"])
        data, _ = self.fail_("set", path, "rtsp_port", '"554"', "--json")
        self.assertIn("数の項目", data["error"])
        data, _ = self.fail_("set", path, "night_mode", "はい")
        self.assertIn("true か false", data["error"])
        data, _ = self.fail_("set", path, "night_mode", "1", "--json")
        self.assertIn("true か false", data["error"])
        data, _ = self.fail_("set", path, "camera_ip", "1", "--json")
        self.assertIn("文字の項目", data["error"])
        data, _ = self.fail_("set", path, "camera_ip", "null", "--json")
        self.assertIn("null にはできません", data["error"])
        self.assert_unchanged(path, before)

    def test_nested_and_bad_json(self):
        path = self.copy(CAMERA)
        before = self.read(path)
        data, _ = self.fail_("set", path, "motion_zones", '[["a"]]', "--json")
        self.assertIn("入れ子", data["error"])
        data, _ = self.fail_("set", path, "motion_zones", '{"a": 1}', "--json")
        self.assertIn("{…}", data["error"])
        data, _ = self.fail_("set", path, "motion_zones", "[a, b", "--json")
        self.assertIn("JSON として読めません", data["error"])
        data, _ = self.fail_("set", path, "motion_zones", "[a, [b]]")
        self.assertIn("リストとして読めません", data["error"])
        data, _ = self.fail_("set", path, "rtsp_port", "NaN", "--json")
        self.assertIn("NaN", data["error"])
        self.assert_unchanged(path, before)

    def test_choices(self):
        path = self.copy(CAMERA)
        before = self.read(path)
        data, _ = self.fail_("set", path, "resolution", "8k")
        self.assertIn("720p / 1080p / 4k", data["error"])
        self.assertEqual(data["choices"], ["720p", "1080p", "4k"])
        self.assert_unchanged(path, before)
        self.ok("set", path, "resolution", "4k")
        self.assertEqual(self.value(path, "resolution"), "4k")
        coffee = self.copy(COFFEE)
        self.fail_("set", coffee, "cleaning_days", '["mon", "holiday"]', "--json")
        self.ok("set", coffee, "cleaning_days", '["sat"]', "--json")
        self.ok("set", coffee, "bean_type", "arabica")  # 「（…）」の注は値に含めない

    def test_choices_refuse_empty(self):
        """選べる値に空がない項目は、空にできない。選べる値を示して断る（S1-r3）。"""
        path = self.copy(CAMERA)
        before = self.read(path)
        data, _ = self.fail_("set", path, "resolution", "")
        self.assertIn("「resolution」は空にできません", data["error"])
        self.assertIn("720p / 1080p / 4k", data["error"])
        self.assertEqual(data["choices"], ["720p", "1080p", "4k"])
        code, data, _ = self.tool("set", path, "resolution", "--stdin", stdin="")
        self.assertEqual(code, 2)
        self.assertIn("空にできません", data["error"])
        self.assert_unchanged(path, before)
        coffee = self.copy(COFFEE)
        before = self.read(coffee)
        data, _ = self.fail_("set", coffee, "cleaning_days", "[]", "--json")
        self.assertIn("空にできません", data["error"])
        self.fail_("set", coffee, "cleaning_days", '[""]', "--json")
        self.assert_unchanged(coffee, before)
        # 選べる値に "" があれば、空にできる
        path = self.write("c.yaml", "# 見本\n\n# 色。空なら色なし。\n# 選べる値：\"\" / red / blue\ncolor: \"red\"\n")
        data, _ = self.fail_("set", path, "color", "green")
        self.assertIn('"" / red / blue', data["error"])
        self.assertTrue(self.ok("set", path, "color", "")["changed"])
        self.assertEqual(self.value(path, "color"), "")

    def test_secret_masked_in_set_and_errors(self):
        path = self.copy(CAMERA)
        secret = "new-Secret-9999"
        code, data, out = self.tool("set", path, "api_token", secret)
        self.assertEqual(code, 0)
        self.assertEqual(data["value"], MASK)
        self.assertEqual(data["previous"], MASK)
        self.assertNotIn(secret, out)
        self.assertNotIn("tk-0123456789abcdef", out)
        code, data, out = self.tool("set", path, "api_token", "[" + secret, "--json")
        self.assertEqual(code, 2)
        self.assertNotIn(secret, out)
        code, data, out = self.tool("get", path, "api_token")
        self.assertNotIn(secret, out)
        # ファイルには書かれている
        self.assertIn(secret.encode("utf-8"), self.read(path))
        # 空のパスワードは、空と分かるように返す
        coffee = self.copy(COFFEE)
        self.assertEqual(self.value(coffee, "maintenance_password"), "")
        code, data, out = self.tool("set", coffee, "maintenance_password", "--stdin", stdin="p@ss: w#rd\n")
        self.assertEqual(code, 0)
        self.assertNotIn("p@ss", out)
        self.assertEqual(self.value(coffee, "maintenance_password"), MASK)

    def test_empty_secret_is_not_masked_in_errors(self):
        path = self.write("pin.yaml", "# 見本\n\n# 暗証の番号\n# 例：1234\nsecret_pin: 0\n"
                                      "\n# 空の暗証\napi_token: \"\"\n")
        data, out = self.fail_("set", path, "secret_pin", "abc")
        self.assertIn(MASK, data["error"])  # 空でない値は伏せる
        data, out = self.fail_("set", path, "api_token", "", "--json")
        self.assertNotIn(MASK, data["error"])
        self.assertIn("（空）", data["error"])

    def test_secret_not_shown_in_parse_errors(self):
        cases = [
            "# 見本\n\n# パスワード\n  password: hunter2-secret\n",          # 字下げした行
            "# 見本\n\n# パスワード\npassword: 'hunter2'-secret'\n",         # 引用符の後ろの余分な文字
            "# 見本\n\n# パスワード\npassword: \"hunter2-secret\" x\n",
            "# 見本\n\n# トークン\napi_token:\n  - hunter2-secret: x\n",     # リストの中
            "# 見本\n\n# トークン\napi_token: \"hunter2-secret\n",          # 閉じていない
        ]
        for text in cases:
            path = self.write("bad.yaml", text)
            data, out = self.fail_("show", path)
            self.assertNotIn("hunter2", out, text)
            self.assertIn("行目", data["error"], text)
            folder = os.path.join(self.tmp, "listed")
            os.makedirs(folder, exist_ok=True)
            shutil.copyfile(path, os.path.join(folder, "bad.yaml"))
            code, data, out = self.tool("list", "--dir", folder)
            self.assertEqual(code, 0)
            self.assertNotIn("hunter2", out, text)
            code, data, out = self.tool("list")  # 作業フォルダを探すときも
            self.assertEqual(code, 0)
            self.assertNotIn("hunter2", out, text)

    def test_missing_file_and_folder(self):
        data, _ = self.fail_("show", os.path.join(self.tmp, "none.yaml"))
        self.assertIn("ファイルがありません", data["error"])
        data, _ = self.fail_("show", self.tmp)
        self.assertIn("フォルダ", data["error"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "none.yaml")))

    def test_unreadable_yaml_reports_line(self):
        cases = [
            ("a: 1\nb:\n  c: 2\n", "3 行目"),
            ("a: 1\n  - x\n", "2 行目"),
            ("- x\n", "1 行目"),
            ("a: 1\na: 2\n", "2回書かれています"),
            ('a: "abc\n', "閉じていません"),
            ("a: [1, 2\n", "閉じていません"),
            ("a: b: c\n", "「: 」"),
            ("a: |\n  text\n", "複数行"),
            ("a: &x 1\n", "「&」で始まって"),
            ("a: {b: 1}\n", "入れ子"),
            ("just text\n", "1 行目"),
            ("a: [[1]]\n", "入れ子"),
            ("a:\n  - x\n    - y\n", "字下げ"),
            ("a: 1\nb\n", "2 行目"),
        ]
        for text, word in cases:
            path = self.write("bad.yaml", text)
            data, _ = self.fail_("show", path)
            self.assertIn(word, data["error"], text)
            data, _ = self.fail_("set", path, "a", "x")
            self.assertEqual(self.read(path), text.encode("utf-8"))

    def test_not_utf8(self):
        path = self.write("sjis.yaml", "# 説明\nname: 名前\n".encode("cp932"), binary=True)
        data, _ = self.fail_("show", path)
        self.assertIn("UTF-8", data["error"])

    def test_bad_arguments_still_json(self):
        path = self.copy(COFFEE)
        for args in (("frobnicate",), ("show",), ("get", path), ("set", path, "machine_name"),
                     ("list", "extra"), ("list", "--dir")):
            data, out = self.fail_(*args)
            json.loads(out)
        data = self.ok()
        self.assertIn("usage", data)
        data = self.ok("help")
        self.assertTrue(data["usage"])


# ---------------------------------------------------------------------------
# 読み取りの細かい点と、ふつうの YAML との食い違いがないこと
# ---------------------------------------------------------------------------

SCALARS = """# 見本
s1: "a\\tb\\u00e9\\x41\\\\"
s2: 'it''s'
s3: plain text with#hash # コメント
s4: plain  # コメント
s5: ~
s6: Null
s7: TRUE
s8: -12
s9: +3.5e+2
s10: 0755
s11: http://example.com:8080/a
s12: ""
s13:
s14: [1, 2.5, true, null, "x, y", 'z']
s15:
  -
  - "q"
s16: [ ]
s17: .5
"""


class ParseTest(ToolTestCase):
    def test_scalars(self):
        path = self.write("s.yaml", SCALARS)
        it = {i["key"]: i["value"] for i in self.ok("show", path)["items"]}
        self.assertEqual(it["s1"], "a\tbéA\\")
        self.assertEqual(it["s2"], "it's")
        self.assertEqual(it["s3"], "plain text with#hash")  # 「 #」からはコメント
        self.assertEqual(it["s4"], "plain")
        self.assertIsNone(it["s5"])
        self.assertIsNone(it["s6"])
        self.assertIs(it["s7"], True)
        self.assertEqual(it["s8"], -12)
        self.assertEqual(it["s9"], 350.0)
        self.assertEqual(it["s10"], "0755")  # 0 で始まる数は文字として扱う
        self.assertEqual(it["s11"], "http://example.com:8080/a")
        self.assertEqual(it["s12"], "")
        self.assertIsNone(it["s13"])
        self.assertEqual(it["s14"], [1, 2.5, True, None, "x, y", "z"])
        self.assertEqual(it["s15"], [None, "q"])
        self.assertEqual(it["s16"], [])
        self.assertEqual(it["s17"], 0.5)

    def test_japanese_keys(self):
        path = self.write("jp.yaml", "# 日本語の項目名\n\n# 部屋の名前（必須）\n部屋: \"\"\n\n# 入室のパスワード\n入室パスワード: \"abc\"\n")
        it = self.items(path)
        self.assertTrue(it["部屋"]["required"])
        self.assertTrue(it["入室パスワード"]["secret"])
        self.assertEqual(it["入室パスワード"]["value"], MASK)
        self.ok("set", path, "部屋", "会議室Ａ")
        self.assertEqual(self.value(path, "部屋"), "会議室Ａ")

    @unittest.skipUnless(HAVE_YAML, "PyYAML がない")
    def test_scalars_agree_with_pyyaml(self):
        import yaml
        path = self.write("s.yaml", SCALARS)
        ours = {i["key"]: i["value"] for i in self.ok("show", path)["items"]}
        theirs = yaml.safe_load(SCALARS)
        for key, value in ours.items():
            if key == "s10":
                continue  # 0755 は、PyYAML では 8 進数。この道具では文字（書き戻すときは "0755"）
            self.assertEqual(theirs[key], value, key)
            self.assertEqual(type(theirs[key]), type(value), key)

    @unittest.skipUnless(HAVE_YAML, "PyYAML がない")
    def test_fixtures_agree_with_pyyaml(self):
        import yaml
        for name in (COFFEE, CAMERA):
            path = self.copy(name)
            ours = {i["key"]: i["value"] for i in self.ok("show", path)["items"] if not i["secret"]}
            with open(path, encoding="utf-8") as f:
                theirs = yaml.safe_load(f)
            for k, v in ours.items():
                self.assertEqual(theirs[k], v, k)

    @unittest.skipUnless(HAVE_YAML, "PyYAML がない")
    def test_written_files_are_valid_yaml(self):
        import yaml
        path = self.copy(COFFEE)
        values = [
            ("machine_name", "「試作」: #1 \"x\" \\ \t 改行\nも 😀 \u2028 \x7f", False),
            ("cup_size_ml", "123456789012", False),
            ("water_temp_c", "1e+20", True),
            ("water_temp_c", "-0.001", True),
            ("favorite_menu", '["a: b", "# c", "", "x\\"y", 1, true, null]', True),
            ("cleaning_days", '["mon"]', True),
            ("last_service", '"null"', True),
            ("greeting", "yes", False),
            ("bean_type", "robusta", False),
        ]
        for key, raw, as_json in values:
            args = ["set", path, key, raw] + (["--json"] if as_json else [])
            self.ok(*args)
            with open(path, encoding="utf-8") as f:
                theirs = yaml.safe_load(f)
            self.assertEqual(theirs[key], self.value(path, key), key)
        with open(path, encoding="utf-8") as f:
            theirs = yaml.safe_load(f)
        self.assertEqual(theirs["greeting"], "yes")  # 文字の yes は true にならない
        self.assertEqual(theirs["last_service"], "null")
        self.assertEqual(theirs["water_temp_c"], -0.001)


if __name__ == "__main__":
    unittest.main()
