"""The connector registry: one instance per supported site."""
from __future__ import annotations

from .base import Connector, Field, PublishError, Result, scrub  # noqa: F401
from .bluesky import Bluesky
from .deviantart import DeviantArt
from .discord import Discord
from .instagram import Instagram
from .oauth import OAuthConnector  # noqa: F401
from .pixiv import Pixiv
from .telegram import Telegram
from .tumblr import Tumblr

REGISTRY = {
    connector.id: connector
    for connector in (Bluesky(), Telegram(), Discord(), Tumblr(), DeviantArt(),
                      Instagram(), Pixiv())
}


def get(platform_id: str):
    """The connector for a platform id, or None."""
    return REGISTRY.get(platform_id)
