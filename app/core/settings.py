"""Small non-secret settings that must survive restarts (caption styles).

Kept in data/settings.json — never for secrets, which live in the keyring.
"""
from __future__ import annotations

import json

from . import paths

VALID_STYLES = ("ai", "name_series", "none")
DEFAULT_STYLE = "ai"


def _file():
    return paths.DATA / "settings.json"


def load() -> dict:
    try:
        data = json.loads(_file().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    paths.ensure_dirs()
    _file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def stored_styles() -> dict:
    styles = load().get("caption_styles")
    return dict(styles) if isinstance(styles, dict) else {}


def style_for(platform_id: str, default: str = DEFAULT_STYLE) -> str:
    """The saved caption style for a site, or its default."""
    value = stored_styles().get(platform_id, "")
    return value if value in VALID_STYLES else default


def set_styles(styles: dict) -> dict:
    """Merge valid style choices into settings.json and return what is stored."""
    stored = stored_styles()
    for key, value in (styles or {}).items():
        if value in VALID_STYLES:
            stored[str(key)] = value
    data = load()
    data["caption_styles"] = stored
    save(data)
    return stored
