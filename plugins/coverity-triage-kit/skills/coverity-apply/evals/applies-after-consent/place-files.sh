#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
here="$PWD"
cd repo
git init -q -b main
git add -A
git -c user.name=eval -c user.email=eval@example.com commit -q -m "first"
git init -q --bare -b main "$here/origin.git"
git remote add origin "$here/origin.git"
git push -q origin main
cd "$here"
run_json="coverity-triage-out/20261108-101500/run.json"
sed "s#__REPO__#$here/repo#" "$run_json" > "$run_json.tmp"
mv "$run_json.tmp" "$run_json"
