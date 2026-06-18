"""Unit coverage for mcp_server._substitute (prompts/get placeholder fill).

These exercise the pure substitution helper directly — no hub or subprocess —
so they pin the single-pass contract: an argument value is literal text, never
re-scanned for further placeholders.
"""

from zhub.mcp_server import _substitute


def test_basic_substitution():
    assert _substitute("Hi {name}", {"name": "Sam"}) == "Hi Sam"


def test_non_string_value_coerced():
    assert _substitute("amount: {n}", {"n": 42}) == "amount: 42"


def test_unknown_placeholder_left_intact():
    assert _substitute("Hi {name} {missing}", {"name": "Sam"}) == "Hi Sam {missing}"


def test_empty_args_returns_template_unchanged():
    assert _substitute("No {vars} here", {}) == "No {vars} here"


def test_repeated_placeholder_all_filled():
    assert _substitute("{x}-{x}", {"x": "A"}) == "A-A"


def test_value_containing_other_placeholder_is_literal():
    # The argument order puts {text} before {style}; the naive replace-in-a-loop
    # substituted {text} first, then re-scanned its inserted value and expanded
    # the literal "{style}" inside it into the style argument. The value must
    # stay verbatim.
    out = _substitute(
        "Text: {text}. Style: {style}",
        {"text": "make it {style}", "style": "formal"},
    )
    assert out == "Text: make it {style}. Style: formal"


def test_value_is_never_treated_as_template():
    # {a}'s value is itself "{b}"; it must not be expanded into b's value.
    assert _substitute("{a}", {"a": "{b}", "b": "Z"}) == "{b}"
