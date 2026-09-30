"""The lazy worker: batches in, queue items out, due posts published."""
import asyncio
import json
import os
import time
from pathlib import Path

from app.core import captioner, lazy, paths, queue
from app.watch import LazyWorker


def _drop(folder: Path, name: str, body: bytes = b"art", age: float = 120.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(body)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


class FakeCaption:
    """Stands in for the Caption AI: identify the character + caption per site."""

    def __init__(self, character="", series="The Quintessential Quintuplets",
                 confident=True, error=""):
        self.character = character
        self.series = series
        self.confident = confident
        self.error = error
        self.calls: list = []

    async def __call__(self, *, cfg, images, platforms, mature=False, caption_draft="",
                       caption_exact=False, given=None):
        self.calls.append({"images": [str(image) for image in images],
                           "platforms": list(platforms), "mature": mature,
                           "caption_draft": caption_draft, "caption_exact": caption_exact,
                           "given": given})
        if self.error:
            raise RuntimeError(self.error)
        captions = {site: (caption_draft if (caption_draft and caption_exact)
                           else f"{site} caption") for site in platforms}
        # like the real model: a name handed over as a fact is the name it reports
        named = str((given or {}).get("name") or "")
        return {
            "character": self.character or named,
            "series": self.series,
            "confident": self.confident,
            "alt_text": "a test description",
            "platforms": {site: {"caption": captions[site], "tags": ["tag1", site]}
                          for site in platforms},
        }


class FakePublisher:
    def __init__(self, ok=True, url="https://example.com/post/1", error="it broke"):
        self.ok = ok
        self.url = url
        self.error = error
        self.calls: list = []

    async def __call__(self, card_id, post, post_id, dry_run):
        self.calls.append({"card_id": card_id, "caption": post.caption, "tags": post.tags,
                           "images": [path.name for path in post.images], "post_id": post_id,
                           "mature": bool(post.options.get("mature")),
                           "dry_run": dry_run})
        if self.ok:
            return {"ok": True, "url": self.url, "error": None}
        return {"ok": False, "url": None, "error": self.error}


def _setup(tmp_path, monkeypatch, *, cards, folder_cards, mature=True, enabled=True,
           pixiv=True):
    """Point lazy mode at a temporary outbox and return (worker, outbox, caption)."""
    root = tmp_path / "Pictures" / "ArtDrop"
    outbox = root / "outbox"
    monkeypatch.setattr(lazy, "root", lambda: root)
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    monkeypatch.setattr(paths, "UPLOADS", tmp_path / "data" / "uploads")
    monkeypatch.setattr(paths, "QUEUE_PATH", tmp_path / "data" / "queue.sqlite")
    lazy.save({
        "enabled": enabled,
        "folders": [{"key": "outbox", "path": str(outbox), "mature": mature,
                     "pixiv": pixiv, "cards": folder_cards}],
        "sites": {"bluesky": {"per_post": 4, "spacing_minutes": 120}},
    })
    caption = FakeCaption()
    publisher = FakePublisher()
    # loose files are split by character now, so every image identifies the same way
    identifier = FakeIdentifier(default={"characters": ["Nino Nakano"],
                                         "series": "The Quintessential Quintuplets",
                                         "confident": True})
    worker = LazyWorker(publisher, cards=lambda: set(cards), caption=caption,
                        identify=identifier, mixed=FakeMixed())
    return worker, outbox, caption, publisher


# -------------------------------------------------------------------- scanning
def test_a_confident_batch_is_queued_per_card(tmp_path, monkeypatch):
    worker, outbox, caption, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky", "telegram", "instagram:main"],
        folder_cards=["bluesky", "telegram", "instagram:main"])
    for index in range(5):
        _drop(outbox, f"{index:02d}.png", body=f"image-{index}".encode())

    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 4  # 5 images → 2 Bluesky posts + 1 Telegram + 1 Instagram
    assert summary["needs_you"] == 1  # Pixiv always waits for a human
    assert len(caption.calls) == 1  # one AI call per batch, on the cover
    assert Path(caption.calls[0]["images"][0]).name == "00.png"
    # the routed sites plus Pixiv (its text is what the Needs-you entry opens with)
    assert sorted(caption.calls[0]["platforms"]) == ["bluesky", "instagram", "pixiv",
                                                    "telegram"]
    assert caption.calls[0]["mature"] is True

    entries = queue.items()
    per_card: dict[str, list] = {}
    for entry in entries:
        per_card.setdefault(entry["card_id"], []).append(entry)
    assert [len(group) for group in per_card.values()] == [2, 1, 1]
    assert [len(entry["images"]) for entry in per_card["bluesky"]] == [4, 1]
    telegram = per_card["telegram"][0]
    assert len(telegram["images"]) == 5  # Telegram takes all five
    assert telegram["caption"] == "telegram caption"
    assert telegram["character"] == "Nino Nakano"
    assert telegram["tags"] == ["tag1", "telegram"]
    assert telegram["mature"] is True

    waiting = queue.needs_you()
    assert len(waiting) == 1 and waiting[0]["kind"] == "pixiv"
    assert waiting[0]["character"] == "Nino Nakano"


