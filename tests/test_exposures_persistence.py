"""Phase 7.0 — SQLite storage for capability-only exposures."""

import pytest

from zhub.persistence import Storage, hash_key


@pytest.fixture
def store(tmp_path):
    return Storage(tmp_path / "test.db")


def test_add_exposure_returns_id_and_persists(store):
    eid = store.add_exposure(
        name="weather-sensor",
        manifest={"capabilities": [{"name": "weather_lookup"}], "public": True},
        device_key_hash=hash_key("dx_secret"),
    )
    assert eid.startswith("ex_")
    rows = store.all_exposures()
    assert len(rows) == 1
    e = rows[0]
    assert e["exposure_id"] == eid
    assert e["name"] == "weather-sensor"
    assert e["manifest"]["public"] is True
    assert e["device_key_hash"] == hash_key("dx_secret")


def test_lookup_exposure_by_id(store):
    eid = store.add_exposure("x", {}, hash_key("dx_a"))
    found = store.lookup_exposure(eid)
    assert found is not None
    assert found["exposure_id"] == eid


def test_lookup_exposure_by_device_key(store):
    eid = store.add_exposure("x", {}, hash_key("dx_unique"))
    found = store.lookup_exposure_by_key_hash(hash_key("dx_unique"))
    assert found is not None
    assert found["exposure_id"] == eid


def test_remove_exposure(store):
    eid = store.add_exposure("x", {}, hash_key("dx_y"))
    store.remove_exposure(eid)
    assert store.lookup_exposure(eid) is None
    assert store.all_exposures() == []


def test_exposures_persist_across_reopen(tmp_path):
    db = tmp_path / "p.db"
    s1 = Storage(db)
    eid = s1.add_exposure("x", {}, hash_key("dx_z"))
    s1.close()
    s2 = Storage(db)
    assert s2.lookup_exposure(eid) is not None
