"""Too many images: the first N are posted, with a visible note."""
import asyncio

from PIL import Image

from app.core.post import Post
from app.platforms import REGISTRY


def _images(tmp_path, count):
    paths = []
    for index in range(count):
        path = tmp_path / f"art{index:02d}.png"
        Image.new("RGB", (64, 64), (index * 20 % 255, 40, 90)).save(path, "PNG")
        paths.append(path)
    return paths


def _dry_run(platform_id, tmp_path, count):
    post = Post(images=_images(tmp_path, count), caption="hello", character="Stelle",
                options={platform_id: {"style": "name_series"}})
    return asyncio.run(REGISTRY[platform_id].publish(post, {}, dry_run=True))


def test_bluesky_keeps_the_first_four(tmp_path):
    result = _dry_run("bluesky", tmp_path, 10)
    assert result.ok is True
    assert len(result.preview["images"]) == 4
    assert result.preview["note"] == "the first 4 of 10 images (Bluesky's limit)"


def test_deviantart_keeps_the_first_one(tmp_path):
    result = _dry_run("deviantart", tmp_path, 10)
    assert result.ok is True
    assert len(result.preview["images"]) == 1
    assert result.preview["note"] == "the first 1 of 10 images (DeviantArt's limit)"


def test_telegram_takes_all_ten_without_a_note(tmp_path):
    result = _dry_run("telegram", tmp_path, 10)
    assert result.ok is True
    assert len(result.preview["images"]) == 10
    assert "note" not in result.preview


def test_instagram_takes_all_ten_without_a_note(tmp_path):
    result = _dry_run("instagram", tmp_path, 10)
    assert result.ok is True
    assert len(result.preview["images"]) == 10
    assert "note" not in result.preview


def test_send_receives_only_the_trimmed_images(tmp_path, monkeypatch):
    sent = {}

    async def fake_send(post, cfg, text, images):
        sent["count"] = len(images)
        return "https://example.com/1", ""

    monkeypatch.setattr(REGISTRY["bluesky"], "send", fake_send)
    post = Post(images=_images(tmp_path, 10), caption="hello")
    result = asyncio.run(REGISTRY["bluesky"].publish(
        post, {"handle": "h", "app_password": "p"}, dry_run=False))
    assert result.ok is True
    assert sent["count"] == 4
    assert result.note == "the first 4 of 10 images (Bluesky's limit)"
