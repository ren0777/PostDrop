"""Shared OAuth connector base: loopback callback plus the paste-code fallback."""
from __future__ import annotations

import httpx

from ..core import accounts, net, oauth, paths, secrets
from ..core.oauth import Provider
from .base import Connector, PublishError


class OAuthConnector(Connector):
    is_oauth = True
    oauth_provider: Provider
    oauth_help = ""

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        """An httpx client for this site's API traffic.

        Sites that need it (Tumblr) get the narrow TLS curve; every other
        connector keeps the default context.
        """
        return httpx.AsyncClient(
            timeout=timeout,
            verify=net.tls_context(self.oauth_provider.narrow_tls),
        )

    def redirect_default(self) -> str:
        return f"http://127.0.0.1:{paths.PORT}/oauth/{self.id}"

    def redirect_uri(self, cfg: dict) -> str:
        return str((cfg or {}).get("redirect_uri") or "").strip() or self.redirect_default()

    def connected(self, cfg: dict) -> bool:
        return bool(((cfg or {}).get("tokens") or {}).get("access_token"))

    def save_tokens(self, cfg: dict, tokens: dict) -> None:
        """Store tokens for this card (accounts keep their own keyring entry)."""
        cfg["tokens"] = tokens
        card_id = self.card_id(cfg)
        if ":" in card_id:
            site, _, key = card_id.partition(":")
            accounts.update(site, key, {"tokens": tokens})
        else:
            secrets.save(self.id, cfg)

    def start_url(self, cfg: dict) -> str:
        """The provider URL to open; remembers a one-shot state value.

        For PKCE providers the code verifier is stored with the state, and the
        S256 challenge goes into the authorize URL.
        """
        verifier = oauth.new_code_verifier() if self.oauth_provider.pkce else ""
        state = oauth.new_state(self.card_id(cfg), verifier)
        return oauth.authorize_url(
            self.oauth_provider,
            client_id=str(cfg.get("client_id") or "").strip(),
            redirect_uri=self.redirect_uri(cfg),
            state=state,
            code_challenge=oauth.code_challenge(verifier) if verifier else None,
        )

    async def complete(self, cfg: dict, code: str, redirect_uri: str | None = None,
                       code_verifier: str | None = None) -> None:
        """Exchange an authorization code for tokens and store them."""
        code = str(code or "").strip()
        if not code:
            raise PublishError("No code was given.")
        tokens = await oauth.exchange_code(
            self.oauth_provider,
            client_id=str(cfg.get("client_id") or "").strip(),
            client_secret=str(cfg.get("client_secret") or "").strip(),
            code=code,
            redirect_uri=redirect_uri or self.redirect_uri(cfg),
            code_verifier=code_verifier,
        )
        self.save_tokens(cfg, tokens)

    async def access_token(self, cfg: dict) -> str:
        """A valid bearer token, refreshing (and saving) it when it has expired."""
        tokens = dict((cfg or {}).get("tokens") or {})
        if oauth.needs_refresh(tokens):
            if not tokens.get("refresh_token"):
                raise PublishError(
                    f"Not connected to {self.label} yet — open Accounts and press Connect."
                )
            tokens = await oauth.refresh_tokens(
                self.oauth_provider,
                client_id=str(cfg.get("client_id") or "").strip(),
                client_secret=str(cfg.get("client_secret") or "").strip(),
                refresh_token=str(tokens["refresh_token"]),
            )
            self.save_tokens(cfg, tokens)
        return tokens["access_token"]
