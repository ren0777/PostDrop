"""Bluesky connector — plain XRPC over httpx, no SDK."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from .base import Connector, Field, PublishError

DEFAULT_PDS = "https://bsky.social"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)

_TAG_RE = re.compile(r"(?<![\w#])#([A-Za-z0-9_]+)")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TRIM = ".,;:!?)]}'\""


def build_facets(text: str) -> list[dict]:
    """Rich-text facets for #hashtags and links, using UTF-8 byte offsets."""
    facets: list[dict] = []
    for match in _TAG_RE.finditer(text):
        start = len(text[: match.start()].encode("utf-8"))
        end = len(text[: match.end()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": match.group(1)}],
        })
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TRIM)
        if not url:
            continue
        start = len(text[: match.start()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
        })
    return facets


def _describe_error(response: httpx.Response, fallback: str) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = (body.get("message") or body.get("error")
              or response.text[:200] or f"HTTP {response.status_code}")
    return f"{fallback}: {detail}"


class Bluesky(Connector):
    id = "bluesky"
    label = "Bluesky"
    caption_limit = 300
    max_images = 4
    max_bytes = 976_560
    max_side = 2000
    original_formats = ("JPEG", "PNG")
    fields = [
        Field("handle", "Handle", placeholder="you.bsky.social"),
        Field(
            "app_password", "App password", secret=True,
            placeholder="xxxx-xxxx-xxxx-xxxx",
            help="Bluesky → Settings → Privacy and security → App passwords.",
        ),
    ]
    setup_help = (
        "In Bluesky, open Settings → Privacy and security → App passwords and create one "
        "for ArtDrop. Paste your handle (you.bsky.social) and the app password here. "
        "Never use your real account password."
    )

    # ------------------------------------------------------------------ helpers
    def _pds(self, session: dict) -> str:
        did_doc = session.get("didDoc") or {}
        for service in did_doc.get("service") or []:
            if service.get("id") in ("#atproto_pds", "atproto_pds"):
                endpoint = service.get("serviceEndpoint")
                if endpoint:
                    return str(endpoint).rstrip("/")
        return DEFAULT_PDS

    async def _login(self, client: httpx.AsyncClient, cfg: dict) -> dict:
        response = await client.post(
            f"{DEFAULT_PDS}/xrpc/com.atproto.server.createSession",
            json={"identifier": cfg.get("handle", ""), "password": cfg.get("app_password", "")},
        )
        if response.status_code != 200:
            raise PublishError(_describe_error(response, "Bluesky login failed"))
        return response.json()

    # ---------------------------------------------------------------- interface
    async def test(self, cfg: dict) -> str:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            session = await self._login(client, cfg)
        return f"Connected as @{session.get('handle') or cfg.get('handle')}"

    async def send(self, post, cfg: dict, text: str, images: list) -> tuple[str, str]:
        alt = (self.options_for(post, cfg).get("alt_text") or post.alt_text or "").strip()
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            session = await self._login(client, cfg)
            pds = self._pds(session)
            headers = {"Authorization": f"Bearer {session['accessJwt']}"}

            blobs = []
            for image in images:
                response = await client.post(
                    f"{pds}/xrpc/com.atproto.repo.uploadBlob",
                    headers={**headers, "Content-Type": image.mime},
                    content=image.data,
                )
                if response.status_code != 200:
                    raise PublishError(_describe_error(response, f"Bluesky rejected {image.filename}"))
                blobs.append(response.json()["blob"])

            record = {
                "$type": "app.bsky.feed.post",
                "text": text,
                "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "embed": {
                    "$type": "app.bsky.embed.images",
                    "images": [
                        {
                            "alt": alt,
                            "image": blob,
                            "aspectRatio": {"width": image.width, "height": image.height},
                        }
                        for blob, image in zip(blobs, images)
                    ],
                },
            }
            facets = build_facets(text)
            if facets:
                record["facets"] = facets

            response = await client.post(
                f"{pds}/xrpc/com.atproto.repo.createRecord",
                headers=headers,
                json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
            )
            if response.status_code != 200:
                raise PublishError(_describe_error(response, "Bluesky post failed"))
            uri = response.json().get("uri", "")

        rkey = uri.rsplit("/", 1)[-1]
        handle = session.get("handle") or cfg.get("handle")
        return f"https://bsky.app/profile/{handle}/post/{rkey}", ""
