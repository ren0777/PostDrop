"""The lazy queue: spacing, quiet hours, persistence, dedupe."""
import time

from app.core import lazy, queue


def _item(card_id="bluesky", **extra):
    return {"card_id": card_id, "caption": "cap", "images": ["a.png"], "cover": "a.png",
            **extra}


def test_items_are_spaced_per_card(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    now = time.time()
    ids = queue.add_items([_item("bluesky"), _item("bluesky"), _item("telegram")],
                          spacing_minutes={"bluesky": 120, "telegram": 120}, now=now)

    entries = {item["id"]: item for item in queue.items()}
    assert len(ids) == 3
    first_bsky = next(entry for entry in entries.values() if entry["card_id"] == "bluesky")
    rest_bsky = [entry for entry in entries.values()
                 if entry["card_id"] == "bluesky" and entry["id"] != first_bsky["id"]]
    assert rest_bsky[0]["due_at"] - first_bsky["due_at"] == 120 * 60
    # a different card gets its own queue
    telegram = next(entry for entry in entries.values() if entry["card_id"] == "telegram")
    assert abs(telegram["due_at"] - first_bsky["due_at"]) < 1


def test_spacing_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    now = time.time()
    queue.add_items([_item("tumblr")], spacing_minutes={"tumblr": 120}, now=now)
    # a new call (a new connection: the app restarted) still waits its turn
    queue.add_items([_item("tumblr")], spacing_minutes={"tumblr": 120}, now=now + 60)
    due = sorted(item["due_at"] for item in queue.items())
    assert due[1] - due[0] == 120 * 60


def test_quiet_hours_push_a_due_time_out(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    stamp = time.mktime((2026, 9, 30, 2, 30, 0, 0, 0, -1))  # 02:30 local
    moved = queue.shift_out_of_quiet(stamp, {"enabled": True, "start": 1, "end": 8})
    assert time.localtime(moved).tm_hour >= 8
    # …and the chain continues from where it landed, never bunched up
    queue.add_items([_item("discord"), _item("discord")],
                    spacing_minutes={"discord": 120},
                    quiet={"enabled": True, "start": 1, "end": 8}, now=stamp)
    times = sorted(item["due_at"] for item in queue.items())
    assert time.localtime(times[0]).tm_hour >= 8
    assert times[1] - times[0] == 120 * 60


def test_quiet_hours_off_leaves_the_time_alone(tmp_path):
    stamp = time.mktime((2026, 9, 30, 2, 30, 0, 0, 0, -1))
    assert queue.shift_out_of_quiet(stamp, {"enabled": False}) == stamp
    assert queue.shift_out_of_quiet(stamp, None) == stamp


def test_a_pc_that_was_off_catches_up_one_post_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    now = time.time()
    queue.add_items([_item("bluesky"), _item("bluesky")],
                    spacing_minutes={"bluesky": 120}, now=now - 10_000)

    due = queue.due_items(now)
    assert len(due) == 2  # both are overdue…
    assert queue.card_ready("bluesky", 120, now) is True  # …but the card posts one
    queue.mark_posted(due[0]["id"], "https://example.com/1", now=now)
    assert queue.card_ready("bluesky", 120, now) is False  # then it waits its spacing


def test_hashes_are_never_posted_twice_to_one_card(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    assert queue.already_posted("abc", "bluesky") is False
    queue.remember_posted("abc", "bluesky", "https://example.com/1")
    assert queue.already_posted("abc", "bluesky") is True
    assert queue.already_posted("abc", "telegram") is False  # another card is its own story


def test_seen_files_are_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    image = tmp_path / "outbox" / "a.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"art")

    queue.remember_files([image], status="queued")

    record = queue.known_files()[str(image)]
    assert record["status"] == "queued"
    assert record["digest"] == lazy.digest(image)
    assert record["size"] == 3

    queue.mark_files([image], "posted")
    assert queue.known_files()[str(image)]["status"] == "posted"

    queue.forget_files([image])  # the file moved away: the path is free again
    assert queue.known_files() == {}


def test_items_can_be_skipped_retried_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    item_id = queue.add_items([_item()], now=time.time())[0]

    queue.mark_failed(item_id, "boom")
    entry = queue.item(item_id)
    assert entry["status"] == "failed" and entry["error"] == "boom"
    assert queue.retry_item(item_id) is True
    assert queue.item(item_id)["status"] == "pending"

    assert queue.update_caption(item_id, "new text", ["a", "b"]) is True
    entry = queue.item(item_id)
    assert entry["caption"] == "new text" and entry["tags"] == ["a", "b"]

    assert queue.skip_item(item_id) is True
    assert queue.item(item_id)["status"] == "skipped"
    assert queue.remove_item(item_id) is True
    assert queue.item(item_id) is None


def test_the_payload_groups_by_card_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    now = time.time()
    queue.add_items([_item("bluesky"), _item("bluesky"), _item("telegram")],
                    spacing_minutes={}, now=now)
    queue.add_needs_you(batch_key="b", source="outbox", folder_key="outbox", kind="pixiv",
                        images=["a.png"])

    payload = queue.queue_payload(now=now)

    cards = {group["card_id"]: group for group in payload["cards"]}
    assert cards["bluesky"]["pending"] == 2
    assert cards["telegram"]["pending"] == 1
    assert payload["counts"] == {"pending": 3, "failed": 0, "missing": 0, "needs_you": 1}


def test_a_batch_is_only_moved_when_all_its_items_are_done(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    ids = queue.add_items([_item("bluesky", batch_key="b1"), _item("telegram", batch_key="b1")],
                          now=time.time())
    queue.add_batch("b1", "outbox", "outbox", ["a.png"])

    assert queue.batch_pending("b1") == 2
    queue.mark_posted(ids[0], "https://example.com/1")
    assert queue.batch_pending("b1") == 1
    queue.mark_posted(ids[1], "https://example.com/2")
    assert queue.batch_pending("b1") == 0
    assert sorted(queue.batch_links("b1")) == ["https://example.com/1", "https://example.com/2"]

    queue.mark_batch_moved("b1")
    assert queue.batch("b1")["moved_at"] > 0


def test_the_ai_retry_budget_backs_off_then_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    now = 1_000_000.0
    key = "identify:/tmp/x.png"

    assert queue.ai_ready(key, now) is True and queue.ai_attempts(key) == 0
    assert queue.ai_available(key) is True

    assert queue.ai_failed(key, now) == 1
    assert queue.ai_ready(key, now) is False            # 1 minute
    assert queue.ai_ready(key, now + 61) is True
    assert queue.ai_failed(key, now + 61) == 2
    assert queue.ai_ready(key, now + 61 + 299) is False  # 5 minutes
    assert queue.ai_ready(key, now + 61 + 301) is True
    assert queue.ai_failed(key, now + 400) == 3
    assert queue.ai_available(key) is False              # 15 minutes, then a human
    assert queue.ai_ready(key, now + 400 + 901) is True

    queue.ai_reset(key)
    assert queue.ai_attempts(key) == 0 and queue.ai_ready(key, now) is True


def test_ai_forget_prefix_clears_a_group(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    queue.ai_failed("identify:/a.png")
    queue.ai_failed("identify:/b.png")
    queue.ai_failed("caption:outbox/cover.png")

    queue.ai_forget_prefix("identify:")

    assert queue.ai_attempts("identify:/a.png") == 0
    assert queue.ai_attempts("caption:outbox/cover.png") == 1


def test_a_needs_you_note_is_kept_and_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    entry_id = queue.add_needs_you(batch_key="b", source="outbox", folder_key="outbox",
                                   kind="unknown", images=["a.png"], note="AI unavailable")

    entry = queue.needs_you_entry(entry_id)

    assert entry["note"] == "AI unavailable"
    assert queue.needs_you()[0]["note"] == "AI unavailable"


def test_removed_posts_give_their_slots_back(tmp_path, monkeypatch):
    """Live bug: deleted duplicates kept the rest waiting 14 hours."""
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    now = time.time()
    ids = queue.add_items([_item("telegram") for _ in range(8)],
                          spacing_minutes={"telegram": 120}, now=now)
    for item_id in ids[:6]:
        queue.remove_item(item_id)
    assert min(item["due_at"] for item in queue.items()) - now >= 6 * 3600

    queue.compact_schedule(now=now, quiet={"enabled": False})

    due = sorted(item["due_at"] for item in queue.items())
    assert abs(due[0] - now) < 1 and due[1] - due[0] == 120 * 60
    # and a new post lines up right behind them, not behind the removed ones
    queue.add_items([_item("telegram")], spacing_minutes={"telegram": 120}, now=now)
    due = sorted(item["due_at"] for item in queue.items())
    assert due[2] - due[1] == 120 * 60


def test_compacting_never_delays_a_post(tmp_path, monkeypatch):
    monkeypatch.setattr(lazy, "root", lambda: tmp_path / "ArtDrop")
    now = time.time()
    queue.add_items([_item("telegram")], spacing_minutes={"telegram": 120}, now=now)
    queue.mark_posted(queue.add_items([_item("telegram")], spacing_minutes={"telegram": 120},
                                      now=now)[0], now=now)
    before = {item["id"]: item["due_at"] for item in queue.items(status="pending")}

    queue.compact_schedule(now=now, quiet={"enabled": False})

    after = {item["id"]: item["due_at"] for item in queue.items(status="pending")}
    assert all(after[key] <= before[key] for key in before)
