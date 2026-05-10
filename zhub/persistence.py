"""
SQLite-backed persistence for the hub.

Stores publisher records (name, manifest JSON, hashed api key, last_seen)
across hub restarts. When a publisher reconnects after a hub restart, the
hub recognizes it (api key matches the stored hash) and rehydrates the
in-memory registry.

Connections are NOT persisted — they're inherently ephemeral (alive while
the WebSocket is). On restart, clients reconnect fresh.

Schema (v1):

    CREATE TABLE publishers (
      name           TEXT PRIMARY KEY,
      manifest_json  TEXT NOT NULL,
      api_key_hash   TEXT NOT NULL,
      first_seen     INTEGER NOT NULL,
      last_seen      INTEGER NOT NULL,
      total_chats    INTEGER NOT NULL DEFAULT 0
    );
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("zhub.persistence")


class Storage:
    """Tiny synchronous SQLite wrapper. Single connection guarded by a lock."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS publishers (
        name           TEXT PRIMARY KEY,
        manifest_json  TEXT NOT NULL,
        api_key_hash   TEXT NOT NULL,
        first_seen     INTEGER NOT NULL,
        last_seen      INTEGER NOT NULL,
        total_chats    INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_publishers_last_seen ON publishers(last_seen);
    CREATE TABLE IF NOT EXISTS entity_extensions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        section     TEXT NOT NULL,
        title       TEXT NOT NULL,
        body        TEXT NOT NULL,
        added_by    TEXT NOT NULL,
        added_at    INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_extensions_section ON entity_extensions(section);
    CREATE TABLE IF NOT EXISTS exposures (
        exposure_id     TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        manifest_json   TEXT NOT NULL,
        device_key_hash TEXT NOT NULL,
        first_seen      INTEGER NOT NULL,
        last_seen       INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_exposures_key ON exposures(device_key_hash);
    """

    def __init__(self, path: str | Path = "zhub.db") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()
        log.info("persistence opened at %s", self.path)

    def upsert_publisher(self, name: str, manifest: dict[str, Any], api_key_hash: str) -> None:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute("SELECT first_seen FROM publishers WHERE name = ?", (name,))
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO publishers (name, manifest_json, api_key_hash, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, json.dumps(manifest), api_key_hash, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE publishers SET manifest_json = ?, api_key_hash = ?, last_seen = ? WHERE name = ?",
                    (json.dumps(manifest), api_key_hash, now, name),
                )
            self._conn.commit()

    def touch_publisher(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE publishers SET last_seen = ? WHERE name = ?",
                (int(time.time()), name),
            )
            self._conn.commit()

    def increment_chats(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE publishers SET total_chats = total_chats + 1 WHERE name = ?",
                (name,),
            )
            self._conn.commit()

    def lookup_publisher(self, name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT name, manifest_json, api_key_hash, first_seen, last_seen, total_chats "
                "FROM publishers WHERE name = ?",
                (name,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "name": row[0],
            "manifest": json.loads(row[1]),
            "api_key_hash": row[2],
            "first_seen": row[3],
            "last_seen": row[4],
            "total_chats": row[5],
        }

    def all_publishers(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT name, manifest_json, api_key_hash, first_seen, last_seen, total_chats "
                "FROM publishers ORDER BY last_seen DESC"
            )
            rows = cur.fetchall()
        return [
            {
                "name": r[0],
                "manifest": json.loads(r[1]),
                "api_key_hash": r[2],
                "first_seen": r[3],
                "last_seen": r[4],
                "total_chats": r[5],
            }
            for r in rows
        ]

    def remove_publisher(self, name: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM publishers WHERE name = ?", (name,))
            self._conn.commit()

    # ---- entity extensions (Phase 4.1) -----------------------------------

    def add_entity_extension(
        self, section: str, title: str, body: str, added_by: str,
    ) -> int:
        """Insert a new operator-added entity recipe. Returns the new id."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO entity_extensions (section, title, body, added_by, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (section, title, body, added_by, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_entity_extensions(
        self, section: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return all extensions in insertion order, optionally filtered by section."""
        with self._lock:
            if section is None:
                cur = self._conn.execute(
                    "SELECT id, section, title, body, added_by, added_at "
                    "FROM entity_extensions ORDER BY id ASC"
                )
            else:
                cur = self._conn.execute(
                    "SELECT id, section, title, body, added_by, added_at "
                    "FROM entity_extensions WHERE section = ? ORDER BY id ASC",
                    (section,),
                )
            rows = cur.fetchall()
        return [
            {
                "id": r[0], "section": r[1], "title": r[2],
                "body": r[3], "added_by": r[4], "added_at": r[5],
            }
            for r in rows
        ]

    def delete_entity_extension(self, ext_id: int) -> bool:
        """Delete by id; returns True if a row was removed, False otherwise."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM entity_extensions WHERE id = ?", (ext_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count_entity_extensions(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM entity_extensions")
            return int(cur.fetchone()[0])

    # ---- exposures (Phase 7.0) -------------------------------------------

    def add_exposure(self, name: str, manifest: dict[str, Any],
                     device_key_hash: str,
                     exposure_id: Optional[str] = None) -> str:
        """Insert a new exposure. If exposure_id is None, mints a fresh `ex_...`.
        Returns the exposure_id."""
        import secrets as _secrets
        eid = exposure_id or "ex_" + _secrets.token_urlsafe(8)
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO exposures (exposure_id, name, manifest_json, "
                "device_key_hash, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (eid, name, json.dumps(manifest), device_key_hash, now, now),
            )
            self._conn.commit()
        return eid

    def lookup_exposure(self, exposure_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT exposure_id, name, manifest_json, device_key_hash, "
                "first_seen, last_seen FROM exposures WHERE exposure_id = ?",
                (exposure_id,),
            )
            row = cur.fetchone()
        return _exposure_row_to_dict(row)

    def lookup_exposure_by_key_hash(self, device_key_hash: str) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT exposure_id, name, manifest_json, device_key_hash, "
                "first_seen, last_seen FROM exposures WHERE device_key_hash = ?",
                (device_key_hash,),
            )
            row = cur.fetchone()
        return _exposure_row_to_dict(row)

    def all_exposures(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT exposure_id, name, manifest_json, device_key_hash, "
                "first_seen, last_seen FROM exposures ORDER BY first_seen ASC"
            )
            rows = cur.fetchall()
        return [_exposure_row_to_dict(r) for r in rows if r]

    def touch_exposure(self, exposure_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE exposures SET last_seen = ? WHERE exposure_id = ?",
                (int(time.time()), exposure_id),
            )
            self._conn.commit()

    def remove_exposure(self, exposure_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM exposures WHERE exposure_id = ?", (exposure_id,)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _exposure_row_to_dict(row) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {
        "exposure_id": row[0],
        "name": row[1],
        "manifest": json.loads(row[2]),
        "device_key_hash": row[3],
        "first_seen": row[4],
        "last_seen": row[5],
    }
