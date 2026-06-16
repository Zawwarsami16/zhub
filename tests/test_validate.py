"""Base-path coverage for the JSON-Schema subset validator.

`validate()` gates every tool invocation: when an LLM emits arguments that
don't match a connected capability's declared schema, the hub short-circuits
the call (server.py `_resolve_one`). The union-typed structural path is pinned
in test_validate_union_types; everything else — primitive type gating, the
bool/number/integer interplay, array `items`, nested objects, the `_kind`
message reporter, multi-error accumulation, and the untyped passthrough —
had no direct test. These pin the contract the resolver depends on.
"""

from zhub.validate import validate, _kind


# --- primitive type gate + message shape ----------------------------------

def test_type_mismatch_reports_root_and_kinds():
    assert validate(5, {"type": "string"}) == [
        "<root>: expected string, got integer"
    ]


def test_int_validates_as_number_but_float_not_as_integer():
    # JSON has one numeric type; the validator treats any int as a valid
    # `number`, but an `integer` must be a real int (a float like 5.0 fails).
    assert validate(5, {"type": "number"}) == []
    assert validate(5.0, {"type": "integer"}) == [
        "<root>: expected integer, got number"
    ]


def test_bool_is_not_integer_or_number():
    # bool is an int subclass in Python; the validator must reject it for
    # numeric types or a schema expecting a count would accept True.
    assert validate(True, {"type": "integer"}) == [
        "<root>: expected integer, got boolean"
    ]
    assert validate(True, {"type": "number"}) == [
        "<root>: expected number, got boolean"
    ]


def test_null_member_of_union_passes():
    assert validate(None, {"type": ["string", "null"]}) == []


def test_unknown_type_string_is_lenient():
    # Unsupported keywords/types are ignored rather than crashing the gate.
    assert validate(5, {"type": "weird"}) == []


def test_empty_or_non_dict_schema_passes():
    assert validate({"anything": 1}, {}) == []
    assert validate(5, "not-a-schema") == []  # type: ignore[arg-type]


# --- structural: objects ----------------------------------------------------

def test_missing_required_accumulates_each_field():
    errs = validate({}, {"type": "object", "required": ["a", "b"]})
    assert errs == [
        "<root>: missing required field 'a'",
        "<root>: missing required field 'b'",
    ]


def test_property_type_error_carries_field_name():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    assert validate({"a": "x", "b": 3}, schema) == [
        "b: expected string, got integer"
    ]


def test_nested_object_path_is_dotted():
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": "object", "properties": {"y": {"type": "string"}}}
        },
    }
    assert validate({"x": {"y": 1}}, schema) == [
        "x.y: expected string, got integer"
    ]


def test_required_ignored_when_value_is_not_an_object():
    # `required` is only meaningful for objects; a string value must not
    # trip a missing-field error.
    assert validate("hi", {"required": ["a"]}) == []


# --- structural: arrays -----------------------------------------------------

def test_array_item_error_carries_index():
    schema = {"type": "array", "items": {"type": "integer"}}
    assert validate([1, "x", 3], schema) == [
        "[1]: expected integer, got string"
    ]


# --- type-mismatch short-circuit -------------------------------------------

def test_type_mismatch_short_circuits_structural_checks():
    # When the top-level type is wrong, only the type error is reported —
    # downstream required/properties checks assume the type matched.
    assert validate(5, {"type": "object", "required": ["a"]}) == [
        "<root>: expected object, got integer"
    ]


# --- untyped schemas still run structural checks ---------------------------

def test_untyped_schema_still_checks_required_and_properties():
    errs = validate(
        {}, {"required": ["a"], "properties": {"a": {"type": "string"}}}
    )
    assert errs == ["<root>: missing required field 'a'"]


def test_untyped_schema_still_checks_array_items():
    assert validate([1, "x"], {"items": {"type": "integer"}}) == [
        "[1]: expected integer, got string"
    ]


# --- _kind reporter ---------------------------------------------------------

def test_kind_distinguishes_every_json_type():
    assert _kind(True) == "boolean"
    assert _kind(3) == "integer"
    assert _kind(1.5) == "number"
    assert _kind("s") == "string"
    assert _kind([]) == "array"
    assert _kind({}) == "object"
    assert _kind(None) == "null"


def test_kind_falls_back_to_python_type_name():
    assert _kind((1, 2)) == "tuple"
