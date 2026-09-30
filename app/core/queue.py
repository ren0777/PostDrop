"""The lazy-mode queue: SQLite, one item per card per chunk, spaced out.

`data/queue.sqlite` survives restarts. Each item is one post to one card: a chunk
of a batch's images, the caption text written for that site, its due time, and
what happened when it ran. Files and hashes are remembered here too, so nothing
is ever queued or posted twice for the same card.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import lazy, paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    folder_key TEXT NOT NULL DEFAULT '',
    card_id TEXT NOT NULL,
    images TEXT NOT NULL DEFAULT '[]',
    cover TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    alt_text TEXT NOT NULL DEFAULT '',
    character TEXT NOT NULL DEFAULT '',
    series TEXT NOT NULL DEFAULT '',
    mature INTEGER NOT NULL DEFAULT 0,
    due_at REAL NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_card ON items (card_id, status, due_at);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    digest TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posted (
    digest TEXT NOT NULL,
    card_id TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    posted_at REAL NOT NULL,
    PRIMARY KEY (digest, card_id)
);
CREATE TABLE IF NOT EXISTS cards (
    card_id TEXT PRIMARY KEY,
    last_post_at REAL NOT NULL DEFAULT 0,
    last_slot REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS batches (
    batch_key TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    folder_key TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    moved_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS identities (
    digest TEXT PRIMARY KEY,
    characters TEXT NOT NULL DEFAULT '[]',
    series TEXT NOT NULL DEFAULT '',
    confident INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'ai',
    asked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS needs_you (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    folder_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'unknown',
    images TEXT NOT NULL DEFAULT '[]',
    cover TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    alt_text TEXT NOT NULL DEFAULT '',
    character TEXT NOT NULL DEFAULT '',
    series TEXT NOT NULL DEFAULT '',
    platforms TEXT NOT NULL DEFAULT '{}',
    mature INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS ai_retries (
    key TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
"""


