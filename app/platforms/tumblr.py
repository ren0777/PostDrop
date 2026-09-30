"""Tumblr connector — OAuth2 and NPF (Neue Post Format) posts."""
from __future__ import annotations

import json

import httpx

from ..core.images import Prepared
from ..core.oauth import Provider
from .base import Field, PublishError
from .oauth import OAuthConnector

API = "https://api.tumblr.com/v2"
TIMEOUT = httpx.Timeout(120.0, connect=15.0)
USER_AGENT = "ArtDrop/1.0"
MAX_TAGS = 30


def blog_host(blog: str) -> str:
    """'name' or '@name' → 'name.tumblr.com'; a full hostname stays as is."""
    value = str(blog or "").strip().lstrip("@").rstrip("/")
    if not value:
        return ""
    return value if "." in value else f"{value}.tumblr.com"


def build_npf(text: str, tags: list[str], draft: bool, images: list[Prepared],
              alt: str = "") -> tuple[dict, dict]:
    """The NPF payload and the file parts it references."""
    blocks: list[dict] = []
    files: dict = {}
    for index, image in enumerate(images, start=1):
        identifier = f"media{index}"
        block = {
            "type": "image",
            "media": [{
                "identifier": identifier,
                "type": image.mime,
                "width": image.width,
                "height": image.height,
            }],
        }
        if alt:
            block["alt_text"] = alt
        blocks.append(block)
        files[identifier] = (image.filename, image.data, image.mime)
    if text:
        blocks.append({"type": "text", "text": text})
    payload = {
        "content": blocks,
        "tags": ",".join(tags[:MAX_TAGS]),
        "state": "draft" if draft else "published",
    }
    return payload, files


def post_url(blog: str, post_id: str, published: bool) -> str:
    host = blog_host(blog)
    if not published or not host or not post_id:
        return ""
    return f"https://{host}/post/{post_id}"


class Tumblr(OAuthConnector):
    id = "tumblr"
    label = "Tumblr"
    caption_limit = 4096
    max_images = 10
    max_bytes = 20_000_000
    max_side = None
    original_formats = ("JPEG", "PNG", "WEBP")
    tags_in_caption = False
    api = API
    oauth_provider = Provider(
        authorize_url="https://www.tumblr.com/oauth2/authorize",
        token_url="https://api.tumblr.com/v2/oauth2/token",
        scope="basic write offline_access",
        narrow_tls=True,  # api.tumblr.com drops the default ClientHello on some PCs
    )
    fields = [
        Field("blog", "Blog", placeholder="yourname.tumblr.com",
              help="The blog the posts go to."),
        Field("client_id", "OAuth2 client ID",
              help="From tumblr.com/oauth/apps — called 'OAuth2.0 Client ID' there."),
        Field("client_secret", "OAuth2 client secret", secret=True,
              help="Called 'OAuth2.0 Client Secret' on the same page."),
        Field("redirect_uri", "Redirect URI", required=False,
              placeholder="http://localhost:8765/oauth/tumblr",
              help="Leave empty to use the default. Must also be listed in the Tumblr app."),
    ]
    option_fields = [
        Field("draft", "Post as a draft (good for testing)", kind="checkbox", required=False),
    ]
    oauth_help = (
        "Register an app at tumblr.com/oauth/apps. Under 'OAuth2 redirect URLs' add "
        "http://localhost:8765/oauth/tumblr (Tumblr rejects IP-address URLs), save your "
        "client ID and secret here, then press "
        "Connect. If Tumblr refuses the localhost URL, register any other URL instead, paste it "
        "into the Redirect URI field, press Connect, and copy the code from the address bar into "
        "the box below."
    )
    setup_help = (
        "1) Open tumblr.com/oauth/apps → Register application. "
        "2) Set the website to anything (e.g. https://example.com) and add the redirect URL "
        "http://localhost:8765/oauth/tumblr. "
        "3) Paste the OAuth2 client ID and secret here, plus your blog name, and Save. "
        "4) Press Connect, allow access, and press Test connection."
    )

    # ------------------------------------------------------------------ helpers
    def _json(self, response: httpx.Response, fallback: str) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = {}
        meta = body.get("meta") or {}
        status = meta.get("status")
        # Tumblr answers 201 Created for a new post, 200 for the rest.
        if response.status_code >= 300 or (status and int(status) >= 400):
            errors = body.get("errors") or []
            detail = errors[0].get("detail") if errors and isinstance(errors[0], dict) else None
            raise PublishError(f"{fallback}: {detail or meta.get('msg') or f'HTTP {response.status_code}'}")
        response_body = body.get("response")
        return response_body if isinstance(response_body, dict) else {}

    # ---------------------------------------------------------------- interface
    def redirect_default(self) -> str:
        """Tumblr rejects IP-address URLs, so its redirect is localhost."""
        from ..core import paths  # noqa: PLC0415 — kept local: this is the only use

        return f"http://localhost:{paths.PORT}/oauth/{self.id}"

    async def test(self, cfg: dict) -> str:
        token = await self.access_token(cfg)
        async with self._client(TIMEOUT) as client:
            response = await client.get(
                f"{self.api}/user/info",
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
            )
        body = self._json(response, "Tumblr user/info failed")
        user = body.get("user") or {}
        blogs = [str(blog.get("name") or "") for blog in (user.get("blogs") or [])]
        wanted = str(cfg.get("blog") or "").split(".")[0].lstrip("@")
        if blogs and wanted and wanted not in blogs:
            raise PublishError(
                f"Connected as {user.get('name')} — but {cfg.get('blog')} is not one of this "
                f"account's blogs ({', '.join(name for name in blogs if name)})."
            )
        return f"Connected as {user.get('name')} — posting to {cfg.get('blog')}"

    async def send(self, post, cfg: dict, text: str, images: list) -> tuple[str, str]:
        token = await self.access_token(cfg)
        options = self.options_for(post, cfg)
        alt = str(options.get("alt_text") or post.alt_text or "").strip()
        draft = bool(options.get("draft"))
        payload, media = build_npf(text, self.resolved_tags(post, cfg), draft, images, alt)

        files = {"json": (None, json.dumps(payload), "application/json")}
        files.update(media)
        blog = blog_host(cfg.get("blog"))
        if not blog:
            raise PublishError("No blog name is set — add it in Accounts.")
        async with self._client(TIMEOUT) as client:
            response = await client.post(
                f"{self.api}/blog/{blog}/posts",
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                files=files,
            )
        body = self._json(response, "Tumblr post failed")
        post_id = str(body.get("id_string") or body.get("id") or "")
        # Tumblr's API cannot set community labels (tumblr/docs issues #110 and #125),
        # so a published mature post needs the label added by hand in the app.
        note = ""
        if (bool(options.get("mature")) or bool(post.options.get("mature"))) and not draft:
            note = ("Tumblr's API can't set community labels — mark this post Mature "
                    "in the Tumblr app")
        return post_url(blog, post_id, published=not draft), note