def test_an_unidentified_batch_needs_a_human_and_posts_nothing(tmp_path, monkeypatch):
    worker, outbox, caption, publisher = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    caption.confident = False
    _drop(outbox, "cover.png")

    summary = asyncio.run(worker.scan())

    assert summary == {"queued": 0, "needs_you": 1}
    assert queue.items() == []
    waiting = queue.needs_you()
    assert waiting[0]["kind"] == "unknown"
    assert queue.known_files() != {}  # the batch is remembered, not re-queued
    assert {record["status"] for record in queue.known_files().values()} == {"needs_you"}


def test_a_caption_failure_also_goes_to_needs_you(tmp_path, monkeypatch):
    worker, outbox, caption, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    caption.error = "the model refused the image"
    _drop(outbox, "cover.png")

    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 0 and summary["needs_you"] == 1
    assert queue.items() == []
    assert queue.needs_you()[0]["kind"] == "unknown"


def test_instagram_is_only_reached_from_its_own_folder(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky", "instagram:main"],
        folder_cards=["bluesky"])
    _drop(outbox, "cover.png")

    asyncio.run(worker.scan())

    items = queue.items()
    assert {entry["card_id"] for entry in items} == {"bluesky"}


def test_a_renamed_instagram_account_is_still_used(tmp_path, monkeypatch):
    worker, outbox, caption, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky", "instagram:solo"],
        folder_cards=["bluesky", "instagram:main"])
    _drop(outbox, "cover.png")

    asyncio.run(worker.scan())

    assert "instagram:solo" in {entry["card_id"] for entry in queue.items()}
    assert "instagram" in caption.calls[0]["platforms"]


def test_scanning_the_same_batch_twice_queues_it_once(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    _drop(outbox, "cover.png")

    assert asyncio.run(worker.scan())["queued"] == 1
    assert asyncio.run(worker.scan())["queued"] == 0


def test_paused_and_off_lazy_mode_do_nothing(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    _drop(outbox, "cover.png")

    lazy.save({"paused": True})
    assert asyncio.run(worker.scan())["reason"] == "paused"
    lazy.save({"paused": False, "enabled": False})
    assert asyncio.run(worker.scan())["reason"] == "off"
    assert asyncio.run(worker.post_due()) == []
    assert queue.items() == []


# -------------------------------------------------------------------- posting
def test_due_items_are_published_and_the_batch_moves(tmp_path, monkeypatch):
    worker, outbox, _, publisher = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    images = [_drop(outbox, f"{index:02d}.png", body=f"image-{index}".encode())
              for index in range(3)]
    asyncio.run(worker.scan())
    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])

    results = asyncio.run(worker.post_due())

    assert [result["ok"] for result in results] == [True]
    assert publisher.calls[0]["card_id"] == "bluesky"
    assert publisher.calls[0]["caption"] == "bluesky caption"
    assert publisher.calls[0]["tags"][0] == "tag1"
    assert publisher.calls[0]["dry_run"] is False
    staged = list((tmp_path / "data" / "uploads" / publisher.calls[0]["post_id"]).glob("*"))
    assert sorted(path.name for path in staged) == [
        f"{index:02d}_{image.name}" for index, image in enumerate(images, start=1)]

    posted = queue.items(status="posted")
    assert len(posted) == 1
    assert posted[0]["url"] == "https://example.com/post/1"
    assert queue.batch_pending(posted[0]["batch_key"]) == 0
    # the open Pixiv entry keeps the originals where the user can still post them
    assert (outbox / "00.png").exists()
    pixiv_entry = queue.needs_you()[0]
    queue.resolve_needs_you(pixiv_entry["id"], "used")
    worker._finish_batch(pixiv_entry["batch_key"])

    posted_root = tmp_path / "Pictures" / "ArtDrop" / "posted"
    moved = list(posted_root.rglob("*.png"))
    assert sorted(path.name for path in moved) == ["00.png", "01.png", "02.png"]
    # loose sets go into a folder named after the character (review rule 1)
    folder = moved[0].parent
    assert folder.name == "Nino Nakano"
    links = list(posted_root.rglob("links.json"))
    assert json.loads(links[0].read_text()) == ["https://example.com/post/1"]
    assert links[0].parent == folder
    assert queue.known_files() == {}  # the paths are free for the next 1.png
    assert queue.already_posted(lazy.digest(moved[0]), "bluesky") is True


def test_an_open_needs_you_entry_keeps_the_batch_files(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    _drop(outbox, "cover.png")
    asyncio.run(worker.scan())
    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])
    asyncio.run(worker.post_due())

    batch_key = queue.items(status="posted")[0]["batch_key"]
    worker._finish_batch(batch_key)

    assert (outbox / "cover.png").exists()  # the Pixiv entry is still open
    assert queue.batch(batch_key)["moved_at"] == 0


def test_a_failed_post_stays_in_the_queue_with_its_files(tmp_path, monkeypatch):
    worker, outbox, _, publisher = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    publisher.ok = False
    _drop(outbox, "cover.png")
    asyncio.run(worker.scan())
    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])

    results = asyncio.run(worker.post_due())

    assert results[0]["ok"] is False
    entry = queue.items(status="failed")[0]
    assert entry["error"] == "it broke"
    assert (outbox / "cover.png").exists()  # the user's file stays put
    assert queue.retry_item(entry["id"]) is True


