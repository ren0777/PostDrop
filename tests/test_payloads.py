"""Discord webhook payload and Telegram post-URL helpers."""
from app.platforms.discord import build_payload
from app.platforms.telegram import Telegram


def test_discord_payload_includes_alt_text_per_file():
    payload = build_payload("hi", "alt text", "ArtDrop", 2)
    assert payload["content"] == "hi"
    assert payload["username"] == "ArtDrop"
    assert payload["attachments"] == [
        {"id": "0", "description": "alt text"},
        {"id": "1", "description": "alt text"},
    ]


def test_discord_payload_omits_empty_username():
    payload = build_payload("hi", "", "", 1)
    assert "username" not in payload


def test_telegram_public_channel_url():
    assert Telegram().post_url("@mychannel", 12) == "https://t.me/mychannel/12"


def test_telegram_private_channel_url():
    assert Telegram().post_url("-1001234567890", 7) == "https://t.me/c/1234567890/7"


def test_telegram_numeric_chat_has_no_public_url():
    assert Telegram().post_url("12345", 7) == ""
    assert Telegram().post_url("@mychannel", None) == ""
