"""Environment variables that hold the credentials.

Read: this process's environment, then (Windows) the user's saved variables in ``HKCU\\Environment``,
so that a value saved a moment ago is seen without restarting anything. An empty value and a value
left as ``${NAME}`` (not expanded by the program that started us) count as not set.
Write: only on Windows, into the user's variables (like ``[Environment]::SetEnvironmentVariable(.., 'User')``).
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


def get_env(name: str) -> str | None:
    if not name:
        return None
    value = _usable(os.environ.get(name))
    if value is not None:
        return value
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                stored, kind = winreg.QueryValueEx(key, name)
            if kind == getattr(winreg, "REG_EXPAND_SZ", 2):  # a value saved with %NAME% in it
                try:
                    stored = winreg.ExpandEnvironmentStrings(str(stored))
                except (AttributeError, OSError):
                    pass
            return _usable(str(stored))
        except OSError:
            return None
    return None


def can_set_user_env() -> bool:
    """Saving a variable for later programs is done only on Windows (the PCs this screen is for)."""
    return sys.platform == "win32"


def set_user_env(name: str, value: str) -> None:
    if not can_set_user_env():
        raise OSError("環境変数を保存できるのは Windows だけです")
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    os.environ[name] = value
    # Tell the running programs (Explorer, new terminals) that the variables changed.
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
