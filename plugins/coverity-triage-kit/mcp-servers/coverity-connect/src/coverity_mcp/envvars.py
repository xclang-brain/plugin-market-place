"""Environment variables: credentials and the settings of the env-only mode.

Rules (the same in every place that reads a user's variable):

- A value that is empty, or that still reads ``${NAME}`` (a client that did not expand the
  ``env`` block of its MCP registration), counts as not set.
- On Windows, a variable missing from the process is read from the user's saved variables
  (``HKCU\\Environment``). Clients such as the Copilot runtime pass only PATH to MCP servers, and
  a value saved after the client started is seen without a restart. REG_EXPAND_SZ values are
  expanded.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable

UNEXPANDED = re.compile(r"^\s*\$\{[^}]*\}\s*$")
MASK = "****"


def is_unset(value: str | None) -> bool:
    return value is None or not value.strip() or bool(UNEXPANDED.match(value))


def _registry(name: str) -> str | None:
    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    text = str(value) if value is not None else ""
    if kind == getattr(winreg, "REG_EXPAND_SZ", 2):
        try:
            text = winreg.ExpandEnvironmentStrings(text)
        except (AttributeError, OSError):
            pass
    return text


def get_env(name: str) -> str | None:
    """The value of a user's variable, or None when it is not set (see the module rules)."""
    value = os.environ.get(name)
    if not is_unset(value):
        return value.strip()
    if sys.platform == "win32":
        stored = _registry(name)
        if not is_unset(stored):
            return stored.strip()
    return None


def drop_unexpanded() -> list[str]:
    """Remove variables that arrived as a literal ``${...}``, so that libraries reading them
    (for example the proxy variables read by httpx) see them as not set. Returns their names."""
    names = [k for k, v in os.environ.items() if UNEXPANDED.match(v or "")]
    for name in names:
        os.environ.pop(name, None)
    return names


class Masker:
    """Replaces the values of the credential variables with ``****`` in text written to logs,
    error messages and records."""

    MIN_LENGTH = 3  # shorter values would mask unrelated text

    def __init__(self, names: Iterable[str]):
        values = {v for v in (get_env(n) for n in names) if v and len(v) >= self.MIN_LENGTH}
        self.values = sorted(values, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.values:
            value = value.replace(secret, MASK)
        return value

    def data(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.data(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.data(v) for v in value]
        return value
