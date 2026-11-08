"""Environment variables for credentials. On Windows the user's saved variables are read as well."""

from __future__ import annotations

import os
import sys


def _placeholder(value: str) -> bool:
    """A value that was never filled in (empty, or a ``${NAME}`` that nobody expanded)."""
    value = value.strip()
    return not value or (value.startswith("${") and value.endswith("}"))


def get_env(name: str) -> str | None:
    """The value of an environment variable; on Windows also HKCU\\Environment (saved for the user,
    visible without restarting the terminal)."""
    if not name:
        return None
    value = os.environ.get(name)
    if value and not _placeholder(value):
        return value
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                stored, _ = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        stored = str(stored or "")
        return None if _placeholder(stored) else stored
    return None


KEY_ENVS = ("COVERITY_PASSPHRASE",)


def secrets(user_env: str, key_env: str) -> list[str]:
    """Values that never go into a log or a report (masked as ****)."""
    found = [get_env(n) for n in (user_env, key_env, *KEY_ENVS)]
    return sorted({v for v in found if v and len(v) >= 2}, key=len, reverse=True)
