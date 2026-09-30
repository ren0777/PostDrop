"""OAuth helpers: URLs, tokens, refresh logic, the state store."""
import asyncio
import base64
import hashlib
import time

import pytest

from app.core import oauth


@pytest.fixture(autouse=True)
def clean_states():
    oauth._states.clear()
    yield
    oauth._states.clear()


def test_authorize_url_has_the_expected_parameters():
    provider = oauth.Provider(authorize_url="https://example.com/auth",
                              token_url="https://example.com/token", scope="basic write")
    url = oauth.authorize_url(provider, client_id="cid",
                              redirect_uri="http://127.0.0.1:8765/oauth/x", state="st")
    assert url.startswith("https://example.com/auth?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "scope=basic+write" in url
    assert "state=st" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fx" in url


def test_tokens_from_adds_expiry_and_requires_a_token():
    tokens = oauth.tokens_from({"access_token": "a", "expires_in": 3600, "refresh_token": "r"})
    assert tokens["access_token"] == "a"
    assert tokens["refresh_token"] == "r"
    assert tokens["expires_at"] > 0
    with pytest.raises(oauth.OAuthError):
        oauth.tokens_from({"expires_in": 3600})


def test_needs_refresh():
    assert oauth.needs_refresh({}) is True
    assert oauth.needs_refresh({"access_token": "a"}) is False
    assert oauth.needs_refresh({"access_token": "a", "expires_at": 1}) is True
    assert oauth.needs_refresh({"access_token": "a", "expires_at": 10 ** 12}) is False


def test_state_is_single_use_and_bound_to_the_platform():
    state = oauth.new_state("tumblr")
    assert oauth.check_state(state, "deviantart") is False  # wrong platform consumes it
    state = oauth.new_state("tumblr")
    assert oauth.check_state(state, "tumblr") is True
    assert oauth.check_state(state, "tumblr") is False  # second use fails


def test_exchange_and_refresh_payloads(monkeypatch):
    calls = []

    async def fake_request(provider, data):
        calls.append(data)
        if data["grant_type"] == "authorization_code":
            return {"access_token": "new", "refresh_token": "rot", "expires_in": 100}
        return {"access_token": "refreshed", "expires_in": 100}

    monkeypatch.setattr(oauth, "_token_request", fake_request)
    provider = oauth.Provider(authorize_url="a", token_url="t")

    tokens = asyncio.run(oauth.exchange_code(provider, client_id="c", client_secret="s",
                                             code="co", redirect_uri="r"))
    assert tokens["access_token"] == "new"
    assert tokens["refresh_token"] == "rot"
    assert calls[0] == {
        "grant_type": "authorization_code", "code": "co", "client_id": "c",
        "client_secret": "s", "redirect_uri": "r",
    }

    refreshed = asyncio.run(oauth.refresh_tokens(provider, client_id="c",
                                                 client_secret="s", refresh_token="rot"))
    assert refreshed["access_token"] == "refreshed"
    assert refreshed["refresh_token"] == "rot"  # kept when the provider does not rotate


# ----------------------------------------------------------------------- PKCE
def test_authorize_url_adds_pkce_only_when_asked():
    plain = oauth.Provider(authorize_url="https://x/auth", token_url="https://x/token")
    url = oauth.authorize_url(plain, client_id="c", redirect_uri="r", state="s",
                              code_challenge="CH")
    assert "code_challenge" not in url

    pkce = oauth.Provider(authorize_url="https://x/auth", token_url="https://x/token", pkce=True)
    url = oauth.authorize_url(pkce, client_id="c", redirect_uri="r", state="s",
                              code_challenge="CH")
    assert "code_challenge=CH" in url
    assert "code_challenge_method=S256" in url

    url = oauth.authorize_url(pkce, client_id="c", redirect_uri="r", state="s")
    assert "code_challenge" not in url


def test_code_challenge_is_base64url_sha256_without_padding():
    verifier = oauth.new_code_verifier()
    assert 43 <= len(verifier) <= 128
    challenge = oauth.code_challenge(verifier)
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected
    assert "=" not in challenge


def test_exchange_code_sends_the_verifier_only_when_given(monkeypatch):
    calls = []

    async def fake_request(provider, data):
        calls.append(data)
        return {"access_token": "t"}

    monkeypatch.setattr(oauth, "_token_request", fake_request)
    provider = oauth.Provider(authorize_url="a", token_url="t", pkce=True)

    asyncio.run(oauth.exchange_code(provider, client_id="c", client_secret="s", code="co",
                                    redirect_uri="r", code_verifier="the-verifier"))
    assert calls[0]["code_verifier"] == "the-verifier"

    asyncio.run(oauth.exchange_code(provider, client_id="c", client_secret="s", code="co",
                                    redirect_uri="r"))
    assert "code_verifier" not in calls[1]


def test_state_carries_the_verifier_and_latest_finds_the_newest():
    state = oauth.new_state("deviantart", "verifier-1")
    entry = oauth.take_state(state, "deviantart")
    assert entry["verifier"] == "verifier-1"
    assert oauth.latest_verifier("deviantart") == ""  # nothing left

    oauth.new_state("deviantart", "verifier-2")
    time.sleep(0.002)
    oauth.new_state("deviantart", "verifier-3")
    assert oauth.latest_verifier("deviantart") == "verifier-3"
    assert oauth.latest_verifier("tumblr") == ""


# ------------------------------------------------------------------------ TLS
def test_provider_defaults_have_no_pkce_and_no_narrow_tls():
    provider = oauth.Provider(authorize_url="a", token_url="t")
    assert provider.pkce is False
    assert provider.narrow_tls is False


def test_token_request_uses_the_narrow_context_only_when_asked(monkeypatch):
    captured = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "t"}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            return FakeResponse()

    from app.core import net

    monkeypatch.setattr(oauth.httpx, "AsyncClient", FakeClient)
    narrow = oauth.Provider(authorize_url="a", token_url="t", narrow_tls=True)
    plain = oauth.Provider(authorize_url="a", token_url="t")
    asyncio.run(oauth._token_request(narrow, {"grant_type": "x"}))
    asyncio.run(oauth._token_request(plain, {"grant_type": "x"}))

    assert captured[0]["verify"] is net.tls_context(True)
    assert captured[1]["verify"] is net.tls_context(False)


def test_the_checklist_redirects_are_the_defaults():
    """Tumblr rejects IP-address URLs; DeviantArt only accepts https."""
    from app.platforms import REGISTRY

    assert REGISTRY["tumblr"].redirect_default() == "http://localhost:8765/oauth/tumblr"
    assert REGISTRY["deviantart"].redirect_default() == "https://127.0.0.1/artdrop"
    # the others keep the loopback default, which the local callback serves
    assert REGISTRY["instagram"].redirect_default() == "http://127.0.0.1:8765/oauth/instagram"