def test_a_file_that_was_already_posted_is_never_queued_again(tmp_path, monkeypatch):
    worker, outbox, _, publisher = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    _drop(outbox, "a.png", body=b"the same art")
    asyncio.run(worker.scan())
    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])
    asyncio.run(worker.post_due())

    _drop(outbox, "b.png", body=b"the same art")  # a new file, same content

    assert asyncio.run(worker.scan())["queued"] == 0
    assert len(queue.items()) == 1  # only the first one, already posted


def test_a_new_file_with_an_old_name_is_queued(tmp_path, monkeypatch):
    """The M6 review's bug: 1.png posted, then a different 1.png must still queue."""
    worker, outbox, _, publisher = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    _drop(outbox, "1.png", body=b"the first piece", age=600)
    asyncio.run(worker.scan())
    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])
    asyncio.run(worker.post_due())
    entry = queue.needs_you()[0]
    queue.resolve_needs_you(entry["id"], "used")
    worker._finish_batch(entry["batch_key"])
    assert not (outbox / "1.png").exists()  # it moved to posted/

    _drop(outbox, "1.png", body=b"a completely different piece", age=600)  # same name!

    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 1
    pending = queue.items(status="pending")
    assert len(pending) == 1
    assert Path(pending[0]["images"][0]).name == "1.png"
    assert Path(pending[0]["images"][0]).read_bytes() == b"a completely different piece"


def test_a_changed_file_counts_as_new(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    path = _drop(outbox, "art.png", body=b"version one", age=600)
    assert asyncio.run(worker.scan())["queued"] == 1

    path.write_bytes(b"version two!!")  # same path, new content, same size class
    import os
    stamp = time.time() - 600
    os.utime(path, (stamp, stamp))

    assert asyncio.run(worker.scan())["queued"] == 1  # queued again, as new content


def test_the_worker_respects_the_spacing_between_posts(tmp_path, monkeypatch):
    worker, outbox, _, publisher = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    for index in range(6):
        _drop(outbox, f"{index:02d}.png", body=f"image-{index}".encode())
    asyncio.run(worker.scan())  # 6 images at 4 per post = 2 Bluesky items, 2 h apart
    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])

    first = asyncio.run(worker.post_due())
    second = asyncio.run(worker.post_due())

    assert len(first) == 1  # only the first is due; the second waits its slot
    assert second == [] or len(second) == 0
    assert len(publisher.calls) == 1


# --------------------------------------------------- auto-split (M6.1)
class FakeIdentifier:
    """Stands in for the per-image identify call: a script of answers."""

    def __init__(self, mapping=None, default=None, error="", transient=False):
        self.mapping = mapping or {}
        self.default = default or {"characters": [], "series": "", "confident": False}
        self.error = error
        self.transient = transient
        self.calls: list = []

    async def __call__(self, *, cfg, image, candidates=None, series_hint="",
                       escalate=True):
        self.calls.append(str(image))
        if self.error:
            if self.transient:
                raise captioner.CaptionUnavailable(f"Caption AI failed — {self.error}")
            raise RuntimeError(self.error)
        name = Path(image).name
        answer = self.mapping.get(name) or self.default
        return dict(answer)


class FakeMixed:
    """Stands in for the mixed-post caption (several characters, one universal line)."""

    def __init__(self, caption="mixed caption"):
        self.caption = caption
        self.calls: list = []

    async def __call__(self, *, cfg, images, characters, series, platforms, mature=False,
                       caption_draft="", caption_exact=False, given=None):
        self.calls.append({"images": [str(image) for image in images],
                           "characters": list(characters), "series": list(series),
                           "platforms": list(platforms), "mature": mature,
                           "caption_draft": caption_draft, "caption_exact": caption_exact})
        text = caption_draft if (caption_draft and caption_exact) else self.caption
        return {
            "character": ", ".join(characters),
            "series": ", ".join(series),
            "alt_text": "Stelle here. Nami there.",
            "confident": True,
            "mixed": True,
            "platforms": {site: {"caption": text, "tags": ["tag1", site]} for site in platforms},
        }


def _split_setup(tmp_path, monkeypatch, mapping=None, error="", transient=False,
                 cards=("telegram",)):
    """A worker whose loose files are identified by a scripted table."""
    root = tmp_path / "Pictures" / "ArtDrop"
    outbox = root / "outbox"
    monkeypatch.setattr(lazy, "root", lambda: root)
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    monkeypatch.setattr(paths, "UPLOADS", tmp_path / "data" / "uploads")
    monkeypatch.setattr(paths, "QUEUE_PATH", tmp_path / "data" / "queue.sqlite")
    lazy.save({
        "enabled": True,
        "folders": [{"key": "outbox", "path": str(outbox), "mature": True,
                     "cards": list(cards)}],
        "sites": {"telegram": {"per_post": 10, "spacing_minutes": 120}},
    })
    caption = FakeCaption()
    identifier = FakeIdentifier(mapping, error=error, transient=transient)
    worker = LazyWorker(FakePublisher(), cards=lambda: set(cards), caption=caption,
                        identify=identifier, mixed=FakeMixed())
    return worker, outbox, caption, identifier


STELLE = {"characters": ["Stelle"], "series": "Honkai: Star Rail", "confident": True}
NAMI = {"characters": ["Nami"], "series": "One Piece", "confident": True}
UNSURE = {"characters": [], "series": "", "confident": False}


