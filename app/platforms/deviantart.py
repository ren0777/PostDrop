"""DeviantArt connector — OAuth2, Sta.sh upload, then publish a deviation.

Field names and array shapes follow the 20240701 API spec: `title` and
`artist_comments` belong on /stash/submit, while /stash/publish takes `itemid`,
`is_mature` (always), `mature_level` (when mature), `tags[]` and `galleryids[]`.
"""
from __future__ import annotations

import re

import httpx

from ..core.oauth import Provider
from ..core.post import clip_title
from .base import Field, PublishError
from .oauth import OAuthConnector

API = "https://www.deviantart.com/api/v1/oauth2"
TIMEOUT = httpx.Timeout(300.0, connect=20.0)
USER_AGENT = "ArtDrop/1.0"
# DeviantArt only accepts https redirect URIs; this is the one the setup guide
# registers (the browser lands on it and the user pastes the address back).
DEV_REDIRECT = "https://127.0.0.1/artdrop"
MAX_TAGS = 30
TITLE_LIMIT = 50
MATURE_LEVELS = ("moderate", "strict")
DEFAULT_MATURE_LEVEL = "moderate"
_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_TAG_CLEAN_RE = re.compile(r"[^A-Za-z0-9_]")


def base36(number: int) -> str:
    number = int(number)
    if number <= 0:
        return "0"
    out = ""
    while number:
        number, remainder = divmod(number, 36)
        out = _BASE36_DIGITS[remainder] + out
    return out


def stash_url(itemid) -> str:
    return f"https://sta.sh/0{base36(int(itemid))}" if itemid else ""


def default_title(text: str, character: str = "", series: str = "") -> str:
    """A DeviantArt title: the character and series, or the first caption line.

    Titles are cut at a word boundary with no dangling punctuation.
    """
    character = (character or "").strip()
    series = (series or "").strip()
    if character:
        value = f"{character} — {series}" if series else character
        return clip_title(value, TITLE_LIMIT) or "Untitled"
    line = next((part.strip() for part in (text or "").splitlines() if part.strip()), "")
    return clip_title(line.lstrip("#"), TITLE_LIMIT) or "Untitled"


