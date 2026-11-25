"""SQLite persistence for detections and species metadata."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "detections.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    scientific  TEXT NOT NULL,
    common      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    image       TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS species_meta (
    scientific  TEXT PRIMARY KEY,
    common      TEXT,
    image       TEXT,
    description TEXT,
    fetched_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_detections_ts ON detections (ts DESC);
"""


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def insert_detection(scientific: str, common: str, confidence: float) -> int:
    with _lock:
        cur = _connect().execute(
            "INSERT INTO detections (ts, scientific, common, confidence) VALUES (?, ?, ?, ?)",
            (now_ms(), scientific, common, confidence),
        )
        _connect().commit()
        return int(cur.lastrowid)


def update_detection_meta(detection_id: int, image: str | None, description: str | None) -> None:
    with _lock:
        _connect().execute(
            "UPDATE detections SET image = ?, description = ? WHERE id = ?",
            (image, description, detection_id),
        )
        _connect().commit()


def list_detections(limit: int = 500) -> list[dict[str, Any]]:
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM detections ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_detections() -> None:
    with _lock:
        _connect().execute("DELETE FROM detections")
        _connect().commit()


def get_species_meta(scientific: str) -> dict[str, Any] | None:
    with _lock:
        row = _connect().execute(
            "SELECT * FROM species_meta WHERE scientific = ?", (scientific,)
        ).fetchone()
    return dict(row) if row else None


def put_species_meta(scientific: str, common: str | None, image: str | None, description: str | None) -> None:
    with _lock:
        _connect().execute(
            """INSERT INTO species_meta (scientific, common, image, description, fetched_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(scientific) DO UPDATE SET
                 common = excluded.common,
                 image = excluded.image,
                 description = excluded.description,
                 fetched_at = excluded.fetched_at""",
            (scientific, common, image, description, now_ms()),
        )
        _connect().commit()