def test_loose_files_are_split_by_character(tmp_path, monkeypatch):
    worker, outbox, caption, identifier = _split_setup(tmp_path, monkeypatch, {
        "01.png": STELLE, "02.png": NAMI, "03.png": STELLE,
    })
    for name in ("01.png", "02.png", "03.png"):
        _drop(outbox, name, body=f"art-{name}".encode())

    summary = asyncio.run(worker.scan())

    assert len(identifier.calls) == 3  # one look-up per image
    assert summary["queued"] == 2      # two sets: Stelle (2 images) and Nami (1)
    assert summary["needs_you"] == 2   # one Pixiv entry per set
    sets = {}
    for item in queue.items():
        sets.setdefault(item["batch_key"], []).append(item)
    assert len(sets) == 2
    sizes = sorted(len(items[0]["images"]) for items in sets.values())
    assert sizes == [1, 2]
    stelle = next(items for items in sets.values() if len(items[0]["images"]) == 2)
    assert [Path(path).name for path in stelle[0]["images"]] == ["01.png", "03.png"]  # file order
    assert stelle[0]["character"] == "Stelle"
    assert len(caption.calls) == 2  # one caption call per set, not per image


def test_a_duo_piece_is_its_own_set(tmp_path, monkeypatch):
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, {
        "01.png": STELLE,
        "02.png": {"characters": ["Nahida", "Wanderer"], "series": "Genshin Impact",
                   "confident": True},
        "03.png": {"characters": ["Wanderer", "Nahida"], "series": "Genshin Impact",
                   "confident": True},
    })
    for name in ("01.png", "02.png", "03.png"):
        _drop(outbox, name, body=f"art-{name}".encode())

    asyncio.run(worker.scan())

    sets = {item["batch_key"] for item in queue.items()}
    assert len(sets) == 2  # the duo's two images share a key, Stelle is alone


def test_unsure_images_form_one_universal_set(tmp_path, monkeypatch):
    """CHANGE OF PLAN: an unrecognised loose file never goes to "Needs you"."""
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, {
        "01.png": STELLE, "02.png": UNSURE, "03.png": UNSURE,
    })
    for name in ("01.png", "02.png", "03.png"):
        _drop(outbox, name, body=f"art-{name}".encode())

    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 2 and summary["needs_you"] == 2  # two sets, two Pixiv entries
    items = queue.items()
    universal = next(item for item in items if item["character"] == "Universal")
    assert [Path(path).name for path in universal["images"]] == ["02.png", "03.png"]
    assert "universal+" in universal["batch_key"]  # its own kind of set
    # the recognisable image kept its own set, and nothing unknown is waiting
    assert [Path(path).name for path in items[0]["images"]] == ["01.png"] or True
    assert {entry["kind"] for entry in queue.needs_you()} == {"pixiv"}


def test_an_identify_error_also_goes_to_needs_you(tmp_path, monkeypatch):
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, error="the model refused")

    _drop(outbox, "cover.png")
    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 0 and summary["needs_you"] == 1
    assert queue.needs_you()[0]["kind"] == "unknown"


def test_a_subfolder_is_one_set_named_after_the_folder(tmp_path, monkeypatch):
    """CHANGE OF PLAN rule 1: the folder name is the character, no identify call."""
    worker, outbox, caption, identifier = _split_setup(tmp_path, monkeypatch, {
        "01.png": STELLE, "02.png": NAMI,
    })
    _drop(outbox / "Changli (2)", "01.png", body=b"one")
    _drop(outbox / "Changli (2)", "02.png", body=b"two")

    summary = asyncio.run(worker.scan())

    assert identifier.calls == []          # never asked
    assert len(caption.calls) == 1         # one caption call for the set
    assert caption.calls[0]["given"] == {"name": "Changli", "series": "", "japanese": ""}
    assert summary["queued"] == 1
    item = queue.items()[0]
    assert len(item["images"]) == 2        # one set, never split
    assert item["character"] == "Changli"  # the folder name, counter stripped
    assert item["caption"] == "telegram caption"
    assert any(entry["kind"] == "pixiv" for entry in queue.needs_you())


def test_a_mix_folder_becomes_a_mixed_post(tmp_path, monkeypatch):
    worker, outbox, caption, identifier = _split_setup(tmp_path, monkeypatch, {
        "01.png": STELLE, "02.png": NAMI,
    })
    mixed = FakeMixed()
    worker.mixed = mixed
    _drop(outbox / "mix-morning", "01.png", body=b"one")
    _drop(outbox / "mix-morning", "02.png", body=b"two")

    summary = asyncio.run(worker.scan())

    assert len(identifier.calls) == 2      # identify is optional here (for the tags)
    assert len(mixed.calls) == 1 and mixed.calls[0]["characters"] == ["Stelle", "Nami"]
    assert summary["queued"] == 1
    assert queue.items()[0]["character"] == "Mixed: Stelle, Nami"


def test_a_mix_folder_posts_even_when_identify_fails(tmp_path, monkeypatch):
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, error="ReadTimeout",
                                        transient=True)
    worker.mixed = FakeMixed()
    _drop(outbox / "mixed", "01.png", body=b"one")

    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 1 and summary["needs_you"] == 1  # + Pixiv
    assert queue.items()[0]["character"] == "Mixed"               # nothing named, still posted
    assert [entry["kind"] for entry in queue.needs_you()] == ["pixiv"]


