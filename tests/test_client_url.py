"""Unit tests for zhub.client._to_ws_url — the http/ws → wss URL builder.

The helper documents that it preserves both the port and any path prefix
(a hub may be reverse-proxied under e.g. example.com/zhub/). Port was always
kept; the path prefix used to be dropped whenever a scheme was present, which
silently routed publishers/clients to the wrong WS endpoint behind a proxy.
"""

from zhub.client import _to_ws_url

import pytest


def test_scheme_to_secure_ws():
    assert _to_ws_url("https://hub.example.com", "/ws/publish") == "wss://hub.example.com/ws/publish"
    assert _to_ws_url("http://hub.example.com", "/ws/publish") == "ws://hub.example.com/ws/publish"


def test_already_ws_scheme_preserved():
    assert _to_ws_url("ws://localhost:8080", "/ws/connect") == "ws://localhost:8080/ws/connect"
    assert _to_ws_url("wss://hub.example.com", "/ws/connect") == "wss://hub.example.com/ws/connect"


def test_unknown_scheme_defaults_to_secure():
    assert _to_ws_url("ftp://hub.example.com", "/ws/publish") == "wss://hub.example.com/ws/publish"


def test_port_is_preserved():
    assert _to_ws_url("https://hub.example.com:9000", "/ws/publish") == "wss://hub.example.com:9000/ws/publish"


def test_path_prefix_is_preserved_with_scheme():
    # regression: the prefix used to be dropped when a scheme was present
    assert _to_ws_url("https://hub.example.com/zhub", "/ws/publish") == "wss://hub.example.com/zhub/ws/publish"


def test_port_and_path_prefix_both_preserved():
    assert (
        _to_ws_url("https://hub.example.com:9000/api/zhub", "/ws/connect")
        == "wss://hub.example.com:9000/api/zhub/ws/connect"
    )


def test_bare_host_accepted():
    assert _to_ws_url("hub.example.com", "/ws/expose") == "wss://hub.example.com/ws/expose"


def test_bare_host_with_prefix_preserved():
    assert _to_ws_url("hub.example.com/zhub", "/ws/publish") == "wss://hub.example.com/zhub/ws/publish"


def test_trailing_slash_does_not_double():
    assert _to_ws_url("https://hub.example.com/", "/ws/publish") == "wss://hub.example.com/ws/publish"


def test_empty_url_raises():
    with pytest.raises(ValueError):
        _to_ws_url("", "/ws/publish")
