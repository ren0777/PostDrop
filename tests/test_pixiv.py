"""Pixiv: the rating gate, the rate limit, and the browser-flow boundary."""
import asyncio
import time
from pathlib import Path

import pytest
from PIL import Image

from app.core import secrets
from app.core.images import Prepared
from app.core.post import Post
from app.platforms import pixiv
from app.platforms.base import PublishError
from app.platforms.pixiv import Pixiv


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(secrets, "load", lambda key: dict(store.get(key, {})))
    monkeypatch.setattr(secrets, "save", lambda key, cfg: store.__setitem__(key, dict(cfg)))
    monkeypatch.setattr(secrets, "delete", lambda key: store.pop(key, None))
    return store


class FakeBrowser:
    """Stands in for the real driver: own Chrome to sign in, Playwright after."""

    def __init__(self, url="https://www.pixiv.net/artworks/123", login=None,
                 window="started", note=""):
        self.url = url
        self.login_account = {"user_id": "42", "name": "ren"} if login is None else login
        self.window = window  # "started", or "open" when the profile is in use
        self.note = note  # the driver's note when nothing was published
        self.logins: list = []
        self.verifies: list = []
        self.closes = 0
        self.specs: list = []

    def open_login_window(self, profile):
        self.logins.append(str(profile))
        return self.window

    def open_verify_window(self, profile):
        self.verifies.append(str(profile))

    def read_login(self, profile):
        return dict(self.login_account)

    def close_window(self, profile):
        self.closes += 1

    def fill(self, profile, spec):
        self.specs.append(spec)
        return (self.url, "") if self.url else (None, self.note)


def _post(tmp_path, **kwargs):
    path = tmp_path / "art.png"
    Image.new("RGB", (64, 64)).save(path, "PNG")
    return Post(images=[path], **kwargs)


def _prepared(name="art.jpg"):
    return Prepared(data=b"jpeg", mime="image/jpeg", width=8, height=8, filename=name)


def _rating_options(rating="r18"):
    return {"pixiv:one": {"style": "ai", "rating": rating}}


# ------------------------------------------------------------------ the gate
def test_the_rating_is_required(tmp_path):
    post = _post(tmp_path, caption="x", options={"pixiv:one": {"style": "ai"}})
    result = asyncio.run(Pixiv().publish(post, {"card_id": "pixiv:one"}, dry_run=True))
    assert result.ok is False
    assert "Choose the Pixiv rating" in result.error


def test_the_mature_tick_warns_about_all_ages(tmp_path):
    post = _post(tmp_path, caption="x", options={**_rating_options("all"), "mature": True})
    result = asyncio.run(Pixiv().publish(post, {"card_id": "pixiv:one"}, dry_run=True))
    assert result.ok is True
    assert result.preview["rating"] == "all"
    assert "R-18" in result.preview["warning"]


# ----------------------------------------------------------------- the flow
def test_the_flow_fills_the_form_without_submitting(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="星の夜のキャプション", character="星",
                 series="崩壊：スターレイル",
                 options={"pixiv:one": {"style": "ai", "rating": "r18", "title": "星の夜"}})
    url, note = asyncio.run(Pixiv().send(post, {"card_id": "pixiv:one"},
                                          "星の夜のキャプション", [_prepared()]))
    assert url == "https://www.pixiv.net/artworks/123"
    spec = fake.specs[0]
    assert spec.submit is False  # semi-auto stops before the Post button
    assert spec.ai_flag is True  # always on: Pixiv labels AI works itself
    assert spec.rating == "r18"
    assert spec.title == "星の夜"
    assert spec.caption == "星の夜のキャプション"
    assert spec.images and not spec.images[0].exists()  # staging was cleaned up
    assert "投稿する" not in note and "Post" in note


