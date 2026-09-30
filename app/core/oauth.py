"""OAuth 2.0 helpers shared by the Tumblr and DeviantArt connectors.

Only the authorization-code flow is implemented. Two modes are supported:
the automatic loopback callback (the provider redirects to 127.0.0.1:8765)
and the paste-the-code fallback for providers that refuse localhost URIs.
"""
from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from secrets import token_urlsafe
from urllib.parse import urlencode

import httpx

from . import net

USER_AGENT = "ArtDrop/1.0"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)
STATE_TTL = 600.0


class OAuthError(Exception):
    """A failure with a message that is safe to show the user."""


@dataclass
class Provider:
    authorize_url: str
    token_url: str
    scope: str = ""
    extra_authorize: dict = field(default_factory=dict)
    pkce: bool = False  # DeviantArt (OAuth 2.1) requires PKCE; Tumblr does not
    narrow_tls: bool = False  # Tumblr's TLS handshake needs the X25519 curve


def authorize_url(provider: Provider, *, client_id: str, redirect_uri: str, state: str,
                  code_challenge: str | None = None) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if provider.scope:
        params["scope"] = provider.scope
    if provider.pkce and code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    params.update(provider.extra_authorize)
    return f"{provider.authorize_url}?{urlencode(params)}"


def new_code_verifier() -> str:
    """A PKCE verifier: 43–128 characters from [A-Za-z0-9-._~]."""
    return token_urlsafe(64)[:128]


def code_challenge(verifier: str) -> str:
    """base64url(sha256(verifier)) without padding, the S256 method."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def tokens_from(body: dict) -> dict:
    """Normalise a token response; adds an expiry with a minute of head-room."""
    tokens = {
        "access_token": str(body.get("access_token") or ""),
        "refresh_token": str(body.get("refresh_token") or ""),
        "token_type": str(body.get("token_type") or "Bearer"),
        "scope": str(body.get("scope") or ""),
    }
    expires_in = body.get("expires_in")
    if expires_in:
        try:
            tokens["expires_at"] = time.time() + max(0, int(expires_in) - 60)
        except (TypeError, ValueError):
            pass
    if not tokens["access_token"]:
        raise OAuthError("the provider did not return an access token")
    return tokens


def needs_refresh(tokens: dict, margin: float = 60.0) -> bool:
    """True when the token is missing, or expires within `margin` seconds."""
    if not tokens or not tokens.get("access_token"):
        return True
    expires_at = tokens.get("expires_at")
    return bool(expires_at) and time.time() + margin >= float(expires_at)


async def _token_request(provider: Provider, data: dict) -> dict:
    """One POST to the token endpoint. Isolated so tests can replace it."""
    async with httpx.AsyncClient(timeout=TIMEOUT,
                                 verify=net.tls_context(provider.narrow_tls)) as client:
        response = await client.post(
            provider.token_url,
            data=data,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict) or body.get("error"):
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error_description") or body.get("error") or "")
        raise OAuthError(
            f"token endpoint returned HTTP {response.status_code}" + (f" — {detail}" if detail else "")
        )
    return body


async def exchange_code(provider: Provider, *, client_id: str, client_secret: str,
                        code: str, redirect_uri: str,
                        code_verifier: str | None = None) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    body = await _token_request(provider, data)
    return tokens_from(body)


async def refresh_tokens(provider: Provider, *, client_id: str, client_secret: str,
                         refresh_token: str) -> dict:
    body = await _token_request(provider, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    if not body.get("refresh_token"):
        body["refresh_token"] = refresh_token  # keep the old one when not rotated
    return tokens_from(body)


# --------------------------------------------------------------- state store
_states: dict[str, dict] = {}


def new_state(platform_id: str, code_verifier: str = "") -> str:
    now = time.time()
    for key, value in list(_states.items()):
        if now - value["created"] > STATE_TTL:
            del _states[key]
    state = token_urlsafe(16)
    _states[state] = {"platform": platform_id, "created": now, "verifier": code_verifier}
    return state


def take_state(state: str, platform_id: str) -> dict | None:
    """Consume a state value; returns its entry (with the PKCE verifier) or None."""
    entry = _states.pop(state or "", None)
    if not entry or entry["platform"] != platform_id:
        return None
    if time.time() - entry["created"] > STATE_TTL:
        return None
    return entry


def check_state(state: str, platform_id: str) -> bool:
    """One-shot check that this state came from us, for this site."""
    return take_state(state, platform_id) is not None


def latest_verifier(platform_id: str) -> str:
    """The most recent unexpired code verifier for a site (bare-code pastes)."""
    newest, verifier = 0.0, ""
    now = time.time()
    for entry in _states.values():
        if entry["platform"] != platform_id or now - entry["created"] > STATE_TTL:
            continue
        if entry.get("verifier") and entry["created"] > newest:
            newest, verifier = entry["created"], entry["verifier"]
    return verifier
