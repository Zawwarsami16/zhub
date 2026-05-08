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

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
