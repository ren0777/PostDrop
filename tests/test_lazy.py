"""Lazy mode (M6): settings, folder scanning, batches and routing."""
import os
import time
from pathlib import Path

from app.core import lazy, settings


def _drop(folder: Path, name: str, when: float, body: bytes = b"x") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(body)
    path.touch()
    import os
    os.utime(path, (when, when))
    return path


# ----------------------------------------------------------------- settings
def test_defaults_are_the_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    data = lazy.stored()
    assert data["enabled"] is False and data["paused"] is False
    folders = {folder["key"]: folder for folder in data["folders"]}
    assert set(folders) == {"outbox", "outbox-instagram"}
    assert "instagram:main" not in folders["outbox"]["cards"]
    assert "instagram:main" in folders["outbox-instagram"]["cards"]
    assert folders["outbox"]["mature"] is True
    assert folders["outbox-instagram"]["mature"] is False


def test_spacing_can_never_go_below_thirty_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    data = lazy.save({"sites": {"bluesky": {"per_post": 4, "spacing_minutes": 5}}})
    assert data["sites"]["bluesky"]["spacing_minutes"] == lazy.MIN_SPACING_MINUTES
    assert data["sites"]["bluesky"]["per_post"] == 4


def test_settings_survive_a_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    lazy.save({"enabled": True, "enabled_folders": None,
               "quiet": {"enabled": True, "start": 23, "end": 7}})
    again = lazy.stored()
    assert again["enabled"] is True
    assert again["quiet"] == {"enabled": True, "start": 23, "end": 7}
    assert settings.load()["lazy"]["enabled"] is True


def test_relative_paths_resolve_under_the_pictures_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    assert lazy.resolve("outbox") == tmp_path / "ArtDrop" / "outbox"
    absolute = str(tmp_path / "elsewhere")
    assert lazy.resolve(absolute) == Path(absolute)


