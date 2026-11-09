"""apply-code は反映の計画（apply-plan.json）を1回だけ読み、確認の文字列を確かめたバイト列をそのまま使う。

確かめた後で計画が書き換わっても（preview をもう一度動かした場合など）、確認していない計画では反映しない。
Windows で preview が計画を置き換えている間に読むと PermissionError になることがあるので、少し待って読み直す。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from apply_testlib import SKILL, Fixture, TempDirCase, needs_git

try:
    import yaml  # noqa: F401  (the script needs it; it is a PEP 723 dependency)
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def load_apply():
    spec = importlib.util.spec_from_file_location("apply_under_test_plan_read", SKILL / "scripts" / "apply.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TWO_REJECTS = [
    {"id": "G1", "cids": [20005, 20006], "recommendation": "deviation", "decision": "reject"},
    {"id": "G2", "cids": [20007, 20008], "recommendation": "deviation", "decision": "reject"},
]


@unittest.skipUnless(HAVE_YAML, "pyyaml がないので、スクリプトを読み込む試験は飛ばします")
@needs_git
class PlanReadOnce(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.apply = load_apply()
        self.fx = Fixture(self.tmp)
        self.run_dir = self.fx.make_run(TWO_REJECTS)
        self.token = self.apply.cmd_preview(argparse.Namespace(run=str(self.run_dir)))["confirmation_token"]
        self.plan_path = self.run_dir / "apply-plan.json"

    def test_a_plan_rewritten_after_the_check_is_not_used(self) -> None:
        confirmed = json.loads(self.plan_path.read_text(encoding="utf-8"))
        swapped = dict(confirmed, triage=[e for e in confirmed["triage"] if e["id"] == "G2"])
        swapped_bytes = (json.dumps(swapped, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        original_read = Path.read_bytes
        reads = []

        def read_then_swap(path: Path) -> bytes:
            data = original_read(path)
            if path == self.plan_path:
                reads.append(data)
                path.write_bytes(swapped_bytes)  # another preview lands right after this read
            return data

        with mock.patch.object(Path, "read_bytes", read_then_swap):
            result = self.apply.cmd_apply_code(argparse.Namespace(run=str(self.run_dir), token=self.token))
        self.assertEqual(len(reads), 1, "計画を2回読んだ")
        self.assertEqual(sorted(result["rejected"]), ["G1", "G2"], "確認していない計画で反映した")

    def test_waits_while_the_plan_is_being_replaced_on_windows(self) -> None:
        original_read = Path.read_bytes
        failures = []

        def busy_once(path: Path) -> bytes:
            if path == self.plan_path and not failures:
                failures.append(path)
                raise PermissionError(13, "別のプロセスが使用中です", str(path))
            return original_read(path)

        with mock.patch.object(Path, "read_bytes", busy_once), mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(self.apply.time, "sleep"):
            result = self.apply.cmd_apply_code(argparse.Namespace(run=str(self.run_dir), token=self.token))
        self.assertEqual(len(failures), 1)
        self.assertTrue(result["ok"])

    def test_a_missing_plan_is_the_token_error(self) -> None:
        self.plan_path.unlink()
        with self.assertRaises(self.apply.ApplyError) as caught:
            self.apply.cmd_apply_code(argparse.Namespace(run=str(self.run_dir), token=self.token))
        self.assertIn("確認の文字列", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
