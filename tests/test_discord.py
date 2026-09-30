"""Discord: the post link comes from the webhook's own guild id."""
import asyncio

import pytest

from app.core import secrets
from app.core.images import Prepared
from app.core.post import Post
from app.platforms import discord


def _prepared():
    return Prepared(data=b"png", mime="image/png", width=8, height=8, filename="art.png")


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(secrets, "load", lambda key: dict(store.get(key, {})))
    monkeypatch.setattr(secrets, "save", lambda key, cfg: store.__setitem__(key, dict(cfg)))
    monkeypatch.setattr(secrets, "delete", lambda key: store.pop(key, None))
    return store


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient: a scripted GET and POST, plus a call log."""

    calls: list = []
    get_payload: dict = {}
    post_payload: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        FakeClient.calls.append(("GET", url))
        return FakeResponse(FakeClient.get_payload)

    async def post(self, url, **kwargs):
        FakeClient.calls.append(("POST", url))
        return FakeResponse(FakeClient.post_payload)


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.get_payload = {"guild_id": "4242", "channel_id": "77", "name": "art"}
    FakeClient.post_payload = {"id": "99", "channel_id": "77"}
    monkeypatch.setattr(discord.httpx, "AsyncClient", FakeClient)
    return FakeClient


WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def _post():
    return Post(images=[], caption="hello")


def test_a_link_is_built_from_the_cached_guild_id():
    cfg = {"webhook_url": WEBHOOK, "guild_id": "4242"}

    url, note = asyncio.run(discord.Discord().send(_post(), cfg, "hello", [_prepared()]))

    assert url == "https://discord.com/channels/4242/77/99"
    assert note == ""
    assert [call[0] for call in FakeClient.calls] == ["POST"]  # no extra round trip


def test_the_guild_id_is_learned_and_remembered_when_missing(fake_keyring):
    cfg = {"webhook_url": WEBHOOK}

    url, _ = asyncio.run(discord.Discord().send(_post(), cfg, "hello", [_prepared()]))

    assert url == "https://discord.com/channels/4242/77/99"
    assert [call[0] for call in FakeClient.calls] == ["POST", "GET"]
    assert fake_keyring["discord"]["guild_id"] == "4242"
    assert fake_keyring["discord"]["channel_id"] == "77"


def test_test_connection_remembers_the_guild(fake_keyring):
    detail = asyncio.run(discord.Discord().test({"webhook_url": WEBHOOK}))

    assert "channel 77" in detail
    assert fake_keyring["discord"]["guild_id"] == "4242"


def test_a_post_with_nothing_to_link_to_stays_empty():
    FakeClient.post_payload = {"id": "99"}  # no channel, and the webhook has none either
    FakeClient.get_payload = {}

    url, note = asyncio.run(discord.Discord().send(
        _post(), {"webhook_url": WEBHOOK}, "hello", [_prepared()]))

    assert url == "" and note == ""


def test_a_rejected_webhook_says_so():
    fake = FakeClient
    fake.get_payload = None
    with pytest.raises(Exception) as info:
        asyncio.run(discord.Discord().test({"webhook_url": WEBHOOK}))
    assert "Discord" in str(info.value)
