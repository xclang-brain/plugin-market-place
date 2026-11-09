#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
git init -q repo 2>/dev/null || mkdir -p repo/.git
printf 'output_dir: %s\n' "$PWD/out" >> repo/.coverity-triage/config.yaml
