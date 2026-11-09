#!/usr/bin/env bash
# Put the run folder in place and make its times agree with operations.log (local time of this machine):
# the epochs in run.json and the file times are those of 2026-11-01, so the work in progress (G1) has had
# no activity since 09:44.
set -eu
cp -R "$(dirname "$0")/files/." .
run=out/20261101-090000
epoch() { date -d "2026-11-01 $1" +%s; }
python3 - "$run/run.json" "$(epoch 09:01:40)" "$(epoch 09:01:41)" "$(epoch 09:11:50)" <<'PY'
import json, sys
path, t20001, t20002, tg1 = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
with open(path, encoding="utf-8") as f:
    state = json.load(f)
started = {"20001": t20001, "20002": t20002, "G1": tg1}
for item in state["items"]:
    item["started"] = started[item["id"]]
state["tasks"]["worker:G1"]["launched_at"] = tg1
with open(path, "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
touch -d "2026-11-01 09:00:40" "$run/issues.mapped.json"
touch -d "2026-11-01 09:08:30" "$run/items/20001/result.json"
touch -d "2026-11-01 09:44:00" "$run/items/G1/progress.json"
touch -d "2026-11-01 09:41:20" "$run/operations.log"
touch -d "2026-11-01 09:45:00" "$run/run.json"
