"""Union-type ("type": ["object", "null"]) structural validation.

The validator advertises that `type` may be a list of types. The primitive
type gate handled that, but the structural checks (required/properties for
objects, items for arrays) only fired for a bare "object"/"array" string, so
a union-typed schema silently skipped them and malformed args slipped through
to the capability handler. These cases pin the union path.
"""

from zhub.validate import validate


def test_union_object_reports_missing_required():
    schema = {
        "type": ["object", "null"],
        "required": ["city"],
        "properties": {"city": {"type": "string"}},
    }
    errs = validate({}, schema)
    assert any("city" in e for e in errs), errs


def test_union_object_reports_bad_nested_type():
    schema = {
        "type": ["object", "null"],
        "required": ["city"],
        "properties": {"city": {"type": "string"}},
    }
    errs = validate({"city": 42}, schema)
    assert any("city" in e and "string" in e for e in errs), errs


def test_union_object_accepts_valid_and_null():
    schema = {
        "type": ["object", "null"],
        "required": ["city"],
        "properties": {"city": {"type": "string"}},
    }
    assert validate({"city": "x"}, schema) == []
    # null is a permitted member of the union; it has no properties to check
    assert validate(None, schema) == []


def test_union_array_reports_bad_items():
    schema = {"type": ["array", "null"], "items": {"type": "string"}}
    errs = validate([1, 2], schema)
    assert len(errs) == 2, errs
    assert validate(["a", "b"], schema) == []


def test_single_type_and_untyped_unchanged():
    # regression guard for the non-union paths
    assert validate({}, {"type": "object", "required": ["city"]}) == [
        "<root>: missing required field 'city'"
    ]
    assert validate("x", {"type": "string"}) == []
    assert validate(5, {"type": "string"}) == ["<root>: expected string, got integer"]
    # untyped schema still infers structure from the value
    assert validate({"city": 42}, {"properties": {"city": {"type": "string"}}}) == [
        "city: expected string, got integer"
    ]