def test_full_auto_submits(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", options=_rating_options("all"))
    url, note = asyncio.run(Pixiv().send(post, {"card_id": "pixiv:one", "full_auto": True},
                                          "x", [_prepared()]))
    assert fake.specs[0].submit is True
    assert url.endswith("/artworks/123")
    assert note == ""


def test_a_closed_window_is_reported(monkeypatch, tmp_path):
    fake = FakeBrowser(url=None, note="window closed")
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", options=_rating_options())
    result = asyncio.run(Pixiv().publish(post, {"card_id": "pixiv:one"}, dry_run=False))
    assert result.ok is False
    assert "not submitted" in result.error
    assert "window closed" in result.error  # the driver's note reaches the card


def test_a_watch_that_ends_without_a_post_says_so(monkeypatch, tmp_path):
    fake = FakeBrowser(url=None)  # no note: the window stayed open, Post was never pressed
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", options=_rating_options())
    result = asyncio.run(Pixiv().publish(post, {"card_id": "pixiv:one"}, dry_run=False))
    assert result.ok is False
    assert "not submitted" in result.error and "window closed" not in result.error


def test_a_posted_but_linkless_result_warns_and_keeps_the_rate_limit(
        monkeypatch, tmp_path, fake_keyring):
    # Pixiv accepted the work but no id could be read: never call it "not submitted"
    fake = FakeBrowser(url=None, note="posted")
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", options=_rating_options())
    result = asyncio.run(Pixiv().publish(post, {"card_id": "pixiv:one"}, dry_run=False))
    assert result.ok is False
    assert "could not be read" in result.error and "not submitted" not in result.error
    # the three-minute guard still applies, so this cannot be double-posted
    assert fake_keyring["pixiv:one"]["last_post_at"] > time.time() - 5


def test_the_debug_log_setting_is_on_unless_switched_off(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", options=_rating_options("all"))

    asyncio.run(Pixiv().send(post, {"card_id": "pixiv:one"}, "x", [_prepared()]))
    assert fake.specs[0].debug is True  # default: logging until it is confirmed

    asyncio.run(Pixiv().send(post, {"card_id": "pixiv:one", "debug_log": False}, "x",
                             [_prepared()]))
    assert fake.specs[1].debug is False


def test_at_most_ten_tags(monkeypatch, tmp_path):
    post = _post(tmp_path, caption="x", tags=[f"tag{index}" for index in range(15)],
                 options=_rating_options())
    connector = Pixiv()
    assert len(connector.tags_for(post, {"card_id": "pixiv:one"})) == 10


# ------------------------------------------------------------------ the limit
def test_pixiv_waits_three_minutes_between_posts(monkeypatch, tmp_path, fake_keyring):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", options=_rating_options())
    cfg = {"card_id": "pixiv:one", "last_post_at": time.time()}
    result = asyncio.run(Pixiv().publish(post, cfg, dry_run=False))
    assert result.ok is False
    assert "wait about" in result.error
    assert fake.specs == []  # never reached the browser

    cfg["last_post_at"] = time.time() - 300
    result = asyncio.run(Pixiv().publish(post, cfg, dry_run=False))
    assert result.ok is True
    assert fake_keyring["pixiv:one"]["last_post_at"] > time.time() - 5


# -------------------------------------------------------------------- login
def test_login_opens_the_users_own_chrome(monkeypatch, fake_keyring):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)

    detail = asyncio.run(Pixiv().login({"card_id": "pixiv:one", "name": "Account 1"}))

    assert "close that window" in detail and "Check login" in detail
    assert Path(fake.logins[0]).parts[-2:] == ("pixiv-profiles", "one")
    # only Check login marks the account connected
    assert fake_keyring.get("pixiv:one", {}).get("connected") is not True


def test_login_says_when_the_chrome_window_is_already_open(monkeypatch, fake_keyring):
    fake = FakeBrowser(window="open")
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    detail = asyncio.run(Pixiv().login({"card_id": "pixiv:one"}))
    assert "already open" in detail


def test_check_login_remembers_the_pixiv_account(monkeypatch, fake_keyring):
    fake = FakeBrowser(login={"user_id": "99", "name": "artist"})
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)

    detail = asyncio.run(Pixiv().test({"card_id": "pixiv:one"}))

    assert "artist" in detail and "99" in detail
    assert fake.verifies and not fake.logins  # no Chrome: this one is Playwright
    stored = fake_keyring["pixiv:one"]
    assert stored["username"] == "artist"
    assert stored["pixiv_user_id"] == "99"
    assert stored["connected"] is True


def test_check_login_without_a_user_id_is_still_connected(monkeypatch, fake_keyring):
    fake = FakeBrowser(login={"user_id": "", "name": ""})
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    detail = asyncio.run(Pixiv().test({"card_id": "pixiv:one"}))
    assert detail == pixiv.LOGGED_IN_HINT
    assert fake_keyring["pixiv:one"]["connected"] is True


def test_check_login_keeps_the_name_it_already_knew(monkeypatch, fake_keyring):
    fake_keyring["pixiv:one"] = {"username": "artist", "pixiv_user_id": "99"}
    fake = FakeBrowser(login={"user_id": "", "name": ""})
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    asyncio.run(Pixiv().test({"card_id": "pixiv:one"}))
    stored = fake_keyring["pixiv:one"]
    assert stored["username"] == "artist" and stored["pixiv_user_id"] == "99"


