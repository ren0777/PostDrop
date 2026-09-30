"""The Pixiv upload form, driven by Playwright.

Signing in is **not** automated: `open_login_window()` starts the user's own
Chrome (see `app.core.browser`) because Cloudflare refuses an automated browser.
Playwright only runs when that profile is free — to check the login and to fill
the upload form.

Every selector below was read off the real create page (2026-09-30). The account
UI is in English, so they key off `name`/`value` attributes rather than Japanese
label text. Two rules keep it honest:

* the upload fields are not inside any `<form>` (the only form on the page is the
  header search), so lookups are page-wide and every hit is checked against the
  page header and the search box before it is used;
* a value that cannot be verified (the age rating, the AI switch, the tag
  counter) stops the flow, keeps the window open and says which field to fill by
  hand.

It never guesses.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from ..core import browser

log = logging.getLogger("artdrop.pixiv")

CREATE_URL = "https://www.pixiv.net/illustration/create"
LOGIN_URL = "https://accounts.pixiv.net/login"
DASHBOARD_URL = "https://www.pixiv.net/dashboard/works"
ACCOUNTS_HOST = "accounts.pixiv.net"

MAX_TAGS = 10
WATCH_SECONDS = 600.0
FORM_WAIT_SECONDS = 30.0

# Pixiv does not navigate to the new artwork; it reloads the create form. So the
# work id comes from Pixiv's own page calls, and the works dashboard is the
# fallback.
API_HINTS = ("/ajax/", "/rpc/")  # every Pixiv page call we read (GET and POST)
# Only these paths may turn an id into the artwork link (word boundary, so
# "/ajax/search/artworks" can never match "/work").
TRUSTED_PATH_RE = re.compile(r"/(?:create|work|works|upload|uploads)(?:[^a-z]|$)")
RPC_WRITE_HINT = "/rpc/"
CREATE_PATH = "/work/create"
CREATE_PAGE_PATH = "/illustration/create"
PROGRESS_HINT = "progress"
PROFILE_ALL_HINT = "/profile/all"
PROFILE_TOP_HINT = "/profile/top"
USER_PATH_RE = re.compile(r"/ajax/user/(\d+)(?:/|$)")
ARTWORK_PATH_RE = re.compile(r"/artworks/(\d+)")
USER_ID_KEYS = ("userId", "user_id", "pixivId", "id")
STRONG_ID_KEYS = ("illustId", "illust_id")  # an id under one of these is enough
WEAK_ID_KEYS = ("id",)  # trusted only when the same object also reports completion
STATUS_KEYS = ("status", "state", "result", "progress", "phase", "step")
SUCCESS_WORDS = ("complete", "completed", "success", "succeeded", "done", "finished")
DASHBOARD_LINKS = ('a[href*="/artworks/"]',)
CARD_TIME_XPATH = "xpath=ancestor::*[self::li or self::div][1]//time[@datetime]"
# The uploader itself is the only place a preview may be looked for.
UPLOAD_ANCHORS = ('input[name="files[]"]', 'text=Add images', 'text=画像を追加')
UPLOAD_AREA_XPATHS = ("xpath=ancestor::div[1]", "xpath=..")
PREVIEW_SELECTORS = ('img[src^="blob:"]', "img")
CLOSED_ERROR_HINTS = ("target closed", "has been closed", "page closed", "browser closed")

# The card's rating ids → the form's radio values (x_restrict).
RATINGS = {
    "all": "general",
    "r18": "r18",
    "r18g": "r18g",
}
# ai_type: "no" is "notAiGenerated".
AI_YES = "aiGenerated"

TAG_COUNTER_RE = re.compile(rf"\d+\s*/\s*{MAX_TAGS}")

# The upload fields are **not inside any `<form>`** (the only form on the page is
# the header search), so the lookups are page-wide; the header/search guard in
# `_verify_field` is what keeps them off the site search.
FORM_SCOPE = ("body",)

# Where a form field must never come from.
HEADER_AREAS = ("header", '[role="banner"]')
SEARCH_MARKS = ("search", "検索")

# Required controls; choosing R-18 can reveal more of them.
REQUIRED_SELECTORS = (
    'input[type="radio"][required]',
    'input[type="checkbox"][required]',
    'select[required]',
    'textarea[required]',
    'input[type="text"][required]',
)

# Each entry is a tuple of candidates; "{value}" is filled in per choice.
SELECTORS: dict[str, tuple[str, ...]] = {
    # Signed-out markers, in both languages.
    "logged_out": ('text=ログイン', 'text=Log in', 'text=新規登録', 'text=Sign up'),
    # The account id, when the page exposes one (optional: it often does not).
    "account_id": ('meta[name="user_id"]',),
    # Inside the form scope.
    "file_input": ('input[type="file"][name="files[]"]',),
    "title": ('input[name="title"]',),
    "caption": ('textarea[name="comment"]',),
    "tags": ('input[placeholder="Tags"]', 'input[placeholder="タグ"]'),
    "rating": ('input[type="radio"][name="x_restrict"][value="{value}"]',),
    "ai": ('input[type="radio"][name="ai_type"][value="{value}"]',),
    # Only for full auto; the real button sits outside the form and appears twice.
    "submit": ('[role="button"]:has-text("Post")', '[role="button"]:has-text("投稿する")'),
}


class FieldMissing(RuntimeError):
    """A form field could not be found (or could not be verified): the window
    stays open so the user can finish that field by hand."""

    def __init__(self, field_key: str, label: str = "", message: str = ""):
        self.field_key = field_key
        self.label = label or field_key
        super().__init__(message or f"could not find the {self.label} field on the Pixiv form")


@dataclass
class FormSpec:
    images: list[Path]
    title: str = ""
    caption: str = ""
    tags: list[str] = field(default_factory=list)
    rating: str = "all"
    ai_flag: bool = True
    submit: bool = False
    watch_seconds: float = WATCH_SECONDS
    debug: bool = True  # write Pixiv call paths/keys to data/artdrop.log


def first_present(candidates, probe):
    """The first candidate a probe accepts; None when none of them work."""
    for candidate in candidates:
        try:
            if probe(candidate):
                return candidate
        except Exception:  # noqa: BLE001 — a broken candidate is just skipped
            continue
    return None


def login_text_present(page) -> bool:
    """True when the page shows a sign-in / sign-up control."""
    def probe(selector: str) -> bool:
        locator = page.locator(selector).first
        return bool(locator.count()) and bool(locator.is_visible())

    return first_present(SELECTORS["logged_out"], probe) is not None


def logged_in(page) -> bool:
    """True when the page is a Pixiv page that is not redirecting to sign in.

    The create page redirects to accounts.pixiv.net when signed out, and shows
    "Log in" / "Sign up" (or their Japanese equivalents) when it is not.
    """
    if ACCOUNTS_HOST in (page.url or ""):
        return False
    return not login_text_present(page)


def _user_id_from_page(page) -> str:
    """The account id from the page itself (a meta tag), or ""."""
    for selector in SELECTORS["account_id"]:
        try:
            locator = page.locator(selector).first
            if locator.count():
                return (locator.get_attribute("content") or "").strip()
        except Exception:  # noqa: BLE001
            continue
    return ""


def account_info(page) -> dict:
    """{} when signed out, else {'user_id', 'name'} — both possibly empty.

    The create page's header has no profile links, so the id is best-effort and
    the name is not available; the connector copes with empty values.
    """
    if not logged_in(page):
        return {}
    return {"user_id": _user_id_from_page(page), "name": ""}


def settle(page, seconds: float = 20.0) -> None:
    """Give the create page a moment to redirect or render before judging it.

    Signed out it 302s to accounts.pixiv.net; signed in it renders the form.
    Judging a page that is still blank would call a signed-out page connected.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if ACCOUNTS_HOST in (page.url or ""):
            return
        for group in ("file_input", "logged_out"):
            for candidate in SELECTORS[group]:
                try:
                    if page.locator(candidate).first.count():
                        return
                except Exception:  # noqa: BLE001 — keep waiting
                    break
        page.wait_for_timeout(500)


