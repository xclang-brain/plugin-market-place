# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Run every case of assets/contract/confidence/cases.json through the screen's own path: a run folder
(run.json with the case's item, else with the review taken in as it is; items/<ID>/review.json; verify.json)
read by guilib.runs, and
print one JSON object: {"rule_file": "...", "cases": [{"name", "expect", "got", "direct"}]}.

The tests start this with uv (the same libraries as gui.py). Nothing is written outside a temporary folder.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL / "scripts"))

from guilib import runs  # noqa: E402

CASES = SKILL / "assets" / "contract" / "confidence" / "cases.json"
ITEM = "20001"


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    out = []
    with tempfile.TemporaryDirectory(prefix="gui-confidence-") as tmp:
        for n, case in enumerate(data["cases"]):
            given = case["input"]
            review, verify = given.get("review"), given.get("verify")
            folder = Path(tmp) / f"20261108-1000{n:02d}"
            if isinstance(given.get("run_item"), dict):
                taken = given["run_item"].get("review")
            else:
                taken = review.get("outcome") if isinstance(review, dict) else None
            write(folder / "run.json", {"format": 2, "items": [{"id": ITEM, "cids": [20001], "status": "done",
                                                                "review": taken}]})
            if review is not None:
                write(folder / "items" / ITEM / "review.json", review)
            verified = {ITEM: verify} if verify is not None else {}
            run = runs.Run(folder)
            confidence, reason = runs.effective_confidence(run, run.item(ITEM), given["result"], verified)
            direct = runs.CONFIDENCE.effective_confidence(given["result"], review, verify, given.get("run_item"))
            out.append({"name": case["name"], "expect": case["expect"],
                        "got": {"confidence": confidence, "reason": reason}, "direct": direct})
    answer = {"rule_file": str(Path(runs.CONFIDENCE.__file__).resolve()), "cases": out}
    sys.stdout.buffer.write((json.dumps(answer, ensure_ascii=False) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