def test_check_login_reports_a_missing_login(monkeypatch, fake_keyring):
    class NotLoggedIn(FakeBrowser):
        def read_login(self, profile):
            return {}

    monkeypatch.setattr(Pixiv, "browser", lambda self: NotLoggedIn())
    with pytest.raises(PublishError):
        asyncio.run(Pixiv().test({"card_id": "pixiv:one"}, timeout=0))


# ---------------------------------------------------------------------- tags
def test_at_least_one_tag_is_always_sent(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)

    named = _post(tmp_path, caption="x", character="星", series="崩壊：スターレイル",
                  options=_rating_options("all"))
    asyncio.run(Pixiv().send(named, {"card_id": "pixiv:one"}, "x", [_prepared()]))
    assert fake.specs[0].tags == ["星"]

    plain = _post(tmp_path, caption="x", options=_rating_options("all"))
    asyncio.run(Pixiv().send(plain, {"card_id": "pixiv:one"}, "x", [_prepared()]))
    assert fake.specs[1].tags == [pixiv.FALLBACK_TAG]


def test_the_dry_run_says_when_a_tag_is_added(tmp_path):
    post = _post(tmp_path, caption="x", options=_rating_options("all"))
    extra = Pixiv().preview_extra(post, {"card_id": "pixiv:one"})
    assert pixiv.FALLBACK_TAG in extra["note"]


def test_pixiv_takes_only_png_jpeg_and_gif():
    # its file input accepts those three, so WEBP must be converted
    assert Pixiv.original_formats == ("JPEG", "PNG", "GIF")
    assert Pixiv.png_formats == ("WEBP",)


def test_the_character_is_always_one_of_the_sent_tags(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", character="Stelle", tags=["fanart"],
                 options=_rating_options("all"))
    asyncio.run(Pixiv().send(post, {"card_id": "pixiv:one"}, "x", [_prepared()]))
    assert fake.specs[0].tags == ["Stelle", "fanart"]


def test_the_character_tag_is_not_duplicated(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", character="Stelle", tags=["stelle", "fanart"],
                 options=_rating_options("all"))
    asyncio.run(Pixiv().send(post, {"card_id": "pixiv:one"}, "x", [_prepared()]))
    assert fake.specs[0].tags == ["stelle", "fanart"]


def test_a_spaced_character_name_is_sent_as_one_tag(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", character="quintessential quintuplets",
                 tags=["fanart"], options=_rating_options("all"))
    asyncio.run(Pixiv().send(post, {"card_id": "pixiv:one"}, "x", [_prepared()]))
    assert fake.specs[0].tags == ["quintessentialquintuplets", "fanart"]


def test_the_sent_tags_never_exceed_ten(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(Pixiv, "browser", lambda self: fake)
    post = _post(tmp_path, caption="x", character="Stelle",
                 tags=[f"tag{index}" for index in range(10)], options=_rating_options("all"))
    asyncio.run(Pixiv().send(post, {"card_id": "pixiv:one"}, "x", [_prepared()]))
    tags = fake.specs[0].tags
    assert len(tags) == 10 and tags[0] == "Stelle"


def test_a_post_over_pixivs_total_limit_shrinks_only_the_big_images(tmp_path):
    """Live: a 19-image mix was 333 MB; Pixiv takes 200 MB per post."""
    import os as _os

    from app.core.post import Post
    from app.platforms.pixiv import Pixiv

    paths_list = []
    for index, side in enumerate((700, 700, 700, 120)):
        path = tmp_path / f"{index:02d}_art.png"
        Image.frombytes("RGB", (side, side), _os.urandom(side * side * 3)).save(path, "PNG")
        paths_list.append(path)
    small = paths_list[-1].read_bytes()
    connector = Pixiv()
    connector.max_total_bytes = 3_000_000  # the three noisy 700px PNGs are ~1.5 MB each

    prepared = connector.prepare_images(Post(images=paths_list))

    assert sum(len(item.data) for item in prepared) <= connector.max_total_bytes
    assert [item.mime for item in prepared[:3]] == ["image/jpeg"] * 3  # re-encoded
    assert prepared[3].data == small and prepared[3].mime == "image/png"  # untouched
    assert Pixiv.max_total_bytes == 200_000_000
