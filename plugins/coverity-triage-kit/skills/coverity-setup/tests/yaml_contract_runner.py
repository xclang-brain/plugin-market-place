# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Run the comment-keeping rewrite on test data (used by the tests through ``uv run``).

  yaml_contract_runner.py cases <cases.json>   -> {"results": [{"name", "text", "notice"}]}
  yaml_contract_runner.py random <rounds>      -> {"failures": [...]}  (random edits; comments must stay)
"""
from __future__ import annotations

import copy
import json
import random
import re
import sys

sys.dont_write_bytecode = True
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import yaml  # noqa: E402
import yamledit  # noqa: E402


def run_cases(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    results = []
    for case in data["cases"]:
        text, lost = yamledit.rewrite(case["input"]["original"], case["input"]["values"])
        results.append({"name": case["name"], "text": text, "notice": lost})
    return {"results": results}


def _norm(line):
    return re.sub(r"\s+", " ", line.strip()) if line is not None else None


def comments_moved(src: str, out: str) -> list[str]:
    """Another way to find comments separated from their line (a comment line: the next line with data)."""
    s, o = src.splitlines(), out.splitlines()
    on = [_norm(x) for x in o]
    bad = []
    for i, line in enumerate(s):
        if "#" not in line:
            continue
        if line.lstrip().startswith("#"):
            partner = next((_norm(x) for x in s[i + 1:] if x.strip() and not x.lstrip().startswith("#")), None)
            if partner is None or partner not in on:
                continue
            j = on.index(partner)
            above = [x for x in on[:j] if x]
            k = len(above) - 1
            block = []
            while k >= 0 and above[k].startswith("#"):
                block.append(above[k])
                k -= 1
            if _norm(line) not in block:
                bad.append(line)
        elif _norm(line) not in on:
            body = _norm(line[:line.index("#")])
            if body in [_norm(x.split("#")[0]) for x in o]:
                bad.append(line)
    return bad


LONG = 'call C:\\tools\\env.bat && make -f makefileXX CFLAGS="-O2 -g" all -j8 V=1 PLATFORM=stm32f4 BOARD=em4-rev3'


def base(rnd: random.Random) -> str:
    coverity = {"url": "https://cov.example.com:8443", "api": "auto", "user_env": "COV_USER", "key_env": "COV_AUTH_KEY",
                "triage_store": "Default Triage Store", "revision_field": "sourceVersion",
                "path_strip_prefixes": rnd.sample(["/a/", "/b/", "C:/c/", "/d/"], rnd.randint(0, 3))}
    if rnd.random() < .5:
        coverity["ca_file"] = "C:/certs/ca.pem"
    vcs = {"type": "git", "base_branch": "main", "branch_prefix": "coverity-fix/"} if rnd.random() < .7 else {"type": "svn"}
    data = {"coverity": coverity, "vcs": vcs, "output_dir": "out", "max_items": 100, "parallel": 4,
            "options": {"annotation": False, "markdown": False},
            "verify": {"default": "build", "build_command": LONG if rnd.random() < .5 else "make all"},
            "agents": {"integration": rnd.random() < .5, "review": False, "max_rework": 1}}
    return "# 見出し\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def comment(rnd: random.Random, text: str) -> str:
    out, n = [], 0
    lines = text.splitlines()

    def cont(x):  # the continuation line of a folded value
        return x.strip() and not x.lstrip().startswith(("-", "#")) and ":" not in x

    for i, line in enumerate(lines):
        if line.startswith("#") or cont(line):
            out.append(line)
            continue
        if rnd.random() < .3:
            n += 1
            out.append(" " * (len(line) - len(line.lstrip())) + f"# 上の注釈{n}")
            if rnd.random() < .2:
                out.append("")
        following = i + 1 < len(lines) and cont(lines[i + 1])
        if rnd.random() < .3 and not line.rstrip().endswith(":") and not following:
            n += 1
            line += f"   # 行末{n}"
        out.append(line)
    return "\n".join(out) + "\n"


def change(rnd: random.Random, d: dict) -> None:
    r = rnd.random
    if r() < .3:
        d["coverity"]["url"] = "https://cov2.example.com"
    if r() < .3:
        if d["coverity"].get("ca_file"):
            del d["coverity"]["ca_file"]
        else:
            d["coverity"]["ca_file"] = "D:/x.pem"
    if r() < .5:
        values = d["coverity"]["path_strip_prefixes"]
        kind = rnd.choice(["add", "insert", "del", "chg", "empty", "swap", "chg+add"])
        if kind == "add":
            values.append("/new/")
        elif kind == "insert":
            values.insert(0, "/first/")
        elif kind == "swap" and len(values) > 1:
            values.reverse()
        elif kind == "chg+add" and values:
            values[-1] = "/chg/"
            values.append("/new/")
        elif kind == "del" and values:
            values.pop(rnd.randrange(len(values)))
        elif kind == "chg" and values:
            values[rnd.randrange(len(values))] = "/chg/"
        elif kind == "empty":
            values.clear()
    if r() < .2 and d["vcs"]["type"] == "git":
        d["vcs"] = {"type": "svn"}
    if r() < .3:
        d["verify"]["build_command"] = LONG + " X=1"
    if r() < .3:
        d["agents"]["review"] = not d["agents"]["review"]
    if r() < .2:
        d["max_items"] = 7
    if r() < .2:
        d["options"]["metrics"] = True


def run_random(rounds: int) -> dict:
    rnd = random.Random(20261108)
    failures = []
    for round_ in range(rounds):
        text = comment(rnd, base(rnd))
        new = copy.deepcopy(yaml.safe_load(text))
        change(rnd, new)
        crlf = round_ % 5 == 0
        source = text.replace("\n", "\r\n") if crlf else text
        out, lost = yamledit.rewrite(source, new)
        plain = out.replace("\r\n", "\n") if crlf else out
        problems = []
        if lost:
            problems.append("注釈を残せなかった")
        if crlf and out.count("\r\n") != out.count("\n"):
            problems.append("CRLF が残らなかった")
        if yaml.safe_load(plain) != new:
            problems.append("値が違う")
        moved = comments_moved(text, plain)
        if moved:
            problems.append(f"注釈が動いた: {moved}")
        if problems:
            failures.append({"round": round_, "problems": problems, "text": text, "out": plain})
    return {"rounds": rounds, "failures": failures[:5], "failed": len(failures)}


def main() -> None:
    mode = sys.argv[1]
    result = run_cases(sys.argv[2]) if mode == "cases" else run_random(int(sys.argv[2]))
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=True) + "\n").encode("ascii"))


if __name__ == "__main__":
    main()