def test_ensure_folders_creates_them(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    created = lazy.ensure_folders()
    assert (tmp_path / "ArtDrop" / "outbox").is_dir()
    assert (tmp_path / "ArtDrop" / "outbox-instagram").is_dir()
    assert (tmp_path / "ArtDrop" / "posted").is_dir()
    assert len(created) == 2


# ------------------------------------------------------------------ batches
def test_files_dropped_together_form_one_batch(tmp_path):
    folder = tmp_path / "outbox"
    _drop(folder, "02_b.png", 1_000)
    _drop(folder, "01_a.png", 990)
    _drop(folder, "03_c.png", 1_010)

    batches = lazy.batches(folder, now=1_100, settle=0)

    assert len(batches) == 1
    assert [path.name for path in batches[0]] == ["01_a.png", "02_b.png", "03_c.png"]


def test_a_late_file_starts_a_second_batch(tmp_path):
    folder = tmp_path / "outbox"
    _drop(folder, "a.png", 1_000)
    _drop(folder, "b.png", 1_000 + lazy.BATCH_WINDOW_SECONDS + 5)

    batches = lazy.batches(folder, now=2_000, settle=0)

    assert [[path.name for path in batch] for batch in batches] == [["a.png"], ["b.png"]]


def test_each_subfolder_is_its_own_batch(tmp_path):
    folder = tmp_path / "outbox"
    _drop(folder, "cover.png", 1_000)
    _drop(folder / "Ichika set", "01.png", 1_001)
    _drop(folder / "Ichika set", "02.png", 1_002)

    batches = lazy.batches(folder, now=1_100, settle=0)

    assert [len(batch) for batch in batches] == [1, 2]  # oldest batch first
    assert all(len({path.parent for path in batch}) == 1 for batch in batches)


def test_a_fresh_file_waits_for_the_settling_time(tmp_path):
    folder = tmp_path / "outbox"
    _drop(folder, "a.png", 1_000)

    assert lazy.batches(folder, now=1_000 + lazy.SETTLE_SECONDS - 1, settle=30) == []
    assert len(lazy.batches(folder, now=1_000 + lazy.SETTLE_SECONDS + 1, settle=30)) == 1


def test_temp_and_posted_files_are_ignored(tmp_path):
    folder = tmp_path / "outbox"
    _drop(folder, "a.png", 1_000)
    _drop(folder, ".hidden.png", 1_000)
    _drop(folder, "b.png.part", 1_000)
    _drop(folder, "notes.txt", 1_000)
    _drop(folder / "posted", "old.png", 1_000)

    names = [path.name for batch in lazy.batches(folder, now=2_000, settle=0) for path in batch]
    assert names == ["a.png"]


def test_batch_key_is_stable_and_content_based(tmp_path):
    folder = tmp_path / "outbox"
    cover = _drop(folder, "cover.png", 1_000, b"one")
    batch = [cover]
    first = lazy.batch_key(folder, batch)
    assert first == lazy.batch_key(folder, batch)
    cover.write_bytes(b"two")
    assert lazy.batch_key(folder, batch) != first


# ------------------------------------------------------------- chunking
def test_chunking_matches_the_plan(tmp_path):
    images = [tmp_path / f"{index:02d}.png" for index in range(1, 21)]
    assert len(lazy.chunks(images, 10)) == 2   # Telegram, Discord, Tumblr, Instagram
    assert len(lazy.chunks(images, 4)) == 5    # Bluesky
    assert len(lazy.chunks(images, 1)) == 20   # DeviantArt
    assert len(lazy.chunks(images, 10)[0]) == 10


def test_chunks_keep_the_order_and_never_go_empty(tmp_path):
    images = [tmp_path / f"{index}.png" for index in range(3)]
    parts = lazy.chunks(images, 2)
    assert [[path.name for path in part] for part in parts] == [["0.png", "1.png"], ["2.png"]]
    assert lazy.chunks([], 5) == []
    assert len(lazy.chunks(images, 0)) == 3  # a silly per_post still posts one at a time


# ---------------------------------------------------------------- moving
def test_a_finished_batch_moves_to_posted(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    folder = tmp_path / "ArtDrop" / "outbox"
    _drop(folder, "cover.png", 1_000)

    target = lazy.move_to_posted([folder / "cover.png"])

    assert not (folder / "cover.png").exists()
    assert (target / "cover.png").exists()
    assert target.parent.name == "posted"


def test_posted_sets_keep_their_folder_name(tmp_path, monkeypatch):
    """A subfolder (or character) set gets `posted\\<date>\\<name>\\` (review rule 1)."""
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    folder = tmp_path / "ArtDrop" / "outbox"
    _drop(folder / "Changli (2)", "01.png", 1_000)

    target = lazy.move_to_posted([folder / "Changli (2)" / "01.png"], name="Changli (2)")

    assert target.name == "Changli (2)" and (target / "01.png").exists()
    assert target.parent.parent.name == "posted"
    # the same character posts again the same day: -2, never a mix-up
    _drop(folder / "Changli (2)", "02.png", 1_000)
    again = lazy.move_to_posted([folder / "Changli (2)" / "02.png"], name="Changli (2)")
    assert again.name == "Changli (2)-2" and (again / "02.png").exists()


def test_emptied_folders_are_freed_safely(tmp_path, monkeypatch):
    """Review rule 2: an emptied set folder goes, the outbox root and `_` folders stay."""
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    root = tmp_path / "ArtDrop" / "outbox"
    set_folder = root / "Changli (2)"
    set_folder.mkdir(parents=True)
    (root / "_notes").mkdir()

    removed = lazy.remove_if_empty({set_folder}, roots=[root])

    assert removed == [set_folder] and not set_folder.exists()
    assert root.is_dir()  # never a configured root
    assert lazy.remove_if_empty({root / "_notes"}, roots=[]) == []
    assert (root / "_notes").is_dir()  # never an `_` folder
    # a folder with a stray file keeps its place
    _drop(root / "Kafka", "01.png", 1_000)
    assert lazy.remove_if_empty({root / "Kafka"}, roots=[]) == []
    assert (root / "Kafka" / "01.png").exists()


# ------------------------------------------------- splitting (M6.1)
def test_names_are_normalized_for_grouping():
    assert lazy.normalize_name("Stelle") == "stelle"
    assert lazy.normalize_name("Stelle!") == lazy.normalize_name("STELLE")
    assert lazy.normalize_name("The Traveler") == "traveler"  # spaces, punctuation, "the"
    assert lazy.normalize_name("中野二乃") == "中野二乃"


def test_the_group_key_is_the_series_plus_sorted_characters():
    one = lazy.group_key("Honkai: Star Rail", ["Stelle"])
    assert one == lazy.group_key("honkai star rail", ["stelle "])
    assert one == lazy.group_key("The Honkai: Star Rail", ["Stelle"])  # "the" is dropped
    # a duo is its own key, in any order
    duo = lazy.group_key("Genshin Impact", ["Nahida", "Wanderer"])
    assert duo == lazy.group_key("Genshin Impact", ["Wanderer", "Nahida"])
    assert duo != lazy.group_key("Genshin Impact", ["Nahida"])
    assert lazy.group_key("", ["Stelle"]) != lazy.group_key("Honkai: Star Rail", ["Stelle"])


def test_the_set_key_carries_the_group(tmp_path):
    folder = tmp_path / "outbox"
    image = _drop(folder, "01.png", 1_000)
    first = lazy.set_key(folder, [image], "stelle")
    assert first == lazy.set_key(folder, [image], "stelle")
    assert first != lazy.set_key(folder, [image], "nami")
    assert first != lazy.set_key(folder, [image])  # an explicit set has its own key


def test_a_caption_txt_is_a_draft_by_default(tmp_path):
    folder = tmp_path / "set"
    folder.mkdir(parents=True)
    (folder / "caption.txt").write_text("Good morning ☀️", encoding="utf-8")

    assert lazy.caption_draft(folder) == ("Good morning ☀️", False)
    assert lazy.caption_draft(tmp_path / "nowhere") == ("", False)


def test_the_exact_marker_is_stripped_and_switches_the_mode(tmp_path):
    folder = tmp_path / "set"
    folder.mkdir(parents=True)
    (folder / "caption.txt").write_text("  !ExAcT\nGood morning ☀️\n", encoding="utf-8")

    text, exact = lazy.caption_draft(folder)

    assert exact is True
    assert text == "Good morning ☀️"  # the marker line is gone, the rest untouched
    assert "!exact" not in text


def test_the_marker_only_counts_on_the_first_line(tmp_path):
    folder = tmp_path / "set"
    folder.mkdir(parents=True)
    (folder / "caption.txt").write_text("Hello!\n!exact", encoding="utf-8")

    assert lazy.caption_draft(folder) == ("Hello!\n!exact", False)


def test_caption_txt_is_a_sidecar_not_an_image(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    folder = tmp_path / "outbox" / "set"
    _drop(folder, "01.png", 1_000)
    (folder / "caption.txt").write_text("words", encoding="utf-8")

    assert [path.name for path in lazy.sidecars(folder)] == ["caption.txt"]
    assert [path.name for path in lazy.files(folder)] == ["01.png"]
    assert lazy.unusable(folder) == []  # a caption.txt is expected, not stuck


def test_a_folder_name_becomes_the_character():
    # explicit counter forms are stripped, once
    assert lazy.artist_name("Changli (2)") == "Changli"
    assert lazy.artist_name("Changli [2]") == "Changli"
    assert lazy.artist_name("Changli #2") == "Changli"
    assert lazy.artist_name("Changli_2") == "Changli"
    assert lazy.artist_name("Changli-2") == "Changli"
    assert lazy.artist_name("Changli v2") == "Changli"
    assert lazy.artist_name("Android 18 (2)") == "Android 18"
    # a plain space-separated number may be part of the name
    assert lazy.artist_name("Android 18") == "Android 18"
    assert lazy.artist_name("Area 51") == "Area 51"
    assert lazy.artist_name("Changli 2") == "Changli 2"
    assert lazy.artist_name("Changli") == "Changli"
    assert lazy.artist_name("March 7th") == "March 7th"
    assert lazy.artist_name("2B") == "2B"
    assert lazy.artist_name("Kafka") == "Kafka"


def test_mix_folders_are_recognised():
    for name in ("mix", "Mix", "MIXED", "mix-morning", "mixed_set", "mixed-2"):
        assert lazy.is_mix_folder(name) is True, name
    for name in ("Miku", "mixer", "mixes", "Golden Mix"):
        assert lazy.is_mix_folder(name) is False, name


def test_underscore_folders_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    folder = tmp_path / "outbox"
    _drop(folder / "_EXAMPLE Character Name", "01.png", 1_000)
    _drop(folder / "Kafka", "01.png", 1_000)

    assert [path.parent.name for path in lazy.files(folder)] == ["Kafka"]
    assert lazy.unusable(folder) == []
    assert lazy.is_ignored_dir("_notes") is True and lazy.is_ignored_dir("notes") is False


def test_a_folder_name_can_carry_the_series():
    assert lazy.folder_identity("Citlali- genshin impact") == ("Citlali", "Genshin Impact")
    assert lazy.folder_identity("Citlali - Genshin Impact") == ("Citlali", "Genshin Impact")
    assert lazy.folder_identity("Citlali -Genshin Impact") == ("Citlali", "Genshin Impact")
    assert lazy.folder_identity("Citlali - Genshin Impact (2)") == ("Citlali", "Genshin Impact")
    assert lazy.folder_identity("Kafka - honkai: star rail") == ("Kafka", "Honkai: Star Rail")
    assert lazy.folder_identity("Nino - The Quintessential Quintuplets") == (
        "Nino", "The Quintessential Quintuplets")  # the artist's own casing is kept
    assert lazy.folder_identity("Ellen - ZZZ") == ("Ellen", "ZZZ")
    # hyphens without spaces are part of the name, never a split
    assert lazy.folder_identity("Hu-Tao") == ("Hu Tao", "")
    assert lazy.folder_identity("Changli-2") == ("Changli", "")
    assert lazy.folder_identity("March 7th") == ("March 7th", "")
    assert lazy.folder_identity("Changli - 2") == ("Changli 2", "")  # a number is no series
    assert lazy.artist_name("Citlali- genshin impact") == "Citlali"


def test_a_subfolder_is_one_batch_however_old_its_files_are(tmp_path):
    """Issue #3: 12 images with dates a month apart were cut into ~8 sets."""
    folder = tmp_path / "outbox"
    sub = folder / "mix 1"
    sub.mkdir(parents=True)
    now = time.time()
    for index, age_days in enumerate((33, 33, 32, 28, 22, 2)):
        path = sub / f"{index}.png"
        path.write_bytes(b"x%d" % index)
        stamp = now - age_days * 86400
        os.utime(path, (stamp, stamp))

    found = lazy.batches(folder, now=now)

    assert [[path.name for path in batch] for batch in found] == [
        ["0.png", "1.png", "2.png", "3.png", "4.png", "5.png"]]


def test_a_copied_file_arrives_when_it_was_copied(tmp_path):
    from conftest import REAL_ARRIVED

    path = tmp_path / "old.png"
    path.write_bytes(b"x")
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))  # a copy keeps its old modified date…

    assert REAL_ARRIVED(path) > time.time() - 60  # …but it landed just now