def sanitize_tags(tags) -> list[str]:
    """DeviantArt tags: letters, numbers and underscore only, deduped, max 30."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags or []:
        clean = _TAG_CLEAN_RE.sub("", str(tag))
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        out.append(clean)
        if len(out) >= MAX_TAGS:
            break
    return out


def gallery_ids(options: dict) -> list[str]:
    """The gallery folder UUIDs typed on the card (comma or space separated)."""
    raw = str((options or {}).get("gallery_folder") or "")
    return [part for part in re.split(r"[,\s]+", raw) if part]


def submit_data(title: str, comments: str, tags: list[str]) -> dict:
    """Form fields for /stash/submit — the title and the caption live here.

    httpx repeats the key when a value is a list, which is how DA arrays are sent.
    """
    return {
        "title": title,
        "artist_comments": comments,
        "is_ai_generated": "true",
        "noai": "true",
        "tags[]": list(tags),
    }


def publish_data(itemid, tags: list[str], options: dict, mature: bool) -> dict:
    """Form fields for /stash/publish. There is no title or description here."""
    data = {
        "itemid": str(itemid),
        "is_mature": "true" if mature else "false",
        "is_ai_generated": "true",
        "noai": "true",
        "tags[]": list(tags),
    }
    if mature:
        level = str((options or {}).get("mature_level") or DEFAULT_MATURE_LEVEL)
        data["mature_level"] = level if level in MATURE_LEVELS else DEFAULT_MATURE_LEVEL
    folders = gallery_ids(options)
    if folders:
        data["galleryids[]"] = folders
    return data


def agreement_error(body: dict) -> bool:
    """True when the failure looks like the submission-policy / terms agreement."""
    body = body if isinstance(body, dict) else {}
    if body.get("error_code") in (0, 1):
        return True
    text = f"{body.get('error', '')} {body.get('error_description', '')}".lower()
    return "policy" in text or "terms" in text or "agree" in text


def _body(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class DeviantArt(OAuthConnector):
    id = "deviantart"
    label = "DeviantArt"
    caption_limit = 65_535
    max_images = 1  # a deviation is a single file
    max_bytes = 30_000_000
    max_side = None
    original_formats = ("JPEG", "PNG", "WEBP", "GIF")
    tags_in_caption = False
    api = API
    oauth_provider = Provider(
        authorize_url="https://www.deviantart.com/oauth2/authorize",
        token_url="https://www.deviantart.com/oauth2/token",
        scope="basic stash publish",
        pkce=True,  # OAuth 2.1: S256 challenges are required for new apps
    )
    fields = [
        Field("client_id", "Client ID",
              help="From deviantart.com/developers/applications."),
        Field("client_secret", "Client secret", secret=True,
              help="Shown next to the client ID on the same page."),
        Field("redirect_uri", "Redirect URI", required=False,
              placeholder="https://127.0.0.1/artdrop",
              help="The https URL you registered with DeviantArt (it only accepts https). "
                   "Leave empty to use the default."),
    ]
    option_fields = [
        Field("title", "Title (optional)", required=False,
              placeholder="Defaults to the first caption line"),
        Field("gallery_folder", "Gallery folder ID (UUID, optional)", required=False, sticky=True,
              placeholder="from the folder's URL",
              help="Several folders can be separated by commas."),
        Field("mature", "Mature (18+)", kind="checkbox", required=False),
        Field("mature_level", "Mature level", kind="select", required=False,
              choices=MATURE_LEVELS),
        Field("stash_only", "Save to Sta.sh only — no publish (testing)", kind="checkbox",
              required=False),
    ]
    oauth_help = (
        "Register a **Confidential** application at deviantart.com/developers/applications, tick "
        "the scopes Basic information, Stash and Publish, and add https://127.0.0.1/artdrop as its "
        "redirect URI. Save the client ID and secret here, then press Connect: the browser lands "
        "on that https address, and you copy the whole address from its address bar into the box "
        "below."
    )
    setup_help = (
        "1) Open deviantart.com/developers/applications → Register application, client type "
        "Confidential. "
        "2) Add the redirect URI https://127.0.0.1/artdrop (DeviantArt only accepts https) and "
        "grant the scopes Basic information, Stash and Publish. "
        "3) Paste the client ID and secret here and Save. "
        "4) Press Connect, allow access, then copy the address you land on into the box below and "
        "press Finish connection. "
        "Tip: tick 'Save to Sta.sh only' on a first post to check everything without publishing."
    )

    # ------------------------------------------------------------------ helpers
    def _json(self, response: httpx.Response, fallback: str) -> dict:
        body = _body(response)
        error = body.get("error") or (body.get("error_description") if body.get("status") == "error" else "")
        if response.status_code != 200 or error:
            detail = body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
            raise PublishError(f"{fallback}: {detail}")
        return body

    def _needs_agreement(self, response: httpx.Response) -> bool:
        body = _body(response)
        if response.status_code == 200 and not (body.get("error") or body.get("status") == "error"):
            return False
        return agreement_error(body)

    # --------------------------------------------------------------- OAuth bits
    def redirect_default(self) -> str:
        """DeviantArt only accepts https redirect URIs, so the default is this one."""
        return DEV_REDIRECT

    # ---------------------------------------------------------------- interface
    async def test(self, cfg: dict) -> str:
        token = await self.access_token(cfg)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                f"{self.api}/user/whoami",
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
            )
        body = self._json(response, "DeviantArt whoami failed")
        user = body.get("user") if isinstance(body.get("user"), dict) else body
        return f"Connected as {user.get('username') or user.get('name') or 'unknown'}"

    async def send(self, post, cfg: dict, text: str, images: list) -> tuple[str, str]:
        token = await self.access_token(cfg)
        options = self.options_for(post, cfg)
        mature = bool(options.get("mature")) or bool(post.options.get("mature"))
        tags = sanitize_tags(self.resolved_tags(post, cfg))
        image = images[0]
        title = str(options.get("title") or "").strip() or default_title(
            text, post.character, post.series)

        headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            submit = await client.post(
                f"{self.api}/stash/submit",
                headers=headers,
                data=submit_data(title, text, tags),
                files={"file": (image.filename, image.data, image.mime)},
            )
            body = self._json(submit, "DeviantArt Sta.sh upload failed")
            itemid = body.get("itemid")
            if not itemid:
                raise PublishError("DeviantArt Sta.sh upload failed: no item id came back.")

            if options.get("stash_only"):
                return stash_url(itemid), ""

            fields = publish_data(itemid, tags, options, mature)
            publish = await client.post(f"{self.api}/stash/publish", headers=headers, data=fields)
            note = ""
            if self._needs_agreement(publish):
                # Older API versions want the policy and terms accepted first.
                fields = dict(fields, agree_submission="true", agree_tos="true")
                publish = await client.post(f"{self.api}/stash/publish", headers=headers, data=fields)
                note = ("DeviantArt asked for the submission policy / terms agreement; "
                        "the retry with agree_submission + agree_tos worked")

        result = self._json(publish, "DeviantArt publish failed")
        return str(result.get("url") or ""), note
