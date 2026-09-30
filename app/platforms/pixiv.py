"""Pixiv connector — a real browser window, never Pixiv's private APIs.

Signing in opens Pixiv's login page in the user's **own Chrome** (Cloudflare
refuses an automated browser); the session lives in a private browser profile.
Posting uses Playwright to fill the upload form; in the default semi-auto mode
the user presses Post themselves, and ArtDrop waits for the artwork URL.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from ..core import browser, captioner, paths
from ..core.post import clip_title
from . import pixiv_browser
from .base import Connector, Field, PublishError

TITLE_LIMIT = 32
MIN_GAP_SECONDS = 180.0  # at most one Pixiv post every three minutes
VERIFY_TIMEOUT = 60.0
POLL_SECONDS = 2.0
LOGGED_IN_HINT = "Logged in to Pixiv."
FALLBACK_TAG = "イラスト"


class Pixiv(Connector):
    id = "pixiv"
    label = "Pixiv"
    caption_limit = 4096
    max_images = 30
    max_tags = 10
    max_bytes = 32_000_000
    max_total_bytes = 200_000_000  # Pixiv: 200 MB for the whole post
    max_side = None
    # Pixiv's file input accepts image/gif, image/jpeg, image/png only.
    original_formats = ("JPEG", "PNG", "GIF")
    png_formats = ("WEBP",)
    tags_in_caption = False
    fields = []  # nothing shared: the browser profile is the login
    option_fields = [
        Field("title", "Title (Japanese, optional)", required=False,
              placeholder="✨ Generate fills this, or the character name is used"),
        Field("rating", "Rating (choose one every post)", kind="rating",
              choices=(("all", "All ages (全年齢)"), ("r18", "R-18"), ("r18g", "R-18G")),
              help="ArtDrop blocks posting until you pick one. Semi-auto: the form opens and "
                   "you press Post yourself."),
    ]
    setup_help = (
        "Pixiv is driven through a real browser window, and only for the upload form. Press "
        "Log in and ArtDrop opens Pixiv's sign-in page in your own Chrome — Cloudflare refuses "
        "automated browsers, so the sign-in is never automated. Sign in normally (CAPTCHA and "
        "2FA included), close that Chrome window, then press Check login. The session stays in "
        "a private profile under data\\pixiv-profiles, and ArtDrop never sees your password. "
        "Posts stop before the Post button unless Full auto is ticked. ArtDrop always sets the "
        "AI-generated switch, sends at least one tag (Pixiv requires one), and converts WEBP "
        "files to PNG because Pixiv accepts only JPEG, PNG and GIF. If you normally log in to "
        "Pixiv with Google, set a Pixiv password first (pixiv.net → Settings → Account → "
        "Password), then sign in with email + password."
    )

    # ------------------------------------------------------------------ helpers
    def browser(self) -> pixiv_browser.PixivBrowser:
        """The DOM driver; tests replace this whole method."""
        return pixiv_browser.PixivBrowser()

    def account_key(self, cfg: dict) -> str:
        return self.card_id(cfg).partition(":")[2] or "default"

    def profile(self, cfg: dict) -> Path:
        return browser.profile_dir(self.id, self.account_key(cfg))

    def default_title(self, post, text: str) -> str:
        character = str(post.character or "").strip()
        series = str(post.series or "").strip()
        if character:
            value = f"{character} — {series}" if series else character
            return clip_title(value, TITLE_LIMIT) or "Pixiv post"
        line = next((part.strip() for part in (text or "").splitlines() if part.strip()), "")
        return clip_title(line.lstrip("#"), TITLE_LIMIT) or "Pixiv post"

    def fallback_tag(self, post) -> str:
        """Pixiv needs at least one tag; use the character or series name."""
        name = str(post.character or "").strip() or str(post.series or "").strip()
        return captioner.as_tag(clip_title(name, 30)) or FALLBACK_TAG

    def post_tags(self, post, cfg: dict) -> list[str]:
        """The tags to send: never empty (the form requires one), and always with
        the artist's own spelling of the character as one tag."""
        tags = captioner.with_character_tag(self.tags_for(post, cfg), post.character,
                                           max_tags=self.max_tags)
        return tags or [self.fallback_tag(post)]

    def card_problem(self, post, cfg: dict) -> str | None:
        rating = str(self.options_for(post, cfg).get("rating") or "").strip()
        if rating not in pixiv_browser.RATINGS:
            return "Choose the Pixiv rating (All ages / R-18 / R-18G) before posting."
        return None

    def debug_logging(self, cfg: dict) -> bool:
        """Write each Pixiv page call to data/artdrop.log (on unless switched off)."""
        value = cfg.get("debug_log")
        return True if value is None else bool(value)

    def preview_extra(self, post, cfg: dict) -> dict:
        options = self.options_for(post, cfg)
        rating = str(options.get("rating") or "").strip()
        extra: dict = {"rating": rating}
        if rating == "all" and bool(post.options.get("mature")):
            extra["warning"] = ("All ages is chosen while Mature is ticked — Pixiv requires "
                                "R-18 for sexual content.")
        if not self.tags_for(post, cfg):
            extra["note"] = (f"Pixiv needs at least one tag, so “{self.fallback_tag(post)}” "
                             "will be added.")
        return extra

    # ------------------------------------------------------------ browser login
    async def _wait_for_login(self, driver, profile: Path, timeout: float) -> dict:
        """Poll the open create page in short steps until it shows a login."""
        deadline = time.time() + timeout
        while True:
            account = await browser.run(driver.read_login, profile)
            if account or time.time() >= deadline:
                return account
            await asyncio.sleep(POLL_SECONDS)

    async def login(self, cfg: dict) -> str:
        """Open Pixiv's sign-in page in the user's own Chrome (no automation)."""
        result = await browser.run(self.browser().open_login_window, self.profile(cfg))
        if result == "open":
            return ("The Chrome login window is already open — finish signing in there, close "
                    "it, then press Check login.")
        return ("Log in in the Chrome window, then close that window and press Check login.")

    async def logout(self, cfg: dict) -> str:
        profile = self.profile(cfg)
        if await browser.run(browser.profile_locked, profile):
            raise PublishError("close the Pixiv Chrome window first — it still has this "
                               "profile open.")
        await browser.run(browser.close_context, profile)
        shutil.rmtree(profile, ignore_errors=True)
        self.remember(cfg, {"connected": False, "username": "", "pixiv_user_id": ""})
        return "Logged out — the browser profile was deleted."

    # ---------------------------------------------------------------- interface
    async def test(self, cfg: dict, timeout: float = VERIFY_TIMEOUT) -> str:
        """Check login: open the create page with Playwright and see it load."""
        profile = self.profile(cfg)
        driver = self.browser()
        await browser.run(driver.open_verify_window, profile)
        account = await self._wait_for_login(driver, profile, timeout)
        await browser.run(driver.close_window, profile)
        if not account:
            raise PublishError("Not logged in — press Log in, sign in in the Chrome window, "
                               "close it, then press Check login.")
        name = str(account.get("name") or "").strip()
        user_id = str(account.get("user_id") or "").strip()
        # Keep whatever was known before: the create page often exposes neither.
        known = {"connected": True}
        if name:
            known["username"] = name
        if user_id:
            known["pixiv_user_id"] = user_id
        self.remember(cfg, known)
        return f"Logged in as {name} (user id {user_id})" if name and user_id else LOGGED_IN_HINT

    async def send(self, post, cfg: dict, text: str, images: list) -> tuple[str, str]:
        options = self.options_for(post, cfg)
        rating = str(options.get("rating") or "").strip()
        if rating not in pixiv_browser.RATINGS:
            raise PublishError("Choose the Pixiv rating (All ages / R-18 / R-18G) before posting.")

        last = float(cfg.get("last_post_at") or 0)
        if last:
            wait = MIN_GAP_SECONDS - (time.time() - last)
            if wait > 0:
                raise PublishError(f"Pixiv: wait about {int(wait) + 1} seconds before the "
                                   "next post (at most one every three minutes).")

        title = str(options.get("title") or "").strip() or self.default_title(post, text)
        full_auto = bool(cfg.get("full_auto"))
        paths.ensure_dirs()
        staging = Path(tempfile.mkdtemp(prefix="pixiv-", dir=str(paths.DATA)))
        files: list[Path] = []
        try:
            for index, image in enumerate(images, start=1):
                path = staging / f"{index:02d}_{image.filename}"
                path.write_bytes(image.data)
                files.append(path)
            spec = pixiv_browser.FormSpec(
                images=files,
                title=clip_title(title, TITLE_LIMIT),
                caption=text,
                tags=self.post_tags(post, cfg),
                rating=rating,
                ai_flag=True,
                submit=full_auto,
                debug=self.debug_logging(cfg),
            )
            url, watch_note = await browser.run(self.browser().fill, self.profile(cfg), spec)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if not url:
            if watch_note == "posted":
                # Pixiv took the work: keep the three-minute guard even without a link.
                self.remember(cfg, {"last_post_at": time.time()})
                raise PublishError("posted — the artwork is on Pixiv, but its link could not "
                                   "be read; check your profile before posting again.")
            detail = f" ({watch_note})" if watch_note else ""
            raise PublishError(f"not submitted{detail} — Post was not pressed before the watch "
                               "window ended.")
        self.remember(cfg, {"last_post_at": time.time()})
        note = "" if full_auto else "you pressed Post in the Pixiv window"
        return url, note
