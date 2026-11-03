"""Environment variables for credentials (spec D-34, D-68)."""

from __future__ import annotations

import os
import sys


def get_env(name: str) -> str | None:
    """Environment variable, also reading the user's saved variables on Windows.

    Values saved as user environment variables are visible without restarting VS Code.
    """
    value = os.environ.get(name)
    if value:
        return value
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                stored, _ = winreg.QueryValueEx(key, name)
                return str(stored) or None
        except OSError:
            return None
    return None
