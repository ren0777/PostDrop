"""Caption styles: ai / name_series / none, and the Telegram caption rules."""
import asyncio
import json

import pytest
from PIL import Image

from app.core import paths, settings
from app.core.post import Post
from app.platforms import REGISTRY
from app.platforms.telegram import group_data, photo_data


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    monkeypatch.setattr(paths, "UPLOADS", paths.DATA / "uploads")


def _post(tmp_path, **kwargs):
    path = tmp_path / "art.png"
    Image.new("RGB", (64, 64)).save(path, "PNG")
    return Post(images=[path], **kwargs)


def _text(platform_id, post):
    result = asyncio.run(REGISTRY[platform_id].publish(post, {}, dry_run=True))
    assert result.ok is True, result.error
    return result.preview["text"]


def test_ai_style_uses_caption_and_tags(tmp_path):
    post = _post(tmp_path, caption="hello", tags=["art"], options={"discord": {"style": "ai"}})
    assert _text("discord", post) == "hello\n\n#art"


def test_name_series_style_ignores_caption_and_tags(tmp_path):
    post = _post(tmp_path, caption="should not appear", tags=["art"],
                 character="Stelle", series="Honkai: Star Rail",
                 options={"discord": {"style": "name_series"}})
    assert _text("discord", post) == "Stelle — Honkai: Star Rail"


def test_name_series_without_a_series_is_just_the_name(tmp_path):
    post = _post(tmp_path, caption="x", character="Stelle",
                 options={"discord": {"style": "name_series"}})
    assert _text("discord", post) == "Stelle"


def test_none_style_never_falls_back_to_the_shared_caption(tmp_path):
    post = _post(tmp_path, caption="must not appear", tags=["art"], character="Stelle",
                 options={"discord": {"style": "none"}})
    assert _text("discord", post) == ""


def test_telegram_defaults_to_name_series(tmp_path):
    post = _post(tmp_path, caption="must not appear", tags=["art"], character="Stelle")
    assert _text("telegram", post) == "Stelle"


def test_explicit_style_wins_over_the_saved_setting(tmp_path):
    settings.set_styles({"telegram": "none"})
    post = _post(tmp_path, caption="x", character="Stelle",
                 options={"telegram": {"style": "name_series"}})
    assert _text("telegram", post) == "Stelle"


def test_saved_setting_is_used_when_the_post_has_no_style(tmp_path):
    settings.set_styles({"discord": "none"})
    post = _post(tmp_path, caption="x", options={})
    assert _text("discord", post) == ""


def test_set_styles_ignores_unknown_values(tmp_path):
    settings.set_styles({"telegram": "bogus", "discord": "none"})
    assert settings.style_for("telegram", "name_series") == "name_series"
    assert settings.style_for("discord", "ai") == "none"


def test_telegram_empty_caption_is_omitted():
    assert photo_data("@chan", "") == {"chat_id": "@chan"}
    assert photo_data("@chan", "hi") == {"chat_id": "@chan", "caption": "hi"}

    media = json.loads(group_data("@chan", "", 2)["media"])
    assert "caption" not in media[0]

    media = json.loads(group_data("@chan", "hi", 2)["media"])
    assert media[0]["caption"] == "hi"
