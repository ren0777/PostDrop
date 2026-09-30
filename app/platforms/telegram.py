"""Telegram connector — Bot API (sendPhoto / sendMediaGroup)."""
from __future__ import annotations

import json

import httpx

from .base import Connector, Field, PublishError

TIMEOUT = httpx.Timeout(120.0, connect=15.0)


def photo_data(chat_id: str, text: str) -> dict:
    """sendPhoto form fields; an empty caption is left out entirely."""
    data = {"chat_id": chat_id}
    if text:
        data["caption"] = text
    return data


def group_data(chat_id: str, text: str, count: int) -> dict:
    """sendMediaGroup form fields; the caption goes on the first item."""
    media = [{"type": "photo", "media": f"attach://file{index}"} for index in range(count)]
    if text:
        media[0]["caption"] = text
    return {"chat_id": chat_id, "media": json.dumps(media)}


def _describe_error(response: httpx.Response, fallback: str) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = body.get("description") or response.text[:200] or f"HTTP {response.status_code}"
    return f"{fallback}: {detail}"


class Telegram(Connector):
    id = "telegram"
    label = "Telegram"
    caption_limit = 1024
    max_images = 10
    max_bytes = 9_500_000
    max_side = 2560
    original_formats = ("JPEG", "PNG")
    default_caption_style = "name_series"
    api = "https://api.telegram.org"
    fields = [
        Field(
            "bot_token", "Bot token", secret=True,
            placeholder="123456789:AA...",
            help="Talk to @BotFather in Telegram: /newbot, then copy the token.",
        ),
        Field(
            "chat_id", "Chat id", placeholder="@my_channel or -100...",
            help="The @username of a public channel, or the numeric id of a private one. "
                 "The bot must be an admin of the channel.",
        ),
    ]
    setup_help = (
        "1) In Telegram, message @BotFather and send /newbot — copy the token it gives you. "
        "2) Create a channel (or use an existing one) and add the bot as an admin. "
        "3) For a public channel use its @username as the chat id. For a private one, send a "
        "message in the channel and open https://api.telegram.org/bot<TOKEN>/getUpdates — "
        "the chat id looks like -1001234567890."
    )

    # ------------------------------------------------------------------ helpers
    def _method(self, cfg: dict, name: str) -> str:
        return f"{self.api}/bot{cfg.get('bot_token', '')}/{name}"

    def _parse(self, response: httpx.Response, fallback: str) -> dict | list:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            raise PublishError(_describe_error(response, fallback))
        return body.get("result") or {}

    def post_url(self, chat_id, message_id) -> str:
        chat = str(chat_id or "").strip()
        if not message_id:
            return ""
        if chat.startswith("@"):
            return f"https://t.me/{chat[1:]}/{message_id}"
        if chat.startswith("-100"):
            return f"https://t.me/c/{chat[4:]}/{message_id}"
        return ""

    # ---------------------------------------------------------------- interface
    async def test(self, cfg: dict) -> str:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            me = self._parse(await client.get(self._method(cfg, "getMe")), "Telegram getMe failed")
            chat = self._parse(
                await client.get(self._method(cfg, "getChat"),
                                 params={"chat_id": cfg.get("chat_id", "")}),
                "Telegram getChat failed",
            )
        title = chat.get("title") or chat.get("username") or chat.get("id")
        return f"Bot @{me.get('username')} → chat “{title}”"

    async def send(self, post, cfg: dict, text: str, images: list) -> tuple[str, str]:
        chat_id = cfg.get("chat_id", "")
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if len(images) == 1:
                image = images[0]
                response = await client.post(
                    self._method(cfg, "sendPhoto"),
                    data=photo_data(chat_id, text),
                    files={"photo": (image.filename, image.data, image.mime)},
                )
                message = self._parse(response, "Telegram sendPhoto failed")
            else:
                files = {
                    f"file{index}": (image.filename, image.data, image.mime)
                    for index, image in enumerate(images)
                }
                response = await client.post(
                    self._method(cfg, "sendMediaGroup"),
                    data=group_data(chat_id, text, len(images)),
                    files=files,
                )
                result = self._parse(response, "Telegram sendMediaGroup failed")
                message = result[0] if isinstance(result, list) and result else result

        return self.post_url(chat_id, message.get("message_id")), ""
