"""The confirmation token is a shared rule (contract "token"): ``src/coverity_mcp/plantoken.py`` is a byte
copy of the file of the part that writes the plan file, and the write-back tools use it as it is.

Every case of ``tests/contract/token/cases.json`` is run through the copied function. The tools add input
checks of their own at the tool boundary (unknown keys and an empty list are refused, so that a misspelt key
is not lost); whenever a tool takes the input of a case, its token must be the function's.

Case format: ``{"name", "input": {"plan_text": "<exact file text>"} | {"entries": [...]},
"expect": {"token": "<12 hex>"}}``.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from support import HERE, MISSING, SRC

CASES = HERE / "contract" / "token" / "cases.json"
RULE = SRC / "coverity_mcp" / "plantoken.py"


def load_rule():
    """The copied file on its own (standard library only), without the server's packages."""
    spec = importlib.util.spec_from_file_location("coverity_mcp_plantoken_under_test", RULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cases() -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    assert data.get("contract") == "token" and data.get("cases"), CASES
    return data["cases"]


class TokenRuleTest(unittest.TestCase):
    """The function itself: runs with or without the server's packages."""

    def test_every_case_through_the_copied_function(self) -> None:
        rule = load_rule()
        for number, case in enumerate(cases()):
            with self.subTest(case.get("name", number)):
                given = case["input"]
                if "plan_text" in given:
                    token = rule.token_for_bytes(given["plan_text"].encode("utf-8"))
                else:
                    token = rule.token_for_entries(given["entries"])
                self.assertEqual(token, case["expect"]["token"])

    def test_the_copy_reads_nothing_else(self) -> None:
        text = RULE.read_text(encoding="utf-8")
        imports = [line.split()[1] for line in text.splitlines() if line.startswith(("import ", "from "))]
        self.assertEqual(sorted(set(imports)), ["__future__", "hashlib", "json", "typing"])


@unittest.skipIf(MISSING, "the server's packages are not in this Python (test_uv_child.py runs this in uv)")
class TokenToolTest(unittest.TestCase):
    """The tools use the copied function: same token whenever they take the input."""

    def test_the_tools_give_the_function_s_token(self) -> None:
        from coverity_mcp import plantoken, tools

        taken = 0
        with tempfile.TemporaryDirectory(prefix="cov-mcp-token-") as tmp:
            for number, case in enumerate(cases()):
                with self.subTest(case.get("name", number)):
                    given = case["input"]
                    try:
                        if "plan_text" in given:
                            plan = Path(tmp) / f"plan-{number}.json"
                            plan.write_bytes(given["plan_text"].encode("utf-8"))
                            out = tools.preview_triage(plan_file=str(plan))
                        else:
                            out = tools.preview_triage(entries=given["entries"])
                    except tools.ToolFailure:
                        continue  # refused at the tool boundary (not a plan, unknown keys, empty list)
                    taken += 1
                    self.assertEqual(out["confirmation_token"], case["expect"]["token"])
        self.assertGreater(taken, 0)
        self.assertIs(tools.plantoken, plantoken)
        self.assertEqual(Path(plantoken.__file__).resolve(), RULE.resolve())

    def test_unknown_keys_and_empty_entries_are_refused_at_the_tool(self) -> None:
        from coverity_mcp import tools

        entry = {"id": "20001", "cids": [20001], "classification": "False Positive", "action": "Ignore"}
        self.assertEqual(tools.preview_triage(entries=[entry])["confirmation_token"],
                         load_rule().token_for_entries([entry]))
        for bad in ([], [dict(entry, comments="誤検知。")]):
            with self.subTest(entries=bad):
                with self.assertRaises(tools.ToolFailure):
                    tools.preview_triage(entries=bad)


if __name__ == "__main__":
    unittest.main()
