# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""The settings file of a repository for the triage of Coverity warnings: create it, check it.

    uv run --native-tls --quiet setup.py init  --repo <repo> --url <URL> --stream <stream>
    uv run --native-tls --quiet setup.py check --repo <repo>

init writes <repo>/.coverity-triage/config.yaml and never touches an existing one. Both commands check the
settings file, the credentials (set or not, never a value) and the build options, and print one JSON object.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

TEMPLATE = """\
coverity:
  # Coverity Connect の URL
  url: {url}
  # 対象のストリーム
  stream: {stream}

conditions:
  # 取得するチェッカー名（* と ? のワイルドカードを使用できます）。空のときは、すべてのチェッカー
  checkers: []
  # 取得する Impact（High / Medium / Low）。空のときは、すべて
  impacts: []
  # 取得する警告の分類（Classification）
  classification: [Unclassified]
  # 取得する件数の上限
  limit: 20

# 結果フォルダーの場所（リポジトリからの相対パス、または絶対パス。リポジトリの外を指定します）
output_dir: ../coverity-triage-out
# 3段階目で同時に動かすエージェントの数（1〜8）
parallel: 3
# git で修正を取り込む先のブランチ。空のときは、自動で判定します
base_branch: ""

build:
  # ビルドのコマンド（下の2つのオプションで使用します）
  command: ""
  # オプション: 修正を適用した状態でビルドして確認します
  check_fixes: false
  # オプション: 取得の前に、解析と Coverity への登録を行います
  initial_analysis: false
  # オプションの解析で cov-analyze に渡す引数
  analyze_args: "--all"
"""
# The person runs one of these in their own terminal: the key never passes through the chat.
CREDENTIALS = {"powershell": """\
[Environment]::SetEnvironmentVariable('COV_USER', '<ユーザー名>', 'User')
$k = Read-Host 'COV_AUTH_KEY（認証キー。入力は表示されません）' -AsSecureString
[Environment]::SetEnvironmentVariable('COV_AUTH_KEY', [Net.NetworkCredential]::new('', $k).Password, 'User')""",
               "bash": """\
read -r -p 'COV_USER: ' COV_USER
read -r -s -p 'COV_AUTH_KEY（認証キー。入力は表示されません）: ' COV_AUTH_KEY; echo
export COV_USER COV_AUTH_KEY      # このターミナルから copilot を起動します"""}
SECTIONS = ("coverity", "conditions", "build")


def strings(value, allowed: tuple = ()) -> bool:
    """A list of strings (those of ``allowed``); a scalar is a one-item list."""
    return all(isinstance(item, str) and (not allowed or item in allowed)
               for item in (value if isinstance(value, list) else [value]))


TEXT = ("文字列", lambda v: isinstance(v, str))
TEXTS = ("文字列のリスト", strings)
FLAG = ("true または false", lambda v: isinstance(v, bool))
# The 13 keys: what a value must be. A missing or empty value means the default and is not looked at.
KEYS = {
    "coverity.url": ("https:// または http:// で始まる URL", lambda v: str(v).lower().startswith(("https://", "http://"))),
    "coverity.stream": TEXT,
    "conditions.checkers": TEXTS,
    "conditions.impacts": ("High / Medium / Low のリスト", lambda v: strings(v, ("High", "Medium", "Low"))),
    "conditions.classification": TEXTS,
    "conditions.limit": ("1 以上の整数", lambda v: type(v) is int and v >= 1),
    "output_dir": TEXT,
    "parallel": ("1〜8 の整数", lambda v: type(v) is int and 1 <= v <= 8),
    "base_branch": TEXT,
    "build.command": ("1行の文字列", lambda v: isinstance(v, str) and "\n" not in v.strip()),
    "build.check_fixes": FLAG,
    "build.initial_analysis": FLAG,
    "build.analyze_args": TEXT,
}