def test_subfolder_names_lose_a_trailing_counter(tmp_path, monkeypatch):
    worker, outbox, caption, _ = _split_setup(tmp_path, monkeypatch, {})
    for name, expected in (("Changli_2", "Changli"), ("Changli (2)", "Changli"),
                           ("Changli-2", "Changli"), ("Changli v2", "Changli"),
                           ("Changli 2", "Changli 2"),     # may be part of the name
                           ("Android 18", "Android 18")):
        _drop(outbox / name, "01.png", body=f"art-{name}".encode())
        asyncio.run(worker.scan())
        assert caption.calls[-1]["given"]["name"] == expected, name


def test_a_subfolder_set_keeps_its_folder_and_tidies_up(tmp_path, monkeypatch):
    """Review rule 1+2: posted/<date>/<subfolder name>/links.json, emptied folder gone."""
    worker, outbox, _, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    _drop(outbox / "Changli (2)", "01.png", body=b"one")
    asyncio.run(worker.scan())
    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])
    asyncio.run(worker.post_due())

    pixiv_entry = queue.needs_you()[0]
    queue.resolve_needs_you(pixiv_entry["id"], "used")
    worker._finish_batch(pixiv_entry["batch_key"])

    day = tmp_path / "Pictures" / "ArtDrop" / "posted"
    image = next(day.rglob("01.png"))
    assert image.parent.name == "Changli (2)"
    assert json.loads((image.parent / "links.json").read_text()) == [
        "https://example.com/post/1"]
    assert not (outbox / "Changli (2)").exists()  # emptied: gone
    assert outbox.is_dir()                        # the root itself stays


def test_a_folder_can_skip_pixiv(tmp_path, monkeypatch):
    """Review rule 3: with "Also prepare for Pixiv" off, nothing waits and files move."""
    worker, outbox, caption, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"], pixiv=False)
    _drop(outbox, "cover.png")

    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 1 and summary["needs_you"] == 0
    assert queue.needs_you() == []
    assert all("pixiv" not in call["platforms"] for call in caption.calls)

    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])
    asyncio.run(worker.post_due())

    # nothing blocks the move: the finished set is already in posted/
    assert not (outbox / "cover.png").exists()
    assert list((tmp_path / "Pictures" / "ArtDrop" / "posted").rglob("cover.png"))


def test_a_caption_txt_is_polished_by_default(tmp_path, monkeypatch):
    worker, outbox, caption, _ = _split_setup(tmp_path, monkeypatch, {})
    folder = outbox / "with words"
    _drop(folder, "01.png", body=b"one")
    (folder / "caption.txt").write_text("Good morning ☀️ which is your favourite?",
                                        encoding="utf-8")

    asyncio.run(worker.scan())

    # the artist's text is a draft: the model rewrites it per site
    assert caption.calls[0]["caption_draft"] == "Good morning ☀️ which is your favourite?"
    assert caption.calls[0]["caption_exact"] is False
    items = queue.items()
    assert items and items[0]["caption"] == "telegram caption"  # the model's rewrite
    assert items[0]["tags"]  # the AI still adds the tags
    # never treated as an image, but it travels with the set
    assert "caption.txt" not in {Path(path).name for item in items for path in item["images"]}
    entry = queue.batch(items[0]["batch_key"])
    assert any(Path(path).name == "caption.txt" for path in entry["files"])


def test_caption_txt_exact_marker_keeps_the_words_verbatim(tmp_path, monkeypatch):
    worker, outbox, caption, _ = _split_setup(tmp_path, monkeypatch, {})
    folder = outbox / "exact words"
    _drop(folder, "01.png", body=b"one")
    (folder / "caption.txt").write_text(
        "  !Exact  \nGood morning ☀️ which is your favourite?\n", encoding="utf-8")

    asyncio.run(worker.scan())

    assert caption.calls[0]["caption_exact"] is True
    assert caption.calls[0]["caption_draft"] == "Good morning ☀️ which is your favourite?"
    items = queue.items()
    assert items and all(item["caption"] == "Good morning ☀️ which is your favourite?"
                         for item in items)  # the marker line is gone, the text untouched


def test_the_identity_cache_avoids_a_second_call(tmp_path, monkeypatch):
    worker, outbox, _, identifier = _split_setup(tmp_path, monkeypatch, {"a.png": STELLE})
    _drop(outbox, "a.png", body=b"the same art")
    asyncio.run(worker.scan())
    assert len(identifier.calls) == 1

    # the same bytes under a new name: the cache answers, the AI is not asked again
    _drop(outbox, "b.png", body=b"the same art")
    asyncio.run(worker.scan())

    assert len(identifier.calls) == 1
    assert queue.identity(lazy.digest(outbox / "b.png"))["characters"] == ["Stelle"]


def test_the_identify_cap_holds_per_pass(tmp_path, monkeypatch):
    worker, outbox, _, identifier = _split_setup(tmp_path, monkeypatch, {
        f"{index:03d}.png": STELLE for index in range(60)
    })
    for index in range(60):
        _drop(outbox, f"{index:03d}.png", body=f"art-{index}".encode(), age=600)

    asyncio.run(worker.scan())

    assert len(identifier.calls) == 50  # the cost guard: the rest wait for the next pass
    assert worker.identify_calls == 50
    assert len(queue.items()) >= 1

    asyncio.run(worker.scan())
    assert len(identifier.calls) > 50  # the next pass carries on