def artwork_url(url: str) -> str:
    """The public URL for a /artworks/<id> page, or ""."""
    match = re.search(r"/artworks/(\d+)", url or "")
    return f"https://www.pixiv.net/artworks/{match.group(1)}" if match else ""


def url_path(url: str) -> str:
    """A URL's path only — a query string (with its tokens) never reaches the log."""
    return urlparse(str(url or "")).path or ""


def is_pixiv_api(url: str) -> bool:
    """True for Pixiv's own page calls (ajax/rpc), whatever they create.

    Deliberately broad: the exact create endpoint differs between Pixiv's
    uploaders, so every call is read and only a *trusted* path may produce the
    artwork link (`trusted_id_path`).
    """
    lowered = str(url or "").lower()
    if "pixiv.net" not in lowered:
        return False
    return any(hint in lowered for hint in API_HINTS)


def trusted_id_path(url: str) -> bool:
    """True when an id from this URL may become the artwork link."""
    path = url_path(url).lower()
    if RPC_WRITE_HINT in path:
        return True  # Pixiv's rpc endpoints are the write side
    return bool(TRUSTED_PATH_RE.search(path))


def _walk_dicts(payload):
    """Every dict inside a JSON payload, so `body.illustId` is found too."""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def looks_complete(node) -> bool:
    """True when a response object says the work is finished."""
    for key, value in node.items():
        name = str(key).lower()
        if name in ("success", "ok") and value is True:
            return True
        if name == "error" and value is False:
            return True
        if name in STATUS_KEYS and str(value).strip().casefold() in SUCCESS_WORDS:
            return True
    return False


