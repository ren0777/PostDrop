"""SQLite log of every publish attempt (real and dry run)."""
from __future__ import annotations

import sqlite3
import time

from . import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    post_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    ok INTEGER NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    error TEXT,
    caption TEXT,
    note TEXT
)
"""

# Columns added after the first release, applied to older databases on open.
_MIGRATIONS = (("note", "ALTER TABLE history ADD COLUMN note TEXT"),)

_COLUMNS = ("ts", "post_id", "platform", "ok", "dry_run", "url", "error", "caption", "note")


def _connect() -> sqlite3.Connection:
    paths.ensure_dirs()
    connection = sqlite3.connect(paths.DB_PATH)
    connection.execute(_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(history)")}
    for name, statement in _MIGRATIONS:
        if name not in columns:
            connection.execute(statement)
    return connection


def record(*, post_id: str, platform: str, ok: bool, dry_run: bool,
           url: str | None = None, error: str | None = None, caption: str = "",
           note: str | None = None) -> None:
    connection = _connect()
    try:
        connection.execute(
            "INSERT INTO history (ts, post_id, platform, ok, dry_run, url, error, caption, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), post_id, platform, int(bool(ok)), int(bool(dry_run)),
             url, error, (caption or "")[:500], note),
        )
        connection.commit()
    finally:
        connection.close()


def recent(limit: int = 100) -> list[dict]:
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT ts, post_id, platform, ok, dry_run, url, error, caption, note"
            " FROM history ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        connection.close()
    return [dict(zip(_COLUMNS, row)) for row in rows]
