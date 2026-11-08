#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
git init -q -b main
git add -A
git -c user.name=eval -c user.email=eval@example.com commit -q -m "first"
