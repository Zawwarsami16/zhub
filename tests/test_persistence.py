"""SQLite persistence tests."""

import os
import tempfile

import pytest

from zhub.persistence import Storage, hash_key


def test_upsert_and_lookup():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        s = Storage(path)
        manifest = {"name": "zai", "description": "test", "public": True, "capabilities": []}
        s.upsert_publisher("zai", manifest, hash_key("zk_secret_xyz"))
        row = s.lookup_publisher("zai")
        assert row is not None
        assert row["name"] == "zai"
        assert row["manifest"]["public"] is True
        assert row["api_key_hash"] == hash_key("zk_secret_xyz")
        s.close()
    finally:
        os.unlink(path)


def test_persistence_survives_reopen():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        s1 = Storage(path)
        s1.upsert_publisher("a", {"name": "a"}, hash_key("k1"))
        s1.upsert_publisher("b", {"name": "b"}, hash_key("k2"))
        s1.increment_chats("a")
        s1.increment_chats("a")
        s1.close()

        s2 = Storage(path)
        all_rows = s2.all_publishers()
        names = {r["name"] for r in all_rows}
        assert names == {"a", "b"}
        a_row = s2.lookup_publisher("a")
        assert a_row["total_chats"] == 2
        s2.close()
    finally:
        os.unlink(path)


def test_remove_publisher():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        s = Storage(path)
        s.upsert_publisher("x", {"name": "x"}, hash_key("k"))
        assert s.lookup_publisher("x") is not None
        s.remove_publisher("x")
        assert s.lookup_publisher("x") is None
        s.close()
    finally:
        os.unlink(path)


def test_hash_key_deterministic():
    assert hash_key("foo") == hash_key("foo")
    assert hash_key("foo") != hash_key("bar")
    assert len(hash_key("anything")) == 64  # SHA-256 hex
