"""The confirmation token is a shared rule: a tool that writes the plan file computes the same value
from the same bytes. Every case of ``tests/contract/token/cases.json`` (a copy of the shared test
data, when it is there) must give the expected token through coverity_preview_triage.

Case format: ``{"name", "input": {"plan_text": "<exact file text>"} | {"entries": [...]},
"expect": {"token": "<12 hex>"}}``. The copy is placed here by the maintainers of the shared data.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import HERE, MISSING, only_with_packages

load_tests = only_with_packages

CASES = HERE / "contract" / "token" / "cases.json"


@unittest.skipUnless(CASES.is_file(), f"the shared test data is not here yet: {CASES}")
class TokenContractTest(unittest.TestCase):

    def test_every_case(self) -> None:
        from coverity_mcp import tools
        from coverity_mcp.models import PlanEntry

        data = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertEqual(data.get("contract"), "token")
        self.assertTrue(data.get("cases"))
        with tempfile.TemporaryDirectory(prefix="cov-mcp-token-") as tmp:
            for number, case in enumerate(data["cases"]):
                with self.subTest(case.get("name", number)):
                    given = case["input"]
                    if "plan_text" in given:
                        raw = given["plan_text"].encode("utf-8")
                        plan = Path(tmp) / f"plan-{number}.json"
                        plan.write_bytes(raw)
                        try:
                            token = tools.preview_triage(plan_file=str(plan))["confirmation_token"]
                        except tools.ToolFailure:  # a text that is not a plan: the rule is on the bytes alone
                            token = tools.plan_token(raw)
                    else:
                        # The rule reads only the known keys of each entry. The tool also refuses other keys
                        # and an empty list (a misspelt key would be lost); when it takes the entries, its
                        # token must be the same.
                        entries = given["entries"]
                        token = tools.entries_token([PlanEntry.model_validate(e) for e in entries])
                        try:
                            from_tool = tools.preview_triage(entries=entries)["confirmation_token"]
                        except tools.ToolFailure:
                            from_tool = token
                        self.assertEqual(from_tool, token)
                    self.assertEqual(token, case["expect"]["token"])


if __name__ == "__main__" and not MISSING:
    unittest.main()
