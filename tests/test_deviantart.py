"""DeviantArt: id encoding, titles, submit/publish fields, the single-image rule."""
import asyncio

import httpx
from PIL import Image

from app.core import net
from app.core.post import Post
from app.platforms.deviantart import (DeviantArt, agreement_error, base36, default_title,
                                      gallery_ids, publish_data, sanitize_tags, stash_url,
                                      submit_data)


def test_base36_and_stash_url():
    assert base36(0) == "0"
    assert base36(36) == "10"
    assert base36(1295) == "zz"
    assert stash_url(1295) == "https://sta.sh/0zz"
    assert stash_url(None) == ""


def test_default_title_uses_the_first_caption_line():
    assert default_title("Stelle at the festival\n\nmore text") == "Stelle at the festival"
    assert default_title("#Tagged\nx") == "Tagged"
    assert default_title("") == "Untitled"
    assert len(default_title("x" * 80)) == 50


def test_default_title_prefers_the_character_and_series():
    assert default_title("Stelle enjoys a festival", "Stelle", "Honkai: Star Rail") == \
        "Stelle — Honkai: Star Rail"
    assert default_title("caption", "Stelle", "") == "Stelle"


def test_default_title_cuts_at_a_word_boundary():
    title = default_title("Stelle enjoys a summer festival night in a floral kimono")
    assert title == "Stelle enjoys a summer festival night in a floral"
    assert len(title) <= 50


def test_default_title_strips_dangling_punctuation():
    assert default_title("Stelle at the festival!") == "Stelle at the festival"


def test_sanitize_tags_cleans_dedupes_and_limits():
    assert sanitize_tags(["Honkai: Star Rail", "stelle", "Stelle", "", "ai-art!", "ok_tag"]) == [
        "HonkaiStarRail", "stelle", "aiart", "ok_tag",
    ]
    assert len(sanitize_tags([f"tag{index}" for index in range(40)])) == 30


def test_submit_data_carries_the_title_and_caption():
    data = submit_data("Festival", "Stelle at the festival", ["Stelle", "aiart"])
    assert data["title"] == "Festival"
    assert data["artist_comments"] == "Stelle at the festival"
    assert data["is_ai_generated"] == "true"
    assert data["noai"] == "true"
    assert data["tags[]"] == ["Stelle", "aiart"]
    assert "description" not in data
    assert "itemid" not in data


def test_publish_data_always_sends_is_mature_and_the_tag_array():
    data = publish_data(42, ["Stelle"], {}, mature=False)
    assert data["itemid"] == "42"
    assert data["is_mature"] == "false"
    assert data["tags[]"] == ["Stelle"]
    assert "mature_level" not in data


def test_publish_data_mature_level():
    assert publish_data(1, [], {}, mature=True)["mature_level"] == "moderate"
    assert publish_data(1, [], {"mature_level": "strict"}, mature=True)["mature_level"] == "strict"
    assert publish_data(1, [], {"mature_level": "wild"}, mature=True)["mature_level"] == "moderate"


def test_publish_data_uses_galleryids():
    data = publish_data(1, [], {"gallery_folder": "abc-123, def-456"}, mature=False)
    assert data["galleryids[]"] == ["abc-123", "def-456"]
    assert "galleryids[]" not in publish_data(1, [], {}, mature=False)
    assert gallery_ids({"gallery_folder": "one two"}) == ["one", "two"]
    assert gallery_ids({}) == []


def test_agreement_error_detection():
    assert agreement_error({"error_code": 0}) is True
    assert agreement_error({"error_code": 1}) is True
    assert agreement_error({"error": "Must accept DA terms of service"}) is True
    assert agreement_error({"error": "Invalid itemid"}) is False
    assert agreement_error({}) is False


def test_dry_run_keeps_hashtags_out_of_the_description(tmp_path):
    path = tmp_path / "art.png"
    Image.new("RGB", (64, 64)).save(path, "PNG")
    post = Post(images=[path], caption="Stelle at the festival", tags=["art"],
                options={"deviantart": {"style": "ai"}})
    result = asyncio.run(DeviantArt().publish(post, {}, dry_run=True))
    assert result.ok is True
    assert result.preview["text"] == "Stelle at the festival"


def test_deviantart_keeps_the_default_tls_context(monkeypatch):
    captured = {}

    def fake_client(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    DeviantArt()._client(httpx.Timeout(5))
    assert captured["verify"] is net.tls_context(False)
