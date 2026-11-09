"""Mock cov-commit-defects (specification 4.4, 4.5).

``cov-commit-defects --dir <idir> --url <URL> --stream <S> [--user U] [--password P | --auth-key-file F]
[--version V] [--target T] [--description D] [--snapshot-id-file F] [--strip-path P] [--ticker-mode M]
[--on-new-cert trust|distrust] [--certs F]``

The user name comes from --user, the URL, COV_USER, USER, the OS user; the password from --password,
the URL, COVERITY_PASSPHRASE, the file named by COVERITY_PASSPHRASE_FILE (official order). An
authentication key file replaces both (it must not be readable by others).

The warnings go to the emulator (``POST <url>/__emulator__/commit``, HTTP Basic). The emulator makes
a new snapshot of the stream, keeps the CID of a known mergeKey, gives new mergeKeys new CIDs, and the
CIDs missing now become Fixed. Exit code 0; COV_EMU_COMMIT_RESULT=3 commits and exits 3 (non-fatal
errors), =2 fails without committing.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import ssl
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .common import ISSUES_FILE, OUTPUT, EMIT, TUS_FILE, banner, read_json, strip_path, ticker, utc_stamp

VALUE = {"--dir", "--url", "--stream", "--user", "--password", "--auth-key-file", "--version", "--target",
         "--description", "--snapshot-id-file", "--strip-path", "--ticker-mode", "--on-new-cert", "--certs", "--host",
         "--port", "--https-port", "--dataport", "--tmpdir", "--security-file", "--cid-assignment-timeout",
         "--comparison-snapshot-id", "--preview-report", "--preview-report-v2", "--preview-report-v3", "--encryption"}
FLAGS = {"--ssl", "--no-banner", "--noxrefs", "--debug", "--verbose"}


def _error_log(idir: Path, text: str) -> None:
    out = idir / OUTPUT
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "commit-error-log.txt", "a", encoding="utf-8") as f:
        f.write(f"{utc_stamp()} {text}\n")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts: dict[str, list[str]] = {}
    flags: set[str] = set()
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, value = a.partition("=")
        if name in VALUE:
            if not eq:
                if i + 1 >= len(argv):
                    sys.stdout.write(f"[ERROR] Option {a} needs a value\n")
                    return 2
                value = argv[i + 1]
                i += 1
            opts.setdefault(name, []).append(value)
        elif name in FLAGS:
            flags.add(name)
        else:
            sys.stdout.write(f"[ERROR] Unrecognized option: {a}\n")
            return 2
        i += 1
    one = lambda k: (opts.get(k) or [None])[-1]  # noqa: E731
    started = time.time()
    if "--no-banner" not in flags:
        sys.stdout.write(banner("Defect Commit Client"))
        sys.stdout.write("\n")
    if not one("--dir") or not one("--stream"):
        sys.stdout.write("[ERROR] --dir and --stream are required\n")
        return 2
    idir = Path(one("--dir"))
    tus = (read_json(idir / EMIT / TUS_FILE, {}) or {}).get("tus") or []
    data = read_json(idir / OUTPUT / ISSUES_FILE)
    if not tus:
        sys.stdout.write("Error: intermediate directory contains no translation units.\n")
    if data is None:
        sys.stdout.write("[ERROR] Analysis results are missing.\n        Please read the documentation to determine the "
                         "appropriate\n        ordering in which to run the Coverity Prevent commands.\n")
        _error_log(idir, "[ERROR] Analysis results are missing.")
        return 2
    analysis = read_json(idir / OUTPUT / "emulator-analysis.json", {}) or {}

    url = one("--url")
    if not url:
        host = one("--host") or "localhost"
        if "--ssl" in flags or one("--https-port"):
            url = f"https://{host}:{one('--https-port') or one('--port') or 443}"
        else:
            url = f"http://{host}:{one('--port') or 80}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        sys.stdout.write(f"[ERROR] Invalid URL: {url}\n")
        return 2
    user = one("--user") or parts.username or os.environ.get("COV_USER") or os.environ.get("USER")
    password = one("--password") or parts.password or os.environ.get("COVERITY_PASSPHRASE")
    if not password and os.environ.get("COVERITY_PASSPHRASE_FILE"):
        try:
            password = Path(os.environ["COVERITY_PASSPHRASE_FILE"]).read_text(encoding="utf-8").strip()
        except OSError:
            password = None
    key_file = one("--auth-key-file")
    if key_file:
        try:
            mode = os.stat(key_file).st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                sys.stdout.write(f"[ERROR] The authentication key file {key_file} is readable by other users. "
                                 "Restrict its permissions so that only you can read it.\n")
                return 2
            key = json.loads(Path(key_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            sys.stdout.write(f"[ERROR] Cannot read the authentication key file {key_file}: {exc}\n")
            return 2
        if one("--user") and one("--user") != key.get("username"):
            sys.stdout.write(f"[WARNING] --user {one('--user')} is ignored; the authentication key belongs to "
                             f"{key.get('username')}.\n")
        user, password = key.get("username"), key.get("key")
    if not user:
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001
            user = "admin"
    if not password:
        sys.stdout.write("[ERROR] No password or authentication key was given (use --password, the environment "
                         "variable COVERITY_PASSPHRASE or --auth-key-file).\n")
        return 2
    base = urlunsplit((parts.scheme, parts.hostname + (f":{parts.port}" if parts.port else ""), parts.path.rstrip("/"),
                       "", ""))
    context = None
    if parts.scheme == "https":
        if one("--on-new-cert") == "trust":
            context = ssl._create_unverified_context()
        else:
            context = ssl.create_default_context(cafile=one("--certs"))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    sys.stdout.write(f"Connecting to server {parts.hostname}:{port}\n")
    if parts.scheme == "https":
        sys.stdout.write("Connection is encrypted.\n")
    sys.stdout.flush()

    strip = opts.get("--strip-path") or []
    issues = []
    for issue in data.get("issues", []):
        issue = dict(issue)
        if strip:
            issue["strippedMainEventFilePathname"] = strip_path(issue["strippedMainEventFilePathname"], strip)
        issues.append(issue)
    files = analysis.get("files") or {}
    payload = {"stream": one("--stream"), "snapshot": {
        "sourceVersion": one("--version"), "target": one("--target"), "description": one("--description"),
        "analysisVersion": analysis.get("analysisVersion"),
        "analysisInternalVersion": "e0e0e0e0e0 p-" + ".".join(str(analysis.get("analysisVersion", "2024.12")).split(".")[:2]) + "-push-1",
        "analysisCommandLine": analysis.get("analysisCommandLine"), "analysisHost": analysis.get("analysisHost"),
        "analysisIntermediateDir": analysis.get("analysisIntermediateDir"), "analysisTime": analysis.get("analysisTime"),
        "buildCommandLine": analysis.get("buildCommandLine"), "buildHost": analysis.get("buildHost"),
        "buildIntermediateDir": analysis.get("analysisIntermediateDir"), "buildTime": analysis.get("buildTime"),
        "buildSuccessCount": analysis.get("buildSuccessCount"), "buildFailureCount": analysis.get("buildFailureCount"),
        "enabledCheckers": analysis.get("enabledCheckers"),
        "files": {strip_path(k, analysis.get("stripPath") or []) if k else k: v for k, v in files.items()}},
        "issues": issues}
    tick = (one("--ticker-mode") or "spin")
    functions = len({i.get("functionDisplayName") for i in issues if i.get("functionDisplayName")})
    for what, n in (("file descriptions", len(files)), ("source files", len(files)),
                    ("cross-reference bundles", len(files)), ("functions", functions)):
        verb = "Calculating" if what == "cross-reference bundles" else "Committing"
        sys.stdout.write(f"{utc_stamp()} - {verb} {n} {what}...\n{ticker(tick)}")
    sys.stdout.write(f"{utc_stamp()} - Committing {len(issues)} defect files...\n{ticker(tick)}")
    sys.stdout.flush()
    result = os.environ.get("COV_EMU_COMMIT_RESULT", "0")
    if result == "2":
        sys.stdout.write("[ERROR] The commit failed (emulated failure COV_EMU_COMMIT_RESULT=2).\n")
        _error_log(idir, "[ERROR] The commit failed (emulated failure).")
        return 2
    request = urllib.request.Request(base + "/__emulator__/commit", data=json.dumps(payload).encode("utf-8"),
                                     method="POST", headers={
                                         "Content-Type": "application/json",
                                         "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()})
    try:
        with urllib.request.urlopen(request, timeout=120, context=context) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error", "")
        except ValueError:
            message = ""
        if exc.code == 401:
            text = f"[ERROR] Authentication failed for user {user}. Check the user name and the password or authentication key."
        elif exc.code == 404:
            text = f"[ERROR] {message or 'Stream not found.'}"
        else:
            text = f"[ERROR] The server refused the commit ({exc.code}): {message}"
        sys.stdout.write(text + "\n")
        _error_log(idir, text)
        return 2
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            text = (f"[ERROR] The certificate of the server {parts.hostname}:{port} is not trusted ({reason.verify_message}). "
                    "Use --on-new-cert trust or --certs <file>.")
        else:
            text = f"[ERROR] Unable to connect to server {parts.hostname}:{port}: {reason}"
        sys.stdout.write(text + "\n")
        _error_log(idir, text)
        return 2
    sys.stdout.write(f"{utc_stamp()} - Committing 3 output files...\n{ticker(tick)}")
    snap = answer["snapshotId"]
    sys.stdout.write(f"New snapshot ID {snap} added.\n")
    elapsed = int(time.time() - started)
    sys.stdout.write(f"Elapsed time: {elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}\n")
    if one("--snapshot-id-file"):
        Path(one("--snapshot-id-file")).write_text(str(snap), encoding="utf-8")
    if result == "3":
        text = "[WARNING] Some files could not be committed (emulated non-fatal error COV_EMU_COMMIT_RESULT=3)."
        sys.stdout.write(text + "\n")
        _error_log(idir, text)
        return 3
    return 0