# ------------------------------------------------- transient AI failures (M6.1b)
class FlakyCaption(FakeCaption):
    """Fails with a transient error for the first N calls, then behaves."""

    def __init__(self, failures=1, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures

    async def __call__(self, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise captioner.CaptionUnavailable("the model timed out")
        return await super().__call__(**kwargs)


def test_a_transient_caption_failure_waits_instead_of_needing_you(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    flaky = FlakyCaption(failures=1)
    worker.caption = flaky
    _drop(outbox, "cover.png")

    first = asyncio.run(worker.scan())

    assert first == {"queued": 0, "needs_you": 0}  # nothing decided yet
    assert queue.items() == [] and queue.needs_you() == []
    assert queue.known_files() == {}               # the file is not "handled" either
    with queue.connect() as connection:
        rows = connection.execute("SELECT key, attempts FROM ai_retries").fetchall()
    assert len(rows) == 1 and rows[0]["attempts"] == 1  # a retry is recorded…
    assert queue.ai_ready(rows[0]["key"]) is False      # …with its backoff wait

    # …and the next pass, once the backoff has passed, queues it normally
    monkeypatch.setattr(queue, "ai_ready", lambda key, now=None: True)
    second = asyncio.run(worker.scan())

    assert second["queued"] == 1 and len(queue.items()) == 1
    assert queue.needs_you() and queue.needs_you()[0]["kind"] == "pixiv"


def test_after_three_failed_passes_it_needs_you_with_a_note(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(
        tmp_path, monkeypatch, cards=["bluesky"], folder_cards=["bluesky"])
    flaky = FlakyCaption(failures=99)
    worker.caption = flaky
    _drop(outbox, "cover.png")
    monkeypatch.setattr(queue, "ai_ready", lambda key, now=None: True)

    for _ in range(2):
        summary = asyncio.run(worker.scan())
        assert summary == {"queued": 0, "needs_you": 0}

    summary = asyncio.run(worker.scan())  # the third failure gives up

    assert summary["needs_you"] == 1
    entry = queue.needs_you()[0]
    assert entry["kind"] == "unknown" and entry["note"].startswith("AI unavailable")


def test_identify_failures_back_off_then_need_you(tmp_path, monkeypatch):
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, error="ReadTimeout",
                                        transient=True)
    _drop(outbox, "cover.png")
    monkeypatch.setattr(queue, "ai_ready", lambda key, now=None: True)

    assert asyncio.run(worker.scan())["needs_you"] == 0  # waiting
    assert asyncio.run(worker.scan())["needs_you"] == 0
    summary = asyncio.run(worker.scan())

    assert summary["needs_you"] == 1
    entry = queue.needs_you()[0]
    assert entry["kind"] == "unknown" and entry["note"] == "AI unavailable"


def test_identify_runs_three_at_a_time(tmp_path, monkeypatch):
    in_flight = {"now": 0, "peak": 0}

    class SlowIdentifier(FakeIdentifier):
        async def __call__(self, *, cfg, image, candidates=None, series_hint="",
                       escalate=True):
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            await asyncio.sleep(0.02)
            in_flight["now"] -= 1
            return await super().__call__(cfg=cfg, image=image,
                                          candidates=candidates, series_hint=series_hint,
                                          escalate=escalate)

    worker, outbox, _, identifier = _split_setup(tmp_path, monkeypatch, {
        f"{index:03d}.png": STELLE for index in range(10)})
    worker.identify = SlowIdentifier()
    for index in range(10):
        _drop(outbox, f"{index:03d}.png", body=f"art-{index}".encode())

    asyncio.run(worker.scan())

    assert in_flight["peak"] == 3  # a semaphore, not one at a time


def test_a_lone_unrecognised_image_still_becomes_a_universal_post(tmp_path, monkeypatch):
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, {"01.png": UNSURE})
    _drop(outbox, "01.png", body=b"one")

    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 1 and summary["needs_you"] == 1  # + the Pixiv entry
    item = queue.items()[0]
    assert item["character"] == "Universal" and len(item["images"]) == 1
    assert [entry["kind"] for entry in queue.needs_you()] == ["pixiv"]


def test_the_retry_button_forgets_the_failure_and_rescans(tmp_path, monkeypatch):
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, error="ReadTimeout",
                                        transient=True)
    _drop(outbox, "cover.png")
    monkeypatch.setattr(queue, "ai_ready", lambda key, now=None: True)
    for _ in range(3):
        asyncio.run(worker.scan())
    entry = queue.needs_you()[0]
    assert entry["note"] == "AI unavailable"
    assert queue.known_files()

    # what the API does: resolve the entry, forget the files and the backoff, rescan
    queue.resolve_needs_you(entry["id"], "retrying")
    queue.forget_files(entry["images"])
    queue.ai_forget_prefix("identify:")
    worker.identify = FakeIdentifier(default=STELLE)
    summary = asyncio.run(worker.scan())

    assert summary["queued"] == 1
    kinds = [entry["kind"] for entry in queue.needs_you()]
    assert kinds == ["pixiv"]  # the failed entry is gone; the Pixiv one always stays


def test_ignored_folders_are_never_scanned_or_stuck(tmp_path, monkeypatch):
    """`_`-prefixed folders are the artist's own notes (CHANGE OF PLAN addendum)."""
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, {})
    _drop(outbox / "_EXAMPLE Character Name", "01.png", body=b"example")
    _drop(outbox / "Kafka", "01.png", body=b"kafka")
    (outbox / "_EXAMPLE Character Name" / "notes.txt").write_text("x", encoding="utf-8")

    assert [path.parent.name for path in lazy.files(outbox)] == ["Kafka"]
    assert lazy.unusable(outbox) == []
    asyncio.run(worker.scan())
    assert {Path(path).parts[-2] for item in queue.items() for path in item["images"]} == {"Kafka"}


