"""Instagram: image preparation, the token flow, containers and carousels."""
import asyncio
import time

import httpx
import pytest
from PIL import Image

from app.core.images import Prepared, prepare
from app.core.post import Post
from app.platforms.instagram import Instagram


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    """Answers the handful of endpoints the connector uses."""

    def __init__(self, calls):
        self.calls = calls
        self.containers = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, params=None, **kwargs):
        self.calls.append(("POST", url, dict(data or {})))
        if url.endswith("/media_publish"):
            return _Response(200, {"id": "media-1"})
        if url.endswith("/media"):
            self.containers += 1
            return _Response(200, {"id": f"container-{self.containers}"})
        return _Response(404, {"error": {"message": "nope"}})

    async def get(self, url, params=None, **kwargs):
        self.calls.append(("GET", url, dict(params or {})))
        if (params or {}).get("fields") == "status_code":
            return _Response(200, {"status_code": "FINISHED"})
        if (params or {}).get("fields") == "permalink":
            return _Response(200, {"permalink": "https://www.instagram.com/p/abc/"})
        return _Response(200, {"username": "artist", "user_id": "42"})


class _FakeHost:
    def __init__(self):
        self.uploaded = []
        self.deleted = []

    def upload(self, data, filename):
        self.uploaded.append(filename)
        from app.core.imagehost import HostedImage
        return HostedImage(url=f"https://i.ibb.co/{filename}", delete_url="https://ibb.co/x")

    def delete(self, hosted):
        self.deleted.append(hosted.url)


def _prepared(name="art.jpg"):
    return Prepared(data=b"jpeg-bytes", mime="image/jpeg", width=800, height=600, filename=name)


def _cfg():
    return {"tokens": {"access_token": "tok", "user_id": "42", "expires_at": time.time() + 10 ** 7}}


# ------------------------------------------------------------------ preparation
def test_prepare_images_reencodes_and_pads(tmp_path):
    path = tmp_path / "tall.png"
    Image.new("RGB", (1000, 2000), (10, 60, 120)).save(path, "PNG")
    post = Post(images=[path], caption="x")
    prepared = Instagram().prepare_images(post)
    assert len(prepared) == 1
    item = prepared[0]
    assert item.mime == "image/jpeg"
    assert abs(item.width / item.height - 0.8) < 0.02
    assert max(item.width, item.height) <= 1440


# ---------------------------------------------------------------- send flow
def _run_send(monkeypatch, images, *, cfg_extra=None, options=None, token="tok"):
    calls = []
    host = _FakeHost()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(calls))
    monkeypatch.setattr(Instagram, "host", lambda self, cfg: host)
    cfg = {"username": "artist",
           "tokens": {"access_token": token, "user_id": "42", "expires_at": time.time() + 10 ** 7}}
    cfg.update(cfg_extra or {})
    post = Post(images=[], caption="hi", options=options or {})
    url, note = asyncio.run(Instagram().send(post, cfg, "hello", images))
    return url, note, calls, host


def test_test_connection_remembers_a_missing_username(monkeypatch):
    saved = {}
    monkeypatch.setattr("app.platforms.oauth.accounts.update",
                        lambda site, key, fields: saved.update(fields, site=site, key=key))
    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(calls))
    cfg = {"tokens": {"access_token": "tok", "user_id": "42",
                      "expires_at": time.time() + 10 ** 7},
           "card_id": "instagram:main"}
    detail = asyncio.run(Instagram().test(cfg))
    assert "@artist" in detail
    assert saved["username"] == "artist"
    assert saved["site"] == "instagram" and saved["key"] == "main"


def test_send_remembers_a_missing_username(monkeypatch):
    saved = {}
    monkeypatch.setattr("app.platforms.oauth.accounts.update",
                        lambda site, key, fields: saved.update(fields))
    calls = []
    host = _FakeHost()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(calls))
    monkeypatch.setattr(Instagram, "host", lambda self, cfg: host)
    cfg = {"tokens": {"access_token": "tok", "user_id": "42",
                      "expires_at": time.time() + 10 ** 7},
           "card_id": "instagram:main"}
    asyncio.run(Instagram().send(Post(images=[], caption="x"), cfg, "hi", [_prepared()]))
    assert saved.get("username") == "artist"


def test_send_single_image(monkeypatch):
    result, note, calls, host = _run_send(monkeypatch, [_prepared()])
    assert result == "https://www.instagram.com/p/abc/"
    assert note == ""
    assert host.uploaded == ["art.jpg"]
    assert host.deleted == ["https://i.ibb.co/art.jpg"]
    create = [call for call in calls if call[0] == "POST" and call[1].endswith("/media")][0]
    assert create[2]["image_url"] == "https://i.ibb.co/art.jpg"
    assert create[2]["caption"] == "hello"
    assert "is_carousel_item" not in create[2]
    publish = [call for call in calls if call[1].endswith("/media_publish")][0]
    assert publish[2]["creation_id"] == "container-1"