def collect_ids(payload, keys: set) -> list[str]:
    """Ids in a Pixiv response that are safe to treat as the new work.

    An `illustId` / `illust_id` is enough on its own; a bare `id` counts only
    when the same object also reports completion — so a progress poll, a search
    result or a recommendation can never be mistaken for the finished post.
    """
    found: list[str] = []
    for node in _walk_dicts(payload):
        complete = looks_complete(node)
        for key, value in node.items():
            name = str(key)
            keys.add(name)
            if name not in STRONG_ID_KEYS and name not in WEAK_ID_KEYS:
                continue
            if not isinstance(value, (str, int)) or not str(value).isdigit():
                continue
            if name in STRONG_ID_KEYS or complete:
                found.append(str(value))
    return found


def key_names(payload, limit: int = 40) -> list[str]:
    """The JSON key names in a payload — the log never sees any value."""
    names: list[str] = []
    for node in _walk_dicts(payload):
        for key in node:
            if str(key) not in names:
                names.append(str(key))
            if len(names) >= limit:
                return names
    return names


def payload_is_candidate(payload) -> bool:
    """True when a payload looks like it could carry the new work."""
    return any(looks_complete(node) for node in _walk_dicts(payload))


def progress_finished(payload) -> bool:
    """True when a create-progress answer says the work is finished.

    Only the status words count here: every one of Pixiv's answers carries
    `error: false`, and the first poll of a real post says "not finished".
    """
    for node in _walk_dicts(payload):
        for key, value in node.items():
            if str(key).lower() in ("status", "state", "progress", "phase", "step"):
                if str(value).strip().casefold() in SUCCESS_WORDS:
                    return True
    return False


def progress_status(payload) -> str:
    """The status word of a progress answer, for the log."""
    for node in _walk_dicts(payload):
        for key, value in node.items():
            if str(key).lower() in ("status", "state", "progress", "phase", "step"):
                return str(value)[:40]
    return ""


def first_numeric(payload, keys) -> str:
    """The first numeric value stored under one of these key names."""
    for node in _walk_dicts(payload or {}):
        for key, value in node.items():
            if str(key) in keys and str(value).isdigit():
                return str(value)
    return ""


def illustration_ids(payload) -> list[str]:
    """The work ids in a profile answer: `body.illusts` keys (or its items)."""
    found: list[str] = []
    for node in _walk_dicts(payload or {}):
        illusts = node.get("illusts")
        if isinstance(illusts, dict):
            found.extend(str(key) for key in illusts if str(key).isdigit())
        elif isinstance(illusts, list):
            for item in illusts:
                if isinstance(item, dict):
                    found.extend(str(item.get(key)) for key in STRONG_ID_KEYS
                                 if str(item.get(key, "")).isdigit())
    return found


def upload_complete_ids(payload, keys: set) -> list[str]:
    """The work id Pixiv returns with `uploadCompleteData` / its `work` object."""
    found: list[str] = []
    for node in _walk_dicts(payload or {}):
        for name in ("uploadCompleteData", "work"):
            data = node.get(name)
            if not isinstance(data, (dict, list)):
                continue
            for inner in _walk_dicts(data):
                for key, value in inner.items():
                    keys.add(str(key))
                    if not isinstance(value, (str, int)) or not str(value).isdigit():
                        continue
                    if str(key) in STRONG_ID_KEYS or str(key) == "id":
                        found.append(str(value))
    return found


