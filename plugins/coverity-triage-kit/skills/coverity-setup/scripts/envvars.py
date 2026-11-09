"""Read an environment variable the way the credential readers do (standard library only).

On Windows a variable saved for the user (``HKCU\\Environment``, what
``[Environment]::SetEnvironmentVariable(name, value, 'User')`` writes) is read directly, so a value
saved a moment ago is seen without restarting the terminal. An empty value, or one that arrived as an
unexpanded ``${NAME}``, counts as not set. Values are never printed by this module.
"""
from __future__ import annotations

import os
import re
import sys

_UNEXPANDED = re.compile(r"^\$\{[^}]*\}$")


def _usable(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or _UNEXPANDED.match(value):
        return None
    return value


def _user_registry(name: str) -> str | None:
    """The user's saved variable on Windows (None elsewhere, or when it is not there)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            stored, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if kind == getattr(winreg, "REG_EXPAND_SZ", 2):  # a value saved with %NAME% in it
        try:
            stored = winreg.ExpandEnvironmentStrings(str(stored))
        except (AttributeError, OSError):
            pass
    return _usable(str(stored))


def get_env(name: str) -> str | None:
    """The value of ``name`` from this process, else (Windows) from the user's saved variables."""
    return _usable(os.environ.get(name)) or _user_registry(name)


def where(name: str) -> str | None:
    """Where the value was found: "process", "user" (Windows saved variable), or None."""
    if _usable(os.environ.get(name)):
        return "process"
    if _user_registry(name):
        return "user"
    return None