def test_send_carousel(monkeypatch):
    result, _, calls, host = _run_send(monkeypatch, [_prepared("a.jpg"), _prepared("b.jpg")])
    assert result == "https://www.instagram.com/p/abc/"
    assert host.uploaded == ["a.jpg", "b.jpg"]
    assert len(host.deleted) == 2
    creates = [call for call in calls if call[0] == "POST" and call[1].endswith("/media")]
    assert len(creates) == 3
    assert creates[0][2]["is_carousel_item"] == "true"
    assert creates[1][2]["is_carousel_item"] == "true"
    assert creates[2][2]["media_type"] == "CAROUSEL"
    assert creates[2][2]["children"] == "container-1,container-2"
    assert creates[2][2]["caption"] == "hello"
    publish = [call for call in calls if call[1].endswith("/media_publish")][0]
    assert publish[2]["creation_id"] == "container-3"


def test_send_uses_the_image_host_and_cleans_up(monkeypatch):
    _, _, _, host = _run_send(monkeypatch, [_prepared()])
    assert host.deleted, "the hosted image should be deleted afterwards"


# ------------------------------------------------------------ collaborators
def test_collaborators_go_on_the_single_container(monkeypatch):
    _, note, calls, _ = _run_send(monkeypatch, [_prepared()],
                                  cfg_extra={"can_invite": True,
                                             "collaborators": "artist.alt"})
    create = [call for call in calls if call[0] == "POST" and call[1].endswith("/media")][0]
    assert create[2]["collaborators"] == '["artist.alt"]'
    assert note == "collab invite sent to @artist.alt — accept it in the Instagram app"


def test_collaborators_go_on_the_carousel_parent_only(monkeypatch):
    _, note, calls, _ = _run_send(monkeypatch, [_prepared("a.jpg"), _prepared("b.jpg")],
                                  cfg_extra={"can_invite": True,
                                             "collaborators": "artist.alt"})
    creates = [call for call in calls if call[0] == "POST" and call[1].endswith("/media")]
    assert "collaborators" not in creates[0][2]
    assert "collaborators" not in creates[1][2]
    assert creates[2][2]["collaborators"] == '["artist.alt"]'
    assert "artist.alt" in note


def test_the_per_post_field_overrides_the_account_default(monkeypatch):
    _, _, calls, _ = _run_send(
        monkeypatch, [_prepared()],
        cfg_extra={"can_invite": True, "collaborators": "default_one",
                   "card_id": "instagram:main"},
        options={"instagram:main": {"collaborators": "@PerPost, second"}})
    create = [call for call in calls if call[0] == "POST" and call[1].endswith("/media")][0]
    assert create[2]["collaborators"] == '["PerPost", "second"]'


def test_a_solo_account_never_sends_collaborators(monkeypatch):
    _, note, calls, _ = _run_send(monkeypatch, [_prepared()],
                                  cfg_extra={"can_invite": False, "collaborators": "someone"})
    create = [call for call in calls if call[0] == "POST" and call[1].endswith("/media")][0]
    assert "collaborators" not in create[2]
    assert note == ""


def test_a_cleared_field_means_no_invite(monkeypatch):
    _, note, calls, _ = _run_send(
        monkeypatch, [_prepared()],
        cfg_extra={"can_invite": True, "collaborators": "artist.alt",
                   "card_id": "instagram:main"},
        options={"instagram:main": {"collaborators": ""}})
    create = [call for call in calls if call[0] == "POST" and call[1].endswith("/media")][0]
    assert "collaborators" not in create[2]
    assert note == ""


def test_preview_extra_lists_who_will_be_invited():
    connector = Instagram()
    post = Post(images=[], caption="x", options={"instagram:main": {"style": "ai"}})
    cfg = {"can_invite": True, "collaborators": "artist.alt", "card_id": "instagram:main"}
    assert connector.preview_extra(post, cfg) == {"collaborators": ["artist.alt"]}

    cleared = Post(images=[], caption="x",
                   options={"instagram:main": {"collaborators": ""}})
    assert connector.preview_extra(cleared, cfg) == {"collaborators": []}

    assert connector.preview_extra(post, {"can_invite": False}) == {}