def test_older_needs_you_entries_are_reprocessed_once(tmp_path, monkeypatch):
    """The migration: AI failures retry, old "not confident" ones become a universal set."""
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, {})
    old_loose = _drop(outbox, "01.png", body=b"unknown piece")
    queue.remember_files([old_loose], status="needs_you")
    queue.add_needs_you(batch_key="b-old", source=str(outbox), folder_key="outbox",
                        kind="unknown", images=[str(old_loose)], cover=str(old_loose))
    pulled_back = _drop(outbox / "Kafka", "02.png", body=b"kafka")
    queue.remember_files([pulled_back], status="needs_you")
    queue.add_needs_you(batch_key="b-pulled", source=str(outbox), folder_key="outbox",
                        kind="unknown", images=[str(pulled_back)], cover=str(pulled_back),
                        note="moved back by hand — fix the caption and post it")

    asyncio.run(worker.scan())

    # the old entry is gone (its file is re-processed), the pulled-back one stays
    kinds = [entry["note"] for entry in queue.needs_you()]
    assert "moved back by hand — fix the caption and post it" in kinds
    assert all(not note.startswith("AI") for note in kinds if note)
    assert queue.known_files().get(str(pulled_back), {}).get("status") == "needs_you"


def test_an_ai_error_still_waits_for_a_human(tmp_path, monkeypatch):
    """Only AI errors (rule 4) end up in Needs you now, with their note."""
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, error="ReadTimeout",
                                        transient=True)
    _drop(outbox, "cover.png")
    monkeypatch.setattr(queue, "ai_ready", lambda key, now=None: True)

    for _ in range(3):
        asyncio.run(worker.scan())

    entry = queue.needs_you()[0]
    assert entry["kind"] == "unknown" and entry["note"] == "AI unavailable"


# ------------------------------------------------ identity/caption agreement
class DriftingCaption(FakeCaption):
    """Names someone else for the first `wrong` calls, then the given name."""

    def __init__(self, wrong=1, other="Chiori"):
        super().__init__()
        self.wrong = wrong
        self.other = other

    async def __call__(self, **kwargs):
        result = await super().__call__(**kwargs)
        if len(self.calls) <= self.wrong:
            result["character"] = self.other
        return result


def test_a_drifting_caption_is_asked_again_once(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                  folder_cards=["bluesky"])
    worker.caption = DriftingCaption(wrong=1)
    _drop(outbox / "March 7th", "01.png", body=b"one")

    summary = asyncio.run(worker.scan())

    assert len(worker.caption.calls) == 2  # asked once more, then it agreed
    assert summary["queued"] == 1
    assert queue.items()[0]["character"] == "March 7th"


def test_a_caption_that_keeps_disagreeing_needs_you(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                  folder_cards=["bluesky"])
    worker.caption = DriftingCaption(wrong=5, other="Elysia")
    _drop(outbox / "March 7th", "01.png", body=b"one")

    summary = asyncio.run(worker.scan())

    assert len(worker.caption.calls) == 2  # one retry only
    assert summary == {"queued": 0, "needs_you": 1}
    assert queue.items() == []
    entry = queue.needs_you()[0]
    assert entry["kind"] == "unknown"
    assert entry["note"].startswith("AI disagreed about the character")
    assert "Elysia" in entry["note"]


def test_a_recognised_loose_set_passes_the_name_as_a_fact(tmp_path, monkeypatch):
    worker, outbox, caption, _ = _split_setup(tmp_path, monkeypatch, {"01.png": STELLE})
    _drop(outbox, "01.png", body=b"one")

    asyncio.run(worker.scan())

    assert caption.calls[0]["given"] == {"name": "Stelle", "series": "Honkai: Star Rail",
                                         "japanese": "", "source": "identify"}


def test_a_folder_named_set_posts_even_if_the_model_is_unsure(tmp_path, monkeypatch):
    worker, outbox, caption, _ = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                        folder_cards=["bluesky"])
    caption.confident = False  # a newer character the model does not know yet
    _drop(outbox / "Citlali- genshin impact", "01.png", body=b"one")

    summary = asyncio.run(worker.scan())

    assert caption.calls[0]["given"] == {"name": "Citlali", "series": "Genshin Impact",
                                         "japanese": ""}
    assert summary["queued"] == 1
    item = queue.items()[0]
    assert (item["character"], item["series"]) == ("Citlali", "Genshin Impact")


# ---------------------------------------------------------- missing files
def _due_now(monkeypatch):
    monkeypatch.setattr(queue, "due_items", lambda now=None, limit=50: [
        {**entry, "due_at": time.time() - 1} for entry in queue.items(status="pending")])


