"""Filesystem locations and the local server address."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "history.sqlite"
QUEUE_PATH = DATA / "queue.sqlite"

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/"

UPLOAD_RETENTION_DAYS = 7


def ensure_dirs() -> None:
    """Create the data folders if they are missing."""
    UPLOADS.mkdir(parents=True, exist_ok=True)


def upload_dir(post_id: str) -> Path:
    return UPLOADS / post_id


def clean_old_uploads(days: int = UPLOAD_RETENTION_DAYS) -> int:
    """Delete upload folders untouched for `days` days. Returns how many went."""
    if not UPLOADS.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for folder in UPLOADS.iterdir():
        try:
            if not folder.is_dir() or _newest_mtime(folder) >= cutoff:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _newest_mtime(folder: Path) -> float:
    newest = folder.stat().st_mtime
    for child in folder.iterdir():
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return newest
