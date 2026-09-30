"""The Post model, tag parsing and caption building."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SPLIT_RE = re.compile(r"[,\s]+")
_TITLE_TRIM = " -–—:,;.!?·•'\"“”‘’"


def clip_title(value: str, limit: int = 50) -> str:
    """A title cut at a word boundary, with no dangling punctuation."""
    value = (value or "").strip()
    if len(value) > limit:
        head = value[:limit]
        if value[limit] not in " " and " " in head:
            head = head[: head.rfind(" ")]
        value = head
    return value.rstrip(_TITLE_TRIM).strip()


@dataclass
class Post:
    images: list[Path]
    caption: str = ""
    tags: list[str] = field(default_factory=list)
    alt_text: str = ""
    options: dict = field(default_factory=dict)
    character: str = ""
    series: str = ""

    def caption_for(self, platform_id: str) -> str:
        """The caption to use for one site: its override, or the shared caption."""
        options = self.options.get(platform_id) or {}
        override = str(options.get("caption") or "").strip()
        return override or self.caption


def parse_tags(raw) -> list[str]:
    """Normalise tags from a string, a list, or a mix. Keeps order, drops duplicates."""
    if raw is None:
        items: list[str] = []
    elif isinstance(raw, str):
        items = _SPLIT_RE.split(raw)
    elif isinstance(raw, (list, tuple, set)):
        items = []
        for entry in raw:
            items.extend(_SPLIT_RE.split(str(entry)))
    else:
        items = _SPLIT_RE.split(str(raw))

    tags: list[str] = []
    seen: set[str] = set()
    for item in items:
        tag = (item or "").strip().lstrip("#")
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        tags.append(tag)
    return tags


def build_caption(caption: str, tags: list[str], limit: int) -> str:
    """Caption plus a blank line plus "#tag #tag", fitted to `limit`.

    Tags are dropped from the end first; the caption itself is only cut
    (ending with an ellipsis) when it is too long on its own.
    """
    caption = (caption or "").strip()
    tags = [tag for tag in (tags or []) if tag]
    if limit and len(caption) > limit:
        caption = caption[: max(0, limit - 1)].rstrip() + "…"

    tag_line = " ".join("#" + tag for tag in tags)
    if not tag_line:
        return caption

    if caption:
        while tags and len(caption) + 2 + len(tag_line) > limit:
            tags = tags[:-1]
            tag_line = " ".join("#" + tag for tag in tags)
        return caption + ("\n\n" + tag_line if tag_line else "")

    while tags and len(tag_line) > limit:
        tags = tags[:-1]
        tag_line = " ".join("#" + tag for tag in tags)
    return tag_line