def connect() -> sqlite3.Connection:
    """A connection to the queue database, with the schema in place."""
    paths.DATA.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(paths.QUEUE_PATH), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)  # CREATE TABLE IF NOT EXISTS: cheap and idempotent
    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Add the columns later versions need (M6.1 added size/mtime, then notes)."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(files)")}
    if "size" not in columns:
        connection.execute("ALTER TABLE files ADD COLUMN size INTEGER NOT NULL DEFAULT 0")
    if "mtime" not in columns:
        connection.execute("ALTER TABLE files ADD COLUMN mtime REAL NOT NULL DEFAULT 0")
    notes = {row["name"] for row in connection.execute("PRAGMA table_info(needs_you)")}
    if "note" not in notes:
        connection.execute("ALTER TABLE needs_you ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    sources = {row["name"] for row in connection.execute("PRAGMA table_info(identities)")}
    if "source" not in sources:
        connection.execute("ALTER TABLE identities ADD COLUMN source TEXT NOT NULL DEFAULT 'ai'")


def init() -> None:
    """Create the tables (and the data folder) if they are missing."""
    with connect():
        pass


# ------------------------------------------------------------------ scheduling
def shift_out_of_quiet(when: float, quiet: dict | None) -> float:
    """Quiet hours push a due time forward to the end of the window."""
    if not quiet or not quiet.get("enabled"):
        return when
    start = int(quiet.get("start") or 0)
    end = int(quiet.get("end") or 0)
    if start == end:
        return when
    for _ in range(288):  # at most 24 h, in five-minute steps
        stamp = time.localtime(when)
        hour = stamp.tm_hour + stamp.tm_min / 60
        inside = (start <= hour < end) if start < end else (hour >= start or hour < end)
        if not inside:
            return when
        when += 300
    return when


def next_slot(connection, card_id: str, now: float, spacing_seconds: float) -> float:
    """The first moment a card may post again (its own spacing, never tighter)."""
    row = connection.execute(
        "SELECT last_post_at, last_slot FROM cards WHERE card_id = ?", (card_id,)
    ).fetchone()
    last = max(float(row["last_post_at"] or 0), float(row["last_slot"] or 0)) if row else 0.0
    return max(now, last + spacing_seconds)


def add_items(items: list[dict], *, spacing_minutes: dict | None = None,
              quiet: dict | None = None, now: float | None = None) -> list[int]:
    """Insert items, giving every card its own spaced slots (never all at once)."""
    now = time.time() if now is None else now
    spacing_minutes = spacing_minutes or {}
    slots: dict[str, float] = {}
    ids: list[int] = []
    with connect() as connection:
        for item in items:
            card_id = str(item["card_id"])
            spacing = float(spacing_minutes.get(card_id, lazy.DEFAULT_SPACING_MINUTES)) * 60
            start = slots.get(card_id)
            if start is None:
                start = next_slot(connection, card_id, now, spacing)
            due_at = shift_out_of_quiet(start, quiet)
            cursor = connection.execute(
                """INSERT INTO items (batch_key, source, folder_key, card_id, images, cover,
                       caption, tags, alt_text, character, series, mature, due_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(item.get("batch_key") or ""),
                    str(item.get("source") or ""),
                    str(item.get("folder_key") or ""),
                    card_id,
                    json.dumps([str(path) for path in item.get("images") or []]),
                    str(item.get("cover") or ""),
                    str(item.get("caption") or ""),
                    json.dumps([str(tag) for tag in item.get("tags") or []]),
                    str(item.get("alt_text") or ""),
                    str(item.get("character") or ""),
                    str(item.get("series") or ""),
                    1 if item.get("mature") else 0,
                    float(due_at),
                    float(item.get("created_at") or now),
                ),
            )
            ids.append(int(cursor.lastrowid))
            slots[card_id] = due_at + spacing
            _touch_slot(connection, card_id, due_at)
    return ids


def _touch_slot(connection, card_id: str, when: float) -> None:
    connection.execute(
        """INSERT INTO cards (card_id, last_post_at, last_slot) VALUES (?, 0, ?)
           ON CONFLICT(card_id) DO UPDATE SET last_slot = MAX(last_slot, ?)""",
        (card_id, when, when),
    )


def compact_schedule(now: float | None = None, quiet: dict | None = None) -> int:
    """Close the gaps that removed or dropped posts left behind.

    A queued post reserves its card's next slot, and slots only ever grew, so
    deleting posts left the rest waiting hours for nothing. Each card's waiting
    posts are re-spaced from its last real post (or now), keeping their order;
    a post is only ever moved earlier, never later. Returns how many moved.
    """
    now = time.time() if now is None else now
    data = lazy.stored()
    quiet = data["quiet"] if quiet is None else quiet
    moved = 0
    with connect() as connection:
        card_ids = {str(row["card_id"]) for row in connection.execute(
            "SELECT DISTINCT card_id FROM items WHERE status = 'pending'")}
        card_ids |= {str(row["card_id"]) for row in connection.execute(
            "SELECT card_id FROM cards")}
        for card_id in sorted(card_ids):
            spacing = float(lazy.limits_for(card_id.partition(":")[0], data)
                            ["spacing_minutes"]) * 60
            row = connection.execute("SELECT last_post_at FROM cards WHERE card_id = ?",
                                     (card_id,)).fetchone()
            cursor_at = max(now, (float(row["last_post_at"] or 0) if row else 0.0) + spacing)
            last_due = 0.0
            for entry in connection.execute(
                    "SELECT id, due_at FROM items WHERE card_id = ? AND status = 'pending' "
                    "ORDER BY due_at ASC, id ASC", (card_id,)).fetchall():
                slot = shift_out_of_quiet(cursor_at, quiet)
                due = min(float(entry["due_at"]), slot)
                if due < float(entry["due_at"]):
                    connection.execute("UPDATE items SET due_at = ? WHERE id = ?",
                                       (due, int(entry["id"])))
                    moved += 1
                last_due = max(last_due, due)
                cursor_at = max(slot, due) + spacing
            connection.execute(
                """INSERT INTO cards (card_id, last_post_at, last_slot) VALUES (?, 0, ?)
                   ON CONFLICT(card_id) DO UPDATE SET last_slot = ?""",
                (card_id, last_due, last_due))
    return moved


def card_ready(card_id: str, spacing_minutes: int | float, now: float | None = None) -> bool:
    """True when this card is due for another lazy post (PC-off catch-up)."""
    now = time.time() if now is None else now
    with connect() as connection:
        row = connection.execute("SELECT last_post_at FROM cards WHERE card_id = ?",
                                 (card_id,)).fetchone()
    last = float(row["last_post_at"] or 0) if row else 0.0
    return now - last >= float(spacing_minutes) * 60


# --------------------------------------------------------------------- reading
def _row_to_item(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("images", "tags"):
        try:
            item[key] = json.loads(item[key])
        except (TypeError, ValueError):
            item[key] = []
    item["mature"] = bool(item.get("mature"))
    return item


def items(status: str | None = None, limit: int = 200, card_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM items"
    clauses, args = [], []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if card_id:
        clauses.append("card_id = ?")
        args.append(card_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY due_at ASC, id ASC LIMIT ?"
    args.append(int(limit))
    with connect() as connection:
        rows = connection.execute(query, args).fetchall()
    return [item for item in (_row_to_item(row) for row in rows) if item]


def item(item_id: int) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM items WHERE id = ?", (int(item_id),)).fetchone()
    return _row_to_item(row)


def due_items(now: float | None = None, limit: int = 50) -> list[dict]:
    now = time.time() if now is None else now
    with connect() as connection:
        rows = connection.execute(
            """SELECT * FROM items WHERE status = 'pending' AND due_at <= ?
               ORDER BY due_at ASC, id ASC LIMIT ?""",
            (now, int(limit)),
        ).fetchall()
    return [entry for entry in (_row_to_item(row) for row in rows) if entry]


def queue_payload(now: float | None = None, limit: int = 200) -> dict:
    """Everything the Queue tab shows: items per card, plus the counters."""
    now = time.time() if now is None else now
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM items WHERE status IN ('pending', 'failed', 'missing') "
            "ORDER BY due_at ASC, id ASC LIMIT ?", (int(limit),)).fetchall()
    entries = [entry for entry in (_row_to_item(row) for row in rows) if entry]
    cards: dict[str, dict] = {}
    for entry in entries:
        group = cards.setdefault(entry["card_id"], {
            "card_id": entry["card_id"], "items": [], "next_due": None, "pending": 0,
        })
        group["items"].append(entry)
        if entry["status"] == "pending":
            group["pending"] += 1
            if group["next_due"] is None:
                group["next_due"] = entry["due_at"]
    with connect() as connection:
        counts = {
            "pending": connection.execute(
                "SELECT COUNT(*) FROM items WHERE status = 'pending'").fetchone()[0],
            "failed": connection.execute(
                "SELECT COUNT(*) FROM items WHERE status = 'failed'").fetchone()[0],
            "missing": connection.execute(
                "SELECT COUNT(*) FROM items WHERE status = 'missing'").fetchone()[0],
            "needs_you": connection.execute(
                "SELECT COUNT(*) FROM needs_you WHERE status = 'open'").fetchone()[0],
        }
    return {"now": now, "cards": list(cards.values()), "counts": counts}


# -------------------------------------------------------------------- updating
def mark_posted(item_id: int, url: str = "", note: str = "",
                now: float | None = None) -> None:
    now = time.time() if now is None else now
    with connect() as connection:
        row = connection.execute("SELECT card_id FROM items WHERE id = ?",
                                 (int(item_id),)).fetchone()
        connection.execute(
            """UPDATE items SET status = 'posted', url = ?, note = ?, error = '',
                      due_at = ? WHERE id = ?""",
            (str(url), str(note), now, int(item_id)),
        )
        if row is not None:
            connection.execute(
                """INSERT INTO cards (card_id, last_post_at, last_slot) VALUES (?, ?, 0)
                   ON CONFLICT(card_id) DO UPDATE SET last_post_at = MAX(last_post_at, ?)""",
                (str(row["card_id"]), now, now),
            )


def mark_failed(item_id: int, error: str) -> None:
    with connect() as connection:
        connection.execute(
            """UPDATE items SET status = 'failed', error = ?, attempts = attempts + 1
               WHERE id = ?""",
            (str(error)[:500], int(item_id)),
        )


def retry_item(item_id: int, now: float | None = None) -> bool:
    """Put a failed item back in the queue, due now."""
    now = time.time() if now is None else now
    with connect() as connection:
        cursor = connection.execute(
            """UPDATE items SET status = 'pending', due_at = ?, error = ''
               WHERE id = ? AND status = 'failed'""",
            (now, int(item_id)),
        )
    return cursor.rowcount > 0


def skip_item(item_id: int) -> bool:
    with connect() as connection:
        cursor = connection.execute("UPDATE items SET status = 'skipped' WHERE id = ?",
                                    (int(item_id),))
    return cursor.rowcount > 0


def remove_item(item_id: int) -> bool:
    with connect() as connection:
        cursor = connection.execute("DELETE FROM items WHERE id = ?", (int(item_id),))
    return cursor.rowcount > 0


def set_item_files(item_id: int, images: list[str], *, status: str | None = None,
                   error: str | None = None) -> bool:
    """Replace an item's images (and cover); optionally change its status and error."""
    images = [str(path) for path in images]
    fields = ["images = ?", "cover = ?"]
    args: list = [json.dumps(images), images[0] if images else ""]
    if status is not None:
        fields.append("status = ?")
        args.append(str(status))
    if error is not None:
        fields.append("error = ?")
        args.append(str(error)[:500])
    args.append(int(item_id))
    with connect() as connection:
        cursor = connection.execute(
            f"UPDATE items SET {', '.join(fields)} WHERE id = ?", args)
    return cursor.rowcount > 0


def set_needs_you_files(entry_id: int, images: list[str], *, note: str | None = None,
                        status: str | None = None) -> bool:
    """Replace a 'Needs you' entry's images; optionally its note or status."""
    images = [str(path) for path in images]
    fields = ["images = ?", "cover = ?"]
    args: list = [json.dumps(images), images[0] if images else ""]
    if note is not None:
        fields.append("note = ?")
        args.append(str(note)[:300])
    if status is not None:
        fields.append("status = ?")
        args.append(str(status))
    args.append(int(entry_id))
    with connect() as connection:
        cursor = connection.execute(
            f"UPDATE needs_you SET {', '.join(fields)} WHERE id = ?", args)
    return cursor.rowcount > 0


def update_caption(item_id: int, caption: str, tags: list[str] | None = None) -> bool:
    with connect() as connection:
        if tags is None:
            cursor = connection.execute("UPDATE items SET caption = ? WHERE id = ?",
                                        (str(caption), int(item_id)))
        else:
            cursor = connection.execute(
                "UPDATE items SET caption = ?, tags = ? WHERE id = ?",
                (str(caption), json.dumps([str(tag) for tag in tags]), int(item_id)),
            )
    return cursor.rowcount > 0


# ------------------------------------------------------------------- dedupe
def remember_files(paths_list, status: str = "queued", now: float | None = None) -> None:
    """Record a file's content, so a *changed* file at the same path counts as new."""
    now = time.time() if now is None else now
    with connect() as connection:
        for path in paths_list:
            path = Path(path)
            try:
                stat = path.stat()
                digest = lazy.digest(path)
            except OSError:
                continue
            connection.execute(
                """INSERT INTO files (path, digest, size, mtime, status, seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET digest = excluded.digest,
                       size = excluded.size, mtime = excluded.mtime,
                       status = excluded.status, seen_at = excluded.seen_at""",
                (str(path), digest, int(stat.st_size), float(stat.st_mtime), str(status), now),
            )


def known_files() -> dict[str, dict]:
    """Every file path already seen: its recorded digest, size and status."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT path, digest, size, mtime, status FROM files").fetchall()
    return {
        str(row["path"]): {
            "digest": str(row["digest"]),
            "size": int(row["size"] or 0),
            "mtime": float(row["mtime"] or 0),
            "status": str(row["status"]),
        }
        for row in rows
    }


def forget_files(paths_list) -> None:
    """Free these paths again (the files moved away, so nothing owns them)."""
    with connect() as connection:
        for path in paths_list:
            connection.execute("DELETE FROM files WHERE path = ?", (str(path),))


def mark_files(paths_list, status: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with connect() as connection:
        for path in paths_list:
            connection.execute("UPDATE files SET status = ?, seen_at = ? WHERE path = ?",
                               (str(status), now, str(path)))


def already_posted(digest_value: str, card_id: str) -> bool:
    with connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM posted WHERE digest = ? AND card_id = ?",
            (str(digest_value), str(card_id)),
        ).fetchone()
    return row is not None


def remember_posted(digest_value: str, card_id: str, url: str = "",
                    now: float | None = None) -> None:
    now = time.time() if now is None else now
    with connect() as connection:
        connection.execute(
            """INSERT INTO posted (digest, card_id, url, posted_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(digest, card_id) DO UPDATE SET url = excluded.url,
                   posted_at = excluded.posted_at""",
            (str(digest_value), str(card_id), str(url), now),
        )


# ------------------------------------------------------------- AI retry budget
AI_BACKOFF_MINUTES = (1.0, 5.0, 15.0)  # waits before each retry, then: Needs you


def ai_attempts(key: str) -> int:
    with connect() as connection:
        row = connection.execute("SELECT attempts FROM ai_retries WHERE key = ?",
                                 (str(key),)).fetchone()
    return int(row["attempts"]) if row else 0


def ai_ready(key: str, now: float | None = None) -> bool:
    """True when this key may ask the AI again (no failures, or its wait is over)."""
    now = time.time() if now is None else now
    with connect() as connection:
        row = connection.execute("SELECT next_at FROM ai_retries WHERE key = ?",
                                 (str(key),)).fetchone()
    return row is None or float(row["next_at"] or 0) <= now


def ai_available(key: str) -> bool:
    """False once every retry was spent: only a human can move it on now."""
    return ai_attempts(key) < len(AI_BACKOFF_MINUTES)


def ai_failed(key: str, now: float | None = None) -> int:
    """Record one transient failure and set the next try; returns the attempt count."""
    now = time.time() if now is None else now
    attempts = ai_attempts(key) + 1
    wait = AI_BACKOFF_MINUTES[min(attempts, len(AI_BACKOFF_MINUTES)) - 1] * 60
    with connect() as connection:
        connection.execute(
            """INSERT INTO ai_retries (key, attempts, next_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET attempts = excluded.attempts,
                   next_at = excluded.next_at, updated_at = excluded.updated_at""",
            (str(key), attempts, now + wait, now),
        )
    return attempts


def ai_reset(key: str) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM ai_retries WHERE key = ?", (str(key),))


def ai_forget_prefix(prefix: str) -> None:
    """Clear the budget for every key starting with this prefix (a retry button)."""
    with connect() as connection:
        connection.execute("DELETE FROM ai_retries WHERE key LIKE ?", (f"{prefix}%",))


# ------------------------------------------------------------------ identities
def identity(digest_value: str) -> dict | None:
    """What the AI said about this image before, or None (M6.1 auto-split cache)."""
    with connect() as connection:
        row = connection.execute("SELECT * FROM identities WHERE digest = ?",
                                 (str(digest_value),)).fetchone()
    if row is None:
        return None
    entry = dict(row)
    try:
        entry["characters"] = json.loads(entry["characters"])
    except (TypeError, ValueError):
        entry["characters"] = []
    entry["confident"] = bool(entry.get("confident"))
    return entry


def remember_identity(digest_value: str, *, characters, series: str, confident: bool,
                      model: str = "", source: str = "ai",
                      now: float | None = None) -> None:
    """`source` is "ai", or "filename" when the artist's own file name named it."""
    now = time.time() if now is None else now
    with connect() as connection:
        connection.execute(
            """INSERT INTO identities (digest, characters, series, confident, model, source,
                   asked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(digest) DO UPDATE SET characters = excluded.characters,
                   series = excluded.series, confident = excluded.confident,
                   model = excluded.model, source = excluded.source,
                   asked_at = excluded.asked_at""",
            (str(digest_value), json.dumps([str(name) for name in characters or []]),
             str(series or ""), 1 if confident else 0, str(model or ""), str(source or "ai"),
             now),
        )


def known_characters() -> list[str]:
    """Every character name the AI was sure about (the "named someone else" check)."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT characters FROM identities WHERE confident = 1").fetchall()
    names: dict[str, str] = {}
    for row in rows:
        try:
            values = json.loads(row["characters"])
        except (TypeError, ValueError):
            continue
        for name in values if isinstance(values, list) else []:
            text = str(name or "").strip()
            if text:
                names.setdefault(text.casefold(), text)
    return sorted(names.values())


# -------------------------------------------------------------------- batches
def add_batch(batch_key: str, source: str, folder_key: str, batch_files,
              now: float | None = None) -> None:
    now = time.time() if now is None else now
    with connect() as connection:
        connection.execute(
            """INSERT INTO batches (batch_key, source, folder_key, files, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(batch_key) DO UPDATE SET files = excluded.files""",
            (str(batch_key), str(source), str(folder_key),
             json.dumps([str(path) for path in batch_files]), now),
        )


def batch(batch_key: str) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM batches WHERE batch_key = ?",
                                 (str(batch_key),)).fetchone()
    if row is None:
        return None
    entry = dict(row)
    try:
        entry["files"] = json.loads(entry["files"])
    except (TypeError, ValueError):
        entry["files"] = []
    return entry


def batch_pending(batch_key: str) -> int:
    with connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM items WHERE batch_key = ? AND status IN ('pending', 'failed')",
            (str(batch_key),),
        ).fetchone()
    return int(row[0])


def batch_links(batch_key: str) -> list[str]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT url FROM items WHERE batch_key = ? AND status = 'posted' AND url != ''",
            (str(batch_key),),
        ).fetchall()
    return [str(row["url"]) for row in rows]


def batch_characters(batch_key: str) -> list[str]:
    """The distinct set labels of a batch's items (the posted folder's name)."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT DISTINCT character FROM items WHERE batch_key = ? AND character != ''",
            (str(batch_key),),
        ).fetchall()
    return [str(row["character"]) for row in rows]


def mark_batch_moved(batch_key: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with connect() as connection:
        connection.execute("UPDATE batches SET moved_at = ? WHERE batch_key = ?",
                           (now, str(batch_key)))


# ------------------------------------------------------------------ needs you
def add_needs_you(*, batch_key: str, source: str, folder_key: str, kind: str,
                  images, cover: str = "", caption: str = "", tags=None, alt_text: str = "",
                  character: str = "", series: str = "", platforms: dict | None = None,
                  mature: bool = False, note: str = "", now: float | None = None) -> int:
    """A thing only a human can finish. `note` says why, when it was the AI's fault."""
    now = time.time() if now is None else now
    with connect() as connection:
        cursor = connection.execute(
            """INSERT INTO needs_you (batch_key, source, folder_key, kind, images, cover,
                   caption, tags, alt_text, character, series, platforms, mature, note,
                   created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(batch_key), str(source), str(folder_key), str(kind),
             json.dumps([str(path) for path in images]), str(cover or ""), str(caption),
             json.dumps([str(tag) for tag in tags or []]), str(alt_text), str(character),
             str(series), json.dumps(platforms or {}), 1 if mature else 0, str(note)[:300],
             now),
        )
        return int(cursor.lastrowid)


def needs_you(status: str = "open", limit: int = 100) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM needs_you WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (str(status), int(limit)),
        ).fetchall()
    out = []
    for row in rows:
        entry = dict(row)
        for key in ("images", "tags"):
            try:
                entry[key] = json.loads(entry[key])
            except (TypeError, ValueError):
                entry[key] = []
        try:
            entry["platforms"] = json.loads(entry["platforms"])
        except (TypeError, ValueError):
            entry["platforms"] = {}
        entry["mature"] = bool(entry.get("mature"))
        out.append(entry)
    return out


def needs_you_entry(entry_id: int) -> dict | None:
    entries = needs_you("open", limit=1)
    for entry in entries:
        if int(entry["id"]) == int(entry_id):
            return entry
    with connect() as connection:
        row = connection.execute("SELECT * FROM needs_you WHERE id = ?",
                                 (int(entry_id),)).fetchone()
    if row is None:
        return None
    entry = dict(row)
    for key in ("images", "tags"):
        try:
            entry[key] = json.loads(entry[key])
        except (TypeError, ValueError):
            entry[key] = []
    try:
        entry["platforms"] = json.loads(entry["platforms"])
    except (TypeError, ValueError):
        entry["platforms"] = {}
    return entry


def open_needs_you(batch_key: str) -> int:
    """How many 'Needs you' entries of this batch are still waiting."""
    with connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM needs_you WHERE batch_key = ? AND status = 'open'",
            (str(batch_key),),
        ).fetchone()
    return int(row[0])


def resolve_needs_you(entry_id: int, status: str = "used", now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with connect() as connection:
        cursor = connection.execute("UPDATE needs_you SET status = ? WHERE id = ?",
                                    (str(status), int(entry_id)))
    return cursor.rowcount > 0
