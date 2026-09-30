"""Browser plumbing for the browser-driven connectors (Pixiv).

Two kinds of window, on purpose:

* **signing in** uses the user's own Chrome (`open_plain_chrome()`), started
  with `subprocess.Popen` — Cloudflare refuses an automated browser, and the
  login has nothing to do with ArtDrop anyway;
* **everything else** (checking the login, filling the upload form) uses
  Playwright, which is imported inside `_playwright()` so the app and the tests
  run without it installed.

Playwright rules, so nothing is ever launched twice or left locked:

* every call goes through `run()`, which uses **one dedicated thread**;
  Playwright's sync objects are bound to the thread that made them;
* there is **one Playwright per process** and **one open context per profile**,
  so a parked window is reused and closing a context never stops the manager;
* a profile that another browser already has open is never launched.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import paths

# Prefer a real browser the user already has; Chromium is the fallback.
CHANNELS = ("chrome", "msedge", None)

# Windows keeps these out of PATH, so the usual install folders are checked.
CHROME_INSTALLS = (
    "Google/Chrome/Application/chrome.exe",
    "Microsoft/Edge/Application/msedge.exe",
)
CHROME_COMMANDS = ("chrome", "google-chrome", "google-chrome-stable", "msedge", "chromium")
LOCK_NAMES = ("SingletonLock", "lockfile")
LOCK_SETTLE_SECONDS = 3.0

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artdrop-browser")
_manager = None  # the one Playwright, started lazily on the browser thread
_contexts: dict[str, object] = {}  # profile path -> its open browser context


async def run(func, /, *args, **kwargs):
    """Run one browser call on the single browser thread."""
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(_executor, lambda: func(*args, **kwargs))
    return await loop.run_in_executor(_executor, func, *args)


def profile_dir(site: str, key: str) -> Path:
    """The persistent profile for one account (its login lives in there)."""
    folder = paths.DATA / f"{site}-profiles" / key
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------- normal Chrome
def browser_candidates(env: dict | None = None) -> list[str]:
    """Every place a normal Chrome or Edge could be, best first."""
    env = os.environ if env is None else env
    roots = (env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA"))
    found = [str(Path(root) / install) for root in roots if root for install in CHROME_INSTALLS]
    found += [shutil.which(name) or "" for name in CHROME_COMMANDS]
    return [path for path in found if path]


def find_chrome(env: dict | None = None, exists=os.path.exists) -> str | None:
    """The user's own Chrome (Edge as a fallback), or None when neither is there."""
    for path in browser_candidates(env):
        if exists(path):
            return path
    return None


def profile_locked(profile: Path) -> bool:
    """True when a browser still has this profile open."""
    return any((Path(profile) / name).exists() for name in LOCK_NAMES)


def open_plain_chrome(profile: Path, url: str) -> str:
    """Open the sign-in page in the user's own Chrome, with no automation.

    Returns "open" when that profile already has a window — the login is then
    never started twice.
    """
    profile = Path(profile)
    deadline = time.time() + LOCK_SETTLE_SECONDS
    while profile_locked(profile) and time.time() < deadline:
        time.sleep(0.25)  # a window that just closed leaves its lock for a moment
    if profile_locked(profile):
        return "open"

    exe = find_chrome()
    if exe is None:
        raise RuntimeError(
            "could not find Google Chrome or Microsoft Edge — install Chrome, then press "
            "Log in again."
        )
    subprocess.Popen([exe, f"--user-data-dir={profile}", "--no-first-run", url])
    return "started"


# ------------------------------------------------------------------- Playwright
def _playwright():
    """The shared Playwright; started once, on the browser thread only."""
    global _manager
    if _manager is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 — not installed, or broken install
            raise RuntimeError(
                "Pixiv needs a component that is not installed yet — restart ArtDrop with "
                "Start ArtDrop.bat so it can install it."
            ) from exc
        _manager = sync_playwright().start()
    return _manager


def open_context(profile: Path, headless: bool = False):
    """The browser context for a profile: reuse an open one, else launch it."""
    key = str(profile)
    context = _contexts.get(key)
    if context is not None:
        if page_for(profile) is None:
            _contexts.pop(key, None)
        else:
            return context

    _refuse_locked(profile)
    manager = _playwright()
    last_error: Exception | None = None
    for channel in CHANNELS:
        try:
            context = manager.chromium.launch_persistent_context(
                user_data_dir=key,
                channel=channel,
                headless=headless,
                args=["--start-maximized"],
                no_viewport=True,
                # Playwright turns the Chromium sandbox off by default; keep it on
                # (Chrome otherwise shows the "unsupported flag --no-sandbox" bar).
                chromium_sandbox=True,
            )
            _contexts[key] = context
            return context
        except Exception as exc:  # noqa: BLE001 — try the next browser
            last_error = exc
    message = str(last_error or "")
    if any(marker in message for marker in ("ProcessSingleton", "SingletonLock", "in use")):
        raise RuntimeError(locked_message(profile))
    raise RuntimeError(f"could not start a browser: {last_error}")


def locked_message(profile: Path) -> str:
    """What to say when another browser still holds this profile."""
    return (f"a Chrome window still has this Pixiv account open — close it first, then try "
            f"again (if no window is open, delete {Path(profile) / 'lockfile'})")


def _refuse_locked(profile: Path) -> None:
    if profile_locked(profile):
        raise RuntimeError(locked_message(profile))


def page_for(profile: Path):
    """The profile's page, or None when its window is closed."""
    context = _contexts.get(str(profile))
    if context is None:
        return None
    try:
        pages = context.pages
        return pages[0] if pages else context.new_page()
    except Exception:  # noqa: BLE001 — the user closed the window
        _contexts.pop(str(profile), None)
        return None


def close_context(profile: Path) -> bool:
    """Close one window (the shared manager keeps running). True when there was one."""
    context = _contexts.pop(str(profile), None)
    if context is None:
        return False
    try:
        context.close()
    except Exception:  # noqa: BLE001 — already gone
        pass
    return True


def close_all() -> None:
    """Close every window and stop the shared Playwright (app shutdown)."""
    global _manager
    for key in list(_contexts):
        close_context(Path(key))
    if _manager is not None:
        try:
            _manager.stop()
        except Exception:  # noqa: BLE001
            pass
        _manager = None
