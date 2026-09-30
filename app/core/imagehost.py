"""Image hosting for the sites that need a public URL (Instagram).

Instagram fetches the image itself, so the bytes go to a host for a few
minutes. The host is behind a tiny interface so it can be swapped later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

IMGBB_UPLOAD = "https://api.imgbb.com/1/upload"
TIMEOUT = httpx.Timeout(120.0, connect=20.0)


class ImageHostError(Exception):
    """A failure with a message that is safe to show the user."""


@dataclass
class HostedImage:
    url: str
    delete_url: str = ""


class ImageHost(ABC):
    """Somewhere an image can live for a few minutes at a public URL."""

    name = ""

    @abstractmethod
    def upload(self, data: bytes, filename: str) -> HostedImage:
        """Put the bytes somewhere public and return the URL."""

    def delete(self, hosted: HostedImage) -> None:
        """Best effort; many hosts expire the image on their own."""


class ImgBBHost(ImageHost):
    """imgbb's free API, with a short expiry so nothing lingers."""

    name = "imgbb"

    def __init__(self, api_key: str, expiration: int = 3600):
        self.api_key = api_key
        self.expiration = expiration

    def upload(self, data: bytes, filename: str) -> HostedImage:
        response = httpx.post(
            IMGBB_UPLOAD,
            params={"key": self.api_key, "expiration": self.expiration},
            files={"image": (filename, data, "image/jpeg")},
            timeout=TIMEOUT,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("success"):
            detail = ""
            if isinstance(body, dict):
                error = body.get("error")
                detail = str((error or {}).get("message") if isinstance(error, dict) else error or "")
            raise ImageHostError(f"imgbb upload failed: {detail or f'HTTP {response.status_code}'}")
        data_out = body.get("data") if isinstance(body.get("data"), dict) else {}
        url = str(data_out.get("url") or data_out.get("display_url") or "")
        if not url:
            raise ImageHostError("imgbb did not return an image URL.")
        return HostedImage(url=url, delete_url=str(data_out.get("delete_url") or ""))

    def delete(self, hosted: HostedImage) -> None:
        """imgbb's delete link is a web page with a confirm button, so this is a
        no-op; the one-hour `expiration` cleans the image up instead."""
        return
