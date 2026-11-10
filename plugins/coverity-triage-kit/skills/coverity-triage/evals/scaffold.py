"""Prepare the working folder of an eval case, so that the case runs the skill as a user's request does.

    python scaffold.py <case folder> <working folder>

The working folder becomes a git repository with the files of the case, a settings file and a result
folder that already holds what the two Coverity tools would have fetched: the list with the one warning of
the case (warning.json) and its warning path. The request in prompt.md then needs no Coverity server.
Prints the result folder; the graders read cid/<CID>/result.json and cid/<CID>/fix.patch there.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

case, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
shutil.copytree(case / "files", work, dirs_exist_ok=True)
settings = work / ".coverity-triage" / "config.yaml"
settings.parent.mkdir(exist_ok=True)
settings.write_text("coverity:\n  url: https://coverity.example.co.jp:8443\n  stream: eval\n"
                    f"output_dir: ../{work.name}-out\n", encoding="utf-8")
for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "eval"]):
    subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", *args], cwd=work, check=True)
triage = Path(__file__).resolve().parents[1] / "scripts" / "triage.py"
start = subprocess.run(["uv", "run", "--native-tls", "--quiet", str(triage), "start", "--repo", str(work)],
                       check=True, capture_output=True)
reply = json.loads(start.stdout.decode("utf-8"))
warning = json.loads((case / "warning.json").read_text(encoding="utf-8"))
issue = {key: warning[key] for key in ("cid", "checker", "file", "line", "function", "impact")}
detail = Path(reply["run_dir"]) / "cid" / str(issue["cid"]) / "issue.json"
detail.parent.mkdir(parents=True)
for path, data in ((Path(reply["fetch"][0]["arguments"]["output_file"]), {"stream": "eval", "issues": [issue]}),
                   (detail, {key: warning[key] for key in ("events", "checker_description")})):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(reply["run_dir"])
