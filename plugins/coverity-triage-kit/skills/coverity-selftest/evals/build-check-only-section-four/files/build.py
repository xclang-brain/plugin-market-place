"""Trial build of the sample: checks the source exists and writes out.txt."""
from pathlib import Path

source = Path("src/main.c")
if not source.is_file():
    raise SystemExit("src/main.c がありません")
Path("out.txt").write_text("build ok\n", encoding="utf-8")
print("build ok")
