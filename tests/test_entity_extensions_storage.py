"""Tests for SQLite-backed storage of operator-added entity extensions."""

import time

import pytest

from zhub.persistence import Storage


@pytest.fixture
def store(tmp_path):
    return Storage(tmp_path / "test.db")


def test_add_extension_returns_id_and_persists(store):
    eid = store.add_entity_extension(
        section="patterns",
        title="loki shortcut",
        body="If user says X, call send_whatsapp via /v1/invoke directly.",
        added_by="zai",
    )
    assert isinstance(eid, int) and eid > 0
    rows = store.list_entity_extensions()
    assert len(rows) == 1
    e = rows[0]
    assert e["id"] == eid
    assert e["section"] == "patterns"
    assert e["title"] == "loki shortcut"
    assert e["body"].startswith("If user says X")
    assert e["added_by"] == "zai"
    assert isinstance(e["added_at"], int)


def test_list_returns_all_in_insertion_order(store):
    store.add_entity_extension("errors", "418", "It's a teapot.", "zai")
    time.sleep(0.001)
    store.add_entity_extension("debug", "no audio", "Check pulse.", "loki")
    rows = store.list_entity_extensions()
    titles = [r["title"] for r in rows]
    assert titles == ["418", "no audio"]


def test_filter_list_by_section(store):
    store.add_entity_extension("errors", "418", "Teapot.", "zai")
    store.add_entity_extension("debug", "no audio", "Check pulse.", "loki")
    store.add_entity_extension("errors", "419", "Foo.", "zai")
    rows = store.list_entity_extensions(section="errors")
    assert sorted(r["title"] for r in rows) == ["418", "419"]


def test_delete_extension_by_id(store):
    eid = store.add_entity_extension("patterns", "p", "b", "zai")
    assert store.delete_entity_extension(eid) is True
    assert store.list_entity_extensions() == []


def test_delete_nonexistent_returns_false(store):
    assert store.delete_entity_extension(999_999) is False


def test_extension_count(store):
    assert store.count_entity_extensions() == 0
    store.add_entity_extension("p", "x", "y", "zai")
    store.add_entity_extension("e", "x", "y", "zai")
    assert store.count_entity_extensions() == 2


def test_extensions_persist_across_storage_reopens(tmp_path):
    db = tmp_path / "persist.db"
    s1 = Storage(db)
    eid = s1.add_entity_extension("patterns", "x", "y", "zai")
    s1.close()
    s2 = Storage(db)
    rows = s2.list_entity_extensions()
    assert len(rows) == 1
    assert rows[0]["id"] == eid
