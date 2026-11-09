"""A small JSON Schema (draft 2020-12) validator for the keywords the contract schemas use.

The schemas in assets/ are copies of other parts' contracts; ``unsupported(schema)`` lists keywords
this validator does not know, and a test keeps that list empty, so a new keyword in a contract is
noticed instead of being ignored silently.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .common import ASSETS_DIR

KNOWN = {"$schema", "$id", "$comment", "title", "description", "default", "examples", "type", "properties",
         "required", "additionalProperties", "items", "enum", "const", "pattern", "minLength", "maxLength",
         "minimum", "maximum", "minItems", "maxItems", "uniqueItems", "$ref", "$defs", "allOf", "anyOf", "oneOf",
         "not", "if", "then", "else", "propertyNames"}
TYPE_JA = {"object": "「キー: 値」の形（オブジェクト）", "array": "配列", "string": "文字列", "integer": "整数",
           "number": "数", "boolean": "true か false", "null": "null"}


def unsupported(schema: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key not in KNOWN:
                found.add(key)
            if key in ("properties", "$defs"):
                for sub in value.values():
                    found |= unsupported(sub)
            elif key in ("items", "additionalProperties", "if", "then", "else", "not", "propertyNames"):
                found |= unsupported(value)
            elif key in ("allOf", "anyOf", "oneOf"):
                for sub in value:
                    found |= unsupported(sub)
    return found


@lru_cache(maxsize=None)
def _compiled(pattern: str) -> re.Pattern[str]:
    # ECMA-262 "$" matches only at the very end (Python's also before a final newline).
    converted = re.sub(r"(?<!\\)\$(?=\)*$)", r"\\Z", pattern)
    return re.compile(converted)


def _is_type(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return False


def _where(path: str) -> str:
    return path or "（全体）"


class Validator:
    def __init__(self, schema: dict[str, Any]):
        self.root = schema

    def _resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise ValueError(f"unsupported $ref: {ref}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def errors(self, value: Any, schema: Any = None, path: str = "") -> list[str]:
        schema = self.root if schema is None else schema
        if schema is True or schema == {}:
            return []
        if schema is False:
            return [f"{_where(path)} は使えない項目です"]
        out: list[str] = []
        if "$ref" in schema:
            out += self.errors(value, self._resolve(schema["$ref"]), path)
        types = schema.get("type")
        if types is not None:
            names = types if isinstance(types, list) else [types]
            if not any(_is_type(value, n) for n in names):
                return out + [f"{_where(path)} は " + " か ".join(TYPE_JA.get(n, n) for n in names) + " にしてください"]
        if "enum" in schema and value not in schema["enum"]:
            shown = " / ".join("null" if v is None else str(v) for v in schema["enum"])
            out.append(f"{_where(path)} は次のどれかにしてください: {shown}")
        if "const" in schema and value != schema["const"]:
            out.append(f"{_where(path)} は {json.dumps(schema['const'], ensure_ascii=False)} にしてください")
        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                out.append(f"{_where(path)} は空にできません" if schema["minLength"] == 1
                           else f"{_where(path)} は {schema['minLength']} 文字以上にしてください")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                out.append(f"{_where(path)} は {schema['maxLength']} 文字以内にしてください（今は {len(value)} 文字）")
            if "pattern" in schema and not _compiled(schema["pattern"]).search(value):
                out.append(f"{_where(path)} は空白だけにできません" if schema["pattern"] == "\\S"
                           else f"{_where(path)} の形が正しくありません（前後に空白を入れない、空にしない など）")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                out.append(f"{_where(path)} は {schema['minimum']} 以上にしてください")
            if "maximum" in schema and value > schema["maximum"]:
                out.append(f"{_where(path)} は {schema['maximum']} 以下にしてください")
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                out.append(f"{_where(path)} は {schema['minItems']} 個以上にしてください")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                out.append(f"{_where(path)} は {schema['maxItems']} 個以下にしてください")
            if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
                out.append(f"{_where(path)} に同じ値が重なっています")
            if "items" in schema:
                for n, element in enumerate(value):
                    out += self.errors(element, schema["items"], f"{path}[{n}]")
        if isinstance(value, dict):
            props = schema.get("properties") or {}
            for key in schema.get("required") or []:
                if key not in value:
                    out.append(f"{path + '.' if path else ''}{key} がありません")
            for key, element in value.items():
                sub = f"{path}.{key}" if path else str(key)
                if key in props:
                    out += self.errors(element, props[key], sub)
                elif "additionalProperties" in schema:
                    extra = schema["additionalProperties"]
                    if extra is False:
                        out.append(f"{sub} は使えない項目です（決まった項目だけを書きます）")
                    elif isinstance(extra, dict):
                        out += self.errors(element, extra, sub)
        for sub in schema.get("allOf") or []:
            out += self.errors(value, sub, path)
        if "anyOf" in schema and not any(not self.errors(value, s, path) for s in schema["anyOf"]):
            out.append(f"{_where(path)} の形が正しくありません")
        if "oneOf" in schema and sum(1 for s in schema["oneOf"] if not self.errors(value, s, path)) != 1:
            out.append(f"{_where(path)} の形が正しくありません")
        if "not" in schema and not self.errors(value, schema["not"], path):
            out.append(f"{_where(path)} の形が正しくありません")
        if "if" in schema:
            branch = "then" if not self.errors(value, schema["if"], path) else "else"
            if branch in schema:
                out += self.errors(value, schema[branch], path)
        return out


@lru_cache(maxsize=None)
def load(name: str) -> dict[str, Any]:
    """A schema (or criteria.json) of assets/."""
    return json.loads((ASSETS_DIR / name).read_text(encoding="utf-8"))


def validate(value: Any, name: str) -> list[str]:
    """The problems of ``value`` against assets/<name> (empty: valid)."""
    return Validator(load(name)).errors(value)


def validate_with(value: Any, schema_path: Path) -> list[str]:
    return Validator(json.loads(schema_path.read_text(encoding="utf-8"))).errors(value)
