"""A small JSON Schema checker (the keywords the contract schemas use), stdlib only.

Supported: type, enum, const, required, properties, additionalProperties, patternProperties,
items, prefixItems, minItems, maxItems, uniqueItems, minimum, maximum, exclusiveMinimum,
exclusiveMaximum, minLength, maxLength, pattern, anyOf, oneOf, allOf, not, $ref (inside the same
file: ``#/$defs/...``) and if/then/else. Other keywords (description, default, format, ...) are ignored.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool)
    or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def load(path: Path) -> dict[str, Any] | None:
    """A schema file, or None when there is none (the copies of the contracts may not be here yet)."""
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return None


def errors(value: Any, schema: dict[str, Any], limit: int = 20) -> list[str]:
    """What does not match, one line each (``$.items[0].status: ...``); empty when it matches."""
    found: list[str] = []
    _check(value, schema, schema, "$", found, limit)
    return found[:limit]


def _ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#"):
        raise ValueError(f"unsupported $ref {ref}")
    node: Any = root
    for part in [p for p in ref[1:].split("/") if p]:
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _equal(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b or (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
        and not isinstance(a, bool) and not isinstance(b, bool) and a == b)


def _check(value: Any, schema: Any, root: dict[str, Any], at: str, out: list[str], limit: int) -> None:
    if len(out) >= limit or schema is True:
        return
    if schema is False:
        out.append(f"{at}: ここには何も書けません")
        return
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        _check(value, _ref(root, schema["$ref"]), root, at, out, limit)
    kinds = schema.get("type")
    if kinds is not None:
        names = kinds if isinstance(kinds, list) else [kinds]
        if not any(_TYPES.get(name, lambda v: True)(value) for name in names):
            out.append(f"{at}: 型が違います（{' / '.join(names)} にします）")
            return
    if "enum" in schema and not any(_equal(value, x) for x in schema["enum"]):
        out.append(f"{at}: 次のどれかにします: {', '.join(json.dumps(x, ensure_ascii=False) for x in schema['enum'])}")
    if "const" in schema and not _equal(value, schema["const"]):
        out.append(f"{at}: {json.dumps(schema['const'], ensure_ascii=False)} にします")
    if isinstance(value, dict):
        _object(value, schema, root, at, out, limit)
    if isinstance(value, list):
        _array(value, schema, root, at, out, limit)
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{at}: {schema['minLength']} 文字以上にします")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{at}: {schema['maxLength']} 文字以内にします")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{at}: 書き方が違います（{schema['pattern']}）")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{at}: {schema['minimum']} 以上にします")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{at}: {schema['maximum']} 以下にします")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append(f"{at}: {schema['exclusiveMinimum']} より大きくします")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            out.append(f"{at}: {schema['exclusiveMaximum']} より小さくします")
    for sub in schema.get("allOf") or []:
        _check(value, sub, root, at, out, limit)
    if "anyOf" in schema and not any(not errors_of(value, s, root) for s in schema["anyOf"]):
        out.append(f"{at}: どの形にも合いません")
    if "oneOf" in schema and sum(1 for s in schema["oneOf"] if not errors_of(value, s, root)) != 1:
        out.append(f"{at}: 形がちょうど 1 つに合いません")
    if "not" in schema and not errors_of(value, schema["not"], root):
        out.append(f"{at}: この形にはできません")
    if "if" in schema:
        branch = schema.get("then") if not errors_of(value, schema["if"], root) else schema.get("else")
        if branch is not None:
            _check(value, branch, root, at, out, limit)


def errors_of(value: Any, schema: Any, root: dict[str, Any]) -> list[str]:
    found: list[str] = []
    _check(value, schema, root, "$", found, 5)
    return found


def _object(value: dict[str, Any], schema: dict[str, Any], root: dict[str, Any], at: str,
            out: list[str], limit: int) -> None:
    for name in schema.get("required") or []:
        if name not in value:
            out.append(f"{at}.{name}: ありません")
    props = schema.get("properties") or {}
    patterns = schema.get("patternProperties") or {}
    extra = schema.get("additionalProperties", True)
    for key, item in value.items():
        matched = False
        if key in props:
            matched = True
            _check(item, props[key], root, f"{at}.{key}", out, limit)
        for pattern, sub in patterns.items():
            if re.search(pattern, key):
                matched = True
                _check(item, sub, root, f"{at}.{key}", out, limit)
        if not matched:
            if extra is False:
                out.append(f"{at}.{key}: この項目は書けません")
            elif isinstance(extra, dict):
                _check(item, extra, root, f"{at}.{key}", out, limit)


def _array(value: list[Any], schema: dict[str, Any], root: dict[str, Any], at: str,
           out: list[str], limit: int) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        out.append(f"{at}: {schema['minItems']} 個以上にします")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        out.append(f"{at}: {schema['maxItems']} 個以下にします")
    if schema.get("uniqueItems"):
        seen = [json.dumps(v, sort_keys=True) for v in value]
        if len(seen) != len(set(seen)):
            out.append(f"{at}: 同じ値が重なっています")
    prefix = schema.get("prefixItems") or []
    for i, item in enumerate(value):
        if i < len(prefix):
            _check(item, prefix[i], root, f"{at}[{i}]", out, limit)
        elif isinstance(schema.get("items"), (dict, bool)):
            _check(item, schema["items"], root, f"{at}[{i}]", out, limit)
