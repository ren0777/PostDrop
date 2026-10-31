"""API behavior: guards, double-post guard, parallel publishes, dry runs."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core import oauth, paths, secrets
from app.main import app
from app.platforms import REGISTRY

HEADERS = {"X-ArtDrop": "1"}
PLATFORMS = ("bluesky", "telegram", "discord")


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 64), (200, 60, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def store():
    return {
        "bluesky": {"handle": "test.bsky.social", "app_password": "app-pass-1234"},
        "telegram": {"bot_token": "123456:test-token", "chat_id": "@testchan"},
        "discord": {"webhook_url": "https://discord.com/api/webhooks/1/abc"},
    }


@pytest.fixture()
def sent():
    return []


@pytest.fixture()
def client(tmp_path, monkeypatch, store, sent):
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    monkeypatch.setattr(paths, "UPLOADS", paths.DATA / "uploads")
    monkeypatch.setattr(paths, "DB_PATH", paths.DATA / "history.sqlite")

    monkeypatch.setattr(secrets, "load", lambda platform_id: dict(store.get(platform_id, {})))
    monkeypatch.setattr(secrets, "save",
                        lambda platform_id, cfg: store.__setitem__(platform_id, dict(cfg)))
    monkeypatch.setattr(secrets, "delete", lambda platform_id: store.pop(platform_id, None))

    async def fake_send(post, cfg, text, images):
        sent.append(text)
        return "https://example.com/post/1", ""

    for platform_id in PLATFORMS:
        monkeypatch.setattr(REGISTRY[platform_id], "send", fake_send)

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def _upload(client) -> str:
    response = client.post(
        "/api/upload",
        files=[("images", ("art.png", _png(), "image/png"))],
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["post_id"]


# ---------------------------------------------------------------- guards
def test_double_post_guard(client, sent):
    post_id = _upload(client)
    payload = {"post_id": post_id, "platform": "discord", "caption": "hello",
               "tags": "art ai", "dry_run": False}

    first = client.post("/api/publish", json=payload, headers=HEADERS).json()
    assert first["ok"] is True
    assert first["url"] == "https://example.com/post/1"

    second = client.post("/api/publish", json=payload, headers=HEADERS).json()
    assert second["already"] is True
    assert second["url"] == "https://example.com/post/1"
    assert len(sent) == 1


def test_parallel_publishes_keep_every_status_entry(client, sent, monkeypatch):
    """All sites publish at once; every result must survive in status.json."""
    calls = {}
    barrier = threading.Barrier(len(PLATFORMS), timeout=10)

    for platform_id in PLATFORMS:
        def make_slow_send(pid):
            async def slow_send(post, cfg, text, images):
                # Finish only when all three publishes are inside send() at once,
                # so this really is concurrent, not just fast.
                await asyncio.to_thread(barrier.wait)
                calls[pid] = calls.get(pid, 0) + 1
                return f"https://example.com/{pid}/1", ""
            return slow_send
        monkeypatch.setattr(REGISTRY[platform_id], "send", make_slow_send(platform_id))

    post_id = _upload(client)
    payload = {"post_id": post_id, "caption": "hi", "dry_run": False}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PLATFORMS)) as pool:
        futures = [
            pool.submit(client.post, "/api/publish",
                        json={**payload, "platform": platform_id}, headers=HEADERS)
            for platform_id in PLATFORMS
        ]
        results = [future.result().json() for future in futures]

    assert all(result["ok"] for result in results)

    status = json.loads((paths.upload_dir(post_id) / "status.json").read_text("utf-8"))
    assert set(status) == set(PLATFORMS)
    assert all(status[platform_id]["ok"] for platform_id in PLATFORMS)
    assert all(calls[platform_id] == 1 for platform_id in PLATFORMS)

    # And the guard still refuses a repeat, because the entry survived.
    again = client.post("/api/publish", json={**payload, "platform": "bluesky"},
                        headers=HEADERS).json()
    assert again["already"] is True
    assert calls["bluesky"] == 1


def test_dry_run_posts_nothing_and_writes_no_status(client, sent):
    post_id = _upload(client)
    payload = {"post_id": post_id, "platform": "discord", "caption": "hello", "dry_run": True}
    result = client.post("/api/publish", json=payload, headers=HEADERS).json()
    assert result["ok"] is True
    assert result["preview"]["ready"] is True
    assert sent == []
    assert not (paths.upload_dir(post_id) / "status.json").exists()


def test_write_requests_need_the_custom_header(client):
    assert client.post("/api/accounts/discord", json={}).status_code == 403


def test_foreign_host_is_rejected(client):
    response = client.get("/api/platforms", headers={"Host": "evil.example"})
    assert response.status_code == 403


# ---------------------------------------------------------------- uploads
def test_upload_limit_is_shared_with_image_prep():
    from app.core.images import MAX_INPUT_BYTES as prep_limit
    from app.main import MAX_INPUT_BYTES as upload_limit
    assert upload_limit == prep_limit


def test_oversized_upload_is_rejected(client, monkeypatch):
    monkeypatch.setattr("app.main.MAX_INPUT_BYTES", 100)
    response = client.post(
        "/api/upload",
        files=[("images", ("art.png", _png(), "image/png"))],
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "larger than" in response.json()["detail"]


# ------------------------------------------------------------- caption AI
def test_caption_settings_roundtrip_without_echoing_the_key(client):
    saved = client.post("/api/caption/settings", headers=HEADERS,
                        json={"api_key": "secret-key-123", "vision_model": "vm",
                              "backup_model": "bm"}).json()
    assert saved["ok"] is True
    assert saved["configured"] is True

    got = client.get("/api/caption/settings", headers=HEADERS).json()
    assert got["api_key_saved"] is True
    assert got["vision_model"] == "vm"
    assert got["backup_model"] == "bm"
    assert "secret-key-123" not in json.dumps(got)


def test_caption_provider_switch_keeps_each_key(client):
    client.post("/api/caption/settings", headers=HEADERS, json={"api_key": "oc-key"})
    client.post("/api/caption/settings", headers=HEADERS, json={"provider": "gemini"})
    got = client.get("/api/caption/settings", headers=HEADERS).json()
    assert got["provider"] == "gemini"
    assert got["api_key_saved"] is False
    assert got["vision_model"] == got["providers"]["gemini"]["vision_model"]

    client.post("/api/caption/settings", headers=HEADERS, json={"provider": "opencode"})
    assert client.get("/api/caption/settings", headers=HEADERS).json()["api_key_saved"] is True


def test_caption_needs_a_key(client):
    response = client.post("/api/caption", headers=HEADERS,
                           json={"character": "Ch", "platforms": ["bluesky"]}).json()
    assert response["ok"] is False
    assert "not set up" in response["error"]


def test_caption_needs_a_character(client, store):
    store["caption"] = {"api_key": "k"}
    response = client.post("/api/caption", headers=HEADERS,
                           json={"character": "", "platforms": ["bluesky"]}).json()
    assert response["ok"] is False
    assert "character name" in response["error"]


def test_caption_works_for_an_instagram_card(client, store, monkeypatch):
    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Solo"})
    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Main"})
    store["caption"] = {"api_key": "k"}
    seen = {}

    async def fake_generate(**kwargs):
        seen.update(kwargs)
        return {"series": "Honkai: Star Rail", "title": "Festival", "alt_text": "a",
                "platforms": {"instagram": {"caption": "cap", "tags": ["a", "b"]}},
                "used_model": "vm", "fallback": False}

    monkeypatch.setattr("app.main.captioner.generate", fake_generate)

    single = client.post("/api/caption", headers=HEADERS,
                         json={"character": "Stelle", "platforms": ["instagram:main"]}).json()
    assert single["ok"] is True
    assert seen["platforms"] == ["instagram"]  # the prompt asks once per site
    assert single["platforms"]["instagram:main"]["caption"] == "cap"

    # two accounts of one site get the same AI answer, keyed by their cards
    both = client.post("/api/caption", headers=HEADERS,
                       json={"character": "Stelle",
                             "platforms": ["instagram:solo", "instagram:main"]}).json()
    assert seen["platforms"] == ["instagram"]
    assert set(both["platforms"]) == {"instagram:solo", "instagram:main"}
    assert both["platforms"]["instagram:solo"]["caption"] == "cap"


def test_caption_endpoint_filters_platforms_and_returns_the_result(client, store, monkeypatch):
    store["caption"] = {"api_key": "k"}
    seen = {}

    async def fake_generate(**kwargs):
        seen.update(kwargs)
        return {"alt_text": "alt",
                "platforms": {"bluesky": {"caption": "cap", "tags": ["t"]}},
                "used_model": "vm", "fallback": False}

    monkeypatch.setattr("app.main.captioner.generate", fake_generate)
    response = client.post("/api/caption", headers=HEADERS,
                           json={"character": "Ch", "platforms": ["bluesky", "unknown"]}).json()
    assert response["ok"] is True
    assert response["platforms"]["bluesky"]["caption"] == "cap"
    assert seen["platforms"] == ["bluesky"]


# ---------------------------------------------------------------- history
def test_history_records_the_post(client):
    post_id = _upload(client)
    client.post("/api/publish", json={"post_id": post_id, "platform": "discord",
                                      "caption": "hello", "dry_run": False}, headers=HEADERS)
    items = client.get("/api/history", headers=HEADERS).json()["items"]
    assert items and items[0]["platform"] == "discord" and items[0]["ok"] == 1


# ------------------------------------------------------------------ styles
def test_settings_round_trip_and_validation(client):
    default = client.get("/api/settings", headers=HEADERS).json()["caption_styles"]
    assert default["telegram"] == "name_series"
    assert default["bluesky"] == "ai"

    saved = client.post("/api/settings", headers=HEADERS, json={
        "caption_styles": {"telegram": "ai", "nope": "none", "bluesky": "bogus"},
    }).json()
    assert saved["caption_styles"]["telegram"] == "ai"
    assert saved["caption_styles"]["bluesky"] == "ai"  # an invalid value is ignored
    assert "nope" not in saved["caption_styles"]

    again = client.get("/api/settings", headers=HEADERS).json()["caption_styles"]
    assert again["telegram"] == "ai"


def test_publish_uses_the_style_from_the_body(client, sent):
    post_id = _upload(client)
    payload = {"post_id": post_id, "platform": "telegram", "caption": "ignored",
               "character": "Stelle", "series": "Honkai: Star Rail",
               "options": {"telegram": {"style": "name_series"}}, "dry_run": False}
    result = client.post("/api/publish", json=payload, headers=HEADERS).json()
    assert result["ok"] is True
    assert sent[-1] == "Stelle — Honkai: Star Rail"


def test_publish_defaults_telegram_to_name_series(client, sent):
    post_id = _upload(client)
    payload = {"post_id": post_id, "platform": "telegram", "caption": "ignored",
               "character": "Stelle", "dry_run": False}
    result = client.post("/api/publish", json=payload, headers=HEADERS).json()
    assert result["ok"] is True
    assert sent[-1] == "Stelle"


# ------------------------------------------------------------------- OAuth
def test_platforms_payload_includes_the_new_sites(client):
    items = {p["id"]: p for p in client.get("/api/platforms", headers=HEADERS).json()["platforms"]}
    assert {"bluesky", "telegram", "discord", "tumblr", "deviantart"} <= set(items)
    assert items["tumblr"]["is_oauth"] is True
    assert items["tumblr"]["connected"] is False
    assert items["tumblr"]["tags_in_caption"] is False
    assert items["bluesky"]["is_oauth"] is False
    da_kinds = [field["kind"] for field in items["deviantart"]["option_fields"]]
    assert da_kinds.count("checkbox") == 2
    assert "select" in da_kinds


def test_oauth_start_needs_saved_settings(client):
    response = client.post("/api/oauth/tumblr/start", headers=HEADERS).json()
    assert response["ok"] is False
    assert "Fill in and save" in response["error"]


def test_oauth_connect_and_disconnect(client, store, monkeypatch):
    store["tumblr"] = {"blog": "myname.tumblr.com", "client_id": "cid",
                       "client_secret": "secret"}

    async def fake_request(provider, data):
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "the-code"
        return {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}

    monkeypatch.setattr("app.main.oauth._token_request", fake_request)

    started = client.post("/api/oauth/tumblr/start", headers=HEADERS).json()
    assert started["ok"] is True
    assert started["url"].startswith("https://www.tumblr.com/oauth2/authorize?")
    assert "state=" in started["url"]

    finished = client.post("/api/oauth/tumblr/code", headers=HEADERS,
                           json={"code": "the-code"}).json()
    assert finished["ok"] is True

    items = {p["id"]: p for p in client.get("/api/platforms", headers=HEADERS).json()["platforms"]}
    assert items["tumblr"]["connected"] is True

    assert client.post("/api/oauth/tumblr/disconnect", headers=HEADERS).json()["ok"] is True
    items = {p["id"]: p for p in client.get("/api/platforms", headers=HEADERS).json()["platforms"]}
    assert items["tumblr"]["connected"] is False


def test_oauth_code_accepts_a_whole_url(client, store, monkeypatch):
    store["tumblr"] = {"blog": "myname.tumblr.com", "client_id": "cid",
                       "client_secret": "secret"}
    seen = {}

    async def fake_request(provider, data):
        seen.update(data)
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr("app.main.oauth._token_request", fake_request)
    url = "https://myname.tumblr.com/?code=abc123&state=stale-state"
    response = client.post("/api/oauth/tumblr/code", headers=HEADERS, json={"code": url}).json()
    assert response["ok"] is True
    assert seen["code"] == "abc123"


def test_oauth_callback_route(client, store, monkeypatch):
    store["tumblr"] = {"blog": "myname.tumblr.com", "client_id": "cid",
                       "client_secret": "secret"}

    async def fake_request(provider, data):
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr("app.main.oauth._token_request", fake_request)
    started = client.post("/api/oauth/tumblr/start", headers=HEADERS).json()
    state = parse_qs(urlparse(started["url"]).query)["state"][0]

    page = client.get(f"/oauth/tumblr?code=abc&state={state}")
    assert page.status_code == 200
    assert "connected" in page.text

    again = client.get(f"/oauth/tumblr?code=abc&state={state}")
    assert "did not match" in again.text


# --------------------------------------------------------------------- PKCE
def test_tumblr_flow_has_no_pkce(client, store, monkeypatch):
    store["tumblr"] = {"blog": "myname.tumblr.com", "client_id": "cid",
                       "client_secret": "secret"}
    seen = {}

    async def fake_request(provider, data):
        seen.update(data)
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr("app.main.oauth._token_request", fake_request)
    started = client.post("/api/oauth/tumblr/start", headers=HEADERS).json()
    query = parse_qs(urlparse(started["url"]).query)
    assert "code_challenge" not in query

    page = client.get(f"/oauth/tumblr?code=c&state={query['state'][0]}")
    assert "connected" in page.text
    assert "code_verifier" not in seen


def test_oauth_pkce_paste_with_state_uses_its_verifier(client, store, monkeypatch):
    store["deviantart"] = {"client_id": "cid", "client_secret": "secret"}
    seen = {}

    async def fake_request(provider, data):
        seen.update(data)
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr("app.main.oauth._token_request", fake_request)
    started = client.post("/api/oauth/deviantart/start", headers=HEADERS).json()
    query = parse_qs(urlparse(started["url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    state = query["state"][0]

    pasted = f"https://127.0.0.1/artdrop?code=xyz&state={state}"
    response = client.post("/api/oauth/deviantart/code", headers=HEADERS,
                           json={"code": pasted}).json()
    assert response["ok"] is True
    assert seen["code_verifier"]
    assert oauth.code_challenge(seen["code_verifier"]) == query["code_challenge"][0]


def test_oauth_pkce_bare_code_uses_the_latest_verifier(client, store, monkeypatch):
    store["deviantart"] = {"client_id": "cid", "client_secret": "secret"}
    seen = {}

    async def fake_request(provider, data):
        seen.update(data)
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr("app.main.oauth._token_request", fake_request)
    started = client.post("/api/oauth/deviantart/start", headers=HEADERS).json()
    challenge = parse_qs(urlparse(started["url"]).query)["code_challenge"][0]

    response = client.post("/api/oauth/deviantart/code", headers=HEADERS,
                           json={"code": "bare-code"}).json()
    assert response["ok"] is True
    assert seen["code_verifier"]
    assert oauth.code_challenge(seen["code_verifier"]) == challenge


def test_oauth_pkce_without_a_verifier_asks_to_reconnect(client, store, monkeypatch):
    oauth._states.clear()
    store["deviantart"] = {"client_id": "cid", "client_secret": "secret"}

    async def should_not_run(provider, data):
        raise AssertionError("the token endpoint must not be called without a verifier")

    monkeypatch.setattr("app.main.oauth._token_request", should_not_run)
    response = client.post("/api/oauth/deviantart/code", headers=HEADERS,
                           json={"code": "bare-code"}).json()
    assert response["ok"] is False
    assert "Press Connect again" in response["error"]


# ------------------------------------------------------- M2 follow-up + M3
def test_platforms_payload_includes_instagram_settings(client):
    data = client.get("/api/platforms", headers=HEADERS).json()
    section = data["account_sections"]["instagram"]
    assert {field["key"] for field in section["fields"]} == {
        "client_id", "client_secret", "redirect_uri", "imgbb_api_key"}
    assert section["accounts"] == []
    assert "instagram" not in {platform["id"] for platform in data["platforms"]}


def test_deviantart_option_fields_flags(client):
    items = {p["id"]: p for p in client.get("/api/platforms", headers=HEADERS).json()["platforms"]}
    deviantart = {field["key"]: field for field in items["deviantart"]["option_fields"]}
    assert deviantart["gallery_folder"]["sticky"] is True
    assert deviantart["title"]["sticky"] is False


def test_instagram_accounts_are_created_and_listed(client):
    created = client.post("/api/instagram/accounts", headers=HEADERS,
                          json={"name": "Solo"}).json()
    assert created["ok"] is True
    assert created["id"] == "instagram:solo"

    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Main"})
    items = {p["id"]: p for p in client.get("/api/platforms", headers=HEADERS).json()["platforms"]}
    assert {"instagram:solo", "instagram:main"} <= set(items)
    assert items["instagram:solo"]["account_of"] == "instagram"
    assert items["instagram:solo"]["label"] == "Instagram — Solo"
    assert items["instagram:solo"]["fields"] == []
    assert items["instagram:solo"]["option_fields"] == []

    section = client.get("/api/platforms", headers=HEADERS).json()["account_sections"]["instagram"]
    assert {account["key"] for account in section["accounts"]} == {"solo", "main"}


def test_instagram_collab_settings_show_up_on_the_card(client):
    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Main"})
    client.post("/api/instagram/accounts/main", headers=HEADERS,
                json={"can_invite": True, "collaborators": "@artist.alt, extra"})
    items = {p["id"]: p for p in client.get("/api/platforms", headers=HEADERS).json()["platforms"]}
    fields = items["instagram:main"]["option_fields"]
    assert fields and fields[0]["key"] == "collaborators"
    assert fields[0]["value"] == "artist.alt,extra"

    section = client.get("/api/platforms", headers=HEADERS).json()["account_sections"]["instagram"]
    account = next(item for item in section["accounts"] if item["key"] == "main")
    assert account["can_invite"] is True
    assert account["collaborators"] == "artist.alt,extra"


def test_collaborators_prefill_and_dry_run_preview(client):
    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Main"})
    client.post("/api/instagram/accounts/main", headers=HEADERS,
                json={"can_invite": True, "collaborators": "@artist.alt"})
    client.post("/api/accounts/instagram", headers=HEADERS, json={
        "client_id": "cid", "client_secret": "secret", "imgbb_api_key": "key"})

    items = {p["id"]: p for p in client.get("/api/platforms", headers=HEADERS).json()["platforms"]}
    field = items["instagram:main"]["option_fields"][0]
    assert field["value"] == "artist.alt"  # the card prefills from this
    assert field["sticky"] is True           # and keeps it across a new post
    assert "Clear the box" in field["help"]

    post_id = _upload(client)
    preview = client.post("/api/publish", headers=HEADERS, json={
        "post_id": post_id, "platform": "instagram:main", "caption": "x",
        "dry_run": True}).json()["preview"]
    assert preview["collaborators"] == ["artist.alt"]

    cleared = client.post("/api/publish", headers=HEADERS, json={
        "post_id": post_id, "platform": "instagram:main", "caption": "x",
        "options": {"instagram:main": {"collaborators": ""}},
        "dry_run": True}).json()["preview"]
    assert cleared["collaborators"] == []


def test_two_instagram_accounts_publish_independently(client, store, monkeypatch):
    calls = []

    async def fake_send(self, post, cfg, text, images):
        calls.append(cfg.get("card_id"))
        return f"https://example.com/{len(calls)}", ""

    monkeypatch.setattr("app.platforms.instagram.Instagram.send", fake_send)
    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Solo"})
    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Main"})
    client.post("/api/accounts/instagram", headers=HEADERS, json={
        "client_id": "cid", "client_secret": "secret", "imgbb_api_key": "key"})

    post_id = _upload(client)
    payload = {"post_id": post_id, "caption": "hello", "dry_run": False}
    solo = client.post("/api/publish", headers=HEADERS,
                       json={**payload, "platform": "instagram:solo"}).json()
    collab = client.post("/api/publish", headers=HEADERS,
                         json={**payload, "platform": "instagram:main"}).json()
    assert solo["ok"] is True and collab["ok"] is True
    assert calls == ["instagram:solo", "instagram:main"]

    # the double-post guard and History are per account
    again = client.post("/api/publish", headers=HEADERS,
                        json={**payload, "platform": "instagram:solo"}).json()
    assert again["already"] is True
    assert len(calls) == 2
    items = client.get("/api/history", headers=HEADERS).json()["items"]
    assert {item["platform"] for item in items} == {"instagram:solo", "instagram:main"}


def test_caption_styles_are_per_instagram_card(client):
    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Solo"})
    client.post("/api/instagram/accounts", headers=HEADERS, json={"name": "Main"})
    styles = client.get("/api/settings", headers=HEADERS).json()["caption_styles"]
    assert "instagram:solo" in styles and "instagram:main" in styles

    saved = client.post("/api/settings", headers=HEADERS,
                        json={"caption_styles": {"instagram:solo": "none", "nope": "ai"}}).json()
    assert saved["caption_styles"]["instagram:solo"] == "none"
    assert "nope" not in saved["caption_styles"]


# ------------------------------------------------------------------- Pixiv
def test_pixiv_accounts_and_card_payload(client):
    created = client.post("/api/pixiv/accounts", headers=HEADERS, json={"name": "Art"}).json()
    assert created["ok"] is True
    assert created["id"] == "pixiv:art"

    data = client.get("/api/platforms", headers=HEADERS).json()
    items = {p["id"]: p for p in data["platforms"]}
    assert items["pixiv:art"]["label"] == "Pixiv — Art"
    kinds = [field["kind"] for field in items["pixiv:art"]["option_fields"]]
    assert kinds == ["text", "rating"]
    rating = items["pixiv:art"]["option_fields"][1]
    assert rating["choices"][0] == ["all", "All ages (全年齢)"]

    section = data["account_sections"]["pixiv"]
    assert section["flow"] == "browser"
    assert section["fields"] == []  # nothing shared: the profile is the login
    assert section["accounts"][0]["settings"][0]["key"] == "full_auto"


def test_full_auto_saves_for_a_pixiv_account(client):
    client.post("/api/pixiv/accounts", headers=HEADERS, json={"name": "Art"})
    saved = client.post("/api/pixiv/accounts/art", headers=HEADERS,
                        json={"name": "Art", "full_auto": True}).json()
    assert saved["ok"] is True
    accounts = client.get("/api/platforms", headers=HEADERS).json()[
        "account_sections"]["pixiv"]["accounts"]
    assert accounts[0]["name"] == "Art"
    assert accounts[0]["settings"][0]["value"] is True


def test_pixiv_debug_logging_is_on_by_default_and_can_be_switched_off(client):
    client.post("/api/pixiv/accounts", headers=HEADERS, json={"name": "Art"})
    fresh = client.get("/api/platforms", headers=HEADERS).json()[
        "account_sections"]["pixiv"]["accounts"][0]
    logging_field = next(item for item in fresh["settings"] if item["key"] == "debug_log")
    assert logging_field["value"] is True  # on until the capture is confirmed
    assert "artdrop.log" in logging_field["help"]

    client.post("/api/pixiv/accounts/art", headers=HEADERS,
                json={"name": "Art", "debug_log": False})
    stored = client.get("/api/platforms", headers=HEADERS).json()[
        "account_sections"]["pixiv"]["accounts"][0]
    assert next(item for item in stored["settings"]
                if item["key"] == "debug_log")["value"] is False


def test_browser_login_endpoint_calls_the_connector(client, monkeypatch):
    client.post("/api/pixiv/accounts", headers=HEADERS, json={"name": "Art"})

    async def fake_login(self, cfg):
        return "Logged in as ren"

    monkeypatch.setattr("app.platforms.pixiv.Pixiv.login", fake_login)
    response = client.post("/api/browser-login/pixiv:art", headers=HEADERS).json()
    assert response["ok"] is True
    assert response["detail"] == "Logged in as ren"


def test_pixiv_blocks_publishing_without_a_rating(client):
    client.post("/api/pixiv/accounts", headers=HEADERS, json={"name": "Art"})
    post_id = _upload(client)
    result = client.post("/api/publish", headers=HEADERS, json={
        "post_id": post_id, "platform": "pixiv:art", "caption": "x", "dry_run": True}).json()
    assert result["ok"] is False
    assert "Choose the Pixiv rating" in result["error"]


def test_publish_notes_trimmed_images_and_history_keeps_it(client):
    files = [("images", (f"art{index}.png", _png(), "image/png")) for index in range(5)]
    post_id = client.post("/api/upload", files=files, headers=HEADERS).json()["post_id"]
    payload = {"post_id": post_id, "platform": "bluesky", "caption": "hello", "dry_run": False}
    result = client.post("/api/publish", json=payload, headers=HEADERS).json()
    assert result["ok"] is True
    assert result["note"] == "the first 4 of 5 images (Bluesky's limit)"

    items = client.get("/api/history", headers=HEADERS).json()["items"]
    assert items[0]["note"] == "the first 4 of 5 images (Bluesky's limit)"


def test_dry_run_shows_the_trim_note_too(client):
    files = [("images", (f"art{index}.png", _png(), "image/png")) for index in range(6)]
    post_id = client.post("/api/upload", files=files, headers=HEADERS).json()["post_id"]
    payload = {"post_id": post_id, "platform": "deviantart", "caption": "hello", "dry_run": True}
    result = client.post("/api/publish", json=payload, headers=HEADERS).json()
    assert result["ok"] is True
    assert len(result["preview"]["images"]) == 1
    assert result["preview"]["note"] == "the first 1 of 6 images (DeviantArt's limit)"


def test_saving_a_trailing_slash_redirect_warns(client):
    saved = client.post("/api/accounts/instagram", headers=HEADERS, json={
        "client_id": "cid", "client_secret": "secret",
        "redirect_uri": "https://localhost/artdrop/"}).json()
    assert saved["ok"] is True
    assert "no trailing slash" in saved["warning"]

    fine = client.post("/api/accounts/instagram", headers=HEADERS, json={
        "redirect_uri": "https://localhost/artdrop"}).json()
    assert fine["warning"] == ""


# ---------------------------------------------------------- lazy mode (M6)
def test_lazy_settings_round_trip(client):
    fresh = client.get("/api/lazy", headers=HEADERS).json()
    assert fresh["settings"]["enabled"] is False
    assert {folder["key"] for folder in fresh["settings"]["folders"]} == {
        "outbox", "outbox-instagram"}
    assert [card["id"] for card in fresh["cards"]][:1] == ["bluesky"]

    saved = client.post("/api/lazy", headers=HEADERS, json={
        "enabled": True,
        "folders": [{"key": "outbox", "path": "outbox", "mature": True, "cards": ["bluesky"]}],
        "sites": {"bluesky": {"per_post": 4, "spacing_minutes": 5}},
        "quiet": {"enabled": True, "start": 23, "end": 7},
    }).json()

    assert saved["ok"] is True
    settings = saved["settings"]
    assert settings["enabled"] is True
    assert settings["quiet"] == {"enabled": True, "start": 23, "end": 7}
    assert settings["sites"]["bluesky"]["spacing_minutes"] == 30  # the floor holds
    assert saved["settings"]["folders"][0]["exists"] is True  # created on save


def test_lazy_refuses_an_unknown_card(client):
    response = client.post("/api/lazy", headers=HEADERS, json={
        "folders": [{"key": "outbox", "path": "outbox", "mature": True,
                     "cards": ["myspace"]}]}).json()
    assert response["ok"] is False and "unknown card" in response["error"]


def test_lazy_pause_and_scan(client):
    client.post("/api/lazy", headers=HEADERS,
                json={"enabled": True, "folders": [
                    {"key": "outbox", "path": "outbox", "mature": True, "cards": ["bluesky"]}]})
    assert client.post("/api/lazy/pause", headers=HEADERS,
                       json={"paused": True}).json()["settings"]["paused"] is True
    scan = client.post("/api/lazy/scan", headers=HEADERS).json()
    assert scan["ok"] is True
    assert scan["summary"]["reason"] == "paused"
    client.post("/api/lazy/pause", headers=HEADERS, json={"paused": False})


def test_queue_actions_over_http(client):
    from app.core import queue

    item_id = queue.add_items([{"card_id": "bluesky", "caption": "hello",
                                "images": [], "cover": ""}])[0]
    assert item_id

    listed = client.get("/api/queue", headers=HEADERS).json()
    assert listed["queue"]["counts"]["pending"] == 1
    assert listed["queue"]["cards"][0]["items"][0]["caption"] == "hello"

    edited = client.post(f"/api/queue/{item_id}/caption", headers=HEADERS,
                         json={"caption": "better"}).json()
    assert edited["queue"]["cards"][0]["items"][0]["caption"] == "better"

    assert client.post(f"/api/queue/{item_id}/skip", headers=HEADERS).json()["ok"] is True
    assert client.get("/api/queue", headers=HEADERS).json()["queue"]["counts"]["pending"] == 0
    assert client.post(f"/api/queue/{item_id}/remove", headers=HEADERS).json()["ok"] is True
    assert client.post(f"/api/queue/99999/remove", headers=HEADERS).json()["ok"] is False


def test_a_needs_you_batch_opens_as_a_post(client):
    from app.core import queue

    source = paths.DATA / "outbox.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_png())
    entry_id = queue.add_needs_you(
        batch_key="b1", source="outbox", folder_key="outbox", kind="pixiv",
        images=[str(source)], cover=str(source), caption="二乃", character="Nino",
        series="The Quintessential Quintuplets",
        platforms={"pixiv": {"caption": "二乃", "tags": ["Nino"]}},
    )

    used = client.post(f"/api/needs-you/{entry_id}/use", headers=HEADERS).json()

    assert used["ok"] is True and used["kind"] == "pixiv"
    assert used["character"] == "Nino" and used["caption"] == "二乃"
    assert used["images"] and used["images"][0]["name"].endswith(".png")
    staged = client.get(f"/api/posts/{used['post_id']}/image/0", headers=HEADERS)
    assert staged.status_code == 200 and staged.content
    assert client.get("/api/queue", headers=HEADERS).json()["needs_you"] == []


def test_a_needs_you_draft_carries_the_folder_defaults(client):
    from app.core import queue

    client.post("/api/lazy", headers=HEADERS, json={
        "enabled": True,
        "folders": [{"key": "outbox", "path": "outbox", "mature": True,
                     "cards": ["bluesky", "telegram"]}]})
    source = paths.DATA / "outbox-piece.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_png())
    entry_id = queue.add_needs_you(
        batch_key="b2", source="outbox", folder_key="outbox", kind="unknown",
        images=[str(source)], cover=str(source), caption="", mature=True)

    used = client.post(f"/api/needs-you/{entry_id}/use", headers=HEADERS).json()

    assert used["ok"] is True
    assert used["mature"] is True  # the mature folder's default travels to the Post tab
    assert used["cards"] == ["bluesky", "telegram"]  # the folder's routed cards, not the AI's


def test_the_caption_endpoint_hands_the_draft_to_the_ai(client, monkeypatch):
    from app.core import captioner

    seen = {}

    async def fake_generate(*, cfg, **kwargs):
        seen.update(kwargs)
        return {"series": "", "title": "", "alt_text": "",
                "platforms": {site: {"caption": "polished", "tags": []}
                              for site in kwargs["platforms"]}}

    monkeypatch.setattr(captioner, "generate", fake_generate)
    client.post("/api/caption/settings", headers=HEADERS, json={"api_key": "k"})
    post_id = client.post("/api/upload", files=[("images", ("a.png", _png(), "image/png"))],
                          headers=HEADERS).json()["post_id"]

    client.post("/api/caption", headers=HEADERS, json={
        "post_id": post_id, "character": "Stelle", "platforms": ["bluesky"],
        "caption": "my draft ☀️", "caption_exact": True})

    assert seen["caption_draft"] == "my draft ☀️"
    assert seen["caption_exact"] is True
    assert seen["character"] == "Stelle"


def test_retry_ai_puts_a_failed_entry_back_to_work(client):
    from app.core import queue

    source = paths.DATA / "retry-piece.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_png())
    queue.remember_files([source], status="needs_you")
    entry_id = queue.add_needs_you(
        batch_key="b3", source="outbox", folder_key="outbox", kind="unknown",
        images=[str(source)], cover=str(source), note="AI unavailable")

    retried = client.post(f"/api/needs-you/{entry_id}/retry-ai", headers=HEADERS).json()

    assert retried["ok"] is True
    assert queue.known_files() == {}       # the file counts as new work again
    assert queue.needs_you() == []         # and the entry is out of the way


def test_retry_ai_is_refused_for_an_entry_that_was_not_the_ais_fault(client):
    from app.core import queue

    entry_id = queue.add_needs_you(batch_key="b4", source="outbox", folder_key="outbox",
                                   kind="unknown", images=[], note="")

    response = client.post(f"/api/needs-you/{entry_id}/retry-ai", headers=HEADERS).json()

    assert response["ok"] is False and "not the AI" in response["error"]


def test_the_needs_you_payload_carries_the_note(client):
    from app.core import queue

    queue.add_needs_you(batch_key="b5", source="outbox", folder_key="outbox",
                        kind="unknown", images=[], note="AI unavailable")

    entries = client.get("/api/queue", headers=HEADERS).json()["needs_you"]

    assert entries[0]["note"] == "AI unavailable"


def test_a_pixiv_entry_that_is_the_last_blocker_says_so(client):
    """The Queue tab marks a set whose only remaining blocker is its Pixiv entry."""
    from app.core import queue

    source = paths.DATA / "waiting.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_png())
    queue.add_needs_you(
        batch_key="b-wait", source="outbox", folder_key="outbox", kind="pixiv",
        images=[str(source)], cover=str(source), caption="", character="Changli")

    listed = client.get("/api/queue", headers=HEADERS).json()["needs_you"][0]
    assert listed["waiting_pixiv"] is True  # nothing else is pending for this set

    queue.add_items([{"card_id": "bluesky", "caption": "hi", "images": [str(source)],
                      "batch_key": "b-wait", "character": "Changli"}])
    listed = client.get("/api/queue", headers=HEADERS).json()["needs_you"][0]
    assert listed["waiting_pixiv"] is False  # the set still has a post to go


def test_the_pixiv_folder_switch_round_trips(client):
    client.post("/api/lazy", headers=HEADERS, json={
        "enabled": True,
        "folders": [{"key": "outbox", "path": "outbox", "mature": True, "pixiv": False,
                     "cards": ["bluesky"]}]})
    folder = client.get("/api/lazy", headers=HEADERS).json()["settings"]["folders"][0]
    assert folder["pixiv"] is False

    # a folder saved without the switch keeps the default (Pixiv on)
    client.post("/api/lazy", headers=HEADERS, json={
        "enabled": True,
        "folders": [{"key": "outbox", "path": "outbox", "mature": True,
                     "cards": ["bluesky"]}]})
    folder = client.get("/api/lazy", headers=HEADERS).json()["settings"]["folders"][0]
    assert folder["pixiv"] is True


def test_a_mixed_entry_opens_without_its_label_as_a_character(client):
    """Live bug: "Mixed: A, B…" became the Pixiv tag and the AI captioned one image."""
    from app.core import queue

    source = paths.DATA / "mixed.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_png())
    entry_id = queue.add_needs_you(
        batch_key="outbox/1.png+mixed+x:abc", source="outbox", folder_key="outbox",
        kind="pixiv", images=[str(source)], cover=str(source), caption="",
        character="Mixed: Jane Doe, Kafka", series="Zenless Zone Zero, Honkai: Star Rail",
        platforms={"pixiv": {"caption": "推しはどの子？", "tags": ["JaneDoe", "Kafka"],
                             "title": "推しはどの子？"}},
    )

    used = client.post(f"/api/needs-you/{entry_id}/use", headers=HEADERS).json()

    assert used["character"] == "" and used["series"] == ""
    assert used["mixed"] == {"characters": ["Jane Doe", "Kafka"],
                             "series": ["Zenless Zone Zero", "Honkai: Star Rail"]}


def test_the_caption_endpoint_writes_a_mixed_caption_for_a_mix(client, monkeypatch):
    from app.core import captioner

    seen = {}

    async def fake_mixed(**kwargs):
        seen.update(kwargs)
        return {"alt_text": "", "platforms": {"bluesky": {"caption": "Who's your fav?",
                                                          "tags": ["Kafka"]}}}

    async def never(**kwargs):
        raise AssertionError("the single-character writer must not run for a mix")

    monkeypatch.setattr(captioner, "generate_mixed", fake_mixed)
    monkeypatch.setattr(captioner, "generate", never)
    response = client.post("/api/caption", headers=HEADERS, json={
        "platforms": ["bluesky"], "character": "",
        "mixed": {"characters": ["Jane Doe", "Kafka"], "series": ["Zenless Zone Zero"]}})

    assert seen["characters"] == ["Jane Doe", "Kafka"]
    body = response.json()
    assert body["ok"] is True
