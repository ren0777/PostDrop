"""Upload folder retention: old ones are removed, fresh ones are kept."""
import os
import time

from app.core import paths


def test_clean_old_uploads_removes_only_old_folders(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    old = uploads / "oldpost"
    fresh = uploads / "freshpost"
    old.mkdir(parents=True)
    fresh.mkdir()
    (old / "01_art.png").write_bytes(b"old")
    (fresh / "01_art.png").write_bytes(b"new")

    stale = time.time() - 8 * 86400
    for target in (old, old / "01_art.png"):
        os.utime(target, (stale, stale))

    monkeypatch.setattr(paths, "UPLOADS", uploads)

    assert paths.clean_old_uploads() == 1
    assert not old.exists()
    assert fresh.exists()


def test_clean_old_uploads_without_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "UPLOADS", tmp_path / "missing")
    assert paths.clean_old_uploads() == 0
