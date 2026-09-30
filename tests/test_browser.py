"""Pixiv's browser plumbing and its selector fallbacks (no browser needed)."""
import asyncio
import sys
import threading
import types
from pathlib import Path

import pytest

from app.core import browser, paths
from app.platforms import pixiv_browser


# ------------------------------------------------- one Playwright, one window
class FakePage:
    pass


class FakeContext:
    def __init__(self, log, key):
        self.log = log
        self.key = key
        self._pages: list = []
        self.closed = False

    @property
    def pages(self):
        if self.closed:
            raise RuntimeError("target closed")  # what a real closed window does
        return list(self._pages)

    def new_page(self):
        page = FakePage()
        self._pages.append(page)
        return page

    def close(self):
        self.closed = True
        self.log.append(("close", self.key))


class FakeManager:
    def __init__(self, log):
        self.log = log

    @property
    def chromium(self):
        return self

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.log.append(("launch", user_data_dir))
        self.log.append(("kwargs", kwargs))
        return FakeContext(self.log, user_data_dir)

    def stop(self):
        self.log.append(("stop", None))


class FakeSyncPlaywright:
    def __init__(self, log):
        self.log = log

    def start(self):
        self.log.append(("start", None))
        return FakeManager(self.log)


@pytest.fixture
def fake_playwright(monkeypatch):
    """A stand-in `playwright.sync_api` module plus a clean browser module."""
    log: list = []
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: FakeSyncPlaywright(log)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(browser, "_manager", None)
    monkeypatch.setattr(browser, "_contexts", {})
    return log


def _calls(log):
    return [entry[0] for entry in log]


def test_two_logins_on_one_profile_share_one_window(fake_playwright, tmp_path):
    profile = tmp_path / "profiles" / "art"
    profile.mkdir(parents=True)

    first = browser.open_context(profile)
    second = browser.open_context(profile)

    assert first is second
    assert _calls(fake_playwright).count("start") == 1  # never a second Playwright
    assert _calls(fake_playwright).count("launch") == 1  # never a second window


def test_closing_a_window_keeps_the_shared_playwright(fake_playwright, tmp_path):
    profile = tmp_path / "profiles" / "art"
    profile.mkdir(parents=True)

    browser.open_context(profile)
    browser.close_context(profile)
    browser.open_context(profile)

    assert _calls(fake_playwright).count("close") == 1
    assert _calls(fake_playwright).count("launch") == 2  # a new window...
    assert _calls(fake_playwright).count("start") == 1  # ...on the same Playwright
    assert "stop" not in _calls(fake_playwright)


def test_a_window_the_user_closed_by_hand_is_reopened(fake_playwright, tmp_path):
    profile = tmp_path / "profiles" / "art"
    profile.mkdir(parents=True)

    context = browser.open_context(profile)
    context.closed = True  # the user closed the Pixiv window

    assert browser.page_for(profile) is None
    assert str(profile) not in browser._contexts
    browser.open_context(profile)
    assert _calls(fake_playwright).count("launch") == 2


def test_shutdown_closes_every_window_and_stops_playwright(fake_playwright, tmp_path):
    for name in ("one", "two"):
        profile = tmp_path / "profiles" / name
        profile.mkdir(parents=True)
        browser.open_context(profile)

    browser.close_all()

    assert _calls(fake_playwright).count("close") == 2
    assert _calls(fake_playwright).count("stop") == 1
    assert browser._manager is None
    assert browser._contexts == {}


def test_starting_without_playwright_says_what_to_do(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "_manager", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(RuntimeError) as info:
        browser.open_context(tmp_path / "profiles" / "art")
    assert "restart ArtDrop" in str(info.value)


# --------------------------------------------- the user's own Chrome (login)
def test_find_chrome_looks_in_the_windows_install_folders(monkeypatch):
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    env = {"PROGRAMFILES": r"C:\Program Files",
           "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
           "LOCALAPPDATA": r"C:\Users\ann\AppData\Local"}
    chrome = str(Path(r"C:\Program Files") / "Google/Chrome/Application/chrome.exe")
    edge = str(Path(r"C:\Program Files (x86)") / "Microsoft/Edge/Application/msedge.exe")
    assert browser.find_chrome(env, exists=lambda path: path == chrome) == chrome
    assert browser.find_chrome(env, exists=lambda path: path == edge) == edge
    assert browser.find_chrome(env, exists=lambda path: False) is None


def test_open_plain_chrome_starts_the_users_own_browser(monkeypatch, tmp_path):
    profile = tmp_path / "profiles" / "art"
    profile.mkdir(parents=True)
    started: list = []
    monkeypatch.setattr(browser, "find_chrome", lambda *args, **kwargs: r"C:\chrome.exe")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kwargs: started.append(args))

    result = browser.open_plain_chrome(profile, "https://accounts.pixiv.net/login")

    assert result == "started"
    assert started[0][0] == r"C:\chrome.exe"
    assert f"--user-data-dir={profile}" in started[0]
    assert started[0][-1] == "https://accounts.pixiv.net/login"


