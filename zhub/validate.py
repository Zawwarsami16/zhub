"""Minimal JSON-Schema subset validator.

Used by the hub to short-circuit a tool invocation when an LLM emits
arguments that don't match the connected capability's declared schema.
We deliberately avoid pulling in jsonschema as a dependency — capabilities
are declared with simple shapes, and a tight subset is enough.

Supported keywords:
  type            ("object", "string", "number", "integer", "boolean",
                   "array", "null"; or a list of these)
  required        list of property names (only meaningful for type=object)
  properties      object mapping name → sub-schema (recursed)
  items           sub-schema for array elements (recursed)

Anything else is ignored. Returns a list of human-readable error strings;
empty list means valid.
"""

from __future__ import annotations

from typing import Any


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    """Return a list of validation errors. Empty list means the value
    matches the schema. ``path`` is used to produce field-level messages
    like 'foo.bar: expected string, got integer'."""
    if not isinstance(schema, dict) or not schema:
        return []

    errors: list[str] = []

    expected_type = schema.get("type")
    types = None
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in types):
            actual = _kind(value)
            joined = " or ".join(types)
            errors.append(f"{path or '<root>'}: expected {joined}, got {actual}")
            return errors  # short-circuit; downstream checks assume the type matches

    # Structural checks run whenever the value is actually a dict/list and the
    # schema permits that type. `types is None` covers untyped schemas; the
    # `"object"/"array" in types` membership covers both a bare "object" string
    # and a union like ["object", "null"] — the list form was silently skipping
    # required/properties/items before.
    if isinstance(value, dict) and (types is None or "object" in types):
        required = schema.get("required") or []
        for field in required:
            if field not in value:
                errors.append(f"{path or '<root>'}: missing required field '{field}'")
        properties = schema.get("properties") or {}
        for k, sub_schema in properties.items():
            if k in value:
                sub_path = f"{path}.{k}" if path else k
                errors.extend(validate(value[k], sub_schema, sub_path))

    if isinstance(value, list) and (types is None or "array" in types):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                sub_path = f"{path}[{i}]" if path else f"[{i}]"
                errors.extend(validate(item, item_schema, sub_path))

    return errors


def _kind(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    if v is None:
        return "null"
    return type(v).__name__