def test_parse_collaborators_validation():
    from app.platforms.instagram import parse_collaborators
    assert parse_collaborators("@a, @b c") == ["a", "b", "c"]
    assert parse_collaborators("@one @two @three @four") == ["one", "two", "three"]
    assert parse_collaborators("same, same, other") == ["same", "other"]
    assert parse_collaborators("") == []
    assert parse_collaborators(None) == []


# ------------------------------------------------------------------- oauth
def _run_complete(monkeypatch, short_body):
    """complete() with a fake client; returns (cfg, calls, saved)."""
    calls: list = []
    saved: dict = {}
    monkeypatch.setattr("app.platforms.instagram.secrets.save",
                        lambda platform_id, cfg: saved.update(cfg))

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None, **kwargs):
            calls.append(("POST", url, dict(data or {})))
            return _Response(200, short_body)

        async def get(self, url, params=None, **kwargs):
            calls.append(("GET", url, dict(params or {})))
            return _Response(200, {"access_token": "long", "expires_in": 5183944})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    cfg = {"client_id": "cid", "client_secret": "secret"}
    asyncio.run(Instagram().complete(cfg, "the-code"))
    return cfg, calls, saved


def test_complete_accepts_the_documented_data_shape(monkeypatch):
    cfg, calls, saved = _run_complete(
        monkeypatch, {"data": [{"access_token": "short", "user_id": "42"}]})
    assert cfg["tokens"]["access_token"] == "long"
    assert cfg["tokens"]["user_id"] == "42"
    assert cfg["tokens"]["expires_at"] > time.time() + 50 * 86400
    assert calls[0][2]["code"] == "the-code"
    assert calls[1][2]["grant_type"] == "ig_exchange_token"
    assert calls[1][2]["access_token"] == "short"
    assert saved["tokens"]["access_token"] == "long"


def test_complete_accepts_the_flat_shape_with_an_int_user_id(monkeypatch):
    cfg, _, _ = _run_complete(
        monkeypatch, {"access_token": "short", "user_id": 17841400000000000,
                      "permissions": ["instagram_business_basic"]})
    assert cfg["tokens"]["access_token"] == "long"
    assert cfg["tokens"]["user_id"] == "17841400000000000"


def test_access_token_refreshes_near_expiry(monkeypatch):
    calls = []
    saved = {}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(calls))
    monkeypatch.setattr("app.platforms.instagram.secrets.save",
                        lambda platform_id, cfg: saved.update(cfg))

    async def fake_get(url, params=None, **kwargs):
        calls.append(("GET", url, dict(params or {})))
        return _Response(200, {"access_token": "fresh", "expires_in": 5183944})

    client = _FakeClient(calls)
    client.get = fake_get
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    cfg = {"tokens": {"access_token": "old", "user_id": "42",
                      "expires_at": time.time() + 100}}  # inside the 7-day margin
    token = asyncio.run(Instagram().access_token(cfg))
    assert token == "fresh"
    assert calls[0][2]["grant_type"] == "ig_refresh_token"
    assert cfg["tokens"]["user_id"] == "42"


def test_access_token_is_reused_when_fresh(monkeypatch):
    cfg = {"tokens": {"access_token": "tok", "user_id": "42",
                      "expires_at": time.time() + 40 * 86400}}
    assert asyncio.run(Instagram().access_token(cfg)) == "tok"


def test_not_connected_raises(monkeypatch):
    from app.platforms.base import PublishError
    with pytest.raises(PublishError):
        asyncio.run(Instagram().access_token({}))


def test_error_shapes_become_readable_messages():
    from app.platforms.base import PublishError
    connector = Instagram()

    with pytest.raises(PublishError) as flat:
        connector._json(_Response(400, {"error_type": "OAuthException", "code": 400,
                                        "error_message": "Invalid authorization code"}),
                        "Instagram code exchange failed")
    assert "OAuthException" in str(flat.value)
    assert "Invalid authorization code" in str(flat.value)

    with pytest.raises(PublishError) as graph:
        connector._json(_Response(400, {"error": {"message": "Invalid OAuth access token"}}), "x")
    assert "Invalid OAuth access token" in str(graph.value)

    with pytest.raises(PublishError) as plain:
        connector._json(_Response(500, {}), "x")
    assert "HTTP 500" in str(plain.value)


def test_config_warning_about_the_trailing_slash():
    connector = Instagram()
    warning = connector.config_warning({"redirect_uri": "https://localhost/artdrop/"})
    assert "no trailing slash" in warning
    assert connector.config_warning({"redirect_uri": "https://localhost/artdrop"}) == ""
    assert connector.config_warning({}) == ""
    assert connector.config_warning({"redirect_uri": connector.redirect_default()}) == ""