def test_a_profile_that_is_already_open_is_never_started_twice(monkeypatch, tmp_path):
    profile = tmp_path / "profiles" / "art"
    profile.mkdir(parents=True)
    (profile / "lockfile").write_text("")  # Chrome still has this profile open
    monkeypatch.setattr(browser, "LOCK_SETTLE_SECONDS", 0.0)
    calls: list = []
    monkeypatch.setattr(browser, "find_chrome", lambda *args, **kwargs: r"C:\chrome.exe")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kwargs: calls.append(args))

    assert browser.open_plain_chrome(profile, "https://accounts.pixiv.net/login") == "open"
    assert calls == []


def test_a_locked_profile_stops_playwright(fake_playwright, tmp_path):
    profile = tmp_path / "profiles" / "art"
    profile.mkdir(parents=True)
    (profile / "lockfile").write_text("")

    with pytest.raises(RuntimeError) as info:
        browser.open_context(profile)

    assert "close it first" in str(info.value)
    assert _calls(fake_playwright) == []  # Playwright was not even started


def test_playwright_keeps_the_chromium_sandbox(fake_playwright, tmp_path):
    profile = tmp_path / "profiles" / "art"
    profile.mkdir(parents=True)

    browser.open_context(profile)

    kwargs = [entry[1] for entry in fake_playwright if entry[0] == "kwargs"][-1]
    assert kwargs["chromium_sandbox"] is True
    assert "--no-sandbox" not in kwargs["args"]
    assert kwargs["channel"] == "chrome"  # the user's own browser first


def test_profile_dirs_are_per_account(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    first = browser.profile_dir("pixiv", "one")
    second = browser.profile_dir("pixiv", "two")
    assert first != second
    assert first.exists() and second.exists()
    assert first.parent.name == "pixiv-profiles"


def test_browsers_are_preferred_over_the_bundled_chromium():
    assert browser.CHANNELS[0] == "chrome"
    assert browser.CHANNELS[-1] is None


def test_selectors_key_off_names_not_label_texts():
    # The account UI is English, so text fallbacks would break; every form
    # selector must pin a name/value/type attribute.
    for key in ("file_input", "title", "caption", "tags", "rating", "ai"):
        candidates = pixiv_browser.SELECTORS[key]
        assert candidates, key
        for candidate in candidates:
            assert "[" in candidate and "=" in candidate, (key, candidate)


def test_browser_calls_run_on_one_dedicated_thread():
    async def collect():
        first = await browser.run(threading.get_ident)
        second = await browser.run(threading.get_ident)
        return first, second

    first, second = asyncio.run(collect())
    assert first == second
    assert first != threading.get_ident()  # never the event-loop thread


def test_a_parked_window_is_closed_on_the_same_thread():
    class Driver:
        def __init__(self):
            self.threads = {}

        def open(self):
            self.threads["open"] = threading.get_ident()
            return self

        def close(self):
            self.threads["close"] = threading.get_ident()
            return self

    async def flow():
        driver = Driver()
        await browser.run(driver.open)
        await browser.run(driver.close)
        return driver.threads

    threads = asyncio.run(flow())
    assert threads["open"] == threads["close"]


def test_first_present_takes_the_first_working_candidate():
    assert pixiv_browser.first_present(("a", "b", "c"), lambda s: s == "b") == "b"
    assert pixiv_browser.first_present(("a", "b"), lambda s: False) is None


def test_first_present_skips_broken_candidates():
    def probe(candidate):
        if candidate == "boom":
            raise RuntimeError("bad selector")
        return candidate == "ok"

    assert pixiv_browser.first_present(("boom", "ok"), probe) == "ok"


def test_ratings_map_to_the_form_values():
    assert pixiv_browser.RATINGS == {"all": "general", "r18": "r18", "r18g": "r18g"}
    assert pixiv_browser.AI_YES == "aiGenerated"


def test_artwork_url():
    assert pixiv_browser.artwork_url("https://www.pixiv.net/artworks/12345") == \
        "https://www.pixiv.net/artworks/12345"
    assert pixiv_browser.artwork_url("https://www.pixiv.net/illustration/create") == ""
    assert pixiv_browser.artwork_url("") == ""
