"""Instagram connector — Instagram API with Instagram Login.

No Facebook Page is needed: a Business or Creator account is enough. Meta
fetches the image itself, so the bytes go to an ImageHost (imgbb by default)
that expires them within the hour, then become media containers.
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from ..core import secrets
from ..core.imagehost import HostedImage, ImgBBHost
from ..core.images import Prepared, pad_to_ratio, prepare
from ..core.oauth import Provider, needs_refresh, tokens_from
from .base import Field, PublishError
from .oauth import OAuthConnector

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
LONG_TOKEN_URL = "https://graph.instagram.com/access_token"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
API_VERSION = "v26.0"
API = f"https://graph.instagram.com/{API_VERSION}"
SCOPES = "instagram_business_basic,instagram_business_content_publish"
TIMEOUT = httpx.Timeout(120.0, connect=20.0)
POLL_TRIES = 10
POLL_DELAY = 2.0
REFRESH_MARGIN = 7 * 86400  # ask for a fresh 60-day token while a week is left
MAX_COLLABORATORS = 3


def parse_collaborators(raw) -> list[str]:
    """Usernames without @, deduped in order, at most three."""
    if isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        items = re.split(r"[,\s]+", str(raw or ""))
    out: list[str] = []
    for item in items:
        name = item.strip().lstrip("@").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= MAX_COLLABORATORS:
            break
    return out


def _token_entry(body: dict) -> dict:
    """Meta answers either {"data": [{...}]} or a flat {"access_token": ...}."""
    entries = body.get("data") if isinstance(body, dict) else None
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return entries[0]
    return body if isinstance(body, dict) else {}


class Instagram(OAuthConnector):
    id = "instagram"
    label = "Instagram"
    caption_limit = 2200
    max_images = 10
    max_tags = 30
    max_bytes = 8_000_000
    max_side = 1440  # what Instagram recommends
    original_formats = ("JPEG",)
    api = API
    oauth_provider = Provider(authorize_url=AUTHORIZE_URL, token_url=TOKEN_URL, scope=SCOPES)
    fields = [
        Field("client_id", "Instagram app ID",
              help="developers.facebook.com → your app → Instagram → API setup with Instagram login."),
        Field("client_secret", "Instagram app secret", secret=True),
        Field("redirect_uri", "Redirect URI", required=False,
              placeholder="https://localhost/artdrop",
              help="Leave empty for the local one. Must also be listed in the app's business login settings."),
        Field("imgbb_api_key", "imgbb API key", secret=True,
              help="Free from api.imgbb.com — Instagram needs the image on a public URL."),
    ]
    oauth_help = (
        "Create an app at developers.facebook.com and pick the 'Instagram API with Instagram "
        "login' use case. Add your Instagram account under API setup with Instagram login → "
        "Generate access tokens → Add account (the old 'Instagram Tester' role does not work for "
        "new apps). Add the redirect URI https://localhost/artdrop — exactly, with no trailing "
        "slash — to the app, then paste the app ID, app secret and a free imgbb API key here and "
        "Save. Press Connect, allow access, then Test connection. If the token exchange says the "
        "code is wrong, press Connect again and paste the newest address."
    )
    setup_help = (
        "1) You need an Instagram Professional account (Business or Creator) — it is free to "
        "switch in the Instagram app. 2) Create an app at developers.facebook.com and pick the "
        "Instagram use case. 3) Under API setup with Instagram login, press Generate access "
        "tokens → Add account to attach your Instagram account (the Instagram Tester role is "
        "retired). 4) Register https://localhost/artdrop as the redirect URI — no trailing "
        "slash. 5) Paste the app ID, app secret, that redirect URI and a free imgbb key from "
        "api.imgbb.com here. 6) Save, Connect, then Test connection."
    )

    # ------------------------------------------------------------------ helpers
    def prepare_images(self, post) -> list[Prepared]:
        """Always JPEG, inside Instagram's 4:5 … 1.91:1 window, max side 1440."""
        prepared = []
        for path in post.images:
            item = prepare(path, max_bytes=self.max_bytes, max_side=self.max_side,
                           allow_original=False)
            prepared.append(pad_to_ratio(item, max_bytes=self.max_bytes, max_side=self.max_side))
        return prepared

    def host(self, cfg: dict) -> ImgBBHost:
        key = str(cfg.get("imgbb_api_key") or "").strip()
        if not key:
            raise PublishError("Instagram needs a free imgbb API key — add it in Accounts.")
        return ImgBBHost(key)

    def _json(self, response: httpx.Response, fallback: str) -> dict:
        """Both error shapes: Graph's {"error": {...}} and Instagram's flat one."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        detail = ""
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif error:
            detail = str(error)
        if not detail:
            detail = str(body.get("error_message") or "")
        error_type = str(body.get("error_type") or "")
        if response.status_code != 200 or detail or error_type:
            reason = detail or f"HTTP {response.status_code}"
            if error_type:
                reason = f"{error_type} — {reason}"
            raise PublishError(f"{fallback}: {reason}")
        return body

    def config_warning(self, cfg: dict) -> str:
        redirect = str((cfg or {}).get("redirect_uri") or "").strip()
        if redirect.endswith("/") and redirect != self.redirect_default():
            return ("Instagram only accepts the exact URI registered in its Business login "
                    "settings — https://localhost/artdrop has no trailing slash.")
        return ""

    def _user_id(self, cfg: dict) -> str:
        user_id = str((cfg.get("tokens") or {}).get("user_id") or "").strip()
        if not user_id:
            raise PublishError("Instagram account id is missing — press Connect again in Accounts.")
        return user_id

    async def _container(self, client: httpx.AsyncClient, user_id: str, token: str,
                         fields: dict) -> str:
        response = await client.post(f"{self.api}/{user_id}/media",
                                     data={**fields, "access_token": token})
        body = self._json(response, "Instagram media container failed")
        container_id = str(body.get("id") or "")
        if not container_id:
            raise PublishError("Instagram did not return a container id.")
        return container_id

    async def _wait_ready(self, client: httpx.AsyncClient, container_id: str, token: str) -> None:
        for attempt in range(POLL_TRIES):
            response = await client.get(f"{self.api}/{container_id}",
                                        params={"fields": "status_code", "access_token": token})
            body = self._json(response, "Instagram container check failed")
            status = str(body.get("status_code") or "")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the image ({status}).")
            if attempt < POLL_TRIES - 1:
                await asyncio.sleep(POLL_DELAY)
        raise PublishError("Instagram took too long to process the image.")

    async def _permalink(self, client: httpx.AsyncClient, media_id: str, token: str) -> str:
        if not media_id:
            return ""
        try:
            response = await client.get(f"{self.api}/{media_id}",
                                        params={"fields": "permalink", "access_token": token})
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return ""
        return str(body.get("permalink") or "") if isinstance(body, dict) else ""

    # ------------------------------------------------------------ oauth flow
    async def complete(self, cfg: dict, code: str, redirect_uri: str | None = None,
                       code_verifier: str | None = None) -> None:
        """Instagram's flow: code → short-lived token → 60-day token."""
        code = str(code or "").strip()
        if not code:
            raise PublishError("No code was given.")
        redirect = redirect_uri or self.redirect_uri(cfg)
        client_id = str(cfg.get("client_id") or "").strip()
        client_secret = str(cfg.get("client_secret") or "").strip()
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            short_response = await client.post(TOKEN_URL, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect,
                "code": code,
            })
            short_body = self._json(short_response, "Instagram code exchange failed")
            entry = _token_entry(short_body)
            short_token = str(entry.get("access_token") or "")
            user_id = str(entry.get("user_id") or "")
            if not short_token:
                raise PublishError("Instagram did not return an access token.")
            long_response = await client.get(LONG_TOKEN_URL, params={
                "grant_type": "ig_exchange_token",
                "client_secret": client_secret,
                "access_token": short_token,
            })
        long_body = self._json(long_response, "Instagram long-lived token failed")
        tokens = tokens_from(long_body)
        tokens["user_id"] = user_id
        self.save_tokens(cfg, tokens)

    async def access_token(self, cfg: dict) -> str:
        tokens = dict((cfg.get("tokens") or {}))
        if tokens.get("access_token") and not needs_refresh(tokens, REFRESH_MARGIN):
            return tokens["access_token"]
        if not tokens.get("access_token"):
            raise PublishError("Not connected to Instagram yet — open Accounts and press Connect.")
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(REFRESH_URL, params={
                "grant_type": "ig_refresh_token",
                "access_token": tokens["access_token"],
            })
        body = self._json(response, "Instagram token refresh failed")
        refreshed = tokens_from(body)
        refreshed["user_id"] = tokens.get("user_id", "")
        self.save_tokens(cfg, refreshed)
        return refreshed["access_token"]

    async def _remember_username(self, client: httpx.AsyncClient, cfg: dict, token: str) -> None:
        """Fill in a missing username (the migrated account starts without one)."""
        if str(cfg.get("username") or "").strip():
            return
        try:
            response = await client.get(
                f"{self.api}/me",
                params={"fields": "user_id,username", "access_token": token},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return
        name = str(body.get("username") or "") if isinstance(body, dict) else ""
        if name:
            self.remember(cfg, {"username": name})

    # ---------------------------------------------------------------- interface
    async def test(self, cfg: dict) -> str:
        token = await self.access_token(cfg)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                f"{self.api}/me",
                params={"fields": "user_id,username", "access_token": token},
            )
        body = self._json(response, "Instagram profile check failed")
        name = str(body.get("username") or "")
        if name:
            self.remember(cfg, {"username": name})
        user_id = body.get("user_id") or body.get("id") or self._user_id(cfg)
        return f"Connected as @{name or 'unknown'} (id {user_id})"

    def resolved_collaborators(self, post, cfg: dict) -> list[str]:
        """Who to invite: the card's value when the key is there, else the default.

        An empty value on the card means "no collaborators on this post".
        """
        if not bool(cfg.get("can_invite")):
            return []
        options = self.options_for(post, cfg)
        if "collaborators" in options:
            return parse_collaborators(options.get("collaborators"))
        return parse_collaborators(cfg.get("collaborators"))

    def preview_extra(self, post, cfg: dict) -> dict:
        if not bool(cfg.get("can_invite")):
            return {}
        return {"collaborators": self.resolved_collaborators(post, cfg)}

    async def send(self, post, cfg: dict, text: str, images: list) -> tuple[str, str]:
        token = await self.access_token(cfg)
        user_id = self._user_id(cfg)
        host = self.host(cfg)
        options = self.options_for(post, cfg)
        alt = str(options.get("alt_text") or post.alt_text or "").strip()
        collaborators = self.resolved_collaborators(post, cfg)
        collab_field = {"collaborators": json.dumps(collaborators)} if collaborators else {}

        uploaded: list[HostedImage] = []
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            try:
                await self._remember_username(client, cfg, token)
                for image in images:
                    uploaded.append(await asyncio.to_thread(host.upload, image.data, image.filename))

                if len(uploaded) == 1:
                    container_id = await self._container(client, user_id, token, {
                        "image_url": uploaded[0].url,
                        "caption": text,
                        **({"alt_text": alt} if alt else {}),
                        **collab_field,
                    })
                else:
                    children = []
                    for item in uploaded:
                        children.append(await self._container(client, user_id, token, {
                            "image_url": item.url,
                            "is_carousel_item": "true",
                            **({"alt_text": alt} if alt else {}),
                        }))
                    container_id = await self._container(client, user_id, token, {
                        "media_type": "CAROUSEL",
                        "children": ",".join(children),
                        "caption": text,
                        **collab_field,
                    })

                await self._wait_ready(client, container_id, token)
                response = await client.post(
                    f"{self.api}/{user_id}/media_publish",
                    data={"creation_id": container_id, "access_token": token},
                )
                body = self._json(response, "Instagram publish failed")
                url = await self._permalink(client, str(body.get("id") or ""), token)
            finally:
                for item in uploaded:
                    await asyncio.to_thread(host.delete, item)

        note = ""
        if collaborators:
            names = ", ".join(f"@{name}" for name in collaborators)
            note = f"collab invite sent to {names} — accept it in the Instagram app"
        return url, note
