"""What the server reads: the settings file of a repository and the credentials."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


class Failure(Exception):
    """A tool error: one or two Japanese sentences that say what is wrong and what to correct."""


@dataclass
class Settings:
    url: str
    stream: str
    checkers: list[str]
    impacts: list[str]
    classification: list[str]
    limit: int

    @property
    def conditions(self) -> dict:
        """The conditions as they are applied (defaults filled in)."""
        return {"checkers": self.checkers, "impacts": self.impacts, "classification": self.classification,
                "limit": self.limit}


def absolute(value: str, name: str) -> Path:
    """A path argument. The server runs in its own folder, so a relative path would point there."""
    path = Path(str(value))
    if not path.is_absolute():
        raise Failure(f"{name} は絶対パスで指定してください: {value}")
    return path


def _list(value) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if item is not None and str(item).strip()]


def load(repo_root: str) -> Settings:
    """coverity.url, coverity.stream and conditions.* of <repo_root>/.coverity-triage/config.yaml."""
    path = absolute(repo_root, "repo_root") / ".coverity-triage" / "config.yaml"
    shown = path.as_posix()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, NotADirectoryError):
        raise Failure(f"設定ファイルがありません: {shown}。"
                      "/coverity-triage-kit:coverity-setup <URL> <ストリーム> を実行してください") from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Failure(f"設定ファイルを読み取れません: {shown}（{' '.join(str(exc).split())}）") from None
    data = data if isinstance(data, dict) else {}
    coverity = data.get("coverity") if isinstance(data.get("coverity"), dict) else {}
    conditions = data.get("conditions") if isinstance(data.get("conditions"), dict) else {}
    url, stream = str(coverity.get("url") or "").strip().rstrip("/"), str(coverity.get("stream") or "").strip()
    if not url or not stream:
        raise Failure(f"設定ファイルに coverity.url と coverity.stream を記入してください: {shown}")
    if not url.lower().startswith(("https://", "http://")):
        raise Failure(f"設定ファイルの coverity.url は https:// または http:// で始めてください: {shown}")
    limit = conditions.get("limit")
    limit = 20 if limit is None or limit == "" else limit
    if type(limit) is not int or limit < 1:
        raise Failure(f"設定ファイルの conditions.limit は 1 以上の整数で指定してください: {shown}")
    return Settings(url, stream, _list(conditions.get("checkers")), _list(conditions.get("impacts")),
                    _list(conditions.get("classification")) or ["Unclassified"], limit)


def get_env(name: str) -> str:
    """An environment variable; on Windows also the value saved for the user (HKCU\\Environment), which a
    client started before it was saved does not pass on."""
    value = os.environ.get(name, "").strip()
    if not value and sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, name)[0]).strip()
        except OSError:
            value = ""
    return value


def credentials() -> tuple[str, str]:
    user, key = get_env("COV_USER"), get_env("COV_AUTH_KEY")
    if not user or not key:
        raise Failure("環境変数 COV_USER と COV_AUTH_KEY を設定してください")
    return user, key