class Problem(Exception):
    """Something the person can correct. Printed as {"ok": false, "error": ...}."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise Problem(f"引数が正しくありません（{message}）")


def is_set(name: str) -> bool:
    """An environment variable has a value. On Windows the value saved for the user (HKCU\\Environment)
    counts too: a terminal opened before it was saved does not see it."""
    value = os.environ.get(name, "").strip()
    if not value and sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, name)[0]).strip()
        except OSError:
            value = ""
    return bool(value)


def check(name: str, problem: str, fine: str) -> dict:
    return {"name": name, "ok": not problem, "message": problem or fine}


def check_settings(path: Path) -> tuple[dict, dict]:
    """The check 設定ファイル, and the values by key ("coverity.url": ...; {} when the file cannot be read)."""
    shown = path.as_posix()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, NotADirectoryError):
        return check("設定ファイル", f"設定ファイルがありません: {shown}。"
                     "/coverity-triage-kit:coverity-setup <URL> <ストリーム> を実行してください", ""), {}
    except (OSError, ValueError, yaml.YAMLError) as exc:  # ValueError: not UTF-8, or a date such as 2026-13-01
        return check("設定ファイル", f"設定ファイルを読み取れません: {shown}（{' '.join(str(exc).split())}）", ""), {}
    values, unknown = {}, []
    for key, value in (data if isinstance(data, dict) else {}).items():
        if key in SECTIONS and isinstance(value, dict):
            values.update({f"{key}.{name}": item for name, item in value.items()})
        elif "." in str(key):  # "coverity.url: ..." at the top is not the key url below coverity
            unknown.append(str(key))
        else:
            values[str(key)] = value
    given = {key: value for key, value in values.items() if value not in (None, "", [])}
    wrong = [f"{key} を記入してください" for key in ("coverity.url", "coverity.stream") if key not in given]
    wrong += [f"{key} の値が正しくありません" for key in SECTIONS if key in given]
    wrong += [f"{key}（{KEYS[key][0]}）の値が正しくありません" for key, value in given.items()
              if key in KEYS and not KEYS[key][1](value)]
    unknown += [key for key in values if key not in KEYS and key not in SECTIONS]
    unused = [f"使用しないキーがあります: {', '.join(unknown)}"] if unknown else []
    return check("設定ファイル", f"設定ファイルを訂正してください: {shown}。{'。'.join(wrong + unused)}" if wrong else "",
                 "".join(unused) or "設定ファイルを確認しました"), values


def check_build(values: dict) -> list[dict]:
    """The check ビルドのオプション, only when an option is on."""
    options = [key for key in ("build.check_fixes", "build.initial_analysis") if values.get(key) is True]
    wrong = []
    if options and not str(values.get("build.command") or "").strip():
        wrong.append(f"{'、'.join(options)} を使用するには、設定ファイルの build.command にビルドのコマンドを記入してください")
    missing = [tool for tool in ("cov-build", "cov-analyze", "cov-commit-defects")
               if "build.initial_analysis" in options and not shutil.which(tool)]
    if missing:
        wrong.append(f"build.initial_analysis で使用する {'、'.join(missing)} コマンドが見つかりません。"
                     "インストールして、PATH を設定してください")
    return [check("ビルドのオプション", "。".join(wrong), "ビルドのオプションを使用できます")] if options else []


def answer(args) -> dict:
    repo = Path(args.repo).resolve()
    kind = "git" if (repo / ".git").exists() else "svn" if (repo / ".svn").is_dir() else ""
    if not kind:
        raise Problem("git または svn のリポジトリの最上位フォルダーで実行してください")
    path = repo / ".coverity-triage" / "config.yaml"
    created, wanted = False, None
    if args.command == "init":
        wanted = (args.url.strip().rstrip("/"), args.stream.strip())
        if not KEYS["coverity.url"][1](wanted[0]) or not wanted[1]:
            raise Problem("URL（https:// または http:// で始まるもの）とストリームを指定してください")
        url, stream = (json.dumps(value, ensure_ascii=False) for value in wanted)
        data = TEMPLATE.format(url=url, stream=stream).encode("utf-8")
        path.parent.mkdir(exist_ok=True)
        try:
            with path.open("xb") as file:  # exclusive: an existing settings file is never rewritten
                file.write(data)
            created = True
        except FileExistsError:
            pass
    settings, values = check_settings(path)
    have = is_set("COV_USER") and is_set("COV_AUTH_KEY")
    checks = [settings, check("認証情報", "" if have else "環境変数 COV_USER と COV_AUTH_KEY を設定してください",
                              "COV_USER と COV_AUTH_KEY は設定済みです"), *check_build(values)]
    out = {"ok": all(item["ok"] for item in checks), "config_file": path.as_posix(), "created": created,
           "vcs": kind, "checks": checks}
    if not have:
        out["credentials"] = CREDENTIALS
    elif settings["ok"]:
        out["fetch"] = [{"tool": "coverity_check_connection", "arguments": {"repo_root": repo.as_posix()}}]
    current = (str(values.get("coverity.url")).strip().rstrip("/"), str(values.get("coverity.stream")).strip())
    if created:
        message = f"設定ファイルを作成しました: {path.as_posix()}"
    elif wanted and wanted != current:
        message = ("設定ファイルは既にあります。URL やストリームを変更するときは、"
                   f"{path.as_posix()} の coverity.url と coverity.stream を編集してください")
    else:
        message = "設定を確認しました" if out["ok"] else "設定を確認しました。対応が必要な項目があります"
    return {**out, "message": message, "next": []}


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="setup.py")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    init = sub.add_parser("init")
    for name in ("--repo", "--url", "--stream"):
        init.add_argument(name, required=True)
    sub.add_parser("check").add_argument("--repo", required=True)
    code = 0
    try:
        out = answer(parser.parse_args(argv))
    except Problem as exc:
        out = {"ok": False, "error": str(exc), "next": []}
    except Exception as exc:  # noqa: BLE001 - one JSON object, never a traceback
        out, code = {"ok": False, "error": f"予期しないエラーが発生しました: {type(exc).__name__}: {exc}", "next": []}, 1
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(main())
