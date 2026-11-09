"""Helpers for this skill's tests (only files of this skill folder are used)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")


def load_script(name: str):
    """Import scripts/<name>.py by path without writing bytecode."""
    sys.dont_write_bytecode = True
    path = SKILL / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_skill_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def run_script(name: str, *args: str, stdin: str | bytes | None = None, env: dict | None = None,
               cwd: str | Path | None = None) -> tuple[int, bytes, bytes]:
    """Run scripts/<name>.py the documented way (uv run --native-tls --quiet)."""
    full_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    full_env.update(env or {})
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    command = [UV or "uv", "run", "--native-tls", "--quiet", str(SKILL / "scripts" / f"{name}.py"), *args]
    proc = subprocess.run(command, input=data, capture_output=True, env=full_env,
                          cwd=str(cwd or tempfile.gettempdir()), timeout=300)
    return proc.returncode, proc.stdout, proc.stderr


def run_json(name: str, *args: str, **kwargs) -> tuple[int, dict]:
    code, out, err = run_script(name, *args, **kwargs)
    text = out.decode("utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(f"expected one JSON line, got {text!r} (stderr {err!r})")
    return code, json.loads(lines[0])


def frontmatter(text: str) -> tuple[dict, str]:
    """Tiny parser for the flat SKILL.md frontmatter (key: value lines)."""
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        raise AssertionError("SKILL.md has no frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, match.group(2)


def forbidden_patterns(own: str = "") -> list[tuple[str, re.Pattern]]:
    """Words a self-contained skill folder must not contain. Names of other parts are checked by the
    plugin-level independence tests, so this folder does not need to know them."""
    patterns = []
    words = ["Web管理" + "画面", "エクステ" + "ンション", "exten" + "sion", "orches" + "trator", "統括エー" + "ジェント",
             "cove" + "rity_", ".." + "/", "${PLUGIN" + "_ROOT}", "PLUGIN" + "_ROOT", "plugin" + ".json",
             "com.github" + ".copilot"]
    patterns += [(w, re.compile(re.escape(w), re.I)) for w in words]
    return patterns


def skill_text_files() -> list[Path]:
    skip = {"__pycache__", ".venv"}
    return [p for p in SKILL.rglob("*") if p.is_file() and not (set(p.parts) & skip)
            and p.suffix in {".md", ".py", ".json", ".yaml", ".yml", ".sh", ".txt", ".c", ".h", ".patch", ""}]


def clean_bytecode() -> None:
    shutil.rmtree(Path(__file__).resolve().parent / "__pycache__", ignore_errors=True)
