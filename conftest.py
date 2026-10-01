"""Make the `app` package importable when pytest runs from the project root."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import paths  # noqa: E402 — needs the path above
from app.core import lazy  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    """Tests must never write into the project's data/ folder (the app shares it)."""
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    monkeypatch.setattr(paths, "UPLOADS", tmp_path / "data" / "uploads")
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "data" / "history.sqlite")
    monkeypatch.setattr(paths, "QUEUE_PATH", tmp_path / "data" / "queue.sqlite")
    # …and never into the user's real Pictures folder either.
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "Pictures" / "ArtDrop")


REAL_ARRIVED = lazy.arrived


@pytest.fixture(autouse=True)
def arrival_is_the_modified_time(monkeypatch):
    """Tests date files with os.utime, which cannot set Windows' creation time:
    the faked modified time stands for when the file arrived."""
    monkeypatch.setattr(lazy, "arrived", lambda path: float(path.stat().st_mtime))