def best_id(entries) -> str:
    """The id from the most recent create/work call, else an upload one, else any."""
    entries = list(entries)
    for words in (("create", "work"), ("upload",)):
        for artwork_id, url in reversed(entries):
            if any(word in str(url).lower() for word in words):
                return artwork_id
    return entries[-1][0] if entries else ""


def is_closed_error(exc) -> bool:
    """True when the failure is only "that window is gone"."""
    message = str(exc).lower()
    return any(hint in message for hint in CLOSED_ERROR_HINTS)


def parse_time(value: str) -> float:
    """A `<time datetime="…">` value as a timestamp, or 0.0 when unreadable."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


class PixivBrowser:
    """The real browser driver. Tests replace this whole object."""

    # ------------------------------------------------------------------ session
    def open_login_window(self, profile: Path) -> str:
        """Sign in with the user's own Chrome — Playwright would be challenged."""
        had_window = browser.close_context(profile)  # ours must not hold the profile
        if had_window:
            time.sleep(0.5)
        return browser.open_plain_chrome(profile, LOGIN_URL)

    def open_verify_window(self, profile: Path) -> None:
        """Open the create page in Playwright (this is what proves the login)."""
        context = browser.open_context(profile)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(CREATE_URL, wait_until="domcontentloaded")
        settle(page)

    def read_login(self, profile: Path) -> dict:
        """{} when signed out, else the (possibly empty) account details."""
        page = browser.page_for(profile)
        if page is None or not logged_in(page):
            return {}
        return {"user_id": self._user_id(page), "name": ""}

    def close_window(self, profile: Path) -> None:
        """Close this profile's window (the shared Playwright keeps running)."""
        browser.close_context(profile)

    # ------------------------------------------------------------------ finding
    def _scope(self, page) -> str:
        selector = first_present(
            FORM_SCOPE,
            lambda candidate: page.locator(candidate).first.count() > 0,
        )
        if not selector:
            raise FieldMissing("form", "upload form")
        return selector

    def _verify_field(self, page, selector: str, candidate: str, field_key: str,
                      label: str) -> None:
        """Refuse a field that is in the page header or is the site search box."""
        for area in HEADER_AREAS:
            try:
                inside = page.locator(f"{area} {candidate}").first.count() > 0
            except Exception:  # noqa: BLE001 — a broken probe is skipped
                continue
            if inside:
                raise FieldMissing(
                    field_key, label,
                    f"the {label} field is inside the page header — refusing to fill it",
                )
        try:
            placeholder = str(page.locator(selector).first.get_attribute("placeholder") or "")
        except Exception:  # noqa: BLE001
            placeholder = ""
        if any(mark in placeholder.casefold() for mark in SEARCH_MARKS):
            raise FieldMissing(
                field_key, label,
                f"the {label} field looks like the site search box — refusing to fill it",
            )

    def _field_selector(self, page, field_key: str, label: str) -> str:
        """A field's selector: page-wide, but never the header or a search box."""
        scope = self._scope(page)
        selector = first_present(
            SELECTORS[field_key],
            lambda candidate: page.locator(f"{scope} {candidate}").first.count() > 0,
        )
        if not selector:
            raise FieldMissing(field_key, label)
        self._verify_field(page, f"{scope} {selector}", selector, field_key, label)
        return f"{scope} {selector}"

    def _checked(self, page, selector: str) -> bool:
        """True only when the page says the element is really selected."""
        locator = page.locator(selector).first
        try:
            if locator.is_checked():
                return True
        except Exception:  # noqa: BLE001 — not an input; try the ARIA flags
            pass
        try:
            for attribute in ("aria-checked", "data-checked", "aria-selected"):
                if (locator.get_attribute(attribute) or "").lower() == "true":
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _check_option(self, page, field_key: str, value: str, label: str) -> None:
        """Check a radio, then verify it really stayed checked."""
        scope = self._scope(page)
        selector = first_present(
            SELECTORS[field_key],
            lambda candidate: page.locator(
                f"{scope} {candidate}".format(value=value)).first.count() > 0,
        )
        if not selector:
            raise FieldMissing(field_key, label)
        full = f"{scope} {selector}".format(value=value)
        self._verify_field(page, full, selector.format(value=value), field_key, label)
        try:
            page.locator(full).first.check()
        except FieldMissing:
            raise
        except Exception as exc:  # noqa: BLE001 — not checkable, or it refused
            raise FieldMissing(field_key, label) from exc
        page.wait_for_timeout(300)
        if not self._checked(page, full):
            raise FieldMissing(field_key, f"{label} (it did not stay checked)")

    def _type(self, page, selector: str, value: str) -> None:
        locator = page.locator(selector).first
        try:
            locator.fill(value)
        except Exception:  # noqa: BLE001 — rich text editors need real typing
            locator.click()
            page.keyboard.type(value)

    def _upload(self, page, images: list[Path]) -> None:
        selector = self._field_selector(page, "file_input", "image upload")
        page.locator(selector).first.set_input_files([str(path) for path in images])
        page.wait_for_timeout(1500)

    def _wait_for_form(self, page) -> None:
        """The rest of the form shows up once the chosen files are accepted."""
        for candidate in SELECTORS["title"]:
            try:
                page.locator(candidate).first.wait_for(
                    state="attached", timeout=FORM_WAIT_SECONDS * 1000)
                return
            except Exception:  # noqa: BLE001 — let the field lookup report it
                continue

    # --------------------------------------------------------------------- tags
    def _tag_count(self, page) -> int | None:
        """The "0/10" counter next to the tag box, or None when there is none."""
        try:
            locator = page.get_by_text(TAG_COUNTER_RE).first
            if not locator.count():
                return None
            match = TAG_COUNTER_RE.search(locator.inner_text() or "")
            return int(match.group(0).split("/")[0].strip()) if match else None
        except Exception:  # noqa: BLE001
            return None

    def _set_tags(self, page, tags: list[str]) -> None:
        tags = [str(tag).strip() for tag in tags if str(tag).strip()][:MAX_TAGS]
        if not tags:
            raise FieldMissing("tags", "at least one tag (Pixiv requires one)")
        selector = self._field_selector(page, "tags", "tags")
        locator = page.locator(selector).first
        before = self._tag_count(page)
        for tag in tags:
            locator.click()
            page.keyboard.type(tag)
            page.wait_for_timeout(200)
            page.keyboard.press("Enter")
            page.wait_for_timeout(250)
        after = self._tag_count(page)
        if after is None or after <= (before or 0):
            raise FieldMissing(
                "tags",
                "tags",
                f"the tags were typed but the 0/{MAX_TAGS} counter did not move — add them by hand",
            )

    # ------------------------------------------------------------------- rating
    def _visible_required(self, page) -> tuple[str, ...]:
        """Every required control that is visible right now."""
        scope = self._scope(page)
        found: list[str] = []
        for selector in REQUIRED_SELECTORS:
            locator = page.locator(f"{scope} {selector}")
            for index in range(locator.count()):
                node = locator.nth(index)
                try:
                    if not node.is_visible():
                        continue
                    found.append(f"{node.get_attribute('name') or selector}="
                                 f"{node.get_attribute('value') or ''}")
                except Exception:  # noqa: BLE001 — a vanished node is skipped
                    continue
        return tuple(sorted(found))

    def _set_rating(self, page, rating: str) -> None:
        value = RATINGS.get(rating, RATINGS["all"])
        before = self._visible_required(page)
        self._check_option(page, "rating", value, f"age rating ({rating})")
        page.wait_for_timeout(400)
        appeared = [item for item in self._visible_required(page) if item not in before]
        if appeared:
            raise FieldMissing(
                "rating details",
                "age rating details",
                "choosing R-18 revealed extra required options — set them in the window "
                "and post from there",
            )

    def _set_ai_flag(self, page) -> None:
        self._check_option(page, "ai", AI_YES, "AI-generated work (Yes)")

    # ------------------------------------------------------------------- submit
    def _submit_button(self, page):
        """The visible Post button, or None (it appears twice, outside the form)."""
        for name in ("Post", "投稿する"):
            try:
                locator = page.get_by_role("button", name=name, exact=True)
                total = locator.count()
            except Exception:  # noqa: BLE001 — role lookup unavailable
                continue
            for index in range(total):
                node = locator.nth(index)
                try:
                    if node.is_visible():
                        return node
                except Exception:  # noqa: BLE001
                    continue
        for selector in SELECTORS["submit"]:
            locator = page.locator(selector)
            for index in range(locator.count()):
                node = locator.nth(index)
                try:
                    if node.is_visible():
                        return node
                except Exception:  # noqa: BLE001
                    continue
        return None

    # -------------------------------------------------------------- the result
    def _fetch_json(self, page, path: str):
        """Call one of Pixiv's own endpoints inside the page (its session).

        Used for the profile id diff, which is independent of Pixiv's UI.
        """
        try:
            return page.evaluate(
                """async (path) => {
                    const response = await fetch(path, {credentials: 'include'});
                    return await response.json();
                }""",
                path,
            )
        except Exception:  # noqa: BLE001 — the page is gone, or the call failed
            return None

    def _user_id(self, page) -> str:
        """The signed-in Pixiv user id: from the page, or from /ajax/user/extra."""
        found = _user_id_from_page(page)
        if found:
            return found
        return first_numeric(self._fetch_json(page, "/ajax/user/extra"), USER_ID_KEYS)

    def _profile_illust_ids(self, page, user_id: str) -> set[str]:
        """The account's existing work ids, from Pixiv's own profile endpoint."""
        if page is None or not str(user_id).isdigit():
            return set()
        payload = self._fetch_json(page, f"/ajax/user/{user_id}{PROFILE_ALL_HINT}")
        return set(illustration_ids(payload))

    def _capture_creates(self, context, debug: bool = True) -> dict:
        """Watch Pixiv's own page calls while the user posts.

        Registered on the **context**, before the upload: page-level listeners
        stop at a navigation, and Pixiv navigates away from the create form once
        the work is accepted. Every ajax/rpc answer is read, GET and POST alike —
        the create POST has no id, the progress poll and the profile answers do.
        With `debug`, each call's method and path (never the query string) and
        the key names of candidate answers go to `data/artdrop.log`.
        """
        captured: dict = {
            "entries": [],  # (id, url) from trusted create/work/upload paths
            "complete_ids": [],  # ids from uploadCompleteData / the work object
            "artwork_hits": [],  # /artworks/<id> paths seen after the create POST
            "keys": set(),
            "debug": bool(debug),
            "logged": set(),
            "handler": None,
            "created": False,  # the create POST has been seen
            "progress_done": False,
            "closed": False,
            "user_id": "",
            "before": set(),  # the account's works before this post
        }

        def on_response(response) -> None:
            try:
                url = str(response.url or "")
                if not is_pixiv_api(url):
                    return
                method = str(getattr(response.request, "method", "")).upper()
                path = url_path(url)
                if captured["debug"]:
                    log.info("Pixiv call %s %s", method, path)
                found_user = USER_PATH_RE.search(path)
                if found_user and not captured["user_id"]:
                    captured["user_id"] = found_user.group(1)
                payload = response.json()
            except Exception:  # noqa: BLE001 — an image body or a redirect: nothing to read
                return

            if CREATE_PATH in path:
                captured["created"] = True
                if PROGRESS_HINT in path:
                    if progress_finished(payload):
                        captured["progress_done"] = True
                    if captured["debug"]:
                        log.info("Pixiv progress %s", progress_status(payload) or "?")
            if PROFILE_TOP_HINT in path:
                for artwork_id in upload_complete_ids(payload, captured["keys"]):
                    captured["complete_ids"].append((artwork_id, url))
                    if captured["debug"]:
                        log.info("Pixiv uploadCompleteData id %s", artwork_id)
            ids = collect_ids(payload, captured["keys"])
            if captured["debug"] and (ids or payload_is_candidate(payload)):
                log.info("Pixiv answer %s %s keys=%s", method, path, key_names(payload))
            if ids and trusted_id_path(url):
                for artwork_id in ids:
                    captured["entries"].append((artwork_id, url))
                    if captured["debug"] and artwork_id not in captured["logged"]:
                        captured["logged"].add(artwork_id)
                        log.info("Pixiv id %s seen in %s %s", artwork_id, method, path)
            hit = ARTWORK_PATH_RE.search(path)
            if hit and captured["created"]:
                captured["artwork_hits"].append((hit.group(1), url))

        try:
            context.on("response", on_response)
            captured["handler"] = on_response
        except Exception:  # noqa: BLE001 — no event support: the fallbacks still run
            pass
        return captured

    def _stop_capture(self, context, captured: dict) -> None:
        handler = captured.get("handler")
        if handler is None:
            return
        try:
            context.remove_listener("response", handler)
        except Exception:  # noqa: BLE001
            pass
        captured["handler"] = None

    def _resolved_link(self, captured: dict, page) -> str:
        """The new work's URL from whatever Pixiv has told us so far."""
        for key in ("complete_ids", "entries", "artwork_hits"):
            artwork_id = best_id(captured.get(key) or [])
            if artwork_id:
                return f"https://www.pixiv.net/artworks/{artwork_id}"
        if captured.get("created") and self._may_diff(captured, page):
            known = set(captured.get("before") or ())
            now = self._profile_illust_ids(page, captured.get("user_id") or "")
            new_ids = [artwork_id for artwork_id in now if artwork_id not in known]
            if new_ids:
                newest = max(new_ids, key=int)
                if captured.get("debug"):
                    log.info("Pixiv profile diff found %s", newest)
                return f"https://www.pixiv.net/artworks/{newest}"
        return ""

    def _may_diff(self, captured: dict, page) -> bool:
        """Pixiv says the work is done, or has left the create form entirely."""
        if page is None or not str(captured.get("user_id") or "").isdigit():
            return False
        if captured.get("progress_done"):
            return True
        try:
            return CREATE_PAGE_PATH not in (page.url or "")
        except Exception:  # noqa: BLE001 — a closed page counts as "left"
            return True

    def _no_link_note(self, captured: dict) -> str:
        """Why no link: the window closed, or Post did happen anyway."""
        if captured.get("closed"):
            return "window closed"
        return "posted" if captured.get("created") else ""

    def _upload_area(self, page):
        """The element around the uploader: the file input or the Add-images button."""
        for anchor in UPLOAD_ANCHORS:
            try:
                node = page.locator(anchor).first
                if not node.count():
                    continue
            except Exception:  # noqa: BLE001
                continue
            for xpath in UPLOAD_AREA_XPATHS:
                try:
                    area = node.locator(xpath).first
                    if area.count():
                        return area
                except Exception:  # noqa: BLE001
                    continue
        return None

    def _has_previews(self, page) -> bool:
        """True while the chosen files are still shown around the uploader.

        Deliberately narrow: a canvas or a thumbnail elsewhere on the page (a
        banner, a recommendation strip) must not look like an uploaded file, or
        the form would never count as reset after a real post.
        """
        area = self._upload_area(page)
        if area is None:
            return False
        for selector in PREVIEW_SELECTORS:
            try:
                if area.locator(selector).first.count():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _form_reset(self, page) -> bool:
        """True when the create form emptied itself, which means it was accepted."""
        try:
            title = page.locator('input[name="title"]').first
            if not title.count():
                return False
            if str(title.input_value() or "").strip():
                return False
        except Exception:  # noqa: BLE001 — cannot read it, so do not guess
            return False
        return not self._has_previews(page)

    def _read_dashboard(self, page, opened_at: float) -> str:
        """The newest work on the works page, when it was posted after we started."""
        cards: list[tuple[str, float]] = []
        try:
            locator = page.locator(DASHBOARD_LINKS[0])
            total = min(locator.count(), 12)
        except Exception:  # noqa: BLE001
            return ""
        for index in range(total):
            node = locator.nth(index)
            try:
                url = artwork_url(node.get_attribute("href") or "")
                if not url:
                    continue
                stamp = node.locator(CARD_TIME_XPATH).first
                when = parse_time(stamp.get_attribute("datetime") or "") if stamp.count() else 0.0
            except Exception:  # noqa: BLE001 — a card we cannot read is skipped
                continue
            cards.append((url, when))
        if not cards:
            return ""
        if any(when for _, when in cards):  # timestamps available: trust only new ones
            return next((url for url, when in cards if when and when >= opened_at), "")
        return cards[0][0]  # no timestamps on this layout; the reset was the evidence

    def _newest_work(self, context, opened_at: float) -> str:
        """Look the new work up on the works dashboard (the fallback path)."""
        try:
            page = context.new_page()
        except Exception:  # noqa: BLE001 — the window may have gone
            return ""
        try:
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            return self._read_dashboard(page, opened_at)
        except Exception:  # noqa: BLE001
            return ""
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    def _watch(self, profile: Path, context, page, seconds: float, captured: dict,
               opened_at: float) -> tuple[str, str]:
        """Wait for the new work's URL after the user (or we) submit.

        Returns (url, note). The note is "window closed" only when the window
        really closed with no create POST seen, and "posted" when Pixiv accepted
        the work but no id could be read. Pixiv reloads the create form — or
        navigates to the profile — instead of opening the new work, so the page
        calls, a profile id diff and the works dashboard are used in turn.
        """
        deadline = time.time() + seconds
        used_dashboard = False
        while time.time() < deadline:
            try:
                if page.is_closed():
                    captured["closed"] = True
                    return self._after_window_closed(profile, context, captured, opened_at)
                url = artwork_url(page.url or "")
                if url:
                    return url, ""
                url = self._resolved_link(captured, page)
                if url:
                    return url, ""
                if not used_dashboard and self._form_reset(page):
                    used_dashboard = True
                    url = self._newest_work(context, opened_at)
                    if url:
                        return url, ""
                page.wait_for_timeout(2000)
            except Exception as exc:  # noqa: BLE001
                if is_closed_error(exc):
                    captured["closed"] = True
                    return self._after_window_closed(profile, context, captured, opened_at)
                raise
        return "", self._no_link_note(captured)

    def _after_window_closed(self, profile: Path, context, captured: dict,
                             opened_at: float) -> tuple[str, str]:
        """The window went away mid-post: check whether the work still landed."""
        url = self._resolved_link(captured, None)
        if url:
            return url, ""
        url = self._newest_work(context, opened_at)
        if url:
            return url, ""
        # the window took its context with it: open the profile once more
        try:
            fresh = browser.open_context(profile)
        except Exception:  # noqa: BLE001 — still locked, or no browser around
            return "", self._no_link_note(captured)
        try:
            page = fresh.pages[0] if fresh.pages else fresh.new_page()
            url = self._resolved_link(captured, page) or self._newest_work(fresh, opened_at)
        except Exception:  # noqa: BLE001
            url = ""
        finally:
            browser.close_context(profile)
        return (url, "") if url else ("", self._no_link_note(captured))

    # --------------------------------------------------------------------- fill
    def fill(self, profile: Path, spec: FormSpec) -> tuple[str, str]:
        """Fill the form; submit only when asked. Returns (artwork url, note).

        The window is reused when it is already open, and stays open on a failure
        so the user can set the missing field by hand and submit there.
        """
        opened_at = time.time()  # the dashboard fallback only trusts newer works
        context = browser.open_context(profile)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(CREATE_URL, wait_until="domcontentloaded")
        settle(page)
        if not logged_in(page):
            raise RuntimeError("not logged in — press Log in, sign in in the Chrome window, "
                               "close it, then press Check login")
        # The capture goes on the context before the upload, and the account's
        # existing work ids are remembered now: the new id is the difference.
        captured = self._capture_creates(context, debug=spec.debug)
        captured["user_id"] = self._user_id(page)
        captured["before"] = self._profile_illust_ids(page, captured["user_id"])
        if spec.debug:
            log.info("Pixiv account %s: %s works before this post", captured["user_id"] or "?",
                     len(captured["before"]))
        url, note = "", ""
        try:
            try:
                self._upload(page, spec.images)
                self._wait_for_form(page)
                if spec.title:
                    self._type(page, self._field_selector(page, "title", "title"), spec.title)
                if spec.caption:
                    self._type(page, self._field_selector(page, "caption", "caption"),
                               spec.caption)
                self._set_tags(page, spec.tags)
                self._set_rating(page, spec.rating)
                self._set_ai_flag(page)
            except FieldMissing as exc:
                raise RuntimeError(
                    f"{exc} — the window is still open, set that field by hand and submit there"
                ) from exc
            page.bring_to_front()
            if spec.submit:
                button = self._submit_button(page)
                if button is None:
                    raise RuntimeError("could not find the Post button — the window is still "
                                       "open, press it there")
                button.click()
            url, note = self._watch(profile, context, page, spec.watch_seconds, captured,
                                    opened_at)
        finally:
            self._stop_capture(context, captured)
        if url:
            browser.close_context(profile)
        return url, note
