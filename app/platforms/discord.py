"""Discord connector — channel webhook."""
from __future__ import annotations

import json

import httpx

from .base import Connector, Field, PublishError

TIMEOUT = httpx.Timeout(120.0, connect=15.0)

_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)


def build_payload(text: str, alt: str, username: str, count: int) -> dict:
    """The payload_json sent alongside the files."""
    payload: dict = {"content": text}
    if username:
        payload["username"] = username
    if count:
        payload["attachments"] = [
            {"id": str(index), "description": alt} for index in range(count)
        ]
    return payload


class Discord(Connector):
    id = "discord"
    label = "Discord"
    caption_limit = 2000
    max_images = 10
    max_bytes = 9_500_000
    max_side = None
    original_formats = ("JPEG", "PNG", "WEBP", "GIF")
    fields = [
        Field(
            "webhook_url", "Webhook URL", secret=True,
            placeholder="https://discord.com/api/webhooks/...",
            help="Channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.",
        ),
        Field(
            "username", "Display name", required=False, placeholder="ArtDrop",
            help="Optional name shown above the post.",
        ),
    ]
    setup_help = (
        "In Discord, right-click the channel → Edit Channel → Integrations → Webhooks → "
        "New Webhook, then Copy Webhook URL and paste it here. Treat the URL like a password: "
        "anyone who has it can post to that channel."
    )

    # ------------------------------------------------------------------ helpers
    def _webhook_url(self, cfg: dict) -> str:
        url = str(cfg.get("webhook_url", "")).strip()
        if not url.startswith(_WEBHOOK_PREFIXES):
            raise PublishError("That does not look like a Discord webhook URL.")
        return url

    async def _webhook_info(self, cfg: dict) -> dict:
        """The webhook's own guild/channel ids (the execute answer often omits them).

        The post link needs a guild id, and Discord only returns it here, so it is
        remembered in the config the first time it is seen.
        """
        url = self._webhook_url(cfg)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return {}
        try:
            info = response.json()
        except ValueError:
            return {}
        return info if isinstance(info, dict) else {}

    def _remember_webhook(self, cfg: dict, info: dict) -> None:
        fields = {}
        if info.get("guild_id"):
            fields["guild_id"] = str(info["guild_id"])
        if info.get("channel_id"):
            fields["channel_id"] = str(info["channel_id"])
        if fields:
            self.remember(cfg, fields)

    # ---------------------------------------------------------------- interface
    async def test(self, cfg: dict) -> str:
        url = self._webhook_url(cfg)
        info = await self._webhook_info(cfg)
        if not info:
            raise PublishError("Discord rejected the webhook — check the URL.")
        self._remember_webhook(cfg, info)
        return f"Webhook “{info.get('name')}” (channel {info.get('channel_id')})"

    async def send(self, post, cfg: dict, text: str, images: list) -> tuple[str, str]:
        url = self._webhook_url(cfg)
        alt = (self.options_for(post, cfg).get("alt_text") or post.alt_text or "").strip()
        payload = build_payload(text, alt, str(cfg.get("username", "")).strip(), len(images))
        files = {
            f"files[{index}]": (image.filename, image.data, image.mime)
            for index, image in enumerate(images)
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                url,
                params={"wait": "true"},
                data={"payload_json": json.dumps(payload)},
                files=files,
            )
        if response.status_code != 200:
            try:
                detail = response.json().get("message")
            except ValueError:
                detail = response.text[:200]
            raise PublishError(f"Discord rejected the post: {detail or f'HTTP {response.status_code}'}")

        message = response.json()
        guild = str(message.get("guild_id") or cfg.get("guild_id") or "")
        channel = str(message.get("channel_id") or cfg.get("channel_id") or "")
        message_id = str(message.get("id") or "")
        if not guild:
            # The execute answer has no guild_id: ask the webhook itself, once.
            info = await self._webhook_info(cfg)
            guild = str(info.get("guild_id") or "")
            channel = channel or str(info.get("channel_id") or "")
            self._remember_webhook(cfg, info)
        if guild and channel and message_id:
            return f"https://discord.com/channels/{guild}/{channel}/{message_id}", ""
        return "", ""
