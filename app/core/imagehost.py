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


UGUU_UPLOAD = "https://uguu.se/upload"


class UguuHost(ImageHost):
    """uguu.se: no account or key, and files delete themselves after 3 hours.

    The backup when imgbb is down. Its links serve the image itself (what
    Instagram's fetcher needs); Litterbox and 0x0.st were tried and are
    blocked or closed, and tmpfiles.org only links to a web page.
    """

    name = "uguu"

    def upload(self, data: bytes, filename: str) -> HostedImage:
        try:
            response = httpx.post(UGUU_UPLOAD, files={"files[]": (filename, data, "image/jpeg")},
                                  timeout=TIMEOUT)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageHostError(f"uguu upload failed: {type(exc).__name__}") from exc
        files = body.get("files") if isinstance(body, dict) else None
        url = str((files[0] or {}).get("url") or "") if isinstance(files, list) and files else ""
        if response.status_code != 200 or not body.get("success") or not url:
            detail = str(body.get("description") or "") if isinstance(body, dict) else ""
            raise ImageHostError(f"uguu upload failed: {detail or f'HTTP {response.status_code}'}")
        return HostedImage(url=url)


class FallbackHost(ImageHost):
    """Try each host in turn: a maintenance day at one host does not stop posting."""

    name = "fallback"

    def __init__(self, hosts: list[ImageHost]):
        self.hosts = list(hosts)
        self._owner: dict[str, ImageHost] = {}

    def upload(self, data: bytes, filename: str) -> HostedImage:
        errors: list[str] = []
        for host in self.hosts:
            try:
                hosted = host.upload(data, filename)
            except ImageHostError as exc:
                errors.append(str(exc))
                continue
            self._owner[hosted.url] = host
            return hosted
        raise ImageHostError("; ".join(errors) or "no image host is set up")

    def delete(self, hosted: HostedImage) -> None:
        owner = self._owner.get(hosted.url)
        if owner is not None:
            owner.delete(hosted)
