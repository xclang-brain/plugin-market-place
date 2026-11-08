# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0", "jsonschema>=4.18,<5"]
# ///
"""Run every case of assets/contract/<name>/cases.json through the code the run uses (scripts/runlib/rules.py,
which loads the copied scripts), and print one JSON object: {"<name>": [{"name", "expect", "got"}], ...}.

The tests start this with uv (the same libraries as run.py), so they need no library in their own Python.
Nothing is written except the temporary files of the lint cases, in a temporary folder.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL / "scripts"))

from runlib import rules  # noqa: E402

CONTRACTS = SKILL / "assets" / "contract"


def cases(name: str) -> list[dict]:
    path = CONTRACTS / name / "cases.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract") != name or data.get("version") != 1:
        raise SystemExit(f"{path}: contract / version が違います")
    return data["cases"]


def comment(case: dict) -> dict:
    text = case["input"]["comment"]
    got = {"ok": rules.script("check_comment").check_comment(text)["ok"]}
    # the copies inside the result and plan rules (what tick really runs) must answer the same
    got["inside_result"] = rules.script("check_result").check_comment(text)["ok"]
    got["inside_plan"] = rules.script("check_plan").check_comment(text)["ok"]
    return got


def result(case: dict) -> dict:
    given = case["input"]
    return {"ok": rules.result_check(given["result"], given.get("item"), given.get("cids"))["ok"]}


def lint(case: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="lint-") as tmp:
        root = Path(tmp)
        for rel, text in (case["input"].get("files") or {}).items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        return {"points": rules.lint_points(rules.lint_findings(case["input"]["result"], root))}


def plan(case: dict) -> dict:
    return {"ok": rules.plan_check(case["input"]["plan"], case["input"].get("items"))["ok"]}


def confidence(case: dict) -> dict:
    given = case["input"]
    return {"confidence": rules.effective_confidence(given["result"], given.get("review"), given.get("verify"),
                                                     given.get("run_item"))["confidence"]}


RUNNERS = {"comment": comment, "result": result, "lint": lint, "plan": plan, "confidence": confidence}


def main() -> int:
    names = sys.argv[1:] or [n for n in RUNNERS if (CONTRACTS / n / "cases.json").is_file()]
    out = {}
    for name in names:
        out[name] = [{"name": c["name"], "expect": c["expect"], "got": RUNNERS[name](c)} for c in cases(name)]
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
