"""The command line: one JSON object, the output encoding rule, and the folder itself."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest

import helpers
from guilib import common, schema

# Words a part of this kind must not contain (built from pieces, so that this file does not contain them).
BANNED = [re.escape(".." + "/"), "PLUGIN" + "_ROOT", re.escape("plugin" + ".json"), re.escape("com.github" + ".copilot"),
          "coverity" + "_", "exten" + "sion", "orches" + "trator", "Web管理" + "画面", "エクステ" + "ンション",
          "統括" + "エージェント", re.escape("ct" + ".py")]


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = helpers.temp_dir(self)

    def test_output_follows_pythonioencoding_and_escapes_only_what_cannot_be_written(self) -> None:
        values = {"name": "日本語の名前😀"}
        environ = helpers.env(self.tmp, PYTHONIOENCODING="cp932")
        proc = subprocess.run([helpers.UV, "run", "--native-tls", "--quiet", str(helpers.GUI), "yaml-edit", "--stdin"],
                              input=json.dumps({"original": "", "values": values}).encode(), capture_output=True,
                              env=environ, timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = proc.stdout.decode("cp932")
        self.assertIn("日本語の名前", text)
        self.assertIn("\\ud83d\\ude00", text)
        self.assertEqual(json.loads(text)["text"], "name: 日本語の名前😀\n")

    def test_encode_json_helper(self) -> None:
        self.assertEqual(common.encode_json({"a": "é"}, "ascii"), b'{\n "a": "\\u00e9"\n}')
        self.assertEqual(json.loads(common.encode_json({"a": "表"}, "utf-8")), {"a": "表"})
        self.assertEqual(json.loads(common.encode_json({"a": "表"}, "no-such-codec")), {"a": "表"})

    def test_usage_errors_are_one_json_object(self) -> None:
        code, data = helpers.run_gui(["nothing"], helpers.env(self.tmp))
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertIn("引数が正しくありません", data["error"])

    def test_the_folder_names_nothing_outside_itself(self) -> None:
        found = []
        for path in helpers.SKILL.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix not in (".py", ".md", ".html", ".json", ".yaml", ".sh"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for word in BANNED:
                if re.search(word, text):
                    found.append(f"{path.relative_to(helpers.SKILL)}: {word}")
        self.assertEqual(found, [])

    def test_the_copy_of_the_result_schema_reads_the_example(self) -> None:
        loaded = schema.load(helpers.SKILL / "assets" / "result.schema.json")
        self.assertIsNotNone(loaded)
        self.assertEqual(schema.errors(helpers.result("20001"), loaded), [])
        bad = helpers.result("20001", confidence="sure")
        bad["deviation"]["comment"] = " " + "あ" * 201
        problems = schema.errors(bad, loaded)
        self.assertTrue(any("confidence" in p for p in problems))
        self.assertTrue(any("comment" in p for p in problems))

    def test_the_schema_checker(self) -> None:
        root = {"type": "object", "required": ["a"], "additionalProperties": False,
                "properties": {"a": {"$ref": "#/$defs/n"}, "b": {"type": ["string", "null"], "enum": ["x", None]},
                               "c": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                               "d": {"anyOf": [{"type": "string"}, {"type": "boolean"}]}},
                "$defs": {"n": {"type": "integer", "minimum": 1, "maximum": 3}}}
        self.assertEqual(schema.errors({"a": 2, "b": None, "c": [1], "d": True}, root), [])
        self.assertTrue(schema.errors({"a": 0}, root))
        self.assertTrue(schema.errors({"a": True}, root))
        self.assertTrue(schema.errors({"a": 1, "e": 1}, root))
        self.assertTrue(schema.errors({"a": 1, "b": "y"}, root))
        self.assertTrue(schema.errors({"a": 1, "c": []}, root))
        self.assertTrue(schema.errors({"a": 1, "c": ["1"]}, root))
        self.assertTrue(schema.errors({"a": 1, "d": 3}, root))
        self.assertTrue(schema.errors({}, root))

    def test_a_copy_of_the_folder_under_another_name_works(self) -> None:
        copy = self.tmp / "renamed-screen"
        shutil.copytree(helpers.SKILL, copy, ignore=shutil.ignore_patterns("__pycache__", "evals"))
        proc = subprocess.run([helpers.UV, "run", "--native-tls", "--quiet", str(copy / "scripts" / "gui.py"), "yaml-edit", "--stdin"],
                              input=b'{"original": "a: 1  # c\\n", "values": {"a": 2}}', capture_output=True,
                              env=helpers.env(self.tmp), timeout=120, check=False)
        self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["text"], "a: 2  # c\n")
        self.assertFalse(list(copy.rglob("__pycache__")))


if __name__ == "__main__":
    unittest.main()
