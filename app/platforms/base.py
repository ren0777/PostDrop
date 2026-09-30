"""Shared connector machinery: Field, Result, PublishError, Connector."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from ..core import accounts, secrets, settings
from ..core.images import Prepared, prepare
from ..core.post import Post, build_caption, parse_tags

log = logging.getLogger("artdrop.publish")


class PublishError(Exception):
    """A failure with a message that is safe to show the user."""


@dataclass
class Field:
    key: str
    label: str
    secret: bool = False
    placeholder: str = ""
    help: str = ""
    required: bool = True
    kind: str = "text"  # "text", "checkbox" or "select" (option fields)
    choices: tuple = ()  # for kind="select"
    sticky: bool = False  # option fields: keep the value for the next post


@dataclass
class Result:
    ok: bool
    url: str | None = None
    error: str | None = None
    preview: dict | None = None
    note: str | None = None  # something the user should know after a success


def scrub(message, cfg: dict, connector: "Connector") -> str:
    """Blank out any secret values (tokens, webhook URLs) from an error text."""
    text = str(message)
    for field in connector.fields:
        if not field.secret:
            continue
        value = str((cfg or {}).get(field.key, ""))
        if len(value) >= 6:
            text = text.replace(value, "***")
    # Access tokens travel in URLs for some sites, so blank those too.
    tokens = (cfg or {}).get("tokens")
    if isinstance(tokens, dict):
        for value in tokens.values():
            value = str(value or "")
            if len(value) >= 6:
                text = text.replace(value, "***")
    return text


class Connector:
    id = ""
    label = ""
    caption_limit = 2000
    max_images = 10
    max_bytes = 9_500_000
    max_total_bytes: int | None = None  # a whole post's limit, when the site has one
    max_side: int | None = None
    original_formats = ("JPEG", "PNG")
    # Formats the site cannot take at all: re-encode them to PNG (lossless).
    png_formats: tuple[str, ...] = ()
    fields: list[Field] = []
    setup_help = ""
    # True when the connector puts the tags in a dedicated field instead of
    # appending "#tag #tag" to the caption (Tumblr, DeviantArt).
    tags_in_caption = True
    # Cap on how many tags go into the caption (Instagram: 30). None = all.
    max_tags: int | None = None
    # Extra per-post inputs the UI shows on the site's card.
    option_fields: list[Field] = []
    # True for connectors that connect through OAuth (see platforms/oauth.py).
    is_oauth = False
    # The caption style a card starts with (see core/settings.py).
    default_caption_style = "ai"

    # ---------------------------------------------------------------- helpers
    def connected(self, cfg: dict) -> bool:
        return False

    def config_warning(self, cfg: dict) -> str:
        """A hint to show after saving this site's settings, or ""."""
        return ""

    def remember(self, cfg: dict, fields: dict) -> None:
        """Persist a little account detail after a success (works for accounts too)."""
        card_id = self.card_id(cfg)
        if ":" in card_id:
            site, _, key = card_id.partition(":")
            accounts.update(site, key, fields)
        else:
            cfg.update(fields)
            secrets.save(self.id, cfg)

    async def login(self, cfg: dict) -> str:
        """Interactive login for browser-driven sites (Pixiv)."""
        raise PublishError(f"{self.label} does not use a browser login.")

    async def logout(self, cfg: dict) -> str:
        """Forget a browser login (Pixiv); does nothing elsewhere."""
        return ""

    def card_problem(self, post: Post, cfg: dict) -> str | None:
        """A problem with this post for this card (e.g. a missing Pixiv rating)."""
        return None

    def tags_for(self, post: Post, cfg: dict | None = None) -> list[str]:
        """The tags for this card, capped by the site's own limit."""
        tags = self.resolved_tags(post, cfg)
        return tags[: self.max_tags] if self.max_tags is not None else tags

    def card_id(self, cfg: dict | None = None) -> str:
        """The card this publish belongs to (an Instagram account has its own)."""
        return str((cfg or {}).get("card_id") or self.id)

    def options_for(self, post: Post, cfg: dict | None = None) -> dict:
        """This card's per-post options: caption, tags, style and site extras."""
        return post.options.get(self.card_id(cfg)) or {}

    def caption_style(self, post: Post, cfg: dict | None = None) -> str:
        """This post's style for this card: the per-post choice, or the saved one."""
        options = self.options_for(post, cfg)
        style = str(options.get("style") or "").strip()
        if style in settings.VALID_STYLES:
            return style
        return settings.style_for(self.card_id(cfg), self.default_caption_style)

    def build_text(self, post: Post, cfg: dict | None = None) -> str:
        """The text this card will show, following this post's caption style."""
        style = self.caption_style(post, cfg)
        if style == "none":
            return ""
        if style == "name_series":
            name = str(post.character or "").strip()
            series = str(post.series or "").strip()
            if not name:
                return ""
            return f"{name} — {series}" if series else name
        tags = self.tags_for(post, cfg)
        return build_caption(
            post.caption_for(self.card_id(cfg)),
            tags if self.tags_in_caption else [],
            self.caption_limit,
        )

    def resolved_tags(self, post: Post, cfg: dict | None = None) -> list[str]:
        """The tags for this card: its own override, or the shared ones."""
        options = self.options_for(post, cfg)
        override = options.get("tags")
        return parse_tags(override) if override else list(post.tags)

    def preview_extra(self, post: Post, cfg: dict) -> dict:
        """Extra fields for the dry-run preview of this card (optional)."""
        return {}

    def missing(self, cfg: dict) -> list[str]:
        """Labels of required fields that are not filled in yet."""
        return [
            field.label
            for field in self.fields
            if field.required and not str((cfg or {}).get(field.key, "")).strip()
        ]

    def check(self, post: Post) -> str | None:
        """A human-readable problem with this post, or None."""
        if not post.images:
            return "No images were attached."
        return None

    def prepare_images(self, post: Post) -> list[Prepared]:
        prepared = [
            prepare(
                path,
                max_bytes=self.max_bytes,
                max_side=self.max_side,
                formats=self.original_formats,
                png_formats=self.png_formats,
            )
            for path in post.images
        ]
        if self.max_total_bytes and sum(len(item.data) for item in prepared) \
                > self.max_total_bytes:
            prepared = self._fit_total(post, prepared)
        return prepared

    def _fit_total(self, post: Post, prepared: list[Prepared]) -> list[Prepared]:
        """Shrink only the biggest images until the whole post fits the site's total.

        Water-filling: images under their fair share of the budget stay untouched,
        and the rest are re-encoded (high-quality JPEG) to share what is left.
        """
        budget = int(self.max_total_bytes * 0.97)  # headroom for the form's own overhead
        sizes = sorted(len(item.data) for item in prepared)
        remaining, cap = budget, None
        for index, size in enumerate(sizes):
            share = remaining // (len(sizes) - index)
            if size > share:
                cap = share
                break
            remaining -= size
        if cap is None:
            return prepared
        return [
            prepare(path, max_bytes=min(cap, self.max_bytes), max_side=self.max_side,
                    allow_original=False, formats=self.original_formats)
            if len(item.data) > cap else item
            for path, item in zip(post.images, prepared)
        ]

    # -------------------------------------------------------------- interface
    async def test(self, cfg: dict) -> str:
        """Check the saved settings and describe the connected account."""
        raise NotImplementedError

    async def send(self, post: Post, cfg: dict, text: str, images: list[Prepared]) -> tuple[str, str]:
        """Post for real and return (public URL, a note for the user or "")."""
        raise NotImplementedError

    async def publish(self, post: Post, cfg: dict, dry_run: bool) -> Result:
        """Publish (or just preview) one post on this site. Never raises."""
        try:
            problem = self.check(post) or self.card_problem(post, cfg)
            if problem:
                return Result(ok=False, error=problem)

            # Sites take a limited number of images: send the first few and say so.
            trimmed = ""
            if len(post.images) > self.max_images:
                trimmed = (f"the first {self.max_images} of {len(post.images)} images "
                           f"({self.label}'s limit)")
                post = replace(post, images=post.images[: self.max_images])

            # The text depends on the card's caption style (see core/settings.py).
            text = self.build_text(post, cfg)
            images = self.prepare_images(post)
            missing = self.missing(cfg)

            if dry_run:
                preview = {
                    "text": text,
                    "images": [
                        {
                            "name": image.filename,
                            "kb": image.size_kb,
                            "width": image.width,
                            "height": image.height,
                            "mime": image.mime,
                        }
                        for image in images
                    ],
                    "ready": not missing,
                    "missing": missing,
                }
                if trimmed:
                    preview["note"] = trimmed
                preview.update(self.preview_extra(post, cfg) or {})
                return Result(ok=True, preview=preview)

            if missing:
                return Result(ok=False, error="Not set up yet — missing: " + ", ".join(missing))

            url, send_note = await self.send(post, cfg, text, images)
            note = " — ".join(piece for piece in (trimmed, send_note) if piece)
            return Result(ok=True, url=url or None, note=note or None)
        except PublishError as exc:
            return Result(ok=False, error=scrub(exc, cfg, self))
        except Exception as exc:  # noqa: BLE001 — one site must never break the others
            log.exception("unexpected failure in %s", self.id)
            return Result(ok=False, error=scrub(f"{type(exc).__name__}: {exc}", cfg, self))