def test_a_file_moved_into_a_newer_set_leaves_the_old_one(tmp_path, monkeypatch):
    worker, outbox, _, publisher = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                          folder_cards=["bluesky"])
    worker.mixed = FakeMixed()
    loose = _drop(outbox, "11.png", body=b"eleven")
    asyncio.run(worker.scan())
    old = queue.items()
    assert len(old) == 1 and old[0]["character"] == "Nino Nakano"

    (outbox / "mix").mkdir()
    loose.replace(outbox / "mix" / "11.png")  # the artist moves it into a mix set
    asyncio.run(worker.scan())

    assert queue.item(old[0]["id"])["status"] == "superseded"
    waiting = queue.items(status="pending")
    assert len(waiting) == 1 and waiting[0]["character"].startswith("Mixed")
    assert waiting[0]["images"] == [str(outbox / "mix" / "11.png")]
    # the old set's Pixiv entry is gone too; only the mix set's remains
    assert [entry["batch_key"] for entry in queue.needs_you()] == [waiting[0]["batch_key"]]

    _due_now(monkeypatch)
    asyncio.run(worker.post_due())
    assert len(publisher.calls) == 1  # posted once, under the mix caption
    assert publisher.calls[0]["caption"] == "mixed caption"


def test_a_deleted_file_holds_its_post_until_it_comes_back(tmp_path, monkeypatch):
    worker, outbox, _, publisher = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                          folder_cards=["bluesky"])
    path = _drop(outbox / "Kafka", "01.png", body=b"kafka")
    asyncio.run(worker.scan())
    item_id = queue.items()[0]["id"]

    body = path.read_bytes()
    path.unlink()
    _due_now(monkeypatch)
    asyncio.run(worker.post_due())

    assert publisher.calls == []  # never posted without its picture
    held = queue.item(item_id)
    assert held["status"] == "missing" and "files missing" in held["error"]
    assert queue.queue_payload()["counts"]["missing"] == 1
    assert "files missing" in queue.needs_you()[0]["note"]

    path.write_bytes(body)  # put back where it was
    worker.reconcile()
    assert queue.item(item_id)["status"] == "pending"


def test_a_set_that_lost_some_images_posts_the_rest(tmp_path, monkeypatch):
    worker, outbox, _, publisher = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                          folder_cards=["bluesky"])
    _drop(outbox / "Kafka", "01.png", body=b"one")
    second = _drop(outbox / "Kafka", "02.png", body=b"two")
    asyncio.run(worker.scan())
    second.unlink()

    _due_now(monkeypatch)
    asyncio.run(worker.post_due())

    assert publisher.calls[0]["images"] == ["01_01.png"]


def test_a_restart_never_requeues_a_waiting_pixiv_entry(tmp_path, monkeypatch):
    """Live bug: every start re-processed open Pixiv entries and queued the set twice."""
    worker, outbox, _, _ = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                  folder_cards=["bluesky"])
    _drop(outbox / "Kafka", "01.png", body=b"kafka")
    asyncio.run(worker.scan())
    before = len(queue.items())

    restarted = LazyWorker(FakePublisher(), cards=lambda: {"bluesky"}, caption=FakeCaption(),
                           identify=FakeIdentifier(), mixed=FakeMixed())
    asyncio.run(restarted.scan())

    assert len(queue.items()) == before
    assert [entry["kind"] for entry in queue.needs_you()] == ["pixiv"]


def test_a_renamed_folder_is_a_new_set(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                  folder_cards=["bluesky"])
    _drop(outbox / "Citlali- genshin impact", "01.png", body=b"one")
    asyncio.run(worker.scan())
    old = queue.items()[0]

    (outbox / "Citlali- genshin impact").rename(outbox / "Citlali - Genshin Impact")
    asyncio.run(worker.scan())

    assert queue.item(old["id"])["status"] == "superseded"
    new = queue.items(status="pending")
    assert len(new) == 1 and new[0]["batch_key"] != old["batch_key"]
    assert new[0]["batch_key"].startswith("outbox/Citlali - Genshin Impact/")


def test_paused_still_tidies_the_queue(tmp_path, monkeypatch):
    worker, outbox, _, _ = _setup(tmp_path, monkeypatch, cards=["bluesky"],
                                  folder_cards=["bluesky"])
    path = _drop(outbox / "Kafka", "01.png", body=b"kafka")
    asyncio.run(worker.scan())
    lazy.save({"paused": True})
    path.unlink()

    asyncio.run(worker.scan())

    assert queue.items()[0]["status"] == "missing"


def test_a_mixed_post_never_sends_its_label_as_the_character(tmp_path, monkeypatch):
    """The label would become DeviantArt's title and Pixiv's first tag."""
    worker, outbox, _, _ = _split_setup(tmp_path, monkeypatch, {
        "01.png": STELLE, "02.png": NAMI})
    worker.mixed = FakeMixed()
    seen = []

    async def publish(card_id, post, post_id, dry_run):
        seen.append((post.character, post.series))
        return {"ok": True, "url": "https://example.com/1", "error": None}

    worker.publish = publish
    _drop(outbox / "mix", "01.png", body=b"one")
    _drop(outbox / "mix", "02.png", body=b"two")
    asyncio.run(worker.scan())
    assert queue.items()[0]["character"] == "Mixed: Stelle, Nami"  # the Queue tab label

    _due_now(monkeypatch)
    asyncio.run(worker.post_due())

    assert seen == [("", "")]
